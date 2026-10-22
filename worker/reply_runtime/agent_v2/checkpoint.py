from __future__ import annotations

from typing import Iterable, Mapping

from .policy import TOOL_SAFETY_READ_ONLY


SAFE_DETERMINISTIC_STAGES = {
    "collecting",
    "waiting_for_image",
    "waiting_for_human_reply",
    "queued_retrieval",
}


def recovery_decision(
    *,
    runtime_version: str,
    answer_engine: str,
    stage: str,
    tools: Iterable[Mapping[str, object]] = (),
) -> tuple[bool, str]:
    if runtime_version != "v2":
        return False, "legacy_runtime"
    if stage in SAFE_DETERMINISTIC_STAGES:
        return True, "safe_deterministic_stage"
    if stage != "retrieving":
        return False, "unsafe_stage"
    if answer_engine != "mcp":
        return False, "provider_request_may_be_uncertain"
    tools = list(tools)
    if tools and all(str(tool.get("safetyClass") or "") == TOOL_SAFETY_READ_ONLY for tool in tools):
        return True, "read_only_mcp_replay"
    return False, "tool_effect_not_read_only"
