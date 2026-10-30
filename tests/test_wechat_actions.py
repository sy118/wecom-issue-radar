"""Tests for the personal WeChat backend (``worker/wechat``).

Everything runs against temporary directories with a fake WeChat source and a
fake model adapter: no WeChat installation, network access or DPAPI vault is
required.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worker.wechat import actions as wechat_actions
from worker.wechat import discovery, service
from worker.wechat.errors import ProxyTalkError
from worker.wechat.models import SourceCursor, SourceMessage
from worker.wechat.store import RuntimeStore


class FakeSource:
    """Minimal stand-in for ``LocalWeChatSource`` (no database access)."""

    def __init__(self, db_dir: str = r"C:\Users\tester\Documents\xwechat_files\wxid_me\db_storage") -> None:
        self.db_dir = db_dir
        self.chats: list[dict] = [
            {
                "accountId": "wxid_me",
                "chatId": "wxid_friend",
                "displayName": "朋友",
                "kind": "direct",
                "lastMessage": "在吗",
                "lastTimestamp": 1_700_000_000,
                "unread": 0,
            }
        ]
        self.poll_batches: list[tuple[list[SourceMessage], list[SourceCursor]]] = []
        self.poll_backlogs: list[bool] = []
        self.image_key_scan_calls = 0
        self.image_key_saved: list[str] = []
        self.image_key_error = ""
        self.inspect_payload = {
            "platformSupported": True,
            "running": True,
            "configuredDbDir": db_dir,
            "accounts": [{"accountId": "wxid_me", "dbDir": db_dir, "selected": True}],
            "keyCount": 2,
            "databaseCount": 3,
            "coveredDatabaseCount": 3,
            "ready": True,
            "imageKeyConfigured": True,
            "imageKeyVerified": True,
        }

    def inspect(self) -> dict:
        return dict(self.inspect_payload)

    def select_root(self, db_dir: str) -> dict:
        if "missing" in str(db_dir):
            raise ProxyTalkError("WECHAT_DB_NOT_FOUND", "选择的微信数据库目录不存在。")
        self.db_dir = str(db_dir)
        self.inspect_payload["configuredDbDir"] = self.db_dir
        return self.inspect()

    def unlock(self, db_dir: str) -> dict:
        self.db_dir = str(db_dir)
        return self.inspect()

    def list_chats(self) -> list[dict]:
        return [dict(chat) for chat in self.chats]

    def poll(self, projects, *, backlog: bool):
        self.poll_backlogs.append(bool(backlog))
        if self.poll_batches:
            return self.poll_batches.pop(0)
        return [], []

    def enrich_for_model(self, project, messages, *, supports_vision: bool = True):
        return [dict(message) for message in messages]

    def image_preview(self, project, message, *, max_bytes: int = 12 * 1024 * 1024) -> dict:
        return {
            "available": True,
            "mimeType": "image/png",
            "dataBase64": "aGVsbG8=",
            "reason": "",
        }

    def materialize_media(self, project, message, destination):
        return []

    def scan_image_key(self) -> dict:
        self.image_key_scan_calls += 1
        if self.image_key_error:
            raise ProxyTalkError("WECHAT_IMAGE_KEY_NOT_FOUND", self.image_key_error)
        self.inspect_payload["imageKeyConfigured"] = True
        self.inspect_payload["imageKeyVerified"] = True
        return self.inspect()

    def save_image_key(self, value: str) -> dict:
        self.image_key_saved.append(value)
        if self.image_key_error:
            raise ProxyTalkError("WECHAT_IMAGE_KEY_INVALID", self.image_key_error)
        self.inspect_payload["imageKeyConfigured"] = bool(value)
        self.inspect_payload["imageKeyVerified"] = bool(value)
        return self.inspect()

    def close(self) -> None:
        return None


class FakeModel:
    def __init__(self, text: str = "测试草稿内容") -> None:
        self.text = text
        self.calls: list[dict] = []

    def generate(self, provider, *, api_key, project, messages, skills, evidence, instruction=""):
        self.calls.append(
            {
                "providerId": provider["id"],
                "model": provider["model"],
                "apiKey": api_key,
                "instruction": instruction,
                "messageCount": len(messages),
            }
        )
        return self.text

    def plan_tools(self, provider, *, api_key, project, messages, tools):
        return []

    def test(self, provider, *, api_key):
        return {"ok": True, "sample": self.text}


class WeChatActionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.config_path = self.root / "config.local.json"
        self._write_config({})
        self._saved_env = {
            key: os.environ.get(key)
            for key in (
                "WECOM_ISSUE_RADAR_DATA_DIR",
                "WECOM_ISSUE_RADAR_CONFIG",
                "WECOM_ISSUE_RADAR_WECHAT_TEMP",
            )
        }
        self.addCleanup(self._restore_env)
        self.addCleanup(service.reset_runtime_for_tests)
        self.source = FakeSource()
        self.model = FakeModel()
        self.discovery_result = {
            "candidates": [
                {
                    "path": self.source.db_dir,
                    "accountId": "wxid_me",
                    "root": str(Path(self.source.db_dir).parent),
                    "reason": "session/session.db；3 个消息分片",
                    "score": 94,
                    "valid": True,
                    "hasSession": True,
                    "shardCount": 3,
                    "modifiedAt": 1_700_000_000,
                    "source": "微信 4.x 默认目录",
                    "selected": False,
                },
                {
                    "path": str(Path(self.source.db_dir).parent / "wxid_old" / "db_storage"),
                    "accountId": "wxid_old",
                    "root": str(Path(self.source.db_dir).parent / "wxid_old"),
                    "reason": "缺少 session/session.db",
                    "score": 5,
                    "valid": False,
                    "hasSession": False,
                    "shardCount": 0,
                    "modifiedAt": 0,
                    "source": "微信 4.x 默认目录",
                    "selected": False,
                },
            ],
            "checked": ["C:\\Users\\tester\\Documents\\xwechat_files"],
            "defaultDbDir": self.source.db_dir,
            "reason": "",
            "scannedAt": "2026-10-22T10:00:00+0800",
        }
        self.discovery_calls = 0
        discovery_patch = mock.patch(
            "worker.wechat.discovery.scan",
            side_effect=self._fake_discovery_scan,
        )
        discovery_patch.start()
        self.addCleanup(discovery_patch.stop)
        self.runtime = self._boot()

    def _fake_discovery_scan(self, **_kwargs) -> dict:
        self.discovery_calls += 1
        return copy.deepcopy(self.discovery_result)

    def _restore_env(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _write_config(self, llm: dict) -> None:
        payload = {"config_version": 4, "llm": llm}
        self.config_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _boot(self):
        return service.configure_for_tests(
            data_root_path=self.root / "data",
            config_path_value=self.config_path,
            source=self.source,
            model=self.model,
        )

    def _action(self, action: str, payload: dict | None = None) -> dict:
        return wechat_actions.handle_wechat_action(action, payload or {})

    def _create_project(self, **overrides) -> dict:
        project = {
            "id": "p1",
            "accountId": "wxid_me",
            "chatId": "wxid_friend",
            "chatTitle": "朋友",
            "name": "朋友项目",
            "kind": "direct",
            "summary": "测试摘要",
            "prompt": "自然回复",
        }
        project.update(overrides)
        return self._action("wechat_project_save", {"project": project})["project"]

    # -- detect ----------------------------------------------------------

    def test_detect_maps_candidates_and_accounts(self):
        result = self._action("wechat_detect")
        self.assertTrue(result["found"])
        self.assertTrue(result["running"])
        self.assertFalse(result["platformSupported"] is False)
        self.assertEqual(result["dbDir"], self.source.db_dir)
        self.assertEqual(len(result["candidates"]), 2)
        self.assertTrue(result["candidates"][0]["valid"])
        self.assertFalse(result["candidates"][1]["valid"])
        self.assertEqual(len(result["accounts"]), 1)
        self.assertEqual(result["accounts"][0]["accountId"], "wxid_me")
        self.assertEqual(result["accounts"][0]["dbDir"], self.source.db_dir)
        self.assertTrue(result["accounts"][0]["selected"])
        self.assertIsInstance(result["version"], str)
        self.assertTrue(result["ready"])
        self.assertEqual(result["checkedPaths"], ["C:\\Users\\tester\\Documents\\xwechat_files"])

    def test_detect_reports_readable_reason_when_nothing_found(self):
        self.discovery_result = {
            "candidates": [],
            "checked": ["C:\\Users\\tester\\Documents\\xwechat_files"],
            "defaultDbDir": "",
            "reason": "未找到 db_storage 目录。请确认已登录微信 4.x 并至少打开过一次聊天。",
            "scannedAt": "2026-10-22T10:00:00+0800",
        }
        result = self._action("wechat_detect")
        self.assertFalse(result["found"])
        self.assertEqual(result["candidates"], [])
        self.assertIn("db_storage", result["reason"])

    def test_detect_prefers_configured_directory(self):
        self.runtime.store.save_settings({"wechatDbDir": self.source.db_dir})
        result = self._action("wechat_detect")
        self.assertEqual(result["dbDir"], self.source.db_dir)
        self.assertTrue(result["accounts"][0]["selected"])

    def _create_db_storage(self, account: str = "wxid_me") -> Path:
        db_dir = self.root / "xwechat_files" / account / "db_storage"
        (db_dir / "session").mkdir(parents=True, exist_ok=True)
        (db_dir / "session" / "session.db").write_bytes(b"")
        (db_dir / "message").mkdir()
        (db_dir / "message" / "message_0.db").write_bytes(b"")
        return db_dir

    def test_detect_reuses_configured_directory_without_full_scan(self):
        db_dir = self._create_db_storage()
        self.runtime.store.save_settings({"wechatDbDir": str(db_dir)})
        self.discovery_calls = 0
        result = self._action("wechat_detect")
        self.assertTrue(result["found"])
        self.assertTrue(result["cached"])
        self.assertFalse(result["forced"])
        self.assertEqual(self.discovery_calls, 0)
        self.assertEqual([item["path"] for item in result["candidates"]], [str(db_dir.resolve())])

    def test_detect_force_rescans_all_roots(self):
        db_dir = self._create_db_storage()
        self.runtime.store.save_settings({"wechatDbDir": str(db_dir)})
        self.discovery_calls = 0
        result = self._action("wechat_detect", {"force": True})
        self.assertFalse(result["cached"])
        self.assertTrue(result["forced"])
        self.assertEqual(self.discovery_calls, 1)

    def test_select_account_by_candidate_path(self):
        result = self._action("wechat_select", {"dbDir": self.source.db_dir})
        self.assertTrue(result["selected"])
        self.assertEqual(result["dbDir"], self.source.db_dir)
        with self.assertRaises(ProxyTalkError) as context:
            self._action("wechat_select", {"dbDir": ""})
        self.assertEqual(context.exception.code, "WECHAT_DB_NOT_FOUND")

    def test_action_prefix_is_optional(self):
        self.assertEqual(self._action("wechat.detect")["dbDir"], self.source.db_dir)
        self.assertEqual(self._action("detect")["dbDir"], self.source.db_dir)

    def test_unknown_action_is_rejected_in_chinese(self):
        with self.assertRaises(ValueError) as context:
            self._action("wechat_explode")
        self.assertIn("不支持的微信动作", str(context.exception))

    # -- projects --------------------------------------------------------

    def test_project_crud_roundtrip(self):
        project = self._create_project()
        self.assertEqual(project["id"], "p1")
        listed = self._action("wechat_projects")["projects"]
        self.assertEqual([item["id"] for item in listed], ["p1"])
        self.assertEqual(listed[0]["draft"]["status"], "idle")
        self.assertFalse(listed[0]["draft"]["hasContent"])

        deleted = self._action("wechat_project_delete", {"projectId": "p1"})
        self.assertTrue(deleted["deleted"])
        self.assertEqual(self._action("wechat_projects")["projects"], [])

    # -- messages --------------------------------------------------------

    def test_messages_return_media_descriptors(self):
        self._create_project()
        self.runtime.store.ingest(
            [
                SourceMessage(
                    project_id="p1",
                    source_key="k1",
                    source_db="message/message_0.db",
                    local_id=7,
                    server_id=0,
                    sort_seq=7,
                    create_time=1_700_000_100,
                    sender_id="wxid_friend",
                    sender_name="朋友",
                    direction="incoming",
                    kind="image",
                    text="[图片]",
                    media=[{"kind": "image", "localId": 7, "sourceDb": "message/message_0.db"}],
                )
            ],
            [SourceCursor("p1", "message/message_0.db", 1_700_000_100, 7, 7)],
        )
        result = self._action("wechat_messages", {"projectId": "p1"})
        self.assertEqual(len(result["messages"]), 1)
        message = result["messages"][0]
        self.assertEqual(message["id"], "k1")
        self.assertEqual(message["media"][0]["kind"], "image")
        self.assertTrue(message["hasMedia"])
        self.assertEqual(message["mediaCount"], 1)

        preview = self._action("wechat_media_preview", {"projectId": "p1", "messageId": "k1"})
        self.assertTrue(preview["available"])
        self.assertTrue(preview["dataUrl"].startswith("data:image/png;base64,"))

    # -- drafts ----------------------------------------------------------

    def test_draft_generate_without_model_fails_in_chinese(self):
        self._create_project()
        providers = self._action("wechat_providers")
        self.assertEqual(providers["providers"], [])
        with self.assertRaises(ProxyTalkError) as context:
            self._action("wechat_draft_generate", {"projectId": "p1"})
        self.assertEqual(context.exception.code, "MODEL_NOT_CONFIGURED")
        self.assertIn("模型连接", context.exception.message)

    def test_draft_generate_uses_config_llm_provider(self):
        self._write_config(
            {
                "provider": "openai_compatible",
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-test-key",
                "model": "gpt-4o-mini",
                "temperature": 0.2,
                "max_output_tokens": 800,
            }
        )
        self.runtime = self._boot()
        providers = self._action("wechat_providers")
        self.assertEqual([item["id"] for item in providers["providers"]], [service.DEFAULT_PROVIDER_ID])

        project = self._create_project()
        self.assertEqual(project["providerId"], service.DEFAULT_PROVIDER_ID)
        self.runtime.store.ingest(
            [
                SourceMessage(
                    project_id="p1",
                    source_key="k1",
                    source_db="message/message_0.db",
                    local_id=1,
                    server_id=0,
                    sort_seq=1,
                    create_time=1_700_000_000,
                    sender_id="wxid_friend",
                    sender_name="朋友",
                    direction="incoming",
                    kind="text",
                    text="帮我看看这个",
                )
            ],
            [SourceCursor("p1", "message/message_0.db", 1_700_000_000, 1, 1)],
        )
        draft = self._action("wechat_draft_generate", {"projectId": "p1"})["draft"]
        self.assertEqual(draft["status"], "ready")
        self.assertEqual(draft["content"], "测试草稿内容")
        self.assertEqual(self.model.calls[0]["apiKey"], "sk-test-key")
        self.assertEqual(self.model.calls[0]["model"], "gpt-4o-mini")

    def test_default_provider_syncs_with_global_model_after_reboot(self):
        self._write_config(
            {
                "provider": "openai_compatible",
                "base_url": "https://dashscope.example.com/compatible-mode/v1",
                "api_key": "sk-first",
                "model": "qwen3.7-max",
                "temperature": 0.2,
                "max_output_tokens": 900,
            }
        )
        self.runtime = self._boot()
        first = self._action("wechat_providers")
        self.assertEqual([item["id"] for item in first["providers"]], [service.DEFAULT_PROVIDER_ID])
        self.assertEqual(first["providers"][0]["model"], "qwen3.7-max")
        self.assertTrue(first["providers"][0]["hasSecret"])
        self.assertEqual(first["llm"]["model"], "qwen3.7-max")

        self._write_config(
            {
                "provider": "openai_compatible",
                "base_url": "https://dashscope.example.com/compatible-mode/v1",
                "api_key": "sk-second",
                "model": "deepseek-v4.1-flash",
                "temperature": 0.2,
                "max_output_tokens": 900,
            }
        )
        self.runtime = self._boot()
        second = self._action("wechat_providers")
        default = next(item for item in second["providers"] if item["id"] == service.DEFAULT_PROVIDER_ID)
        self.assertEqual(default["model"], "deepseek-v4.1-flash")
        self.assertEqual(default["baseUrl"], "https://dashscope.example.com/compatible-mode/v1")
        self.assertTrue(default["hasSecret"])
        self.assertEqual(second["llm"]["model"], "deepseek-v4.1-flash")

    def test_default_provider_sync_preserves_custom_providers(self):
        self._write_config(
            {
                "provider": "openai_compatible",
                "base_url": "https://dashscope.example.com/compatible-mode/v1",
                "api_key": "sk-global",
                "model": "qwen3.7-max",
            }
        )
        self.runtime = self._boot()
        saved = self._action(
            "wechat_provider_save",
            {
                "provider": {
                    "id": "custom-provider",
                    "name": "自建模型",
                    "kind": "openai_compatible",
                    "baseUrl": "https://api.example.com/v1",
                    "model": "gpt-4o-mini",
                    "temperature": 0.4,
                    "maxOutputTokens": 700,
                },
                "apiKey": "sk-custom",
            },
        )["provider"]
        self.assertEqual(saved["model"], "gpt-4o-mini")

        self._write_config(
            {
                "provider": "openai_compatible",
                "base_url": "https://dashscope.example.com/compatible-mode/v1",
                "api_key": "sk-global",
                "model": "deepseek-v4.1-flash",
            }
        )
        self.runtime = self._boot()
        providers = self._action("wechat_providers")["providers"]
        custom = next(item for item in providers if item["id"] == "custom-provider")
        default = next(item for item in providers if item["id"] == service.DEFAULT_PROVIDER_ID)
        self.assertEqual(custom["name"], "自建模型")
        self.assertEqual(custom["model"], "gpt-4o-mini")
        self.assertEqual(custom["baseUrl"], "https://api.example.com/v1")
        self.assertTrue(custom["hasSecret"])
        self.assertEqual(default["model"], "deepseek-v4.1-flash")

    def test_default_provider_reports_synced_flag_only_when_changed(self):
        self._write_config({"base_url": "https://api.example.com/v1", "api_key": "sk-a", "model": "qwen3.7-max"})
        self.runtime = self._boot()
        self.assertFalse(self.runtime.ensure_default_provider()["synced"])

        self._write_config({"base_url": "https://api.example.com/v1", "api_key": "sk-a", "model": "deepseek-v4.1-flash"})
        result = self.runtime.ensure_default_provider()
        self.assertTrue(result["synced"])
        self.assertFalse(self.runtime.ensure_default_provider()["synced"])

    def test_draft_save_and_rewrite_style_validation(self):
        self._create_project()
        draft = self._action("wechat_draft_save", {"projectId": "p1", "content": " 你好 "})["draft"]
        self.assertEqual(draft["status"], "ready")
        self.assertEqual(draft["content"], " 你好 ")
        with self.assertRaises(ProxyTalkError) as context:
            self._action("wechat_draft_rewrite", {"projectId": "p1", "style": "bogus"})
        self.assertEqual(context.exception.code, "INVALID_REWRITE")

    def _enable_model(self) -> None:
        self._write_config(
            {
                "provider": "openai_compatible",
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-test-key",
                "model": "gpt-4o-mini",
                "temperature": 0.2,
                "max_output_tokens": 800,
            }
        )
        self.runtime = self._boot()

    def _ingest_text_message(self, text: str = "需要回复的消息") -> None:
        self.runtime.store.ingest(
            [
                SourceMessage(
                    project_id="p1",
                    source_key="k1",
                    source_db="message/message_0.db",
                    local_id=1,
                    server_id=0,
                    sort_seq=1,
                    create_time=1_700_000_000,
                    sender_id="wxid_friend",
                    sender_name="朋友",
                    direction="incoming",
                    kind="text",
                    text=text,
                )
            ],
            [SourceCursor("p1", "message/message_0.db", 1_700_000_000, 1, 1)],
        )

    def test_draft_rewrite_accepts_custom_instruction(self):
        self._enable_model()
        self._create_project()
        self._ingest_text_message()
        self._action("wechat_draft_rewrite", {"projectId": "p1", "instruction": "改成正式书面语"})
        self.assertEqual(self.model.calls[-1]["instruction"], "改成正式书面语")

    def test_draft_rewrite_uses_configured_style_instruction(self):
        self._enable_model()
        styles = [{"id": "formal", "label": "正式", "instruction": "用正式、书面的方式改写。"}]
        self._action("wechat_settings_save", {"settings": {"replyStyles": styles}})
        self._create_project()
        self._ingest_text_message()
        self._action("wechat_draft_rewrite", {"projectId": "p1", "styleId": "formal"})
        self.assertEqual(self.model.calls[-1]["instruction"], "用正式、书面的方式改写。")

    def test_reply_styles_defaults_validation_and_persistence(self):
        settings = self._action("wechat_settings")["settings"]
        self.assertEqual(
            [item["id"] for item in settings["replyStyles"]],
            ["shorter", "politer", "casual", "another"],
        )
        self.assertEqual(settings["defaultTone"], "自然友好")
        styles = [
            {"id": "formal", "label": "正式", "instruction": "改成正式、书面的回复。"},
            {"id": "warm", "label": "温和", "instruction": "语气更温和体贴。"},
        ]
        saved = self._action("wechat_settings_save", {"settings": {"replyStyles": styles}})["settings"]
        self.assertEqual(saved["replyStyles"], styles)
        self.assertEqual(self._action("wechat_settings")["settings"]["replyStyles"], styles)
        with self.assertRaises(ProxyTalkError) as context:
            self._action(
                "wechat_settings_save",
                {"settings": {"replyStyles": [{"id": "x", "label": "X", "instruction": ""}]}},
            )
        self.assertEqual(context.exception.code, "INVALID_SETTINGS")
        with self.assertRaises(ProxyTalkError):
            self._action("wechat_settings_save", {"settings": {"replyStyles": [styles[0], styles[0]]}})

    def test_project_tone_is_persisted(self):
        project = self._create_project(tone="专业稳重")
        self.assertEqual(project["tone"], "专业稳重")
        listed = self._action("wechat_projects")["projects"]
        self.assertEqual(listed[0]["tone"], "专业稳重")

    # -- poll ------------------------------------------------------------

    def test_poll_deduplicates_and_marks_draft_stale(self):
        self._create_project()
        self._action("wechat_draft_save", {"projectId": "p1", "content": "旧草稿"})
        message = SourceMessage(
            project_id="p1",
            source_key="k1",
            source_db="message/message_0.db",
            local_id=1,
            server_id=0,
            sort_seq=1,
            create_time=1_700_000_000,
            sender_id="wxid_friend",
            sender_name="朋友",
            direction="incoming",
            kind="text",
            text="新消息",
        )
        cursor = SourceCursor("p1", "message/message_0.db", 1_700_000_000, 1, 1)
        self.source.poll_batches = [([message], [cursor]), ([message], [cursor])]

        first = self._action("wechat_poll")
        self.assertTrue(first["updated"])
        self.assertEqual(first["inserted"], 1)
        entry = first["projects"][0]
        self.assertEqual(entry["newMessageCount"], 1)
        self.assertEqual([message["text"] for message in entry["newMessages"]], ["新消息"])
        self.assertEqual(entry["unreadDelta"], 1)
        self.assertTrue(entry["draft"]["stale"])
        self.assertEqual(self.source.poll_backlogs, [False])

        second = self._action("wechat_poll")
        self.assertFalse(second["updated"])
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["projects"], [])
        self.assertEqual(self.source.poll_backlogs, [False, False])

    # -- keys ------------------------------------------------------------

    def test_scan_keys_reports_chinese_message_without_running_wechat(self):
        self.runtime.store.save_settings({"wechatDbDir": str(self.root / "db_storage")})
        with mock.patch("worker.wechat.wechat_source.wechat_is_running", return_value=False):
            result = self._action("wechat_scan_keys")
        self.assertFalse(result["ok"])
        self.assertIn("微信", result["message"])
        self.assertEqual(result["code"], "WECHAT_NOT_RUNNING")

    def test_image_key_scan_and_save_actions(self):
        scanned = self._action("wechat_image_key_scan")
        self.assertTrue(scanned["ok"])
        self.assertTrue(scanned["imageKeyVerified"])
        self.assertEqual(self.source.image_key_scan_calls, 1)

        saved = self._action("wechat_image_key_save", {"imageKey": "0123456789abcdef"})
        self.assertTrue(saved["ok"])
        self.assertEqual(self.source.image_key_saved, ["0123456789abcdef"])
        self.assertTrue(saved["imageKeyConfigured"])

        cleared = self._action("wechat_image_key_save", {"imageKey": ""})
        self.assertTrue(cleared["ok"])
        self.assertEqual(self.source.image_key_saved[-1], "")
        self.assertIn("清除", cleared["message"])

    def test_image_key_errors_are_reported_in_chinese(self):
        self.source.image_key_error = "没有找到可用于验证的 V2 图片。"
        scanned = self._action("wechat_image_key_scan")
        self.assertFalse(scanned["ok"])
        self.assertEqual(scanned["code"], "WECHAT_IMAGE_KEY_NOT_FOUND")
        self.assertIn("V2", scanned["message"])
        saved = self._action("wechat_image_key_save", {"imageKey": "0123456789abcdef"})
        self.assertFalse(saved["ok"])
        self.assertEqual(saved["code"], "WECHAT_IMAGE_KEY_INVALID")

    # -- settings / export contract --------------------------------------
    def test_settings_verb_accepts_desktop_payload_and_saves(self):
        result = self._action(
            "wechat_settings",
            {
                "settings": {
                    "dbDir": "",
                    "wechatDbDir": "",
                    "pollIntervalSeconds": 12,
                    "contextMessageLimit": 80,
                    "contextCharacterLimit": 30_000,
                    "voiceTranscription": False,
                }
            },
        )
        self.assertEqual(result["settings"]["pollIntervalSeconds"], 12)
        self.assertEqual(result["settings"]["contextMessageLimit"], 80)
        self.assertEqual(result["settings"]["contextCharacterLimit"], 30_000)
        # A read without a settings payload must not modify stored preferences.
        self.assertEqual(self._action("wechat_settings")["settings"]["pollIntervalSeconds"], 12)

    def test_settings_reject_invalid_db_dir_switch(self):
        self.runtime.store.save_settings({"wechatDbDir": str(self.root / "db_storage")})
        with self.assertRaises(ProxyTalkError) as context:
            self._action(
                "wechat_settings_save",
                {"settings": {"wechatDbDir": str(self.root / "missing" / "db_storage")}},
            )
        self.assertEqual(context.exception.code, "WECHAT_DB_NOT_FOUND")

    def test_export_writes_markdown_and_json(self):
        self._create_project()
        self.runtime.store.ingest(
            [
                SourceMessage(
                    project_id="p1",
                    source_key="k1",
                    source_db="message/message_0.db",
                    local_id=1,
                    server_id=0,
                    sort_seq=1,
                    create_time=1_767_225_600,
                    sender_id="wxid_friend",
                    sender_name="朋友",
                    direction="incoming",
                    kind="text",
                    text="导出内容",
                )
            ],
            [SourceCursor("p1", "message/message_0.db", 1_767_225_600, 1, 1)],
        )
        result = self._action(
            "wechat_export",
            {
                "projectId": "p1",
                "start": "2026-01-01T00:00:00",
                "end": "2026-12-31T23:59:59",
            },
        )
        self.assertTrue(result["dir"])
        self.assertIn("chat.md", result["files"])
        self.assertIn("chat.json", result["files"])
        self.assertEqual(result["messageCount"], 1)

    def test_rewrite_style_aliases_match_desktop_contract(self):
        self.assertEqual(wechat_actions.REWRITE_STYLES["politer"], "polite")
        self.assertEqual(wechat_actions.REWRITE_STYLES["another"], "regenerate")
        self.assertEqual(wechat_actions.REWRITE_STYLES["shorter"], "shorter")

    # -- legacy import ---------------------------------------------------

    def _build_legacy_database(self) -> Path:
        legacy_dir = self.root / "legacy"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        path = legacy_dir / "proxytalk.sqlite3"
        store = RuntimeStore(path)
        try:
            store.save_settings({"contextMessageLimit": 42})
            store.save_provider(
                {
                    "id": "legacy-provider",
                    "name": "旧模型",
                    "kind": "openai_compatible",
                    "baseUrl": "https://api.example.com/v1",
                    "model": "gpt-4o-mini",
                },
                secret_ref="",
            )
            store.save_project(
                {
                    "id": "legacy-p1",
                    "accountId": "wxid_me",
                    "chatId": "wxid_old",
                    "chatTitle": "旧聊天",
                    "name": "旧项目",
                    "kind": "direct",
                    "providerId": "legacy-provider",
                }
            )
            store.ingest(
                [
                    SourceMessage(
                        project_id="legacy-p1",
                        source_key="legacy-k1",
                        source_db="message/message_0.db",
                        local_id=3,
                        server_id=0,
                        sort_seq=3,
                        create_time=1_699_000_000,
                        sender_id="wxid_old",
                        sender_name="旧聊天",
                        direction="incoming",
                        kind="text",
                        text="旧消息",
                    )
                ],
                [SourceCursor("legacy-p1", "message/message_0.db", 1_699_000_000, 3, 3)],
            )
            store.save_draft("legacy-p1", content="旧草稿", status="ready")
        finally:
            store.close()
        return path

    def test_import_proxytalk_is_idempotent_and_read_only(self):
        legacy = self._build_legacy_database()
        before = hashlib.sha256(legacy.read_bytes()).hexdigest()

        first = self._action("wechat_import_proxytalk", {"path": str(legacy)})
        self.assertTrue(first["imported"])
        self.assertEqual(first["counts"]["projects"], 1)
        self.assertEqual(first["counts"]["messages"], 1)
        self.assertEqual(first["counts"]["drafts"], 1)
        self.assertEqual(self.runtime.store.settings()["contextMessageLimit"], 42)

        projects = self._action("wechat_projects")["projects"]
        imported = [item for item in projects if item["id"] == "legacy-p1"]
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0]["draft"]["content"], "旧草稿")
        messages = self._action("wechat_messages", {"projectId": "legacy-p1"})["messages"]
        self.assertEqual([message["text"] for message in messages], ["旧消息"])

        second = self._action("wechat_import_proxytalk", {"path": str(legacy)})
        self.assertTrue(second["imported"])
        self.assertEqual(second["counts"]["projects"], 0)
        self.assertEqual(second["counts"]["messages"], 0)
        self.assertEqual(second["counts"]["drafts"], 0)
        self.assertEqual(len(self._action("wechat_projects")["projects"]), 1)
        self.assertEqual(hashlib.sha256(legacy.read_bytes()).hexdigest(), before)

    def test_import_proxytalk_reports_missing_source(self):
        missing = self.root / "missing" / "proxytalk.sqlite3"
        result = self._action("wechat_import_proxytalk", {"path": str(missing)})
        self.assertFalse(result["imported"])
        self.assertEqual(result["counts"]["projects"], 0)
        self.assertIn("未找到", result["message"])


class WeChatDiscoveryTests(unittest.TestCase):
    def test_scan_scores_candidates_and_prefers_valid_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "xwechat_files" / "wxid_aaa_1234" / "db_storage"
            (good / "session").mkdir(parents=True)
            (good / "session" / "session.db").write_bytes(b"")
            (good / "message").mkdir()
            (good / "message" / "message_0.db").write_bytes(b"")
            (good / "contact").mkdir()
            (good / "contact" / "contact.db").write_bytes(b"")
            broken = root / "xwechat_files" / "wxid_bbb_5678" / "db_storage"
            (broken / "message").mkdir(parents=True)
            old = root / "WeChat Files" / "wxid_ccc" / "db_storage"
            (old / "session").mkdir(parents=True)
            result = discovery.scan(
                roots=[
                    (root / "xwechat_files", "微信 4.x 默认目录"),
                    (root / "WeChat Files", "微信 3.x 默认目录"),
                ]
            )
            paths = [item["path"] for item in result["candidates"]]
            self.assertEqual(paths[0], str(good))
            good_item = next(item for item in result["candidates"] if item["path"] == str(good))
            self.assertTrue(good_item["valid"])
            self.assertGreaterEqual(good_item["score"], 60)
            self.assertEqual(good_item["accountId"], "wxid_aaa_1234")
            broken_item = next(item for item in result["candidates"] if item["path"] == str(broken))
            self.assertFalse(broken_item["valid"])
            self.assertIn("session.db", broken_item["reason"])
            old_item = next(item for item in result["candidates"] if item["path"] == str(old))
            self.assertFalse(old_item["valid"])
            self.assertEqual(result["defaultDbDir"], str(good))
            self.assertEqual(
                result["checked"],
                [str(root / "xwechat_files"), str(root / "WeChat Files")],
            )

    def test_scan_explains_failure_when_nothing_found(self):
        with tempfile.TemporaryDirectory() as directory:
            result = discovery.scan(roots=[(Path(directory) / "empty", "测试目录")])
        self.assertEqual(result["candidates"], [])
        self.assertIn("未找到", result["reason"])
        self.assertEqual(result["defaultDbDir"], "")

    def test_scan_marks_configured_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_dir = root / "xwechat_files" / "wxid_ddd_9999" / "db_storage"
            (db_dir / "session").mkdir(parents=True)
            (db_dir / "session" / "session.db").write_bytes(b"")
            result = discovery.scan(
                configured=str(db_dir),
                roots=[(root / "xwechat_files", "微信 4.x 默认目录")],
            )
        candidate = result["candidates"][0]
        self.assertTrue(candidate["selected"])
        self.assertEqual(result["defaultDbDir"], str(db_dir))


class WeChatBacklogWindowTests(unittest.TestCase):
    def test_initial_backlog_only_reads_latest_window(self):
        from worker.wechat.vault import MemoryVault
        from worker.wechat.wechat_source import BACKLOG_INITIAL_LIMIT, LocalWeChatSource

        with tempfile.TemporaryDirectory() as directory:
            source = LocalWeChatSource(None, MemoryVault(), Path(directory))
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(
                "CREATE TABLE Msg_test(local_id INTEGER, local_type INTEGER, create_time INTEGER, message_content TEXT)"
            )
            connection.executemany(
                "INSERT INTO Msg_test VALUES(?,?,?,?)",
                [(index + 1, 1, 1_700_000_000 + index, f"msg-{index}") for index in range(500)],
            )
            connection.commit()
            project = {"id": "p1", "chatId": "wxid_friend", "kind": "direct", "accountId": "wxid_me"}
            messages, cursor = source._read_chat_table(
                connection,
                project,
                "Msg_test",
                "message/message_0.db",
                SourceCursor("p1", "message/message_0.db"),
                {},
                True,
            )
        self.assertEqual(len(messages), BACKLOG_INITIAL_LIMIT)
        self.assertEqual(messages[0].text, "msg-200")
        self.assertEqual(messages[-1].text, "msg-499")
        self.assertIsNotNone(cursor)
        self.assertEqual(cursor.create_time, 1_700_000_499)
        self.assertEqual(cursor.local_id, 500)


if __name__ == "__main__":
    unittest.main()
