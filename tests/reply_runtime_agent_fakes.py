from __future__ import annotations

from worker.reply_runtime.agent import AgentRetrievalResult


def retrieval_from_calls(calls, *, invoke_tool, has_evidence, timeout_seconds=900):
    """In-memory Agent adapter for ReplyRuntime tests at the Agent seam."""

    evidence = []
    normalized_calls = list(calls or [])
    for call in normalized_calls:
        server_id = str(call.get("serverId") or "")
        tool_name = str(call.get("toolName") or "")
        arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        result = invoke_tool(server_id, tool_name, arguments, int(timeout_seconds))
        if has_evidence(result):
            evidence.append(
                {
                    "serverId": server_id,
                    "toolName": tool_name,
                    "arguments": arguments,
                    "result": result,
                }
            )
    return AgentRetrievalResult(
        evidence=evidence,
        rounds=1 if normalized_calls else 0,
        tool_calls=len(normalized_calls),
        stop_reason="model_stopped",
    )
