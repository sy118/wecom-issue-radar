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

from . import media as wecom_media
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
IMAGE_WAIT_POLL_SECONDS = 0.5


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
    raw_by_message: dict[int, dict] = {}
    copied_sources: dict[int, dict[str, dict]] = {}
    image_source_retry_delays = iter(IMAGE_SOURCE_RETRY_DELAYS_SECONDS)
    max_cursor = None
    for idx, msg in enumerate(raw_messages, start=1):
        formatted = format_message(msg, user_map, member_names, tz)
        message_id = int(formatted["message_id"])
        raw_by_message[message_id] = msg
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
        copied_sources[message_id] = {
            os.path.normcase(str(item.get("source_path") or "")): item
            for item in copied_files
            if item.get("source_path")
        }
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

    # One shared resolver for every message in this run: local_extra_content
    # paths first, then file.db/CacheMapping (MD5 refs cover forwarded images),
    # then a bounded Cache/Image filename scan.  The wait budget is configurable
    # (config["image_wait_seconds"] or WECOM_IMAGE_WAIT_SECONDS, default 30 s)
    # and only recent messages are polled, so historical reports stay fast.
    media_requests = build_media_requests(records, raw_by_message)
    if media_requests:
        wait_seconds = wecom_media.image_wait_seconds(
            config, default=wecom_media.DEFAULT_DAILY_IMAGE_WAIT_SECONDS
        )
        media_resolver = wecom_media.MediaResolver(config)
        if wait_seconds > 0 and media_resolver.precondition().ok:
            # An explicit process check stops a pointless 30 s poll when the
            # WeCom client is closed; missing images stay honestly labelled.
            media_resolver.process_running = wecom_media.wecom_process_running()
        descriptors_by_message = media_resolver.resolve_batch(
            media_requests,
            wait_seconds=wait_seconds,
            poll_interval=IMAGE_WAIT_POLL_SECONDS,
        )
        manifest_records.extend(
            apply_media_descriptors(
                records,
                descriptors_by_message,
                bulk_dir=bulk_dir,
                conversation_id=conversation_id,
                copied_sources=copied_sources,
            )
        )

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


def build_media_requests(
    records: list[dict],
    raw_by_message: dict[int, dict],
) -> list[wecom_media.MediaRequest]:
    """Build resolver input for every message that declares a visible image."""

    requests = []
    for record in records:
        message_id = int(record.get("message_id") or 0)
        if not message_id:
            continue
        visible = int(record.get("image_count_visible") or 0)
        if visible <= 0:
            continue
        raw = dict(raw_by_message.get(message_id) or {})
        content_type = int(record.get("content_type") or raw.get("content_type") or 0)
        hints = list(
            wecom_media.filename_hints_from_raw({**raw, "text": record.get("raw_text") or ""})
        )
        hints.extend(str(name) for name in record.get("missing_image_names") or [] if name)
        requests.append(
            wecom_media.MediaRequest(
                message_id=message_id,
                conversation_id=str(record.get("conversation_id") or ""),
                content_type=content_type,
                local_extra=raw.get("local_extra_content_raw"),
                content_raw=raw.get("content_raw"),
                extra_content_raw=raw.get("extra_content_raw"),
                text=str(record.get("raw_text") or ""),
                expected_count=visible,
                filename_hints=tuple(dict.fromkeys(hints)),
                md5_refs=tuple(wecom_media.extract_image_md5_refs(content_type, raw)),
                send_time=int(record.get("send_time") or 0),
            )
        )
    return requests


def _record_image_status(visible: int, available: int) -> str:
    if visible <= 0:
        return "not_required"
    if available <= 0:
        return "missing_original_images"
    return "ready" if available >= visible else "partial_missing_original_images"


def _image_already_copied(record: dict, descriptor: dict) -> dict | None:
    """Reuse legacy copies instead of copying the same cache file twice."""

    source_path = str(descriptor.get("path") or "")
    filename = sanitize_filename(str(descriptor.get("filename") or ""))
    for image in record.get("images") or []:
        if not isinstance(image, dict):
            continue
        if source_path and str(image.get("source_path") or ""):
            try:
                if os.path.normcase(str(Path(str(image["source_path"])))) == os.path.normcase(
                    str(Path(source_path))
                ):
                    return image
            except (OSError, ValueError):
                pass
        local_name = str(image.get("filename") or "")
        if filename and local_name.endswith("_" + filename):
            return image
    return None


