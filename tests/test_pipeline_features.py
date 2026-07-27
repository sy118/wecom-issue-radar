from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worker.pipeline.config_store import load_config, save_config
from worker.pipeline.exporter import export_day
from worker.pipeline.llm_analyzer import (
    analyze_batch_with_retry,
    analyze_day,
    build_issue_definitions,
    parse_model_json,
)
from worker.pipeline.smart_sheet import issue_values, preview_sync


class ConfigStoreTests(unittest.TestCase):
    def test_default_prompts_are_added_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.local.json"
            path.write_text('{"prompts":{"items":[]}}', encoding="utf-8")
            config, loaded_path = load_config(path)
            self.assertEqual(loaded_path, path.resolve())
            self.assertGreaterEqual(len(config["prompts"]["items"]), 3)
            saved = save_config(config, path)
            persisted = json.loads(saved.read_text(encoding="utf-8"))
            self.assertEqual(persisted["prompts"]["default_id"], "daily_issue_standard")


class ExportTests(unittest.TestCase):
    def test_exports_complete_chat_and_issue_list(self):
        with tempfile.TemporaryDirectory() as directory:
            day_dir = Path(directory)
            grouped = day_dir / "grouped_issues"
            grouped.mkdir()
            message = {
                "date": "2026-07-23",
                "message_time": "2026-07-23 09:30:00",
                "send_time": 1,
                "conversation_name": "测试群",
                "sender": "张三",
                "type": "文本",
                "raw_text": "系统无法登录",
                "image_count_visible": 1,
                "image_paths": ["C:/temp/example.png"],
                "files": [],
                "message_id": 101,
            }
            (day_dir / "raw_messages.jsonl").write_text(json.dumps(message, ensure_ascii=False) + "\n", encoding="utf-8")
            ocr = {"source_message_id": 101, "ocr_text": "登录失败"}
            (grouped / "image_ocr.jsonl").write_text(json.dumps(ocr, ensure_ascii=False) + "\n", encoding="utf-8")
            definitions = {
                "issues": [
                    {
                        "key": "issue_001_test",
                        "message_time": message["message_time"],
                        "sender": "张三",
                        "module_text": "前端",
                        "issue_category_text": "线上问题",
                        "problem_description": "系统无法登录",
                        "issue_summary_text": "登录失败",
                        "reason": "结论：待排查",
                        "image_refs": [],
                    }
                ]
            }
            (grouped / "issue_definitions_20260723.json").write_text(json.dumps(definitions, ensure_ascii=False), encoding="utf-8")
            outputs = export_day(day_dir, "2026-07-23", "测试群")
            self.assertTrue(Path(outputs["xlsx"]).exists())
            markdown = Path(outputs["markdown"]).read_text(encoding="utf-8")
            self.assertIn("系统无法登录", markdown)
            self.assertIn("截图 OCR", markdown)
            self.assertIn("问题清单", markdown)


