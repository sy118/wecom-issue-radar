"""Tests for the one-shot config commands exposed by the Python worker.

These cover the M0 contract that replaced ``src-tauri/src/config.rs``:
normalization on bootstrap/save, runtime-partition preservation, backup
redaction/restore and unique-ID rejection. Everything runs on temp directories
and never touches Qt, the network or the WeCom database.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worker.main import dispatch
from worker.pipeline import config_commands as commands
from worker.pipeline.config_store import CURRENT_CONFIG_VERSION, load_config, save_config


class ConfigCommandTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.root = Path(self._temporary_directory.name)
        self.config_path = self.root / "config.local.json"

    def _seed_current_config(self) -> dict:
        """Write a realistic v4 config containing every kind of secret."""
        config, _ = load_config(self.config_path)
        # Materialize the v4 baseline first so legacy-mirror edits below have a
        # persisted config to diff against (same flow as app bootstrap -> save).
        save_config(config, self.config_path)
        config, _ = load_config(self.config_path)
        config["wxwork_db_dir"] = "C:/current/wxwork"
        config["wxwork_keys_file"] = "C:/current/keys.json"
        config["default_workspace"] = "D:/current/exports"
        config["llm"]["api_key"] = "current-llm-key"
        config["llm"]["model"] = "current-model"
        config["ocr"]["api_key"] = "current-ocr-key"
        config["smart_sheet"]["upload"]["corpid"] = "current-corp-id"
        config["smart_sheet"]["upload"]["corpsecret"] = "current-corp-secret"
        config["smart_sheet"]["templates"][0]["webhook_url"] = (
            "https://qyapi.weixin.qq.com/current-template-hook"
        )
        config["mcp_servers"] = [
            {
                "id": "knowledge",
                "name": "知识库",
                "transport": "streamable-http",
                "url": "https://mcp.example/api",
                "headers": {"Authorization": "Bearer current"},
                "env": {"MCP_TOKEN": "current-env-token"},
            }
        ]
        config["reply_listeners"] = [
            {
                "id": "support",
                "name": "客服群",
                "groupId": "group-1",
                "enabled": True,
                "webhookUrl": (
                    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=current"
                ),
            }
        ]
        config["schedules"] = [
            {
                "id": "daily",
                "name": "每日盘点",
                "enabled": True,
                "promptId": "daily_issue_standard",
                "smartSheetTemplateId": "default",
            }
        ]
        save_config(config, self.config_path)
        seeded, _ = load_config(self.config_path)
        return seeded


class BootstrapTests(ConfigCommandTestCase):
    def test_bootstrap_new_path_returns_normalized_defaults(self):
        result = commands.bootstrap_config(self.config_path)

        self.assertEqual(Path(result["configPath"]), self.config_path.resolve())
        config = result["config"]
        self.assertEqual(config["config_version"], CURRENT_CONFIG_VERSION)
        self.assertEqual(CURRENT_CONFIG_VERSION, 4)
        self.assertGreaterEqual(len(config["ai_strategies"]), 3)
        self.assertEqual(
            {strategy["scene"] for strategy in config["ai_strategies"]},
            {"issue_analysis", "reply", "eq_reply"},
        )
        target_kinds = {target["kind"] for target in config["publish_targets"]}
        self.assertTrue({"excel", "markdown"} <= target_kinds)
        self.assertIn("bindings", config)
        self.assertGreaterEqual(len(config["prompts"]["items"]), 3)
        prompt_ids = [item["id"] for item in config["prompts"]["items"]]
        self.assertIn("daily_issue_standard", prompt_ids)
        self.assertIn(config["prompts"]["default_id"], prompt_ids)
        self.assertGreaterEqual(len(config["smart_sheet"]["templates"]), 1)
        for partition in ("schedules", "mcp_servers", "reply_listeners"):
            self.assertIsInstance(config[partition], list)

    def test_empty_config_path_falls_back_to_default_config_path(self):
        with mock.patch.dict(
            os.environ, {"WECOM_ISSUE_RADAR_CONFIG": str(self.config_path)}
        ):
            result = commands.bootstrap_config("")

        self.assertEqual(Path(result["configPath"]), self.config_path.resolve())

    def test_bootstrap_invalid_json_reports_actionable_error(self):
        self.config_path.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "读取配置失败"):
            commands.bootstrap_config(self.config_path)


class SaveTests(ConfigCommandTestCase):
    def test_save_inherits_missing_runtime_partitions(self):
        seeded = self._seed_current_config()
        incoming, _ = load_config(self.config_path)
        for partition in ("schedules", "mcp_servers", "reply_listeners"):
            incoming.pop(partition)

        result = commands.save_config_command(incoming, self.config_path)
        config = result["config"]

        self.assertEqual([item["id"] for item in config["schedules"]], ["daily"])
        self.assertEqual(
            config["mcp_servers"][0]["headers"]["Authorization"], "Bearer current"
        )
        self.assertEqual(
            config["reply_listeners"][0]["webhookUrl"],
            seeded["reply_listeners"][0]["webhookUrl"],
        )
        persisted, _ = load_config(self.config_path)
        self.assertEqual(persisted["mcp_servers"], config["mcp_servers"])

    def test_save_inherits_empty_runtime_partitions(self):
        self._seed_current_config()
        incoming, _ = load_config(self.config_path)
        incoming["schedules"] = []
        incoming["mcp_servers"] = []
        incoming["reply_listeners"] = []

        config = commands.save_config_command(incoming, self.config_path)["config"]

        self.assertEqual([item["id"] for item in config["schedules"]], ["daily"])
        self.assertEqual([item["id"] for item in config["mcp_servers"]], ["knowledge"])
        self.assertEqual([item["id"] for item in config["reply_listeners"]], ["support"])

    def test_save_keeps_explicit_runtime_partitions(self):
        self._seed_current_config()
        incoming, _ = load_config(self.config_path)
        incoming["schedules"] = [
            {
                "id": "weekly",
                "name": "周报任务",
                "promptId": "daily_issue_standard",
                "smartSheetTemplateId": "default",
            }
        ]
        incoming["mcp_servers"] = [{"id": "other", "name": "新 MCP"}]
        incoming["reply_listeners"] = []

        config = commands.save_config_command(incoming, self.config_path)["config"]

        self.assertEqual([item["id"] for item in config["schedules"]], ["weekly"])
        self.assertEqual([item["id"] for item in config["mcp_servers"]], ["other"])
        # Empty reply_listeners are still inherited from the persisted config.
        self.assertEqual([item["id"] for item in config["reply_listeners"]], ["support"])

    def test_save_with_preserve_schedules_false_replaces_schedules(self):
        self._seed_current_config()
        incoming, _ = load_config(self.config_path)
        incoming["schedules"] = [
            {
                "id": "weekly",
                "name": "周报任务",
                "promptId": "daily_issue_standard",
                "smartSheetTemplateId": "default",
            }
        ]

        config = commands.save_config_command(
            incoming, self.config_path, preserve_schedules=False
        )["config"]
        self.assertEqual([item["id"] for item in config["schedules"]], ["weekly"])

        incoming["schedules"] = []
        config = commands.save_config_command(
            incoming, self.config_path, preserve_schedules=False
        )["config"]
        self.assertEqual(config["schedules"], [])
        persisted, _ = load_config(self.config_path)
        self.assertEqual(persisted["schedules"], [])

    def test_save_rejects_non_boolean_preserve_schedules(self):
        incoming, _ = load_config(self.config_path)
        with self.assertRaisesRegex(ValueError, "preserveSchedules"):
            commands.save_config_command(
                incoming, self.config_path, preserve_schedules="yes"
            )

    def test_save_rejects_non_object_config(self):
        with self.assertRaisesRegex(ValueError, "JSON 对象"):
            commands.save_config_command(["not", "a", "config"], self.config_path)

    def test_save_creates_parent_directories(self):
        nested = self.root / "nested" / "deeper" / "config.local.json"
        incoming, _ = load_config(nested)

        result = commands.save_config_command(incoming, nested)

        self.assertTrue(nested.is_file())
        self.assertEqual(Path(result["configPath"]), nested.resolve())

    def test_save_inheriting_schedules_rejects_dangling_prompt_reference(self):
        self._seed_current_config()
        incoming, _ = load_config(self.config_path)
        incoming.pop("schedules")
        incoming["prompts"]["items"] = [
            {"id": "custom", "name": "自定义提示词", "content": "只保留这一条"}
        ]

        with self.assertRaisesRegex(ValueError, "提示词仍被定时任务"):
            commands.save_config_command(incoming, self.config_path)


class UniqueIdTests(ConfigCommandTestCase):
    def test_save_rejects_duplicate_prompt_ids(self):
        config, _ = load_config(self.config_path)
        config["prompts"]["items"].append(copy.deepcopy(config["prompts"]["items"][0]))

        with self.assertRaisesRegex(ValueError, "提示词"):
            commands.save_config_command(config, self.config_path)

    def test_save_rejects_duplicate_template_ids(self):
        config, _ = load_config(self.config_path)
        config["smart_sheet"]["templates"].append(
            copy.deepcopy(config["smart_sheet"]["templates"][0])
        )

        with self.assertRaisesRegex(ValueError, "腾讯文档模板"):
            commands.save_config_command(config, self.config_path)

    def test_save_rejects_duplicate_schedule_ids(self):
        config, _ = load_config(self.config_path)
        config["schedules"] = [
            {"id": "same", "name": "A", "promptId": "daily_issue_standard"},
            {"id": "same", "name": "B", "promptId": "daily_issue_standard"},
        ]

        with self.assertRaisesRegex(ValueError, "定时任务 ID 不能重复"):
            commands.save_config_command(config, self.config_path)

    def test_save_rejects_empty_schedule_id(self):
        config, _ = load_config(self.config_path)
        config["schedules"] = [{"name": "没有 ID 的任务"}]

        with self.assertRaisesRegex(ValueError, "定时任务 ID 不能为空"):
            commands.save_config_command(config, self.config_path)

    def test_save_rejects_duplicate_mcp_server_and_reply_listener_ids(self):
        config, _ = load_config(self.config_path)
        config["mcp_servers"] = [{"id": "dup"}, {"id": "dup"}]
        config["reply_listeners"] = [{"id": "dup"}, {"id": "dup"}]

        with self.assertRaisesRegex(ValueError, "MCP 服务器"):
            commands.save_config_command(config, self.config_path)

        config["mcp_servers"] = [{"id": "one"}]
        with self.assertRaisesRegex(ValueError, "回复监听"):
            commands.save_config_command(config, self.config_path)


class ExportBackupTests(ConfigCommandTestCase):
    def test_export_backup_redacts_secrets_and_keeps_structure(self):
        self._seed_current_config()
        backup_path = self.root / "backup.json"

        result = commands.export_config_backup(backup_path, self.config_path)

        self.assertEqual(Path(result["path"]), backup_path.resolve())
        backup = json.loads(backup_path.read_text(encoding="utf-8"))
        self.assertEqual(backup["format"], commands.CONFIG_BACKUP_FORMAT)
        self.assertEqual(backup["_backup_version"], 2)
        exported = backup["config"]
        self.assertEqual(exported["wxwork_db_dir"], "")
        self.assertEqual(exported["wxwork_keys_file"], "")
        self.assertEqual(exported["default_workspace"], "")
        self.assertEqual(exported["diagnostics"], {})
        self.assertEqual(exported["llm"]["api_key"], "")
        self.assertEqual(exported["llm"]["model"], "current-model")
        self.assertEqual(exported["ocr"]["api_key"], "")
        self.assertEqual(exported["smart_sheet"]["upload"]["corpsecret"], "")
        self.assertEqual(exported["smart_sheet"]["upload"]["corpid"], "")
        self.assertEqual(exported["smart_sheet"]["templates"][0]["webhook_url"], "")
        self.assertEqual(exported["mcp_servers"][0]["headers"], {"Authorization": ""})
        self.assertEqual(exported["mcp_servers"][0]["env"], {"MCP_TOKEN": ""})
        self.assertEqual(exported["mcp_servers"][0]["url"], "https://mcp.example/api")
        self.assertEqual(exported["mcp_servers"][0]["headers"].__class__, dict)
        self.assertEqual(exported["reply_listeners"][0]["webhookUrl"], "")
        self.assertEqual([item["id"] for item in exported["schedules"]], ["daily"])

        # Exporting must not mutate the active config.
        active, _ = load_config(self.config_path)
        self.assertEqual(active["llm"]["api_key"], "current-llm-key")
        self.assertEqual(
            active["mcp_servers"][0]["headers"]["Authorization"], "Bearer current"
        )

    def test_sanitize_blank_values_keep_json_shapes(self):
        sanitized = commands.sanitize_backup_config(
            {
                "llm": {"api_key": "secret", "model": "keep"},
                "wxwork_db_dir": "C:/private/wxwork",
                "default_workspace": "D:/private/exports",
                "diagnostics": {"agent_execution_logging": True},
                "cookies": {"sid": "abc"},
                "secret": ["a", "b"],
                "access_token": 123,
                "nested": {"client_secret": "s"},
                "mcp_servers": [
                    {
                        "id": "x",
                        "headers": {"Authorization": "Bearer y", "X-Trace-Id": ""},
                        "env": {"MCP_TOKEN": "t"},
                    }
                ],
            }
        )

        self.assertEqual(sanitized["llm"]["api_key"], "")
        self.assertEqual(sanitized["llm"]["model"], "keep")
        self.assertEqual(sanitized["wxwork_db_dir"], "")
        self.assertEqual(sanitized["default_workspace"], "")
        self.assertEqual(sanitized["diagnostics"], {})
        self.assertEqual(sanitized["cookies"], {})
        self.assertEqual(sanitized["secret"], [])
        self.assertEqual(sanitized["access_token"], 0)
        self.assertEqual(sanitized["nested"]["client_secret"], "")
        self.assertEqual(
            sanitized["mcp_servers"][0]["headers"],
            {"Authorization": "", "X-Trace-Id": ""},
        )
        self.assertEqual(sanitized["mcp_servers"][0]["env"], {"MCP_TOKEN": ""})

    def test_export_refuses_to_overwrite_active_config(self):
        self._seed_current_config()
        with self.assertRaisesRegex(ValueError, "不能覆盖"):
            commands.export_config_backup(self.config_path, self.config_path)

    def test_export_creates_parent_directories(self):
        self._seed_current_config()
        backup_path = self.root / "nested" / "backup.json"

        result = commands.export_config_backup(backup_path, self.config_path)

        self.assertTrue(backup_path.is_file())
        self.assertEqual(Path(result["path"]), backup_path.resolve())

    def test_export_requires_a_backup_path(self):
        with self.assertRaisesRegex(ValueError, "备份文件路径"):
            commands.export_config_backup("", self.config_path)


class ImportBackupTests(ConfigCommandTestCase):
    def test_import_restores_active_credentials_and_applies_portable_fields(self):
        self._seed_current_config()
        backup_path = self.root / "backup.json"
        commands.export_config_backup(backup_path, self.config_path)
        backup = json.loads(backup_path.read_text(encoding="utf-8"))
        backup["config"]["llm"]["model"] = "imported-model"
        backup["config"]["prompts"]["items"][0]["name"] = "导入后的提示词"
        backup["config"]["smart_sheet"]["templates"][0]["name"] = "导入后的模板"
        backup_path.write_text(
            json.dumps(backup, ensure_ascii=False), encoding="utf-8"
        )

        result = commands.import_config_backup(backup_path, self.config_path)
        config = result["config"]

        # Portable fields come from the backup.
        self.assertEqual(config["llm"]["model"], "imported-model")
        self.assertEqual(
            config["prompts"]["items"][0]["name"], "导入后的提示词"
        )
        self.assertEqual(
            config["smart_sheet"]["templates"][0]["name"], "导入后的模板"
        )
        # Secrets and machine-local paths are restored from the active config.
        self.assertEqual(config["wxwork_db_dir"], "C:/current/wxwork")
        self.assertEqual(config["wxwork_keys_file"], "C:/current/keys.json")
        self.assertEqual(config["default_workspace"], "D:/current/exports")
        self.assertEqual(config["diagnostics"], {"agent_execution_logging": False})
        self.assertEqual(config["llm"]["api_key"], "current-llm-key")
        self.assertEqual(config["ocr"]["api_key"], "current-ocr-key")
        self.assertEqual(
            config["smart_sheet"]["upload"]["corpsecret"], "current-corp-secret"
        )
        self.assertEqual(
            config["smart_sheet"]["upload"]["corpid"], "current-corp-id"
        )
        self.assertEqual(
            config["smart_sheet"]["templates"][0]["webhook_url"],
            "https://qyapi.weixin.qq.com/current-template-hook",
        )
        self.assertEqual(
            config["mcp_servers"][0]["headers"]["Authorization"], "Bearer current"
        )
        self.assertEqual(
            config["mcp_servers"][0]["env"]["MCP_TOKEN"], "current-env-token"
        )
        self.assertEqual(
            config["reply_listeners"][0]["webhookUrl"],
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=current",
        )
        # The imported config is normalized and immediately usable.
        self.assertEqual(config["config_version"], CURRENT_CONFIG_VERSION)
        self.assertTrue(config["prompts"]["items"])
        self.assertEqual([item["id"] for item in config["schedules"]], ["daily"])
        persisted, _ = load_config(self.config_path)
        self.assertEqual(persisted["llm"]["model"], "imported-model")
        self.assertEqual(persisted["llm"]["api_key"], "current-llm-key")

    def test_restore_matches_partitions_by_id_not_position(self):
        imported = {
            "mcp_servers": [
                {"id": "beta", "headers": {"Authorization": ""}},
                {"id": "alpha", "headers": {"Authorization": ""}},
            ]
        }
        current = {
            "mcp_servers": [
                {"id": "alpha", "headers": {"Authorization": "Bearer alpha"}},
                {"id": "beta", "headers": {"Authorization": "Bearer beta"}},
            ]
        }

        restored = commands.restore_sensitive_values(imported, current)

        self.assertEqual(
            restored["mcp_servers"][0]["headers"]["Authorization"], "Bearer beta"
        )
        self.assertEqual(
            restored["mcp_servers"][1]["headers"]["Authorization"], "Bearer alpha"
        )

    def test_restore_keeps_unknown_sensitive_items_blank(self):
        imported = {
            "llm": {"api_key": ""},
            "mcp_servers": [{"id": "unknown", "headers": {"Authorization": ""}}],
        }
        current = {
            "llm": {"api_key": "current"},
            "mcp_servers": [
                {"id": "known", "headers": {"Authorization": "Bearer known"}}
            ],
        }

        restored = commands.restore_sensitive_values(imported, current)

        self.assertEqual(restored["llm"]["api_key"], "current")
        self.assertEqual(
            restored["mcp_servers"][0]["headers"]["Authorization"], ""
        )

    def test_import_accepts_legacy_v1_backup_with_removed_keys(self):
        self._seed_current_config()
        legacy_backup = {
            "format": commands.CONFIG_BACKUP_FORMAT,
            "backupVersion": 1,
            "config": {
                "config_version": 3,
                "prompts": {
                    "items": [{"id": "daily_issue_standard", "content": "旧提示词"}]
                },
                "smart_sheet": {
                    "templates": [{"id": "default", "name": "默认问题清单"}]
                },
                "llm": {"model": "legacy-model"},
                "mcp_servers": [{"id": "knowledge", "url": "https://legacy.example"}],
                "reply_listeners": [{"id": "support", "groupId": "group-1"}],
                "schedules": [],
            },
        }
        backup_path = self.root / "legacy-backup.json"
        backup_path.write_text(
            json.dumps(legacy_backup, ensure_ascii=False), encoding="utf-8"
        )

        config = commands.import_config_backup(backup_path, self.config_path)["config"]

        self.assertEqual(config["llm"]["model"], "legacy-model")
        self.assertEqual(config["llm"]["api_key"], "current-llm-key")
        self.assertEqual(
            config["mcp_servers"][0]["headers"]["Authorization"], "Bearer current"
        )
        self.assertEqual(
            config["mcp_servers"][0]["env"]["MCP_TOKEN"], "current-env-token"
        )
        self.assertEqual(
            config["reply_listeners"][0]["webhookUrl"],
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=current",
        )

    def test_import_rejects_missing_file(self):
        with self.assertRaisesRegex(ValueError, "找不到配置备份文件"):
            commands.import_config_backup(self.root / "missing.json", self.config_path)

    def test_import_rejects_invalid_json(self):
        backup_path = self.root / "invalid.json"
        backup_path.write_text("this is not json", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "不是有效的 JSON"):
            commands.import_config_backup(backup_path, self.config_path)

    def test_import_rejects_foreign_backup_format(self):
        backup_path = self.root / "foreign.json"
        backup_path.write_text(
            json.dumps({"format": "other-app", "config": {"config_version": 3}}),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "不是企微问题雷达"):
            commands.import_config_backup(backup_path, self.config_path)

    def test_import_rejects_missing_config_object(self):
        backup_path = self.root / "no-config.json"
        backup_path.write_text(
            json.dumps({"_backup_version": 2}), encoding="utf-8"
        )

        with self.assertRaisesRegex(ValueError, "缺少有效的 config 对象"):
            commands.import_config_backup(backup_path, self.config_path)

    def test_import_rejects_unsupported_backup_version(self):
        backup_path = self.root / "future.json"
        backup_path.write_text(
            json.dumps(
                {
                    "format": commands.CONFIG_BACKUP_FORMAT,
                    "_backup_version": 9,
                    "config": {"config_version": 3},
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "暂不支持版本"):
            commands.import_config_backup(backup_path, self.config_path)

    def test_import_rejects_backup_larger_than_16_mb(self):
        self._seed_current_config()
        backup_path = self.root / "huge.json"
        backup_path.write_bytes(b"{}")
        with backup_path.open("r+b") as file:
            file.truncate(commands.MAX_CONFIG_BACKUP_BYTES + 1)

        with self.assertRaisesRegex(ValueError, "16 MB"):
            commands.import_config_backup(backup_path, self.config_path)

    def test_import_size_limit_is_injectable(self):
        self._seed_current_config()
        backup_path = self.root / "small.json"
        backup_path.write_text(
            json.dumps(
                {
                    "format": commands.CONFIG_BACKUP_FORMAT,
                    "_backup_version": 2,
                    "config": {"config_version": 3},
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(ValueError):
            commands.import_config_backup(
                backup_path, self.config_path, max_backup_bytes=4
            )


class DispatchWiringTests(unittest.TestCase):
    def test_dispatch_routes_all_config_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.local.json"
            backup_path = Path(directory) / "backup.json"

            boot = dispatch(
                {"action": "config_bootstrap", "payload": {"configPath": str(path)}}
            )
            self.assertEqual(boot["config"]["config_version"], CURRENT_CONFIG_VERSION)

            saved = dispatch(
                {
                    "action": "config_save",
                    "payload": {"config": boot["config"], "configPath": str(path)},
                }
            )
            self.assertTrue(Path(saved["configPath"]).is_file())

            exported = dispatch(
                {
                    "action": "config_export_backup",
                    "payload": {"path": str(backup_path), "configPath": str(path)},
                }
            )
            self.assertEqual(Path(exported["path"]), backup_path.resolve())

            imported = dispatch(
                {
                    "action": "config_import_backup",
                    "payload": {"path": str(backup_path), "configPath": str(path)},
                }
            )
            self.assertIn("prompts", imported["config"])
            self.assertEqual(
                imported["config"]["config_version"], CURRENT_CONFIG_VERSION
            )

    def test_dispatch_honours_preserve_schedules_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.local.json"
            boot = dispatch(
                {"action": "config_bootstrap", "payload": {"configPath": str(path)}}
            )["config"]
            boot["schedules"] = [
                {
                    "id": "daily",
                    "name": "每日盘点",
                    "promptId": "daily_issue_standard",
                }
            ]
            saved = dispatch(
                {
                    "action": "config_save",
                    "payload": {
                        "config": boot,
                        "configPath": str(path),
                        "preserveSchedules": False,
                    },
                }
            )
            self.assertEqual(
                [item["id"] for item in saved["config"]["schedules"]], ["daily"]
            )

            # Default (true) inherits the persisted schedules from a stale snapshot.
            snapshot = copy.deepcopy(saved["config"])
            snapshot["schedules"] = []
            inherited = dispatch(
                {
                    "action": "config_save",
                    "payload": {"config": snapshot, "configPath": str(path)},
                }
            )
            self.assertEqual(
                [item["id"] for item in inherited["config"]["schedules"]], ["daily"]
            )

            cleared = dispatch(
                {
                    "action": "config_save",
                    "payload": {
                        "config": snapshot,
                        "configPath": str(path),
                        "preserveSchedules": False,
                    },
                }
            )
            self.assertEqual(cleared["config"]["schedules"], [])


if __name__ == "__main__":
    unittest.main()