def _materialize_media_image(
    descriptor: dict,
    bulk_dir: Path,
    message_id: int,
    ordinal: int,
) -> dict | None:
    source = Path(str(descriptor.get("path") or ""))
    if not source.is_file():
        return None
    filename = str(descriptor.get("filename") or source.name)
    safe_filename = sanitize_filename(filename)
    target = bulk_dir / f"{message_id}_{ordinal:02d}_{safe_filename}"
    try:
        if not target.exists():
            shutil.copy2(source, target)
        size = target.stat().st_size
    except OSError:
        return None
    return {
        "local_path": str(target),
        "filename": target.name,
        "size_bytes": size,
        "capture_method": "wecom_media",
        "source_message_id": message_id,
        "source_server_id": str(descriptor.get("server_id") or ""),
        "source_path": str(source),
    }


def _manifest_from_image(
    image: dict,
    message_id: int,
    record: dict,
    conversation_id: str,
    descriptor: dict | None = None,
) -> dict | None:
    local_path = str(image.get("local_path") or "")
    if not local_path:
        return None
    path = Path(local_path)
    try:
        if not path.is_file():
            return None
        modified_at = iso_from_mtime(path)
    except OSError:
        return None
    return {
        "local_path": local_path,
        "filename": str(image.get("filename") or path.name),
        "size_bytes": int(image.get("size_bytes") or 0),
        "modified_at": modified_at,
        "accepted": True,
        "source_message_id": int(message_id),
        "source_server_id": str(record.get("server_id") or ""),
        "conversation_id": str(record.get("conversation_id") or conversation_id),
        "md5": str((descriptor or {}).get("md5") or image.get("md5") or ""),
    }


def apply_media_descriptors(
    records: list[dict],
    descriptors_by_message: dict[int, list],
    *,
    bulk_dir: Path,
    conversation_id: str,
    copied_sources: dict[int, dict[str, dict]] | None = None,
) -> list[dict]:
    """Merge resolver descriptors into raw message records.

    Available descriptors are copied into the bulk cache (unless a legacy copy
    already exists) and added to the manifest.  Missing descriptors become
    explicit placeholders with a stable reason code; they never remove a
    previously available image.
    """

    copied_sources = copied_sources or {}
    by_message_id = {int(record.get("message_id") or 0): record for record in records}
    manifest_records: list[dict] = []
    for raw_message_id, descriptors in descriptors_by_message.items():
        message_id = int(raw_message_id or 0)
        record = by_message_id.get(message_id)
        if record is None:
            continue
        existing_images = [image for image in record.get("images") or [] if isinstance(image, dict)]
        merged_images = list(existing_images)
        available: list[dict] = []
        missing: list[dict] = []
        sources = copied_sources.get(message_id, {})
        for descriptor in descriptors:
            payload = descriptor.to_dict() if hasattr(descriptor, "to_dict") else dict(descriptor)
            if payload.get("available"):
                available.append(payload)
                legacy = _image_already_copied(record, payload)
                if legacy is not None:
                    manifest = _manifest_from_image(legacy, message_id, record, conversation_id, payload)
                    if manifest:
                        manifest_records.append(manifest)
                    continue
                source_key = os.path.normcase(str(payload.get("path") or ""))
                if source_key and source_key in sources:
                    legacy = sources[source_key]
                    manifest = _manifest_from_image(legacy, message_id, record, conversation_id, payload)
                    if manifest:
                        manifest_records.append(manifest)
                    continue
                copied = _materialize_media_image(
                    payload, bulk_dir, message_id, len(merged_images) + 1
                )
                if copied is None:
                    payload["available"] = False
                    payload["status"] = "missing"
                    payload["reason"] = wecom_media.REASON_PARTIAL
                    missing.append(payload)
                    continue
                merged_images.append(copied)
                manifest = _manifest_from_image(copied, message_id, record, conversation_id, payload)
                if manifest:
                    manifest_records.append(manifest)
            else:
                missing.append(payload)
        deduped_images = []
        seen_paths: set[str] = set()
        for image in merged_images:
            local_path = str(image.get("local_path") or "")
            key = os.path.normcase(local_path) if local_path else ""
            if key and key in seen_paths:
                continue
            if key:
                seen_paths.add(key)
            deduped_images.append(image)
        if not available and deduped_images:
            # Keep the descriptor list useful when a rescan could not reach the
            # database but the message already had cached images.
            available = [
                _descriptor_from_existing_image(image, record) for image in deduped_images
            ]
        record["images"] = deduped_images
        record["image_paths"] = [str(image.get("local_path") or "") for image in deduped_images]
        record["image_descriptors"] = [*available, *missing]
        record["missing_images"] = missing
        record["missing_image_reasons"] = sorted(
            {str(item.get("reason") or "") for item in missing if item.get("reason")}
        )
        missing_names = [
            str(item.get("filename") or "") for item in missing if item.get("filename")
        ]
        record["missing_image_names"] = list(
            dict.fromkeys([*(record.get("missing_image_names") or []), *missing_names])
        )
        visible = max(
            int(record.get("image_count_visible") or 0),
            len(available) + len(missing),
        )
        record["image_count_visible"] = visible
        record["image_status"] = _record_image_status(visible, len(deduped_images))
    return manifest_records


