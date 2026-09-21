"""Text-only eligibility, local budget preflight and redaction for Jev.

Jev is a text-only System One endpoint. Before anything is serialized we decide
whether a call is allowed at all, and we bound its size locally. The local
budget is a *project* preflight, not a substitute for the server's token limit:
when in doubt we fall back rather than risk a truncated request.

This module is pure: it takes already-projected text and flags from the caller
so it never has to know how messages are stored.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Iterable, Mapping, Sequence

from worker.jev_config import (
    STATE_PLUS_LONGEST_QUESTION_TOKEN_BUDGET,
    TOTAL_TOKEN_BUDGET,
    JevConfig,
)
from worker.jev_questions import QuestionSet

REASON_ELIGIBLE = ""
REASON_NOT_ELIGIBLE = "not_eligible"
REASON_IMAGE_ONLY = "image_only"
REASON_TOO_LARGE = "too_large"

# CJK and kana tokenize near one token per character; everything else averages
# roughly a quarter. Counting CJK as 1 keeps the estimate on the safe side.
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")

_BASE64_BLOB_RE = re.compile(r"(?:data:(?:image|audio|video)/[a-z0-9.+-]+;base64,|[A-Za-z0-9+/]{512,}={0,2})")
_DATA_URI_RE = re.compile(r"data:[a-z0-9.+-]+/[a-z0-9.+-]+;base64,", re.IGNORECASE)
# Same as above but also swallowing the base64 payload that follows it.
_DATA_URI_WITH_PAYLOAD_RE = re.compile(
    r"data:[a-z0-9.+-]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]*", re.IGNORECASE
)
_LOCAL_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/][^\s\"'<>|]*)"          # C:\Users\...
    r"|(?:\\\\[^\s\"'<>|]+)"                    # \\server\share
    r"|(?:/(?:Users|home|var|tmp|mnt|opt)/[^\s\"'<>|]*)",  # POSIX absolute
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE)
_WEBHOOK_KEY_RE = re.compile(r"([?&]key=)[A-Za-z0-9._\-]{8,}", re.IGNORECASE)

REDACTED = "[已隐藏]"


@dataclass(frozen=True)
class EligibilityDecision:
    """Whether a Jev call may proceed, and why not when it may not."""

    eligible: bool
    reason: str = REASON_ELIGIBLE
    detail: str = ""

    @staticmethod
    def allow() -> "EligibilityDecision":
        return EligibilityDecision(eligible=True)

    @staticmethod
    def deny(reason: str, detail: str = "") -> "EligibilityDecision":
        return EligibilityDecision(eligible=False, reason=reason, detail=detail)


def estimate_tokens(text: str) -> int:
    """Conservative token estimate; deliberately over-counts CJK."""
    value = str(text or "")
    cjk = len(_CJK_RE.findall(value))
    return cjk + math.ceil((len(value) - cjk) / 4)


def serialized(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""


def contains_binary_payload(value: Any, _depth: int = 0) -> bool:
    """True when ``value`` carries image/audio/video bytes or a base64 blob."""
    if _depth > 12:
        return False
    if isinstance(value, (bytes, bytearray, memoryview)):
        return True
    if isinstance(value, str):
        return bool(_DATA_URI_RE.search(value) or _BASE64_BLOB_RE.search(value))
    if isinstance(value, Mapping):
        return any(contains_binary_payload(item, _depth + 1) for item in value.values())
    if isinstance(value, Sequence):
        return any(contains_binary_payload(item, _depth + 1) for item in value)
    return False


def contains_local_path(value: Any, _depth: int = 0) -> bool:
    if _depth > 12:
        return False
    if isinstance(value, str):
        return bool(_LOCAL_PATH_RE.search(value))
    if isinstance(value, Mapping):
        return any(contains_local_path(item, _depth + 1) for item in value.values())
    if isinstance(value, Sequence):
        return any(contains_local_path(item, _depth + 1) for item in value)
    return False


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Strip credentials and local paths from a fragment bound for Jev."""
    value = str(text or "")
    for secret in secrets:
        token = str(secret or "")
        if len(token) >= 6:
            value = value.replace(token, REDACTED)
    # Consume the payload too: replacing only the `data:...;base64,` prefix
    # would leave the encoded bytes sitting in the state.
    value = _DATA_URI_WITH_PAYLOAD_RE.sub(REDACTED, value)
    value = _BEARER_RE.sub(REDACTED, value)
    value = _WEBHOOK_KEY_RE.sub(r"\1" + REDACTED, value)
    return _LOCAL_PATH_RE.sub(REDACTED, value)


