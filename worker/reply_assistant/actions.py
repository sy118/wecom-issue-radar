"""One-shot worker actions for the reply assistant.

Only ``eq_reply`` remains: it writes three ready-to-send Chinese reply drafts
(styles), reads the hidden intent of the message (interpretation) and lists the
risks/suggestions to check before sending (check). When no model is configured
or the bounded generation budget runs out, it returns deterministic rule-based
results with ``fallback: true`` and a machine-readable ``fallbackReason``.
"""

from __future__ import annotations

import time

from . import images, model, prompts, rules

# Reasoning models count hidden reasoning tokens toward max_tokens. 1200 was
# routinely exhausted before the JSON answer was finished (empty/truncated
# content); 3000 leaves room for thinking plus the full three-style contract.
MAX_TOKENS = 3000
MAX_FOCUS_MESSAGES = 10
MAX_CONTEXT_RULE_INPUT_CHARS = 2000

# Bounded generation budget (seconds): never wait on the legacy 180s config.
MAX_TOTAL_BUDGET_SECONDS = 45.0
IMAGE_ATTEMPT_TIMEOUT_SECONDS = 22
TEXT_RETRY_TIMEOUT_SECONDS = 18
TEXT_ONLY_TIMEOUT_SECONDS = 35
MIN_RETRY_REMAINING_SECONDS = 5.0

_FAILURE_REASON_BY_KIND = {
    "timeout": "timeout",
    "http_4xx": "http_error",
    "http_5xx": "http_error",
    "network_error": "network_error",
    "invalid_response": "invalid_response",
    "empty": "empty",
    "vision_unsupported": "vision_unsupported",
    "unknown": "unknown",
}


def _load_config() -> dict:
    from worker.pipeline.config_store import load_config

    try:
        config, _path = load_config()
        return config if isinstance(config, dict) else {}
    except Exception:  # A broken config must not take the assistant down.
        return {}


def _normalize_focus_messages(value: object) -> list[dict]:
    """Normalize the optional multi-message reply targets (max 10, non-empty text)."""

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


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.perf_counter() - started) * 1000)))


def _attempt_timeout(deadline: float, cap_seconds: int) -> int:
    """Per-attempt timeout: the cap inside the remaining total budget."""

    remaining = deadline - time.perf_counter()
    if remaining <= 0:
        return 1
    return max(1, int(min(float(cap_seconds), remaining)))


def _fallback_reason(exc) -> str:
    kind = str(getattr(exc, "kind", "") or "").strip()
    return _FAILURE_REASON_BY_KIND.get(kind, "unknown")


def _invoke_model(
    config: dict,
    system_prompt: str,
    user_prompt: str,
    *,
    timeout_seconds: int,
    images_for_call: list | None = None,
) -> tuple[dict | None, str, int, int]:
    """One bounded model attempt: ``(payload, reason, modelMs, parseMs)``."""

    call_started = time.perf_counter()
    kwargs: dict = {
        "max_tokens": MAX_TOKENS,
        "json_mode": True,
        "timeout_seconds": timeout_seconds,
    }
    if images_for_call:
        kwargs["images"] = images_for_call
    try:
        raw = model.call_llm(config, system_prompt, user_prompt, **kwargs)
    except model.ModelUnavailable as exc:
        return None, _fallback_reason(exc), _elapsed_ms(call_started), 0
    except Exception:  # noqa: BLE001 - unexpected transport errors fall back too
        return None, "unknown", _elapsed_ms(call_started), 0
    model_ms = _elapsed_ms(call_started)
    parse_started = time.perf_counter()
    try:
        payload = model.parse_json_object(raw)
    except model.ModelUnavailable as exc:
        return None, _fallback_reason(exc), model_ms, _elapsed_ms(parse_started)
    except Exception:  # noqa: BLE001 - defensive against odd parser failures
        return None, "invalid_response", model_ms, _elapsed_ms(parse_started)
    return payload, "", model_ms, _elapsed_ms(parse_started)


