from __future__ import annotations

import mimetypes
import os
import re
import threading
import time
from pathlib import Path

from worker.wecom.local_db import (
    FileResolver,
    MessageDatabaseSnapshot,
    conversation_display_name,
    duplicated_direct_titles,
    format_message,
    get_conversation_state,
    load_config,
    load_conversation_directory,
    load_member_names,
    load_user_directory,
    normalize_epoch_seconds,
    read_messages,
    resolve_self_identity,
)
from worker.reply_runtime.message_normalizer import normalize_wecom_message
from worker.wecom import media as wecom_media


class LocalWeComMessageSource:
    """Incremental adapter over encrypted local WeCom databases."""

    _DIRECTORY_TTL_SECONDS = 30.0
    _CONTEXT_TTL_SECONDS = 10.0

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
        self._cache_lock = threading.Lock()
        self._directory_cache: dict | None = None
        self._context_cache: dict[tuple, tuple[float, dict]] = {}
        self._warmup_started = False

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
            normalized = {
                "cursor": list(_raw_cursor(raw)),
                **normalize_wecom_message(
                    raw,
                    formatted,
                    group_id=group_id,
                    identity=identity,
                ),
            }
            result.append(
                _resolve_message_db_image_paths(config, raw, normalized)
            )
        return _resolve_message_files(config, group_id, result)

    # -- read-only chat context (high-EQ reply) --------------------------
    #
    # These helpers never touch listener cursors or runtime state: the page
    # polls them purely for display and for composing a reply.

    def recent_conversations(self, limit: int = 40, *, refresh: bool = False) -> dict:
        """Newest conversations plus the logged-in identity used for direct names."""

        capped = max(1, min(int(limit or 40), 100))
        directory = self._directory(refresh=refresh)
        user_map = directory.get("user_map") or {}
        member_names = directory.get("member_names") or {}
        accounts = directory.get("accounts") or {}
        self_name = str(directory.get("self_name") or "")
        duplicate_titles = directory.get("duplicate_titles") or set()
        chats = []
        for row in directory.get("conversations") or []:
            conversation_id = str(row.get("conversation_id") or "").strip()
            if not conversation_id:
                continue
            display_name = conversation_display_name(
                conversation_id,
                str(row.get("stored_name") or ""),
                user_map=user_map,
                member_names=member_names,
                accounts=accounts,
                self_name=self_name,
                duplicate_titles=duplicate_titles,
            )
            if not display_name:
                display_name = (
                    "私聊会话"
                    if conversation_id.startswith("S:")
                    else conversation_id
                )
            chats.append(
                {
                    "conversationId": conversation_id,
                    "displayName": display_name,
                    "lastMessageTime": normalize_epoch_seconds(
                        row.get("last_message_time")
                    ),
                    "lastMessageId": int(row.get("last_message_id") or 0),
                    "isGroup": bool(row.get("is_group")),
                }
            )
        chats.sort(
            key=lambda chat: (chat["lastMessageTime"], chat["lastMessageId"]),
            reverse=True,
        )
        chats = chats[:capped]
        self_id = int(directory.get("self_id") or 0)
        self._schedule_warmup(chats)
        return {
            "chats": chats,
            "selfSenderId": str(self_id) if self_id else "",
            "selfSenderName": self_name,
        }

    def read_context(
        self,
        conversation_id: str,
        *,
        limit: int = 20,
        window_seconds: int = 21600,
        before: int | None = None,
        refresh: bool = False,
    ) -> dict:
        """Read one page of display context; the newest page unless ``before`` is set."""

        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            raise ValueError("缺少会话 ID")
        page_limit = max(1, min(int(limit or 20), 50))
        window = max(60, min(int(window_seconds or 21600), 86400))
        before_ts = _optional_seconds(before)
        cache_key = (conversation_id, page_limit, window, before_ts)
        now = time.monotonic()
        with self._cache_lock:
            cached = self._context_cache.get(cache_key)
            if (
                cached is not None
                and not refresh
                and now - cached[0] < self._CONTEXT_TTL_SECONDS
            ):
                return cached[1]
        payload = self._read_context_page(
            conversation_id,
            limit=page_limit,
            window_seconds=window,
            before=before_ts or None,
        )
        with self._cache_lock:
            if len(self._context_cache) >= 64:
                oldest = min(
                    self._context_cache, key=lambda key: self._context_cache[key][0]
                )
                self._context_cache.pop(oldest, None)
            self._context_cache[cache_key] = (time.monotonic(), payload)
        return payload

    def _read_context_page(
        self,
        conversation_id: str,
        *,
        limit: int,
        window_seconds: int,
        before: int | None,
    ) -> dict:
        config = load_config(self.config_path)
        now = int(time.time())
        upper = min(int(before), now + 2) if before else now + 2
        if before:
            start_ts = max(0, upper - 30 * 86400)
            rows = read_messages(
                config,
                conversation_id,
                start_ts,
                upper,
                limit=limit + 1,
                snapshot=self._message_snapshot,
            )
            window_fallback = False
            has_more = len(rows) > limit
        else:
            start_ts = max(0, now - window_seconds)
            rows = read_messages(
                config,
                conversation_id,
                start_ts,
                now + 2,
                limit=limit + 1,
                snapshot=self._message_snapshot,
            )
            window_fallback = False
            has_more = len(rows) > limit
            if not rows:
                # Quiet chats (often direct chats) would otherwise look empty:
                # fall back to their most recent messages regardless of the window.
                rows = read_messages(
                    config,
                    conversation_id,
                    max(0, now - 30 * 86400),
                    now + 2,
                    limit=limit + 1,
                    snapshot=self._message_snapshot,
                )
                window_fallback = bool(rows)
                has_more = len(rows) > limit
            elif len(rows) < limit:
                # The 6h window holds less than one page: older messages outside
                # the window (within 30 days) must still page upwards, otherwise
                # a quiet conversation could never "load earlier" messages.
                has_more = self._has_context_before(
                    config, conversation_id, start_ts, now
                )
        page_rows = sorted(rows[:limit] if len(rows) > limit else rows, key=_raw_cursor)
        directory = self._safe_directory()
        row = self._directory_row(directory, conversation_id)
        if row is not None:
            is_group = bool(row.get("is_group"))
            display_name = conversation_display_name(
                conversation_id,
                str(row.get("stored_name") or ""),
                user_map=directory.get("user_map") or {},
                member_names=directory.get("member_names") or {},
                accounts=directory.get("accounts") or {},
                self_name=str(directory.get("self_name") or ""),
                duplicate_titles=directory.get("duplicate_titles") or set(),
            )
        else:
            is_group = conversation_id.startswith("R:")
            display_name = ""
        if not display_name:
            display_name = (
                "私聊会话" if conversation_id.startswith("S:") else conversation_id
            )
        user_map = directory.get("user_map") or {}
        member_names = directory.get("member_names") or {}
        identities = directory.get("identities") or {}
        messages = []
        for raw in page_rows:
            formatted = format_message(raw, user_map, member_names)
            sender_id = int(formatted.get("sender_id") or 0)
            identity = identities.get(sender_id) or {}
            normalized = normalize_wecom_message(
                raw,
                formatted,
                group_id=conversation_id,
                identity=identity,
            )
            normalized = _resolve_message_db_image_paths(config, raw, normalized)
            images = []
            for index, image in enumerate(normalized.get("images") or []):
                if not isinstance(image, dict):
                    continue
                images.append(
                    {
                        "index": index,
                        "available": bool(image.get("localPath"))
                        and not image.get("errorCode"),
                        "mimeType": str(image.get("mimeType") or ""),
                        "errorCode": str(image.get("errorCode") or ""),
                    }
                )
            messages.append(
                {
                    "messageId": str(normalized.get("messageId") or ""),
                    "senderId": str(normalized.get("senderId") or ""),
                    "senderName": str(normalized.get("senderName") or ""),
                    "sendTime": int(normalized.get("sendTime") or 0),
                    "contentType": str(normalized.get("contentType") or ""),
                    "text": _context_text(normalized.get("text")),
                    "images": images,
                    "imageCount": len(images),
                }
            )
        self_id = int(directory.get("self_id") or 0)
        return {
            "conversationId": conversation_id,
            "displayName": display_name,
            "isGroup": is_group,
            "windowFallback": window_fallback,
            "hasMore": has_more,
            "selfSenderId": str(self_id) if self_id else "",
            "messages": messages,
        }

    def _has_context_before(
        self,
        config: dict,
        conversation_id: str,
        before_ts: int,
        now: int,
    ) -> bool:
        """Whether older messages exist between ``before_ts`` and the 30-day window."""

        end_ts = int(before_ts) - 1
        start_ts = max(0, int(now) - 30 * 86400)
        if end_ts < start_ts:
            return False
        rows = read_messages(
            config,
            conversation_id,
            start_ts,
            end_ts,
            limit=1,
            snapshot=self._message_snapshot,
        )
        return bool(rows)

    # -- directory / context caches ---------------------------------------

    def _directory(self, *, refresh: bool = False) -> dict:
        with self._cache_lock:
            cached = self._directory_cache
            if (
                cached is not None
                and not refresh
                and time.monotonic() - float(cached.get("at") or 0.0)
                < self._DIRECTORY_TTL_SECONDS
            ):
                return cached
            entry = self._build_directory()
            self._directory_cache = entry
            return entry

    def _safe_directory(self) -> dict:
        try:
            return self._directory(refresh=False)
        except Exception:  # noqa: BLE001 - naming must not break message reading
            return {}

    def _build_directory(self) -> dict:
        config = load_config(self.config_path)
        directory = load_conversation_directory(config)
        conversations = directory.get("conversations") or []
        member_names = directory.get("member_names") or {}
        user_map: dict = {}
        accounts: dict = {}
        identities: dict = {}
        if any(not row.get("is_group") for row in conversations):
            try:
                user_directory = load_user_directory(config)
            except Exception:  # noqa: BLE001 - a broken user.db must not hide chats
                user_directory = {}
            user_map = user_directory.get("user_map") or {}
            accounts = user_directory.get("accounts") or {}
            identities = user_directory.get("identities") or {}
        conversation_ids = [
            str(row.get("conversation_id") or "")
            for row in conversations
            if str(row.get("conversation_id") or "").startswith("S:")
        ]
        self_id, self_name = resolve_self_identity(
            conversation_ids, user_map, accounts
        )
        return {
            "at": time.monotonic(),
            "conversations": conversations,
            "member_names": member_names,
            "user_map": user_map,
            "accounts": accounts,
            "identities": identities,
            "self_id": self_id,
            "self_name": self_name,
            "duplicate_titles": duplicated_direct_titles(conversations),
        }

    @staticmethod
    def _directory_row(directory: dict, conversation_id: str) -> dict | None:
        for row in directory.get("conversations") or []:
            if str(row.get("conversation_id") or "") == conversation_id:
                return row
        return None

    # -- prewarm ----------------------------------------------------------

    def _schedule_warmup(self, chats: list[dict]) -> None:
        if self._warmup_started:
            return
        self._warmup_started = True
        conversation_id = str(chats[0].get("conversationId") or "") if chats else ""
        if not conversation_id:
            return
        try:
            _spawn_background_thread(
                self.warmup,
                conversation_id,
                name="wecom-chat-context-warmup",
            )
        except Exception:  # noqa: BLE001 - prewarming is best effort
            return

    def warmup(self, conversation_id: str = "") -> None:
        """Warm the message snapshot from the newest conversation (best effort)."""

        try:
            target = str(conversation_id or "").strip() or self._newest_conversation_id()
            if not target:
                return
            config = load_config(self.config_path)
            now = int(time.time())
            read_messages(
                config,
                target,
                max(0, now - 30 * 86400),
                now + 2,
                limit=40,
                snapshot=self._message_snapshot,
            )
        except Exception:  # noqa: BLE001 - prewarming must never surface
            return

    def _newest_conversation_id(self) -> str:
        directory = self._safe_directory()
        rows = sorted(
            directory.get("conversations") or [],
            key=lambda row: (
                int(row.get("last_message_time") or 0),
                int(row.get("last_message_id") or 0),
            ),
            reverse=True,
        )
        return str(rows[0].get("conversation_id") or "") if rows else ""

    def resolve_image_paths(
        self,
        conversation_id: str,
        message_ids: list[str],
        *,
        limit: int = 80,
        window_seconds: int = 86400,
    ) -> dict[str, list[dict]]:
        """Resolve image local paths for OCR without trusting renderer paths.

        The renderer only sends message ids; paths come from the decrypted
        snapshot and are validated by ``_message_db_image_descriptor``.
        """

        wanted = {str(value) for value in message_ids if str(value)}
        if not wanted:
            return {}
        config = load_config(self.config_path)
        now = int(time.time())
        start_ts = max(0, now - max(60, int(window_seconds or 86400)))
        rows = read_messages(
            config,
            conversation_id,
            start_ts,
            now + 2,
            limit=max(1, min(int(limit or 80), 200)),
            snapshot=self._message_snapshot,
        )
        resolved: dict[str, list[dict]] = {}
        for raw in rows:
            message_id = str(raw.get("message_id") or "")
            if message_id not in wanted:
                continue
            formatted = format_message(raw, self._user_map, self._member_names)
            normalized = normalize_wecom_message(
                raw,
                formatted,
                group_id=conversation_id,
                identity=None,
            )
            normalized = _resolve_message_db_image_paths(config, raw, normalized)
            images = []
            for index, image in enumerate(normalized.get("images") or []):
                if not isinstance(image, dict):
                    continue
                images.append(
                    {
                        "index": index,
                        "localPath": str(image.get("localPath") or ""),
                        "mimeType": str(image.get("mimeType") or "image/jpeg"),
                        "errorCode": str(image.get("errorCode") or ""),
                    }
                )
            resolved[message_id] = images
        return resolved

    def close(self) -> None:
        self._message_snapshot.close()

    def refresh_images(self, listener: dict, messages: list[dict]) -> list[dict]:
        """Re-resolve images outside polling without moving the message cursor."""

        config = load_config(self.config_path)
        directly_refreshed = [
            _refresh_message_db_image_paths(config, message)
            if isinstance(message, dict)
            else message
            for message in messages
        ]
        group_id = str(listener.get("groupId") or "")
        directly_refreshed = _resolve_message_files(
            config, group_id, directly_refreshed
        )
        candidates = [
            message
            for message in directly_refreshed
            if isinstance(message, dict)
            and _message_images_need_refresh(message)
        ]
        message_ids = []
        image_md5_refs_by_message: dict[int, list[str]] = {}
        for message in candidates:
            try:
                message_id = int(message.get("messageId") or 0)
            except (TypeError, ValueError):
                continue
            if message_id:
                message_ids.append(message_id)
                refs = _normalized_image_md5_refs(message.get("imageMd5Refs"))
                if refs:
                    image_md5_refs_by_message.setdefault(message_id, []).extend(refs)
        if not message_ids:
            return [
                _finalize_pending_images(message)
                if isinstance(message, dict) else message
                for message in directly_refreshed
            ]

        resolver = FileResolver(config)
        if image_md5_refs_by_message:
            files = resolver.find_files_for_messages(
                group_id,
                message_ids,
                image_md5_refs_by_message={
                    message_id: list(dict.fromkeys(refs))
                    for message_id, refs in image_md5_refs_by_message.items()
                },
            )
        else:
            files = resolver.find_files_for_messages(group_id, message_ids)
        refreshed = []
        for message in directly_refreshed:
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
            expected_refs = _normalized_image_md5_refs(message.get("imageMd5Refs"))
            resolved = _resolve_image_infos(
                resolver,
                image_infos,
                expected_refs=expected_refs,
            )
            if resolved:
                existing_images = list(message.get("images") or [])
                has_direct_available = any(
                    isinstance(image, dict)
                    and image.get("localPath")
                    and not image.get("errorCode")
                    for image in existing_images
                )
                refreshed.append(
                    {
                        **message,
                        "images": (
                            _merge_image_descriptors(existing_images, resolved)
                            if has_direct_available
                            else resolved
                        ),
                    }
                )
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
        user_directory = load_user_directory(config)
        self._user_map = user_directory.get("user_map") or {}
        self._identities = user_directory.get("identities") or {}
        self._member_names = load_member_names(config)
        self._identity_loaded_at = now


