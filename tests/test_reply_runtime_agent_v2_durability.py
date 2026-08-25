from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from worker.reply_runtime import ReplyRuntime, RuntimeProtocolError
from worker.reply_runtime.agent_v2.checkpoint import recovery_decision
from worker.reply_runtime.agent_v2.timeline import AgentTimelineRecorder
from worker.reply_runtime.execution_log import AgentExecutionLogManager
from tests.test_reply_runtime_agent_v2 import CatalogMcp, command


def configured_v2_runtime(database: Path) -> tuple[ReplyRuntime, dict]:
    runtime = ReplyRuntime(database, mcp=CatalogMcp(), autostart=False)
    command(
        runtime,
        "save-mcp",
        0,
        {
            "kind": "mcp.save",
            "server": {
                "id": "kb",
                "name": "Knowledge",
                "transportType": "sse",
                "url": "https://mcp.example.test/sse",
            },
        },
    )
    tool = next(
        item
        for item in command(
            runtime, "discover", 1, {"kind": "mcp.test", "serverId": "kb"}
        )["tools"]
        if item["name"] == "read_search"
    )
    listener = command(
        runtime,
        "save-listener",
        2,
        {
            "kind": "listener.save",
            "listener": {
                "id": "agent",
                "name": "Agent v2",
                "groupId": "room",
                "enabled": True,
                "runtimeVersion": "v2",
                "toolGrants": [
                    {
                        "serverId": "kb",
                        "toolName": "read_search",
                        "schemaSha256": tool["schemaSha256"],
                    }
                ],
            },
        },
    )["listener"]
    return runtime, listener


class AgentV2DurabilityTests(unittest.TestCase):
    def test_restart_recovers_only_deterministic_or_read_only_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runtime.sqlite3"
            runtime, listener = configured_v2_runtime(database)
            with runtime.store.transaction() as db:
                for work_id, status, engine in (
                    ("mcp-retrieving", "retrieving", "mcp"),
                    ("deterministic", "collecting", "mcp"),
                    ("before-send", "ready_to_send", "mcp"),
                    ("dify-uncertain", "retrieving", "dify"),
                ):
                    db.execute(
                        """INSERT INTO reply_work_items(
                               id,listener_id,group_id,sender_id,status,question,
                               listener_generation,created_at,updated_at,runtime_version,
                               answer_engine
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            work_id,
                            "agent",
                            "room",
                            "alice",
                            status,
                            "question",
                            listener["generation"],
                            1_000.0,
                            1_000.0,
                            "v2",
                            engine,
                        ),
                    )
            # Simulate a process disappearing without ReplyRuntime.close().
            runtime.store.close()

            reopened = ReplyRuntime(database, mcp=CatalogMcp(), autostart=False)
            reopened._lease_loop = lambda: None
            reopened._poll_loop = lambda: None
            reopened._monitor_loop = lambda: None
            reopened.start()
            with reopened.store.lock:
                states = {
                    str(row["id"]): (
                        str(row["status"]),
                        str(row["recovery_state"]),
                        str(row["pending_reason"]),
                    )
                    for row in reopened.store.connection.execute(
                        "SELECT id,status,recovery_state,pending_reason FROM reply_work_items"
                    ).fetchall()
                }
            detail = reopened.query(
                {"kind": "work.detail", "workId": "mcp-retrieving"}
            )["item"]
            reopened.close()

        self.assertEqual(
            states["mcp-retrieving"][:2],
            ("queued_retrieval", "read_only_mcp_replay"),
        )
        self.assertEqual(
            states["deterministic"][:2],
            ("collecting", "safe_deterministic_stage"),
        )
        self.assertEqual(states["before-send"][0], "pending")
        self.assertEqual(
            states["before-send"][2], "recovered_before_delivery_requires_review"
        )
        self.assertEqual(states["dify-uncertain"][0], "closed_runtime_restarted")
        self.assertEqual(
            states["dify-uncertain"][1], "provider_request_may_be_uncertain"
        )
        self.assertIn(
            "recovery_scheduled", [event["kind"] for event in detail["timeline"]]
        )

    def test_recovery_policy_rejects_unknown_or_dify_retrieval_replay(self):
        self.assertEqual(
            recovery_decision(
                runtime_version="v2",
                answer_engine="mcp",
                stage="retrieving",
                tools=[{"safetyClass": "read_only"}],
            ),
            (True, "read_only_mcp_replay"),
        )
        self.assertFalse(
            recovery_decision(
                runtime_version="v2",
                answer_engine="mcp",
                stage="retrieving",
                tools=[{"safetyClass": "unknown"}],
            )[0]
        )
        self.assertEqual(
            recovery_decision(
                runtime_version="v2",
                answer_engine="dify",
                stage="retrieving",
                tools=[{"safetyClass": "read_only"}],
            )[1],
            "provider_request_may_be_uncertain",
        )

    def test_manual_retry_clears_provider_state_and_starts_new_timeline_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, listener = configured_v2_runtime(
                Path(directory) / "runtime.sqlite3"
            )
            with runtime.store.transaction() as db:
                db.execute(
                    """INSERT INTO reply_work_items(
                           id,listener_id,group_id,sender_id,status,question,
                           evidence_json,answer,review_json,error_json,pending_reason,
                           generation,listener_generation,created_at,updated_at,
                           completed_at,runtime_version,answer_engine,provider_audit_json,
                           stop_reason,recovery_state,agent_usage_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "failed-work",
                        "agent",
                        "room",
                        "alice",
                        "failed",
                        "question",
                        '[{"result":"old"}]',
                        "old answer",
                        '{"supported":false}',
                        '{"code":"OLD"}',
                        "old failure",
                        4,
                        listener["generation"],
                        1_000.0,
                        1_000.0,
                        1_000.0,
                        "v2",
                        "mcp",
                        '{"rounds":2}',
                        "max_rounds",
                        "",
                        '{"totalTokens":99}',
                    ),
                )
            result = command(
                runtime,
                "retry-work",
                3,
                {
                    "kind": "work.retry",
                    "workId": "failed-work",
                    "expectedVersion": 4,
                },
            )
            detail = runtime.query(
                {"kind": "work.detail", "workId": "failed-work"}
            )["item"]
            with runtime.store.lock:
                raw = dict(
                    runtime.store.connection.execute(
                        "SELECT * FROM reply_work_items WHERE id='failed-work'"
                    ).fetchone()
                )
            runtime.close()

        self.assertEqual(result["status"], "queued_retrieval")
        self.assertEqual(result["version"], 5)
        self.assertEqual(raw["evidence_json"], "[]")
        self.assertEqual(raw["answer"], "")
        self.assertIsNone(raw["error_json"])
        self.assertEqual(raw["provider_audit_json"], "{}")
        self.assertEqual(raw["recovery_state"], "manual_retry")
        self.assertEqual(detail["timeline"][0]["workGeneration"], 5)
        self.assertEqual(detail["timeline"][0]["kind"], "manual_retry_scheduled")


