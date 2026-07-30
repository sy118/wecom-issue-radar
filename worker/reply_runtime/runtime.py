from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from concurrent.futures import Future, wait as wait_futures
from datetime import datetime, timezone
from pathlib import Path

from .errors import RuntimeProtocolError
from .store import RuntimeStore, decode_json, encode_json


MCP_TRANSPORTS = {"stdio", "sse", "streamable-http"}
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
WECOM_WEBHOOK_PREFIX = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="


class _RetryableMessageClassification(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


class ReplyRuntime:
    """Durable command/query seam for group-listening reply automation."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        model=None,
        mcp=None,
        webhook=None,
        clock=None,
        message_source=None,
        event_sink=None,
        config_path: str | Path | None = None,
        autostart: bool = True,
    ) -> None:
        self.store = RuntimeStore(database_path)
        self.model = model
        self.mcp = mcp
        self.webhook = webhook
        self.clock = clock
        self.message_source = message_source
        self.event_sink = event_sink
        self.config_path = str(config_path) if config_path else None
        self._closed = False
        self._lease_lost = False
        self._started = False
        self._started_at: float | None = None
        self._owner_id = str(uuid.uuid4())
        self._stop_event = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._lease_thread: threading.Thread | None = None
        # A stale generation may keep running after a best-effort cancellation (for
        # example a blocking MCP call). Track the generation and its occupied slot so
        # it cannot be overwritten by a newer future for the same work item.
        self._futures: dict[tuple[str, int], tuple[Future, str, str]] = {}
        self._future_lock = threading.RLock()
        self._poll_lock = threading.RLock()
        self._assign_lock = threading.RLock()
        self._sender_assignment_locks: dict[tuple[str, str], threading.RLock] = {}
        self._sender_assignment_locks_guard = threading.RLock()
        self._assignment_condition = threading.Condition(threading.RLock())
        self._active_assignments: dict[int, str] = {}
        if autostart:
            self.start()

    def start(self) -> None:
        if self._started:
            return
        now = self._now()
        with self.store.transaction() as db:
            lease = db.execute("SELECT * FROM runtime_lease WHERE singleton=1").fetchone()
            if lease and lease["owner_id"] != self._owner_id and float(lease["expires_at"]) > now:
                raise RuntimeProtocolError(
                    "RUNTIME_ALREADY_RUNNING", "another reply runtime owns the delivery lease"
                )
            db.execute(
                "INSERT OR REPLACE INTO runtime_lease(singleton,owner_id,expires_at,updated_at) VALUES(1,?,?,?)",
                (self._owner_id, now + 15, now),
            )
            # A process that disappeared after starting a request cannot know whether WeCom
            # accepted it. Preserve at-most-once semantics by never auto-retrying it.
            interrupted_outbox = db.execute(
                """SELECT o.payload_json,w.group_id FROM reply_outbox o
                   JOIN reply_work_items w ON w.id=o.work_id WHERE o.status='sending'"""
            ).fetchall()
            for interrupted in interrupted_outbox:
                payload = decode_json(interrupted["payload_json"], {})
                db.execute(
                    "INSERT OR REPLACE INTO recent_outbound(fingerprint,group_id,sent_at) VALUES(?,?,?)",
                    (
                        _outbound_fingerprint(
                            str(interrupted["group_id"]), str(payload.get("text") or "")
                        ),
                        str(interrupted["group_id"]),
                        now,
                    ),
                )
            db.execute("UPDATE reply_outbox SET status='delivery_unknown' WHERE status='sending'")
            in_progress_commands = db.execute(
                """SELECT command_id,result_json FROM runtime_commands
                   WHERE json_extract(result_json,'$.__in_progress__') IS NOT NULL"""
            ).fetchall()
            for command_row in in_progress_commands:
                progress = decode_json(command_row["result_json"], {})
                interrupted_error = {
                    "code": "WEBHOOK_DELIVERY_UNKNOWN",
                    "message": "runtime restarted during webhook delivery",
                    "details": {"deliveryId": progress.get("deliveryId")},
                }
                db.execute(
                    "UPDATE runtime_commands SET result_json=? WHERE command_id=?",
                    (
                        encode_json({"__error__": interrupted_error}),
                        command_row["command_id"],
                    ),
                )
                if progress.get("deliveryId"):
                    db.execute(
                        """UPDATE webhook_deliveries SET status='delivery_unknown',error_json=?
                           WHERE id=? AND status='sending'""",
                        (encode_json(interrupted_error), progress["deliveryId"]),
                    )
            db.execute(
                """UPDATE reply_work_items SET status='delivery_unknown',
                       pending_reason='runtime stopped during webhook delivery'
                   WHERE status='sending'"""
            )
            db.execute(
                """UPDATE reply_work_items SET status='closed_runtime_restarted',generation=generation+1,
                       error_json=?,pending_reason='runtime restarted during processing',completed_at=?,updated_at=?
                   WHERE status IN ('collecting','waiting_for_human_reply','queued_retrieval',
                                    'retrieving','ready_to_send')""",
                (
                    encode_json({"code": "RUNTIME_RESTARTED", "message": "processing was interrupted"}),
                    now,
                    now,
                ),
            )
            # Every desktop launch starts at the current message tail. Pending/manual
            # decisions survive, but offline backlog and interrupted automation do not.
            db.execute(
                """DELETE FROM reply_inbox
                   WHERE assigned_work_id IS NULL OR assigned_work_id LIKE 'claim:%'"""
            )
            db.execute("DELETE FROM runtime_cursors")
        self._started = True
        self._started_at = now
        self._lease_thread = threading.Thread(
            target=self._lease_loop, name="reply-runtime-lease", daemon=True
        )
        self._lease_thread.start()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="reply-runtime-monitor", daemon=True
        )
        self._monitor_thread.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_event.set()
        if self._monitor_thread and self._monitor_thread is not threading.current_thread():
            self._monitor_thread.join(timeout=10)
        if self._lease_thread and self._lease_thread is not threading.current_thread():
            self._lease_thread.join(timeout=10)
        with self.store.transaction() as db:
            now = self._now()
            lease = db.execute(
                "SELECT owner_id FROM runtime_lease WHERE singleton=1"
            ).fetchone()
            owns_lease = not self._started or bool(
                lease and lease["owner_id"] == self._owner_id
            )
            if owns_lease:
                db.execute(
                    """UPDATE reply_work_items SET status='failed',generation=generation+1,error_json=?,
                           pending_reason='runtime shutting down',updated_at=?,completed_at=?
                       WHERE status IN ('queued_retrieval','retrieving','ready_to_send')""",
                    (encode_json({"code": "RUNTIME_SHUTDOWN", "message": "processing was interrupted"}), now, now),
                )
        if hasattr(self.mcp, "close"):
            try:
                self.mcp.close()
            except Exception:
                pass
        if hasattr(self.message_source, "close"):
            try:
                self.message_source.close()
            except Exception:
                pass
        with self.store.transaction() as db:
            db.execute(
                "DELETE FROM runtime_lease WHERE singleton=1 AND owner_id=?", (self._owner_id,)
            )
        self.store.close()

    def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once(wait=False)
            except Exception as exc:
                try:
                    self._emit_event({"kind": "runtime.loop_failed", "message": str(exc)})
                except Exception:
                    pass
            self._stop_event.wait(1.0)

    def _lease_loop(self) -> None:
        while not self._stop_event.wait(3.0):
            try:
                now = self._now()
                with self.store.transaction() as db:
                    renewed = db.execute(
                        """UPDATE runtime_lease SET expires_at=?,updated_at=?
                           WHERE singleton=1 AND owner_id=?""",
                        (now + 15, now, self._owner_id),
                    ).rowcount
                if int(renewed or 0) != 1:
                    self._lease_lost = True
                    self._stop_event.set()
                    return
            except Exception:
                if self._stop_event.is_set():
                    return

    def _assert_current_lease(self, db, now: float) -> None:
        if not self._started:
            return
        lease = db.execute(
            "SELECT owner_id,expires_at FROM runtime_lease WHERE singleton=1"
        ).fetchone()
        if (
            not lease
            or lease["owner_id"] != self._owner_id
            or float(lease["expires_at"]) <= now
        ):
            self._lease_lost = True
            self._stop_event.set()
            raise RuntimeProtocolError(
                "RUNTIME_LEASE_LOST", "reply runtime no longer owns its delivery lease"
            )

    def execute(self, envelope: dict) -> dict:
        if self._lease_lost:
            raise RuntimeProtocolError("RUNTIME_LEASE_LOST", "reply runtime no longer owns its lease")
        if not isinstance(envelope, dict):
            raise RuntimeProtocolError("INVALID_COMMAND", "command payload must be an object")
        if envelope.get("protocolVersion") != 1:
            raise RuntimeProtocolError(
                "UNSUPPORTED_PROTOCOL",
                "protocolVersion must be 1",
                details={"supported": [1]},
            )
        command_id = str(envelope.get("commandId") or "").strip()
        body = envelope.get("body")
        if not command_id or not isinstance(body, dict):
            raise RuntimeProtocolError(
                "INVALID_COMMAND",
                "commandId and body are required",
            )
        request_hash = _sha256_json(
            {
                "protocolVersion": 1,
                "expectedRevision": envelope.get("expectedRevision"),
                "body": body,
            }
        )
        body_kind = str(body.get("kind") or "")
        if body_kind != "runtime.tick" and envelope.get("expectedRevision") is None:
            raise RuntimeProtocolError(
                "REVISION_REQUIRED", "expectedRevision is required for write commands"
            )
        if body_kind == "runtime.tick":
            return self._execute_tick(envelope, command_id, request_hash, body)
        if body_kind == "mcp.test":
            return self._execute_mcp_test_command(envelope, command_id, request_hash, body)
        if body_kind == "listener.test_webhook":
            return self._execute_webhook_test_command(envelope, command_id, request_hash, body)
        if body_kind in {"work.send", "work.send_plain_at"}:
            return self._execute_delivery_command(envelope, command_id, request_hash, body)
        with self.store.transaction() as db:
            previous = db.execute(
                "SELECT request_hash, result_json FROM runtime_commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise RuntimeProtocolError(
                        "COMMAND_ID_REUSED",
                        "commandId was already used for a different command",
                    )
                return _command_result(decode_json(previous["result_json"], {}))

            expected = envelope.get("expectedRevision")
            current = self.store.revision(db)
            if expected is not None and int(expected) != current:
                raise RuntimeProtocolError(
                    "REVISION_CONFLICT",
                    f"configuration revision changed from {expected} to {current}",
                    retryable=True,
                    details={"expectedRevision": int(expected), "actualRevision": current},
                )

            result = self._execute_body(db, body)
            db.execute(
                "INSERT INTO runtime_commands(command_id, request_hash, result_json, created_at) VALUES (?, ?, ?, ?)",
                (command_id, request_hash, encode_json(result), self._now()),
            )
            return result

    def query(self, query: dict) -> dict:
        if not isinstance(query, dict):
            raise RuntimeProtocolError("INVALID_QUERY", "query payload must be an object")
        if "protocolVersion" in query:
            if query.get("protocolVersion") != 1:
                raise RuntimeProtocolError(
                    "UNSUPPORTED_PROTOCOL", "protocolVersion must be 1", details={"supported": [1]}
                )
            body = query.get("body")
            if not isinstance(body, dict):
                raise RuntimeProtocolError("INVALID_QUERY", "query body must be an object")
            query = body
        kind = str(query.get("kind") or "")
        if kind == "runtime.snapshot":
            return self._runtime_snapshot()
        if kind == "mcp.list":
            with self.store.lock:
                rows = self.store.connection.execute(
                    "SELECT * FROM mcp_servers ORDER BY lower(json_extract(public_json, '$.name')), id"
                ).fetchall()
                return {
                    "revision": self.store.revision(),
                    "servers": [self._public_server(row) for row in rows],
                }
        if kind == "mcp.catalog":
            server_id = str(query.get("serverId") or "").strip()
            with self.store.lock:
                if not server_id:
                    rows = self.store.connection.execute(
                        "SELECT * FROM mcp_servers ORDER BY id"
                    ).fetchall()
                    return {
                        "revision": self.store.revision(),
                        "catalogs": [
                            {
                                "serverId": row["id"],
                                "tools": decode_json(row["catalog_json"], []),
                                "updatedAt": _iso_time(row["catalog_updated_at"]),
                                "error": decode_json(row["catalog_error_json"], None),
                            }
                            for row in rows
                        ],
                    }
                row = self.store.connection.execute(
                    "SELECT * FROM mcp_servers WHERE id = ?", (server_id,)
                ).fetchone()
                if not row:
                    raise RuntimeProtocolError("MCP_NOT_FOUND", f"MCP server not found: {server_id}")
                return {
                    "revision": self.store.revision(),
                    "serverId": server_id,
                    "tools": decode_json(row["catalog_json"], []),
                    "updatedAt": _iso_time(row["catalog_updated_at"]),
                    "error": decode_json(row["catalog_error_json"], None),
                }
        if kind == "listener.list":
            with self.store.lock:
                rows = self.store.connection.execute(
                    """SELECT * FROM reply_listeners
                       WHERE coalesce(json_extract(public_json, '$.deleted'),0)=0
                       ORDER BY lower(json_extract(public_json, '$.name')), id"""
                ).fetchall()
                return {
                    "revision": self.store.revision(),
                    "listeners": [self._public_listener(row) for row in rows],
                }
        if kind == "work.list":
            return self._query_work_list(query)
        if kind == "work.detail":
            work_id = str(query.get("workId") or "").strip()
            with self.store.lock:
                row = self.store.connection.execute(
                    "SELECT * FROM reply_work_items WHERE id=?", (work_id,)
                ).fetchone()
                if not row:
                    raise RuntimeProtocolError("WORK_NOT_FOUND", f"work item not found: {work_id}")
                return {"revision": self.store.revision(), "item": self._public_work(row, detail=True)}
        raise RuntimeProtocolError("UNKNOWN_QUERY", f"unknown runtime query: {kind}")

    def _runtime_snapshot(self) -> dict:
        with self.store.lock:
            db = self.store.connection
            pending = db.execute(
                "SELECT count(*) FROM reply_work_items WHERE status IN ('pending','delivery_unknown','delivery_failed')"
            ).fetchone()[0]
            active = db.execute(
                "SELECT count(*) FROM reply_work_items WHERE status='retrieving'"
            ).fetchone()[0]
            queued = db.execute(
                "SELECT count(*) FROM reply_work_items WHERE status='queued_retrieval'"
            ).fetchone()[0]
            recent_failures = db.execute(
                "SELECT count(*) FROM reply_work_items WHERE status IN ('failed','delivery_failed') AND updated_at>?",
                (self._now() - 86400,),
            ).fetchone()[0]
            seq = int(db.execute("SELECT value FROM runtime_meta WHERE key='event_seq'").fetchone()[0])
            return {
                "protocolVersion": 1,
                "revision": self.store.revision(),
                "running": self._started and not self._closed and not self._lease_lost,
                "startedAt": _iso_time(self._started_at),
                "activeRetrievals": int(active),
                "queuedRetrievals": int(queued),
                "pendingCount": int(pending),
                "recentFailures": int(recent_failures),
                "lastEventSeq": seq,
            }

    def _execute_body(self, db, body: dict) -> dict:
        kind = str(body.get("kind") or "")
        if kind == "mcp.save":
            return self._save_mcp(db, body)
        if kind == "mcp.test":
            return self._test_mcp(db, body)
        if kind == "mcp.delete":
            return self._delete_mcp(db, body)
        if kind == "listener.save":
            return self._save_listener(db, body)
        if kind == "listener.delete":
            return self._delete_listener(db, body)
        if kind == "listener.test_webhook":
            return self._test_listener_webhook(db, body)
        if kind == "listener.confirm_webhook":
            return self._confirm_listener_webhook(db, body)
        if kind == "work.discard":
            return self._discard_work(db, body)
        raise RuntimeProtocolError("UNKNOWN_COMMAND", f"unknown runtime command: {kind}")

    def _save_mcp(self, db, body: dict) -> dict:
        raw = body.get("server")
        if not isinstance(raw, dict):
            raise RuntimeProtocolError("INVALID_MCP_SERVER", "server must be an object")
        server_id = str(raw.get("id") or uuid.uuid4()).strip()
        if not ID_PATTERN.fullmatch(server_id):
            raise RuntimeProtocolError("INVALID_MCP_SERVER", "server id has an invalid format")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise RuntimeProtocolError("INVALID_MCP_SERVER", "server name is required")
        transport = str(raw.get("transportType") or raw.get("transport") or "").lower().strip()
        if transport not in MCP_TRANSPORTS:
            raise RuntimeProtocolError(
                "INVALID_MCP_SERVER",
                "transportType must be stdio, sse, or streamable-http",
            )
        if transport == "stdio":
            command = str(raw.get("command") or "").strip()
            if not command:
                raise RuntimeProtocolError("INVALID_MCP_SERVER", "stdio command is required")
            args = raw.get("args") or []
            if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
                raise RuntimeProtocolError("INVALID_MCP_SERVER", "stdio args must be strings")
            public = {
                "id": server_id,
                "name": name,
                "enabled": bool(raw.get("enabled", True)),
                "transportType": transport,
                "command": command,
                "args": args,
                "cwd": str(raw.get("cwd") or "").strip(),
            }
        else:
            url = str(raw.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                raise RuntimeProtocolError("INVALID_MCP_SERVER", "HTTP MCP URL is required")
            public = {
                "id": server_id,
                "name": name,
                "enabled": bool(raw.get("enabled", True)),
                "transportType": transport,
                "url": url,
            }

        existing = db.execute("SELECT * FROM mcp_servers WHERE id = ?", (server_id,)).fetchone()
        if existing and any(name in raw for name in ("headers", "env")):
            raise RuntimeProtocolError(
                "INVALID_SECRET_UPDATE",
                "existing MCP secrets must use keep, replace, or clear patch semantics",
            )
        secrets = decode_json(existing["secret_json"], {}) if existing else {}
        secrets = _merge_secret(secrets, "headers", raw, body)
        secrets = _merge_secret(secrets, "env", raw, body)
        connection_fingerprint = _mcp_connection_fingerprint(public, secrets)
        previous_public = decode_json(existing["public_json"], {}) if existing else {}
        previous_fingerprint = (
            str(existing["connection_fingerprint"] or "")
            if existing else ""
        ) or (_mcp_connection_fingerprint(previous_public, decode_json(existing["secret_json"], {})) if existing else "")
        connection_changed = bool(existing and previous_fingerprint != connection_fingerprint)
        enabled_changed = bool(
            existing and bool(previous_public.get("enabled", True)) != bool(public.get("enabled", True))
        )
        revision = self.store.bump_revision(db)
        now = self._now()
        if existing:
            if connection_changed:
                db.execute(
                    """UPDATE mcp_servers SET public_json=?,secret_json=?,connection_fingerprint=?,
                           catalog_json='[]',catalog_connection_fingerprint='',catalog_error_json=?,
                           catalog_updated_at=NULL,revision=?,updated_at=? WHERE id=?""",
                    (
                        encode_json(public), encode_json(secrets), connection_fingerprint,
                        encode_json({"code": "MCP_REDISCOVERY_REQUIRED", "message": "MCP connection changed; test and rediscover tools"}),
                        revision, now, server_id,
                    ),
                )
            else:
                db.execute(
                    """UPDATE mcp_servers SET public_json=?,secret_json=?,connection_fingerprint=?,
                           revision=?,updated_at=? WHERE id=?""",
                    (encode_json(public), encode_json(secrets), connection_fingerprint, revision, now, server_id),
                )
        else:
            db.execute(
                """INSERT INTO mcp_servers(
                       id,public_json,secret_json,revision,connection_fingerprint,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?)""",
                (server_id, encode_json(public), encode_json(secrets), revision, connection_fingerprint, now, now),
            )
        if connection_changed or enabled_changed:
            self._invalidate_mcp_dependents(
                db,
                server_id,
                revision,
                "MCP_CONFIGURATION_CHANGED",
                "MCP capability configuration changed",
                revoke_grants=connection_changed,
            )
        row = db.execute("SELECT * FROM mcp_servers WHERE id = ?", (server_id,)).fetchone()
        return {"revision": revision, "server": self._public_server(row)}

    def _test_mcp(self, db, body: dict) -> dict:
        server_id = str(body.get("serverId") or "").strip()
        row = db.execute("SELECT * FROM mcp_servers WHERE id = ?", (server_id,)).fetchone()
        if not row:
            raise RuntimeProtocolError("MCP_NOT_FOUND", f"MCP server not found: {server_id}")
        if self.mcp is None:
            raise RuntimeProtocolError(
                "MCP_ADAPTER_UNAVAILABLE",
                "MCP support is unavailable in this worker",
            )
        server = decode_json(row["public_json"], {})
        server["secrets"] = decode_json(row["secret_json"], {})
        try:
            discovered = self.mcp.discover(server)
        except RuntimeProtocolError:
            raise
        except Exception as exc:
            raise RuntimeProtocolError(
                "MCP_CONNECTION_FAILED",
                f"could not connect to MCP server: {exc}",
                retryable=True,
            ) from exc
        tools = _normalize_tools(discovered)
        revision = self.store.bump_revision(db)
        now = self._now()
        db.execute(
            "UPDATE mcp_servers SET catalog_json=?, catalog_error_json=NULL, catalog_updated_at=?, revision=?, updated_at=? WHERE id=?",
            (encode_json(tools), now, revision, now, server_id),
        )
        return {
            "revision": revision,
            "serverId": server_id,
            "connected": True,
            "tools": tools,
        }

    def _delete_mcp(self, db, body: dict) -> dict:
        server_id = str(body.get("serverId") or body.get("id") or "").strip()
        if not db.execute("SELECT 1 FROM mcp_servers WHERE id=?", (server_id,)).fetchone():
            raise RuntimeProtocolError("MCP_NOT_FOUND", f"MCP server not found: {server_id}")
        revision = self.store.bump_revision(db)
        self._invalidate_mcp_dependents(
            db,
            server_id,
            revision,
            "MCP_SERVER_DELETED",
            "MCP server was deleted",
            revoke_grants=True,
        )
        db.execute("DELETE FROM mcp_servers WHERE id=?", (server_id,))
        return {"revision": revision, "serverId": server_id, "deleted": True}

    def _save_listener(self, db, body: dict) -> dict:
        raw = body.get("listener")
        if not isinstance(raw, dict):
            raise RuntimeProtocolError("INVALID_LISTENER", "listener must be an object")
        listener_id = str(raw.get("id") or uuid.uuid4()).strip()
        if not ID_PATTERN.fullmatch(listener_id):
            raise RuntimeProtocolError("INVALID_LISTENER", "listener id has an invalid format")
        name = str(raw.get("name") or "").strip()
        group_id = str(raw.get("groupId") or "").strip()
        if not name or not group_id:
            raise RuntimeProtocolError("INVALID_LISTENER", "listener name and groupId are required")
        enabled = bool(raw.get("enabled", False))
        if enabled:
            duplicate = db.execute(
                "SELECT id FROM reply_listeners WHERE id <> ? AND json_extract(public_json, '$.groupId') = ? AND json_extract(public_json, '$.enabled') = 1",
                (listener_id, group_id),
            ).fetchone()
            if duplicate:
                raise RuntimeProtocolError(
                    "GROUP_ALREADY_LISTENED",
                    "only one enabled listener is allowed for a group",
                    details={"listenerId": duplicate["id"], "groupId": group_id},
                )

        grants = raw.get("toolGrants") or []
        if not isinstance(grants, list) or not grants:
            raise RuntimeProtocolError("INVALID_TOOL_GRANT", "at least one MCP tool must be granted")
        normalized_grants = []
        for grant in grants:
            normalized_grants.append(self._validate_grant(db, grant))

        public = {
            "id": listener_id,
            "name": name,
            "groupId": group_id,
            "groupName": str(raw.get("groupName") or group_id).strip() or group_id,
            "enabled": enabled,
            "toolGrants": normalized_grants,
            "systemPrompt": str(raw.get("systemPrompt") or "").strip(),
            "pollIntervalSeconds": _bounded_int(raw, "pollIntervalSeconds", 5, 2, 60),
            "sameSenderMergeSeconds": _bounded_int(raw, "sameSenderMergeSeconds", 20, 2, 120),
            "humanReplyWaitSeconds": _bounded_int(raw, "humanReplyWaitSeconds", 120, 10, 3600),
            "sessionTimeoutSeconds": _bounded_int(raw, "sessionTimeoutSeconds", 1800, 60, 86400),
            "maxConcurrency": _bounded_int(raw, "maxConcurrency", 4, 1, 20),
            "mcpTimeoutSeconds": _bounded_int(raw, "mcpTimeoutSeconds", 900, 60, 1800),
            "autoSend": bool(raw.get("autoSend", False)),
        }
        existing = db.execute("SELECT * FROM reply_listeners WHERE id=?", (listener_id,)).fetchone()
        if existing and "webhookUrl" in raw:
            raise RuntimeProtocolError(
                "INVALID_SECRET_UPDATE",
                "existing webhook secrets must use keep, replace, or clear patch semantics",
            )
        old_public = decode_json(existing["public_json"], {}) if existing else {}
        old_url = str(existing["webhook_url"] or "") if existing else ""
        webhook_url = _secret_string_update(old_url, raw, body, "webhookUrl")
        if webhook_url and not webhook_url.startswith(WECOM_WEBHOOK_PREFIX):
            raise RuntimeProtocolError(
                "INVALID_WEBHOOK_URL",
                "webhookUrl must be an official WeCom group robot URL",
            )
        fingerprint = _fingerprint(webhook_url)
        confirmed = bool(
            existing
            and existing["webhook_confirmed_fingerprint"] == fingerprint
            and existing["webhook_confirmed_group_id"] == group_id
        )
        if public["autoSend"] and not confirmed:
            raise RuntimeProtocolError(
                "WEBHOOK_CONFIRMATION_REQUIRED",
                "automatic sending requires a visible webhook test confirmation",
            )
        revision = self.store.bump_revision(db)
        now = self._now()
        generation = int(existing["generation"] or 0) + 1 if existing else 1
        if existing:
            reset_confirmation = old_url != webhook_url or old_public.get("groupId") != group_id
            reset_cursor = (
                old_public.get("groupId") != group_id
                or bool(old_public.get("enabled", False)) != enabled
            )
            db.execute(
                """UPDATE reply_listeners
                   SET public_json=?, webhook_url=?, webhook_fingerprint=?, revision=?, generation=?, updated_at=?,
                       webhook_test_code=CASE WHEN ? THEN NULL ELSE webhook_test_code END,
                       webhook_tested_at=CASE WHEN ? THEN NULL ELSE webhook_tested_at END,
                       webhook_confirmed_fingerprint=CASE WHEN ? THEN NULL ELSE webhook_confirmed_fingerprint END,
                       webhook_confirmed_group_id=CASE WHEN ? THEN NULL ELSE webhook_confirmed_group_id END,
                       webhook_confirmed_at=CASE WHEN ? THEN NULL ELSE webhook_confirmed_at END
                   WHERE id=?""",
                (
                    encode_json(public), webhook_url, fingerprint, revision, generation, now,
                    reset_confirmation, reset_confirmation, reset_confirmation, reset_confirmation,
                    reset_confirmation, listener_id,
                ),
            )
            if reset_cursor:
                db.execute("DELETE FROM runtime_cursors WHERE listener_id=?", (listener_id,))
                db.execute(
                    "DELETE FROM reply_inbox WHERE listener_id=? AND assigned_work_id IS NULL",
                    (listener_id,),
                )
            if old_public.get("groupId") != group_id:
                db.execute("DELETE FROM sender_sessions WHERE listener_id=?", (listener_id,))
                db.execute("DELETE FROM reply_inbox WHERE listener_id=?", (listener_id,))
        else:
            db.execute(
                """INSERT INTO reply_listeners(
                       id, public_json, webhook_url, webhook_fingerprint, revision, generation, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (listener_id, encode_json(public), webhook_url, fingerprint, revision, generation, now, now),
            )
        if existing:
            self._close_listener_work(
                db,
                listener_id,
                "LISTENER_CONFIGURATION_CHANGED",
                "listener configuration changed",
            )
        row = db.execute("SELECT * FROM reply_listeners WHERE id=?", (listener_id,)).fetchone()
        return {"revision": revision, "listener": self._public_listener(row)}

    def _delete_listener(self, db, body: dict) -> dict:
        listener_id = str(body.get("listenerId") or body.get("id") or "").strip()
        if not db.execute("SELECT 1 FROM reply_listeners WHERE id=?", (listener_id,)).fetchone():
            raise RuntimeProtocolError("LISTENER_NOT_FOUND", f"listener not found: {listener_id}")
        row = db.execute("SELECT * FROM reply_listeners WHERE id=?", (listener_id,)).fetchone()
        public = decode_json(row["public_json"], {})
        public["enabled"] = False
        public["deleted"] = True
        revision = self.store.bump_revision(db)
        db.execute(
            "UPDATE reply_listeners SET public_json=?,revision=?,generation=generation+1,updated_at=? WHERE id=?",
            (encode_json(public), revision, self._now(), listener_id),
        )
        self._close_listener_work(
            db, listener_id, "LISTENER_DELETED", "listener was deleted"
        )
        return {"revision": revision, "listenerId": listener_id, "deleted": True}

    def _invalidate_mcp_dependents(
        self,
        db,
        server_id: str,
        revision: int,
        code: str,
        message: str,
        *,
        revoke_grants: bool = False,
        current_tools: list[dict] | None = None,
    ) -> None:
        rows = db.execute("SELECT * FROM reply_listeners").fetchall()
        for row in rows:
            listener = decode_json(row["public_json"], {})
            grants = listener.get("toolGrants") or []
            dependent = [
                grant for grant in grants
                if isinstance(grant, dict) and str(grant.get("serverId") or "") == server_id
            ]
            if not dependent:
                continue
            if revoke_grants or current_tools is not None:
                tool_hashes = {
                    str(tool.get("name") or ""): str(tool.get("schemaSha256") or "")
                    for tool in current_tools or []
                }
                updated_grants = []
                for grant in grants:
                    updated = dict(grant) if isinstance(grant, dict) else grant
                    if isinstance(updated, dict) and str(updated.get("serverId") or "") == server_id:
                        mismatched = revoke_grants or tool_hashes.get(str(updated.get("toolName") or "")) != str(
                            updated.get("schemaSha256") or ""
                        )
                        if mismatched:
                            updated["invalidated"] = True
                            updated["invalidatedReason"] = code
                    updated_grants.append(updated)
                listener["toolGrants"] = updated_grants
            db.execute(
                """UPDATE reply_listeners SET public_json=?,generation=generation+1,revision=?,updated_at=?
                   WHERE id=?""",
                (encode_json(listener), revision, self._now(), row["id"]),
            )
            db.execute("DELETE FROM runtime_cursors WHERE listener_id=?", (row["id"],))
            db.execute(
                "DELETE FROM reply_inbox WHERE listener_id=? AND assigned_work_id IS NULL",
                (row["id"],),
            )
            self._close_listener_work(db, row["id"], code, message)

    def _close_listener_work(self, db, listener_id: str, code: str, message: str) -> None:
        now = self._now()
        db.execute(
            """UPDATE reply_work_items
               SET status='closed_configuration_changed',generation=generation+1,error_json=?,
                   pending_reason=?,updated_at=?,completed_at=?
               WHERE listener_id=?
                 AND status IN ('collecting','waiting_for_human_reply','queued_retrieval',
                                'retrieving','ready_to_send','pending','delivery_failed')""",
            (encode_json({"code": code, "message": message}), message, now, now, listener_id),
        )

    def _discard_work(self, db, body: dict) -> dict:
        work_id = str(body.get("workId") or "").strip()
        row = db.execute("SELECT * FROM reply_work_items WHERE id=?", (work_id,)).fetchone()
        if not row:
            raise RuntimeProtocolError("WORK_NOT_FOUND", f"work item not found: {work_id}")
        expected = body.get("expectedVersion")
        if expected is not None and int(expected) != int(row["generation"]):
            raise RuntimeProtocolError("WORK_VERSION_CONFLICT", "work item changed; refresh before acting", retryable=True)
        if row["status"] not in {"pending", "delivery_failed", "delivery_unknown"}:
            raise RuntimeProtocolError("WORK_NOT_PENDING", "only a pending reply can be discarded")
        now = self._now()
        db.execute(
            """UPDATE reply_work_items SET status='discarded',generation=generation+1,
                   pending_reason='discarded_by_user',updated_at=?,completed_at=? WHERE id=?""",
            (now, now, work_id),
        )
        return {"workId": work_id, "status": "discarded", "version": int(row["generation"]) + 1}

    def _test_listener_webhook(self, db, body: dict) -> dict:
        listener_id = str(body.get("listenerId") or "").strip()
        row = db.execute("SELECT * FROM reply_listeners WHERE id=?", (listener_id,)).fetchone()
        if not row:
            raise RuntimeProtocolError("LISTENER_NOT_FOUND", f"listener not found: {listener_id}")
        if not row["webhook_url"]:
            raise RuntimeProtocolError("WEBHOOK_NOT_CONFIGURED", "listener webhook is not configured")
        if self.webhook is None:
            raise RuntimeProtocolError("WEBHOOK_ADAPTER_UNAVAILABLE", "webhook adapter is unavailable")
        self._enforce_webhook_rate(db, row["webhook_fingerprint"])
        code = f"WIR-{secrets.token_hex(3).upper()}"
        delivery_id = str(uuid.uuid4())
        text = f"企微问题雷达 webhook 测试码：{code}\n请确认此消息出现在所选群后再开启自动发送。"
        now = self._now()
        try:
            response = self.webhook.send(
                webhookUrl=row["webhook_url"], text=text, mentionedList=[],
                mentionedMobileList=[], timeoutSeconds=15, deliveryId=delivery_id,
            )
            status = _delivery_status(response)
        except TimeoutError as exc:
            status, response = "delivery_unknown", {"message": str(exc)}
        except Exception as exc:
            db.execute(
                "INSERT INTO webhook_deliveries VALUES(?,?,?,?,?,?,?,?,?)",
                (delivery_id, row["webhook_fingerprint"], listener_id, None, "test", "failed", None,
                 encode_json({"message": str(exc)}), now),
            )
            raise RuntimeProtocolError("WEBHOOK_SEND_FAILED", f"webhook test failed: {exc}", retryable=True) from exc
        db.execute(
            "INSERT INTO webhook_deliveries VALUES(?,?,?,?,?,?,?,?,?)",
            (delivery_id, row["webhook_fingerprint"], listener_id, None, "test", status,
             encode_json(response), None, now),
        )
        if status != "sent":
            raise RuntimeProtocolError(
                "WEBHOOK_DELIVERY_UNKNOWN" if status == "delivery_unknown" else "WEBHOOK_SEND_FAILED",
                "the webhook test could not be confirmed as delivered",
                retryable=status != "delivery_unknown",
            )
        revision = self.store.bump_revision(db)
        db.execute(
            """UPDATE reply_listeners SET webhook_test_code=?,webhook_tested_at=?,revision=?,updated_at=?
               WHERE id=?""",
            (code, now, revision, now, listener_id),
        )
        return {
            "revision": revision, "listenerId": listener_id, "testCode": code,
            "code": code, "challengeId": code,
            "testedAt": now, "delivered": True,
        }

    def _confirm_listener_webhook(self, db, body: dict) -> dict:
        listener_id = str(body.get("listenerId") or "").strip()
        code = str(body.get("testCode") or body.get("challengeId") or "").strip()
        if body.get("appearedInSelectedGroup") is not True:
            raise RuntimeProtocolError(
                "WEBHOOK_NOT_VISIBLE",
                "automatic sending cannot be enabled until the test appears in the selected group",
            )
        row = db.execute("SELECT * FROM reply_listeners WHERE id=?", (listener_id,)).fetchone()
        if not row:
            raise RuntimeProtocolError("LISTENER_NOT_FOUND", f"listener not found: {listener_id}")
        if not code or code != str(row["webhook_test_code"] or ""):
            raise RuntimeProtocolError("WEBHOOK_TEST_CODE_MISMATCH", "webhook test code does not match")
        if row["webhook_tested_at"] is None or self._now() - float(row["webhook_tested_at"]) > 1800:
            raise RuntimeProtocolError("WEBHOOK_TEST_EXPIRED", "webhook test confirmation expired")
        public = decode_json(row["public_json"], {})
        now = self._now()
        revision = self.store.bump_revision(db)
        db.execute(
            """UPDATE reply_listeners SET webhook_confirmed_fingerprint=?,webhook_confirmed_group_id=?,
                   webhook_confirmed_at=?,revision=?,updated_at=? WHERE id=?""",
            (row["webhook_fingerprint"], public.get("groupId") or "", now, revision, now, listener_id),
        )
        return {"revision": revision, "listenerId": listener_id, "confirmed": True, "confirmedAt": now}

    def _enforce_webhook_rate(self, db, fingerprint: str) -> None:
        count = db.execute(
            "SELECT count(*) FROM webhook_deliveries WHERE webhook_fingerprint=? AND attempted_at>?",
            (fingerprint, self._now() - 60),
        ).fetchone()[0]
        if int(count) >= 20:
            raise RuntimeProtocolError(
                "WEBHOOK_RATE_LIMITED",
                "this webhook has reached the 20 messages per minute safety limit",
                retryable=True,
            )

    def _validate_grant(self, db, grant: dict) -> dict:
        if not isinstance(grant, dict):
            raise RuntimeProtocolError("INVALID_TOOL_GRANT", "tool grant must be an object")
        server_id = str(grant.get("serverId") or "").strip()
        tool_name = str(grant.get("toolName") or "").strip()
        schema_hash = str(grant.get("schemaSha256") or "").strip().lower()
        row = db.execute(
            """SELECT public_json,catalog_json,catalog_error_json,connection_fingerprint,
                      catalog_connection_fingerprint
               FROM mcp_servers WHERE id=?""",
            (server_id,),
        ).fetchone()
        tools = decode_json(row["catalog_json"], []) if row else []
        matched = next((tool for tool in tools if tool.get("name") == tool_name), None)
        server = decode_json(row["public_json"], {}) if row else {}
        catalog_ready = bool(
            row
            and server.get("enabled", True)
            and row["catalog_error_json"] is None
            and str(row["connection_fingerprint"] or "")
            == str(row["catalog_connection_fingerprint"] or "")
        )
        if not catalog_ready or not matched or matched.get("schemaSha256") != schema_hash:
            raise RuntimeProtocolError(
                "INVALID_TOOL_GRANT",
                f"tool grant no longer matches discovered schema: {server_id}/{tool_name}",
            )
        return {"serverId": server_id, "toolName": tool_name, "schemaSha256": schema_hash}

    def _public_listener(self, row) -> dict:
        result = decode_json(row["public_json"], {})
        fingerprint = str(row["webhook_fingerprint"] or "")
        group_id = str(result.get("groupId") or "")
        confirmed = bool(
            fingerprint
            and row["webhook_confirmed_fingerprint"] == fingerprint
            and row["webhook_confirmed_group_id"] == group_id
        )
        result["revision"] = int(row["revision"])
        result["generation"] = int(row["generation"])
        result["webhook"] = {
            "configured": bool(row["webhook_url"]),
            "fingerprint": fingerprint,
            "testedAt": _iso_time(row["webhook_tested_at"]),
            "confirmed": confirmed,
            "confirmedAt": _iso_time(row["webhook_confirmed_at"]) if confirmed else None,
        }
        result["health"] = self._listener_health(result)
        result["pendingCount"] = int(
            self.store.connection.execute(
                """SELECT count(*) FROM reply_work_items
                   WHERE listener_id=? AND status IN ('pending','delivery_unknown','delivery_failed')""",
                (row["id"],),
            ).fetchone()[0]
        )
        cursor = self.store.connection.execute(
            "SELECT updated_at FROM runtime_cursors WHERE listener_id=?", (row["id"],)
        ).fetchone()
        result["lastPollAt"] = _iso_time(cursor["updated_at"]) if cursor else None
        return result

    def _listener_health(self, listener: dict) -> dict:
        for grant in listener.get("toolGrants") or []:
            if grant.get("invalidated"):
                status = (
                    "tool_schema_changed"
                    if grant.get("invalidatedReason") == "MCP_CATALOG_CHANGED"
                    else "tool_grant_invalidated"
                )
                return {"status": status, "grant": grant}
            row = self.store.connection.execute(
                """SELECT public_json,catalog_json,catalog_error_json,connection_fingerprint,
                          catalog_connection_fingerprint FROM mcp_servers WHERE id=?""",
                (grant.get("serverId"),),
            ).fetchone()
            if not row:
                return {"status": "missing_server", "grant": grant}
            server = decode_json(row["public_json"], {})
            if not server.get("enabled"):
                return {"status": "disabled_server", "grant": grant}
            if row["catalog_error_json"] is not None:
                return {"status": "server_unhealthy", "grant": grant}
            if str(row["catalog_connection_fingerprint"] or "") != str(
                row["connection_fingerprint"] or ""
            ):
                return {"status": "rediscovery_required", "grant": grant}
            tool = next(
                (item for item in decode_json(row["catalog_json"], []) if item.get("name") == grant.get("toolName")),
                None,
            )
            if not tool:
                return {"status": "missing_tool", "grant": grant}
            if tool.get("schemaSha256") != grant.get("schemaSha256"):
                return {"status": "tool_schema_changed", "grant": grant}
        return {"status": "ready"}

    def _execute_tick(self, envelope: dict, command_id: str, request_hash: str, body: dict) -> dict:
        with self.store.transaction() as db:
            previous = db.execute(
                "SELECT request_hash, result_json FROM runtime_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise RuntimeProtocolError(
                        "COMMAND_ID_REUSED",
                        "commandId was already used for a different command",
                    )
                return _command_result(decode_json(previous["result_json"], {}))
            current = self.store.revision(db)
            expected = envelope.get("expectedRevision")
            if expected is not None and int(expected) != current:
                raise RuntimeProtocolError(
                    "REVISION_CONFLICT",
                    f"configuration revision changed from {expected} to {current}",
                    retryable=True,
                    details={"expectedRevision": int(expected), "actualRevision": current},
                )
        result = self.run_once(wait=bool(body.get("wait", False)))
        with self.store.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO runtime_commands(command_id, request_hash, result_json, created_at) VALUES (?, ?, ?, ?)",
                (command_id, request_hash, encode_json(result), self._now()),
            )
        return result

    def _execute_mcp_test_command(
        self, envelope: dict, command_id: str, request_hash: str, body: dict
    ) -> dict:
        """Discover tools without holding the SQLite write lock across network I/O."""

        server_id = str(body.get("serverId") or "").strip()
        with self.store.transaction() as db:
            previous = db.execute(
                "SELECT request_hash,result_json FROM runtime_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise RuntimeProtocolError(
                        "COMMAND_ID_REUSED", "commandId was already used for a different command"
                    )
                return _command_result(decode_json(previous["result_json"], {}))
            current = self.store.revision(db)
            expected = envelope.get("expectedRevision")
            if expected is not None and int(expected) != current:
                raise RuntimeProtocolError(
                    "REVISION_CONFLICT",
                    "configuration changed; refresh before testing MCP",
                    retryable=True,
                    details={"expectedRevision": int(expected), "actualRevision": current},
                )
            row = db.execute("SELECT * FROM mcp_servers WHERE id=?", (server_id,)).fetchone()
            if not row:
                raise RuntimeProtocolError("MCP_NOT_FOUND", f"MCP server not found: {server_id}")
            if self.mcp is None:
                raise RuntimeProtocolError(
                    "MCP_ADAPTER_UNAVAILABLE", "MCP support is unavailable in this worker"
                )
            server = decode_json(row["public_json"], {})
            server["secrets"] = decode_json(row["secret_json"], {})
            connection_fingerprint = str(row["connection_fingerprint"] or "") or _mcp_connection_fingerprint(
                server, server["secrets"]
            )

        try:
            tools = self.redact_public(_normalize_tools(self.mcp.discover(server)))
            discovery_error = None
        except Exception as exc:
            tools = None
            discovery_error = RuntimeProtocolError(
                "MCP_CONNECTION_FAILED",
                "could not connect to or discover tools from the MCP server",
                retryable=True,
            )

        now = self._now()
        with self.store.transaction() as db:
            previous = db.execute(
                "SELECT request_hash,result_json FROM runtime_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise RuntimeProtocolError(
                        "COMMAND_ID_REUSED", "commandId was already used for a different command"
                    )
                return _command_result(decode_json(previous["result_json"], {}))
            expected = envelope.get("expectedRevision")
            current_revision = self.store.revision(db)
            if expected is not None and int(expected) != current_revision:
                raise RuntimeProtocolError(
                    "REVISION_CONFLICT",
                    "configuration changed while the MCP test was running",
                    retryable=True,
                    details={
                        "expectedRevision": int(expected),
                        "actualRevision": current_revision,
                    },
                )
            row = db.execute("SELECT * FROM mcp_servers WHERE id=?", (server_id,)).fetchone()
            current_fingerprint = str(row["connection_fingerprint"] or "") if row else ""
            if not row or current_fingerprint != connection_fingerprint:
                raise RuntimeProtocolError(
                    "MCP_CONFIGURATION_CHANGED",
                    "MCP configuration changed while the connection test was running",
                    retryable=True,
                )
            revision = self.store.bump_revision(db)
            if discovery_error is not None:
                public_error = discovery_error.as_dict()
                db.execute(
                    """UPDATE mcp_servers SET catalog_error_json=?,revision=?,updated_at=?
                       WHERE id=?""",
                    (encode_json(public_error), revision, now, server_id),
                )
                self._invalidate_mcp_dependents(
                    db,
                    server_id,
                    revision,
                    "MCP_CONNECTION_FAILED",
                    "MCP connection or tool discovery failed",
                )
                db.execute(
                    "INSERT INTO runtime_commands(command_id,request_hash,result_json,created_at) VALUES(?,?,?,?)",
                    (
                        command_id,
                        request_hash,
                        encode_json({"__error__": public_error}),
                        now,
                    ),
                )
            else:
                old_tools = decode_json(row["catalog_json"], [])
                catalog_changed = (
                    old_tools != tools
                    or str(row["catalog_connection_fingerprint"] or "") != connection_fingerprint
                    or row["catalog_error_json"] is not None
                )
                db.execute(
                    """UPDATE mcp_servers SET catalog_json=?,catalog_connection_fingerprint=?,
                           catalog_error_json=NULL,catalog_updated_at=?,revision=?,updated_at=? WHERE id=?""",
                    (encode_json(tools), connection_fingerprint, now, revision, now, server_id),
                )
                if catalog_changed:
                    self._invalidate_mcp_dependents(
                        db,
                        server_id,
                        revision,
                        "MCP_CATALOG_CHANGED",
                        "MCP tool catalog or schema changed",
                        current_tools=tools,
                    )
                result = {
                    "revision": revision,
                    "serverId": server_id,
                    "connected": True,
                    "tools": tools,
                }
                db.execute(
                    "INSERT INTO runtime_commands(command_id,request_hash,result_json,created_at) VALUES(?,?,?,?)",
                    (command_id, request_hash, encode_json(result), now),
                )
        if discovery_error is not None:
            raise discovery_error
        return result

    def _execute_webhook_test_command(
        self, envelope: dict, command_id: str, request_hash: str, body: dict
    ) -> dict:
        listener_id = str(body.get("listenerId") or "").strip()
        now = self._now()
        code = f"WIR-{secrets.token_hex(3).upper()}"
        delivery_id = str(uuid.uuid4())
        reservation_token = str(uuid.uuid4())
        text = f"企微问题雷达 webhook 测试码：{code}\n请确认此消息出现在所选群后再开启自动发送。"
        with self.store.transaction() as db:
            self._assert_current_lease(db, now)
            previous = db.execute(
                "SELECT request_hash,result_json FROM runtime_commands WHERE command_id=?", (command_id,)
            ).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise RuntimeProtocolError(
                        "COMMAND_ID_REUSED", "commandId was already used for a different command"
                    )
                return _command_result(decode_json(previous["result_json"], {}))
            current = self.store.revision(db)
            expected = envelope.get("expectedRevision")
            if expected is not None and int(expected) != current:
                raise RuntimeProtocolError(
                    "REVISION_CONFLICT", "configuration changed; refresh before testing",
                    retryable=True,
                    details={"expectedRevision": int(expected), "actualRevision": current},
                )
            listener = db.execute("SELECT * FROM reply_listeners WHERE id=?", (listener_id,)).fetchone()
            if not listener:
                raise RuntimeProtocolError("LISTENER_NOT_FOUND", f"listener not found: {listener_id}")
            if not listener["webhook_url"]:
                raise RuntimeProtocolError("WEBHOOK_NOT_CONFIGURED", "listener webhook is not configured")
            if self.webhook is None:
                raise RuntimeProtocolError("WEBHOOK_ADAPTER_UNAVAILABLE", "webhook adapter is unavailable")
            self._enforce_webhook_rate(db, listener["webhook_fingerprint"])
            webhook_url = listener["webhook_url"]
            fingerprint = listener["webhook_fingerprint"]
            listener_generation = int(listener["generation"])
            listener_public = decode_json(listener["public_json"], {})
            group_id = str(listener_public.get("groupId") or "")
            starting_revision = current
            db.execute(
                "INSERT INTO webhook_deliveries VALUES(?,?,?,?,?,?,?,?,?)",
                (delivery_id, fingerprint, listener_id, None, "test", "sending", None, None, now),
            )
            db.execute(
                "INSERT INTO runtime_commands(command_id,request_hash,result_json,created_at) VALUES(?,?,?,?)",
                (
                    command_id,
                    request_hash,
                    encode_json({"__in_progress__": reservation_token, "deliveryId": delivery_id}),
                    now,
                ),
            )
        try:
            response = self.webhook.send(
                webhookUrl=webhook_url,
                text=text,
                mentionedList=[],
                mentionedMobileList=[],
                timeoutSeconds=15,
                deliveryId=delivery_id,
            )
            status = _delivery_status(response)
            error = None
        except TimeoutError as exc:
            response, status, error = None, "delivery_unknown", {"message": str(exc)}
        except RuntimeProtocolError as exc:
            uncertain = exc.code in {
                "WEBHOOK_NETWORK_ERROR", "WEBHOOK_TIMEOUT", "WEBHOOK_DELIVERY_UNKNOWN",
                "WEBHOOK_INVALID_RESPONSE",
            }
            response = None
            status = "delivery_unknown" if uncertain else "failed"
            error = exc.as_dict()
        except Exception as exc:
            response, status, error = None, "delivery_unknown", {"message": str(exc)}
        protocol_error = None
        result = None
        with self.store.transaction() as db:
            self._assert_current_lease(db, self._now())
            reservation = db.execute(
                "SELECT request_hash,result_json FROM runtime_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
            reservation_state = decode_json(reservation["result_json"], {}) if reservation else {}
            if (
                not reservation
                or reservation["request_hash"] != request_hash
                or reservation_state.get("__in_progress__") != reservation_token
            ):
                raise RuntimeProtocolError(
                    "WEBHOOK_DELIVERY_UNKNOWN",
                    "webhook delivery ownership changed before the test completed",
                    details={"deliveryId": delivery_id},
                )
            db.execute(
                "UPDATE webhook_deliveries SET status=?,response_json=?,error_json=? WHERE id=?",
                (
                    status,
                    encode_json(response) if response is not None else None,
                    encode_json(error) if error is not None else None,
                    delivery_id,
                ),
            )
            if status in {"sent", "delivery_unknown"}:
                db.execute(
                    "INSERT OR REPLACE INTO recent_outbound(fingerprint,group_id,sent_at) VALUES(?,?,?)",
                    (
                        _outbound_fingerprint(
                            group_id, text
                        ),
                        group_id,
                        now,
                    ),
                )
            current_revision = self.store.revision(db)
            current_listener = db.execute(
                "SELECT * FROM reply_listeners WHERE id=?", (listener_id,)
            ).fetchone()
            current_public = (
                decode_json(current_listener["public_json"], {}) if current_listener else {}
            )
            configuration_changed = bool(
                current_revision != starting_revision
                or not current_listener
                or int(current_listener["generation"]) != listener_generation
                or str(current_listener["webhook_fingerprint"] or "") != str(fingerprint or "")
                or str(current_public.get("groupId") or "") != group_id
            )
            if configuration_changed:
                protocol_error = RuntimeProtocolError(
                    "WEBHOOK_CONFIGURATION_CHANGED",
                    "listener or webhook configuration changed while the test message was being delivered",
                    retryable=True,
                    details={"deliveryId": delivery_id, "actualRevision": current_revision},
                )
                completed = db.execute(
                    """UPDATE runtime_commands SET result_json=?
                       WHERE command_id=? AND request_hash=?
                         AND json_extract(result_json,'$.__in_progress__')=?""",
                    (
                        encode_json({"__error__": protocol_error.as_dict()}),
                        command_id,
                        request_hash,
                        reservation_token,
                    ),
                ).rowcount
                if int(completed or 0) != 1:
                    raise RuntimeProtocolError(
                        "WEBHOOK_DELIVERY_UNKNOWN",
                        "webhook test command reservation was lost",
                        details={"deliveryId": delivery_id},
                    )
            elif status == "sent":
                revision = self.store.bump_revision(db)
                db.execute(
                    """UPDATE reply_listeners SET webhook_test_code=?,webhook_tested_at=?,revision=?,updated_at=?
                       WHERE id=?""",
                    (code, now, revision, now, listener_id),
                )
                result = {
                    "revision": revision,
                    "listenerId": listener_id,
                    "testCode": code,
                    "code": code,
                    "challengeId": code,
                    "testedAt": _iso_time(now),
                    "delivered": True,
                }
                completed = db.execute(
                    """UPDATE runtime_commands SET result_json=?
                       WHERE command_id=? AND request_hash=?
                         AND json_extract(result_json,'$.__in_progress__')=?""",
                    (encode_json(result), command_id, request_hash, reservation_token),
                ).rowcount
                if int(completed or 0) != 1:
                    raise RuntimeProtocolError(
                        "WEBHOOK_DELIVERY_UNKNOWN",
                        "webhook test command reservation was lost",
                        details={"deliveryId": delivery_id},
                    )
            else:
                protocol_error = RuntimeProtocolError(
                    "WEBHOOK_DELIVERY_UNKNOWN" if status == "delivery_unknown" else "WEBHOOK_SEND_FAILED",
                    "the webhook test could not be confirmed as delivered",
                    retryable=status == "failed",
                    details={"deliveryId": delivery_id},
                )
                completed = db.execute(
                    """UPDATE runtime_commands SET result_json=?
                       WHERE command_id=? AND request_hash=?
                         AND json_extract(result_json,'$.__in_progress__')=?""",
                    (
                        encode_json({"__error__": protocol_error.as_dict()}),
                        command_id,
                        request_hash,
                        reservation_token,
                    ),
                ).rowcount
                if int(completed or 0) != 1:
                    raise RuntimeProtocolError(
                        "WEBHOOK_DELIVERY_UNKNOWN",
                        "webhook test command reservation was lost",
                        details={"deliveryId": delivery_id},
                    )
        if protocol_error is not None:
            raise protocol_error
        return result

    def _execute_delivery_command(
        self, envelope: dict, command_id: str, request_hash: str, body: dict
    ) -> dict:
        with self.store.transaction() as db:
            previous = db.execute(
                "SELECT request_hash,result_json FROM runtime_commands WHERE command_id=?", (command_id,)
            ).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise RuntimeProtocolError(
                        "COMMAND_ID_REUSED", "commandId was already used for a different command"
                    )
                return _command_result(decode_json(previous["result_json"], {}))
            expected_revision = envelope.get("expectedRevision")
            current_revision = self.store.revision(db)
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise RuntimeProtocolError(
                    "REVISION_CONFLICT", "configuration changed; refresh before sending",
                    retryable=True,
                    details={"expectedRevision": int(expected_revision), "actualRevision": current_revision},
                )
            work_id = str(body.get("workId") or "").strip()
            work = db.execute(
                "SELECT generation,listener_generation,status FROM reply_work_items WHERE id=?",
                (work_id,),
            ).fetchone()
            if not work:
                raise RuntimeProtocolError("WORK_NOT_FOUND", f"work item not found: {work_id}")
            expected_version = body.get("expectedVersion")
            if expected_version is not None and int(expected_version) != int(work["generation"]):
                raise RuntimeProtocolError(
                    "WORK_VERSION_CONFLICT", "work item changed; refresh before acting", retryable=True
                )
            if work["status"] == "sending":
                outbox = db.execute(
                    "SELECT delivery_id FROM reply_outbox WHERE work_id=?", (work_id,)
                ).fetchone()
                raise RuntimeProtocolError(
                    "COMMAND_IN_PROGRESS",
                    "this reply is already being delivered",
                    retryable=True,
                    details={"deliveryId": outbox["delivery_id"] if outbox else None},
                )
            if work["status"] not in {"pending", "delivery_failed", "delivery_unknown"}:
                raise RuntimeProtocolError("WORK_NOT_PENDING", "only a pending reply can be sent")
        plain_at = body.get("kind") == "work.send_plain_at"
        if plain_at and body.get("acknowledgement") != "PLAIN_AT_IS_NOT_A_TRUE_MENTION":
            raise RuntimeProtocolError(
                "PLAIN_AT_ACKNOWLEDGEMENT_REQUIRED",
                "sending a plain @name requires explicit acknowledgement",
            )
        result = self._deliver_work(
            work_id,
            plain_at=plain_at,
            automatic=False,
            confirmed_not_delivered=bool(body.get("confirmedNotDelivered", False)),
            expected_work_generation=int(work["generation"]),
            expected_listener_generation=int(work["listener_generation"]),
        )
        with self.store.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO runtime_commands(command_id,request_hash,result_json,created_at) VALUES(?,?,?,?)",
                (command_id, request_hash, encode_json(result), self._now()),
            )
        return result

    def run_once(self, *, wait: bool = False) -> dict:
        if self._closed:
            raise RuntimeProtocolError("RUNTIME_CLOSED", "reply runtime is closed")
        if self._lease_lost:
            raise RuntimeProtocolError("RUNTIME_LEASE_LOST", "reply runtime no longer owns its lease")
        completed = self._reap_futures()
        polled = self._poll_messages()
        assigned = self._assign_inbox()
        classified = self._classify_due()
        queued = self._queue_due_retrievals()
        scheduled = self._schedule_retrievals()
        if wait:
            with self._future_lock:
                pending = [entry[0] for entry in self._futures.values()]
            if pending:
                wait_futures(pending)
            completed += self._reap_futures()
        result = {
            "revision": self.store.revision(),
            "polledMessages": polled,
            "assignedMessages": assigned,
            "classified": classified,
            "queued": queued,
            "scheduled": scheduled,
            "inFlight": self._in_flight_count(),
        }
        if completed or polled or assigned or classified or queued or scheduled:
            self._emit_event(
                {
                    "kind": "runtime.activity",
                    "completed": completed,
                    "polledMessages": polled,
                    "scheduled": scheduled,
                }
            )
        return result

    def _enabled_listener_rows(self) -> list:
        with self.store.lock:
            return self.store.connection.execute(
                "SELECT * FROM reply_listeners WHERE json_extract(public_json, '$.enabled')=1 ORDER BY id"
            ).fetchall()

    def _poll_listener_is_current(self, db, original_row, listener: dict) -> bool:
        current = db.execute(
            "SELECT * FROM reply_listeners WHERE id=?", (original_row["id"],)
        ).fetchone()
        if not current or int(current["generation"]) != int(original_row["generation"]):
            return False
        current_public = decode_json(current["public_json"], {})
        return bool(
            current_public.get("enabled")
            and str(current_public.get("groupId") or "")
            == str(listener.get("groupId") or "")
            and self._listener_health(current_public).get("status") == "ready"
        )

    def _poll_messages(
        self,
        *,
        listener_id: str | None = None,
        force: bool = False,
        strict: bool = False,
    ) -> int:
        with self._poll_lock:
            return self._poll_messages_locked(
                listener_id=listener_id, force=force, strict=strict
            )

    def _poll_messages_locked(
        self,
        *,
        listener_id: str | None = None,
        force: bool = False,
        strict: bool = False,
    ) -> int:
        if self.message_source is None:
            if strict:
                raise RuntimeProtocolError(
                    "MESSAGE_SOURCE_UNAVAILABLE", "message source is unavailable for send preflight"
                )
            return 0
        now = self._now()
        count = 0
        if listener_id:
            with self.store.lock:
                target = self.store.connection.execute(
                    "SELECT * FROM reply_listeners WHERE id=?", (listener_id,)
                ).fetchone()
            rows = [target] if target is not None else []
        else:
            rows = self._enabled_listener_rows()
        for row in rows:
            listener = self._public_listener(row)
            if listener["health"]["status"] != "ready":
                if strict:
                    raise RuntimeProtocolError(
                        "LISTENER_NOT_READY", "listener MCP grants are not ready for automatic sending"
                    )
                continue
            with self.store.lock:
                cursor_row = self.store.connection.execute(
                    "SELECT * FROM runtime_cursors WHERE listener_id=?", (row["id"],)
                ).fetchone()
            poll_seconds = int(listener["pollIntervalSeconds"])
            if not force and cursor_row and float(cursor_row["next_poll_at"] or 0) > now:
                continue
            if not cursor_row:
                try:
                    raw_cursor = self.message_source.watermark(listener)
                except Exception as exc:
                    if strict:
                        raise RuntimeProtocolError(
                            "MESSAGE_POLL_FAILED", "could not establish the current group-message watermark"
                        ) from exc
                    self._emit_event(
                        {"kind": "listener.poll_failed", "listenerId": listener["id"], "message": str(exc)}
                    )
                    continue
                cursor = (
                    list(raw_cursor)
                    if isinstance(raw_cursor, (list, tuple))
                    else raw_cursor
                )
                if cursor is None:
                    cursor = [int(now), 2**63 - 1, 2**63 - 1, 2**63 - 1]
                with self.store.transaction() as db:
                    if not self._poll_listener_is_current(db, row, listener):
                        continue
                    db.execute(
                        "INSERT OR REPLACE INTO runtime_cursors(listener_id,cursor_json,next_poll_at,initialized_at,updated_at) VALUES(?,?,?,?,?)",
                        (listener["id"], encode_json(cursor), now + poll_seconds, now, now),
                    )
                continue
            raw_cursor = decode_json(cursor_row["cursor_json"], None)
            cursor = raw_cursor
            try:
                force_reader = getattr(self.message_source, "read_force", None)
                if force and callable(force_reader):
                    messages = force_reader(listener, cursor) or []
                else:
                    messages = self.message_source.read(listener, cursor) or []
            except Exception as exc:
                if strict:
                    raise RuntimeProtocolError(
                        "MESSAGE_POLL_FAILED", "could not refresh group messages before automatic sending"
                    ) from exc
                self._emit_event(
                    {"kind": "listener.poll_failed", "listenerId": listener["id"], "message": str(exc)}
                )
                with self.store.transaction() as db:
                    db.execute(
                        "UPDATE runtime_cursors SET next_poll_at=?,updated_at=? WHERE listener_id=?",
                        (now + poll_seconds, now, listener["id"]),
                    )
                continue
            ordered = sorted(messages, key=lambda item: _cursor_sort_key(_message_cursor(item)))
            max_cursor = cursor
            with self.store.transaction() as db:
                if not self._poll_listener_is_current(db, row, listener):
                    continue
                for message in ordered:
                    normalized = _normalize_message(message, listener["groupId"])
                    message_cursor = normalized.pop("cursor")
                    outbound = db.execute(
                        "SELECT 1 FROM recent_outbound WHERE fingerprint=? AND sent_at>?",
                        (
                            _outbound_fingerprint(listener["groupId"], normalized.get("text") or ""),
                            now - 600,
                        ),
                    ).fetchone()
                    if outbound:
                        if max_cursor is None or _cursor_sort_key(message_cursor) > _cursor_sort_key(max_cursor):
                            max_cursor = message_cursor
                        continue
                    inserted = db.execute(
                        """INSERT OR IGNORE INTO reply_inbox(
                               listener_id,message_id,server_id,sequence,send_time,payload_json,received_at
                           ) VALUES(?,?,?,?,?,?,?)""",
                        (
                            listener["id"], normalized["messageId"], normalized["serverId"],
                            normalized["sequence"], normalized["sendTime"], encode_json(normalized), now,
                        ),
                    ).rowcount
                    count += int(inserted or 0)
                    if max_cursor is None or _cursor_sort_key(message_cursor) > _cursor_sort_key(max_cursor):
                        max_cursor = message_cursor
                db.execute(
                    "UPDATE runtime_cursors SET cursor_json=?,next_poll_at=?,updated_at=? WHERE listener_id=?",
                    (encode_json(max_cursor), now + poll_seconds, now, listener["id"]),
                )
                db.execute("DELETE FROM recent_outbound WHERE sent_at<?", (now - 600,))
        return count

    def _assign_inbox(
        self, *, listener_id: str | None = None, strict: bool = False
    ) -> int:
        now = self._now()
        with self._assign_lock:
            with self.store.transaction() as db:
                if listener_id:
                    inbox_rows = db.execute(
                        """SELECT * FROM reply_inbox
                           WHERE assigned_work_id IS NULL AND listener_id=?
                             AND (?=1 OR retry_after<=?)
                           ORDER BY send_time,sequence,id LIMIT 20""",
                        (listener_id, int(strict), now),
                    ).fetchall()
                else:
                    inbox_rows = db.execute(
                        """SELECT * FROM reply_inbox WHERE assigned_work_id IS NULL
                             AND (?=1 OR retry_after<=?)
                           ORDER BY send_time,sequence,id LIMIT 20""",
                        (int(strict), now),
                    ).fetchall()
                claimed = []
                for inbox in inbox_rows:
                    token = f"claim:{uuid.uuid4()}"
                    if db.execute(
                        """UPDATE reply_inbox SET assigned_work_id=?
                           WHERE id=? AND assigned_work_id IS NULL""",
                        (token, inbox["id"]),
                    ).rowcount:
                        claimed.append((inbox, token))
            with self._assignment_condition:
                for inbox, _token in claimed:
                    self._active_assignments[int(inbox["id"])] = str(inbox["listener_id"])

        grouped: dict[tuple[str, str], list[tuple[object, str, dict, str | None]]] = {}
        for inbox, token in claimed:
            message = decode_json(inbox["payload_json"], {})
            primed_work_id = self._prime_claimed_work(inbox, token, message, now)
            key = (str(inbox["listener_id"]), str(message.get("senderId") or ""))
            grouped.setdefault(key, []).append((inbox, token, message, primed_work_id))
        futures = [
            self._submit_daemon(
                self._process_claimed_sender_group, key, entries, now, strict
            )
            for key, entries in grouped.items()
        ]
        if futures:
            wait_futures(futures)
        return sum(int(future.result() or 0) for future in futures)

    def _sender_assignment_lock(self, listener_id: str, sender_id: str) -> threading.RLock:
        key = (listener_id, sender_id)
        with self._sender_assignment_locks_guard:
            return self._sender_assignment_locks.setdefault(key, threading.RLock())

    @staticmethod
    def _has_earlier_unresolved_inbox(db, inbox, sender_id: str) -> bool:
        return bool(
            db.execute(
                """SELECT 1 FROM reply_inbox
                   WHERE listener_id=?
                     AND json_extract(payload_json,'$.senderId')=?
                     AND (assigned_work_id IS NULL OR assigned_work_id LIKE 'claim:%')
                     AND (send_time<? OR (send_time=? AND sequence<?)
                          OR (send_time=? AND sequence=? AND id<?))
                   LIMIT 1""",
                (
                    inbox["listener_id"], sender_id,
                    inbox["send_time"], inbox["send_time"], inbox["sequence"],
                    inbox["send_time"], inbox["sequence"], inbox["id"],
                ),
            ).fetchone()
        )

    def _process_claimed_sender_group(
        self,
        key: tuple[str, str],
        entries: list[tuple[object, str, dict, str | None]],
        now: float,
        strict: bool,
    ) -> int:
        entries.sort(key=lambda entry: (float(entry[0]["send_time"]), int(entry[0]["sequence"]), int(entry[0]["id"])))
        sender_lock = self._sender_assignment_lock(*key)
        try:
            with sender_lock:
                first = entries[0][0]
                with self.store.lock:
                    earlier = self._has_earlier_unresolved_inbox(
                        self.store.connection, first, key[1]
                    )
                if earlier:
                    return 0
                processed = 0
                for inbox, token, message, primed_work_id in entries:
                    try:
                        processed += self._process_claimed_inbox(
                            inbox,
                            token,
                            message,
                            now,
                            primed_work_id=primed_work_id,
                            strict=strict,
                        )
                    except _RetryableMessageClassification as exc:
                        self._defer_classification_retry(
                            inbox,
                            token,
                            primed_work_id,
                            exc,
                            now,
                        )
                        if strict:
                            raise RuntimeProtocolError(
                                exc.code,
                                exc.public_message,
                                retryable=True,
                            ) from exc
                        self._emit_event(
                            {
                                "kind": "message.classification_deferred",
                                "listenerId": key[0],
                                "inboxId": int(inbox["id"]),
                                "code": exc.code,
                                "message": exc.public_message,
                            }
                        )
                        # Preserve same-sender FIFO. Later entries remain unassigned
                        # and cannot overtake the message whose semantics are unknown.
                        break
                return processed
        finally:
            with self.store.transaction() as db:
                for inbox, token, _message, _primed_work_id in entries:
                    db.execute(
                        """UPDATE reply_inbox SET assigned_work_id=NULL
                           WHERE id=? AND assigned_work_id=?""",
                        (inbox["id"], token),
                    )
            with self._assignment_condition:
                for inbox, _token, _message, _primed_work_id in entries:
                    self._active_assignments.pop(int(inbox["id"]), None)
                self._assignment_condition.notify_all()

    def _defer_classification_retry(
        self,
        inbox,
        claim_token: str,
        primed_work_id: str | None,
        error: _RetryableMessageClassification,
        now: float,
    ) -> None:
        with self.store.transaction() as db:
            current = db.execute(
                "SELECT classification_attempts FROM reply_inbox WHERE id=?",
                (inbox["id"],),
            ).fetchone()
            attempts = int(current["classification_attempts"] or 0) + 1 if current else 1
            retry_delay = min(60, 2 ** min(attempts, 6))
            if primed_work_id:
                db.execute(
                    """DELETE FROM reply_work_items
                       WHERE id=? AND status='collecting' AND generation=1""",
                    (primed_work_id,),
                )
            db.execute(
                """UPDATE reply_inbox
                   SET assigned_work_id=NULL,retry_after=?,classification_attempts=?,
                       classification_error_json=?
                   WHERE id=? AND assigned_work_id IN (?,?)""",
                (
                    now + retry_delay,
                    attempts,
                    encode_json({"code": error.code, "message": error.public_message}),
                    inbox["id"],
                    claim_token,
                    primed_work_id or claim_token,
                ),
            )

    def _prime_claimed_work(
        self, inbox, claim_token: str, message: dict, now: float
    ) -> str | None:
        content_type = str(message.get("contentType") or "text")
        if content_type not in {"text", "link", "image"}:
            return None
        with self.store.transaction() as db:
            listener_row = db.execute(
                "SELECT * FROM reply_listeners WHERE id=?", (inbox["listener_id"],)
            ).fetchone()
            if not listener_row:
                return None
            listener = decode_json(listener_row["public_json"], {})
            if (
                not listener.get("enabled")
                or str(message.get("groupId") or "") != str(listener.get("groupId") or "")
                or self._listener_health(listener).get("status") != "ready"
            ):
                return None
            earlier_unresolved = self._has_earlier_unresolved_inbox(
                db, inbox, str(message.get("senderId") or "")
            )
            if earlier_unresolved:
                # A later message can be selected while the sender's oldest item is
                # under classification backoff. Keep it unprimed so the sender-group
                # FIFO guard can release the claim without creating an orphan work.
                return None
            active = db.execute(
                """SELECT 1 FROM reply_work_items
                   WHERE listener_id=? AND sender_id=?
                     AND status IN ('collecting','waiting_for_human_reply','queued_retrieval',
                                    'retrieving','ready_to_send') LIMIT 1""",
                (listener_row["id"], str(message.get("senderId") or "")),
            ).fetchone()
            if active:
                return None
            work_id = str(uuid.uuid4())
            if not self._insert_work(
                db, work_id, listener_row, message, "collecting", now, [message],
                inbox_id=int(inbox["id"]), expected_assignment=claim_token,
            ):
                return None
            claimed = db.execute(
                """UPDATE reply_inbox SET assigned_work_id=?
                   WHERE id=? AND assigned_work_id=?""",
                (work_id, inbox["id"], claim_token),
            ).rowcount
            if not claimed:
                db.execute("DELETE FROM reply_work_items WHERE id=?", (work_id,))
                return None
            return work_id

    def _process_claimed_inbox(
        self, inbox, claim_token: str, message: dict, now: float, *,
        primed_work_id: str | None, strict: bool,
    ) -> int:
        with self.store.lock:
            listener_row = self.store.connection.execute(
                "SELECT * FROM reply_listeners WHERE id=?", (inbox["listener_id"],)
            ).fetchone()
        if not listener_row:
            return 0
        listener = decode_json(listener_row["public_json"], {})
        if str(message.get("groupId") or "") != str(listener.get("groupId") or ""):
            with self.store.transaction() as db:
                db.execute(
                    "DELETE FROM reply_inbox WHERE id=? AND assigned_work_id=?",
                    (inbox["id"], claim_token),
                )
            return 0
        with self.store.lock:
            listener_health = self._listener_health(listener).get("status")
        unavailable = (
            "listener_disabled" if not listener.get("enabled")
            else "listener_not_ready" if listener_health != "ready"
            else ""
        )
        if unavailable:
            work_id = str(uuid.uuid4())
            with self.store.transaction() as db:
                if not self._insert_work(
                    db, work_id, listener_row, message, f"ignored_{unavailable}", now, [message],
                    inbox_id=int(inbox["id"]), expected_assignment=claim_token,
                ):
                    return 0
                db.execute(
                    "UPDATE reply_work_items SET pending_reason=? WHERE id=?",
                    (unavailable, work_id),
                )
                db.execute(
                    "UPDATE reply_inbox SET assigned_work_id=? WHERE id=? AND assigned_work_id=?",
                    (work_id, inbox["id"], claim_token),
                )
            return 1

        content_type = str(message.get("contentType") or "text")
        if content_type not in {"text", "link", "image"}:
            work_id = str(uuid.uuid4())
            with self.store.transaction() as db:
                if not self._insert_work(
                    db, work_id, listener_row, message, "ignored_unsupported", now, [message],
                    inbox_id=int(inbox["id"]), expected_assignment=claim_token,
                ):
                    return 0
                db.execute(
                    "UPDATE reply_inbox SET assigned_work_id=? WHERE id=? AND assigned_work_id=?",
                    (work_id, inbox["id"], claim_token),
                )
            return 1

        sender_id = str(message.get("senderId") or "")
        human_match = self._match_human_answer(
            listener_row, inbox, message, now, strict=strict
        )
        if primed_work_id:
            with self.store.transaction() as db:
                primed = db.execute(
                    "SELECT status,listener_generation FROM reply_work_items WHERE id=?",
                    (primed_work_id,),
                ).fetchone()
                current_listener = db.execute(
                    "SELECT generation FROM reply_listeners WHERE id=?", (listener_row["id"],)
                ).fetchone()
                primed_is_current = bool(
                    primed
                    and primed["status"] == "collecting"
                    and current_listener
                    and int(primed["listener_generation"]) == int(current_listener["generation"])
                    and int(current_listener["generation"]) == int(listener_row["generation"])
                )
                if not primed_is_current:
                    return 0
                if human_match and "question" not in human_match["labels"]:
                    db.execute("DELETE FROM reply_work_items WHERE id=?", (primed_work_id,))
                    db.execute(
                        """UPDATE reply_inbox SET assigned_work_id=?
                           WHERE id=? AND assigned_work_id=?""",
                        (human_match["workId"], inbox["id"], primed_work_id),
                    )
            return 1
        if human_match:
            if "question" not in human_match["labels"]:
                with self.store.transaction() as db:
                    db.execute(
                        "UPDATE reply_inbox SET assigned_work_id=? WHERE id=? AND assigned_work_id=?",
                        (human_match["workId"], inbox["id"], claim_token),
                    )
                return 1
            return int(self._create_and_assign_work(
                inbox, claim_token, listener_row, message, "collecting", now
            ))

        with self.store.lock:
            active = self.store.connection.execute(
                """SELECT * FROM reply_work_items
                   WHERE listener_id=? AND sender_id=?
                     AND status IN ('collecting','waiting_for_human_reply','queued_retrieval',
                                    'retrieving','ready_to_send')
                   ORDER BY created_at DESC LIMIT 1""",
                (listener_row["id"], sender_id),
            ).fetchone()
        if not active:
            return int(self._create_and_assign_work(
                inbox, claim_token, listener_row, message, "collecting", now
            ))

        try:
            classification = self.model.classify(
                messages=[message],
                groupContext=self._recent_group_context(listener_row["id"], now),
                question=active["question"],
            ) if self.model is not None else {}
            labels = set(classification.get("labels") or []) if isinstance(classification, dict) else set()
        except Exception as exc:
            raise _RetryableMessageClassification(
                "MESSAGE_CLASSIFICATION_FAILED",
                "could not classify the newly arrived message",
            ) from exc

        active_statuses = (
            "collecting", "waiting_for_human_reply", "queued_retrieval", "retrieving", "ready_to_send"
        )
        if "withdrawn" in labels:
            with self.store.transaction() as db:
                changed = db.execute(
                    """UPDATE reply_work_items SET status='withdrawn',generation=generation+1,
                           pending_reason='question withdrawn by sender',human_answer_message_json=?,
                           updated_at=?,completed_at=? WHERE id=? AND generation=?
                           AND status IN (?,?,?,?,?)""",
                    (encode_json(message), now, now, active["id"], active["generation"], *active_statuses),
                ).rowcount
                if changed:
                    db.execute(
                        "UPDATE reply_inbox SET assigned_work_id=? WHERE id=? AND assigned_work_id=?",
                        (active["id"], inbox["id"], claim_token),
                    )
            if changed:
                self._cancel_work_futures(str(active["id"]))
            return int(bool(changed))

        if "human_answer" in labels:
            with self.store.transaction() as db:
                changed = db.execute(
                    """UPDATE reply_work_items SET status='answered_by_human',generation=generation+1,
                           human_answered_at=?,human_answer_message_json=?,
                           pending_reason='questioner reported the issue resolved',updated_at=?,completed_at=?
                       WHERE id=? AND generation=? AND status IN (?,?,?,?,?)""",
                    (now, encode_json(message), now, now, active["id"], active["generation"], *active_statuses),
                ).rowcount
                if changed:
                    self._append_session_turn_in_transaction(
                        db, active["listener_id"], active["sender_id"],
                        {"question": active["question"], "answer": str(message.get("text") or ""), "answeredBy": "human"},
                        now,
                    )
                    db.execute(
                        "UPDATE reply_inbox SET assigned_work_id=? WHERE id=? AND assigned_work_id=?",
                        (active["id"], inbox["id"], claim_token),
                    )
            if changed:
                self._cancel_work_futures(str(active["id"]))
            if "question" in labels:
                return int(self._create_and_assign_work(
                    inbox, claim_token, listener_row, message, "collecting", now
                ))
            return int(bool(changed))

        if "supplement" in labels:
            messages = decode_json(active["messages_json"], []) + [message]
            with self.store.transaction() as db:
                changed = db.execute(
                    """UPDATE reply_work_items SET status='collecting',messages_json=?,question=?,
                           merge_due_at=?,human_wait_due_at=NULL,human_answered_at=NULL,
                           human_answer_message_json=NULL,generation=generation+1,updated_at=?
                       WHERE id=? AND generation=? AND status IN (?,?,?,?,?)""",
                    (encode_json(messages), _question_text(messages),
                     now + int(listener["sameSenderMergeSeconds"]), now,
                     active["id"], active["generation"], *active_statuses),
                ).rowcount
                if changed:
                    db.execute(
                        "UPDATE reply_inbox SET assigned_work_id=? WHERE id=? AND assigned_work_id=?",
                        (active["id"], inbox["id"], claim_token),
                    )
            if changed:
                self._cancel_work_futures(str(active["id"]))
            return int(bool(changed))

        return int(self._create_and_assign_work(
            inbox, claim_token, listener_row, message, "collecting", now
        ))

    def _create_and_assign_work(
        self, inbox, claim_token: str, listener_row, message: dict, status: str, now: float
    ) -> bool:
        work_id = str(uuid.uuid4())
        with self.store.transaction() as db:
            if not self._insert_work(
                db, work_id, listener_row, message, status, now, [message],
                inbox_id=int(inbox["id"]), expected_assignment=claim_token,
            ):
                return False
            db.execute(
                "UPDATE reply_inbox SET assigned_work_id=? WHERE id=? AND assigned_work_id=?",
                (work_id, inbox["id"], claim_token),
            )
        return True

    def _match_human_answer(
        self,
        listener_row,
        inbox,
        message: dict,
        now: float,
        *,
        strict: bool = False,
    ) -> dict | None:
        sender_id = str(message.get("senderId") or "")
        with self.store.lock:
            candidates = self.store.connection.execute(
                """SELECT * FROM reply_work_items
                   WHERE listener_id=? AND sender_id<>?
                      AND status IN ('collecting','waiting_for_human_reply','queued_retrieval',
                                     'retrieving','ready_to_send')
                     AND EXISTS (
                         SELECT 1 FROM reply_inbox source
                          WHERE source.assigned_work_id=reply_work_items.id
                            AND (source.send_time<?
                                 OR (source.send_time=? AND source.sequence<?)
                                 OR (source.send_time=? AND source.sequence=? AND source.id<?))
                     )
                   ORDER BY created_at DESC LIMIT 50""",
                (
                    listener_row["id"], sender_id,
                    inbox["send_time"], inbox["send_time"], inbox["sequence"],
                    inbox["send_time"], inbox["sequence"], inbox["id"],
                ),
            ).fetchall()
        if not candidates or self.model is None:
            return None
        context = self._recent_group_context(listener_row["id"], now)
        try:
            batch_matcher = getattr(self.model, "match_human_answers", None)
            if callable(batch_matcher):
                classification = batch_matcher(
                    message=message,
                    groupContext=context,
                    candidates=[
                        {"workId": row["id"], "question": row["question"]}
                        for row in candidates
                    ],
                )
                matches = classification.get("matches") if isinstance(classification, dict) else []
                matches = matches if isinstance(matches, list) else []
                labels_by_id = {
                    str(item.get("workId") or ""): set(item.get("labels") or [])
                    for item in matches if isinstance(item, dict)
                }
                for work_id in (
                    classification.get("matchedWorkIds") or []
                    if isinstance(classification, dict) else []
                ):
                    labels_by_id.setdefault(str(work_id), {"human_answer"})
                classified_candidates = [
                    (candidate, labels_by_id.get(str(candidate["id"]), set()))
                    for candidate in candidates
                ]
            else:
                # Compatibility adapters get one bounded classification call. The built-in
                # adapter implements the batch seam above, so candidate count never multiplies
                # the 60-second model budget.
                candidate = candidates[0]
                classification = self.model.classify(
                    messages=[message], groupContext=context, question=candidate["question"]
                )
                labels = set(classification.get("labels") or []) if isinstance(classification, dict) else set()
                classified_candidates = [(candidate, labels)]
        except Exception as exc:
            raise _RetryableMessageClassification(
                "HUMAN_ANSWER_CLASSIFICATION_FAILED",
                "could not verify whether the new message answered a pending question",
            ) from exc

        for candidate, labels in classified_candidates:
            if "human_answer" not in labels:
                continue
            with self.store.transaction() as db:
                current = db.execute(
                    "SELECT status,generation FROM reply_work_items WHERE id=?", (candidate["id"],)
                ).fetchone()
                current_listener = db.execute(
                    "SELECT generation FROM reply_listeners WHERE id=?", (listener_row["id"],)
                ).fetchone()
                if (
                    not current
                    or int(current["generation"]) != int(candidate["generation"])
                    or not current_listener
                    or int(current_listener["generation"]) != int(listener_row["generation"])
                    or current["status"] not in {
                    "collecting", "waiting_for_human_reply", "queued_retrieval", "retrieving",
                    "ready_to_send"
                    }
                ):
                    continue
                if current["status"] == "retrieving":
                    next_status = "retrieving"
                    completed_at = None
                    pending_reason = candidate["pending_reason"]
                elif current["status"] == "ready_to_send":
                    next_status = "pending"
                    completed_at = now
                    pending_reason = "human_answered_after_review"
                else:
                    next_status = "answered_by_human"
                    completed_at = now
                    pending_reason = "answered_by_human"
                db.execute(
                    """UPDATE reply_work_items SET status=?,human_answered_at=?,
                           human_answer_message_json=?,pending_reason=?,updated_at=?,completed_at=? WHERE id=?""",
                    (
                        next_status, now, encode_json(message), pending_reason,
                        now, completed_at, candidate["id"],
                    ),
                )
                if next_status == "answered_by_human":
                    self._append_session_turn_in_transaction(
                        db,
                        candidate["listener_id"],
                        candidate["sender_id"],
                        {
                            "question": candidate["question"],
                            "answer": str(message.get("text") or ""),
                            "answeredBy": "human",
                        },
                        now,
                    )
            return {"workId": candidate["id"], "labels": labels}
        return None

    def _insert_work(
        self, db, work_id: str, listener_row, message: dict, status: str, now: float,
        messages: list, *, inbox_id: int, expected_assignment: str,
    ) -> bool:
        self._assert_current_lease(db, self._now())
        assignment = db.execute(
            "SELECT assigned_work_id FROM reply_inbox WHERE id=?", (inbox_id,)
        ).fetchone()
        if not assignment or str(assignment["assigned_work_id"] or "") != expected_assignment:
            return False
        current_listener = db.execute(
            "SELECT generation,public_json FROM reply_listeners WHERE id=?",
            (listener_row["id"],),
        ).fetchone()
        if not current_listener or int(current_listener["generation"]) != int(listener_row["generation"]):
            return False
        listener = decode_json(listener_row["public_json"], {})
        current_public = decode_json(current_listener["public_json"], {})
        if str(current_public.get("groupId") or "") != str(listener.get("groupId") or ""):
            return False
        group_context = self._recent_group_context(listener_row["id"], now)
        db.execute(
            """INSERT INTO reply_work_items(
                   id,listener_id,group_id,sender_id,sender_name,sender_account,sender_mobile,status,
                   question,messages_json,group_context_json,generation,listener_generation,merge_due_at,
                   created_at,updated_at,completed_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                work_id, listener_row["id"], listener["groupId"], str(message.get("senderId") or ""),
                str(message.get("senderName") or ""), str(message.get("account") or ""),
                str(message.get("mobile") or ""), status, _question_text(messages), encode_json(messages),
                encode_json(group_context), 1, int(listener_row["generation"]),
                now + int(listener["sameSenderMergeSeconds"]) if status == "collecting" else None,
                now, now, now if status.startswith("ignored_") else None,
            ),
        )
        if status == "ignored_unsupported":
            content_type = str(message.get("contentType") or "unknown")
            db.execute(
                "UPDATE reply_work_items SET pending_reason=? WHERE id=?",
                (f"unsupported_content_type:{content_type}", work_id),
            )
        return True

    def _recent_group_context(self, listener_id: str, now: float) -> list[dict]:
        rows = self.store.connection.execute(
            """SELECT payload_json FROM reply_inbox
               WHERE listener_id=? AND send_time>=? ORDER BY send_time DESC,sequence DESC LIMIT 20""",
            (listener_id, now - 600),
        ).fetchall()
        return [decode_json(row["payload_json"], {}) for row in reversed(rows)]

    def _classify_due(self) -> int:
        now = self._now()
        with self.store.lock:
            rows = self.store.connection.execute(
                "SELECT * FROM reply_work_items WHERE status='collecting' AND merge_due_at<=? ORDER BY created_at",
                (now,),
            ).fetchall()
        count = 0
        for row in rows:
            with self.store.lock:
                listener_row = self.store.connection.execute(
                    "SELECT * FROM reply_listeners WHERE id=?", (row["listener_id"],)
                ).fetchone()
                current_group_context = self._recent_group_context(row["listener_id"], now)
            if not listener_row:
                continue
            listener = decode_json(listener_row["public_json"], {})
            try:
                if self.model is None:
                    raise RuntimeProtocolError("MODEL_UNAVAILABLE", "model adapter is not configured")
                classification = self.model.classify(
                    messages=decode_json(row["messages_json"], []),
                    groupContext=current_group_context,
                    question=None,
                )
                labels = set(classification.get("labels") or []) if isinstance(classification, dict) else set()
            except Exception as exc:
                self._fail_work(
                    row["id"],
                    "CLASSIFICATION_FAILED",
                    str(exc),
                    generation=int(row["generation"]),
                    expected_status="collecting",
                )
                count += 1
                continue
            with self.store.transaction() as db:
                current = db.execute("SELECT generation,status FROM reply_work_items WHERE id=?", (row["id"],)).fetchone()
                current_listener = db.execute(
                    "SELECT public_json,generation FROM reply_listeners WHERE id=?",
                    (row["listener_id"],),
                ).fetchone()
                current_listener_public = (
                    decode_json(current_listener["public_json"], {}) if current_listener else {}
                )
                if (
                    not current
                    or current["status"] != "collecting"
                    or int(current["generation"]) != int(row["generation"])
                    or not current_listener
                    or not current_listener_public.get("enabled")
                    or int(current_listener["generation"]) != int(row["listener_generation"])
                ):
                    continue
                if "withdrawn" in labels:
                    db.execute(
                        """UPDATE reply_work_items SET status='withdrawn',review_json=?,
                               pending_reason='question withdrawn by sender',completed_at=?,updated_at=?
                           WHERE id=?""",
                        (encode_json(classification), now, now, row["id"]),
                    )
                elif "question" in labels:
                    db.execute(
                        """UPDATE reply_work_items SET status='waiting_for_human_reply',
                               human_wait_due_at=?,updated_at=? WHERE id=?""",
                        (now + int(listener["humanReplyWaitSeconds"]), now, row["id"]),
                    )
                else:
                    db.execute(
                        """UPDATE reply_work_items SET status='ignored_non_question',review_json=?,
                               pending_reason='classified_as_non_question',completed_at=?,updated_at=?
                           WHERE id=?""",
                        (encode_json(classification), now, now, row["id"]),
                    )
            count += 1
        return count

    def _queue_due_retrievals(self) -> int:
        now = self._now()
        with self.store.transaction() as db:
            cursor = db.execute(
                """UPDATE reply_work_items SET status='queued_retrieval',updated_at=?
                   WHERE status='waiting_for_human_reply' AND human_wait_due_at<=?
                     AND human_answered_at IS NULL
                     AND EXISTS (
                       SELECT 1 FROM reply_listeners l
                       WHERE l.id=reply_work_items.listener_id
                         AND json_extract(l.public_json,'$.enabled')=1
                         AND l.generation=reply_work_items.listener_generation
                     )""",
                (now, now),
            )
            return int(cursor.rowcount or 0)

    def _schedule_retrievals(self) -> int:
        scheduled = 0
        with self.store.lock:
            listeners = self.store.connection.execute(
                "SELECT * FROM reply_listeners WHERE json_extract(public_json, '$.enabled')=1 ORDER BY id"
            ).fetchall()
        for listener_row in listeners:
            listener = self._public_listener(listener_row)
            if listener["health"]["status"] != "ready":
                continue
            with self._future_lock:
                live_entries = [
                    (key, entry)
                    for key, entry in self._futures.items()
                    if not entry[0].done() and entry[1] == listener["id"]
                ]
                live_work_ids = {key[0] for key, _entry in live_entries}
                running_senders = {entry[2] for _key, entry in live_entries}
                with self.store.lock:
                    db_running = self.store.connection.execute(
                        """SELECT id,sender_id FROM reply_work_items
                           WHERE listener_id=? AND status='retrieving'""",
                        (listener["id"],),
                    ).fetchall()
                    running = len(live_entries) + sum(
                        1 for row in db_running if str(row["id"]) not in live_work_ids
                    )
                    running_senders.update(str(row["sender_id"]) for row in db_running)
                    rows = self.store.connection.execute(
                        """SELECT * FROM reply_work_items q
                           WHERE q.listener_id=? AND q.status='queued_retrieval'
                             AND NOT EXISTS (
                               SELECT 1 FROM reply_work_items x
                               WHERE x.listener_id=q.listener_id AND x.sender_id=q.sender_id
                                 AND (x.created_at<q.created_at OR (x.created_at=q.created_at AND x.id<q.id))
                                 AND x.status IN ('collecting','waiting_for_human_reply',
                                                  'queued_retrieval','retrieving','ready_to_send','sending')
                             )
                           ORDER BY q.created_at,q.id LIMIT 200""",
                        (listener["id"],),
                    ).fetchall()
                remaining_slots = max(0, int(listener["maxConcurrency"]) - int(running))
                for work in rows:
                    if remaining_slots <= 0:
                        break
                    sender_id = str(work["sender_id"])
                    generation = int(work["generation"])
                    key = (str(work["id"]), generation)
                    if sender_id in running_senders or key in self._futures:
                        continue
                    with self.store.transaction() as db:
                        current_listener = db.execute(
                            "SELECT public_json,generation FROM reply_listeners WHERE id=?",
                            (listener["id"],),
                        ).fetchone()
                        current_listener_public = (
                            decode_json(current_listener["public_json"], {})
                            if current_listener else {}
                        )
                        listener_current = bool(
                            current_listener
                            and current_listener_public.get("enabled")
                            and int(current_listener["generation"])
                            == int(work["listener_generation"])
                        )
                        predecessor = None if not listener_current else db.execute(
                            """SELECT 1 FROM reply_work_items x
                               WHERE x.listener_id=? AND x.sender_id=? AND x.id<>?
                                 AND (x.created_at<? OR (x.created_at=? AND x.id<?))
                                 AND x.status IN ('collecting','waiting_for_human_reply',
                                                  'queued_retrieval','retrieving','ready_to_send','sending')
                               LIMIT 1""",
                            (
                                listener["id"], sender_id, work["id"], work["created_at"],
                                work["created_at"], work["id"],
                            ),
                        ).fetchone()
                        changed = 0 if not listener_current or predecessor else db.execute(
                            """UPDATE reply_work_items SET status='retrieving',updated_at=?
                               WHERE id=? AND status='queued_retrieval' AND generation=?""",
                            (self._now(), work["id"], generation),
                        ).rowcount
                    if not changed:
                        continue
                    future = self._submit_daemon(
                        self._retrieve_work,
                        work["id"],
                        generation,
                        int(work["listener_generation"]),
                    )
                    self._futures[key] = (future, listener["id"], sender_id)
                    running_senders.add(sender_id)
                    remaining_slots -= 1
                    scheduled += 1
        return scheduled

    def _submit_daemon(self, function, *args) -> Future:
        future: Future = Future()

        def run() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                result = function(*args)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)

        threading.Thread(target=run, name="reply-retrieval", daemon=True).start()
        return future

    def _retrieve_work(self, work_id: str, generation: int, listener_generation: int) -> None:
        try:
            with self.store.lock:
                work = self.store.connection.execute(
                    "SELECT * FROM reply_work_items WHERE id=?", (work_id,)
                ).fetchone()
                listener_row = self.store.connection.execute(
                    "SELECT * FROM reply_listeners WHERE id=?", (work["listener_id"],)
                ).fetchone() if work else None
            if not work or not listener_row:
                return
            listener = decode_json(listener_row["public_json"], {})
            context = self._session_context(work["listener_id"], work["sender_id"], listener)
            grants = listener.get("toolGrants") or []
            tools = self._granted_tool_specs(grants)
            if self.model is None or self.mcp is None:
                raise RuntimeProtocolError("RUNTIME_ADAPTER_UNAVAILABLE", "model or MCP adapter is unavailable")
            calls = self.model.plan_tools(
                question=work["question"], context=context, tools=tools,
                systemPrompt=listener.get("systemPrompt") or "",
            )
            if not isinstance(calls, list):
                raise RuntimeProtocolError("INVALID_TOOL_PLAN", "model tool plan must be a list")
            allowed = {(grant["serverId"], grant["toolName"]): grant for grant in grants}
            evidence = []
            started = time.monotonic()
            for call in calls:
                key = (str(call.get("serverId") or ""), str(call.get("toolName") or ""))
                if key not in allowed:
                    continue
                remaining = int(listener["mcpTimeoutSeconds"] - (time.monotonic() - started))
                if remaining <= 0:
                    raise RuntimeProtocolError("MCP_TIMEOUT", "MCP retrieval budget was exhausted", retryable=True)
                server = self._private_server(key[0])
                result = self.mcp.call(
                    server=server,
                    toolName=key[1],
                    arguments=call.get("arguments") if isinstance(call.get("arguments"), dict) else {},
                    timeoutSeconds=remaining,
                )
                if _has_evidence(result):
                    result = self.redact_public(result)
                    evidence.append(
                        {"serverId": key[0], "toolName": key[1], "arguments": call.get("arguments") or {}, "result": result}
                    )
            if not evidence:
                self._terminal_if_current(
                    work_id, generation, listener_generation, "skipped_no_evidence",
                    evidence=[], pending_reason="MCP returned no usable evidence",
                )
                return
            messages = decode_json(work["messages_json"], [])
            images = [image for message in messages for image in (message.get("images") or [])]
            answer = str(
                self.model.answer(
                    question=work["question"], context=context, evidence=evidence,
                    systemPrompt=listener.get("systemPrompt") or "", images=images,
                ) or ""
            ).strip()
            answer = self.redact_public(answer)
            if not answer:
                self._terminal_if_current(
                    work_id, generation, listener_generation, "skipped_empty_answer",
                    evidence=evidence, pending_reason="model returned an empty answer",
                )
                return
            visible_prefix = f"@{work['sender_name']}\n" if work["sender_name"] else ""
            max_answer_bytes = max(1, 2048 - len(visible_prefix.encode("utf-8")))
            if len(answer.encode("utf-8")) > max_answer_bytes and hasattr(self.model, "compress"):
                answer = str(
                    self.model.compress(
                        question=work["question"],
                        answer=answer,
                        evidence=evidence,
                        maxUtf8Bytes=max_answer_bytes,
                    ) or ""
                ).strip()
                answer = self.redact_public(answer)
            if not answer:
                self._terminal_if_current(
                    work_id,
                    generation,
                    listener_generation,
                    "skipped_empty_answer",
                    evidence=evidence,
                    pending_reason="model returned an empty answer after compression",
                )
                return
            review = self.model.review(question=work["question"], answer=answer, evidence=evidence)
            if not isinstance(review, dict) or review.get("supported") is not True:
                self._terminal_if_current(
                    work_id, generation, listener_generation, "skipped_review_failed",
                    evidence=evidence, answer=answer, review=review if isinstance(review, dict) else {},
                    pending_reason="independent evidence review did not pass",
                )
                return
            self._finish_reviewed_work(work_id, generation, listener_generation, evidence, answer, review)
        except RuntimeProtocolError as exc:
            self._fail_work(work_id, exc.code, exc.message, generation=generation)
        except Exception as exc:
            self._fail_work(work_id, "RETRIEVAL_FAILED", str(exc), generation=generation)

    def _finish_reviewed_work(self, work_id, generation, listener_generation, evidence, answer, review) -> None:
        now = self._now()
        should_deliver = False
        with self.store.transaction() as db:
            work = db.execute("SELECT * FROM reply_work_items WHERE id=?", (work_id,)).fetchone()
            listener_row = db.execute(
                "SELECT * FROM reply_listeners WHERE id=?", (work["listener_id"],)
            ).fetchone() if work else None
            if (
                not work or not listener_row or work["status"] != "retrieving"
                or int(work["generation"]) != generation
                or int(listener_row["generation"]) != listener_generation
            ):
                return
            listener = decode_json(listener_row["public_json"], {})
            if work["human_answered_at"] is not None:
                reason = "human_answered_during_retrieval"
            elif not listener.get("autoSend"):
                reason = "automatic_sending_disabled"
            elif not _true_mention(work):
                reason = "true_mention_unavailable"
            elif len((f"@{work['sender_name']}\n{answer}").encode("utf-8")) > 2048:
                reason = "answer_exceeds_webhook_limit"
            else:
                fingerprint = str(listener_row["webhook_fingerprint"] or "")
                confirmed = bool(
                    fingerprint
                    and listener_row["webhook_confirmed_fingerprint"] == fingerprint
                    and listener_row["webhook_confirmed_group_id"] == listener.get("groupId")
                )
                if confirmed and listener_row["webhook_url"]:
                    reason = ""
                    should_deliver = True
                else:
                    reason = "webhook_confirmation_required"
            target_status = "ready_to_send" if should_deliver else "pending"
            db.execute(
                """UPDATE reply_work_items SET status=?,evidence_json=?,answer=?,review_json=?,
                       pending_reason=?,updated_at=?,completed_at=? WHERE id=?""",
                (target_status, encode_json(evidence), answer, encode_json(review), reason, now, now, work_id),
            )
        if should_deliver:
            self._deliver_work(
                work_id,
                plain_at=False,
                automatic=True,
                expected_work_generation=generation,
                expected_listener_generation=listener_generation,
            )

    def _automatic_delivery_preflight(
        self, work_id: str, work_generation: int, listener_generation: int
    ) -> bool:
        with self.store.lock:
            initial = self.store.connection.execute(
                "SELECT listener_id FROM reply_work_items WHERE id=?", (work_id,)
            ).fetchone()
        if not initial:
            return False
        try:
            self._poll_messages(
                listener_id=str(initial["listener_id"]), force=True, strict=True
            )
            self._drain_preflight_inbox(str(initial["listener_id"]))
        except Exception:
            self._hold_automatic_work(
                work_id,
                work_generation,
                "automatic_preflight_failed",
                "AUTOMATIC_PREFLIGHT_FAILED",
            )
            return False

        with self.store.transaction() as db:
            work = db.execute(
                "SELECT * FROM reply_work_items WHERE id=?", (work_id,)
            ).fetchone()
            listener_row = db.execute(
                "SELECT * FROM reply_listeners WHERE id=?", (work["listener_id"],)
            ).fetchone() if work else None
            if (
                not work
                or not listener_row
                or work["status"] != "ready_to_send"
                or int(work["generation"]) != int(work_generation)
                or int(listener_row["generation"]) != int(listener_generation)
                or int(work["listener_generation"]) != int(listener_generation)
            ):
                return False
            listener = decode_json(listener_row["public_json"], {})
            fingerprint = str(listener_row["webhook_fingerprint"] or "")
            confirmed = bool(
                fingerprint
                and listener_row["webhook_confirmed_fingerprint"] == fingerprint
                and listener_row["webhook_confirmed_group_id"] == listener.get("groupId")
            )
            if work["human_answered_at"] is not None:
                reason = "human_answered_after_review"
            elif not listener.get("enabled") or not listener.get("autoSend"):
                reason = "automatic_sending_disabled"
            elif self._listener_health(listener).get("status") != "ready":
                reason = "listener_not_ready"
            elif not confirmed or not listener_row["webhook_url"]:
                reason = "webhook_confirmation_required"
            elif not _true_mention(work):
                reason = "true_mention_unavailable"
            else:
                return True
            db.execute(
                """UPDATE reply_work_items SET status='pending',pending_reason=?,
                       updated_at=?,completed_at=? WHERE id=? AND status='ready_to_send'""",
                (reason, self._now(), self._now(), work_id),
            )
            return False

    def _wait_for_listener_assignments(
        self, listener_id: str, deadline: float
    ) -> None:
        with self._assignment_condition:
            while listener_id in self._active_assignments.values():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeProtocolError(
                        "MESSAGE_CLASSIFICATION_TIMEOUT",
                        "in-flight message classification did not finish before automatic send preflight",
                    )
                self._assignment_condition.wait(timeout=remaining)

    def _drain_preflight_inbox(self, listener_id: str) -> None:
        deadline = time.monotonic() + 180
        for _batch in range(100):
            self._wait_for_listener_assignments(listener_id, deadline)
            if time.monotonic() >= deadline:
                raise RuntimeProtocolError(
                    "MESSAGE_CLASSIFICATION_TIMEOUT",
                    "message classification exceeded the automatic send preflight budget",
                )
            assigned = self._assign_inbox(listener_id=listener_id, strict=True)
            self._wait_for_listener_assignments(listener_id, deadline)
            with self.store.lock:
                remaining = int(
                    self.store.connection.execute(
                        """SELECT count(*) FROM reply_inbox
                           WHERE listener_id=? AND assigned_work_id IS NULL""",
                        (listener_id,),
                    ).fetchone()[0]
                )
            if remaining == 0:
                return
            if assigned == 0:
                raise RuntimeProtocolError(
                    "MESSAGE_ASSIGNMENT_STALLED",
                    "could not safely classify all group messages before automatic sending",
                )
        raise RuntimeProtocolError(
            "MESSAGE_ASSIGNMENT_BACKLOG",
            "too many group messages arrived during automatic send preflight",
        )

    def _hold_automatic_work(
        self, work_id: str, generation: int, reason: str, code: str
    ) -> None:
        now = self._now()
        public_error = self.redact_public(
            {"code": code, "message": "automatic send preflight did not complete"}
        )
        with self.store.transaction() as db:
            db.execute(
                """UPDATE reply_work_items SET status='pending',pending_reason=?,error_json=?,
                       updated_at=?,completed_at=?
                   WHERE id=? AND generation=? AND status='ready_to_send'""",
                (
                    reason,
                    encode_json(public_error),
                    now,
                    now,
                    work_id,
                    generation,
                ),
            )

    def _deliver_work(
        self,
        work_id: str,
        *,
        plain_at: bool,
        automatic: bool,
        confirmed_not_delivered: bool = False,
        expected_work_generation: int | None = None,
        expected_listener_generation: int | None = None,
    ) -> dict:
        if automatic:
            if expected_work_generation is None or expected_listener_generation is None:
                raise RuntimeProtocolError(
                    "AUTOMATIC_PREFLIGHT_REQUIRED", "automatic delivery requires generation fences"
                )
            if not self._automatic_delivery_preflight(
                work_id, expected_work_generation, expected_listener_generation
            ):
                with self.store.lock:
                    current = self.store.connection.execute(
                        "SELECT status,pending_reason FROM reply_work_items WHERE id=?", (work_id,)
                    ).fetchone()
                return {
                    "workId": work_id,
                    "status": str(current["status"] if current else "not_sent"),
                    "automatic": True,
                    "reason": str(current["pending_reason"] if current else "preflight_failed"),
                }
        now = self._now()
        with self.store.transaction() as db:
            self._assert_current_lease(db, now)
            work = db.execute("SELECT * FROM reply_work_items WHERE id=?", (work_id,)).fetchone()
            if not work:
                raise RuntimeProtocolError("WORK_NOT_FOUND", f"work item not found: {work_id}")
            listener = db.execute("SELECT * FROM reply_listeners WHERE id=?", (work["listener_id"],)).fetchone()
            if not listener or not listener["webhook_url"]:
                raise RuntimeProtocolError("WEBHOOK_NOT_CONFIGURED", "listener webhook is not configured")
            if int(work["listener_generation"]) != int(listener["generation"]):
                raise RuntimeProtocolError(
                    "WORK_CONFIGURATION_CHANGED",
                    "the listener configuration changed after this answer was produced",
                )
            if expected_work_generation is not None and int(work["generation"]) != int(expected_work_generation):
                raise RuntimeProtocolError("WORK_VERSION_CONFLICT", "work item changed before delivery")
            if expected_listener_generation is not None and int(listener["generation"]) != int(expected_listener_generation):
                raise RuntimeProtocolError("WORK_CONFIGURATION_CHANGED", "listener changed before delivery")
            if automatic and work["status"] != "ready_to_send":
                raise RuntimeProtocolError("AUTOMATIC_SEND_BLOCKED", "automatic send safety state changed")
            if not automatic and work["status"] not in {
                "pending", "delivery_failed", "delivery_unknown"
            }:
                raise RuntimeProtocolError(
                    "WORK_NOT_PENDING", "work item is no longer eligible for manual sending"
                )
            existing = db.execute("SELECT * FROM reply_outbox WHERE work_id=?", (work_id,)).fetchone()
            if existing and existing["status"] == "sent":
                return {"workId": work_id, "status": "sent", "alreadySent": True}
            if existing and existing["status"] in {"sending", "delivery_unknown"} and not confirmed_not_delivered:
                raise RuntimeProtocolError(
                    "DELIVERY_CONFIRMATION_REQUIRED",
                    "delivery result is unknown; confirm the message is absent before retrying",
                )
            account = str(work["sender_account"] or "").strip()
            mobile = str(work["sender_mobile"] or "").strip()
            if plain_at:
                mentioned, mentioned_mobile = [], []
            elif account and not account.isdigit():
                mentioned, mentioned_mobile = [account], []
            elif re.fullmatch(r"\+?\d{6,20}", mobile):
                mentioned, mentioned_mobile = [], [mobile]
            else:
                raise RuntimeProtocolError(
                    "TRUE_MENTION_UNAVAILABLE",
                    "sender account/mobile could not be resolved; use work.send_plain_at explicitly",
                )
            visible_prefix = f"@{work['sender_name']}\n" if work["sender_name"] else ""
            text = visible_prefix + str(work["answer"] or "")
            if len(text.encode("utf-8")) > 2048:
                raise RuntimeProtocolError(
                    "WEBHOOK_TEXT_TOO_LONG", "final webhook text exceeds 2048 UTF-8 bytes"
                )
            self._enforce_webhook_rate(db, listener["webhook_fingerprint"])
            delivery_id = str(uuid.uuid4())
            payload = {
                "webhookUrl": listener["webhook_url"], "text": text,
                "mentionedList": mentioned, "mentionedMobileList": mentioned_mobile,
                "timeoutSeconds": 15, "deliveryId": delivery_id,
            }
            stored_payload = {key: value for key, value in payload.items() if key != "webhookUrl"}
            if existing:
                db.execute(
                    """UPDATE reply_outbox SET delivery_id=?,payload_json=?,status='sending',
                           attempt_count=attempt_count+1,response_json=NULL,error_json=NULL,updated_at=? WHERE work_id=?""",
                    (delivery_id, encode_json(stored_payload), now, work_id),
                )
            else:
                db.execute(
                    "INSERT INTO reply_outbox VALUES(?,?,?,?,?,?,?,?,?)",
                    (work_id, delivery_id, encode_json(stored_payload), "sending", 1, None, None, now, now),
                )
            db.execute(
                "UPDATE reply_work_items SET status='sending',pending_reason='',updated_at=? WHERE id=?",
                (now, work_id),
            )
            db.execute(
                "INSERT INTO webhook_deliveries VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    delivery_id,
                    listener["webhook_fingerprint"],
                    work["listener_id"],
                    work_id,
                    "reply",
                    "sending",
                    None,
                    None,
                    now,
                ),
            )
        if self.webhook is None:
            self._complete_delivery(work_id, delivery_id, "failed", None, {"message": "webhook adapter unavailable"})
            raise RuntimeProtocolError("WEBHOOK_ADAPTER_UNAVAILABLE", "webhook adapter is unavailable")
        try:
            response = self.webhook.send(**payload)
            status = _delivery_status(response)
            error = None
        except TimeoutError as exc:
            response, status, error = None, "delivery_unknown", {"message": str(exc)}
        except RuntimeProtocolError as exc:
            uncertain = exc.code in {
                "WEBHOOK_NETWORK_ERROR", "WEBHOOK_TIMEOUT", "WEBHOOK_DELIVERY_UNKNOWN",
                "WEBHOOK_INVALID_RESPONSE",
            }
            response = None
            status = "delivery_unknown" if uncertain else "failed"
            error = exc.as_dict()
        except Exception as exc:
            response, status, error = None, "delivery_unknown", {"message": str(exc)}
        self._complete_delivery(work_id, delivery_id, status, response, error)
        return {"workId": work_id, "status": status, "automatic": automatic}

    def _complete_delivery(self, work_id, delivery_id, status, response, error) -> None:
        now = self._now()
        response = self.redact_public(response)
        error = self.redact_public(error)
        with self.store.transaction() as db:
            outbox = db.execute("SELECT * FROM reply_outbox WHERE work_id=?", (work_id,)).fetchone()
            work = db.execute("SELECT * FROM reply_work_items WHERE id=?", (work_id,)).fetchone()
            listener = db.execute("SELECT * FROM reply_listeners WHERE id=?", (work["listener_id"],)).fetchone() if work else None
            if not outbox or outbox["delivery_id"] != delivery_id or not listener:
                return
            db.execute(
                "UPDATE reply_outbox SET status=?,response_json=?,error_json=?,updated_at=? WHERE work_id=?",
                (status, encode_json(response) if response is not None else None,
                 encode_json(error) if error is not None else None, now, work_id),
            )
            db.execute(
                """UPDATE webhook_deliveries SET status=?,response_json=?,error_json=?
                   WHERE id=?""",
                (
                    status,
                    encode_json(response) if response is not None else None,
                    encode_json(error) if error is not None else None,
                    delivery_id,
                ),
            )
            work_status = "sent" if status == "sent" else ("delivery_unknown" if status == "delivery_unknown" else "delivery_failed")
            db.execute(
                "UPDATE reply_work_items SET status=?,pending_reason=?,updated_at=?,completed_at=? WHERE id=?",
                (
                    work_status,
                    "" if status == "sent" else ("delivery result is unknown" if status == "delivery_unknown" else "webhook send failed"),
                    now, now, work_id,
                ),
            )
            if status in {"sent", "delivery_unknown"}:
                payload = decode_json(outbox["payload_json"], {})
                db.execute(
                    "INSERT OR REPLACE INTO recent_outbound(fingerprint,group_id,sent_at) VALUES(?,?,?)",
                    (
                        _outbound_fingerprint(work["group_id"], str(payload.get("text") or "")),
                        work["group_id"], now,
                    ),
                )
            if status == "sent":
                self._append_session_turn_in_transaction(
                    db,
                    work["listener_id"],
                    work["sender_id"],
                    {"question": work["question"], "answer": work["answer"], "answeredBy": "ai"},
                    now,
                )

    def _terminal_if_current(
        self, work_id, generation, listener_generation, status, *, evidence=None,
        answer="", review=None, pending_reason="",
    ) -> None:
        now = self._now()
        with self.store.transaction() as db:
            work = db.execute("SELECT * FROM reply_work_items WHERE id=?", (work_id,)).fetchone()
            listener = db.execute(
                "SELECT generation FROM reply_listeners WHERE id=?", (work["listener_id"],)
            ).fetchone() if work else None
            if (
                not work or not listener or work["status"] != "retrieving"
                or int(work["generation"]) != generation
                or int(listener["generation"]) != listener_generation
            ):
                return
            db.execute(
                """UPDATE reply_work_items SET status=?,evidence_json=?,answer=?,review_json=?,
                       pending_reason=?,updated_at=?,completed_at=? WHERE id=?""",
                (
                    status, encode_json(evidence or []), answer,
                    encode_json(review) if review is not None else None,
                    pending_reason, now, now, work_id,
                ),
            )

    def _fail_work(
        self,
        work_id: str,
        code: str,
        message: str,
        *,
        generation: int | None = None,
        expected_status: str | None = None,
    ) -> None:
        now = self._now()
        public_error = self.redact_public({"code": code, "message": message})
        with self.store.transaction() as db:
            row = db.execute("SELECT generation,status FROM reply_work_items WHERE id=?", (work_id,)).fetchone()
            required_status = expected_status or ("retrieving" if generation is not None else None)
            if (
                not row
                or (generation is not None and int(row["generation"]) != generation)
                or (required_status is not None and row["status"] != required_status)
            ):
                return
            db.execute(
                "UPDATE reply_work_items SET status='failed',error_json=?,updated_at=?,completed_at=? WHERE id=?",
                (encode_json(public_error), now, now, work_id),
            )

    def _reap_futures(self) -> int:
        with self._future_lock:
            completed = [key for key, entry in self._futures.items() if entry[0].done()]
            for key in completed:
                self._futures.pop(key, None)
            return len(completed)

    def _in_flight_count(self) -> int:
        with self._future_lock:
            return sum(1 for entry in self._futures.values() if not entry[0].done())

    def _cancel_work_futures(self, work_id: str) -> None:
        with self._future_lock:
            for (candidate_id, _generation), entry in self._futures.items():
                if candidate_id == work_id:
                    entry[0].cancel()

    def _cancel_listener_futures(self, listener_id: str) -> None:
        with self._future_lock:
            for entry in self._futures.values():
                if entry[1] == listener_id:
                    entry[0].cancel()

    def _granted_tool_specs(self, grants: list[dict]) -> list[dict]:
        result = []
        with self.store.lock:
            for grant in grants:
                if grant.get("invalidated"):
                    continue
                row = self.store.connection.execute(
                    """SELECT public_json,catalog_json,catalog_error_json,connection_fingerprint,
                              catalog_connection_fingerprint FROM mcp_servers WHERE id=?""",
                    (grant["serverId"],),
                ).fetchone()
                server = decode_json(row["public_json"], {}) if row else {}
                if (
                    not row
                    or not server.get("enabled", True)
                    or row["catalog_error_json"] is not None
                    or str(row["connection_fingerprint"] or "")
                    != str(row["catalog_connection_fingerprint"] or "")
                ):
                    continue
                tool = next(
                    (item for item in decode_json(row["catalog_json"], []) if item.get("name") == grant["toolName"]),
                    None,
                )
                if tool and tool.get("schemaSha256") == grant.get("schemaSha256"):
                    result.append({**tool, "serverId": grant["serverId"]})
        return result

    def _private_server(self, server_id: str) -> dict:
        with self.store.lock:
            row = self.store.connection.execute("SELECT * FROM mcp_servers WHERE id=?", (server_id,)).fetchone()
        if not row:
            raise RuntimeProtocolError("MCP_NOT_FOUND", f"MCP server not found: {server_id}")
        server = decode_json(row["public_json"], {})
        server["secrets"] = decode_json(row["secret_json"], {})
        return server

    def _session_context(self, listener_id: str, sender_id: str, listener: dict) -> list[dict]:
        with self.store.lock:
            row = self.store.connection.execute(
                "SELECT * FROM sender_sessions WHERE listener_id=? AND sender_id=?",
                (listener_id, sender_id),
            ).fetchone()
        if not row or self._now() - float(row["last_activity_at"]) > int(listener["sessionTimeoutSeconds"]):
            return []
        return decode_json(row["turns_json"], [])

    def _append_session_turn_in_transaction(
        self, db, listener_id: str, sender_id: str, turn: dict, now: float
    ) -> None:
        row = db.execute(
            """SELECT turns_json,last_activity_at FROM sender_sessions
               WHERE listener_id=? AND sender_id=?""",
            (listener_id, sender_id),
        ).fetchone()
        listener_row = db.execute(
            "SELECT public_json FROM reply_listeners WHERE id=?", (listener_id,)
        ).fetchone()
        listener = decode_json(listener_row["public_json"], {}) if listener_row else {}
        timeout_seconds = int(listener.get("sessionTimeoutSeconds") or 1800)
        expired = bool(
            row and now - float(row["last_activity_at"] or 0) > timeout_seconds
        )
        turns = decode_json(row["turns_json"], []) if row and not expired else []
        turns.append(turn)
        turns = turns[-6:]
        while turns and len(encode_json(turns)) > 32_000:
            turns.pop(0)
        db.execute(
            """INSERT INTO sender_sessions(listener_id,sender_id,turns_json,last_activity_at)
               VALUES(?,?,?,?) ON CONFLICT(listener_id,sender_id)
               DO UPDATE SET turns_json=excluded.turns_json,last_activity_at=excluded.last_activity_at""",
            (listener_id, sender_id, encode_json(turns), now),
        )

    def _query_work_list(self, query: dict) -> dict:
        clauses = []
        values = []
        if query.get("listenerId"):
            clauses.append("listener_id=?")
            values.append(str(query["listenerId"]))
        if query.get("status"):
            statuses = query["status"] if isinstance(query["status"], list) else [query["status"]]
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            values.extend(str(status) for status in statuses)
        bucket = str(query.get("bucket") or "").strip().lower()
        pending_statuses = ("pending", "delivery_unknown", "delivery_failed")
        active_statuses = (
            "collecting", "waiting_for_human_reply", "queued_retrieval",
            "retrieving", "ready_to_send", "sending",
        )
        if bucket == "pending":
            clauses.append("status IN (?,?,?)")
            values.extend(pending_statuses)
        elif bucket == "history":
            excluded = pending_statuses + active_statuses
            clauses.append(f"status NOT IN ({','.join('?' for _ in excluded)})")
            values.extend(excluded)
        elif bucket == "active":
            clauses.append(f"status IN ({','.join('?' for _ in active_statuses)})")
            values.extend(active_statuses)
        elif bucket:
            raise RuntimeProtocolError(
                "INVALID_QUERY", "work.list bucket must be pending, history, or active"
            )
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        limit = max(1, min(int(query.get("limit") or 100), 500))
        if query.get("offset") is not None:
            offset = max(0, int(query.get("offset") or 0))
        else:
            page = max(1, int(query.get("page") or 1))
            offset = (page - 1) * limit
        with self.store.lock:
            total = int(
                self.store.connection.execute(
                    f"SELECT count(*) FROM reply_work_items{where}", tuple(values)
                ).fetchone()[0]
            )
            rows = self.store.connection.execute(
                f"""SELECT * FROM reply_work_items{where}
                    ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?""",
                (*values, limit, offset),
            ).fetchall()
            return {
                "revision": self.store.revision(),
                "items": [self._public_work(row, detail=False) for row in rows],
                "total": total,
                "offset": offset,
                "limit": limit,
                "hasMore": offset + len(rows) < total,
            }

    def _public_work(self, row, *, detail: bool) -> dict:
        listener_row = self.store.connection.execute(
            "SELECT public_json FROM reply_listeners WHERE id=?", (row["listener_id"],)
        ).fetchone()
        listener = decode_json(listener_row["public_json"], {}) if listener_row else {}
        account = str(row["sender_account"] or "").strip()
        mobile = str(row["sender_mobile"] or "").strip()
        mention_mode = (
            "userid" if account and not account.isdigit()
            else "mobile" if re.fullmatch(r"\+?\d{6,20}", mobile)
            else "unresolved"
        )
        error = decode_json(row["error_json"], None)
        result = {
            "id": row["id"], "listenerId": row["listener_id"], "groupId": row["group_id"],
            "version": int(row["generation"]),
            "listenerName": str(listener.get("name") or ""),
            "groupName": str(
                (listener.get("groupName") or row["group_id"])
                if listener.get("groupId") == row["group_id"]
                else row["group_id"]
            ),
            "senderId": row["sender_id"], "senderName": row["sender_name"],
            "status": row["status"], "question": row["question"], "answer": row["answer"],
            "pendingReason": row["pending_reason"], "generation": int(row["generation"]),
            "reason": (error or {}).get("message") if isinstance(error, dict) else row["pending_reason"],
            "humanAnsweredAt": _iso_time(row["human_answered_at"]),
            "createdAt": _iso_time(row["created_at"]),
            "updatedAt": _iso_time(row["updated_at"]),
            "completedAt": _iso_time(row["completed_at"]),
            "mentionMode": mention_mode,
            "mention": {
                "available": mention_mode != "unresolved",
                "accountConfigured": bool(account),
                "mobileConfigured": bool(mobile),
            },
            "error": error,
        }
        if detail:
            evidence_items = []
            for item in decode_json(row["evidence_json"], []):
                if not isinstance(item, dict):
                    continue
                server_id = str(item.get("serverId") or "")
                server_row = self.store.connection.execute(
                    "SELECT public_json FROM mcp_servers WHERE id=?", (server_id,)
                ).fetchone()
                server = decode_json(server_row["public_json"], {}) if server_row else {}
                summary = _evidence_summary(item.get("result"))
                evidence_items.append(
                    {
                        "serverId": server_id,
                        "serverName": str(server.get("name") or ""),
                        "toolName": str(item.get("toolName") or ""),
                        "summary": summary,
                    }
                )
            human_message = decode_json(row["human_answer_message_json"], None)
            safe_human_message = None
            if isinstance(human_message, dict):
                safe_human_message = {
                    "senderName": str(human_message.get("senderName") or ""),
                    "text": str(human_message.get("text") or ""),
                    "sendTime": human_message.get("sendTime"),
                    "contentType": str(human_message.get("contentType") or "text"),
                }
            result.update(
                {
                    "evidence": evidence_items,
                    "review": decode_json(row["review_json"], None),
                    "humanAnswerMessage": safe_human_message,
                }
            )
        return self.redact_public(result)

    def _emit_event(self, event: dict) -> int:
        event = self.redact_public(event)
        now = self._now()
        with self.store.transaction() as db:
            seq = int(db.execute("SELECT value FROM runtime_meta WHERE key='event_seq'").fetchone()[0]) + 1
            db.execute("UPDATE runtime_meta SET value=? WHERE key='event_seq'", (str(seq),))
            db.execute(
                "INSERT INTO runtime_events(seq,event_json,created_at) VALUES(?,?,?)",
                (seq, encode_json(event), now),
            )
        if self.event_sink:
            try:
                self.event_sink(seq, event)
            except Exception:
                pass
        return seq

    def _public_server(self, row) -> dict:
        result = decode_json(row["public_json"], {})
        secrets = decode_json(row["secret_json"], {})
        result["revision"] = int(row["revision"])
        result["secrets"] = {
            "headersConfigured": bool(secrets.get("headers")),
            "envConfigured": bool(secrets.get("env")),
            "fingerprint": _sha256_json(secrets) if secrets else "",
        }
        result["catalog"] = {
            "toolCount": len(decode_json(row["catalog_json"], [])),
            "updatedAt": _iso_time(row["catalog_updated_at"]),
            "error": decode_json(row["catalog_error_json"], None),
        }
        result["updatedAt"] = _iso_time(row["updated_at"])
        result["toolCount"] = len(decode_json(row["catalog_json"], []))
        return result

    def redact_public(self, value):
        return _redact_structure(value, self._configured_secret_values())

    def _configured_secret_values(self) -> set[str]:
        values: set[str] = set()
        try:
            with self.store.lock:
                listeners = self.store.connection.execute(
                    "SELECT webhook_url FROM reply_listeners WHERE webhook_url<>''"
                ).fetchall()
                servers = self.store.connection.execute(
                    "SELECT secret_json FROM mcp_servers"
                ).fetchall()
            for row in listeners:
                url = str(row["webhook_url"] or "")
                if url:
                    values.add(url)
                    match = re.search(r"[?&]key=([^&\s]+)", url, flags=re.IGNORECASE)
                    if match:
                        values.add(match.group(1))
            for row in servers:
                _collect_secret_strings(decode_json(row["secret_json"], {}), values)
        except Exception:
            pass
        if self.config_path:
            try:
                config = json.loads(Path(self.config_path).read_text(encoding="utf-8"))
                _collect_keyed_secret_strings(config, values)
            except Exception:
                pass
        return {value for value in values if len(value) >= 4}

    def _now(self) -> float:
        if self.clock is None:
            return time.time()
        value = getattr(self.clock, "now", None)
        return float(value() if callable(value) else value)


def _merge_secret(current: dict, name: str, raw: dict, body: dict) -> dict:
    result = dict(current)
    direct_present = name in raw
    patch_root = body.get("secrets") or body.get("secretPatch") or raw.get("secretPatch") or {}
    patch = patch_root.get(name) if isinstance(patch_root, dict) else None
    if direct_present:
        value = raw.get(name)
        if value is None:
            result.pop(name, None)
        elif isinstance(value, dict):
            result[name] = value
        else:
            raise RuntimeProtocolError("INVALID_SECRET", f"{name} must be an object")
    elif isinstance(patch, dict):
        operation = str(patch.get("mode") or patch.get("op") or "keep")
        if operation == "clear":
            result.pop(name, None)
        elif operation == "replace":
            value = patch.get("value")
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError as exc:
                    raise RuntimeProtocolError(
                        "INVALID_SECRET", f"replacement {name} must contain a JSON object"
                    ) from exc
            if not isinstance(value, dict):
                raise RuntimeProtocolError("INVALID_SECRET", f"replacement {name} must be an object")
            result[name] = value
        elif operation != "keep":
            raise RuntimeProtocolError("INVALID_SECRET", f"unknown secret operation: {operation}")
    return result


def _sha256_json(value) -> str:
    return hashlib.sha256(encode_json(value).encode("utf-8")).hexdigest()


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16] if value else ""


def _mcp_connection_fingerprint(public: dict, secrets_value: dict) -> str:
    transport = str(public.get("transportType") or public.get("transport") or "").lower()
    if transport == "stdio":
        connection = {
            "transportType": transport,
            "command": str(public.get("command") or ""),
            "args": [str(value) for value in public.get("args") or []],
            "cwd": str(public.get("cwd") or ""),
            "env": secrets_value.get("env") or {},
        }
    else:
        connection = {
            "transportType": transport,
            "url": str(public.get("url") or ""),
            "headers": secrets_value.get("headers") or {},
        }
    return _sha256_json(connection)


def _normalize_tools(raw_tools) -> list[dict]:
    if not isinstance(raw_tools, list):
        raise RuntimeProtocolError("INVALID_MCP_CATALOG", "MCP tools response must be a list")
    result = []
    seen = set()
    for raw in raw_tools:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        schema = raw.get("inputSchema") or raw.get("input_schema") or {"type": "object"}
        if not name or name in seen or not isinstance(schema, dict):
            continue
        seen.add(name)
        result.append(
            {
                "name": name,
                "description": str(raw.get("description") or ""),
                "inputSchema": schema,
                "schemaSha256": _sha256_json(schema),
            }
        )
    return sorted(result, key=lambda item: item["name"])


def _bounded_int(raw: dict, name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(raw.get(name, default))
    except (TypeError, ValueError) as exc:
        raise RuntimeProtocolError("INVALID_LISTENER", f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise RuntimeProtocolError(
            "INVALID_LISTENER",
            f"{name} must be between {minimum} and {maximum}",
        )
    return value


def _secret_string_update(current: str, raw: dict, body: dict, name: str) -> str:
    if name in raw:
        return str(raw.get(name) or "").strip()
    patch_root = body.get("secrets") or body.get("secretPatch") or raw.get("secretPatch") or {}
    patch = patch_root.get(name) if isinstance(patch_root, dict) else None
    if not isinstance(patch, dict):
        return current
    operation = str(patch.get("mode") or patch.get("op") or "keep")
    if operation == "keep":
        return current
    if operation == "clear":
        return ""
    if operation == "replace":
        return str(patch.get("value") or "").strip()
    raise RuntimeProtocolError("INVALID_SECRET", f"unknown secret operation: {operation}")


def _message_cursor(message: dict) -> list:
    raw = message.get("cursor")
    if isinstance(raw, (list, tuple)) and len(raw) >= 4:
        # The source owns the opaque cursor representation and receives it back on
        # the next read. Preserve its values; use _cursor_sort_key only for local
        # ordering so numeric and opaque source implementations both remain valid.
        return list(raw[:4])
    return [
        message.get("sendTime") or 0,
        message.get("sequence") or 0,
        message.get("messageId") or 0,
        message.get("serverId") or 0,
    ]


def _cursor_sort_key(cursor) -> tuple:
    values = list(cursor or [])
    values.extend([0] * (4 - len(values)))
    return (
        _cursor_number(values[0]),
        _cursor_number(values[1]),
        _cursor_component(values[2]),
        _cursor_component(values[3]),
    )


def _cursor_number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _cursor_component(value) -> tuple[int, int | str]:
    try:
        return (0, int(value or 0))
    except (TypeError, ValueError):
        return (1, str(value or ""))


def _normalize_message(message: dict, group_id: str) -> dict:
    if not isinstance(message, dict):
        raise RuntimeProtocolError("INVALID_MESSAGE", "message source returned a non-object")
    cursor = _message_cursor(message)
    message_id = str(message.get("messageId") or "").strip()
    sender_id = str(message.get("senderId") or "").strip()
    if not message_id or not sender_id:
        raise RuntimeProtocolError("INVALID_MESSAGE", "messageId and senderId are required")
    images = message.get("images") or []
    if not isinstance(images, list):
        images = []
    return {
        "cursor": cursor,
        "messageId": message_id,
        "serverId": str(message.get("serverId") or ""),
        "sequence": int(message.get("sequence") or 0),
        "sendTime": int(message.get("sendTime") or cursor[0]),
        "groupId": str(message.get("groupId") or group_id),
        "senderId": sender_id,
        "senderName": str(message.get("senderName") or sender_id),
        "account": str(message.get("account") or ""),
        "mobile": str(message.get("mobile") or ""),
        "contentType": str(message.get("contentType") or "text").lower(),
        "text": str(message.get("text") or "").strip(),
        "link": message.get("link") if isinstance(message.get("link"), dict) else None,
        "images": images,
    }


def _question_text(messages: list[dict]) -> str:
    pieces = []
    for message in messages:
        text = str(message.get("text") or "").strip()
        link = message.get("link") if isinstance(message.get("link"), dict) else None
        if text:
            pieces.append(text)
        if link:
            title = str(link.get("title") or "").strip()
            url = str(link.get("url") or "").strip()
            if title or url:
                pieces.append(" ".join(item for item in (title, url) if item))
        if message.get("images") and not text:
            pieces.append("[图片]")
    return "\n".join(pieces).strip()


def _has_evidence(value) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bool(bytes(value))
    if isinstance(value, (list, tuple, set)):
        return any(_has_evidence(item) for item in value)
    if isinstance(value, dict):
        if value.get("isError") is True or value.get("is_error") is True:
            return False
        semantic_keys = (
            "content", "structuredContent", "structured_content", "text", "data", "resource"
        )
        present = [key for key in semantic_keys if key in value]
        if present:
            return any(_has_evidence(value.get(key)) for key in present)
        ignored_keys = {
            "type", "isError", "is_error", "meta", "_meta", "metadata", "annotations",
            "mimeType", "mime_type",
        }
        return any(
            _has_evidence(item) for key, item in value.items() if key not in ignored_keys
        )
    return bool(value)


def _true_mention(work) -> bool:
    account = str(work["sender_account"] or "").strip()
    if account and not account.isdigit():
        return True
    mobile = str(work["sender_mobile"] or "").strip()
    return bool(re.fullmatch(r"\+?\d{6,20}", mobile))


def _delivery_status(response) -> str:
    if response is None:
        return "sent"
    if isinstance(response, dict):
        explicit = str(response.get("status") or "").lower()
        if explicit in {"sent", "success", "ok"}:
            return "sent"
        if explicit in {"delivery_unknown", "unknown", "timeout"}:
            return "delivery_unknown"
        if explicit in {"failed", "error", "rejected"}:
            return "failed"
        errcode = response.get("errcode")
        if errcode is not None:
            return "sent" if int(errcode) == 0 else "failed"
    return "sent" if response is True else "failed"


def _iso_time(value) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _outbound_fingerprint(group_id: str, text: str) -> str:
    normalized = " ".join(str(text or "").split())
    return hashlib.sha256(f"{group_id}\n{normalized}".encode("utf-8")).hexdigest()


def _evidence_summary(value, *, max_chars: int = 600) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(value or "")
    text = " ".join(text.split())
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def _command_result(value: dict) -> dict:
    if isinstance(value, dict) and value.get("__in_progress__"):
        raise RuntimeProtocolError(
            "COMMAND_IN_PROGRESS",
            "an identical command is already in progress",
            retryable=True,
            details={"deliveryId": value.get("deliveryId")},
        )
    if isinstance(value, dict) and isinstance(value.get("__error__"), dict):
        error = value["__error__"]
        raise RuntimeProtocolError(
            str(error.get("code") or "COMMAND_FAILED"),
            str(error.get("message") or "command failed"),
            retryable=bool(error.get("retryable", False)),
            details=error.get("details") if isinstance(error.get("details"), dict) else None,
        )
    return value


def _collect_secret_strings(value, result: set[str]) -> None:
    if isinstance(value, dict):
        for nested in value.values():
            _collect_secret_strings(nested, result)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _collect_secret_strings(nested, result)
    elif isinstance(value, str) and value:
        result.add(value)


def _collect_keyed_secret_strings(value, result: set[str], *, secret_context: bool = False) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            keyed_secret = secret_context or bool(
                re.search(r"api.?key|token|secret|authorization|password|webhook", str(key), re.I)
            )
            _collect_keyed_secret_strings(nested, result, secret_context=keyed_secret)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _collect_keyed_secret_strings(nested, result, secret_context=secret_context)
    elif secret_context and isinstance(value, str) and value:
        result.add(value)


def _redact_structure(value, secrets_value: set[str]):
    if isinstance(value, dict):
        return {str(key): _redact_structure(nested, secrets_value) for key, nested in value.items()}
    if isinstance(value, list):
        return [_redact_structure(nested, secrets_value) for nested in value]
    if isinstance(value, tuple):
        return [_redact_structure(nested, secrets_value) for nested in value]
    if not isinstance(value, str):
        return value
    text = value
    for secret_value in sorted(secrets_value, key=len, reverse=True):
        text = text.replace(secret_value, "[REDACTED]")
    text = re.sub(
        r"(https://qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=)[^&\s]+",
        r"\1[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
    return text
