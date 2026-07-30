from __future__ import annotations

import time
from pathlib import Path

from worker.wecom.local_db import (
    FileResolver,
    MessageDatabaseSnapshot,
    format_message,
    get_conversation_state,
    load_config,
    load_member_names,
    load_user_identities,
    load_user_map,
    read_messages,
)


class LocalWeComMessageSource:
    """Incremental adapter over encrypted local WeCom databases."""

    def __init__(
        self,
        config_path: str | Path | None = None,
        *,
        message_snapshot: MessageDatabaseSnapshot | None = None,
    ) -> None:
        self.config_path = str(config_path) if config_path else None
        self._message_snapshot = message_snapshot or MessageDatabaseSnapshot()
        self._identity_loaded_at = 0.0
        self._user_map = {}
        self._member_names = {}
        self._identities = {}

    def watermark(self, listener: dict):
        config = load_config(self.config_path)
        group_id = str(listener.get("groupId") or "")
        state = get_conversation_state(config, group_id)
        if not state:
            return None
        last_time = int(state.get("last_message_time") or 0)
        if not last_time:
            return None
        # Resolve the exact composite tail once. Subsequent reads overlap and durable inbox
        # uniqueness makes changing DB/WAL snapshots safe.
        rows = read_messages(
            config,
            group_id,
            max(0, last_time - 1),
            last_time,
            limit=200,
            snapshot=self._message_snapshot,
        )
        if rows:
            latest = max(rows, key=_raw_cursor)
            return list(_raw_cursor(latest))
        return [last_time, 2**63 - 1, int(state.get("last_message_id") or 0), 2**63 - 1]

    def read_force(self, listener: dict, cursor):
        """Read the encrypted message database even if the fast session watermark lags."""
        return self.read(listener, cursor, bypass_fast_watermark=True)

    def read(self, listener: dict, cursor, *, bypass_fast_watermark: bool = False):
        config = load_config(self.config_path)
        group_id = str(listener.get("groupId") or "")
        cursor_key = _coerce_cursor(cursor)
        state = get_conversation_state(config, group_id)
        if not state:
            return []
        last_message_time = int(state.get("last_message_time") or 0)
        if not bypass_fast_watermark and last_message_time and last_message_time < int(cursor_key[0]):
            return []
        last_message_id = int(state.get("last_message_id") or 0)
        if (
            not bypass_fast_watermark
            and last_message_time == int(cursor_key[0])
            and last_message_id
            and last_message_id == int(cursor_key[2])
        ):
            return []
        # Equal timestamps deliberately overlap: sequence/message/server components can
        # still advance within the same second, and inbox uniqueness removes duplicates.
        start = max(0, int(cursor_key[0]) - 2)
        end = int(time.time()) + 2
        raw_rows = read_messages(
            config,
            group_id,
            start,
            end,
            limit=2000,
            snapshot=self._message_snapshot,
        )
        self._refresh_identities(config)
        selected = [row for row in raw_rows if _raw_cursor(row) > cursor_key]
        image_rows = [row for row in selected if int(row.get("content_type") or 0) in {4, 123}]
        files = {}
        resolver = None
        if image_rows:
            resolver = FileResolver(config)
            files = resolver.find_files_for_messages(
                group_id, [int(row.get("message_id") or 0) for row in image_rows]
            )
        result = []
        for raw in selected:
            formatted = format_message(raw, self._user_map, self._member_names)
            sender_id = int(formatted.get("sender_id") or 0)
            identity = self._identities.get(sender_id) or {}
            content_type = int(formatted.get("content_type") or 0)
            images = []
            if content_type in {4, 123} and resolver is not None:
                for info in files.get(int(formatted["message_id"]), []):
                    if info.get("category") != "Image":
                        continue
                    path = resolver.source_path_for(info)
                    if path and path.is_file():
                        images.append(
                            {
                                "localPath": str(path),
                                "filename": path.name,
                                "mimeType": _image_mime(path),
                            }
                        )
            result.append(
                {
                    "cursor": list(_raw_cursor(raw)),
                    "messageId": str(formatted.get("message_id") or ""),
                    "serverId": str(formatted.get("server_id") or ""),
                    "sequence": int(formatted.get("sequence") or 0),
                    "sendTime": int(formatted.get("send_time") or 0),
                    "groupId": group_id,
                    "senderId": str(sender_id),
                    "senderName": str(identity.get("display_name") or formatted.get("sender") or sender_id),
                    "account": str(identity.get("account") or ""),
                    "mobile": str(identity.get("mobile") or ""),
                    "contentType": _content_kind(content_type),
                    "text": str(formatted.get("content") or ""),
                    "images": images,
                }
            )
        return result

    def close(self) -> None:
        self._message_snapshot.close()

    def _refresh_identities(self, config: dict) -> None:
        now = time.monotonic()
        if self._identity_loaded_at and now - self._identity_loaded_at < 300:
            return
        self._user_map = load_user_map(config)
        self._member_names = load_member_names(config)
        self._identities = load_user_identities(config)
        self._identity_loaded_at = now


def _raw_cursor(row: dict) -> tuple[int, int, int, int]:
    return (
        int(row.get("send_time") or 0),
        int(row.get("sequence") or 0),
        int(row.get("message_id") or 0),
        int(row.get("server_id") or 0),
    )


def _coerce_cursor(cursor) -> tuple[int, int, int, int]:
    values = list(cursor or [0, 0, 0, 0])
    values.extend([0] * (4 - len(values)))
    return tuple(int(value or 0) for value in values[:4])


def _content_kind(content_type: int) -> str:
    if content_type in {4, 123}:
        return "image"
    if content_type == 29:
        return "link"
    if content_type == 7:
        return "voice"
    if content_type in {14, 15, 23}:
        return "file"
    if content_type == 1:
        return "text"
    return f"unsupported:{content_type}"


def _image_mime(path: Path) -> str:
    return {
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(path.suffix.lower(), "image/jpeg")
