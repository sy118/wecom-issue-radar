from __future__ import annotations

from typing import Mapping


TOOL_SAFETY_READ_ONLY = "read_only"
TOOL_SAFETY_UNKNOWN = "unknown"
TOOL_SAFETY_WRITE = "write"
TOOL_SAFETY_DESTRUCTIVE = "destructive"
TOOL_SAFETY_VALUES = {
    TOOL_SAFETY_READ_ONLY,
    TOOL_SAFETY_UNKNOWN,
    TOOL_SAFETY_WRITE,
    TOOL_SAFETY_DESTRUCTIVE,
}


def normalize_tool_annotations(value: object) -> dict:
    """Return the MCP annotation fields that are safe to expose and persist."""

    if hasattr(value, "model_dump"):
        try:
            value = value.model_dump(mode="json", by_alias=True, exclude_none=True)
        except Exception:
            value = None
    if not isinstance(value, Mapping):
        return {}
    aliases = {
        "readOnlyHint": ("readOnlyHint", "read_only_hint"),
        "destructiveHint": ("destructiveHint", "destructive_hint"),
        "idempotentHint": ("idempotentHint", "idempotent_hint"),
        "openWorldHint": ("openWorldHint", "open_world_hint"),
    }
    result: dict[str, bool] = {}
    for public_name, candidates in aliases.items():
        for candidate in candidates:
            if candidate in value and isinstance(value[candidate], bool):
                result[public_name] = bool(value[candidate])
                break
    return result


def classify_tool_safety(
    annotations: object,
    *,
    confirmed_read_only: bool = False,
) -> tuple[str, str]:
    """Classify a tool conservatively from MCP hints plus an explicit override.

    MCP annotations are hints rather than a permission system.  Destructive and
    explicitly non-read-only declarations therefore always win over a local
    confirmation.  A missing declaration remains blocked until an operator has
    explicitly confirmed the current schema as read-only.
    """

    normalized = normalize_tool_annotations(annotations)
    if normalized.get("destructiveHint") is True:
        return TOOL_SAFETY_DESTRUCTIVE, "MCP 标注该工具可能执行破坏性操作"
    if normalized.get("readOnlyHint") is False:
        return TOOL_SAFETY_WRITE, "MCP 明确标注该工具不是只读操作"
    if normalized.get("readOnlyHint") is True:
        return TOOL_SAFETY_READ_ONLY, "MCP 标注该工具为只读操作"
    if confirmed_read_only:
        return TOOL_SAFETY_READ_ONLY, "已由操作员针对当前 Schema 确认为只读"
    return TOOL_SAFETY_UNKNOWN, "MCP 未声明只读属性，需要人工确认"


def tool_is_agent_callable(tool: Mapping[str, object], runtime_version: str) -> bool:
    """Legacy listeners retain compatibility; v2 only exposes read-only tools."""

    if str(runtime_version or "legacy") != "v2":
        return True
    return str(tool.get("safetyClass") or TOOL_SAFETY_UNKNOWN) == TOOL_SAFETY_READ_ONLY
