"""Semantic service unit fixtures (task 6.1).

Covers state redaction, mode behaviour, primitive-aware thresholds and audit
serialization. Implementation/CI fixtures; not executed during this change's
preparation (task 6.4).
"""

from __future__ import annotations

import json
import unittest

from worker.jev_audit import serialize_answer, serialize_answers
from worker.jev_client import ScoreAnswer, parse_response
from worker.jev_config import JEV_FEATURES, JevConfig
from worker.jev_eligibility import redact_state, redact_text
from worker.jev_service import (
    FEATURE_CLASSIFICATION,
    JevSemanticService,
    candidate_state,
    classification_questions,
    quality_gate_questions,
)
from tests.test_jev_wire_contract import _StubOpener, _questions, _valid_response


def _classification_response(is_question=0.95, is_chat=0.04):
    """A response covering exactly the five classification Noul questions."""
    return {
        "model": "jev-1.13.0",
        "answers": {
            "is_question": {"type": "noul", "noul": is_question},
            "is_supplement": {"type": "noul", "noul": 0.02},
            "is_human_answer": {"type": "noul", "noul": 0.03},
            "is_withdrawn": {"type": "noul", "noul": 0.01},
            "is_chat": {"type": "noul", "noul": is_chat},
        },
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }


def _config(**overrides):
    raw = {
        "enabled": True,
        "mode": "assist",
        "base_url": "https://api.typesafe.ai",
        "api_key": "test-key-0123456789",
        "retry_backoff_seconds": 0.0,
        "features": {name: True for name in JEV_FEATURES},
    }
    raw.update(overrides)
    return {"jev": raw}


def _service(script=(), **overrides):
    audit: list = []
    service = JevSemanticService(
        lambda: _config(**overrides),
        audit_sink=audit.append,
        opener=_StubOpener(list(script)),
        sleeper=lambda _s: None,
    )
    return service, audit


class RedactionTests(unittest.TestCase):
    def test_secrets_paths_and_base64_never_reach_the_state(self):
        state = {
            "message": {
                "text": "密钥 test-key-0123456789 在 C:\\Users\\lee\\secret.txt",
                "attachment": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==",
                "url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcdef123456",
                "token": "Bearer abcdefghijklmnop",
            }
        }
        cleaned = json.dumps(redact_state(state, ["test-key-0123456789"]), ensure_ascii=False)
        self.assertNotIn("test-key-0123456789", cleaned)
        self.assertNotIn("C:\\Users\\lee", cleaned)
        self.assertNotIn("iVBORw0KGgoAAAANSUhEUg==", cleaned)
        self.assertNotIn("abcdef123456", cleaned)
        self.assertNotIn("abcdefghijklmnop", cleaned)

    def test_redaction_does_not_mutate_the_input(self):
        state = {"text": "C:\\Users\\lee\\secret.txt"}
        redact_state(state, [])
        self.assertEqual(state["text"], "C:\\Users\\lee\\secret.txt")

    def test_short_secret_like_strings_are_left_alone(self):
        self.assertEqual(redact_text("key=abc", ["abc"]), "key=abc")

    def test_service_redacts_before_the_request_is_built(self):
        service, _audit = _service([_classification_response()])
        opener = service._opener  # noqa: SLF001 - assert on the captured request
        service.ask(
            feature=FEATURE_CLASSIFICATION,
            scope="test",
            state={"message": {"text": "密钥 test-key-0123456789"}},
            questions=classification_questions(),
            secrets=["test-key-0123456789"],
        )
        sent = opener.last_request.data.decode("utf-8")
        self.assertNotIn("test-key-0123456789", sent)


