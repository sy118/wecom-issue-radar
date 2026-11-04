"""Tests for v4.6.6 per-chat prompts, generation budget, timings and fallbacks.

Covers the runtime schema migration + ``chat.prompt.set``, prompt-id resolution
with per-chat role notes, model error classification / timeouts, the bounded
generation budget with ``timings`` / ``ai`` / ``fallbackReason``, and the image
count/total-byte limits. No real WeCom database or network is used.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
import uuid
from pathlib import Path
from unittest import mock

from worker.reply_assistant import actions as assistant_actions
from worker.reply_assistant import images as assistant_images
from worker.reply_assistant import model as assistant_model
from worker.reply_assistant import prompts as assistant_prompts
from worker.reply_assistant.actions import handle_assistant_action
from worker.reply_runtime.runtime import ReplyRuntime
from worker.reply_runtime.store import RuntimeStore


def _chat(conversation_id: str, display_name: str, last_time: int, last_id: int) -> dict:
    return {
        "conversationId": conversation_id,
        "displayName": display_name,
        "lastMessageTime": last_time,
        "lastMessageId": last_id,
        "isGroup": conversation_id.startswith("R:"),
    }


DEFAULT_CHATS = [
    _chat("R:100", "研发群", 3000, 30),
    _chat("S:5_42", "李总", 2000, 20),
]


class _FakeChatSource:
    def __init__(self, chats=None):
        self.chats = [dict(chat) for chat in (chats or DEFAULT_CHATS)]
        self.invalidations = 0

    @staticmethod
    def _payload(chats) -> dict:
        return {
            "chats": [dict(chat) for chat in chats],
            "selfSenderId": "5",
            "selfSenderName": "张三",
        }

    def conversation_catalog(self, *, refresh=False):
        return self._payload(self.chats)

    def lookup_conversations(self, conversation_ids):
        wanted = {str(value) for value in conversation_ids}
        return self._payload(
            [chat for chat in self.chats if chat["conversationId"] in wanted]
        )

    def recent_conversations(self, limit=40, *, refresh=False):
        return self._payload(self.chats[:limit])

    def invalidate_caches(self):
        self.invalidations += 1


class RuntimePromptCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.database_path = Path(self._temporary.name) / "runtime.sqlite3"
        self.source = _FakeChatSource()
        self.runtime = ReplyRuntime(
            self.database_path, message_source=self.source, autostart=False
        )
        self.addCleanup(self.runtime.store.close)

    def _execute(self, body: dict, *, expected_revision: int | None = None) -> dict:
        if expected_revision is None:
            expected_revision = self.runtime.store.revision()
        return self.runtime.execute(
            {
                "protocolVersion": 1,
                "commandId": f"cmd-{uuid.uuid4()}",
                "expectedRevision": expected_revision,
                "body": body,
            }
        )

    def _add(self, conversation_id: str = "R:100") -> dict:
        return self._execute(
            {"kind": "chat.chats.add", "chats": [{"conversationId": conversation_id}]}
        )

    def _set_prompt(self, body: dict, **kwargs) -> dict:
        return self._execute({"kind": "chat.prompt.set", **body}, **kwargs)

    def _recent(self) -> dict:
        return self.runtime.query({"kind": "chat.recent"})

    def _catalog(self) -> dict:
        return self.runtime.query({"kind": "chat.catalog"})


class PromptMigrationTests(unittest.TestCase):
    def test_old_table_is_migrated_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """CREATE TABLE reply_assistant_chats (
                           conversation_id TEXT PRIMARY KEY,
                           alias TEXT NOT NULL DEFAULT '',
                           added_at REAL NOT NULL
                       )"""
                )
                connection.execute(
                    "INSERT INTO reply_assistant_chats(conversation_id, alias, added_at) "
                    "VALUES ('R:100', '研发', 1.0)"
                )
                connection.commit()
            finally:
                connection.close()

            store = RuntimeStore(path)
            try:
                columns = {
                    str(row[1])
                    for row in store.connection.execute(
                        "PRAGMA table_info(reply_assistant_chats)"
                    ).fetchall()
                }
                self.assertTrue({"prompt_id", "prompt_note"} <= columns)
                row = store.connection.execute(
                    "SELECT alias, prompt_id, prompt_note FROM reply_assistant_chats "
                    "WHERE conversation_id = 'R:100'"
                ).fetchone()
                self.assertEqual(row["alias"], "研发")
                self.assertEqual(row["prompt_id"], "")
                self.assertEqual(row["prompt_note"], "")
            finally:
                store.close()

            # Opening the already-migrated database again must stay a no-op.
            reopened = RuntimeStore(path)
            try:
                row = reopened.connection.execute(
                    "SELECT prompt_id FROM reply_assistant_chats "
                    "WHERE conversation_id = 'R:100'"
                ).fetchone()
                self.assertEqual(row["prompt_id"], "")
            finally:
                reopened.close()


class PromptCommandTests(RuntimePromptCase):
    def test_set_prompt_id_and_note_persist_to_recent_and_catalog(self):
        self._add()
        result = self._set_prompt(
            {"conversationId": "R:100", "promptId": " eq_a ", "promptNote": " 客户很急 "}
        )

        self.assertEqual(result, {"revision": self.runtime.store.revision()})
        recent = self._recent()["chats"][0]
        self.assertEqual(recent["promptId"], "eq_a")
        self.assertEqual(recent["promptNote"], "客户很急")
        catalog = self._catalog()["chats"]
        by_id = {chat["conversationId"]: chat for chat in catalog}
        self.assertEqual(by_id["R:100"]["promptId"], "eq_a")
        self.assertEqual(by_id["R:100"]["promptNote"], "客户很急")
        self.assertEqual(by_id["S:5_42"]["promptId"], "")

    def test_missing_fields_are_left_unchanged(self):
        self._add()
        self._set_prompt({"conversationId": "R:100", "promptId": "eq_a"})
        self._set_prompt({"conversationId": "R:100", "promptNote": "补充"})

        chat = self._recent()["chats"][0]
        self.assertEqual(chat["promptId"], "eq_a")
        self.assertEqual(chat["promptNote"], "补充")

        self._set_prompt({"conversationId": "R:100", "promptId": "eq_b"})
        chat = self._recent()["chats"][0]
        self.assertEqual(chat["promptId"], "eq_b")
        self.assertEqual(chat["promptNote"], "补充")

    def test_empty_string_clears_the_binding(self):
        self._add()
        self._set_prompt(
            {"conversationId": "R:100", "promptId": "eq_a", "promptNote": "补充"}
        )
        self._set_prompt({"conversationId": "R:100", "promptId": "", "promptNote": ""})

        chat = self._recent()["chats"][0]
        self.assertEqual(chat["promptId"], "")
        self.assertEqual(chat["promptNote"], "")

    def test_missing_chat_and_invalid_values_are_rejected(self):
        with self.assertRaises(Exception) as raised:
            self._set_prompt({"conversationId": "R:100", "promptId": "eq_a"})
        self.assertEqual(getattr(raised.exception, "code", ""), "INVALID_CHAT")

        self._add()
        with self.assertRaises(Exception) as raised:
            self._set_prompt({"conversationId": "R:100", "promptId": "a" * 121})
        self.assertEqual(getattr(raised.exception, "code", ""), "INVALID_CHAT")

        with self.assertRaises(Exception) as raised:
            self._set_prompt({"conversationId": "R:100", "promptNote": "a" * 501})
        self.assertEqual(getattr(raised.exception, "code", ""), "INVALID_CHAT")

        with self.assertRaises(Exception) as raised:
            self._set_prompt({"conversationId": "bad", "promptId": "eq_a"})
        self.assertEqual(getattr(raised.exception, "code", ""), "INVALID_CHAT")

    def test_limits_are_inclusive(self):
        self._add()
        self._set_prompt(
            {"conversationId": "R:100", "promptId": "a" * 120, "promptNote": "n" * 500}
        )
        chat = self._recent()["chats"][0]
        self.assertEqual(len(chat["promptId"]), 120)
        self.assertEqual(len(chat["promptNote"]), 500)

    def test_revision_conflict_is_reported(self):
        self._add()
        with self.assertRaises(Exception) as raised:
            self._set_prompt(
                {"conversationId": "R:100", "promptId": "eq_a"},
                expected_revision=0,
            )
        self.assertEqual(getattr(raised.exception, "code", ""), "REVISION_CONFLICT")


PROMPT_ITEMS = [
    {"id": "eq_a", "name": "A", "scene": "eq_reply", "content": "角色A：先共情。"},
    {"id": "eq_b", "name": "B", "scene": "eq_reply", "content": "角色B：先给结论。"},
]


class PromptResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {"prompts": {"items": [dict(item) for item in PROMPT_ITEMS]}}

    def test_prompt_id_selects_the_matching_eq_reply_entry(self):
        self.assertEqual(
            assistant_prompts.configured_eq_reply_prompt(self.config, "eq_b"),
            "角色B：先给结论。",
        )
        self.assertEqual(
            assistant_prompts.configured_eq_reply_prompt(self.config, "eq_a"),
            "角色A：先共情。",
        )

    def test_missing_prompt_id_falls_back_to_the_first_entry(self):
        used_id, content, fallback = assistant_prompts.resolve_eq_reply_prompt(
            self.config, "deleted_prompt"
        )
        self.assertEqual(used_id, "eq_a")
        self.assertEqual(content, "角色A：先共情。")
        self.assertTrue(fallback)

    def test_no_requested_prompt_uses_the_first_entry_without_fallback(self):
        used_id, content, fallback = assistant_prompts.resolve_eq_reply_prompt(
            self.config, ""
        )
        self.assertEqual(used_id, "eq_a")
        self.assertFalse(fallback)

    def test_prompt_note_is_appended_after_the_json_contract(self):
        system_prompt = assistant_prompts.eq_reply_system_prompt(
            self.config, "eq_b", "本群是客服群"
        )
        self.assertIn("角色B：先给结论。", system_prompt)
        self.assertIn('"styles"', system_prompt)
        self.assertIn('"interpretation"', system_prompt)
        self.assertIn('"check"', system_prompt)
        self.assertTrue(
            system_prompt.endswith("本群角色补充：本群是客服群")
        )

    def test_prompt_note_is_optional(self):
        system_prompt = assistant_prompts.eq_reply_system_prompt(self.config, "eq_b")
        self.assertNotIn("本群角色补充", system_prompt)

    def test_prompt_note_is_truncated_to_500_chars(self):
        system_prompt = assistant_prompts.eq_reply_system_prompt(
            self.config, "eq_b", "a" * 600
        )
        self.assertTrue(system_prompt.endswith("本群角色补充：" + "a" * 500))

    def test_duplicate_ids_are_ignored_after_the_first_match(self):
        self.config["prompts"]["items"] = [
            {"id": "eq_b", "name": "B", "scene": "eq_reply", "content": "第一条"},
            {"id": "eq_b", "name": "B2", "scene": "eq_reply", "content": "第二条"},
        ]
        self.assertEqual(
            assistant_prompts.configured_eq_reply_prompt(self.config, "eq_b"),
            "第一条",
        )


class _AssistantCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
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

    @staticmethod
    def _full_payload() -> str:
        return json.dumps(
            {
                "styles": {"steady": "稳", "warm": "暖", "brief": "简"},
                "interpretation": {
                    "summary": "解读",
                    "signals": [{"label": "询问点", "detail": "时间"}],
                },
                "check": {"risks": ["风险"], "suggestions": ["建议"]},
            },
            ensure_ascii=False,
        )


class PromptActionTests(_AssistantCase):
    def test_action_uses_bound_prompt_and_note(self):
        self._write_config(
            llm={
                "base_url": "https://llm.example/v1",
                "model": "fake-model",
                "api_key": "fake-key",
                "provider": "openai_compatible",
            },
            prompts=PROMPT_ITEMS,
        )
        with mock.patch.object(
            assistant_model, "call_llm", return_value=self._full_payload()
        ) as call:
            result = handle_assistant_action(
                "eq_reply",
                {
                    "text": "hello",
                    "promptId": "eq_b",
                    "promptNote": "本群是客服群",
                },
            )

        system_prompt = call.call_args.args[1]
        self.assertIn("角色B：先给结论。", system_prompt)
        self.assertIn("本群角色补充：本群是客服群", system_prompt)
        self.assertIn('"styles"', system_prompt)
        self.assertEqual(result["ai"]["promptId"], "eq_b")
        self.assertFalse(result["ai"]["promptFallback"])
        self.assertEqual(result["ai"]["fallbackReason"], "")

    def test_action_reports_prompt_fallback_when_the_binding_was_deleted(self):
        self._write_config(
            llm={
                "base_url": "https://llm.example/v1",
                "model": "fake-model",
                "api_key": "fake-key",
                "provider": "openai_compatible",
            },
            prompts=PROMPT_ITEMS,
        )
        with mock.patch.object(
            assistant_model, "call_llm", return_value=self._full_payload()
        ) as call:
            result = handle_assistant_action(
                "eq_reply", {"text": "hello", "promptId": "deleted_prompt"}
            )

        system_prompt = call.call_args.args[1]
        self.assertIn("角色A：先共情。", system_prompt)
        self.assertEqual(result["ai"]["promptId"], "eq_a")
        self.assertTrue(result["ai"]["promptFallback"])
        self.assertFalse(result["fallback"])


class ModelClassificationTests(unittest.TestCase):
    CONFIG = {
        "llm": {
            "provider": "openai_compatible",
            "base_url": "https://llm.example/v1",
            "model": "m",
            "timeout_seconds": 180,
        }
    }

    def _call(self, **kwargs):
        return assistant_model.call_llm(self.CONFIG, "sys", "user", **kwargs)

    def _call_with_urlopen_error(self, error) -> assistant_model.ModelUnavailable:
        with mock.patch.object(
            assistant_model.urllib.request, "urlopen", side_effect=error
        ):
            with self.assertRaises(assistant_model.ModelUnavailable) as raised:
                self._call()
        return raised.exception

    def test_explicit_timeout_overrides_and_config_is_capped(self):
        captured = []

        def fake_post(_url, _body, _headers, timeout):
            captured.append(timeout)
            return {"choices": [{"message": {"content": "ok"}}]}

        with mock.patch.object(assistant_model, "_post_json", side_effect=fake_post):
            self.assertEqual(self._call(timeout_seconds=18), "ok")
            self.assertEqual(self._call(), "ok")

        self.assertEqual(captured, [18, 45])

    def test_http_400_with_vision_hint_is_vision_unsupported(self):
        error = urllib.error.HTTPError(
            "https://x", 400, "bad", {}, io.BytesIO(b"unsupported image content")
        )
        exc = self._call_with_urlopen_error(error)
        self.assertEqual(exc.kind, "vision_unsupported")

    def test_http_4xx_and_5xx_are_classified(self):
        bad_request = urllib.error.HTTPError(
            "https://x", 400, "bad", {}, io.BytesIO(b"bad request")
        )
        self.assertEqual(self._call_with_urlopen_error(bad_request).kind, "http_4xx")
        server_error = urllib.error.HTTPError(
            "https://x", 503, "down", {}, io.BytesIO(b"overloaded")
        )
        self.assertEqual(self._call_with_urlopen_error(server_error).kind, "http_5xx")

    def test_url_errors_are_classified(self):
        timeout = urllib.error.URLError(TimeoutError("timed out"))
        self.assertEqual(self._call_with_urlopen_error(timeout).kind, "timeout")
        network = urllib.error.URLError("Name or service not known")
        self.assertEqual(
            self._call_with_urlopen_error(network).kind, "network_error"
        )

    def test_invalid_and_empty_responses_are_classified(self):
        class _Response:
            def __init__(self, payload: bytes):
                self._payload = payload

            def read(self):
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        with mock.patch.object(
            assistant_model.urllib.request,
            "urlopen",
            return_value=_Response(b"not json"),
        ):
            with self.assertRaises(assistant_model.ModelUnavailable) as raised:
                self._call()
        self.assertEqual(raised.exception.kind, "invalid_response")

        with mock.patch.object(
            assistant_model, "_post_json", return_value={"choices": []}
        ):
            with self.assertRaises(assistant_model.ModelUnavailable) as raised:
                self._call()
        self.assertEqual(raised.exception.kind, "invalid_response")

        with mock.patch.object(
            assistant_model,
            "_post_json",
            return_value={"choices": [{"message": {"content": "   "}}]},
        ):
            with self.assertRaises(assistant_model.ModelUnavailable) as raised:
                self._call()
        self.assertEqual(raised.exception.kind, "empty")

    def test_parse_json_failures_are_invalid_response(self):
        with self.assertRaises(assistant_model.ModelUnavailable) as raised:
            assistant_model.parse_json_object("not json")
        self.assertEqual(raised.exception.kind, "invalid_response")


class _Clock:
    def __init__(self, value: float = 0.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class BudgetTests(_AssistantCase):
    def setUp(self) -> None:
        super().setUp()
        self._configure_llm()

    def _call_action(self, body: dict, fake_call):
        with mock.patch.object(assistant_model, "call_llm", side_effect=fake_call):
            return handle_assistant_action("eq_reply", body)

    def test_successful_text_generation_uses_single_35s_attempt(self):
        calls = []

        def fake_call(_config, _system, _user, **kwargs):
            calls.append(kwargs)
            return self._full_payload()

        result = self._call_action({"text": "hello"}, fake_call)

        self.assertFalse(result["fallback"])
        self.assertEqual(result["ai"]["fallbackReason"], "")
        self.assertEqual(calls[0]["timeout_seconds"], 35)
        self.assertNotIn("images", calls[0])
        self.assertEqual(result["timings"]["attempts"], 1)
        self.assertGreaterEqual(result["timings"]["totalMs"], 0)
        self.assertGreaterEqual(result["timings"]["modelMs"], 0)
        self.assertGreaterEqual(result["timings"]["parseMs"], 0)
        self.assertIn("imageMs", result["timings"])

    def test_timeout_falls_back_without_a_second_call(self):
        calls = []

        def fake_call(_config, _system, _user, **kwargs):
            calls.append(kwargs)
            raise assistant_model.ModelUnavailable("timeout", "timeout")

        result = self._call_action({"text": "hello"}, fake_call)

        self.assertTrue(result["fallback"])
        self.assertEqual(result["ai"]["fallbackReason"], "timeout")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["timings"]["attempts"], 1)
        self.assertEqual(len(result["styles"]), 3)

    def test_vision_unsupported_retries_once_with_a_short_text_budget(self):
        calls = []
        image = Path("shot.png")

        def fake_call(_config, _system, _user, **kwargs):
            calls.append(kwargs)
            if kwargs.get("images"):
                raise assistant_model.ModelUnavailable(
                    "vision not supported", "vision_unsupported"
                )
            return self._full_payload()

        with mock.patch.object(
            assistant_actions.images, "resolve_message_images", return_value=[image]
        ):
            result = self._call_action(
                {"text": "hello", "conversationId": "R:1", "imageMessageIds": ["1"]},
                fake_call,
            )

        self.assertFalse(result["fallback"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["timeout_seconds"], 22)
        self.assertTrue(calls[0].get("images"))
        self.assertEqual(calls[1]["timeout_seconds"], 18)
        self.assertNotIn("images", calls[1])
        self.assertEqual(result["timings"]["attempts"], 2)
        self.assertEqual(result["images"]["attached"], 1)

    def test_vision_retry_failure_reports_the_last_reason(self):
        calls = []
        image = Path("shot.png")

        def fake_call(_config, _system, _user, **kwargs):
            calls.append(kwargs)
            if kwargs.get("images"):
                raise assistant_model.ModelUnavailable(
                    "vision not supported", "vision_unsupported"
                )
            raise assistant_model.ModelUnavailable("down", "http_5xx")

        with mock.patch.object(
            assistant_actions.images, "resolve_message_images", return_value=[image]
        ):
            result = self._call_action(
                {"text": "hello", "conversationId": "R:1", "imageMessageIds": ["1"]},
                fake_call,
            )

        self.assertTrue(result["fallback"])
        self.assertEqual(result["ai"]["fallbackReason"], "http_error")
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["timings"]["attempts"], 2)

    def test_partial_payload_is_reported_as_partial(self):
        partial = json.dumps({"steady": "只有稳重版"}, ensure_ascii=False)

        def fake_call(_config, _system, _user, **_kwargs):
            return partial

        result = self._call_action({"text": "hello"}, fake_call)

        self.assertTrue(result["fallback"])
        self.assertEqual(result["ai"]["fallbackReason"], "partial")
        self.assertEqual(result["timings"]["attempts"], 1)

    def test_empty_model_answer_is_reported_as_empty(self):
        def fake_call(_config, _system, _user, **_kwargs):
            raise assistant_model.ModelUnavailable("empty", "empty")

        result = self._call_action({"text": "hello"}, fake_call)
        self.assertTrue(result["fallback"])
        self.assertEqual(result["ai"]["fallbackReason"], "empty")

    def test_not_configured_model_skips_the_call(self):
        self._write_config(llm={"base_url": "", "model": ""})

        def fake_call(_config, _system, _user, **_kwargs):
            raise AssertionError("model must not be called when not configured")

        result = self._call_action({"text": "hello"}, fake_call)

        self.assertTrue(result["fallback"])
        self.assertEqual(result["ai"]["fallbackReason"], "not_configured")
        self.assertEqual(result["timings"]["attempts"], 0)
        self.assertEqual(result["images"]["attached"], 0)
        self.assertEqual(result["timings"]["imageMs"], 0)

    def test_budget_shrinks_the_retry_timeout_and_stops_at_45s(self):
        clock = _Clock()
        calls = []
        image = Path("shot.png")

        def fake_invoke(_config, _system, _user, *, timeout_seconds, images_for_call=None):
            calls.append(timeout_seconds)
            clock.value += 20
            return None, "vision_unsupported" if len(calls) == 1 else "timeout", 1, 0

        with mock.patch.object(
            assistant_actions.time, "perf_counter", clock
        ), mock.patch.object(
            assistant_actions.images, "resolve_message_images", return_value=[image]
        ), mock.patch.object(assistant_actions, "_invoke_model", side_effect=fake_invoke):
            result = handle_assistant_action(
                "eq_reply",
                {"text": "hello", "conversationId": "R:1", "imageMessageIds": ["1"]},
            )

        self.assertEqual(calls, [22, 18])
        self.assertTrue(result["fallback"])
        self.assertEqual(result["ai"]["fallbackReason"], "timeout")
        self.assertEqual(result["timings"]["attempts"], 2)
        self.assertLess(result["timings"]["totalMs"], 45_000)

    def test_vision_retry_is_skipped_when_budget_is_exhausted(self):
        clock = _Clock()
        calls = []
        image = Path("shot.png")

        def fake_invoke(_config, _system, _user, *, timeout_seconds, images_for_call=None):
            calls.append(timeout_seconds)
            clock.value += 41
            return None, "vision_unsupported", 1, 0

        with mock.patch.object(
            assistant_actions.time, "perf_counter", clock
        ), mock.patch.object(
            assistant_actions.images, "resolve_message_images", return_value=[image]
        ), mock.patch.object(assistant_actions, "_invoke_model", side_effect=fake_invoke):
            result = handle_assistant_action(
                "eq_reply",
                {"text": "hello", "conversationId": "R:1", "imageMessageIds": ["1"]},
            )

        self.assertEqual(calls, [22])
        self.assertEqual(result["timings"]["attempts"], 1)
        self.assertEqual(result["ai"]["fallbackReason"], "vision_unsupported")

    def test_action_reports_attached_and_skipped_image_counts(self):
        paths = [Path("a.png"), Path("b.png"), Path("c.png")]
        captured = {}

        def fake_call(_config, _system, _user, **kwargs):
            captured.update(kwargs)
            return self._full_payload()

        with mock.patch.object(
            assistant_actions.images,
            "resolve_message_images_with_meta",
            return_value={"paths": paths, "skipped": 2},
        ):
            result = self._call_action(
                {
                    "text": "hello",
                    "conversationId": "R:1",
                    "imageMessageIds": ["1", "2"],
                },
                fake_call,
            )

        self.assertEqual(captured.get("images"), paths)
        self.assertEqual(result["images"], {"attached": 3, "skipped": 2})
        self.assertEqual(result["timings"]["attempts"], 1)


class ImageLimitTests(unittest.TestCase):
    def _resolve(self, sizes: list[int]) -> dict:
        paths = [Path(f"img-{index}.png") for index, _ in enumerate(sizes)]
        descriptors = {
            str(path): mock.Mock(
                available=True,
                mime="image/png",
                size=sizes[index],
            )
            for index, path in enumerate(paths)
        }
        with mock.patch(
            "worker.wecom.local_db.read_messages",
            return_value=[{"message_id": 1, "content_raw": b"", "sequence": 1, "send_time": 10}],
        ), mock.patch(
            "worker.wecom.media.extract_local_image_paths",
            side_effect=lambda _config, _raw: list(paths),
        ), mock.patch(
            "worker.wecom.media.inspect_image_file",
            side_effect=lambda path: descriptors[str(path)],
        ):
            return assistant_images.resolve_message_images_with_meta(
                {}, "R:1", ["1"]
            )

    def test_at_most_three_images_are_attached(self):
        result = self._resolve([10, 10, 10, 10, 10])
        self.assertEqual(len(result["paths"]), 3)
        self.assertEqual(
            [path.name for path in result["paths"]],
            ["img-0.png", "img-1.png", "img-2.png"],
        )
        self.assertEqual(result["skipped"], 2)

    def test_total_byte_budget_skips_large_accumulations(self):
        megabyte = 1024 * 1024
        result = self._resolve([5 * megabyte, 5 * megabyte, 5 * megabyte])
        self.assertEqual(len(result["paths"]), 2)
        self.assertEqual(result["skipped"], 1)

    def test_single_oversized_image_is_skipped(self):
        result = self._resolve([9 * 1024 * 1024, 10])
        self.assertEqual(len(result["paths"]), 1)
        self.assertEqual(result["paths"][0].name, "img-1.png")
        self.assertEqual(result["skipped"], 1)

    def test_resolve_message_images_keeps_the_list_contract(self):
        with mock.patch.object(
            assistant_images,
            "resolve_message_images_with_meta",
            return_value={"paths": [Path("a.png")], "skipped": 2},
        ):
            self.assertEqual(
                assistant_images.resolve_message_images({}, "R:1", ["1"]),
                [Path("a.png")],
            )


if __name__ == "__main__":
    unittest.main()
