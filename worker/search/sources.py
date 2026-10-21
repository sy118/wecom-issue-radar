"""Read-only chat sources for the local search index.

Sources:
* WeCom export: ``<default_workspace>/**/raw_messages.jsonl`` written by
  ``worker/wecom/cache_messages.py``.
* Personal WeChat: ``<data_root>/wechat.sqlite3`` using the ProxyTalk store
  schema (``projects`` / ``messages``). The database is opened read-only and a
  missing table or column is treated as "no data" instead of an error.

Nothing in this module writes to the source databases.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .text import (
    normalize_content_type,
    normalize_kind,
    to_epoch_seconds,
)

WECOM_MSG_PREFIX = "wecom"
WECHAT_MSG_PREFIX = "wechat"
MAX_WECOM_FILES = 2000


@dataclass(frozen=True)
class SearchPaths:
    data_root: Path
    index_path: Path
    wechat_db_path: Path
    workspace_root: Path | None
    timezone: str = "Asia/Shanghai"

    def todos_path(self) -> Path:
        return self.data_root / "daily_todos.json"


def resolve_paths(config: dict | None = None) -> SearchPaths:
    """Resolve index/database/workspace paths.

    ``WECOM_ISSUE_RADAR_CONFIG`` (honoured by config_store) also redirects the
    data root, which keeps tests and portable installs self-contained.
    """
    from worker.pipeline.config_store import default_config_path

    config_path = default_config_path()
    data_root = config_path.parent
    config = config or {}
    workspace = str(config.get("default_workspace") or "").strip()
    workspace_root = Path(workspace).expanduser().resolve() if workspace else None
    return SearchPaths(
        data_root=data_root,
        index_path=data_root / "chat_search_index.db",
        wechat_db_path=data_root / "wechat.sqlite3",
        workspace_root=workspace_root,
        timezone=str(config.get("timezone") or "Asia/Shanghai"),
    )


def list_wecom_files(paths: SearchPaths) -> list[Path]:
    if paths.workspace_root is None or not paths.workspace_root.is_dir():
        return []
    files: list[Path] = []
    try:
        for candidate in paths.workspace_root.rglob("raw_messages.jsonl"):
            if candidate.is_file():
                files.append(candidate)
                if len(files) >= MAX_WECOM_FILES:
                    break
    except OSError:
        return files
    files.sort(key=lambda item: str(item))
    return files


def wecom_file_signature(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return ""
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _clean_wecom_text(record: dict) -> str:
    raw = str(record.get("raw_text") or "").strip()
    if not raw:
        return ""
    kind_label = str(record.get("type") or "").strip()
    if kind_label:
        marker = f"[{kind_label}]"
        index = raw.find(marker)
        if index >= 0:
            raw = raw[index + len(marker) :].strip()
    # ``raw_text`` starts with "sender time"; drop that prefix when the record
    # carries the sender so the indexed body is the actual message content.
    sender = str(record.get("sender") or "").strip()
    if sender and raw.startswith(sender):
        raw = raw[len(sender) :].strip()
    return raw


def _wecom_msg_id(record: dict, conversation_id: str) -> str:
    dedupe_key = str(record.get("dedupe_key") or "").strip()
    if dedupe_key:
        return f"{WECOM_MSG_PREFIX}:{dedupe_key}"
    message_id = record.get("message_id")
    server_id = record.get("server_id", 0)
    send_time = record.get("send_time", 0)
    return (
        f"{WECOM_MSG_PREFIX}:{conversation_id}:{message_id}:"
        f"{server_id}:{send_time}"
    )


def wecom_record_to_message(record: dict) -> dict | None:
    if not isinstance(record, dict):
        return None
    conversation_id = str(record.get("conversation_id") or "").strip()
    if not conversation_id:
        return None
    send_time = to_epoch_seconds(record.get("send_time"))
    kind = normalize_kind(record.get("type"))
    if kind == "other":
        kind = normalize_content_type(record.get("content_type")) or "other"
    msg_id = _wecom_msg_id(record, conversation_id)
    return {
        "source": WECOM_MSG_PREFIX,
        "msg_id": msg_id,
        "source_key": str(record.get("message_id") or msg_id),
        "conv_id": conversation_id,
        "conv_name": str(record.get("conversation_name") or conversation_id),
        "kind": kind,
        "sender_name": str(record.get("sender") or ""),
        "direction": "",
        "text": _clean_wecom_text(record),
        "ts": send_time,
    }


def iter_wecom_file_messages(path: Path) -> Iterator[dict]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = wecom_record_to_message(record)
                if message is not None:
                    yield message
    except OSError:
        return


def _readonly_connection(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        uri = path.as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        return connection
    except (OSError, sqlite3.Error):
        return None


def wechat_table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row["name"]) for row in rows}


def wechat_tables(connection: sqlite3.Connection) -> set[str]:
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    except sqlite3.Error:
        return set()
    return {str(row["name"]) for row in rows}


def load_wechat_projects(connection: sqlite3.Connection) -> dict[str, dict]:
    tables = wechat_tables(connection)
    if "projects" not in tables:
        return {}
    columns = wechat_table_columns(connection, "projects")
    if "id" not in columns:
        return {}
    names = [
        column
        for column in ("id", "name", "chat_title")
        if column in columns
    ]
    try:
        rows = connection.execute(
            f"SELECT {', '.join(names)} FROM projects"
        ).fetchall()
    except sqlite3.Error:
        return {}
    projects: dict[str, dict] = {}
    for row in rows:
        project_id = str(row["id"] or "")
        if not project_id:
            continue
        projects[project_id] = {
            "name": str(row["name"] or "") if "name" in names else "",
            "chat_title": str(row["chat_title"] or "") if "chat_title" in names else "",
        }
    return projects


def _wechat_row_to_message(
    row: sqlite3.Row, project: dict, project_id: str
) -> dict | None:
    keys = row.keys()
    source_key = str(row["source_key"] or "") if "source_key" in keys else ""
    create_time = to_epoch_seconds(row["create_time"] if "create_time" in keys else 0)
    if not create_time:
        return None
    conv_name = str(project.get("chat_title") or project.get("name") or project_id)
    text = str((row["text"] if "text" in keys else "") or "")
    kind = normalize_kind(row["kind"] if "kind" in keys else "")
    direction = str((row["direction"] if "direction" in keys else "") or "")
    sender_name = str((row["sender_name"] if "sender_name" in keys else "") or "")
    stable_key = source_key or str(row["_rowid"] if "_rowid" in keys else create_time)
    return {
        "source": WECHAT_MSG_PREFIX,
        "msg_id": f"{WECHAT_MSG_PREFIX}:{project_id}:{stable_key}",
        "source_key": stable_key,
        "conv_id": project_id,
        "conv_name": conv_name,
        "kind": kind,
        "sender_name": sender_name,
        "direction": direction,
        "text": text,
        "ts": create_time,
    }


def iter_wechat_message_batches(
    paths: SearchPaths,
    cursors: dict[str, int],
    *,
    batch_size: int = 2000,
) -> Iterator[tuple[str, list[dict], int]]:
    """Yield ``(project_id, messages, last_rowid)`` for incremental indexing."""
    connection = _readonly_connection(paths.wechat_db_path)
    if connection is None:
        return
    try:
        if "messages" not in wechat_tables(connection):
            return
        columns = wechat_table_columns(connection, "messages")
        if "project_id" not in columns:
            return
        projects = load_wechat_projects(connection)
        try:
            project_rows = connection.execute(
                "SELECT DISTINCT project_id FROM messages"
            ).fetchall()
        except sqlite3.Error:
            return
        for project_row in project_rows:
            project_id = str(project_row["project_id"] or "")
            if not project_id:
                continue
            project = projects.get(project_id, {})
            last_rowid = int(cursors.get(project_id, 0) or 0)
            while True:
                try:
                    rows = connection.execute(
                        "SELECT rowid AS _rowid, * FROM messages "
                        "WHERE project_id=? AND rowid>? ORDER BY rowid LIMIT ?",
                        (project_id, last_rowid, batch_size),
                    ).fetchall()
                except sqlite3.Error:
                    break
                if not rows:
                    break
                messages = []
                for row in rows:
                    message = _wechat_row_to_message(row, project, project_id)
                    if message is not None:
                        messages.append(message)
                last_rowid = int(rows[-1]["_rowid"])
                yield project_id, messages, last_rowid
                if len(rows) < batch_size:
                    break
    finally:
        connection.close()


def iter_wechat_messages(paths: SearchPaths, *, limit: int = 200_000) -> Iterator[dict]:
    """Yield every personal WeChat message (fallback scan when the index is bad)."""
    connection = _readonly_connection(paths.wechat_db_path)
    if connection is None:
        return
    try:
        if "messages" not in wechat_tables(connection):
            return
        projects = load_wechat_projects(connection)
        try:
            rows = connection.execute(
                "SELECT rowid AS _rowid, * FROM messages ORDER BY rowid LIMIT ?",
                (int(limit),),
            )
        except sqlite3.Error:
            return
        for row in rows:
            project_id = str(row["project_id"] or "") if "project_id" in row.keys() else ""
            if not project_id:
                continue
            message = _wechat_row_to_message(
                row, projects.get(project_id, {}), project_id
            )
            if message is not None:
                yield message
    finally:
        connection.close()


def iter_source_messages(paths: SearchPaths, *, limit: int = 200_000) -> Iterator[dict]:
    """Yield every message from every source (used by degraded/fallback queries)."""
    yielded = 0
    for file_path in list_wecom_files(paths):
        for message in iter_wecom_file_messages(file_path):
            yield message
            yielded += 1
            if yielded >= limit:
                return
    for message in iter_wechat_messages(paths, limit=max(0, limit - yielded)):
        yield message
