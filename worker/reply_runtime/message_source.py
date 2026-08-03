from __future__ import annotations

import re
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
        # The session watermark may lag behind message.db. Always overlap the local
        # message database so detection is measured from database visibility, while
        # durable inbox identity absorbs unchanged rows and higher-sequence replays.
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
        formatted_rows = [
            (raw, format_message(raw, self._user_map, self._member_names))
            for raw in selected
        ]
        result = []
        for raw, formatted in formatted_rows:
            sender_id = int(formatted.get("sender_id") or 0)
            identity = self._identities.get(sender_id) or {}
            content_type = int(formatted.get("content_type") or 0)
            formatted_text = str(formatted.get("content") or "")
            has_image_attachment = _may_have_image_attachment(content_type, formatted_text)
            # Keep polling lightweight: file.db decryption and Cache scans happen
            # later in refresh_images(), outside the independent poll loop.
            images = (
                [
                    {
                        "filename": "",
                        "mimeType": "image/jpeg",
                        "errorCode": "IMAGE_RESOLUTION_PENDING",
                    }
                ]
                if has_image_attachment else []
            )
            text = (
                _clean_message_text(
                    formatted_text,
                    [],
                )
                if has_image_attachment
                else formatted_text.strip()
            )
            if not text and (content_type in {4, 123} or images):
                text = "[图片]"
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
                    "text": text,
                    "images": images,
                }
            )
        return result

    def close(self) -> None:
        self._message_snapshot.close()

    def refresh_images(self, listener: dict, messages: list[dict]) -> list[dict]:
        """Re-resolve images outside polling without moving the message cursor."""

        candidates = [
            message
            for message in messages
            if isinstance(message, dict)
            and _message_images_need_refresh(message)
        ]
        message_ids = []
        for message in candidates:
            try:
                message_id = int(message.get("messageId") or 0)
            except (TypeError, ValueError):
                continue
            if message_id:
                message_ids.append(message_id)
        if not message_ids:
            return [
                _finalize_pending_images(message)
                if isinstance(message, dict) else message
                for message in messages
            ]

        config = load_config(self.config_path)
        group_id = str(listener.get("groupId") or "")
        resolver = FileResolver(config)
        files = resolver.find_files_for_messages(group_id, message_ids)
        refreshed = []
        for message in messages:
            if not isinstance(message, dict):
                refreshed.append(message)
                continue
            try:
                message_id = int(message.get("messageId") or 0)
            except (TypeError, ValueError):
                message_id = 0
            image_infos = [
                info
                for info in files.get(message_id, [])
                if info.get("category") == "Image"
            ]
            resolved = _resolve_image_infos(resolver, image_infos)
            if resolved:
                refreshed.append({**message, "images": resolved})
            elif any(
                _image_resolution_pending(image)
                or _local_image_path_missing(image)
                for image in (message.get("images") or [])
                if isinstance(image, dict)
            ):
                fallback = resolved or [
                    _missing_image_descriptor(image)
                    if _image_resolution_pending(image)
                    or _local_image_path_missing(image)
                    else image
                    for image in (message.get("images") or [])
                    if isinstance(image, dict)
                ]
                refreshed.append({**message, "images": fallback})
            else:
                refreshed.append(message)
        return refreshed

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
    if content_type in {0, 1, 2}:
        return "text"
    return f"unsupported:{content_type}"


_IMAGE_PLACEHOLDER_RE = re.compile(r"\[(?:图片|截图|图像|image)\]", re.IGNORECASE)
_BINARY_PLACEHOLDER_RE = re.compile(r"\[二进制内容\s+\d+\s+字节\]")
_IMAGE_FILENAME_LINE_RE = re.compile(
    r"^[^\\/:*?\"<>|\r\n]{1,220}\.(?:png|jpe?g|gif|webp|bmp|svg|ico)$",
    re.IGNORECASE,
)


def _may_have_image_attachment(content_type: int, text: str) -> bool:
    if content_type in {4, 123}:
        return True
    if content_type != 2:
        return False
    value = str(text or "")
    return bool(
        _IMAGE_PLACEHOLDER_RE.search(value)
        or any(
            _IMAGE_FILENAME_LINE_RE.fullmatch(line.strip())
            for line in value.splitlines()
        )
    )


def _resolve_image_infos(resolver: FileResolver, image_infos: list[dict]) -> list[dict]:
    available_images = []
    unavailable_images = []
    batch_resolver = getattr(resolver, "source_paths_for", None)
    paths = (
        batch_resolver(image_infos)
        if callable(batch_resolver)
        else [resolver.source_path_for(info) for info in image_infos]
    )
    for info, path in zip(image_infos, paths):
        if path is not None and path.is_file():
            available_images.append(
                {
                    "localPath": str(path),
                    "filename": path.name,
                    "mimeType": _image_mime(path),
                }
            )
        else:
            filename = str(info.get("name") or "")
            unavailable_images.append(
                {
                    "filename": filename,
                    "mimeType": _image_mime(Path(filename)),
                    "errorCode": "IMAGE_FILE_MISSING",
                }
            )
    return available_images + unavailable_images


def _local_image_path_missing(image: dict) -> bool:
    local_path = str(image.get("localPath") or "")
    return bool(local_path and not Path(local_path).is_file())


def _image_resolution_pending(image: dict) -> bool:
    return str(image.get("errorCode") or "") == "IMAGE_RESOLUTION_PENDING"


def _message_images_need_refresh(message: dict) -> bool:
    return any(
        isinstance(image, dict)
        and (
            str(image.get("errorCode") or "")
            in {"IMAGE_RESOLUTION_PENDING", "IMAGE_FILE_MISSING"}
            or _local_image_path_missing(image)
        )
        for image in (message.get("images") or [])
    )


def _finalize_pending_images(message: dict) -> dict:
    images = list(message.get("images") or [])
    if not any(
        isinstance(image, dict) and _image_resolution_pending(image)
        for image in images
    ):
        return message
    return {
        **message,
        "images": [
            _missing_image_descriptor(image)
            if isinstance(image, dict) and _image_resolution_pending(image)
            else image
            for image in images
        ],
    }


def _missing_image_descriptor(image: dict) -> dict:
    filename = str(image.get("filename") or "")
    if not filename:
        filename = Path(str(image.get("localPath") or "")).name
    return {
        "filename": filename,
        "mimeType": str(image.get("mimeType") or _image_mime(Path(filename))),
        "errorCode": "IMAGE_FILE_MISSING",
    }


def _clean_message_text(text: str, filenames: list[str]) -> str:
    """Remove attachment decorations while preserving the user's actual words."""

    cleaned = str(text or "")
    for filename in sorted({name for name in filenames if name}, key=len, reverse=True):
        cleaned = cleaned.replace(filename, "")
    cleaned = _IMAGE_PLACEHOLDER_RE.sub("", cleaned)
    cleaned = _BINARY_PLACEHOLDER_RE.sub("", cleaned)
    lines = []
    for value in cleaned.splitlines():
        line = value.strip()
        if not line or _IMAGE_FILENAME_LINE_RE.fullmatch(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _image_mime(path: Path) -> str:
    return {
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(path.suffix.lower(), "image/jpeg")