def _spawn_background_thread(target, argument, *, name: str) -> None:
    """Run ``target(argument)`` on a daemon thread so queries stay responsive."""

    thread = threading.Thread(target=target, args=(argument,), name=name, daemon=True)
    thread.start()


def _optional_seconds(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _resolve_message_files(config: dict, group_id: str, messages: list[dict]) -> list[dict]:
    """Resolve non-image attachments through file.db and CacheMapping in one batch."""

    candidates = [
        message
        for message in messages
        if isinstance(message, dict)
        and str(message.get("contentType") or "") in {"file", "voice"}
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
        return messages

    resolver = FileResolver(config)
    files_by_message = resolver.find_files_for_messages(group_id, message_ids)
    result = []
    for message in messages:
        if message not in candidates:
            result.append(message)
            continue
        try:
            message_id = int(message.get("messageId") or 0)
        except (TypeError, ValueError):
            message_id = 0
        infos = [
            info
            for info in files_by_message.get(message_id, [])
            if isinstance(info, dict) and str(info.get("category") or "File") != "Image"
        ]
        paths = resolver.source_paths_for(infos) if infos else []
        descriptors = _file_descriptors(config, infos, paths)
        if not descriptors:
            descriptors = [_pending_file_descriptor({})]
        result.append({**message, "files": descriptors})
    return result


def _file_descriptors(config: dict, infos: list[dict], paths: list[Path | None]) -> list[dict]:
    result = []
    seen = set()
    for info, path in zip(infos, paths):
        safe_path = _safe_cache_file_candidate(config, path) if path is not None else None
        if safe_path is None or not safe_path.is_file():
            result.append(_pending_file_descriptor(info))
            continue
        identity = os.path.normcase(str(safe_path.resolve(strict=False)))
        if identity in seen:
            continue
        seen.add(identity)
        try:
            size = int(safe_path.stat().st_size)
        except OSError:
            result.append(_pending_file_descriptor(info))
            continue
        result.append(
            {
                "localPath": str(safe_path),
                "filename": str(info.get("name") or safe_path.name) or safe_path.name,
                "mimeType": mimetypes.guess_type(safe_path.name)[0]
                or "application/octet-stream",
                "size": size,
            }
        )
    return result


def _safe_cache_file_candidate(config: dict, candidate: Path | None) -> Path | None:
    raw_data_dir = str(config.get("wxwork_db_dir") or "").strip()
    if not raw_data_dir or candidate is None:
        return None
    try:
        cache_root = (Path(raw_data_dir).parent / "Cache").resolve(strict=True)
        display_path = Path(os.path.abspath(os.path.normpath(str(candidate))))
        if display_path.is_symlink() or not display_path.is_file():
            return None
        resolved = display_path.resolve(strict=True)
        if not resolved.is_relative_to(cache_root):
            return None
        return display_path
    except (OSError, RuntimeError, ValueError):
        return None


def _pending_file_descriptor(info: dict) -> dict:
    filename = Path(str(info.get("name") or "")).name
    return {
        "filename": filename,
        "mimeType": mimetypes.guess_type(filename)[0] or "application/octet-stream",
        "size": max(0, int(info.get("size") or 0)),
        "errorCode": "FILE_RESOLUTION_PENDING",
    }


def _resolve_message_db_image_paths(config: dict, raw: dict, message: dict) -> dict:
    """Resolve trusted WeCom Cache/Image paths without decrypting file.db."""

    paths = _message_db_image_paths(config, raw.get("local_extra_content_raw"))
    if not paths:
        return message
    descriptors = [_message_db_image_descriptor(config, path) for path in paths]
    expected_count = max(len(descriptors), len(message.get("images") or []))
    descriptors.extend(
        _pending_image_descriptor()
        for _ in range(max(0, expected_count - len(descriptors)))
    )
    return {**message, "images": descriptors}


def _refresh_message_db_image_paths(config: dict, message: dict) -> dict:
    images = list(message.get("images") or [])
    changed = False
    refreshed = []
    for image in images:
        if not isinstance(image, dict):
            continue
        local_path = str(image.get("localPath") or "")
        if (
            local_path
            and str(image.get("errorCode") or "")
            in {"IMAGE_RESOLUTION_PENDING", "IMAGE_FILE_MISSING"}
        ):
            descriptor = _message_db_image_descriptor(config, Path(local_path))
            refreshed.append(descriptor)
            changed = changed or descriptor != image
        else:
            refreshed.append(image)
    return {**message, "images": refreshed} if changed else message


def _message_db_image_paths(config: dict, raw_value) -> list[Path]:
    """Trusted Cache/Image paths embedded in the message DB row.

    The daily pipeline uses the same extractor through
    :mod:`worker.wecom.media`, so both chains agree on order and trust rules.
    """

    return wecom_media.extract_local_image_paths(config, raw_value)


def _wecom_image_cache_root(config: dict) -> Path | None:
    return wecom_media.image_cache_root(config)


def _safe_cache_image_candidate(cache_root: Path, candidate: Path) -> Path | None:
    return wecom_media.safe_cache_image_path(cache_root, candidate)


def _message_db_image_descriptor(config: dict, path: Path) -> dict:
    cache_root = _wecom_image_cache_root(config)
    safe_path = (
        _safe_cache_image_candidate(cache_root, path)
        if cache_root is not None
        else None
    )
    if safe_path is not None and safe_path.is_file():
        return {
            "localPath": str(safe_path),
            "filename": safe_path.name,
            "mimeType": _image_mime(safe_path),
        }
    descriptor = _pending_image_descriptor()
    if safe_path is not None:
        descriptor["localPath"] = str(safe_path)
        descriptor["filename"] = safe_path.name
        descriptor["mimeType"] = _image_mime(safe_path)
    return descriptor


def _pending_image_descriptor() -> dict:
    return {
        "filename": "",
        "mimeType": "image/jpeg",
        "errorCode": "IMAGE_RESOLUTION_PENDING",
    }


def _merge_image_descriptors(existing: list[dict], resolved: list[dict]) -> list[dict]:
    """Fill unresolved source-order slots and deduplicate resolver fallbacks."""

    seen_paths = {
        os.path.normcase(str(Path(str(image.get("localPath"))).resolve(strict=False)))
        for image in existing
        if isinstance(image, dict)
        and not image.get("errorCode")
        and str(image.get("localPath") or "")
    }
    candidates = []
    missing = []
    for image in resolved:
        if not isinstance(image, dict):
            continue
        local_path = str(image.get("localPath") or "")
        if local_path and not image.get("errorCode"):
            identity = os.path.normcase(str(Path(local_path).resolve(strict=False)))
            if identity in seen_paths:
                continue
            seen_paths.add(identity)
            candidates.append(image)
        elif image.get("errorCode"):
            missing.append(image)

    result = []
    available = iter(candidates)
    unavailable = iter(missing)
    for image in existing:
        if not isinstance(image, dict):
            continue
        if not image.get("errorCode") and (
            not image.get("localPath") or Path(str(image["localPath"])).is_file()
        ):
            result.append(image)
            continue
        replacement = next(available, None)
        if replacement is None:
            replacement = next(unavailable, None)
        result.append(replacement or _missing_image_descriptor(image))
    result.extend(list(available))
    return result


_MAX_CONTEXT_TEXT_CHARS = 2000


def _context_text(value) -> str:
    text = str(value or "").strip()
    if len(text) <= _MAX_CONTEXT_TEXT_CHARS:
        return text
    return text[:_MAX_CONTEXT_TEXT_CHARS] + "…"


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


_IMAGE_MD5_REF_TEXT_RE = re.compile(r"^[0-9A-Fa-f]{32}$")
_MAX_IMAGE_MD5_REFS = 64


def _normalized_image_md5_refs(value) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    seen = set()
    for item in value:
        ref = str(item or "").strip().lower()
        if not _IMAGE_MD5_REF_TEXT_RE.fullmatch(ref) or ref in seen:
            continue
        seen.add(ref)
        result.append(ref)
        if len(result) >= _MAX_IMAGE_MD5_REFS:
            break
    return result


def _resolve_image_infos(
    resolver: FileResolver,
    image_infos: list[dict],
    *,
    expected_refs: list[str] | None = None,
) -> list[dict]:
    batch_resolver = getattr(resolver, "source_paths_for", None)
    paths = (
        batch_resolver(image_infos)
        if callable(batch_resolver)
        else [resolver.source_path_for(info) for info in image_infos]
    )
    standalone = []
    candidates_by_ref: dict[str, list[tuple[dict, Path | None]]] = {}
    for info, path in zip(image_infos, paths):
        lookup_md5 = str(info.get("lookup_md5") or "")
        if lookup_md5:
            candidates_by_ref.setdefault(lookup_md5, []).append((info, path))
        else:
            standalone.append((info, path))

    if expected_refs:
        result = []
        for ref in expected_refs:
            candidates = candidates_by_ref.get(ref, [])
            if not candidates:
                result.append(_missing_image_descriptor({}))
                continue
            info, path = next(
                (
                    (candidate_info, candidate_path)
                    for candidate_info, candidate_path in candidates
                    if candidate_path is not None and candidate_path.is_file()
                ),
                candidates[0],
            )
            result.append(_resolved_image_descriptor(info, path))
        return result

    selected = list(standalone)
    for candidates in candidates_by_ref.values():
        selected.append(
            next(
                (
                    (info, path)
                    for info, path in candidates
                    if path is not None and path.is_file()
                ),
                candidates[0],
            )
        )

    descriptors = [_resolved_image_descriptor(info, path) for info, path in selected]
    return [item for item in descriptors if not item.get("errorCode")] + [
        item for item in descriptors if item.get("errorCode")
    ]


def _resolved_image_descriptor(info: dict, path: Path | None) -> dict:
    if path is not None and path.is_file():
        return {
            "localPath": str(path),
            "filename": path.name,
            "mimeType": _image_mime(path),
        }
    filename = str(info.get("name") or "")
    return _missing_image_descriptor(
        {"filename": filename, "mimeType": _image_mime(Path(filename))}
    )


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
    local_path = str(image.get("localPath") or "")
    filename = str(image.get("filename") or "")
    if not filename:
        filename = Path(local_path).name
    descriptor = {
        "filename": filename,
        "mimeType": str(image.get("mimeType") or _image_mime(Path(filename))),
        "errorCode": "IMAGE_FILE_MISSING",
    }
    if local_path:
        descriptor["localPath"] = local_path
    return descriptor


def _image_mime(path: Path) -> str:
    return {
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(path.suffix.lower(), "image/jpeg")
