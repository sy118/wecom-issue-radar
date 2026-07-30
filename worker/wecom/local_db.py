from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from .content_decoder import decode_content
from .crypto import decrypt_wxsqlite3_aes128_page
from .paths import resolve_config_path, resolve_config_relative_path


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent.parent
PAGE_SZ = 4096
WAL_HEADER_SZ = 32
WAL_FRAME_HDR_SZ = 24
VALID_WAL_PAGE_SIZES = (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536)
MESSAGE_TABLES = ("message_table", "message_small_table", "kf_message_tableV1")
MEDIA_CONTENT_TYPES = {4, 14, 15, 23, 123}
IMAGE_CONTENT_TYPES = {4, 123}
TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")
MESSAGE_SNAPSHOT_PREFIX = "wecom-reply-message-cache-"

_SNAPSHOT_REGISTRY_LOCK = threading.RLock()
_ACTIVE_SNAPSHOT_PATHS: set[str] = set()
_CLEANED_SNAPSHOT_DIRECTORIES: set[str] = set()

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".3gp"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico"}
VOICE_EXTS = {".mp3", ".wav", ".aac", ".ogg", ".wma", ".silk"}
FILE_NAME_RE = re.compile(
    r"([^\\/:*?\"<>|\r\n\t ]{1,180}\."
    r"(?:png|jpe?g|webp|gif|bmp|svg|ico|mp4|mov|avi|mkv|wmv|flv|webm|3gp|"
    r"mp3|wav|aac|ogg|wma|silk|pdf|docx?|xlsx?|pptx?|txt|csv|zip|rar))",
    re.IGNORECASE,
)


def load_config(config_path: str | None = None) -> dict:
    path = resolve_config_path(config_path)
    with path.open("r", encoding="utf-8") as file:
        config = json.load(file)
    config["_config_path"] = str(path)
    config["_skill_dir"] = str(SKILL_DIR)
    config["_config_dir"] = str(path.parent)
    config["wxwork_db_dir"] = str(resolve_config_path_value(config["wxwork_db_dir"], path))
    config["wxwork_keys_file"] = str(resolve_config_path_value(config.get("wxwork_keys_file") or "wxwork_keys.json", path))
    return config


def resolve_config_path_value(value: str | os.PathLike, config_path: str | os.PathLike) -> Path:
    return resolve_config_relative_path(value, config_path)


def load_keys(config: dict) -> dict:
    keys_path = Path(config["wxwork_keys_file"])
    if not keys_path.exists():
        raise FileNotFoundError(
            f"Missing wxwork key file: {keys_path}. "
            "请在设置页面点击“提取密钥”后重试。"
        )
    with keys_path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    return {key: value for key, value in data.items() if not key.startswith("_")}


def key_for_db(config: dict, db_name: str) -> bytes:
    keys = config.setdefault("_keys_cache", load_keys(config))
    entry = keys.get(db_name) or keys.get(Path(db_name).name)
    if not entry and db_name != "session.db":
        entry = keys.get("session.db")
    if not entry or not entry.get("enc_key"):
        raise KeyError(f"Missing enc_key for {db_name} in {config['wxwork_keys_file']}")
    return bytes.fromhex(entry["enc_key"])


def db_path(config: dict, db_name: str) -> Path:
    return Path(config["wxwork_db_dir"]) / db_name


def safe_unlink(path: str | os.PathLike) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def decrypt_db_to_temp(
    enc_path: str | os.PathLike,
    raw_key: bytes,
    *,
    temp_prefix: str = "wecom-chat-cache-",
    temp_dir: str | os.PathLike | None = None,
) -> str:
    enc_path = str(enc_path)
    size = os.path.getsize(enc_path)
    total_pages = (size + PAGE_SZ - 1) // PAGE_SZ
    fd, tmp_path = tempfile.mkstemp(
        prefix=temp_prefix,
        suffix=".db",
        dir=str(temp_dir) if temp_dir is not None else None,
    )
    os.close(fd)
    try:
        with open(enc_path, "rb") as fin, open(tmp_path, "wb") as fout:
            for page_no in range(1, total_pages + 1):
                page = fin.read(PAGE_SZ)
                if not page:
                    break
                if len(page) < PAGE_SZ:
                    page += b"\x00" * (PAGE_SZ - len(page))
                fout.write(decrypt_wxsqlite3_aes128_page(raw_key, page, page_no))
    except Exception:
        safe_unlink(tmp_path)
        raise
    return tmp_path


def patch_wal_frames(decrypted_path: str, wal_path: str | os.PathLike, raw_key: bytes) -> tuple[int, int]:
    patched, skipped, _ = patch_wal_frames_incremental(
        decrypted_path,
        wal_path,
        raw_key,
        start_offset=WAL_HEADER_SZ,
    )
    return patched, skipped


