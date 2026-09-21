"""Frozen System One wire-contract fixtures (tasks 2.1a, 6.1a, 6.1b).

These pin the HTTP contract: endpoint/header construction, the
``{state, model, questions}`` request envelope for all three primitives, and
the accept/reject boundary of the typed response parser.

These are implementation/CI fixtures. Per task 6.4 they are not executed as
part of this change's preparation.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import unittest
from io import BytesIO

from worker.jev_client import (
    JevClient,
    JevError,
    JevResponseError,
    build_headers,
    build_request_body,
    normalize_endpoint,
    parse_response,
)
from worker.jev_config import JevConfig
from worker.jev_eligibility import (
    REASON_IMAGE_ONLY,
    REASON_TOO_LARGE,
    assess_budget,
    assess_text_eligibility,
)
from worker.jev_questions import (
    NO_MATCH_OPTION,
    ChoiceQuestion,
    JevQuestionError,
    NoulQuestion,
    ScoreQuestion,
    build_question_set,
    opaque_ids,
)
from worker.jev_service import human_answer_questions

BASE = "https://api.typesafe.ai"


# --------------------------------------------------------------------------
# Test transport stub
# --------------------------------------------------------------------------


class _Response:
    def __init__(self, payload, headers):
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = headers

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _StubOpener:
    """Replays a scripted list of responses; ints are HTTP status codes."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.last_request = None

    def open(self, request, timeout=None):  # noqa: ARG002
        self.calls += 1
        self.last_request = request
        item = self.script.pop(0)
        if isinstance(item, int):
            raise urllib.error.HTTPError(
                request.full_url, item, "error", {}, BytesIO(b"{}")
            )
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            payload, headers = item
            return _Response(payload, headers)
        return _Response(item, {})


def _client(script, **overrides):
    raw = {
        "enabled": True,
        "mode": "assist",
        "base_url": BASE,
        "api_key": "test-key-0123456789",
        "retry_backoff_seconds": 0.0,
        "max_retries": 2,
    }
    raw.update(overrides)
    opener = _StubOpener(script)
    config = JevConfig.from_mapping(raw)
    client = JevClient(config, opener=opener, clock=lambda: 0.0, sleeper=lambda _s: None)
    return client, opener


def _questions():
    return build_question_set(
        [
            ("is_question", NoulQuestion(instructions="Does it ask?", criteria={"true": "是", "false": "否"})),
            ("route", ChoiceQuestion(instructions="Which?", options={"q0": "登录", NO_MATCH_OPTION: "都不是"})),
            ("evidence", ScoreQuestion(instructions="How sufficient?", levels=["不足", "部分", "充分"])),
        ]
    )


