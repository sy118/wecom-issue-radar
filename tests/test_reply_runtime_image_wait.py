from __future__ import annotations

import base64
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from tests.test_reply_runtime_additional import (
    FakeClock,
    FakeMcp,
    FakeMessages,
    ScriptedModel,
    baseline,
    command,
    configure_runtime,
)
from worker.reply_runtime import ReplyRuntime
from worker.reply_runtime.message_source import LocalWeComMessageSource


ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _pending_image_message(*, number: int = 7, send_time: int = 1_001) -> dict:
    return {
        "cursor": [send_time, 1, number, 9],
        "messageId": str(number),
        "serverId": "9",
        "sequence": 1,
        "sendTime": send_time,
        "groupId": "room",
        "senderId": "alice",
        "senderName": "Alice",
        "contentType": "image",
        "text": "[image]",
        "images": [
            {
                "filename": "",
                "mimeType": "image/jpeg",
                "errorCode": "IMAGE_RESOLUTION_PENDING",
            }
        ],
    }


def _pending_file_message(*, number: int = 8, send_time: int = 1_001) -> dict:
    return {
        "cursor": [send_time, 1, number, 9],
        "messageId": str(number),
        "serverId": "9",
        "sequence": 1,
        "sendTime": send_time,
        "groupId": "room",
        "senderId": "alice",
        "senderName": "Alice",
        "contentType": "file",
        "text": "[文件]",
        "images": [],
        "files": [
            {
                "filename": "stock.pdf",
                "mimeType": "application/pdf",
                "size": 100,
                "errorCode": "FILE_RESOLUTION_PENDING",
            }
        ],
    }


def _timestamp(value: str | None, fallback: float) -> float:
    if not value:
        return fallback
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class _DelayedWeComCacheSource(FakeMessages):
    def __init__(self, config_path: Path):
        super().__init__()
        self._local = LocalWeComMessageSource(config_path)

    def refresh_images(self, listener, messages):
        return self._local.refresh_images(listener, messages)

    def close(self):
        self._local.close()


