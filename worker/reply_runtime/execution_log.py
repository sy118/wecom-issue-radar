from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


MAX_LOG_FILES = 100
MAX_LOG_AGE_SECONDS = 30 * 24 * 60 * 60
MAX_LOG_FILE_BYTES = 20 * 1024 * 1024
MAX_STRING_CHARS = 32_000
MAX_COLLECTION_ITEMS = 100
MAX_NESTING_DEPTH = 8

_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|authorization|cookie|headers?|password|secret|"
    r"(?:access[_-]?|refresh[_-]?|auth[_-]?|mcp[_-]?)?token|"
    r"webhook(?:[_-]?url)?|env(?:ironment)?)(?:$|[_-])",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|key|token|secret|signature|password)=)[^&#\s]+"
)


class AgentExecutionLogManager:
    """Creates bounded, opt-in JSONL traces for one answer-engine run."""

    def __init__(self, config_loader, directory: str | Path) -> None:
        self._config_loader = config_loader
        self.directory = Path(directory).expanduser().resolve()
        self._lock = threading.RLock()

    def start_run(self, metadata: dict) -> "AgentExecutionLogSession":
        try:
            config = self._config_loader() or {}
        except Exception:
            return AgentExecutionLogSession.disabled()
        diagnostics = config.get("diagnostics") if isinstance(config, dict) else None
        enabled = bool(
            isinstance(diagnostics, dict)
            and diagnostics.get("agent_execution_logging") is True
        )
        if not enabled:
            return AgentExecutionLogSession.disabled()

        secrets = _configured_secrets(config)
        try:
            with self._lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                # Reserve one slot for the run that is about to be created so the
                # directory never grows beyond the advertised retention limit.
                self._prune_locked(keep_count=max(0, MAX_LOG_FILES - 1))
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
                work_id = _safe_filename_part(str(metadata.get("workId") or "work"))
                run_id = uuid.uuid4().hex[:12]
                path = self.directory / f"agent-{timestamp}-{work_id}-{run_id}.jsonl"
                handle = path.open("x", encoding="utf-8", newline="\n")
        except OSError:
            return AgentExecutionLogSession.disabled()

        session = AgentExecutionLogSession(handle, path, secrets)
        session.event("run_started", metadata)
        return session

    def _prune_locked(self, *, keep_count: int = MAX_LOG_FILES) -> None:
        now = time.time()
        candidates = []
        for path in self.directory.glob("agent-*.jsonl"):
            try:
                resolved = path.resolve()
                resolved.relative_to(self.directory)
                stat = resolved.stat()
            except (OSError, ValueError):
                continue
            candidates.append((stat.st_mtime, resolved))
        candidates.sort(reverse=True)
        for index, (modified_at, path) in enumerate(candidates):
            if index < keep_count and now - modified_at <= MAX_LOG_AGE_SECONDS:
                continue
            try:
                path.unlink()
            except OSError:
                pass


class AgentExecutionLogSession:
    def __init__(self, handle, path: Path | None, secrets: tuple[str, ...]) -> None:
        self._handle = handle
        self.path = path
        self._secrets = secrets
        self._lock = threading.RLock()
        self._closed = handle is None
        self._bytes_written = 0
        self._truncated = False

    @classmethod
    def disabled(cls) -> "AgentExecutionLogSession":
        return cls(None, None, ())

    @property
    def enabled(self) -> bool:
        return not self._closed and self._handle is not None

    def event(self, kind: str, data: dict | None = None) -> None:
        if not self.enabled:
            return
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": str(kind or "event")[:100],
            "data": _sanitize(data or {}, self._secrets),
        }
        try:
            encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
        except (TypeError, ValueError):
            return
        with self._lock:
            if not self.enabled:
                return
            if self._bytes_written + len(encoded) > MAX_LOG_FILE_BYTES:
                if not self._truncated:
                    self._truncated = True
                    self._write_line(
                        {
                            "timestamp": datetime.now(timezone.utc).isoformat(
                                timespec="milliseconds"
                            ),
                            "event": "log_truncated",
                            "data": {"maxBytes": MAX_LOG_FILE_BYTES},
                        }
                    )
                return
            try:
                self._handle.write(encoded.decode("utf-8"))
                self._handle.flush()
                self._bytes_written += len(encoded)
            except OSError:
                try:
                    self._handle.close()
                except OSError:
                    pass
                self._closed = True

    def close(self, outcome: dict | None = None) -> None:
        with self._lock:
            if not self.enabled:
                return
            self.event("run_finished", outcome or {})
            try:
                self._handle.close()
            except OSError:
                pass
            self._closed = True

    def _write_line(self, payload: dict) -> None:
        try:
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            encoded_size = len(text.encode("utf-8"))
            if self._bytes_written + encoded_size > MAX_LOG_FILE_BYTES:
                return
            self._handle.write(text)
            self._handle.flush()
            self._bytes_written += encoded_size
        except (OSError, TypeError, ValueError):
            pass


def _configured_secrets(value) -> tuple[str, ...]:
    found = set()

    def visit(item, key: str = "", secret_context: bool = False) -> None:
        secret_context = secret_context or _is_sensitive_key(key)
        if isinstance(item, dict):
            for nested_key, nested in item.items():
                visit(nested, str(nested_key), secret_context)
        elif isinstance(item, list):
            for nested in item:
                visit(nested, key, secret_context)
        elif isinstance(item, str) and len(item) >= 6 and secret_context:
            found.add(item)

    visit(value)
    return tuple(sorted(found, key=len, reverse=True))


def _sanitize(value, secrets: tuple[str, ...], depth: int = 0, key: str = ""):
    if _is_sensitive_key(key):
        return "[REDACTED]"
    if depth >= MAX_NESTING_DEPTH:
        return "[TRUNCATED: nesting depth]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = value
        for secret in secrets:
            text = text.replace(secret, "[REDACTED]")
        text = _BEARER_RE.sub("Bearer [REDACTED]", text)
        text = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", text)
        if len(text) > MAX_STRING_CHARS:
            omitted = len(text) - MAX_STRING_CHARS
            text = f"{text[:MAX_STRING_CHARS]}...[TRUNCATED {omitted} chars]"
        return text
    if isinstance(value, dict):
        result = {}
        items = list(value.items())
        for nested_key, nested in items[:MAX_COLLECTION_ITEMS]:
            name = str(nested_key)
            result[name] = _sanitize(nested, secrets, depth + 1, name)
        if len(items) > MAX_COLLECTION_ITEMS:
            result["__truncated_items__"] = len(items) - MAX_COLLECTION_ITEMS
        return result
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        result = [
            _sanitize(item, secrets, depth + 1, key)
            for item in items[:MAX_COLLECTION_ITEMS]
        ]
        if len(items) > MAX_COLLECTION_ITEMS:
            result.append(f"[TRUNCATED {len(items) - MAX_COLLECTION_ITEMS} items]")
        return result
    return _sanitize(str(value), secrets, depth + 1, key)


def _safe_filename_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return (safe or "work")[:80]


def _is_sensitive_key(value: str) -> bool:
    # Normalize camelCase so apiKey/accessToken are protected while ordinary
    # model controls such as maxTokens remain inspectable.
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value or ""))
    return bool(_SENSITIVE_KEY_RE.search(normalized))
