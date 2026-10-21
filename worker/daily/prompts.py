"""Prompt copy and static fallbacks for the daily assistant.

Every model-facing helper has a deterministic Chinese fallback so the UI keeps
working when no model is configured, the network is down or the answer cannot be
parsed.
"""

from __future__ import annotations

EQ_REPLY_SYSTEM = (
    "你是企业沟通顾问。根据用户提供的消息和上下文，写三条可以直接发送的中文回复草稿："
    "steady（稳重、专业、给出明确下一步）、warm（亲和、先共情再回应）、"
    "brief（简短、只保留必要信息）。不要编造事实，不要承诺做不到的事，不要使用表情符号。"
    "只返回 JSON 对象：{\"steady\": \"...\", \"warm\": \"...\", \"brief\": \"...\"}。"
)

EQ_REPLY_STYLES = (
    ("steady", "稳重版", "收到，我先核对一下具体情况，确认后尽快给你明确答复。"),
    ("warm", "亲和版", "谢谢你告诉我这件事，我理解你的顾虑。我马上跟进，有进展会第一时间回复你。"),
    ("brief", "简短版", "收到，我来跟进，稍后回复你。"),
)

WEEKLY_SYSTEM = (
    "你是产品与研发团队的周报助手。请根据给定时间范围内的聊天记录，写一份中文周报草稿："
    "包含「本周进展」「问题与风险」「下周计划」三部分，按项目/会话归纳，"
    "只使用材料中出现的事实，不要编造；控制在 800 字以内，使用 Markdown 标题与列表。"
)


def static_eq_replies(text: str, context: str = "") -> list[dict]:
    """Deterministic three-style reply drafts used when the model is unavailable."""
    styles = []
    for style_id, title, content in EQ_REPLY_STYLES:
        styles.append({"id": style_id, "title": title, "content": content})
    return styles


def eq_reply_prompt(text: str, context: str = "") -> tuple[str, str]:
    user_parts = [f"需要回复的消息：\n{text}"]
    if context.strip():
        user_parts.append(f"相关上下文：\n{context.strip()[:4000]}")
    user_parts.append("请按要求只返回 JSON 对象。")
    return EQ_REPLY_SYSTEM, "\n\n".join(user_parts)


def build_weekly_digest(messages: list[dict], *, max_messages: int = 400) -> str:
    lines = []
    for message in messages[:max_messages]:
        conversation = str(message.get("conv_name") or message.get("conv_id") or "")
        sender = str(message.get("sender_name") or "")
        text = str(message.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"[{conversation}] {sender}: {text[:300]}")
    digest = "\n".join(lines)
    return digest[:16000]


def weekly_prompt(
    start_date: str, end_date: str, messages: list[dict], stats: dict
) -> tuple[str, str]:
    digest = build_weekly_digest(messages)
    user = (
        f"时间范围：{start_date} ~ {end_date}\n"
        f"消息数：{stats.get('messages', 0)}，会话数：{stats.get('conversations', 0)}\n\n"
        f"聊天记录（按时间排序）：\n{digest or '（无文本内容）'}"
    )
    return WEEKLY_SYSTEM, user


def empty_week_draft(start_date: str, end_date: str) -> str:
    return (
        f"所选时间范围（{start_date} ~ {end_date}）内没有可汇总的消息，"
        "请更换日期范围或数据来源后再生成周报。"
    )


def weekly_fallback_draft(
    start_date: str,
    end_date: str,
    stats: dict,
    conversations: list[tuple[str, int]],
) -> str:
    lines = [
        f"# 周报（{start_date} ~ {end_date}）",
        "",
        "## 数据范围",
        f"- 消息：{stats.get('messages', 0)} 条",
        f"- 会话：{stats.get('conversations', 0)} 个",
        "",
        "## 本周进展",
        "（模型暂不可用，已生成基础统计草稿；可稍后重新生成或手动补充。）",
    ]
    if conversations:
        lines.append("")
        lines.append("## 主要会话")
        for name, count in conversations[:10]:
            lines.append(f"- {name}：{count} 条消息")
    lines.extend(
        [
            "",
            "## 问题与风险",
            "- 待补充",
            "",
            "## 下周计划",
            "- 待补充",
        ]
    )
    return "\n".join(lines)