def _rescan_raw_messages(config: dict, records: list[dict]) -> dict[int, dict]:
    """Re-read message DB rows for the requested records (best effort)."""

    by_conversation: dict[str, list[dict]] = {}
    for record in records:
        conversation_id = str(record.get("conversation_id") or "")
        if not conversation_id:
            continue
        by_conversation.setdefault(conversation_id, []).append(record)
    result: dict[int, dict] = {}
    for conversation_id, group in by_conversation.items():
        send_times = [int(row.get("send_time") or 0) for row in group if int(row.get("send_time") or 0)]
        if not send_times:
            continue
        try:
            rows = read_messages(
                config,
                conversation_id,
                min(send_times) - 1,
                max(send_times) + 1,
                limit=0,
            )
        except Exception:
            # Rescan is best effort: when the DB is closed/locked, keep the
            # existing records and let the cache/file stages report the status.
            continue
        for row in rows:
            message_id = int(row.get("message_id") or 0)
            if message_id:
                result[message_id] = row
    return result


def _descriptor_from_existing_image(image: dict, record: dict) -> dict:
    local_path = str(image.get("local_path") or "")
    path = Path(local_path)
    return {
        "available": True,
        "status": "ready",
        "reason": "",
        "path": local_path,
        "filename": str(image.get("filename") or path.name),
        "mime": wecom_media.image_mime(path),
        "size": int(image.get("size_bytes") or 0),
        "md5": str(image.get("md5") or ""),
        "source": "local_cache",
        "detail": "",
    }