class ModeBehaviourTests(unittest.TestCase):
    def test_off_never_calls_out(self):
        service, audit = _service([_valid_response()], mode="off")
        outcome = service.ask(
            feature=FEATURE_CLASSIFICATION, scope="t", state={}, questions=classification_questions()
        )
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.status, "disabled")
        self.assertEqual(service._opener.calls, 0)  # noqa: SLF001
        self.assertEqual(len(audit), 1)  # skipped calls are still audited

    def test_disabled_jev_never_calls_out(self):
        service, _audit = _service([_valid_response()], enabled=False)
        outcome = service.ask(
            feature=FEATURE_CLASSIFICATION, scope="t", state={}, questions=classification_questions()
        )
        self.assertFalse(outcome.applied)
        self.assertEqual(service._opener.calls, 0)  # noqa: SLF001

    def test_feature_flag_off_never_calls_out(self):
        service, _audit = _service(
            [_valid_response()], features={name: False for name in JEV_FEATURES}
        )
        outcome = service.ask(
            feature=FEATURE_CLASSIFICATION, scope="t", state={}, questions=classification_questions()
        )
        self.assertFalse(outcome.applied)
        self.assertEqual(service._opener.calls, 0)  # noqa: SLF001

    def test_shadow_calls_out_but_never_applies(self):
        service, audit = _service([_classification_response()], mode="shadow")
        outcome = service.ask(
            feature=FEATURE_CLASSIFICATION, scope="t", state={}, questions=classification_questions()
        )
        self.assertEqual(outcome.status, "shadow")
        self.assertFalse(outcome.applied)
        self.assertTrue(outcome.noul_yes("is_question"))
        self.assertEqual(audit[-1].status, "shadow")

    def test_assist_applies_only_decisive_judgments(self):
        service, _audit = _service([_classification_response()])
        outcome = service.ask(
            feature=FEATURE_CLASSIFICATION, scope="t", state={}, questions=classification_questions()
        )
        self.assertTrue(outcome.applied)
        self.assertEqual(outcome.status, "ok")


class NoulThresholdTests(unittest.TestCase):
    def _noul_payload(self, value):
        return _classification_response(is_question=value)

    def test_high_confidence_yes(self):
        service, _audit = _service([self._noul_payload(0.95)])
        outcome = service.ask(
            feature=FEATURE_CLASSIFICATION, scope="t", state={}, questions=classification_questions()
        )
        self.assertTrue(outcome.applied)
        self.assertTrue(outcome.noul_yes("is_question"))

    def test_review_band_falls_back(self):
        service, _audit = _service([self._noul_payload(0.5)])
        outcome = service.ask(
            feature=FEATURE_CLASSIFICATION, scope="t", state={}, questions=classification_questions()
        )
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.status, "review")
        self.assertEqual(outcome.fallback_reason, "review_interval")

    def test_high_confidence_no_is_a_result_not_a_failure(self):
        service, _audit = _service([self._noul_payload(0.01)])
        outcome = service.ask(
            feature=FEATURE_CLASSIFICATION, scope="t", state={}, questions=classification_questions()
        )
        # A confident negative must still be applied, and must not be recorded
        # as a service problem.
        self.assertTrue(outcome.applied)
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.fallback_reason, "")
        self.assertTrue(outcome.noul_no("is_question"))

    def test_per_feature_thresholds_override_the_global_pair(self):
        service, _audit = _service(
            [self._noul_payload(0.6)],
            thresholds={"runtime_classification": {"yes": 0.55, "no": 0.2}},
        )
        outcome = service.ask(
            feature=FEATURE_CLASSIFICATION, scope="t", state={}, questions=classification_questions()
        )
        self.assertTrue(outcome.applied)
        self.assertTrue(outcome.noul_yes("is_question"))

    def test_thresholds_do_not_mix_primitives(self):
        # A Choice/Score answer has no `noul`; it must never be graded by the
        # Noul cut points.
        config = JevConfig.from_mapping(
            {"features": {name: True for name in JEV_FEATURES}, "decision_threshold": 0.7}
        )
        response = parse_response(_valid_response(), _questions())
        service, _audit = _service([_classification_response()])
        noul, choices, scores = service._evaluate_answers(  # noqa: SLF001
            config, FEATURE_CLASSIFICATION, _questions(), response
        )
        self.assertIn("is_question", noul)
        self.assertIn("route", choices)
        self.assertIn("evidence", scores)
        self.assertEqual(choices["route"].choice, "q0")
        self.assertAlmostEqual(scores["evidence"].score, 1.60)
        self.assertAlmostEqual(scores["evidence"].normalized, 0.80)


