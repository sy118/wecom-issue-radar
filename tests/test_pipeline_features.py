from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from worker.pipeline.config_store import load_config, save_config
from worker.pipeline.exporter import export_day
from worker.pipeline.llm_analyzer import build_issue_definitions, parse_model_json
from worker.pipeline.smart_sheet import preview_sync


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


class SmartSheetTests(unittest.TestCase):
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
