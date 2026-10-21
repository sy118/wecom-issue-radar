from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import ipaddress
from pathlib import Path
import sqlite3
import threading
from typing import Iterable
from urllib.parse import urlsplit

from .errors import ProxyTalkError, require
from .models import SourceCursor, SourceMessage


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('openai_compatible','anthropic')),
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    temperature REAL NOT NULL DEFAULT 0.3,
    max_output_tokens INTEGER NOT NULL DEFAULT 1200,
    supports_vision INTEGER NOT NULL DEFAULT 1,
    secret_ref TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_servers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    transport TEXT NOT NULL CHECK(transport IN ('stdio','http')),
    config_json TEXT NOT NULL,
    secret_ref TEXT NOT NULL,
    catalog_json TEXT NOT NULL DEFAULT '[]',
    last_error TEXT NOT NULL DEFAULT '',
    tested_at TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    chat_title TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('direct','group')),
    enabled INTEGER NOT NULL DEFAULT 1,
    prompt TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    trigger_json TEXT NOT NULL,
    provider_id TEXT,
    skill_paths_json TEXT NOT NULL DEFAULT '[]',
    mcp_server_ids_json TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, chat_id),
    FOREIGN KEY(provider_id) REFERENCES providers(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS source_cursors (
    project_id TEXT NOT NULL,
    source_db TEXT NOT NULL,
    create_time INTEGER NOT NULL DEFAULT 0,
    sort_seq INTEGER NOT NULL DEFAULT 0,
    local_id INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id, source_db),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    source_key TEXT NOT NULL UNIQUE,
    source_db TEXT NOT NULL,
    local_id INTEGER NOT NULL,
    server_id INTEGER NOT NULL DEFAULT 0,
    sort_seq INTEGER NOT NULL DEFAULT 0,
    create_time INTEGER NOT NULL,
    sender_id TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL CHECK(direction IN ('incoming','outgoing','system')),
    kind TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    quote_json TEXT,
    media_json TEXT NOT NULL DEFAULT '[]',
    mentioned_me INTEGER NOT NULL DEFAULT 0,
    backlog INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_project_time
ON messages(project_id, create_time DESC, sort_seq DESC, local_id DESC);

CREATE TABLE IF NOT EXISTS drafts (
    project_id TEXT PRIMARY KEY,
    content TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'idle',
    based_on_json TEXT NOT NULL DEFAULT '[]',
    model TEXT NOT NULL DEFAULT '',
    tool_trace_json TEXT NOT NULL DEFAULT '[]',
    stale INTEGER NOT NULL DEFAULT 0,
    error_code TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tool_approvals (
    project_id TEXT NOT NULL,
    server_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_hash TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    PRIMARY KEY(project_id, server_id, tool_name, arguments_hash),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS runtime_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode(value: str | None, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _service_url_is_safe(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        if not hostname or parsed.username or parsed.password or parsed.fragment:
            return False
        _ = parsed.port  # Force validation of malformed and out-of-range ports.
        if parsed.scheme == "https":
            return True
        if parsed.scheme != "http":
            return False
        if hostname.casefold() == "localhost":
            return True
        return ipaddress.ip_address(hostname).is_loopback
    except (ValueError, UnicodeError):
        return False


class RuntimeStore:
    """Durable state module; callers interact only through domain-shaped methods."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.connection.executescript(SCHEMA)
            provider_columns = {
                str(row[1])
                for row in self.connection.execute("PRAGMA table_info(providers)")
            }
            if "supports_vision" not in provider_columns:
                self.connection.execute(
                    "ALTER TABLE providers ADD COLUMN supports_vision INTEGER NOT NULL DEFAULT 1"
                )
            project_columns = {
                str(row[1])
                for row in self.connection.execute("PRAGMA table_info(projects)")
            }
            if "chat_title" not in project_columns:
                self.connection.execute(
                    "ALTER TABLE projects ADD COLUMN chat_title TEXT NOT NULL DEFAULT ''"
                )
                self.connection.execute("UPDATE projects SET chat_title=name WHERE chat_title='' ")
            self.connection.execute(
                """UPDATE drafts
                   SET status=CASE WHEN content='' THEN 'idle' ELSE 'ready' END,
                       stale=1,error_code='DRAFT_INTERRUPTED'
                   WHERE status='generating'"""
            )
            self.connection.execute(
                """UPDATE drafts
                   SET status=CASE WHEN content='' THEN 'idle' ELSE 'ready' END,
                       tool_trace_json='[]',stale=1,error_code='TOOL_APPROVAL_STALE'
                   WHERE status='awaiting_tool_approval'"""
            )
            self.connection.execute("DELETE FROM tool_approvals")
            self.connection.commit()

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    @contextmanager
    def transaction(self):
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                yield self.connection
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def get_setting(self, key: str, fallback=None):
        with self.lock:
            row = self.connection.execute(
                "SELECT value_json FROM settings WHERE key=?", (key,)
            ).fetchone()
        return _decode(row[0], fallback) if row else fallback

    def save_settings(self, values: dict) -> dict:
        allowed = {
            "wechatDbDir",
            "pollIntervalSeconds",
            "contextMessageLimit",
            "contextCharacterLimit",
            "voiceTranscription",
            "voiceCommand",
            "voiceModelPath",
            "voiceArguments",
            "imageSearchRoots",
        }
        unknown = set(values) - allowed
        require(not unknown, "INVALID_SETTINGS", f"不支持的设置项：{', '.join(sorted(unknown))}")
        normalized = dict(values)
        if "wechatDbDir" in normalized:
            normalized["wechatDbDir"] = str(normalized["wechatDbDir"] or "")[:4096]
        if "pollIntervalSeconds" in normalized:
            normalized["pollIntervalSeconds"] = min(30, max(1, int(normalized["pollIntervalSeconds"])))
        if "contextMessageLimit" in normalized:
            normalized["contextMessageLimit"] = min(500, max(10, int(normalized["contextMessageLimit"])))
        if "contextCharacterLimit" in normalized:
            normalized["contextCharacterLimit"] = min(100_000, max(4000, int(normalized["contextCharacterLimit"])))
        if "voiceTranscription" in normalized:
            normalized["voiceTranscription"] = bool(normalized["voiceTranscription"])
        for key in ("voiceCommand", "voiceModelPath"):
            if key in normalized:
                normalized[key] = str(normalized[key] or "")[:4096]
        if "voiceArguments" in normalized:
            require(isinstance(normalized["voiceArguments"], list), "INVALID_SETTINGS", "语音转录参数必须是列表。")
            normalized["voiceArguments"] = [str(item)[:4096] for item in normalized["voiceArguments"][:64]]
        if "imageSearchRoots" in normalized:
            require(isinstance(normalized["imageSearchRoots"], list), "INVALID_SETTINGS", "图片搜索目录必须是列表。")
            normalized["imageSearchRoots"] = [str(item)[:4096] for item in normalized["imageSearchRoots"][:32]]
        now = _now()
        with self.transaction() as conn:
            for key, value in normalized.items():
                conn.execute(
                    """INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)
                       ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                       updated_at=excluded.updated_at""",
                    (key, _encode(value), now),
                )
        return self.settings()

    def settings(self) -> dict:
        defaults = {
            "wechatDbDir": "",
            "pollIntervalSeconds": 3,
            "contextMessageLimit": 60,
            "contextCharacterLimit": 24000,
            "voiceTranscription": False,
            "voiceCommand": "",
            "voiceModelPath": "",
            "voiceArguments": ["{input}"],
            "imageSearchRoots": [],
        }
        with self.lock:
            rows = self.connection.execute("SELECT key,value_json FROM settings").fetchall()
        for row in rows:
            defaults[row["key"]] = _decode(row["value_json"], defaults.get(row["key"]))
        return defaults

    def save_provider(self, value: dict, *, secret_ref: str) -> dict:
        provider_id = str(value.get("id") or "").strip()
        name = str(value.get("name") or "").strip()
        kind = str(value.get("kind") or "openai_compatible").strip()
        base_url = str(value.get("baseUrl") or "").strip().rstrip("/")
        model = str(value.get("model") or "").strip()
        require(provider_id and name, "INVALID_PROVIDER", "模型配置名称和 ID 不能为空。")
        require(kind in {"openai_compatible", "anthropic"}, "INVALID_PROVIDER", "模型类型无效。")
        require(_service_url_is_safe(base_url), "INVALID_PROVIDER", "模型 Base URL 必须使用 HTTPS；本机回环地址可使用 HTTP。")
        require(model, "INVALID_PROVIDER", "模型名称不能为空。")
        temperature = min(2.0, max(0.0, float(value.get("temperature", 0.3))))
        max_tokens = min(16000, max(128, int(value.get("maxOutputTokens", 1200))))
        supports_vision = int(bool(value.get("supportsVision", True)))
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO providers(id,name,kind,base_url,model,temperature,max_output_tokens,supports_vision,secret_ref,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,
                   base_url=excluded.base_url,model=excluded.model,temperature=excluded.temperature,
                   max_output_tokens=excluded.max_output_tokens,supports_vision=excluded.supports_vision,
                   secret_ref=excluded.secret_ref,
                   updated_at=excluded.updated_at""",
                (provider_id, name, kind, base_url, model, temperature, max_tokens, supports_vision, secret_ref, _now()),
            )
        return self.provider(provider_id)

    def provider(self, provider_id: str) -> dict:
        with self.lock:
            row = self.connection.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
        if not row:
            raise ProxyTalkError("PROVIDER_NOT_FOUND", "找不到项目使用的模型配置。")
        return self._provider_public(row)

    def providers(self) -> list[dict]:
        with self.lock:
            rows = self.connection.execute("SELECT * FROM providers ORDER BY name COLLATE NOCASE").fetchall()
        return [self._provider_public(row) for row in rows]

    @staticmethod
    def _provider_public(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "kind": row["kind"],
            "baseUrl": row["base_url"],
            "model": row["model"],
            "temperature": row["temperature"],
            "maxOutputTokens": row["max_output_tokens"],
            "supportsVision": bool(row["supports_vision"]),
            "secretRef": row["secret_ref"],
            "hasSecret": bool(row["secret_ref"]),
        }

    def save_mcp_server(self, value: dict, *, secret_ref: str) -> dict:
        server_id = str(value.get("id") or "").strip()
        name = str(value.get("name") or "").strip()
        transport = str(value.get("transport") or "stdio").strip()
        require(server_id and name, "INVALID_MCP_SERVER", "MCP 名称和 ID 不能为空。")
        require(transport in {"stdio", "http"}, "INVALID_MCP_SERVER", "MCP 传输类型无效。")
        if transport == "stdio":
            command = str(value.get("command") or "").strip()[:4096]
            require(command, "INVALID_MCP_SERVER", "stdio MCP 必须填写启动命令。")
            args = value.get("args") or []
            require(isinstance(args, list), "INVALID_MCP_SERVER", "MCP 参数必须是列表。")
            config = {"command": command, "args": [str(item)[:4096] for item in args[:64]]}
        else:
            url = str(value.get("url") or "").strip()[:8192]
            require(_service_url_is_safe(url), "INVALID_MCP_SERVER", "远程 MCP 必须使用 HTTPS；本机回环地址可使用 HTTP。")
            config = {"url": url}
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO mcp_servers(id,name,transport,config_json,secret_ref,updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name,transport=excluded.transport,
                   config_json=excluded.config_json,secret_ref=excluded.secret_ref,catalog_json='[]',
                   last_error='',tested_at=NULL,updated_at=excluded.updated_at""",
                (server_id, name, transport, _encode(config), secret_ref, _now()),
            )
        return self.mcp_server(server_id)

    def update_mcp_catalog(self, server_id: str, catalog: list[dict], error: str = "") -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE mcp_servers SET catalog_json=?,last_error=?,tested_at=?,updated_at=? WHERE id=?",
                (_encode(catalog), error[:500], _now(), _now(), server_id),
            )

    def mcp_server(self, server_id: str) -> dict:
        with self.lock:
            row = self.connection.execute("SELECT * FROM mcp_servers WHERE id=?", (server_id,)).fetchone()
        if not row:
            raise ProxyTalkError("MCP_NOT_FOUND", "找不到 MCP 配置。")
        return self._mcp_public(row)

    def mcp_servers(self) -> list[dict]:
        with self.lock:
            rows = self.connection.execute("SELECT * FROM mcp_servers ORDER BY name COLLATE NOCASE").fetchall()
        return [self._mcp_public(row) for row in rows]

    @staticmethod
    def _mcp_public(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "transport": row["transport"],
            **_decode(row["config_json"], {}),
            "secretRef": row["secret_ref"],
            "hasSecret": bool(row["secret_ref"]),
            "catalog": _decode(row["catalog_json"], []),
            "lastError": row["last_error"],
            "testedAt": row["tested_at"],
            "enabled": bool(row["enabled"]),
        }

    def save_project(self, value: dict) -> dict:
        project_id = str(value.get("id") or "").strip()[:128]
        account_id = str(value.get("accountId") or "").strip()[:256]
        chat_id = str(value.get("chatId") or "").strip()[:256]
        chat_title = str(value.get("chatTitle") or value.get("name") or "").strip()[:256]
        name = str(value.get("name") or "").strip()[:256]
        kind = str(value.get("kind") or "direct")
        require(project_id and account_id and chat_id and chat_title and name, "INVALID_PROJECT", "项目和微信聊天信息不完整。")
        require(kind in {"direct", "group"}, "INVALID_PROJECT", "聊天类型无效。")
        trigger = value.get("trigger") if isinstance(value.get("trigger"), dict) else {}
        default_trigger = {
            "mode": "auto" if kind == "direct" else "manual",
            "keywords": [],
            "senderIds": [],
            "mentionMe": False,
            "quietWindowMs": 1800,
        }
        default_trigger.update(trigger)
        require(default_trigger["mode"] in {"auto", "manual", "rules"}, "INVALID_TRIGGER", "触发模式无效。")
        require(kind == "group" or default_trigger["mode"] != "rules", "INVALID_TRIGGER", "单聊不支持群聊规则触发。")
        keywords = default_trigger.get("keywords")
        sender_ids = default_trigger.get("senderIds")
        require(isinstance(keywords, list) and isinstance(sender_ids, list), "INVALID_TRIGGER", "关键词和发送者规则必须是列表。")
        default_trigger = {
            "mode": default_trigger["mode"],
            "keywords": [str(item).strip()[:100] for item in keywords[:32] if str(item).strip()],
            "senderIds": [str(item).strip()[:256] for item in sender_ids[:64] if str(item).strip()],
            "mentionMe": bool(default_trigger.get("mentionMe")),
            "quietWindowMs": min(10_000, max(300, int(default_trigger.get("quietWindowMs") or 1800))),
        }
        provider_id = str(value.get("providerId") or "").strip()
        if provider_id:
            self.provider(provider_id)
        skill_paths = value.get("skillPaths") or []
        mcp_server_ids = value.get("mcpServerIds") or []
        require(isinstance(skill_paths, list), "INVALID_PROJECT", "Skill 路径必须是列表。")
        require(isinstance(mcp_server_ids, list), "INVALID_PROJECT", "MCP 配置必须是列表。")
        skill_paths = [str(path).strip()[:4096] for path in skill_paths[:16] if str(path).strip()]
        mcp_server_ids = [str(item).strip()[:128] for item in mcp_server_ids[:32] if str(item).strip()]
        for server_id in mcp_server_ids:
            self.mcp_server(server_id)
        now = _now()
        with self.transaction() as conn:
            previous = conn.execute("SELECT revision,created_at FROM projects WHERE id=?", (project_id,)).fetchone()
            revision = int(previous["revision"] + 1) if previous else 1
            created_at = previous["created_at"] if previous else now
            try:
                conn.execute(
                    """INSERT INTO projects(id,account_id,chat_id,chat_title,name,kind,enabled,prompt,summary,
                       trigger_json,provider_id,skill_paths_json,mcp_server_ids_json,revision,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET account_id=excluded.account_id,chat_id=excluded.chat_id,
                       chat_title=excluded.chat_title,name=excluded.name,kind=excluded.kind,enabled=excluded.enabled,prompt=excluded.prompt,
                       summary=excluded.summary,trigger_json=excluded.trigger_json,provider_id=excluded.provider_id,
                       skill_paths_json=excluded.skill_paths_json,mcp_server_ids_json=excluded.mcp_server_ids_json,
                       revision=excluded.revision,updated_at=excluded.updated_at""",
                    (
                        project_id, account_id, chat_id, chat_title, name, kind, int(bool(value.get("enabled", True))),
                        str(value.get("prompt") or "")[:50_000], str(value.get("summary") or "")[:50_000], _encode(default_trigger),
                        provider_id or None,
                        _encode(skill_paths),
                        _encode(mcp_server_ids),
                        revision, created_at, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ProxyTalkError("PROJECT_EXISTS", "这个微信聊天已经建立了项目。") from exc
            conn.execute(
                "INSERT OR IGNORE INTO drafts(project_id,updated_at) VALUES(?,?)",
                (project_id, now),
            )
        return self.project(project_id)

    def delete_project(self, project_id: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM projects WHERE id=?", (project_id,))

    def project(self, project_id: str) -> dict:
        with self.lock:
            row = self.connection.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise ProxyTalkError("PROJECT_NOT_FOUND", "找不到这个聊天项目。")
        return self._project_public(row)

    def projects(self, *, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM projects"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY updated_at DESC"
        with self.lock:
            rows = self.connection.execute(sql).fetchall()
        return [self._project_public(row) for row in rows]

    @staticmethod
    def _project_public(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"], "accountId": row["account_id"], "chatId": row["chat_id"],
            "chatTitle": row["chat_title"] or row["name"],
            "name": row["name"], "kind": row["kind"], "enabled": bool(row["enabled"]),
            "prompt": row["prompt"], "summary": row["summary"],
            "trigger": _decode(row["trigger_json"], {}), "providerId": row["provider_id"] or "",
            "skillPaths": _decode(row["skill_paths_json"], []),
            "mcpServerIds": _decode(row["mcp_server_ids_json"], []),
            "revision": row["revision"], "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        }

    def cursors(self, project_id: str) -> dict[str, SourceCursor]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM source_cursors WHERE project_id=?", (project_id,)
            ).fetchall()
        return {
            row["source_db"]: SourceCursor(
                project_id=project_id,
                source_db=row["source_db"],
                create_time=row["create_time"],
                sort_seq=row["sort_seq"],
                local_id=row["local_id"],
            )
            for row in rows
        }

    def ingest(self, messages: Iterable[SourceMessage], cursors: Iterable[SourceCursor]) -> list[dict]:
        inserted: list[dict] = []
        now = _now()
        with self.transaction() as conn:
            for message in messages:
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO messages(project_id,source_key,source_db,local_id,server_id,
                       sort_seq,create_time,sender_id,sender_name,direction,kind,text,quote_json,media_json,
                       mentioned_me,backlog,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        message.project_id, message.source_key, message.source_db, message.local_id,
                        message.server_id, message.sort_seq, message.create_time, message.sender_id,
                        message.sender_name, message.direction, message.kind, message.text,
                        _encode(message.quote) if message.quote else None, _encode(message.media),
                        int(message.mentioned_me), int(message.backlog), now,
                    ),
                )
                if cursor.rowcount:
                    inserted.append(message.public())
            for cursor in cursors:
                conn.execute(
                    """INSERT INTO source_cursors(project_id,source_db,create_time,sort_seq,local_id,updated_at)
                       VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,source_db) DO UPDATE SET
                       create_time=excluded.create_time,sort_seq=excluded.sort_seq,
                       local_id=excluded.local_id,updated_at=excluded.updated_at""",
                    (cursor.project_id, cursor.source_db, cursor.create_time, cursor.sort_seq, cursor.local_id, now),
                )
        return inserted

    def messages(self, project_id: str, *, limit: int = 100, before: int | None = None) -> list[dict]:
        params: list = [project_id]
        where = "project_id=?"
        if before is not None:
            where += " AND create_time<?"
            params.append(int(before))
        params.append(min(1000, max(1, int(limit))))
        with self.lock:
            rows = self.connection.execute(
                f"SELECT * FROM messages WHERE {where} ORDER BY create_time DESC,sort_seq DESC,local_id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._message_public(row) for row in reversed(rows)]

    def message(self, project_id: str, message_id: str) -> dict:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM messages WHERE project_id=? AND source_key=? LIMIT 1",
                (project_id, message_id),
            ).fetchone()
        if not row:
            raise ProxyTalkError("MESSAGE_NOT_FOUND", "找不到这个聊天项目中的消息。")
        return self._message_public(row)

    def export_messages(self, project_id: str, *, start_time: int = 0, end_time: int = 0) -> list[dict]:
        where = ["project_id=?"]
        params: list = [project_id]
        if start_time:
            where.append("create_time>=?")
            params.append(int(start_time))
        if end_time:
            where.append("create_time<=?")
            params.append(int(end_time))
        with self.lock:
            rows = self.connection.execute(
                f"SELECT * FROM messages WHERE {' AND '.join(where)} ORDER BY create_time,sort_seq,local_id",
                params,
            ).fetchall()
        return [self._message_public(row) for row in rows]

    @staticmethod
    def _message_public(row: sqlite3.Row) -> dict:
        return {
            "id": row["source_key"], "projectId": row["project_id"], "sourceDb": row["source_db"],
            "localId": row["local_id"], "serverId": row["server_id"], "sortSeq": row["sort_seq"],
            "createTime": row["create_time"], "senderId": row["sender_id"],
            "senderName": row["sender_name"], "direction": row["direction"], "kind": row["kind"],
            "text": row["text"], "quote": _decode(row["quote_json"], None),
            "media": _decode(row["media_json"], []), "mentionedMe": bool(row["mentioned_me"]),
            "backlog": bool(row["backlog"]),
        }

    def save_draft(self, project_id: str, **changes) -> dict:
        current = self.draft(project_id)
        mapping = {
            "content": changes.get("content", current["content"]),
            "status": changes.get("status", current["status"]),
            "basedOn": changes.get("basedOn", current["basedOn"]),
            "model": changes.get("model", current["model"]),
            "toolTrace": changes.get("toolTrace", current["toolTrace"]),
            "stale": changes.get("stale", current["stale"]),
            "errorCode": changes.get("errorCode", current["errorCode"]),
        }
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO drafts(project_id,content,status,based_on_json,model,tool_trace_json,stale,error_code,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET content=excluded.content,
                   status=excluded.status,based_on_json=excluded.based_on_json,model=excluded.model,
                   tool_trace_json=excluded.tool_trace_json,stale=excluded.stale,error_code=excluded.error_code,
                   updated_at=excluded.updated_at""",
                (
                    project_id, mapping["content"], mapping["status"], _encode(mapping["basedOn"]),
                    mapping["model"], _encode(mapping["toolTrace"]), int(bool(mapping["stale"])),
                    mapping["errorCode"], _now(),
                ),
            )
        return self.draft(project_id)

    def draft(self, project_id: str) -> dict:
        with self.lock:
            row = self.connection.execute("SELECT * FROM drafts WHERE project_id=?", (project_id,)).fetchone()
        if not row:
            return {
                "projectId": project_id, "content": "", "status": "idle", "basedOn": [],
                "model": "", "toolTrace": [], "stale": False, "errorCode": "", "updatedAt": "",
            }
        return {
            "projectId": project_id, "content": row["content"], "status": row["status"],
            "basedOn": _decode(row["based_on_json"], []), "model": row["model"],
            "toolTrace": _decode(row["tool_trace_json"], []), "stale": bool(row["stale"]),
            "errorCode": row["error_code"], "updatedAt": row["updated_at"],
        }

    def approve_tool(self, project_id: str, server_id: str, tool_name: str, arguments_hash: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tool_approvals VALUES(?,?,?,?,?)",
                (project_id, server_id, tool_name, arguments_hash, _now()),
            )

    def tool_is_approved(self, project_id: str, server_id: str, tool_name: str, arguments_hash: str) -> bool:
        with self.lock:
            row = self.connection.execute(
                "SELECT 1 FROM tool_approvals WHERE project_id=? AND server_id=? AND tool_name=? AND arguments_hash=?",
                (project_id, server_id, tool_name, arguments_hash),
            ).fetchone()
        return bool(row)

    def consume_tool_approval(self, project_id: str, server_id: str, tool_name: str, arguments_hash: str) -> bool:
        """Atomically consume a single approval so side-effecting calls always ask again."""
        with self.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM tool_approvals WHERE project_id=? AND server_id=? AND tool_name=? AND arguments_hash=?",
                (project_id, server_id, tool_name, arguments_hash),
            )
        return bool(cursor.rowcount)

    def clear_tool_approvals(self, project_id: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM tool_approvals WHERE project_id=?", (project_id,))

    def next_event_seq(self) -> int:
        with self.transaction() as conn:
            row = conn.execute("SELECT value FROM runtime_meta WHERE key='event_seq'").fetchone()
            value = int(row[0]) + 1 if row else 1
            conn.execute(
                "INSERT INTO runtime_meta(key,value) VALUES('event_seq',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(value),),
            )
        return value

    def snapshot(self) -> dict:
        projects = self.projects()
        return {
            "settings": self.settings(),
            "providers": self.providers(),
            "mcpServers": self.mcp_servers(),
            "projects": projects,
            "drafts": {project["id"]: self.draft(project["id"]) for project in projects},
        }