def _valid_response(**overrides):
    payload = {
        "model": "jev-1.13.0",
        "answers": {
            "is_question": {"type": "noul", "noul": 0.95},
            "route": {
                "type": "choice",
                "choice": "q0",
                "probabilities": {"q0": 0.88, NO_MATCH_OPTION: 0.12},
                "confidence": 0.81,
            },
            "evidence": {
                "type": "score",
                "score": 1.60,
                "legend": {"0": "不足", "1": "部分", "2": "充分"},
                "probabilities": {"0": 0.10, "1": 0.20, "2": 0.70},
                "confidence": 0.72,
            },
        },
        "usage": {"input_tokens": 296, "output_tokens": 20},
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# 2.1a -- endpoint, headers, request envelope
# --------------------------------------------------------------------------


class EndpointContractTests(unittest.TestCase):
    def test_all_configured_forms_normalize_to_one_endpoint(self):
        expected = "https://api.typesafe.ai/v1/systemone"
        for raw in (
            "https://api.typesafe.ai",
            "https://api.typesafe.ai/",
            "https://api.typesafe.ai/v1",
            "https://api.typesafe.ai/v1/",
            "https://api.typesafe.ai/v1/systemone",
            "https://api.typesafe.ai/v1/systemone/",
        ):
            self.assertEqual(normalize_endpoint(raw), expected, raw)

    def test_query_fragment_and_credentials_are_never_forwarded(self):
        self.assertEqual(
            normalize_endpoint("https://api.typesafe.ai/v1/systemone?key=leak#frag"),
            "https://api.typesafe.ai/v1/systemone",
        )
        with self.assertRaises(JevError):
            normalize_endpoint("https://user:pass@api.typesafe.ai")

    def test_plain_http_is_refused_except_explicit_localhost(self):
        with self.assertRaises(JevError):
            normalize_endpoint("http://api.typesafe.ai")
        self.assertEqual(
            normalize_endpoint("http://localhost:8080", allow_insecure_localhost=True),
            "http://localhost:8080/v1/systemone",
        )
        with self.assertRaises(JevError):
            normalize_endpoint("http://localhost:8080")  # opt-in required

    def test_headers_carry_bearer_and_never_put_key_in_url(self):
        headers = build_headers("test-key-0123456789", "corr-1")
        self.assertEqual(headers["Authorization"], "Bearer test-key-0123456789")
        self.assertEqual(headers["Content-Type"], "application/json")
        client, opener = _client([_valid_response()])
        client.evaluate({"text": "hi"}, _questions())
        self.assertNotIn("test-key-0123456789", opener.last_request.full_url)
        self.assertEqual(opener.last_request.get_method(), "POST")


class RequestEnvelopeTests(unittest.TestCase):
    def test_body_has_exactly_state_model_questions(self):
        config = JevConfig.from_mapping({"model": "jev-latest"})
        body = build_request_body("hello", config, _questions())
        self.assertEqual(set(body), {"state", "model", "questions"})
        self.assertEqual(body["model"], "jev-latest")

    def test_chat_completions_fields_are_absent(self):
        body = build_request_body("hello", JevConfig.from_mapping({}), _questions())
        for forbidden in ("messages", "temperature", "max_tokens", "response_format", "stream"):
            self.assertNotIn(forbidden, body)

    def test_noul_choice_score_encodings(self):
        payload = _questions().payload()
        self.assertEqual(payload["is_question"]["type"], "noul")
        self.assertEqual(payload["is_question"]["criteria"], {"true": "是", "false": "否"})
        self.assertEqual(payload["route"]["type"], "choice")
        self.assertIn(NO_MATCH_OPTION, payload["route"]["criteria"])
        self.assertEqual(payload["evidence"]["type"], "score")
        self.assertEqual(payload["evidence"]["criteria"], ["不足", "部分", "充分"])

    def test_question_definition_validation_rejects_bad_shapes(self):
        with self.assertRaises(JevQuestionError):
            build_question_set([("q", NoulQuestion(instructions="", criteria={}))])
        with self.assertRaises(JevQuestionError):
            build_question_set([("q", NoulQuestion(instructions="x", criteria={"maybe": "?"}))])
        with self.assertRaises(JevQuestionError):
            build_question_set([("q", ChoiceQuestion(instructions="x", options={}))])
        with self.assertRaises(JevQuestionError):
            build_question_set([("q", ScoreQuestion(instructions="x", levels=["only one"]))])
        with self.assertRaises(JevQuestionError):
            build_question_set(
                [("q", ScoreQuestion(instructions="x", levels=[str(i) for i in range(11)]))]
            )
        with self.assertRaises(JevQuestionError):
            build_question_set([("q" f"{i}", NoulQuestion(instructions="x")) for i in range(33)])
        with self.assertRaises(JevQuestionError):
            build_question_set({})

    def test_opaque_candidate_ids_keep_semantics_out_of_the_id(self):
        built = human_answer_questions([("work-123", "登录失败了吗？"), ("work-456", "订单卡住了吗？")])
        self.assertEqual(
            sorted(built.questions.ids()),
            ["does_message_answer_q0", "does_message_answer_q1"],
        )
        # The work ID must survive worker-side, not in the wire payload.
        self.assertEqual(built.work_by_question_id["does_message_answer_q0"], "work-123")
        self.assertNotIn("work-123", json.dumps(built.questions.payload(), ensure_ascii=False))
        self.assertEqual(opaque_ids(3), ["q0", "q1", "q2"])


# --------------------------------------------------------------------------
# 6.1a -- typed response parsing
# --------------------------------------------------------------------------


class ResponseParsingTests(unittest.TestCase):
    def test_valid_typed_response_is_parsed_by_question_id(self):
        response = parse_response(_valid_response(), _questions(), requested_model="jev-latest")
        self.assertEqual(response.noul("is_question"), 0.95)
        self.assertEqual(response.choice("route").choice, "q0")
        self.assertEqual(response.choice("route").is_tied, False)
        self.assertAlmostEqual(response.score("evidence").score, 1.60)
        self.assertEqual(response.model, "jev-1.13.0")

    def test_choice_tie_is_exposed_but_never_resolved_by_the_client(self):
        payload = _valid_response()
        payload["answers"]["route"] = {
            "type": "choice",
            "choice": "q0",
            "probabilities": {"q0": 0.5, NO_MATCH_OPTION: 0.5},
            "confidence": 0.5,
        }
        answer = parse_response(payload, _questions()).choice("route")
        self.assertTrue(answer.is_tied)
        self.assertEqual(set(answer.tied), {"q0", NO_MATCH_OPTION})

    def test_invalid_shapes_are_rejected_wholesale(self):
        cases = {}

        missing = _valid_response()
        missing["answers"] = {k: v for k, v in missing["answers"].items() if k != "route"}
        cases["missing question"] = missing

        unknown = _valid_response()
        unknown["answers"]["bogus"] = {"type": "noul", "noul": 0.5}
        cases["unknown question id"] = unknown

        mismatch = _valid_response()
        mismatch["answers"]["is_question"] = {
            "type": "choice", "choice": "q0", "probabilities": {"q0": 1.0, NO_MATCH_OPTION: 0.0}, "confidence": 0.9,
        }
        cases["answer type mismatch"] = mismatch

        for name, noul in (("above one", 1.5), ("below zero", -0.1), ("nan", float("nan"))):
            out_of_range = _valid_response()
            out_of_range["answers"]["is_question"] = {"type": "noul", "noul": noul}
            cases[f"noul out of range ({name})"] = out_of_range

        key_mismatch = _valid_response()
        key_mismatch["answers"]["route"]["probabilities"] = {"q0": 0.9, "extra": 0.1}
        cases["probability key mismatch"] = key_mismatch

        bad_sum = _valid_response()
        bad_sum["answers"]["route"]["probabilities"] = {"q0": 0.5, NO_MATCH_OPTION: 0.2}
        cases["probabilities do not sum to one"] = bad_sum

        bad_legend = _valid_response()
        bad_legend["answers"]["evidence"]["legend"] = {"0": "不足", "1": "部分"}
        cases["score legend key mismatch"] = bad_legend

        bad_score = _valid_response()
        bad_score["answers"]["evidence"]["score"] = 0.2
        cases["score inconsistent with weighted value"] = bad_score

        bad_choice = _valid_response()
        bad_choice["answers"]["route"]["choice"] = "nonexistent"
        cases["choice outside criteria"] = bad_choice

        empty = _valid_response()
        empty["answers"] = {}
        cases["empty answers"] = empty

        for name, payload in cases.items():
            with self.assertRaises(JevResponseError, msg=name):
                parse_response(payload, _questions())

    def test_chat_completions_shaped_body_is_rejected(self):
        with self.assertRaises(JevResponseError):
            parse_response({"choices": [{"message": {"content": "hi"}}]}, _questions())

    def test_missing_answers_key_is_not_treated_as_a_judgment(self):
        with self.assertRaises(JevResponseError):
            parse_response({"model": "jev-1.13.0"}, _questions())


class RetryClassificationTests(unittest.TestCase):
    def test_retryable_statuses_retry_within_budget(self):
        for status in (408, 429, 500, 502, 503, 504, 529):
            client, opener = _client([status, _valid_response()])
            response = client.evaluate({"text": "hi"}, _questions())
            self.assertEqual(opener.calls, 2, status)
            self.assertEqual(response.attempts, 2, status)

    def test_non_retryable_statuses_fail_immediately(self):
        for status in (400, 401, 403, 404, 422):
            client, opener = _client([status, _valid_response()])
            with self.assertRaises(JevError, msg=status):
                client.evaluate({"text": "hi"}, _questions())
            self.assertEqual(opener.calls, 1, status)

    def test_401_is_a_config_error_and_never_retries_the_secret(self):
        client, opener = _client([401])
        with self.assertRaises(JevError) as caught:
            client.evaluate({"text": "hi"}, _questions())
        self.assertEqual(caught.exception.kind, "config_error")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(opener.calls, 1)

    def test_retry_stops_after_max_attempts(self):
        client, opener = _client([500, 500, 500])
        with self.assertRaises(JevError) as caught:
            client.evaluate({"text": "hi"}, _questions())
        self.assertEqual(caught.exception.kind, "service_unavailable")
        self.assertEqual(opener.calls, 3)

    def test_connection_and_timeout_errors_are_retryable(self):
        client, opener = _client([socket.timeout("timed out"), _valid_response()])
        response = client.evaluate({"text": "hi"}, _questions())
        self.assertEqual(opener.calls, 2)
        self.assertEqual(response.attempts, 2)

    def test_429_without_a_hint_falls_back_to_bounded_backoff(self):
        slept: list[float] = []
        opener = _StubOpener([429, _valid_response()])
        client = JevClient(
            JevConfig.from_mapping(
                {
                    "enabled": True,
                    "base_url": BASE,
                    "api_key": "k" * 12,
                    "retry_backoff_seconds": 0.5,
                }
            ),
            opener=opener,
            clock=lambda: 0.0,
            sleeper=slept.append,
        )
        client.evaluate({"text": "hi"}, _questions())
        self.assertEqual(opener.calls, 2)
        self.assertEqual(len(slept), 1)
        # Jittered between 50% and 100% of the 0.5s base.
        self.assertTrue(0.25 <= slept[0] <= 0.5, slept)

    def test_retry_after_ms_is_preferred_over_retry_after(self):
        class HeaderOpener(_StubOpener):
            def open(self, request, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise urllib.error.HTTPError(
                        request.full_url,
                        429,
                        "rate limited",
                        {"Retry-After": "10", "retry-after-ms": "250"},
                        BytesIO(b"{}"),
                    )
                return _Response(_valid_response(), {})

        slept: list[float] = []
        opener = HeaderOpener([])
        client = JevClient(
            JevConfig.from_mapping(
                {
                    "enabled": True,
                    "base_url": BASE,
                    "api_key": "k" * 12,
                    "max_retries": 1,
                    "total_budget_seconds": 60,
                }
            ),
            opener=opener,
            clock=lambda: 0.0,
            sleeper=slept.append,
        )
        client.evaluate({"text": "hi"}, _questions())
        self.assertEqual(slept, [0.25])

    def test_oversized_retry_after_is_capped_by_the_budget(self):
        class HeaderOpener(_StubOpener):
            def open(self, request, timeout=None):
                self.calls += 1
                raise urllib.error.HTTPError(
                    request.full_url,
                    529,
                    "overloaded",
                    {"Retry-After": "3600"},
                    BytesIO(b"{}"),
                )

        slept: list[float] = []
        client = JevClient(
            JevConfig.from_mapping(
                {
                    "enabled": True,
                    "base_url": BASE,
                    "api_key": "k" * 12,
                    "max_retries": 3,
                    "total_budget_seconds": 5,
                }
            ),
            opener=HeaderOpener([]),
            clock=lambda: 0.0,
            sleeper=slept.append,
        )
        with self.assertRaises(JevError):
            client.evaluate({"text": "hi"}, _questions())
        self.assertTrue(all(delay <= 5 for delay in slept), slept)


# --------------------------------------------------------------------------
# 6.1b -- metadata, budgets, eligibility
# --------------------------------------------------------------------------


class MetadataToleranceTests(unittest.TestCase):
    def test_missing_or_null_usage_does_not_discard_valid_answers(self):
        for payload in (
            {k: v for k, v in _valid_response().items() if k != "usage"},
            _valid_response(usage=None),
            _valid_response(usage={}),
            _valid_response(usage={"input_tokens": None}),
        ):
            response = parse_response(payload, _questions())
            self.assertEqual(response.noul("is_question"), 0.95)

    def test_missing_model_does_not_discard_valid_answers(self):
        payload = {k: v for k, v in _valid_response().items() if k != "model"}
        response = parse_response(payload, _questions())
        self.assertEqual(response.model, "")
        self.assertEqual(response.noul("is_question"), 0.95)

    def test_requested_alias_and_response_version_are_recorded_separately(self):
        response = parse_response(
            _valid_response(), _questions(), requested_model="jev-latest"
        )
        self.assertEqual(response.requested_model, "jev-latest")
        self.assertEqual(response.model, "jev-1.13.0")

    def test_provider_request_id_is_captured_case_insensitively(self):
        client, _opener = _client([(_valid_response(), {"X-TypeSafe-Request-Id": "req-123"})])
        response = client.evaluate({"text": "hi"}, _questions())
        self.assertEqual(response.request_id, "req-123")
        self.assertTrue(response.correlation_id)

    def test_absent_provider_request_id_is_not_fabricated(self):
        client, _opener = _client([(_valid_response(), {})])
        response = client.evaluate({"text": "hi"}, _questions())
        self.assertEqual(response.request_id, "")
        self.assertTrue(response.correlation_id)


class EligibilityAndBudgetTests(unittest.TestCase):
    def test_image_only_message_is_not_eligible(self):
        decision = assess_text_eligibility("", has_pending_images=True)
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason, REASON_IMAGE_ONLY)
        no_match = assess_text_eligibility("", has_ready_images=True)
        self.assertEqual(no_match.reason, REASON_IMAGE_ONLY)

    def test_readable_text_or_ready_projection_is_eligible(self):
        self.assertTrue(assess_text_eligibility("登录失败").eligible)
        self.assertTrue(assess_text_eligibility("", has_ready_text_projection=True).eligible)

    def test_state_above_max_state_chars_is_too_large(self):
        config = JevConfig.from_mapping({"max_state_chars": 200})
        decision = assess_budget("字" * 500, _questions(), config)
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason, REASON_TOO_LARGE)

    def test_state_plus_longest_question_token_budget(self):
        # 40k CJK characters estimate to 40k tokens, past the 32k state+question rule.
        config = JevConfig.from_mapping({"max_state_chars": 100_000})
        decision = assess_budget("字" * 40_000, _questions(), config)
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason, REASON_TOO_LARGE)

    def test_normal_state_is_within_budget(self):
        config = JevConfig.from_mapping({})
        self.assertTrue(assess_budget({"text": "登录失败"}, _questions(), config).eligible)


if __name__ == "__main__":
    unittest.main()
