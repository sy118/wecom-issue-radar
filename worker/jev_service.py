"""Semantic service: feature questions, thresholds and fail-open policy.

This is the only place that decides whether a Jev judgment may influence
business state. It encodes three rules:

* ``off`` never calls; ``shadow`` calls and audits but never applies; ``assist``
  applies only input that is eligible and fully decisive.
* Every failure mode -- timeout, rate limit, invalid body, ineligible input --
  falls back to the existing LLM/deterministic path. A *high-confidence no* is
  a real answer, not a failure, and is never reported as one.
* Thresholds are primitive-aware. Noul is graded on ``noul``; Choice and Score
  are graded on their own ``confidence``/distribution and never routed through
  the Noul probability path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

from worker.jev_audit import (
    SemanticJudgment,
    record_from_error,
    record_from_response,
    skipped_judgment,
    state_hash as compute_state_hash,
)
from worker.jev_client import (
    ChoiceAnswer,
    JevClient,
    JevError,
    JevResponse,
    NoulAnswer,
    ScoreAnswer,
)
from worker.jev_config import JEV_MODES, JevConfig, JevThresholds
from worker.jev_eligibility import (
    EligibilityDecision,
    assess_budget,
    assess_state_safety,
    redact_state,
)
from worker.jev_questions import (
    NO_MATCH_OPTION,
    NoulQuestion,
    QuestionSet,
    build_question_set,
    opaque_ids,
)

FEATURE_CLASSIFICATION = "runtime_classification"
FEATURE_HUMAN_ANSWER = "human_answer_matching"
FEATURE_QUALITY_GATE = "issue_quality_gate"

VERDICT_YES = "yes"
VERDICT_NO = "no"
VERDICT_REVIEW = "review"
VERDICT_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class NoulVerdict:
    question_id: str
    noul: float | None
    verdict: str
    thresholds: JevThresholds

    @property
    def decisive(self) -> bool:
        return self.verdict in (VERDICT_YES, VERDICT_NO)

    @property
    def is_yes(self) -> bool:
        return self.verdict == VERDICT_YES

    @property
    def is_no(self) -> bool:
        return self.verdict == VERDICT_NO


@dataclass(frozen=True)
class ChoiceVerdict:
    """Choice is graded on its own distribution; `choice` may be a `none` option."""

    question_id: str
    choice: str
    confidence: float
    probabilities: Mapping[str, float]
    tied: tuple[str, ...]
    min_confidence: float

    @property
    def is_no_match(self) -> bool:
        return self.choice == NO_MATCH_OPTION

    @property
    def decisive(self) -> bool:
        return not self.tied and self.confidence >= self.min_confidence


@dataclass(frozen=True)
class ScoreVerdict:
    question_id: str
    score: float
    max_score: float
    confidence: float
    legend: Mapping[str, Any]
    probabilities: Mapping[str, float]
    min_confidence: float

    @property
    def decisive(self) -> bool:
        return self.confidence >= self.min_confidence

    @property
    def normalized(self) -> float:
        if self.max_score <= 0:
            return 0.0
        return self.score / self.max_score


@dataclass(frozen=True)
class SemanticOutcome:
    """Result of one semantic call, including the audit record."""

    feature: str
    scope: str
    mode: str
    status: str
    fallback_reason: str
    applied: bool
    judgment: SemanticJudgment
    response: JevResponse | None = None
    noul: Mapping[str, NoulVerdict] = field(default_factory=dict)
    choices: Mapping[str, ChoiceVerdict] = field(default_factory=dict)
    scores: Mapping[str, ScoreVerdict] = field(default_factory=dict)

    def noul_verdict(self, question_id: str) -> NoulVerdict | None:
        return self.noul.get(question_id)

    def noul_yes(self, question_id: str) -> bool:
        verdict = self.noul.get(question_id)
        return bool(verdict and verdict.is_yes)

    def noul_no(self, question_id: str) -> bool:
        verdict = self.noul.get(question_id)
        return bool(verdict and verdict.is_no)

    @property
    def all_decisive(self) -> bool:
        """True when no requested judgment landed in the review band."""
        for verdict in self.noul.values():
            if not verdict.decisive:
                return False
        for verdict in self.choices.values():
            if not verdict.decisive:
                return False
        for verdict in self.scores.values():
            if not verdict.decisive:
                return False
        return True


# --------------------------------------------------------------------------
# Primitive-aware evaluation
# --------------------------------------------------------------------------


def evaluate_noul(question_id: str, answer: NoulAnswer | None, thresholds: JevThresholds) -> NoulVerdict:
    if answer is None:
        return NoulVerdict(question_id, None, VERDICT_UNAVAILABLE, thresholds)
    # A negative answer is data, not a failure: `noul` near 0 is a confident no.
    return NoulVerdict(question_id, answer.noul, thresholds.classify(answer.noul), thresholds)


def evaluate_choice(
    question_id: str, answer: ChoiceAnswer | None, min_confidence: float
) -> ChoiceVerdict | None:
    if answer is None:
        return None
    return ChoiceVerdict(
        question_id=question_id,
        choice=answer.choice,
        confidence=answer.confidence,
        probabilities=dict(answer.probabilities),
        tied=tuple(answer.tied),
        min_confidence=min_confidence,
    )


def evaluate_score(
    question_id: str, answer: ScoreAnswer | None, min_confidence: float
) -> ScoreVerdict | None:
    if answer is None:
        return None
    max_score = float(len(answer.level_keys) - 1) if answer.level_keys else 0.0
    return ScoreVerdict(
        question_id=question_id,
        score=answer.score,
        max_score=max_score,
        confidence=answer.confidence,
        legend=dict(answer.legend),
        probabilities=dict(answer.probabilities),
        min_confidence=min_confidence,
    )


class JevSemanticService:
    """Applies mode, thresholds and fallback around the transport client."""

    def __init__(
        self,
        config_loader: Callable[[], Any],
        *,
        audit_sink: Callable[[SemanticJudgment], None] | None = None,
        opener: Any = None,
        allow_insecure_localhost: bool = False,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config_loader = config_loader
        self._audit_sink = audit_sink
        self._opener = opener
        self._allow_insecure_localhost = allow_insecure_localhost
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleeper = sleeper

    # -- configuration -----------------------------------------------------

    def config(self) -> JevConfig:
        raw = self._config_loader() or {}
        return JevConfig.from_app_config(raw)

    def client(self, config: JevConfig) -> JevClient:
        return JevClient(
            config,
            opener=self._opener,
            clock=self._clock,
            sleeper=self._sleeper,
        )

    # -- audit -------------------------------------------------------------

    def _audit(self, judgment: SemanticJudgment) -> None:
        if self._audit_sink is None:
            return
        try:
            self._audit_sink(judgment)
        except Exception:
            # Auditing must never break the caller's flow: the business path
            # already fails open, and a lost sidecar is not worth an exception.
            pass

    def record_skip(
        self,
        *,
        feature: str,
        scope: str,
        reason: str,
        work_id: str = "",
        generation: int | None = None,
    ) -> SemanticJudgment:
        """Audit a call the caller suppressed before reaching the service.

        Message-level eligibility (image-only input, unreadable text) is decided
        by the feature layer, but it still counts toward the fallback rate this
        change exists to measure.
        """
        judgment = skipped_judgment(
            feature=feature,
            scope=scope,
            reason=reason,
            now=self._wall_clock(),
            mode=self.config().mode,
            status="not_eligible",
            work_id=work_id,
            generation=generation,
        )
        self._audit(judgment)
        return judgment

    def record_comparison(
        self,
        *,
        feature: str,
        scope: str,
        comparison: Mapping[str, Any],
        work_id: str = "",
        generation: int | None = None,
    ) -> SemanticJudgment:
        """Record shadow disagreement between Jev and the existing classifier.

        Written as its own lightweight row so the judgment row keeps only what
        Jev actually returned; this row is what threshold calibration reads.
        """
        judgment = SemanticJudgment(
            feature=feature,
            scope=scope,
            status="shadow",
            mode=self.config().mode,
            work_id=work_id,
            generation=generation,
            comparison=dict(comparison),
            created_at=self._wall_clock(),
        )
        self._audit(judgment)
        return judgment

    # -- main entry point --------------------------------------------------

    def ask(
        self,
        *,
        feature: str,
        scope: str,
        state: Any,
        questions: QuestionSet,
        work_id: str = "",
        generation: int | None = None,
        secrets: Sequence[str] = (),
        comparison: Mapping[str, Any] | None = None,
    ) -> SemanticOutcome:
        config = self.config()
        mode = config.mode if config.mode in JEV_MODES else "off"

        if not config.active(feature):
            reason = "feature_disabled" if config.enabled and mode != "off" else "disabled"
            judgment = skipped_judgment(
                feature=feature,
                scope=scope,
                reason=reason,
                now=self._wall_clock(),
                mode=mode,
                status="disabled",
                work_id=work_id,
                generation=generation,
            )
            self._audit(judgment)
            return SemanticOutcome(feature, scope, mode, "disabled", reason, False, judgment)

        safe_state = redact_state(state, secrets)
        denial = self._preflight(safe_state, questions, config)
        if denial is not None:
            judgment = skipped_judgment(
                feature=feature,
                scope=scope,
                reason=denial.reason,
                now=self._wall_clock(),
                mode=mode,
                status="not_eligible",
                work_id=work_id,
                generation=generation,
            )
            self._audit(judgment)
            return SemanticOutcome(
                feature, scope, mode, "not_eligible", denial.reason, False, judgment
            )

        hashed = compute_state_hash(safe_state)
        started = self._clock()
        try:
            response = self.client(config).evaluate(
                safe_state,
                questions,
                allow_insecure_localhost=self._allow_insecure_localhost,
            )
        except JevError as exc:
            judgment = record_from_error(
                exc,
                feature=feature,
                scope=scope,
                mode=mode,
                now=self._wall_clock(),
                requested_model=config.model,
                latency_ms=int((self._clock() - started) * 1000),
                hashed_state=hashed,
                work_id=work_id,
                generation=generation,
            )
            self._audit(judgment)
            # Fail open: the caller keeps whatever the existing path produced.
            return SemanticOutcome(feature, scope, mode, exc.kind, exc.kind, False, judgment)

        noul, choices, scores = self._evaluate_answers(config, feature, questions, response)
        judgment = record_from_response(
            response,
            feature=feature,
            scope=scope,
            mode=mode,
            now=self._wall_clock(),
            work_id=work_id,
            generation=generation,
            hashed_state=hashed,
            status="shadow" if config.shadows() else "ok",
            comparison=comparison,
        )
        self._audit(judgment)

        if config.shadows():
            # Shadow records the disagreement but must not change any label,
            # status, snapshot or sync result.
            return SemanticOutcome(
                feature, scope, mode, "shadow", "", False, judgment, response, noul, choices, scores
            )

        review = self._review_reason(noul, choices, scores)
        if review:
            judgment = replace(judgment, status="review", fallback_reason=review)
            self._audit(judgment)
            return SemanticOutcome(
                feature, scope, mode, "review", review, False, judgment, response, noul, choices, scores
            )

        return SemanticOutcome(
            feature, scope, mode, "ok", "", True, judgment, response, noul, choices, scores
        )

    def _preflight(self, state: Any, questions: QuestionSet, config: JevConfig) -> EligibilityDecision | None:
        for decision in (assess_state_safety(state), assess_budget(state, questions, config)):
            if not decision.eligible:
                return decision
        return None

    def _evaluate_answers(
        self, config: JevConfig, feature: str, questions: QuestionSet, response: JevResponse
    ) -> tuple[dict[str, NoulVerdict], dict[str, ChoiceVerdict], dict[str, ScoreVerdict]]:
        thresholds = config.thresholds_for(feature)
        # Choice/Score gate on their own confidence, never on a Noul cut point.
        min_confidence = thresholds.yes
        noul: dict[str, NoulVerdict] = {}
        choices: dict[str, ChoiceVerdict] = {}
        scores: dict[str, ScoreVerdict] = {}
        for question_id, expected_type in questions.types.items():
            answer = response.answers.get(question_id)
            if expected_type == "noul":
                noul[question_id] = evaluate_noul(
                    question_id, answer if isinstance(answer, NoulAnswer) else None, thresholds
                )
            elif expected_type == "choice":
                verdict = evaluate_choice(
                    question_id, answer if isinstance(answer, ChoiceAnswer) else None, min_confidence
                )
                if verdict is not None:
                    choices[question_id] = verdict
            else:
                verdict = evaluate_score(
                    question_id, answer if isinstance(answer, ScoreAnswer) else None, min_confidence
                )
                if verdict is not None:
                    scores[question_id] = verdict
        return noul, choices, scores

    def _review_reason(
        self,
        noul: Mapping[str, NoulVerdict],
        choices: Mapping[str, ChoiceVerdict],
        scores: Mapping[str, ScoreVerdict],
    ) -> str:
        if any(not verdict.decisive for verdict in noul.values()):
            return "review_interval"
        if any(not verdict.decisive for verdict in choices.values()):
            return "review_interval"
        if any(not verdict.decisive for verdict in scores.values()):
            return "review_interval"
        return ""


# --------------------------------------------------------------------------
# Feature-specific question definitions
# --------------------------------------------------------------------------

CLASSIFICATION_LABELS: tuple[str, ...] = (
    "is_question",
    "is_supplement",
    "is_human_answer",
    "is_withdrawn",
    "is_chat",
)


def classification_questions() -> QuestionSet:
    """Independent, non-complementary label questions.

    The labels overlap on purpose, so each is asked separately: deriving
    ``is_chat`` from ``1 - is_question`` would lose exactly the ambiguity this
    feature exists to resolve.
    """
    return build_question_set(
        [
            (
                "is_question",
                NoulQuestion(
                    instructions="Does `message.text` raise a new question that needs a reply?",
                    criteria={"true": "提出了需要回复的新问题", "false": "没有提出新问题"},
                ),
            ),
            (
                "is_supplement",
                NoulQuestion(
                    instructions="Does `message.text` add information to the open question in `open_question`?",
                    criteria={"true": "补充了已有问题的信息", "false": "与已有问题无关"},
                ),
            ),
            (
                "is_human_answer",
                NoulQuestion(
                    instructions="Does `message.text` resolve the open question in `open_question`?",
                    criteria={"true": "明确回答了该问题", "false": "没有回答该问题"},
                ),
            ),
            (
                "is_withdrawn",
                NoulQuestion(
                    instructions="Does `message.text` withdraw or cancel the open question?",
                    criteria={"true": "撤回或取消了问题", "false": "没有撤回"},
                ),
            ),
            (
                "is_chat",
                NoulQuestion(
                    instructions="Is `message.text` casual conversation, acknowledgement or off-topic chatter?",
                    criteria={"true": "闲聊或无关内容", "false": "包含实际业务内容"},
                ),
            ),
        ]
    )


@dataclass(frozen=True)
class CandidateMatchQuestions:
    """Opaque-ID question map plus the business mapping kept worker-side."""

    questions: QuestionSet
    work_by_question_id: Mapping[str, str]


def human_answer_questions(candidates: Sequence[tuple[str, str]]) -> CandidateMatchQuestions:
    """One independent Noul per candidate, keyed by an opaque ``qN`` id.

    The candidate's own question text goes into the rubric, not into the
    question ID: IDs carry no meaning to the model.
    """
    entries: list[tuple[str, NoulQuestion]] = []
    work_by_question_id: dict[str, str] = {}
    for opaque_id, (work_id, question_text) in zip(opaque_ids(len(candidates)), candidates):
        question_id = f"does_message_answer_{opaque_id}"
        entries.append(
            (
                question_id,
                NoulQuestion(
                    instructions=(
                        f"Does `message.text` answer the open question {opaque_id} "
                        f"described in `candidates.{opaque_id}`?"
                    ),
                    criteria={
                        "true": "该消息回答了这个候选问题",
                        "false": "该消息没有回答这个候选问题",
                    },
                ),
            )
        )
        work_by_question_id[question_id] = work_id
    return CandidateMatchQuestions(
        questions=build_question_set(entries), work_by_question_id=work_by_question_id
    )


QUALITY_GATE_QUESTIONS: tuple[str, ...] = (
    "is_real_issue",
    "evidence_sufficient",
    "context_is_one_issue",
)


def quality_gate_questions() -> QuestionSet:
    return build_question_set(
        [
            (
                "is_real_issue",
                NoulQuestion(
                    instructions="Does `candidate` describe a real problem worth tracking?",
                    criteria={"true": "是真实问题", "false": "寒暄、通知或简单确认"},
                ),
            ),
            (
                "evidence_sufficient",
                NoulQuestion(
                    instructions="Do `candidate.messages` contain enough readable evidence to describe the problem?",
                    criteria={"true": "证据充分", "false": "证据不足"},
                ),
            ),
            (
                "context_is_one_issue",
                NoulQuestion(
                    instructions="Do `candidate.messages` describe a single issue rather than several?",
                    criteria={"true": "围绕同一个问题", "false": "混合了多个问题"},
                ),
            ),
        ]
    )


def state_payload(*, instructions: str, **fields: Any) -> dict[str, Any]:
    """Named state object; questions reference these fields by backticked path."""
    return {"instructions": instructions, **fields}


def candidate_state(
    message_text: str, candidates: Sequence[tuple[str, str]]
) -> dict[str, Any]:
    """State for human-answer matching.

    ``candidates`` is the same ``(work_id, question_text)`` sequence accepted by
    :func:`human_answer_questions`; both derive their opaque ``qN`` keys with
    :func:`opaque_ids`, so a caller builds one list and passes it to both. The
    raw work ID stays worker-side; the model only ever sees the opaque key, with
    the candidate's actual wording carried in the state.
    """
    return {
        "instructions": (
            "`message.text` is the new group message. `candidates.<id>.question` lists the "
            "open questions; judge each one independently."
        ),
        "message": {"text": message_text},
        "candidates": {
            opaque_id: {"question": question_text}
            for opaque_id, (_work_id, question_text) in zip(
                opaque_ids(len(candidates)), candidates
            )
        },
    }