def patch_wal_frames_incremental(
    decrypted_path: str,
    wal_path: str | os.PathLike,
    raw_key: bytes,
    *,
    start_offset: int = WAL_HEADER_SZ,
    max_size: int | None = None,
    expected_generation: tuple[str, int, int, bytes, int] | None = None,
    expected_commit_checksum: tuple[int, int] | None = None,
    expected_start_checksum: tuple[int, int] | None = None,
) -> tuple[int, int, int]:
    """Apply valid, committed encrypted WAL frames from a known boundary.

    The WAL is fully checksum/salt validated before any plaintext page is written.
    Frames after the last commit marker are never published. The returned offset is
    therefore a committed resume point, not merely the end of a complete frame.
    """
    scan = _scan_valid_wal(
        Path(wal_path),
        max_size=max_size,
        scan_from=max(int(start_offset), WAL_HEADER_SZ),
        start_checksum=expected_start_checksum,
        expected_generation=expected_generation,
        collect_pages=True,
    )
    if scan is None:
        return 0, 0, WAL_HEADER_SZ
    descriptor = scan.descriptor
    if expected_generation is not None and descriptor.generation != expected_generation:
        raise sqlite3.DatabaseError("WAL generation changed before patch")
    if (
        expected_commit_checksum is not None
        and descriptor.commit_checksum != expected_commit_checksum
    ):
        raise sqlite3.DatabaseError("WAL commit checksum changed before patch")
    if max_size is not None and descriptor.committed_end != int(max_size):
        raise sqlite3.DatabaseError("WAL commit boundary changed before patch")
    if start_offset > descriptor.committed_end:
        raise sqlite3.DatabaseError("WAL resume offset is past the committed boundary")
    if expected_start_checksum is not None:
        checksum_at_start = dict(scan.frame_checksums).get(int(start_offset))
        if checksum_at_start != expected_start_checksum:
            raise sqlite3.DatabaseError("WAL committed prefix changed before patch")

    decrypted_pages: list[tuple[int, bytes]] = []
    for page_no, encrypted_page in scan.pages:
        try:
            decrypted = decrypt_wxsqlite3_aes128_page(
                raw_key, encrypted_page, page_no
            )
        except Exception as exc:
            raise sqlite3.DatabaseError(
                f"failed to decrypt committed WAL page {page_no}"
            ) from exc
        if len(decrypted) != descriptor.page_size:
            raise sqlite3.DatabaseError("decrypted WAL page has an invalid size")
        decrypted_pages.append((page_no, decrypted))

    if not _wal_generation_still_matches(Path(wal_path), descriptor):
        raise sqlite3.DatabaseError("WAL changed while committed pages were prepared")
    with open(decrypted_path, "r+b") as fdb:
        for page_no, decrypted in decrypted_pages:
            fdb.seek(0, 2)
            current_size = fdb.tell()
            target_size = page_no * descriptor.page_size
            if current_size < target_size:
                fdb.write(b"\x00" * (target_size - current_size))
            fdb.seek((page_no - 1) * descriptor.page_size)
            fdb.write(decrypted)
        if scan.committed_db_size is not None:
            fdb.truncate(scan.committed_db_size * descriptor.page_size)
        fdb.flush()
        os.fsync(fdb.fileno())
    return len(decrypted_pages), 0, descriptor.committed_end


@contextmanager
def decrypted_connection(config: dict, db_name: str, include_wal: bool = True):
    path = db_path(config, db_name)
    raw_key = key_for_db(config, db_name)
    tmp = decrypt_db_to_temp(path, raw_key)
    try:
        if include_wal:
            try:
                patch_wal_frames(tmp, str(path) + "-wal", raw_key)
            except sqlite3.DatabaseError:
                pass
        conn = sqlite3.connect(tmp)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    finally:
        safe_unlink(tmp)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def list_conversations(config: dict, limit: int = 50, search: str = "", include_direct: bool = False) -> list[dict]:
    with decrypted_connection(config, "session.db") as conn:
        where = []
        params: list[object] = []
        if not include_direct:
            where.append("id LIKE 'R:%'")
        keyword = (search or "").strip()
        if keyword:
            where.append("(id LIKE ? OR name LIKE ? OR roomname_remark LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like, like])
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        rows = conn.execute(
            "SELECT id, name, roomname_remark, last_message_time, last_message_id "
            f"FROM conversation_table {where_sql} "
            "ORDER BY last_message_time DESC LIMIT ?",
            (*params, max(int(limit or 50), 1)),
        ).fetchall()
        return [
            {
                "conversation_id": row["id"],
                "display_name": row["roomname_remark"] or row["name"] or row["id"],
                "last_message_time": int(row["last_message_time"] or 0),
                "last_message_id": int(row["last_message_id"] or 0),
            }
            for row in rows
        ]


def get_conversation_state(config: dict, conversation_id: str) -> dict | None:
    with decrypted_connection(config, "session.db") as conn:
        row = conn.execute(
            "SELECT id, name, roomname_remark, last_message_time, last_message_id "
            "FROM conversation_table WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "conversation_id": row["id"],
            "display_name": row["roomname_remark"] or row["name"] or conversation_id,
            "last_message_time": int(row["last_message_time"] or 0),
            "last_message_id": int(row["last_message_id"] or 0),
        }


def load_user_map(config: dict) -> dict[int, str]:
    path = db_path(config, "user.db")
    if not path.exists():
        return {}
    users: dict[int, str] = {}
    with decrypted_connection(config, "user.db") as conn:
        if table_exists(conn, "user_table"):
            for row in conn.execute(
                "SELECT id, name, real_name, account, external_corp_name, external_job FROM user_table"
            ):
                name = row["real_name"] or row["name"] or row["account"] or ""
                if row["external_corp_name"] and row["external_corp_name"] not in name:
                    name = f"{name}({row['external_corp_name']})" if name else row["external_corp_name"]
                if name:
                    users[int(row["id"])] = name
        if table_exists(conn, "external_user_relation_v3"):
            for row in conn.execute(
                "SELECT user_id, remarks, real_remarks, corp_remark FROM external_user_relation_v3"
            ):
                name = row["real_remarks"] or row["remarks"] or row["corp_remark"] or ""
                if name and int(row["user_id"]) not in users:
                    users[int(row["user_id"])] = name
    return users


def load_user_identities(config: dict) -> dict[int, dict[str, str]]:
    """Load sender display/mention identities without treating local numeric ids as userids."""

    path = db_path(config, "user.db")
    if not path.exists():
        return {}
    identities: dict[int, dict[str, str]] = {}
    with decrypted_connection(config, "user.db") as conn:
        if not table_exists(conn, "user_table"):
            return identities
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(user_table)").fetchall()
        }
        selected = [
            column
            for column in ("id", "name", "real_name", "account", "mobile", "phone", "telephone")
            if column in columns
        ]
        if "id" not in selected:
            return identities
        for row in conn.execute(f"SELECT {','.join(selected)} FROM user_table"):
            sender_id = int(row["id"] or 0)
            if not sender_id:
                continue
            account = str(row["account"] or "").strip() if "account" in selected else ""
            mobile = ""
            for column in ("mobile", "phone", "telephone"):
                if column in selected and row[column]:
                    mobile = str(row[column]).strip()
                    break
            display_name = ""
            for column in ("real_name", "name"):
                if column in selected and row[column]:
                    display_name = str(row[column]).strip()
                    if display_name:
                        break
            identities[sender_id] = {
                "display_name": display_name,
                "account": account if account and not account.isdigit() else "",
                "mobile": mobile,
            }
    return identities


