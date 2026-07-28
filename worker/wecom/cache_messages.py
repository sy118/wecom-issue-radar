from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .local_db import (
    FileResolver,
    date_from_ts,
    extract_file_names,
    format_message,
    get_conversation_state,
    iso_from_mtime,
    list_conversations,
    load_config,
    load_member_names,
    load_user_map,
    read_messages,
    today_text,
)


IMAGE_CATEGORIES = {"Image"}
VIDEO_CONTENT_TYPES = {23}
CLOCK_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
# Shared across one group run, so permanently missing images add at most 350 ms
# of intentional waiting rather than multiplying the delay by the image count.
IMAGE_SOURCE_RETRY_DELAYS_SECONDS = (0.1, 0.25)


def clock_time(value: str) -> str:
    text = str(value or "")
    if not CLOCK_RE.fullmatch(text):
        raise argparse.ArgumentTypeError("时间必须使用两位 24 小时制 HH:MM 格式")
    return text


def time_range_bounds(
    start_date: str,
    start_time: str,
    end_date: str,
    end_time: str,
    tz: ZoneInfo,
) -> tuple[int, int]:
    start = datetime.strptime(f"{start_date} {clock_time(start_time)}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    end = (
        datetime.strptime(f"{end_date} {clock_time(end_time)}", "%Y-%m-%d %H:%M")
        .replace(tzinfo=tz)
        + timedelta(seconds=59)
    )
    if end < start:
        raise ValueError("结束时间不能早于开始时间")
    return int(start.timestamp()), int(end.timestamp())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cache WeCom group messages from local WXWork databases.")
    parser.add_argument("--config", default=None, help="Path to config.local.json")
    parser.add_argument("--workspace", default="", help="Workspace that contains work/YYYY-MM-DD")
    parser.add_argument("--date", default="", help="Asia/Shanghai date, YYYY-MM-DD")
    parser.add_argument("--start-date", default="", help="Inclusive start date, YYYY-MM-DD")
    parser.add_argument("--end-date", default="", help="Inclusive end date, YYYY-MM-DD")
    parser.add_argument("--conversation-id", default="", help="Conversation ID to export; defaults to config target")
    parser.add_argument("--start-time", type=clock_time, default="00:00", help="Inclusive start minute, HH:MM")
    parser.add_argument("--end-time", type=clock_time, default="23:59", help="Inclusive end minute, HH:MM")
    parser.add_argument("--since-cursor", action="store_true", help="Read only messages after saved cursor")
    parser.add_argument("--list-conversations", action="store_true", help="List recent conversations and exit")
    parser.add_argument("--search", default="", help="Search text for --list-conversations")
    parser.add_argument("--limit", type=int, default=0, help="Optional max messages")
    parser.add_argument("--include-direct", action="store_true", help="Include direct chats when listing conversations")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    tz = ZoneInfo(config.get("timezone") or "Asia/Shanghai")

    if args.list_conversations:
        rows = list_conversations(
            config,
            limit=args.limit or 50,
            search=args.search,
            include_direct=args.include_direct,
        )
        print("conversation_id\tname\tlast_message_time")
        for row in rows:
            ts = int(row.get("last_message_time") or 0)
            text = datetime.fromtimestamp(ts, tz=tz).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
            print(f"{row['conversation_id']}\t{row['display_name']}\t{text}")
        return 0

    workspace = Path(args.workspace or config.get("default_workspace") or Path.cwd()).resolve()
    start_date = args.start_date or args.date or today_text(tz)
    end_date = args.end_date or start_date
    date_text = range_directory_name(start_date, end_date)
    cursor_path = workspace / "work" / ".state" / "wecom_chat_daily_cache_cursor.json"

    if args.since_cursor:
        cursor = read_json(cursor_path) or {}
        conversation_id = args.conversation_id or config["target_group_id"]
        conversation = get_conversation_state(config, conversation_id)
        if not conversation:
            raise SystemExit(f"target conversation not found: {conversation_id}")
        if not cursor.get("last_send_time"):
            latest_messages = read_messages(config, conversation_id, 0, int(datetime.now(tz).timestamp()), limit=1)
            latest = latest_messages[-1] if latest_messages else {}
            write_json(
                cursor_path,
                {
                    "conversation_id": conversation_id,
                    "last_send_time": int(latest.get("send_time") or conversation.get("last_message_time") or 0),
                    "last_message_id": int(latest.get("message_id") or conversation.get("last_message_id") or 0),
                    "last_server_id": int(latest.get("server_id") or 0),
                    "updated_at": datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S"),
                    "initialized": True,
                },
            )
            print(
                json.dumps(
                    {
                        "workspace": str(workspace),
                        "cursor": str(cursor_path),
                        "initialized": True,
                        "message": "cursor initialized at current conversation state; run again to fetch new messages",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        start_ts = int(cursor.get("last_send_time") or 0) + 1
        end_ts = int(datetime.now(tz).timestamp())
        if not args.date and not args.start_date and start_ts > 0:
            start_date = date_from_ts(start_ts, tz)
            end_date = start_date
            date_text = start_date
    else:
        start_ts, end_ts = time_range_bounds(start_date, args.start_time, end_date, args.end_time, tz)

    conversation_id = args.conversation_id or config["target_group_id"]
    conversation = get_conversation_state(config, conversation_id)
    if not conversation:
        raise SystemExit(f"target conversation not found: {conversation_id}")

    day_dir = workspace / "work" / date_text
    attachments_dir = day_dir / "raw_attachments"
    bulk_dir = attachments_dir / "_bulk_hd_cache"
    grouped_dir = day_dir / "grouped_issues"
    for path in (attachments_dir, bulk_dir, grouped_dir, cursor_path.parent):
        path.mkdir(parents=True, exist_ok=True)

    user_map = load_user_map(config)
    member_names = load_member_names(config)
    raw_messages = read_messages(config, conversation_id, start_ts, end_ts, args.limit)
    conversation_last_message_time = int(conversation.get("last_message_time") or 0)
    if (
        not raw_messages
        and not args.since_cursor
        and conversation_last_message_time >= start_ts
    ):
        # The live WeCom database and its WAL can change while a decrypted snapshot is
        # being assembled. A fresh snapshot avoids treating that transient empty read
        # as proof that an otherwise active group has no messages.
        raw_messages = read_messages(config, conversation_id, start_ts, end_ts, args.limit)
    resolver = FileResolver(config)
    files_by_message = resolver.find_files_for_messages(
        conversation_id,
        [int(msg.get("message_id") or 0) for msg in raw_messages],
    )

    records = []
    manifest_records = []
    image_source_retry_delays = iter(IMAGE_SOURCE_RETRY_DELAYS_SECONDS)
    max_cursor = None
    for idx, msg in enumerate(raw_messages, start=1):
        formatted = format_message(msg, user_map, member_names, tz)
        referenced_file_names = extract_file_names(formatted.get("content", ""))
        file_infos = files_by_message.get(int(formatted["message_id"]), [])
        known_names = {Path(str(info.get("name") or "")).name for info in file_infos if info.get("name")}
        missing_referenced_names = [
            name for name in referenced_file_names
            if Path(name).name not in known_names
        ]
        if missing_referenced_names:
            file_infos = dedupe_file_infos([*file_infos, *resolver.find_files_by_names(missing_referenced_names)])

        copied_files = []
        images = []
        for ordinal, file_info in enumerate(file_infos, start=1):
            copied = copy_attachment(
                resolver=resolver,
                file_info=file_info,
                attachments_dir=attachments_dir,
                bulk_dir=bulk_dir,
                date_text=date_text,
                message_id=formatted["message_id"],
                ordinal=ordinal,
                image_source_retry_delays=image_source_retry_delays,
            )
            if not copied:
                continue
            copied_files.append(copied)
            if copied["category"] in IMAGE_CATEGORIES:
                image = {
                    "local_path": copied["local_path"],
                    "filename": copied["filename"],
                    "size_bytes": copied["size_bytes"],
                    "capture_method": "local_db_cache",
                    "source_message_id": formatted["message_id"],
                    "source_server_id": formatted["server_id"],
                }
                images.append(image)
                manifest_records.append(
                    {
                        "local_path": copied["local_path"],
                        "filename": copied["filename"],
                        "size_bytes": copied["size_bytes"],
                        "modified_at": copied["modified_at"],
                        "accepted": True,
                        "source_message_id": formatted["message_id"],
                        "source_server_id": formatted["server_id"],
                        "conversation_id": conversation_id,
                    }
                )

        referenced_image_names = [
            name for name in referenced_file_names
            if os.path.splitext(name)[1].lower() in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico"}
        ]
        image_count_visible = max(len(images), len(referenced_image_names))
        if formatted["content_type"] in {4, 123} and image_count_visible == 0:
            image_count_visible = 1
        copied_original_names = {item.get("original_filename") for item in copied_files if item.get("original_filename")}
        missing_image_names = [
            name for name in referenced_image_names
            if name not in copied_original_names and name not in {image.get("filename") for image in images}
        ]

        record = {
            "date": formatted["date"] or date_text,
            "message_time": formatted["message_time"],
            "sender": formatted["sender"],
            "sender_id": formatted["sender_id"],
            "raw_text": build_raw_text(formatted),
            "dedupe_key": dedupe_key(formatted),
            "conversation_id": conversation_id,
            "conversation_name": conversation.get("display_name") or config.get("target_group_name") or "",
            "message_id": formatted["message_id"],
            "server_id": formatted["server_id"],
            "sequence": formatted["sequence"],
            "content_type": formatted["content_type"],
            "type": formatted["type"],
            "send_time": formatted["send_time"],
            "images": images,
            "image_paths": [image["local_path"] for image in images],
            "image_count_visible": image_count_visible,
            "image_status": image_status(formatted["content_type"], image_count_visible, images),
            "missing_image_names": missing_image_names,
            "files": copied_files,
            "is_video": formatted["content_type"] in VIDEO_CONTENT_TYPES,
            "source_row_index": idx,
            "source_table": formatted.get("source_table", ""),
        }
        records.append(record)
        max_cursor = formatted

    records = merge_existing_records(day_dir / "raw_messages.jsonl", records)
    if not args.since_cursor:
        records = [
            record
            for record in records
            if start_ts <= int(record.get("send_time") or 0) <= end_ts
        ]
    write_jsonl(day_dir / "raw_messages.jsonl", records)
    manifest_records = merge_manifest_records(bulk_dir / "hd_cache_manifest.json", manifest_records)
    if not args.since_cursor:
        selected_message_ids = {int(record.get("message_id") or 0) for record in records}
        manifest_records = [
            record
            for record in manifest_records
            if int(record.get("source_message_id") or 0) in selected_message_ids
        ]
    write_json(bulk_dir / "hd_cache_manifest.json", {"date": date_text, "records": manifest_records})

    if args.since_cursor and max_cursor:
        write_json(
            cursor_path,
            {
                "conversation_id": conversation_id,
                "last_send_time": int(max_cursor["send_time"]),
                "last_message_id": int(max_cursor["message_id"]),
                "last_server_id": int(max_cursor["server_id"]),
                "updated_at": datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

    print(
        json.dumps(
            {
                "workspace": str(workspace),
                "day_dir": str(day_dir),
                "raw_messages": str(day_dir / "raw_messages.jsonl"),
                "message_count": len(records),
                "new_message_count": len(raw_messages),
                "start_date": start_date,
                "end_date": end_date,
                "start_time": args.start_time if not args.since_cursor else "",
                "end_time": args.end_time if not args.since_cursor else "",
                "start_timestamp": start_ts,
                "end_timestamp": end_ts,
                "image_manifest": str(bulk_dir / "hd_cache_manifest.json"),
                "image_count": len(manifest_records),
                "cursor": str(cursor_path) if args.since_cursor else "",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def range_directory_name(start_date: str, end_date: str) -> str:
    return start_date if start_date == end_date else f"{start_date}_to_{end_date}"


def build_raw_text(formatted: dict) -> str:
    pieces = []
    if formatted.get("sender") or formatted.get("time"):
        pieces.append(f"{formatted.get('sender', '')} {formatted.get('time', '')}".strip())
    if formatted.get("type"):
        pieces.append(f"[{formatted['type']}]")
    pieces.append(str(formatted.get("content") or ""))
    return " ".join(piece for piece in pieces if piece).strip()


def dedupe_key(formatted: dict) -> str:
    return (
        f"{formatted.get('conversation_id', '')}:"
        f"{formatted.get('message_id', 0)}:"
        f"{formatted.get('server_id', 0)}:"
        f"{formatted.get('send_time', 0)}"
    )


def image_status(content_type: int, visible_count: int, images: list[dict]) -> str:
    if visible_count <= 0:
        return "not_required"
    if images:
        return "ready" if len(images) >= visible_count else "partial_missing_original_images"
    return "missing_original_images"


def dedupe_file_infos(file_infos: list[dict]) -> list[dict]:
    result = []
    seen = set()
    for info in file_infos:
        key = (
            str(info.get("source_path") or ""),
            str(info.get("server_id") or ""),
            str(info.get("name") or ""),
            str(info.get("md5") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(info)
    return result


def copy_attachment(
    *,
    resolver: FileResolver,
    file_info: dict,
    attachments_dir: Path,
    bulk_dir: Path,
    date_text: str,
    message_id: int,
    ordinal: int,
    image_source_retry_delays: Iterator[float] | None = None,
) -> dict | None:
    category = file_info.get("category") or "File"
    src = resolver.source_path_for(file_info)
    if category in IMAGE_CATEGORIES and image_source_retry_delays is not None:
        while not src or not src.exists() or not src.is_file():
            delay = next(image_source_retry_delays, None)
            if delay is None:
                break
            time.sleep(delay)
            src = resolver.source_path_for(file_info)
    if not src or not src.exists() or not src.is_file():
        return None
    filename = file_info.get("name") or src.name
    safe_filename = sanitize_filename(filename)
    prefix = f"{message_id}_{ordinal:02d}_"
    target_root = bulk_dir if category == "Image" else attachments_dir / category.lower()
    target_root.mkdir(parents=True, exist_ok=True)
    target = target_root / f"{prefix}{safe_filename}"
    if not target.exists():
        shutil.copy2(src, target)
    return {
        "local_path": str(target),
        "filename": target.name,
        "original_filename": filename,
        "category": category,
        "size_bytes": target.stat().st_size,
        "modified_at": iso_from_mtime(target),
        "source_path": str(src),
        "server_id": str(file_info.get("server_id") or ""),
        "md5": file_info.get("md5") or "",
    }


def sanitize_filename(value: str) -> str:
    name = os.path.basename(value or "attachment")
    name = "".join(ch if ch not in '<>:"/\\|?*\r\n\t' else "_" for ch in name).strip(" .")
    return name[:160] or "attachment"


def read_json(path: Path):
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def merge_existing_records(path: Path, new_records: list[dict]) -> list[dict]:
    merged = {record.get("dedupe_key"): record for record in read_jsonl(path) if record.get("dedupe_key")}
    for record in new_records:
        merged[record["dedupe_key"]] = record
    return sorted(
        merged.values(),
        key=lambda row: (int(row.get("send_time") or 0), int(row.get("message_id") or 0), int(row.get("server_id") or 0)),
    )


def merge_manifest_records(path: Path, new_records: list[dict]) -> list[dict]:
    existing_doc = read_json(path) or {}
    merged = {}
    for record in existing_doc.get("records") or []:
        key = manifest_key(record)
        if key:
            merged[key] = record
    for record in new_records:
        key = manifest_key(record)
        if key:
            merged[key] = record
    return sorted(
        merged.values(),
        key=lambda row: (
            int(row.get("source_message_id") or 0),
            str(row.get("local_path") or ""),
        ),
    )


def manifest_key(record: dict) -> str:
    local_path = str(record.get("local_path") or "")
    if local_path:
        return local_path
    return f"{record.get('source_message_id', '')}:{record.get('filename', '')}"


if __name__ == "__main__":
    raise SystemExit(main())
