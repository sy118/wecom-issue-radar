from __future__ import annotations

import unittest

from langchain_core.messages import AIMessage, ToolMessage

from worker.reply_runtime.agent import LangGraphMcpAgent
from worker.reply_runtime.errors import RuntimeProtocolError


class _ScriptedToolModel:
    def __init__(self) -> None:
        self.invocations = 0
        self.bound_tools = []

    def bind_tools(self, tools):
        self.bound_tools = list(tools)
        return self

    def invoke(self, _messages, **_kwargs):
        self.invocations += 1
        tool_name = self.bound_tools[0].name
        if self.invocations == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": tool_name,
                        "args": {"query": "订单 42"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": tool_name,
                    "args": {"query": "order_id:42 detail"},
                    "id": "call-2",
                    "type": "tool_call",
                }
            ],
        )


class _SequenceToolModel:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.bound_tools = []
        self.messages = []

    def bind_tools(self, tools):
        self.bound_tools = list(tools)
        return self

    def invoke(self, messages, **_kwargs):
        self.messages.append(list(messages))
        if not self.responses:
            return AIMessage(content="", tool_calls=[])
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, AIMessage):
            return response
        calls = []
        for index, item in enumerate(response or []):
            tool_index, arguments = item[:2]
            call_id = item[2] if len(item) > 2 else f"call-{len(self.messages)}-{index}"
            calls.append(
                {
                    "name": self.bound_tools[tool_index].name,
                    "args": arguments,
                    "id": call_id,
                    "type": "tool_call",
                }
            )
        return AIMessage(content="", tool_calls=calls)


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class LangGraphMcpAgentTests(unittest.TestCase):
    TOOLS = [
        {
            "serverId": "kb",
            "toolName": "search",
            "description": "Search the knowledge base",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        }
    ]

    SECOND_TOOL = {
        "serverId": "orders",
        "toolName": "detail",
        "description": "Read order details",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    }

    def retrieve(self, model, invoke_tool, **overrides):
        values = {
            "question": "订单 42 为什么卡住？",
            "context": [],
            "tools": self.TOOLS,
            "system_prompt": "只使用 MCP 证据。",
            "image_content": [],
            "invoke_tool": invoke_tool,
            "has_evidence": lambda value: bool(value.get("rows")),
            "max_rounds": 6,
            "max_tool_calls": 200,
            "timeout_seconds": 30,
        }
        values.update(overrides)
        clock = values.pop("clock", None)
        return LangGraphMcpAgent(lambda _timeout: model, clock=clock).retrieve(**values)

    def test_second_tool_query_can_use_the_first_result(self):
        model = _ScriptedToolModel()
        calls = []

        def invoke_tool(server_id, tool_name, arguments, _remaining_seconds):
            calls.append((server_id, tool_name, arguments))
            if len(calls) == 1:
                return {"rows": [{"order_id": 42}]}
            return {"rows": [{"order_id": 42, "status": "blocked"}]}

        result = LangGraphMcpAgent(lambda _timeout: model).retrieve(
            question="订单 42 为什么卡住？",
            context=[],
            tools=self.TOOLS,
            system_prompt="只使用 MCP 证据。",
            image_content=[],
            invoke_tool=invoke_tool,
            has_evidence=lambda value: bool(value.get("rows")),
            max_rounds=2,
            max_tool_calls=12,
            timeout_seconds=30,
        )

        self.assertEqual(
            calls,
            [
                ("kb", "search", {"query": "订单 42"}),
                ("kb", "search", {"query": "order_id:42 detail"}),
            ],
        )
        self.assertEqual(result.rounds, 2)
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual(result.stop_reason, "max_rounds")
        self.assertEqual([item["result"] for item in result.evidence], [
            {"rows": [{"order_id": 42}]},
            {"rows": [{"order_id": 42, "status": "blocked"}]},
        ])

    def test_empty_result_can_be_rewritten_before_collecting_evidence(self):
        model = _SequenceToolModel(
            [
                [(0, {"query": "unknown title"})],
                [(0, {"query": "order_id:42"})],
                [],
            ]
        )
        calls = []

        def invoke_tool(server_id, tool_name, arguments, _remaining):
            calls.append((server_id, tool_name, arguments))
            return {"rows": [] if len(calls) == 1 else [{"order_id": 42}]}

        result = self.retrieve(model, invoke_tool, max_rounds=3)

        self.assertEqual(len(calls), 2)
        self.assertEqual(result.rounds, 3)
        self.assertEqual(result.stop_reason, "model_stopped")
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(result.evidence[0]["arguments"], {"query": "order_id:42"})

    def test_empty_result_early_stop_is_reprompted_to_try_another_tool(self):
        model = _SequenceToolModel(
            [
                [(0, {"query": "财务支出单 保存接口"})],
                [],
                [(1, {"id": 42})],
                [],
            ]
        )
        calls = []

        def invoke_tool(server_id, tool_name, arguments, _remaining):
            calls.append((server_id, tool_name, arguments))
            if tool_name == "search":
                return {"rows": []}
            return {"rows": [{"path": "/api/caiwuapply/save"}]}

        result = self.retrieve(
            model,
            invoke_tool,
            tools=[*self.TOOLS, self.SECOND_TOOL],
            max_rounds=6,
        )

        self.assertEqual([item[1] for item in calls], ["search", "detail"])
        self.assertEqual(result.rounds, 4)
        self.assertEqual(result.stop_reason, "model_stopped")
        self.assertEqual(len(result.evidence), 1)

    def test_trace_reports_model_tool_evidence_and_stop_events(self):
        model = _SequenceToolModel([[(0, {"query": "order 42"})], []])
        events = []

        result = self.retrieve(
            model,
            lambda _server, _tool, _arguments, _remaining: {
                "rows": [{"order_id": 42}]
            },
            max_rounds=2,
            trace=lambda kind, data: events.append((kind, data)),
        )

        names = [kind for kind, _data in events]
        self.assertEqual(names[0], "agent_started")
        self.assertIn("model_decision_completed", names)
        self.assertIn("tool_call_started", names)
        self.assertIn("tool_call_completed", names)
        self.assertEqual(names[-1], "agent_finished")
        self.assertEqual(events[-1][1]["evidenceCount"], 1)
        self.assertEqual(result.stop_reason, "model_stopped")

    def test_multiple_calls_in_one_round_execute_in_model_order(self):
        model = _SequenceToolModel(
            [[(0, {"query": "order 42"}), (1, {"id": 42})], []]
        )
        calls = []

        def invoke_tool(server_id, tool_name, arguments, _remaining):
            calls.append((server_id, tool_name, arguments))
            return {"rows": [{"source": tool_name}]}

        result = self.retrieve(
            model,
            invoke_tool,
            tools=[*self.TOOLS, self.SECOND_TOOL],
            max_rounds=2,
        )

        self.assertEqual(
            calls,
            [
                ("kb", "search", {"query": "order 42"}),
                ("orders", "detail", {"id": 42}),
            ],
        )
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual(result.stop_reason, "model_stopped")

    def test_tool_failure_is_returned_to_model_before_it_uses_another_tool(self):
        model = _SequenceToolModel(
            [[(0, {"query": "order 42"})], [(1, {"id": 42})], []]
        )
        calls = []

        def invoke_tool(server_id, tool_name, arguments, _remaining):
            calls.append((server_id, tool_name, arguments))
            if tool_name == "search":
                raise RuntimeError("temporary backend failure")
            return {"rows": [{"id": 42, "status": "blocked"}]}

        result = self.retrieve(
            model,
            invoke_tool,
            tools=[*self.TOOLS, self.SECOND_TOOL],
            max_rounds=3,
        )

        self.assertEqual([item[1] for item in calls], ["search", "detail"])
        self.assertTrue(
            any(
                isinstance(message, ToolMessage)
                and "temporary backend failure" in str(message.content)
                for message in model.messages[1]
            )
        )
        self.assertEqual(len(result.evidence), 1)

    def test_invalid_arguments_count_as_an_attempt_and_are_not_sent_to_mcp(self):
        model = _SequenceToolModel(
            [[(0, {})], [(0, {"query": "order 42"})]]
        )
        calls = []

        def invoke_tool(server_id, tool_name, arguments, _remaining):
            calls.append((server_id, tool_name, arguments))
            return {"rows": [{"order_id": 42}]}

        result = self.retrieve(model, invoke_tool, max_rounds=2)

        self.assertEqual(calls, [("kb", "search", {"query": "order 42"})])
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual(len(result.evidence), 1)

    def test_duplicate_call_reuses_the_tool_message_without_repeating_mcp(self):
        model = _SequenceToolModel(
            [
                [(0, {"query": "order 42"})],
                [(0, {"query": "order 42"})],
            ]
        )
        calls = []

        def invoke_tool(server_id, tool_name, arguments, _remaining):
            calls.append((server_id, tool_name, arguments))
            return {"rows": [{"order_id": 42}]}

        result = self.retrieve(model, invoke_tool, max_rounds=2)

        self.assertEqual(len(calls), 1)
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual(len(result.evidence), 1)

    def test_two_hundred_call_limit_drops_the_rest_of_the_model_batch(self):
        model = _SequenceToolModel(
            [[(0, {"query": f"q-{index}"}) for index in range(201)]]
        )
        calls = []

        def invoke_tool(server_id, tool_name, arguments, _remaining):
            calls.append((server_id, tool_name, arguments))
            return {"rows": [{"query": arguments["query"]}]}

        result = self.retrieve(model, invoke_tool, max_rounds=6)

        self.assertEqual(len(calls), 200)
        self.assertEqual(result.tool_calls, 200)
        self.assertEqual(result.stop_reason, "max_tool_calls")
        self.assertEqual(len(result.evidence), 200)

    def test_configured_two_six_and_two_hundred_round_limits_are_exact(self):
        for rounds in (2, 6, 200):
            with self.subTest(rounds=rounds):
                model = _SequenceToolModel(
                    [[(0, {"query": f"round-{index}"})] for index in range(rounds)]
                )
                result = self.retrieve(
                    model,
                    lambda _server, _tool, arguments, _remaining: {
                        "rows": [{"query": arguments["query"]}]
                    },
                    max_rounds=rounds,
                )

                self.assertEqual(result.rounds, rounds)
                self.assertEqual(result.tool_calls, rounds)
                self.assertEqual(
                    result.stop_reason,
                    "max_tool_calls" if rounds == 200 else "max_rounds",
                )

    def test_non_object_or_invalid_authorized_schemas_are_not_exposed(self):
        model = _SequenceToolModel([[]])
        for schema in (
            {"type": "string"},
            {"type": "object", "properties": {"query": {"type": "not-real"}}},
        ):
            with self.subTest(schema=schema):
                result = self.retrieve(
                    model,
                    lambda *_args: {"rows": []},
                    tools=[{**self.TOOLS[0], "inputSchema": schema}],
                )
                self.assertEqual(result.stop_reason, "no_tools")
                self.assertEqual(result.rounds, 0)

    def test_tool_timeout_stops_later_calls_in_the_same_batch(self):
        model = _SequenceToolModel(
            [[(0, {"query": "slow"}), (0, {"query": "must-not-run"})]]
        )
        calls = []

        def invoke_tool(_server_id, _tool_name, arguments, _remaining):
            calls.append(arguments)
            raise TimeoutError("deadline exceeded")

        result = self.retrieve(model, invoke_tool)

        self.assertEqual(calls, [{"query": "slow"}])
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(result.stop_reason, "timeout")
        self.assertEqual(result.evidence, [])

    def test_timeout_after_evidence_preserves_that_evidence(self):
        clock = _Clock()
        model = _SequenceToolModel([[(0, {"query": "slow but useful"})]])

        def invoke_tool(_server_id, _tool_name, _arguments, _remaining):
            clock.value = 31
            return {"rows": [{"order_id": 42}]}

        result = self.retrieve(model, invoke_tool, clock=clock)

        self.assertEqual(result.stop_reason, "timeout")
        self.assertEqual(len(result.evidence), 1)

    def test_provider_timeout_exception_is_a_timeout_result(self):
        class APITimeoutError(Exception):
            pass

        model = _SequenceToolModel([APITimeoutError("request timed out")])
        result = self.retrieve(model, lambda *_args: {"rows": []})

        self.assertEqual(result.stop_reason, "timeout")
        self.assertEqual(result.evidence, [])

    def test_unknown_tool_and_invalid_native_calls_fail_explicitly(self):
        class UnknownToolModel(_SequenceToolModel):
            def invoke(self, messages, **_kwargs):
                self.messages.append(list(messages))
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "not_authorized",
                            "args": {},
                            "id": "unknown",
                            "type": "tool_call",
                        }
                    ],
                )

        with self.assertRaises(RuntimeProtocolError) as unknown:
            self.retrieve(UnknownToolModel([]), lambda *_args: {})
        self.assertEqual(unknown.exception.code, "MODEL_INVALID_TOOL_CALL")

        invalid_message = AIMessage(
            content="",
            invalid_tool_calls=[
                {
                    "name": "broken",
                    "args": "{not-json",
                    "id": "invalid",
                    "error": "invalid JSON",
                    "type": "invalid_tool_call",
                }
            ],
        )
        with self.assertRaises(RuntimeProtocolError) as invalid:
            self.retrieve(_SequenceToolModel([invalid_message]), lambda *_args: {})
        self.assertEqual(invalid.exception.code, "MODEL_INVALID_TOOL_CALL")

    def test_model_must_support_native_tool_binding(self):
        class NoToolModel:
            def invoke(self, _messages):
                return AIMessage(content="")

        with self.assertRaises(RuntimeProtocolError) as missing:
            self.retrieve(NoToolModel(), lambda *_args: {})
        self.assertEqual(missing.exception.code, "MODEL_TOOL_CALLING_UNSUPPORTED")

        class RejectingModel:
            def bind_tools(self, _tools):
                raise RuntimeError("unsupported parameter: tools")

        with self.assertRaises(RuntimeProtocolError) as rejected:
            self.retrieve(RejectingModel(), lambda *_args: {})
        self.assertEqual(rejected.exception.code, "MODEL_TOOL_CALLING_UNSUPPORTED")

    def test_vision_rejection_has_a_specific_error(self):
        model = _SequenceToolModel(
            [RuntimeError("image_url is not supported by this model")]
        )
        with self.assertRaises(RuntimeProtocolError) as raised:
            self.retrieve(
                model,
                lambda *_args: {},
                image_content=[{"type": "image_url", "image_url": {"url": "data:"}}],
            )

        self.assertEqual(raised.exception.code, "MODEL_VISION_UNSUPPORTED")


if __name__ == "__main__":
    unittest.main()
