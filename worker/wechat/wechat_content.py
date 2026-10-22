from __future__ import annotations

import re
import xml.etree.ElementTree as ET

try:  # zstandard is only needed for WeChat 4.x compressed message content.
    import zstandard as zstd
except ImportError:  # pragma: no cover - reported through RuntimeError on use
    zstd = None


_DECOMPRESSOR = zstd.ZstdDecompressor() if zstd is not None else None
_UNSAFE_XML = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)


MESSAGE_LABELS = {
    1: "text",
    3: "image",
    34: "voice",
    43: "video",
    47: "image",
    49: "link",
    10000: "system",
    10002: "system",
}


def decode_content(raw, compression_flag: int = 0) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        if compression_flag == 4:
            if _DECOMPRESSOR is None:
                raise RuntimeError("解压微信压缩消息需要 zstandard 依赖")
            try:
                return _DECOMPRESSOR.decompress(raw, max_output_size=8 * 1024 * 1024).decode("utf-8", errors="replace")
            except zstd.ZstdError:
                return ""
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def split_group_sender(content: str, is_group: bool) -> tuple[str, str]:
    if not is_group:
        return "", content
    if ":\n" in content:
        possible, body = content.split(":\n", 1)
        if re.fullmatch(r"[A-Za-z0-9_.@-]{2,160}", possible):
            return possible, body
    match = re.match(r"^([A-Za-z0-9_.@-]{2,160}):(?=<\?xml|<msg|<sysmsg)", content)
    if match:
        return match.group(1), content[match.end() :]
    return "", content


def parse_message(local_type: int, content: str, account_id: str) -> tuple[str, str, dict | None, list[dict], bool]:
    base_type = int(local_type) & 0xFFFFFFFF
    kind = MESSAGE_LABELS.get(base_type, "unknown")
    quote = None
    media: list[dict] = []
    mentioned_me = False
    parsed_xml = _safe_xml(content) if content.lstrip().startswith("<") else None
    if parsed_xml is not None:
        at_users = _find_text(parsed_xml, "atuserlist")
        mentioned_me = bool(
            account_id
            and account_id in {item.strip() for item in at_users.split(",") if item.strip()}
        )

    if base_type == 3:
        return "image", "[图片]", None, [{"kind": "image"}], mentioned_me
    if base_type == 34:
        return "voice", "[语音]", None, [{"kind": "voice"}], mentioned_me
    if base_type == 43:
        return "video", "[视频]", None, [{"kind": "video"}], mentioned_me
    if base_type == 47:
        return "image", "[表情]", None, [{"kind": "sticker"}], mentioned_me
    if base_type in {10000, 10002}:
        return "system", _plain_text(content)[:500], None, [], False
    if base_type != 49:
        return kind, content, quote, media, mentioned_me

    root = parsed_xml if parsed_xml is not None else _safe_xml(content)
    if root is None:
        return "link", "[链接/文件]", None, [], False
    title = _find_text(root, "title")
    url = _find_text(root, "url")
    app_type = _find_text(root, "type")
    refer = root.find(".//refermsg")
    if refer is not None:
        quote = {
            "sender": _find_text(refer, "displayname") or _find_text(refer, "fromusr"),
            "text": _quote_text(_find_text(refer, "type"), _find_element_text(refer, "content")),
        }
        text = title or _find_text(root, "des") or "[引用消息]"
        return "quote", text, quote, media, mentioned_me
    if app_type in {"6", "74"}:
        media.append({"kind": "file", "name": title})
        return "file", f"[文件] {title}".strip(), None, media, mentioned_me
    label = title or url or "链接/小程序"
    return "link", f"[分享] {label}", None, ([{"kind": "link", "url": url}] if url else []), mentioned_me


def _safe_xml(content: str):
    if not content or len(content) > 800_000 or _UNSAFE_XML.search(content):
        return None
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        return None


def _find_text(root, tag: str) -> str:
    node = root.find(f".//{tag}")
    return node.text.strip() if node is not None and node.text else ""


def _find_element_text(root, tag: str) -> str:
    """Return text or serialized child XML for tags whose payload is nested."""
    node = root.find(f".//{tag}")
    if node is None:
        return ""
    if node.text and node.text.strip():
        return node.text.strip()
    return "".join(ET.tostring(child, encoding="unicode") for child in node).strip()


_QUOTE_PLACEHOLDERS = {
    "3": "[图片]",
    "47": "[表情]",
    "34": "[语音]",
    "43": "[视频]",
    "10000": "[系统消息]",
    "10002": "[系统消息]",
}


def _quote_text(refer_type: str, content: str) -> str:
    """Render the quoted message without leaking raw XML into the UI."""
    kind = (refer_type or "").strip()
    if kind in _QUOTE_PLACEHOLDERS:
        return _QUOTE_PLACEHOLDERS[kind]
    value = (content or "").strip()
    if not value:
        return "[消息]"
    if value.startswith("<") or "<img" in value or "<msg" in value:
        parsed = _safe_xml(value)
        if parsed is not None:
            title = _find_text(parsed, "title") or _find_text(parsed, "des")
            if title:
                return title[:2000]
            if parsed.find(".//img") is not None:
                return "[图片]"
            if parsed.find(".//voice") is not None:
                return "[语音]"
            if parsed.find(".//video") is not None:
                return "[视频]"
        plain = _plain_text(value)
        if plain and not plain.lstrip().startswith("<"):
            return plain[:2000]
        return "[非文本消息]"
    return value[:2000]


def _plain_text(content: str) -> str:
    return re.sub(r"<[^>]{1,200}>", "", content or "").strip()
