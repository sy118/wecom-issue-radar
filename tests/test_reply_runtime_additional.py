from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from worker.reply_runtime import ReplyRuntime, RuntimeProtocolError


class FakeClock:
    def __init__(self, value: float = 1_000.0):
        self.value = value

    def now(self) -> float:
        return self.value


class FakeMessages:
    def __init__(self):
        self.rows: list[dict] = []

    def watermark(self, listener):
        return self.rows[-1]["cursor"] if self.rows else [999, 0, 0, 1]

    def read(self, listener, cursor):
        return [
            row
            for row in self.rows
            if cursor is None or tuple(row["cursor"]) > tuple(cursor)
        ]

    def read_force(self, listener, cursor):
        return self.read(listener, cursor)


class ScriptedModel:
    def __init__(
        self,
        *,
        answer: str = "Evidence-backed answer",
        compressed: str | None = None,
        review_supported: bool = True,
    ):
        self.answer_text = answer
        self.compressed = compressed
        self.review_supported = review_supported
        self.review_answers: list[str] = []

    def classify(self, *, messages, groupContext, question=None):
        text = str(messages[-1].get("text") or "")
        if question is not None:
            return {"labels": ["chat"]}
        return {"labels": ["chat"] if text.startswith("chat:") else ["question"]}

    def plan_tools(self, **kwargs):
        return [{"serverId": "kb", "toolName": "search", "arguments": {"query": kwargs["question"]}}]

    def answer(self, **kwargs):
        return self.answer_text

    def compress(self, **kwargs):
        return self.compressed if self.compressed is not None else kwargs["answer"]

    def review(self, **kwargs):
        self.review_answers.append(kwargs["answer"])
        return {"supported": self.review_supported, "reason": "scripted review"}


