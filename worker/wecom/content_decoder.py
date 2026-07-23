"""
消息内容解码器（从 wechat-decrypt/export_wxwork_messages.py 提取）。

纯解析函数，无 I/O，无副作用。
"""
import re


def _read_varint(data, pos):
    value = 0
    shift = 0
    while pos < len(data) and shift < 64:
        b = data[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            return value, pos
        shift += 7
    raise ValueError("bad varint")


def _clean_text(text):
    text = "".join(
        ch if ch in "\n\t" or (ch.isprintable() and ch not in "\x0b\x0c") else " "
        for ch in text
    )
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _looks_like_plain_text(data, text):
    if not text:
        return False
    control = sum(1 for b in data if b < 32 and b not in (9, 10, 13))
    if control / max(len(data), 1) > 0.08:
        return False
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\n\t")
    return printable / max(len(text), 1) > 0.9


# 常见的 protobuf wire-type-2 标签字节（field_number << 3 | 2）
_PROTOBUF_TAG_BYTES = frozenset({0x0A, 0x12, 0x1A, 0x22, 0x2A, 0x32, 0x3A, 0x42, 0x4A})


def _decode_text_segment(segment):
    """尝试将 segment 解码为纯文本。如果看起来仍像嵌套 protobuf，返回 None。"""
    if not segment or b"\x00" in segment:
        return None

    # 如果段首是 protobuf wire-type-2 标签，很可能是嵌套消息，让调用方递归解析
    if segment[0] in _PROTOBUF_TAG_BYTES and len(segment) >= 2:
        # 第二字节若是 varint 长度 (< 0x80)，确认为 protobuf 结构
        if segment[1] < 0x80:
            return None

    try:
        text = segment.decode("utf-8")
    except UnicodeDecodeError:
        return None
    text = _clean_text(text)
    if len(text) < 2:
        return None
    if re.fullmatch(r"[0-9a-fA-F]{32,}", text):
        return None
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\n\t")
    if printable / max(len(text), 1) < 0.9:
        return None
    return text


def _parse_protobuf_strings(data, depth=0):
    if depth > 4 or not data:
        return []
    pos = 0
    out = []
    fields = 0
    try:
        while pos < len(data):
            tag, pos = _read_varint(data, pos)
            if tag == 0:
                return []
            wire = tag & 7
            fields += 1
            if wire == 0:
                _, pos = _read_varint(data, pos)
            elif wire == 1:
                pos += 8
            elif wire == 5:
                pos += 4
            elif wire == 2:
                length, pos = _read_varint(data, pos)
                if length < 0 or pos + length > len(data):
                    return []
                segment = data[pos:pos + length]
                pos += length
                text = _decode_text_segment(segment)
                if text:
                    out.append(text)
                else:
                    out.extend(_parse_protobuf_strings(segment, depth + 1))
            else:
                return []
            if pos > len(data):
                return []
    except Exception:
        return []
    return out if fields else []


def _dedupe_texts(values):
    seen = set()
    out = []
    for value in values:
        value = _clean_text(value)
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def decode_content(raw):
    """将企微消息的二进制内容解码为可读文本。

    策略：
      1. protobuf 解析 — 正确提取嵌套结构中的文本片段
      2. 直接 UTF-8 解码（跳过 protobuf 头部控制字节）— 回退
      3. 其他编码尝试
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return _clean_text(raw)
    data = bytes(raw)
    if not data:
        return ""

    # ① 优先 protobuf 解析 — 正确处理嵌套结构
    texts = _dedupe_texts(_parse_protobuf_strings(data))
    if texts:
        meaningful = [t for t in texts if len(t) >= 3]
        if meaningful:
            longest = max(meaningful, key=len)
            others = [t for t in meaningful if t != longest and len(t) >= 3]
            if others:
                return _clean_text("\n".join([longest] + others))
            return _clean_text(longest)

    # ② 直接 UTF-8 解码（跳过开头少量控制字节）— 回退策略
    for skip in (0, 6, 8, 10, 12):
        try:
            plain = data[skip:].decode("utf-8")
            if _looks_like_plain_text(data[skip:], plain):
                return _clean_text(plain)
        except UnicodeDecodeError:
            continue

    # ③ 回退编码尝试。不要宽松尝试 utf-16le：企微媒体/文件消息中的二进制片段
    # 很容易被误解码成可打印的韩文或 CJK 兼容字符，造成终端乱码。
    for enc in ("utf-8", "gbk"):
        try:
            text = _clean_text(data.decode(enc, errors="replace"))
            if text and "�" not in text[:20]:
                return text[:2000]
        except Exception:
            continue
    return f"[二进制内容 {len(data)} 字节]"
