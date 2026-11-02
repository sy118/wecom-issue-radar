"""Tests for the read-only WeCom chat context and on-demand OCR helpers."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from worker import ocr as ocr_module
from worker.reply_runtime import message_source as message_source_module
from worker.reply_runtime.chat_context import ChatOcrService
from worker.reply_runtime.message_source import LocalWeComMessageSource
from worker.reply_runtime.runtime import ReplyRuntime, RuntimeProtocolError


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


class ReadContextTests(unittest.TestCase):
    def _source(self) -> LocalWeComMessageSource:
        source = object.__new__(LocalWeComMessageSource)
        source.config_path = None
        source._message_snapshot = object()
        source._user_map = {}
        source._member_names = {}
        source._identities = {}
        source._identity_loaded_at = time.time()
        return source

    def test_read_context_returns_ascending_window(self):
        rows = [
            {"message_id": 2, "send_time": 200, "sequence": 2, "sender_id": 2},
            {"message_id": 1, "send_time": 100, "sequence": 1, "sender_id": 1},
        ]
        with mock.patch.object(message_source_module, "load_config", return_value={}), \
            mock.patch.object(message_source_module, "read_messages", return_value=rows), \
            mock.patch.object(message_source_module, "format_message", side_effect=lambda raw, *_args: _formatted(
                raw["message_id"], raw["sender_id"], raw["send_time"], f"消息{raw['message_id']}"
            )), \
            mock.patch.object(message_source_module, "normalize_wecom_message", side_effect=lambda raw, formatted, **_kwargs: _normalized(
                raw["message_id"], raw["sender_id"], raw["send_time"], formatted["content"]
            )), \
            mock.patch.object(message_source_module, "_resolve_message_db_image_paths", side_effect=lambda _config, _raw, message: message), \
            mock.patch.object(message_source_module, "get_conversation_state", return_value={"display_name": "客服群"}):
            result = self._source().read_context("R:1", limit=30, window_seconds=21600)

        self.assertEqual(result["conversationId"], "R:1")
        self.assertEqual(result["displayName"], "客服群")
        self.assertTrue(result["isGroup"])
        self.assertEqual([item["messageId"] for item in result["messages"]], ["1", "2"])
        self.assertEqual(result["messages"][0]["text"], "消息1")

    def test_read_context_requires_conversation_id(self):
        with self.assertRaises(ValueError):
            self._source().read_context("  ")

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
            result = self._source().resolve_image_paths("R:1", ["1", "3"])

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

    def recent_conversations(self, limit):
        self.context_calls.append(limit)
        return [{"conversationId": "R:1", "displayName": "群", "lastMessageTime": 1, "isGroup": True}]

    def read_context(self, conversation_id, *, limit, window_seconds):
        self.context_calls.append((conversation_id, limit, window_seconds))
        return {"conversationId": conversation_id, "displayName": "群", "isGroup": True, "messages": []}

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
        self.assertEqual(source.context_calls, [100])
        self.assertEqual(result["chats"][0]["conversationId"], "R:1")

    def test_chat_context_clamps_and_requires_id(self):
        source = _FakeSource()
        runtime = self._runtime(source)
        runtime._query_chat_context({"conversationId": "R:1", "limit": 999, "windowSeconds": 1})
        self.assertEqual(source.context_calls[-1], ("R:1", 50, 60))
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


if __name__ == "__main__":
    unittest.main()
