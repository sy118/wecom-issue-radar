"""Prompt copy for the reply assistant (``eq_reply``).

The system prompt is editable from the prompt library: the first
``config.prompts.items`` entry whose ``scene`` is ``eq_reply`` wins. Whatever the
user wrote, the fixed JSON output contract (styles / interpretation / check) is
kept so the renderer can always parse the model answer.
"""

from __future__ import annotations

EQ_REPLY_SCENE = "eq_reply"

EQ_REPLY_JSON_CONTRACT = (
    "只返回 JSON 对象："
    '{"styles":{"steady":"...","warm":"...","brief":"..."},'
    '"interpretation":{"summary":"...","signals":[{"label":"...","detail":"..."}]},'
    '"check":{"risks":["..."],"suggestions":["..."]}}。'
)

EQ_REPLY_INSTRUCTIONS = (
    "你是企业沟通顾问。根据用户提供的消息、上下文和沟通对象，完成两件事："
    "1）写出三条可直接发送的中文回复草稿：steady（稳重、专业、给出明确下一步）、"
    "warm（亲和、先共情再回应）、brief（简短、只保留必要信息）；"
    "2）解读这条消息并给出回复前的检查清单。不要编造事实，不要承诺做不到的事，不要使用表情符号。"
)

# Built-in system prompt used when the prompt library has no editable content for
# the scene. It keeps the historical instructions and output contract.
EQ_REPLY_SYSTEM = EQ_REPLY_INSTRUCTIONS + EQ_REPLY_JSON_CONTRACT

# Appended to an edited prompt so the three reply variants and the JSON shape
# stay stable no matter what the user wrote.
EQ_REPLY_OUTPUT_REQUIREMENT = (
    "请写出三条可直接发送的中文回复草稿：steady（稳重、专业、给出明确下一步）、"
    "warm（亲和、先共情再回应）、brief（简短、只保留必要信息），"
    "并给出潜台词解读与回复前的检查清单。" + EQ_REPLY_JSON_CONTRACT
)

_CONTRACT_MARKERS = ('"styles"', '"interpretation"', '"check"')
MAX_PROMPT_NOTE_CHARS = 500
MAX_REPLY_INSTRUCTION_CHARS = 500
MAX_CONTEXT_PROMPT_CHARS = 3000


def _prompt_items(config: dict | None) -> list:
    if not isinstance(config, dict):
        return []
    prompt_config = config.get("prompts")
    if not isinstance(prompt_config, dict):
        return []
    items = prompt_config.get("items")
    return items if isinstance(items, list) else []


def _eq_reply_items(config: dict | None) -> list[dict]:
    return [
        item
        for item in _prompt_items(config)
        if isinstance(item, dict)
        and str(item.get("scene") or "").strip() == EQ_REPLY_SCENE
    ]


def _item_id(item: dict | None) -> str:
    return str(item.get("id") or "").strip() if isinstance(item, dict) else ""


def _item_content(item: dict | None) -> str:
    return str(item.get("content") or "").strip() if isinstance(item, dict) else ""


def resolve_eq_reply_prompt(
    config: dict | None, prompt_id: str = ""
) -> tuple[str, str, bool]:
    """Return ``(prompt id used, content, requested prompt missing)``.

    An exact ``id`` match in the ``eq_reply`` scene wins; a missing or deleted
    binding falls back to the first ``eq_reply`` entry (existing behaviour).
    """

    items = _eq_reply_items(config)
    requested = str(prompt_id or "").strip()
    if requested:
        for item in items:
            if _item_id(item) == requested:
                return requested, _item_content(item), False
    default = items[0] if items else None
    return _item_id(default), _item_content(default), bool(requested)


def configured_eq_reply_prompt(config: dict | None, prompt_id: str = "") -> str:
    """Return the editable ``eq_reply`` prompt content (bound id first)."""

    _used_id, content, _fallback = resolve_eq_reply_prompt(config, prompt_id)
    return content


def eq_reply_system_prompt(
    config: dict | None = None,
    prompt_id: str = "",
    prompt_note: str = "",
) -> str:
    """Build the system prompt, preferring the bound/editable prompt library entry."""

    content = configured_eq_reply_prompt(config, prompt_id)
    if not content:
        base = EQ_REPLY_SYSTEM
    elif all(marker in content for marker in _CONTRACT_MARKERS):
        # The edited prompt already spells out the JSON contract; use it as is.
        base = content
    else:
        base = f"{content}\n\n{EQ_REPLY_OUTPUT_REQUIREMENT}"
    note = str(prompt_note or "").strip()
    if len(note) > MAX_PROMPT_NOTE_CHARS:
        note = note[:MAX_PROMPT_NOTE_CHARS].rstrip()
    if note:
        base = f"{base}\n\n本群角色补充：{note}"
    return base


def _focus_message_line(item: dict) -> str:
    """One ``- [timeLabel] sender：text`` line for a multi-message focus list."""

    text = str(item.get("text") or "").strip()
    if not text:
        return ""
    sender = str(item.get("senderName") or "").strip()
    time_label = str(item.get("timeLabel") or "").strip()
    prefix = f"[{time_label}] " if time_label else ""
    if sender:
        return f"- {prefix}{sender}：{text}"
    return f"- {prefix}{text}"


def eq_reply_prompt(
    config: dict | None,
    text: str,
    context: str = "",
    recipient: str = "",
    image_count: int = 0,
    focus_messages: list | None = None,
    prompt_id: str = "",
    prompt_note: str = "",
    reply_instruction: str = "",
) -> tuple[str, str]:
    focus_lines = [
        line
        for line in (
            _focus_message_line(item)
            for item in (focus_messages or [])
            if isinstance(item, dict)
        )
        if line
    ]
    parts = []
    if focus_lines:
        parts.append(
            f"需要回复的消息（共 {len(focus_lines)} 条）：\n" + "\n".join(focus_lines)
        )
        parts.append("请综合回应以上消息，不要逐条机械回复。")
    else:
        stripped = text.strip()
        if stripped:
            parts.append(f"需要回复的消息：\n{stripped}")
        elif context.strip():
            # No explicit target (0 selected messages): reply against the group
            # context without inventing anything that is not there.
            parts.append(
                "需要回复的消息：未指定；请根据以下群聊上下文判断需要回应什么，"
                "综合给出得体回复，不要臆测上下文里没有的信息。"
            )
        else:
            parts.append("需要回复的消息：\n")
    instruction = str(reply_instruction or "").strip()
    if len(instruction) > MAX_REPLY_INSTRUCTION_CHARS:
        instruction = instruction[:MAX_REPLY_INSTRUCTION_CHARS].rstrip()
    if instruction:
        parts.append(f"本次回复的补充要求：\n{instruction}")
    if recipient.strip():
        parts.append(f"沟通对象：{recipient.strip()}")
    if context.strip():
        parts.append(
            f"相关上下文：\n{context.strip()[:MAX_CONTEXT_PROMPT_CHARS]}"
        )
    parts.append("请按要求只返回 JSON 对象。")
    if image_count > 0:
        parts.append(
            "本轮附带 {image_count} 张聊天图片，请结合图片内容理解对方消息；"
            "不要编造图片里没有的信息。".format(image_count=image_count)
        )
    return (
        eq_reply_system_prompt(
            config, prompt_id=prompt_id, prompt_note=prompt_note
        ),
        "\n\n".join(parts),
    )