def action_eq_reply(config: dict, body: dict) -> dict:
    started = time.perf_counter()
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

    requested_prompt_id = str(body.get("promptId") or "").strip()
    prompt_note = str(body.get("promptNote") or "").strip()
    prompt_id_used, _prompt_content, prompt_missing = prompts.resolve_eq_reply_prompt(
        config, requested_prompt_id
    )
    ai = {
        "promptId": prompt_id_used,
        "promptFallback": prompt_missing,
        "fallbackReason": "",
    }

    image_paths: list = []
    image_skipped = 0
    image_ms = 0
    model_ms = 0
    parse_ms = 0
    attempts = 0
    fallback_reason = ""
    payload = None
    configured = model.model_configured(config)
    if not configured:
        fallback_reason = "not_configured"
    else:
        image_started = time.perf_counter()
        image_paths, image_skipped = _resolve_images(config, body)
        image_ms = _elapsed_ms(image_started)
        system_prompt, user_prompt = prompts.eq_reply_prompt(
            config,
            raw_text,
            context,
            recipient,
            image_count=len(image_paths),
            focus_messages=focus_messages,
            prompt_id=requested_prompt_id,
            prompt_note=prompt_note,
        )
        deadline = started + MAX_TOTAL_BUDGET_SECONDS
        if image_paths:
            payload, reason, call_ms, parse_elapsed = _invoke_model(
                config,
                system_prompt,
                user_prompt,
                timeout_seconds=_attempt_timeout(deadline, IMAGE_ATTEMPT_TIMEOUT_SECONDS),
                images_for_call=image_paths,
            )
            attempts += 1
            model_ms += call_ms
            parse_ms += parse_elapsed
            fallback_reason = reason
            if (
                payload is None
                and reason == "vision_unsupported"
                and deadline - time.perf_counter() > MIN_RETRY_REMAINING_SECONDS
            ):
                payload, reason, call_ms, parse_elapsed = _invoke_model(
                    config,
                    system_prompt,
                    user_prompt,
                    timeout_seconds=_attempt_timeout(
                        deadline, TEXT_RETRY_TIMEOUT_SECONDS
                    ),
                )
                attempts += 1
                model_ms += call_ms
                parse_ms += parse_elapsed
                fallback_reason = reason
        else:
            payload, reason, call_ms, parse_elapsed = _invoke_model(
                config,
                system_prompt,
                user_prompt,
                timeout_seconds=_attempt_timeout(deadline, TEXT_ONLY_TIMEOUT_SECONDS),
            )
            attempts = 1
            model_ms += call_ms
            parse_ms += parse_elapsed
            fallback_reason = reason

    if payload is None:
        styles = fallback_styles
        interpretation = fallback_interpretation
        check = fallback_check
        fallback = True
    else:
        styles, interpretation, check, fallback = _eq_reply_from_model(
            payload, fallback_styles, fallback_interpretation, fallback_check
        )
        fallback_reason = "partial" if fallback else ""
    ai["fallbackReason"] = fallback_reason
    return {
        "styles": styles,
        "interpretation": interpretation,
        "check": check,
        "fallback": fallback,
        "images": {
            "attached": len(image_paths) if configured else 0,
            "skipped": image_skipped,
        },
        "timings": {
            "imageMs": image_ms,
            "modelMs": model_ms,
            "parseMs": parse_ms,
            "totalMs": _elapsed_ms(started),
            "attempts": attempts,
        },
        "ai": ai,
    }


def _resolve_images(config: dict, body: dict) -> tuple[list, int]:
    conversation_id = str(body.get("conversationId") or "").strip()
    raw_ids = body.get("imageMessageIds")
    image_message_ids = (
        [str(item).strip() for item in raw_ids if str(item).strip()]
        if isinstance(raw_ids, list)
        else []
    )
    if not conversation_id or not image_message_ids:
        return [], 0
    resolver = images.resolve_message_images
    original_resolver = getattr(images, "_ORIGINAL_RESOLVE_MESSAGE_IMAGES", None)
    meta_resolver = getattr(images, "resolve_message_images_with_meta", None)
    if callable(meta_resolver) and resolver is original_resolver:
        try:
            resolved = meta_resolver(config, conversation_id, image_message_ids)
        except Exception:  # Images are best-effort; text reply must still work.
            resolved = None
        if isinstance(resolved, dict):
            return (
                list(resolved.get("paths") or []),
                max(0, int(resolved.get("skipped") or 0)),
            )
    try:
        return list(resolver(config, conversation_id, image_message_ids)), 0
    except Exception:  # Images are best-effort; text reply must still work.
        return [], 0


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
