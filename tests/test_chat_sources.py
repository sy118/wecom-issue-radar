from __future__ import annotations

import unittest

from worker.chat_sources import ChatMessage, messages_to_dicts, normalized_keys


class ChatSourceNormalizationTests(unittest.TestCase):
    def _wecom(self) -> ChatMessage:
        return ChatMessage(
            source="wecom",
            account_id="corp-1",
            chat_id="R:123",
            chat_name="客服群",
            chat_kind="group",
            sender_id="zhangsan",
            sender_name="张三",
            direction="incoming",
            kind="text",
            text="登录报错",
            timestamp=1_700_000_000,
            sequence=4,
            source_key="wecom:123:4",
            mentioned_me=True,
        )

    def _wechat(self) -> ChatMessage:
        return ChatMessage(
            source="wechat",
            account_id="wxid-1",
            chat_id="wxid-2",
            chat_name="小王",
            chat_kind="direct",
            sender_id="wxid-2",
            sender_name="小王",
            direction="outgoing",
            kind="image",
            text="",
            timestamp=1_700_000_100,
            sequence=1,
            source_key="wechat:db:1",
            media=[{"status": "ready", "filename": "a.jpg"}],
        )

    def test_both_sources_share_one_serialization_shape(self) -> None:
        messages = [self._wecom(), self._wechat()]
        payloads = messages_to_dicts(messages)
        self.assertEqual(len(payloads), 2)
        for payload in payloads:
            self.assertEqual(tuple(payload.keys()), normalized_keys())
            self.assertIn(payload["source"], {"wecom", "wechat"})
            self.assertIn(payload["direction"], {"incoming", "outgoing", "system"})
            self.assertIsInstance(payload["media"], list)

    def test_media_and_quote_survive_serialization(self) -> None:
        payload = messages_to_dicts([self._wechat()])[0]
        self.assertEqual(payload["media"][0]["status"], "ready")
        self.assertIsNone(payload["quote"])
        self.assertFalse(payload["mentionedMe"])

    def test_protocol_is_structural(self) -> None:
        from worker.chat_sources import ChatSource

        class FakeSource:
            def inspect(self): return None
            def watermark(self): return {}
            def read(self, cursor=None, limit=200): return [], {}
            def refresh_media(self, messages): return list(messages)
            def close(self): return None

        self.assertIsInstance(FakeSource(), ChatSource)


if __name__ == "__main__":
    unittest.main()
