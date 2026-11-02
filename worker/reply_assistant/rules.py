"""Deterministic Chinese fallbacks for ``eq_reply``.

These rules are used when no model is configured or the model answer cannot be
parsed. They always return the full response shape (styles / interpretation /
check) so the UI never shows an error.
"""

from __future__ import annotations

import re

STYLE_DEFINITIONS = (
    ("steady", "稳重版"),
    ("warm", "亲和版"),
    ("brief", "简短版"),
)

_SIGNAL_RULES = (
    ("情绪信号", ("急", "催", "投诉", "不满", "生气", "失望", "抱歉", "感谢")),
    ("时间节点", ("今天", "明天", "后天", "尽快", "下班前", "本周", "下周", "月底", "马上", "立即")),
    ("询问点", ("?", "？", "吗", "呢", "什么时候", "何时", "能否", "是否", "如何", "怎么")),
    ("明确诉求", ("请", "麻烦", "需要", "希望", "帮忙", "协助", "支持")),
    ("潜在风险", ("风险", "延期", "故障", "异常", "投诉", "赔付", "退款", "预算", "合规")),
)


def _clip(value: object, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _address(recipient: str) -> str:
    name = str(recipient or "").strip()
    if not name:
        return ""
    return f"{name}，"


def _matched_keywords(text: str, keywords: tuple[str, ...]) -> list[str]:
    return [keyword for keyword in keywords if keyword.lower() in text.lower()]


def interpret(text: str, context: str = "", recipient: str = "") -> dict:
    """Rule-based reading of the incoming message and the expected reply."""
    value = str(text or "").strip()
    summary_source = re.split(r"[。！？!?]", value, maxsplit=1)[0].strip() or value
    signals: list[dict] = []
    if recipient.strip():
        signals.append({"label": "沟通对象", "detail": recipient.strip()})
    for label, keywords in _SIGNAL_RULES:
        matched = _matched_keywords(value, keywords)
        if matched:
            signals.append({"label": label, "detail": "提到：" + "、".join(matched[:5])})
    if context.strip():
        signals.append({"label": "上下文", "detail": _clip(context, 80)})
    if not signals:
        signals.append(
            {
                "label": "未识别到明确诉求",
                "detail": "建议先确认对方的核心问题、期望结果与时间要求。",
            }
        )
    return {"summary": _clip(summary_source, 100) or "对方发来了一段需要回复的消息。", "signals": signals}


def check_reply(text: str, recipient: str = "") -> dict:
    """Rule-based risk/suggestion checklist for the reply."""
    value = str(text or "")
    risks: list[str] = []
    suggestions: list[str] = []
    if _matched_keywords(value, ("今天", "明天", "尽快", "下班前", "马上", "立即", "本周", "下周")):
        risks.append("时间较紧，不要承诺无法保证的完成时间，先确认后再给时间点。")
        suggestions.append("先给出能确认的下一步，再说明预计回复时间。")
    if _matched_keywords(value, ("赔付", "退款", "赔偿", "预算", "合规")):
        risks.append("涉及赔付、退款或权限问题，先确认政策与授权再回复。")
        suggestions.append("如需内部审批，先回复已收到并说明审核进度。")
    if _matched_keywords(value, ("投诉", "不满", "生气", "失望", "催")):
        risks.append("对方情绪较强，先回应情绪与影响，再给事实和处理方案。")
        suggestions.append("先表达理解并复述影响，避免辩解式表达。")
    if _matched_keywords(value, ("数据", "上线", "故障", "延期", "异常", "回滚")):
        risks.append("先核实当前状态和影响范围，避免复述未经确认的结论。")
        suggestions.append("回复中用事实描述现状，并标明需要确认的信息。")
    if any(keyword in recipient for keyword in ("总", "领导", "老板", "客户")):
        risks.append("面向领导或客户，先给结论和明确下一步，避免铺垫过长。")
        suggestions.append("按“结论—原因—下一步”的顺序组织回复。")
    risks.append("避免在没有依据时承诺结果，或替其他团队表态。")
    suggestions.append("先复述对方诉求并确认理解一致。")
    suggestions.append("保持简洁，避免过度道歉或情绪化表达。")
    deduped_risks = list(dict.fromkeys(risks))[:5]
    deduped_suggestions = list(dict.fromkeys(suggestions))[:5]
    return {"risks": deduped_risks, "suggestions": deduped_suggestions}


def static_styles(text: str, context: str = "", recipient: str = "") -> list[dict]:
    address = _address(recipient)
    return [
        {
            "id": "steady",
            "title": "稳重版",
            "content": (
                f"{address}收到，我先确认一下具体情况，今天内给您一个明确答复；"
                "如有风险我会提前同步，避免影响您的安排。"
            ),
        },
        {
            "id": "warm",
            "title": "亲和版",
            "content": (
                f"{address}感谢您的提醒，我理解这件事比较着急。我马上跟进，"
                "有进展第一时间同步您，也感谢您的耐心等待。"
            ),
        },
        {
            "id": "brief",
            "title": "简短版",
            "content": f"{address}收到，我来跟进，稍后回复您。",
        },
    ]
