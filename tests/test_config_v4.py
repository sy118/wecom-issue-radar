"""Tests for the config v3→v4 migration and prompts/publish targets split.

Config v4 introduced ``ai_strategies`` (scene-scoped instructions) and
``publish_targets`` (pluggable output destinations) while keeping the legacy
``prompts`` / ``smart_sheet`` partitions as mirrors for the existing pipeline.
These tests pin the migration, mirror consistency, idempotency, backup
redaction/restore and unique-ID rules.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from worker.main import dispatch
from worker.pipeline import config_commands as commands
from worker.pipeline import config_store
from worker.pipeline.config_store import (
    AI_STRATEGY_SCENES,
    CURRENT_CONFIG_VERSION,
    load_config,
    normalize_config,
    save_config,
    validate_unique_config_ids,
)

TENCENT_KIND = "tencent_smartsheet"


def _v3_config() -> dict:
    """A representative v3 config: prompts, one Tencent template, one schedule."""
    return {
        "config_version": 3,
        "llm": {"api_key": "llm-secret", "model": "v3-model"},
        "prompts": {
            "default_id": "daily_issue_standard",
            "default_issue_fields": [],
            "items": [
                {
                    "id": "daily_issue_standard",
                    "name": "标准问题盘点",
                    "description": "识别问题",
                    "content": "整理当天群聊中的问题。",
                    "issue_fields": [],
                    "default_smart_sheet_template_id": "default",
                },
                {
                    "id": "customer_voice",
                    "name": "客户声音",
                    "description": "识别需求",
                    "content": "识别客户需求与体验问题。",
                    "issue_fields": [],
                    "default_smart_sheet_template_id": "default",
                },
            ],
        },
        "smart_sheet": {
            "default_template_id": "default",
            "templates": [
                {
                    "id": "default",
                    "name": "默认问题清单",
                    "url": "https://docs.qq.com/sheet/abc",
                    "webhook_url": "https://qyapi.weixin.qq.com/hook?key=secret",
                    "webhook_url_env": "WECOM_SMARTSHEET_WEBHOOK_URL",
                    "batch_size": 50,
                    "schema": {
                        "f04Gwj": {"title": "*模块", "type": "single_select", "enum": ["A"]},
                    },
                    "field_mappings": [
                        {
                            "source_key": "module_text",
                            "target_field_id": "f04Gwj",
                            "target_type": "single_select",
                            "required": True,
                            "default_value": "A",
                        }
                    ],
                }
            ],
            "upload": {
                "token_endpoint": "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                "corpid": "corp-id",
                "corpsecret": "corp-secret",
            },
        },
        "schedules": [
            {
                "id": "daily",
                "name": "每日盘点",
                "enabled": True,
                "promptId": "daily_issue_standard",
                "smartSheetTemplateId": "default",
            }
        ],
        "mcp_servers": [],
        "reply_listeners": [],
    }


class ConfigV4TestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.root = Path(self._temporary_directory.name)
        self.config_path = self.root / "config.local.json"

    def _write_v3(self, config: dict | None = None) -> dict:
        fixture = config or _v3_config()
        self.config_path.write_text(
            json.dumps(fixture, ensure_ascii=False), encoding="utf-8"
        )
        return fixture

    def _seed_v4(self) -> dict:
        """Materialize a v4 config from the v3 fixture (baseline for partial saves)."""
        self._write_v3()
        config, _ = load_config(self.config_path)
        save_config(config, self.config_path)
        seeded, _ = load_config(self.config_path)
        return seeded

    def _read_persisted(self) -> dict:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def _assert_mirrors_consistent(self, config: dict) -> None:
        self.assertEqual(config["config_version"], 4)
        strategies = config["ai_strategies"]
        targets = config["publish_targets"]
        prompts = config["prompts"]
        smart_sheet = config["smart_sheet"]

        # prompts is the full legacy view of ai_strategies and carries `scene`.
        self.assertEqual(
            [item["id"] for item in prompts["items"]],
            [strategy["id"] for strategy in strategies],
        )
        for item, strategy in zip(prompts["items"], strategies):
            self.assertEqual(item.get("scene"), strategy["scene"])
            self.assertEqual(strategy["name"], item["name"])
            self.assertEqual(strategy["description"], item["description"])
            self.assertEqual(strategy["content"], item["content"])
            if strategy["scene"] == "issue_analysis":
                self.assertEqual(strategy["issue_fields"], item["issue_fields"])
            else:
                self.assertEqual(item["issue_fields"], [])
        issue_ids = [
            strategy["id"]
            for strategy in strategies
            if strategy["scene"] == "issue_analysis"
        ]
        self.assertIn(prompts["default_id"], issue_ids)

        target_kinds = [target["kind"] for target in targets]
        self.assertIn("excel", target_kinds)
        self.assertIn("markdown", target_kinds)
        self.assertIn("excel", [target["id"] for target in targets])
        self.assertIn("markdown", [target["id"] for target in targets])

        tenant_templates = []
        for target in targets:
            if target["kind"] != TENCENT_KIND:
                continue
            template_id = str(target["config"].get("template_id") or "")
            tenant_templates.append((template_id, target))
        self.assertEqual(
            [item["id"] for item in smart_sheet["templates"]],
            [template_id for template_id, _target in tenant_templates],
        )
        for template_id, target in tenant_templates:
            template = next(
                item
                for item in smart_sheet["templates"]
                if item["id"] == template_id
            )
            self.assertEqual(template["name"], target["name"])
            self.assertEqual(template["url"], target["config"]["url"])
            self.assertEqual(template["webhook_url"], target["config"]["webhook_url"])
            self.assertEqual(
                template["webhook_url_env"], target["config"]["webhook_url_env"]
            )
            self.assertEqual(template["batch_size"], target["config"]["batch_size"])
            self.assertEqual(template["schema"], target["config"]["schema"])
        self.assertIn(
            smart_sheet["default_template_id"],
            [item["id"] for item in smart_sheet["templates"]],
        )

        target_ids = [target["id"] for target in targets]
        prompt_ids = [item["id"] for item in prompts["items"]]
        for schedule in config["schedules"]:
            self.assertIn(schedule["publish_target_id"], target_ids)
            if schedule.get("promptId"):
                self.assertIn(schedule["promptId"], prompt_ids)

        self.assertEqual(config["bindings"]["default_prompt_id"], prompts["default_id"])
        self.assertIn(
            config["bindings"]["default_publish_target_id"], target_ids
        )


class MigrationTests(ConfigV4TestCase):
    def test_v3_config_migrates_to_full_v4_structure(self):
        self._write_v3()

        config, _ = load_config(self.config_path)

        self._assert_mirrors_consistent(config)
        scenes = [strategy["scene"] for strategy in config["ai_strategies"]]
        for scene in AI_STRATEGY_SCENES:
            if scene == "issue_analysis":
                self.assertGreaterEqual(scenes.count(scene), 1)
            else:
                self.assertEqual(scenes.count(scene), 1, scene)
        issue_ids = [
            strategy["id"]
            for strategy in config["ai_strategies"]
            if strategy["scene"] == "issue_analysis"
        ]
        self.assertEqual(issue_ids, ["daily_issue_standard", "customer_voice"])

        targets = {target["id"]: target for target in config["publish_targets"]}
        self.assertEqual(targets["excel"]["kind"], "excel")
        self.assertEqual(targets["markdown"]["kind"], "markdown")
        # Migrated Tencent targets keep the legacy template ID (frontend matches
        # its smart_sheet mirror by ID) and store it explicitly in config.
        tencent = targets["default"]
        self.assertEqual(tencent["kind"], TENCENT_KIND)
        self.assertEqual(tencent["config"]["template_id"], "default")
        self.assertEqual(tencent["config"]["url"], "https://docs.qq.com/sheet/abc")
        self.assertEqual(tencent["config"]["batch_size"], 50)

        # Legacy upload settings stay readable through the smart_sheet mirror.
        self.assertEqual(
            config["smart_sheet"]["upload"]["corpsecret"], "corp-secret"
        )
        self.assertEqual(
            config["smart_sheet"]["templates"][0]["webhook_url"],
            "https://qyapi.weixin.qq.com/hook?key=secret",
        )
        # Schedule migrated from the Tencent template reference.
        self.assertEqual(config["schedules"][0]["publish_target_id"], "default")
        self.assertEqual(config["schedules"][0]["smartSheetTemplateId"], "default")

    def test_migration_is_idempotent_across_repeated_loads_and_saves(self):
        self._write_v3()
        first, _ = load_config(self.config_path)
        save_config(first, self.config_path)
        persisted_first = self._read_persisted()

        second, _ = load_config(self.config_path)
        self.assertEqual(
            [strategy["id"] for strategy in second["ai_strategies"]],
            [strategy["id"] for strategy in first["ai_strategies"]],
        )
        self.assertEqual(
            [target["id"] for target in second["publish_targets"]],
            [target["id"] for target in first["publish_targets"]],
        )
        save_config(second, self.config_path)
        persisted_second = self._read_persisted()
        self.assertEqual(persisted_first, persisted_second)
        # Built-in scene strategies are never duplicated.
        scenes = [strategy["scene"] for strategy in second["ai_strategies"]]
        for scene in AI_STRATEGY_SCENES:
            if scene == "issue_analysis":
                self.assertGreaterEqual(scenes.count(scene), 1)
            else:
                self.assertEqual(scenes.count(scene), 1, scene)

    def test_v4_config_with_missing_partitions_is_repaired(self):
        fixture = _v3_config()
        fixture["config_version"] = 4
        self._write_v3(fixture)

        config, _ = load_config(self.config_path)

        self._assert_mirrors_consistent(config)
        self.assertEqual(len(config["ai_strategies"]), 4)
        self.assertEqual(
            {target["kind"] for target in config["publish_targets"]},
            {"excel", "markdown", TENCENT_KIND},
        )

    def test_schedule_defaults_to_excel_when_no_template_reference(self):
        fixture = _v3_config()
        fixture["schedules"] = [
            {"id": "plain", "name": "无模板任务", "promptId": "daily_issue_standard"}
        ]
        self._write_v3(fixture)

        config, _ = load_config(self.config_path)

        self.assertEqual(config["schedules"][0]["publish_target_id"], "excel")
        self._assert_mirrors_consistent(config)

    def test_template_id_colliding_with_base_kind_gets_prefixed_target_id(self):
        fixture = _v3_config()
        fixture["smart_sheet"]["templates"] = [
            {
                "id": "excel",
                "name": "名为 excel 的模板",
                "url": "",
                "webhook_url": "",
                "webhook_url_env": "",
                "batch_size": 50,
                "schema": {},
                "field_mappings": [],
            }
        ]
        fixture["smart_sheet"]["default_template_id"] = "excel"
        self._write_v3(fixture)

        config, _ = load_config(self.config_path)

        target_ids = [target["id"] for target in config["publish_targets"]]
        self.assertIn("excel", target_ids)  # base Excel target
        self.assertIn("tencent:excel", target_ids)  # colliding Tencent template
        self._assert_mirrors_consistent(config)

    def test_v4_streaming_edits_win_over_stale_mirror_on_load(self):
        self._seed_v4()
        persisted = self._read_persisted()
        persisted["ai_strategies"][0]["content"] = "编辑后的策略内容"
        persisted["prompts"]["items"][0]["content"] = "过期的镜像内容"
        self.config_path.write_text(
            json.dumps(persisted, ensure_ascii=False), encoding="utf-8"
        )

        config, _ = load_config(self.config_path)

        issue_strategy = next(
            strategy
            for strategy in config["ai_strategies"]
            if strategy["scene"] == "issue_analysis"
        )
        self.assertEqual(issue_strategy["content"], "编辑后的策略内容")
        self.assertEqual(config["prompts"]["items"][0]["content"], "编辑后的策略内容")
        self._assert_mirrors_consistent(config)


class SaveMergeTests(ConfigV4TestCase):
    def test_prompts_page_style_save_keeps_every_scene(self):
        """The frontend edits prompts.items (with scene) and derives ai_strategies."""
        self._seed_v4()
        incoming, _ = load_config(self.config_path)
        incoming["prompts"]["items"][0]["name"] = "前端改名后的提示词"
        derived = [
            {
                "id": item["id"],
                "name": item["name"],
                "description": item.get("description") or "",
                "scene": item.get("scene") or "issue_analysis",
                "content": item.get("content") or "",
                "issue_fields": item.get("issue_fields") or [],
            }
            for item in incoming["prompts"]["items"]
        ]
        incoming["ai_strategies"] = derived

        config = commands.save_config_command(incoming, self.config_path)["config"]

        self.assertEqual(len(config["ai_strategies"]), len(derived))
        scenes = [strategy["scene"] for strategy in config["ai_strategies"]]
        for scene in AI_STRATEGY_SCENES:
            self.assertGreaterEqual(scenes.count(scene), 1, scene)
        self.assertEqual(
            config["prompts"]["items"][0]["name"], "前端改名后的提示词"
        )
        issue_ids = [
            strategy["id"]
            for strategy in config["ai_strategies"]
            if strategy["scene"] == "issue_analysis"
        ]
        self.assertIn(config["prompts"]["default_id"], issue_ids)
        self._assert_mirrors_consistent(config)

    def test_publish_targets_page_style_save_keeps_target_ids_and_schema(self):
        """PublishTargetsPage rebuilds the mirror keyed by target ID."""
        self._seed_v4()
        incoming, _ = load_config(self.config_path)
        original_schema = None
        mirrored = []
        for target in incoming["publish_targets"]:
            if target["kind"] != TENCENT_KIND:
                continue
            existing = next(
                (
                    template
                    for template in incoming["smart_sheet"]["templates"]
                    if template["id"] == target["id"]
                ),
                None,
            )
            if existing is not None:
                original_schema = existing["schema"]
            config = target["config"]
            mirrored.append(
                {
                    "id": target["id"],
                    "name": target["name"],
                    "url": str(config.get("url") or ""),
                    "webhook_url_env": str(config.get("webhook_url_env") or ""),
                    "webhook_url": str(config.get("webhook_url") or ""),
                    "batch_size": int(config.get("batch_size") or 50),
                    "schema": existing["schema"] if existing else {},
                    "field_mappings": existing["field_mappings"] if existing else [],
                }
            )
        incoming["smart_sheet"]["templates"] = mirrored
        incoming["smart_sheet"]["default_template_id"] = mirrored[0]["id"] if mirrored else ""

        config = commands.save_config_command(incoming, self.config_path)["config"]

        tencent = next(
            target for target in config["publish_targets"] if target["kind"] == TENCENT_KIND
        )
        self.assertEqual(tencent["id"], "default")
        self.assertEqual(tencent["config"]["template_id"], "default")
        self.assertIsNotNone(original_schema)
        self.assertEqual(tencent["config"]["schema"], original_schema)
        self.assertEqual(
            [template["id"] for template in config["smart_sheet"]["templates"]],
            ["default"],
        )
        self._assert_mirrors_consistent(config)

    def test_legacy_prompt_edits_are_folded_into_ai_strategies(self):
        self._seed_v4()
        incoming, _ = load_config(self.config_path)
        incoming["prompts"]["items"][0]["name"] = "改名后的提示词"
        incoming["prompts"]["items"][0]["content"] = "旧页面编辑内容"
        incoming["prompts"]["items"][1]["name"] = "客户声音（旧页面）"

        result = commands.save_config_command(incoming, self.config_path)
        config = result["config"]

        by_id = {strategy["id"]: strategy for strategy in config["ai_strategies"]}
        self.assertEqual(by_id["daily_issue_standard"]["name"], "改名后的提示词")
        self.assertEqual(by_id["daily_issue_standard"]["content"], "旧页面编辑内容")
        self.assertEqual(by_id["customer_voice"]["name"], "客户声音（旧页面）")
        self.assertEqual(config["prompts"]["items"][0]["name"], "改名后的提示词")
        self._assert_mirrors_consistent(config)

    def test_legacy_smart_sheet_edits_are_folded_into_publish_targets(self):
        self._seed_v4()
        incoming, _ = load_config(self.config_path)
        incoming["smart_sheet"]["templates"][0]["name"] = "旧页面改名的模板"
        incoming["smart_sheet"]["templates"][0]["webhook_url"] = "https://new.example/hook"

        config = commands.save_config_command(incoming, self.config_path)["config"]

        target = next(
            item for item in config["publish_targets"] if item["kind"] == TENCENT_KIND
        )
        self.assertEqual(target["name"], "旧页面改名的模板")
        self.assertEqual(target["config"]["webhook_url"], "https://new.example/hook")
        self.assertEqual(
            config["smart_sheet"]["templates"][0]["name"], "旧页面改名的模板"
        )
        self._assert_mirrors_consistent(config)

    def test_new_strategy_and_target_edits_are_canonical(self):
        self._seed_v4()
        incoming, _ = load_config(self.config_path)
        reply = next(
            strategy
            for strategy in incoming["ai_strategies"]
            if strategy["scene"] == "reply"
        )
        reply["content"] = "新的回复策略内容"
        incoming["ai_strategies"].append(
            {
                "id": "reply_formal",
                "name": "正式回复",
                "description": "正式口吻",
                "scene": "reply",
                "content": "请用正式口吻回复。",
                "issue_fields": [],
            }
        )
        incoming["publish_targets"].append(
            {
                "id": "second",
                "name": "第二个腾讯文档",
                "kind": TENCENT_KIND,
                "config": {
                    "template_id": "second",
                    "url": "https://docs.qq.com/sheet/second",
                    "webhook_url": "https://qyapi.weixin.qq.com/hook?key=second",
                    "webhook_url_env": "",
                    "batch_size": 20,
                    "schema": {},
                    "field_mappings": [],
                },
            }
        )

        config = commands.save_config_command(incoming, self.config_path)["config"]

        reply_after = next(
            strategy
            for strategy in config["ai_strategies"]
            if strategy["scene"] == "reply" and strategy["id"] == reply["id"]
        )
        self.assertEqual(reply_after["content"], "新的回复策略内容")
        expected_count = len(incoming["ai_strategies"])
        self.assertEqual(len(config["ai_strategies"]), expected_count)
        # The new Tencent target is reflected in the smart_sheet mirror.
        template_ids = [item["id"] for item in config["smart_sheet"]["templates"]]
        self.assertIn("second", template_ids)
        target_ids = [target["id"] for target in config["publish_targets"]]
        self.assertIn("second", target_ids)
        self._assert_mirrors_consistent(config)

    def test_excel_and_markdown_targets_are_always_present(self):
        self._seed_v4()
        incoming, _ = load_config(self.config_path)
        incoming["publish_targets"] = [
            target
            for target in incoming["publish_targets"]
            if target["kind"] == TENCENT_KIND
        ]

        config = commands.save_config_command(incoming, self.config_path)["config"]

        kinds = [target["kind"] for target in config["publish_targets"]]
        self.assertIn("excel", kinds)
        self.assertIn("markdown", kinds)
        self._assert_mirrors_consistent(config)


class UniqueIdV4Tests(ConfigV4TestCase):
    def test_duplicate_ai_strategy_ids_are_rejected(self):
        config, _ = load_config(self.config_path)
        duplicate = copy.deepcopy(config["ai_strategies"][0])
        duplicate["scene"] = "reply"
        duplicate["id"] = config["ai_strategies"][1]["id"]
        config["ai_strategies"].append(duplicate)

        with self.assertRaisesRegex(ValueError, "AI 策略 ID 不能重复"):
            commands.save_config_command(config, self.config_path)

    def test_duplicate_publish_target_ids_are_rejected(self):
        config, _ = load_config(self.config_path)
        duplicate = copy.deepcopy(config["publish_targets"][0])
        config["publish_targets"].append(duplicate)

        with self.assertRaisesRegex(ValueError, "发布目标 ID 不能重复"):
            commands.save_config_command(config, self.config_path)

    def test_validator_reports_new_partitions(self):
        config, _ = load_config(self.config_path)
        config["ai_strategies"].append(copy.deepcopy(config["ai_strategies"][0]))
        with self.assertRaisesRegex(ValueError, "AI 策略 ID 不能重复"):
            validate_unique_config_ids(config)


class BackupV4Tests(ConfigV4TestCase):
    def test_export_redacts_publish_target_secrets_and_import_restores_them(self):
        self._seed_v4()
        backup_path = self.root / "backup-v4.json"

        commands.export_config_backup(backup_path, self.config_path)
        backup = json.loads(backup_path.read_text(encoding="utf-8"))
        self.assertEqual(backup["_backup_version"], 2)
        exported = backup["config"]
        self.assertEqual(exported["config_version"], 4)
        tencent = next(
            target for target in exported["publish_targets"] if target["kind"] == TENCENT_KIND
        )
        self.assertEqual(tencent["config"]["webhook_url"], "")
        self.assertEqual(tencent["config"]["url"], "https://docs.qq.com/sheet/abc")
        self.assertEqual(exported["smart_sheet"]["upload"]["corpsecret"], "")
        smart_template = next(
            item for item in exported["smart_sheet"]["templates"] if item["id"] == "default"
        )
        self.assertEqual(smart_template["webhook_url"], "")
        self.assertEqual(exported["llm"]["api_key"], "")
        self.assertEqual(exported["smart_sheet"]["upload"]["corpid"], "")

        result = commands.import_config_backup(backup_path, self.config_path)
        config = result["config"]
        self.assertEqual(config["config_version"], 4)
        restored = next(
            target for target in config["publish_targets"] if target["kind"] == TENCENT_KIND
        )
        self.assertEqual(
            restored["config"]["webhook_url"],
            "https://qyapi.weixin.qq.com/hook?key=secret",
        )
        self.assertEqual(
            config["smart_sheet"]["templates"][0]["webhook_url"],
            "https://qyapi.weixin.qq.com/hook?key=secret",
        )
        self.assertEqual(config["llm"]["api_key"], "llm-secret")
        self.assertEqual(config["smart_sheet"]["upload"]["corpsecret"], "corp-secret")
        self._assert_mirrors_consistent(config)

    def test_imported_v3_backup_is_migrated_and_usable(self):
        self._seed_v4()
        legacy_backup = {
            "format": commands.CONFIG_BACKUP_FORMAT,
            "backupVersion": 1,
            "config": _v3_config(),
        }
        backup_path = self.root / "legacy-v3-backup.json"
        backup_path.write_text(
            json.dumps(legacy_backup, ensure_ascii=False), encoding="utf-8"
        )

        config = commands.import_config_backup(backup_path, self.config_path)["config"]

        self._assert_mirrors_consistent(config)
        self.assertEqual(config["config_version"], 4)
        self.assertTrue(config["prompts"]["items"])
        self.assertTrue(config["ai_strategies"])
        persisted, _ = load_config(self.config_path)
        self.assertEqual(persisted["config_version"], 4)

    def test_backup_round_trip_into_fresh_config(self):
        self._seed_v4()
        backup_path = self.root / "portable.json"
        commands.export_config_backup(backup_path, self.config_path)

        fresh_path = self.root / "fresh" / "config.local.json"
        result = commands.import_config_backup(backup_path, fresh_path)
        config = result["config"]

        self.assertEqual(config["config_version"], 4)
        self.assertTrue(fresh_path.is_file())
        self._assert_mirrors_consistent(config)
        # Secrets cannot be recovered from the backup itself on a fresh machine.
        self.assertEqual(config["llm"]["api_key"], "")
        self.assertEqual(config["smart_sheet"]["upload"]["corpsecret"], "")

    def test_bootstrap_materializes_v4_defaults_once(self):
        result = commands.bootstrap_config(self.config_path)
        first = result["config"]
        self.assertTrue(self.config_path.is_file())
        self.assertEqual(first["config_version"], 4)
        saved = self._read_persisted()
        self.assertEqual(saved["config_version"], 4)

        again = commands.bootstrap_config(self.config_path)["config"]
        self.assertEqual(
            [strategy["id"] for strategy in again["ai_strategies"]],
            [strategy["id"] for strategy in first["ai_strategies"]],
        )
        self.assertEqual(self._read_persisted(), saved)


class TransformMirrorTests(ConfigV4TestCase):
    def _tencent_target(self, config: dict) -> dict:
        return next(
            target
            for target in config["publish_targets"]
            if target["kind"] == TENCENT_KIND
        )

    def _transforms(self) -> dict:
        return {
            "f_date": {"mode": "date", "date_format": "YYYY/MM/DD"},
            "f_enum": {"mode": "enum", "enum_map": {"缺陷": "已处理"}},
            "f_text": {"mode": "template", "template": "{{$value}}"},
            "f_const": {"mode": "constant", "constant": "固定值"},
        }

    def test_transforms_are_mirrored_and_survive_round_trips(self):
        self._seed_v4()
        config, _ = load_config(self.config_path)
        self._tencent_target(config)["config"]["transforms"] = self._transforms()

        saved = commands.save_config_command(config, self.config_path)["config"]

        expected = self._transforms()
        self.assertEqual(self._tencent_target(saved)["config"]["transforms"], expected)
        self.assertEqual(saved["smart_sheet"]["templates"][0]["transforms"], expected)
        persisted = self._read_persisted()
        self.assertEqual(
            self._tencent_target(persisted)["config"]["transforms"], expected
        )
        self.assertEqual(
            persisted["smart_sheet"]["templates"][0]["transforms"], expected
        )

        reloaded, _ = load_config(self.config_path)
        commands.save_config_command(reloaded, self.config_path)
        self.assertEqual(self._read_persisted(), persisted)

    def test_legacy_transform_shapes_are_normalized(self):
        self._seed_v4()
        config, _ = load_config(self.config_path)
        self._tencent_target(config)["config"]["transforms"] = {
            "f_date": {"type": "date", "format": "YYYYMMDD"},
            "f_const": {"type": "constant", "value": "X"},
            "f_enum": {"type": "enum", "values": "A=1\nB=2"},
            "f_bad": {"type": "unsupported"},
            "": {"mode": "constant", "constant": "ignored"},
        }

        saved = commands.save_config_command(config, self.config_path)["config"]
        transforms = self._tencent_target(saved)["config"]["transforms"]

        self.assertEqual(
            transforms,
            {
                "f_date": {"mode": "date", "date_format": "YYYYMMDD"},
                "f_const": {"mode": "constant", "constant": "X"},
                "f_enum": {"mode": "enum", "enum_map": {"A": "1", "B": "2"}},
            },
        )
        self.assertEqual(saved["smart_sheet"]["templates"][0]["transforms"], transforms)

    def test_legacy_configs_get_empty_transforms(self):
        self._seed_v4()

        config, _ = load_config(self.config_path)

        self.assertEqual(self._tencent_target(config)["config"]["transforms"], {})
        self.assertEqual(config["smart_sheet"]["templates"][0]["transforms"], {})

    def test_renderer_mirror_without_transforms_keeps_target_transforms(self):
        self._seed_v4()
        config, _ = load_config(self.config_path)
        self._tencent_target(config)["config"]["transforms"] = self._transforms()
        commands.save_config_command(config, self.config_path)

        loaded, _ = load_config(self.config_path)
        # buildPublishSave() rebuilds the legacy mirror without transforms.
        for template in loaded["smart_sheet"]["templates"]:
            template.pop("transforms", None)

        saved = commands.save_config_command(loaded, self.config_path)["config"]

        self.assertEqual(
            self._tencent_target(saved)["config"]["transforms"], self._transforms()
        )
        self.assertEqual(
            saved["smart_sheet"]["templates"][0]["transforms"], self._transforms()
        )

    def test_legacy_template_transforms_migrate_to_publish_target(self):
        fixture = _v3_config()
        fixture["smart_sheet"]["templates"][0]["transforms"] = {
            "f_date": {"mode": "date", "date_format": "YYYY-MM-DD"}
        }
        self._write_v3(fixture)

        config, _ = load_config(self.config_path)

        expected = {"f_date": {"mode": "date", "date_format": "YYYY-MM-DD"}}
        self.assertEqual(self._tencent_target(config)["config"]["transforms"], expected)
        self.assertEqual(config["smart_sheet"]["templates"][0]["transforms"], expected)

    def test_ensure_smart_sheet_config_keeps_transforms(self):
        template = {
            "id": "t1",
            "name": "模板",
            "url": "",
            "webhook_url_env": "",
            "webhook_url": "",
            "batch_size": 50,
            "schema": {},
            "field_mappings": [],
            "transforms": {"f1": {"mode": "constant", "constant": "X"}},
        }

        normalized = config_store.ensure_smart_sheet_config(
            {"smart_sheet": {"templates": [template]}}
        )

        self.assertEqual(
            normalized["smart_sheet"]["templates"][0]["transforms"],
            {"f1": {"mode": "constant", "constant": "X"}},
        )


class DispatchV4Tests(ConfigV4TestCase):
    def test_dispatch_bootstrap_returns_v4_shape(self):
        result = dispatch(
            {
                "action": "config_bootstrap",
                "payload": {"configPath": str(self.config_path)},
            }
        )
        config = result["config"]
        self.assertEqual(config["config_version"], 4)
        self.assertIn("ai_strategies", config)
        self.assertIn("publish_targets", config)
        self.assertIn("bindings", config)


class NormalizeApiTests(ConfigV4TestCase):
    def test_normalize_config_is_version_aware(self):
        legacy = _v3_config()
        normalized = normalize_config(legacy)
        self.assertEqual(normalized["config_version"], CURRENT_CONFIG_VERSION)
        self.assertEqual(
            {strategy["scene"] for strategy in normalized["ai_strategies"]},
            set(AI_STRATEGY_SCENES),
        )
        self.assertTrue(
            {"excel", "markdown"}
            <= {target["kind"] for target in normalized["publish_targets"]}
        )

    def test_current_version_constant_is_four(self):
        self.assertEqual(config_store.CURRENT_CONFIG_VERSION, 4)


if __name__ == "__main__":
    unittest.main()
