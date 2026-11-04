"""One-shot worker actions for the reply assistant.

Only ``eq_reply`` remains: it writes three ready-to-send Chinese reply drafts
(styles), reads the hidden intent of the message (interpretation) and lists the
risks/suggestions to check before sending (check). When no model is configured
(or the model fails), it returns deterministic rule-based results with
``fallback: true`` instead of raising to the UI.
"""

from __future__ import annotations

from . import images, model, prompts, rules

MAX_TOKENS = 1200
MAX_FOCUS_MESSAGES = 20
MAX_CONTEXT_RULE_INPUT_CHARS = 2000


def _load_config() -> dict:
    from worker.pipeline.config_store import load_config

    try:
        config, _path = load_config()
        return config if isinstance(config, dict) else {}
    except Exception:  # A broken config must not take the assistant down.
        return {}


def _normalize_focus_messages(value: object) -> list[dict]:
    """Normalize the optional multi-message reply targets (max 20, non-empty text)."""

    if not isinstance(value, list):
        return []
    focus: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        focus.append(
            {
                "senderName": str(item.get("senderName") or "").strip(),
                "timeLabel": str(item.get("timeLabel") or "").strip(),
                "text": text,
            }
        )
        if len(focus) >= MAX_FOCUS_MESSAGES:
            break
    return focus


def _focus_fallback_text(focus_messages: list[dict]) -> str:
    return "\n".join(item["text"] for item in focus_messages).strip()


def _context_rule_text(context: str) -> str:
    """Deterministic rule fallback input when only the group context is available."""

    value = str(context or "").strip()
    if len(value) <= MAX_CONTEXT_RULE_INPUT_CHARS:
        return value
    return value[:MAX_CONTEXT_RULE_INPUT_CHARS].rstrip()


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
    raw_text = str(body.get("text") or "").strip()
    focus_messages = _normalize_focus_messages(body.get("focusMessages"))
    context = str(body.get("context") or "").strip()
    text = raw_text or _focus_fallback_text(focus_messages)
    rule_text = text
    if not rule_text:
        # Group chats may generate with zero selected messages: the context is
        # the target then, and the deterministic rules run against it.
        if not context:
            raise ValueError("请输入需要回复的消息内容")
        rule_text = _context_rule_text(context)
    recipient = str(body.get("recipient") or "").strip()
    fallback_styles = rules.static_styles(rule_text, context, recipient)
    fallback_interpretation = rules.interpret(rule_text, context, recipient)
    fallback_check = rules.check_reply(rule_text, recipient)
    image_paths = _resolve_images(config, body)
    if not model.model_configured(config):
        return {
            "styles": fallback_styles,
            "interpretation": fallback_interpretation,
            "check": fallback_check,
            "fallback": True,
            "images": {"attached": 0},
        }
    system_prompt, user_prompt = prompts.eq_reply_prompt(
        config,
        raw_text,
        context,
        recipient,
        image_count=len(image_paths),
        focus_messages=focus_messages,
    )
    payload = _call_model(config, system_prompt, user_prompt, image_paths)
    if payload is None:
        return {
            "styles": fallback_styles,
            "interpretation": fallback_interpretation,
            "check": fallback_check,
            "fallback": True,
            "images": {"attached": len(image_paths)},
        }
    styles, interpretation, check, fallback = _eq_reply_from_model(
        payload, fallback_styles, fallback_interpretation, fallback_check
    )
    return {
        "styles": styles,
        "interpretation": interpretation,
        "check": check,
        "fallback": fallback,
        "images": {"attached": len(image_paths)},
    }




def _resolve_images(config: dict, body: dict) -> list:
    conversation_id = str(body.get("conversationId") or "").strip()
    raw_ids = body.get("imageMessageIds")
    image_message_ids = (
        [str(item).strip() for item in raw_ids if str(item).strip()]
        if isinstance(raw_ids, list)
        else []
    )
    if not conversation_id or not image_message_ids:
        return []
    try:
        return images.resolve_message_images(
            config, conversation_id, image_message_ids
        )
    except Exception:  # Images are best-effort; text reply must still work.
        return []


def _call_model(config: dict, system_prompt: str, user_prompt: str, image_paths: list):
    try:
        raw = model.call_llm(
            config,
            system_prompt,
            user_prompt,
            max_tokens=MAX_TOKENS,
            json_mode=True,
            images=image_paths,
        )
        return model.parse_json_object(raw)
    except model.ModelUnavailable:
        if not image_paths:
            return None
    try:
        raw = model.call_llm(
            config,
            system_prompt,
            user_prompt,
            max_tokens=MAX_TOKENS,
            json_mode=True,
        )
        return model.parse_json_object(raw)
    except model.ModelUnavailable:
        return None


_ACTIONS = {
    "eq_reply": action_eq_reply,
}


def handle_assistant_action(action: str, body: dict | None = None) -> dict:
    """Dispatch an ``assistant_action`` worker request."""
    body = body if isinstance(body, dict) else {}
    name = str(action or "").strip()
    handler = _ACTIONS.get(name)
    if handler is None:
        raise ValueError(f"未知的助手动作：{name}")
    return handler(_load_config(), body)
