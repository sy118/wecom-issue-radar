"""Prompt copy for the daily assistant (eq_reply / summarize)."""

from __future__ import annotations

EQ_REPLY_SYSTEM = (
    "你是企业沟通顾问。根据用户提供的消息、上下文和沟通对象，完成两件事："
    "1）写出三条可直接发送的中文回复草稿：steady（稳重、专业、给出明确下一步）、"
    "warm（亲和、先共情再回应）、brief（简短、只保留必要信息）；"
    "2）解读这条消息并给出回复前的检查清单。不要编造事实，不要承诺做不到的事，不要使用表情符号。"
    "只返回 JSON 对象："
    '{"styles":{"steady":"...","warm":"...","brief":"..."},'
    '"interpretation":{"summary":"...","signals":[{"label":"...","detail":"..."}]},'
    '"check":{"risks":["..."],"suggestions":["..."]}}。'
)

SUMMARIZE_SYSTEM = (
    "你是中文信息整理助手。请把用户提供的聊天/文本整理成结构化结果："
    "summary（一段不超过 120 字的摘要）、points（要点数组）、"
    "risks（风险或问题数组，没有则为空数组）、next（下一步动作数组，没有则为空数组）。"
    "只使用材料中出现的事实，不要编造；只返回 JSON 对象："
    '{"summary":"...","points":["..."],"risks":["..."],"next":["..."]}。'
)

SUMMARIZE_MODE_HINTS = {
    "digest": "任务类型：速读摘要，突出结论、关键信息与需要跟进的事项。",
    "meeting": "任务类型：会议纪要，突出讨论结论、待办责任人与截止时间、未决问题。",
}


def eq_reply_prompt(text: str, context: str = "", recipient: str = "") -> tuple[str, str]:
    parts = [f"需要回复的消息：\n{text.strip()}"]
    if recipient.strip():
        parts.append(f"沟通对象：{recipient.strip()}")
    if context.strip():
        parts.append(f"相关上下文：\n{context.strip()[:4000]}")
    parts.append("请按要求只返回 JSON 对象。")
    return EQ_REPLY_SYSTEM, "\n\n".join(parts)


def summarize_prompt(text: str, mode: str) -> tuple[str, str]:
    hint = SUMMARIZE_MODE_HINTS.get(mode, SUMMARIZE_MODE_HINTS["digest"])
    user = f"{hint}\n\n材料：\n{str(text).strip()[:16000]}\n\n请按要求只返回 JSON 对象。"
    return SUMMARIZE_SYSTEM, user
