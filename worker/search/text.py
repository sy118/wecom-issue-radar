"""Text helpers for the local chat search index.

Chinese is indexed and queried as "character + space" so SQLite's unicode61
tokenizer can match it; latin words stay whole tokens. Only text derived from
the message body is indexed, never media paths or logs.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CJK_RANGES = (
    (0x3400, 0x4DBF),  # CJK extension A
    (0x4E00, 0x9FFF),  # CJK unified ideographs
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0x20000, 0x2FA1F),  # CJK extensions B-F
)

KIND_ALIASES = {
    "text": "text",
    "文本": "text",
    "文字": "text",
    "消息": "text",
    "image": "image",
    "img": "image",
    "图片": "image",
    "照片": "image",
    "表情": "image",
    "file": "file",
    "文件": "file",
    "附件": "file",
    "文档": "file",
    "voice": "voice",
    "audio": "voice",
    "语音": "voice",
    "video": "video",
    "视频": "video",
    "link": "link",
    "链接": "link",
    "url": "link",
    "图文": "link",
    "system": "system",
    "系统": "system",
    "系统消息": "system",
    "notice": "system",
    "other": "other",
}

# wecom cache_messages writes WeCom content_type numbers; keep a small fallback map.
CONTENT_TYPE_KINDS = {
    1: "text",
    3: "image",
    4: "image",
    49: "file",
    10000: "system",
}


def is_cjk(character: str) -> bool:
    code = ord(character)
    return any(start <= code <= end for start, end in CJK_RANGES)


def space_cjk(text: object) -> str:
    """Return text where every CJK character is a standalone token."""
    value = str(text or "")
    out: list[str] = []
    for character in value:
        if is_cjk(character):
            out.append(" ")
            out.append(character)
            out.append(" ")
        else:
            out.append(character)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _fts_quote(phrase: str) -> str:
    return '"' + phrase.replace('"', '""') + '"'


def build_match_query(query: object) -> str:
    """Build a safe FTS5 MATCH expression for arbitrary user input.

    Each whitespace-separated part becomes one or more quoted phrases; CJK runs
    become character phrases ("登 录 失 败"), latin runs stay word tokens. All
    parts must match (AND) so extra words narrow the result.
    """
    value = str(query or "").strip()
    if not value:
        return ""
    terms: list[str] = []
    segment_pattern = (
        r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+|[0-9A-Za-z_]+"
    )
    for part in re.split(r"\s+", value):
        if not part:
            continue
        for segment in re.findall(segment_pattern, part):
            if is_cjk(segment[0]):
                terms.append(_fts_quote(" ".join(segment)))
            else:
                terms.append(_fts_quote(segment))
    if not terms:
        return ""
    return " AND ".join(f"body : ({term})" for term in terms)


def normalize_kind(value: object) -> str:
    """Map WeCom/WeChat message type labels to a canonical kind."""
    text = str(value or "").strip()
    if not text:
        return "other"
    lowered = text.lower()
    if lowered in KIND_ALIASES:
        return KIND_ALIASES[lowered]
    for key, kind in KIND_ALIASES.items():
        if key.isascii():
            if key in lowered:
                return kind
        elif key in text:
            return kind
    return "other"


def normalize_content_type(value: object) -> str | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return CONTENT_TYPE_KINDS.get(number)


def normalize_source(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"wecom", "wxwork", "企业微信", "企微"}:
        return "wecom"
    if text in {"wechat", "微信", "个人微信", "weixin"}:
        return "wechat"
    return text


def timezone_for(name: object):
    zone_name = str(name or "").strip() or "Asia/Shanghai"
    try:
        return ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return datetime.now().astimezone().tzinfo or timezone.utc


def to_epoch_seconds(value: object) -> int:
    """Coerce a stored timestamp (seconds, milliseconds or ISO text) to seconds."""
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        number = int(value)
        return number // 1000 if number > 100_000_000_000 else number
    text = str(value).strip()
    if re.fullmatch(r"-?\d+", text):
        return to_epoch_seconds(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def parse_time_bound(
    value: object, tz_name: object = "Asia/Shanghai", *, end_of_day: bool = False
) -> int | None:
    """Parse a filter bound (epoch, millisecond epoch, ISO date/datetime)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("时间筛选格式无效，请使用 YYYY-MM-DD 或时间戳")
    if isinstance(value, (int, float)):
        return to_epoch_seconds(value)
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"-?\d{9,14}", text):
        return to_epoch_seconds(int(text))
    zone = timezone_for(tz_name)
    date_match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if date_match:
        day = date(int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3)))
        moment = datetime(day.year, day.month, day.day, tzinfo=zone)
        if end_of_day:
            moment = moment + timedelta(days=1) - timedelta(seconds=1)
        return int(moment.timestamp())
    iso_text = text.replace(" ", "T", 1) if " " in text and "T" not in text else text
    try:
        parsed = datetime.fromisoformat(iso_text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("时间筛选格式无效，请使用 YYYY-MM-DD 或 ISO 时间") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return int(parsed.timestamp())


def iso_utc(timestamp: int | float | None) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
