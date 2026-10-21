"""Heuristic to-do / commitment extraction from indexed chat messages.

The rules are intentionally conservative: a candidate is only auto-created when
both the intent (a request directed at me, or a promise I made) and actionable
wording are present. Ambiguous candidates are returned for confirmation with a
lower confidence instead of silently becoming reminders.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from worker.search.text import timezone_for

AUTO_CREATE_CONFIDENCE = 0.65
MIN_CANDIDATE_CONFIDENCE = 0.35

REQUEST_STRONG = (
    "麻烦你",
    "麻烦帮",
    "请你",
    "请帮",
    "需要你",
    "你确认",
    "你处理",
    "你跟进",
    "你安排",
    "你提供",
    "你整理",
    "你准备",
    "你同步",
    "你回复",
    "你发",
    "你那边",
    "帮我",
    "帮忙",
    "交给你",
)
REQUEST_WEAK = (
    "记得",
    "别忘了",
    "尽快",
    "下班前",
    "确认一下",
    "看一下",
    "处理一下",
    "跟进一下",
    "回复一下",
    "同步一下",
)
COMMIT_STRONG = (
    "我来",
    "我处理",
    "我看",
    "我确认",
    "我安排",
    "我负责",
    "我会",
    "我尽快",
    "稍后",
    "马上",
    "回头",
    "明天给",
    "今天给",
)
COMMIT_WEAK = ("好的", "收到", "可以", "没问题")
ACTION_WORDS = (
    "完成",
    "提交",
    "发出",
    "发送",
    "回复",
    "确认",
    "整理",
    "同步",
    "跟进",
    "处理",
    "准备",
    "提供",
    "安排",
    "审核",
    "修复",
    "更新",
    "发我",
    "发一下",
    "给出",
)
SKIP_KINDS = {"image", "file", "voice", "video", "system", "other"}

_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_GREETING_PREFIX = re.compile(
    r"^(?:@\S+\s*|(?:你好|您好|hi|hello|在吗|在么|打扰了)[，,。.!！~\s]*)",
    re.IGNORECASE,
)


def _hit(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker.lower() in text.lower() for marker in markers)


def _clean_title(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(r"https?://\S+", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = _GREETING_PREFIX.sub("", value).strip()
    for prefix in ("麻烦你", "麻烦帮", "请你", "请帮", "帮我", "帮忙", "需要你"):
        if value.startswith(prefix):
            value = value[len(prefix) :].lstrip("，,。.!！:： ")
            break
    value = value.strip("。！？；;,.，、 \t")
    if len(value) > 80:
        value = value[:80].rstrip() + "…"
    return value


def _next_weekday(base: date, weekday: int, *, next_week: bool) -> date:
    if next_week:
        monday = base + timedelta(days=7 - base.weekday())
        return monday + timedelta(days=weekday)
    delta = (weekday - base.weekday()) % 7
    return base + timedelta(days=delta)


def extract_due(
    text: str, timestamp: int, tz_name: object = "Asia/Shanghai"
) -> str | None:
    """Resolve a due date mentioned in ``text``; returns ``YYYY-MM-DD`` or None."""
    value = str(text or "")
    zone = timezone_for(tz_name)
    if timestamp:
        base_moment = datetime.fromtimestamp(int(timestamp), tz=zone)
    else:
        base_moment = datetime.now(tz=zone)
    base = base_moment.date()

    match = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日号]?", value)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
        except ValueError:
            pass
    match = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", value)
    if match:
        try:
            due = date(base.year, int(match.group(1)), int(match.group(2)))
        except ValueError:
            due = None
        if due is not None:
            if due < base:
                try:
                    due = date(base.year + 1, due.month, due.day)
                except ValueError:
                    return due.isoformat()
            return due.isoformat()
    if "大后天" in value:
        return (base + timedelta(days=3)).isoformat()
    if "后天" in value:
        return (base + timedelta(days=2)).isoformat()
    if "明天" in value:
        return (base + timedelta(days=1)).isoformat()
    if "今天" in value or "今晚" in value or "下班前" in value or "今日" in value:
        return base.isoformat()
    if "月底" in value:
        next_month = (base.replace(day=28) + timedelta(days=4)).replace(day=1)
        return (next_month - timedelta(days=1)).isoformat()
    match = re.search(r"下周([一二三四五六日天])", value)
    if match:
        return _next_weekday(base, _WEEKDAYS[match.group(1)], next_week=True).isoformat()
    match = re.search(r"(?:本周|这周|周|星期)([一二三四五六日天])", value)
    if match:
        return _next_weekday(base, _WEEKDAYS[match.group(1)], next_week=False).isoformat()
    return None


def _score_message(message: dict) -> dict | None:
    text = str(message.get("text") or "").strip()
    if len(text) < 3 or len(text) > 500:
        return None
    if message.get("kind") in SKIP_KINDS:
        return None
    direction = str(message.get("direction") or "")
    request_strong = _hit(text, REQUEST_STRONG)
    request_weak = _hit(text, REQUEST_WEAK)
    commit_strong = _hit(text, COMMIT_STRONG)
    commit_weak = _hit(text, COMMIT_WEAK)
    action = _hit(text, ACTION_WORDS)
    request_score = (0.45 if request_strong else 0.0) + (0.15 if request_weak else 0.0)
    commit_score = (0.45 if commit_strong else 0.0) + (0.1 if commit_weak else 0.0)
    if direction == "outgoing":
        commit_score += 0.1
    elif direction == "incoming":
        request_score += 0.1
    intent = "commit" if commit_score > request_score else "request"
    score = max(request_score, commit_score)
    if score <= 0:
        return None
    if action:
        score += 0.15
    due = extract_due(text, int(message.get("ts") or 0), message.get("_timezone"))
    if due:
        score += 0.2
    if len(text) <= 120:
        score += 0.05
    if not direction and message.get("source") == "wecom":
        score -= 0.1
    score = max(0.0, min(0.95, score))
    if score < MIN_CANDIDATE_CONFIDENCE:
        return None
    title = _clean_title(text)
    if not title:
        return None
    return {
        "title": title,
        "due": due or "",
        "confidence": round(score, 2),
        "intent": intent,
    }


def extract_candidates(
    messages: list[dict], *, timezone_name: object = "Asia/Shanghai"
) -> list[dict]:
    """Return deduplicated to-do candidates sorted by confidence then recency."""
    candidates: list[dict] = []
    seen_titles: set[str] = set()
    for message in messages:
        message = dict(message)
        message["_timezone"] = timezone_name
        scored = _score_message(message)
        if not scored:
            continue
        normalized = re.sub(r"\s+", "", scored["title"]).casefold()
        if normalized in seen_titles:
            continue
        seen_titles.add(normalized)
        candidates.append(
            {
                **scored,
                "source": str(message.get("source") or ""),
                "convId": str(message.get("conv_id") or ""),
                "convName": str(message.get("conv_name") or ""),
                "senderName": str(message.get("sender_name") or ""),
                "direction": str(message.get("direction") or ""),
                "msgId": str(message.get("msg_id") or ""),
                "createdAt": _iso_from_ts(message.get("ts")),
                "ts": int(message.get("ts") or 0),
            }
        )
    candidates.sort(key=lambda item: (-item["confidence"], -item["ts"]))
    return candidates


def _iso_from_ts(value: object) -> str:
    from worker.search.text import iso_utc

    try:
        timestamp = int(value or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        timestamp = 0
    return iso_utc(timestamp)