def redact_state(value: Any, secrets: Iterable[str] = (), _depth: int = 0) -> Any:
    """Deep-copy ``value`` with secrets and local paths replaced.

    Returns a new structure; the input is never mutated.
    """
    if _depth > 12:
        return value
    secret_list = [str(secret) for secret in secrets if len(str(secret or "")) >= 6]
    if isinstance(value, str):
        return redact_text(value, secret_list)
    if isinstance(value, Mapping):
        return {
            redact_state(key, secret_list, _depth + 1): redact_state(item, secret_list, _depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_state(item, secret_list, _depth + 1) for item in value]
    return value


def filename_only(local_path: str) -> str:
    """Attachment metadata is reduced to a bare filename, never a path."""
    try:
        return PurePath(str(local_path or "")).name
    except (TypeError, ValueError):
        return ""


def assess_text_eligibility(
    text: str,
    *,
    has_pending_images: bool = False,
    has_ready_images: bool = False,
    has_ready_text_projection: bool = False,
) -> EligibilityDecision:
    """Decide whether a message can be judged from text alone.

    A ready OCR/text projection counts as readable text. Unresolved images do
    not disqualify a message that also carries readable text, but an
    image-only message must go to the existing vision/waiting path.
    """
    readable = bool(str(text or "").strip()) or has_ready_text_projection
    if not readable:
        if has_pending_images or has_ready_images:
            return EligibilityDecision.deny(
                REASON_IMAGE_ONLY, "the message has no readable text; only images are available"
            )
        return EligibilityDecision.deny(
            REASON_NOT_ELIGIBLE, "the message has no readable text"
        )
    return EligibilityDecision.allow()


def assess_budget(state: Any, questions: QuestionSet, config: JevConfig) -> EligibilityDecision:
    """Bound the serialized state plus every question before sending."""
    state_json = serialized(state)
    if not state_json:
        return EligibilityDecision.deny(REASON_NOT_ELIGIBLE, "state is not JSON-serializable")
    if len(state_json) > config.max_state_chars:
        return EligibilityDecision.deny(
            REASON_TOO_LARGE,
            f"state is {len(state_json)} characters, above the {config.max_state_chars} limit",
        )
    question_jsons = [serialized(question) for question in questions.questions.values()]
    if not question_jsons:
        return EligibilityDecision.deny(REASON_NOT_ELIGIBLE, "no questions were supplied")

    state_tokens = estimate_tokens(state_json)
    question_tokens = [estimate_tokens(item) for item in question_jsons]
    longest = max(question_tokens)
    if state_tokens + sum(question_tokens) > TOTAL_TOKEN_BUDGET:
        return EligibilityDecision.deny(
            REASON_TOO_LARGE, "state plus questions exceeds the total token budget"
        )
    if state_tokens + longest > STATE_PLUS_LONGEST_QUESTION_TOKEN_BUDGET:
        return EligibilityDecision.deny(
            REASON_TOO_LARGE, "state plus the longest question exceeds the per-question token budget"
        )
    return EligibilityDecision.allow()


def assess_state_safety(value: Any) -> EligibilityDecision:
    """Last line of defence before the payload leaves the process."""
    if contains_binary_payload(value):
        return EligibilityDecision.deny(
            REASON_IMAGE_ONLY, "state contains image/binary payloads and cannot be sent to Jev"
        )
    return EligibilityDecision.allow()
