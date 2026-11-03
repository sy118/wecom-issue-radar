"""Tests for the read-only WeCom chat context and on-demand OCR helpers."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from worker import ocr as ocr_module
from worker.reply_runtime import message_source as message_source_module
from worker.reply_runtime.chat_context import ChatOcrService
from worker.reply_runtime.message_source import LocalWeComMessageSource
from worker.reply_runtime.runtime import ReplyRuntime, RuntimeProtocolError
from worker.reply_runtime.message_normalizer import (
    normalize_wecom_message,
    sanitize_message_text,
)
from worker.reply_runtime.message_source import _context_text
from worker.wecom.local_db import (
    conversation_display_name,
    direct_conversation_display_name,
    direct_peer_id,
    direct_self_id,
    direct_self_userid,
    duplicated_direct_titles,
    normalize_epoch_seconds,
    resolve_direct_conversation_name,
    resolve_direct_self,
    resolve_self_identity,
)


def _directory_entry(
    conversations=None,
    *,
    member_names=None,
    user_map=None,
    accounts=None,
    identities=None,
    self_id=0,
    self_name="",
) -> dict:
    return {
        "at": time.monotonic(),
        "conversations": list(conversations or []),
        "member_names": dict(member_names or {}),
        "user_map": dict(user_map or {}),
        "accounts": dict(accounts or {}),
        "identities": dict(identities or {}),
        "self_id": self_id,
        "self_name": self_name,
        "duplicate_titles": set(),
    }


def _source() -> LocalWeComMessageSource:
    source = object.__new__(LocalWeComMessageSource)
    source.config_path = None
    source._message_snapshot = object()
    source._user_map = {}
    source._member_names = {}
    source._identities = {}
    source._identity_loaded_at = time.time()
    source._cache_lock = threading.Lock()
    source._directory_cache = None
    source._context_cache = {}
    source._warmup_started = True
    return source


def _formatted(message_id: int, sender_id: int, send_time: int, text: str) -> dict:
    return {
        "sender": f"用户{sender_id}",
        "sender_id": sender_id,
        "message_id": message_id,
        "send_time": send_time,
        "content": text,
    }


def _normalized(message_id: int, sender_id: int, send_time: int, text: str) -> dict:
    return {
        "messageId": str(message_id),
        "senderId": str(sender_id),
        "senderName": f"用户{sender_id}",
        "sendTime": send_time,
        "contentType": "text",
        "text": text,
        "images": [],
    }


def _row(conversation_id: str, name: str, last_time: int = 0, last_id: int = 0) -> dict:
    return {
        "conversation_id": conversation_id,
        "name": name,
        "roomname_remark": "",
        "stored_name": name,
        "last_message_time": last_time,
        "last_message_id": last_id,
        "is_group": conversation_id.startswith("R:"),
    }


class ReadContextTests(unittest.TestCase):
    def test_read_context_returns_ascending_window(self):
        rows = [
            {"message_id": 2, "send_time": 200, "sequence": 2, "sender_id": 2},
            {"message_id": 1, "send_time": 100, "sequence": 1, "sender_id": 1},
        ]
        source = _source()
        directory = _directory_entry([_row("R:1", "客服群")])
        with mock.patch.object(message_source_module, "load_config", return_value={}), \
            mock.patch.object(message_source_module, "read_messages", return_value=rows), \
            mock.patch.object(message_source_module, "format_message", side_effect=lambda raw, *_args: _formatted(
                raw["message_id"], raw["sender_id"], raw["send_time"], f"消息{raw['message_id']}"
            )), \
            mock.patch.object(message_source_module, "normalize_wecom_message", side_effect=lambda raw, formatted, **_kwargs: _normalized(
                raw["message_id"], raw["sender_id"], raw["send_time"], formatted["content"]
            )), \
            mock.patch.object(message_source_module, "_resolve_message_db_image_paths", side_effect=lambda _config, _raw, message: message), \
            mock.patch.object(source, "_directory", return_value=directory):
            result = source.read_context("R:1", limit=30, window_seconds=21600)

        self.assertEqual(result["conversationId"], "R:1")
        self.assertEqual(result["displayName"], "客服群")
        self.assertTrue(result["isGroup"])
        self.assertEqual([item["messageId"] for item in result["messages"]], ["1", "2"])
        self.assertEqual(result["messages"][0]["text"], "消息1")
        self.assertIn("hasMore", result)
        self.assertIn("selfSenderId", result)

    def test_read_context_requires_conversation_id(self):
        with self.assertRaises(ValueError):
            _source().read_context("  ")

    def test_resolve_image_paths_only_returns_requested_messages(self):
        rows = [
            {"message_id": 1}, {"message_id": 2}, {"message_id": 3},
        ]
        with mock.patch.object(message_source_module, "load_config", return_value={}), \
            mock.patch.object(message_source_module, "read_messages", return_value=rows), \
            mock.patch.object(message_source_module, "format_message", return_value={}), \
            mock.patch.object(message_source_module, "normalize_wecom_message", return_value={"images": []}), \
            mock.patch.object(
                message_source_module,
                "_resolve_message_db_image_paths",
                side_effect=lambda _config, raw, message: {
                    **message,
                    "images": [{
                        "localPath": f"/cache/{raw['message_id']}.png",
                        "mimeType": "image/png",
                        "errorCode": "",
                    }],
                },
            ):
            result = _source().resolve_image_paths("R:1", ["1", "3"])

        self.assertEqual(sorted(result), ["1", "3"])
        self.assertTrue(result["1"][0]["localPath"].endswith("1.png"))
        self.assertEqual(result["1"][0]["mimeType"], "image/png")


class ChatOcrServiceTests(unittest.TestCase):
    def _engine_config(self) -> dict:
        return {
            "ocr": {
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-ocr",
                "model": "qwen-vl-max",
            }
        }

    def test_ocr_uses_cache_and_budget_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "a.png"
            image.write_bytes(b"fake-image-bytes")
            service = ChatOcrService(Path(directory) / "cache")
            payload = {"1": [{"index": 0, "localPath": str(image), "mimeType": "image/png", "errorCode": ""}]}
            with mock.patch(
                "worker.reply_runtime.chat_context.ocr_image_bytes",
                return_value={"text": "识别文本", "model": "qwen-vl-max", "source": "ocr", "error": ""},
            ) as ocr_call:
                first = service.ocr_messages(self._engine_config(), payload)
                second = service.ocr_messages(self._engine_config(), payload)

            self.assertEqual(ocr_call.call_count, 1)
            self.assertEqual(first["results"][0]["text"], "识别文本")
            self.assertEqual(second["results"][0]["text"], "识别文本")
            self.assertTrue(second["engine"]["available"])

    def test_ocr_reports_engine_unavailable(self):
        service = ChatOcrService(None)
        result = service.ocr_messages(
            {},
            {"1": [{"index": 0, "localPath": "C:/missing.png", "mimeType": "image/png", "errorCode": ""}]},
        )
        self.assertEqual(result["results"][0]["error"], "未配置 OCR 模型")
        self.assertFalse(result["engine"]["available"])

    def test_ocr_caps_images_per_round(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ChatOcrService(None)
            payload = {}
            for index in range(3):
                image = Path(directory) / f"{index}.png"
                image.write_bytes(f"image-{index}".encode())
                payload[str(index)] = [{
                    "index": 0,
                    "localPath": str(image),
                    "mimeType": "image/png",
                    "errorCode": "",
                }]
            with mock.patch(
                "worker.reply_runtime.chat_context.ocr_image_bytes",
                return_value={"text": "ok", "model": "m", "source": "ocr", "error": ""},
            ):
                result = service.ocr_messages(self._engine_config(), payload, max_images=1)

        errors = [item["error"] for item in result["results"] if item["error"]]
        self.assertEqual(errors, ["超过单轮识别上限", "超过单轮识别上限"])

    def test_ocr_marks_unavailable_images(self):
        service = ChatOcrService(None)
        result = service.ocr_messages(
            self._engine_config(),
            {"1": [{"index": 0, "localPath": "", "mimeType": "image/png", "errorCode": "IMAGE_FILE_MISSING"}]},
        )
        self.assertEqual(result["results"][0]["error"], "图片不可用")


class OcrEngineTests(unittest.TestCase):
    def test_dedicated_ocr_model_wins(self):
        engine = ocr_module.resolve_ocr_engine(
            {
                "ocr": {"base_url": "https://api.anthropic.com", "api_key": "k", "model": "claude-sonnet-4-6"},
                "llm": {"base_url": "https://api.deepseek.com/v1", "api_key": "k2", "model": "deepseek-v4.1-flash"},
            }
        )
        self.assertEqual(engine["source"], "ocr")
        self.assertEqual(engine["kind"], "anthropic")

    def test_falls_back_to_global_model(self):
        engine = ocr_module.resolve_ocr_engine(
            {
                "llm": {
                    "provider": "openai_compatible",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "k2",
                    "model": "deepseek-v4.1-flash",
                }
            }
        )
        self.assertEqual(engine["source"], "llm")
        self.assertEqual(engine["kind"], "openai_compatible")

    def test_no_engine(self):
        self.assertIsNone(ocr_module.resolve_ocr_engine({}))

    def test_engine_kind_inferred_from_base_url(self):
        engine = ocr_module.resolve_ocr_engine(
            {"ocr": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key": "k", "model": "qwen-vl-max"}}
        )
        self.assertEqual(engine["kind"], "openai_compatible")


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args) -> None:
        return None


class OcrRequestTests(unittest.TestCase):
    def test_anthropic_payload_parsed(self):
        engine = {"kind": "anthropic", "baseUrl": "https://api.anthropic.com", "apiKey": "k", "model": "claude-sonnet-4-6", "source": "ocr"}
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse({"content": [{"type": "text", "text": "截图文字"}]})

        with mock.patch.object(ocr_module.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = ocr_module.ocr_image_bytes(b"image", engine=engine, media_type="image/png")

        self.assertEqual(result["text"], "截图文字")
        self.assertEqual(result["error"], "")
        self.assertTrue(captured["url"].endswith("/v1/messages"))
        self.assertEqual(captured["body"]["model"], "claude-sonnet-4-6")

    def test_openai_payload_parsed(self):
        engine = {"kind": "openai_compatible", "baseUrl": "https://dashscope.example.com/compatible-mode/v1", "apiKey": "k", "model": "qwen-vl-max", "source": "llm"}
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            return _FakeResponse({"choices": [{"message": {"content": "图片文字"}}]})

        with mock.patch.object(ocr_module.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = ocr_module.ocr_image_bytes(b"image", engine=engine, media_type="image/jpeg")

        self.assertEqual(result["text"], "图片文字")
        self.assertTrue(captured["url"].endswith("/chat/completions"))

    def test_ocr_error_is_returned_not_raised(self):
        engine = {"kind": "anthropic", "baseUrl": "https://api.anthropic.com", "apiKey": "k", "model": "m", "source": "ocr"}
        with mock.patch.object(ocr_module.urllib.request, "urlopen", side_effect=OSError("network down")):
            result = ocr_module.ocr_image_bytes(b"image", engine=engine)
        self.assertEqual(result["text"], "")
        self.assertIn("network down", result["error"])


class _FakeSource:
    def __init__(self) -> None:
        self.context_calls = []
        self.ocr_calls = []

    def recent_conversations(self, limit, *, refresh=False):
        self.context_calls.append((limit, refresh))
        return {
            "chats": [
                {
                    "conversationId": "R:1",
                    "displayName": "群",
                    "lastMessageTime": 1,
                    "lastMessageId": 1,
                    "isGroup": True,
                }
            ],
            "selfSenderId": "2",
            "selfSenderName": "我",
        }

    def read_context(
        self,
        conversation_id,
        *,
        limit,
        window_seconds,
        before=None,
        refresh=False,
    ):
        self.context_calls.append(
            (conversation_id, limit, window_seconds, before, refresh)
        )
        return {
            "conversationId": conversation_id,
            "displayName": "群",
            "isGroup": True,
            "windowFallback": False,
            "hasMore": False,
            "selfSenderId": "2",
            "messages": [],
        }

    def resolve_image_paths(self, conversation_id, message_ids):
        self.ocr_calls.append((conversation_id, message_ids))
        return {"1": []}


class _FakeOcrService:
    def ocr_messages(self, _config, images_by_message):
        return {"results": [], "engine": {"available": True, "source": "ocr", "model": "m"}}


class RuntimeQueryTests(unittest.TestCase):
    def _runtime(self, source) -> ReplyRuntime:
        runtime = object.__new__(ReplyRuntime)
        runtime.message_source = source
        runtime.config_path = None
        return runtime

    def test_chat_recent_clamps_limit(self):
        source = _FakeSource()
        result = self._runtime(source)._query_chat_recent({"limit": 999})
        self.assertEqual(source.context_calls, [(100, False)])
        self.assertEqual(result["chats"][0]["conversationId"], "R:1")
        self.assertEqual(result["selfSenderId"], "2")
        self.assertEqual(result["selfSenderName"], "我")

    def test_chat_context_clamps_and_requires_id(self):
        source = _FakeSource()
        runtime = self._runtime(source)
        runtime._query_chat_context({"conversationId": "R:1", "limit": 999, "windowSeconds": 1})
        self.assertEqual(source.context_calls[-1], ("R:1", 50, 60, None, False))
        with self.assertRaises(RuntimeProtocolError):
            runtime._query_chat_context({})

    def test_chat_ocr_limits_message_ids_and_returns_engine(self):
        source = _FakeSource()
        runtime = self._runtime(source)
        runtime._chat_ocr_service = lambda: _FakeOcrService()
        result = runtime._query_chat_ocr(
            {"conversationId": "R:1", "messageIds": [str(index) for index in range(10)]}
        )
        self.assertEqual(source.ocr_calls[-1], ("R:1", ["0", "1", "2", "3", "4", "5"]))
        self.assertTrue(result["engine"]["available"])


class DirectConversationNameTests(unittest.TestCase):
    def test_peer_id_from_direct_chat_id(self):
        self.assertEqual(
            direct_peer_id("S:1688856756296405_337769972893279308"),
            337769972893279308,
        )
        self.assertEqual(direct_peer_id("R:123"), 0)
        self.assertEqual(direct_peer_id("S:1_not-a-number"), 0)

    def test_self_userid_prefix_is_textual(self):
        self.assertEqual(
            direct_self_userid("S:zhangsan_337769972893279308"), "zhangsan"
        )
        self.assertEqual(direct_self_userid("R:1"), "")

    def test_resolve_name_from_user_map(self):
        self.assertEqual(
            resolve_direct_conversation_name("S:1_42", {42: "李总"}, {}),
            "李总",
        )

    def test_resolve_name_from_member_names(self):
        self.assertEqual(
            resolve_direct_conversation_name(
                "S:1_42", {}, {"S:1_42": {42: "王姐"}}
            ),
            "王姐",
        )

    def test_unresolved_name_is_empty(self):
        self.assertEqual(resolve_direct_conversation_name("S:1_7", {}, {}), "")

    def test_self_identity_resolved_from_account(self):
        self.assertEqual(
            resolve_direct_self("S:zhangsan_42", {5: "张三", 42: "李总"}, {"zhangsan": 5}),
            (5, "张三"),
        )

    def test_self_identity_falls_back_to_numeric_id(self):
        self.assertEqual(
            resolve_direct_self("S:5_42", {5: "张三"}, {}),
            (5, "张三"),
        )

    def test_resolve_self_identity_skips_unresolvable_rows(self):
        self.assertEqual(
            resolve_self_identity(
                ["S:unknown_42", "S:5_42"], {5: "张三"}, {}
            ),
            (5, "张三"),
        )

    def test_read_context_falls_back_for_unnamed_direct_chat(self):
        source = _source()
        directory = _directory_entry([_row("S:1_42", "S:1_42")])
        with mock.patch.object(message_source_module, "load_config", return_value={}), \
            mock.patch.object(message_source_module, "read_messages", return_value=[]), \
            mock.patch.object(source, "_directory", return_value=directory):
            result = source.read_context("S:1_42")

        self.assertEqual(result["displayName"], "私聊会话")
        self.assertFalse(result["isGroup"])


class AppMessageTextTests(unittest.TestCase):
    APP_PAYLOAD = (
        '{"source_icon_desc":"","url":"","desc":"\u4f01\u5fae\u52a9\u624b",'
        '"target":"1","selected":"0","display":{"title":"\u667a\u80fd\u81ea\u52a8\u5316\u52a9\u624b",'
        '"content":"\u6c99\u7bb1\u5185\u4e0d\u5b58\u5728 /data"}}'
    )

    def test_app_json_becomes_readable_summary(self):
        text = sanitize_message_text(self.APP_PAYLOAD, 38)
        self.assertIn("企微助手", text)
        self.assertIn("智能自动化助手", text)
        self.assertNotIn("source_icon_desc", text)

    def test_truncated_app_json_keeps_readable_field(self):
        truncated = (
            '{"source_icon_desc":"","desc":"\u4f01\u5fae\u52a9\u624b",'
            '"display":"main_title":'
        )
        self.assertEqual(
            sanitize_message_text(truncated, 38), "企微助手"
        )

    def test_control_and_zero_width_characters_removed(self):
        self.assertEqual(
            sanitize_message_text("\u200b\x00hello\n\n\nworld"),
            "hello\n\nworld",
        )

    def test_plain_text_untouched(self):
        self.assertEqual(sanitize_message_text("hello"), "hello")

    def test_app_content_type_is_labelled(self):
        formatted = {
            "sender_id": 1,
            "content_type": 38,
            "content": '{"desc":"\u4f01\u5fae\u52a9\u624b"}',
            "message_id": 1,
            "send_time": 100,
        }
        normalized = normalize_wecom_message({}, formatted, group_id="R:1")
        self.assertEqual(normalized["contentType"], "app")
        self.assertEqual(normalized["text"], "企微助手")

    def test_context_text_is_truncated(self):
        truncated = _context_text("x" * 2100)
        self.assertEqual(len(truncated), 2001)
        self.assertTrue(truncated.endswith("…"))

class WindowFallbackTests(unittest.TestCase):
    def test_quiet_direct_chat_falls_back_to_older_messages(self):
        rows = [{"message_id": 1, "send_time": 100, "sequence": 1, "sender_id": 1}]
        source = _source()
        directory = _directory_entry([_row("S:1_2", "S:1_2")])
        with mock.patch.object(message_source_module, "load_config", return_value={}), \
            mock.patch.object(
                message_source_module, "read_messages", side_effect=[[], rows]
            ), \
            mock.patch.object(
                message_source_module,
                "format_message",
                return_value={
                    "sender_id": 1,
                    "message_id": 1,
                    "send_time": 100,
                    "content": "older",
                },
            ), \
            mock.patch.object(
                message_source_module,
                "normalize_wecom_message",
                return_value={
                    "messageId": "1",
                    "senderId": "1",
                    "senderName": "A",
                    "sendTime": 100,
                    "contentType": "text",
                    "text": "older",
                    "images": [],
                },
            ), \
            mock.patch.object(
                message_source_module,
                "_resolve_message_db_image_paths",
                side_effect=lambda _config, _raw, message: message,
            ), \
            mock.patch.object(source, "_directory", return_value=directory):
            result = source.read_context("S:1_2")

        self.assertTrue(result["windowFallback"])
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(result["messages"][0]["text"], "older")

    def test_normal_window_does_not_set_fallback(self):
        rows = [{"message_id": 2, "send_time": 200, "sequence": 2, "sender_id": 1}]
        source = _source()
        directory = _directory_entry([_row("R:1", "客服群")])
        with mock.patch.object(message_source_module, "load_config", return_value={}), \
            mock.patch.object(message_source_module, "read_messages", return_value=rows), \
            mock.patch.object(
                message_source_module,
                "format_message",
                return_value={
                    "sender_id": 1,
                    "message_id": 2,
                    "send_time": 200,
                    "content": "fresh",
                },
            ), \
            mock.patch.object(
                message_source_module,
                "normalize_wecom_message",
                return_value={
                    "messageId": "2",
                    "senderId": "1",
                    "senderName": "A",
                    "sendTime": 200,
                    "contentType": "text",
                    "text": "fresh",
                    "images": [],
                },
            ), \
            mock.patch.object(
                message_source_module,
                "_resolve_message_db_image_paths",
                side_effect=lambda _config, _raw, message: message,
            ), \
            mock.patch.object(source, "_directory", return_value=directory):
            result = source.read_context("R:1")

        self.assertFalse(result["windowFallback"])


class DirectNamePreferenceTests(unittest.TestCase):
    def test_self_title_is_ignored_in_favour_of_peer(self):
        name = direct_conversation_display_name(
            "S:5_42",
            "我自己",
            {5: "我自己", 42: "李总"},
            {},
        )
        self.assertEqual(name, "李总")

    def test_custom_remark_is_kept_when_peer_is_unknown(self):
        name = direct_conversation_display_name(
            "S:5_42",
            "客户备注",
            {5: "我自己"},
            {},
        )
        self.assertEqual(name, "客户备注")

    def test_missing_peer_falls_back_to_stored_name(self):
        name = direct_conversation_display_name("S:5_42", "客户A", {}, {})
        self.assertEqual(name, "客户A")

    def test_own_name_is_never_shown(self):
        self.assertEqual(
            direct_conversation_display_name(
                "S:5_42", "我自己", {5: "我自己"}, {}
            ),
            "私聊会话",
        )

    def test_self_id_prefix(self):
        self.assertEqual(
            direct_self_id("S:1688856756296405_337769972893279308"),
            1688856756296405,
        )
        self.assertEqual(direct_self_id("R:1"), 0)
        self.assertEqual(direct_self_id("S:zhangsan_42"), 0)

    def test_millisecond_timestamps_are_normalized(self):
        self.assertEqual(normalize_epoch_seconds(1700000000000), 1700000000)
        self.assertEqual(normalize_epoch_seconds(1700000000), 1700000000)
        self.assertEqual(normalize_epoch_seconds(0), 0)

    def test_duplicated_direct_titles_are_detected(self):
        rows = [
            _row("S:1_1", "我的名字"),
            _row("S:1_2", "我的名字"),
            _row("S:1_3", "我的名字"),
            _row("S:1_4", "客户备注"),
            _row("R:1", "我的名字"),
        ]
        self.assertEqual(duplicated_direct_titles(rows), {"我的名字"})
        self.assertEqual(
            conversation_display_name(
                "S:1_4", "客户备注", duplicate_titles={"客户备注"}
            ),
            "私聊会话",
        )


class RecentChatSortingTests(unittest.TestCase):
    def test_mixed_units_are_sorted_by_normalized_seconds(self):
        rows = [
            _row("R:1", "A", 1700000000, 1),
            _row("R:2", "B", 1700000000000, 2),
            _row("R:3", "C", 1600000000, 3),
        ]
        source = _source()
        with mock.patch.object(
            source, "_directory", return_value=_directory_entry(rows)
        ), mock.patch.object(source, "_schedule_warmup"):
            result = source.recent_conversations(3)

        chats = result["chats"]
        self.assertEqual(
            [chat["conversationId"] for chat in chats], ["R:2", "R:1", "R:3"]
        )
        self.assertEqual(chats[0]["lastMessageTime"], 1700000000)
        self.assertEqual(result["selfSenderId"], "")
        self.assertEqual(result["selfSenderName"], "")


if __name__ == "__main__":
    unittest.main()
