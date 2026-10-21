"""Unified chat source abstractions shared by WeCom and personal WeChat."""

from .base import (
    ChatMessage,
    ChatSource,
    MessageDirection,
    MessageKind,
    SourceStatus,
    messages_to_dicts,
    normalized_keys,
)

__all__ = [
    "ChatMessage",
    "ChatSource",
    "MessageDirection",
    "MessageKind",
    "SourceStatus",
    "messages_to_dicts",
    "normalized_keys",
]
