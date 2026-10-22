"""Tests for Tencent Smart Sheet field transforms (worker/pipeline/smart_sheet)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worker.pipeline import smart_sheet

DATE_TEXT = "2026-07-23"


def text_mapping(
    target: str,
    source: str,
    *,
    default_value: str = "",
    required: bool = False,
) -> dict:
    return {
        "source_key": source,
        "target_field_id": target,
        "target_type": "text",
        "required": required,
        "default_value": default_value,
    }


def make_template(
    transforms: dict | None = None,
    *,
    mappings: list[dict] | None = None,
    schema: dict | None = None,
) -> dict:
    return {
        "id": "t1",
        "name": "测试模板",
        "schema": schema
        or {
            "f_value": {"title": "补充说明", "type": "text"},
            "f_date": {"title": "日期", "type": "text"},
            "f_enum": {"title": "状态", "type": "text"},
        },
        "field_mappings": mappings
        or [
            text_mapping("f_value", "problem_description"),
            text_mapping("f_date", "$message_time"),
            text_mapping("f_enum", "category"),
        ],
        "transforms": transforms or {},
    }


def make_issue(values: dict | None = None, *, message_time: str = "2026-07-23 09:30:45") -> dict:
    return {
        "key": "issue_1",
        "sender": "张三",
        "message_time": message_time,
        "values": values
        if values is not None
        else {"problem_description": "登录失败", "category": "功能缺陷"},
    }


def values_for(template: dict, issue: dict, warnings: list[str] | None = None) -> dict:
    return smart_sheet.issue_values(
        template, issue, DATE_TEXT, [], warnings=warnings
    )


class TransformTests(unittest.TestCase):
    def test_direct_transform_keeps_the_source_value(self):
        template = make_template({"f_value": {"mode": "direct"}})

        values = values_for(template, make_issue())

        self.assertEqual(values["f_value"], "登录失败")

    def test_constant_transform_overrides_source_and_default(self):
        template = make_template(
            {"f_value": {"mode": "constant", "constant": "固定说明"}},
            mappings=[text_mapping("f_value", "problem_description", default_value="默认值")],
        )

        values = values_for(template, make_issue())

        self.assertEqual(values["f_value"], "固定说明")

    def test_constant_transform_fills_an_empty_optional_field(self):
        template = make_template(
            {"f_value": {"mode": "constant", "constant": "自动补充"}},
            mappings=[text_mapping("f_value", "missing_field")],
        )

        values = values_for(template, make_issue())

        self.assertEqual(values["f_value"], "自动补充")

    def test_constant_transform_can_empty_an_optional_field(self):
        template = make_template(
            {"f_value": {"mode": "constant", "constant": ""}},
        )

        values = values_for(template, make_issue())

        self.assertNotIn("f_value", values)

    def test_template_transform_replaces_source_key_and_dollar_value(self):
        template = make_template(
            {
                "f_value": {
                    "mode": "template",
                    "template": "{{$value}}|{{problem_description}}|{{sender}}|{{$date}}",
                }
            }
        )

        values = values_for(template, make_issue())

        self.assertEqual(values["f_value"], "登录失败|登录失败|张三|2026-07-23")

    def test_template_transform_uses_the_default_value(self):
        template = make_template(
            {"f_value": {"mode": "template", "template": "{{$value}}（默认回填）"}},
            mappings=[text_mapping("f_value", "reason", default_value="原因待确认")],
        )

        values = values_for(template, make_issue(values={"problem_description": "登录失败"}))

        self.assertEqual(values["f_value"], "原因待确认（默认回填）")

    def test_default_value_is_filled_before_the_transform(self):
        template = make_template(
            {"f_value": {"mode": "template", "template": "前缀-{{$value}}"}},
            mappings=[text_mapping("f_value", "reason", default_value="默认原因")],
        )

        values = values_for(template, make_issue(values={"problem_description": "登录失败"}))

        self.assertEqual(values["f_value"], "前缀-默认原因")

    def test_date_transform_formats_supported_tokens(self):
        template = make_template(
            {
                "f_date": {"mode": "date", "date_format": "YYYY/MM/DD HH:mm:ss"},
                "f_value": {"mode": "date", "date_format": "YY-MM-DD HH:mm"},
            },
            mappings=[
                text_mapping("f_date", "$message_time"),
                text_mapping("f_value", "$message_time"),
            ],
        )

        values = values_for(template, make_issue())

        self.assertEqual(values["f_date"], "2026/07/23 09:30:45")
        self.assertEqual(values["f_value"], "26-07-23 09:30")

    def test_date_transform_uses_export_date_when_source_is_empty(self):
        template = make_template(
            {"f_date": {"mode": "date", "date_format": "YYYYMMDD"}},
            mappings=[text_mapping("f_date", "reason")],
        )

        values = values_for(template, make_issue(values={"problem_description": "登录失败"}))

        self.assertEqual(values["f_date"], "20260723")

    def test_date_transform_keeps_original_value_and_records_a_warning(self):
        template = make_template(
            {"f_date": {"mode": "date", "date_format": "YYYY-MM-DD"}},
            mappings=[text_mapping("f_date", "reason")],
        )
        warnings: list[str] = []

        values = values_for(
            template,
            make_issue(values={"problem_description": "登录失败", "reason": "not-a-date"}),
            warnings,
        )

        self.assertEqual(values["f_date"], "not-a-date")
        self.assertEqual(len(warnings), 1)
        self.assertIn("日期", warnings[0])
        self.assertIn("not-a-date", warnings[0])

    def test_date_transform_accepts_iso_datetime(self):
        template = make_template(
            {"f_date": {"mode": "date", "date_format": "YYYY-MM-DD"}},
            mappings=[text_mapping("f_date", "reason")],
        )

        values = values_for(
            template,
            make_issue(values={"reason": "2026-08-01T12:30:00+08:00"}),
        )

        self.assertEqual(values["f_date"], "2026-08-01")

    def test_enum_transform_maps_known_value(self):
        template = make_template(
            {
                "f_enum": {
                    "mode": "enum",
                    "enum_map": {"功能缺陷": "待处理", "需求": "需求评估"},
                }
            }
        )

        values = values_for(template, make_issue())

        self.assertEqual(values["f_enum"], "待处理")

    def test_enum_transform_keeps_original_value_on_miss(self):
        template = make_template(
            {"f_enum": {"mode": "enum", "enum_map": {"功能缺陷": "待处理"}}}
        )

        values = values_for(template, make_issue(values={"category": "咨询"}))

        self.assertEqual(values["f_enum"], "咨询")

    def test_enum_transform_accepts_legacy_line_values(self):
        template = make_template(
            {
                "f_enum": {
                    "type": "enum",
                    "values": "功能缺陷=待评估\n需求=需求评估",
                }
            }
        )

        values = values_for(template, make_issue())

        self.assertEqual(values["f_enum"], "待评估")

    def test_unknown_transform_mode_behaves_like_direct(self):
        template = make_template({"f_value": {"mode": "unsupported"}})

        values = values_for(template, make_issue())

        self.assertEqual(values["f_value"], "登录失败")

    def test_required_empty_value_after_transform_is_rejected(self):
        template = make_template(
            {"f_date": {"mode": "constant", "constant": ""}},
            mappings=[text_mapping("f_date", "$message_time", required=True)],
            schema={"f_date": {"title": "*日期", "type": "text"}},
        )

        values = values_for(template, make_issue())

        with self.assertRaisesRegex(ValueError, "必填字段"):
            smart_sheet.validate_record_values(template, make_issue(), values)

    def test_missing_transforms_keep_legacy_behaviour(self):
        template = make_template()
        template.pop("transforms")

        values = values_for(template, make_issue())

        self.assertEqual(values["f_value"], "登录失败")


class RevisionTests(unittest.TestCase):
    def test_revision_ignores_empty_transforms_and_changes_with_transforms(self):
        base = {
            "id": "t1",
            "name": "测试模板",
            "url": "",
            "webhook_url": "",
            "batch_size": 50,
            "schema": {"f_value": {"title": "补充说明", "type": "text"}},
            "field_mappings": [text_mapping("f_value", "problem_description")],
        }
        base["transforms"] = {}

        self.assertEqual(
            smart_sheet.template_revision(base),
            smart_sheet.template_revision({**base}),
        )

        changed = {**base, "transforms": {"f_value": {"mode": "constant", "constant": "X"}}}
        self.assertNotEqual(
            smart_sheet.template_revision(base),
            smart_sheet.template_revision(changed),
        )


class PreviewWarningTests(unittest.TestCase):
    def test_preview_sync_surfaces_transform_warnings(self):
        template = make_template(
            {"f_date": {"mode": "date", "date_format": "YYYY-MM-DD"}},
            mappings=[text_mapping("f_date", "reason")],
            schema={"f_date": {"title": "日期", "type": "text"}},
        )
        config = {
            "config_version": 4,
            "smart_sheet": {"default_template_id": "t1", "templates": [template]},
        }
        document = {
            "issues": [{"key": "issue_1", "values": {"reason": "bad-date"}}],
            "issue_fields": [{"key": "reason", "label": "原因", "type": "text"}],
            "image_manifest": {},
            "definition_path": "definition.json",
            "prompt": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(smart_sheet, "load_sync_document", return_value=document),
                mock.patch.object(
                    smart_sheet, "load_ledger", return_value={"version": 2, "templates": {}}
                ),
                mock.patch.object(smart_sheet, "summarize_image_integrity", return_value={}),
                mock.patch.object(smart_sheet, "load_raw_messages", return_value=[]),
            ):
                result = smart_sheet.preview_sync(config, Path(directory), DATE_TEXT)

        self.assertTrue(result["mapping_valid"])
        self.assertTrue(result["transform_warnings"])
        self.assertIn("日期", result["transform_warnings"][0])


if __name__ == "__main__":
    unittest.main()