def load_member_names(config: dict) -> dict[str, dict[int, str]]:
    members: dict[str, dict[int, str]] = defaultdict(dict)
    with decrypted_connection(config, "session.db") as conn:
        if table_exists(conn, "conversation_user_table"):
            for row in conn.execute("SELECT conversation_id, user_id, nick_name FROM conversation_user_table"):
                if row["nick_name"]:
                    members[row["conversation_id"]][int(row["user_id"])] = row["nick_name"]
        if table_exists(conn, "conversation_member_nickname_table"):
            room_map = {}
            if table_exists(conn, "conversation_table"):
                for row in conn.execute("SELECT con_numeric_id, id FROM conversation_table"):
                    room_map[int(row["con_numeric_id"])] = row["id"]
            for row in conn.execute("SELECT room_id, userid, nickname FROM conversation_member_nickname_table"):
                cid = room_map.get(int(row["room_id"]))
                if cid and row["nickname"]:
                    members[cid][int(row["userid"])] = row["nickname"]
    return dict(members)


@dataclass(frozen=True)
class _MessageBaseSignature:
    path: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    key_sha256: str


@dataclass(frozen=True)
class _WalDescriptor:
    path: str
    device: int
    inode: int
    header: bytes
    page_size: int
    committed_end: int
    commit_checksum: tuple[int, int]
    raw_size: int
    mtime_ns: int

    @property
    def generation(self) -> tuple[str, int, int, bytes, int]:
        return (self.path, self.device, self.inode, self.header, self.page_size)


@dataclass(frozen=True)
class _WalScan:
    descriptor: _WalDescriptor
    committed_db_size: int | None
    pages: tuple[tuple[int, bytes], ...]
    frame_checksums: tuple[tuple[int, tuple[int, int]], ...]


