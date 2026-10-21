"""Unified read-only chat source contract.

Both the enterprise WeCom reader and the personal WeChat reader expose the same
shape to the application layer: an opaque cursor, incremental reads, media
refresh, and a normalized message record. The concrete readers stay responsible
for encryption, snapshots and version quirks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Protocol, runtime_checkable


MessageDirection = Literal["incoming", "outgoing", "system"]
MessageKind = Literal[
    "text", "image", "voice", "video", "link", "file", "quote", "system", "unknown"
]


@dataclass(slots=True)
class ChatMessage:
    source: Literal["wecom", "wechat"]
    account_id: str
    chat_id: str
    chat_name: str
    chat_kind: Literal["direct", "group"]
    sender_id: str
    sender_name: str
    direction: MessageDirection
    kind: MessageKind
    text: str
    timestamp: int
    sequence: int = 0
    local_id: int = 0
    source_key: str = ""
    quote: dict[str, Any] | None = None
    media: list[dict[str, Any]] = field(default_factory=list)
    mentioned_me: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "accountId": self.account_id,
            "chatId": self.chat_id,
            "chatName": self.chat_name,
            "chatKind": self.chat_kind,
            "senderId": self.sender_id,
            "senderName": self.sender_name,
            "direction": self.direction,
            "kind": self.kind,
            "text": self.text,
            "timestamp": self.timestamp,
            "sequence": self.sequence,
            "localId": self.local_id,
            "sourceKey": self.source_key,
            "quote": self.quote,
            "media": self.media,
            "mentionedMe": self.mentioned_me,
        }


@dataclass(slots=True)
class SourceStatus:
    available: bool
    reason: str = ""
    message: str = ""
    account_id: str = ""


@runtime_checkable
class ChatSource(Protocol):
    """Read-only contract implemented by every chat source."""

    def inspect(self) -> SourceStatus: ...

    def watermark(self) -> dict[str, Any]: ...

    def read(self, cursor: dict[str, Any] | None = None, limit: int = 200) -> tuple[list[ChatMessage], dict[str, Any]]: ...

    def refresh_media(self, messages: Iterable[ChatMessage]) -> list[ChatMessage]: ...

    def close(self) -> None: ...


def messages_to_dicts(messages: Iterable[ChatMessage]) -> list[dict[str, Any]]:
    """Serializes messages for JSON boundaries (IPC, search index, exports)."""

    return [message.to_dict() for message in messages]


def normalized_keys() -> tuple[str, ...]:
    """The stable field order shared by both sources and the renderer."""

    return (
        "source",
        "accountId",
        "chatId",
        "chatName",
        "chatKind",
        "senderId",
        "senderName",
        "direction",
        "kind",
        "text",
        "timestamp",
        "sequence",
        "localId",
        "sourceKey",
        "quote",
        "media",
        "mentionedMe",
    )
