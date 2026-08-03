from __future__ import annotations

import tempfile
import unittest

from tests.test_reply_runtime_additional import (
    FakeClock,
    FakeMcp,
    FakeMessages,
    FakeWebhook,
    ScriptedModel,
    add_message,
    baseline,
    command,
    configure_runtime,
    drive_to_retrieval,
)
from worker.reply_runtime import RuntimeProtocolError
from worker.reply_runtime.runtime import _has_evidence


class ReplyRuntimeEvidenceGateTests(unittest.TestCase):
    def test_common_zero_result_text_payloads_do_not_generate_or_send(self):
        cases = {
            "zero-record-summary": {
                "content": [{"type": "text", "text": "查询成功，共 0 条记录"}],
            },
            "bare-zero-string": "0",
        }

        for name, payload in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                class TrackingModel(ScriptedModel):
                    def __init__(self):
                        super().__init__(answer="answer generated from an empty result")
                        self.answer_calls = 0

                    def answer(self, **kwargs):
                        self.answer_calls += 1
                        return super().answer(**kwargs)

                clock = FakeClock()
                source = FakeMessages()
                webhook = FakeWebhook()
                model = TrackingModel()
                runtime, listener = configure_runtime(
                    directory,
                    clock=clock,
                    messages=source,
                    model=model,
                    mcp=FakeMcp(payload),
                    webhook=webhook,
                    listener_overrides={
                        "webhookUrl": (
                            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
                            f"zero-result-{name}"
                        ),
                    },
                )
                try:
                    tested = command(
                        runtime,
                        f"test-{name}-webhook",
                        3,
                        {"kind": "listener.test_webhook", "listenerId": "listener"},
                    )
                    command(
                        runtime,
                        f"confirm-{name}-webhook",
                        tested["revision"],
                        {
                            "kind": "listener.confirm_webhook",
                            "listenerId": "listener",
                            "testCode": tested["testCode"],
                            "appearedInSelectedGroup": True,
                        },
                    )
                    listener.pop("webhookUrl")
                    listener["autoSend"] = True
                    command(
                        runtime,
                        f"enable-{name}-auto-send",
                        5,
                        {"kind": "listener.save", "listener": listener},
                    )
                    webhook.calls.clear()
                    baseline(runtime, revision=6)
                    add_message(
                        source,
                        number=1,
                        sender_id="alice",
                        account="alice",
                        text="Why is this record present?",
                    )
                    drive_to_retrieval(
                        runtime,
                        clock,
                        revision=6,
                        prefix=f"zero-result-{name}",
                    )
                    listed = runtime.query({"kind": "work.list"})["items"][0]
                    item = runtime.query(
                        {"kind": "work.detail", "workId": listed["id"]}
                    )["item"]
                finally:
                    runtime.close()

                self.assertEqual(
                    (model.answer_calls, len(model.review_answers), len(webhook.calls)),
                    (0, 0, 0),
                )
                self.assertEqual(item["status"], "skipped_no_evidence")
                self.assertEqual(item["evidence"], [])
                self.assertEqual(item["answer"], "")

    def test_image_cannot_replace_empty_mcp_evidence_for_automatic_reply(self):
        class TrackingModel(ScriptedModel):
            def __init__(self):
                super().__init__(answer="answer generated without MCP evidence")
                self.answer_calls = 0

            def answer(self, **kwargs):
                self.answer_calls += 1
                return super().answer(**kwargs)

        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            source = FakeMessages()
            webhook = FakeWebhook()
            model = TrackingModel()
            runtime, listener = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=model,
                mcp=FakeMcp({"content": []}),
                webhook=webhook,
                listener_overrides={
                    "webhookUrl": (
                        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
                        "empty-evidence-with-image"
                    ),
                },
            )
            tested = command(
                runtime,
                "test-empty-evidence-webhook",
                3,
                {"kind": "listener.test_webhook", "listenerId": "listener"},
            )
            command(
                runtime,
                "confirm-empty-evidence-webhook",
                tested["revision"],
                {
                    "kind": "listener.confirm_webhook",
                    "listenerId": "listener",
                    "testCode": tested["testCode"],
                    "appearedInSelectedGroup": True,
                },
            )
            listener.pop("webhookUrl")
            listener["autoSend"] = True
            command(
                runtime,
                "enable-empty-evidence-auto-send",
                5,
                {"kind": "listener.save", "listener": listener},
            )

            # Ignore the ownership-test webhook; only business replies count below.
            webhook.calls.clear()
            baseline(runtime, revision=6)
            add_message(
                source,
                number=1,
                sender_id="alice",
                account="alice",
                text="Why do these records appear?",
            )
            source.rows[-1].update(
                contentType="image",
                images=[{"base64": "aA==", "mimeType": "image/png"}],
            )
            drive_to_retrieval(
                runtime,
                clock,
                revision=6,
                prefix="empty-evidence-with-image",
            )
            listed = runtime.query({"kind": "work.list"})["items"][0]
            item = runtime.query(
                {"kind": "work.detail", "workId": listed["id"]}
            )["item"]
            runtime.close()

        self.assertEqual(
            (model.answer_calls, len(model.review_answers), len(webhook.calls)),
            (0, 0, 0),
        )
        self.assertEqual(item["status"], "skipped_no_evidence")
        self.assertEqual(item["pendingReason"], "MCP returned no usable evidence")
        self.assertEqual(item["evidence"], [])
        self.assertEqual(item["answer"], "")
        self.assertIsNotNone(item["completedAt"])

    def test_manual_send_does_not_deliver_legacy_pending_work_without_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            source = FakeMessages()
            webhook = FakeWebhook()
            runtime, _listener = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=FakeMcp(),
                webhook=webhook,
                listener_overrides={
                    "webhookUrl": (
                        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
                        "legacy-pending-without-evidence"
                    ),
                },
            )
            try:
                baseline(runtime)
                add_message(
                    source,
                    number=1,
                    sender_id="alice",
                    account="alice",
                    text="Why is this record present?",
                )
                drive_to_retrieval(
                    runtime,
                    clock,
                    prefix="legacy-pending-without-evidence",
                )
                pending = runtime.query({"kind": "work.list"})["items"][0]
                self.assertEqual(pending["status"], "pending")

                # Pending rows created by an older release can contain an answer whose
                # only context was an image. Recreate that durable upgrade state while
                # exercising delivery through the public command boundary.
                with runtime.store.transaction() as db:
                    db.execute(
                        "UPDATE reply_work_items SET evidence_json='[]' WHERE id=?",
                        (pending["id"],),
                    )
                webhook.calls.clear()

                try:
                    command(
                        runtime,
                        "manual-send-legacy-empty-evidence",
                        3,
                        {
                            "kind": "work.send",
                            "workId": pending["id"],
                            "expectedVersion": pending["version"],
                        },
                    )
                except RuntimeProtocolError:
                    pass
                after = runtime.query(
                    {"kind": "work.detail", "workId": pending["id"]}
                )["item"]
            finally:
                runtime.close()

        self.assertEqual(webhook.calls, [])
        self.assertNotIn(after["status"], {"sending", "sent", "delivery_unknown"})

    def test_semantically_empty_mcp_payloads_are_not_evidence(self):
        cases = {
            "explicit-no-result-text": {
                "content": [{"type": "text", "text": "未检索到相关数据"}],
            },
            "serialized-empty-list": "[]",
            "successful-empty-query": {
                "structuredContent": {
                    "success": True,
                    "rows": [],
                    "message": "查询成功，无匹配记录",
                },
            },
            "zero-records-without-total-prefix": "查询成功，0 条记录",
            "zero-matches": "命中 0 条",
            "zero-result-count": "结果数：0",
        }

        for name, payload in cases.items():
            with self.subTest(case=name):
                self.assertFalse(_has_evidence(payload))


if __name__ == "__main__":
    unittest.main()
