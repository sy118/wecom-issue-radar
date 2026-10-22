"""Tests for the v4.2 daily assistant backend (worker/daily).

Covers the five worker actions (eq_reply / summarize / phrases_list /
phrase_save / phrase_delete), fake-model parsing, no-model rule fallbacks and
atomic phrase storage. No network, Qt or real WeChat/WeCom data is used.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worker.daily import model as daily_model
from worker.daily import phrases as daily_phrases
from worker.daily.actions import handle_daily_action
from worker.main import dispatch as worker_dispatch


class DailyActionsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.root = Path(self._temporary_directory.name)
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
            "timezone": "Asia/Shanghai",
            "llm": llm
            or {"base_url": "", "model": "", "api_key": "", "provider": "openai_compatible"},
        }
        self.config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def _configure_llm(self) -> None:
        self._write_config(
            {
                "base_url": "https://llm.example/v1",
                "model": "fake-model",
                "api_key": "fake-key",
                "provider": "openai_compatible",
            }
        )

    def _phrases_file(self) -> Path:
        return self.root / "phrases.json"

    def _read_phrases_file(self) -> dict:
        return json.loads(self._phrases_file().read_text(encoding="utf-8"))


class EqReplyTests(DailyActionsTestCase):
    def test_no_model_returns_rule_fallback_with_full_shape(self):
        result = handle_daily_action(
            "eq_reply", {"text": "客户在催上线时间，麻烦今天确认一下"}
        )

        self.assertTrue(result["fallback"])
        self.assertEqual(
            [style["id"] for style in result["styles"]], ["steady", "warm", "brief"]
        )
        for style in result["styles"]:
            self.assertTrue(style["title"])
            self.assertTrue(style["content"])
        interpretation = result["interpretation"]
        self.assertTrue(interpretation["summary"])
        self.assertIsInstance(interpretation["signals"], list)
        self.assertTrue(interpretation["signals"])
        for signal in interpretation["signals"]:
            self.assertTrue(signal["label"])
            self.assertTrue(signal["detail"])
        check = result["check"]
        self.assertTrue(check["risks"])
        self.assertTrue(check["suggestions"])
        json.dumps(result, ensure_ascii=False)

    def test_model_answer_is_parsed(self):
        self._configure_llm()
        payload = json.dumps(
            {
                "styles": {
                    "steady": "模型稳重版",
                    "warm": "模型亲和版",
                    "brief": "模型简短版",
                },
                "interpretation": {
                    "summary": "模型解读",
                    "signals": [{"label": "时间节点", "detail": "今天"}],
                },
                "check": {"risks": ["模型风险"], "suggestions": ["模型建议"]},
            },
            ensure_ascii=False,
        )
        with mock.patch.object(daily_model, "call_llm", return_value=payload) as call:
            result = handle_daily_action(
                "eq_reply",
                {"text": "客户在催进度", "context": "项目 A", "recipient": "李总"},
            )

        self.assertFalse(result["fallback"])
        self.assertEqual(
            [style["content"] for style in result["styles"]],
            ["模型稳重版", "模型亲和版", "模型简短版"],
        )
        self.assertEqual(result["interpretation"]["summary"], "模型解读")
        self.assertEqual(result["check"]["risks"], ["模型风险"])
        user_prompt = call.call_args.args[2]
        self.assertIn("李总", user_prompt)
        self.assertIn("项目 A", user_prompt)

    def test_partial_model_answer_marks_fallback_and_fills_static_styles(self):
        self._configure_llm()
        payload = json.dumps({"steady": "只有稳重版"}, ensure_ascii=False)
        with mock.patch.object(daily_model, "call_llm", return_value=payload):
            result = handle_daily_action("eq_reply", {"text": "明天能交付吗？"})

        self.assertTrue(result["fallback"])
        self.assertEqual(result["styles"][0]["content"], "只有稳重版")
        self.assertEqual(len(result["styles"]), 3)
        self.assertTrue(result["interpretation"]["summary"])
        self.assertTrue(result["check"]["suggestions"])

    def test_model_failure_falls_back(self):
        self._configure_llm()
        with mock.patch.object(
            daily_model, "call_llm", side_effect=daily_model.ModelUnavailable("boom")
        ):
            result = handle_daily_action("eq_reply", {"text": "客户在催进度"})

        self.assertTrue(result["fallback"])
        self.assertEqual(len(result["styles"]), 3)
        self.assertTrue(result["interpretation"]["signals"])

    def test_unparsable_model_answer_falls_back(self):
        self._configure_llm()
        with mock.patch.object(daily_model, "call_llm", return_value="not json"):
            result = handle_daily_action("eq_reply", {"text": "在吗？"})

        self.assertTrue(result["fallback"])
        self.assertEqual(len(result["styles"]), 3)

    def test_missing_text_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "请输入需要回复的消息内容"):
            handle_daily_action("eq_reply", {})


class SummarizeTests(DailyActionsTestCase):
    def test_digest_fallback_without_model(self):
        text = (
            "今天上线遇到两个问题：登录接口偶发超时；导出任务偶发延迟。"
            "已回滚并临时扩容，明天上午继续排查。需要产品确认监控阈值。"
        )
        result = handle_daily_action("summarize", {"text": text, "mode": "digest"})

        self.assertTrue(result["fallback"])
        self.assertTrue(result["summary"])
        self.assertTrue(result["points"])
        self.assertIsInstance(result["risks"], list)
        self.assertIsInstance(result["next"], list)
        self.assertTrue(any("超时" in point or "延迟" in point for point in result["points"]))
        json.dumps(result, ensure_ascii=False)

    def test_meeting_fallback_uses_meeting_copy(self):
        text = "会议结论：本周完成导出优化。风险：测试环境不稳定。下一步：王五负责补充回归用例。"
        result = handle_daily_action("summarize", {"text": text, "mode": "meeting"})

        self.assertTrue(result["fallback"])
        self.assertIn("会议纪要", result["summary"])
        self.assertTrue(result["points"])
        self.assertTrue(result["risks"])
        self.assertTrue(result["next"])

    def test_model_answer_is_parsed(self):
        self._configure_llm()
        payload = json.dumps(
            {
                "summary": "模型摘要",
                "points": ["要点一", "要点二"],
                "risks": ["风险一"],
                "next": ["动作一"],
            },
            ensure_ascii=False,
        )
        with mock.patch.object(daily_model, "call_llm", return_value=payload):
            result = handle_daily_action("summarize", {"text": "任意内容"})

        self.assertFalse(result["fallback"])
        self.assertEqual(
            result,
            {
                "summary": "模型摘要",
                "points": ["要点一", "要点二"],
                "risks": ["风险一"],
                "next": ["动作一"],
                "fallback": False,
            },
        )

    def test_partial_model_answer_falls_back_for_missing_sections(self):
        self._configure_llm()
        with mock.patch.object(
            daily_model, "call_llm", return_value=json.dumps({"summary": "只有摘要"})
        ):
            result = handle_daily_action("summarize", {"text": "需要明天给出结论。"})

        self.assertTrue(result["fallback"])
        self.assertTrue(result["summary"])
        self.assertTrue(result["points"])

    def test_model_failure_falls_back(self):
        self._configure_llm()
        with mock.patch.object(
            daily_model, "call_llm", side_effect=daily_model.ModelUnavailable("down")
        ):
            result = handle_daily_action("summarize", {"text": "今天完成修复并上线。"})

        self.assertTrue(result["fallback"])
        self.assertTrue(result["summary"])
        self.assertTrue(result["points"])

    def test_missing_text_and_invalid_mode_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "请输入需要整理的内容"):
            handle_daily_action("summarize", {})
        with self.assertRaisesRegex(ValueError, "digest 或 meeting"):
            handle_daily_action("summarize", {"text": "内容", "mode": "unknown"})


class PhrasesTests(DailyActionsTestCase):
    def test_defaults_are_returned_without_writing_the_store(self):
        result = handle_daily_action("phrases_list", {})

        items = result["phrases"]
        self.assertEqual(len(items), 5)
        self.assertFalse(self._phrases_file().exists())
        self.assertEqual(
            [item["title"] for item in items],
            sorted(item["title"] for item in items),
        )
        for item in items:
            self.assertTrue(item["id"])
            self.assertTrue(item["title"])
            self.assertTrue(item["category"])
            self.assertTrue(item["content"])
            self.assertIsInstance(item["variables"], list)
        self.assertIn("builtin_leave", [item["id"] for item in items])
        leave = next(item for item in items if item["id"] == "builtin_leave")
        self.assertEqual(leave["variables"], ["姓名", "事由", "日期"])
        json.dumps(result, ensure_ascii=False)

    def test_phrase_save_creates_atomic_file_and_extracts_variables(self):
        result = handle_daily_action(
            "phrase_save",
            {
                "phrase": {
                    "title": "测试话术",
                    "category": "沟通",
                    "content": "{{姓名}}，请在 {{日期}} 前反馈，谢谢。",
                }
            },
        )

        phrase = result["phrase"]
        self.assertTrue(phrase["id"].startswith("phrase_"))
        self.assertEqual(phrase["title"], "测试话术")
        self.assertEqual(phrase["variables"], ["姓名", "日期"])
        self.assertTrue(self._phrases_file().is_file())
        stored = self._read_phrases_file()
        self.assertEqual(stored["version"], 1)
        # The first write persists the built-in defaults together with the new phrase
        # so the UI list stays stable after a reload.
        self.assertEqual(len(stored["phrases"]), 6)
        self.assertIn("builtin_leave", [item["id"] for item in stored["phrases"]])
        saved = next(item for item in stored["phrases"] if item["id"] == phrase["id"])
        self.assertEqual(saved["content"], phrase["content"])
        self.assertEqual(list(self.root.glob("phrases.json.tmp")), [])

        listed = handle_daily_action("phrases_list", {})["phrases"]
        self.assertEqual(len(listed), 6)
        self.assertIn(phrase["id"], [item["id"] for item in listed])

    def test_phrase_save_updates_an_existing_phrase(self):
        created = handle_daily_action(
            "phrase_save", {"phrase": {"title": "旧标题", "content": "旧内容"}}
        )["phrase"]

        updated = handle_daily_action(
            "phrase_save",
            {
                "phrase": {
                    "id": created["id"],
                    "title": "新标题",
                    "category": "汇报",
                    "content": "新内容",
                }
            },
        )["phrase"]

        self.assertEqual(updated["id"], created["id"])
        stored = self._read_phrases_file()
        self.assertEqual(len(stored["phrases"]), 6)
        updated_stored = next(
            item for item in stored["phrases"] if item["id"] == created["id"]
        )
        self.assertEqual(updated_stored["title"], "新标题")
        self.assertEqual(updated_stored["category"], "汇报")

    def test_phrases_are_stored_sorted_by_title(self):
        for title, content in (
            ("Beta 话术", "B 内容"),
            ("Alpha 话术", "A 内容"),
        ):
            handle_daily_action(
                "phrase_save", {"phrase": {"title": title, "content": content}}
            )

        stored = self._read_phrases_file()
        self.assertEqual(
            [item["title"] for item in stored["phrases"]],
            sorted(item["title"] for item in stored["phrases"]),
        )
        custom_titles = [
            item["title"]
            for item in stored["phrases"]
            if item["title"].endswith("话术")
        ]
        self.assertEqual(custom_titles, ["Alpha 话术", "Beta 话术"])
        listed = handle_daily_action("phrases_list", {})["phrases"]
        self.assertEqual(
            [item["title"] for item in listed],
            sorted(item["title"] for item in listed),
        )

    def test_phrase_save_respects_provided_variables(self):
        phrase = handle_daily_action(
            "phrase_save",
            {
                "phrase": {
                    "title": "变量话术",
                    "content": "正文不含占位符",
                    "variables": ["x", "y", "x", ""],
                }
            },
        )["phrase"]

        self.assertEqual(phrase["variables"], ["x", "y"])

    def test_phrase_save_validates_title_and_content(self):
        with self.assertRaisesRegex(ValueError, "话术标题不能为空"):
            handle_daily_action("phrase_save", {"phrase": {"content": "内容"}})
        with self.assertRaisesRegex(ValueError, "话术内容不能为空"):
            handle_daily_action("phrase_save", {"phrase": {"title": "标题"}})
        with self.assertRaisesRegex(ValueError, "请提供要保存的话术内容"):
            handle_daily_action("phrase_save", {})

    def test_phrase_delete_is_idempotent(self):
        phrase = handle_daily_action(
            "phrase_save", {"phrase": {"title": "待删除", "content": "内容"}}
        )["phrase"]

        self.assertEqual(handle_daily_action("phrase_delete", {"id": phrase["id"]}), {"ok": True})
        self.assertEqual(handle_daily_action("phrase_delete", {"id": phrase["id"]}), {"ok": True})
        # An empty store falls back to the built-in defaults again.
        remaining = handle_daily_action("phrases_list", {})["phrases"]
        self.assertEqual(len(remaining), 5)
        self.assertNotIn(phrase["id"], [item["id"] for item in remaining])

    def test_deleting_a_builtin_default_persists(self):
        self.assertEqual(
            handle_daily_action("phrase_delete", {"id": "builtin_leave"}), {"ok": True}
        )

        listed = handle_daily_action("phrases_list", {})["phrases"]
        self.assertEqual(len(listed), 4)
        self.assertNotIn("builtin_leave", [item["id"] for item in listed])
        self.assertTrue(self._phrases_file().is_file())

    def test_phrase_delete_requires_id(self):
        with self.assertRaisesRegex(ValueError, "话术 ID"):
            handle_daily_action("phrase_delete", {})

    def test_atomic_write_failure_keeps_the_previous_file(self):
        first = handle_daily_action(
            "phrase_save", {"phrase": {"title": "原话术", "content": "原内容"}}
        )["phrase"]
        original_bytes = self._phrases_file().read_bytes()

        with mock.patch.object(
            daily_phrases.os, "replace", side_effect=OSError("locked")
        ):
            with self.assertRaisesRegex(ValueError, "保存话术失败"):
                handle_daily_action(
                    "phrase_save",
                    {"phrase": {"id": first["id"], "title": "新话术", "content": "新内容"}},
                )

        self.assertEqual(self._phrases_file().read_bytes(), original_bytes)
        self.assertEqual(list(self.root.glob("phrases.json.tmp")), [])

    def test_corrupt_store_falls_back_to_defaults(self):
        self._phrases_file().write_text("{not json", encoding="utf-8")

        result = handle_daily_action("phrases_list", {})

        self.assertEqual(len(result["phrases"]), 5)
        self.assertIn("builtin_leave", [item["id"] for item in result["phrases"]])


class DispatchTests(DailyActionsTestCase):
    def test_unknown_action_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "未知的日常助手动作"):
            handle_daily_action("weekly", {})

    def test_main_dispatch_routes_daily_action(self):
        result = worker_dispatch(
            {
                "action": "daily_action",
                "payload": {"action": "phrases_list", "payload": {}},
            }
        )
        self.assertEqual(len(result["phrases"]), 5)

    def test_search_action_is_gone(self):
        import worker.main as worker_main

        self.assertFalse(hasattr(worker_main, "handle_search_action"))
        with self.assertRaisesRegex(ValueError, "unknown worker action"):
            worker_dispatch({"action": "search_action", "payload": {}})

    def test_all_action_results_are_json_serializable(self):
        results = [
            handle_daily_action("eq_reply", {"text": "在吗？"}),
            handle_daily_action("summarize", {"text": "今天完成修复。"}),
            handle_daily_action("phrases_list", {}),
            handle_daily_action(
                "phrase_save", {"phrase": {"title": "序列化", "content": "内容"}}
            ),
        ]
        for result in results:
            json.dumps(result, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
