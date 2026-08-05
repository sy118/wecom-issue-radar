from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO runtime_meta(key, value) VALUES ('revision', '0');
INSERT OR IGNORE INTO runtime_meta(key, value) VALUES ('event_seq', '0');

CREATE TABLE IF NOT EXISTS runtime_commands (
    command_id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_servers (
    id TEXT PRIMARY KEY,
    public_json TEXT NOT NULL,
    secret_json TEXT NOT NULL,
    revision INTEGER NOT NULL,
    connection_fingerprint TEXT NOT NULL DEFAULT '',
    catalog_json TEXT NOT NULL DEFAULT '[]',
    catalog_connection_fingerprint TEXT NOT NULL DEFAULT '',
    catalog_error_json TEXT,
    catalog_updated_at REAL,
    last_test_result_json TEXT,
    last_tested_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS reply_listeners (
    id TEXT PRIMARY KEY,
    public_json TEXT NOT NULL,
    webhook_url TEXT NOT NULL DEFAULT '',
    webhook_fingerprint TEXT NOT NULL DEFAULT '',
    webhook_test_code TEXT,
    webhook_tested_at REAL,
    webhook_confirmed_fingerprint TEXT,
    webhook_confirmed_group_id TEXT,
    webhook_confirmed_at REAL,
    revision INTEGER NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_enabled_listener_per_group
ON reply_listeners(json_extract(public_json, '$.groupId'))
WHERE json_extract(public_json, '$.enabled') = 1;

CREATE TABLE IF NOT EXISTS runtime_cursors (
    listener_id TEXT PRIMARY KEY REFERENCES reply_listeners(id) ON DELETE CASCADE,
    cursor_json TEXT,
    next_poll_at REAL NOT NULL DEFAULT 0,
    initialized_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS reply_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listener_id TEXT NOT NULL REFERENCES reply_listeners(id) ON DELETE CASCADE,
    group_id TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL,
    server_id TEXT NOT NULL DEFAULT '',
    sequence INTEGER NOT NULL DEFAULT 0,
    send_time REAL NOT NULL,
    payload_json TEXT NOT NULL,
    assigned_work_id TEXT,
    retry_after REAL NOT NULL DEFAULT 0,
    classification_attempts INTEGER NOT NULL DEFAULT 0,
    classification_error_json TEXT,
    received_at REAL NOT NULL,
    duplicate_of_inbox_id INTEGER REFERENCES reply_inbox(id),
    UNIQUE(listener_id, send_time, sequence, message_id, server_id)
);
CREATE INDEX IF NOT EXISTS reply_inbox_unassigned
ON reply_inbox(listener_id, assigned_work_id, send_time, sequence);

CREATE TABLE IF NOT EXISTS reply_work_items (
    id TEXT PRIMARY KEY,
    listener_id TEXT NOT NULL REFERENCES reply_listeners(id) ON DELETE CASCADE,
    group_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',
    sender_account TEXT NOT NULL DEFAULT '',
    sender_mobile TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    question TEXT NOT NULL DEFAULT '',
    messages_json TEXT NOT NULL DEFAULT '[]',
    group_context_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    answer TEXT NOT NULL DEFAULT '',
    review_json TEXT,
    error_json TEXT,
    pending_reason TEXT NOT NULL DEFAULT '',
    generation INTEGER NOT NULL DEFAULT 1,
    listener_generation INTEGER NOT NULL,
    merge_due_at REAL,
    human_wait_due_at REAL,
    image_retry_at REAL,
    image_wait_due_at REAL,
    human_answered_at REAL,
    human_answer_message_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL,
    duplicate_of_work_id TEXT REFERENCES reply_work_items(id)
);
CREATE INDEX IF NOT EXISTS reply_work_by_listener_status
ON reply_work_items(listener_id, status, created_at);
CREATE INDEX IF NOT EXISTS reply_work_by_sender
ON reply_work_items(listener_id, sender_id, created_at);

CREATE TABLE IF NOT EXISTS sender_sessions (
    listener_id TEXT NOT NULL REFERENCES reply_listeners(id) ON DELETE CASCADE,
    sender_id TEXT NOT NULL,
    turns_json TEXT NOT NULL DEFAULT '[]',
    last_activity_at REAL NOT NULL,
    PRIMARY KEY(listener_id, sender_id)
);

CREATE TABLE IF NOT EXISTS runtime_events (
    seq INTEGER PRIMARY KEY,
    event_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id TEXT PRIMARY KEY,
    webhook_fingerprint TEXT NOT NULL,
    listener_id TEXT NOT NULL,
    work_id TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    response_json TEXT,
    error_json TEXT,
    attempted_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS webhook_rate_window
ON webhook_deliveries(webhook_fingerprint, attempted_at);

CREATE TABLE IF NOT EXISTS reply_outbox (
    work_id TEXT PRIMARY KEY REFERENCES reply_work_items(id) ON DELETE CASCADE,
    delivery_id TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    response_json TEXT,
    error_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_lease (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    owner_id TEXT NOT NULL,
    expires_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS recent_outbound (
    fingerprint TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    sent_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS outbound_reservations (
    delivery_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    group_id TEXT NOT NULL,
    reserved_at REAL NOT NULL,
    PRIMARY KEY(delivery_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS outbound_reservations_by_fingerprint
ON outbound_reservations(fingerprint, reserved_at);
"""


class RuntimeStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.executescript(SCHEMA)
        self._migrate_schema()
        self.lock = threading.RLock()

    def _migrate_schema(self) -> None:
        mcp_columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(mcp_servers)").fetchall()
        }
        if "connection_fingerprint" not in mcp_columns:
            self.connection.execute(
                "ALTER TABLE mcp_servers ADD COLUMN connection_fingerprint TEXT NOT NULL DEFAULT ''"
            )
        if "catalog_connection_fingerprint" not in mcp_columns:
            self.connection.execute(
                "ALTER TABLE mcp_servers ADD COLUMN catalog_connection_fingerprint TEXT NOT NULL DEFAULT ''"
            )
        for name, declaration in (
            ("last_test_result_json", "TEXT"),
            ("last_tested_at", "REAL"),
        ):
            if name not in mcp_columns:
                self.connection.execute(
                    f"ALTER TABLE mcp_servers ADD COLUMN {name} {declaration}"
                )
        inbox_columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(reply_inbox)").fetchall()
        }
        for name, declaration in (
            ("group_id", "TEXT NOT NULL DEFAULT ''"),
            ("retry_after", "REAL NOT NULL DEFAULT 0"),
            ("classification_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("classification_error_json", "TEXT"),
            ("duplicate_of_inbox_id", "INTEGER REFERENCES reply_inbox(id)"),
        ):
            if name not in inbox_columns:
                self.connection.execute(
                    f"ALTER TABLE reply_inbox ADD COLUMN {name} {declaration}"
                )
        work_columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(reply_work_items)").fetchall()
        }
        for name, declaration in (
            ("image_retry_at", "REAL"),
            ("image_wait_due_at", "REAL"),
        ):
            if name not in work_columns:
                self.connection.execute(
                    f"ALTER TABLE reply_work_items ADD COLUMN {name} {declaration}"
                )
        if "duplicate_of_work_id" not in work_columns:
            self.connection.execute(
                "ALTER TABLE reply_work_items ADD COLUMN duplicate_of_work_id TEXT REFERENCES reply_work_items(id)"
            )
        self.connection.execute(
            """UPDATE reply_inbox
               SET group_id=coalesce(json_extract(payload_json,'$.groupId'),'')
               WHERE group_id=''"""
        )
        # v3.2.20 keyed replays by server_id, but WeCom can expose the same
        # message first without server metadata and enrich it a few seconds later.
        # Rebuild the index around the provisional identity before folding legacy
        # rows so both snapshots can only own one canonical inbox item.
        self.connection.execute("DROP INDEX IF EXISTS reply_inbox_stable_identity")
        self._fold_stable_message_duplicates()
        self.connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS reply_inbox_stable_identity
               ON reply_inbox(listener_id,group_id,message_id)
               WHERE duplicate_of_inbox_id IS NULL
                 AND message_id NOT IN ('','0')"""
        )
        self.connection.execute(
            """CREATE INDEX IF NOT EXISTS reply_work_duplicate_of
               ON reply_work_items(duplicate_of_work_id)"""
        )

    def _fold_stable_message_duplicates(self) -> None:
        """Link replayed stable messages without deleting historical rows or work."""

        groups = self.connection.execute(
            """SELECT listener_id,group_id,message_id
               FROM reply_inbox
               WHERE message_id NOT IN ('','0')
               GROUP BY listener_id,group_id,message_id
               HAVING count(*)>1"""
        ).fetchall()
        duplicate_groups = []
        parent: dict[str, str] = {}

        def find(work_id: str) -> str:
            parent.setdefault(work_id, work_id)
            while parent[work_id] != work_id:
                parent[work_id] = parent[parent[work_id]]
                work_id = parent[work_id]
            return work_id

        def union(left: str, right: str) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        for group in groups:
            rows = self.connection.execute(
                """SELECT * FROM reply_inbox
                   WHERE listener_id=? AND group_id=? AND message_id=?
                   ORDER BY CASE WHEN server_id NOT IN ('','0') THEN 1 ELSE 0 END DESC,
                            sequence DESC,received_at DESC,id DESC""",
                tuple(group),
            ).fetchall()
            work_ids = [
                str(row["assigned_work_id"])
                for row in rows
                if row["assigned_work_id"]
                and not str(row["assigned_work_id"]).startswith("claim:")
            ]
            work_ids = list(dict.fromkeys(work_ids))
            for work_id in work_ids:
                find(work_id)
            for work_id in work_ids[1:]:
                union(work_ids[0], work_id)
            duplicate_groups.append((tuple(group), rows, work_ids))

        work_rows = []
        all_work_ids = list(parent)
        for offset in range(0, len(all_work_ids), 500):
            chunk = all_work_ids[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            work_rows.extend(
                self.connection.execute(
                    f"SELECT * FROM reply_work_items WHERE id IN ({placeholders})",
                    tuple(chunk),
                ).fetchall()
            )
        rows_by_component: dict[str, list] = {}
        for work in work_rows:
            rows_by_component.setdefault(find(str(work["id"])), []).append(work)

        representative_by_work: dict[str, str] = {}
        for component_rows in rows_by_component.values():
            representative_work = max(
                component_rows,
                key=lambda row: (
                    int(str(row["status"]) == "sent"),
                    float(row["updated_at"] or 0),
                    float(row["created_at"] or 0),
                    str(row["id"]),
                ),
            )
            representative_id = str(representative_work["id"])
            for work in component_rows:
                work_id = str(work["id"])
                representative_by_work[work_id] = representative_id
                if len(component_rows) > 1:
                    self.connection.execute(
                        "UPDATE reply_work_items SET duplicate_of_work_id=? WHERE id=?",
                        (None if work_id == representative_id else representative_id, work_id),
                    )

        for group, rows, _work_ids in duplicate_groups:
            representative_work_id = next(
                (
                    work_id
                    for work_id in _work_ids
                    if representative_by_work.get(work_id) == work_id
                ),
                None,
            )
            canonical = next(
                (
                    row
                    for row in rows
                    if representative_work_id
                    and str(row["assigned_work_id"] or "") == representative_work_id
                ),
                rows[0],
            )
            canonical_id = int(canonical["id"])
            richest = rows[0]
            if int(richest["id"]) != canonical_id:
                self.connection.execute(
                    """UPDATE reply_inbox
                       SET payload_json=?
                       WHERE id=?""",
                    (richest["payload_json"], canonical_id),
                )
            self.connection.execute(
                """UPDATE reply_inbox SET duplicate_of_inbox_id=?
                   WHERE listener_id=? AND group_id=? AND message_id=?""",
                (canonical_id, *tuple(group)),
            )
            self.connection.execute(
                "UPDATE reply_inbox SET duplicate_of_inbox_id=NULL WHERE id=?",
                (canonical_id,),
            )

    @contextmanager
    def transaction(self):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def revision(self, connection: sqlite3.Connection | None = None) -> int:
        if connection is None:
            with self.lock:
                return self.revision(self.connection)
        row = connection.execute(
            "SELECT value FROM runtime_meta WHERE key = 'revision'"
        ).fetchone()
        return int(row[0])

    def bump_revision(self, connection: sqlite3.Connection) -> int:
        revision = self.revision(connection) + 1
        connection.execute(
            "UPDATE runtime_meta SET value = ? WHERE key = 'revision'",
            (str(revision),),
        )
        return revision

    def close(self) -> None:
        with self.lock:
            self.connection.close()


def encode_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode_json(value: str | bytes | None, default=None):
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
