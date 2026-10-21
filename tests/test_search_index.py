"""Tests for the local FTS5 chat search index (worker/search)."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from worker.pipeline.config_store import load_config
from worker.search import index as search_index
from worker.search.actions import handle_search_action
from worker.search.sources import resolve_paths


def _ts(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())


def wecom_record(
    *,
    message_id: int,
    sender: str,
    text: str,
    send_time: int,
    conversation_id: str = "g1",
    conversation_name: str = "测试群",
    kind: str = "文本",
    content_type: int = 1,
) -> dict:
    time_text = datetime.fromtimestamp(send_time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "date": time_text.split(" ")[0],
        "message_time": time_text,
        "sender": sender,
        "sender_id": f"u-{sender}",
        "raw_text": f"{sender} {time_text} [{kind}] {text}",
        "dedupe_key": f"{conversation_id}:{message_id}:0:{send_time}",
        "conversation_id": conversation_id,
        "conversation_name": conversation_name,
        "message_id": message_id,
        "server_id": 0,
        "content_type": content_type,
        "type": kind,
        "send_time": send_time,
    }


class SearchIndexTestCase(unittest.TestCase):
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

    def _write_config(self, extra: dict | None = None) -> None:
        config = {
            "config_version": 4,
            "default_workspace": str(self.workspace),
            "timezone": "Asia/Shanghai",
            "llm": {"base_url": "", "model": "", "api_key": ""},
        }
        config.update(extra or {})
        self.config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def _write_wecom(self, records: list[dict], date_dir: str = "2026-10-20") -> Path:
        directory = self.workspace / "groups" / "g1" / "work" / date_dir
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "raw_messages.jsonl"
        with path.open("w", encoding="utf-8") as file:
            for record in records:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path

    def _append_wecom(self, path: Path, record: dict) -> None:
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _create_wechat(self, messages: list[dict]) -> Path:
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
        return path

    def _config(self) -> dict:
        config, _path = load_config(self.config_path)
        return config

    def _paths(self):
        return resolve_paths(self._config())

    def _build(self) -> dict:
        return search_index.rebuild(self._paths())

    def _seed_basic_messages(self) -> None:
        self._write_wecom(
            [
                wecom_record(
                    message_id=1,
                    sender="张三",
                    text="登录失败，提示网络异常",
                    send_time=_ts("2026-10-20T10:00:00"),
                ),
                wecom_record(
                    message_id=2,
                    sender="李四",
                    text="麻烦你明天下午前把周报发我",
                    send_time=_ts("2026-10-20T11:00:00"),
                ),
            ]
        )
        self._create_wechat(
            [
                {
                    "project_id": "p1",
                    "source_key": "s1",
                    "create_time": _ts("2026-10-20T12:00:00"),
                    "sender_name": "王五",
                    "direction": "incoming",
                    "kind": "text",
                    "text": "login failed with network error",
                }
            ]
        )


class IndexLifecycleTests(SearchIndexTestCase):
    def test_rebuild_indexes_both_sources(self):
        self._seed_basic_messages()

        result = self._build()

        self.assertEqual(result["indexed"], 3)
        status = handle_search_action("status", {})
        self.assertEqual(status["indexed"], 3)
        self.assertEqual(status["total"], 3)
        self.assertFalse(status["building"])
        self.assertEqual(status["error"], "")
        self.assertTrue(status["lastIndexedAt"])

    def test_rebuild_is_idempotent(self):
        self._seed_basic_messages()
        self._build()

        second = handle_search_action("rebuild", {})
        self.assertEqual(second, {"started": True})
        third = handle_search_action("rebuild", {})

        self.assertEqual(second, third)
        status = handle_search_action("status", {})
        self.assertEqual(status["indexed"], 3)

    def test_empty_sources_return_empty_results(self):
        status = handle_search_action("status", {})
        self.assertEqual(status["indexed"], 0)
        self.assertEqual(status["error"], "")

        result = handle_search_action("query", {"query": "任何内容"})
        self.assertEqual(result, {"items": [], "total": 0})

    def test_missing_wechat_tables_are_tolerated(self):
        path = self.root / "wechat.sqlite3"
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE unrelated (id TEXT)")
            connection.commit()
        finally:
            connection.close()
        self._write_wecom(
            [
                wecom_record(
                    message_id=1,
                    sender="张三",
                    text="只有企微数据",
                    send_time=_ts("2026-10-20T10:00:00"),
                )
            ]
        )

        result = self._build()

        self.assertEqual(result["indexed"], 1)

    def test_corrupt_index_reports_status_and_falls_back_to_sources(self):
        self._seed_basic_messages()
        self._build()
        self.assertEqual(handle_search_action("status", {})["indexed"], 3)
        self._paths().index_path.write_bytes(b"this is not a sqlite database")

        status = handle_search_action("status", {})
        self.assertNotEqual(status["error"], "")
        self.assertIn("索引", status["error"])

        fallback = handle_search_action("query", {"query": "登录失败"})
        self.assertEqual(fallback["total"], 1)

        rebuild = handle_search_action("rebuild", {})
        self.assertEqual(rebuild, {"started": True})
        self.assertEqual(handle_search_action("status", {})["indexed"], 3)


class QueryTests(SearchIndexTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._seed_basic_messages()
        self._build()

    def test_cjk_and_latin_queries_hit(self):
        cjk = handle_search_action("query", {"query": "登录失败"})
        self.assertEqual(cjk["total"], 1)
        item = cjk["items"][0]
        self.assertEqual(item["source"], "wecom")
        self.assertEqual(item["convName"], "测试群")
        self.assertEqual(item["senderName"], "张三")
        self.assertEqual(item["text"], "登录失败，提示网络异常")
        self.assertIn("msgId", item)

        latin = handle_search_action("query", {"query": "login network"})
        self.assertEqual(latin["total"], 1)
        self.assertEqual(latin["items"][0]["source"], "wechat")

    def test_filters_by_sender_source_kind_and_time(self):
        by_sender = handle_search_action("query", {"sender": "李四"})
        self.assertEqual(by_sender["total"], 1)
        self.assertIn("周报", by_sender["items"][0]["text"])

        by_source = handle_search_action("query", {"source": "wechat"})
        self.assertEqual(by_source["total"], 1)
        self.assertEqual(by_source["items"][0]["source"], "wechat")

        by_kind = handle_search_action("query", {"kinds": ["text"]})
        self.assertEqual(by_kind["total"], 3)
        by_kind_none = handle_search_action("query", {"kinds": ["image"]})
        self.assertEqual(by_kind_none["total"], 0)

        by_date = handle_search_action(
            "query", {"from": "2026-10-20", "to": "2026-10-20"}
        )
        self.assertEqual(by_date["total"], 3)
        outside = handle_search_action(
            "query", {"from": "2026-10-21", "to": "2026-10-21"}
        )
        self.assertEqual(outside["total"], 0)

    def test_limit_and_offset(self):
        first = handle_search_action("query", {"limit": 1})
        self.assertEqual(first["total"], 3)
        self.assertEqual(len(first["items"]), 1)
        second = handle_search_action("query", {"limit": 1, "offset": 1})
        self.assertEqual(len(second["items"]), 1)
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])

    def test_special_characters_do_not_crash(self):
        for query in ['"*()', "登录 OR 失败", "a%b_c", "  "]:
            result = handle_search_action("query", {"query": query})
            self.assertIn("items", result)
            self.assertIn("total", result)

    def test_context_returns_neighbours_in_time_order(self):
        items = handle_search_action("query", {"sender": "李四"})["items"]
        target = items[0]

        context = handle_search_action("context", {"id": target["id"], "radius": 1})

        texts = [item["text"] for item in context["messages"]]
        self.assertEqual(len(texts), 2)  # one wecom neighbour each side
        self.assertEqual(context["messages"][1]["id"], target["id"])
        self.assertEqual(texts, ["登录失败，提示网络异常", "麻烦你明天下午前把周报发我"])
        self.assertEqual(context["messages"][0]["direction"], "")

    def test_context_unknown_id_returns_empty(self):
        result = handle_search_action("context", {"id": "missing:id"})
        self.assertEqual(result, {"messages": []})

    def test_unknown_action_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "未知的搜索动作"):
            handle_search_action("nope", {})

    def test_results_are_json_serializable(self):
        status = handle_search_action("status", {})
        query = handle_search_action("query", {"query": "登录"})
        context = handle_search_action("context", {"id": query["items"][0]["id"]})
        json.dumps(status, ensure_ascii=False)
        json.dumps(query, ensure_ascii=False)
        json.dumps(context, ensure_ascii=False)


class IncrementalTests(SearchIndexTestCase):
    def test_query_picks_up_new_wecom_and_wechat_messages(self):
        self._seed_basic_messages()
        self._build()
        self.assertEqual(handle_search_action("status", {})["indexed"], 3)

        wecom_path = self.workspace / "groups" / "g1" / "work" / "2026-10-20" / "raw_messages.jsonl"
        self._append_wecom(
            wecom_path,
            wecom_record(
                message_id=3,
                sender="赵六",
                text="新功能已经上线了",
                send_time=_ts("2026-10-20T13:00:00"),
            ),
        )
        connection = sqlite3.connect(self.root / "wechat.sqlite3")
        try:
            connection.execute(
                "INSERT INTO messages (project_id,source_key,create_time,"
                "sender_name,direction,kind,text) VALUES (?,?,?,?,?,?,?)",
                ("p1", "s2", _ts("2026-10-20T14:00:00"), "王五", "incoming", "text", "微信新消息"),
            )
            connection.commit()
        finally:
            connection.close()

        result = handle_search_action("query", {"query": "新功能"})
        self.assertEqual(result["total"], 1)
        status = handle_search_action("status", {})
        self.assertEqual(status["indexed"], 5)

    def test_rebuild_after_incremental_keeps_counts(self):
        self._seed_basic_messages()
        self._build()
        self._append_wecom(
            self.workspace / "groups" / "g1" / "work" / "2026-10-20" / "raw_messages.jsonl",
            wecom_record(
                message_id=9,
                sender="张三",
                text="补充消息",
                send_time=_ts("2026-10-20T15:00:00"),
            ),
        )
        handle_search_action("status", {})

        handle_search_action("rebuild", {})

        self.assertEqual(handle_search_action("status", {})["indexed"], 4)
        self.assertEqual(handle_search_action("query", {"query": "补充"})["total"], 1)
