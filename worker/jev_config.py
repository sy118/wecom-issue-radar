"""Jev semantic-assist configuration.

Jev is an optional text/state -> typed-judgment sidecar. It never owns work
state, never reads image pixels and never generates natural language: it only
answers bounded questions with probabilities that code maps back onto the
existing deterministic state machine.

This module is deliberately offline. It normalizes configuration only; the
transport, question and audit layers live in their own modules.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Literal, Mapping

JevMode = Literal["off", "shadow", "assist"]
JevFeature = Literal[
    "runtime_classification",
    "human_answer_matching",
    "issue_quality_gate",
]

JEV_MODES: tuple[str, ...] = ("off", "shadow", "assist")
JEV_FEATURES: tuple[str, ...] = (
    "runtime_classification",
    "human_answer_matching",
    "issue_quality_gate",
)
DEFAULT_JEV_MODE = "shadow"

# Terminal outcome of one semantic call, recorded in the audit sidecar.
JUDGMENT_STATUSES: tuple[str, ...] = (
    "ok",
    "disabled",
    "not_eligible",
    "shadow",
    "timeout",
    "service_unavailable",
    "config_error",
    "invalid_response",
    "review",
)

# Why a feature fell back to the existing LLM / deterministic path. A
# high-confidence negative is *not* a fallback reason: it is a valid answer.
FALLBACK_REASONS: tuple[str, ...] = (
    "",
    "disabled",
    "feature_disabled",
    "not_eligible",
    "image_only",
    "too_large",
    "review_interval",
    "timeout",
    "rate_limited",
    "service_unavailable",
    "config_error",
    "invalid_response",
    "stale_generation",
    "state_changed",
    "ambiguous_match",
)

DEFAULT_JEV_CONFIG: dict[str, Any] = {
    "enabled": False,
    "mode": DEFAULT_JEV_MODE,
    "base_url": "",
    "api_key": "",
    "model": "jev-latest",
    "timeout_seconds": 30,
    "total_budget_seconds": 30,
    "max_state_chars": 6000,
    # Global Noul defaults. A probability >= decision_threshold is a
    # high-confidence yes; <= review_floor is a high-confidence no; anything
    # in between is the review band and always falls back.
    "decision_threshold": 0.7,
    "review_floor": 0.3,
    "max_candidates": 10,
    "max_retries": 2,
    "retry_backoff_seconds": 0.5,
    "max_retry_backoff_seconds": 8.0,
    "thresholds": {name: {} for name in JEV_FEATURES},
    "features": {name: False for name in JEV_FEATURES},
}

# Official System One limits, used only as a conservative local preflight.
MAX_QUESTIONS_PER_REQUEST = 32
MAX_CHOICE_OPTIONS = 255
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10
TOTAL_TOKEN_BUDGET = 64_000
STATE_PLUS_LONGEST_QUESTION_TOKEN_BUDGET = 32_000


def _bounded_float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(minimum, min(maximum, number))


def _bounded_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def normalize_jev_config(raw: Any) -> dict[str, Any]:
    """Merge ``raw`` over the defaults and clamp every value into a safe range.

    Unknown keys are dropped so a hand-edited config cannot smuggle arbitrary
    values into the request path. An unrecognized ``mode`` degrades to ``off``
    (fail safe: never call out on ambiguous intent).
    """
    source = raw if isinstance(raw, Mapping) else {}
    config = copy.deepcopy(DEFAULT_JEV_CONFIG)

    config["enabled"] = bool(source.get("enabled", DEFAULT_JEV_CONFIG["enabled"]))
    mode = str(source.get("mode", DEFAULT_JEV_CONFIG["mode"]) or "").strip().lower()
    config["mode"] = mode if mode in JEV_MODES else "off"
    config["base_url"] = str(source.get("base_url") or "").strip()
    config["api_key"] = str(source.get("api_key") or "").strip()
    config["model"] = str(source.get("model") or DEFAULT_JEV_CONFIG["model"]).strip()

    config["timeout_seconds"] = _bounded_int(
        source.get("timeout_seconds"), DEFAULT_JEV_CONFIG["timeout_seconds"], minimum=1, maximum=300
    )
    config["total_budget_seconds"] = _bounded_int(
        source.get("total_budget_seconds"),
        DEFAULT_JEV_CONFIG["total_budget_seconds"],
        minimum=1,
        maximum=600,
    )
    config["max_state_chars"] = _bounded_int(
        source.get("max_state_chars"), DEFAULT_JEV_CONFIG["max_state_chars"], minimum=200, maximum=100_000
    )
    config["max_candidates"] = _bounded_int(
        source.get("max_candidates"), DEFAULT_JEV_CONFIG["max_candidates"], minimum=1, maximum=50
    )
    config["max_retries"] = _bounded_int(
        source.get("max_retries"), DEFAULT_JEV_CONFIG["max_retries"], minimum=0, maximum=5
    )
    config["retry_backoff_seconds"] = _bounded_float(
        source.get("retry_backoff_seconds"),
        DEFAULT_JEV_CONFIG["retry_backoff_seconds"],
        minimum=0.0,
        maximum=30.0,
    )
    max_backoff = _bounded_float(
        source.get("max_retry_backoff_seconds"),
        DEFAULT_JEV_CONFIG["max_retry_backoff_seconds"],
        minimum=0.0,
        maximum=120.0,
    )
    config["max_retry_backoff_seconds"] = max(max_backoff, config["retry_backoff_seconds"])

    review_floor = _bounded_float(
        source.get("review_floor"), DEFAULT_JEV_CONFIG["review_floor"], minimum=0.0, maximum=1.0
    )
    decision_threshold = _bounded_float(
        source.get("decision_threshold"),
        DEFAULT_JEV_CONFIG["decision_threshold"],
        minimum=0.0,
        maximum=1.0,
    )
    # A floor above the decision threshold would make the review band empty and
    # silently turn every mid-range judgment into a confident answer.
    config["decision_threshold"] = decision_threshold
    config["review_floor"] = min(review_floor, decision_threshold)

    raw_thresholds = source.get("thresholds")
    thresholds: dict[str, dict[str, float]] = {}
    for name in JEV_FEATURES:
        override = raw_thresholds.get(name) if isinstance(raw_thresholds, Mapping) else None
        override = override if isinstance(override, Mapping) else {}
        yes = _bounded_float(
            override.get("yes"), config["decision_threshold"], minimum=0.0, maximum=1.0
        )
        no = _bounded_float(override.get("no"), config["review_floor"], minimum=0.0, maximum=1.0)
        thresholds[name] = {"yes": yes, "no": min(no, yes)}
    config["thresholds"] = thresholds

    raw_features = source.get("features")
    features = raw_features if isinstance(raw_features, Mapping) else {}
    config["features"] = {
        name: bool(features.get(name, DEFAULT_JEV_CONFIG["features"][name]))
        for name in JEV_FEATURES
    }
    return config


@dataclass(frozen=True)
class JevThresholds:
    """Per-feature Noul cut points in [0, 1]."""

    yes: float
    no: float

    def classify(self, noul: float | None) -> str:
        """Return ``yes`` / ``no`` / ``review`` for one Noul probability."""
        if noul is None:
            return "review"
        try:
            value = float(noul)
        except (TypeError, ValueError):
            return "review"
        if value != value:
            return "review"
        if value >= self.yes:
            return "yes"
        if value <= self.no:
            return "no"
        return "review"


@dataclass(frozen=True)
class JevConfig:
    """Immutable view over a normalized ``jev`` config object."""

    enabled: bool = False
    mode: str = DEFAULT_JEV_MODE
    base_url: str = ""
    api_key: str = ""
    model: str = "jev-latest"
    timeout_seconds: int = 30
    total_budget_seconds: int = 30
    max_state_chars: int = 6000
    decision_threshold: float = 0.7
    review_floor: float = 0.3
    max_candidates: int = 10
    max_retries: int = 2
    retry_backoff_seconds: float = 0.5
    max_retry_backoff_seconds: float = 8.0
    thresholds: Mapping[str, Any] = None  # type: ignore[assignment]
    features: Mapping[str, bool] = None  # type: ignore[assignment]

    @classmethod
    def from_mapping(cls, raw: Any) -> "JevConfig":
        config = normalize_jev_config(raw)
        return cls(
            enabled=config["enabled"],
            mode=config["mode"],
            base_url=config["base_url"],
            api_key=config["api_key"],
            model=config["model"],
            timeout_seconds=config["timeout_seconds"],
            total_budget_seconds=config["total_budget_seconds"],
            max_state_chars=config["max_state_chars"],
            decision_threshold=config["decision_threshold"],
            review_floor=config["review_floor"],
            max_candidates=config["max_candidates"],
            max_retries=config["max_retries"],
            retry_backoff_seconds=config["retry_backoff_seconds"],
            max_retry_backoff_seconds=config["max_retry_backoff_seconds"],
            thresholds=copy.deepcopy(config["thresholds"]),
            features=dict(config["features"]),
        )

    @classmethod
    def from_app_config(cls, config: Any) -> "JevConfig":
        source = config if isinstance(config, Mapping) else {}
        return cls.from_mapping(source.get("jev"))

    @property
    def configured(self) -> bool:
        """True when a request could actually be issued."""
        return bool(self.enabled and self.mode != "off" and self.base_url and self.api_key)

    def feature_enabled(self, feature: str) -> bool:
        return bool(self.features.get(feature))

    def active(self, feature: str) -> bool:
        """True when ``feature`` may issue requests in the current mode."""
        return self.configured and self.feature_enabled(feature)

    def shadows(self) -> bool:
        return self.mode == "shadow"

    def decides(self) -> bool:
        return self.mode == "assist"

    def thresholds_for(self, feature: str) -> JevThresholds:
        """Resolve per-feature cut points, falling back to the global pair."""
        override = self.thresholds.get(feature) if isinstance(self.thresholds, Mapping) else None
        override = override if isinstance(override, Mapping) else {}
        yes = _bounded_float(
            override.get("yes"), self.decision_threshold, minimum=0.0, maximum=1.0
        )
        no = _bounded_float(override.get("no"), self.review_floor, minimum=0.0, maximum=1.0)
        return JevThresholds(yes=yes, no=min(no, yes))
