"""One-shot worker actions for the daily assistant.

Actions: ``eq_reply``, ``weekly``, ``todos``, ``todos_refresh``, ``todo_update``.
Model failures never reach the UI: the actions return static Chinese fallbacks
with ``fallback: true`` instead.
"""

from __future__ import annotations

import time

from worker.search import index as search_index
from worker.search.sources import resolve_paths
from worker.search.text import normalize_source, parse_time_bound

from . import model, prompts, store
from .extract import AUTO_CREATE_CONFIDENCE, extract_candidates

TODOS_LOOKBACK_DAYS = 90
TODOS_MESSAGE_LIMIT = 2000


def _load_config() -> dict:
    from worker.pipeline.config_store import load_config

    try:
        config, _path = load_config()
        return config if isinstance(config, dict) else {}
    except Exception:  # A broken config must not take the assistant down.
        return {}


def _paths(config: dict):
    return resolve_paths(config)


def _parse_sources(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    sources = []
    for item in value:
        source = normalize_source(item)
        if source in {"wecom", "wechat"} and source not in sources:
            sources.append(source)
    return sources


def _day_bounds(payload: dict, timezone_name: object) -> tuple[int, int, str, str]:
    start_text = str(payload.get("startDate") or "").strip()
    end_text = str(payload.get("endDate") or "").strip()
    if not start_text or not end_text:
        raise ValueError("请选择周报的开始日期和结束日期")
    start_ts = parse_time_bound(start_text, timezone_name)
    end_ts = parse_time_bound(end_text, timezone_name, end_of_day=True)
    if start_ts is None or end_ts is None:
        raise ValueError("周报日期格式无效，请使用 YYYY-MM-DD")
    if start_ts > end_ts:
        raise ValueError("周报开始日期不能晚于结束日期")
    return start_ts, end_ts, start_text, end_text


def _static_eq_reply(text: str, context: str) -> dict:
    return {
        "styles": prompts.static_eq_replies(text, context),
        "fallback": True,
    }


def action_eq_reply(config: dict, body: dict) -> dict:
    text = str(body.get("text") or "").strip()
    if not text:
        raise ValueError("请输入需要回复的消息内容")
    context = str(body.get("context") or "").strip()
    if not model.model_configured(config):
        return _static_eq_reply(text, context)
    try:
        system_prompt, user_prompt = prompts.eq_reply_prompt(text, context)
        raw = model.call_llm(config, system_prompt, user_prompt, max_tokens=900, json_mode=True)
        payload = model.parse_json_object(raw)
    except model.ModelUnavailable:
        return _static_eq_reply(text, context)
    styles = []
    fallback = False
    for style_id, title, _content in prompts.EQ_REPLY_STYLES:
        content = str(payload.get(style_id) or "").strip()
        if not content:
            content = next(
                item["content"]
                for item in prompts.static_eq_replies(text, context)
                if item["id"] == style_id
            )
            fallback = True
        styles.append({"id": style_id, "title": title, "content": content})
    return {"styles": styles, "fallback": fallback}


def _conversation_counts(messages: list[dict]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for message in messages:
        name = str(message.get("conv_name") or message.get("conv_id") or "未命名会话")
        counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def action_weekly(config: dict, body: dict) -> dict:
    paths = _paths(config)
    start_ts, end_ts, start_text, end_text = _day_bounds(body, paths.timezone)
    sources = _parse_sources(body.get("sources"))
    messages = search_index.recent_messages(
        paths,
        from_ts=start_ts,
        to_ts=end_ts,
        sources=sources,
        limit=4000,
        ascending=True,
    )
    stats = {
        "messages": len(messages),
        "conversations": len(
            {(message.get("source"), message.get("conv_id")) for message in messages}
        ),
    }
    if not messages:
        return {
            "draft": prompts.empty_week_draft(start_text, end_text),
            "stats": stats,
            "fallback": False,
        }
    if model.model_configured(config):
        try:
            system_prompt, user_prompt = prompts.weekly_prompt(
                start_text, end_text, messages, stats
            )
            draft = model.call_llm(config, system_prompt, user_prompt, max_tokens=1600)
            if draft.strip():
                return {"draft": draft.strip(), "stats": stats, "fallback": False}
        except model.ModelUnavailable:
            pass
    return {
        "draft": prompts.weekly_fallback_draft(
            start_text, end_text, stats, _conversation_counts(messages)
        ),
        "stats": stats,
        "fallback": True,
    }


def action_todos(config: dict, body: dict) -> dict:
    paths = _paths(config)
    data = store.load_store(paths)
    status = str(body.get("status") or "active").strip().lower()
    return {"items": store.list_items(data, status)}


def _lookback_seconds(days: int) -> int:
    return int(time.time()) - max(1, min(days, 365)) * 86400


def action_todos_refresh(config: dict, body: dict) -> dict:
    paths = _paths(config)
    try:
        days = int(body.get("days") or TODOS_LOOKBACK_DAYS)
    except (TypeError, ValueError):
        days = TODOS_LOOKBACK_DAYS
    data = store.load_store(paths)
    try:
        messages = search_index.recent_messages(
            paths,
            from_ts=_lookback_seconds(days),
            limit=TODOS_MESSAGE_LIMIT,
            ascending=False,
        )
    except Exception:  # Search must fail open for the assistant.
        messages = []
    processed = store.processed_ids(data)
    fresh = [message for message in messages if message.get("msg_id") not in processed]
    candidates = extract_candidates(fresh, timezone_name=paths.timezone)
    created = 0
    new_ids: list[str] = [str(message["msg_id"]) for message in fresh if message.get("msg_id")]
    for candidate in candidates:
        if candidate["confidence"] >= AUTO_CREATE_CONFIDENCE:
            if store.add_todo(data, candidate):
                created += 1
        else:
            store.add_candidate(data, candidate)
    store.mark_processed(data, new_ids)
    store.save_store(paths, data)
    return {
        "created": created,
        "items": store.list_items(data, "active"),
        "candidates": store.list_items(data, "pending"),
    }


def action_todo_update(config: dict, body: dict) -> dict:
    item_id = str(body.get("id") or "").strip()
    if not item_id:
        raise ValueError("请提供要更新的待办 ID")
    paths = _paths(config)
    data = store.load_store(paths)
    item = store.update_item(data, item_id, bool(body.get("done")))
    if item is None:
        raise ValueError("找不到该待办，可能已被删除")
    store.save_store(paths, data)
    return {"item": item}


_ACTIONS = {
    "eq_reply": action_eq_reply,
    "weekly": action_weekly,
    "todos": action_todos,
    "todos_refresh": action_todos_refresh,
    "todo_update": action_todo_update,
}


def handle_daily_action(action: str, body: dict | None = None) -> dict:
    """Dispatch a ``daily_action`` worker request."""
    body = body if isinstance(body, dict) else {}
    name = str(action or "").strip().lower()
    handler = _ACTIONS.get(name)
    if handler is None:
        raise ValueError(f"未知的日常助手动作：{name}")
    config = _load_config()
    return handler(config, body)