class AnalyzerTests(unittest.TestCase):
    def analyze_with_responses(self, responses: list[str]) -> tuple[dict, mock.Mock, list[str]]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        day_dir = Path(temporary.name)
        message = {
            "message_id": 101,
            "message_time": "2026-07-27 10:00:00",
            "send_time": 1,
            "sender": "测试用户",
            "sender_id": 1,
            "raw_text": "系统一直报错，无法提交订单",
            "dedupe_key": "R:test:101",
            "image_count_visible": 0,
        }
        (day_dir / "raw_messages.jsonl").write_text(
            json.dumps(message, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        config, _ = load_config(day_dir / "config.local.json")
        progress: list[str] = []
        with mock.patch(
            "worker.pipeline.llm_analyzer.call_model",
            side_effect=responses,
        ) as call_model:
            definition_path = analyze_day(
                config,
                day_dir,
                "2026-07-27",
                "测试群",
                progress=progress.append,
            )
        document = json.loads(definition_path.read_text(encoding="utf-8"))
        return document, call_model, progress

    def test_cross_day_issue_definition_records_full_range_and_end_date(self):
        messages = [
            {
                "message_id": 1,
                "message_time": "2026-07-23 23:30:00",
                "send_time": 1,
                "sender": "李四",
                "raw_text": "夜间问题",
                "dedupe_key": "R:1:1",
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            result = build_issue_definitions(
                messages=messages,
                model_issues=[{"seed_message_id": 1, "problem_description": "夜间问题"}],
                day_dir=Path(directory),
                date_text="2026-07-24",
                prompt={"id": "p", "name": "测试"},
                start_date="2026-07-23",
                start_time="23:00",
                end_date="2026-07-24",
                end_time="01:00",
            )

        self.assertEqual(result["date"], "2026-07-24")
        self.assertEqual(
            result["range"],
            {
                "startDate": "2026-07-23",
                "startTime": "23:00",
                "endDate": "2026-07-24",
                "endTime": "01:00",
            },
        )

    def test_model_json_and_issue_definition_are_normalized(self):
        parsed = parse_model_json('```json\n{"issues":[{"seed_message_id":1}]}\n```')
        self.assertEqual(parsed["issues"][0]["seed_message_id"], 1)
        messages = [
            {
                "message_id": 1,
                "message_time": "2026-07-23 10:00:00",
                "send_time": 1,
                "sender": "李四",
                "sender_id": 9,
                "raw_text": "查不到订单",
                "dedupe_key": "R:1:1",
                "image_count_visible": 0,
            }
        ]
        config = {
            "smart_sheet": {
                "schema": {
                    "f04Gwj": {"enum": ["订单/售后", "前端"]},
                    "fsFBqK": {"enum": ["线上问题", "待评估"]},
                },
                "defaults": {"module_text": "订单/售后", "issue_category_text": "待评估"},
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            result = build_issue_definitions(
                messages=messages,
                model_issues=[
                    {
                        "seed_message_id": 1,
                        "context_message_ids": [1],
                        "module_text": "模型乱写的模块",
                        "issue_category_text": "线上问题",
                        "problem_description": "订单无法查询",
                        "issue_summary_text": "订单查询失败",
                        "reason": "结论：待排查",
                    }
                ],
                day_dir=Path(directory),
                date_text="2026-07-23",
                prompt={"id": "p", "name": "测试"},
                config=config,
            )
        self.assertEqual(result["issues"][0]["module_text"], "订单/售后")
        self.assertEqual(result["issues"][0]["issue_category_text"], "线上问题")

    def test_model_json_accepts_common_response_wrappers(self):
        payloads = [
            '{"result":{"issues":[{"seed_message_id":1}]}}',
            '{"data":{"output":{"issues":[{"seed_message_id":1}]}}}',
            '{"response":[{"seed_message_id":1}]}',
        ]

        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertEqual(
                    parse_model_json(payload)["issues"][0]["seed_message_id"],
                    1,
                )

    def test_analysis_retries_once_after_malformed_json(self):
        document, call_model, progress = self.analyze_with_responses([
            "模型没有按要求返回 JSON",
            json.dumps({
                "issues": [{
                    "seed_message_id": 101,
                    "context_message_ids": [101],
                    "values": {"problem_description": "无法提交订单"},
                }],
            }, ensure_ascii=False),
        ])

        self.assertEqual(call_model.call_count, 2)
        self.assertEqual(len(document["issues"]), 1)
        self.assertTrue(any("格式异常" in message and "重试" in message for message in progress))

    def test_standard_analysis_response_keeps_the_single_call_path(self):
        document, call_model, progress = self.analyze_with_responses([
            json.dumps({
                "issues": [{
                    "seed_message_id": 101,
                    "context_message_ids": [101],
                    "values": {"problem_description": "无法提交订单"},
                }],
            }, ensure_ascii=False),
        ])

        self.assertEqual(call_model.call_count, 1)
        self.assertEqual(len(document["issues"]), 1)
        self.assertFalse(any("重试" in message for message in progress))

    def test_model_service_errors_are_not_retried(self):
        with mock.patch(
            "worker.pipeline.llm_analyzer.call_model",
            side_effect=RuntimeError("大模型请求失败 HTTP 503"),
        ) as call_model:
            with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                analyze_batch_with_retry(
                    {},
                    "测试提示词",
                    [{"message_id": 101}],
                    batch_index=1,
                    batch_count=1,
                    progress=lambda _message: None,
                )

        self.assertEqual(call_model.call_count, 1)

    def test_analysis_retries_candidates_with_invalid_message_ids(self):
        document, call_model, progress = self.analyze_with_responses([
            json.dumps({
                "issues": [{
                    "seed_message_id": "message_101",
                    "values": {"problem_description": "无法提交订单"},
                }],
            }, ensure_ascii=False),
            json.dumps({
                "issues": [{
                    "seed_message_id": 101,
                    "context_message_ids": [101],
                    "values": {"problem_description": "无法提交订单"},
                }],
            }, ensure_ascii=False),
        ])

        self.assertEqual(call_model.call_count, 2)
        self.assertEqual(len(document["issues"]), 1)
        self.assertTrue(any("消息 ID" in message and "重试" in message for message in progress))

    def test_analysis_rejects_invalid_message_ids_after_the_single_retry(self):
        invalid = json.dumps({
            "issues": [{
                "seed_message_id": "message_101",
                "values": {"problem_description": "无法提交订单"},
            }],
        }, ensure_ascii=False)

        with self.assertRaisesRegex(ValueError, "纠正重试.*有效消息 ID"):
            self.analyze_with_responses([invalid, invalid])

    def test_genuine_empty_issue_array_remains_a_successful_empty_result(self):
        document, call_model, progress = self.analyze_with_responses([
            '{"issues":[]}',
        ])

        self.assertEqual(call_model.call_count, 1)
        self.assertEqual(document["issues"], [])
        self.assertTrue(any("识别 0 个问题" in message for message in progress))


class SmartSheetTests(unittest.TestCase):
    def test_issue_values_omits_an_empty_optional_user_field(self):
        template = {
            "schema": {"fUser": {"title": "负责人", "type": "user"}},
            "field_mappings": [{
                "source_key": "missing_user",
                "target_field_id": "fUser",
                "target_type": "user",
                "required": False,
                "default_value": "",
            }],
        }

        self.assertEqual(issue_values(template, {"key": "a"}, "2026-07-23", []), {})

    def test_preview_blocks_a_default_value_outside_the_target_enum(self):
        with tempfile.TemporaryDirectory() as directory:
            day_dir = Path(directory)
            snapshots = day_dir / "grouped_issues" / "snapshots"
            snapshots.mkdir(parents=True)
            definition_path = snapshots / "issue_definitions_20260723_test.json"
            definition_path.write_text(json.dumps({
                "image_manifest": {},
                "issue_fields": [],
                "issues": [{"key": "a"}],
            }), encoding="utf-8")
            config = {
                "smart_sheet": {
                    "default_template_id": "default",
                    "templates": [{
                        "id": "default",
                        "name": "测试模板",
                        "webhook_url": "https://example.invalid",
                        "schema": {
                            "fStatus": {
                                "title": "状态",
                                "type": "single_select",
                                "enum": ["待处理"],
                            },
                        },
                        "field_mappings": [{
                            "source_key": "missing_status",
                            "target_field_id": "fStatus",
                            "target_type": "single_select",
                            "required": False,
                            "default_value": "待评估",
                        }],
                    }],
                },
            }

            preview = preview_sync(
                config,
                day_dir,
                "2026-07-23",
                definition_path=definition_path,
            )

            self.assertFalse(preview["mapping_valid"])
            self.assertIn("待评估", preview["validation_error"])

    def test_preview_skips_locally_synced_issue_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            day_dir = Path(directory)
            grouped = day_dir / "grouped_issues"
            grouped.mkdir()
            snapshots = grouped / "snapshots"
            snapshots.mkdir()
            definitions = {
                "image_manifest": {},
                "issues": [{"key": "a"}, {"key": "b"}],
            }
            definition_path = snapshots / "issue_definitions_20260723_test.json"
            definition_path.write_text(json.dumps(definitions), encoding="utf-8")
            (day_dir / "smartsheet_desktop_sync_state.json").write_text(
                json.dumps({"synced": {"a": {"record_id": "legacy-a"}}}),
                encoding="utf-8",
            )
            preview = preview_sync(
                {"smart_sheet": {"webhook_url": "https://example.invalid"}},
                day_dir,
                "2026-07-23",
                definition_path=definition_path,
            )
            self.assertEqual(preview["pending"], 1)
            self.assertEqual(preview["already_synced"], 1)


if __name__ == "__main__":
    unittest.main()
