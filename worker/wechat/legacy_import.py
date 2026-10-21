"""One-shot, read-only import of a ProxyTalk ``proxytalk.sqlite3`` database.

The legacy database is never opened in place: it (and its WAL) is copied into a
scratch directory and only the copy is read.  Every write is idempotent
(``INSERT OR IGNORE`` / "key absent" checks), so running the import twice leaves
the merged database unchanged.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from .errors import ProxyTalkError
from .vault import SecretVault

IMPORT_MARKER = "proxytalk_imported_at"
IMPORT_SETTINGS_KEYS = (
    "pollIntervalSeconds",
    "contextMessageLimit",
    "contextCharacterLimit",
    "voiceTranscription",
    "voiceCommand",
    "voiceModelPath",
    "voiceArguments",
    "imageSearchRoots",
)


def default_source_path() -> Path:
    appdata = (os.environ.get("APPDATA") or "").strip()
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "ProxyTalk" / "proxytalk.sqlite3"


def import_proxytalk(runtime, payload: dict | None = None) -> dict:
    payload = payload or {}
    raw = payload.get("path") or payload.get("sourcePath")
    source = Path(str(raw)).expanduser() if raw else default_source_path()
    if not source.is_file():
        return {
            "imported": False,
            "source": str(source),
            "message": "未找到 ProxyTalk 旧数据，已跳过导入。",
            "counts": _empty_counts(),
        }
    work = Path(tempfile.mkdtemp(prefix="proxytalk-import-", dir=runtime.temporary_root))
    try:
        copy_path = work / "proxytalk.sqlite3"
        shutil.copy2(source, copy_path)
        for suffix in ("-wal", "-shm"):
            extra = Path(str(source) + suffix)
            if extra.is_file():
                shutil.copy2(extra, Path(str(copy_path) + suffix))
        connection = sqlite3.connect(str(copy_path))
        connection.row_factory = sqlite3.Row
        try:
            counts = _import_database(runtime, connection, source)
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise ProxyTalkError("PROXYTALK_IMPORT_FAILED", "无法读取 ProxyTalk 旧数据，可能不是有效的数据库文件。") from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)
    _copy_secret_files(runtime, source)
    return {
        "imported": True,
        "source": str(source),
        "message": (
            f"已导入 ProxyTalk 旧数据：项目 {counts['projects']} 个、"
            f"消息 {counts['messages']} 条、草稿 {counts['drafts']} 条、"
            f"模型连接 {counts['providers']} 个。"
        ),
        "counts": counts,
    }


def _empty_counts() -> dict:
    return {
        "settings": 0,
        "providers": 0,
        "projects": 0,
        "messages": 0,
        "drafts": 0,
        "cursors": 0,
        "skippedProjects": 0,
        "skippedMessages": 0,
        "skippedDrafts": 0,
        "skippedProviders": 0,
    }


def _import_database(runtime, connection: sqlite3.Connection, source: Path) -> dict:
    store = runtime.store
    tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"projects", "messages"}.issubset(tables):
        raise ProxyTalkError("PROXYTALK_IMPORT_INVALID", "旧数据库缺少项目或消息表，无法导入。")
    counts = _empty_counts()
    _import_settings(store, connection, tables, counts)
    _import_providers(runtime, connection, tables, counts)
    _import_projects(store, connection, tables, counts)
    _import_messages(store, connection, tables, counts)
    _import_drafts(store, connection, tables, counts)
    _import_cursors(store, connection, tables, counts)
    _mark_imported(store, source)
    return counts


def _import_settings(store, connection, tables, counts) -> None:
    if "settings" not in tables:
        return
    existing = {str(row[0]) for row in store.connection.execute("SELECT key FROM settings")}
    values = {}
    for row in connection.execute("SELECT key,value_json FROM settings"):
        key = str(row["key"])
        if key not in IMPORT_SETTINGS_KEYS or key in existing:
            continue
        try:
            values[key] = json.loads(row["value_json"])
        except (TypeError, ValueError):
            continue
    if not values:
        return
    store.save_settings(values)
    counts["settings"] = len(values)


def _import_providers(runtime, connection, tables, counts) -> None:
    if "providers" not in tables:
        return
    store = runtime.store
    columns = _columns(connection, "providers")
    for row in connection.execute("SELECT * FROM providers"):
        provider_id = str(row["id"] or "").strip()
        if not provider_id:
            continue
        try:
            store.provider(provider_id)
            continue
        except ProxyTalkError:
            pass
        secret_ref = str(row["secret_ref"] or "") if "secret_ref" in columns else ""
        if secret_ref:
            try:
                persisted = secret_ref if runtime.vault.has(secret_ref) else ""
            except Exception:
                persisted = ""
        else:
            persisted = ""
        value = {
            "id": provider_id,
            "name": str(row["name"] or provider_id),
            "kind": str(row["kind"] or "openai_compatible"),
            "baseUrl": str(row["base_url"] or ""),
            "model": str(row["model"] or ""),
            "temperature": float(row["temperature"] if "temperature" in columns else 0.3),
            "maxOutputTokens": int(row["max_output_tokens"] if "max_output_tokens" in columns else 1200),
            "supportsVision": bool(row["supports_vision"]) if "supports_vision" in columns else True,
        }
        try:
            store.save_provider(value, secret_ref=persisted)
        except ProxyTalkError:
            counts["skippedProviders"] += 1
            continue
        counts["providers"] += 1


def _import_projects(store, connection, tables, counts) -> None:
    if "projects" not in tables:
        return
    known_providers = {provider["id"] for provider in store.providers()}
    known_mcp = {server["id"] for server in store.mcp_servers()}
    columns = _columns(connection, "projects")
    for row in connection.execute("SELECT * FROM projects"):
        project_id = str(row["id"] or "").strip()
        if not project_id:
            continue
        if store.connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
            counts["skippedProjects"] += 1
            continue
        provider_id = str(row["provider_id"] or "") if "provider_id" in columns else ""
        if provider_id not in known_providers:
            provider_id = ""
        try:
            mcp_ids = [item for item in json.loads(row["mcp_server_ids_json"] or "[]") if item in known_mcp]
        except (TypeError, ValueError):
            mcp_ids = []
        name = str(row["name"] or project_id)
        try:
            store.connection.execute(
                """INSERT OR IGNORE INTO projects(id,account_id,chat_id,chat_title,name,kind,enabled,prompt,summary,
                   trigger_json,provider_id,skill_paths_json,mcp_server_ids_json,revision,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    project_id,
                    str(row["account_id"] or ""),
                    str(row["chat_id"] or ""),
                    str(row["chat_title"] or name) if "chat_title" in columns else name,
                    name,
                    str(row["kind"] or "direct"),
                    int(row["enabled"] if "enabled" in columns else 1),
                    str(row["prompt"] or ""),
                    str(row["summary"] or ""),
                    str(row["trigger_json"] or "{}"),
                    provider_id or None,
                    str(row["skill_paths_json"] or "[]"),
                    json.dumps(mcp_ids, ensure_ascii=False, separators=(",", ":")),
                    int(row["revision"] or 1) if "revision" in columns else 1,
                    str(row["created_at"] or ""),
                    str(row["updated_at"] or ""),
                ),
            )
            store.connection.execute(
                "INSERT OR IGNORE INTO drafts(project_id,updated_at) VALUES(?,?)",
                (project_id, str(row["updated_at"] or "")),
            )
            counts["projects"] += 1
        except sqlite3.DatabaseError:
            counts["skippedProjects"] += 1
    store.connection.commit()


