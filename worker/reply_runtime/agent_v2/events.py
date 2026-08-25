from __future__ import annotations

from typing import Mapping


ROLE_BY_EVENT_PREFIX = {
    "classification": "classifier",
    "human_answer": "human_reply_matcher",
    "agent_": "retriever",
    "model_decision": "retriever",
    "bootstrap_": "retriever",
    "tool_call": "retriever",
    "context_compaction": "retriever",
    "answer_generation": "composer",
    "answer_compression": "compressor",
    "independent_review": "reviewer",
    "delivery": "delivery",
}


def event_role(kind: str) -> str:
    value = str(kind or "")
    for prefix, role in ROLE_BY_EVENT_PREFIX.items():
        if value.startswith(prefix):
            return role
    return "runtime"


def timeline_metadata(kind: str, data: object) -> dict:
    """Project detailed traces into the always-on, content-light timeline."""

    source: Mapping[str, object] = data if isinstance(data, Mapping) else {}
    allowed = {
        "round",
        "attempt",
        "maxAttempts",
        "maxRounds",
        "maxToolCalls",
        "timeoutSeconds",
        "remainingSeconds",
        "messageCount",
        "evidenceCount",
        "toolCalls",
        "serverId",
        "toolName",
        "alias",
        "reason",
        "remainingCalls",
        "usableEvidence",
        "timeout",
        "provider",
        "answerUtf8Bytes",
        "maxUtf8Bytes",
        "stopReason",
        "beforeChars",
        "afterChars",
        "droppedMessages",
        "cached",
        "resumed",
        "recovery",
        "inputTokens",
        "outputTokens",
        "totalTokens",
        "safetyClass",
        "status",
        "code",
    }
    result = {str(key): value for key, value in source.items() if key in allowed}
    if "result" in source:
        result["resultPresent"] = source.get("result") is not None
    if "arguments" in source:
        arguments = source.get("arguments")
        result["argumentKeys"] = sorted(str(key) for key in arguments) if isinstance(arguments, Mapping) else []
    if "error" in source:
        result["hasError"] = bool(source.get("error"))
    return result