def rescan_images(
    day_dir: str | Path,
    *,
    config_path: str | Path | None = None,
    message_ids: list[int] | None = None,
    wait_seconds: float = 0.0,
) -> dict:
    """Re-scan local WeCom caches for a finished run and update its media state.

    Safe to call repeatedly: manifest records are keyed by local path and image
    lists are rebuilt with de-duplication, so a second call is a no-op when
    nothing changed.  Returns a JSON-serialisable summary for the IPC layer::

        {
          "dayDir", "date", "scannedMessages", "updatedMessages",
          "availableImages", "missingImages", "missingReasons",
          "descriptors": {"<message_id>": [descriptor, ...]},
          "changed": bool,
          "precondition": {...} | None,
        }

    This function updates ``raw_messages.jsonl`` and the HD cache manifest so
    subsequent exports/previews see the recovered images.  It does not rewrite
    the frozen problem snapshot (``image_manifest`` in grouped issues); callers
    that freeze issue snapshots should re-run analysis or re-freeze explicitly.
    """

    day_path = Path(day_dir).resolve()
    records_path = day_path / "raw_messages.jsonl"
    records = read_jsonl(records_path)
    bulk_dir = day_path / "raw_attachments" / "_bulk_hd_cache"
    manifest_path = bulk_dir / "hd_cache_manifest.json"
    summary = {
        "dayDir": str(day_path),
        "date": "",
        "scannedMessages": 0,
        "updatedMessages": 0,
        "availableImages": 0,
        "missingImages": 0,
        "missingReasons": {},
        "descriptors": {},
        "changed": False,
        "precondition": None,
    }
    if not records:
        return summary
    target_ids = {int(value) for value in (message_ids or []) if int(value or 0) > 0}
    selected = [
        record
        for record in records
        if (not target_ids or int(record.get("message_id") or 0) in target_ids)
        and int(record.get("image_count_visible") or 0) > 0
    ]
    summary["date"] = str(selected[0].get("date") or "") if selected else ""
    summary["scannedMessages"] = len(selected)
    if not selected:
        return summary
    config = load_config(str(config_path) if config_path else None)
    raw_by_message = _rescan_raw_messages(config, selected)
    requests = build_media_requests(selected, raw_by_message)
    media_resolver = wecom_media.MediaResolver(config)
    if not media_resolver.precondition().ok:
        summary["precondition"] = media_resolver.precondition().to_dict()
    descriptors_by_message = media_resolver.resolve_batch(
        requests,
        wait_seconds=max(0.0, float(wait_seconds or 0.0)),
        poll_interval=IMAGE_WAIT_POLL_SECONDS,
    )
    before = {
        int(record.get("message_id") or 0): (
            tuple(str(image.get("local_path") or "") for image in record.get("images") or []),
            str(record.get("image_status") or ""),
        )
        for record in selected
    }
    new_manifest = apply_media_descriptors(
        selected,
        descriptors_by_message,
        bulk_dir=bulk_dir,
        conversation_id=str(selected[0].get("conversation_id") or ""),
    )
    updated = 0
    for record in selected:
        message_id = int(record.get("message_id") or 0)
        if before.get(message_id) != (
            tuple(str(image.get("local_path") or "") for image in record.get("images") or []),
            str(record.get("image_status") or ""),
        ):
            updated += 1
    total_available = 0
    total_missing = 0
    reasons: dict[str, int] = {}
    for record in selected:
        total_available += len(record.get("images") or [])
        for item in record.get("missing_images") or []:
            reason = str(item.get("reason") or "")
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1
        total_missing += len(record.get("missing_images") or [])
    manifest_doc = read_json(manifest_path) or {"date": summary["date"], "records": []}
    old_records = {
        manifest_key(item): item for item in manifest_doc.get("records") or [] if manifest_key(item)
    }
    kept = [
        item
        for item in manifest_doc.get("records") or []
        if target_ids and int(item.get("source_message_id") or 0) not in target_ids
    ]
    rebuilt: list[dict] = []
    for record in selected:
        message_id = int(record.get("message_id") or 0)
        for image in record.get("images") or []:
            manifest = _manifest_from_image(
                image, message_id, record, str(record.get("conversation_id") or "")
            )
            if manifest:
                rebuilt.append(manifest)
    merged = dict(old_records) if not target_ids else {}
    for item in [*kept, *rebuilt, *new_manifest]:
        key = manifest_key(item)
        if key:
            merged[key] = item
    new_manifest_records = sorted(
        merged.values(),
        key=lambda row: (
            int(row.get("source_message_id") or 0),
            str(row.get("local_path") or ""),
        ),
    )
    changed = bool(updated) or new_manifest_records != list(manifest_doc.get("records") or [])
    summary.update(
        {
            "updatedMessages": updated,
            "availableImages": total_available,
            "missingImages": total_missing,
            "missingReasons": reasons,
            "changed": changed,
        }
    )
    if changed:
        write_jsonl(records_path, records)
        write_json(
            manifest_path,
            {
                "date": summary["date"] or manifest_doc.get("date") or "",
                "records": new_manifest_records,
            },
        )
    summary["descriptors"] = {
        str(int(message_id)): [
            item.to_dict() if hasattr(item, "to_dict") else dict(item)
            for item in descriptors
        ]
        for message_id, descriptors in descriptors_by_message.items()
    }
    return summary


if __name__ == "__main__":
    raise SystemExit(main())
