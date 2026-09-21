"""Post-generation issue quality gate.

Runs *after* ``build_issue_definitions()`` and only ever attaches a
``semantic_signals`` sidecar to each candidate. It never edits a candidate's
``key``, ``values``, ``timeline``, ``image_refs`` or any sync identity, and it
never removes a candidate -- uncertain ones are marked for review instead.

Like every other Jev surface this fails open: when Jev is disabled or
unavailable the snapshot is returned byte-for-byte compatible, just without the
sidecar.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from worker.jev_service import (
    FEATURE_QUALITY_GATE,
    JevSemanticService,
    quality_gate_questions,
)

# Image states that mean the candidate is resting on evidence Jev cannot see.
INCOMPLETE_IMAGE_STATUSES = frozenset(
    {"partial_missing_original_images", "missing_original_images"}
)

# Bound the per-candidate state rather than reusing the ~80k-char LLM batch.
MAX_STATE_MESSAGES = 12
MAX_STATE_MESSAGE_CHARS = 400


def build_quality_gate_service(config: Mapping[str, Any] | None) -> JevSemanticService | None:
    """Build the gate's service, or ``None`` when the capability is inactive."""
    source = config if isinstance(config, Mapping) else {}
    service = JevSemanticService(lambda: source)
    if not service.config().active(FEATURE_QUALITY_GATE):
        return None
    return service


def candidate_state(issue: Mapping[str, Any], *, max_messages: int = MAX_STATE_MESSAGES) -> dict[str, Any]:
    """Bounded state for one candidate: its fields plus a few related messages."""
    values = issue.get("values") if isinstance(issue.get("values"), Mapping) else {}
    messages: list[dict[str, Any]] = []
    for item in (issue.get("timeline") or [])[:max_messages]:
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("raw_text") or "").strip()
        if not text:
            continue
        messages.append(
            {
                "sender": str(item.get("sender") or ""),
                "role": str(item.get("role") or ""),
                "text": text[:MAX_STATE_MESSAGE_CHARS],
            }
        )
    return {
        "candidate": {
            "summary": str(values.get("issue_summary_text") or ""),
            "description": str(values.get("problem_description") or ""),
            "module": str(values.get("module_text") or ""),
            "messages": messages,
        }
    }


def _signals(
    judgment: Any,
    *,
    status: str,
    mode: str,
    fallback_reason: str,
    applied: bool,
    review_required: bool,
    review_reasons: Sequence[str],
    image_status: str,
) -> dict[str, Any]:
    """Sidecar payload. Contains judgments only -- never candidate content."""
    return {
        "feature": FEATURE_QUALITY_GATE,
        "status": status,
        "mode": mode,
        "fallbackReason": fallback_reason,
        "applied": bool(applied),
        # Only `assist` may act on the judgment; `shadow` records the opinion in
        # reviewReasons without flagging the candidate.
        "reviewRequired": bool(review_required),
        "reviewReasons": sorted(set(review_reasons)),
        "imageStatus": image_status,
        "answers": {
            question_id: {
                "type": answer.get("type"),
                "noul": answer.get("noul"),
            }
            for question_id, answer in (judgment.answers or {}).items()
            if isinstance(answer, Mapping)
        },
        "requestedModel": judgment.requested_model,
        "responseModel": judgment.response_model,
        "correlationId": judgment.correlation_id,
        "providerRequestId": judgment.provider_request_id,
        "latencyMs": judgment.latency_ms,
        "attempts": judgment.attempts,
    }


def _evaluate_issue(
    issue: Mapping[str, Any], service: JevSemanticService, scope: str
) -> dict[str, Any]:
    image_status = str(issue.get("image_status") or "")
    reasons: list[str] = []

    work_id = str(issue.get("key") or "")
    acting = service.config().decides()
    if image_status in INCOMPLETE_IMAGE_STATUSES:
        # Missing images must never be read as "no problem here", and there is
        # no point asking Jev to judge evidence it cannot see.
        judgment = service.record_skip(
            feature=FEATURE_QUALITY_GATE,
            scope=scope,
            reason="image_evidence_incomplete",
            work_id=work_id,
        )
        return _signals(
            judgment,
            status="not_eligible",
            mode=judgment.mode,
            fallback_reason="image_evidence_incomplete",
            applied=False,
            review_required=acting,
            review_reasons=["image_evidence_incomplete"],
            image_status=image_status,
        )

    outcome = service.ask(
        feature=FEATURE_QUALITY_GATE,
        scope=scope,
        state=candidate_state(issue),
        questions=quality_gate_questions(),
        work_id=work_id,
    )
    # The opinion is recorded in every mode: in shadow it is exactly what
    # threshold calibration reads, and `reviewRequired` below is what decides
    # whether it is acted upon.
    if outcome.noul_no("is_real_issue"):
        reasons.append("likely_not_an_issue")
    if outcome.noul_no("evidence_sufficient"):
        reasons.append("insufficient_evidence")
    if outcome.noul_no("context_is_one_issue"):
        reasons.append("mixed_context")
    if outcome.status == "review":
        reasons.append("review_interval")

    return _signals(
        outcome.judgment,
        status=outcome.status,
        mode=outcome.mode,
        fallback_reason=outcome.fallback_reason,
        applied=outcome.applied,
        review_required=acting and bool(reasons),
        review_reasons=reasons,
        image_status=image_status,
    )


def apply_quality_gate(
    snapshot: Mapping[str, Any],
    *,
    service: JevSemanticService | None,
    logger: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Return a new snapshot with ``semantic_signals`` attached to each issue.

    The input snapshot is never mutated, and no existing field is modified.
    """
    result = dict(snapshot)
    issues = list(snapshot.get("issues") or [])
    if service is None:
        # Jev unavailable: emit a snapshot compatible with the current flow.
        result["issues"] = issues
        return result

    scope = f"pipeline:{snapshot.get('date') or ''}"
    gated: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, Mapping):
            gated.append(issue)
            continue
        enriched = dict(issue)
        try:
            enriched["semantic_signals"] = _evaluate_issue(issue, service, scope)
        except Exception as exc:
            # The quality gate is advisory: a failure here must never cost the
            # day's candidates.
            if logger is not None:
                logger(f"Jev 质量门跳过候选 {issue.get('key')}：{exc}")
        gated.append(enriched)
    result["issues"] = gated
    return result
