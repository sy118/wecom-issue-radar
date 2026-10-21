"""Tests for the personal WeChat backend (``worker/wechat``).

Everything runs against temporary directories with a fake WeChat source and a
fake model adapter: no WeChat installation, network access or DPAPI vault is
required.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worker.wechat import actions as wechat_actions
from worker.wechat import service
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
        self.runtime = self._boot()

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

    def test_detect_maps_source_status(self):
        result = self._action("wechat_detect")
        self.assertTrue(result["found"])
        self.assertTrue(result["running"])
        self.assertFalse(result["platformSupported"] is False)
        self.assertEqual(result["dbDir"], self.source.db_dir)
        self.assertEqual(len(result["accounts"]), 1)
        self.assertEqual(result["accounts"][0]["accountId"], "wxid_me")
        self.assertEqual(result["accounts"][0]["dbDir"], self.source.db_dir)
        self.assertIsInstance(result["version"], str)
        self.assertTrue(result["ready"])

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

    def test_draft_save_and_rewrite_style_validation(self):
        self._create_project()
        draft = self._action("wechat_draft_save", {"projectId": "p1", "content": " 你好 "})["draft"]
        self.assertEqual(draft["status"], "ready")
        self.assertEqual(draft["content"], " 你好 ")
        with self.assertRaises(ProxyTalkError) as context:
            self._action("wechat_draft_rewrite", {"projectId": "p1", "style": "bogus"})
        self.assertEqual(context.exception.code, "INVALID_REWRITE")

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
        self.assertEqual(self.source.poll_backlogs, [True])

        second = self._action("wechat_poll")
        self.assertFalse(second["updated"])
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["projects"], [])
        self.assertEqual(self.source.poll_backlogs, [True, False])

    # -- keys ------------------------------------------------------------

    def test_scan_keys_reports_chinese_message_without_running_wechat(self):
        self.runtime.store.save_settings({"wechatDbDir": str(self.root / "db_storage")})
        with mock.patch("worker.wechat.wechat_source.wechat_is_running", return_value=False):
            result = self._action("wechat_scan_keys")
        self.assertFalse(result["ok"])
        self.assertIn("微信", result["message"])
        self.assertEqual(result["code"], "WECHAT_NOT_RUNNING")

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


if __name__ == "__main__":
    unittest.main()
