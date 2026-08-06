from __future__ import annotations

import re


_IMAGE_REFERENCE_CONTENT_TYPES = {2, 123, 1011}
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
# 32-byte ASCII hex value. Other fields also carry unrelated 32-byte values,
# so a generic hex scan would produce false attachments.
_IMAGE_MD5_REF_RE = re.compile(rb"\x52\x20([0-9A-Fa-f]{32})")
_MAX_IMAGE_REF_SCAN_BYTES = 1_048_576
_MAX_IMAGE_MD5_REFS = 64


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
    if content_type not in _IMAGE_REFERENCE_CONTENT_TYPES:
        return []
    result = []
    seen = set()
    for field in _IMAGE_REFERENCE_FIELDS:
        for ref in _extract_image_md5_refs(raw.get(field)):
            if ref in seen:
                continue
            seen.add(ref)
            result.append(ref)
            if len(result) >= _MAX_IMAGE_MD5_REFS:
                return result
    return result


def _extract_image_md5_refs(raw) -> list[str]:
    if isinstance(raw, str):
        value = raw.encode("ascii", errors="ignore")
    elif isinstance(raw, memoryview):
        value = raw.tobytes()
    elif isinstance(raw, (bytes, bytearray)):
        value = bytes(raw)
    else:
        return []
    result = []
    seen = set()
    for match in _IMAGE_MD5_REF_RE.finditer(value[:_MAX_IMAGE_REF_SCAN_BYTES]):
        ref = match.group(1).decode("ascii").lower()
        if ref in seen:
            continue
        seen.add(ref)
        result.append(ref)
        if len(result) >= _MAX_IMAGE_MD5_REFS:
            break
    return result


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
    if content_type in {0, 1, 2}:
        return "text"
    return f"unsupported:{content_type}"


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
