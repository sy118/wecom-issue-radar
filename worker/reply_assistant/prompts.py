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


def _prompt_items(config: dict | None) -> list:
    if not isinstance(config, dict):
        return []
    prompt_config = config.get("prompts")
    if not isinstance(prompt_config, dict):
        return []
    items = prompt_config.get("items")
    return items if isinstance(items, list) else []


def configured_eq_reply_prompt(config: dict | None) -> str:
    """Return the first editable ``eq_reply`` prompt content, or ``""``."""
    for item in _prompt_items(config):
        if not isinstance(item, dict):
            continue
        if str(item.get("scene") or "").strip() != EQ_REPLY_SCENE:
            continue
        return str(item.get("content") or "").strip()
    return ""


def eq_reply_system_prompt(config: dict | None = None) -> str:
    """Build the system prompt, preferring the editable prompt library entry."""
    content = configured_eq_reply_prompt(config)
    if not content:
        return EQ_REPLY_SYSTEM
    if all(marker in content for marker in _CONTRACT_MARKERS):
        # The edited prompt already spells out the JSON contract; use it as is.
        return content
    return f"{content}\n\n{EQ_REPLY_OUTPUT_REQUIREMENT}"


def eq_reply_prompt(
    config: dict | None,
    text: str,
    context: str = "",
    recipient: str = "",
) -> tuple[str, str]:
    parts = [f"需要回复的消息：\n{text.strip()}"]
    if recipient.strip():
        parts.append(f"沟通对象：{recipient.strip()}")
    if context.strip():
        parts.append(f"相关上下文：\n{context.strip()[:4000]}")
    parts.append("请按要求只返回 JSON 对象。")
    return eq_reply_system_prompt(config), "\n\n".join(parts)
