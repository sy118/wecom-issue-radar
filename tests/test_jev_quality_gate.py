"""Daily quality-gate fixtures (task 6.3).

The central claim under test: the gate may only *add* a ``semantic_signals``
sidecar. Issue keys, field values, timelines, image references and sync
identities must survive byte-for-byte, and no candidate may ever be removed.

Implementation/CI fixtures; not executed during this change's preparation.
"""

from __future__ import annotations

import copy
import json
import unittest

from worker.jev_config import JEV_FEATURES
from worker.jev_quality_gate import (
    apply_quality_gate,
    build_quality_gate_service,
    candidate_state,
)
from worker.jev_service import JevSemanticService
from tests.test_jev_wire_contract import _StubOpener


def _quality_response(is_real=0.95, evidence=0.9, one_issue=0.88):
    return {
        "model": "jev-1.13.0",
        "answers": {
            "is_real_issue": {"type": "noul", "noul": is_real},
            "evidence_sufficient": {"type": "noul", "noul": evidence},
            "context_is_one_issue": {"type": "noul", "noul": one_issue},
        },
        "usage": {"input_tokens": 120, "output_tokens": 8},
    }


def _service(script, *, mode="assist", features=None):
    flags = {name: True for name in JEV_FEATURES} if features is None else features
    return JevSemanticService(
        lambda: {
            "jev": {
                "enabled": True,
                "mode": mode,
                "base_url": "https://api.typesafe.ai",
                "api_key": "test-key-0123456789",
                "retry_backoff_seconds": 0.0,
                "features": flags,
            }
        },
        opener=_StubOpener(list(script)),
        sleeper=lambda _s: None,
    )


def _snapshot():
    return {
        "date": "2026-09-21",
        "range": {"startDate": "2026-09-21", "startTime": "00:00"},
        "schema_version": 2,
        "issue_fields": [{"key": "problem_description"}],
        "issues": [
            {
                "key": "issue_001_aaaaaaaaaaaa",
                "tenant": "",
                "sender": "张三",
                "values": {"problem_description": "登录后订单页一直转圈", "module_text": "订单/售后"},
                "raw_message_keys": ["msg-1"],
                "context_message_keys": ["msg-1", "msg-2"],
                "image_refs": ["bulk:1_01"],
                "image_status": "ready",
                "timeline": [
                    {"message_id": 1, "raw_text": "登录后订单页一直转圈", "sender": "张三", "role": "question"},
                    {"message_id": 2, "raw_text": "已经重试三次了", "sender": "张三", "role": "question"},
                ],
            },
            {
                "key": "issue_002_bbbbbbbbbbbb",
                "tenant": "",
                "sender": "李四",
                "values": {"problem_description": "订单提交失败"},
                "raw_message_keys": ["msg-3"],
                "context_message_keys": ["msg-3"],
                "image_refs": [],
                "image_status": "missing_original_images",
                "timeline": [
                    {"message_id": 3, "raw_text": "订单提交失败", "sender": "李四", "role": "question"},
                ],
            },
        ],
    }


IDENTITY_FIELDS = (
    "key",
    "tenant",
    "sender",
    "values",
    "raw_message_keys",
    "context_message_keys",
    "image_refs",
    "timeline",
)


class QualityGateIdentityTests(unittest.TestCase):
    def test_disabled_jev_returns_a_compatible_snapshot(self):
        snapshot = _snapshot()
        original = copy.deepcopy(snapshot)
        result = apply_quality_gate(snapshot, service=None)
        self.assertEqual(result, original)
        for issue in result["issues"]:
            self.assertNotIn("semantic_signals", issue)

    def test_identity_fields_survive_untouched(self):
        snapshot = _snapshot()
        original = copy.deepcopy(snapshot)
        service = _service([_quality_response(), _quality_response()])
        result = apply_quality_gate(snapshot, service=service)
        for produced, source in zip(result["issues"], original["issues"]):
            for field in IDENTITY_FIELDS:
                self.assertEqual(produced[field], source[field], field)

    def test_input_snapshot_is_not_mutated(self):
        snapshot = _snapshot()
        original = copy.deepcopy(snapshot)
        service = _service([_quality_response(), _quality_response()])
        apply_quality_gate(snapshot, service=service)
        self.assertEqual(snapshot, original)

    def test_candidates_are_never_removed(self):
        snapshot = _snapshot()
        service = _service([_quality_response(is_real=0.01), _quality_response()])
        result = apply_quality_gate(snapshot, service=service)
        self.assertEqual(len(result["issues"]), 2)
        self.assertEqual(
            [issue["key"] for issue in result["issues"]],
            ["issue_001_aaaaaaaaaaaa", "issue_002_bbbbbbbbbbbb"],
        )

    def test_likely_non_issue_is_marked_but_kept(self):
        service = _service([_quality_response(is_real=0.02), _quality_response()])
        result = apply_quality_gate(_snapshot(), service=service)
        signals = result["issues"][0]["semantic_signals"]
        self.assertIn("likely_not_an_issue", signals["reviewReasons"])
        self.assertTrue(signals["reviewRequired"])
        self.assertEqual(result["issues"][0]["key"], "issue_001_aaaaaaaaaaaa")


