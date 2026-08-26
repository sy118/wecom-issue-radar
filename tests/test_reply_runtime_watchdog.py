from __future__ import annotations

import json
import unittest

from worker.reply_runtime.runtime import (
    _image_retry_delay,
    _work_progress,
)


class ReplyRuntimeWatchdogUnitTests(unittest.TestCase):
    def test_image_retry_delay_uses_fast_interval_before_window(self):
        self.assertEqual(_image_retry_delay(100.0, 85.0), 2)
        self.assertEqual(_image_retry_delay(119.0, 100.0), 2)

    def test_image_retry_delay_uses_normal_interval_after_window(self):
        self.assertEqual(_image_retry_delay(130.0, 100.0), 5)
        self.assertEqual(_image_retry_delay(1000.0, 100.0), 5)

    def test_work_progress_derives_round_and_last_event(self):
        row = {
            "provider_audit_json": json.dumps({"rounds": 4, "toolCalls": 7}),
            "stop_reason": "",
            "error_json": None,
            "answer_engine": "mcp",
            "created_at": 1000.0,
        }
        timeline = [
            {
                "kind": "model_decision_started",
                "metadata": {"round": 3, "toolCalls": 5},
                "createdAt": "2026-08-26T00:00:00+00:00",
            },
            {
                "kind": "tool_call_completed",
                "metadata": {"round": 4, "toolCalls": 7},
                "createdAt": "2026-08-26T00:00:05+00:00",
            },
        ]
        progress = _work_progress(row, {"maxAgentRounds": 200}, timeline, 1015.0)
        self.assertEqual(progress["currentRound"], 4)
        self.assertEqual(progress["toolCalls"], 7)
        self.assertEqual(progress["lastEventKind"], "tool_call_completed")
        self.assertEqual(progress["maxRounds"], 200)
        self.assertEqual(progress["elapsedSeconds"], 15)

    def test_work_progress_uses_stop_reason_and_error_code(self):
        row = {
            "provider_audit_json": "{}",
            "stop_reason": "agent_stalled",
            "error_json": json.dumps({"code": "AGENT_STALLED", "message": "stalled"}),
            "answer_engine": "dify",
            "created_at": 100.0,
        }
        progress = _work_progress(row, {}, [], 100.0)
        self.assertEqual(progress["stopReason"], "agent_stalled")
        self.assertEqual(progress["errorCode"], "AGENT_STALLED")
        self.assertEqual(progress["maxRounds"], 1)


if __name__ == "__main__":
    unittest.main()
