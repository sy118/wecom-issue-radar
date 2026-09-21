"""System One HTTP transport for Jev.

The client speaks TypeSafe's native ``POST /v1/systemone`` contract. It is
deliberately *not* a Chat Completions client: there is no ``messages`` field, no
generated text and no streaming. Responses are typed judgments with
probabilities that are validated against the request's own question map before
they are allowed anywhere near business state.

Layering:

1. endpoint / header builder -- one normalized URL, Bearer auth;
2. question builder -- lives in :mod:`worker.jev_questions`;
3. typed response parser -- strict on decision-affecting fields;
4. semantic normalization -- raw typed answers preserved for audit.

Nothing here writes business state, and the API key never leaves this module.
"""

from __future__ import annotations

import json
import math
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from worker.jev_config import JevConfig
from worker.jev_questions import (
    NO_MATCH_OPTION,
    QuestionSet,
    ScoreQuestion,
    ChoiceQuestion,
    NoulQuestion,
    build_question_set,
)

SYSTEMONE_PATH = "/v1/systemone"

# Localhost is the only host allowed over plain HTTP, and only when the caller
# explicitly opts in (an on-prem gateway during development).
_INSECURE_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}

# Probabilities keyed by option/level must sum to ~1. 255 options of float
# rounding still land well inside this window.
PROBABILITY_SUM_TOLERANCE = 5e-3
# `score` is the level-probability-weighted position on the rubric.
SCORE_WEIGHT_TOLERANCE = 1e-2
# Two options whose maxima differ by less than this count as a tie.
TIE_TOLERANCE = 1e-6

# Answer-bearing Chat Completions fields. Their presence means we are talking to
# the wrong endpoint, not that Jev answered us.
_CHAT_COMPLETIONS_FIELDS = ("choices", "message", "delta", "finish_reason")

# Retryable by contract. Everything else 4xx is a caller/config error.
_RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})
_NON_RETRYABLE_STATUSES = frozenset({400, 401, 403, 404, 422})

_TYPE_NOUL = "noul"
_TYPE_CHOICE = "choice"
_TYPE_SCORE = "score"


