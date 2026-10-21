"""Local FTS5 chat search index.

Layout (``~/.wecom-issue-radar/chat_search_index.db``)::

    msg(id, msg_id UNIQUE, source, source_key, conv_id, conv_name, kind,
        sender_name, direction, text, ts)
    msg_fts(body)                 -- FTS5, CJK stored as "character + space"
    index_cursor(source, conv_id, cursor)
    meta(key, value)

Only message text and metadata are stored; media paths and logs never enter the
index. A rebuild is written to a temp database and atomically swapped in, so a
half-written index never replaces a working one.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Iterable

from .sources import (
    SearchPaths,
    iter_source_messages,
    iter_wecom_file_messages,
    iter_wechat_message_batches,
    list_wecom_files,
    wecom_file_signature,
)
from .text import (
    build_match_query,
    now_iso,
    normalize_kind,
    normalize_source,
    parse_time_bound,
    space_cjk,
)

INDEX_SCHEMA_VERSION = "1"
BUILD_LOCK = threading.Lock()

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS msg (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    conv_id TEXT NOT NULL,
    conv_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    sender_name TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_source_ts ON msg(source, ts DESC);
CREATE INDEX IF NOT EXISTS idx_msg_conv_ts ON msg(source, conv_id, ts);
CREATE INDEX IF NOT EXISTS idx_msg_ts ON msg(ts DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS msg_fts USING fts5(body, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS index_cursor (
    source TEXT NOT NULL,
    conv_id TEXT NOT NULL,
    cursor TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(source, conv_id)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_REQUIRED_TABLES = {"msg", "msg_fts", "index_cursor", "meta"}
VALID_LIMIT_MAX = 200
CJK_QUERY_MAX_CHARS = 4000


class IndexUnavailable(RuntimeError):
    """The index file is missing, damaged or cannot be read."""


def _connect(path: Path, *, create: bool = False) -> sqlite3.Connection | None:
    if not path.exists():
        if not create:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(str(path), timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=DELETE")
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    except sqlite3.Error as exc:
        try:
            connection.close()
        except (UnboundLocalError, sqlite3.Error):
            pass
        raise IndexUnavailable(f"索引数据库无法打开，请重建索引：{exc}") from exc
    if not _REQUIRED_TABLES <= tables:
        if not create:
            connection.close()
            raise IndexUnavailable("索引结构缺失或损坏，请重建索引")
        try:
            connection.executescript(_CREATE_SQL)
            connection.commit()
        except sqlite3.Error as exc:
            connection.close()
            raise IndexUnavailable(f"索引数据库初始化失败：{exc}") from exc
    return connection


def _meta_get(connection: sqlite3.Connection, key: str) -> str:
    try:
        row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return ""
    return str(row["value"] or "") if row else ""


def _meta_set(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _message_count(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute("SELECT COUNT(*) AS n FROM msg").fetchone()
    except sqlite3.Error:
        return 0
    return int(row["n"] or 0) if row else 0


def _upsert_batch(connection: sqlite3.Connection, messages: Iterable[dict]) -> int:
    changed = 0
    for message in messages:
        msg_id = str(message.get("msg_id") or "").strip()
        if not msg_id:
            continue
        body = space_cjk(str(message.get("text") or ""))[:CJK_QUERY_MAX_CHARS]
        existing = connection.execute(
            "SELECT id FROM msg WHERE msg_id=?", (msg_id,)
        ).fetchone()
        if existing is None:
            cursor = connection.execute(
                "INSERT INTO msg(msg_id,source,source_key,conv_id,conv_name,kind,"
                "sender_name,direction,text,ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    msg_id,
                    str(message.get("source") or ""),
                    str(message.get("source_key") or ""),
                    str(message.get("conv_id") or ""),
                    str(message.get("conv_name") or ""),
                    str(message.get("kind") or "other"),
                    str(message.get("sender_name") or ""),
                    str(message.get("direction") or ""),
                    str(message.get("text") or ""),
                    int(message.get("ts") or 0),
                ),
            )
            rowid = int(cursor.lastrowid or 0)
        else:
            rowid = int(existing["id"])
            connection.execute(
                "UPDATE msg SET source=?,source_key=?,conv_id=?,conv_name=?,kind=?,"
                "sender_name=?,direction=?,text=?,ts=? WHERE id=?",
                (
                    str(message.get("source") or ""),
                    str(message.get("source_key") or ""),
                    str(message.get("conv_id") or ""),
                    str(message.get("conv_name") or ""),
                    str(message.get("kind") or "other"),
                    str(message.get("sender_name") or ""),
                    str(message.get("direction") or ""),
                    str(message.get("text") or ""),
                    int(message.get("ts") or 0),
                    rowid,
                ),
            )
            connection.execute("DELETE FROM msg_fts WHERE rowid=?", (rowid,))
        try:
            connection.execute(
                "INSERT INTO msg_fts(rowid, body) VALUES(?,?)", (rowid, body)
            )
        except sqlite3.Error:
            # A malformed body must never abort the whole indexing pass.
            pass
        changed += 1
    return changed


def _set_cursor(
    connection: sqlite3.Connection, source: str, conv_id: str, cursor: str
) -> None:
    connection.execute(
        "INSERT INTO index_cursor(source,conv_id,cursor,updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(source,conv_id) DO UPDATE SET cursor=excluded.cursor,"
        "updated_at=excluded.updated_at",
        (source, conv_id, cursor, now_iso()),
    )


def _get_cursor(connection: sqlite3.Connection, source: str, conv_id: str) -> str:
    try:
        row = connection.execute(
            "SELECT cursor FROM index_cursor WHERE source=? AND conv_id=?",
            (source, conv_id),
        ).fetchone()
    except sqlite3.Error:
        return ""
    return str(row["cursor"] or "") if row else ""


def _index_wecom_files(
    connection: sqlite3.Connection,
    paths: SearchPaths,
    *,
    use_cursors: bool,
) -> int:
    changed = 0
    for file_path in list_wecom_files(paths):
        signature = wecom_file_signature(file_path)
        cursor_key = str(file_path)
        with connection:
            if use_cursors:
                previous = _get_cursor(connection, "wecom_file", cursor_key)
                if previous and previous == signature:
                    continue
            messages = list(iter_wecom_file_messages(file_path))
            changed += _upsert_batch(connection, messages)
            _set_cursor(connection, "wecom_file", cursor_key, signature)
    return changed


def _index_wechat(
    connection: sqlite3.Connection,
    paths: SearchPaths,
    *,
    use_cursors: bool,
) -> int:
    cursors: dict[str, int] = {}
    if use_cursors:
        try:
            rows = connection.execute(
                "SELECT conv_id, cursor FROM index_cursor WHERE source='wechat'"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for row in rows:
            try:
                cursors[str(row["conv_id"])] = int(str(row["cursor"]) or 0)
            except (TypeError, ValueError):
                cursors[str(row["conv_id"])] = 0
    changed = 0
    for project_id, messages, last_rowid in iter_wechat_message_batches(paths, cursors):
        with connection:
            changed += _upsert_batch(connection, messages)
            _set_cursor(connection, "wechat", project_id, str(last_rowid))
    return changed


def refresh(paths: SearchPaths) -> dict:
    """Incrementally index changed WeCom files and new WeChat messages."""
    connection = _connect(paths.index_path, create=True)
    if connection is None:
        raise IndexUnavailable("索引数据库不存在")
    try:
        changed = _index_wecom_files(connection, paths, use_cursors=True)
        changed += _index_wechat(connection, paths, use_cursors=True)
        if changed:
            with connection:
                _meta_set(connection, "last_indexed_at", now_iso())
        try:
            with connection:
                _meta_set(connection, "message_count", str(_message_count(connection)))
                _meta_set(connection, "schema_version", INDEX_SCHEMA_VERSION)
        except sqlite3.Error:
            pass
        return {"indexed": _message_count(connection), "updated": changed}
    finally:
        connection.close()


def rebuild(paths: SearchPaths) -> dict:
    """Rebuild the whole index into a temp database and atomically swap it in."""
    with BUILD_LOCK:
        temp_path = paths.index_path.with_name(paths.index_path.name + ".rebuild")
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError as exc:
            raise IndexUnavailable(f"无法清理临时索引：{exc}") from exc
        connection = _connect(temp_path, create=True)
        if connection is None:
            raise IndexUnavailable("无法创建索引数据库")
        try:
            changed = _index_wecom_files(connection, paths, use_cursors=False)
            changed += _index_wechat(connection, paths, use_cursors=False)
            with connection:
                _meta_set(connection, "schema_version", INDEX_SCHEMA_VERSION)
                _meta_set(connection, "last_indexed_at", now_iso())
                _meta_set(connection, "message_count", str(_message_count(connection)))
            indexed = _message_count(connection)
        finally:
            connection.close()
        try:
            os.replace(temp_path, paths.index_path)
        except OSError as exc:
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise IndexUnavailable(f"无法替换索引数据库：{exc}") from exc
        return {"indexed": indexed, "updated": changed}


def ensure_ready(paths: SearchPaths) -> None:
    """Make sure a usable index exists and includes the latest source data."""
    if not paths.index_path.exists():
        rebuild(paths)
        return
    connection = _connect(paths.index_path)
    if connection is None:
        raise IndexUnavailable("索引数据库不存在")
    connection.close()
    refresh(paths)


def status(paths: SearchPaths) -> dict:
    result = {
        "indexed": 0,
        "total": 0,
        "building": False,
        "progress": 0.0,
        "lastIndexedAt": "",
        "error": "",
    }
    try:
        ensure_ready(paths)
        connection = _connect(paths.index_path)
        if connection is None:
            result["error"] = "索引数据库不存在，请重建索引"
            return result
        try:
            indexed = _message_count(connection)
            result["lastIndexedAt"] = _meta_get(connection, "last_indexed_at")
        finally:
            connection.close()
        result["indexed"] = indexed
        result["total"] = indexed
        result["progress"] = 1.0 if indexed else 0.0
        return result
    except IndexUnavailable as exc:
        result["error"] = str(exc)
        return result
    except (OSError, sqlite3.Error) as exc:
        result["error"] = f"索引不可用：{exc}"
        return result


def _clamp_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _filter_values(value: object) -> list[str]:
    if isinstance(value, list):
        raw = [str(item) for item in value]
    elif isinstance(value, str):
        raw = [part for part in value.replace(",", " ").split() if part]
    else:
        raw = []
    return [item.strip() for item in raw if item.strip()]


def _normalize_kind_filter(kinds: list[str]) -> list[str]:
    normalized = []
    for kind in kinds:
        canonical = normalize_kind(kind)
        if canonical not in normalized:
            normalized.append(canonical)
    return normalized


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _row_to_item(row: sqlite3.Row) -> dict:
    return {
        "id": str(row["msg_id"]),
        "source": str(row["source"]),
        "convId": str(row["conv_id"]),
        "convName": str(row["conv_name"]),
        "kind": str(row["kind"]),
        "senderName": str(row["sender_name"]),
        "text": str(row["text"]),
        "ts": int(row["ts"]),
        "msgId": str(row["source_key"]),
    }


def _row_to_message(row: sqlite3.Row) -> dict:
    item = _row_to_item(row)
    return {
        "source": item["source"],
        "msg_id": item["id"],
        "source_key": item["msgId"],
        "conv_id": item["convId"],
        "conv_name": item["convName"],
        "kind": item["kind"],
        "sender_name": item["senderName"],
        "direction": str(row["direction"]),
        "text": item["text"],
        "ts": item["ts"],
    }


def _parse_query(body: dict, tz_name: object) -> dict:
    query_text = str(body.get("query") or "").strip()
    source = normalize_source(body.get("source"))
    sources = [] if source in {"", "all"} else [source]
    kinds = _normalize_kind_filter(_filter_values(body.get("kinds")))
    sender = str(body.get("sender") or "").strip()
    from_ts = parse_time_bound(body.get("from"), tz_name)
    to_ts = parse_time_bound(body.get("to"), tz_name, end_of_day=True)
    limit = _clamp_int(body.get("limit"), 50, 1, VALID_LIMIT_MAX)
    offset = _clamp_int(body.get("offset"), 0, 0, 100_000)
    return {
        "query": query_text,
        "sources": sources,
        "kinds": kinds,
        "sender": sender,
        "from_ts": from_ts,
        "to_ts": to_ts,
        "limit": limit,
        "offset": offset,
    }


def _query_sql(
    connection: sqlite3.Connection, params: dict
) -> tuple[list[dict], int]:
    where = ["1=1"]
    values: list = []
    if params["sources"]:
        where.append(f"m.source IN ({','.join('?' for _ in params['sources'])})")
        values.extend(params["sources"])
    if params["kinds"]:
        where.append(f"m.kind IN ({','.join('?' for _ in params['kinds'])})")
        values.extend(params["kinds"])
    if params["sender"]:
        where.append("LOWER(m.sender_name) LIKE ? ESCAPE '\\'")
        values.append(f"%{_like_escape(params['sender'].lower())}%")
    if params["from_ts"] is not None:
        where.append("m.ts>=?")
        values.append(int(params["from_ts"]))
    if params["to_ts"] is not None:
        where.append("m.ts<=?")
        values.append(int(params["to_ts"]))
    match = build_match_query(params["query"])
    if match:
        where.append("m.id IN (SELECT rowid FROM msg_fts WHERE msg_fts MATCH ?)")
        values.append(match)
    clause = " AND ".join(where)
    count_row = connection.execute(
        f"SELECT COUNT(*) AS n FROM msg m WHERE {clause}", values
    ).fetchone()
    total = int(count_row["n"] or 0) if count_row else 0
    rows = connection.execute(
        f"SELECT m.* FROM msg m WHERE {clause} "
        "ORDER BY m.ts DESC, m.id DESC LIMIT ? OFFSET ?",
        [*values, params["limit"], params["offset"]],
    ).fetchall()
    return [_row_to_item(row) for row in rows], total


def _fallback_match(text: str, query: str) -> bool:
    if not query:
        return True
    haystack = text.casefold()
    return all(part.casefold() in haystack for part in query.split() if part)


def _fallback_query(paths: SearchPaths, params: dict) -> tuple[list[dict], int]:
    matched: dict[str, dict] = {}
    for message in iter_source_messages(paths):
        if params["sources"] and message["source"] not in params["sources"]:
            continue
        if params["kinds"] and message["kind"] not in params["kinds"]:
            continue
        if params["sender"] and params["sender"].casefold() not in str(
            message["sender_name"]
        ).casefold():
            continue
        if params["from_ts"] is not None and message["ts"] < params["from_ts"]:
            continue
        if params["to_ts"] is not None and message["ts"] > params["to_ts"]:
            continue
        if not _fallback_match(str(message["text"]), params["query"]):
            continue
        msg_id = str(message["msg_id"])
        if msg_id in matched:
            continue
        matched[msg_id] = {
            "id": msg_id,
            "source": message["source"],
            "convId": message["conv_id"],
            "convName": message["conv_name"],
            "kind": message["kind"],
            "senderName": message["sender_name"],
            "text": message["text"],
            "ts": message["ts"],
            "msgId": message["source_key"],
        }
    items = sorted(matched.values(), key=lambda item: (-item["ts"], item["id"]))
    total = len(items)
    start = params["offset"]
    return items[start : start + params["limit"]], total


def query(paths: SearchPaths, body: dict | None = None) -> dict:
    body = body or {}
    params = _parse_query(body, paths.timezone)
    try:
        ensure_ready(paths)
        connection = _connect(paths.index_path)
        if connection is None:
            raise IndexUnavailable("索引数据库不存在")
        try:
            items, total = _query_sql(connection, params)
        finally:
            connection.close()
        return {"items": items, "total": total}
    except IndexUnavailable:
        items, total = _fallback_query(paths, params)
        return {"items": items, "total": total}


def _context_items(messages: list[dict]) -> list[dict]:
    return [
        {
            "id": message["msg_id"],
            "senderName": message["sender_name"],
            "text": message["text"],
            "ts": message["ts"],
            "direction": message["direction"],
        }
        for message in messages
    ]


def _fallback_context(paths: SearchPaths, msg_id: str, radius: int) -> list[dict]:
    all_messages: list[dict] = []
    target_index = -1
    for message in iter_source_messages(paths):
        if message["msg_id"] == msg_id:
            target_index = len(all_messages)
        all_messages.append(message)
        if target_index >= 0 and len(all_messages) > target_index + radius + 500:
            break
    if target_index < 0:
        return []
    target = all_messages[target_index]
    siblings = [
        message
        for message in all_messages
        if message["source"] == target["source"]
        and message["conv_id"] == target["conv_id"]
    ]
    siblings.sort(key=lambda item: (item["ts"], item["msg_id"]))
    try:
        position = siblings.index(target)
    except ValueError:
        return []
    window = siblings[max(0, position - radius) : position + radius + 1]
    return _context_items(window)


def context(paths: SearchPaths, body: dict | None = None) -> dict:
    body = body or {}
    msg_id = str(body.get("id") or "").strip()
    radius = _clamp_int(body.get("radius"), 5, 0, 20)
    if not msg_id:
        raise ValueError("请提供要查看上下文的消息 ID")
    try:
        ensure_ready(paths)
        connection = _connect(paths.index_path)
        if connection is None:
            raise IndexUnavailable("索引数据库不存在")
        try:
            target = connection.execute(
                "SELECT * FROM msg WHERE msg_id=?", (msg_id,)
            ).fetchone()
            if target is None:
                return {"messages": []}
            before = connection.execute(
                "SELECT * FROM msg WHERE source=? AND conv_id=? AND "
                "(ts<? OR (ts=? AND id<?)) ORDER BY ts DESC, id DESC LIMIT ?",
                (
                    target["source"],
                    target["conv_id"],
                    target["ts"],
                    target["ts"],
                    target["id"],
                    radius,
                ),
            ).fetchall()
            after = connection.execute(
                "SELECT * FROM msg WHERE source=? AND conv_id=? AND "
                "(ts>? OR (ts=? AND id>?)) ORDER BY ts ASC, id ASC LIMIT ?",
                (
                    target["source"],
                    target["conv_id"],
                    target["ts"],
                    target["ts"],
                    target["id"],
                    radius,
                ),
            ).fetchall()
            rows = list(reversed(before)) + [target] + list(after)
            return {
                "messages": [
                    {
                        "id": str(row["msg_id"]),
                        "senderName": str(row["sender_name"]),
                        "text": str(row["text"]),
                        "ts": int(row["ts"]),
                        "direction": str(row["direction"]),
                    }
                    for row in rows
                ]
            }
        finally:
            connection.close()
    except IndexUnavailable:
        return {"messages": _fallback_context(paths, msg_id, radius)}


def recent_messages(
    paths: SearchPaths,
    *,
    from_ts: int | None = None,
    to_ts: int | None = None,
    sources: list[str] | None = None,
    limit: int = 2000,
    ascending: bool = False,
) -> list[dict]:
    """Return full message records for the daily assistant (weekly/todos)."""
    sources = [normalize_source(source) for source in (sources or []) if source]
    sources = [source for source in sources if source in {"wecom", "wechat"}]
    bound_limit = max(1, min(int(limit), 20000))
    order = "ASC" if ascending else "DESC"
    try:
        ensure_ready(paths)
        connection = _connect(paths.index_path)
        if connection is None:
            raise IndexUnavailable("索引数据库不存在")
        try:
            where = ["1=1"]
            values: list = []
            if from_ts is not None:
                where.append("ts>=?")
                values.append(int(from_ts))
            if to_ts is not None:
                where.append("ts<=?")
                values.append(int(to_ts))
            if sources:
                where.append(f"source IN ({','.join('?' for _ in sources)})")
                values.extend(sources)
            rows = connection.execute(
                f"SELECT * FROM msg WHERE {' AND '.join(where)} "
                f"ORDER BY ts {order}, id {order} LIMIT ?",
                [*values, bound_limit],
            ).fetchall()
            return [_row_to_message(row) for row in rows]
        finally:
            connection.close()
    except IndexUnavailable:
        messages = []
        for message in iter_source_messages(paths):
            if from_ts is not None and message["ts"] < from_ts:
                continue
            if to_ts is not None and message["ts"] > to_ts:
                continue
            if sources and message["source"] not in sources:
                continue
            messages.append(message)
            if len(messages) >= bound_limit * 4:
                break
        messages.sort(key=lambda item: (item["ts"], item["msg_id"]), reverse=not ascending)
        return messages[:bound_limit]
