"""One-shot worker actions for the daily assistant.

Actions: ``eq_reply`` / ``summarize`` / ``phrases_list`` / ``phrase_save`` /
``phrase_delete``. When no model is configured (or the model fails), eq_reply and
summarize return deterministic Chinese rule-based results with ``fallback: true``
instead of raising to the UI.
"""

from __future__ import annotations

from . import model, phrases, prompts, rules

DEFAULT_SUMMARIZE_MODE = "digest"
SUMMARIZE_MODES = ("digest", "meeting")
MAX_MODEL_POINTS = 12


def _load_config() -> dict:
    from worker.pipeline.config_store import load_config

    try:
        config, _path = load_config()
        return config if isinstance(config, dict) else {}
    except Exception:  # A broken config must not take the assistant down.
        return {}


def _string_list(value: object, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _normalize_interpretation(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    summary = str(value.get("summary") or "").strip()
    signals = []
    raw_signals = value.get("signals")
    if isinstance(raw_signals, list):
        for item in raw_signals:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            detail = str(item.get("detail") or "").strip()
            if label and detail:
                signals.append({"label": label, "detail": detail})
    if not summary or not signals:
        return None
    return {"summary": summary, "signals": signals}


def _normalize_check(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    risks = _string_list(value.get("risks"), 5)
    suggestions = _string_list(value.get("suggestions"), 5)
    if not risks or not suggestions:
        return None
    return {"risks": risks, "suggestions": suggestions}


def _eq_reply_from_model(
    payload: dict,
    fallback_styles: list[dict],
    fallback_interpretation: dict,
    fallback_check: dict,
) -> tuple[list[dict], dict, dict, bool]:
    style_source = payload.get("styles") if isinstance(payload.get("styles"), dict) else payload
    fallback = False
    styles = []
    fallback_by_id = {item["id"]: item for item in fallback_styles}
    for style_id, title in rules.STYLE_DEFINITIONS:
        content = ""
        if isinstance(style_source, dict):
            content = str(style_source.get(style_id) or "").strip()
        if not content:
            content = fallback_by_id[style_id]["content"]
            fallback = True
        styles.append({"id": style_id, "title": title, "content": content})
    interpretation = _normalize_interpretation(payload.get("interpretation"))
    if interpretation is None:
        interpretation = fallback_interpretation
        fallback = True
    check = _normalize_check(payload.get("check"))
    if check is None:
        check = fallback_check
        fallback = True
    return styles, interpretation, check, fallback


def action_eq_reply(config: dict, body: dict) -> dict:
    text = str(body.get("text") or "").strip()
    if not text:
        raise ValueError("请输入需要回复的消息内容")
    context = str(body.get("context") or "").strip()
    recipient = str(body.get("recipient") or "").strip()
    fallback_styles = rules.static_styles(text, context, recipient)
    fallback_interpretation = rules.interpret(text, context, recipient)
    fallback_check = rules.check_reply(text, recipient)
    if not model.model_configured(config):
        return {
            "styles": fallback_styles,
            "interpretation": fallback_interpretation,
            "check": fallback_check,
            "fallback": True,
        }
    try:
        system_prompt, user_prompt = prompts.eq_reply_prompt(text, context, recipient)
        raw = model.call_llm(config, system_prompt, user_prompt, max_tokens=1200, json_mode=True)
        payload = model.parse_json_object(raw)
    except model.ModelUnavailable:
        return {
            "styles": fallback_styles,
            "interpretation": fallback_interpretation,
            "check": fallback_check,
            "fallback": True,
        }
    styles, interpretation, check, fallback = _eq_reply_from_model(
        payload, fallback_styles, fallback_interpretation, fallback_check
    )
    return {
        "styles": styles,
        "interpretation": interpretation,
        "check": check,
        "fallback": fallback,
    }


def _summarize_from_model(
    payload: dict, fallback: dict
) -> tuple[dict, bool]:
    summary = str(payload.get("summary") or "").strip()
    points = _string_list(payload.get("points"), MAX_MODEL_POINTS)
    if not summary or not points:
        return fallback, True
    fallback_used = False
    risks = _string_list(payload.get("risks"), 8)
    if not isinstance(payload.get("risks"), list):
        risks = fallback["risks"]
        fallback_used = True
    next_steps = _string_list(payload.get("next"), 8)
    if not isinstance(payload.get("next"), list):
        next_steps = fallback["next"]
        fallback_used = True
    return {
        "summary": summary,
        "points": points,
        "risks": risks,
        "next": next_steps,
    }, fallback_used


def action_summarize(config: dict, body: dict) -> dict:
    text = str(body.get("text") or "").strip()
    if not text:
        raise ValueError("请输入需要整理的内容")
    mode = str(body.get("mode") or DEFAULT_SUMMARIZE_MODE).strip().lower()
    if mode not in SUMMARIZE_MODES:
        raise ValueError("mode 必须是 digest 或 meeting")
    fallback = rules.summarize_rules(text, mode)
    if not model.model_configured(config):
        return {**fallback, "fallback": True}
    try:
        system_prompt, user_prompt = prompts.summarize_prompt(text, mode)
        raw = model.call_llm(config, system_prompt, user_prompt, max_tokens=1400, json_mode=True)
        payload = model.parse_json_object(raw)
    except model.ModelUnavailable:
        return {**fallback, "fallback": True}
    result, fallback_used = _summarize_from_model(payload, fallback)
    return {**result, "fallback": fallback_used}


def action_phrases_list(config: dict, body: dict) -> dict:
    return {"phrases": phrases.list_phrases()}


def action_phrase_save(config: dict, body: dict) -> dict:
    raw = body.get("phrase")
    if not isinstance(raw, dict):
        raw = body
    if not str(raw.get("title") or "").strip() and not str(raw.get("content") or "").strip():
        raise ValueError("请提供要保存的话术内容")
    return {"phrase": phrases.save_phrase(raw)}


def action_phrase_delete(config: dict, body: dict) -> dict:
    return phrases.delete_phrase(body.get("id"))


_ACTIONS = {
    "eq_reply": action_eq_reply,
    "summarize": action_summarize,
    "phrases_list": action_phrases_list,
    "phrase_save": action_phrase_save,
    "phrase_delete": action_phrase_delete,
}


def handle_daily_action(action: str, body: dict | None = None) -> dict:
    """Dispatch a ``daily_action`` worker request."""
    body = body if isinstance(body, dict) else {}
    name = str(action or "").strip()
    handler = _ACTIONS.get(name)
    if handler is None:
        raise ValueError(f"未知的日常助手动作：{name}")
    return handler(_load_config(), body)