class JevError(Exception):
    """Transport/contract failure. ``kind`` maps onto an audit status."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            "status": self.status,
            "retryable": self.retryable,
        }


class JevResponseError(JevError):
    def __init__(self, message: str) -> None:
        super().__init__("invalid_response", message)


# --------------------------------------------------------------------------
# 1. Endpoint and headers
# --------------------------------------------------------------------------


def normalize_endpoint(base_url: str, *, allow_insecure_localhost: bool = False) -> str:
    """Normalize any configured address to exactly one ``/v1/systemone`` URL.

    Accepts a service root, a ``/v1`` address, or the full endpoint. Query
    strings and fragments are dropped rather than forwarded: they could carry
    credentials or be mistaken for the API key.
    """
    raw = str(base_url or "").strip()
    if not raw:
        raise JevError("config_error", "Jev base_url is not configured")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise JevError("config_error", "Jev base_url must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise JevError("config_error", "Jev base_url must not embed credentials")
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" and host not in _INSECURE_HOSTS:
        raise JevError("config_error", "Jev base_url must use HTTPS")
    if parsed.scheme == "http" and not allow_insecure_localhost:
        raise JevError(
            "config_error",
            "plain-HTTP Jev endpoints are limited to an explicitly allowed localhost",
        )

    path = parsed.path.rstrip("/")
    if path.endswith(SYSTEMONE_PATH):
        normalized_path = path
    elif path.endswith("/v1"):
        normalized_path = f"{path}{SYSTEMONE_PATH[len('/v1'):]}"
    else:
        normalized_path = f"{path}{SYSTEMONE_PATH}"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))


def build_headers(api_key: str, correlation_id: str) -> dict[str, str]:
    key = str(api_key or "").strip()
    if not key:
        raise JevError("config_error", "Jev API key is not configured")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        # Client-side correlation only; the provider request id is separate.
        "X-Client-Request-Id": correlation_id,
    }


def build_request_body(
    state: Any,
    config: JevConfig,
    questions: QuestionSet,
    *,
    model: str | None = None,
) -> dict[str, Any]:
    """Build the ``{state, model, questions}`` envelope.

    Only these three top-level keys are ever sent. Chat Completions parameters
    (``messages``, ``temperature``, ``max_tokens``, ``response_format``) are
    absent by construction.
    """
    return {
        "state": state,
        "model": str(model or config.model or "jev-latest"),
        "questions": questions.payload(),
    }


# --------------------------------------------------------------------------
# 3. Typed answers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class NoulAnswer:
    """A yes/no judgment. ``noul`` *is* the confidence: there is no separate one."""

    noul: float
    raw: Mapping[str, Any] = field(default_factory=dict)
    type: str = _TYPE_NOUL


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    probabilities: Mapping[str, float]
    confidence: float
    tied: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)
    type: str = _TYPE_CHOICE

    @property
    def is_tied(self) -> bool:
        return len(self.tied) > 1


@dataclass(frozen=True)
class ScoreAnswer:
    score: float
    legend: Mapping[str, Any]
    probabilities: Mapping[str, float]
    confidence: float
    level_keys: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)
    type: str = _TYPE_SCORE


TypedAnswer = NoulAnswer | ChoiceAnswer | ScoreAnswer


@dataclass(frozen=True)
class JevResponse:
    """A validated System One response."""

    answers: Mapping[str, TypedAnswer]
    model: str = ""
    requested_model: str = ""
    usage: Mapping[str, Any] | None = None
    request_id: str = ""
    correlation_id: str = ""
    latency_ms: int = 0
    attempts: int = 1
    retry_after: float | None = None

    def noul(self, question_id: str) -> float | None:
        answer = self.answers.get(question_id)
        return answer.noul if isinstance(answer, NoulAnswer) else None

    def choice(self, question_id: str) -> ChoiceAnswer | None:
        answer = self.answers.get(question_id)
        return answer if isinstance(answer, ChoiceAnswer) else None

    def score(self, question_id: str) -> ScoreAnswer | None:
        answer = self.answers.get(question_id)
        return answer if isinstance(answer, ScoreAnswer) else None


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _require_probability(value: Any, label: str) -> float:
    number = _finite_number(value)
    if number is None or not 0.0 <= number <= 1.0:
        raise JevResponseError(f"{label} must be a finite probability in [0, 1]")
    return number


def _require_distribution(value: Any, expected_keys: Sequence[str], label: str) -> dict[str, float]:
    """Every option/level key must be present exactly once and sum to ~1."""
    if not isinstance(value, Mapping):
        raise JevResponseError(f"{label} must be an object of probabilities")
    provided = {str(key) for key in value}
    expected = {str(key) for key in expected_keys}
    if provided != expected:
        missing = sorted(expected - provided)
        unknown = sorted(provided - expected)
        raise JevResponseError(
            f"{label} keys must match the request exactly (missing={missing}, unknown={unknown})"
        )
    distribution: dict[str, float] = {}
    for key, raw in value.items():
        distribution[str(key)] = _require_probability(raw, f"{label}.{key}")
    total = math.fsum(distribution.values())
    if abs(total - 1.0) > PROBABILITY_SUM_TOLERANCE:
        raise JevResponseError(f"{label} must sum to 1, got {total:.6f}")
    return distribution


def _argmax_ties(distribution: Mapping[str, float]) -> tuple[str, ...]:
    if not distribution:
        return ()
    best = max(distribution.values())
    tied = tuple(sorted(key for key, value in distribution.items() if best - value <= TIE_TOLERANCE))
    return tied


def _parse_noul(question_id: str, payload: Mapping[str, Any]) -> NoulAnswer:
    if "noul" not in payload:
        raise JevResponseError(f"noul answer '{question_id}' is missing 'noul'")
    value = _require_probability(payload["noul"], f"answers.{question_id}.noul")
    return NoulAnswer(noul=value, raw=dict(payload))


def _parse_choice(question_id: str, payload: Mapping[str, Any], option_ids: Sequence[str]) -> ChoiceAnswer:
    for key in ("choice", "probabilities", "confidence"):
        if key not in payload:
            raise JevResponseError(f"choice answer '{question_id}' is missing '{key}'")
    choice = payload["choice"]
    if not isinstance(choice, str) or choice not in {str(item) for item in option_ids}:
        raise JevResponseError(
            f"choice answer '{question_id}' returned an option outside the request criteria"
        )
    probabilities = _require_distribution(
        payload["probabilities"], option_ids, f"answers.{question_id}.probabilities"
    )
    confidence = _require_probability(payload["confidence"], f"answers.{question_id}.confidence")
    tied = _argmax_ties(probabilities)
    # A tie is a review signal, not something the client may resolve by picking
    # arbitrarily; the deterministic ordering lives in the feature layer.
    if tied and choice not in tied:
        raise JevResponseError(
            f"choice answer '{question_id}' disagrees with its own distribution maximum"
        )
    return ChoiceAnswer(
        choice=choice,
        probabilities=probabilities,
        confidence=confidence,
        tied=tied,
        raw=dict(payload),
    )


def _parse_score(question_id: str, payload: Mapping[str, Any], level_count: int) -> ScoreAnswer:
    for key in ("score", "legend", "probabilities", "confidence"):
        if key not in payload:
            raise JevResponseError(f"score answer '{question_id}' is missing '{key}'")
    level_keys = tuple(str(index) for index in range(level_count))
    probabilities = _require_distribution(
        payload["probabilities"], level_keys, f"answers.{question_id}.probabilities"
    )
    legend = payload["legend"]
    if not isinstance(legend, Mapping):
        raise JevResponseError(f"answers.{question_id}.legend must be an object")
    if {str(key) for key in legend} != set(level_keys):
        raise JevResponseError(
            f"answers.{question_id}.legend keys must match the criteria levels exactly"
        )
    score = _finite_number(payload["score"])
    if score is None:
        raise JevResponseError(f"answers.{question_id}.score must be a finite number")
    upper = float(level_count - 1)
    if not 0.0 <= score <= upper:
        raise JevResponseError(
            f"answers.{question_id}.score must fall between 0 and {upper:.0f}, got {score}"
        )
    weighted = math.fsum(float(key) * value for key, value in probabilities.items())
    if abs(score - weighted) > SCORE_WEIGHT_TOLERANCE:
        raise JevResponseError(
            f"answers.{question_id}.score ({score}) is not consistent with its weighted value ({weighted:.6f})"
        )
    confidence = _require_probability(payload["confidence"], f"answers.{question_id}.confidence")
    # Legend values may be structured; keep them verbatim rather than coercing.
    normalized_legend = {str(key): legend[key] for key in legend}
    return ScoreAnswer(
        score=score,
        legend=normalized_legend,
        probabilities=probabilities,
        confidence=confidence,
        level_keys=level_keys,
        raw=dict(payload),
    )


def parse_response(
    payload: Any,
    questions: QuestionSet,
    *,
    requested_model: str = "",
    correlation_id: str = "",
    latency_ms: int = 0,
    attempts: int = 1,
    request_id: str = "",
) -> JevResponse:
    """Validate a System One response against the request's question map.

    Strict on anything that feeds a business decision; lenient on ``model`` and
    ``usage``, which are metadata and must not cost us a structurally valid
    answer set.
    """
    if not isinstance(payload, Mapping):
        raise JevResponseError("Jev response must be a JSON object")
    for key in _CHAT_COMPLETIONS_FIELDS:
        if key in payload:
            raise JevResponseError(
                f"Jev response contains Chat Completions field '{key}'; the endpoint is not System One"
            )
    if "answers" not in payload:
        raise JevResponseError("Jev response is missing 'answers'")
    raw_answers = payload["answers"]
    if not isinstance(raw_answers, Mapping) or not raw_answers:
        raise JevResponseError("'answers' must be a non-empty object")

    expected_ids = set(questions.types)
    provided_ids = {str(key) for key in raw_answers}
    if provided_ids != expected_ids:
        missing = sorted(expected_ids - provided_ids)
        unknown = sorted(provided_ids - expected_ids)
        raise JevResponseError(
            f"'answers' must cover the request exactly (missing={missing}, unknown={unknown})"
        )

    answers: dict[str, TypedAnswer] = {}
    for question_id in questions.ids():
        raw = raw_answers[question_id]
        if not isinstance(raw, Mapping):
            raise JevResponseError(f"answer '{question_id}' must be an object")
        expected_type = questions.types[question_id]
        actual_type = raw.get("type")
        if actual_type != expected_type:
            raise JevResponseError(
                f"answer '{question_id}' type mismatch: expected '{expected_type}', got '{actual_type}'"
            )
        definition = questions.questions[question_id]
        if expected_type == _TYPE_NOUL:
            answers[question_id] = _parse_noul(question_id, raw)
        elif expected_type == _TYPE_CHOICE:
            answers[question_id] = _parse_choice(
                question_id, raw, list(definition["criteria"].keys())
            )
        else:
            answers[question_id] = _parse_score(
                question_id, raw, len(definition["criteria"])
            )

    model = payload.get("model")
    usage = payload.get("usage")
    return JevResponse(
        answers=answers,
        model=str(model) if isinstance(model, str) else "",
        requested_model=requested_model,
        usage=usage if isinstance(usage, Mapping) else None,
        request_id=request_id,
        correlation_id=correlation_id,
        latency_ms=latency_ms,
        attempts=attempts,
    )


# --------------------------------------------------------------------------
# 2. Retry policy
# --------------------------------------------------------------------------


def header_value(headers: Any, name: str) -> str:
    """Case-insensitive header lookup.

    Real urllib responses expose a case-insensitive ``HTTPMessage``, but any
    other mapping must not silently lose the provider request id just because it
    spelled the header differently.
    """
    if headers is None:
        return ""
    try:
        value = headers.get(name)
    except AttributeError:
        return ""
    if value not in (None, ""):
        return str(value).strip()
    try:
        items = list(headers.items())
    except AttributeError:
        return ""
    target = name.lower()
    for key, candidate in items:
        if str(key).lower() == target and candidate not in (None, ""):
            return str(candidate).strip()
    return ""


def _parse_retry_after(headers: Any, now: float) -> float | None:
    """Read ``retry-after-ms`` (preferred) or ``Retry-After`` (seconds/HTTP-date)."""

    def header(name: str) -> str | None:
        try:
            value = headers.get(name)
        except AttributeError:
            return None
        return str(value).strip() if value not in (None, "") else None

    milliseconds = header("retry-after-ms")
    if milliseconds is not None:
        try:
            return max(0.0, float(milliseconds) / 1000.0)
        except ValueError:
            pass

    raw = header("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    # HTTP-date form.
    try:
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(raw)
        if target is None:
            return None
        return max(0.0, target.timestamp() - now)
    except (TypeError, ValueError, OverflowError):
        return None


def _backoff_delay(config: JevConfig, attempt: int) -> float:
    base = config.retry_backoff_seconds * (2 ** max(0, attempt - 1))
    capped = min(base, config.max_retry_backoff_seconds)
    # Jitter avoids a synchronized retry stampede across workers.
    return capped * (0.5 + random.random() / 2)


def _classify_http_error(status: int, headers: Any, now: float) -> JevError:
    retry_after = _parse_retry_after(headers, now)
    if status in (401, 403):
        return JevError(
            "config_error",
            f"Jev rejected the API key (HTTP {status})",
            status=status,
            retryable=False,
        )
    if status == 422:
        return JevError(
            "config_error",
            "Jev rejected the request schema (HTTP 422)",
            status=status,
            retryable=False,
        )
    if status in _NON_RETRYABLE_STATUSES:
        return JevError(
            "config_error", f"Jev request failed (HTTP {status})", status=status, retryable=False
        )
    if status in _RETRYABLE_STATUSES:
        kind = "rate_limited" if status in (429, 529) else "service_unavailable"
        return JevError(
            kind,
            f"Jev is temporarily unavailable (HTTP {status})",
            status=status,
            retryable=True,
            retry_after=retry_after,
        )
    return JevError(
        "service_unavailable", f"Jev returned an unexpected status (HTTP {status})", status=status
    )


class _NoCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to carry the Bearer token to a different host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        origin = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(newurl)
        if (origin.scheme, origin.netloc) != (target.scheme, target.netloc):
            raise JevError(
                "config_error",
                "Jev endpoint attempted a cross-host redirect; refusing to forward credentials",
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class JevClient:
    """Stateless-per-call System One client.

    One instance may serve many features; every :meth:`evaluate` call carries
    its own correlation id and retry budget.
    """

    def __init__(self, config: JevConfig, *, opener: Any = None, clock=time.monotonic, sleeper=time.sleep) -> None:
        self.config = config
        self._opener = opener or urllib.request.build_opener(_NoCrossHostRedirect())
        self._clock = clock
        self._sleep = sleeper

    # -- transport ---------------------------------------------------------

    def _post(self, url: str, body: dict[str, Any], headers: dict[str, str], timeout: int) -> tuple[Any, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")), response.headers
        except urllib.error.HTTPError as exc:
            raise _classify_http_error(int(exc.code), exc.headers, self._clock()) from None
        except (socket.timeout, TimeoutError) as exc:
            raise JevError("timeout", "Jev request timed out", retryable=True) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise JevError("timeout", "Jev request timed out", retryable=True) from exc
            raise JevError(
                "service_unavailable", "Jev endpoint is unreachable", retryable=True
            ) from exc
        except json.JSONDecodeError as exc:
            raise JevError("invalid_response", "Jev returned a non-JSON body") from exc

    def evaluate(
        self,
        state: Any,
        questions: QuestionSet,
        *,
        model: str | None = None,
        allow_insecure_localhost: bool = False,
    ) -> JevResponse:
        """Issue one System One request, retrying only within the total budget."""
        if not questions.types:
            raise JevError("config_error", "a Jev request requires at least one question")
        url = normalize_endpoint(self.config.base_url, allow_insecure_localhost=allow_insecure_localhost)
        correlation_id = uuid.uuid4().hex
        headers = build_headers(self.config.api_key, correlation_id)
        body = build_request_body(state, self.config, questions, model=model)
        requested_model = str(body["model"])

        deadline = self._clock() + float(self.config.total_budget_seconds)
        attempts = 0
        last_error: JevError | None = None
        last_retry_after: float | None = None

        while attempts <= self.config.max_retries:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise JevError(
                    "timeout",
                    "Jev retry budget was exhausted before the request could be sent",
                    retryable=False,
                )
            attempts += 1
            started = self._clock()
            try:
                payload, response_headers = self._post(
                    url, body, headers, min(int(self.config.timeout_seconds), max(1, int(remaining)))
                )
            except JevError as exc:
                last_error = exc
                last_retry_after = exc.retry_after
                if not exc.retryable or attempts > self.config.max_retries:
                    raise
                remaining_budget = deadline - self._clock()
                # Only an exhausted budget stops the retry. A zero delay is a
                # legitimate configuration (no backoff) and must still retry.
                if remaining_budget <= 0:
                    raise
                delay = exc.retry_after if exc.retry_after is not None else _backoff_delay(self.config, attempts)
                delay = max(0.0, min(delay, remaining_budget))
                if delay > 0:
                    self._sleep(delay)
                continue

            latency_ms = int((self._clock() - started) * 1000)
            request_id = header_value(response_headers, "x-typesafe-request-id")
            try:
                return parse_response(
                    payload,
                    questions,
                    requested_model=requested_model,
                    correlation_id=correlation_id,
                    latency_ms=latency_ms,
                    attempts=attempts,
                    request_id=request_id,
                )
            except JevResponseError:
                # A malformed body is not retryable: the same request would
                # produce the same malformed body.
                raise

        raise last_error or JevError(
            "service_unavailable", "Jev request failed", retry_after=last_retry_after
        )

    # -- health check ------------------------------------------------------

    def health_check(self, *, allow_insecure_localhost: bool = False) -> dict[str, Any]:
        """Validate the typed endpoint with one small synthetic request.

        Exercises all three primitives so a URL that merely resolves cannot pass.
        """
        synthetic_state = {
            "probe": "health-check",
            "text": "用户反馈登录后订单页面一直转圈，已经重试三次。",
        }
        questions = build_question_set(
            [
                ("probe_noul", NoulQuestion(instructions="Does the text describe a problem?", criteria={"true": "是", "false": "否"})),
                ("probe_choice", ChoiceQuestion(instructions="Which area does the text concern?", options={"auth": "登录", "orders": "订单", NO_MATCH_OPTION: "其他"})),
                ("probe_score", ScoreQuestion(instructions="How severe is the reported issue?", levels=["不严重", "一般", "严重"])),
            ]
        )
        try:
            response = self.evaluate(
                synthetic_state,
                questions,
                allow_insecure_localhost=allow_insecure_localhost,
            )
        except JevError as exc:
            return {
                "ok": False,
                "kind": exc.kind,
                "message": exc.message,
                "status": exc.status,
            }
        return {
            "ok": True,
            "kind": "ok",
            "requestedModel": response.requested_model,
            "responseModel": response.model,
            "usage": dict(response.usage) if response.usage else None,
            "requestId": response.request_id,
            "latencyMs": response.latency_ms,
            "attempts": response.attempts,
            "primitives": sorted({answer.type for answer in response.answers.values()}),
        }