class MessageDatabaseSnapshot:
    """One thread-safe, committed plaintext view shared by all listeners."""

    def __init__(
        self,
        *,
        decryptor: Callable[[str | os.PathLike, bytes], str] | None = None,
        wal_applier: Callable[..., tuple[int, int, int]] | None = None,
        temp_dir: str | os.PathLike | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._decryptor = decryptor
        self._wal_applier = wal_applier or patch_wal_frames_incremental
        self._temp_dir = Path(temp_dir or tempfile.gettempdir()).resolve()
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        self._closed = False
        self._snapshot_path: Path | None = None
        self._base_signature: _MessageBaseSignature | None = None
        self._wal_generation: tuple[str, int, int, bytes, int] | None = None
        self._wal_applied_end = WAL_HEADER_SZ
        self._wal_commit_checksum = (0, 0)
        self._wal_raw_size = 0
        self._wal_mtime_ns = 0
        _cleanup_stale_message_snapshots(self._temp_dir)

    def read_messages(
        self,
        config: dict,
        conversation_id: str,
        start_ts: int,
        end_ts: int,
        limit: int = 0,
    ) -> list[dict]:
        encrypted_path = db_path(config, "message.db").resolve()
        raw_key = key_for_db(config, "message.db")
        with self._lock:
            if self._closed:
                raise RuntimeError("message database snapshot is closed")
            try:
                self._refresh(encrypted_path, raw_key)
                assert self._snapshot_path is not None
                return _query_decrypted_message_path(
                    self._snapshot_path,
                    conversation_id,
                    start_ts,
                    end_ts,
                    limit,
                )
            except Exception:
                # Never fall back to an older base view: read_force is part of the
                # automatic-send safety gate, so missing WAL replies must fail closed.
                self._discard_snapshot()
                raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._discard_snapshot()

    def _refresh(self, encrypted_path: Path, raw_key: bytes) -> None:
        for _attempt in range(3):
            base_before = _message_base_signature(encrypted_path, raw_key)
            wal_path = Path(str(encrypted_path) + "-wal")
            wal_before = self._describe_current_wal(wal_path)
            if self._requires_full_rebuild(base_before, wal_before):
                self._rebuild_snapshot(encrypted_path, raw_key, base_before, wal_before)
            elif wal_before is not None and wal_before.committed_end > self._wal_applied_end:
                self._advance_wal(raw_key, wal_before)

            base_after = _message_base_signature(encrypted_path, raw_key)
            wal_after = self._describe_current_wal(wal_path)
            if base_after != base_before:
                self._discard_snapshot()
                continue
            if not self._state_matches_wal(wal_after):
                self._discard_snapshot()
                continue
            # A commit appended after our first descriptor must be visible to this
            # read, especially when it is a forced automatic-send preflight.
            if wal_after is not None and wal_after.committed_end > self._wal_applied_end:
                continue
            if wal_after is not None and wal_after.generation == self._wal_generation:
                self._wal_raw_size = wal_after.raw_size
                self._wal_mtime_ns = wal_after.mtime_ns
            return
        self._discard_snapshot()
        raise sqlite3.DatabaseError("message database changed throughout snapshot refresh")

    def _requires_full_rebuild(
        self,
        base: _MessageBaseSignature,
        wal: _WalDescriptor | None,
    ) -> bool:
        if self._snapshot_path is None or self._base_signature != base:
            return True
        if wal is None:
            return self._wal_generation is not None or self._wal_applied_end != WAL_HEADER_SZ
        if self._wal_generation is None and self._wal_applied_end == WAL_HEADER_SZ:
            return False
        if wal.generation != self._wal_generation:
            return True
        if wal.committed_end < self._wal_applied_end:
            return True
        if (
            wal.committed_end == self._wal_applied_end
            and wal.commit_checksum != self._wal_commit_checksum
        ):
            return True
        return False

    def _describe_current_wal(self, path: Path) -> _WalDescriptor | None:
        if self._wal_generation is not None:
            try:
                stat = path.stat()
                generation_path, device, inode, header, page_size = self._wal_generation
                if (
                    str(path.resolve()) == generation_path
                    and int(stat.st_dev) == device
                    and int(stat.st_ino) == inode
                    and int(stat.st_size) == self._wal_raw_size
                    and int(stat.st_mtime_ns) == self._wal_mtime_ns
                ):
                    return _WalDescriptor(
                        path=generation_path,
                        device=device,
                        inode=inode,
                        header=header,
                        page_size=page_size,
                        committed_end=self._wal_applied_end,
                        commit_checksum=self._wal_commit_checksum,
                        raw_size=self._wal_raw_size,
                        mtime_ns=self._wal_mtime_ns,
                    )
                # Within one WAL generation SQLite only appends frames. Resume from
                # the last validated commit instead of rescanning and retaining the
                # entire WAL on every message. Same-length rewrites and truncations
                # deliberately take the full-scan path below so corruption/reset is
                # detected rather than mistaken for an append.
                if (
                    str(path.resolve()) == generation_path
                    and int(stat.st_dev) == device
                    and int(stat.st_ino) == inode
                    and int(stat.st_size) > self._wal_raw_size
                ):
                    with path.open("rb") as stream:
                        current_header = stream.read(WAL_HEADER_SZ)
                    if current_header == header:
                        return _describe_wal(
                            path,
                            start_offset=self._wal_applied_end,
                            start_checksum=self._wal_commit_checksum,
                            expected_generation=self._wal_generation,
                        )
            except FileNotFoundError:
                return None
        return _describe_wal(path)

    def _decrypt_base(self, encrypted_path: Path, raw_key: bytes) -> Path:
        if self._decryptor is None:
            candidate = Path(
                decrypt_db_to_temp(
                    encrypted_path,
                    raw_key,
                    temp_prefix=_snapshot_temp_prefix(),
                    temp_dir=self._temp_dir,
                )
            )
        else:
            candidate = Path(self._decryptor(encrypted_path, raw_key))
        candidate = candidate.resolve()
        if candidate == encrypted_path:
            raise ValueError("snapshot decryptor must return a separate plaintext file")
        _register_snapshot_path(candidate)
        _restrict_snapshot_permissions(candidate)
        return candidate

    def _rebuild_snapshot(
        self,
        encrypted_path: Path,
        raw_key: bytes,
        expected_base: _MessageBaseSignature,
        expected_wal: _WalDescriptor | None,
    ) -> None:
        candidate = self._decrypt_base(encrypted_path, raw_key)
        try:
            if _message_base_signature(encrypted_path, raw_key) != expected_base:
                raise sqlite3.DatabaseError("base changed during snapshot rebuild")
            if expected_wal is not None and expected_wal.committed_end > WAL_HEADER_SZ:
                _, _, applied_end = self._wal_applier(
                    str(candidate),
                    expected_wal.path,
                    raw_key,
                    start_offset=WAL_HEADER_SZ,
                    max_size=expected_wal.committed_end,
                    expected_generation=expected_wal.generation,
                    expected_commit_checksum=expected_wal.commit_checksum,
                )
                if int(applied_end) != expected_wal.committed_end:
                    raise sqlite3.DatabaseError("WAL rebuild ended off commit boundary")
            _validate_decrypted_snapshot(candidate)
        except Exception:
            _remove_snapshot_path(candidate)
            raise

        old_snapshot = self._snapshot_path
        self._snapshot_path = candidate
        self._base_signature = expected_base
        self._set_wal_metadata(expected_wal)
        _remove_snapshot_path(old_snapshot)

    def _advance_wal(self, raw_key: bytes, descriptor: _WalDescriptor) -> None:
        assert self._snapshot_path is not None
        expected_start_checksum = self._wal_commit_checksum
        if self._wal_applied_end == WAL_HEADER_SZ:
            # The first committed transaction starts from the validated WAL header
            # checksum, not the snapshot's zero-value metadata.
            expected_start_checksum = struct.unpack_from(">II", descriptor.header, 24)
        try:
            _, _, applied_end = self._wal_applier(
                str(self._snapshot_path),
                descriptor.path,
                raw_key,
                start_offset=self._wal_applied_end,
                max_size=descriptor.committed_end,
                expected_generation=descriptor.generation,
                expected_commit_checksum=descriptor.commit_checksum,
                expected_start_checksum=expected_start_checksum,
            )
            if int(applied_end) != descriptor.committed_end:
                raise sqlite3.DatabaseError("WAL refresh ended off commit boundary")
            _validate_decrypted_snapshot(self._snapshot_path)
        except Exception:
            # The caller owns fail-closed invalidation. No query can race this
            # in-place update because read_messages holds the snapshot RLock.
            raise
        self._set_wal_metadata(descriptor)

    def _state_matches_wal(self, descriptor: _WalDescriptor | None) -> bool:
        if descriptor is None or descriptor.committed_end <= WAL_HEADER_SZ:
            return self._wal_generation is None and self._wal_applied_end == WAL_HEADER_SZ
        if self._wal_generation != descriptor.generation:
            return False
        if self._wal_applied_end > descriptor.committed_end:
            return False
        if (
            self._wal_applied_end == descriptor.committed_end
            and self._wal_commit_checksum != descriptor.commit_checksum
        ):
            return False
        return True

    def _set_wal_metadata(self, descriptor: _WalDescriptor | None) -> None:
        if descriptor is None or descriptor.committed_end <= WAL_HEADER_SZ:
            self._reset_wal_metadata()
            return
        self._wal_generation = descriptor.generation
        self._wal_applied_end = descriptor.committed_end
        self._wal_commit_checksum = descriptor.commit_checksum
        self._wal_raw_size = descriptor.raw_size
        self._wal_mtime_ns = descriptor.mtime_ns

    def _discard_snapshot(self) -> None:
        old_snapshot = self._snapshot_path
        self._snapshot_path = None
        self._base_signature = None
        self._reset_wal_metadata()
        _remove_snapshot_path(old_snapshot)

    def _reset_wal_metadata(self) -> None:
        self._wal_generation = None
        self._wal_applied_end = WAL_HEADER_SZ
        self._wal_commit_checksum = (0, 0)
        self._wal_raw_size = 0
        self._wal_mtime_ns = 0


def read_messages(
    config: dict,
    conversation_id: str,
    start_ts: int,
    end_ts: int,
    limit: int = 0,
    *,
    snapshot: MessageDatabaseSnapshot | None = None,
) -> list[dict]:
    if snapshot is not None:
        return snapshot.read_messages(config, conversation_id, start_ts, end_ts, limit)
    path = db_path(config, "message.db")
    raw_key = key_for_db(config, "message.db")
    tmp = decrypt_db_to_temp(path, raw_key)
    try:
        main_results = _query_decrypted_message_path(
            Path(tmp), conversation_id, start_ts, end_ts, limit
        )

        wal_results = []
        if os.path.exists(str(path) + "-wal"):
            try:
                patch_wal_frames(tmp, str(path) + "-wal", raw_key)
                wal_results = _query_decrypted_message_path(
                    Path(tmp), conversation_id, start_ts, end_ts, limit
                )
            except sqlite3.DatabaseError:
                wal_results = []
        return _merge_message_results(main_results, wal_results, limit)
    finally:
        safe_unlink(tmp)


def query_message_tables(
    conn: sqlite3.Connection,
    conversation_id: str,
    start_ts: int,
    end_ts: int,
    limit: int = 0,
) -> list[dict]:
    results = []
    for table in MESSAGE_TABLES:
        if not table_exists(conn, table):
            continue
        sql = (
            f'SELECT "{table}" AS source_table, message_id, server_id, sequence, sender_id, '
            "conversation_id, content_type, send_time, flag, content, extra_content, local_extra_content "
            f'FROM "{table}" '
            "WHERE conversation_id = ? AND send_time >= ? AND send_time <= ? "
            "ORDER BY send_time DESC, sequence DESC, message_id DESC"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        for row in conn.execute(sql, (conversation_id, int(start_ts), int(end_ts))).fetchall():
            results.append(
                {
                    "source_table": row["source_table"],
                    "message_id": int(row["message_id"] or 0),
                    "server_id": int(row["server_id"] or 0),
                    "sequence": int(row["sequence"] or 0),
                    "sender_id": int(row["sender_id"] or 0),
                    "conversation_id": row["conversation_id"],
                    "content_type": int(row["content_type"] or 0),
                    "send_time": int(row["send_time"] or 0),
                    "flag": int(row["flag"] or 0),
                    "content_raw": row["content"],
                    "extra_content_raw": row["extra_content"],
                    "local_extra_content_raw": row["local_extra_content"],
                }
            )
    return results


def message_sort_key(msg: dict) -> tuple[int, int, int, int]:
    return (
        int(msg.get("send_time") or 0),
        int(msg.get("sequence") or 0),
        int(msg.get("message_id") or 0),
        int(msg.get("server_id") or 0),
    )


def _query_decrypted_message_path(
    path: Path,
    conversation_id: str,
    start_ts: int,
    end_ts: int,
    limit: int,
) -> list[dict]:
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return query_message_tables(conn, conversation_id, start_ts, end_ts, limit)
    finally:
        conn.close()


def _validate_decrypted_snapshot(path: Path) -> None:
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA schema_version").fetchone()
    finally:
        conn.close()


def _merge_message_results(
    main_results: list[dict], wal_results: list[dict], limit: int
) -> list[dict]:
    merged = {}
    for msg in [*main_results, *wal_results]:
        key = (msg["conversation_id"], msg["message_id"], msg["server_id"])
        if key not in merged or message_sort_key(msg) > message_sort_key(merged[key]):
            merged[key] = msg
    messages = list(merged.values())
    messages.sort(key=message_sort_key)
    if limit:
        messages = messages[-limit:]
    return messages


def _message_base_signature(path: Path, raw_key: bytes) -> _MessageBaseSignature:
    stat = path.stat()
    return _MessageBaseSignature(
        path=str(path.resolve()),
        device=int(stat.st_dev),
        inode=int(stat.st_ino),
        size=int(stat.st_size),
        mtime_ns=int(stat.st_mtime_ns),
        ctime_ns=int(stat.st_ctime_ns),
        key_sha256=hashlib.sha256(raw_key).hexdigest(),
    )


def _describe_wal(
    path: Path,
    *,
    start_offset: int = WAL_HEADER_SZ,
    start_checksum: tuple[int, int] | None = None,
    expected_generation: tuple[str, int, int, bytes, int] | None = None,
) -> _WalDescriptor | None:
    scan = _scan_valid_wal(
        path,
        scan_from=start_offset,
        start_checksum=start_checksum,
        expected_generation=expected_generation,
        collect_pages=False,
    )
    return scan.descriptor if scan is not None else None


def _scan_valid_wal(
    path: Path,
    *,
    max_size: int | None = None,
    scan_from: int = WAL_HEADER_SZ,
    start_checksum: tuple[int, int] | None = None,
    expected_generation: tuple[str, int, int, bytes, int] | None = None,
    collect_pages: bool = True,
) -> _WalScan | None:
    try:
        before = path.stat()
        raw_size = int(before.st_size)
        if raw_size < WAL_HEADER_SZ:
            return None
        scan_size = raw_size if max_size is None else min(raw_size, int(max_size))
        with path.open("rb") as stream:
            header = stream.read(WAL_HEADER_SZ)
            if len(header) != WAL_HEADER_SZ:
                raise sqlite3.DatabaseError("WAL header is truncated")
            magic = struct.unpack_from(">I", header, 0)[0]
            if magic not in {0x377F0682, 0x377F0683}:
                raise sqlite3.DatabaseError("WAL header magic is invalid")
            page_size_raw = struct.unpack_from(">I", header, 8)[0]
            page_size = 65536 if page_size_raw == 1 else page_size_raw
            if page_size not in VALID_WAL_PAGE_SIZES:
                raise sqlite3.DatabaseError("WAL page size is invalid")
            checksum_order = ">" if magic & 1 else "<"
            header_checksum = _wal_checksum(header[:24], (0, 0), checksum_order)
            if header_checksum != struct.unpack_from(">II", header, 24):
                raise sqlite3.DatabaseError("WAL header checksum is invalid")
            salt = struct.unpack_from(">II", header, 16)
            frame_size = WAL_FRAME_HDR_SZ + page_size
            resume_at = max(int(scan_from), WAL_HEADER_SZ)
            if (resume_at - WAL_HEADER_SZ) % frame_size:
                raise sqlite3.DatabaseError("WAL resume offset is not frame aligned")
            if resume_at > scan_size:
                raise sqlite3.DatabaseError("WAL was truncated before the resume boundary")

            generation = (
                str(path.resolve()),
                int(before.st_dev),
                int(before.st_ino),
                header,
                page_size,
            )
            if expected_generation is not None and generation != expected_generation:
                raise sqlite3.DatabaseError("WAL generation changed before scan")

            if resume_at == WAL_HEADER_SZ:
                checksum = header_checksum
                if start_checksum is not None and start_checksum != header_checksum:
                    raise sqlite3.DatabaseError("WAL header boundary checksum changed")
            else:
                if start_checksum is None:
                    raise sqlite3.DatabaseError("WAL resume checksum is required")
                previous_header_at = resume_at - frame_size
                stream.seek(previous_header_at)
                previous_header = stream.read(WAL_FRAME_HDR_SZ)
                if len(previous_header) != WAL_FRAME_HDR_SZ:
                    raise sqlite3.DatabaseError("WAL resume frame header is truncated")
                if struct.unpack_from(">II", previous_header, 8) != salt:
                    raise sqlite3.DatabaseError("WAL resume frame salt does not match header")
                if struct.unpack_from(">II", previous_header, 16) != start_checksum:
                    raise sqlite3.DatabaseError("WAL committed prefix checksum changed")
                checksum = start_checksum

            committed_end = resume_at
            committed_checksum = checksum
            committed_db_size: int | None = None
            frames: list[tuple[int, int, bytes]] = []
            frame_checksums: list[tuple[int, tuple[int, int]]] = [
                (resume_at, checksum)
            ]
            pos = resume_at
            while pos + frame_size <= scan_size:
                stream.seek(pos)
                frame_header = stream.read(WAL_FRAME_HDR_SZ)
                encrypted_page = stream.read(page_size)
                if len(frame_header) != WAL_FRAME_HDR_SZ or len(encrypted_page) != page_size:
                    raise sqlite3.DatabaseError("WAL frame is truncated")
                page_no, db_size = struct.unpack_from(">II", frame_header, 0)
                if page_no == 0 or page_no > 1_000_000:
                    raise sqlite3.DatabaseError("WAL frame page number is invalid")
                if struct.unpack_from(">II", frame_header, 8) != salt:
                    raise sqlite3.DatabaseError("WAL frame salt does not match header")
                checksum = _wal_checksum(
                    frame_header[:8] + encrypted_page,
                    checksum,
                    checksum_order,
                )
                if checksum != struct.unpack_from(">II", frame_header, 16):
                    raise sqlite3.DatabaseError("WAL frame checksum is invalid")
                frame_end = pos + frame_size
                if collect_pages:
                    frames.append((frame_end, page_no, encrypted_page))
                if collect_pages:
                    frame_checksums.append((frame_end, checksum))
                if db_size:
                    if db_size > 1_000_000:
                        raise sqlite3.DatabaseError("WAL commit database size is invalid")
                    committed_end = frame_end
                    committed_checksum = checksum
                    committed_db_size = db_size
                pos = frame_end

        after = path.stat()
        if (
            int(after.st_dev) != int(before.st_dev)
            or int(after.st_ino) != int(before.st_ino)
            or int(after.st_size) < scan_size
        ):
            raise sqlite3.DatabaseError("WAL identity changed during scan")
        with path.open("rb") as stream:
            if stream.read(WAL_HEADER_SZ) != header:
                raise sqlite3.DatabaseError("WAL generation changed during scan")

        descriptor = _WalDescriptor(
            path=str(path.resolve()),
            device=int(before.st_dev),
            inode=int(before.st_ino),
            header=header,
            page_size=page_size,
            committed_end=committed_end,
            commit_checksum=committed_checksum,
            raw_size=raw_size,
            mtime_ns=int(before.st_mtime_ns),
        )
        pages = tuple(
            (page_no, encrypted_page)
            for frame_end, page_no, encrypted_page in frames
            if resume_at < frame_end <= committed_end
        )
        return _WalScan(
            descriptor=descriptor,
            committed_db_size=committed_db_size,
            pages=pages,
            frame_checksums=tuple(frame_checksums),
        )
    except FileNotFoundError:
        return None


def _wal_checksum(
    data: bytes,
    seed: tuple[int, int],
    byte_order: str,
) -> tuple[int, int]:
    if len(data) % 8:
        raise sqlite3.DatabaseError("WAL checksum input is not 64-bit aligned")
    words = struct.unpack(f"{byte_order}{len(data) // 4}I", data)
    first, second = seed
    for index in range(0, len(words), 2):
        first = (first + words[index] + second) & 0xFFFFFFFF
        second = (second + words[index + 1] + first) & 0xFFFFFFFF
    return first, second


def _wal_generation_still_matches(path: Path, descriptor: _WalDescriptor) -> bool:
    try:
        stat = path.stat()
        if (
            int(stat.st_dev) != descriptor.device
            or int(stat.st_ino) != descriptor.inode
            or int(stat.st_size) < descriptor.committed_end
        ):
            return False
        with path.open("rb") as stream:
            return stream.read(WAL_HEADER_SZ) == descriptor.header
    except OSError:
        return False


def _snapshot_temp_prefix() -> str:
    return f"{MESSAGE_SNAPSHOT_PREFIX}{os.getpid()}-"


def _new_snapshot_path(temp_dir: Path, *, suffix: str) -> Path:
    fd, raw_path = tempfile.mkstemp(
        prefix=_snapshot_temp_prefix(),
        suffix=suffix,
        dir=str(temp_dir),
    )
    os.close(fd)
    path = Path(raw_path).resolve()
    _register_snapshot_path(path)
    _restrict_snapshot_permissions(path)
    return path


def _register_snapshot_path(path: Path) -> None:
    with _SNAPSHOT_REGISTRY_LOCK:
        _ACTIVE_SNAPSHOT_PATHS.add(str(path.resolve()))


def _remove_snapshot_path(path: Path | None) -> None:
    if path is None:
        return
    resolved = str(path.resolve())
    removed = True
    # immutable=1 prevents SQLite from creating these in normal operation, but
    # clean them defensively so an interrupted/legacy open cannot leave plaintext
    # journal content behind after the main snapshot is closed.
    for candidate in (
        resolved,
        resolved + "-wal",
        resolved + "-shm",
        resolved + "-journal",
    ):
        try:
            os.unlink(candidate)
        except FileNotFoundError:
            continue
        except OSError:
            removed = False
    if removed:
        with _SNAPSHOT_REGISTRY_LOCK:
            _ACTIVE_SNAPSHOT_PATHS.discard(resolved)


def _restrict_snapshot_permissions(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _cleanup_stale_message_snapshots(temp_dir: Path) -> None:
    directory = str(temp_dir.resolve())
    with _SNAPSHOT_REGISTRY_LOCK:
        if directory in _CLEANED_SNAPSHOT_DIRECTORIES:
            return
        _CLEANED_SNAPSHOT_DIRECTORIES.add(directory)
        active = set(_ACTIVE_SNAPSHOT_PATHS)
    now = time.time()
    for path in temp_dir.glob(f"{MESSAGE_SNAPSHOT_PREFIX}*"):
        resolved = str(path.resolve())
        if resolved in active or not path.is_file():
            continue
        match = re.match(
            rf"^{re.escape(MESSAGE_SNAPSHOT_PREFIX)}(\d+)-",
            path.name,
        )
        if match:
            pid = int(match.group(1))
            if pid != os.getpid() and _pid_is_alive(pid):
                continue
        else:
            try:
                if now - path.stat().st_mtime < 24 * 60 * 60:
                    continue
            except OSError:
                continue
        safe_unlink(path)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            process = kernel32.OpenProcess(0x1000, False, pid)
            if process:
                kernel32.CloseHandle(process)
                return True
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: the PID does not exist.
                return False
            # Access denied and other indeterminate failures are treated as alive;
            # stale cleanup must never delete another live runtime's plaintext view.
            return True
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def resolve_sender(sender_id: int, conversation_id: str, user_map: dict, member_names: dict) -> str:
    if not sender_id:
        return "系统"
    if member_names and conversation_id in member_names:
        nick = member_names[conversation_id].get(sender_id)
        if nick:
            return nick
    if user_map and sender_id in user_map:
        return user_map[sender_id]
    return str(sender_id)


def msg_type_name(content_type: int) -> str:
    labels = {
        4: "图片",
        7: "语音",
        14: "文件",
        15: "文件",
        23: "视频",
        29: "链接",
        38: "应用消息",
        40: "通话",
        123: "截图",
        1011: "会议",
    }
    return labels.get(content_type, "")


def extract_filename(raw: bytes) -> str:
    if not raw:
        return ""
    try:
        text = raw.decode("utf-8", errors="ignore")
    except Exception:
        return ""
    for match in re.finditer(r'([^\\/:*?"<>|\x00-\x1f]{1,80}\.\w{2,5})(?:[\x00-\x1f\\]|$)', text):
        candidate = match.group(1)
        if re.fullmatch(r"[0-9a-fA-F]{32,}", candidate):
            continue
        if candidate.count(".") == 1:
            return candidate
    return ""


def format_message(msg: dict, user_map: dict, member_names: dict, tz: ZoneInfo = TZ_SHANGHAI) -> dict:
    content_type = int(msg.get("content_type") or 0)
    sender = resolve_sender(int(msg.get("sender_id") or 0), msg.get("conversation_id", ""), user_map, member_names)
    content = decode_content(msg.get("content_raw"))
    extra = decode_content(msg.get("extra_content_raw"))
    local_extra = decode_content(msg.get("local_extra_content_raw"))
    if content_type in MEDIA_CONTENT_TYPES:
        filename = extract_filename(msg.get("local_extra_content_raw") or b"") or extract_filename(msg.get("content_raw") or b"")
        display_text = format_media_text(content_type, filename, content, extra, local_extra)
    else:
        display_text = content or extra or local_extra or f"[{msg_type_name(content_type)}]"
    send_time = int(msg.get("send_time") or 0)
    dt = datetime.fromtimestamp(send_time, tz=tz) if send_time else None
    return {
        "sender": sender,
        "sender_id": int(msg.get("sender_id") or 0),
        "message_time": dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "",
        "date": dt.strftime("%Y-%m-%d") if dt else "",
        "time": dt.strftime("%H:%M") if dt else "",
        "send_time": send_time,
        "type": msg_type_name(content_type),
        "content_type": content_type,
        "content": display_text,
        "conversation_id": msg.get("conversation_id", ""),
        "message_id": int(msg.get("message_id") or 0),
        "server_id": int(msg.get("server_id") or 0),
        "sequence": int(msg.get("sequence") or 0),
        "source_table": msg.get("source_table", ""),
    }


def format_media_text(content_type: int, filename: str, *texts: str) -> str:
    parts = []
    if filename:
        parts.append(filename)
    for text in texts:
        cleaned = clean_media_text(text, filename)
        if cleaned and cleaned not in parts:
            parts.append(cleaned)
    if parts:
        return "\n".join(parts)
    return f"[{msg_type_name(content_type)}]"


def clean_media_text(text: str, filename: str = "") -> str:
    if not text:
        return ""
    lines = []
    for line in str(text).splitlines():
        line = line.strip()
        if not line:
            continue
        if filename:
            line = line.replace(filename, "").strip()
        if not line or is_media_metadata_line(line) or looks_like_garbled_media_text(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def is_media_metadata_line(line: str) -> bool:
    if len(line) <= 1:
        return True
    if ":\\" in line or "\\Cache\\" in line or "/Cache/" in line:
        return True
    if re.fullmatch(r"[A-Za-z0-9+/=]{20,}", line):
        return True
    if re.fullmatch(r"[0-9a-fA-F]{24,}", line):
        return True
    return False


def looks_like_garbled_media_text(line: str) -> bool:
    if not line or line.startswith(("http://", "https://")) or len(line) <= 8:
        return False
    total = len(line)
    hangul = sum(1 for ch in line if "\uac00" <= ch <= "\ud7af")
    cjk_compat = sum(1 for ch in line if "\u3300" <= ch <= "\u33ff")
    strange = sum(
        1
        for ch in line
        if not ch.isascii()
        and not ("\u4e00" <= ch <= "\u9fff")
        and not ("\u3000" <= ch <= "\u303f")
        and not ("\uff00" <= ch <= "\uffef")
    )
    return bool((hangul and total >= 20) or cjk_compat / total > 0.08 or strange / total > 0.25)


def extract_file_names(text: str) -> list[str]:
    result = []
    seen = set()
    for match in FILE_NAME_RE.finditer(text or ""):
        name = os.path.basename(match.group(1).strip(" ，,。；;、()（）[]【】\"'“”"))
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def detect_category(name: str, extension_type: int) -> str:
    ext = os.path.splitext(name or "")[1].lower()
    if ext in VIDEO_EXTS:
        return "Video"
    if ext in IMAGE_EXTS:
        return "Image"
    if ext in VOICE_EXTS:
        return "Voice"
    return {0: "Image", 1: "File", 2: "File", 3: "Voice", 4: "Image", 5: "File", 6: "File"}.get(
        int(extension_type or 0),
        "File",
    )


class FileResolver:
    def __init__(self, config: dict):
        self.config = config
        self.root = Path(config["wxwork_db_dir"]).parent
        self.cache_mapping_db = self._find_cache_mapping_db()

    def _find_cache_mapping_db(self) -> Path | None:
        mapping_dir = self.root / "CacheMapping"
        if not mapping_dir.is_dir():
            return None
        for path in mapping_dir.iterdir():
            if path.suffix == ".db" and not path.name.endswith(("-wal", "-shm")):
                return path
        return None

    def find_files_for_messages(self, conversation_id: str, message_ids: Iterable[int]) -> dict[int, list[dict]]:
        ids = sorted({int(mid) for mid in message_ids if int(mid or 0)})
        grouped: dict[int, list[dict]] = {mid: [] for mid in ids}
        if not ids or not db_path(self.config, "file.db").exists():
            return grouped
        with decrypted_connection(self.config, "file.db") as conn:
            if not table_exists(conn, "file_table4"):
                return grouped
            for chunk in chunks(ids, 400):
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    "SELECT message_id, server_id, name, md5, size, extension_type "
                    f"FROM file_table4 WHERE conversation_id = ? AND message_id IN ({placeholders})",
                    (conversation_id, *chunk),
                ).fetchall()
                for row in rows:
                    ext_type = int(row["extension_type"] or 0)
                    name = row["name"] or ""
                    grouped.setdefault(int(row["message_id"] or 0), []).append(
                        {
                            "message_id": int(row["message_id"] or 0),
                            "server_id": str(row["server_id"] or ""),
                            "name": name,
                            "md5": row["md5"] or "",
                            "size": int(row["size"] or 0),
                            "extension_type": ext_type,
                            "category": detect_category(name, ext_type),
                        }
                    )
        return grouped

    def find_files_by_names(self, file_names: list[str]) -> list[dict]:
        cache_root = self.root / "Cache"
        if not cache_root.is_dir():
            return []
        pending = {os.path.basename(name.strip()) for name in file_names if name and name.strip()}
        results = []
        for dirpath, _, filenames in os.walk(cache_root):
            matched = pending.intersection(filenames)
            for file_name in list(matched):
                source_path = Path(dirpath) / file_name
                category = detect_category(file_name, 0)
                results.append(
                    {
                        "message_id": 0,
                        "server_id": f"name:{file_name}",
                        "name": file_name,
                        "md5": "",
                        "size": source_path.stat().st_size if source_path.exists() else 0,
                        "extension_type": 0,
                        "category": category,
                        "source_path": str(source_path),
                    }
                )
                pending.remove(file_name)
            if not pending:
                break
        return results

    def lookup_cache_path(self, server_id: str) -> str | None:
        if not self.cache_mapping_db or not self.cache_mapping_db.exists():
            return None
        conn = sqlite3.connect(self.cache_mapping_db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT file_name FROM mapping WHERE key = ?", (server_id,)).fetchone()
            return row["file_name"] if row else None
        finally:
            conn.close()

    def source_path_for(self, file_info: dict) -> Path | None:
        if file_info.get("source_path"):
            path = Path(file_info["source_path"])
            return path if path.exists() else None
        category = file_info.get("category") or "File"
        file_name = file_info.get("name") or ""
        cache_rel = self.lookup_cache_path(str(file_info.get("server_id") or ""))
        if cache_rel:
            src = Path(cache_rel) if os.path.isabs(cache_rel) else self.root / "Cache" / category / cache_rel
            if src.exists():
                return src
        fallback = self.find_files_by_names([file_name])
        if fallback:
            path = Path(fallback[0].get("source_path") or "")
            if path.exists():
                return path
        return None


def chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def day_bounds(date_text: str, tz: ZoneInfo = TZ_SHANGHAI) -> tuple[int, int]:
    start = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=tz)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return int(start.timestamp()), int(end.timestamp())


def date_from_ts(ts: int, tz: ZoneInfo = TZ_SHANGHAI) -> str:
    return datetime.fromtimestamp(int(ts), tz=tz).strftime("%Y-%m-%d")


def now_ts(tz: ZoneInfo = TZ_SHANGHAI) -> int:
    return int(datetime.now(tz).timestamp())


def today_text(tz: ZoneInfo = TZ_SHANGHAI) -> str:
    return datetime.now(tz).strftime("%Y-%m-%d")


def iso_from_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
