from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from worker.reply_runtime.execution_log import AgentExecutionLogManager
from tests.test_reply_runtime_additional import (
    FakeClock,
    FakeMcp,
    FakeMessages,
    ScriptedModel,
    add_message,
    baseline,
    configure_runtime,
    drive_to_retrieval,
)


class AgentExecutionLogTests(unittest.TestCase):
    def test_logging_is_disabled_by_default_and_creates_no_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "agent-logs"
            manager = AgentExecutionLogManager(lambda: {}, target)

            session = manager.start_run({"workId": "work-1"})
            session.event("model_decision", {"answer": "not written"})
            session.close({"status": "completed"})

            self.assertFalse(session.enabled)
            self.assertFalse(target.exists())

    def test_enabled_log_is_jsonl_and_redacts_known_and_structural_secrets(self):
        secret = "model-secret-value"
        env_secret = "opaque-environment-value"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "agent-logs"
            manager = AgentExecutionLogManager(
                lambda: {
                    "diagnostics": {"agent_execution_logging": True},
                    "llm": {"api_key": secret},
                    "mcp_servers": [{"env": {"CUSTOM_NAME": env_secret}}],
                },
                target,
            )

            session = manager.start_run(
                {"workId": "work-1", "question": "财务支出单的保存接口是啥？"}
            )
            session.event(
                "tool_call_completed",
                {
                    "arguments": {
                        "query": "财务支出单 保存接口",
                        "maxTokens": 12000,
                    },
                    "result": (
                        f"Bearer {secret} https://example.test/?token={secret} "
                        f"environment={env_secret}"
                    ),
                    "headers": {"Authorization": f"Bearer {secret}"},
                },
            )
            path = session.path
            session.close({"status": "skipped_no_evidence", "evidenceCount": 0})

            self.assertIsNotNone(path)
            lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(
                [line["event"] for line in lines],
                ["run_started", "tool_call_completed", "run_finished"],
            )
            serialized = json.dumps(lines, ensure_ascii=False)
            self.assertNotIn(secret, serialized)
            self.assertNotIn(env_secret, serialized)
            self.assertIn("[REDACTED]", serialized)
            self.assertEqual(
                lines[1]["data"]["arguments"]["query"], "财务支出单 保存接口"
            )
            self.assertEqual(lines[1]["data"]["arguments"]["maxTokens"], 12000)
            self.assertEqual(lines[1]["data"]["headers"], "[REDACTED]")

    def test_retention_reserves_the_new_run_slot_and_never_exceeds_one_hundred_files(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "agent-logs"
            target.mkdir()
            for index in range(100):
                (target / f"agent-existing-{index:03d}.jsonl").write_text(
                    "{}\n", encoding="utf-8"
                )
            manager = AgentExecutionLogManager(
                lambda: {"diagnostics": {"agent_execution_logging": True}},
                target,
            )

            session = manager.start_run({"workId": "work-new"})
            session.close({"status": "completed"})

            self.assertEqual(len(list(target.glob("agent-*.jsonl"))), 100)

    def test_size_cap_includes_the_truncation_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "agent-logs"
            manager = AgentExecutionLogManager(
                lambda: {"diagnostics": {"agent_execution_logging": True}},
                target,
            )
            with patch("worker.reply_runtime.execution_log.MAX_LOG_FILE_BYTES", 700):
                session = manager.start_run({"workId": "bounded"})
                path = session.path
                session.event("oversized", {"result": "x" * 10_000})
                session.close({"status": "completed"})

            self.assertIsNotNone(path)
            self.assertLessEqual(path.stat().st_size, 700)
            events = [
                json.loads(line)["event"]
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("log_truncated", events)

    def test_runtime_writes_one_complete_trace_for_an_enabled_work_item(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "agent-logs"
            manager = AgentExecutionLogManager(
                lambda: {"diagnostics": {"agent_execution_logging": True}},
                target,
            )
            clock, messages = FakeClock(), FakeMessages()
            runtime, _listener = configure_runtime(
                directory,
                clock=clock,
                messages=messages,
                model=ScriptedModel(),
                mcp=FakeMcp(),
                execution_logs=manager,
            )
            try:
                baseline(runtime)
                add_message(
                    messages,
                    number=1,
                    sender_id="alice",
                    text="财务支出单的保存接口是啥？",
                )
                drive_to_retrieval(runtime, clock, prefix="execution-log")
            finally:
                runtime.close()

            files = list(target.glob("agent-*.jsonl"))
            self.assertEqual(len(files), 1)
            events = [
                json.loads(line)
                for line in files[0].read_text(encoding="utf-8").splitlines()
            ]
            names = [event["event"] for event in events]
            self.assertEqual(names[0], "run_started")
            self.assertIn("answer_generation_completed", names)
            self.assertIn("answer_engine_completed", names)
            self.assertIn("independent_review_completed", names)
            self.assertEqual(names[-1], "run_finished")
            self.assertEqual(events[-1]["data"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