def _import_messages(store, connection, tables, counts) -> None:
    if "messages" not in tables:
        return
    columns = _columns(connection, "messages")
    known = {project["id"] for project in store.projects()}
    if "project_id" not in columns:
        return
    before = store.connection.total_changes
    rows = []
    for row in connection.execute("SELECT * FROM messages"):
        project_id = str(row["project_id"] or "")
        if project_id not in known:
            counts["skippedMessages"] += 1
            continue
        rows.append(
            (
                project_id,
                str(row["source_key"] or ""),
                str(row["source_db"] or "") if "source_db" in columns else "",
                int(row["local_id"] or 0) if "local_id" in columns else 0,
                int(row["server_id"] or 0) if "server_id" in columns else 0,
                int(row["sort_seq"] or 0) if "sort_seq" in columns else 0,
                int(row["create_time"] or 0) if "create_time" in columns else 0,
                str(row["sender_id"] or "") if "sender_id" in columns else "",
                str(row["sender_name"] or "") if "sender_name" in columns else "",
                str(row["direction"] or "incoming") if "direction" in columns else "incoming",
                str(row["kind"] or "text") if "kind" in columns else "text",
                str(row["text"] or "") if "text" in columns else "",
                row["quote_json"] if "quote_json" in columns else None,
                str(row["media_json"] or "[]") if "media_json" in columns else "[]",
                int(row["mentioned_me"] or 0) if "mentioned_me" in columns else 0,
                int(row["backlog"] or 0) if "backlog" in columns else 0,
                str(row["created_at"] or "") if "created_at" in columns else "",
            )
        )
    if not rows:
        return
    store.connection.executemany(
        """INSERT OR IGNORE INTO messages(project_id,source_key,source_db,local_id,server_id,sort_seq,
           create_time,sender_id,sender_name,direction,kind,text,quote_json,media_json,mentioned_me,
           backlog,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    store.connection.commit()
    counts["messages"] = store.connection.total_changes - before


def _import_drafts(store, connection, tables, counts) -> None:
    if "drafts" not in tables:
        return
    known = {project["id"] for project in store.projects()}
    columns = _columns(connection, "drafts")
    for row in connection.execute("SELECT * FROM drafts"):
        project_id = str(row["project_id"] or "")
        if project_id not in known:
            counts["skippedDrafts"] += 1
            continue
        existing = store.connection.execute(
            "SELECT content FROM drafts WHERE project_id=?", (project_id,)
        ).fetchone()
        if existing is not None and str(existing["content"] or ""):
            counts["skippedDrafts"] += 1
            continue
        store.connection.execute(
            """INSERT INTO drafts(project_id,content,status,based_on_json,model,tool_trace_json,
               stale,error_code,updated_at) VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(project_id) DO UPDATE SET content=excluded.content,status=excluded.status,
               based_on_json=excluded.based_on_json,model=excluded.model,
               tool_trace_json=excluded.tool_trace_json,stale=excluded.stale,
               error_code=excluded.error_code,updated_at=excluded.updated_at""",
            (
                project_id,
                str(row["content"] or ""),
                str(row["status"] or "idle"),
                str(row["based_on_json"] or "[]"),
                str(row["model"] or "") if "model" in columns else "",
                str(row["tool_trace_json"] or "[]") if "tool_trace_json" in columns else "[]",
                int(row["stale"] or 0) if "stale" in columns else 0,
                str(row["error_code"] or "") if "error_code" in columns else "",
                str(row["updated_at"] or ""),
            ),
        )
        counts["drafts"] += 1
    store.connection.commit()


def _import_cursors(store, connection, tables, counts) -> None:
    if "source_cursors" not in tables:
        return
    known = {project["id"] for project in store.projects()}
    columns = _columns(connection, "source_cursors")
    before = store.connection.total_changes
    rows = []
    for row in connection.execute("SELECT * FROM source_cursors"):
        project_id = str(row["project_id"] or "")
        if project_id not in known:
            continue
        rows.append(
            (
                project_id,
                str(row["source_db"] or ""),
                int(row["create_time"] or 0) if "create_time" in columns else 0,
                int(row["sort_seq"] or 0) if "sort_seq" in columns else 0,
                int(row["local_id"] or 0) if "local_id" in columns else 0,
                str(row["updated_at"] or ""),
            )
        )
    if not rows:
        return
    store.connection.executemany(
        """INSERT INTO source_cursors(project_id,source_db,create_time,sort_seq,local_id,updated_at)
           VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,source_db) DO NOTHING""",
        rows,
    )
    store.connection.commit()
    counts["cursors"] = store.connection.total_changes - before


def _mark_imported(store, source: Path) -> None:
    store.connection.execute(
        "INSERT INTO runtime_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (IMPORT_MARKER, str(source)),
    )
    store.connection.commit()


def _copy_secret_files(runtime, source: Path) -> None:
    """Copy DPAPI blobs (user-bound ciphertext) so imported API keys stay usable."""

    vault = runtime.vault
    if not isinstance(vault, SecretVault):
        return
    old = source.parent / "secrets"
    if not old.is_dir():
        return
    target = Path(getattr(vault, "root", "") or "")
    if not str(target):
        return
    try:
        target.mkdir(parents=True, exist_ok=True)
        for item in old.glob("*.dpapi"):
            destination = target / item.name
            if destination.is_file():
                continue
            shutil.copy2(item, destination)
    except OSError:
        return


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