class AgentV2DiagnosticsTests(unittest.TestCase):
    def test_timeline_is_always_on_but_detail_is_opt_in_and_redacted(self):
        secret = "super-secret-token"
        with tempfile.TemporaryDirectory() as directory:
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", autostart=False)
            command(
                runtime,
                "save-secret",
                0,
                {
                    "kind": "mcp.save",
                    "server": {
                        "id": "kb",
                        "name": "Knowledge",
                        "transportType": "sse",
                        "url": "https://mcp.example.test/sse",
                        "headers": {"Authorization": f"Bearer {secret}"},
                    },
                },
            )
            with runtime.store.transaction() as db:
                db.execute(
                    """INSERT INTO reply_listeners(
                           id,public_json,revision,generation,created_at,updated_at
                       ) VALUES('agent',?,1,1,1000,1000)""",
                    (
                        json.dumps(
                            {
                                "id": "agent",
                                "name": "Agent",
                                "groupId": "room",
                                "runtimeVersion": "v2",
                                "answerEngine": "mcp",
                                "toolGrants": [],
                            }
                        ),
                    ),
                )
                db.execute(
                    """INSERT INTO reply_work_items(
                           id,listener_id,group_id,sender_id,status,question,
                           listener_generation,created_at,updated_at,runtime_version,
                           answer_engine
                       ) VALUES('work','agent','room','alice','failed','question',
                                1,1000,1000,'v2','mcp')"""
                )
            recorder = AgentTimelineRecorder(
                runtime.store,
                work_id="work",
                work_generation=1,
                detailed=True,
                redact=runtime.redact_public,
                wall_clock=lambda: 1_001.0,
            )
            recorder.event(
                "tool_call_completed",
                {
                    "serverId": "kb",
                    "toolName": "search",
                    "arguments": {"query": f"find {secret}"},
                    "result": f"Bearer {secret}",
                    "headers": {"Authorization": f"Bearer {secret}"},
                    "usableEvidence": True,
                },
            )
            detail = runtime.query({"kind": "work.detail", "workId": "work"})[
                "item"
            ]
            runtime.close()

        serialized = json.dumps(detail["timeline"], ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertIn("[REDACTED]", serialized)
        event = detail["timeline"][0]
        self.assertEqual(event["role"], "retriever")
        self.assertEqual(event["metadata"]["argumentKeys"], ["query"])
        self.assertTrue(event["metadata"]["resultPresent"])

    def test_model_capability_persists_and_configuration_fingerprint_expires_it(self):
        class Model:
            fingerprint = "a" * 64

            def capability_fingerprint(self):
                return self.fingerprint

            def test_capabilities(self):
                return {
                    "status": "success",
                    "provider": "openai_compatible",
                    "model": "agent-model",
                    "capabilities": {
                        name: {"status": "supported", "supported": True}
                        for name in (
                            "structuredOutput",
                            "toolCalling",
                            "vision",
                            "context",
                        )
                    },
                }

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runtime.sqlite3"
            model = Model()
            runtime = ReplyRuntime(database, model=model, autostart=False)
            tested = command(
                runtime,
                "test-model",
                0,
                {"kind": "model.test_capabilities"},
            )
            current = runtime.query({"kind": "model.capabilities"})
            runtime.close()

            reopened_model = Model()
            reopened = ReplyRuntime(database, model=reopened_model, autostart=False)
            durable = reopened.query({"kind": "model.capabilities"})
            reopened_model.fingerprint = "b" * 64
            stale = reopened.query({"kind": "model.capabilities"})
            reopened.close()

        self.assertTrue(tested["current"])
        self.assertEqual(tested["fingerprint"], "a" * 16)
        self.assertTrue(current["current"])
        self.assertTrue(durable["current"])
        self.assertFalse(stale["current"])
        self.assertEqual(stale["fingerprint"], "b" * 16)

    def test_diagnostic_clear_requires_ack_and_export_is_redacted_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_directory = root / "logs"
            manager = AgentExecutionLogManager(
                lambda: {"diagnostics": {"agent_execution_logging": True}},
                log_directory,
            )
            runtime, listener = configured_v2_runtime(root / "runtime.sqlite3")
            runtime.execution_logs = manager
            with runtime.store.transaction() as db:
                db.execute(
                    """INSERT INTO reply_work_items(
                           id,listener_id,group_id,sender_id,status,question,
                           listener_generation,created_at,updated_at,completed_at,
                           runtime_version,answer_engine
                       ) VALUES('work','agent','room','alice','failed','question',
                                ?,1000,1000,1000,'v2','mcp')""",
                    (listener["generation"],),
                )
            session = manager.start_run({"workId": "work", "question": "question"})
            session.event("tool_call_completed", {"result": "safe evidence"})
            session.close({"status": "failed"})
            (log_directory / "keep.txt").write_text("keep", encoding="utf-8")

            exported = command(
                runtime,
                "export-work",
                3,
                {"kind": "work.export_diagnostics", "workId": "work"},
            )
            archive_path = Path(exported["path"])
            with zipfile.ZipFile(archive_path) as archive:
                names = set(archive.namelist())
                exported_work = json.loads(archive.read("work.json"))
            with self.assertRaises(RuntimeProtocolError) as unconfirmed:
                command(
                    runtime,
                    "clear-without-ack",
                    3,
                    {"kind": "diagnostics.clear"},
                )
            cleared = command(
                runtime,
                "clear-logs",
                3,
                {
                    "kind": "diagnostics.clear",
                    "acknowledgement": "DELETE_AGENT_LOGS",
                },
            )

            self.assertTrue(archive_path.is_file())
            self.assertTrue(
                {"manifest.json", "work.json", "listener.json", "timeline.json"}
                <= names
            )
            self.assertTrue(any(name.startswith("logs/agent-") for name in names))
            self.assertEqual(exported_work["id"], "work")
            self.assertEqual(
                unconfirmed.exception.code,
                "DIAGNOSTICS_CLEAR_CONFIRMATION_REQUIRED",
            )
            self.assertEqual(cleared["removedFiles"], 1)
            self.assertTrue((log_directory / "keep.txt").is_file())
            runtime.close()


if __name__ == "__main__":
    unittest.main()
