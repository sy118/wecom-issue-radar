from __future__ import annotations

import io
import json
import unittest

from worker.wechat import stdio


def line(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False) + "\n"


class WechatStdioTests(unittest.TestCase):
    def run_requests(self, requests: list[str], handler) -> list[dict]:
        output = io.StringIO()
        stdio.serve_wechat_runtime(io.StringIO("".join(requests)), output, handler=handler)
        return [json.loads(item) for item in output.getvalue().splitlines() if item.strip()]

    def test_correlates_requests_by_id(self):
        calls: list[tuple[str, dict]] = []

        def handler(action: str, payload: dict) -> dict:
            calls.append((action, payload))
            return {"echo": action, "value": payload.get("value")}

        responses = self.run_requests(
            [
                line({"id": "a", "action": "projects", "payload": {"value": 1}}),
                line({"id": "b", "action": "settings", "payload": {"value": 2}}),
            ],
            handler,
        )

        self.assertEqual([item["id"] for item in responses], ["a", "b"])
        self.assertTrue(all(item["ok"] for item in responses))
        self.assertEqual(responses[0]["data"], {"echo": "projects", "value": 1})
        self.assertEqual(calls[0][0], "projects")

    def test_bad_json_returns_structured_error_without_crashing(self):
        responses = self.run_requests(
            ["not-json\n", line({"id": "next", "action": "settings", "payload": {}})],
            lambda action, payload: {"action": action},
        )

        self.assertEqual(len(responses), 2)
        self.assertFalse(responses[0]["ok"])
        self.assertEqual(responses[0]["id"], None)
        self.assertTrue(responses[1]["ok"])

    def test_missing_action_is_rejected(self):
        responses = self.run_requests(
            [line({"id": 3, "payload": {}})],
            lambda action, payload: {},
        )

        self.assertFalse(responses[0]["ok"])
        self.assertEqual(responses[0]["id"], 3)

    def test_handler_exception_is_structured(self):
        def handler(action: str, payload: dict) -> dict:
            raise RuntimeError("boom")

        responses = self.run_requests(
            [line({"id": "err", "action": "messages", "payload": {}})],
            handler,
        )

        self.assertFalse(responses[0]["ok"])
        self.assertEqual(responses[0]["error"]["message"], "boom")

    def test_blank_lines_are_ignored(self):
        responses = self.run_requests(
            ["\n", line({"id": "a", "action": "settings", "payload": {}})],
            lambda action, payload: {},
        )
        self.assertEqual(len(responses), 1)


if __name__ == "__main__":
    unittest.main()
