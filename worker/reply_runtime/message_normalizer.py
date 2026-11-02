from __future__ import annotations

import json
import re


_IMAGE_REFERENCE_FIELDS = (
    "content_raw",
    "extra_content_raw",
    "local_extra_content_raw",
)
_IMAGE_PLACEHOLDER_RE = re.compile(r"\[(?:图片|截图|图像|image)\]", re.IGNORECASE)
_BINARY_PLACEHOLDER_RE = re.compile(r"\[二进制内容\s+\d+\s+字节\]")
_IMAGE_FILENAME_LINE_RE = re.compile(
    r"^[^\\/:*?\"<>|\r\n]{1,220}\.(?:png|jpe?g|gif|webp|bmp|svg|ico)$",
    re.IGNORECASE,
)
_IMAGE_FILENAME_SUFFIX_RE = re.compile(
    r"\.(?:png|jpe?g|gif|webp|bmp|svg|ico)(?=$|[\\/\x00-\x20])",
    re.IGNORECASE,
)
_FILE_PLACEHOLDER_RE = re.compile(r"^\[(?:文件|附件|file)\]$", re.IGNORECASE)
# WeCom protobuf field 10 (wire tag 0x52) contains an attachment MD5 as a
# 32-byte ASCII hex value. The extractor lives in worker.wecom.media so the
# reply runtime and the daily pipeline share one implementation.
_MAX_IMAGE_REF_SCAN_BYTES = 1_048_576

_APP_CONTENT_TYPES = {38}
_APP_PAYLOAD_HINTS = (
    '"source_icon_desc"',
    '"display"',
    '"main_title"',
    '"selected"',
    '"target":',
    '"icon_url"',
)
_PRIORITY_APP_KEYS = (
    "title",
    "main_title",
    "sub_title",
    "subtitle",
    "content",
    "text",
    "desc",
    "description",
    "summary",
    "display",
    "name",
    "label",
)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_TECHNICAL_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:/\\-]{1,80}$")
_MAX_SUMMARY_CHARS = 400


def normalize_wecom_message(
    raw: dict,
    formatted: dict,
    *,
    group_id: str,
    identity: dict | None = None,
) -> dict:
    """Hide WeCom content-type and raw-field quirks behind one message shape."""

    identity = identity or {}
    sender_id = int(formatted.get("sender_id") or 0)
    content_type = int(formatted.get("content_type") or 0)
    formatted_text = str(formatted.get("content") or "")
    image_md5_refs = _image_md5_refs(raw, content_type)
    has_image_attachment = bool(image_md5_refs) or _may_have_image_attachment(
        raw, content_type, formatted_text
    )
    image_count = len(image_md5_refs) if image_md5_refs else int(has_image_attachment)
    images = [
        {
            "filename": "",
            "mimeType": "image/jpeg",
            "errorCode": "IMAGE_RESOLUTION_PENDING",
        }
        for _ in range(image_count)
    ]
    text = (
        _clean_message_text(formatted_text)
        if has_image_attachment
        else formatted_text.strip()
    )
    if not text and (content_type in {4, 123} or images):
        text = "[图片]"

    text = sanitize_message_text(text, content_type)

    return {
        "messageId": str(formatted.get("message_id") or ""),
        "serverId": str(formatted.get("server_id") or ""),
        "sequence": int(formatted.get("sequence") or 0),
        "sendTime": int(formatted.get("send_time") or 0),
        "groupId": group_id,
        "senderId": str(sender_id),
        "senderName": str(identity.get("display_name") or formatted.get("sender") or sender_id),
        "account": str(identity.get("account") or ""),
        "mobile": str(identity.get("mobile") or ""),
        "contentType": _content_kind(content_type, has_image_attachment),
        "text": text,
        "images": images,
        **({"imageMd5Refs": image_md5_refs} if image_md5_refs else {}),
    }


def _image_md5_refs(raw: dict, content_type: int) -> list[str]:
    # Single implementation shared with the daily pipeline.
    from worker.wecom.media import extract_image_md5_refs

    return extract_image_md5_refs(content_type, raw)


def _content_kind(content_type: int, has_image_attachment: bool) -> str:
    if content_type in {4, 123} or (
        content_type in {14, 15, 23, 1011} and has_image_attachment
    ):
        return "image"
    if content_type == 29:
        return "link"
    if content_type == 7:
        return "voice"
    if content_type in {14, 15, 23}:
        return "file"
    if content_type == 38:
        return "app"
    if content_type in {0, 1, 2}:
        return "text"
    return f"unsupported:{content_type}"


def sanitize_message_text(text: str, content_type: int = 0) -> str:
    """Make chat text safe to display and to feed into a model prompt.

    WeCom application messages (content type 38) arrive as a JSON envelope
    instead of words. Showing that raw JSON produced unreadable bubbles in the
    high-EQ reply page, so app payloads are reduced to their readable fields.
    Control and zero-width characters from protobuf payloads are removed too;
    they used to collapse messages into blank-looking rows.
    """

    cleaned = _CONTROL_CHAR_RE.sub("", _ZERO_WIDTH_RE.sub("", str(text or "")))
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if not cleaned:
        return ""
    if content_type in _APP_CONTENT_TYPES or _looks_like_app_payload(cleaned):
        summary = summarize_app_message(cleaned)
        if summary:
            return summary
    return cleaned


