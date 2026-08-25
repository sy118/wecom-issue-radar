"""Agent Runtime v2 building blocks.

The package deliberately stays independent from provider adapters.  It owns the
deterministic policy, context and lifecycle contracts used by the reply runtime;
OpenAI/Anthropic and MCP/Dify remain boundary adapters.
"""

from .policy import (
    TOOL_SAFETY_DESTRUCTIVE,
    TOOL_SAFETY_READ_ONLY,
    TOOL_SAFETY_UNKNOWN,
    TOOL_SAFETY_WRITE,
    classify_tool_safety,
    normalize_tool_annotations,
)

__all__ = [
    "TOOL_SAFETY_DESTRUCTIVE",
    "TOOL_SAFETY_READ_ONLY",
    "TOOL_SAFETY_UNKNOWN",
    "TOOL_SAFETY_WRITE",
    "classify_tool_safety",
    "normalize_tool_annotations",
]
