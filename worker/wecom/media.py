"""Shared WeCom image/media resolution for the daily pipeline and reply runtime.

This module owns the ordered lookup used by every WeCom image consumer:

1. local image paths embedded in the message DB ``local_extra_content`` column,
2. ``file.db`` rows plus the ``CacheMapping`` mapping table (including MD5
   matches across conversations, so forwarded images resolve),
3. a bounded filename scan of the account ``Cache/Image`` directory.

It also owns the honest failure reasons, the file completeness checks and the
configurable cache-wait budget.  It deliberately does not import the reply
runtime or the pipeline package, so both sides can depend on it without a
dependency cycle.

Descriptors are plain dictionaries (``MediaDescriptor.to_dict``) with a stable
shape::

    {
        "available": bool,
        "status": "ready" | "missing",
        "reason": "" | "not_found" | "not_cached" | "partial" | "too_large"
                  | "too_many" | "unsupported" | "permission"
                  | "wecom_not_running" | "keys_missing",
        "path": str,
        "filename": str,
        "mime": str,
        "size": int,
        "md5": str,
        "source": "local_extra_content" | "file_db" | "cache_mapping"
                  | "cache_scan" | "",
        "detail": str,
    }
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .local_db import FileResolver


# Model-provider limits. The reply runtime's adapters enforce the same numbers;
# keeping them here lets the batch pipeline report identical reasons instead of
# discovering the problem only inside a model request.
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGES_PER_MESSAGE = 8
MAX_IMAGE_TOTAL_BYTES = 40 * 1024 * 1024

IMAGE_CONTENT_TYPES = frozenset({4, 123})
IMAGE_MD5_REFERENCE_CONTENT_TYPES = frozenset({2, 123, 1011})
IMAGE_MD5_REFERENCE_FIELDS = ("content_raw", "extra_content_raw", "local_extra_content_raw")

IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"})
IMAGE_MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
}

REASON_NOT_FOUND = "not_found"
REASON_NOT_CACHED = "not_cached"
REASON_PARTIAL = "partial"
REASON_TOO_LARGE = "too_large"
REASON_TOO_MANY = "too_many"
REASON_UNSUPPORTED = "unsupported"
REASON_PERMISSION = "permission"
REASON_WECOM_NOT_RUNNING = "wecom_not_running"
REASON_KEYS_MISSING = "keys_missing"

REASON_LABELS = {
    REASON_NOT_FOUND: "未找到图片缓存",
    REASON_NOT_CACHED: "图片尚未写入本机缓存",
    REASON_PARTIAL: "图片文件不完整",
    REASON_TOO_LARGE: "图片超过大小限制",
    REASON_TOO_MANY: "图片数量超过模型限制",
    REASON_UNSUPPORTED: "图片格式不支持",
    REASON_PERMISSION: "无权限读取图片",
    REASON_WECOM_NOT_RUNNING: "企业微信未运行",
    REASON_KEYS_MISSING: "未提取数据库密钥",
}

DEFAULT_DAILY_IMAGE_WAIT_SECONDS = 30.0
DEFAULT_REPLY_IMAGE_WAIT_SECONDS = 180.0
IMAGE_WAIT_ENV = "WECOM_IMAGE_WAIT_SECONDS"
MAX_IMAGE_WAIT_SECONDS = 900.0

_LOCAL_IMAGE_PATH_RE = re.compile(
    r"(?P<path>(?:[A-Za-z]:[\\/]|\\\\[^\\/\x00-\x1f]+[\\/][^\\/\x00-\x1f]+[\\/])"
    r"[^\x00-\x1f\"<>|?*]{1,2048}?\.(?:png|jpe?g|gif|webp|bmp))",
    re.IGNORECASE,
)
_IMAGE_MD5_REF_RE = re.compile(rb"\x52\x20([0-9A-Fa-f]{32})")
_IMAGE_FILENAME_RE = re.compile(
    r"([^\\/:*?\"<>|\r\n\t ]{1,180}\.(?:png|jpe?g|gif|webp|bmp))",
    re.IGNORECASE,
)
_MAX_REF_SCAN_BYTES = 1_048_576
_MAX_MD5_REFS = 64

_PNG_IEND = b"IEND\xaeB`\x82"


@dataclass(frozen=True)
class MediaDescriptor:
    available: bool
    status: str
    reason: str = ""
    path: str = ""
    filename: str = ""
    mime: str = ""
    size: int = 0
    md5: str = ""
    source: str = ""
    detail: str = ""

    @classmethod
    def ready(
        cls,
        path: Path,
        *,
        source: str = "",
        md5: str = "",
        size: int | None = None,
        mime: str = "",
    ) -> "MediaDescriptor":
        try:
            actual_size = int(size if size is not None else path.stat().st_size)
        except OSError:
            actual_size = 0
        return cls(
            available=True,
            status="ready",
            reason="",
            path=str(path),
            filename=path.name,
            mime=mime or image_mime(path),
            size=actual_size,
            md5=md5,
            source=source,
        )

    @classmethod
    def missing(
        cls,
        reason: str,
        *,
        filename: str = "",
        path: str = "",
        source: str = "",
        md5: str = "",
        detail: str = "",
    ) -> "MediaDescriptor":
        return cls(
            available=False,
            status="missing",
            reason=reason,
            path=path,
            filename=filename,
            mime=image_mime(Path(filename)) if filename else "",
            size=0,
            md5=md5,
            source=source,
            detail=detail or REASON_LABELS.get(reason, ""),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Precondition:
    ok: bool
    reason: str = ""
    message: str = ""
    action: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class MediaRequest:
    """One message's image lookup input.

    ``local_extra``/``content_raw``/``extra_content_raw`` are the raw DB column
    values (bytes or str). ``expected_count`` is the visible image count from
    the caller; ``filename_hints`` enables the bounded cache filename scan.
    """

    message_id: int
    conversation_id: str
    content_type: int = 0
    local_extra: bytes | str | None = None
    content_raw: bytes | str | None = None
    extra_content_raw: bytes | str | None = None
    text: str = ""
    expected_count: int = 0
    filename_hints: tuple[str, ...] = ()
    md5_refs: tuple[str, ...] = ()
    send_time: int = 0


def image_wait_seconds(
    config: dict | None = None,
    *,
    default: float = DEFAULT_DAILY_IMAGE_WAIT_SECONDS,
) -> float:
    """Resolve the image cache wait budget from config or the environment."""

    raw = None
    if isinstance(config, dict):
        raw = config.get("image_wait_seconds")
    if raw is None:
        raw = os.environ.get(IMAGE_WAIT_ENV)
    if raw is None:
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    return max(0.0, min(value, MAX_IMAGE_WAIT_SECONDS))


def image_mime(path: Path) -> str:
    return IMAGE_MIME_BY_EXT.get(path.suffix.lower()) or "image/jpeg"


def wecom_process_running() -> bool:
    """Best-effort check used to explain why an image is still missing."""

    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq WXWork.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=8,
            creationflags=flags,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    for line in (result.stdout or "").splitlines():
        if line.strip().lower().startswith('"wxwork.exe"'):
            return True
    return False


def _as_bytes(value) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, str):
        return value.encode("utf-8", errors="ignore")
    return b""


def extract_local_image_paths(config: dict, raw_value) -> list[Path]:
    """Extract trusted ``Cache/Image`` paths from a message DB column value."""

    cache_root = image_cache_root(config)
    if cache_root is None:
        return []
    data = _as_bytes(raw_value)[:_MAX_REF_SCAN_BYTES]
    if not data:
        return []
    text = data.decode("utf-8", errors="ignore")
    result: list[Path] = []
    seen: set[str] = set()
    for match in _LOCAL_IMAGE_PATH_RE.finditer(text):
        candidate = Path(match.group("path"))
        safe = safe_cache_image_path(cache_root, candidate)
        if safe is None:
            continue
        identity = os.path.normcase(str(safe))
        if identity in seen:
            continue
        seen.add(identity)
        result.append(safe)
    return result


def extract_image_md5_refs(content_type: int, raw: dict) -> list[str]:
    """Return attachment MD5 references carried by protobuf field 10 (0x52 0x20)."""

    if int(content_type or 0) not in IMAGE_MD5_REFERENCE_CONTENT_TYPES:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for field in IMAGE_MD5_REFERENCE_FIELDS:
        data = _as_bytes(raw.get(field))[:_MAX_REF_SCAN_BYTES]
        for match in _IMAGE_MD5_REF_RE.finditer(data):
            ref = match.group(1).decode("ascii").lower()
            if ref in seen:
                continue
            seen.add(ref)
            result.append(ref)
            if len(result) >= _MAX_MD5_REFS:
                return result
    return result


def filename_hints_from_raw(raw: dict) -> tuple[str, ...]:
    """Collect image filename hints from message text/raw fields."""

    hints: list[str] = []
    seen: set[str] = set()
    for field in ("text", "content_raw", "extra_content_raw", "local_extra_content_raw"):
        value = raw.get(field)
        text = value if isinstance(value, str) else _as_bytes(value).decode("utf-8", errors="ignore")
        for match in _IMAGE_FILENAME_RE.finditer(text or ""):
            name = os.path.basename(match.group(1).strip())
            if not name or name in seen:
                continue
            seen.add(name)
            hints.append(name)
    return tuple(hints)


def image_cache_root(config: dict) -> Path | None:
    raw_data_dir = str(config.get("wxwork_db_dir") or "").strip()
    if not raw_data_dir:
        return None
    try:
        return (Path(raw_data_dir).parent / "Cache" / "Image").resolve(strict=False)
    except OSError:
        return None


def safe_cache_image_path(cache_root: Path, candidate: Path) -> Path | None:
    """Keep the database path spelling (8.3 aliases) but trust only Cache/Image."""

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
        return display_path
    except (OSError, RuntimeError, ValueError):
        return None


def inspect_image_file(
    path: Path,
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
    source: str = "",
    md5: str = "",
) -> MediaDescriptor:
    """Validate one cache file and produce a descriptor without reading it whole."""

    try:
        stat = path.stat()
    except PermissionError:
        return MediaDescriptor.missing(REASON_PERMISSION, filename=path.name, path=str(path), source=source, md5=md5)
    except OSError as exc:
        return MediaDescriptor.missing(
            REASON_NOT_CACHED, filename=path.name, path=str(path), source=source, md5=md5, detail=str(exc)
        )
    if not path.is_file():
        return MediaDescriptor.missing(REASON_NOT_FOUND, filename=path.name, path=str(path), source=source, md5=md5)
    size = int(stat.st_size)
    if size <= 0:
        return MediaDescriptor.missing(REASON_PARTIAL, filename=path.name, path=str(path), source=source, md5=md5, detail="empty file")
    if size > max_bytes:
        return MediaDescriptor(
            available=False, status="missing", reason=REASON_TOO_LARGE, path=str(path),
            filename=path.name, mime=image_mime(path), size=size, md5=md5,
            source=source, detail=f"{size} bytes > {max_bytes}",
        )
    suffix = path.suffix.lower()
    if suffix not in IMAGE_EXTS:
        return MediaDescriptor.missing(REASON_UNSUPPORTED, filename=path.name, path=str(path), source=source, md5=md5)
    try:
        with path.open("rb") as handle:
            head = handle.read(32)
            tail = b""
            if size > 32:
                handle.seek(max(0, size - 24))
                tail = handle.read(24)
    except PermissionError:
        return MediaDescriptor.missing(REASON_PERMISSION, filename=path.name, path=str(path), source=source, md5=md5)
    except OSError as exc:
        return MediaDescriptor.missing(
            REASON_NOT_CACHED, filename=path.name, path=str(path), source=source, md5=md5, detail=str(exc)
        )
    suffix_mime = image_mime(path)
    if suffix_mime == "image/png":
        complete = head.startswith(b"\x89PNG\r\n\x1a\n") and tail.endswith(_PNG_IEND)
        if not head.startswith(b"\x89PNG\r\n\x1a\n"):
            return MediaDescriptor.missing(REASON_UNSUPPORTED, filename=path.name, path=str(path), source=source, md5=md5)
    elif suffix_mime == "image/jpeg":
        complete = head.startswith(b"\xff\xd8\xff") and tail.endswith(b"\xff\xd9")
        if not head.startswith(b"\xff\xd8\xff"):
            return MediaDescriptor.missing(REASON_UNSUPPORTED, filename=path.name, path=str(path), source=source, md5=md5)
    elif suffix_mime == "image/gif":
        complete = head.startswith((b"GIF87a", b"GIF89a")) and tail.endswith(b";")
        if not head.startswith((b"GIF87a", b"GIF89a")):
            return MediaDescriptor.missing(REASON_UNSUPPORTED, filename=path.name, path=str(path), source=source, md5=md5)
    elif suffix_mime == "image/bmp":
        complete = head.startswith(b"BM") and size >= 26
    else:  # webp
        complete = head.startswith(b"RIFF") and head[8:12] == b"WEBP" and size >= 16
        if not (head.startswith(b"RIFF") and head[8:12] == b"WEBP"):
            return MediaDescriptor.missing(REASON_UNSUPPORTED, filename=path.name, path=str(path), source=source, md5=md5)
    if not complete:
        return MediaDescriptor.missing(REASON_PARTIAL, filename=path.name, path=str(path), source=source, md5=md5)
    return MediaDescriptor.ready(path, source=source, md5=md5, size=size, mime=suffix_mime)


def model_limit_reason(
    descriptors: Sequence[MediaDescriptor | dict],
    *,
    max_images: int = MAX_IMAGES_PER_MESSAGE,
    max_total_bytes: int = MAX_IMAGE_TOTAL_BYTES,
) -> str:
    """Return ``too_many``/``too_large`` for the model boundary, else empty."""

    available = [
        item for item in descriptors
        if (item.available if isinstance(item, MediaDescriptor) else bool(item.get("available")))
    ]
    if len(available) > max_images:
        return REASON_TOO_MANY
    total = 0
    for item in available:
        total += int(item.size if isinstance(item, MediaDescriptor) else item.get("size") or 0)
    if total > max_total_bytes:
        return REASON_TOO_LARGE
    return ""


class MediaResolver:
    """Ordered image locator shared by the daily pipeline and reply runtime."""

    def __init__(
        self,
        config: dict,
        *,
        process_running: bool | None = None,
        max_bytes: int = MAX_IMAGE_BYTES,
    ) -> None:
        self.config = config or {}
        self.process_running = process_running
        self.max_bytes = int(max_bytes)
        self.cache_root = image_cache_root(self.config)
        self._file_resolver: FileResolver | None = None
        self._file_db_error = ""

    # -- preconditions -------------------------------------------------
    def precondition(self) -> Precondition:
        raw_data_dir = str(self.config.get("wxwork_db_dir") or "").strip()
        if not raw_data_dir:
            return Precondition(
                False, REASON_NOT_FOUND, "未配置企业微信数据目录", "请在设置中选择企业微信数据目录",
            )
        data_dir = Path(raw_data_dir)
        try:
            is_dir = data_dir.is_dir()
        except OSError:
            is_dir = False
        if not is_dir:
            return Precondition(
                False, REASON_NOT_FOUND, f"企业微信数据目录不存在：{data_dir}", "请重新选择企业微信数据目录",
            )
        try:
            has_db = any(data_dir.glob("*.db"))
        except OSError:
            has_db = False
        if not has_db:
            return Precondition(
                False, REASON_NOT_FOUND, "企业微信数据目录中未找到数据库文件", "请重新选择企业微信数据目录",
            )
        keys_file = str(self.config.get("wxwork_keys_file") or "").strip()
        if keys_file:
            try:
                keys_ok = Path(keys_file).is_file()
            except OSError:
                keys_ok = False
            if not keys_ok:
                return Precondition(
                    False, REASON_KEYS_MISSING, "未找到企业微信数据库密钥", "请在设置页提取密钥后重试",
                )
        if not self.cache_root:
            return Precondition(
                False, REASON_NOT_FOUND, "未找到企业微信图片缓存目录", "请打开企业微信并保持登录后重试",
            )
        return Precondition(True)

    def wait_blocked_reason(self) -> str:
        """Return a reason that makes polling pointless, or empty."""

        precondition = self.precondition()
        if not precondition.ok:
            return precondition.reason
        if self.process_running is False:
            return REASON_WECOM_NOT_RUNNING
        return ""

    # -- public resolution --------------------------------------------
    def resolve_one(self, request: MediaRequest) -> list[MediaDescriptor]:
        return self.resolve_batch([request]).get(int(request.message_id), [])

    def resolve_batch(
        self,
        requests: Iterable[MediaRequest],
        *,
        wait_seconds: float = 0.0,
        poll_interval: float = 0.5,
        wait_max_age_seconds: float = 900.0,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> dict[int, list[MediaDescriptor]]:
        """Resolve descriptors for many messages, optionally retrying the cache.

        Retries only when the report/reply could plausibly gain the image: the
        message must be recent (``wait_max_age_seconds``) and the unresolved
        descriptors must be ``not_cached``/``partial``/``not_found``.  Local
        preconditions (data dir, keys, WeCom process) stop the polling early so
        callers never burn the whole budget for nothing.
        """

        requests = list(requests)
        if not requests:
            return {}
        resolved = self._resolve_pass(requests)
        if wait_seconds <= 0:
            return self._annotate(resolved, requests)

        deadline = now() + max(0.0, float(wait_seconds))
        blocked = self.wait_blocked_reason()
        pending = self._pending_requests(requests, resolved, wait_max_age_seconds)
        while pending and not blocked:
            remaining = deadline - now()
            if remaining <= 0:
                break
            sleep(min(poll_interval, remaining))
            retried = self._resolve_pass(pending)
            for request in pending:
                message_id = int(request.message_id)
                if message_id in retried:
                    resolved[message_id] = retried[message_id]
            blocked = self.wait_blocked_reason()
            pending = self._pending_requests(pending, resolved, wait_max_age_seconds)
        return self._annotate(resolved, requests)

    def model_limit_reason(self, descriptors: Sequence[MediaDescriptor | dict]) -> str:
        return model_limit_reason(descriptors)

    # -- internals ----------------------------------------------------
    def _file_resolver_instance(self) -> FileResolver | None:
        if self._file_resolver is not None:
            return self._file_resolver
        if self._file_db_error:
            return None
        try:
            self._file_resolver = FileResolver(self.config)
        except (OSError, KeyError, ValueError) as exc:
            self._file_db_error = str(exc)
            return None
        return self._file_resolver

    def _load_file_rows(self, requests: list[MediaRequest]) -> dict[int, list[dict]]:
        resolver = self._file_resolver_instance()
        if resolver is None:
            return {}
        refs_by_message = {
            int(request.message_id): list(request.md5_refs)
            for request in requests
            if request.md5_refs
        }
        result: dict[int, list[dict]] = {}
        for conversation_id in {request.conversation_id for request in requests}:
            group = [r for r in requests if r.conversation_id == conversation_id]
            ids = [int(r.message_id) for r in group]
            try:
                if refs_by_message:
                    rows = resolver.find_files_for_messages(
                        conversation_id, ids, image_md5_refs_by_message=refs_by_message
                    )
                else:
                    rows = resolver.find_files_for_messages(conversation_id, ids)
            except (OSError, KeyError, ValueError, TypeError) as exc:
                self._file_db_error = str(exc)
                continue
            for message_id, infos in rows.items():
                result.setdefault(int(message_id), []).extend(infos)
        return result

    def _scan_hint_paths(self, hints: Sequence[str]) -> list[dict]:
        resolver = self._file_resolver_instance()
        if resolver is None or not hints or self.cache_root is None:
            return []
        try:
            infos = resolver.find_files_by_names(list(hints))
        except (OSError, KeyError, ValueError):
            return []
        result = []
        for info in infos:
            if str(info.get("category") or "") != "Image":
                continue
            candidate = safe_cache_image_path(self.cache_root, Path(str(info.get("source_path") or "")))
            if candidate is None:
                continue
            result.append({**info, "source_path": str(candidate), "source": "cache_scan"})
        return result

    def _resolve_pass(self, requests: list[MediaRequest]) -> dict[int, list[MediaDescriptor]]:
        grouped: dict[int, list[MediaDescriptor]] = {}
        seen_by_message: dict[int, set[str]] = {int(r.message_id): set() for r in requests}
        file_rows = self._load_file_rows(requests)
        resolver = self._file_resolver_instance()

        def add(message_id: int, descriptor: MediaDescriptor) -> None:
            key = os.path.normcase(str(Path(descriptor.path))) if descriptor.path else ""
            if key and key in seen_by_message[message_id]:
                return
            if key:
                seen_by_message[message_id].add(key)
            grouped.setdefault(message_id, []).append(descriptor)

        for request in requests:
            message_id = int(request.message_id)
            # Stage 1: paths embedded in the message DB row.
            for path in extract_local_image_paths(self.config, request.local_extra):
                add(message_id, inspect_image_file(path, max_bytes=self.max_bytes, source="local_extra_content"))
            # Stage 2: file.db rows + CacheMapping (md5 refs cover forwards).
            infos = file_rows.get(message_id, [])
            image_infos = [info for info in infos if str(info.get("category") or "File") == "Image"]
            if image_infos and resolver is not None:
                try:
                    paths = resolver.source_paths_for(image_infos)
                except (OSError, KeyError, ValueError):
                    paths = [None] * len(image_infos)
                for info, path in zip(image_infos, paths):
                    if path is None or not path.is_file():
                        continue
                    safe = safe_cache_image_path(self.cache_root, path) if self.cache_root else path
                    if safe is None:
                        continue
                    add(
                        message_id,
                        inspect_image_file(
                            safe,
                            max_bytes=self.max_bytes,
                            source="file_db",
                            md5=str(info.get("md5") or ""),
                        ),
                    )
            # Stage 3: bounded filename scan of Cache/Image.
            if request.filename_hints:
                seen_names = {
                    descriptor.filename for descriptor in grouped.get(message_id, [])
                }
                pending_hints = [h for h in request.filename_hints if h not in seen_names]
                for info in self._scan_hint_paths(pending_hints):
                    path = Path(str(info.get("source_path") or ""))
                    md5 = str(info.get("md5") or "")
                    add(message_id, inspect_image_file(path, max_bytes=self.max_bytes, source="cache_scan", md5=md5))

        return self._fill_expected(grouped, requests)

    def _has_mapping(self, info: dict) -> bool:
        return bool(info.get("lookup_md5")) or bool(info.get("server_id")) and str(info.get("name") or "").strip() != ""

    def _fill_expected(
        self,
        grouped: dict[int, list[MediaDescriptor]],
        requests: list[MediaRequest],
    ) -> dict[int, list[MediaDescriptor]]:
        result: dict[int, list[MediaDescriptor]] = {}
        for request in requests:
            message_id = int(request.message_id)
            descriptors = list(grouped.get(message_id, []))
            expected = int(request.expected_count or 0)
            expected = max(expected, len(request.md5_refs))
            if expected <= 0 and int(request.content_type or 0) in IMAGE_CONTENT_TYPES:
                expected = 1
            if expected > len(descriptors):
                known_cache_path = bool(extract_local_image_paths(self.config, request.local_extra))
                reason = REASON_NOT_CACHED if known_cache_path else REASON_NOT_FOUND
                for _ in range(expected - len(descriptors)):
                    descriptors.append(
                        MediaDescriptor.missing(reason, filename="", path="", source="local_extra_content" if known_cache_path else "")
                    )
            result[message_id] = descriptors
        return result

    def _pending_requests(
        self,
        requests: list[MediaRequest],
        resolved: dict[int, list[MediaDescriptor]],
        wait_max_age_seconds: float,
    ) -> list[MediaRequest]:
        now_ts = time.time()
        pending = []
        for request in requests:
            if request.send_time and wait_max_age_seconds > 0:
                if now_ts - float(request.send_time) > float(wait_max_age_seconds):
                    continue
            descriptors = resolved.get(int(request.message_id), [])
            retryable = {
                REASON_NOT_CACHED, REASON_PARTIAL, REASON_NOT_FOUND,
            }
            if any(descriptor.reason in retryable for descriptor in descriptors):
                pending.append(request)
        return pending

    def _annotate(
        self,
        resolved: dict[int, list[MediaDescriptor]],
        requests: list[MediaRequest],
    ) -> dict[int, list[MediaDescriptor]]:
        requested = {int(request.message_id) for request in requests}
        blocked = self.wait_blocked_reason()
        result: dict[int, list[MediaDescriptor]] = {}
        for message_id, descriptors in resolved.items():
            if message_id not in requested:
                continue
            annotated = []
            for descriptor in descriptors:
                if descriptor.available or descriptor.reason in {
                    REASON_PARTIAL, REASON_TOO_LARGE, REASON_UNSUPPORTED, REASON_PERMISSION, REASON_TOO_MANY,
                }:
                    annotated.append(descriptor)
                    continue
                if blocked:
                    annotated.append(
                        MediaDescriptor.missing(
                            blocked,
                            filename=descriptor.filename,
                            path=descriptor.path,
                            source=descriptor.source,
                            md5=descriptor.md5,
                        )
                    )
                else:
                    annotated.append(descriptor)
            result[message_id] = annotated
        return result


def descriptors_to_dicts(descriptors: Iterable[MediaDescriptor | dict]) -> list[dict]:
    result = []
    for item in descriptors:
        result.append(item.to_dict() if isinstance(item, MediaDescriptor) else dict(item))
    return result


def missing_summary(descriptors: Iterable[MediaDescriptor | dict]) -> dict:
    """Summarize missing descriptors by reason (used by exports/preview)."""

    reasons: dict[str, int] = {}
    for item in descriptors:
        reason = item.reason if isinstance(item, MediaDescriptor) else str(item.get("reason") or "")
        available = item.available if isinstance(item, MediaDescriptor) else bool(item.get("available"))
        if available or not reason:
            continue
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "reasons": reasons,
        "labels": {reason: REASON_LABELS.get(reason, reason) for reason in reasons},
        "total": sum(reasons.values()),
    }