class FakeMcp:
    def __init__(self, result=None):
        self.result = result if result is not None else {"content": "useful evidence"}
        self.calls: list[dict] = []

    def discover(self, server):
        return [{"name": "search", "inputSchema": {"type": "object"}}]

    def call(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class FakeWebhook:
    def __init__(self, response=None):
        self.response = response if response is not None else {"status": "sent"}
        self.calls: list[dict] = []

    def send(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def command(runtime: ReplyRuntime, command_id: str, revision: int, body: dict):
    return runtime.execute(
        {
            "protocolVersion": 1,
            "commandId": command_id,
            "expectedRevision": revision,
            "body": body,
        }
    )


def configure_runtime(
    directory: str,
    *,
    clock: FakeClock,
    messages: FakeMessages,
    model: ScriptedModel,
    mcp: FakeMcp,
    webhook: FakeWebhook | None = None,
    listener_overrides: dict | None = None,
):
    runtime = ReplyRuntime(
        Path(directory) / "reply-runtime.sqlite3",
        clock=clock,
        message_source=messages,
        model=model,
        mcp=mcp,
        webhook=webhook,
        autostart=False,
    )
    command(
        runtime,
        "save-mcp",
        0,
        {
            "kind": "mcp.save",
            "server": {
                "id": "kb",
                "name": "Knowledge",
                "enabled": True,
                "transportType": "sse",
                "url": "https://mcp.example.test/sse",
            },
        },
    )
    tool = command(runtime, "discover-mcp", 1, {"kind": "mcp.test", "serverId": "kb"})["tools"][0]
    listener = {
        "id": "listener",
        "name": "Support",
        "groupId": "room",
        "groupName": "Support room",
        "enabled": True,
        "pollIntervalSeconds": 2,
        "sameSenderMergeSeconds": 2,
        "humanReplyWaitSeconds": 10,
        "sessionTimeoutSeconds": 60,
        "maxConcurrency": 4,
        "mcpTimeoutSeconds": 900,
        "autoSend": False,
        "toolGrants": [
            {
                "serverId": "kb",
                "toolName": "search",
                "schemaSha256": tool["schemaSha256"],
            }
        ],
    }
    listener.update(listener_overrides or {})
    command(runtime, "save-listener", 2, {"kind": "listener.save", "listener": listener})
    return runtime, listener


def add_message(
    source: FakeMessages,
    *,
    number: int,
    sender_id: str,
    text: str,
    sender_name: str | None = None,
    account: str = "",
    mobile: str = "",
    send_time: int | None = None,
):
    timestamp = send_time if send_time is not None else 1_000 + number
    source.rows.append(
        {
            "cursor": [timestamp, number, number, 1],
            "messageId": str(number),
            "serverId": "1",
            "sequence": number,
            "sendTime": timestamp,
            "groupId": "room",
            "senderId": sender_id,
            "senderName": sender_name or sender_id.title(),
            "account": account,
            "mobile": mobile,
            "contentType": "text",
            "text": text,
        }
    )


def baseline(runtime: ReplyRuntime, revision: int = 3):
    command(runtime, "baseline", revision, {"kind": "runtime.tick", "wait": True})


def drive_to_retrieval(runtime: ReplyRuntime, clock: FakeClock, *, revision: int = 3, prefix: str = "flow"):
    clock.value = 1_005
    command(runtime, f"{prefix}-collect", revision, {"kind": "runtime.tick", "wait": True})
    clock.value = 1_007
    command(runtime, f"{prefix}-classify", revision, {"kind": "runtime.tick", "wait": True})
    clock.value = 1_017
    command(runtime, f"{prefix}-retrieve", revision, {"kind": "runtime.tick", "wait": True})


class ReplyRuntimeAdditionalPolicyTests(unittest.TestCase):
    def test_plain_text_that_mentions_an_image_marker_or_filename_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            clock, source = FakeClock(), FakeMessages()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=FakeMcp(),
            )
            baseline(runtime)
            add_message(
                source,
                number=1,
                sender_id="alice",
                text="请确认 [图片] 和 error.png 这两个字样",
            )
            clock.value = 1_005
            command(runtime, "literal-image-words", 3, {"kind": "runtime.tick", "wait": True})
            item = runtime.query({"kind": "work.list"})["items"][0]
            runtime.close()

        self.assertEqual(item["question"], "请确认 [图片] 和 error.png 这两个字样")

    def test_autostart_polling_continues_while_classification_is_blocked(self):
        class ObservedMessages(FakeMessages):
            def __init__(self):
                super().__init__()
                self.watermarked = threading.Event()
                self.first_seen = threading.Event()
                self.second_seen = threading.Event()

            def watermark(self, listener):
                result = super().watermark(listener)
                self.watermarked.set()
                return result

            def read(self, listener, cursor):
                rows = super().read(listener, cursor)
                ids = {row["messageId"] for row in rows}
                if "1" in ids:
                    self.first_seen.set()
                if "2" in ids:
                    self.second_seen.set()
                return rows

        class BlockingModel(ScriptedModel):
            def __init__(self):
                super().__init__()
                self.started = threading.Event()
                self.release = threading.Event()

            def classify(self, **kwargs):
                if kwargs.get("question") is None:
                    self.started.set()
                    self.release.wait(5)
                return super().classify(**kwargs)

        with tempfile.TemporaryDirectory() as directory:
            clock, source, model = FakeClock(), ObservedMessages(), BlockingModel()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=model,
                mcp=FakeMcp(),
            )
            runtime.start()
            try:
                self.assertTrue(source.watermarked.wait(2))
                add_message(source, number=1, sender_id="alice", text="Slow question?")
                clock.value = 1_005
                self.assertTrue(source.first_seen.wait(2))
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if runtime.query({"kind": "work.list"})["items"]:
                        break
                    time.sleep(0.02)
                self.assertTrue(runtime.query({"kind": "work.list"})["items"])
                clock.value = 1_008
                self.assertTrue(model.started.wait(3))
                add_message(source, number=2, sender_id="bob", text="Second question?")
                clock.value = 1_011
                polled_while_blocked = source.second_seen.wait(2)
            finally:
                model.release.set()
                runtime.close()

        self.assertTrue(polled_while_blocked)

    def test_initial_watermark_failure_waits_for_listener_poll_interval_before_retry(self):
        class FailingWatermarkMessages(FakeMessages):
            def __init__(self):
                super().__init__()
                self.watermark_calls = 0
                self.read_calls = 0

            def watermark(self, listener):
                self.watermark_calls += 1
                if self.watermark_calls <= 2:
                    raise RuntimeError("message database snapshot unavailable")
                return [1_004, 0, 0, 1]

            def read(self, listener, cursor):
                self.read_calls += 1
                return super().read(listener, cursor)

        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            source = FailingWatermarkMessages()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=FakeMcp(),
            )

            command(runtime, "poll-failure-first", 3, {"kind": "runtime.tick", "wait": True})
            clock.value += 1
            command(runtime, "poll-failure-too-soon", 3, {"kind": "runtime.tick", "wait": True})
            with runtime.store.lock:
                first_retry = runtime.store.connection.execute(
                    "SELECT cursor_json,next_poll_at FROM runtime_cursors WHERE listener_id='listener'"
                ).fetchone()
                first_failure_events = runtime.store.connection.execute(
                    "SELECT count(*) FROM runtime_events WHERE json_extract(event_json, '$.kind')='listener.poll_failed'"
                ).fetchone()[0]

            clock.value += 1
            command(runtime, "poll-failure-after-interval", 3, {"kind": "runtime.tick", "wait": True})
            failed_listener_health = runtime.query({"kind": "listener.list"})["listeners"][0][
                "health"
            ]
            clock.value += 2
            command(runtime, "poll-watermark-recovered", 3, {"kind": "runtime.tick", "wait": True})
            recovered_listener_health = runtime.query({"kind": "listener.list"})["listeners"][0][
                "health"
            ]
            with runtime.store.lock:
                second_retry = runtime.store.connection.execute(
                    "SELECT cursor_json,next_poll_at FROM runtime_cursors WHERE listener_id='listener'"
                ).fetchone()
                second_failure_events = runtime.store.connection.execute(
                    "SELECT count(*) FROM runtime_events WHERE json_extract(event_json, '$.kind')='listener.poll_failed'"
                ).fetchone()[0]
                recovery_events = runtime.store.connection.execute(
                    "SELECT count(*) FROM runtime_events WHERE json_extract(event_json, '$.kind')='listener.poll_recovered'"
                ).fetchone()[0]

            runtime.close()

        self.assertEqual(source.watermark_calls, 3)
        self.assertEqual(source.read_calls, 0)
        self.assertIsNotNone(first_retry)
        self.assertIsNone(first_retry["cursor_json"])
        self.assertEqual(float(first_retry["next_poll_at"]), 1_002.0)
        self.assertEqual(int(first_failure_events), 1)
        self.assertEqual(failed_listener_health["status"], "error")
        self.assertEqual(
            failed_listener_health["message"], "message database snapshot unavailable"
        )
        self.assertIsNotNone(second_retry)
        self.assertEqual(json.loads(second_retry["cursor_json"]), [1_004, 0, 0, 1])
        self.assertEqual(float(second_retry["next_poll_at"]), 1_006.0)
        self.assertEqual(int(second_failure_events), 1)
        self.assertEqual(int(recovery_events), 1)
        self.assertEqual(recovered_listener_health["status"], "ready")

    def test_stale_poll_failure_cannot_delay_a_new_listener_generation(self):
        class BlockingFailureMessages(FakeMessages):
            def __init__(self):
                super().__init__()
                self.read_calls = 0
                self.entered = threading.Event()
                self.release = threading.Event()

            def read(self, listener, cursor):
                self.read_calls += 1
                if self.read_calls == 1:
                    self.entered.set()
                    self.release.wait(5)
                    raise RuntimeError("stale message database failure")
                return []

        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            source = BlockingFailureMessages()
            runtime, listener = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=FakeMcp(),
                listener_overrides={"pollIntervalSeconds": 60},
            )
            baseline(runtime)
            clock.value = 1_060

            old_poll = threading.Thread(
                target=lambda: command(
                    runtime,
                    "stale-generation-poll",
                    3,
                    {"kind": "runtime.tick", "wait": True},
                )
            )
            old_poll.start()
            self.assertTrue(source.entered.wait(2))

            listener["pollIntervalSeconds"] = 2
            saved = command(
                runtime,
                "shorten-poll-interval",
                3,
                {"kind": "listener.save", "listener": listener},
            )
            with runtime.store.lock:
                before_release = runtime.store.connection.execute(
                    "SELECT cursor_json,next_poll_at FROM runtime_cursors WHERE listener_id='listener'"
                ).fetchone()

            source.release.set()
            old_poll.join(2)
            self.assertFalse(old_poll.is_alive())
            with runtime.store.lock:
                after_release = runtime.store.connection.execute(
                    "SELECT cursor_json,next_poll_at FROM runtime_cursors WHERE listener_id='listener'"
                ).fetchone()
                stale_failure_events = runtime.store.connection.execute(
                    """SELECT count(*) FROM runtime_events
                       WHERE json_extract(event_json, '$.kind')='listener.poll_failed'"""
                ).fetchone()[0]

            command(
                runtime,
                "poll-new-generation-immediately",
                saved["revision"],
                {"kind": "runtime.tick", "wait": True},
            )
            with runtime.store.lock:
                after_new_poll = runtime.store.connection.execute(
                    "SELECT cursor_json,next_poll_at FROM runtime_cursors WHERE listener_id='listener'"
                ).fetchone()
            runtime.close()

        self.assertEqual(after_release["cursor_json"], before_release["cursor_json"])
        self.assertEqual(after_release["next_poll_at"], before_release["next_poll_at"])
        self.assertEqual(int(stale_failure_events), 0)
        self.assertEqual(source.read_calls, 2)
        self.assertEqual(float(after_new_poll["next_poll_at"]), 1_062.0)

    def test_listener_update_does_not_reuse_a_prior_generation_poll_failure(self):
        class FailingWatermarkMessages(FakeMessages):
            def watermark(self, listener):
                raise RuntimeError("message database snapshot unavailable")

        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            runtime, listener = configure_runtime(
                directory,
                clock=clock,
                messages=FailingWatermarkMessages(),
                model=ScriptedModel(),
                mcp=FakeMcp(),
            )

            command(runtime, "poll-failure-before-disable", 3, {"kind": "runtime.tick", "wait": True})
            failed_health = runtime.query({"kind": "listener.list"})["listeners"][0]["health"]
            command(
                runtime,
                "disable-listener-after-poll-failure",
                3,
                {
                    "kind": "listener.save",
                    "listener": {**listener, "enabled": False},
                },
            )
            updated_health = runtime.query({"kind": "listener.list"})["listeners"][0]["health"]
            runtime.close()

        self.assertEqual(failed_health["status"], "error")
        self.assertEqual(updated_health["status"], "ready")

    def test_review_rejection_never_becomes_pending_or_calls_webhook(self):
        with tempfile.TemporaryDirectory() as directory:
            clock, source = FakeClock(), FakeMessages()
            model, mcp, webhook = ScriptedModel(review_supported=False), FakeMcp(), FakeWebhook()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=model,
                mcp=mcp,
                webhook=webhook,
                listener_overrides={
                    "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=review-false"
                },
            )
            baseline(runtime)
            add_message(source, number=1, sender_id="alice", text="How do I repair sync?", account="alice")
            drive_to_retrieval(runtime, clock, prefix="review-false")
            item = runtime.query({"kind": "work.list"})["items"][0]
            pending = runtime.query({"kind": "work.list", "bucket": "pending"})
            runtime.close()

        self.assertEqual(item["status"], "skipped_review_failed")
        self.assertEqual(item["answer"], "")
        self.assertEqual(pending["total"], 0)
        self.assertEqual(webhook.calls, [])

    def test_sender_session_expires_and_is_trimmed_to_six_turns_and_32k(self):
        with tempfile.TemporaryDirectory() as directory:
            clock, source = FakeClock(), FakeMessages()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=FakeMcp(),
                listener_overrides={"sessionTimeoutSeconds": 60},
            )
            listener = runtime.query({"kind": "listener.list"})["listeners"][0]
            with runtime.store.transaction() as db:
                for index in range(7):
                    runtime._append_session_turn_in_transaction(
                        db,
                        "listener",
                        "alice",
                        {"question": f"q{index}", "answer": f"a{index}"},
                        clock.value,
                    )
            six_turns = runtime._session_context("listener", "alice", listener)

            with runtime.store.transaction() as db:
                for index in range(7, 14):
                    runtime._append_session_turn_in_transaction(
                        db,
                        "listener",
                        "alice",
                        {"question": f"large-{index}", "answer": "答" * 6_000},
                        clock.value,
                    )
            bounded = runtime._session_context("listener", "alice", listener)
            other_sender = runtime._session_context("listener", "bob", listener)
            clock.value += 61
            expired = runtime._session_context("listener", "alice", listener)
            runtime.close()

        self.assertEqual(len(six_turns), 6)
        self.assertEqual(six_turns[0]["question"], "q1")
        self.assertLessEqual(len(bounded), 6)
        self.assertLessEqual(len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))), 32_000)
        self.assertEqual(other_sender, [])
        self.assertEqual(expired, [])

    def test_mobile_fallback_and_plain_at_acknowledgement_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            clock, source, webhook = FakeClock(), FakeMessages(), FakeWebhook()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=FakeMcp(),
                webhook=webhook,
                listener_overrides={
                    "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=mention-policy"
                },
            )
            baseline(runtime)
            add_message(
                source,
                number=1,
                sender_id="alice-local",
                sender_name="Alice",
                text="Mobile mention question?",
                mobile="13800138000",
            )
            add_message(
                source,
                number=2,
                sender_id="bob-local",
                sender_name="Bob",
                text="Plain mention question?",
            )
            drive_to_retrieval(runtime, clock, prefix="mention-policy")
            items = {item["senderId"]: item for item in runtime.query({"kind": "work.list"})["items"]}

            mobile_item = items["alice-local"]
            command(
                runtime,
                "send-mobile",
                3,
                {"kind": "work.send", "workId": mobile_item["id"], "expectedVersion": mobile_item["version"]},
            )
            plain_item = items["bob-local"]
            with self.assertRaises(RuntimeProtocolError) as unavailable:
                command(runtime, "send-no-identity", 3, {"kind": "work.send", "workId": plain_item["id"]})
            with self.assertRaises(RuntimeProtocolError) as missing_ack:
                command(runtime, "send-plain-no-ack", 3, {"kind": "work.send_plain_at", "workId": plain_item["id"]})
            command(
                runtime,
                "send-plain",
                3,
                {
                    "kind": "work.send_plain_at",
                    "workId": plain_item["id"],
                    "acknowledgement": "PLAIN_AT_IS_NOT_A_TRUE_MENTION",
                },
            )
            runtime.close()

        self.assertEqual(unavailable.exception.code, "TRUE_MENTION_UNAVAILABLE")
        self.assertEqual(missing_ack.exception.code, "PLAIN_AT_ACKNOWLEDGEMENT_REQUIRED")
        self.assertEqual(webhook.calls[0]["mentionedList"], [])
        self.assertEqual(webhook.calls[0]["mentionedMobileList"], ["13800138000"])
        self.assertEqual(webhook.calls[0]["text"], "Evidence-backed answer")
        self.assertEqual(webhook.calls[1]["mentionedList"], [])
        self.assertEqual(webhook.calls[1]["mentionedMobileList"], [])
        self.assertTrue(webhook.calls[1]["text"].startswith("@Bob\n"))

    def test_compressed_answer_is_reviewed_before_delivery(self):
        long_answer = "超" * 2_000
        compressed = "压缩后的证据答案"
        with tempfile.TemporaryDirectory() as directory:
            clock, source, webhook = FakeClock(), FakeMessages(), FakeWebhook()
            model = ScriptedModel(answer=long_answer, compressed=compressed)
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=model,
                mcp=FakeMcp(),
                webhook=webhook,
                listener_overrides={
                    "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=compressed-answer"
                },
            )
            baseline(runtime)
            add_message(source, number=1, sender_id="alice", text="Please find the documented fix", account="alice")
            drive_to_retrieval(runtime, clock, prefix="compressed-answer")
            item = runtime.query({"kind": "work.list"})["items"][0]
            command(runtime, "send-compressed", 3, {"kind": "work.send", "workId": item["id"]})
            runtime.close()

        self.assertEqual(item["answer"], compressed)
        self.assertEqual(model.review_answers, [compressed])
        self.assertLessEqual(len(webhook.calls[0]["text"].encode("utf-8")), 2048)

    def test_answer_still_over_limit_is_not_auto_sent(self):
        too_long = "长" * 2_000
        with tempfile.TemporaryDirectory() as directory:
            clock, source, webhook = FakeClock(), FakeMessages(), FakeWebhook()
            model = ScriptedModel(answer=too_long, compressed=too_long)
            runtime, listener = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=model,
                mcp=FakeMcp(),
                webhook=webhook,
                listener_overrides={
                    "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=over-limit"
                },
            )
            tested = command(runtime, "test-webhook", 3, {"kind": "listener.test_webhook", "listenerId": "listener"})
            command(
                runtime,
                "confirm-webhook",
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
            command(runtime, "enable-auto", 5, {"kind": "listener.save", "listener": listener})
            baseline(runtime, revision=6)
            add_message(source, number=1, sender_id="alice", text="Question with a very long answer?", account="alice")
            drive_to_retrieval(runtime, clock, revision=6, prefix="over-limit")
            item = runtime.query({"kind": "work.list"})["items"][0]
            runtime.close()

        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["pendingReason"], "answer_exceeds_webhook_limit")
        self.assertEqual(model.review_answers, [too_long])
        self.assertEqual(len(webhook.calls), 1)  # webhook ownership test only

    def test_recent_outbound_test_message_echo_is_not_reingested(self):
        with tempfile.TemporaryDirectory() as directory:
            clock, source, webhook = FakeClock(), FakeMessages(), FakeWebhook()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=FakeMcp(),
                webhook=webhook,
                listener_overrides={
                    "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=loop-filter"
                },
            )
            baseline(runtime)
            command(runtime, "test-webhook", 3, {"kind": "listener.test_webhook", "listenerId": "listener"})
            echo_text = webhook.calls[0]["text"]
            add_message(source, number=1, sender_id="robot", sender_name="Robot", text=echo_text)
            clock.value = 1_005
            command(runtime, "poll-echo", 4, {"kind": "runtime.tick", "wait": True})
            items = runtime.query({"kind": "work.list"})["items"]
            runtime.close()

        self.assertEqual(items, [])

    def test_webhook_test_echo_is_filtered_while_delivery_is_in_flight(self):
        class ReentrantWebhook(FakeWebhook):
            runtime = None
            source = None
            clock = None
            polled = None

            def send(self, **kwargs):
                self.calls.append(kwargs)
                add_message(
                    self.source,
                    number=1,
                    sender_id="robot",
                    sender_name="Robot",
                    text=kwargs["text"],
                )
                self.clock.value = 1_001
                self.polled = self.runtime._poll_messages(
                    listener_id="listener", force=True
                )
                return self.response

        with tempfile.TemporaryDirectory() as directory:
            clock, source, webhook = FakeClock(), FakeMessages(), ReentrantWebhook()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=FakeMcp(),
                webhook=webhook,
                listener_overrides={
                    "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=in-flight-loop-filter"
                },
            )
            webhook.runtime = runtime
            webhook.source = source
            webhook.clock = clock
            baseline(runtime)

            tested = command(
                runtime,
                "test-webhook-in-flight",
                3,
                {"kind": "listener.test_webhook", "listenerId": "listener"},
            )
            command(
                runtime,
                "settle-in-flight-echo",
                tested["revision"],
                {"kind": "runtime.tick", "wait": True},
            )
            with runtime.store.lock:
                inbox_count = runtime.store.connection.execute(
                    "SELECT count(*) FROM reply_inbox"
                ).fetchone()[0]
            items = runtime.query({"kind": "work.list"})["items"]
            runtime.close()

        self.assertEqual(webhook.polled, 0)
        self.assertEqual(int(inbox_count), 0)
        self.assertEqual(items, [])

    def test_true_mention_readback_with_visible_name_is_not_reingested(self):
        with tempfile.TemporaryDirectory() as directory:
            clock, source, webhook = FakeClock(), FakeMessages(), FakeWebhook()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=FakeMcp(),
                webhook=webhook,
                listener_overrides={
                    "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=mention-echo"
                },
            )
            baseline(runtime)
            add_message(
                source,
                number=1,
                sender_id="alice-local",
                sender_name="Alice",
                text="Question?",
                mobile="13800138000",
            )
            drive_to_retrieval(runtime, clock, prefix="mention-echo")
            work = runtime.query({"kind": "work.list"})["items"][0]
            command(runtime, "mention-send", 3, {"kind": "work.send", "workId": work["id"]})
            add_message(
                source,
                number=20,
                sender_id="robot",
                sender_name="Robot",
                text="@Alice\nEvidence-backed answer",
                send_time=1_020,
            )
            clock.value = 1_021
            command(runtime, "mention-readback", 3, {"kind": "runtime.tick", "wait": True})
            items = runtime.query({"kind": "work.list"})["items"]
            runtime.close()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "sent")

    def test_failed_webhook_releases_only_its_in_flight_echo_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            clock, source = FakeClock(), FakeMessages()
            webhook = FakeWebhook(response={"status": "failed"})
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(answer="Same answer text"),
                mcp=FakeMcp(),
                webhook=webhook,
                listener_overrides={
                    "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=failed-reservation"
                },
            )
            baseline(runtime)
            add_message(
                source,
                number=1,
                sender_id="alice",
                sender_name="Alice",
                account="alice",
                text="Original question?",
            )
            drive_to_retrieval(runtime, clock, prefix="failed-reservation")
            work = runtime.query({"kind": "work.list"})["items"][0]
            delivery = command(
                runtime,
                "send-failing-webhook",
                3,
                {"kind": "work.send", "workId": work["id"]},
            )

            add_message(
                source,
                number=20,
                sender_id="bob",
                sender_name="Bob",
                text="Same answer text",
                send_time=1_020,
            )
            clock.value = 1_021
            polled = runtime._poll_messages(listener_id="listener", force=True)
            with runtime.store.lock:
                bob_inbox = runtime.store.connection.execute(
                    """SELECT count(*) FROM reply_inbox
                       WHERE message_id='20' AND json_extract(payload_json,'$.senderId')='bob'"""
                ).fetchone()[0]
            runtime.close()

        self.assertEqual(delivery["status"], "failed")
        self.assertEqual(polled, 1)
        self.assertEqual(int(bob_inbox), 1)

    def test_message_held_during_webhook_delivery_is_replayed_after_failure(self):
        class FailingReentrantWebhook(FakeWebhook):
            runtime = None
            source = None
            clock = None
            polled_during_delivery = None

            def send(self, **kwargs):
                self.calls.append(kwargs)
                add_message(
                    self.source,
                    number=20,
                    sender_id="bob",
                    sender_name="Bob",
                    text=kwargs["text"],
                    send_time=1_020,
                )
                self.clock.value = 1_021
                self.polled_during_delivery = self.runtime._poll_messages(
                    listener_id="listener", force=True
                )
                return {"status": "failed"}

        with tempfile.TemporaryDirectory() as directory:
            clock, source = FakeClock(), FakeMessages()
            webhook = FailingReentrantWebhook()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(answer="Same in-flight text"),
                mcp=FakeMcp(),
                webhook=webhook,
                listener_overrides={
                    "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=failed-in-flight-reservation"
                },
            )
            webhook.runtime = runtime
            webhook.source = source
            webhook.clock = clock
            baseline(runtime)
            add_message(
                source,
                number=1,
                sender_id="alice",
                sender_name="Alice",
                account="alice",
                text="Original question?",
            )
            drive_to_retrieval(runtime, clock, prefix="failed-in-flight-reservation")
            work = runtime.query({"kind": "work.list"})["items"][0]
            delivery = command(
                runtime,
                "send-failing-in-flight-webhook",
                3,
                {"kind": "work.send", "workId": work["id"]},
            )

            replayed = runtime._poll_messages(listener_id="listener", force=True)
            with runtime.store.lock:
                bob_inbox = runtime.store.connection.execute(
                    """SELECT count(*) FROM reply_inbox
                       WHERE message_id='20' AND json_extract(payload_json,'$.senderId')='bob'"""
                ).fetchone()[0]
            runtime.close()

        self.assertEqual(delivery["status"], "failed")
        self.assertEqual(webhook.polled_during_delivery, 0)
        self.assertEqual(replayed, 1)
        self.assertEqual(int(bob_inbox), 1)

    def test_history_and_pending_pagination_are_isolated_and_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            clock, source = FakeClock(), FakeMessages()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=FakeMcp(),
            )
            baseline(runtime)
            add_message(source, number=1, sender_id="chat-a", text="chat: acknowledgement")
            add_message(source, number=2, sender_id="chat-b", text="chat: another acknowledgement")
            add_message(source, number=3, sender_id="question-a", text="How do I fix A?")
            add_message(source, number=4, sender_id="question-b", text="How do I fix B?")
            drive_to_retrieval(runtime, clock, prefix="pagination")
            pending_one = runtime.query({"kind": "work.list", "bucket": "pending", "limit": 1, "offset": 0})
            pending_two = runtime.query({"kind": "work.list", "bucket": "pending", "limit": 1, "offset": 1})
            history_one = runtime.query({"kind": "work.list", "bucket": "history", "limit": 1, "offset": 0})
            history_two = runtime.query({"kind": "work.list", "bucket": "history", "limit": 1, "offset": 1})
            runtime.close()

        self.assertEqual(pending_one["total"], 2)
        self.assertTrue(pending_one["hasMore"])
        self.assertEqual(history_one["total"], 2)
        self.assertTrue(history_one["hasMore"])
        self.assertNotEqual(pending_one["items"][0]["id"], pending_two["items"][0]["id"])
        self.assertNotEqual(history_one["items"][0]["id"], history_two["items"][0]["id"])
        self.assertTrue(all(page["items"][0]["status"] == "pending" for page in (pending_one, pending_two)))
        self.assertTrue(
            all(page["items"][0]["status"] == "ignored_non_question" for page in (history_one, history_two))
        )

    def test_supplement_after_collection_starts_a_new_work_item(self):
        class SupplementModel(ScriptedModel):
            def classify(self, *, messages, groupContext, question=None):
                return {"labels": ["supplement"] if question is not None else ["question"]}

        with tempfile.TemporaryDirectory() as directory:
            clock, source, mcp = FakeClock(), FakeMessages(), FakeMcp()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=SupplementModel(),
                mcp=mcp,
            )
            baseline(runtime)
            add_message(source, number=1, sender_id="alice", text="Initial question?", account="alice")
            clock.value = 1_005
            command(runtime, "initial-collect", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 1_007
            command(runtime, "initial-classify", 3, {"kind": "runtime.tick", "wait": True})

            add_message(source, number=8, sender_id="alice", text="Additional constraint", account="alice", send_time=1_008)
            # The prior classification poll advanced this listener's next poll to 1_009.
            clock.value = 1_010
            command(runtime, "supplement-collect", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 1_012
            command(runtime, "supplement-classify", 3, {"kind": "runtime.tick", "wait": True})
            separated = runtime.query({"kind": "work.list"})["items"]
            clock.value = 1_017
            command(runtime, "original-deadline", 3, {"kind": "runtime.tick", "wait": True})
            after = runtime.query({"kind": "work.list"})["items"]
            runtime.close()

        self.assertEqual(len(separated), 2)
        by_question = {item["question"]: item for item in separated}
        self.assertEqual(by_question["Initial question?"]["humanWaitDueAt"], "1970-01-01T00:16:57Z")
        self.assertEqual(by_question["Additional constraint"]["humanWaitDueAt"], "1970-01-01T00:17:02Z")
        after_by_question = {item["question"]: item for item in after}
        self.assertEqual(after_by_question["Initial question?"]["status"], "pending")
        self.assertEqual(after_by_question["Additional constraint"]["status"], "waiting_for_human_reply")
        self.assertEqual(len(mcp.calls), 1)

    def test_supplement_already_in_inbox_cannot_race_the_merge_deadline(self):
        class RacingSupplementModel(ScriptedModel):
            def __init__(self):
                super().__init__()
                self.supplement_started = threading.Event()
                self.release_supplement = threading.Event()
                self.due_started = threading.Event()
                self.release_due = threading.Event()
                self.block_supplement_once = True
                self.block_due = False

            def classify(self, *, messages, groupContext, question=None):
                text = str(messages[-1].get("text") or "")
                if question is not None and text == "Important supplement":
                    if self.block_supplement_once:
                        self.block_supplement_once = False
                        self.supplement_started.set()
                        self.release_supplement.wait(5)
                    return {"labels": ["supplement"]}
                if question is None and self.block_due and text == "Initial question?":
                    self.due_started.set()
                    self.release_due.wait(5)
                return {"labels": ["question"]}

        with tempfile.TemporaryDirectory() as directory:
            clock, source = FakeClock(), FakeMessages()
            model = RacingSupplementModel()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=model,
                mcp=FakeMcp(),
            )
            baseline(runtime)
            add_message(source, number=1, sender_id="alice", text="Initial question?")
            clock.value = 1_002
            command(runtime, "race-initial", 3, {"kind": "runtime.tick", "wait": True})

            add_message(
                source,
                number=2,
                sender_id="alice",
                text="Important supplement",
                send_time=1_003,
            )
            clock.value = 1_003
            self.assertEqual(
                runtime._poll_messages(listener_id="listener", force=True), 1
            )
            clock.value = 1_004

            assignment_thread = threading.Thread(target=runtime._assign_inbox)
            assignment_thread.start()
            self.assertTrue(model.supplement_started.wait(2))

            model.block_due = True
            due_finished = threading.Event()

            def classify_due():
                runtime._classify_due()
                due_finished.set()

            due_thread = threading.Thread(target=classify_due)
            due_thread.start()
            due_entered_before_release = model.due_started.wait(0.25)
            model.release_due.set()
            if due_entered_before_release:
                self.assertTrue(due_finished.wait(2))

            model.release_supplement.set()
            assignment_thread.join(2)
            due_thread.join(2)
            self.assertFalse(assignment_thread.is_alive())
            self.assertFalse(due_thread.is_alive())
            runtime._assign_inbox()
            items = runtime.query({"kind": "work.list"})["items"]
            runtime.close()

        self.assertEqual(len(items), 1)
        self.assertEqual(
            items[0]["question"], "Initial question?\nImportant supplement"
        )
        self.assertEqual(items[0]["status"], "collecting")

    def test_overlap_poll_deduplicates_the_same_composite_message(self):
        class OverlapMessages(FakeMessages):
            def read(self, listener, cursor):
                return list(self.rows)

        with tempfile.TemporaryDirectory() as directory:
            clock, source = FakeClock(), OverlapMessages()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=FakeMcp(),
            )
            baseline(runtime)
            add_message(source, number=1, sender_id="alice", text="One unique question?")
            clock.value = 1_005
            command(runtime, "overlap-first", 3, {"kind": "runtime.tick", "wait": True})
            before = runtime.query({"kind": "work.list"})["items"][0]
            clock.value = 1_007
            command(runtime, "overlap-second", 3, {"kind": "runtime.tick", "wait": True})
            items = runtime.query({"kind": "work.list"})["items"]
            runtime.close()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["version"], before["version"])

    def test_replayed_stable_message_id_updates_collecting_work_without_resetting_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            clock, source = FakeClock(), FakeMessages()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=FakeMcp(),
            )
            baseline(runtime)
            source.rows.append(
                {
                    "cursor": [1_001, 1, 42, 88],
                    "messageId": "42",
                    "serverId": "88",
                    "sequence": 1,
                    "sendTime": 1_001,
                    "groupId": "room",
                    "senderId": "alice",
                    "senderName": "Alice",
                    "account": "alice",
                    "mobile": "",
                    "contentType": "text",
                    "text": "Original question?",
                }
            )
            clock.value = 1_005
            command(runtime, "stable-first", 3, {"kind": "runtime.tick", "wait": True})
            before = runtime.query({"kind": "work.list"})["items"][0]

            source.rows.append(
                {
                    "cursor": [1_001, 2, 42, 88],
                    "messageId": "42",
                    "serverId": "88",
                    "sequence": 2,
                    "sendTime": 1_001,
                    "groupId": "room",
                    "senderId": "alice",
                    "senderName": "Alice",
                    "account": "alice",
                    "mobile": "",
                    "contentType": "text",
                    "text": "Edited question?",
                }
            )
            clock.value = 1_007
            command(runtime, "stable-replay", 3, {"kind": "runtime.tick", "wait": True})
            items = runtime.query({"kind": "work.list"})["items"]
            runtime.close()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["question"], "Edited question?")
        self.assertEqual(items[0]["mergeDueAt"], before["mergeDueAt"])
        self.assertEqual(items[0]["detectedAt"], before["detectedAt"])

    def test_image_context_reaches_planning_and_review_and_public_timing_is_exposed(self):
        class ImageAwareModel(ScriptedModel):
            def __init__(self):
                super().__init__()
                self.planning_images = None
                self.review_images = None

            def plan_tools(self, **kwargs):
                self.planning_images = kwargs.get("images")
                return super().plan_tools(**kwargs)

            def review(self, **kwargs):
                self.review_images = kwargs.get("images")
                return super().review(**kwargs)

        with tempfile.TemporaryDirectory() as directory:
            clock, source, model = FakeClock(), FakeMessages(), ImageAwareModel()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=model,
                mcp=FakeMcp({}),
            )
            baseline(runtime)
            image = {"base64": "aA==", "mimeType": "image/png"}
            missing_image = {
                "filename": "missing.png",
                "errorCode": "IMAGE_FILE_MISSING",
            }
            source.rows.append(
                {
                    "cursor": [1_001, 1, 1, 1],
                    "messageId": "image-1",
                    "serverId": "1",
                    "sequence": 1,
                    "sendTime": 1_001,
                    "groupId": "room",
                    "senderId": "alice",
                    "senderName": "Alice",
                    "account": "alice",
                    "mobile": "",
                    "contentType": "image",
                    "text": "Why are old records shown?",
                    "images": [image, missing_image],
                }
            )
            clock.value = 1_005
            command(runtime, "image-collect", 3, {"kind": "runtime.tick", "wait": True})
            collecting = runtime.query({"kind": "work.list"})["items"][0]
            clock.value = 1_007
            command(runtime, "image-classify", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 1_017
            command(runtime, "image-retrieve", 3, {"kind": "runtime.tick", "wait": True})
            finished = runtime.query({"kind": "work.detail", "workId": collecting["id"]})["item"]
            runtime.close()

        self.assertEqual(collecting["detectedAt"], "1970-01-01T00:16:45Z")
        self.assertEqual(collecting["sourceDelaySeconds"], 4.0)
        self.assertEqual(collecting["mergeDueAt"], "1970-01-01T00:16:47Z")
        self.assertIsNone(collecting["humanWaitDueAt"])
        self.assertEqual(collecting["imageCount"], 2)
        self.assertEqual(collecting["imageAvailableCount"], 1)
        self.assertEqual(collecting["imageUnavailableCount"], 1)
        self.assertEqual(collecting["imageStatus"], "partial")
        self.assertEqual(collecting["duplicateCount"], 0)
        self.assertEqual(model.planning_images, [image])
        self.assertIsNone(model.review_images)
        self.assertEqual(finished["imageCount"], 2)
        self.assertEqual(finished["imageAvailableCount"], 1)
        self.assertEqual(finished["imageUnavailableCount"], 1)
        self.assertEqual(finished["imageStatus"], "partial")
        self.assertEqual(finished["status"], "skipped_no_evidence")

    def test_image_message_without_attachment_ends_explicitly_without_mcp_or_webhook(self):
        with tempfile.TemporaryDirectory() as directory:
            clock, source, mcp, webhook = FakeClock(), FakeMessages(), FakeMcp(), FakeWebhook()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=mcp,
                webhook=webhook,
                listener_overrides={
                    "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=missing-image"
                },
            )
            baseline(runtime)
            source.rows.append(
                {
                    "cursor": [1_001, 1, 1, 1],
                    "messageId": "missing-image",
                    "serverId": "1",
                    "sequence": 1,
                    "sendTime": 1_001,
                    "groupId": "room",
                    "senderId": "alice",
                    "senderName": "Alice",
                    "account": "alice",
                    "mobile": "",
                    "contentType": "image",
                    "text": "[图片]",
                    "images": [{"errorCode": "IMAGE_UNREADABLE"}],
                }
            )
            clock.value = 1_005
            command(runtime, "missing-image", 3, {"kind": "runtime.tick", "wait": True})
            work_id = runtime.query({"kind": "work.list"})["items"][0]["id"]
            collecting = runtime.query({"kind": "work.detail", "workId": work_id})["item"]
            clock.value = 1_007
            command(runtime, "missing-image-due", 3, {"kind": "runtime.tick", "wait": True})
            item = runtime.query({"kind": "work.detail", "workId": work_id})["item"]
            runtime.close()

        self.assertEqual(collecting["status"], "collecting")
        self.assertEqual(collecting["mergeDueAt"], "1970-01-01T00:16:47Z")
        self.assertEqual(item["status"], "skipped_image_unavailable")
        self.assertEqual(item["error"]["code"], "IMAGE_UNREADABLE")
        self.assertEqual(item["error"]["stage"], "collecting")
        self.assertEqual(item["imageStatus"], "unavailable")
        self.assertEqual(mcp.calls, [])
        self.assertEqual(webhook.calls, [])

    def test_image_cache_is_rechecked_once_at_the_merge_deadline(self):
        class CacheAppearingMessages(FakeMessages):
            def __init__(self):
                super().__init__()
                self.refresh_calls = 0

            def refresh_images(self, listener, messages):
                self.refresh_calls += 1
                return [
                    {
                        **message,
                        "images": [{"base64": "aA==", "mimeType": "image/png"}],
                    }
                    for message in messages
                ]

        with tempfile.TemporaryDirectory() as directory:
            clock, source = FakeClock(), CacheAppearingMessages()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=FakeMcp(),
            )
            baseline(runtime)
            source.rows.append(
                {
                    "cursor": [1_001, 1, 1, 1],
                    "messageId": "late-image",
                    "serverId": "1",
                    "sequence": 1,
                    "sendTime": 1_001,
                    "groupId": "room",
                    "senderId": "alice",
                    "senderName": "Alice",
                    "contentType": "image",
                    "text": "[图片]",
                    "images": [{"errorCode": "IMAGE_FILE_MISSING"}],
                }
            )

            clock.value = 1_005
            command(runtime, "late-image-collect", 3, {"kind": "runtime.tick", "wait": True})
            collecting = runtime.query({"kind": "work.list"})["items"][0]
            calls_before_due = source.refresh_calls
            clock.value = 1_007
            command(runtime, "late-image-classify", 3, {"kind": "runtime.tick", "wait": True})
            waiting = runtime.query(
                {"kind": "work.detail", "workId": collecting["id"]}
            )["item"]
            runtime.close()

        self.assertEqual(calls_before_due, 0)
        self.assertEqual(source.refresh_calls, 1)
        self.assertEqual(waiting["status"], "waiting_for_human_reply")
        self.assertEqual(waiting["imageStatus"], "ready")
        self.assertEqual(waiting["imageCount"], 1)

    def test_pending_image_resolution_is_not_reported_as_a_failure_before_merge_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            clock, source = FakeClock(), FakeMessages()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=FakeMcp(),
            )
            baseline(runtime)
            source.rows.append(
                {
                    "cursor": [1_001, 1, 1, 1],
                    "messageId": "pending-image",
                    "serverId": "1",
                    "sequence": 1,
                    "sendTime": 1_001,
                    "groupId": "room",
                    "senderId": "alice",
                    "senderName": "Alice",
                    "contentType": "image",
                    "text": "[图片]",
                    "images": [{"errorCode": "IMAGE_RESOLUTION_PENDING"}],
                }
            )

            clock.value = 1_005
            command(runtime, "pending-image-collect", 3, {"kind": "runtime.tick", "wait": True})
            collecting = runtime.query({"kind": "work.list"})["items"][0]
            clock.value = 1_007
            command(runtime, "pending-image-due", 3, {"kind": "runtime.tick", "wait": True})
            waiting = runtime.query({"kind": "work.detail", "workId": collecting["id"]})["item"]
            clock.value = datetime.fromisoformat(
                waiting["imageWaitDueAt"].replace("Z", "+00:00")
            ).timestamp()
            command(runtime, "pending-image-timeout", 3, {"kind": "runtime.tick", "wait": True})
            finished = runtime.query({"kind": "work.detail", "workId": collecting["id"]})["item"]
            runtime.close()

        self.assertEqual(collecting["status"], "collecting")
        self.assertEqual(collecting["imageStatus"], "resolving")
        self.assertEqual(collecting["imageUnavailableCount"], 0)
        self.assertEqual(waiting["status"], "waiting_for_image")
        self.assertEqual(waiting["imageStatus"], "resolving")
        self.assertEqual(finished["status"], "skipped_image_unavailable")
        self.assertEqual(finished["error"]["code"], "IMAGE_FILE_MISSING")

    def test_image_refresh_cannot_terminate_before_a_pending_supplement_is_merged(self):
        class BlockingRefreshMessages(FakeMessages):
            def __init__(self):
                super().__init__()
                self.entered = threading.Event()
                self.release = threading.Event()

            def refresh_images(self, listener, messages):
                self.entered.set()
                self.release.wait(5)
                return messages

        class SupplementModel(ScriptedModel):
            def classify(self, *, messages, groupContext, question=None):
                return {"labels": ["supplement" if question is not None else "question"]}

        with tempfile.TemporaryDirectory() as directory:
            clock, source = FakeClock(), BlockingRefreshMessages()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=SupplementModel(),
                mcp=FakeMcp(),
            )
            baseline(runtime)
            source.rows.append(
                {
                    "cursor": [1_001, 1, 1, 1],
                    "messageId": "missing-first",
                    "serverId": "1",
                    "sequence": 1,
                    "sendTime": 1_001,
                    "groupId": "room",
                    "senderId": "alice",
                    "senderName": "Alice",
                    "contentType": "image",
                    "text": "[图片]",
                    "images": [{"errorCode": "IMAGE_FILE_MISSING"}],
                }
            )
            clock.value = 1_005
            command(runtime, "refresh-race-collect", 3, {"kind": "runtime.tick", "wait": True})
            work_id = runtime.query({"kind": "work.list"})["items"][0]["id"]

            clock.value = 1_007
            errors = []

            def classify_due():
                try:
                    command(runtime, "refresh-race-due", 3, {"kind": "runtime.tick", "wait": True})
                except BaseException as exc:
                    errors.append(exc)

            due_thread = threading.Thread(target=classify_due)
            due_thread.start()
            self.assertTrue(source.entered.wait(2))
            add_message(
                source,
                number=2,
                sender_id="alice",
                text="补充：订单号是 734402",
                send_time=1_006,
            )
            self.assertEqual(
                runtime._poll_messages(listener_id="listener", force=True), 1
            )
            source.release.set()
            due_thread.join(2)
            before_merge = runtime.query({"kind": "work.detail", "workId": work_id})["item"]
            command(runtime, "refresh-race-merge", 3, {"kind": "runtime.tick", "wait": True})
            after_merge = runtime.query({"kind": "work.detail", "workId": work_id})["item"]
            runtime.close()

        self.assertFalse(due_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(before_merge["status"], "collecting")
        self.assertEqual(after_merge["status"], "collecting")
        self.assertIn("734402", after_merge["question"])
        self.assertEqual(after_merge["mergeDueAt"], "1970-01-01T00:16:49Z")

    def test_image_supplement_is_refreshed_before_same_sender_classification(self):
        available_image = {"base64": "aA==", "mimeType": "image/png"}

        class RefreshingMessages(FakeMessages):
            def __init__(self):
                super().__init__()
                self.refresh_calls = 0

            def refresh_images(self, listener, messages):
                self.refresh_calls += 1
                return [{**message, "images": [available_image]} for message in messages]

        class ImageSupplementModel(ScriptedModel):
            def __init__(self):
                super().__init__()
                self.supplement_images = None

            def classify(self, *, messages, groupContext, question=None):
                if question is not None:
                    self.supplement_images = messages[-1].get("images")
                    return {"labels": ["supplement"]}
                return {"labels": ["question"]}

        with tempfile.TemporaryDirectory() as directory:
            clock, source, model = FakeClock(), RefreshingMessages(), ImageSupplementModel()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=model,
                mcp=FakeMcp(),
                listener_overrides={"sameSenderMergeSeconds": 120},
            )
            baseline(runtime)
            add_message(source, number=1, sender_id="alice", text="Initial question?")
            clock.value = 1_005
            command(runtime, "image-supplement-initial", 3, {"kind": "runtime.tick", "wait": True})
            source.rows.append(
                {
                    "cursor": [1_006, 2, 2, 1],
                    "messageId": "image-supplement",
                    "serverId": "1",
                    "sequence": 2,
                    "sendTime": 1_006,
                    "groupId": "room",
                    "senderId": "alice",
                    "senderName": "Alice",
                    "contentType": "image",
                    "text": "[图片]",
                    "images": [{"errorCode": "IMAGE_RESOLUTION_PENDING"}],
                }
            )
            clock.value = 1_007
            command(runtime, "image-supplement-merge", 3, {"kind": "runtime.tick", "wait": True})
            item = runtime.query({"kind": "work.list"})["items"][0]
            runtime.close()

        self.assertEqual(source.refresh_calls, 1)
        self.assertEqual(model.supplement_images, [available_image])
        self.assertEqual(item["status"], "collecting")
        self.assertEqual(item["imageStatus"], "ready")

    def test_higher_sequence_replay_wins_while_image_supplement_refresh_is_running(self):
        class BlockingRefreshMessages(FakeMessages):
            def __init__(self):
                super().__init__()
                self.calls = 0
                self.entered = threading.Event()
                self.release = threading.Event()

            def refresh_images(self, listener, messages):
                self.calls += 1
                if self.calls == 1:
                    self.entered.set()
                    self.release.wait(5)
                    filename = "old.png"
                else:
                    filename = "new.png"
                return [
                    {
                        **message,
                        "images": [
                            {
                                "base64": "aA==",
                                "mimeType": "image/png",
                                "filename": filename,
                            }
                        ],
                    }
                    for message in messages
                ]

        class ReplayAwareModel(ScriptedModel):
            def __init__(self):
                super().__init__()
                self.supplement_filename = ""

            def classify(self, *, messages, groupContext, question=None):
                if question is not None:
                    self.supplement_filename = str(
                        (messages[-1].get("images") or [{}])[0].get("filename") or ""
                    )
                    return {"labels": ["supplement"]}
                return {"labels": ["question"]}

        with tempfile.TemporaryDirectory() as directory:
            clock, source, model = FakeClock(), BlockingRefreshMessages(), ReplayAwareModel()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=model,
                mcp=FakeMcp(),
                listener_overrides={"sameSenderMergeSeconds": 120},
            )
            baseline(runtime)
            add_message(source, number=1, sender_id="alice", text="Initial question?")
            clock.value = 1_005
            command(runtime, "replay-image-initial", 3, {"kind": "runtime.tick", "wait": True})
            source.rows.append(
                {
                    "cursor": [1_006, 2, 2, 1],
                    "messageId": "replayed-image-supplement",
                    "serverId": "1",
                    "sequence": 2,
                    "sendTime": 1_006,
                    "groupId": "room",
                    "senderId": "alice",
                    "senderName": "Alice",
                    "contentType": "image",
                    "text": "[图片]",
                    "images": [{"errorCode": "IMAGE_RESOLUTION_PENDING"}],
                }
            )
            clock.value = 1_007
            errors = []

            def merge_supplement():
                try:
                    command(runtime, "replay-image-merge", 3, {"kind": "runtime.tick", "wait": True})
                except BaseException as exc:
                    errors.append(exc)

            merge_thread = threading.Thread(target=merge_supplement)
            merge_thread.start()
            self.assertTrue(source.entered.wait(2))
            source.rows.append(
                {
                    **source.rows[-1],
                    "cursor": [1_006, 3, 2, 1],
                    "sequence": 3,
                    "text": "[图片]（编辑后版本）",
                }
            )
            self.assertEqual(runtime._poll_messages(listener_id="listener", force=True), 0)
            source.release.set()
            merge_thread.join(3)
            item = runtime.query({"kind": "work.list"})["items"][0]
            runtime.close()

        self.assertFalse(merge_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertGreaterEqual(source.calls, 2)
        self.assertEqual(model.supplement_filename, "new.png")
        self.assertIn("编辑后版本", item["question"])

    def test_group_member_image_answer_is_refreshed_before_matching(self):
        available_image = {"base64": "aA==", "mimeType": "image/png"}

        class RefreshingMessages(FakeMessages):
            def __init__(self):
                super().__init__()
                self.refresh_calls = 0

            def refresh_images(self, listener, messages):
                self.refresh_calls += 1
                return [{**message, "images": [available_image]} for message in messages]

        class ImageAnswerModel(ScriptedModel):
            def __init__(self):
                super().__init__()
                self.answer_images = None

            def match_human_answers(self, *, message, groupContext, candidates):
                self.answer_images = message.get("images")
                return {
                    "matches": [
                        {"workId": candidates[0]["workId"], "labels": ["human_answer"]}
                    ]
                }

        with tempfile.TemporaryDirectory() as directory:
            clock, source, model = FakeClock(), RefreshingMessages(), ImageAnswerModel()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=model,
                mcp=FakeMcp(),
            )
            baseline(runtime)
            add_message(source, number=1, sender_id="alice", text="Original question?")
            clock.value = 1_005
            command(runtime, "image-answer-collect", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 1_007
            command(runtime, "image-answer-wait", 3, {"kind": "runtime.tick", "wait": True})
            source.rows.append(
                {
                    "cursor": [1_008, 2, 2, 1],
                    "messageId": "image-answer",
                    "serverId": "1",
                    "sequence": 2,
                    "sendTime": 1_008,
                    "groupId": "room",
                    "senderId": "bob",
                    "senderName": "Bob",
                    "contentType": "image",
                    "text": "[图片]",
                    "images": [{"errorCode": "IMAGE_RESOLUTION_PENDING"}],
                }
            )
            clock.value = 1_010
            command(runtime, "image-answer-match", 3, {"kind": "runtime.tick", "wait": True})
            items = runtime.query({"kind": "work.list"})["items"]
            runtime.close()

        self.assertEqual(source.refresh_calls, 1)
        self.assertEqual(model.answer_images, [available_image])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "answered_by_human")

    def test_missing_image_with_substantive_text_uses_the_full_text_workflow(self):
        class TextFallbackModel(ScriptedModel):
            def __init__(self):
                super().__init__()
                self.planning_images = None
                self.answer_images = None
                self.review_images = None

            def plan_tools(self, **kwargs):
                self.planning_images = kwargs.get("images")
                return super().plan_tools(**kwargs)

            def answer(self, **kwargs):
                self.answer_images = kwargs.get("images")
                return super().answer(**kwargs)

            def review(self, **kwargs):
                self.review_images = kwargs.get("images")
                return super().review(**kwargs)

        with tempfile.TemporaryDirectory() as directory:
            clock, source, model = FakeClock(), FakeMessages(), TextFallbackModel()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=model,
                mcp=FakeMcp(),
            )
            baseline(runtime)
            source.rows.append(
                {
                    "cursor": [1_001, 1, 1, 1],
                    "messageId": "missing-image-with-text",
                    "serverId": "1",
                    "sequence": 1,
                    "sendTime": 1_001,
                    "groupId": "room",
                    "senderId": "alice",
                    "senderName": "Alice",
                    "account": "alice",
                    "mobile": "",
                    "contentType": "image",
                    "text": (
                        "企业微信截图_17855776775914.png\n"
                        "[图片]南阳畅联凭证生成失败，麻烦看一下\n"
                        "[二进制内容 2 字节]"
                    ),
                    "images": [
                        {
                            "filename": "企业微信截图_17855776775914.png",
                            "errorCode": "IMAGE_FILE_MISSING",
                        }
                    ],
                }
            )

            clock.value = 1_005
            command(runtime, "missing-text-collect", 3, {"kind": "runtime.tick", "wait": True})
            collecting = runtime.query({"kind": "work.list"})["items"][0]
            clock.value = 1_007
            command(runtime, "missing-text-classify", 3, {"kind": "runtime.tick", "wait": True})
            waiting = runtime.query({"kind": "work.list"})["items"][0]
            clock.value = 1_017
            command(runtime, "missing-text-retrieve", 3, {"kind": "runtime.tick", "wait": True})
            still_waiting = runtime.query({"kind": "work.list"})["items"][0]
            clock.value = 1_187
            command(runtime, "missing-text-timeout", 3, {"kind": "runtime.tick", "wait": True})
            finished = runtime.query({"kind": "work.detail", "workId": collecting["id"]})["item"]
            runtime.close()

        self.assertEqual(collecting["status"], "collecting")
        self.assertEqual(waiting["status"], "waiting_for_human_reply")
        self.assertEqual(still_waiting["status"], "waiting_for_human_reply")
        self.assertEqual(still_waiting["imageStatus"], "resolving")
        self.assertEqual(finished["status"], "pending")
        self.assertEqual(finished["question"], "南阳畅联凭证生成失败，麻烦看一下")
        self.assertEqual(finished["imageStatus"], "unavailable")
        self.assertEqual(model.planning_images, [])
        self.assertEqual(model.answer_images, [])
        self.assertEqual(model.review_images, [])

    def test_image_evicted_during_collection_falls_back_to_substantive_text(self):
        class ImageCaptureModel(ScriptedModel):
            def __init__(self):
                super().__init__()
                self.planning_images = None

            def plan_tools(self, **kwargs):
                self.planning_images = kwargs.get("images")
                return super().plan_tools(**kwargs)

        with tempfile.TemporaryDirectory() as directory:
            clock, source, model = FakeClock(), FakeMessages(), ImageCaptureModel()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=model,
                mcp=FakeMcp(),
            )
            baseline(runtime)
            source.rows.append(
                {
                    "cursor": [1_001, 1, 1, 1],
                    "messageId": "evicted-image",
                    "serverId": "1",
                    "sequence": 1,
                    "sendTime": 1_001,
                    "groupId": "room",
                    "senderId": "alice",
                    "senderName": "Alice",
                    "contentType": "image",
                    "text": "订单已经删除，为什么列表里还会出现？",
                    "images": [
                        {
                            "localPath": "Z:/wecom-cache-evicted/question.png",
                            "filename": "question.png",
                            "mimeType": "image/png",
                        }
                    ],
                }
            )

            drive_to_retrieval(runtime, clock, prefix="evicted-image")
            waiting = runtime.query({"kind": "work.list"})["items"][0]
            clock.value = 1_187
            command(runtime, "evicted-image-timeout", 3, {"kind": "runtime.tick", "wait": True})
            item = runtime.query(
                {"kind": "work.detail", "workId": waiting["id"]}
            )["item"]
            runtime.close()

        self.assertEqual(waiting["status"], "waiting_for_human_reply")
        self.assertEqual(waiting["imageStatus"], "resolving")
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["imageStatus"], "unavailable")
        self.assertEqual(item["imageUnavailableCount"], 1)
        self.assertEqual(model.planning_images, [])

    def test_image_evicted_during_human_wait_is_rechecked_before_retrieval(self):
        class ImageCaptureModel(ScriptedModel):
            def __init__(self):
                super().__init__()
                self.planning_images = None

            def plan_tools(self, **kwargs):
                self.planning_images = kwargs.get("images")
                return super().plan_tools(**kwargs)

        with tempfile.TemporaryDirectory() as directory:
            clock, source, model, mcp = FakeClock(), FakeMessages(), ImageCaptureModel(), FakeMcp()
            image_path = Path(directory) / "question.png"
            image_path.write_bytes(b"cached image")
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=model,
                mcp=mcp,
            )
            baseline(runtime)
            source.rows.append(
                {
                    "cursor": [1_001, 1, 1, 1],
                    "messageId": "evicted-during-human-wait",
                    "serverId": "1",
                    "sequence": 1,
                    "sendTime": 1_001,
                    "groupId": "room",
                    "senderId": "alice",
                    "senderName": "Alice",
                    "contentType": "image",
                    "text": "[图片]",
                    "images": [
                        {
                            "localPath": str(image_path),
                            "filename": image_path.name,
                            "mimeType": "image/png",
                        }
                    ],
                }
            )

            clock.value = 1_005
            command(runtime, "wait-eviction-collect", 3, {"kind": "runtime.tick", "wait": True})
            work_id = runtime.query({"kind": "work.list"})["items"][0]["id"]
            clock.value = 1_007
            command(runtime, "wait-eviction-classify", 3, {"kind": "runtime.tick", "wait": True})
            waiting = runtime.query({"kind": "work.detail", "workId": work_id})["item"]
            image_path.unlink()
            clock.value = 1_017
            command(runtime, "wait-eviction-retrieve", 3, {"kind": "runtime.tick", "wait": True})
            finished = runtime.query({"kind": "work.detail", "workId": work_id})["item"]
            runtime.close()

        self.assertEqual(waiting["status"], "waiting_for_human_reply")
        self.assertEqual(waiting["imageStatus"], "ready")
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["error"]["code"], "IMAGE_FILE_MISSING")
        self.assertEqual(finished["error"]["stage"], "retrieving")
        self.assertEqual(finished["imageStatus"], "unavailable")
        self.assertEqual(model.planning_images, None)
        self.assertEqual(mcp.calls, [])

    def test_supplements_cannot_grow_a_work_beyond_the_image_count_limit(self):
        class SupplementModel(ScriptedModel):
            def classify(self, *, messages, groupContext, question=None):
                return {
                    "labels": ["supplement" if question is not None else "question"]
                }

        image = {"base64": "aA==", "mimeType": "image/png"}
        with tempfile.TemporaryDirectory() as directory:
            clock, source, mcp = FakeClock(), FakeMessages(), FakeMcp()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=SupplementModel(),
                mcp=mcp,
                listener_overrides={"sameSenderMergeSeconds": 120},
            )
            baseline(runtime)
            add_message(source, number=1, sender_id="alice", text="Initial question?")
            source.rows[-1]["images"] = [image] * 4
            clock.value = 1_002
            command(runtime, "collect-image-question", 3, {"kind": "runtime.tick", "wait": True})

            add_message(
                source,
                number=2,
                sender_id="alice",
                text="Important image supplement",
                send_time=1_003,
            )
            source.rows[-1]["images"] = [image] * 5
            clock.value = 1_004
            command(runtime, "collect-image-supplement", 3, {"kind": "runtime.tick", "wait": True})
            item = runtime.query({"kind": "work.list"})["items"][0]
            runtime.close()

        self.assertEqual(item["status"], "skipped_image_unavailable")
        self.assertEqual(item["error"]["code"], "IMAGE_TOO_LARGE")
        self.assertEqual(item["imageCount"], 9)
        self.assertEqual(mcp.calls, [])

    def test_work_detail_distinguishes_collection_and_retrieval_failures(self):
        class StageAwareModel(ScriptedModel):
            def classify(self, *, messages, groupContext, question=None):
                if question is None and messages[-1].get("senderId") == "alice":
                    raise RuntimeError("classification exploded")
                return super().classify(
                    messages=messages,
                    groupContext=groupContext,
                    question=question,
                )

        class InterruptedMcp(FakeMcp):
            def call(self, **kwargs):
                raise RuntimeProtocolError(
                    "MCP_SESSION_INTERRUPTED", "MCP session closed during retrieval"
                )

        with tempfile.TemporaryDirectory() as directory:
            clock, source = FakeClock(), FakeMessages()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=StageAwareModel(),
                mcp=InterruptedMcp(),
            )
            baseline(runtime)
            add_message(source, number=1, sender_id="alice", text="Fails early?")
            add_message(source, number=2, sender_id="bob", text="Fails during MCP?")
            clock.value = 1_005
            command(runtime, "stage-collect", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 1_007
            command(runtime, "stage-classify", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 1_017
            command(runtime, "stage-retrieve", 3, {"kind": "runtime.tick", "wait": True})
            summaries = runtime.query({"kind": "work.list"})["items"]
            details = {
                summary["senderId"]: runtime.query(
                    {"kind": "work.detail", "workId": summary["id"]}
                )["item"]
                for summary in summaries
            }
            runtime.close()

        self.assertEqual(details["alice"]["status"], "failed")
        self.assertEqual(details["alice"]["error"]["code"], "CLASSIFICATION_FAILED")
        self.assertEqual(details["alice"]["error"]["stage"], "collecting")
        self.assertEqual(details["alice"]["answer"], "")
        self.assertEqual(details["bob"]["status"], "failed")
        self.assertEqual(details["bob"]["error"]["code"], "MCP_SESSION_INTERRUPTED")
        self.assertEqual(details["bob"]["error"]["stage"], "retrieving")
        self.assertEqual(details["bob"]["answer"], "")

    def test_shutdown_preserves_the_interrupted_retrieval_stage(self):
        class BlockingMcp(FakeMcp):
            def __init__(self):
                super().__init__()
                self.entered = threading.Event()
                self.release = threading.Event()
                self.returned = threading.Event()

            def call(self, **kwargs):
                self.entered.set()
                self.release.wait(5)
                self.returned.set()
                raise RuntimeProtocolError(
                    "MCP_SESSION_INTERRUPTED", "MCP adapter closed during shutdown"
                )

            def close(self):
                self.release.set()
                self.returned.wait(2)

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "reply-runtime.sqlite3"
            clock, source, mcp = FakeClock(), FakeMessages(), BlockingMcp()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=mcp,
            )
            baseline(runtime)
            add_message(source, number=1, sender_id="alice", text="Shutdown test?")
            clock.value = 1_005
            command(runtime, "shutdown-collect", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 1_007
            command(runtime, "shutdown-classify", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 1_017
            command(runtime, "shutdown-retrieve", 3, {"kind": "runtime.tick", "wait": False})
            self.assertTrue(mcp.entered.wait(2))
            work_id = runtime.query({"kind": "work.list"})["items"][0]["id"]
            runtime.close()

            reopened = ReplyRuntime(database, clock=clock, autostart=False)
            detail = reopened.query({"kind": "work.detail", "workId": work_id})["item"]
            reopened.close()

        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["error"]["code"], "RUNTIME_SHUTDOWN")
        self.assertEqual(detail["error"]["stage"], "retrieving")
        self.assertEqual(detail["answer"], "")

    def test_model_vision_rejection_keeps_the_specific_terminal_error(self):
        class NoVisionModel(ScriptedModel):
            def classify(self, **kwargs):
                raise RuntimeProtocolError(
                    "MODEL_VISION_UNSUPPORTED", "configured model cannot process images"
                )

        with tempfile.TemporaryDirectory() as directory:
            clock, source = FakeClock(), FakeMessages()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=NoVisionModel(),
                mcp=FakeMcp(),
            )
            baseline(runtime)
            source.rows.append(
                {
                    "cursor": [1_001, 1, 1, 1], "messageId": "vision", "serverId": "1",
                    "sequence": 1, "sendTime": 1_001, "groupId": "room",
                    "senderId": "alice", "senderName": "Alice", "account": "alice",
                    "contentType": "image", "text": "What is wrong?",
                    "images": [{"base64": "aA==", "mimeType": "image/png"}],
                }
            )
            clock.value = 1_005
            command(runtime, "vision-collect", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 1_007
            command(runtime, "vision-classify", 3, {"kind": "runtime.tick", "wait": True})
            item = runtime.query({"kind": "work.list"})["items"][0]
            runtime.close()

        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["error"]["code"], "MODEL_VISION_UNSUPPORTED")
        self.assertEqual(item["error"]["stage"], "collecting")
        self.assertEqual(item["imageStatus"], "unsupported")

    def test_bad_image_with_a_human_answer_candidate_terminates_instead_of_retrying(self):
        class CandidateAwareNoVision(ScriptedModel):
            def classify(self, *, messages, groupContext, question=None):
                if question is not None and messages[-1].get("contentType") == "image":
                    raise RuntimeProtocolError("IMAGE_UNREADABLE", "image could not be decoded")
                return super().classify(
                    messages=messages, groupContext=groupContext, question=question
                )

        with tempfile.TemporaryDirectory() as directory:
            clock, source = FakeClock(), FakeMessages()
            runtime, _ = configure_runtime(
                directory, clock=clock, messages=source,
                model=CandidateAwareNoVision(), mcp=FakeMcp(),
            )
            baseline(runtime)
            add_message(source, number=1, sender_id="alice", text="Original question?")
            clock.value = 1_005
            command(runtime, "candidate-collect", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 1_007
            command(runtime, "candidate-classify", 3, {"kind": "runtime.tick", "wait": True})
            source.rows.append(
                {
                    "cursor": [1_008, 2, 2, 1], "messageId": "image-2", "serverId": "1",
                    "sequence": 2, "sendTime": 1_008, "groupId": "room",
                    "senderId": "bob", "senderName": "Bob", "contentType": "image",
                    "text": "Can this help?", "images": [{"localPath": "C:/fixtures/bad.png"}],
                }
            )
            clock.value = 1_010
            command(runtime, "candidate-image", 3, {"kind": "runtime.tick", "wait": True})
            items = {item["senderId"]: item for item in runtime.query({"kind": "work.list"})["items"]}
            runtime.close()

        self.assertEqual(items["alice"]["status"], "waiting_for_human_reply")
        self.assertEqual(items["bob"]["status"], "skipped_image_unavailable")
        self.assertEqual(items["bob"]["error"]["code"], "IMAGE_UNREADABLE")

    def test_legacy_duplicate_history_is_folded_idempotently_and_prefers_sent_then_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "reply-runtime.sqlite3"
            clock, source, webhook = FakeClock(), FakeMessages(), FakeWebhook()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=FakeMcp(),
                webhook=webhook,
                listener_overrides={
                    "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=legacy-fold"
                },
            )
            baseline(runtime)
            add_message(source, number=1, sender_id="alice", text="One question?", account="alice")
            drive_to_retrieval(runtime, clock, prefix="legacy-fold")
            original = runtime.query({"kind": "work.list"})["items"][0]
            command(runtime, "legacy-send", 3, {"kind": "work.send", "workId": original["id"]})
            runtime.close()

            connection = sqlite3.connect(database)
            connection.execute("DROP INDEX reply_inbox_stable_identity")
            connection.execute(
                """INSERT INTO reply_work_items(
                       id,listener_id,group_id,sender_id,sender_name,sender_account,sender_mobile,
                       status,question,messages_json,group_context_json,evidence_json,answer,
                       review_json,error_json,pending_reason,generation,listener_generation,
                       merge_due_at,human_wait_due_at,human_answered_at,human_answer_message_json,
                       created_at,updated_at,completed_at,duplicate_of_work_id)
                   SELECT 'duplicate-work',listener_id,group_id,sender_id,sender_name,sender_account,
                          sender_mobile,'skipped_review_failed',question,messages_json,
                          group_context_json,evidence_json,answer,review_json,error_json,
                          'duplicate replay',generation,listener_generation,merge_due_at,
                          human_wait_due_at,human_answered_at,human_answer_message_json,
                          created_at,updated_at+100,completed_at,NULL
                   FROM reply_work_items WHERE id=?""",
                (original["id"],),
            )
            connection.execute(
                """INSERT INTO reply_inbox(
                       listener_id,group_id,message_id,server_id,sequence,send_time,payload_json,
                       assigned_work_id,retry_after,classification_attempts,classification_error_json,
                       received_at,duplicate_of_inbox_id)
                   SELECT listener_id,group_id,message_id,server_id,sequence+100,send_time,payload_json,
                          'duplicate-work',retry_after,classification_attempts,classification_error_json,
                          received_at+100,NULL
                   FROM reply_inbox WHERE assigned_work_id=?""",
                (original["id"],),
            )
            connection.commit()
            connection.close()

            first_reopen = ReplyRuntime(database, autostart=False)
            first_items = first_reopen.query({"kind": "work.list"})["items"]
            with first_reopen.store.transaction() as db:
                db.execute(
                    "UPDATE reply_work_items SET status='sent',updated_at=9999 WHERE id='duplicate-work'"
                )
            first_reopen.close()

            second_reopen = ReplyRuntime(database, autostart=False)
            second_items = second_reopen.query({"kind": "work.list"})["items"]
            second_reopen.close()

        self.assertEqual([item["id"] for item in first_items], [original["id"]])
        self.assertEqual(first_items[0]["duplicateCount"], 1)
        self.assertEqual([item["id"] for item in second_items], ["duplicate-work"])
        self.assertEqual(second_items[0]["duplicateCount"], 1)

    def test_overlapping_duplicate_message_groups_fold_one_work_component(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "reply-runtime.sqlite3"
            runtime, _ = configure_runtime(
                directory,
                clock=FakeClock(),
                messages=FakeMessages(),
                model=ScriptedModel(),
                mcp=FakeMcp(),
            )
            runtime.close()

            connection = sqlite3.connect(database)
            connection.execute("DROP INDEX reply_inbox_stable_identity")
            connection.executemany(
                """INSERT INTO reply_work_items(
                       id,listener_id,group_id,sender_id,status,question,
                       listener_generation,created_at,updated_at,completed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                [
                    ("work-root", "listener", "room", "alice", "sent", "root", 1, 1, 10, 10),
                    ("work-bridge", "listener", "room", "alice", "failed", "bridge", 1, 2, 20, 20),
                    ("work-tail", "listener", "room", "alice", "failed", "tail", 1, 3, 30, 30),
                ],
            )
            payload = json.dumps({"groupId": "room", "senderId": "alice"})
            connection.executemany(
                """INSERT INTO reply_inbox(
                       listener_id,group_id,message_id,server_id,sequence,send_time,
                       payload_json,assigned_work_id,received_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                [
                    ("listener", "room", "message-a", "server", 1, 101, payload, "work-root", 101),
                    ("listener", "room", "message-a", "server", 2, 102, payload, "work-bridge", 102),
                    ("listener", "room", "message-b", "server", 1, 201, payload, "work-bridge", 201),
                    ("listener", "room", "message-b", "server", 2, 202, payload, "work-tail", 202),
                ],
            )
            connection.commit()
            connection.close()

            first = ReplyRuntime(database, autostart=False)
            first_items = first.query({"kind": "work.list"})["items"]
            links = {
                str(row["id"]): row["duplicate_of_work_id"]
                for row in first.store.connection.execute(
                    "SELECT id,duplicate_of_work_id FROM reply_work_items"
                ).fetchall()
            }
            first.close()

            second = ReplyRuntime(database, autostart=False)
            second_items = second.query({"kind": "work.list"})["items"]
            second.close()

        self.assertEqual([item["id"] for item in first_items], ["work-root"])
        self.assertEqual(first_items[0]["duplicateCount"], 2)
        self.assertEqual(links["work-root"], None)
        self.assertEqual(links["work-bridge"], "work-root")
        self.assertEqual(links["work-tail"], "work-root")
        self.assertEqual([item["id"] for item in second_items], ["work-root"])
        self.assertEqual(second_items[0]["duplicateCount"], 2)

    def test_explicit_webhook_rejection_becomes_delivery_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            clock, source = FakeClock(), FakeMessages()
            webhook = FakeWebhook({"status": "failed", "errorCode": 40001})
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=FakeMcp(),
                webhook=webhook,
                listener_overrides={
                    "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=explicit-failure"
                },
            )
            baseline(runtime)
            add_message(source, number=1, sender_id="alice", text="Question?", account="alice")
            drive_to_retrieval(runtime, clock, prefix="explicit-failure")
            pending = runtime.query({"kind": "work.list"})["items"][0]
            result = command(runtime, "send-failed", 3, {"kind": "work.send", "workId": pending["id"]})
            detail = runtime.query({"kind": "work.detail", "workId": pending["id"]})["item"]
            runtime.close()

        self.assertEqual(result["status"], "failed")
        self.assertEqual(detail["status"], "delivery_failed")

    def test_shared_mcp_budget_allows_six_hundred_seconds_and_bounds_followup_call(self):
        class TwoCallModel(ScriptedModel):
            def plan_tools(self, **kwargs):
                return [
                    {"serverId": "kb", "toolName": "search", "arguments": {"part": 1}},
                    {"serverId": "kb", "toolName": "search", "arguments": {"part": 2}},
                ]

        with tempfile.TemporaryDirectory() as directory:
            clock, source, mcp = FakeClock(), FakeMessages(), FakeMcp()
            runtime, _ = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=TwoCallModel(),
                mcp=mcp,
                listener_overrides={"mcpTimeoutSeconds": 900},
            )
            baseline(runtime)
            add_message(source, number=1, sender_id="alice", text="Question requiring two searches?")
            clock.value = 1_005
            command(runtime, "budget-collect", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 1_007
            command(runtime, "budget-classify", 3, {"kind": "runtime.tick", "wait": True})
            clock.value = 1_017
            with patch("worker.reply_runtime.runtime.time.monotonic", side_effect=[0.0, 0.0, 600.0]):
                command(runtime, "budget-retrieve", 3, {"kind": "runtime.tick", "wait": True})
            item = runtime.query({"kind": "work.list"})["items"][0]
            runtime.close()

        self.assertEqual(item["status"], "pending")
        self.assertEqual([call["timeoutSeconds"] for call in mcp.calls], [900, 300])


if __name__ == "__main__":
    unittest.main()
