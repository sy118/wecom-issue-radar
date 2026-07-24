from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worker.main import extract_keys_console, handle_run
from worker.pipeline.exporter import export_day
from worker.pipeline.tasks import prepare_day
from worker.wecom import cache_messages


class WorkerRunRequestTests(unittest.TestCase):
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
