from __future__ import annotations

import json
import unittest
from unittest import mock

from worker.wechat import actions


class BootstrapTests(unittest.TestCase):
    def test_bootstrap_returns_one_payload_without_inlined_media(self):
        runtime = object()
        with mock.patch.object(actions, "_detect", return_value={"found": True}), \
            mock.patch.object(actions, "_projects", return_value={"projects": [{"id": "p1"}]}), \
            mock.patch.object(actions, "_settings_snapshot", return_value={"settings": {"contextMessageLimit": 50}}), \
            mock.patch.object(actions, "_providers", return_value={"providers": [], "mcpServers": []}), \
            mock.patch.object(actions, "_messages", return_value={"messages": [{"id": "m1", "media": [{"kind": "image"}]}], "hasMore": False}):
            result = actions._bootstrap(runtime, {"projectId": "p1", "messageLimit": 10})

        self.assertTrue(result["detect"]["found"])
        self.assertEqual(result["workspace"]["projects"][0]["id"], "p1")
        self.assertEqual(result["settings"]["settings"]["contextMessageLimit"], 50)
        self.assertEqual(result["messages"][0]["id"], "m1")
        self.assertNotIn("dataUrl", json.dumps(result))
        self.assertNotIn("base64", json.dumps(result))

    def test_bootstrap_without_project_skips_messages(self):
        runtime = object()
        with mock.patch.object(actions, "_detect", return_value={"found": True}), \
            mock.patch.object(actions, "_projects", return_value={"projects": []}), \
            mock.patch.object(actions, "_settings_snapshot", return_value={}), \
            mock.patch.object(actions, "_providers", return_value={}), \
            mock.patch.object(actions, "_messages", side_effect=AssertionError("should not read messages")):
            result = actions._bootstrap(runtime, {})

        self.assertNotIn("messages", result)


if __name__ == "__main__":
    unittest.main()
