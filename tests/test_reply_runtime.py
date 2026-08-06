from __future__ import annotations

import json
import tempfile
import threading
import unittest
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path
from unittest.mock import patch

from worker.reply_runtime import ReplyRuntime, RuntimeProtocolError
from worker.reply_runtime.stdio import run_default_reply_runtime, serve_reply_runtime
from tests.reply_runtime_agent_fakes import retrieval_from_calls


class ReplyRuntimeCommandTests(unittest.TestCase):
    def test_mcp_save_is_durable_idempotent_and_secret_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "reply-runtime.sqlite3"
            runtime = ReplyRuntime(database, autostart=False)
            command = {
                "protocolVersion": 1,
                "commandId": "save-search-server",
                "expectedRevision": 0,
                "body": {
                    "kind": "mcp.save",
                    "server": {
                        "id": "search",
                        "name": "Knowledge search",
                        "enabled": True,
                        "transportType": "streamable-http",
                        "url": "https://mcp.example.test/mcp",
                        "headers": {
                            "Authorization": "Bearer top-secret",
                            "X-Tenant": "support",
                        },
                    },
                },
            }

            first = runtime.execute(command)
            duplicate = runtime.execute(command)
            listed = runtime.query({"kind": "mcp.list"})
            runtime.close()

            reopened = ReplyRuntime(database, autostart=False)
            durable = reopened.query({"kind": "mcp.list"})
            reopened.close()

            self.assertEqual(first, duplicate)
            self.assertEqual(first["revision"], 1)
            self.assertEqual(listed, durable)
            self.assertEqual(listed["revision"], 1)
            self.assertEqual(listed["servers"][0]["id"], "search")
            self.assertTrue(listed["servers"][0]["secrets"]["headersConfigured"])
            self.assertNotIn("headers", listed["servers"][0])
            self.assertNotIn("headerKeys", listed["servers"][0])
            self.assertNotIn("envKeys", listed["servers"][0])
            self.assertNotIn("top-secret", repr(listed))

            runtime = ReplyRuntime(database, autostart=False)
            with self.assertRaises(RuntimeProtocolError) as reused:
                runtime.execute(
                    {
                        **command,
                        "body": {
                            **command["body"],
                            "server": {
                                **command["body"]["server"],
                                "name": "Changed",
                            },
                        },
                    }
                )
            self.assertEqual(reused.exception.code, "COMMAND_ID_REUSED")
            with self.assertRaises(RuntimeProtocolError) as stale:
                runtime.execute(
                    {
                        "protocolVersion": 1,
                        "commandId": "stale-save",
                        "expectedRevision": 0,
                        "body": {
                            "kind": "mcp.save",
                            "server": {
                                "id": "another",
                                "name": "Another",
                                "transportType": "sse",
                                "url": "https://mcp.example.test/sse",
                            },
                        },
                    }
                )
            runtime.close()
            self.assertEqual(stale.exception.code, "REVISION_CONFLICT")

    def test_stdio_protocol_returns_correlated_single_line_responses(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", autostart=False)
            source = StringIO(
                "\n".join(
                    [
                        '{"id":"q1","op":"query","payload":{"protocolVersion":1,"body":{"kind":"runtime.snapshot"}}}',
                        '{"id":"bad","op":"query","payload":{"protocolVersion":99,"body":{"kind":"mcp.list"}}}',
                        '{not-json}',
                    ]
                )
                + "\n"
            )
            output = StringIO()
            serve_reply_runtime(runtime, source, output)

        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(lines[0]["id"], "q1")
        self.assertTrue(lines[0]["ok"])
        self.assertEqual(lines[0]["data"]["protocolVersion"], 1)
        self.assertEqual(lines[1]["error"]["code"], "UNSUPPORTED_PROTOCOL")
        self.assertIsNone(lines[2]["id"])
        self.assertEqual(lines[2]["error"]["code"], "INVALID_NDJSON")
        self.assertEqual(len(output.getvalue().splitlines()), 3)

    def test_default_stdio_uses_utf8_for_non_ascii_listener_save(self):
        class EchoRuntime:
            def execute(self, payload):
                return {"listener": payload["body"]["listener"]}

            def close(self):
                pass

        request = {
            "id": "save-listener",
            "op": "execute",
            "payload": {
                "protocolVersion": 1,
                "commandId": "save-listener",
                "expectedRevision": 0,
                "body": {
                    "kind": "listener.save",
                    "listener": {
                        "id": "finance",
                        "name": "财务开发组自动答疑",
                        "groupId": "R:204075628419884",
                        "groupName": "财务开发组",
                        "enabled": False,
                        "toolGrants": [],
                    },
                },
            },
        }
        encoded_request = (
            json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")

        input_bytes = BytesIO(encoded_request)
        output_bytes = BytesIO()
        input_stream = TextIOWrapper(input_bytes, encoding="ascii")
        output_stream = TextIOWrapper(output_bytes, encoding="ascii")
        with (
            patch(
                "worker.reply_runtime.factory.build_default_runtime",
                return_value=EchoRuntime(),
            ),
            patch("worker.reply_runtime.stdio.sys.stdin", input_stream),
            patch("worker.reply_runtime.stdio.sys.stdout", output_stream),
        ):
            run_default_reply_runtime()

        output_stream.flush()
        response = json.loads(output_bytes.getvalue().decode("utf-8"))

        self.assertTrue(response["ok"], response)
        self.assertEqual(response["data"]["listener"]["name"], "财务开发组自动答疑")
        self.assertEqual(response["data"]["listener"]["groupName"], "财务开发组")

    def test_listener_grants_discovered_tool_and_schema_drift_blocks_it(self):
        class McpBoundary:
            schema_type = "string"

            def discover(self, server):
                self.last_server = server
                return [
                    {
                        "name": "search_kb",
                        "description": "Search the support knowledge base",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": self.schema_type}},
                            "required": ["query"],
                        },
                    }
                ]

        def command(command_id, revision, body):
            return {
                "protocolVersion": 1,
                "commandId": command_id,
                "expectedRevision": revision,
                "body": body,
            }

        with tempfile.TemporaryDirectory() as directory:
            mcp = McpBoundary()
            runtime = ReplyRuntime(
                Path(directory) / "runtime.sqlite3",
                mcp=mcp,
                autostart=False,
            )
            runtime.execute(
                command(
                    "save-mcp",
                    0,
                    {
                        "kind": "mcp.save",
                        "server": {
                            "id": "kb",
                            "name": "Knowledge",
                            "enabled": True,
                            "transportType": "streamable-http",
                            "url": "https://mcp.example.test/mcp",
                            "headers": {"Authorization": "Bearer secret"},
                        },
                    },
                )
            )
            discovered = runtime.execute(
                command("test-mcp", 1, {"kind": "mcp.test", "serverId": "kb"})
            )
            grant = {
                "serverId": "kb",
                "toolName": "search_kb",
                "schemaSha256": discovered["tools"][0]["schemaSha256"],
            }
            saved = runtime.execute(
                command(
                    "save-listener",
                    2,
                    {
                        "kind": "listener.save",
                        "listener": {
                            "id": "support-listener",
                            "name": "Support questions",
                            "groupId": "room-1",
                            "groupName": "Support",
                            "enabled": True,
                            "toolGrants": [grant],
                            "systemPrompt": "Answer only product support questions.",
                            "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret-key",
                        },
                    },
                )
            )
            listeners = runtime.query({"kind": "listener.list"})

            self.assertEqual(saved["listener"]["sameSenderMergeSeconds"], 20)
            self.assertEqual(saved["listener"]["mcpTimeoutSeconds"], 900)
            self.assertEqual(listeners["listeners"][0]["health"]["status"], "ready")
            self.assertTrue(listeners["listeners"][0]["webhook"]["configured"])
            self.assertNotIn("webhookUrl", repr(listeners))
            self.assertNotIn("secret-key", repr(listeners))
            self.assertNotIn("headers", mcp.last_server)
            self.assertEqual(
                mcp.last_server["secrets"]["headers"]["Authorization"],
                "Bearer secret",
            )

            with self.assertRaises(RuntimeProtocolError) as duplicate_group:
                runtime.execute(
                    command(
                        "duplicate-group",
                        3,
                        {
                            "kind": "listener.save",
                            "listener": {
                                "id": "other-listener",
                                "name": "Other",
                                "groupId": "room-1",
                                "enabled": True,
                                "toolGrants": [grant],
                            },
                        },
                    )
                )
            self.assertEqual(duplicate_group.exception.code, "GROUP_ALREADY_LISTENED")

            mcp.schema_type = "number"
            runtime.execute(command("retest-mcp", 3, {"kind": "mcp.test", "serverId": "kb"}))
            changed = runtime.query({"kind": "listener.list"})["listeners"][0]
            mcp.schema_type = "string"
            runtime.execute(command("restore-old-schema", 4, {"kind": "mcp.test", "serverId": "kb"}))
            restored = runtime.query({"kind": "listener.list"})["listeners"][0]
            runtime.close()

        self.assertEqual(changed["health"]["status"], "tool_schema_changed")
        self.assertEqual(restored["health"]["status"], "tool_schema_changed")

    def test_webhook_test_respects_the_twenty_per_minute_limit(self):
        class Clock:
            def now(self):
                return 10_000.0

        class Mcp:
            def discover(self, server):
                return [{"name": "search", "inputSchema": {"type": "object"}}]

        class Webhook:
            calls = 0

            def send(self, **kwargs):
                self.calls += 1
                return {"status": "sent"}

        def command(command_id, revision, body):
            return {"protocolVersion": 1, "commandId": command_id, "expectedRevision": revision, "body": body}

        with tempfile.TemporaryDirectory() as directory:
            webhook = Webhook()
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", clock=Clock(), mcp=Mcp(), webhook=webhook, autostart=False)
            runtime.execute(command("rate-mcp", 0, {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "transportType": "sse", "url": "https://mcp.test/sse"}}))
            tool = runtime.execute(command("rate-catalog", 1, {"kind": "mcp.test", "serverId": "kb"}))["tools"][0]
            runtime.execute(command("rate-listener", 2, {"kind": "listener.save", "listener": {"id": "rate", "name": "Rate", "groupId": "room", "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}], "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=rate-limit-key"}}))
            revision = 3
            for index in range(20):
                result = runtime.execute(command(f"rate-test-{index}", revision, {"kind": "listener.test_webhook", "listenerId": "rate"}))
                revision = result["revision"]
            with self.assertRaises(RuntimeProtocolError) as limited:
                runtime.execute(command("rate-test-blocked", revision, {"kind": "listener.test_webhook", "listenerId": "rate"}))
            runtime.close()

        self.assertEqual(webhook.calls, 20)
        self.assertEqual(limited.exception.code, "WEBHOOK_RATE_LIMITED")


class ReplyRuntimeQuestionFlowTests(unittest.TestCase):
    def test_human_answer_during_merge_window_closes_question_without_retrieval(self):
        class Clock:
            value = 900.0

            def now(self):
                return self.value

        class Messages:
            def __init__(self):
                self.rows = []

            def watermark(self, listener):
                return self.rows[-1]["cursor"] if self.rows else [899, 0, 0, 0]

            def read(self, listener, cursor):
                return [row for row in self.rows if tuple(row["cursor"]) > tuple(cursor)]

        class Model:
            def classify(self, *, messages, groupContext, question=None):
                if question and messages[-1].get("senderId") == "bob":
                    return {"labels": ["human_answer"]}
                return {"labels": ["question"]}

            def retrieve(self, **kwargs):
                raise AssertionError("a question answered during collection must not retrieve")

        class Mcp:
            calls = 0

            def discover(self, server):
                return [{"name": "search", "inputSchema": {"type": "object"}}]

            def call(self, **kwargs):
                self.calls += 1
                return {"content": "unexpected"}

        def execute(runtime, command_id, revision, body):
            return runtime.execute(
                {
                    "protocolVersion": 1,
                    "commandId": command_id,
                    "expectedRevision": revision,
                    "body": body,
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            clock, messages, mcp = Clock(), Messages(), Mcp()
            runtime = ReplyRuntime(
                Path(directory) / "runtime.sqlite3",
                clock=clock,
                message_source=messages,
                model=Model(),
                mcp=mcp,
                autostart=False,
            )
            execute(runtime, "merge-mcp", 0, {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "enabled": True, "transportType": "sse", "url": "https://mcp.test/sse"}})
            tool = execute(runtime, "merge-catalog", 1, {"kind": "mcp.test", "serverId": "kb"})["tools"][0]
            execute(runtime, "merge-listener", 2, {"kind": "listener.save", "listener": {"id": "merge", "name": "Merge", "groupId": "room", "enabled": True, "sameSenderMergeSeconds": 20, "humanReplyWaitSeconds": 10, "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}]}})
            execute(runtime, "merge-baseline", 3, {"kind": "runtime.tick", "wait": True})
            messages.rows.extend(
                [
                    {"cursor": [901, 0, 1, 1], "messageId": "1", "serverId": "1", "sequence": 0, "sendTime": 901, "groupId": "room", "senderId": "alice", "senderName": "Alice", "contentType": "text", "text": "How do I restore sync?"},
                    {"cursor": [902, 0, 2, 1], "messageId": "2", "serverId": "1", "sequence": 0, "sendTime": 902, "groupId": "room", "senderId": "bob", "senderName": "Bob", "contentType": "text", "text": "Enable sync again in Settings."},
                ]
            )
            clock.value = 905
            execute(runtime, "merge-collect", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 1_000
            execute(runtime, "merge-after-wait", 3, {"kind": "runtime.tick", "wait": True})
            items = runtime.query({"kind": "work.list"})["items"]
            runtime.close()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "answered_by_human")
        self.assertEqual(mcp.calls, 0)

    def test_due_question_classification_uses_latest_group_context(self):
        class Clock:
            value = 1_100.0

            def now(self):
                return self.value

        class Messages:
            def __init__(self):
                self.rows = []

            def watermark(self, listener):
                return self.rows[-1]["cursor"] if self.rows else [1_099, 0, 0, 0]

            def read(self, listener, cursor):
                return [row for row in self.rows if tuple(row["cursor"]) > tuple(cursor)]

        class Model:
            alice_context = []

            def classify(self, *, messages, groupContext, question=None):
                if question is not None:
                    return {"labels": ["ordinary_chat"]}
                if messages[-1].get("senderId") == "alice":
                    self.alice_context = list(groupContext)
                    return {"labels": ["question"]}
                return {"labels": ["ordinary_chat"]}

        class Mcp:
            def discover(self, server):
                return [{"name": "search", "inputSchema": {"type": "object"}}]

        def execute(runtime, command_id, revision, body):
            return runtime.execute({"protocolVersion": 1, "commandId": command_id, "expectedRevision": revision, "body": body})

        with tempfile.TemporaryDirectory() as directory:
            clock, messages, model = Clock(), Messages(), Model()
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", clock=clock, message_source=messages, model=model, mcp=Mcp(), autostart=False)
            execute(runtime, "context-mcp", 0, {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "enabled": True, "transportType": "sse", "url": "https://mcp.test/sse"}})
            tool = execute(runtime, "context-catalog", 1, {"kind": "mcp.test", "serverId": "kb"})["tools"][0]
            execute(runtime, "context-listener", 2, {"kind": "listener.save", "listener": {"id": "context", "name": "Context", "groupId": "room", "enabled": True, "sameSenderMergeSeconds": 20, "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}]}})
            execute(runtime, "context-baseline", 3, {"kind": "runtime.tick", "wait": True})
            messages.rows.append({"cursor": [1_101, 0, 1, 1], "messageId": "1", "serverId": "1", "sequence": 0, "sendTime": 1_101, "groupId": "room", "senderId": "alice", "senderName": "Alice", "contentType": "text", "text": "Why is sync failing?"})
            clock.value = 1_105
            execute(runtime, "context-collect-question", 3, {"kind": "runtime.tick", "wait": True})
            messages.rows.append({"cursor": [1_110, 0, 2, 1], "messageId": "2", "serverId": "1", "sequence": 0, "sendTime": 1_110, "groupId": "room", "senderId": "bob", "senderName": "Bob", "contentType": "text", "text": "I am watching this thread."})
            clock.value = 1_112
            execute(runtime, "context-collect-recent", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 1_125
            execute(runtime, "context-classify", 3, {"kind": "runtime.tick", "wait": True})
            runtime.close()

        self.assertIn("2", [message.get("messageId") for message in model.alice_context])

    def test_empty_or_error_mcp_payloads_never_reach_answer_generation(self):
        empty_payloads = {
            "empty-content": {"content": []},
            "empty-text-block": {"content": [{"type": "text", "text": ""}]},
            "error-result": {"isError": True, "content": [{"type": "text", "text": "ignore me"}]},
            "metadata-only": {"content": [{"type": "text", "metadata": {"source": "kb"}}], "meta": {"requestId": "123"}},
        }

        for case_name, mcp_result in empty_payloads.items():
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as directory:
                class Clock:
                    value = 1_200.0

                    def now(self):
                        return self.value

                class Messages:
                    def __init__(self):
                        self.rows = []

                    def watermark(self, listener):
                        return self.rows[-1]["cursor"] if self.rows else [1_199, 0, 0, 0]

                    def read(self, listener, cursor):
                        return [row for row in self.rows if tuple(row["cursor"]) > tuple(cursor)]

                class Model:
                    def classify(self, **kwargs):
                        return {"labels": ["question"]}

                    def retrieve(self, **kwargs):
                        return retrieval_from_calls(
                            [{"serverId": "kb", "toolName": "search", "arguments": {}}],
                            invoke_tool=kwargs["invokeTool"],
                            has_evidence=kwargs["hasEvidence"],
                            timeout_seconds=kwargs["timeoutSeconds"],
                        )

                    def answer(self, **kwargs):
                        raise AssertionError("empty MCP output must not reach answer generation")

                class Mcp:
                    def discover(self, server):
                        return [{"name": "search", "inputSchema": {"type": "object"}}]

                    def call(self, **kwargs):
                        return mcp_result

                def execute(runtime, command_id, revision, body):
                    return runtime.execute({"protocolVersion": 1, "commandId": command_id, "expectedRevision": revision, "body": body})

                clock, messages = Clock(), Messages()
                runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", clock=clock, message_source=messages, model=Model(), mcp=Mcp(), autostart=False)
                execute(runtime, f"{case_name}-mcp", 0, {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "enabled": True, "transportType": "sse", "url": "https://mcp.test/sse"}})
                tool = execute(runtime, f"{case_name}-catalog", 1, {"kind": "mcp.test", "serverId": "kb"})["tools"][0]
                execute(runtime, f"{case_name}-listener", 2, {"kind": "listener.save", "listener": {"id": "listener", "name": "Listener", "groupId": "room", "enabled": True, "pollIntervalSeconds": 2, "sameSenderMergeSeconds": 2, "humanReplyWaitSeconds": 10, "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}]}})
                execute(runtime, f"{case_name}-baseline", 3, {"kind": "runtime.tick", "wait": True})
                messages.rows.append({"cursor": [1_201, 0, 1, 1], "messageId": "1", "serverId": "1", "sequence": 0, "sendTime": 1_201, "groupId": "room", "senderId": "alice", "senderName": "Alice", "contentType": "text", "text": "What does the knowledge base say?"})
                clock.value = 1_202
                execute(runtime, f"{case_name}-collect", 3, {"kind": "runtime.tick", "wait": True})
                clock.value = 1_204
                execute(runtime, f"{case_name}-classify", 3, {"kind": "runtime.tick", "wait": True})
                clock.value = 1_214
                execute(runtime, f"{case_name}-retrieve", 3, {"kind": "runtime.tick", "wait": True})
                item = runtime.query({"kind": "work.list"})["items"][0]
                runtime.close()

                self.assertEqual(item["status"], "skipped_no_evidence")

    def test_question_waits_for_people_and_requires_evidence_and_review(self):
        class Clock:
            value = 1_000.0

            def now(self):
                return self.value

        class Messages:
            def __init__(self):
                self.rows = []

            def watermark(self, listener):
                return self.rows[-1]["cursor"] if self.rows else None

            def read(self, listener, cursor):
                return [row for row in self.rows if cursor is None or tuple(row["cursor"]) > tuple(cursor)]

        class Model:
            def classify(self, *, messages, groupContext, question=None):
                return {"labels": ["question"], "reason": "asks how to fix a product problem"}

            def retrieve(self, *, question, invokeTool, hasEvidence, timeoutSeconds, **kwargs):
                return retrieval_from_calls(
                    [{
                        "serverId": "kb",
                        "toolName": "search_kb",
                        "arguments": {"query": question},
                    }],
                    invoke_tool=invokeTool,
                    has_evidence=hasEvidence,
                    timeout_seconds=timeoutSeconds,
                )

            def answer(self, *, question, context, evidence, systemPrompt, images):
                return "请在设置中重新启用同步。"

            def review(self, *, question, answer, evidence, images):
                return {"supported": True, "reason": "the evidence gives this exact procedure"}

        class Mcp:
            def discover(self, server):
                return [
                    {
                        "name": "search_kb",
                        "description": "Search support docs",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    }
                ]

            def call(self, *, server, toolName, arguments, timeoutSeconds):
                return {"content": "同步关闭时，在设置 > 同步中重新启用。", "source": "support-kb"}

        class Webhook:
            def __init__(self):
                self.sent = []

            def send(self, **payload):
                self.sent.append(payload)
                return {"status": "sent"}

        def execute(runtime, command_id, revision, body):
            return runtime.execute(
                {
                    "protocolVersion": 1,
                    "commandId": command_id,
                    "expectedRevision": revision,
                    "body": body,
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            messages = Messages()
            webhook = Webhook()
            runtime = ReplyRuntime(
                Path(directory) / "runtime.sqlite3",
                clock=clock,
                message_source=messages,
                model=Model(),
                mcp=Mcp(),
                webhook=webhook,
                autostart=False,
            )
            execute(
                runtime,
                "save-mcp",
                0,
                {
                    "kind": "mcp.save",
                    "server": {
                        "id": "kb",
                        "name": "Knowledge",
                        "enabled": True,
                        "transportType": "streamable-http",
                        "url": "https://mcp.example.test/mcp",
                    },
                },
            )
            catalog = execute(runtime, "test-mcp", 1, {"kind": "mcp.test", "serverId": "kb"})
            execute(
                runtime,
                "save-listener",
                2,
                {
                    "kind": "listener.save",
                    "listener": {
                        "id": "support",
                        "name": "Support",
                        "groupId": "room-1",
                        "enabled": True,
                        "toolGrants": [
                            {
                                "serverId": "kb",
                                "toolName": "search_kb",
                                "schemaSha256": catalog["tools"][0]["schemaSha256"],
                            }
                        ],
                        "systemPrompt": "Only answer product questions.",
                        "humanReplyWaitSeconds": 120,
                        "sameSenderMergeSeconds": 20,
                        "autoSend": False,
                    },
                },
            )

            # The first poll establishes a tail cursor and deliberately processes no offline history.
            execute(runtime, "baseline", 3, {"kind": "runtime.tick", "wait": True})
            messages.rows.append(
                {
                    "cursor": [1_001, 1, 10, 20],
                    "messageId": "10",
                    "serverId": "20",
                    "sequence": 1,
                    "sendTime": 1_001,
                    "groupId": "room-1",
                    "senderId": "alice-local-id",
                    "senderName": "Alice",
                    "account": "alice",
                    "mobile": "13800138000",
                    "contentType": "text",
                    "text": "同步功能为什么不工作了？",
                }
            )
            clock.value = 1_005
            execute(runtime, "collect", 3, {"kind": "runtime.tick", "wait": True})
            self.assertEqual(runtime.query({"kind": "work.list"})["items"][0]["status"], "collecting")

            clock.value = 1_025
            execute(runtime, "classify", 3, {"kind": "runtime.tick", "wait": True})
            waiting = runtime.query({"kind": "work.list"})["items"][0]
            self.assertEqual(waiting["status"], "waiting_for_human_reply")

            clock.value = 1_145
            execute(runtime, "retrieve", 3, {"kind": "runtime.tick", "wait": True})
            item = runtime.query({"kind": "work.detail", "workId": waiting["id"]})["item"]
            runtime.close()

        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["answer"], "请在设置中重新启用同步。")
        self.assertEqual(item["review"]["supported"], True)
        self.assertIn("support-kb", item["evidence"][0]["summary"])
        self.assertNotIn("messages", item)
        self.assertNotIn("groupContext", item)
        self.assertEqual(webhook.sent, [])

    def test_confirmed_webhook_auto_sends_with_a_true_account_mention(self):
        class Clock:
            value = 2_000.0

            def now(self):
                return self.value

        class Messages:
            rows = []

            def watermark(self, listener):
                return self.rows[-1]["cursor"] if self.rows else None

            def read(self, listener, cursor):
                return [row for row in self.rows if cursor is None or tuple(row["cursor"]) > tuple(cursor)]

        class Model:
            def classify(self, **kwargs):
                return {"labels": ["question"]}

            def retrieve(self, **kwargs):
                return retrieval_from_calls(
                    [{"serverId": "kb", "toolName": "search", "arguments": {"query": "q"}}],
                    invoke_tool=kwargs["invokeTool"],
                    has_evidence=kwargs["hasEvidence"],
                    timeout_seconds=kwargs["timeoutSeconds"],
                )

            def answer(self, **kwargs):
                return "请清理缓存后重试。"

            def review(self, **kwargs):
                return {"supported": True}

        class Mcp:
            def discover(self, server):
                return [{"name": "search", "inputSchema": {"type": "object"}}]

            def call(self, **kwargs):
                return "知识库步骤：清理缓存后重试。"

        class Webhook:
            def __init__(self):
                self.sent = []

            def send(self, **payload):
                self.sent.append(payload)
                return {"status": "sent", "requestId": f"request-{len(self.sent)}"}

        def execute(runtime, command_id, revision, body):
            return runtime.execute(
                {
                    "protocolVersion": 1,
                    "commandId": command_id,
                    "expectedRevision": revision,
                    "body": body,
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            clock, messages, webhook = Clock(), Messages(), Webhook()
            runtime = ReplyRuntime(
                Path(directory) / "runtime.sqlite3",
                clock=clock,
                message_source=messages,
                model=Model(),
                mcp=Mcp(),
                webhook=webhook,
                autostart=False,
            )
            execute(runtime, "mcp", 0, {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "enabled": True, "transportType": "sse", "url": "https://mcp.test/sse"}})
            tool = execute(runtime, "catalog", 1, {"kind": "mcp.test", "serverId": "kb"})["tools"][0]
            listener = {
                "id": "auto",
                "name": "Auto",
                "groupId": "room",
                "enabled": True,
                "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}],
                "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=visible-test",
                "sameSenderMergeSeconds": 2,
                "humanReplyWaitSeconds": 10,
                "autoSend": False,
            }
            execute(runtime, "listener", 2, {"kind": "listener.save", "listener": listener})
            tested = execute(runtime, "test-webhook", 3, {"kind": "listener.test_webhook", "listenerId": "auto"})
            self.assertIn(tested["testCode"], webhook.sent[0]["text"])
            with self.assertRaises(RuntimeProtocolError) as not_visible:
                execute(runtime, "confirm-without-visible-proof", 4, {"kind": "listener.confirm_webhook", "listenerId": "auto", "testCode": tested["testCode"]})
            self.assertEqual(not_visible.exception.code, "WEBHOOK_NOT_VISIBLE")
            with self.assertRaises(RuntimeProtocolError) as explicitly_not_visible:
                execute(runtime, "confirm-with-false-proof", 4, {"kind": "listener.confirm_webhook", "listenerId": "auto", "testCode": tested["testCode"], "appearedInSelectedGroup": False})
            self.assertEqual(explicitly_not_visible.exception.code, "WEBHOOK_NOT_VISIBLE")
            execute(runtime, "confirm-webhook", 4, {"kind": "listener.confirm_webhook", "listenerId": "auto", "testCode": tested["testCode"], "appearedInSelectedGroup": True})
            listener.pop("webhookUrl")
            listener["autoSend"] = True
            execute(runtime, "enable-auto", 5, {"kind": "listener.save", "listener": listener})
            execute(runtime, "baseline-auto", 6, {"kind": "runtime.tick", "wait": True})

            messages.rows.append(
                {
                    "cursor": [2_001, 1, "m1", "s1"],
                    "messageId": "m1",
                    "serverId": "s1",
                    "sequence": 1,
                    "sendTime": 2_001,
                    "senderId": "123456",  # local numeric id must not be used as userid
                    "senderName": "Alice",
                    "account": "alice.account",
                    "mobile": "13800138000",
                    "contentType": "text",
                    "text": "缓存错误怎么修复？",
                }
            )
            clock.value = 2_005
            execute(runtime, "collect-auto", 6, {"kind": "runtime.tick", "wait": True})
            clock.value = 2_007
            execute(runtime, "classify-auto", 6, {"kind": "runtime.tick", "wait": True})
            clock.value = 2_017
            execute(runtime, "retrieve-auto", 6, {"kind": "runtime.tick", "wait": True})
            work = runtime.query({"kind": "work.list"})["items"][0]
            runtime.close()

        self.assertEqual(work["status"], "sent")
        self.assertEqual(len(webhook.sent), 2)
        self.assertEqual(webhook.sent[1]["mentionedList"], ["alice.account"])
        self.assertEqual(webhook.sent[1]["mentionedMobileList"], [])
        self.assertNotIn("123456", webhook.sent[1]["mentionedList"])

    def test_semantic_human_answer_closes_the_waiting_question(self):
        class Clock:
            value = 3_000.0

            def now(self):
                return self.value

        class Messages:
            rows = []

            def watermark(self, listener):
                return self.rows[-1]["cursor"] if self.rows else None

            def read(self, listener, cursor):
                return [row for row in self.rows if cursor is None or tuple(row["cursor"]) > tuple(cursor)]

        class Model:
            def classify(self, *, messages, groupContext, question=None):
                text = messages[-1]["text"]
                if question:
                    return {"labels": ["human_answer"] if "重新登录" in text else ["chat"]}
                return {"labels": ["question"] if "怎么办" in text else ["chat"]}

            def retrieve(self, **kwargs):
                raise AssertionError("human answered questions must not reach MCP planning")

        class Mcp:
            calls = 0

            def discover(self, server):
                return [{"name": "search", "inputSchema": {"type": "object"}}]

            def call(self, **kwargs):
                self.calls += 1
                return "unused"

        def execute(runtime, command_id, revision, body):
            return runtime.execute({"protocolVersion": 1, "commandId": command_id, "expectedRevision": revision, "body": body})

        with tempfile.TemporaryDirectory() as directory:
            clock, messages, mcp = Clock(), Messages(), Mcp()
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", clock=clock, message_source=messages, model=Model(), mcp=mcp, autostart=False)
            execute(runtime, "mcp-human", 0, {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "transportType": "sse", "url": "https://mcp.test/sse"}})
            tool = execute(runtime, "catalog-human", 1, {"kind": "mcp.test", "serverId": "kb"})["tools"][0]
            execute(runtime, "listener-human", 2, {"kind": "listener.save", "listener": {"id": "human", "name": "Human first", "groupId": "room", "enabled": True, "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}], "sameSenderMergeSeconds": 2, "humanReplyWaitSeconds": 10}})
            execute(runtime, "baseline-human", 3, {"kind": "runtime.tick", "wait": True})
            messages.rows.append({"cursor": [3001, 1, "q", "sq"], "messageId": "q", "serverId": "sq", "sequence": 1, "sendTime": 3001, "senderId": "alice", "senderName": "Alice", "contentType": "text", "text": "登录失败怎么办？"})
            clock.value = 3_005
            execute(runtime, "collect-human", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 3_007
            execute(runtime, "classify-human", 3, {"kind": "runtime.tick", "wait": True})
            waiting = runtime.query({"kind": "work.list"})["items"][0]
            self.assertEqual(waiting["status"], "waiting_for_human_reply")

            messages.rows.append({"cursor": [3008, 2, "a", "sa"], "messageId": "a", "serverId": "sa", "sequence": 2, "sendTime": 3008, "senderId": "bob", "senderName": "Bob", "contentType": "text", "text": "重新登录就可以了。"})
            clock.value = 3_012
            execute(runtime, "human-answer", 3, {"kind": "runtime.tick", "wait": True})
            answered = runtime.query({"kind": "work.detail", "workId": waiting["id"]})["item"]
            clock.value = 3_100
            execute(runtime, "past-deadline", 3, {"kind": "runtime.tick", "wait": True})
            runtime.close()

        self.assertEqual(answered["status"], "answered_by_human")
        self.assertEqual(answered["humanAnswerMessage"]["senderName"], "Bob")
        self.assertEqual(mcp.calls, 0)

    def test_human_answer_during_slow_mcp_forces_the_reviewed_result_to_pending(self):
        class Clock:
            value = 4_000.0

            def now(self):
                return self.value

        class Messages:
            rows = []

            def watermark(self, listener):
                return self.rows[-1]["cursor"] if self.rows else None

            def read(self, listener, cursor):
                return [row for row in self.rows if cursor is None or tuple(row["cursor"]) > tuple(cursor)]

        class Model:
            def classify(self, *, messages, groupContext, question=None):
                if question:
                    return {"labels": ["human_answer"] if messages[-1]["senderId"] == "bob" else ["chat"]}
                return {"labels": ["question"] if messages[-1]["senderId"] == "alice" else ["chat"]}

            def retrieve(self, **kwargs):
                return retrieval_from_calls(
                    [{"serverId": "kb", "toolName": "search", "arguments": {}}],
                    invoke_tool=kwargs["invokeTool"],
                    has_evidence=kwargs["hasEvidence"],
                    timeout_seconds=kwargs["timeoutSeconds"],
                )

            def answer(self, **kwargs):
                return "AI evidence-backed answer"

            def review(self, **kwargs):
                return {"supported": True}

        class Mcp:
            started = threading.Event()
            release = threading.Event()

            def discover(self, server):
                return [{"name": "search", "inputSchema": {"type": "object"}}]

            def call(self, **kwargs):
                self.started.set()
                if not self.release.wait(3):
                    raise TimeoutError("test did not release MCP")
                return "slow but useful evidence"

        def execute(runtime, command_id, revision, body):
            return runtime.execute({"protocolVersion": 1, "commandId": command_id, "expectedRevision": revision, "body": body})

        with tempfile.TemporaryDirectory() as directory:
            clock, messages, mcp = Clock(), Messages(), Mcp()
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", clock=clock, message_source=messages, model=Model(), mcp=mcp, autostart=False)
            execute(runtime, "mcp-slow", 0, {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "transportType": "sse", "url": "https://mcp.test/sse"}})
            tool = execute(runtime, "catalog-slow", 1, {"kind": "mcp.test", "serverId": "kb"})["tools"][0]
            execute(runtime, "listener-slow", 2, {"kind": "listener.save", "listener": {"id": "slow", "name": "Slow", "groupId": "room", "enabled": True, "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}], "pollIntervalSeconds": 2, "sameSenderMergeSeconds": 2, "humanReplyWaitSeconds": 10, "autoSend": False}})
            execute(runtime, "baseline-slow", 3, {"kind": "runtime.tick", "wait": True})
            messages.rows.append({"cursor": [4001, 1, "q", "sq"], "messageId": "q", "serverId": "sq", "sequence": 1, "sendTime": 4001, "senderId": "alice", "senderName": "Alice", "contentType": "text", "text": "这是一个问题吗？"})
            clock.value = 4_002
            execute(runtime, "collect-slow", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 4_004
            execute(runtime, "classify-slow", 3, {"kind": "runtime.tick", "wait": True})
            work_id = runtime.query({"kind": "work.list"})["items"][0]["id"]
            clock.value = 4_014
            execute(runtime, "start-slow", 3, {"kind": "runtime.tick", "wait": False})
            self.assertTrue(mcp.started.wait(2))

            messages.rows.append({"cursor": [4015, 2, "a", "sa"], "messageId": "a", "serverId": "sa", "sequence": 2, "sendTime": 4015, "senderId": "bob", "senderName": "Bob", "contentType": "text", "text": "我来回答这个问题。"})
            clock.value = 4_016
            execute(runtime, "answer-slow", 3, {"kind": "runtime.tick", "wait": False})
            mcp.release.set()
            clock.value = 4_018
            execute(runtime, "finish-slow", 3, {"kind": "runtime.tick", "wait": True})
            item = runtime.query({"kind": "work.detail", "workId": work_id})["item"]
            runtime.close()

        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["pendingReason"], "human_answered_during_retrieval")
        self.assertEqual(item["humanAnswerMessage"]["senderName"], "Bob")

    def test_different_senders_call_the_same_mcp_concurrently_up_to_listener_limit(self):
        class Clock:
            value = 5_000.0

            def now(self):
                return self.value

        class Messages:
            rows = []

            def watermark(self, listener):
                return self.rows[-1]["cursor"] if self.rows else None

            def read(self, listener, cursor):
                return [row for row in self.rows if cursor is None or tuple(row["cursor"]) > tuple(cursor)]

        class Model:
            def classify(self, **kwargs):
                return {"labels": ["question"]}

            def retrieve(self, *, question, invokeTool, hasEvidence, timeoutSeconds, **kwargs):
                return retrieval_from_calls(
                    [{"serverId": "kb", "toolName": "search", "arguments": {"query": question}}],
                    invoke_tool=invokeTool,
                    has_evidence=hasEvidence,
                    timeout_seconds=timeoutSeconds,
                )

            def answer(self, **kwargs):
                return "answer"

            def review(self, **kwargs):
                return {"supported": True}

        class ConcurrentMcp:
            def __init__(self):
                self.lock = threading.Lock()
                self.release = threading.Event()
                self.two_started = threading.Event()
                self.active = 0
                self.max_active = 0

            def discover(self, server):
                return [{"name": "search", "inputSchema": {"type": "object"}}]

            def call(self, **kwargs):
                with self.lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    if self.active >= 2:
                        self.two_started.set()
                if not self.release.wait(3):
                    raise TimeoutError("concurrency test did not release MCP")
                with self.lock:
                    self.active -= 1
                return {"content": "evidence"}

        def execute(runtime, command_id, revision, body):
            return runtime.execute({"protocolVersion": 1, "commandId": command_id, "expectedRevision": revision, "body": body})

        with tempfile.TemporaryDirectory() as directory:
            clock, messages, mcp = Clock(), Messages(), ConcurrentMcp()
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", clock=clock, message_source=messages, model=Model(), mcp=mcp, autostart=False)
            execute(runtime, "mcp-concurrent", 0, {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "transportType": "sse", "url": "https://mcp.test/sse"}})
            tool = execute(runtime, "catalog-concurrent", 1, {"kind": "mcp.test", "serverId": "kb"})["tools"][0]
            execute(runtime, "listener-concurrent", 2, {"kind": "listener.save", "listener": {"id": "concurrent", "name": "Concurrent", "groupId": "room", "enabled": True, "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}], "pollIntervalSeconds": 2, "sameSenderMergeSeconds": 2, "humanReplyWaitSeconds": 10, "maxConcurrency": 2}})
            execute(runtime, "baseline-concurrent", 3, {"kind": "runtime.tick", "wait": True})
            for index, sender in enumerate(("alice", "bob", "carol"), start=1):
                messages.rows.append({"cursor": [5001, index, f"m{index}", f"s{index}"], "messageId": f"m{index}", "serverId": f"s{index}", "sequence": index, "sendTime": 5001, "senderId": sender, "senderName": sender.title(), "contentType": "text", "text": f"question from {sender}?"})
            clock.value = 5_002
            execute(runtime, "collect-concurrent", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 5_004
            execute(runtime, "classify-concurrent", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 5_014
            execute(runtime, "start-concurrent", 3, {"kind": "runtime.tick", "wait": False})
            self.assertTrue(mcp.two_started.wait(2))
            during = runtime.query({"kind": "work.list"})["items"]
            self.assertEqual(sum(item["status"] == "retrieving" for item in during), 2)
            self.assertEqual(sum(item["status"] == "queued_retrieval" for item in during), 1)
            mcp.release.set()
            clock.value = 5_016
            execute(runtime, "finish-first-concurrent", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 5_018
            execute(runtime, "finish-last-concurrent", 3, {"kind": "runtime.tick", "wait": True})
            final = runtime.query({"kind": "work.list"})["items"]
            first_page = runtime.query({"kind": "work.list", "bucket": "pending", "limit": 1, "offset": 0})
            runtime.close()

        self.assertEqual(mcp.max_active, 2)
        self.assertEqual([item["status"] for item in final], ["pending", "pending", "pending"])
        self.assertEqual(first_page["total"], 3)
        self.assertEqual(len(first_page["items"]), 1)
        self.assertTrue(first_page["hasMore"])

    def test_webhook_timeout_is_at_most_once_and_can_be_discarded(self):
        class Clock:
            value = 6_000.0

            def now(self):
                return self.value

        class Messages:
            rows = []

            def watermark(self, listener):
                return self.rows[-1]["cursor"] if self.rows else None

            def read(self, listener, cursor):
                return [row for row in self.rows if cursor is None or tuple(row["cursor"]) > tuple(cursor)]

        class Model:
            def classify(self, **kwargs):
                return {"labels": ["question"]}

            def retrieve(self, **kwargs):
                return retrieval_from_calls(
                    [{"serverId": "kb", "toolName": "search", "arguments": {}}],
                    invoke_tool=kwargs["invokeTool"],
                    has_evidence=kwargs["hasEvidence"],
                    timeout_seconds=kwargs["timeoutSeconds"],
                )

            def answer(self, **kwargs):
                return "reviewed answer"

            def review(self, **kwargs):
                return {"supported": True}

        class Mcp:
            def discover(self, server):
                return [{"name": "search", "inputSchema": {"type": "object"}}]

            def call(self, **kwargs):
                return "evidence"

        class TimeoutWebhook:
            calls = 0

            def send(self, **kwargs):
                self.calls += 1
                raise TimeoutError("response lost after request body was sent")

        def command(command_id, body, revision=None):
            return {
                "protocolVersion": 1,
                "commandId": command_id,
                **({} if revision is None else {"expectedRevision": revision}),
                "body": body,
            }

        with tempfile.TemporaryDirectory() as directory:
            clock, messages, webhook = Clock(), Messages(), TimeoutWebhook()
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", clock=clock, message_source=messages, model=Model(), mcp=Mcp(), webhook=webhook, autostart=False)
            runtime.execute(command("mcp-unknown", {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "transportType": "sse", "url": "https://mcp.test/sse"}}, 0))
            tool = runtime.execute(command("catalog-unknown", {"kind": "mcp.test", "serverId": "kb"}, 1))["tools"][0]
            runtime.execute(command("listener-unknown", {"kind": "listener.save", "listener": {"id": "unknown", "name": "Unknown", "groupId": "room", "enabled": True, "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}], "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=timeout-key", "pollIntervalSeconds": 2, "sameSenderMergeSeconds": 2, "humanReplyWaitSeconds": 10}}, 2))
            runtime.execute(command("baseline-unknown", {"kind": "runtime.tick", "wait": True}, 3))
            messages.rows.append({"cursor": [6001, 1, "q", "s"], "messageId": "q", "serverId": "s", "sequence": 1, "sendTime": 6001, "senderId": "alice", "senderName": "Alice", "account": "alice.account", "contentType": "text", "text": "question?"})
            clock.value = 6_002
            runtime.execute(command("collect-unknown", {"kind": "runtime.tick", "wait": True}, 3))
            clock.value = 6_004
            runtime.execute(command("classify-unknown", {"kind": "runtime.tick", "wait": True}, 3))
            clock.value = 6_014
            runtime.execute(command("retrieve-unknown", {"kind": "runtime.tick", "wait": True}, 3))
            pending = runtime.query({"kind": "work.list"})["items"][0]
            send_command = command("send-unknown", {"kind": "work.send", "workId": pending["id"], "expectedVersion": pending["version"]}, 3)
            first = runtime.execute(send_command)
            duplicate = runtime.execute(send_command)
            unknown = runtime.query({"kind": "work.detail", "workId": pending["id"]})["item"]
            with self.assertRaises(RuntimeProtocolError) as unsafe_retry:
                runtime.execute(command("unsafe-retry", {"kind": "work.send", "workId": pending["id"], "expectedVersion": pending["version"]}, 3))
            discarded = runtime.execute(command("discard-unknown", {"kind": "work.discard", "workId": pending["id"], "expectedVersion": pending["version"]}, 3))
            runtime.close()

        self.assertEqual(first, duplicate)
        self.assertEqual(first["status"], "delivery_unknown")
        self.assertEqual(unknown["status"], "delivery_unknown")
        self.assertEqual(webhook.calls, 1)
        self.assertEqual(unsafe_retry.exception.code, "DELIVERY_CONFIRMATION_REQUIRED")
        self.assertEqual(discarded["status"], "discarded")


if __name__ == "__main__":
    unittest.main()
