from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable

from .events import event_role, timeline_metadata


TIMELINE_RETENTION_SECONDS = 30 * 24 * 60 * 60
MAX_TIMELINE_DETAIL_CHARS = 64_000


class AgentTimelineRecorder:
    """Persists an always-on metadata timeline and optional diagnostic detail."""

    def __init__(
        self,
        store,
        *,
        work_id: str,
        work_generation: int,
        detailed: bool,
        redact: Callable[[object], object],
        wall_clock: Callable[[], float],
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        self._store = store
        self.work_id = str(work_id)
        self.work_generation = int(work_generation)
        self.detailed = bool(detailed)
        self._redact = redact
        self._wall_clock = wall_clock
        self._monotonic = monotonic_clock or time.monotonic
        try:
            with self._store.lock:
                self._sequence = int(
                    self._store.connection.execute(
                        """SELECT coalesce(max(sequence),0) FROM agent_timeline_events
                           WHERE work_id=? AND work_generation=?""",
                        (self.work_id, self.work_generation),
                    ).fetchone()[0]
                )
        except Exception:
            self._sequence = 0
        self._started: dict[tuple, float] = {}
        self._lock = threading.RLock()

    def event(self, kind: str, data: dict | None = None) -> None:
        value = self._redact(data or {})
        if not isinstance(value, dict):
            value = {}
        metadata = timeline_metadata(kind, value)
        duration_ms = self._duration(kind, value)
        if duration_ms is not None:
            metadata["durationMs"] = duration_ms
        detail = _bounded_detail(value) if self.detailed else None
        now = float(self._wall_clock())
        with self._lock:
            self._sequence += 1
            with self._store.transaction() as db:
                db.execute(
                    """INSERT OR REPLACE INTO agent_timeline_events(
                           work_id,work_generation,sequence,role,kind,metadata_json,detail_json,created_at
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        self.work_id,
                        self.work_generation,
                        self._sequence,
                        event_role(kind),
                        str(kind or "event")[:100],
                        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                        (
                            json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
                            if detail is not None
                            else None
                        ),
                        now,
                    ),
                )
                if self._sequence == 1:
                    db.execute(
                        "DELETE FROM agent_timeline_events WHERE created_at<?",
                        (now - TIMELINE_RETENTION_SECONDS,),
                    )

    def _duration(self, kind: str, data: dict) -> int | None:
        suffixes = ("_started", "_completed", "_failed", "_finished")
        suffix = next((item for item in suffixes if kind.endswith(item)), "")
        if not suffix:
            return None
        stem = kind[: -len(suffix)]
        key = (
            stem,
            data.get("round"),
            data.get("attempt"),
            data.get("serverId"),
            data.get("toolName"),
        )
        now = self._monotonic()
        if suffix == "_started":
            self._started[key] = now
            return None
        started = self._started.pop(key, None)
        if started is None:
            # Some events use agent_started/agent_finished without identical data.
            candidates = [item for item in self._started if item[0] == stem]
            if len(candidates) == 1:
                started = self._started.pop(candidates[0])
        if started is None:
            return None
        return max(0, int(round((now - started) * 1000)))


def _bounded_detail(value: dict) -> dict:
    try:
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return {"value": str(value)[:MAX_TIMELINE_DETAIL_CHARS]}
    if len(rendered) <= MAX_TIMELINE_DETAIL_CHARS:
        return value
    return {
        "truncated": True,
        "originalChars": len(rendered),
        "preview": rendered[:MAX_TIMELINE_DETAIL_CHARS],
    }
