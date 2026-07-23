from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import struct
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
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


def decrypt_db_to_temp(enc_path: str | os.PathLike, raw_key: bytes) -> str:
    enc_path = str(enc_path)
    size = os.path.getsize(enc_path)
    total_pages = (size + PAGE_SZ - 1) // PAGE_SZ
    fd, tmp_path = tempfile.mkstemp(prefix="wecom-chat-cache-", suffix=".db")
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
    wal_path = str(wal_path)
    if not os.path.exists(wal_path):
        return 0, 0
    wal_size = os.path.getsize(wal_path)
    if wal_size < WAL_HEADER_SZ + WAL_FRAME_HDR_SZ + PAGE_SZ:
        return 0, 0

    patched = 0
    skipped = 0
    with open(wal_path, "rb") as fwal, open(decrypted_path, "r+b") as fdb:
        wal_header = fwal.read(WAL_HEADER_SZ)
        page_size = struct.unpack_from(">I", wal_header, 8)[0]
        if page_size not in VALID_WAL_PAGE_SIZES:
            page_size = PAGE_SZ
        wal_salt1 = struct.unpack_from(">I", wal_header, 16)[0]
        wal_salt2 = struct.unpack_from(">I", wal_header, 20)[0]

        pos = WAL_HEADER_SZ
        while pos + WAL_FRAME_HDR_SZ + page_size <= wal_size:
            fwal.seek(pos)
            frame_hdr = fwal.read(WAL_FRAME_HDR_SZ)
            page_no = struct.unpack_from(">I", frame_hdr, 0)[0]
            frame_salt1 = struct.unpack_from(">I", frame_hdr, 8)[0]
            frame_salt2 = struct.unpack_from(">I", frame_hdr, 12)[0]
            encrypted_page = fwal.read(page_size)
            if len(encrypted_page) < page_size:
                break

            if (
                page_no == 0
                or page_no > 1_000_000
                or frame_salt1 != wal_salt1
                or frame_salt2 != wal_salt2
            ):
                skipped += 1
                pos += WAL_FRAME_HDR_SZ + page_size
                continue

            try:
                decrypted = decrypt_wxsqlite3_aes128_page(raw_key, encrypted_page, page_no)
                fdb.seek(0, 2)
                current_size = fdb.tell()
                target_size = page_no * PAGE_SZ
                if current_size < target_size:
                    fdb.write(b"\x00" * (target_size - current_size))
                fdb.seek((page_no - 1) * PAGE_SZ)
                fdb.write(decrypted)
                patched += 1
            except Exception:
                skipped += 1
            pos += WAL_FRAME_HDR_SZ + page_size
    return patched, skipped


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


def read_messages(
    config: dict,
    conversation_id: str,
    start_ts: int,
    end_ts: int,
    limit: int = 0,
) -> list[dict]:
    path = db_path(config, "message.db")
    raw_key = key_for_db(config, "message.db")
    tmp = decrypt_db_to_temp(path, raw_key)
    try:
        conn = sqlite3.connect(tmp)
        conn.row_factory = sqlite3.Row
        main_results = query_message_tables(conn, conversation_id, start_ts, end_ts, limit)
        conn.close()

        wal_results = []
        if os.path.exists(str(path) + "-wal"):
            try:
                patch_wal_frames(tmp, str(path) + "-wal", raw_key)
                wal_conn = sqlite3.connect(tmp)
                wal_conn.row_factory = sqlite3.Row
                try:
                    wal_results = query_message_tables(wal_conn, conversation_id, start_ts, end_ts, limit)
                finally:
                    wal_conn.close()
            except sqlite3.DatabaseError:
                wal_results = []

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
