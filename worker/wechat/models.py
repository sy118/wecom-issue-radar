from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


MessageKind = Literal[
    "text", "image", "voice", "video", "link", "file", "quote", "system", "unknown"
]


@dataclass(slots=True)
class Chat:
    account_id: str
    chat_id: str
    display_name: str
    kind: Literal["direct", "group"]
    last_message: str = ""
    last_timestamp: int = 0
    unread: int = 0

    def public(self) -> dict[str, Any]:
        return {
            "accountId": self.account_id,
            "chatId": self.chat_id,
            "displayName": self.display_name,
            "kind": self.kind,
            "lastMessage": self.last_message,
            "lastTimestamp": self.last_timestamp,
            "unread": self.unread,
        }


@dataclass(slots=True)
class SourceCursor:
    project_id: str
    source_db: str
    create_time: int = 0
    sort_seq: int = 0
    local_id: int = 0


@dataclass(slots=True)
class SourceMessage:
    project_id: str
    source_key: str
    source_db: str
    local_id: int
    server_id: int
    sort_seq: int
    create_time: int
    sender_id: str
    sender_name: str
    direction: Literal["incoming", "outgoing", "system"]
    kind: MessageKind
    text: str
    quote: dict[str, Any] | None = None
    media: list[dict[str, Any]] = field(default_factory=list)
    mentioned_me: bool = False
    backlog: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "id": self.source_key,
            "projectId": self.project_id,
            "sourceDb": self.source_db,
            "localId": self.local_id,
            "serverId": self.server_id,
            "sortSeq": self.sort_seq,
            "createTime": self.create_time,
            "senderId": self.sender_id,
            "senderName": self.sender_name,
            "direction": self.direction,
            "kind": self.kind,
            "text": self.text,
            "quote": self.quote,
            "media": self.media,
            "mentionedMe": self.mentioned_me,
            "backlog": self.backlog,
        }
