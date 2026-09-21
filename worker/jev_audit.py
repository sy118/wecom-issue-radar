"""Semantic judgment audit trail.

Jev audit records are kept out of the existing ``provider_audit_json`` so that
generic LLM auditing is unaffected. Every record keeps the *complete* typed
answer per question -- Noul's ``noul`` is stored separately from the
Choice/Score ``confidence``, and Choice/Score keep their full probability maps,
legend and score -- so threshold calibration never requires re-running
inference.

No raw state, Authorization header, image payload or unredacted group text is
ever written here; the state is represented by a hash instead.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from worker.jev_client import ChoiceAnswer, JevError, JevResponse, NoulAnswer, ScoreAnswer, TypedAnswer


def state_hash(value: Any) -> str:
    """Stable fingerprint of the (already redacted) state."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = repr(value)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def serialize_answer(answer: TypedAnswer) -> dict[str, Any]:
    """Preserve the primitive's own semantics.

    A Noul answer carries only ``noul``; inventing a ``confidence`` for it (for
    example ``1 - |2p - 1|``) would fabricate a number Jev never returned.
    """
    if isinstance(answer, NoulAnswer):
        return {"type": "noul", "noul": answer.noul}
    if isinstance(answer, ChoiceAnswer):
        return {
            "type": "choice",
            "choice": answer.choice,
            "probabilities": dict(answer.probabilities),
            "confidence": answer.confidence,
            "tied": list(answer.tied),
        }
    if isinstance(answer, ScoreAnswer):
        return {
            "type": "score",
            "score": answer.score,
            "legend": dict(answer.legend),
            "probabilities": dict(answer.probabilities),
            "confidence": answer.confidence,
            "levelKeys": list(answer.level_keys),
        }
    raise TypeError(f"unsupported typed answer: {type(answer).__name__}")


def serialize_answers(response: JevResponse) -> dict[str, dict[str, Any]]:
    return {question_id: serialize_answer(answer) for question_id, answer in response.answers.items()}


@dataclass(frozen=True)
class SemanticJudgment:
    """One audited semantic call."""

    feature: str
    scope: str
    status: str
    mode: str = ""
    work_id: str = ""
    generation: int | None = None
    answers: Mapping[str, Any] = field(default_factory=dict)
    comparison: Mapping[str, Any] = field(default_factory=dict)
    requested_model: str = ""
    response_model: str = ""
    correlation_id: str = ""
    provider_request_id: str = ""
    attempts: int = 0
    retry_after: float | None = None
    latency_ms: int = 0
    state_hash: str = ""
    fallback_reason: str = ""
    usage: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    created_at: float = 0.0

    @property
    def applied(self) -> bool:
        """True only when this judgment may influence business state."""
        return self.status == "ok" and not self.fallback_reason

    def as_row(self) -> tuple:
        return (
            self.feature,
            self.scope,
            self.work_id,
            self.status,
            self.mode,
            self.generation,
            self.state_hash,
            self.fallback_reason,
            self.requested_model,
            self.response_model,
            self.correlation_id,
            self.provider_request_id,
            self.attempts,
            self.retry_after,
            self.latency_ms,
            _encode(self.answers),
            _encode(self.comparison) if self.comparison else None,
            _encode(self.usage) if self.usage else None,
            _encode(self.error) if self.error else None,
            self.created_at,
        )


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


INSERT_SQL = """
INSERT INTO semantic_judgments(
    feature,scope,work_id,status,mode,generation,state_hash,fallback_reason,
    requested_model,response_model,correlation_id,provider_request_id,
    attempts,retry_after,latency_ms,answers_json,comparison_json,usage_json,
    error_json,created_at
) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def insert_judgment(db: Any, judgment: SemanticJudgment) -> None:
    db.execute(INSERT_SQL, judgment.as_row())


def record_from_response(
    response: JevResponse,
    *,
    feature: str,
    scope: str,
    mode: str,
    now: float,
    work_id: str = "",
    generation: int | None = None,
    hashed_state: str = "",
    status: str = "ok",
    comparison: Mapping[str, Any] | None = None,
) -> SemanticJudgment:
    return SemanticJudgment(
        feature=feature,
        scope=scope,
        status=status,
        mode=mode,
        work_id=work_id,
        generation=generation,
        answers=serialize_answers(response),
        comparison=dict(comparison or {}),
        requested_model=response.requested_model,
        response_model=response.model,
        correlation_id=response.correlation_id,
        provider_request_id=response.request_id,
        attempts=response.attempts,
        retry_after=response.retry_after,
        latency_ms=response.latency_ms,
        state_hash=hashed_state,
        created_at=now,
        usage=dict(response.usage) if response.usage else None,
    )


def record_from_error(
    error: JevError,
    *,
    feature: str,
    scope: str,
    mode: str,
    now: float,
    requested_model: str = "",
    correlation_id: str = "",
    provider_request_id: str = "",
    attempts: int = 0,
    retry_after: float | None = None,
    latency_ms: int = 0,
    hashed_state: str = "",
    work_id: str = "",
    generation: int | None = None,
    fallback_reason: str = "",
) -> SemanticJudgment:
    return SemanticJudgment(
        feature=feature,
        scope=scope,
        status=error.kind,
        mode=mode,
        work_id=work_id,
        generation=generation,
        requested_model=requested_model,
        correlation_id=correlation_id,
        provider_request_id=provider_request_id,
        attempts=attempts,
        retry_after=retry_after if retry_after is not None else error.retry_after,
        latency_ms=latency_ms,
        state_hash=hashed_state,
        fallback_reason=fallback_reason or error.kind,
        error=error.as_dict(),
        created_at=now,
    )


def skipped_judgment(
    *,
    feature: str,
    scope: str,
    reason: str,
    now: float,
    mode: str = "",
    status: str = "not_eligible",
    work_id: str = "",
    generation: int | None = None,
) -> SemanticJudgment:
    """Audit a call that was never issued, so fallback rate stays measurable."""
    return SemanticJudgment(
        feature=feature,
        scope=scope,
        status=status,
        mode=mode,
        work_id=work_id,
        generation=generation,
        fallback_reason=reason,
        created_at=now,
    )
