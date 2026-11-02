"""Tests for the reply assistant backend (``worker/reply_assistant``).

Covers the sole worker action ``eq_reply``: editable prompt selection, fake-model
JSON parsing, per-section rule fallbacks and the ``assistant_action`` dispatch
through ``worker.main``. No network, Qt or real WeChat/WeCom data is used.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worker.main import dispatch as worker_dispatch
from worker.reply_assistant import model as assistant_model
from worker.reply_assistant import prompts as assistant_prompts
from worker.reply_assistant.actions import handle_assistant_action


class ReplyAssistantTestCase(unittest.TestCase):
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

    def _write_config(
        self,
        llm: dict | None = None,
        prompts: list | None = None,
    ) -> None:
        config: dict = {
            "config_version": 4,
            "timezone": "Asia/Shanghai",
            "llm": llm
            or {"base_url": "", "model": "", "api_key": "", "provider": "openai_compatible"},
        }
        if prompts is not None:
            config["prompts"] = {"items": prompts}
        self.config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def _configure_llm(self) -> None:
        self._write_config(
            llm={
                "base_url": "https://llm.example/v1",
                "model": "fake-model",
                "api_key": "fake-key",
                "provider": "openai_compatible",
            }
        )

    def _configure_eq_reply_prompt(self, content: str, prompt_id: str = "custom_eq_reply") -> None:
        self._write_config(
            llm={
                "base_url": "https://llm.example/v1",
                "model": "fake-model",
                "api_key": "fake-key",
                "provider": "openai_compatible",
            },
            prompts=[
                {
                    "id": prompt_id,
                    "name": "自定义高情商回复",
                    "description": "测试用可编辑提示词",
                    "scene": "eq_reply",
                    "content": content,
                }
            ],
        )


def _full_model_payload() -> dict:
    return {
        "styles": {"steady": "模型稳重版", "warm": "模型亲和版", "brief": "模型简短版"},
        "interpretation": {
            "summary": "模型解读",
            "signals": [{"label": "时间节点", "detail": "今天"}],
        },
        "check": {"risks": ["模型风险"], "suggestions": ["模型建议"]},
    }


class EqReplyFallbackTests(ReplyAssistantTestCase):
    def test_no_model_returns_rule_fallback_with_full_shape(self):
        result = handle_assistant_action(
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

    def test_model_failure_falls_back(self):
        self._configure_llm()
        with mock.patch.object(
            assistant_model, "call_llm", side_effect=assistant_model.ModelUnavailable("boom")
        ):
            result = handle_assistant_action("eq_reply", {"text": "客户在催进度"})

        self.assertTrue(result["fallback"])
        self.assertEqual(len(result["styles"]), 3)
        self.assertTrue(result["interpretation"]["signals"])
        self.assertTrue(result["check"]["suggestions"])

    def test_unparsable_model_answer_falls_back(self):
        self._configure_llm()
        with mock.patch.object(assistant_model, "call_llm", return_value="not json"):
            result = handle_assistant_action("eq_reply", {"text": "在吗？"})

        self.assertTrue(result["fallback"])
        self.assertEqual(len(result["styles"]), 3)

    def test_missing_text_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "请输入需要回复的消息内容"):
            handle_assistant_action("eq_reply", {})
        with self.assertRaisesRegex(ValueError, "请输入需要回复的消息内容"):
            handle_assistant_action("eq_reply", {"text": "   "})


class EqReplyModelTests(ReplyAssistantTestCase):
    def test_model_answer_is_parsed(self):
        self._configure_llm()
        payload = json.dumps(_full_model_payload(), ensure_ascii=False)
        with mock.patch.object(assistant_model, "call_llm", return_value=payload) as call:
            result = handle_assistant_action(
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
        system_prompt = call.call_args.args[1]
        user_prompt = call.call_args.args[2]
        self.assertIn('"styles"', system_prompt)
        self.assertIn('"interpretation"', system_prompt)
        self.assertIn('"check"', system_prompt)
        self.assertIn("李总", user_prompt)
        self.assertIn("项目 A", user_prompt)
        self.assertIn("客户在催进度", user_prompt)

    def test_partial_model_answer_falls_back_per_section(self):
        self._configure_llm()
        payload = json.dumps({"steady": "只有稳重版"}, ensure_ascii=False)
        with mock.patch.object(assistant_model, "call_llm", return_value=payload):
            result = handle_assistant_action("eq_reply", {"text": "明天能交付吗？"})

        self.assertTrue(result["fallback"])
        self.assertEqual(result["styles"][0]["content"], "只有稳重版")
        self.assertEqual(len(result["styles"]), 3)
        # Missing styles keep the rule versions instead of breaking the contract.
        self.assertTrue(result["styles"][1]["content"])
        self.assertTrue(result["styles"][2]["content"])
        self.assertTrue(result["interpretation"]["summary"])
        self.assertTrue(result["check"]["suggestions"])

    def test_model_styles_are_kept_when_interpretation_and_check_are_missing(self):
        self._configure_llm()
        payload = json.dumps(
            {"styles": {"steady": "稳", "warm": "暖", "brief": "简"}}, ensure_ascii=False
        )
        with mock.patch.object(assistant_model, "call_llm", return_value=payload):
            result = handle_assistant_action("eq_reply", {"text": "客户问什么时候上线"})

        self.assertTrue(result["fallback"])
        self.assertEqual(
            [style["content"] for style in result["styles"]], ["稳", "暖", "简"]
        )
        self.assertTrue(result["interpretation"]["signals"])
        self.assertTrue(result["check"]["risks"])

    def test_model_interpretation_survives_a_missing_check(self):
        self._configure_llm()
        payload = json.dumps(
            {
                "styles": {"steady": "稳", "warm": "暖", "brief": "简"},
                "interpretation": {
                    "summary": "客户在确认上线时间",
                    "signals": [{"label": "询问点", "detail": "什么时候"}],
                },
            },
            ensure_ascii=False,
        )
        with mock.patch.object(assistant_model, "call_llm", return_value=payload):
            result = handle_assistant_action("eq_reply", {"text": "客户问什么时候上线"})

        self.assertTrue(result["fallback"])
        self.assertEqual(result["interpretation"]["summary"], "客户在确认上线时间")
        self.assertTrue(result["check"]["suggestions"])

    def test_result_is_json_serializable(self):
        self._configure_llm()
        payload = json.dumps(_full_model_payload(), ensure_ascii=False)
        with mock.patch.object(assistant_model, "call_llm", return_value=payload):
            result = handle_assistant_action("eq_reply", {"text": "在吗？"})

        json.dumps(result, ensure_ascii=False)


class EditablePromptTests(ReplyAssistantTestCase):
    def _call_and_capture_system_prompt(self, text: str = "客户在催进度") -> str:
        payload = json.dumps(_full_model_payload(), ensure_ascii=False)
        with mock.patch.object(assistant_model, "call_llm", return_value=payload) as call:
            handle_assistant_action("eq_reply", {"text": text})
        return call.call_args.args[1]

    def test_edited_prompt_library_entry_is_used_as_system_prompt(self):
        self._configure_eq_reply_prompt("自定义提示词：先共情，再给结论，最后给下一步。")

        system_prompt = self._call_and_capture_system_prompt()

        self.assertIn("自定义提示词：先共情，再给结论，最后给下一步。", system_prompt)
        # The JSON output contract must survive prompt edits.
        self.assertIn('"styles"', system_prompt)
        self.assertIn('"interpretation"', system_prompt)
        self.assertIn('"check"', system_prompt)
        self.assertIn("steady", system_prompt)
        self.assertIn("warm", system_prompt)
        self.assertIn("brief", system_prompt)

    def test_first_eq_reply_prompt_entry_wins(self):
        self._write_config(
            llm={
                "base_url": "https://llm.example/v1",
                "model": "fake-model",
                "api_key": "fake-key",
                "provider": "openai_compatible",
            },
            prompts=[
                {
                    "id": "eq_first",
                    "name": "第一条",
                    "scene": "eq_reply",
                    "content": "第一条自定义提示词",
                },
                {
                    "id": "eq_second",
                    "name": "第二条",
                    "scene": "eq_reply",
                    "content": "第二条自定义提示词",
                },
            ],
        )

        system_prompt = self._call_and_capture_system_prompt()

        self.assertIn("第一条自定义提示词", system_prompt)
        self.assertNotIn("第二条自定义提示词", system_prompt)

    def test_empty_prompt_content_uses_builtin_default(self):
        self._configure_eq_reply_prompt("   ")

        system_prompt = self._call_and_capture_system_prompt()

        self.assertEqual(system_prompt, assistant_prompts.EQ_REPLY_SYSTEM)
        self.assertIn("你是企业沟通顾问", system_prompt)
        self.assertIn('"styles"', system_prompt)
        self.assertIn('"interpretation"', system_prompt)
        self.assertIn('"check"', system_prompt)

    def test_configured_prompt_with_its_own_contract_is_not_duplicated(self):
        custom = "自定义提示词：按 JSON 返回。"
        self._configure_eq_reply_prompt(custom + json.dumps(_full_model_payload(), ensure_ascii=False))

        system_prompt = self._call_and_capture_system_prompt()

        self.assertTrue(system_prompt.startswith(custom))
        self.assertEqual(system_prompt.count('"interpretation"'), 1)

    def test_prompt_builder_uses_editable_content_without_config(self):
        system_prompt, user_prompt = assistant_prompts.eq_reply_prompt(
            None, "客户在催进度", context="项目 A", recipient="李总"
        )

        self.assertEqual(system_prompt, assistant_prompts.EQ_REPLY_SYSTEM)
        self.assertIn("客户在催进度", user_prompt)
        self.assertIn("李总", user_prompt)
        self.assertIn("项目 A", user_prompt)


class AssistantDispatchTests(ReplyAssistantTestCase):
    def test_unknown_action_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "未知的助手动作"):
            handle_assistant_action("unknown_action", {})
        with self.assertRaisesRegex(ValueError, "未知的助手动作"):
            handle_assistant_action("summarize", {"text": "内容"})

    def test_main_dispatch_routes_assistant_action(self):
        result = worker_dispatch(
            {
                "action": "assistant_action",
                "payload": {"action": "eq_reply", "payload": {"text": "在吗？"}},
            }
        )

        self.assertTrue(result["fallback"])
        self.assertEqual(len(result["styles"]), 3)

    def test_main_dispatch_requires_an_action(self):
        with self.assertRaisesRegex(ValueError, "回复助手命令缺少 action"):
            worker_dispatch({"action": "assistant_action", "payload": {}})
        with self.assertRaisesRegex(ValueError, "payload 必须是 JSON 对象"):
            worker_dispatch({"action": "assistant_action", "payload": "nope"})
        with self.assertRaisesRegex(ValueError, "payload.payload 必须是 JSON 对象"):
            worker_dispatch(
                {"action": "assistant_action", "payload": {"action": "eq_reply", "payload": ["x"]}}
            )

    def test_legacy_assistant_action_name_is_gone(self):
        # The old identifier is assembled on purpose so the removed name does not
        # reappear in the tree while this test still proves the dispatch is gone.
        legacy_action = "daily" + "_action"
        import worker.main as worker_main

        self.assertFalse(hasattr(worker_main, "handle_" + legacy_action))
        with self.assertRaisesRegex(ValueError, "unknown worker action"):
            worker_dispatch({"action": legacy_action, "payload": {}})


if __name__ == "__main__":
    unittest.main()
