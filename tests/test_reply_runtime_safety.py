from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from worker.reply_runtime import ReplyRuntime, RuntimeProtocolError
from worker.reply_runtime.adapters import McpSdkAdapter, _list_all_tools
from worker.reply_runtime.message_source import LocalWeComMessageSource


class ReplyRuntimeCursorSafetyTests(unittest.TestCase):
    def test_forced_preflight_read_bypasses_lagging_session_watermark(self):
        source = LocalWeComMessageSource()
        source._refresh_identities = lambda config: None
        raw = {
            "send_time": 100,
            "sequence": 1,
            "message_id": 6,
            "server_id": 1,
            "content_type": 1,
        }
        formatted = {
            "send_time": 100,
            "sequence": 1,
            "message_id": 6,
            "server_id": 1,
            "content_type": 1,
            "sender_id": 42,
            "sender": "Alice",
            "content": "human answer",
        }
        with (
            patch("worker.reply_runtime.message_source.load_config", return_value={}),
            patch(
                "worker.reply_runtime.message_source.get_conversation_state",
                return_value={"last_message_time": 100, "last_message_id": 5},
            ),
            patch("worker.reply_runtime.message_source.read_messages", return_value=[raw]) as reader,
            patch("worker.reply_runtime.message_source.format_message", return_value=formatted),
        ):
            fast_result = source.read({"groupId": "room"}, [100, 0, 5, 1])
            reader.assert_not_called()
            forced_result = source.read_force({"groupId": "room"}, [100, 0, 5, 1])

        self.assertEqual(fast_result, [])
        self.assertEqual([item["messageId"] for item in forced_result], ["6"])

    def test_slow_poll_from_old_listener_generation_cannot_cross_into_new_group(self):
        class Clock:
            value = 2_000.0

            def now(self):
                return self.value

        class BlockingSource:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()

            def watermark(self, listener):
                return [1_999, 0, 0, 0]

            def read(self, listener, cursor):
                self.entered.set()
                self.release.wait(5)
                return [{"cursor": [2_001, 0, 1, 1], "messageId": "1", "serverId": "1", "sequence": 0, "sendTime": 2_001, "groupId": "old-room", "senderId": "alice", "contentType": "text", "text": "old group question?"}]

        class Model:
            def classify(self, **kwargs):
                return {"labels": ["question"]}

        class Mcp:
            def discover(self, server):
                return [{"name": "search", "inputSchema": {"type": "object"}}]

        def command(runtime, command_id, revision, body):
            return runtime.execute({"protocolVersion": 1, "commandId": command_id, "expectedRevision": revision, "body": body})

        with tempfile.TemporaryDirectory() as directory:
            clock, source = Clock(), BlockingSource()
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", clock=clock, message_source=source, model=Model(), mcp=Mcp(), autostart=False)
            command(runtime, "stale-mcp", 0, {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "enabled": True, "transportType": "sse", "url": "https://mcp.test/sse"}})
            tool = command(runtime, "stale-catalog", 1, {"kind": "mcp.test", "serverId": "kb"})["tools"][0]
            listener = {"id": "listener", "name": "Listener", "groupId": "old-room", "enabled": True, "pollIntervalSeconds": 2, "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}]}
            command(runtime, "stale-listener", 2, {"kind": "listener.save", "listener": listener})
            command(runtime, "stale-baseline", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 2_002
            poll_thread = threading.Thread(target=lambda: command(runtime, "slow-old-poll", 3, {"kind": "runtime.tick", "wait": True}))
            poll_thread.start()
            self.assertTrue(source.entered.wait(2))
            listener["groupId"] = "new-room"
            command(runtime, "switch-group", 3, {"kind": "listener.save", "listener": listener})
            source.release.set()
            poll_thread.join(2)
            items = runtime.query({"kind": "work.list"})["items"]
            runtime.close()

        self.assertEqual(items, [])

    def test_same_second_zero_sequence_messages_use_a_numeric_composite_cursor(self):
        class Clock:
            value = 1_000.0

            def now(self):
                return self.value

        class Source:
            rows = []
            fail = False

            def watermark(self, listener):
                return [999, 0, 10, 20]

            def read(self, listener, cursor):
                self.last_cursor = cursor
                return self.rows

        class Model:
            def classify(self, **kwargs):
                return {"labels": ["question"]}

        class Mcp:
            def discover(self, server):
                return [{"name": "search", "inputSchema": {"type": "object"}}]

        def command(command_id, revision, body):
            return {
                "protocolVersion": 1,
                "commandId": command_id,
                "expectedRevision": revision,
                "body": body,
            }

        with tempfile.TemporaryDirectory() as directory:
            clock, source = Clock(), Source()
            runtime = ReplyRuntime(
                Path(directory) / "runtime.sqlite3",
                clock=clock,
                message_source=source,
                model=Model(),
                mcp=Mcp(),
                autostart=False,
            )
            runtime.execute(command("mcp", 0, {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "transportType": "sse", "url": "https://mcp.test/sse"}}))
            tool = runtime.execute(command("catalog", 1, {"kind": "mcp.test", "serverId": "kb"}))["tools"][0]
            runtime.execute(command("listener", 2, {"kind": "listener.save", "listener": {"id": "listener", "name": "Listener", "groupId": "room", "enabled": True, "pollIntervalSeconds": 2, "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}]}}))
            runtime.execute(command("baseline", 3, {"kind": "runtime.tick", "wait": True}))
            source.rows = [
                {"cursor": [1000, 0, 11, 21], "messageId": "11", "serverId": "21", "sequence": 0, "sendTime": 1000, "senderId": "alice", "contentType": "text", "text": "first?"},
                {"cursor": [1000, 0, 12, 22], "messageId": "12", "serverId": "22", "sequence": 0, "sendTime": 1000, "senderId": "bob", "contentType": "text", "text": "second?"},
            ]
            clock.value = 1_002
            runtime.execute(command("poll", 3, {"kind": "runtime.tick", "wait": True}))
            items = runtime.query({"kind": "work.list"})["items"]
            runtime.close()

        self.assertEqual(source.last_cursor, [999, 0, 10, 20])
        self.assertTrue(all(isinstance(value, int) for value in source.last_cursor))
        self.assertEqual(len(items), 2)


class McpPaginationTests(unittest.TestCase):
    def test_list_tools_follows_cursors_and_bounds_the_catalog(self):
        class Page:
            def __init__(self, tools, cursor):
                self.tools = tools
                self.nextCursor = cursor

        class Tool:
            def __init__(self, name):
                self.name = name
                self.title = None
                self.description = name
                self.inputSchema = {"type": "object"}

        class Session:
            calls = []

            async def list_tools(self, cursor=None):
                self.calls.append(cursor)
                if cursor is None:
                    return Page([Tool("first")], "page-2")
                return Page([Tool("second")], None)

        session = Session()
        tools = asyncio.run(_list_all_tools(session, max_pages=5, max_tools=10))

        self.assertEqual(session.calls, [None, "page-2"])
        self.assertEqual([tool["name"] for tool in tools], ["first", "second"])

    def test_call_timeout_includes_first_connection_time(self):
        class RecordingAdapter(McpSdkAdapter):
            def __init__(self):
                self.connect_timeout = 15
                self.seen_timeout = None

            def _request(self, server, operation, payload, *, timeout):
                self.seen_timeout = timeout
                return {"ok": True}

        adapter = RecordingAdapter()
        adapter.call(server={"id": "kb"}, toolName="search", arguments={}, timeoutSeconds=60)

        self.assertEqual(adapter.seen_timeout, 60)


class McpHealthPersistenceTests(unittest.TestCase):
    def test_write_commands_require_a_configuration_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", autostart=False)
            with self.assertRaises(RuntimeProtocolError) as missing:
                runtime.execute(
                    {
                        "protocolVersion": 1,
                        "commandId": "missing-revision",
                        "body": {
                            "kind": "mcp.save",
                            "server": {
                                "id": "kb",
                                "name": "KB",
                                "transportType": "sse",
                                "url": "https://mcp.test/sse",
                            },
                        },
                    }
                )
            runtime.close()

        self.assertEqual(missing.exception.code, "REVISION_REQUIRED")

    def test_failed_discovery_is_persisted_redacted_and_idempotent(self):
        class Mcp:
            failing = False

            def discover(self, server):
                if self.failing:
                    raise OSError("Bearer top-secret-token could not connect")
                return [{"name": "search", "inputSchema": {"type": "object"}}]

        def command(command_id, revision, body):
            return {"protocolVersion": 1, "commandId": command_id, "expectedRevision": revision, "body": body}

        with tempfile.TemporaryDirectory() as directory:
            mcp = Mcp()
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", mcp=mcp, autostart=False)
            runtime.execute(command("mcp", 0, {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "enabled": True, "transportType": "sse", "url": "https://mcp.test/sse", "headers": {"Authorization": "Bearer top-secret-token"}}}))
            tool = runtime.execute(command("catalog", 1, {"kind": "mcp.test", "serverId": "kb"}))["tools"][0]
            runtime.execute(command("listener", 2, {"kind": "listener.save", "listener": {"id": "listener", "name": "Listener", "groupId": "room", "enabled": True, "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}]}}))
            mcp.failing = True
            failed_command = command("failed-test", 3, {"kind": "mcp.test", "serverId": "kb"})
            with self.assertRaises(RuntimeProtocolError) as first:
                runtime.execute(failed_command)
            with self.assertRaises(RuntimeProtocolError) as duplicate:
                runtime.execute(failed_command)
            catalog = runtime.query({"kind": "mcp.catalog", "serverId": "kb"})
            listener = runtime.query({"kind": "listener.list"})["listeners"][0]
            runtime.close()

        self.assertEqual(first.exception.code, "MCP_CONNECTION_FAILED")
        self.assertEqual(duplicate.exception.code, "MCP_CONNECTION_FAILED")
        self.assertEqual(catalog["revision"], 4)
        self.assertEqual(catalog["error"]["code"], "MCP_CONNECTION_FAILED")
        self.assertEqual(listener["health"]["status"], "server_unhealthy")
        self.assertNotIn("top-secret-token", repr(catalog))


class ReplyRuntimeGenerationSafetyTests(unittest.TestCase):
    def test_listener_change_during_supplement_classification_cannot_revive_old_work(self):
        class Clock:
            value = 81_000.0

            def now(self):
                return self.value

        class Source:
            def __init__(self):
                self.rows = []

            def watermark(self, listener):
                return self.rows[-1]["cursor"] if self.rows else [80_999, 0, 0, 0]

            def read(self, listener, cursor):
                return [row for row in self.rows if tuple(row["cursor"]) > tuple(cursor)]

        class Model:
            def __init__(self):
                self.supplement_entered = threading.Event()
                self.release = threading.Event()

            def classify(self, *, messages, groupContext, question=None):
                if question is not None:
                    self.supplement_entered.set()
                    self.release.wait(5)
                    return {"labels": ["supplement"]}
                return {"labels": ["question"]}

            def match_human_answers(self, **kwargs):
                return {"matches": []}

        class Mcp:
            def discover(self, server):
                return [{"name": "search", "inputSchema": {"type": "object"}}]

        def command(runtime, command_id, revision, body):
            return runtime.execute({"protocolVersion": 1, "commandId": command_id, "expectedRevision": revision, "body": body})

        with tempfile.TemporaryDirectory() as directory:
            clock, source, model = Clock(), Source(), Model()
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", clock=clock, message_source=source, model=model, mcp=Mcp(), autostart=False)
            command(runtime, "fence-mcp", 0, {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "enabled": True, "transportType": "sse", "url": "https://mcp.test/sse"}})
            tool = command(runtime, "fence-catalog", 1, {"kind": "mcp.test", "serverId": "kb"})["tools"][0]
            listener = {"id": "listener", "name": "Listener", "groupId": "room", "enabled": True, "pollIntervalSeconds": 2, "sameSenderMergeSeconds": 2, "humanReplyWaitSeconds": 120, "systemPrompt": "old", "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}]}
            command(runtime, "fence-listener", 2, {"kind": "listener.save", "listener": listener})
            command(runtime, "fence-baseline", 3, {"kind": "runtime.tick", "wait": True})
            source.rows.append({"cursor": [81_001, 0, 1, 1], "messageId": "1", "serverId": "1", "sequence": 0, "sendTime": 81_001, "groupId": "room", "senderId": "alice", "contentType": "text", "text": "question?"})
            clock.value = 81_002
            command(runtime, "fence-collect", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 81_004
            command(runtime, "fence-classify", 3, {"kind": "runtime.tick", "wait": True})
            source.rows.append({"cursor": [81_005, 0, 2, 1], "messageId": "2", "serverId": "1", "sequence": 0, "sendTime": 81_005, "groupId": "room", "senderId": "alice", "contentType": "text", "text": "more detail"})
            clock.value = 81_006
            supplement_thread = threading.Thread(target=lambda: command(runtime, "fence-supplement", 3, {"kind": "runtime.tick", "wait": True}))
            supplement_thread.start()
            self.assertTrue(model.supplement_entered.wait(2))
            listener["systemPrompt"] = "new"
            command(runtime, "fence-change-listener", 3, {"kind": "listener.save", "listener": listener})
            model.release.set()
            supplement_thread.join(2)
            items = runtime.query({"kind": "work.list"})["items"]
            runtime.close()

        self.assertEqual([item["status"] for item in items], ["closed_configuration_changed"])

    def test_slow_human_answer_classification_does_not_block_other_senders(self):
        class Clock:
            value = 80_000.0

            def now(self):
                return self.value

        class Source:
            def __init__(self):
                self.rows = []

            def watermark(self, listener):
                return self.rows[-1]["cursor"] if self.rows else [79_999, 0, 0, 0]

            def read(self, listener, cursor):
                return [row for row in self.rows if tuple(row["cursor"]) > tuple(cursor)]

        class Model:
            def __init__(self):
                self.bob_entered = threading.Event()
                self.release_bob = threading.Event()
                self.bob_match_calls = 0

            def classify(self, *, messages, groupContext, question=None):
                if question and messages[-1].get("senderId") == "bob":
                    self.bob_match_calls += 1
                    self.bob_entered.set()
                    self.release_bob.wait(5)
                    return {"labels": ["human_answer"]}
                if question:
                    return {"labels": ["chat"]}
                return {"labels": ["question"]}

            def match_human_answers(self, *, message, groupContext, candidates):
                if message.get("senderId") == "bob":
                    self.bob_match_calls += 1
                    self.bob_entered.set()
                    self.release_bob.wait(5)
                    return {"matches": [{"workId": candidates[0]["workId"], "labels": ["human_answer"]}]}
                return {"matches": []}

        class Mcp:
            def discover(self, server):
                return [{"name": "search", "inputSchema": {"type": "object"}}]

        def command(runtime, command_id, revision, body):
            return runtime.execute({"protocolVersion": 1, "commandId": command_id, "expectedRevision": revision, "body": body})

        with tempfile.TemporaryDirectory() as directory:
            clock, source, model = Clock(), Source(), Model()
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", clock=clock, message_source=source, model=model, mcp=Mcp(), autostart=False)
            command(runtime, "nonblocking-mcp", 0, {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "enabled": True, "transportType": "sse", "url": "https://mcp.test/sse"}})
            tool = command(runtime, "nonblocking-catalog", 1, {"kind": "mcp.test", "serverId": "kb"})["tools"][0]
            command(runtime, "nonblocking-listener", 2, {"kind": "listener.save", "listener": {"id": "listener", "name": "Listener", "groupId": "room", "enabled": True, "pollIntervalSeconds": 2, "sameSenderMergeSeconds": 2, "humanReplyWaitSeconds": 120, "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}]}})
            command(runtime, "nonblocking-baseline", 3, {"kind": "runtime.tick", "wait": True})
            source.rows.append({"cursor": [80_001, 0, 1, 1], "messageId": "1", "serverId": "1", "sequence": 0, "sendTime": 80_001, "groupId": "room", "senderId": "alice", "contentType": "text", "text": "question?"})
            clock.value = 80_002
            command(runtime, "nonblocking-collect", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 80_004
            command(runtime, "nonblocking-classify", 3, {"kind": "runtime.tick", "wait": True})
            source.rows.append({"cursor": [80_005, 0, 2, 1], "messageId": "2", "serverId": "1", "sequence": 0, "sendTime": 80_005, "groupId": "room", "senderId": "bob", "contentType": "text", "text": "the answer"})
            clock.value = 80_006
            bob_thread = threading.Thread(target=lambda: command(runtime, "nonblocking-bob", 3, {"kind": "runtime.tick", "wait": True}))
            bob_thread.start()
            self.assertTrue(model.bob_entered.wait(2))
            source.rows.append({"cursor": [80_007, 0, 3, 1], "messageId": "3", "serverId": "1", "sequence": 0, "sendTime": 80_007, "groupId": "room", "senderId": "carol", "contentType": "text", "text": "another question?"})
            clock.value = 80_008
            carol_done = threading.Event()

            def run_carol():
                command(runtime, "nonblocking-carol", 3, {"kind": "runtime.tick", "wait": True})
                carol_done.set()

            carol_thread = threading.Thread(target=run_carol)
            carol_thread.start()
            completed_while_bob_blocked = carol_done.wait(0.5)
            model.release_bob.set()
            bob_thread.join(2)
            carol_thread.join(2)
            items = runtime.query({"kind": "work.list"})["items"]
            runtime.close()

        self.assertTrue(completed_while_bob_blocked)
        self.assertEqual(model.bob_match_calls, 1)
        self.assertIn("carol", [item["senderId"] for item in items])

    def test_mcp_endpoint_change_closes_in_flight_work_and_requires_rediscovery(self):
        class Clock:
            value = 10_000.0

            def now(self):
                return self.value

        class Source:
            rows = []

            def watermark(self, listener):
                return self.rows[-1]["cursor"] if self.rows else [9_999, 0, 0, 0]

            def read(self, listener, cursor):
                return [row for row in self.rows if tuple(row["cursor"]) > tuple(cursor)]

        class Model:
            def classify(self, **kwargs):
                return {"labels": ["question"]}

            def plan_tools(self, **kwargs):
                return [{"serverId": "kb", "toolName": "search", "arguments": {}}]

            def answer(self, **kwargs):
                return "answer"

            def review(self, **kwargs):
                return {"supported": True}

        class SlowMcp:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()

            def discover(self, server):
                return [{"name": "search", "inputSchema": {"type": "object"}}]

            def call(self, **kwargs):
                self.entered.set()
                self.release.wait(5)
                return {"content": "evidence"}

        def execute(runtime, command_id, revision, body, *, wait=None):
            if wait is not None:
                body = {**body, "wait": wait}
            return runtime.execute(
                {
                    "protocolVersion": 1,
                    "commandId": command_id,
                    "expectedRevision": revision,
                    "body": body,
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            clock, source, mcp = Clock(), Source(), SlowMcp()
            runtime = ReplyRuntime(
                Path(directory) / "runtime.sqlite3",
                clock=clock,
                message_source=source,
                model=Model(),
                mcp=mcp,
                autostart=False,
            )
            execute(runtime, "mcp", 0, {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "enabled": True, "transportType": "sse", "url": "https://mcp.test/one"}})
            tool = execute(runtime, "catalog", 1, {"kind": "mcp.test", "serverId": "kb"})["tools"][0]
            execute(runtime, "listener", 2, {"kind": "listener.save", "listener": {"id": "listener", "name": "Listener", "groupId": "room", "enabled": True, "sameSenderMergeSeconds": 2, "humanReplyWaitSeconds": 10, "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}]}})
            execute(runtime, "baseline", 3, {"kind": "runtime.tick"}, wait=True)
            source.rows.append({"cursor": [10_001, 0, 1, 1], "messageId": "1", "serverId": "1", "sequence": 0, "sendTime": 10_001, "senderId": "alice", "senderName": "Alice", "account": "alice", "contentType": "text", "text": "question?"})
            clock.value = 10_005
            execute(runtime, "collect", 3, {"kind": "runtime.tick"}, wait=True)
            clock.value = 10_007
            execute(runtime, "classify", 3, {"kind": "runtime.tick"}, wait=True)
            clock.value = 10_017
            execute(runtime, "retrieve", 3, {"kind": "runtime.tick"}, wait=False)
            self.assertTrue(mcp.entered.wait(2))

            execute(runtime, "move-endpoint", 3, {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "enabled": True, "transportType": "sse", "url": "https://mcp.test/two"}})
            during = runtime.query({"kind": "work.list"})["items"][0]
            health = runtime.query({"kind": "listener.list"})["listeners"][0]["health"]["status"]
            mcp.release.set()
            execute(runtime, "settle", 4, {"kind": "runtime.tick"}, wait=True)
            after = runtime.query({"kind": "work.list"})["items"][0]
            runtime.close()

        self.assertEqual(during["status"], "closed_configuration_changed")
        self.assertNotEqual(health, "ready")
        self.assertEqual(after["status"], "closed_configuration_changed")

    def test_supplement_waits_for_the_stale_retrieval_thread_to_exit(self):
        class Clock:
            value = 20_000.0

            def now(self):
                return self.value

        class Source:
            rows = []

            def watermark(self, listener):
                return self.rows[-1]["cursor"] if self.rows else [19_999, 0, 0, 0]

            def read(self, listener, cursor):
                return [row for row in self.rows if tuple(row["cursor"]) > tuple(cursor)]

        class Model:
            def classify(self, *, messages, groupContext, question=None):
                return {"labels": ["supplement"] if question else ["question"]}

            def plan_tools(self, **kwargs):
                return [{"serverId": "kb", "toolName": "search", "arguments": {}}]

            def answer(self, **kwargs):
                return "answer"

            def review(self, **kwargs):
                return {"supported": True}

        class SlowMcp:
            def __init__(self):
                self.entered = threading.Event()
                self.second_entered = threading.Event()
                self.release = threading.Event()
                self.lock = threading.Lock()
                self.active = 0
                self.max_active = 0
                self.calls = 0

            def discover(self, server):
                return [{"name": "search", "inputSchema": {"type": "object"}}]

            def call(self, **kwargs):
                with self.lock:
                    self.calls += 1
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    self.entered.set()
                    if self.calls >= 2:
                        self.second_entered.set()
                self.release.wait(5)
                with self.lock:
                    self.active -= 1
                return {"content": "evidence"}

        def execute(runtime, command_id, body, *, wait=True):
            return runtime.execute({"protocolVersion": 1, "commandId": command_id, "expectedRevision": 3, "body": {**body, **({"wait": wait} if body.get("kind") == "runtime.tick" else {})}})

        with tempfile.TemporaryDirectory() as directory:
            clock, source, mcp = Clock(), Source(), SlowMcp()
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", clock=clock, message_source=source, model=Model(), mcp=mcp, autostart=False)
            runtime.execute({"protocolVersion": 1, "commandId": "mcp", "expectedRevision": 0, "body": {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "enabled": True, "transportType": "sse", "url": "https://mcp.test/sse"}}})
            tool = runtime.execute({"protocolVersion": 1, "commandId": "catalog", "expectedRevision": 1, "body": {"kind": "mcp.test", "serverId": "kb"}})["tools"][0]
            runtime.execute({"protocolVersion": 1, "commandId": "listener", "expectedRevision": 2, "body": {"kind": "listener.save", "listener": {"id": "listener", "name": "Listener", "groupId": "room", "enabled": True, "sameSenderMergeSeconds": 2, "humanReplyWaitSeconds": 10, "maxConcurrency": 2, "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}]}}})
            execute(runtime, "baseline", {"kind": "runtime.tick"})
            source.rows.append({"cursor": [20_001, 0, 1, 1], "messageId": "1", "serverId": "1", "sequence": 0, "sendTime": 20_001, "senderId": "alice", "senderName": "Alice", "account": "alice", "contentType": "text", "text": "question?"})
            clock.value = 20_005
            execute(runtime, "collect", {"kind": "runtime.tick"})
            clock.value = 20_007
            execute(runtime, "classify", {"kind": "runtime.tick"})
            clock.value = 20_017
            execute(runtime, "start-old", {"kind": "runtime.tick"}, wait=False)
            self.assertTrue(mcp.entered.wait(2))

            source.rows.append({"cursor": [20_018, 0, 2, 1], "messageId": "2", "serverId": "1", "sequence": 0, "sendTime": 20_018, "senderId": "alice", "senderName": "Alice", "account": "alice", "contentType": "text", "text": "supplement"})
            clock.value = 20_022
            execute(runtime, "supplement", {"kind": "runtime.tick"}, wait=False)
            clock.value = 20_024
            execute(runtime, "reclassify", {"kind": "runtime.tick"}, wait=False)
            clock.value = 20_034
            execute(runtime, "try-new-generation", {"kind": "runtime.tick"}, wait=False)
            mcp.second_entered.wait(0.5)
            max_before_release = mcp.max_active
            mcp.release.set()
            execute(runtime, "settle-old", {"kind": "runtime.tick"})
            execute(runtime, "run-new", {"kind": "runtime.tick"})
            all_after_first_preflight = runtime.query({"kind": "work.list"})["items"]
            item = next(
                item for item in all_after_first_preflight
                if item["senderId"] == "alice"
            )
            runtime.close()

        self.assertEqual(max_before_release, 1)
        self.assertEqual(mcp.max_active, 1)
        self.assertEqual(item["status"], "pending", repr(all_after_first_preflight))

    def test_distinct_questions_from_one_sender_are_separate_and_retrieve_fifo(self):
        class Clock:
            value = 30_000.0

            def now(self):
                return self.value

        class Source:
            rows = []

            def watermark(self, listener):
                return self.rows[-1]["cursor"] if self.rows else [29_999, 0, 0, 0]

            def read(self, listener, cursor):
                return [row for row in self.rows if tuple(row["cursor"]) > tuple(cursor)]

        class Model:
            def classify(self, **kwargs):
                return {"labels": ["question"]}

            def plan_tools(self, **kwargs):
                return [{"serverId": "kb", "toolName": "search", "arguments": {}}]

            def answer(self, **kwargs):
                return "answer"

            def review(self, **kwargs):
                return {"supported": True}

        class SlowMcp:
            def __init__(self):
                self.release = threading.Event()
                self.second_entered = threading.Event()
                self.lock = threading.Lock()
                self.calls = 0
                self.active = 0
                self.max_active = 0

            def discover(self, server):
                return [{"name": "search", "inputSchema": {"type": "object"}}]

            def call(self, **kwargs):
                with self.lock:
                    self.calls += 1
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    if self.calls == 2:
                        self.second_entered.set()
                self.release.wait(5)
                with self.lock:
                    self.active -= 1
                return {"content": "evidence"}

        def tick(runtime, command_id, *, wait=True):
            return runtime.execute({"protocolVersion": 1, "commandId": command_id, "expectedRevision": 3, "body": {"kind": "runtime.tick", "wait": wait}})

        with tempfile.TemporaryDirectory() as directory:
            clock, source, mcp = Clock(), Source(), SlowMcp()
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", clock=clock, message_source=source, model=Model(), mcp=mcp, autostart=False)
            runtime.execute({"protocolVersion": 1, "commandId": "mcp", "expectedRevision": 0, "body": {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "enabled": True, "transportType": "sse", "url": "https://mcp.test/sse"}}})
            tool = runtime.execute({"protocolVersion": 1, "commandId": "catalog", "expectedRevision": 1, "body": {"kind": "mcp.test", "serverId": "kb"}})["tools"][0]
            runtime.execute({"protocolVersion": 1, "commandId": "listener", "expectedRevision": 2, "body": {"kind": "listener.save", "listener": {"id": "listener", "name": "Listener", "groupId": "room", "enabled": True, "sameSenderMergeSeconds": 20, "humanReplyWaitSeconds": 10, "maxConcurrency": 2, "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}]}}})
            tick(runtime, "baseline")
            source.rows.append({"cursor": [30_001, 0, 1, 1], "messageId": "1", "serverId": "1", "sequence": 0, "sendTime": 30_001, "senderId": "alice", "senderName": "Alice", "account": "alice", "contentType": "text", "text": "first question?"})
            clock.value = 30_005
            tick(runtime, "first")
            source.rows.append({"cursor": [30_006, 0, 2, 1], "messageId": "2", "serverId": "1", "sequence": 0, "sendTime": 30_006, "senderId": "alice", "senderName": "Alice", "account": "alice", "contentType": "text", "text": "second unrelated question?"})
            clock.value = 30_010
            tick(runtime, "second")
            separate = runtime.query({"kind": "work.list"})["items"]

            clock.value = 30_025
            tick(runtime, "classify-first")
            clock.value = 30_030
            tick(runtime, "classify-second")
            clock.value = 30_040
            tick(runtime, "start-first", wait=False)
            self.assertFalse(mcp.second_entered.wait(0.5))
            before_release = mcp.max_active
            mcp.release.set()
            tick(runtime, "settle-first")
            tick(runtime, "start-second")
            final = runtime.query({"kind": "work.list"})["items"]
            runtime.close()

        self.assertEqual(len(separate), 2)
        self.assertEqual(before_release, 1)
        self.assertEqual(mcp.max_active, 1)
        self.assertEqual([item["status"] for item in final], ["pending", "pending"])


class ReplyRuntimeClassificationRetryTests(unittest.TestCase):
    class Clock:
        value = 60_000.0

        def now(self):
            return self.value

    class Source:
        def __init__(self):
            self.rows = []

        def watermark(self, listener):
            return self.rows[-1]["cursor"] if self.rows else [59_999, 0, 0, 0]

        def read(self, listener, cursor):
            return [row for row in self.rows if tuple(row["cursor"]) > tuple(cursor)]

        def read_force(self, listener, cursor):
            return self.read(listener, cursor)

    class Mcp:
        def discover(self, server):
            return [{"name": "search", "inputSchema": {"type": "object"}}]

        def call(self, **kwargs):
            return {"content": "evidence"}

    class Webhook:
        def __init__(self):
            self.calls = []

        def send(self, **kwargs):
            self.calls.append(kwargs)
            return {"status": "sent"}

    @staticmethod
    def command(runtime, command_id, revision, body):
        return runtime.execute(
            {
                "protocolVersion": 1,
                "commandId": command_id,
                "expectedRevision": revision,
                "body": body,
            }
        )

    def configure(self, directory, *, clock, source, model, webhook=None, automatic=False):
        runtime = ReplyRuntime(
            Path(directory) / "runtime.sqlite3",
            clock=clock,
            message_source=source,
            model=model,
            mcp=self.Mcp(),
            webhook=webhook,
            autostart=False,
        )
        self.command(
            runtime,
            "retry-mcp",
            0,
            {
                "kind": "mcp.save",
                "server": {
                    "id": "kb",
                    "name": "KB",
                    "enabled": True,
                    "transportType": "sse",
                    "url": "https://mcp.test/sse",
                },
            },
        )
        tool = self.command(
            runtime, "retry-catalog", 1, {"kind": "mcp.test", "serverId": "kb"}
        )["tools"][0]
        listener = {
            "id": "listener",
            "name": "Listener",
            "groupId": "room",
            "enabled": True,
            "pollIntervalSeconds": 2,
            "sameSenderMergeSeconds": 2,
            "humanReplyWaitSeconds": 10,
            "autoSend": False,
            "toolGrants": [
                {
                    "serverId": "kb",
                    "toolName": "search",
                    "schemaSha256": tool["schemaSha256"],
                }
            ],
        }
        if automatic:
            listener["webhookUrl"] = (
                "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=classification-retry"
            )
        self.command(
            runtime,
            "retry-listener",
            2,
            {"kind": "listener.save", "listener": listener},
        )
        revision = 3
        if automatic:
            tested = self.command(
                runtime,
                "retry-webhook-test",
                revision,
                {"kind": "listener.test_webhook", "listenerId": "listener"},
            )
            revision = 4
            self.command(
                runtime,
                "retry-webhook-confirm",
                revision,
                {
                    "kind": "listener.confirm_webhook",
                    "listenerId": "listener",
                    "testCode": tested["testCode"],
                    "appearedInSelectedGroup": True,
                },
            )
            revision = 5
            listener.pop("webhookUrl")
            listener["autoSend"] = True
            self.command(
                runtime,
                "retry-enable-auto",
                revision,
                {"kind": "listener.save", "listener": listener},
            )
            revision = 6
        self.command(
            runtime,
            "retry-baseline",
            revision,
            {"kind": "runtime.tick", "wait": True},
        )
        return runtime, revision

    @staticmethod
    def add_message(source, number, sender_id, text, *, send_time, account=""):
        source.rows.append(
            {
                "cursor": [send_time, number, number, 1],
                "messageId": str(number),
                "serverId": "1",
                "sequence": number,
                "sendTime": send_time,
                "groupId": "room",
                "senderId": sender_id,
                "senderName": sender_id.title(),
                "account": account,
                "contentType": "text",
                "text": text,
            }
        )

    def collect_question(self, runtime, revision, clock, source):
        self.add_message(source, 1, "alice", "question?", send_time=60_001, account="alice")
        clock.value = 60_002
        self.command(
            runtime, "retry-collect-question", revision, {"kind": "runtime.tick", "wait": True}
        )
        clock.value = 60_004
        self.command(
            runtime, "retry-classify-question", revision, {"kind": "runtime.tick", "wait": True}
        )

    def test_failed_human_answer_match_is_deferred_without_consuming_the_inbox(self):
        class Model:
            def __init__(self):
                self.fail = True
                self.match_calls = 0

            def classify(self, **kwargs):
                return {"labels": ["question"]}

            def match_human_answers(self, *, message, groupContext, candidates):
                self.match_calls += 1
                if self.fail:
                    raise TimeoutError("classifier unavailable")
                return {
                    "matches": [
                        {"workId": candidates[0]["workId"], "labels": ["human_answer"]}
                    ]
                }

        with tempfile.TemporaryDirectory() as directory:
            clock, source, model = self.Clock(), self.Source(), Model()
            runtime, revision = self.configure(
                directory, clock=clock, source=source, model=model
            )
            self.collect_question(runtime, revision, clock, source)
            self.add_message(source, 2, "bob", "human answer", send_time=60_005)
            clock.value = 60_006
            self.command(
                runtime, "retry-human-fails", revision, {"kind": "runtime.tick", "wait": True}
            )
            with runtime.store.lock:
                deferred = runtime.store.connection.execute(
                    "SELECT * FROM reply_inbox WHERE message_id='2'"
                ).fetchone()
                work_count = runtime.store.connection.execute(
                    "SELECT count(*) FROM reply_work_items"
                ).fetchone()[0]
            before_retry = runtime.query({"kind": "work.list"})["items"]
            self.command(
                runtime, "retry-human-backoff", revision, {"kind": "runtime.tick", "wait": True}
            )
            calls_during_backoff = model.match_calls
            model.fail = False
            clock.value = float(deferred["retry_after"])
            self.command(
                runtime, "retry-human-recovers", revision, {"kind": "runtime.tick", "wait": True}
            )
            after_retry = runtime.query({"kind": "work.list"})["items"]
            with runtime.store.lock:
                recovered_inbox = runtime.store.connection.execute(
                    "SELECT assigned_work_id FROM reply_inbox WHERE message_id='2'"
                ).fetchone()
            runtime.close()

        self.assertEqual(work_count, 1)
        self.assertEqual([item["status"] for item in before_retry], ["waiting_for_human_reply"])
        self.assertIsNone(deferred["assigned_work_id"])
        self.assertEqual(deferred["classification_attempts"], 1)
        self.assertEqual(
            json.loads(deferred["classification_error_json"])["code"],
            "HUMAN_ANSWER_CLASSIFICATION_FAILED",
        )
        self.assertGreater(deferred["retry_after"], 60_006)
        self.assertEqual(calls_during_backoff, 1)
        self.assertEqual(len(after_retry), 1)
        self.assertEqual(after_retry[0]["status"], "answered_by_human")
        self.assertEqual(recovered_inbox["assigned_work_id"], after_retry[0]["id"])

    def test_failed_same_sender_classification_does_not_guess_supplement_or_create_work(self):
        class Model:
            def __init__(self):
                self.fail_supplement = True
                self.supplement_calls = 0
                self.classified_texts = []

            def classify(self, *, messages, groupContext, question=None):
                if question is None:
                    return {"labels": ["question"]}
                self.supplement_calls += 1
                self.classified_texts.append(messages[-1]["text"])
                if self.fail_supplement:
                    raise TimeoutError("classifier unavailable")
                return {"labels": ["supplement"]}

            def match_human_answers(self, **kwargs):
                return {"matches": []}

        with tempfile.TemporaryDirectory() as directory:
            clock, source, model = self.Clock(), self.Source(), Model()
            runtime, revision = self.configure(
                directory, clock=clock, source=source, model=model
            )
            self.collect_question(runtime, revision, clock, source)
            original = runtime.query({"kind": "work.list"})["items"][0]
            self.add_message(source, 2, "alice", "more detail", send_time=60_005, account="alice")
            self.add_message(source, 3, "alice", "final detail", send_time=60_005, account="alice")
            clock.value = 60_006
            self.command(
                runtime,
                "retry-supplement-fails",
                revision,
                {"kind": "runtime.tick", "wait": True},
            )
            with runtime.store.lock:
                deferred = runtime.store.connection.execute(
                    "SELECT * FROM reply_inbox WHERE message_id='2'"
                ).fetchone()
                later = runtime.store.connection.execute(
                    "SELECT * FROM reply_inbox WHERE message_id='3'"
                ).fetchone()
                stored_work = runtime.store.connection.execute(
                    "SELECT * FROM reply_work_items WHERE id=?", (original["id"],)
                ).fetchone()
                work_count = runtime.store.connection.execute(
                    "SELECT count(*) FROM reply_work_items"
                ).fetchone()[0]
            self.command(
                runtime,
                "retry-supplement-backoff",
                revision,
                {"kind": "runtime.tick", "wait": True},
            )
            calls_during_backoff = model.supplement_calls
            model.fail_supplement = False
            clock.value = float(deferred["retry_after"])
            self.command(
                runtime,
                "retry-supplement-recovers",
                revision,
                {"kind": "runtime.tick", "wait": True},
            )
            with runtime.store.lock:
                recovered = runtime.store.connection.execute(
                    "SELECT * FROM reply_work_items WHERE id=?", (original["id"],)
                ).fetchone()
                final_count = runtime.store.connection.execute(
                    "SELECT count(*) FROM reply_work_items"
                ).fetchone()[0]
                supplement_inbox = runtime.store.connection.execute(
                    "SELECT assigned_work_id FROM reply_inbox WHERE message_id='2'"
                ).fetchone()
            runtime.close()

        self.assertEqual(work_count, 1)
        self.assertEqual(stored_work["status"], "waiting_for_human_reply")
        self.assertEqual(json.loads(stored_work["messages_json"])[0]["text"], "question?")
        self.assertIsNone(deferred["assigned_work_id"])
        self.assertEqual(deferred["classification_attempts"], 1)
        self.assertIsNone(later["assigned_work_id"])
        self.assertEqual(later["classification_attempts"], 0)
        self.assertEqual(calls_during_backoff, 1)
        self.assertEqual(
            json.loads(deferred["classification_error_json"])["code"],
            "MESSAGE_CLASSIFICATION_FAILED",
        )
        self.assertEqual(final_count, 1)
        self.assertEqual(recovered["status"], "collecting")
        self.assertEqual(
            [item["text"] for item in json.loads(recovered["messages_json"])],
            ["question?", "more detail", "final detail"],
        )
        self.assertEqual(
            model.classified_texts,
            ["more detail", "more detail", "final detail"],
        )
        self.assertEqual(supplement_inbox["assigned_work_id"], original["id"])

    def test_later_human_responder_message_cannot_be_primed_past_failed_fifo_head(self):
        class Model:
            def __init__(self):
                self.match_calls = 0

            def classify(self, **kwargs):
                return {"labels": ["question"]}

            def match_human_answers(self, **kwargs):
                self.match_calls += 1
                raise TimeoutError("classifier unavailable")

        with tempfile.TemporaryDirectory() as directory:
            clock, source, model = self.Clock(), self.Source(), Model()
            runtime, revision = self.configure(
                directory, clock=clock, source=source, model=model
            )
            self.collect_question(runtime, revision, clock, source)
            self.add_message(source, 2, "bob", "possible answer", send_time=60_005)
            self.add_message(source, 3, "bob", "additional answer", send_time=60_005)
            clock.value = 60_006
            self.command(
                runtime,
                "retry-responder-head-fails",
                revision,
                {"kind": "runtime.tick", "wait": True},
            )
            self.command(
                runtime,
                "retry-responder-backoff",
                revision,
                {"kind": "runtime.tick", "wait": True},
            )
            with runtime.store.lock:
                inbox_rows = runtime.store.connection.execute(
                    "SELECT * FROM reply_inbox WHERE message_id IN ('2','3') ORDER BY message_id"
                ).fetchall()
                work_count = runtime.store.connection.execute(
                    "SELECT count(*) FROM reply_work_items"
                ).fetchone()[0]
            runtime.close()

        self.assertEqual(model.match_calls, 1)
        self.assertEqual(work_count, 1)
        self.assertEqual([row["assigned_work_id"] for row in inbox_rows], [None, None])
        self.assertEqual([row["classification_attempts"] for row in inbox_rows], [1, 0])
        self.assertGreater(inbox_rows[0]["retry_after"], clock.value)

    def test_automatic_preflight_retries_deferred_classification_despite_backoff(self):
        class Model:
            def __init__(self, clock, source):
                self.clock = clock
                self.source = source
                self.runtime = None
                self.match_calls = 0
                self.first_retry_after = None

            def classify(self, **kwargs):
                return {"labels": ["question"]}

            def match_human_answers(self, **kwargs):
                self.match_calls += 1
                raise TimeoutError("classifier unavailable")

            def plan_tools(self, **kwargs):
                return [{"serverId": "kb", "toolName": "search", "arguments": {}}]

            def answer(self, **kwargs):
                return "AI answer"

            def review(self, **kwargs):
                self.source.rows.append(
                    {
                        "cursor": [60_015, 2, 2, 1],
                        "messageId": "2",
                        "serverId": "1",
                        "sequence": 2,
                        "sendTime": 60_015,
                        "groupId": "room",
                        "senderId": "bob",
                        "senderName": "Bob",
                        "contentType": "text",
                        "text": "human answer",
                    }
                )
                self.runtime._poll_messages(listener_id="listener", force=True)
                self.runtime._assign_inbox(listener_id="listener")
                with self.runtime.store.lock:
                    row = self.runtime.store.connection.execute(
                        "SELECT retry_after FROM reply_inbox WHERE message_id='2'"
                    ).fetchone()
                self.first_retry_after = float(row["retry_after"])
                return {"supported": True}

        with tempfile.TemporaryDirectory() as directory:
            clock, source, webhook = self.Clock(), self.Source(), self.Webhook()
            model = Model(clock, source)
            runtime, revision = self.configure(
                directory,
                clock=clock,
                source=source,
                model=model,
                webhook=webhook,
                automatic=True,
            )
            model.runtime = runtime
            self.collect_question(runtime, revision, clock, source)
            clock.value = 60_014
            self.command(
                runtime,
                "retry-start-automatic",
                revision,
                {"kind": "runtime.tick", "wait": True},
            )
            work = next(
                item
                for item in runtime.query({"kind": "work.list"})["items"]
                if item["senderId"] == "alice"
            )
            with runtime.store.lock:
                deferred = runtime.store.connection.execute(
                    "SELECT * FROM reply_inbox WHERE message_id='2'"
                ).fetchone()
                work_count = runtime.store.connection.execute(
                    "SELECT count(*) FROM reply_work_items"
                ).fetchone()[0]
            runtime.close()

        self.assertGreater(model.first_retry_after, clock.value)
        self.assertEqual(model.match_calls, 2)
        self.assertEqual(deferred["classification_attempts"], 2)
        self.assertGreater(deferred["retry_after"], model.first_retry_after)
        self.assertIsNone(deferred["assigned_work_id"])
        self.assertEqual(work_count, 1)
        self.assertEqual(work["status"], "pending")
        self.assertEqual(work["pendingReason"], "automatic_preflight_failed")
        self.assertEqual(len(webhook.calls), 1)  # visible-confirmation test only


class ReplyRuntimeDeliverySafetyTests(unittest.TestCase):
    def test_automatic_preflight_waits_for_an_already_claimed_human_answer(self):
        class Clock:
            value = 41_000.0

            def now(self):
                return self.value

        class Source:
            def __init__(self):
                self.rows = []

            def watermark(self, listener):
                return self.rows[-1]["cursor"] if self.rows else [40_999, 0, 0, 0]

            def read(self, listener, cursor):
                return [row for row in self.rows if tuple(row["cursor"]) > tuple(cursor)]

        class Model:
            def __init__(self, source):
                self.source = source
                self.runtime = None
                self.match_entered = threading.Event()
                self.release_match = threading.Event()
                self.started_claim = False

            def classify(self, **kwargs):
                return {"labels": ["question"]}

            def match_human_answers(self, *, message, groupContext, candidates):
                if message.get("senderId") != "bob":
                    return {"matches": []}
                self.match_entered.set()
                self.release_match.wait(5)
                target = next(candidate for candidate in candidates if candidate["question"] == "question?")
                return {"matches": [{"workId": target["workId"], "labels": ["human_answer"]}]}

            def plan_tools(self, **kwargs):
                return [{"serverId": "kb", "toolName": "search", "arguments": {}}]

            def answer(self, **kwargs):
                return "AI answer"

            def review(self, **kwargs):
                if not self.started_claim:
                    self.started_claim = True
                    self.runtime.clock.value = 41_016
                    self.source.rows.append({"cursor": [41_015, 0, 2, 1], "messageId": "2", "serverId": "1", "sequence": 0, "sendTime": 41_015, "groupId": "room", "senderId": "bob", "senderName": "Bob", "contentType": "text", "text": "human answer"})
                    threading.Thread(
                        target=lambda: self.runtime.execute({"protocolVersion": 1, "commandId": "claim-human-answer", "expectedRevision": 6, "body": {"kind": "runtime.tick", "wait": False}}),
                        daemon=True,
                    ).start()
                    if not self.match_entered.wait(2):
                        raise AssertionError("human-answer classification did not start")
                return {"supported": True}

        class Mcp:
            def discover(self, server):
                return [{"name": "search", "inputSchema": {"type": "object"}}]

            def call(self, **kwargs):
                return {"content": "evidence"}

        class Webhook:
            def __init__(self):
                self.calls = []

            def send(self, **kwargs):
                self.calls.append(kwargs)
                return {"status": "sent"}

        def command(runtime, command_id, revision, body):
            return runtime.execute({"protocolVersion": 1, "commandId": command_id, "expectedRevision": revision, "body": body})

        with tempfile.TemporaryDirectory() as directory:
            clock, source, webhook = Clock(), Source(), Webhook()
            model = Model(source)
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", clock=clock, message_source=source, model=model, mcp=Mcp(), webhook=webhook, autostart=False)
            model.runtime = runtime
            command(runtime, "claim-mcp", 0, {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "enabled": True, "transportType": "sse", "url": "https://mcp.test/sse"}})
            tool = command(runtime, "claim-catalog", 1, {"kind": "mcp.test", "serverId": "kb"})["tools"][0]
            listener = {"id": "listener", "name": "Listener", "groupId": "room", "enabled": True, "pollIntervalSeconds": 2, "sameSenderMergeSeconds": 2, "humanReplyWaitSeconds": 10, "autoSend": False, "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}], "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=claim-preflight"}
            command(runtime, "claim-listener", 2, {"kind": "listener.save", "listener": listener})
            tested = command(runtime, "claim-test", 3, {"kind": "listener.test_webhook", "listenerId": "listener"})
            command(runtime, "claim-confirm", 4, {"kind": "listener.confirm_webhook", "listenerId": "listener", "testCode": tested["testCode"], "appearedInSelectedGroup": True})
            listener.pop("webhookUrl")
            listener["autoSend"] = True
            command(runtime, "claim-enable-auto", 5, {"kind": "listener.save", "listener": listener})
            command(runtime, "claim-baseline", 6, {"kind": "runtime.tick", "wait": True})
            source.rows.append({"cursor": [41_001, 0, 1, 1], "messageId": "1", "serverId": "1", "sequence": 0, "sendTime": 41_001, "groupId": "room", "senderId": "alice", "senderName": "Alice", "account": "alice", "contentType": "text", "text": "question?"})
            clock.value = 41_002
            command(runtime, "claim-collect", 6, {"kind": "runtime.tick", "wait": True})
            clock.value = 41_004
            command(runtime, "claim-classify", 6, {"kind": "runtime.tick", "wait": True})
            clock.value = 41_014
            command(runtime, "claim-start-retrieval", 6, {"kind": "runtime.tick", "wait": False})
            match_started = model.match_entered.wait(2)
            ready_while_claimed = False
            for _ in range(100 if match_started else 1):
                alice = next(item for item in runtime.query({"kind": "work.list"})["items"] if item["senderId"] == "alice")
                if alice["status"] == "ready_to_send":
                    ready_while_claimed = True
                    break
                threading.Event().wait(0.01)
            calls_before_release = len(webhook.calls)
            model.release_match.set()
            command(runtime, "claim-settle", 6, {"kind": "runtime.tick", "wait": True})
            alice = next(item for item in runtime.query({"kind": "work.list"})["items"] if item["senderId"] == "alice")
            runtime.close()

        self.assertTrue(match_started, repr(alice))
        self.assertTrue(ready_while_claimed, repr(alice))
        self.assertEqual(calls_before_release, 1)
        self.assertEqual(len(webhook.calls), 1)
        self.assertEqual(alice["status"], "pending")
        self.assertIn(alice["pendingReason"], {"human_answered_after_review", "human_answered_during_retrieval"})

    def test_webhook_test_cannot_confirm_a_configuration_changed_during_delivery(self):
        class BlockingWebhook:
            def __init__(self):
                self.calls = []
                self.entered = threading.Event()
                self.release = threading.Event()

            def send(self, **kwargs):
                self.calls.append(kwargs)
                self.entered.set()
                self.release.wait(5)
                return {"status": "sent"}

        class Mcp:
            def discover(self, server):
                return [{"name": "search", "inputSchema": {"type": "object"}}]

        def command(runtime, command_id, revision, body):
            return runtime.execute({"protocolVersion": 1, "commandId": command_id, "expectedRevision": revision, "body": body})

        with tempfile.TemporaryDirectory() as directory:
            webhook = BlockingWebhook()
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", mcp=Mcp(), webhook=webhook, autostart=False)
            command(runtime, "race-mcp", 0, {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "enabled": True, "transportType": "sse", "url": "https://mcp.test/sse"}})
            tool = command(runtime, "race-catalog", 1, {"kind": "mcp.test", "serverId": "kb"})["tools"][0]
            listener = {"id": "listener", "name": "Listener", "groupId": "old-room", "enabled": True, "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}], "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=old-key"}
            command(runtime, "race-listener", 2, {"kind": "listener.save", "listener": listener})
            test_errors = []
            test_command = {"protocolVersion": 1, "commandId": "racing-test", "expectedRevision": 3, "body": {"kind": "listener.test_webhook", "listenerId": "listener"}}

            def run_test():
                try:
                    runtime.execute(test_command)
                except RuntimeProtocolError as exc:
                    test_errors.append(exc.code)

            thread = threading.Thread(target=run_test)
            thread.start()
            self.assertTrue(webhook.entered.wait(2))
            listener.pop("webhookUrl")
            listener["groupId"] = "new-room"
            changed = command(
                runtime,
                "replace-listener-during-test",
                3,
                {
                    "kind": "listener.save",
                    "listener": listener,
                    "secretPatch": {"webhookUrl": {"op": "replace", "value": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=new-key"}},
                },
            )
            webhook.release.set()
            thread.join(2)
            old_test_code = webhook.calls[0]["text"][webhook.calls[0]["text"].index("WIR-"):].splitlines()[0]
            duplicate_error = None
            try:
                runtime.execute(test_command)
            except RuntimeProtocolError as exc:
                duplicate_error = exc.code
            confirm_error = None
            try:
                command(runtime, "confirm-stale-test", changed["revision"], {"kind": "listener.confirm_webhook", "listenerId": "listener", "testCode": old_test_code, "appearedInSelectedGroup": True})
            except RuntimeProtocolError as exc:
                confirm_error = exc.code
            listed = runtime.query({"kind": "listener.list"})["listeners"][0]
            runtime.close()

        self.assertEqual(test_errors, ["WEBHOOK_CONFIGURATION_CHANGED"])
        self.assertEqual(duplicate_error, "WEBHOOK_CONFIGURATION_CHANGED")
        self.assertEqual(confirm_error, "WEBHOOK_TEST_CODE_MISMATCH")
        self.assertEqual(len(webhook.calls), 1)
        self.assertIsNone(listed["webhook"]["testedAt"])

    def test_automatic_preflight_catches_a_human_answer_after_review(self):
        class Clock:
            value = 40_000.0

            def now(self):
                return self.value

        class Source:
            rows = []
            fail = False

            def watermark(self, listener):
                return self.rows[-1]["cursor"] if self.rows else [39_999, 0, 0, 0]

            def read(self, listener, cursor):
                if self.fail:
                    raise OSError("message database temporarily unavailable")
                return [row for row in self.rows if tuple(row["cursor"]) > tuple(cursor)]

        class Model:
            def __init__(self, source):
                self.source = source
                self.added_answer = False
                self.mode = "human"

            def classify(self, *, messages, groupContext, question=None):
                if question and messages[-1].get("senderId") == "bob":
                    return {"labels": ["human_answer"]}
                if question is None and str(messages[-1].get("senderId") or "").startswith("chatter-"):
                    return {"labels": ["chat"]}
                return {"labels": ["question"]}

            def match_human_answers(self, *, message, groupContext, candidates):
                if message.get("senderId") != "bob":
                    return {"matches": []}
                target = next(
                    (candidate for candidate in candidates if candidate.get("question") == "question?"),
                    None,
                )
                return {
                    "matches": [] if target is None else [
                        {"workId": target["workId"], "labels": ["human_answer"]}
                    ]
                }

            def plan_tools(self, **kwargs):
                return [{"serverId": "kb", "toolName": "search", "arguments": {}}]

            def answer(self, **kwargs):
                return "AI answer"

            def review(self, **kwargs):
                if self.mode == "fail":
                    self.source.fail = True
                elif not self.added_answer:
                    self.added_answer = True
                    for index in range(2, 22):
                        self.source.rows.append({"cursor": [40_018, index, index, 1], "messageId": str(index), "serverId": "1", "sequence": index, "sendTime": 40_018, "senderId": f"chatter-{index}", "senderName": f"Chatter {index}", "contentType": "text", "text": "following"})
                    self.source.rows.append({"cursor": [40_018, 22, 22, 1], "messageId": "22", "serverId": "1", "sequence": 22, "sendTime": 40_018, "senderId": "bob", "senderName": "Bob", "contentType": "text", "text": "Human answered it"})
                return {"supported": True}

        class Mcp:
            def discover(self, server):
                return [{"name": "search", "inputSchema": {"type": "object"}}]

            def call(self, **kwargs):
                return {"content": "evidence"}

        class Webhook:
            def __init__(self):
                self.calls = []

            def send(self, **kwargs):
                self.calls.append(kwargs)
                return {"status": "sent"}

        def command(runtime, command_id, revision, body):
            return runtime.execute({"protocolVersion": 1, "commandId": command_id, "expectedRevision": revision, "body": body})

        with tempfile.TemporaryDirectory() as directory:
            clock, source, webhook = Clock(), Source(), Webhook()
            model = Model(source)
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", clock=clock, message_source=source, model=model, mcp=Mcp(), webhook=webhook, autostart=False)
            command(runtime, "mcp", 0, {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "enabled": True, "transportType": "sse", "url": "https://mcp.test/sse"}})
            tool = command(runtime, "catalog", 1, {"kind": "mcp.test", "serverId": "kb"})["tools"][0]
            listener = {"id": "listener", "name": "Listener", "groupId": "room", "enabled": True, "sameSenderMergeSeconds": 2, "humanReplyWaitSeconds": 10, "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}], "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=preflight-secret-key", "autoSend": False}
            command(runtime, "listener", 2, {"kind": "listener.save", "listener": listener})
            tested = command(runtime, "test-webhook", 3, {"kind": "listener.test_webhook", "listenerId": "listener"})
            command(runtime, "confirm", 4, {"kind": "listener.confirm_webhook", "listenerId": "listener", "testCode": tested["testCode"], "appearedInSelectedGroup": True})
            listener.pop("webhookUrl")
            listener["autoSend"] = True
            command(runtime, "enable-auto", 5, {"kind": "listener.save", "listener": listener})
            command(runtime, "baseline", 6, {"kind": "runtime.tick", "wait": True})
            source.rows.append({"cursor": [40_001, 0, 1, 1], "messageId": "1", "serverId": "1", "sequence": 0, "sendTime": 40_001, "senderId": "alice", "senderName": "Alice", "account": "alice", "contentType": "text", "text": "question?"})
            clock.value = 40_005
            command(runtime, "collect", 6, {"kind": "runtime.tick", "wait": True})
            clock.value = 40_007
            command(runtime, "classify", 6, {"kind": "runtime.tick", "wait": True})
            clock.value = 40_017
            command(runtime, "retrieve", 6, {"kind": "runtime.tick", "wait": True})
            all_after_first_preflight = runtime.query({"kind": "work.list"})["items"]
            item = next(
                candidate for candidate in all_after_first_preflight
                if candidate["senderId"] == "alice"
            )

            source.fail = False
            model.mode = "fail"
            source.rows.append({"cursor": [40_019, 0, 3, 1], "messageId": "3", "serverId": "1", "sequence": 0, "sendTime": 40_019, "senderId": "carol", "senderName": "Carol", "account": "carol", "contentType": "text", "text": "another question?"})
            clock.value = 40_022
            command(runtime, "collect-preflight-failure", 6, {"kind": "runtime.tick", "wait": True})
            clock.value = 40_024
            command(runtime, "classify-preflight-failure", 6, {"kind": "runtime.tick", "wait": True})
            clock.value = 40_034
            command(runtime, "retrieve-preflight-failure", 6, {"kind": "runtime.tick", "wait": True})
            failed_preflight = next(
                item for item in runtime.query({"kind": "work.list"})["items"]
                if item["senderId"] == "carol"
            )
            runtime.close()

        self.assertEqual(item["status"], "pending", repr(all_after_first_preflight))
        self.assertEqual(item["pendingReason"], "human_answered_after_review")
        self.assertEqual(failed_preflight["status"], "pending")
        self.assertEqual(failed_preflight["pendingReason"], "automatic_preflight_failed")
        self.assertEqual(len(webhook.calls), 1)  # visible confirmation test only

    def test_concurrent_reply_attempts_reserve_the_webhook_rate_slot_before_http(self):
        class Clock:
            value = 50_000.0

            def now(self):
                return self.value

        class Source:
            rows = []

            def watermark(self, listener):
                return self.rows[-1]["cursor"] if self.rows else [49_999, 0, 0, 0]

            def read(self, listener, cursor):
                return [row for row in self.rows if tuple(row["cursor"]) > tuple(cursor)]

        class Model:
            def classify(self, **kwargs):
                return {"labels": ["question"]}

            def plan_tools(self, **kwargs):
                return [{"serverId": "kb", "toolName": "search", "arguments": {}}]

            def answer(self, **kwargs):
                return "answer"

            def review(self, **kwargs):
                return {"supported": True}

        class Mcp:
            def discover(self, server):
                return [{"name": "search", "inputSchema": {"type": "object"}}]

            def call(self, **kwargs):
                return {"content": "evidence"}

        class BlockingWebhook:
            def __init__(self):
                self.reply_calls = 0
                self.reply_entered = threading.Event()
                self.release = threading.Event()
                self.lock = threading.Lock()

            def send(self, **kwargs):
                if "WIR-" in kwargs.get("text", ""):
                    return {"status": "sent"}
                with self.lock:
                    self.reply_calls += 1
                    self.reply_entered.set()
                self.release.wait(5)
                return {"status": "sent"}

        def command(runtime, command_id, revision, body):
            return runtime.execute({"protocolVersion": 1, "commandId": command_id, "expectedRevision": revision, "body": body})

        with tempfile.TemporaryDirectory() as directory:
            clock, source, webhook = Clock(), Source(), BlockingWebhook()
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", clock=clock, message_source=source, model=Model(), mcp=Mcp(), webhook=webhook, autostart=False)
            command(runtime, "mcp", 0, {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "enabled": True, "transportType": "sse", "url": "https://mcp.test/sse"}})
            tool = command(runtime, "catalog", 1, {"kind": "mcp.test", "serverId": "kb"})["tools"][0]
            command(runtime, "listener", 2, {"kind": "listener.save", "listener": {"id": "listener", "name": "Listener", "groupId": "room", "enabled": True, "sameSenderMergeSeconds": 2, "humanReplyWaitSeconds": 10, "maxConcurrency": 2, "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}], "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=rate-reserve"}})
            command(runtime, "baseline", 3, {"kind": "runtime.tick", "wait": True})
            source.rows.extend([
                {"cursor": [50_001, 0, 1, 1], "messageId": "1", "serverId": "1", "sequence": 0, "sendTime": 50_001, "senderId": "alice", "senderName": "Alice", "account": "alice", "contentType": "text", "text": "question one?"},
                {"cursor": [50_002, 0, 2, 1], "messageId": "2", "serverId": "1", "sequence": 0, "sendTime": 50_002, "senderId": "bob", "senderName": "Bob", "account": "bob", "contentType": "text", "text": "question two?"},
            ])
            clock.value = 50_005
            command(runtime, "collect", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 50_007
            command(runtime, "classify", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 50_017
            command(runtime, "retrieve", 3, {"kind": "runtime.tick", "wait": True})
            work_ids = [item["id"] for item in runtime.query({"kind": "work.list"})["items"]]
            revision = 3
            for index in range(19):
                result = command(runtime, f"test-{index}", revision, {"kind": "listener.test_webhook", "listenerId": "listener"})
                revision = result["revision"]

            results = []

            def send(work_id, command_id):
                try:
                    results.append(command(runtime, command_id, revision, {"kind": "work.send", "workId": work_id}))
                except RuntimeProtocolError as exc:
                    results.append(exc.code)

            first = threading.Thread(target=send, args=(work_ids[0], "send-1"))
            first.start()
            self.assertTrue(webhook.reply_entered.wait(2))
            second = threading.Thread(target=send, args=(work_ids[1], "send-2"))
            second.start()
            second.join(2)
            calls_before_release = webhook.reply_calls
            webhook.release.set()
            first.join(2)
            second.join(2)
            runtime.close()

        self.assertEqual(calls_before_release, 1)
        self.assertEqual(webhook.reply_calls, 1)
        self.assertIn("WEBHOOK_RATE_LIMITED", results)


class ReplyRuntimeRestartSafetyTests(unittest.TestCase):
    def test_lease_takeover_drops_stale_claim_before_old_classification_can_insert_work(self):
        class Clock:
            value = 90_000.0

            def now(self):
                return self.value

        class Source:
            def __init__(self):
                self.rows = []

            def watermark(self, listener):
                return self.rows[-1]["cursor"] if self.rows else [89_999, 0, 0, 0]

            def read(self, listener, cursor):
                return [row for row in self.rows if tuple(row["cursor"]) > tuple(cursor)]

        class Model:
            def __init__(self):
                self.active_entered = threading.Event()
                self.release = threading.Event()

            def classify(self, *, messages, groupContext, question=None):
                if question is not None:
                    self.active_entered.set()
                    self.release.wait(5)
                    return {"labels": ["chat"]}
                return {"labels": ["question"]}

            def match_human_answers(self, **kwargs):
                return {"matches": []}

        class Mcp:
            def discover(self, server):
                return [{"name": "search", "inputSchema": {"type": "object"}}]

        def command(runtime, command_id, revision, body):
            return runtime.execute({"protocolVersion": 1, "commandId": command_id, "expectedRevision": revision, "body": body})

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runtime.sqlite3"
            clock, source, model, mcp = Clock(), Source(), Model(), Mcp()
            first = ReplyRuntime(database, clock=clock, message_source=source, model=model, mcp=mcp, autostart=False)
            command(first, "claim-lease-mcp", 0, {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "enabled": True, "transportType": "sse", "url": "https://mcp.test/sse"}})
            tool = command(first, "claim-lease-catalog", 1, {"kind": "mcp.test", "serverId": "kb"})["tools"][0]
            command(first, "claim-lease-listener", 2, {"kind": "listener.save", "listener": {"id": "listener", "name": "Listener", "groupId": "room", "enabled": True, "pollIntervalSeconds": 2, "sameSenderMergeSeconds": 2, "humanReplyWaitSeconds": 120, "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}]}})
            first.start()
            command(first, "claim-lease-baseline", 3, {"kind": "runtime.tick", "wait": True})
            source.rows.append({"cursor": [90_001, 0, 1, 1], "messageId": "1", "serverId": "1", "sequence": 0, "sendTime": 90_001, "groupId": "room", "senderId": "alice", "contentType": "text", "text": "question?"})
            clock.value = 90_002
            command(first, "claim-lease-collect", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 90_004
            command(first, "claim-lease-classify", 3, {"kind": "runtime.tick", "wait": True})
            source.rows.append({"cursor": [90_005, 0, 2, 1], "messageId": "2", "serverId": "1", "sequence": 0, "sendTime": 90_005, "groupId": "room", "senderId": "alice", "contentType": "text", "text": "unrelated follow-up"})
            clock.value = 90_006
            old_errors = []

            def run_old_assignment():
                try:
                    command(first, "claim-lease-old-assignment", 3, {"kind": "runtime.tick", "wait": True})
                except RuntimeProtocolError as exc:
                    old_errors.append(exc.code)

            old_thread = threading.Thread(target=run_old_assignment)
            old_thread.start()
            self.assertTrue(model.active_entered.wait(2))
            clock.value = 90_022
            second = ReplyRuntime(database, clock=clock, message_source=source, model=model, mcp=mcp, autostart=False)
            second.start()
            model.release.set()
            old_thread.join(2)
            items = second.query({"kind": "work.list"})["items"]
            old_running = first.query({"kind": "runtime.snapshot"})["running"]
            first.close()
            current_revision = second.query({"kind": "runtime.snapshot"})["revision"]
            command(second, "claim-lease-new-owner", current_revision, {"kind": "runtime.tick", "wait": True})
            second.close()

        self.assertIn("RUNTIME_LEASE_LOST", old_errors)
        self.assertFalse(old_running)
        self.assertEqual([item["status"] for item in items], ["closed_runtime_restarted"])

    def test_expired_lease_takeover_blocks_old_webhook_test_completion(self):
        class Clock:
            value = 70_000.0

            def now(self):
                return self.value

        class Mcp:
            def discover(self, server):
                return [{"name": "search", "inputSchema": {"type": "object"}}]

        class BlockingWebhook:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()

            def send(self, **kwargs):
                self.entered.set()
                self.release.wait(5)
                return {"status": "sent"}

        def command(runtime, command_id, revision, body):
            return runtime.execute({"protocolVersion": 1, "commandId": command_id, "expectedRevision": revision, "body": body})

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runtime.sqlite3"
            clock, mcp, webhook = Clock(), Mcp(), BlockingWebhook()
            first = ReplyRuntime(database, clock=clock, mcp=mcp, webhook=webhook, autostart=False)
            command(first, "lease-mcp", 0, {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "enabled": True, "transportType": "sse", "url": "https://mcp.test/sse"}})
            tool = command(first, "lease-catalog", 1, {"kind": "mcp.test", "serverId": "kb"})["tools"][0]
            command(first, "lease-listener", 2, {"kind": "listener.save", "listener": {"id": "listener", "name": "Listener", "groupId": "room", "enabled": True, "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}], "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=lease-key"}})
            first.start()
            old_errors = []
            webhook_command = {"protocolVersion": 1, "commandId": "lease-webhook-test", "expectedRevision": 3, "body": {"kind": "listener.test_webhook", "listenerId": "listener"}}

            def run_old_test():
                try:
                    first.execute(webhook_command)
                except RuntimeProtocolError as exc:
                    old_errors.append(exc.code)

            old_thread = threading.Thread(target=run_old_test)
            old_thread.start()
            self.assertTrue(webhook.entered.wait(2))
            clock.value = 70_016
            second = ReplyRuntime(database, clock=clock, mcp=mcp, webhook=webhook, autostart=False)
            second.start()
            webhook.release.set()
            old_thread.join(2)
            tested_at = second.query({"kind": "listener.list"})["listeners"][0]["webhook"]["testedAt"]
            old_running = first.query({"kind": "runtime.snapshot"})["running"]
            duplicate_error = None
            try:
                second.execute(webhook_command)
            except RuntimeProtocolError as exc:
                duplicate_error = exc.code
            first.close()
            current_revision = second.query({"kind": "runtime.snapshot"})["revision"]
            takeover_tick = command(second, "new-owner-tick", current_revision, {"kind": "runtime.tick", "wait": True})
            second.close()

        self.assertIn("RUNTIME_LEASE_LOST", old_errors)
        self.assertEqual(duplicate_error, "WEBHOOK_DELIVERY_UNKNOWN")
        self.assertIsNone(tested_at)
        self.assertFalse(old_running)
        self.assertEqual(takeover_tick["revision"], 3)

    def test_restart_closes_interrupted_work_and_rebaselines_past_offline_messages(self):
        class Clock:
            value = 60_000.0

            def now(self):
                return self.value

        class Source:
            def __init__(self):
                self.rows = []
                self.watermarked = threading.Event()

            def watermark(self, listener):
                self.watermarked.set()
                return self.rows[-1]["cursor"] if self.rows else [59_999, 0, 0, 0]

            def read(self, listener, cursor):
                return [row for row in self.rows if tuple(row["cursor"]) > tuple(cursor)]

        class Model:
            def classify(self, **kwargs):
                return {"labels": ["question"]}

        class Mcp:
            def discover(self, server):
                return [{"name": "search", "inputSchema": {"type": "object"}}]

        def command(runtime, command_id, revision, body):
            return runtime.execute({"protocolVersion": 1, "commandId": command_id, "expectedRevision": revision, "body": body})

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runtime.sqlite3"
            clock, source, mcp = Clock(), Source(), Mcp()
            first = ReplyRuntime(database, clock=clock, message_source=source, model=Model(), mcp=mcp, autostart=False)
            command(first, "mcp", 0, {"kind": "mcp.save", "server": {"id": "kb", "name": "KB", "enabled": True, "transportType": "sse", "url": "https://mcp.test/sse"}})
            tool = command(first, "catalog", 1, {"kind": "mcp.test", "serverId": "kb"})["tools"][0]
            command(first, "listener", 2, {"kind": "listener.save", "listener": {"id": "listener", "name": "Listener", "groupId": "room", "enabled": True, "toolGrants": [{"serverId": "kb", "toolName": "search", "schemaSha256": tool["schemaSha256"]}]}})
            command(first, "baseline", 3, {"kind": "runtime.tick", "wait": True})
            source.rows.append({"cursor": [60_001, 0, 1, 1], "messageId": "1", "serverId": "1", "sequence": 0, "sendTime": 60_001, "senderId": "alice", "contentType": "text", "text": "question before shutdown?"})
            clock.value = 60_005
            command(first, "collect", 3, {"kind": "runtime.tick", "wait": True})
            self.assertEqual(first.query({"kind": "work.list"})["items"][0]["status"], "collecting")
            first.close()

            source.rows.append({"cursor": [60_006, 0, 2, 1], "messageId": "2", "serverId": "1", "sequence": 0, "sendTime": 60_006, "senderId": "bob", "contentType": "text", "text": "offline question?"})
            source.watermarked.clear()
            clock.value = 60_100
            second = ReplyRuntime(database, clock=clock, message_source=source, model=Model(), mcp=mcp, autostart=False)
            second.start()
            self.assertTrue(source.watermarked.wait(2))
            items = second.query({"kind": "work.list"})["items"]
            second.close()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "closed_runtime_restarted")


if __name__ == "__main__":
    unittest.main()
