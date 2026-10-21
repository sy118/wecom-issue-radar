"""Local to-do store for the daily assistant (``daily_todos.json``).

Kept separate from config so a chat/data reset does not wipe user decisions.
Writes are atomic (temp file + replace) and a corrupt file degrades to an empty
store rather than breaking the UI.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

from worker.search.sources import SearchPaths
from worker.search.text import now_iso

STORE_VERSION = 1
MAX_PROCESSED_IDS = 4000
MAX_CANDIDATES = 200
MAX_TODOS = 2000

TODO_FIELDS = (
    "id",
    "title",
    "due",
    "source",
    "convName",
    "senderName",
    "createdAt",
    "done",
    "confidence",
    "msgId",
    "convId",
    "direction",
    "intent",
    "pending",
)


def store_path(paths: SearchPaths) -> Path:
    return paths.todos_path()


def empty_store() -> dict:
    return {"version": STORE_VERSION, "todos": [], "candidates": [], "meta": {}}


def load_store(paths: SearchPaths) -> dict:
    path = store_path(paths)
    if not path.is_file():
        return empty_store()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_store()
    if not isinstance(raw, dict):
        return empty_store()
    data = empty_store()
    for key in ("todos", "candidates"):
        value = raw.get(key)
        if isinstance(value, list):
            data[key] = [item for item in value if isinstance(item, dict)]
    meta = raw.get("meta")
    if isinstance(meta, dict):
        data["meta"] = copy.deepcopy(meta)
    return data


def save_store(paths: SearchPaths, data: dict) -> None:
    path = store_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as file:
        file.write(content)
    temp_path.replace(path)


def _digest(*parts: object) -> str:
    text = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def normalize_title(value: object) -> str:
    text = re.sub(r"\s+", "", str(value or "")).casefold()
    return re.sub(r"[，。！？、；;,.!?：:（）()【】\[\]“”\"'`]+", "", text)


def build_record(candidate: dict, *, pending: bool) -> dict:
    title = str(candidate.get("title") or "").strip()
    record = {
        "id": (
            f"cand_{_digest(title, candidate.get('source'), candidate.get('msgId'))}"
            if pending
            else f"todo_{_digest(title, candidate.get('source'), candidate.get('senderName'))}"
        ),
        "title": title,
        "due": str(candidate.get("due") or ""),
        "source": str(candidate.get("source") or ""),
        "convName": str(candidate.get("convName") or ""),
        "senderName": str(candidate.get("senderName") or ""),
        "createdAt": str(candidate.get("createdAt") or now_iso()),
        "done": False,
        "confidence": round(float(candidate.get("confidence") or 0.0), 2),
        "msgId": str(candidate.get("msgId") or ""),
        "convId": str(candidate.get("convId") or ""),
        "direction": str(candidate.get("direction") or ""),
        "intent": str(candidate.get("intent") or ""),
        "updatedAt": now_iso(),
    }
    if pending:
        record["pending"] = True
    return record


def _find_by_title(data: dict, title: str) -> dict | None:
    normalized = normalize_title(title)
    if not normalized:
        return None
    for key in ("todos", "candidates"):
        for item in data.get(key) or []:
            if normalize_title(item.get("title")) == normalized:
                return item
    return None


def add_todo(data: dict, candidate: dict) -> dict | None:
    if _find_by_title(data, candidate.get("title", "")):
        return None
    record = build_record(candidate, pending=False)
    data.setdefault("todos", []).insert(0, record)
    data["todos"] = data["todos"][:MAX_TODOS]
    return record


def add_candidate(data: dict, candidate: dict) -> dict | None:
    if _find_by_title(data, candidate.get("title", "")):
        return None
    record = build_record(candidate, pending=True)
    data.setdefault("candidates", []).insert(0, record)
    data["candidates"] = data["candidates"][:MAX_CANDIDATES]
    return record


def sorted_items(items: list[dict]) -> list[dict]:
    def sort_key(item: dict):
        return (
            str(item.get("due") or "9999-12-31"),
            -float(item.get("confidence") or 0.0),
            str(item.get("createdAt") or ""),
        )

    return sorted(items, key=sort_key)


def _public(item: dict) -> dict:
    record = {field: item.get(field) for field in TODO_FIELDS if field in item}
    record["done"] = bool(item.get("done"))
    record["confidence"] = float(item.get("confidence") or 0.0)
    if "pending" in item:
        record["pending"] = bool(item.get("pending"))
    return record


def list_items(data: dict, status: str | None = None) -> list[dict]:
    status_name = str(status or "active").strip().lower()
    todos = list(data.get("todos") or [])
    candidates = list(data.get("candidates") or [])
    if status_name in {"pending", "candidate", "candidates", "confirm"}:
        return [_public(item) for item in sorted_items(candidates)]
    if status_name in {"done", "completed", "finished"}:
        return [_public(item) for item in sorted_items([i for i in todos if i.get("done")])]
    if status_name in {"all", "*"}:
        return [_public(item) for item in sorted_items(todos + candidates)]
    if status_name in {"active", "open", "todo", "todos", ""}:
        return [_public(item) for item in sorted_items([i for i in todos if not i.get("done")])]
    raise ValueError(f"未知的待办状态：{status}")


def update_item(data: dict, item_id: str, done: bool) -> dict | None:
    for index, item in enumerate(data.get("todos") or []):
        if str(item.get("id") or "") == item_id:
            item["done"] = bool(done)
            item["updatedAt"] = now_iso()
            data["todos"][index] = item
            return _public(item)
    for index, item in enumerate(list(data.get("candidates") or [])):
        if str(item.get("id") or "") != item_id:
            continue
        candidate = data["candidates"].pop(index)
        candidate.pop("pending", None)
        candidate["id"] = "todo_" + hashlib.sha1(
            f"{candidate.get('title')}|confirm".encode("utf-8")
        ).hexdigest()[:12]
        candidate["done"] = bool(done)
        candidate["updatedAt"] = now_iso()
        data.setdefault("todos", []).insert(0, candidate)
        return _public(candidate)
    return None


def processed_ids(data: dict) -> set[str]:
    meta = data.get("meta") or {}
    values = meta.get("processed") or []
    return {str(value) for value in values if value}


def mark_processed(data: dict, msg_ids: list[str]) -> None:
    meta = data.setdefault("meta", {})
    existing = list(meta.get("processed") or [])
    seen = set(map(str, existing))
    for msg_id in msg_ids:
        if msg_id and msg_id not in seen:
            existing.append(msg_id)
            seen.add(msg_id)
    if len(existing) > MAX_PROCESSED_IDS:
        existing = existing[-MAX_PROCESSED_IDS:]
    meta["processed"] = existing
    meta["lastRefreshAt"] = now_iso()
