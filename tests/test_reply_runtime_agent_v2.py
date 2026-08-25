from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from worker.reply_runtime import ReplyRuntime, RuntimeProtocolError
from worker.reply_runtime.agent import LangGraphMcpAgent
from worker.reply_runtime.agent_v2.checkpoint import recovery_decision
from worker.reply_runtime.agent_v2.context import compact_agent_messages, model_tool_result
from worker.reply_runtime.agent_v2.policy import (
    TOOL_SAFETY_DESTRUCTIVE,
    TOOL_SAFETY_READ_ONLY,
    TOOL_SAFETY_UNKNOWN,
    TOOL_SAFETY_WRITE,
    classify_tool_safety,
    normalize_tool_annotations,
    tool_is_agent_callable,
)
from worker.reply_runtime.agent_v2.timeline import AgentTimelineRecorder
from worker.reply_runtime.execution_log import AgentExecutionLogManager


def command(runtime: ReplyRuntime, command_id: str, revision: int, body: dict):
    return runtime.execute(
        {
            "protocolVersion": 1,
            "commandId": command_id,
            "expectedRevision": revision,
            "body": body,
        }
    )


class CatalogMcp:
    def __init__(self) -> None:
        self.schema_revision = 1

    def discover(self, _server):
        unknown_properties = {"query": {"type": "string"}}
        if self.schema_revision > 1:
            unknown_properties["limit"] = {"type": "integer", "minimum": 1}
        return [
            {
                "name": "unknown_search",
                "description": "Search without MCP annotations",
                "inputSchema": {
                    "type": "object",
                    "properties": unknown_properties,
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "read_search",
                "description": "Explicitly read-only search",
                "annotations": {"readOnlyHint": True, "idempotentHint": True},
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "write_record",
                "annotations": {"readOnlyHint": False},
                "inputSchema": {"type": "object"},
            },
            {
                "name": "delete_record",
                "annotations": {"readOnlyHint": False, "destructiveHint": True},
                "inputSchema": {"type": "object"},
            },
        ]


class StopModel:
    def __init__(self) -> None:
        self.messages = []

    def bind_tools(self, _tools):
        return self

    def invoke(self, messages):
        self.messages.append(list(messages))
        return AIMessage(
            content="",
            tool_calls=[],
            usage_metadata={"input_tokens": 11, "output_tokens": 2, "total_tokens": 13},
        )


class AgentV2PolicyTests(unittest.TestCase):
    def test_annotations_are_normalized_and_classified_conservatively(self):
        self.assertEqual(
            normalize_tool_annotations(
                {
                    "read_only_hint": True,
                    "destructiveHint": False,
                    "ignored": "value",
                }
            ),
            {"readOnlyHint": True, "destructiveHint": False},
        )
        self.assertEqual(classify_tool_safety({})[0], TOOL_SAFETY_UNKNOWN)
        self.assertEqual(
            classify_tool_safety({}, confirmed_read_only=True)[0],
            TOOL_SAFETY_READ_ONLY,
        )
        self.assertEqual(
            classify_tool_safety(
                {"readOnlyHint": False}, confirmed_read_only=True
            )[0],
            TOOL_SAFETY_WRITE,
        )
        self.assertEqual(
            classify_tool_safety(
                {"readOnlyHint": True, "destructiveHint": True},
                confirmed_read_only=True,
            )[0],
            TOOL_SAFETY_DESTRUCTIVE,
        )
        self.assertTrue(tool_is_agent_callable({"safetyClass": "read_only"}, "v2"))
        self.assertFalse(tool_is_agent_callable({"safetyClass": "unknown"}, "v2"))
        self.assertTrue(tool_is_agent_callable({"safetyClass": "write"}, "legacy"))

    def test_unknown_confirmation_is_schema_pinned_and_write_tools_stay_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            mcp = CatalogMcp()
            runtime = ReplyRuntime(
                Path(directory) / "runtime.sqlite3", mcp=mcp, autostart=False
            )
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
            catalog = command(
                runtime, "discover", 1, {"kind": "mcp.test", "serverId": "kb"}
            )
            tools = {tool["name"]: tool for tool in catalog["tools"]}
            self.assertEqual(tools["unknown_search"]["safetyClass"], "unknown")
            self.assertEqual(tools["read_search"]["safetyClass"], "read_only")
            self.assertEqual(tools["write_record"]["safetyClass"], "write")
            self.assertEqual(tools["delete_record"]["safetyClass"], "destructive")

            unknown_grant = {
                "serverId": "kb",
                "toolName": "unknown_search",
                "schemaSha256": tools["unknown_search"]["schemaSha256"],
            }
            with self.assertRaises(RuntimeProtocolError) as blocked:
                command(
                    runtime,
                    "blocked-listener",
                    2,
                    {
                        "kind": "listener.save",
                        "listener": {
                            "id": "agent",
                            "name": "Agent v2",
                            "groupId": "room",
                            "runtimeVersion": "v2",
                            "toolGrants": [unknown_grant],
                        },
                    },
                )
            self.assertEqual(blocked.exception.code, "MCP_TOOL_BLOCKED")

            confirmed = command(
                runtime,
                "confirm-unknown",
                2,
                {
                    "kind": "mcp.tool_policy",
                    "serverId": "kb",
                    "toolName": "unknown_search",
                    "schemaSha256": unknown_grant["schemaSha256"],
                    "action": "confirm_read_only",
                },
            )
            self.assertEqual(confirmed["tool"]["safetyClass"], "read_only")
            saved = command(
                runtime,
                "save-listener",
                3,
                {
                    "kind": "listener.save",
                    "listener": {
                        "id": "agent",
                        "name": "Agent v2",
                        "groupId": "room",
                        "runtimeVersion": "v2",
                        "maxAgentRounds": 200,
                        "maxToolCalls": 200,
                        "toolGrants": [
                            {
                                **unknown_grant,
                                "bootstrap": True,
                                "bootstrapArguments": {"query": "核心索引"},
                            }
                        ],
                    },
                },
            )["listener"]
            self.assertEqual(saved["runtimeVersion"], "v2")
            self.assertEqual(saved["maxAgentRounds"], 200)
            self.assertTrue(saved["toolGrants"][0]["bootstrap"])

            for tool_name in ("write_record", "delete_record"):
                with self.subTest(tool_name=tool_name):
                    with self.assertRaises(RuntimeProtocolError) as unsafe:
                        command(
                            runtime,
                            f"confirm-{tool_name}",
                            4,
                            {
                                "kind": "mcp.tool_policy",
                                "serverId": "kb",
                                "toolName": tool_name,
                                "schemaSha256": tools[tool_name]["schemaSha256"],
                                "action": "confirm_read_only",
                            },
                        )
                    self.assertEqual(unsafe.exception.code, "MCP_TOOL_NOT_READ_ONLY")

            mcp.schema_revision = 2
            changed = command(
                runtime, "schema-change", 4, {"kind": "mcp.test", "serverId": "kb"}
            )
            changed_unknown = next(
                tool for tool in changed["tools"] if tool["name"] == "unknown_search"
            )
            self.assertNotEqual(
                changed_unknown["schemaSha256"], unknown_grant["schemaSha256"]
            )
            self.assertEqual(changed_unknown["safetyClass"], "unknown")
            listener = runtime.query({"kind": "listener.list"})["listeners"][0]
            self.assertTrue(listener["toolGrants"][0]["invalidated"])
            self.assertEqual(listener["health"]["status"], "tool_grant_invalidated")
            runtime.close()

    def test_bootstrap_arguments_are_validated_against_current_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ReplyRuntime(
                Path(directory) / "runtime.sqlite3",
                mcp=CatalogMcp(),
                autostart=False,
            )
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
            catalog = command(
                runtime, "discover", 1, {"kind": "mcp.test", "serverId": "kb"}
            )
            tool = next(item for item in catalog["tools"] if item["name"] == "read_search")
            with self.assertRaises(RuntimeProtocolError) as invalid:
                command(
                    runtime,
                    "invalid-bootstrap",
                    2,
                    {
                        "kind": "listener.save",
                        "listener": {
                            "id": "agent",
                            "name": "Agent v2",
                            "groupId": "room",
                            "runtimeVersion": "v2",
                            "toolGrants": [
                                {
                                    "serverId": "kb",
                                    "toolName": "read_search",
                                    "schemaSha256": tool["schemaSha256"],
                                    "bootstrap": True,
                                    "bootstrapArguments": {},
                                }
                            ],
                        },
                    },
                )
            self.assertEqual(invalid.exception.code, "INVALID_TOOL_BOOTSTRAP")
            runtime.close()


class AgentV2ContextTests(unittest.TestCase):
    def test_compaction_preserves_system_question_recent_exchange_and_evidence(self):
        messages = [SystemMessage(content="system"), HumanMessage(content="question")]
        for index in range(6):
            call_id = f"call-{index}"
            messages.extend(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "search",
                                "args": {"query": str(index)},
                                "id": call_id,
                                "type": "tool_call",
                            }
                        ],
                    ),
                    ToolMessage(
                        content=f"result-{index}-" + ("x" * 4_000),
                        tool_call_id=call_id,
                        name="search",
                    ),
                ]
            )
        compacted = compact_agent_messages(
            messages,
            [
                {
                    "serverId": "kb",
                    "toolName": "search",
                    "arguments": {"query": "order-42"},
                    "result": {"rows": [{"id": 42}]},
                }
            ],
            max_context_chars=8_000,
            threshold=0.8,
        )
        self.assertTrue(compacted.compacted)
        self.assertEqual(compacted.messages[0].content, "system")
        self.assertEqual(compacted.messages[1].content, "question")
        self.assertIn("order-42", str(compacted.messages[2].content))
        self.assertFalse(isinstance(compacted.messages[3], ToolMessage))
        self.assertGreater(compacted.dropped_messages, 0)
        self.assertLess(compacted.after_chars, compacted.before_chars)

    def test_tool_result_model_view_is_bounded(self):
        rendered = model_tool_result("x" * 12_500)
        self.assertTrue(rendered.startswith("x" * 12_000))
        self.assertIn("已截断 500 字符", rendered)
        self.assertLess(len(rendered), 12_100)

    def test_bootstrap_runs_in_order_before_model_and_shares_call_budget(self):
        tools = [
            {
                "serverId": "kb",
                "toolName": "search",
                "description": "search",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "bootstrap": True,
                "bootstrapArguments": {"query": "first"},
                "safetyClass": "read_only",
            },
            {
                "serverId": "orders",
                "toolName": "detail",
                "description": "detail",
                "inputSchema": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
                "bootstrap": True,
                "bootstrapArguments": {"id": 42},
                "safetyClass": "read_only",
            },
        ]
        calls = []
        model = StopModel()

        result = LangGraphMcpAgent(lambda _timeout: model).retrieve(
            question="订单 42？",
            context=[],
            tools=tools,
            system_prompt="只读查询",
            image_content=[],
            invoke_tool=lambda server, tool, arguments, _remaining: (
                calls.append((server, tool, arguments)) or {"rows": [arguments]}
            ),
            has_evidence=lambda value: bool(value.get("rows")),
            max_rounds=2,
            max_tool_calls=2,
            timeout_seconds=30,
        )
        self.assertEqual(
            calls,
            [
                ("kb", "search", {"query": "first"}),
                ("orders", "detail", {"id": 42}),
            ],
        )
        self.assertEqual(result.rounds, 0)
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual(result.stop_reason, "max_tool_calls")
        self.assertEqual(len(result.evidence), 2)
        self.assertEqual(model.messages, [])

        limited_calls = []
        limited = LangGraphMcpAgent(lambda _timeout: StopModel()).retrieve(
            question="订单 42？",
            context=[],
            tools=tools,
            system_prompt="只读查询",
            image_content=[],
            invoke_tool=lambda server, tool, arguments, _remaining: (
                limited_calls.append((server, tool, arguments)) or {"rows": [arguments]}
            ),
            has_evidence=lambda value: bool(value.get("rows")),
            max_rounds=2,
            max_tool_calls=1,
            timeout_seconds=30,
        )
        self.assertEqual(len(limited_calls), 1)
        self.assertEqual(limited.tool_calls, 1)
        self.assertEqual(limited.stop_reason, "max_tool_calls")


if __name__ == "__main__":
    unittest.main()