def _looks_like_app_payload(text: str) -> bool:
    value = str(text or "").lstrip("\ufeff \t\r\n")
    if not value.startswith(("{", "[")):
        return False
    if len(value) < 24:
        return False
    if any(hint in value for hint in _APP_PAYLOAD_HINTS):
        return True
    # JSON-looking long text with known card fields.
    return bool(re.search(r'"(?:title|content|desc|display|summary)"\s*:', value))


def summarize_app_message(text: str) -> str:
    """Extract readable labels from a WeCom app/card JSON payload."""

    raw = str(text or "").strip()
    if not raw:
        return ""
    values: list[str] = []
    payload = None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        values = _quoted_json_values(raw)
    if payload is not None:
        _collect_app_strings(payload, values, depth=0)

    readable: list[str] = []
    total = 0
    for value in values:
        candidate = _clean_app_value(value)
        if (
            not candidate
            or candidate in readable
            or _is_technical_app_value(candidate)
        ):
            continue
        readable.append(candidate)
        total += len(candidate)
        if len(readable) >= 5 or total >= 300:
            break
    if not readable:
        return "[应用消息]" if _looks_like_json(raw) else ""
    return " · ".join(readable)[:_MAX_SUMMARY_CHARS]


def _looks_like_json(text: str) -> bool:
    value = str(text or "").lstrip("\ufeff \t\r\n")
    return value.startswith(("{", "["))


def _collect_app_strings(value, out: list[str], depth: int) -> None:
    if depth > 4 or len(out) >= 24:
        return
    if isinstance(value, str):
        out.append(value)
        return
    if isinstance(value, dict):
        for key in _PRIORITY_APP_KEYS:
            if key in value:
                _collect_app_strings(value[key], out, depth + 1)
        for key, item in value.items():
            if key in _PRIORITY_APP_KEYS:
                continue
            _collect_app_strings(item, out, depth + 1)
        return
    if isinstance(value, list):
        for item in value[:8]:
            _collect_app_strings(item, out, depth + 1)


def _quoted_json_values(text: str) -> list[str]:
    values = []
    for _, value in re.findall(r'"([A-Za-z0-9_\-]+)"\s*:\s*"((?:[^"\\]|\\.){0,200})"', text):
        values.append(value)
    for value in re.findall(r'"([^"\\]{2,200})"', text):
        values.append(value)
    return values


def _clean_app_value(value: str) -> str:
    candidate = _CONTROL_CHAR_RE.sub("", str(value or "")).strip()
    if "\\u" in candidate:
        try:
            candidate = candidate.encode("utf-8").decode("unicode_escape")
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
    return candidate.strip()


def _is_technical_app_value(value: str) -> bool:
    lower = value.lower()
    if len(value) <= 1 or lower in {"0", "1", "true", "false", "null", "yes", "no"}:
        return True
    if value.startswith(("http://", "https://", "data:", "icon_", "icons", "source_")):
        return True
    if re.fullmatch(r"[0-9a-fA-F_\-]{16,}", value):
        return True
    if _TECHNICAL_VALUE_RE.fullmatch(value) and not re.search(r"[\u4e00-\u9fff]", value):
        return True
    return False



def _may_have_image_attachment(raw: dict, content_type: int, text: str) -> bool:
    if content_type in {4, 123}:
        return True
    if content_type in {14, 15, 23}:
        return _raw_fields_contain_image_filename(raw)
    if content_type != 2:
        return False
    value = str(text or "")
    return bool(
        _IMAGE_PLACEHOLDER_RE.search(value)
        or any(
            _IMAGE_FILENAME_LINE_RE.fullmatch(line.strip())
            for line in value.splitlines()
        )
    )


def _raw_fields_contain_image_filename(raw: dict) -> bool:
    for field in _IMAGE_REFERENCE_FIELDS:
        value = raw.get(field)
        if isinstance(value, memoryview):
            value = value.tobytes()
        if isinstance(value, (bytes, bytearray)):
            text = bytes(value[:_MAX_IMAGE_REF_SCAN_BYTES]).decode(
                "utf-8", errors="ignore"
            )
        elif isinstance(value, str):
            text = value[:_MAX_IMAGE_REF_SCAN_BYTES]
        else:
            continue
        if _IMAGE_FILENAME_SUFFIX_RE.search(text):
            return True
    return False


def _clean_message_text(text: str) -> str:
    """Remove attachment decorations while preserving the user's actual words."""

    cleaned = _IMAGE_PLACEHOLDER_RE.sub("", str(text or ""))
    cleaned = _BINARY_PLACEHOLDER_RE.sub("", cleaned)
    lines = []
    for value in cleaned.splitlines():
        line = value.strip()
        if (
            not line
            or _IMAGE_FILENAME_LINE_RE.fullmatch(line)
            or _FILE_PLACEHOLDER_RE.fullmatch(line)
        ):
            continue
        lines.append(line)
    return "\n".join(lines).strip()
