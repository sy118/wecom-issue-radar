"""Reply-runtime Jev fixtures (task 6.2).

Covers the wiring points that decide whether a semantic judgment may touch work
state: shadow no-op, assist thresholds, overlapping labels, stale generations,
image-only fallback and human-answer no-match.

Implementation/CI fixtures; not executed during this change's preparation.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from worker.jev_config import JEV_FEATURES, JevConfig
from worker.jev_service import FEATURE_CLASSIFICATION, JevSemanticService, SemanticOutcome
from worker.reply_runtime import ReplyRuntime
from worker.reply_runtime.store import encode_json
from tests.test_jev_wire_contract import _StubOpener


def _outcome(answers, *, applied=True, status="ok", mode="assist"):
    """A SemanticOutcome carrying Noul verdicts, without a live service."""
    noul = {}
    for question_id, value in answers.items():
        verdict = "yes" if value >= 0.7 else "no" if value <= 0.3 else "review"
        noul[question_id] = type(
            "V",
            (),
            {
                "question_id": question_id,
                "noul": value,
                "verdict": verdict,
                "decisive": verdict in ("yes", "no"),
                "is_yes": verdict == "yes",
                "is_no": verdict == "no",
            },
        )()
    judgment = type("J", (), {"answers": {}, "mode": mode})()
    return SemanticOutcome(
        feature=FEATURE_CLASSIFICATION,
        scope="test",
        mode=mode,
        status=status,
        fallback_reason="" if status == "ok" else status,
        applied=applied and status == "ok",
        judgment=judgment,
        response=None,
        noul=noul,
    )


class _Runtime(ReplyRuntime):
    """Runtime with a fixed Jev config and a stubbed semantic service."""

    def __init__(self, *args, jev_config=None, service=None, **kwargs):
        self._test_jev_config = jev_config or JevConfig.from_mapping({})
        self._test_service = service
        super().__init__(*args, **kwargs)
        if service is not None:
            self.jev = service

    def _jev_config(self):
        return self._test_jev_config


def _jev_config(**overrides):
    raw = {
        "enabled": True,
        "mode": "assist",
        "base_url": "https://api.typesafe.ai",
        "api_key": "test-key-0123456789",
        "features": {name: True for name in JEV_FEATURES},
    }
    raw.update(overrides)
    return JevConfig.from_mapping(raw)


class _StubService:
    def __init__(self, outcome=None, *, error=None):
        self.outcome = outcome
        self.error = error
        self.skips: list[dict] = []
        self.comparisons: list[dict] = []

    def ask(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.asked = kwargs
        return self.outcome

    def record_skip(self, **kwargs):
        self.skips.append(kwargs)
        return None

    def record_comparison(self, **kwargs):
        self.comparisons.append(kwargs)
        return None

    def config(self):
        return _jev_config()


class LabelMappingTests(unittest.TestCase):
    def test_overlapping_labels_are_preserved(self):
        # A single message may be both a question and a human answer; the
        # caller's priority rules decide what that means for work state.
        outcome = _outcome({"is_question": 0.95, "is_human_answer": 0.9, "is_chat": 0.02})
        labels = ReplyRuntime._jev_labels_from_outcome(outcome)
        self.assertEqual(labels, {"question", "human_answer"})

    def test_high_confidence_negatives_contribute_no_labels(self):
        outcome = _outcome({"is_question": 0.02, "is_chat": 0.99})
        labels = ReplyRuntime._jev_labels_from_outcome(outcome)
        self.assertEqual(labels, {"chat"})

    def test_review_verdicts_never_produce_labels(self):
        outcome = _outcome({"is_question": 0.5, "is_withdrawn": 0.02})
        labels = ReplyRuntime._jev_labels_from_outcome(outcome)
        self.assertEqual(labels, set())


class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.runtime = _Runtime(
            Path(self._directory.name) / "runtime.sqlite3", autostart=False
        )

    def tearDown(self):
        self.runtime.close()
        self._directory.cleanup()

    def test_image_only_message_is_not_eligible(self):
        eligible, reason = self.runtime._jev_message_eligibility(
            [{"contentType": "image", "text": "[图片]", "images": [{"localPath": "C:/missing.png"}]}]
        )
        self.assertFalse(eligible)
        self.assertEqual(reason, "image_only")

    def test_text_message_is_eligible(self):
        eligible, _reason = self.runtime._jev_message_eligibility(
            [{"contentType": "text", "text": "登录后订单页一直转圈"}]
        )
        self.assertTrue(eligible)

    def test_inactive_feature_returns_none_without_calling_the_service(self):
        service = _StubService(_outcome({"is_question": 0.95}))
        runtime = _Runtime(
            Path(self._directory.name) / "second.sqlite3",
            autostart=False,
            jev_config=JevConfig.from_mapping({}),
            service=service,
        )
        try:
            listener = {"id": "L1"}
            result = runtime._jev_classify(
                listener_row=listener,
                messages=[{"contentType": "text", "text": "hi"}],
                question=None,
                work_id="w1",
                generation=1,
                scope_kind="work",
            )
            self.assertIsNone(result)
            self.assertFalse(hasattr(service, "asked"))
        finally:
            runtime.close()


class HumanAnswerMatchingTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "runtime.sqlite3"

    def tearDown(self):
        self._directory.cleanup()

    def _runtime_with(self, outcome):
        return _Runtime(
            self.path,
            autostart=False,
            jev_config=_jev_config(),
            service=_StubService(outcome),
        )

    def _candidates(self, count):
        return [
            {"id": f"work-{index}", "question": f"问题 {index}？", "generation": 1,
             "listener_id": "L1", "sender_id": "S2", "pending_reason": ""}
            for index in range(count)
        ]

    def test_no_candidate_matches_returns_nomatch_without_falling_back(self):
        outcome = _outcome({"does_message_answer_q0": 0.02, "does_message_answer_q1": 0.01})
        runtime = self._runtime_with(outcome)
        try:
            tag, classified = runtime._jev_match_human_answer(
                listener_row={"id": "L1"},
                message={"contentType": "text", "text": "好的", "senderId": "S1"},
                candidates=self._candidates(2),
            )
            self.assertEqual(tag, "nomatch")
            self.assertIsNone(classified)
        finally:
            runtime.close()

    def test_one_candidate_matching_is_mapped_to_human_answer(self):
        outcome = _outcome({"does_message_answer_q0": 0.02, "does_message_answer_q1": 0.93})
        runtime = self._runtime_with(outcome)
        try:
            tag, classified = runtime._jev_match_human_answer(
                listener_row={"id": "L1"},
                message={"contentType": "text", "text": "已经修好了", "senderId": "S1"},
                candidates=self._candidates(2),
            )
            self.assertEqual(tag, "match")
            labels = {row["id"]: value for row, value in classified}
            self.assertEqual(labels["work-1"], {"human_answer"})
            self.assertEqual(labels["work-0"], set())
        finally:
            runtime.close()

    def test_multiple_positives_keep_the_existing_candidate_ordering(self):
        # Clearly separated positives are all flagged; the transactional caller
        # applies the first one in deterministic candidate order and returns, so
        # the runner-up stays visible in the audit without being acted upon.
        outcome = _outcome({"does_message_answer_q0": 0.95, "does_message_answer_q1": 0.72})
        runtime = self._runtime_with(outcome)
        try:
            tag, classified = runtime._jev_match_human_answer(
                listener_row={"id": "L1"},
                message={"contentType": "text", "text": "两个都解决了", "senderId": "S1"},
                candidates=self._candidates(2),
            )
            self.assertEqual(tag, "match")
            self.assertEqual(
                [row["id"] for row, labels in classified if labels], ["work-0", "work-1"]
            )
        finally:
            runtime.close()

    def test_near_tie_between_candidates_defers_instead_of_picking_one(self):
        # 0.91 vs 0.88 is inside MATCH_TIE_TOLERANCE: the spec forbids settling
        # that silently, so the existing matcher must run instead.
        outcome = _outcome({"does_message_answer_q0": 0.91, "does_message_answer_q1": 0.88})
        runtime = self._runtime_with(outcome)
        try:
            tag, classified = runtime._jev_match_human_answer(
                listener_row={"id": "L1"},
                message={"contentType": "text", "text": "两个都好了", "senderId": "S1"},
                candidates=self._candidates(2),
            )
            self.assertEqual(tag, "fallback")
            self.assertIsNone(classified)
        finally:
            runtime.close()

    def test_review_band_falls_back_to_the_existing_matcher(self):
        outcome = _outcome({"does_message_answer_q0": 0.5}, applied=False, status="review")
        runtime = self._runtime_with(outcome)
        try:
            tag, _classified = runtime._jev_match_human_answer(
                listener_row={"id": "L1"},
                message={"contentType": "text", "text": "嗯", "senderId": "S1"},
                candidates=self._candidates(1),
            )
            self.assertEqual(tag, "fallback")
        finally:
            runtime.close()

    def test_shadow_never_changes_which_work_a_message_joins(self):
        outcome = _outcome({"does_message_answer_q0": 0.95}, applied=False, status="shadow")
        runtime = self._runtime_with(outcome)
        try:
            tag, _classified = runtime._jev_match_human_answer(
                listener_row={"id": "L1"},
                message={"contentType": "text", "text": "已解决", "senderId": "S1"},
                candidates=self._candidates(1),
            )
            self.assertEqual(tag, "fallback")
        finally:
            runtime.close()

    def test_candidate_count_is_capped_by_config(self):
        service = _StubService(_outcome({"does_message_answer_q0": 0.01}))
        runtime = _Runtime(
            self.path,
            autostart=False,
            jev_config=_jev_config(max_candidates=2),
            service=service,
        )
        try:
            runtime._jev_match_human_answer(
                listener_row={"id": "L1"},
                message={"contentType": "text", "text": "好的", "senderId": "S1"},
                candidates=self._candidates(6),
            )
            # Only the capped candidates are encoded into the request.
            self.assertEqual(len(service.asked["questions"].ids()), 2)
        finally:
            runtime.close()

    def test_image_only_message_skips_the_service(self):
        service = _StubService(_outcome({}))
        runtime = _Runtime(
            self.path,
            autostart=False,
            jev_config=_jev_config(),
            service=service,
        )
        try:
            tag, _classified = runtime._jev_match_human_answer(
                listener_row={"id": "L1"},
                message={
                    "contentType": "image",
                    "text": "[图片]",
                    "senderId": "S1",
                    "images": [{"localPath": "C:/missing/photo.png"}],
                },
                candidates=self._candidates(1),
            )
            self.assertEqual(tag, "fallback")
            self.assertEqual(service.skips[-1]["reason"], "image_only")
            self.assertFalse(hasattr(service, "asked"))
        finally:
            runtime.close()


class StaleGenerationTests(unittest.TestCase):
    """A candidate that moved while Jev was thinking must not be modified."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "runtime.sqlite3"
        self.runtime = _Runtime(self.path, autostart=False, jev_config=_jev_config())

    def tearDown(self):
        self.runtime.close()
        self._directory.cleanup()

    def _seed(self, *, status="collecting", generation=1, listener_generation=1):
        now = self.runtime._now()
        with self.runtime.store.transaction() as db:
            db.execute(
                """INSERT INTO reply_listeners(id,public_json,revision,generation,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                ("L1", encode_json({"groupId": "G1", "enabled": True}), 0, listener_generation, now, now),
            )
            db.execute(
                """INSERT INTO reply_work_items(
                       id,listener_id,group_id,sender_id,status,question,generation,listener_generation,
                       created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("work-0", "L1", "G1", "S2", status, "订单页为什么转圈？", generation, listener_generation, now, now),
            )
            db.execute(
                """INSERT INTO reply_inbox(
                       listener_id,group_id,message_id,sequence,send_time,payload_json,
                       assigned_work_id,received_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                ("L1", "G1", "m1", 1, now - 60, "{}", "work-0", now - 60),
            )
        return now

    def test_stale_generation_is_dropped(self):
        now = self._seed(generation=1)
        runtime = self.runtime

        class RacingService(_StubService):
            """Simulates the work advancing while the Jev request is in flight."""

            def ask(self, **kwargs):
                with runtime.store.transaction() as db:
                    db.execute("UPDATE reply_work_items SET generation=2 WHERE id='work-0'")
                return super().ask(**kwargs)

        # Jev proposes a match, so the generation guard is the only thing that
        # can reject it.
        runtime.jev = RacingService(_outcome({"does_message_answer_q0": 0.95}))

        result = runtime._match_human_answer(
            {"id": "L1", "generation": 1},
            {"id": 1, "send_time": now, "sequence": 2},
            {"contentType": "text", "text": "已经修好了", "senderId": "S1"},
            now,
        )
        self.assertIsNone(result)
        with self.runtime.store.lock:
            row = self.runtime.store.connection.execute(
                "SELECT status,generation FROM reply_work_items WHERE id='work-0'"
            ).fetchone()
        self.assertEqual(row["status"], "collecting")
        self.assertEqual(int(row["generation"]), 2)

    def test_current_generation_is_updated(self):
        now = self._seed(generation=1)
        service = _StubService(_outcome({"does_message_answer_q0": 0.95}))
        self.runtime.jev = service

        result = self.runtime._match_human_answer(
            {"id": "L1", "generation": 1},
            {"id": 1, "send_time": now, "sequence": 2},
            {"contentType": "text", "text": "已经修好了", "senderId": "S1"},
            now,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["workId"], "work-0")
        with self.runtime.store.lock:
            row = self.runtime.store.connection.execute(
                "SELECT status,pending_reason FROM reply_work_items WHERE id='work-0'"
            ).fetchone()
        self.assertEqual(row["status"], "answered_by_human")
        self.assertEqual(row["pending_reason"], "answered_by_human")

    def test_listener_generation_change_is_dropped(self):
        now = self._seed(listener_generation=1)
        with self.runtime.store.transaction() as db:
            db.execute("UPDATE reply_listeners SET generation=5 WHERE id='L1'")
        service = _StubService(_outcome({"does_message_answer_q0": 0.95}))
        self.runtime.jev = service

        result = self.runtime._match_human_answer(
            {"id": "L1", "generation": 1},
            {"id": 1, "send_time": now, "sequence": 2},
            {"contentType": "text", "text": "已经修好了", "senderId": "S1"},
            now,
        )
        self.assertIsNone(result)


def _health_response():
    """A response covering exactly the synthetic health-check questions."""
    return {
        "model": "jev-1.13.0",
        "answers": {
            "probe_noul": {"type": "noul", "noul": 0.9},
            "probe_choice": {
                "type": "choice",
                "choice": "orders",
                "probabilities": {"auth": 0.1, "orders": 0.8, "none": 0.1},
                "confidence": 0.7,
            },
            "probe_score": {
                "type": "score",
                "score": 1.0,
                "legend": {"0": "不严重", "1": "一般", "2": "严重"},
                "probabilities": {"0": 0.2, "1": 0.6, "2": 0.2},
                "confidence": 0.6,
            },
        },
        "usage": {"input_tokens": 429, "output_tokens": 72},
    }


class HealthCheckCommandTests(unittest.TestCase):
    """`jev.test_connection` / `jev.status` -- the settings-page probe."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "runtime.sqlite3"

    def tearDown(self):
        self._directory.cleanup()

    def _runtime(self, script, **overrides):
        opener = _StubOpener(list(script))
        service = JevSemanticService(
            lambda: {"jev": {}},
            opener=opener,
            sleeper=lambda _s: None,
        )
        runtime = _Runtime(
            self.path, autostart=False, jev_config=_jev_config(**overrides), service=service
        )
        return runtime, opener

    def _run_health_check(self, runtime, command_id="health-1"):
        status = runtime.query({"kind": "jev.status"})
        return runtime.execute(
            {
                "protocolVersion": 1,
                "commandId": command_id,
                "expectedRevision": status["revision"],
                "body": {"kind": "jev.test_connection"},
            }
        )

    def test_passing_check_is_recorded_and_then_reported(self):
        runtime, opener = self._runtime(
            [(_health_response(), {"X-TypeSafe-Request-Id": "req-abc"})]
        )
        try:
            before = runtime.query({"kind": "jev.status"})
            self.assertTrue(before["available"])
            self.assertTrue(before["configured"])
            self.assertIsNone(before["testedAt"])

            result = self._run_health_check(runtime)
            self.assertTrue(result["result"]["ok"])
            self.assertEqual(result["result"]["primitives"], ["choice", "noul", "score"])
            self.assertEqual(result["result"]["requestId"], "req-abc")
            self.assertEqual(result["result"]["responseModel"], "jev-1.13.0")
            self.assertEqual(result["result"]["requestedModel"], "jev-latest")
            self.assertEqual(opener.calls, 1)

            after = runtime.query({"kind": "jev.status"})
            self.assertIsNotNone(after["testedAt"])
            self.assertTrue(after["current"])
            self.assertTrue(after["result"]["ok"])
        finally:
            runtime.close()

    def test_status_never_returns_the_api_key(self):
        runtime, _opener = self._runtime([(_health_response(), {})])
        try:
            self._run_health_check(runtime)
            payload = json.dumps(runtime.query({"kind": "jev.status"}), ensure_ascii=False)
            self.assertNotIn("test-key-0123456789", payload)
            self.assertTrue(runtime.query({"kind": "jev.status"})["apiKeyConfigured"])
        finally:
            runtime.close()

    def test_failing_check_is_recorded_as_not_ok(self):
        runtime, _opener = self._runtime([401])
        try:
            result = self._run_health_check(runtime)
            self.assertFalse(result["result"]["ok"])
            self.assertEqual(result["result"]["kind"], "config_error")

            after = runtime.query({"kind": "jev.status"})
            self.assertIsNotNone(after["testedAt"])
            self.assertFalse(after["result"]["ok"])
        finally:
            runtime.close()

    def test_disabled_config_refuses_to_run_the_check(self):
        runtime, opener = self._runtime([_health_response()], enabled=False)
        try:
            with self.assertRaises(Exception) as caught:
                self._run_health_check(runtime)
            self.assertIn("JEV_DISABLED", str(caught.exception) + str(getattr(caught.exception, "code", "")))
            self.assertEqual(opener.calls, 0)
        finally:
            runtime.close()

    def test_unconfigured_endpoint_refuses_to_run_the_check(self):
        runtime, opener = self._runtime([_health_response()], base_url="")
        try:
            with self.assertRaises(Exception) as caught:
                self._run_health_check(runtime)
            self.assertIn(
                "JEV_NOT_CONFIGURED", str(caught.exception) + str(getattr(caught.exception, "code", ""))
            )
            self.assertEqual(opener.calls, 0)
        finally:
            runtime.close()

    def test_replaying_a_command_id_does_not_re_run_the_probe(self):
        runtime, opener = self._runtime([(_health_response(), {})])
        try:
            status = runtime.query({"kind": "jev.status"})
            # An identical envelope: the revision necessarily moved after the
            # first run, so a replay must resend the original expectedRevision.
            envelope = {
                "protocolVersion": 1,
                "commandId": "health-replay",
                "expectedRevision": status["revision"],
                "body": {"kind": "jev.test_connection"},
            }
            first = runtime.execute(envelope)
            second = runtime.execute(envelope)
            self.assertEqual(first["testedAt"], second["testedAt"])
            self.assertEqual(opener.calls, 1)
        finally:
            runtime.close()

    def test_reused_command_id_with_a_new_revision_is_rejected(self):
        runtime, _opener = self._runtime([(_health_response(), {})])
        try:
            first = self._run_health_check(runtime)
            with self.assertRaises(Exception):
                self._run_health_check(runtime)
            self.assertIsNotNone(first["testedAt"])
        finally:
            runtime.close()

    def test_stale_revision_is_rejected(self):
        runtime, opener = self._runtime([(_health_response(), {})])
        try:
            with self.assertRaises(Exception):
                runtime.execute(
                    {
                        "protocolVersion": 1,
                        "commandId": "health-stale",
                        "expectedRevision": 9_999,
                        "body": {"kind": "jev.test_connection"},
                    }
                )
            self.assertEqual(opener.calls, 0)
        finally:
            runtime.close()


class ClassificationWiringTests(unittest.TestCase):
    """The `_jev_classify` seam used by both runtime decision points."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "runtime.sqlite3"

    def tearDown(self):
        self._directory.cleanup()

    def _runtime(self, outcome):
        service = _StubService(outcome)
        runtime = _Runtime(
            self.path, autostart=False, jev_config=_jev_config(), service=service
        )
        return runtime, service

    def test_assist_passes_text_state_and_the_five_questions(self):
        outcome = _outcome({"is_question": 0.95})
        runtime, service = self._runtime(outcome)
        try:
            result = runtime._jev_classify(
                listener_row={"id": "L1"},
                messages=[{"contentType": "text", "text": "登录后订单页转圈"}],
                question=None,
                work_id="w1",
                generation=3,
                scope_kind="work",
            )
            self.assertIsNotNone(result)
            self.assertTrue(result.applied)
            self.assertEqual(
                sorted(service.asked["questions"].ids()),
                ["is_chat", "is_human_answer", "is_question", "is_supplement", "is_withdrawn"],
            )
            self.assertEqual(
                service.asked["state"]["message"]["text"], "登录后订单页转圈"
            )
            self.assertEqual(service.asked["work_id"], "w1")
            self.assertEqual(service.asked["generation"], 3)
        finally:
            runtime.close()

    def test_image_markers_are_never_put_into_the_state(self):
        # `_question_text` would substitute "[图片]"; the Jev state must not,
        # otherwise an image-only message looks readable.
        outcome = _outcome({"is_question": 0.95})
        runtime, service = self._runtime(outcome)
        try:
            runtime._jev_classify(
                listener_row={"id": "L1"},
                messages=[
                    {"contentType": "text", "text": "订单页转圈"},
                    {"contentType": "image", "text": "[图片]", "images": [{"localPath": "C:/x.png"}]},
                ],
                question=None,
                work_id="w1",
                generation=1,
                scope_kind="work",
            )
            text = service.asked["state"]["message"]["text"]
            self.assertIn("订单页转圈", text)
            self.assertNotIn("[图片]", text)
        finally:
            runtime.close()

    def test_shadow_outcome_is_returned_but_not_applied(self):
        outcome = _outcome({"is_question": 0.95}, applied=False, status="shadow")
        runtime, _service = self._runtime(outcome)
        try:
            result = runtime._jev_classify(
                listener_row={"id": "L1"},
                messages=[{"contentType": "text", "text": "hi"}],
                question=None,
                work_id="w1",
                generation=1,
                scope_kind="work",
            )
            self.assertIsNotNone(result)
            self.assertFalse(result.applied)
            self.assertEqual(result.status, "shadow")
        finally:
            runtime.close()


class EmptyCandidateTests(unittest.TestCase):
    def test_no_candidates_returns_none_without_calling_jev(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            service = _StubService(_outcome({}))
            runtime = _Runtime(
                path, autostart=False, jev_config=_jev_config(), service=service
            )
            try:
                result = runtime._match_human_answer(
                    {"id": "L1", "generation": 1},
                    {"id": 1, "send_time": 0.0, "sequence": 1},
                    {"contentType": "text", "text": "有人吗", "senderId": "S1"},
                    0.0,
                )
                self.assertIsNone(result)
                self.assertFalse(hasattr(service, "asked"))
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