class AuditSerializationTests(unittest.TestCase):
    def test_noul_is_stored_separately_from_confidence(self):
        response = parse_response(_valid_response(), _questions())
        serialized = serialize_answers(response)
        self.assertEqual(serialized["is_question"], {"type": "noul", "noul": 0.95})
        # No fabricated confidence for Noul.
        self.assertNotIn("confidence", serialized["is_question"])
        self.assertNotIn("probabilities", serialized["is_question"])

    def test_choice_and_score_keep_full_distributions(self):
        response = parse_response(_valid_response(), _questions())
        serialized = serialize_answers(response)
        route = serialized["route"]
        self.assertEqual(route["type"], "choice")
        self.assertEqual(route["choice"], "q0")
        self.assertEqual(route["probabilities"], {"q0": 0.88, "none": 0.12})
        self.assertAlmostEqual(route["confidence"], 0.81)
        evidence = serialized["evidence"]
        self.assertEqual(evidence["type"], "score")
        self.assertEqual(evidence["legend"], {"0": "不足", "1": "部分", "2": "充分"})
        self.assertEqual(evidence["probabilities"], {"0": 0.10, "1": 0.20, "2": 0.70})
        self.assertAlmostEqual(evidence["score"], 1.60)

    def test_score_level_keys_stay_reversible(self):
        response = parse_response(_valid_response(), _questions())
        answer = response.answers["evidence"]
        self.assertIsInstance(answer, ScoreAnswer)
        self.assertEqual(answer.level_keys, ("0", "1", "2"))
        self.assertEqual(list(answer.legend.keys()), ["0", "1", "2"])

    def test_serialize_answer_rejects_unknown_primitives(self):
        with self.assertRaises(TypeError):
            serialize_answer("not an answer")  # type: ignore[arg-type]

    def test_judgment_carries_ids_models_and_state_hash(self):
        service, audit = _service([_classification_response()])
        service.ask(
            feature=FEATURE_CLASSIFICATION,
            scope="listener:abc",
            state={"message": {"text": "登录失败"}},
            questions=classification_questions(),
            work_id="work-1",
            generation=3,
        )
        judgment = audit[-1]
        self.assertEqual(judgment.feature, FEATURE_CLASSIFICATION)
        self.assertEqual(judgment.work_id, "work-1")
        self.assertEqual(judgment.generation, 3)
        self.assertEqual(judgment.requested_model, "jev-latest")
        self.assertEqual(judgment.response_model, "jev-1.13.0")
        self.assertTrue(judgment.correlation_id)
        self.assertTrue(judgment.state_hash)
        self.assertEqual(len(judgment.state_hash), 64)

    def test_state_hash_does_not_leak_content(self):
        service, audit = _service([_classification_response()])
        service.ask(
            feature=FEATURE_CLASSIFICATION,
            scope="t",
            state={"message": {"text": "订单页面一直转圈"}},
            questions=classification_questions(),
        )
        self.assertNotIn("订单", audit[-1].state_hash)


class QuestionDefinitionTests(unittest.TestCase):
    def test_classification_questions_are_independent_and_non_complementary(self):
        questions = classification_questions()
        self.assertEqual(
            sorted(questions.ids()),
            ["is_chat", "is_human_answer", "is_question", "is_supplement", "is_withdrawn"],
        )
        for question_id in questions.ids():
            self.assertEqual(questions.types[question_id], "noul")

    def test_quality_gate_asks_three_independent_nouls(self):
        questions = quality_gate_questions()
        self.assertEqual(
            sorted(questions.ids()),
            ["context_is_one_issue", "evidence_sufficient", "is_real_issue"],
        )

    def test_candidate_state_keeps_work_ids_worker_side(self):
        entries = [("work-1", "登录失败了吗？")]
        state = candidate_state("新消息", entries)
        self.assertEqual(state["candidates"]["q0"]["question"], "登录失败了吗？")
        self.assertNotIn("work-1", json.dumps(state, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
