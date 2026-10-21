"""Tests for the daily assistant backend (worker/daily)."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from worker.daily import model as daily_model
from worker.daily.actions import handle_daily_action
from worker.pipeline.config_store import load_config
from worker.search import index as search_index
from worker.search.sources import resolve_paths

from tests.test_search_index import wecom_record

TZ = ZoneInfo("Asia/Shanghai")


def _now() -> int:
    return int(time.time())


def _today_range() -> tuple[str, str]:
    today = datetime.now(tz=TZ).date()
    return (today - timedelta(days=1)).isoformat(), today.isoformat()


class DailyActionsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.root = Path(self._temporary_directory.name)
        self.workspace = self.root / "workspace"
        self.config_path = self.root / "config.local.json"
        self._write_config()
        self.env_patch = mock.patch.dict(
            os.environ, {"WECOM_ISSUE_RADAR_CONFIG": str(self.config_path)}
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _write_config(self, llm: dict | None = None) -> None:
        config = {
            "config_version": 4,
            "default_workspace": str(self.workspace),
            "timezone": "Asia/Shanghai",
            "llm": llm
            or {"base_url": "", "model": "", "api_key": "", "provider": "openai_compatible"},
        }
        self.config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def _config(self) -> dict:
        config, _path = load_config(self.config_path)
        return config

    def _paths(self):
        return resolve_paths(self._config())

    def _build(self) -> dict:
        return search_index.rebuild(self._paths())

    def _write_wecom(self, records: list[dict]) -> Path:
        directory = self.workspace / "groups" / "g1" / "work" / "2026-10-20"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "raw_messages.jsonl"
        with path.open("w", encoding="utf-8") as file:
            for record in records:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path

    def _create_wechat(self, messages: list[dict]) -> None:
        path = self.root / "wechat.sqlite3"
        connection = sqlite3.connect(path)
        try:
            connection.executescript(
                """
                CREATE TABLE projects (
                    id TEXT PRIMARY KEY, account_id TEXT, chat_id TEXT,
                    chat_title TEXT, name TEXT, kind TEXT
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT,
                    source_key TEXT, source_db TEXT, local_id INTEGER,
                    server_id INTEGER, sort_seq INTEGER, create_time INTEGER,
                    sender_id TEXT, sender_name TEXT, direction TEXT, kind TEXT,
                    text TEXT
                );
                """
            )
            connection.execute(
                "INSERT INTO projects VALUES ('p1','acc','chat','客户群','客户群','group')"
            )
            for message in messages:
                connection.execute(
                    "INSERT INTO messages (project_id,source_key,create_time,"
                    "sender_name,direction,kind,text) VALUES (?,?,?,?,?,?,?)",
                    (
                        message["project_id"],
                        message["source_key"],
                        message["create_time"],
                        message.get("sender_name", ""),
                        message.get("direction", "incoming"),
                        message.get("kind", "text"),
                        message.get("text", ""),
                    ),
                )
            connection.commit()
        finally:
            connection.close()

    def _seed_weekly_messages(self) -> None:
        now = _now()
        self._write_wecom(
            [
                wecom_record(
                    message_id=1,
                    sender="张三",
                    text="登录问题已修复并发布",
                    send_time=now - 900,
                ),
                wecom_record(
                    message_id=2,
                    sender="李四",
                    text="周报里补充一下客户反馈",
                    send_time=now - 600,
                ),
            ]
        )
        self._create_wechat(
            [
                {
                    "project_id": "p1",
                    "source_key": "s1",
                    "create_time": now - 300,
                    "sender_name": "王五",
                    "direction": "incoming",
                    "kind": "text",
                    "text": "这周客户主要反馈登录速度",
                }
            ]
        )

    def _seed_todo_messages(self) -> None:
        now = _now()
        self._write_wecom(
            [
                wecom_record(
                    message_id=1,
                    sender="李四",
                    text="麻烦你明天下午前把周报发我",
                    send_time=now - 1800,
                )
            ]
        )
        self._create_wechat(
            [
                {
                    "project_id": "p1",
                    "source_key": "s1",
                    "create_time": now - 1700,
                    "sender_name": "我",
                    "direction": "outgoing",
                    "kind": "text",
                    "text": "我看看",
                }
            ]
        )


class EqReplyTests(DailyActionsTestCase):
    def test_static_fallback_without_model(self):
        result = handle_daily_action("eq_reply", {"text": "这个功能什么时候上线？"})

        self.assertTrue(result["fallback"])
        self.assertEqual([style["id"] for style in result["styles"]], ["steady", "warm", "brief"])
        for style in result["styles"]:
            self.assertTrue(style["title"])
            self.assertTrue(style["content"])
        json.dumps(result, ensure_ascii=False)

    def test_model_failure_falls_back(self):
        self._write_config(
            llm={
                "base_url": "https://llm.example/v1",
                "api_key": "key",
                "model": "model",
                "provider": "openai_compatible",
            }
        )
        with mock.patch.object(
            daily_model, "call_llm", side_effect=daily_model.ModelUnavailable("boom")
        ):
            result = handle_daily_action("eq_reply", {"text": "客户在催进度"})

        self.assertTrue(result["fallback"])
        self.assertEqual(len(result["styles"]), 3)

    def test_model_answer_is_parsed(self):
        self._write_config(
            llm={
                "base_url": "https://llm.example/v1",
                "api_key": "key",
                "model": "model",
                "provider": "openai_compatible",
            }
        )
        payload = json.dumps({"steady": "稳重草稿", "warm": "亲和草稿", "brief": "简短草稿"})
        with mock.patch.object(daily_model, "call_llm", return_value=payload):
            result = handle_daily_action(
                "eq_reply", {"text": "客户在催进度", "context": "项目 A"}
            )

        self.assertFalse(result["fallback"])
        self.assertEqual(
            [style["content"] for style in result["styles"]],
            ["稳重草稿", "亲和草稿", "简短草稿"],
        )

    def test_missing_text_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "请输入需要回复的消息内容"):
            handle_daily_action("eq_reply", {})


class WeeklyTests(DailyActionsTestCase):
    def test_weekly_returns_fallback_draft_and_stats(self):
        self._seed_weekly_messages()
        start, end = _today_range()

        result = handle_daily_action(
            "weekly", {"startDate": start, "endDate": end}
        )

        self.assertTrue(result["fallback"])
        self.assertEqual(result["stats"], {"messages": 3, "conversations": 2})
        self.assertIn("周报", result["draft"])
        self.assertIn(start, result["draft"])
        json.dumps(result, ensure_ascii=False)

    def test_weekly_filters_by_source(self):
        self._seed_weekly_messages()
        start, end = _today_range()

        result = handle_daily_action(
            "weekly", {"startDate": start, "endDate": end, "sources": ["wechat"]}
        )

        self.assertEqual(result["stats"], {"messages": 1, "conversations": 1})
        self.assertTrue(result["fallback"])

    def test_weekly_empty_range_prompts_for_another_range(self):
        self._seed_weekly_messages()

        result = handle_daily_action(
            "weekly", {"startDate": "2000-01-01", "endDate": "2000-01-02"}
        )

        self.assertEqual(result["stats"], {"messages": 0, "conversations": 0})
        self.assertFalse(result["fallback"])
        self.assertIn("没有可汇总", result["draft"])
        self.assertIn("更换", result["draft"])

    def test_weekly_rejects_invalid_range(self):
        with self.assertRaisesRegex(ValueError, "开始日期"):
            handle_daily_action("weekly", {})
        with self.assertRaisesRegex(ValueError, "不能晚于"):
            handle_daily_action(
                "weekly", {"startDate": "2026-10-21", "endDate": "2026-10-20"}
            )


class TodoTests(DailyActionsTestCase):
    def _refresh(self) -> dict:
        return handle_daily_action("todos_refresh", {})

    def test_refresh_creates_only_high_confidence_todos(self):
        self._seed_todo_messages()

        result = self._refresh()

        self.assertEqual(result["created"], 1)
        titles = [item["title"] for item in result["items"]]
        self.assertTrue(any("周报" in title for title in titles))
        candidate_titles = [item["title"] for item in result["candidates"]]
        self.assertTrue(any("我看看" in title for title in candidate_titles))
        # Low-confidence candidates are not auto-created as todos.
        for item in result["items"]:
            self.assertNotIn("我看看", item["title"])
        self.assertTrue(all(item["confidence"] >= 0.65 for item in result["items"]))

    def test_refresh_is_incremental(self):
        self._seed_todo_messages()
        first = self._refresh()

        second = self._refresh()

        self.assertEqual(second["created"], 0)
        self.assertEqual(len(second["items"]), len(first["items"]))
        self.assertEqual(len(second["candidates"]), len(first["candidates"]))

    def test_todo_update_completes_and_filters(self):
        self._seed_todo_messages()
        todo = self._refresh()["items"][0]

        updated = handle_daily_action(
            "todo_update", {"id": todo["id"], "done": True}
        )

        self.assertTrue(updated["item"]["done"])
        active = handle_daily_action("todos", {"status": "active"})
        done = handle_daily_action("todos", {"status": "done"})
        self.assertNotIn(todo["id"], [item["id"] for item in active["items"]])
        self.assertIn(todo["id"], [item["id"] for item in done["items"]])

    def test_confirming_candidate_promotes_it_to_todo(self):
        self._seed_todo_messages()
        candidate = self._refresh()["candidates"][0]

        updated = handle_daily_action(
            "todo_update", {"id": candidate["id"], "done": False}
        )

        self.assertEqual(updated["item"]["title"], candidate["title"])
        self.assertNotIn("pending", updated["item"])
        active = handle_daily_action("todos", {"status": "active"})
        self.assertIn(candidate["title"], [item["title"] for item in active["items"]])

    def test_update_unknown_todo_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "找不到该待办"):
            handle_daily_action("todo_update", {"id": "todo_missing", "done": True})

    def test_todos_without_data_returns_empty(self):
        self.assertEqual(handle_daily_action("todos", {}), {"items": []})
        refresh = self._refresh()
        self.assertEqual(refresh["created"], 0)
        self.assertEqual(refresh["items"], [])
        self.assertEqual(refresh["candidates"], [])


class DispatchTests(DailyActionsTestCase):
    def test_unknown_action_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "未知的日常助手动作"):
            handle_daily_action("nope", {})

    def test_all_action_results_are_json_serializable(self):
        self._seed_weekly_messages()
        start, end = _today_range()
        results = [
            handle_daily_action("eq_reply", {"text": "测试消息"}),
            handle_daily_action("weekly", {"startDate": start, "endDate": end}),
            handle_daily_action("todos", {}),
            handle_daily_action("todos_refresh", {}),
        ]
        for result in results:
            json.dumps(result, ensure_ascii=False)
