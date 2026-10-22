from __future__ import annotations

import unittest

from worker.wechat.wechat_content import parse_message


def quote_message(refer_type: str, content: str) -> dict:
    payload = (
        "<msg><appmsg><title>引用</title><type>57</type><refermsg>"
        "<displayname>张三</displayname>"
        f"<type>{refer_type}</type>"
        f"<content>{content}</content>"
        "</refermsg></appmsg></msg>"
    )
    kind, text, quote, _media, _mentioned = parse_message(49, payload, "wxid_me")
    assert kind == "quote"
    assert text == "引用"
    assert quote is not None
    return quote


class QuoteTextTests(unittest.TestCase):
    def test_image_reference_does_not_leak_xml(self):
        quote = quote_message("3", '<msg><img aeskey="deadbeef" cdnthumburl="http://x"/></msg>')
        self.assertEqual(quote["text"], "[图片]")
        self.assertEqual(quote["sender"], "张三")

    def test_sticker_and_voice_and_video_placeholders(self):
        self.assertEqual(quote_message("47", "<msg/>")["text"], "[表情]")
        self.assertEqual(quote_message("34", "<msg/>")["text"], "[语音]")
        self.assertEqual(quote_message("43", "<msg/>")["text"], "[视频]")

    def test_text_reference_stays_readable(self):
        quote = quote_message("1", "原始消息内容")
        self.assertEqual(quote["text"], "原始消息内容")

    def test_link_reference_uses_title(self):
        quote = quote_message("49", "<msg><appmsg><title>需求文档</title></appmsg></msg>")
        self.assertEqual(quote["text"], "需求文档")

    def test_unknown_xml_reference_is_placeholder(self):
        quote = quote_message("49", "<msg><unknown/></msg>")
        self.assertEqual(quote["text"], "[非文本消息]")


if __name__ == "__main__":
    unittest.main()