class QualityGateEvidenceTests(unittest.TestCase):
    def test_unresolved_images_skip_the_judgment_and_flag_review(self):
        service = _service([_quality_response()])
        result = apply_quality_gate(_snapshot(), service=service)
        # The unresolved-image candidate must not consume a request...
        self.assertEqual(service._opener.calls, 1)  # noqa: SLF001
        signals = result["issues"][1]["semantic_signals"]
        self.assertEqual(signals["status"], "not_eligible")
        self.assertIn("image_evidence_incomplete", signals["reviewReasons"])
        self.assertTrue(signals["reviewRequired"])
        # ...and missing evidence must never be read as "no problem here".
        self.assertFalse(signals["applied"])

    def test_shadow_records_the_opinion_without_flagging(self):
        service = _service([_quality_response(is_real=0.02), _quality_response()], mode="shadow")
        result = apply_quality_gate(_snapshot(), service=service)
        signals = result["issues"][0]["semantic_signals"]
        self.assertEqual(signals["status"], "shadow")
        self.assertFalse(signals["reviewRequired"])
        self.assertIn("likely_not_an_issue", signals["reviewReasons"])
        self.assertFalse(signals["applied"])

    def test_review_band_is_recorded_as_a_reason(self):
        service = _service([_quality_response(is_real=0.5), _quality_response()])
        result = apply_quality_gate(_snapshot(), service=service)
        signals = result["issues"][0]["semantic_signals"]
        self.assertEqual(signals["status"], "review")
        self.assertIn("review_interval", signals["reviewReasons"])
        self.assertFalse(signals["applied"])


class QualityGateFailureTests(unittest.TestCase):
    def test_service_failure_still_yields_a_complete_snapshot(self):
        service = _service([500, 500, 500])
        result = apply_quality_gate(_snapshot(), service=service)
        self.assertEqual(len(result["issues"]), 2)
        for issue in result["issues"]:
            self.assertIn(issue["key"], {"issue_001_aaaaaaaaaaaa", "issue_002_bbbbbbbbbbbb"})
        self.assertEqual(result["issues"][0]["semantic_signals"]["status"], "service_unavailable")

    def test_gate_exception_never_costs_a_candidate(self):
        class Exploding:
            def ask(self, **_kwargs):
                raise RuntimeError("boom")

            def record_skip(self, **_kwargs):
                raise RuntimeError("boom")

        result = apply_quality_gate(_snapshot(), service=Exploding())
        self.assertEqual(len(result["issues"]), 2)
        for issue in result["issues"]:
            self.assertNotIn("semantic_signals", issue)

    def test_build_service_returns_none_when_inactive(self):
        for raw in (
            {},
            {"jev": {"enabled": False}},
            {"jev": {"enabled": True, "mode": "off"}},
            {"jev": {"enabled": True, "mode": "assist", "base_url": "", "api_key": "k" * 12}},
        ):
            self.assertIsNone(build_quality_gate_service(raw), raw)


class CandidateStateTests(unittest.TestCase):
    def test_state_is_bounded_and_carries_no_images_or_paths(self):
        issue = _snapshot()["issues"][0]
        issue["timeline"] = [
            {"raw_text": f"消息 {i}" * 200, "sender": "张三", "role": "question", "image_refs": ["bulk:1"]}
            for i in range(40)
        ]
        state = candidate_state(issue)
        self.assertLessEqual(len(state["candidate"]["messages"]), 12)
        for message in state["candidate"]["messages"]:
            self.assertLessEqual(len(message["text"]), 400)
        serialized = json.dumps(state, ensure_ascii=False)
        self.assertNotIn("bulk:1", serialized)
        self.assertNotIn("localPath", serialized)

    def test_empty_timeline_is_still_a_valid_state(self):
        state = candidate_state({"values": {"problem_description": "x"}, "timeline": []})
        self.assertEqual(state["candidate"]["messages"], [])


if __name__ == "__main__":
    unittest.main()
