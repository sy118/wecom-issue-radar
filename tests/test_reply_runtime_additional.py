from __future__ import annotations

import json
import tempfile
import unittest
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

    def test_supplement_restarts_the_human_reply_deadline(self):
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
            clock.value = 1_017  # initial deadline, but before the reset deadline at 1_022
            command(runtime, "old-deadline", 3, {"kind": "runtime.tick", "wait": True})
            before_new_deadline = runtime.query({"kind": "work.list"})["items"][0]
            calls_before_new_deadline = len(mcp.calls)
            clock.value = 1_022
            command(runtime, "new-deadline", 3, {"kind": "runtime.tick", "wait": True})
            after = runtime.query({"kind": "work.list"})["items"][0]
            runtime.close()

        self.assertEqual(before_new_deadline["status"], "waiting_for_human_reply")
        self.assertEqual(calls_before_new_deadline, 0)
        self.assertEqual(after["status"], "pending")
        self.assertEqual(len(mcp.calls), 1)

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
            clock.value = 1_007
            command(runtime, "overlap-second", 3, {"kind": "runtime.tick", "wait": True})
            items = runtime.query({"kind": "work.list"})["items"]
            runtime.close()

        self.assertEqual(len(items), 1)

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
