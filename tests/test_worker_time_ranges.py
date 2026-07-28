from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worker.main import extract_keys_console, handle_preview, handle_run, handle_sync
from worker.pipeline.exporter import export_day
from worker.pipeline.llm_analyzer import NoAnalyzableMessagesError
from worker.pipeline.tasks import prepare_day
from worker.wecom import cache_messages


class WorkerRunRequestTests(unittest.TestCase):
    def test_preview_and_sync_forward_the_frozen_template_revision_contract(self):
        config = {"config_version": 2}
        preview = {
            "pending": 2,
            "already_synced": 0,
            "template_revision": "revision-42",
            "document_revision": "document-24",
            "definition_path": "D:/exports/team/snapshots/definition.json",
        }
        with (
            mock.patch(
                "worker.pipeline.config_store.load_config",
                return_value=(config, Path("config.local.json")),
            ),
            mock.patch(
                "worker.pipeline.smart_sheet.preview_sync",
                return_value=preview,
            ) as preview_sync,
            mock.patch(
                "worker.pipeline.smart_sheet.sync_issues",
                return_value={"synced": 2},
            ) as sync_issues,
        ):
            self.assertEqual(
                handle_preview(
                    {
                        "configPath": "config.local.json",
                        "dayDir": "D:/exports/team",
                        "date": "2026-07-24",
                        "templateId": "incident",
                        "definitionPath": "D:/exports/team/snapshots/definition.json",
                    }
                ),
                preview,
            )
            handle_sync(
                {
                    "configPath": "config.local.json",
                    "dayDir": "D:/exports/team",
                    "date": "2026-07-24",
                    "templateId": "incident",
                    "uploadImages": False,
                    "definitionPath": "D:/exports/team/snapshots/definition.json",
                    "expectedTemplateRevision": "revision-42",
                    "expectedDocumentRevision": "document-24",
                }
            )

        preview_sync.assert_called_once_with(
            config,
            "D:/exports/team",
            "2026-07-24",
            "incident",
            definition_path="D:/exports/team/snapshots/definition.json",
        )
        sync_issues.assert_called_once_with(
            config,
            "D:/exports/team",
            "2026-07-24",
            template_id="incident",
            upload_images=False,
            allow_missing_images=False,
            definition_path="D:/exports/team/snapshots/definition.json",
            expected_template_revision="revision-42",
            expected_document_revision="document-24",
            progress=mock.ANY,
        )

    def test_sync_forwards_the_automatic_missing_image_policy(self):
        config = {"config_version": 2}
        with (
            mock.patch(
                "worker.pipeline.config_store.load_config",
                return_value=(config, Path("config.local.json")),
            ),
            mock.patch(
                "worker.pipeline.smart_sheet.sync_issues",
                return_value={"synced": 1, "image_count": 0},
            ) as sync_issues,
        ):
            handle_sync(
                {
                    "configPath": "config.local.json",
                    "dayDir": "D:/exports/team",
                    "date": "2026-07-24",
                    "templateId": "incident",
                    "definitionPath": "D:/exports/team/snapshots/definition.json",
                    "allowMissingImages": True,
                }
            )

        self.assertTrue(sync_issues.call_args.kwargs["allow_missing_images"])

    def test_run_forwards_the_analysis_snapshot_to_export_and_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            day_dir = root / "groups" / "sales" / "work" / "2026-07-24"
            snapshot = (
                day_dir
                / "grouped_issues"
                / "snapshots"
                / "issue_definitions_20260724_snapshot.json"
            )
            request = {
                "configPath": str(root / "config.local.json"),
                "request": {
                    "date": "2026-07-24",
                    "groups": [{"id": "sales", "name": "销售群"}],
                    "runAnalysis": True,
                    "prepareSmartSheet": True,
                    "smartSheetTemplateId": "incident",
                    "exportMarkdown": True,
                },
            }
            with (
                mock.patch(
                    "worker.pipeline.config_store.load_config",
                    return_value=({}, root / "config.local.json"),
                ),
                mock.patch(
                    "worker.pipeline.tasks.prepare_day",
                    return_value=(day_dir, ""),
                ),
                mock.patch(
                    "worker.pipeline.llm_analyzer.analyze_day",
                    return_value=snapshot,
                ) as analyze_day,
                mock.patch(
                    "worker.pipeline.exporter.export_day",
                    return_value={"markdown": str(root / "report.md")},
                ) as export_day,
                mock.patch(
                    "worker.pipeline.smart_sheet.preview_sync",
                    return_value={
                        "template_id": "incident",
                        "template_name": "故障模板",
                        "template_url": "https://docs.qq.com/sheet/incident",
                        "definition_path": str(snapshot),
                        "document_revision": "document-revision",
                    },
                ) as preview_sync,
            ):
                result = handle_run(request)

            analyze_day.assert_called_once()
            self.assertEqual(export_day.call_args.kwargs["definition_path"], snapshot)
            preview_sync.assert_called_once_with(
                {},
                day_dir,
                "2026-07-24",
                "incident",
                definition_path=snapshot,
            )
            self.assertEqual(result["definitionPath"], str(snapshot))

    def test_cross_day_request_exports_one_range_and_uses_end_date_for_smart_sheet(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.local.json"
            config_path.write_text(
                json.dumps({"default_workspace": str(root)}),
                encoding="utf-8",
            )
            request = {
                "configPath": str(config_path),
                "request": {
                    "startDate": "2026-07-23",
                    "endDate": "2026-07-24",
                    "startTime": "23:00",
                    "endTime": "01:00",
                    "groups": [{"id": "sales-room", "name": "销售群"}],
                    "exportMarkdown": True,
                },
            }
            cache_config = {
                "timezone": "Asia/Shanghai",
                "target_group_id": "sales-room",
                "target_group_name": "销售群",
            }
            conversation = {"display_name": "销售群", "last_message_time": 0}
            resolver = mock.Mock()
            resolver.find_files_for_messages.return_value = {}

            with (
                mock.patch.object(cache_messages, "load_config", return_value=cache_config),
                mock.patch.object(cache_messages, "get_conversation_state", return_value=conversation),
                mock.patch.object(cache_messages, "read_messages", return_value=[]),
                mock.patch.object(cache_messages, "load_user_map", return_value={}),
                mock.patch.object(cache_messages, "load_member_names", return_value={}),
                mock.patch.object(cache_messages, "FileResolver", return_value=resolver),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = handle_run(request)

            run = result["runs"][0]
            markdown_path = Path(run["outputs"]["markdown"])
            markdown_text = markdown_path.read_text(encoding="utf-8")

        self.assertEqual(Path(run["dayDir"]).parent.name, "work")
        self.assertEqual(Path(run["dayDir"]).name, "2026-07-23_to_2026-07-24")
        self.assertEqual(run["startDate"], "2026-07-23")
        self.assertEqual(run["endDate"], "2026-07-24")
        self.assertEqual(run["smartSheetDate"], "2026-07-24")
        self.assertEqual(
            markdown_path.name,
            "2026-07-23_2300--2026-07-24_0100_销售群_聊天与问题盘点.md",
        )
        self.assertIn(
            "聊天范围：2026-07-23 23:00–2026-07-24 01:00",
            markdown_text,
        )


    def test_multiple_groups_run_in_selection_order_with_the_same_time_range(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            day_dirs = [root / "sales" / "2026-07-23", root / "support" / "2026-07-23"]
            request = {
                "configPath": str(root / "config.local.json"),
                "request": {
                    "date": "2026-07-23",
                    "startTime": "09:15",
                    "endTime": "10:30",
                    "groups": [
                        {"id": "sales-room", "name": "销售群"},
                        {"id": "support-room", "name": "客服群"},
                    ],
                    "exportXlsx": True,
                    "exportMarkdown": True,
                },
            }

            with (
                mock.patch("worker.pipeline.config_store.load_config", return_value=({}, root / "config.local.json")),
                mock.patch(
                    "worker.pipeline.tasks.prepare_day",
                    side_effect=[(day_dirs[0], "sales output"), (day_dirs[1], "support output")],
                ) as prepare_day,
                mock.patch(
                    "worker.pipeline.exporter.export_day",
                    side_effect=[
                        {"xlsx": str(root / "sales.xlsx"), "markdown": str(root / "sales.md")},
                        {"xlsx": str(root / "support.xlsx"), "markdown": str(root / "support.md")},
                    ],
                ) as export_day,
            ):
                result = handle_run(request)

            self.assertEqual(
                [(run["groupId"], run["groupName"]) for run in result["runs"]],
                [("sales-room", "销售群"), ("support-room", "客服群")],
            )
            self.assertEqual(result["runs"][0]["dayDir"], str(day_dirs[0]))
            self.assertEqual(result["runs"][1]["outputs"]["xlsx"], str(root / "support.xlsx"))
            self.assertEqual(prepare_day.call_count, 2)
            for call in prepare_day.call_args_list:
                self.assertEqual(call.kwargs["start_time"], "09:15")
                self.assertEqual(call.kwargs["end_time"], "10:30")
            self.assertEqual(export_day.call_count, 2)
            for call in export_day.call_args_list:
                self.assertEqual(call.kwargs["start_time"], "09:15")
                self.assertEqual(call.kwargs["end_time"], "10:30")

    def test_empty_first_group_does_not_block_later_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty_dir = root / "empty" / "2026-07-23"
            success_dir = root / "support" / "2026-07-23"
            snapshot = success_dir / "grouped_issues" / "snapshots" / "issues.json"
            request = {
                "configPath": str(root / "config.local.json"),
                "request": {
                    "date": "2026-07-23",
                    "groups": [
                        {"id": "empty-room", "name": "空群"},
                        {"id": "support-room", "name": "客服群"},
                    ],
                    "runAnalysis": True,
                    "exportXlsx": True,
                },
            }

            with (
                mock.patch(
                    "worker.pipeline.config_store.load_config",
                    return_value=({}, root / "config.local.json"),
                ),
                mock.patch(
                    "worker.pipeline.tasks.prepare_day",
                    side_effect=[(empty_dir, ""), (success_dir, "")],
                ) as prepare_day,
                mock.patch(
                    "worker.pipeline.llm_analyzer.analyze_day",
                    side_effect=[NoAnalyzableMessagesError("空群没有可分析的聊天记录"), snapshot],
                ),
                mock.patch(
                    "worker.pipeline.tasks.issue_count",
                    return_value=2,
                ),
                mock.patch(
                    "worker.pipeline.exporter.export_day",
                    return_value={"xlsx": str(root / "support.xlsx")},
                ) as export_day,
            ):
                result = handle_run(request)

        self.assertEqual(prepare_day.call_count, 2)
        self.assertEqual(export_day.call_count, 1)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["successCount"], 1)
        self.assertEqual(result["emptyCount"], 1)
        self.assertEqual(result["failedCount"], 0)
        self.assertEqual(
            [(run["groupName"], run["status"]) for run in result["runs"]],
            [("空群", "empty"), ("客服群", "success")],
        )

    def test_failed_first_group_is_reported_without_blocking_later_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failed_dir = root / "failed" / "2026-07-23"
            success_dir = root / "support" / "2026-07-23"
            snapshot = success_dir / "grouped_issues" / "snapshots" / "issues.json"
            request = {
                "configPath": str(root / "config.local.json"),
                "request": {
                    "date": "2026-07-23",
                    "groups": [
                        {"id": "failed-room", "name": "故障群"},
                        {"id": "support-room", "name": "客服群"},
                    ],
                    "runAnalysis": True,
                    "exportXlsx": True,
                },
            }

            with (
                mock.patch(
                    "worker.pipeline.config_store.load_config",
                    return_value=({}, root / "config.local.json"),
                ),
                mock.patch(
                    "worker.pipeline.tasks.prepare_day",
                    side_effect=[(failed_dir, ""), (success_dir, "")],
                ) as prepare_day,
                mock.patch(
                    "worker.pipeline.llm_analyzer.analyze_day",
                    side_effect=[RuntimeError("模型服务失败"), snapshot],
                ),
                mock.patch("worker.pipeline.tasks.issue_count", return_value=1),
                mock.patch(
                    "worker.pipeline.exporter.export_day",
                    return_value={"xlsx": str(root / "support.xlsx")},
                ) as export_day,
            ):
                result = handle_run(request)

        self.assertEqual(prepare_day.call_count, 2)
        self.assertEqual(export_day.call_count, 1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failedCount"], 1)
        self.assertEqual(
            [(run["status"], run["error"]) for run in result["runs"]],
            [("failed", "模型服务失败"), ("success", "")],
        )

    def test_prepare_failure_in_first_group_does_not_block_later_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            success_dir = root / "support" / "2026-07-23"
            snapshot = success_dir / "grouped_issues" / "snapshots" / "issues.json"
            request = {
                "configPath": str(root / "config.local.json"),
                "request": {
                    "date": "2026-07-23",
                    "groups": [
                        {"id": "locked-room", "name": "占用群"},
                        {"id": "support-room", "name": "客服群"},
                    ],
                    "runAnalysis": True,
                    "exportXlsx": True,
                },
            }

            with (
                mock.patch(
                    "worker.pipeline.config_store.load_config",
                    return_value=({}, root / "config.local.json"),
                ),
                mock.patch(
                    "worker.pipeline.tasks.prepare_day",
                    side_effect=[PermissionError("数据库文件正在使用"), (success_dir, "")],
                ) as prepare_day,
                mock.patch(
                    "worker.pipeline.llm_analyzer.analyze_day",
                    return_value=snapshot,
                ),
                mock.patch("worker.pipeline.tasks.issue_count", return_value=1),
                mock.patch(
                    "worker.pipeline.exporter.export_day",
                    return_value={"xlsx": str(root / "support.xlsx")},
                ) as export_day,
            ):
                result = handle_run(request)

        self.assertEqual(prepare_day.call_count, 2)
        self.assertEqual(export_day.call_count, 1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failedCount"], 1)
        self.assertEqual(result["runs"][0]["dayDir"], "")
        self.assertEqual(
            [run["status"] for run in result["runs"]],
            ["failed", "success"],
        )

    def test_all_empty_groups_are_reported_as_empty_not_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = {
                "configPath": str(root / "config.local.json"),
                "request": {
                    "date": "2026-07-23",
                    "groups": [
                        {"id": "sales-room", "name": "销售群"},
                        {"id": "support-room", "name": "客服群"},
                    ],
                    "runAnalysis": True,
                    "exportXlsx": True,
                },
            }

            with (
                mock.patch(
                    "worker.pipeline.config_store.load_config",
                    return_value=({}, root / "config.local.json"),
                ),
                mock.patch(
                    "worker.pipeline.tasks.prepare_day",
                    side_effect=[(root / "sales", ""), (root / "support", "")],
                ) as prepare_day,
                mock.patch(
                    "worker.pipeline.llm_analyzer.analyze_day",
                    side_effect=[
                        NoAnalyzableMessagesError("销售群没有可分析的聊天记录"),
                        NoAnalyzableMessagesError("客服群没有可分析的聊天记录"),
                    ],
                ),
                mock.patch("worker.pipeline.exporter.export_day") as export_day,
            ):
                result = handle_run(request)

        self.assertEqual(prepare_day.call_count, 2)
        export_day.assert_not_called()
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["successCount"], 0)
        self.assertEqual(result["emptyCount"], 2)
        self.assertEqual(result["failedCount"], 0)
        self.assertEqual(
            [run["status"] for run in result["runs"]],
            ["empty", "empty"],
        )

    def test_all_failed_groups_return_a_complete_failure_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = {
                "configPath": str(root / "config.local.json"),
                "request": {
                    "date": "2026-07-23",
                    "groups": [
                        {"id": "sales-room", "name": "销售群"},
                        {"id": "support-room", "name": "客服群"},
                    ],
                    "runAnalysis": True,
                    "exportXlsx": True,
                },
            }

            with (
                mock.patch(
                    "worker.pipeline.config_store.load_config",
                    return_value=({}, root / "config.local.json"),
                ),
                mock.patch(
                    "worker.pipeline.tasks.prepare_day",
                    side_effect=[
                        (root / "sales", ""),
                        (root / "support", ""),
                    ],
                ) as prepare_day,
                mock.patch(
                    "worker.pipeline.llm_analyzer.analyze_day",
                    side_effect=[RuntimeError("销售分析失败"), RuntimeError("客服分析失败")],
                ),
                mock.patch("worker.pipeline.exporter.export_day") as export_day,
            ):
                result = handle_run(request)

        self.assertEqual(prepare_day.call_count, 2)
        export_day.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["successCount"], 0)
        self.assertEqual(result["emptyCount"], 0)
        self.assertEqual(result["failedCount"], 2)
        self.assertEqual(
            [run["error"] for run in result["runs"]],
            ["销售分析失败", "客服分析失败"],
        )

    def test_analysis_result_reports_zero_issues_per_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            day_dir = root / "support" / "2026-07-27"
            request = {
                "configPath": str(root / "config.local.json"),
                "request": {
                    "date": "2026-07-27",
                    "groups": [{"id": "support-room", "name": "客服群"}],
                    "runAnalysis": True,
                    "exportXlsx": True,
                },
            }

            with (
                mock.patch(
                    "worker.pipeline.config_store.load_config",
                    return_value=({}, root / "config.local.json"),
                ),
                mock.patch(
                    "worker.pipeline.tasks.prepare_day",
                    return_value=(day_dir, ""),
                ),
                mock.patch(
                    "worker.pipeline.llm_analyzer.analyze_day",
                    return_value=day_dir / "grouped_issues" / "snapshot.json",
                ),
                mock.patch(
                    "worker.pipeline.tasks.issue_count",
                    return_value=0,
                ) as issue_count,
                mock.patch(
                    "worker.pipeline.exporter.export_day",
                    return_value={"xlsx": str(root / "support.xlsx")},
                ),
            ):
                result = handle_run(request)

            self.assertEqual(result["runs"][0]["issueCount"], 0)
            issue_count.assert_called_once_with(day_dir, "2026-07-27")

    def test_legacy_single_group_request_keeps_top_level_result_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            day_dir = root / "legacy" / "2026-07-23"
            request = {
                "configPath": str(root / "config.local.json"),
                "request": {
                    "date": "2026-07-23",
                    "groupId": "legacy-room",
                    "groupName": "旧版群",
                    "exportXlsx": True,
                },
            }

            with (
                mock.patch("worker.pipeline.config_store.load_config", return_value=({}, root / "config.local.json")),
                mock.patch("worker.pipeline.tasks.prepare_day", return_value=(day_dir, "")),
                mock.patch("worker.pipeline.exporter.export_day", return_value={"xlsx": str(root / "legacy.xlsx")}),
            ):
                result = handle_run(request)

            self.assertEqual(len(result["runs"]), 1)
            self.assertEqual(result["groupId"], "legacy-room")
            self.assertEqual(result["groupName"], "旧版群")
            self.assertEqual(result["dayDir"], str(day_dir))
            self.assertEqual(result["outputs"], result["runs"][0]["outputs"])


class KeyExtractionConsoleTests(unittest.TestCase):
    def test_failure_shows_guidance_without_traceback_or_runtime_details(self):
        stdout = io.StringIO()
        with (
            mock.patch(
                "worker.wecom.extract_keys.main",
                side_effect=RuntimeError("secret runtime detail"),
            ),
            mock.patch("builtins.input", return_value=""),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = extract_keys_console()

        output = stdout.getvalue()
        self.assertEqual(exit_code, 1)
        self.assertIn("密钥提取未完成", output)
        self.assertNotIn("Traceback", output)
        self.assertNotIn("secret runtime detail", output)


class CacheMessagesCliTests(unittest.TestCase):
    def test_empty_first_snapshot_is_retried_before_reporting_no_messages(self):
        config = {
            "timezone": "Asia/Shanghai",
            "target_group_id": "sales-room",
            "target_group_name": "销售群",
        }
        conversation = {
            "display_name": "销售群",
            "last_message_time": 1784769300,
        }
        message = {
            "source_table": "message_table",
            "message_id": 101,
            "server_id": 201,
            "sequence": 1,
            "sender_id": 301,
            "conversation_id": "sales-room",
            "content_type": 1,
            "send_time": 1784769300,
            "flag": 0,
            "content_raw": "订单提交失败".encode("utf-8"),
            "extra_content_raw": b"",
            "local_extra_content_raw": b"",
        }
        resolver = mock.Mock()
        resolver.find_files_for_messages.return_value = {}

        with tempfile.TemporaryDirectory() as directory:
            stdout = io.StringIO()
            argv = [
                "cache_messages.py",
                "--workspace",
                directory,
                "--date",
                "2026-07-23",
                "--conversation-id",
                "sales-room",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(cache_messages, "load_config", return_value=config),
                mock.patch.object(cache_messages, "get_conversation_state", return_value=conversation),
                mock.patch.object(cache_messages, "read_messages", side_effect=[[], [message]]) as read_messages,
                mock.patch.object(cache_messages, "load_user_map", return_value={301: "测试用户"}),
                mock.patch.object(cache_messages, "load_member_names", return_value={}),
                mock.patch.object(cache_messages, "FileResolver", return_value=resolver),
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = cache_messages.main()

            payload = json.loads(stdout.getvalue())
            exported = [
                json.loads(line)
                for line in Path(payload["raw_messages"]).read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(exit_code, 0)
        self.assertEqual(read_messages.call_count, 2)
        self.assertEqual(payload["message_count"], 1)
        self.assertEqual(exported[0]["message_id"], 101)

    def test_transient_image_source_is_retried_within_one_run(self):
        config = {
            "timezone": "Asia/Shanghai",
            "target_group_id": "sales-room",
            "target_group_name": "销售群",
        }
        conversation = {
            "display_name": "销售群",
            "last_message_time": 1784769300,
        }
        message = {
            "source_table": "message_table",
            "message_id": 101,
            "server_id": 201,
            "sequence": 1,
            "sender_id": 301,
            "conversation_id": "sales-room",
            "content_type": 4,
            "send_time": 1784769300,
            "flag": 0,
            "content_raw": b"",
            "extra_content_raw": b"",
            "local_extra_content_raw": b"late.png",
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "late.png"
            source.write_bytes(b"transient-image")
            resolver = mock.Mock()
            resolver.find_files_for_messages.return_value = {
                101: [
                    {
                        "message_id": 101,
                        "server_id": "201",
                        "name": "late.png",
                        "md5": "",
                        "size": source.stat().st_size,
                        "extension_type": 0,
                        "category": "Image",
                    }
                ]
            }
            resolver.source_path_for.side_effect = [None, source]
            stdout = io.StringIO()
            argv = [
                "cache_messages.py",
                "--workspace",
                str(root / "workspace"),
                "--date",
                "2026-07-23",
                "--conversation-id",
                "sales-room",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(cache_messages, "load_config", return_value=config),
                mock.patch.object(cache_messages, "get_conversation_state", return_value=conversation),
                mock.patch.object(cache_messages, "read_messages", return_value=[message]),
                mock.patch.object(cache_messages, "load_user_map", return_value={301: "测试用户"}),
                mock.patch.object(cache_messages, "load_member_names", return_value={}),
                mock.patch.object(cache_messages, "FileResolver", return_value=resolver),
                mock.patch.object(cache_messages.time, "sleep") as sleep,
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = cache_messages.main()

            payload = json.loads(stdout.getvalue())
            manifest = json.loads(Path(payload["image_manifest"]).read_text(encoding="utf-8"))
            exported = [
                json.loads(line)
                for line in Path(payload["raw_messages"]).read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(manifest["records"]), 1)
        self.assertEqual(len(exported[0]["images"]), 1)
        self.assertEqual(resolver.source_path_for.call_count, 2)
        sleep.assert_called_once_with(cache_messages.IMAGE_SOURCE_RETRY_DELAYS_SECONDS[0])

    def test_permanently_missing_images_share_one_bounded_retry_budget(self):
        config = {
            "timezone": "Asia/Shanghai",
            "target_group_id": "sales-room",
            "target_group_name": "销售群",
        }
        conversation = {
            "display_name": "销售群",
            "last_message_time": 1784769301,
        }
        messages = [
            {
                "source_table": "message_table",
                "message_id": message_id,
                "server_id": 200 + message_id,
                "sequence": index,
                "sender_id": 301,
                "conversation_id": "sales-room",
                "content_type": 4,
                "send_time": 1784769300 + index,
                "flag": 0,
                "content_raw": b"",
                "extra_content_raw": b"",
                "local_extra_content_raw": f"missing-{index}.png".encode(),
            }
            for index, message_id in enumerate((101, 102), start=1)
        ]
        resolver = mock.Mock()
        resolver.find_files_for_messages.return_value = {
            message["message_id"]: [
                {
                    "message_id": message["message_id"],
                    "server_id": str(message["server_id"]),
                    "name": f"missing-{index}.png",
                    "md5": "",
                    "size": 0,
                    "extension_type": 0,
                    "category": "Image",
                }
            ]
            for index, message in enumerate(messages, start=1)
        }
        resolver.source_path_for.return_value = None

        with tempfile.TemporaryDirectory() as directory:
            stdout = io.StringIO()
            argv = [
                "cache_messages.py",
                "--workspace",
                directory,
                "--date",
                "2026-07-23",
                "--conversation-id",
                "sales-room",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(cache_messages, "load_config", return_value=config),
                mock.patch.object(cache_messages, "get_conversation_state", return_value=conversation),
                mock.patch.object(cache_messages, "read_messages", return_value=messages),
                mock.patch.object(cache_messages, "load_user_map", return_value={301: "测试用户"}),
                mock.patch.object(cache_messages, "load_member_names", return_value={}),
                mock.patch.object(cache_messages, "FileResolver", return_value=resolver),
                mock.patch.object(cache_messages.time, "sleep") as sleep,
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = cache_messages.main()

            payload = json.loads(stdout.getvalue())
            manifest = json.loads(Path(payload["image_manifest"]).read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(manifest["records"], [])
        self.assertEqual(
            sleep.call_args_list,
            [mock.call(delay) for delay in cache_messages.IMAGE_SOURCE_RETRY_DELAYS_SECONDS],
        )
        self.assertEqual(
            resolver.source_path_for.call_count,
            len(messages) + len(cache_messages.IMAGE_SOURCE_RETRY_DELAYS_SECONDS),
        )

    def test_cross_day_range_uses_inclusive_datetime_bounds_and_range_directory(self):
        config = {
            "timezone": "Asia/Shanghai",
            "target_group_id": "sales-room",
            "target_group_name": "销售群",
        }
        conversation = {"display_name": "销售群", "last_message_time": 0}
        resolver = mock.Mock()
        resolver.find_files_for_messages.return_value = {}

        with tempfile.TemporaryDirectory() as directory:
            stdout = io.StringIO()
            argv = [
                "cache_messages.py",
                "--workspace",
                directory,
                "--start-date",
                "2026-07-23",
                "--end-date",
                "2026-07-24",
                "--conversation-id",
                "sales-room",
                "--start-time",
                "23:00",
                "--end-time",
                "01:00",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(cache_messages, "load_config", return_value=config),
                mock.patch.object(cache_messages, "get_conversation_state", return_value=conversation),
                mock.patch.object(cache_messages, "read_messages", return_value=[]) as read_messages,
                mock.patch.object(cache_messages, "load_user_map", return_value={}),
                mock.patch.object(cache_messages, "load_member_names", return_value={}),
                mock.patch.object(cache_messages, "FileResolver", return_value=resolver),
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = cache_messages.main()

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        read_messages.assert_called_once_with(
            config,
            "sales-room",
            1784818800,  # 2026-07-23 23:00:00 Asia/Shanghai
            1784826059,  # 2026-07-24 01:00:59 Asia/Shanghai
            0,
        )
        self.assertEqual(Path(payload["day_dir"]).parent.name, "work")
        self.assertEqual(Path(payload["day_dir"]).name, "2026-07-23_to_2026-07-24")

    def test_selected_group_and_minute_range_are_forwarded_as_inclusive_epoch_bounds(self):
        config = {
            "timezone": "Asia/Shanghai",
            "target_group_id": "configured-room",
            "target_group_name": "配置群",
        }
        conversation = {"display_name": "销售群", "last_message_time": 0}
        resolver = mock.Mock()
        resolver.find_files_for_messages.return_value = {}

        with tempfile.TemporaryDirectory() as directory:
            argv = [
                "cache_messages.py",
                "--workspace",
                directory,
                "--date",
                "2026-07-23",
                "--conversation-id",
                "sales-room",
                "--start-time",
                "09:15",
                "--end-time",
                "10:30",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(cache_messages, "load_config", return_value=config),
                mock.patch.object(cache_messages, "get_conversation_state", return_value=conversation) as get_state,
                mock.patch.object(cache_messages, "read_messages", return_value=[]) as read_messages,
                mock.patch.object(cache_messages, "load_user_map", return_value={}),
                mock.patch.object(cache_messages, "load_member_names", return_value={}),
                mock.patch.object(cache_messages, "FileResolver", return_value=resolver),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = cache_messages.main()

        self.assertEqual(exit_code, 0)
        get_state.assert_called_once_with(config, "sales-room")
        read_messages.assert_called_once_with(
            config,
            "sales-room",
            1784769300,  # 2026-07-23 09:15:00 Asia/Shanghai
            1784773859,  # 2026-07-23 10:30:59 Asia/Shanghai
            0,
        )

    def test_time_arguments_require_strict_two_digit_24_hour_format(self):
        argv = ["cache_messages.py", "--start-time", "9:00"]
        with (
            mock.patch.object(sys, "argv", argv),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            cache_messages.parse_args()
        self.assertEqual(raised.exception.code, 2)

    def test_end_time_cannot_precede_start_time(self):
        config = {"timezone": "Asia/Shanghai", "target_group_id": "configured-room"}
        argv = [
            "cache_messages.py",
            "--date",
            "2026-07-23",
            "--start-time",
            "18:00",
            "--end-time",
            "09:00",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(cache_messages, "load_config", return_value=config),
            self.assertRaisesRegex(ValueError, "结束时间不能早于开始时间"),
        ):
            cache_messages.main()

    def test_cached_messages_outside_the_selected_range_are_not_exported(self):
        config = {"timezone": "Asia/Shanghai", "target_group_id": "sales-room"}
        conversation = {"display_name": "销售群", "last_message_time": 0}
        resolver = mock.Mock()
        resolver.find_files_for_messages.return_value = {}

        with tempfile.TemporaryDirectory() as directory:
            raw_path = Path(directory) / "work" / "2026-07-23" / "raw_messages.jsonl"
            raw_path.parent.mkdir(parents=True)
            cached = [
                {"dedupe_key": "before", "message_id": 1, "send_time": 1784769299},
                {"dedupe_key": "inside", "message_id": 2, "send_time": 1784769300},
                {"dedupe_key": "after", "message_id": 3, "send_time": 1784773860},
            ]
            raw_path.write_text(
                "".join(json.dumps(row) + "\n" for row in cached),
                encoding="utf-8",
            )
            argv = [
                "cache_messages.py",
                "--workspace",
                directory,
                "--date",
                "2026-07-23",
                "--start-time",
                "09:15",
                "--end-time",
                "10:30",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(cache_messages, "load_config", return_value=config),
                mock.patch.object(cache_messages, "get_conversation_state", return_value=conversation),
                mock.patch.object(cache_messages, "read_messages", return_value=[]),
                mock.patch.object(cache_messages, "load_user_map", return_value={}),
                mock.patch.object(cache_messages, "load_member_names", return_value={}),
                mock.patch.object(cache_messages, "FileResolver", return_value=resolver),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                cache_messages.main()

            exported = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([row["dedupe_key"] for row in exported], ["inside"])


class PrepareDayTests(unittest.TestCase):
    def test_selected_group_and_time_range_are_passed_to_the_cache_command(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {"default_workspace": directory}
            config_path = Path(directory) / "config.local.json"
            with mock.patch("worker.pipeline.tasks.invoke_main", return_value="ok") as invoke_main:
                day_dir, output = prepare_day(
                    config,
                    config_path,
                    "2026-07-23",
                    group_id="sales-room",
                    run_ocr=False,
                    start_time="09:15",
                    end_time="10:30",
                )

        arguments = invoke_main.call_args.args[1]
        self.assertEqual(output, "ok")
        self.assertEqual(
            day_dir,
            Path(directory).resolve() / "groups" / "sales-room" / "work" / "2026-07-23",
        )
        self.assertEqual(arguments[arguments.index("--conversation-id") + 1], "sales-room")
        self.assertEqual(arguments[arguments.index("--start-time") + 1], "09:15")
        self.assertEqual(arguments[arguments.index("--end-time") + 1], "10:30")


class TimeRangeExportTests(unittest.TestCase):
    def test_partial_day_export_has_a_distinct_filename_and_documents_the_range(self):
        with tempfile.TemporaryDirectory() as directory:
            day_dir = Path(directory)
            (day_dir / "raw_messages.jsonl").write_text("", encoding="utf-8")

            outputs = export_day(
                day_dir,
                "2026-07-23",
                "销售群",
                export_xlsx=False,
                export_markdown=True,
                include_issues=False,
                start_time="09:15",
                end_time="10:30",
            )

            markdown_path = Path(outputs["markdown"])
            self.assertEqual(markdown_path.name, "2026-07-23_0915-1030_销售群_聊天与问题盘点.md")
            self.assertIn("聊天范围：2026-07-23 09:15–10:30", markdown_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
