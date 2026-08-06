from __future__ import annotations

import mimetypes
import os
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
from worker.reply_runtime.message_normalizer import normalize_wecom_message


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
        self._user_map = load_user_map(config)
        self._member_names = load_member_names(config)
        self._identities = load_user_identities(config)
        self._identity_loaded_at = now


_LOCAL_IMAGE_PATH_RE = re.compile(
    r"(?P<path>(?:[A-Za-z]:[\\/]|\\\\[^\\/\x00-\x1f]+[\\/][^\\/\x00-\x1f]+[\\/])"
    r"[^\x00-\x1f\"<>|?*]{1,2048}?\.(?:png|jpe?g|gif|webp|bmp|svg|ico))",
    re.IGNORECASE,
)
_MAX_LOCAL_IMAGE_PATH_SCAN_BYTES = 1_048_576


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
    cache_root = _wecom_image_cache_root(config)
    if cache_root is None:
        return []
    if isinstance(raw_value, str):
        text = raw_value[:_MAX_LOCAL_IMAGE_PATH_SCAN_BYTES]
    elif isinstance(raw_value, memoryview):
        text = raw_value.tobytes()[:_MAX_LOCAL_IMAGE_PATH_SCAN_BYTES].decode(
            "utf-8", errors="ignore"
        )
    elif isinstance(raw_value, (bytes, bytearray)):
        text = bytes(raw_value[:_MAX_LOCAL_IMAGE_PATH_SCAN_BYTES]).decode(
            "utf-8", errors="ignore"
        )
    else:
        return []

    result = []
    seen = set()
    for match in _LOCAL_IMAGE_PATH_RE.finditer(text):
        candidate = Path(match.group("path"))
        normalized = _safe_cache_image_candidate(cache_root, candidate)
        if normalized is None:
            continue
        identity = os.path.normcase(str(normalized))
        if identity in seen:
            continue
        seen.add(identity)
        result.append(normalized)
    return result


def _wecom_image_cache_root(config: dict) -> Path | None:
    raw_data_dir = str(config.get("wxwork_db_dir") or "").strip()
    if not raw_data_dir:
        return None
    try:
        return (Path(raw_data_dir).parent / "Cache" / "Image").resolve(strict=False)
    except OSError:
        return None


def _safe_cache_image_candidate(cache_root: Path, candidate: Path) -> Path | None:
    try:
        display_path = Path(os.path.abspath(os.path.normpath(str(candidate))))
        resolved_candidate = display_path.resolve(strict=False)
        resolved_root = cache_root.resolve(strict=False)
        if not resolved_candidate.is_relative_to(resolved_root):
            return None
        if resolved_candidate.exists():
            existing = resolved_candidate.resolve(strict=True)
            existing_root = resolved_root.resolve(strict=True)
            if not existing.is_relative_to(existing_root):
                return None
        # Keep the database spelling (including Windows 8.3 aliases) for the
        # descriptor while using resolved paths exclusively for trust checks.
        return display_path
    except (OSError, RuntimeError, ValueError):
        return None


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