class ReplyRuntimeImageWaitTests(unittest.TestCase):
    def test_file_only_message_waits_when_local_cache_file_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            source = FakeMessages()
            runtime, _listener = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=FakeMcp(),
            )
            try:
                baseline(runtime)
                source.rows.append(_pending_file_message())
                clock.value = 1_005
                command(runtime, "collect-file", 3, {"kind": "runtime.tick", "wait": True})
                clock.value = 1_007
                command(runtime, "wait-file", 3, {"kind": "runtime.tick", "wait": True})
                waiting = runtime.query({"kind": "work.list"})["items"][0]
            finally:
                runtime.close()

        self.assertEqual(waiting["status"], "waiting_for_image")
        self.assertIsNotNone(waiting["imageRetryAt"])
        self.assertIsNotNone(waiting["imageWaitDueAt"])

    def test_non_question_text_does_not_wait_for_a_missing_image(self):
        available_image = {"base64": "aA==", "mimeType": "image/png"}

        class CacheAppearingMessages(FakeMessages):
            def __init__(self):
                super().__init__()
                self.refresh_calls = 0

            def refresh_images(self, listener, messages):
                del listener
                self.refresh_calls += 1
                if self.refresh_calls == 1:
                    return messages
                return [
                    {**message, "images": [available_image]}
                    for message in messages
                ]

        class NonQuestionModel(ScriptedModel):
            def classify(self, *, messages, groupContext, question=None):
                del messages, groupContext, question
                return {"labels": ["chat"]}

        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            source = CacheAppearingMessages()
            runtime, _listener = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=NonQuestionModel(),
                mcp=FakeMcp(),
            )
            try:
                baseline(runtime)
                message = _pending_image_message()
                message["text"] = "再试试，我这边看了是好的。"
                source.rows.append(message)

                clock.value = 1_005
                command(runtime, "collect-chat-image", 3, {"kind": "runtime.tick", "wait": True})
                clock.value = 1_007
                command(runtime, "classify-chat-image", 3, {"kind": "runtime.tick", "wait": True})
                finished = runtime.query({"kind": "work.list"})["items"][0]
            finally:
                runtime.close()

        self.assertEqual(finished["status"], "ignored_non_question")
        self.assertEqual(finished["imageStatus"], "unavailable")
        self.assertIsNone(finished.get("imageRetryAt"))
        self.assertEqual(source.refresh_calls, 1)

    def test_restart_closes_an_interrupted_image_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            source = FakeMessages()
            mcp = FakeMcp()
            model = ScriptedModel()
            first, _listener = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=model,
                mcp=mcp,
            )
            second = None
            first_closed = False
            try:
                baseline(first)
                source.rows.append(_pending_image_message())
                clock.value = 1_005
                command(first, "collect-before-restart", 3, {"kind": "runtime.tick", "wait": True})
                clock.value = 1_007
                command(first, "wait-before-restart", 3, {"kind": "runtime.tick", "wait": True})
                waiting = first.query({"kind": "work.list"})["items"][0]
                database_path = first.store.path
                first.close()
                first_closed = True

                second = ReplyRuntime(
                    database_path,
                    clock=clock,
                    message_source=source,
                    model=model,
                    mcp=mcp,
                    autostart=False,
                )
                second.start()
                restarted = second.query(
                    {"kind": "work.detail", "workId": waiting["id"]}
                )["item"]
            finally:
                if not first_closed:
                    first.close()
                if second is not None:
                    second.close()

        self.assertEqual(waiting["status"], "waiting_for_image")
        self.assertEqual(restarted["status"], "closed_runtime_restarted")
        self.assertEqual(restarted["error"]["code"], "RUNTIME_RESTARTED")

    def test_configuration_close_does_not_leave_terminal_work_resolving_an_image(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            source = FakeMessages()
            runtime, listener = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=FakeMcp(),
            )
            try:
                baseline(runtime)
                source.rows.append(_pending_image_message())
                clock.value = 1_005
                command(runtime, "collect-before-disable", 3, {"kind": "runtime.tick", "wait": True})
                clock.value = 1_007
                command(runtime, "wait-before-disable", 3, {"kind": "runtime.tick", "wait": True})
                waiting = runtime.query({"kind": "work.list"})["items"][0]

                listener["enabled"] = False
                command(
                    runtime,
                    "disable-listener-during-image-wait",
                    3,
                    {"kind": "listener.save", "listener": listener},
                )
                closed = runtime.query(
                    {"kind": "work.detail", "workId": waiting["id"]}
                )["item"]
            finally:
                runtime.close()

        self.assertEqual(waiting["status"], "waiting_for_image")
        self.assertEqual(closed["status"], "closed_configuration_changed")
        self.assertNotEqual(closed["imageStatus"], "resolving")

    def test_configuration_close_during_collection_does_not_leave_terminal_image_resolving(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            source = FakeMessages()
            runtime, listener = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=FakeMcp(),
            )
            try:
                baseline(runtime)
                source.rows.append(_pending_image_message())
                clock.value = 1_005
                command(
                    runtime,
                    "collect-image-before-disable",
                    3,
                    {"kind": "runtime.tick", "wait": True},
                )
                collecting = runtime.query({"kind": "work.list"})["items"][0]

                listener["enabled"] = False
                command(
                    runtime,
                    "disable-listener-during-image-collection",
                    3,
                    {"kind": "listener.save", "listener": listener},
                )
                closed = runtime.query(
                    {"kind": "work.detail", "workId": collecting["id"]}
                )["item"]
            finally:
                runtime.close()

        self.assertEqual(collecting["status"], "collecting")
        self.assertEqual(closed["status"], "closed_configuration_changed")
        self.assertNotEqual(closed["imageStatus"], "resolving")

    def test_substantive_text_bypasses_a_late_image_cache(self):
        available_image = {"base64": "aA==", "mimeType": "image/png"}

        class CacheAppearingMessages(FakeMessages):
            def __init__(self):
                super().__init__()
                self.refresh_calls = 0

            def refresh_images(self, listener, messages):
                del listener
                self.refresh_calls += 1
                if self.refresh_calls == 1:
                    return messages
                return [
                    {**message, "images": [available_image]}
                    for message in messages
                ]

        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            source = CacheAppearingMessages()
            model = ScriptedModel()
            runtime, _listener = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=model,
                mcp=FakeMcp(),
            )
            try:
                baseline(runtime)
                message = _pending_image_message()
                message["text"] = "请结合这张截图检查导出报错"
                source.rows.append(message)

                clock.value = 1_005
                command(runtime, "collect-text-image", 3, {"kind": "runtime.tick", "wait": True})
                clock.value = 1_007
                command(runtime, "wait-text-image", 3, {"kind": "runtime.tick", "wait": True})
                waiting = runtime.query({"kind": "work.list"})["items"][0]

                clock.value = _timestamp(waiting.get("imageRetryAt"), 1_012)
                command(runtime, "resolve-text-image", 3, {"kind": "runtime.tick", "wait": True})
                recovered = runtime.query({"kind": "work.list"})["items"][0]
            finally:
                runtime.close()

        self.assertEqual(waiting["status"], "waiting_for_human_reply")
        self.assertEqual(waiting["imageStatus"], "unavailable")
        self.assertIsNone(waiting.get("imageRetryAt"))
        self.assertEqual(recovered["status"], "waiting_for_human_reply")
        self.assertEqual(recovered["imageStatus"], "unavailable")
        self.assertEqual(source.refresh_calls, 1)

    def test_one_available_image_does_not_wait_for_other_missing_images(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            source = FakeMessages()
            runtime, _listener = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=FakeMcp(),
            )
            try:
                baseline(runtime)
                message = _pending_image_message()
                message["text"] = ""
                message["images"] = [
                    {"base64": "aA==", "mimeType": "image/png"},
                    message["images"][0],
                ]
                source.rows.append(message)

                clock.value = 1_005
                command(runtime, "collect-partial-images", 3, {"kind": "runtime.tick", "wait": True})
                clock.value = 1_007
                command(runtime, "classify-partial-images", 3, {"kind": "runtime.tick", "wait": True})
                waiting = runtime.query({"kind": "work.list"})["items"][0]
            finally:
                runtime.close()

        self.assertEqual(waiting["status"], "waiting_for_human_reply")
        self.assertIsNone(waiting.get("imageRetryAt"))
        self.assertIsNone(waiting.get("imageWaitDueAt"))

    def test_substantive_text_retrieves_at_the_human_deadline_without_the_image(self):
        available_image = {"base64": "aA==", "mimeType": "image/png"}

        class ImageCaptureModel(ScriptedModel):
            def __init__(self):
                super().__init__()
                self.planning_images = None

            def retrieve(self, **kwargs):
                self.planning_images = kwargs.get("images")
                return super().retrieve(**kwargs)

        class CacheAfterHumanDeadline(FakeMessages):
            def __init__(self, clock):
                super().__init__()
                self.clock = clock

            def refresh_images(self, listener, messages):
                del listener
                if self.clock.value < 1_050:
                    return messages
                return [
                    {**message, "images": [available_image]}
                    for message in messages
                ]

        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            source = CacheAfterHumanDeadline(clock)
            model = ImageCaptureModel()
            runtime, _listener = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=model,
                mcp=FakeMcp(),
            )
            try:
                baseline(runtime)
                message = _pending_image_message()
                message["text"] = "请结合这张截图检查导出报错"
                source.rows.append(message)

                clock.value = 1_005
                command(runtime, "collect-late-caption", 3, {"kind": "runtime.tick", "wait": True})
                clock.value = 1_007
                command(runtime, "classify-late-caption", 3, {"kind": "runtime.tick", "wait": True})
                waiting = runtime.query({"kind": "work.list"})["items"][0]

                clock.value = 1_017
                command(runtime, "human-deadline-late-caption", 3, {"kind": "runtime.tick", "wait": True})
                still_waiting = runtime.query({"kind": "work.list"})["items"][0]

                clock.value = 1_050
                command(runtime, "image-arrived-late-caption", 3, {"kind": "runtime.tick", "wait": True})
                recovered = runtime.query(
                    {"kind": "work.detail", "workId": waiting["id"]}
                )["item"]
            finally:
                runtime.close()

        self.assertIsNone(waiting.get("imageWaitDueAt"))
        self.assertEqual(still_waiting["status"], "pending")
        self.assertEqual(still_waiting["imageStatus"], "unavailable")
        self.assertEqual(recovered["status"], "pending")
        self.assertEqual(recovered["imageStatus"], "unavailable")
        self.assertEqual(model.planning_images, [])

    def test_image_that_arrives_after_timeout_automatically_reopens_analysis(self):
        available_image = {"base64": "aA==", "mimeType": "image/png"}

        class VeryLateCache(FakeMessages):
            def __init__(self, clock):
                super().__init__()
                self.clock = clock

            def refresh_images(self, listener, messages):
                del listener
                if self.clock.value < 1_300:
                    return messages
                return [
                    {**message, "images": [available_image]}
                    for message in messages
                ]

        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            source = VeryLateCache(clock)
            runtime, _listener = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=FakeMcp(),
            )
            try:
                baseline(runtime)
                message = _pending_image_message()
                message["text"] = ""
                source.rows.append(message)

                clock.value = 1_005
                command(runtime, "late-collect", 3, {"kind": "runtime.tick", "wait": True})
                clock.value = 1_007
                command(runtime, "late-wait", 3, {"kind": "runtime.tick", "wait": True})
                waiting = runtime.query({"kind": "work.list"})["items"][0]

                clock.value = _timestamp(waiting.get("imageWaitDueAt"), 1_187)
                command(runtime, "late-timeout", 3, {"kind": "runtime.tick", "wait": True})
                needs_image = runtime.query(
                    {"kind": "work.detail", "workId": waiting["id"]}
                )["item"]

                clock.value = max(
                    1_300,
                    _timestamp(needs_image.get("imageRetryAt"), 1_300),
                )
                command(runtime, "late-cache-arrived", 3, {"kind": "runtime.tick", "wait": True})
                recovered = runtime.query(
                    {"kind": "work.detail", "workId": waiting["id"]}
                )["item"]
            finally:
                runtime.close()

        self.assertEqual(needs_image["status"], "needs_image")
        self.assertEqual(needs_image["imageStatus"], "unavailable")
        self.assertNotEqual(recovered["status"], "needs_image")
        self.assertIn(recovered["imageStatus"], {"ready", "processed"})

    def test_delayed_wecom_cache_stays_pending_then_recovers_before_image_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "Data"
            mapping_dir = root / "CacheMapping"
            data.mkdir()
            mapping_dir.mkdir()

            file_db = data / "file.db"
            connection = sqlite3.connect(file_db)
            try:
                connection.execute(
                    """CREATE TABLE file_table4 (
                           conversation_id TEXT, message_id INTEGER, server_id TEXT,
                           name TEXT, md5 TEXT, size INTEGER, extension_type INTEGER
                       )"""
                )
                connection.execute(
                    "INSERT INTO file_table4 VALUES (?,?,?,?,?,?,?)",
                    ("room", 7, "wecom-server-token", "question.png", "", len(ONE_PIXEL_PNG), 4),
                )
                connection.commit()
            finally:
                connection.close()

            mapping_db = mapping_dir / "mapping.db"
            connection = sqlite3.connect(mapping_db)
            try:
                connection.execute(
                    """CREATE TABLE mapping(
                           type INTEGER DEFAULT 0 NOT NULL,
                           key TEXT DEFAULT '' NOT NULL,
                           file_name TEXT DEFAULT '',
                           last_modify_time INTEGER DEFAULT 0 NOT NULL,
                           file_md5 INTEGER DEFAULT 0 NOT NULL,
                           PRIMARY KEY(type,key)
                       )"""
                )
                connection.commit()
            finally:
                connection.close()

            keys_path = root / "keys.json"
            keys_path.write_text(
                json.dumps({"file.db": {"enc_key": "11" * 32}}),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "wxwork_db_dir": str(data),
                        "wxwork_keys_file": str(keys_path),
                    }
                ),
                encoding="utf-8",
            )

            @contextmanager
            def plaintext_connection(config, db_name, include_wal=True):
                del include_wal
                db = sqlite3.connect(Path(config["wxwork_db_dir"]) / db_name)
                db.row_factory = sqlite3.Row
                try:
                    yield db
                finally:
                    db.close()

            clock = FakeClock()
            source = _DelayedWeComCacheSource(config_path)
            runtime = None
            with patch(
                "worker.wecom.local_db.decrypted_connection", plaintext_connection
            ):
                try:
                    runtime, listener = configure_runtime(
                        directory,
                        clock=clock,
                        messages=source,
                        model=ScriptedModel(),
                        mcp=FakeMcp(),
                    )
                    baseline(runtime)
                    source.rows.append(_pending_image_message())

                    clock.value = 1_005
                    command(runtime, "collect-image", 3, {"kind": "runtime.tick", "wait": True})
                    clock.value = 1_007
                    command(runtime, "wait-for-image", 3, {"kind": "runtime.tick", "wait": True})
                    waiting = runtime.query({"kind": "work.list"})["items"][0]

                    cache_file = root / "Cache" / "Image" / "2026-08" / "question.png"
                    cache_file.parent.mkdir(parents=True)
                    cache_file.write_bytes(ONE_PIXEL_PNG)
                    connection = sqlite3.connect(mapping_db)
                    try:
                        connection.execute(
                            "INSERT INTO mapping(type,key,file_name,last_modify_time,file_md5) "
                            "VALUES(?,?,?,?,?)",
                            (
                                2,
                                "wecom-server-token",
                                r"2026-08\question.png",
                                1_008,
                                0,
                            ),
                        )
                        connection.commit()
                    finally:
                        connection.close()

                    resolved = source.refresh_images(listener, source.rows)[0]["images"][0]
                    self.assertTrue(Path(resolved["localPath"]).is_file())

                    clock.value = max(
                        1_008,
                        _timestamp(waiting.get("imageRetryAt"), 1_008),
                    )
                    command(runtime, "image-cache-arrived", 3, {"kind": "runtime.tick", "wait": True})
                    recovered = runtime.query({"kind": "work.list"})["items"][0]
                finally:
                    if runtime is not None:
                        runtime.close()

        self.assertEqual(waiting["status"], "waiting_for_image")
        self.assertEqual(waiting["imageStatus"], "resolving")
        self.assertIsNotNone(waiting.get("imageRetryAt"))
        self.assertIsNotNone(waiting.get("imageWaitDueAt"))
        self.assertLess(
            _timestamp(waiting.get("imageRetryAt"), float("inf")),
            _timestamp(waiting.get("imageWaitDueAt"), float("-inf")),
        )
        self.assertEqual(recovered["status"], "waiting_for_human_reply")
        self.assertEqual(recovered["imageStatus"], "ready")

    def test_image_resolution_wait_does_not_pause_independent_message_polling(self):
        class BlockingImageSource(FakeMessages):
            def __init__(self):
                super().__init__()
                self.watermark_called = threading.Event()
                self.refresh_started = threading.Event()
                self.release_refresh = threading.Event()
                self.later_message_polled = threading.Event()

            def watermark(self, listener):
                self.watermark_called.set()
                return super().watermark(listener)

            def read(self, listener, cursor):
                rows = super().read(listener, cursor)
                if any(row.get("messageId") == "8" for row in rows):
                    self.later_message_polled.set()
                return rows

            def refresh_images(self, listener, messages):
                self.refresh_started.set()
                self.release_refresh.wait(5)
                return messages

        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            source = BlockingImageSource()
            runtime, _listener = configure_runtime(
                directory,
                clock=clock,
                messages=source,
                model=ScriptedModel(),
                mcp=FakeMcp(),
            )
            try:
                runtime.start()
                self.assertTrue(source.watermark_called.wait(2))
                source.rows.append(_pending_image_message())
                clock.value = 1_005

                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if runtime.query({"kind": "work.list"})["items"]:
                        break
                    time.sleep(0.02)
                self.assertTrue(runtime.query({"kind": "work.list"})["items"])

                clock.value = 1_007
                self.assertTrue(source.refresh_started.wait(3))
                source.rows.append(
                    {
                        "cursor": [1_008, 2, 8, 9],
                        "messageId": "8",
                        "serverId": "9",
                        "sequence": 2,
                        "sendTime": 1_008,
                        "groupId": "room",
                        "senderId": "bob",
                        "senderName": "Bob",
                        "contentType": "text",
                        "text": "A later question",
                    }
                )
                clock.value = 1_009
                self.assertTrue(
                    source.later_message_polled.wait(2),
                    "message polling stopped while image resolution was waiting",
                )
            finally:
                source.release_refresh.set()
                runtime.close()


if __name__ == "__main__":
    unittest.main()
