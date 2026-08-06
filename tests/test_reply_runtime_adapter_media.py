from __future__ import annotations

import asyncio
import json
import base64
import shutil
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from worker.reply_runtime.adapters import (
    ConfiguredModelAdapter,
    McpSdkAdapter,
    _images_from_messages,
    _openai_image_blocks,
    _without_image_bytes,
)
from worker.reply_runtime.errors import RuntimeProtocolError
from worker.reply_runtime.message_source import LocalWeComMessageSource
from worker.wecom.local_db import FileResolver, MessageDatabaseSnapshot, format_media_text


ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _AsyncTransport:
    async def __aenter__(self):
        return object(), object()

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _FakeMcpSession:
    def __init__(self, harness, number):
        self.harness = harness
        self.number = number

    async def __aenter__(self):
        self.harness.events.append(f"enter:{self.number}")
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        self.harness.events.append(f"exit:{self.number}")
        return False

    async def initialize(self):
        self.harness.events.append(f"initialize:{self.number}")

    async def send_ping(self):
        self.harness.events.append(f"ping:{self.number}")
        delay = self.harness.ping_delays.get(self.number)
        if delay:
            await asyncio.sleep(delay)
        failure = self.harness.ping_failures.get(self.number)
        if failure:
            raise failure
        return {}

    async def call_tool(self, name, arguments, **_kwargs):
        self.harness.events.append(f"call:{self.number}:{name}")
        self.harness.tool_calls.append((self.number, name, arguments))
        key = (self.number, name)
        blocker = self.harness.tool_blockers.get(key)
        if blocker is not None:
            self.harness.tool_started.setdefault(key, threading.Event()).set()
            await asyncio.to_thread(blocker.wait, 10)
        delay = self.harness.tool_delays.get(key)
        if delay:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                self.harness.tool_cancelled.append(key)
                raise
        failure = self.harness.tool_failures.get((self.number, name))
        if failure:
            raise failure
        self.harness.tool_completed.append(key)
        return self.harness.tool_results.get(
            (self.number, name),
            {"isError": False, "content": [{"type": "text", "text": "ok"}]},
        )


class _McpHarness:
    def __init__(self):
        self.events = []
        self.sessions = []
        self.tool_calls = []
        self.tool_started = {}
        self.tool_completed = []
        self.tool_cancelled = []
        self.tool_blockers = {}
        self.tool_delays = {}
        self.ping_delays = {}
        self.ping_failures = {}
        self.tool_failures = {}
        self.tool_results = {}

    def client_session(self, _read_stream, _write_stream):
        session = _FakeMcpSession(self, len(self.sessions) + 1)
        self.sessions.append(session)
        return session

    @staticmethod
    def transport(*_args, **_kwargs):
        return _AsyncTransport()


class MediaTextTests(unittest.TestCase):
    def test_image_text_keeps_caption_without_filename_or_binary_marker(self):
        text = format_media_text(
            4,
            "企业微信截图_17854048953255.png",
            "企业微信截图_17854048953255.png\n帮忙看看怎么支出单列表，会冒出20年之前的单子？",
            "[二进制内容 2 字节]",
        )

        self.assertEqual(text, "帮忙看看怎么支出单列表，会冒出20年之前的单子？")

    def test_image_without_a_real_caption_uses_image_placeholder(self):
        text = format_media_text(
            4,
            "企业微信截图_17854048953255.png",
            "企业微信截图_17854048953255.png",
            "[二进制内容 23 字节]",
        )

        self.assertEqual(text, "[图片]")

    def test_screenshot_without_a_real_caption_uses_image_placeholder(self):
        self.assertEqual(format_media_text(123, "question.png", "question.png"), "[图片]")

    def test_binary_marker_is_only_removed_when_the_whole_line_matches(self):
        text = format_media_text(
            4,
            "",
            "用户原话包含 [二进制内容 2 字节]，请保留",
        )

        self.assertEqual(text, "用户原话包含 [二进制内容 2 字节]，请保留")


class MessageSnapshotIdentityTests(unittest.TestCase):
    def test_single_snapshot_read_keeps_highest_sequence_for_stable_message_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "message.db"
            connection = sqlite3.connect(database)
            try:
                schema = """(
                    message_id INTEGER, server_id INTEGER, sequence INTEGER,
                    sender_id INTEGER, conversation_id TEXT, content_type INTEGER,
                    send_time INTEGER, flag INTEGER, content BLOB,
                    extra_content BLOB, local_extra_content BLOB
                )"""
                connection.execute(f"CREATE TABLE message_table {schema}")
                connection.execute(f"CREATE TABLE message_small_table {schema}")
                low = (219689, 2257295, 28018287, 101, "room", 1, 101, 0, "old", None, None)
                high = (219689, 2257295, 28018290, 101, "room", 1, 100, 0, "latest", None, None)
                connection.execute("INSERT INTO message_table VALUES(?,?,?,?,?,?,?,?,?,?,?)", low)
                connection.execute("INSERT INTO message_small_table VALUES(?,?,?,?,?,?,?,?,?,?,?)", high)
                connection.execute(
                    "INSERT INTO message_table VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (0, 0, 1, 102, "room", 1, 101, 0, "legacy-one", None, None),
                )
                connection.execute(
                    "INSERT INTO message_table VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (0, 0, 2, 103, "room", 1, 102, 0, "legacy-two", None, None),
                )
                connection.commit()
            finally:
                connection.close()
            keys = root / "keys.json"
            keys.write_text(
                json.dumps({"message.db": {"enc_key": "11" * 32}}),
                encoding="utf-8",
            )

            def copy_decrypt(encrypted_path, _raw_key):
                target = root / "snapshot.db"
                shutil.copyfile(encrypted_path, target)
                return str(target)

            snapshot = MessageDatabaseSnapshot(decryptor=copy_decrypt, temp_dir=root)
            try:
                rows = snapshot.read_messages(
                    {
                        "wxwork_db_dir": str(root),
                        "wxwork_keys_file": str(keys),
                    },
                    "room",
                    0,
                    200,
                )
            finally:
                snapshot.close()

        self.assertEqual(len(rows), 3)
        stable = next(row for row in rows if row["message_id"] == 219689)
        self.assertEqual(stable["sequence"], 28018290)
        self.assertEqual(stable["content_raw"], "latest")
        self.assertEqual(
            {row["content_raw"] for row in rows if row["message_id"] == 0},
            {"legacy-one", "legacy-two"},
        )


class MessageSourceSequenceReplayTests(unittest.TestCase):
    def test_type_14_png_in_wecom_image_cache_is_emitted_as_image(self):
        source = LocalWeComMessageSource()
        source._refresh_identities = lambda _config: None
        raw = {
            "send_time": 100,
            "sequence": 1,
            "message_id": 7,
            "server_id": 9,
            "content_type": 14,
            "content_raw": "企业微信截图_1785979325887.png".encode("utf-8"),
            "extra_content_raw": b"",
            "local_extra_content_raw": (
                r"E:\Documents\WXWork\Cache\Image\2026-08\企业微信截图_1785979325887.png"
            ).encode("utf-8"),
        }
        formatted = {
            **raw,
            "sender_id": 42,
            "sender": "Alice",
            "content": "[文件]",
        }
        try:
            with (
                patch("worker.reply_runtime.message_source.load_config", return_value={}),
                patch("worker.reply_runtime.message_source.read_messages", return_value=[raw]),
                patch("worker.reply_runtime.message_source.format_message", return_value=formatted),
            ):
                messages = source.read({"groupId": "room"}, [0, 0, 0, 0])
        finally:
            source.close()

        self.assertEqual(messages[0]["contentType"], "image")
        self.assertEqual(messages[0]["text"], "[图片]")
        self.assertEqual(
            messages[0]["images"],
            [
                {
                    "filename": "",
                    "mimeType": "image/jpeg",
                    "errorCode": "IMAGE_RESOLUTION_PENDING",
                }
            ],
        )

    def test_type_123_extracts_each_image_ref_from_content_raw(self):
        image_refs = [
            "5d72cf6033da41d57123e92480ee7e20",
            "54761e6c791bcb13fd2bcfafcafd1878",
            "d620991caaebb853cb57f6c4f670d0ca",
        ]
        source = LocalWeComMessageSource()
        source._refresh_identities = lambda _config: None
        raw = {
            "send_time": 100,
            "sequence": 28_022_800,
            "message_id": 221_998,
            "server_id": 2_268_105,
            "content_type": 123,
            "content_raw": b"".join(
                b"\x52\x20" + image_ref.encode("ascii")
                for image_ref in image_refs
            ),
            "extra_content_raw": b"",
            "local_extra_content_raw": b"",
        }
        formatted = {
            **raw,
            "sender_id": 42,
            "sender": "Alice",
            "content": "帮忙看看为什么库存历史数据不正确？",
        }
        try:
            with (
                patch("worker.reply_runtime.message_source.load_config", return_value={}),
                patch("worker.reply_runtime.message_source.read_messages", return_value=[raw]),
                patch("worker.reply_runtime.message_source.format_message", return_value=formatted),
            ):
                messages = source.read({"groupId": "room"}, [0, 0, 0, 0])
        finally:
            source.close()

        self.assertEqual(messages[0]["contentType"], "image")
        self.assertEqual(messages[0].get("imageMd5Refs"), image_refs)
        self.assertEqual(len(messages[0]["images"]), 3)
        self.assertTrue(
            all(
                image["errorCode"] == "IMAGE_RESOLUTION_PENDING"
                for image in messages[0]["images"]
            )
        )

    def test_type_1011_collects_image_refs_from_all_raw_fields(self):
        refs = [
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "cccccccccccccccccccccccccccccccc",
        ]
        raw = {
            "send_time": 100,
            "sequence": 1,
            "message_id": 7,
            "server_id": 9,
            "content_type": 1011,
            "content_raw": b"\x52\x20" + refs[0].encode("ascii"),
            "extra_content_raw": b"\x52\x20" + refs[1].encode("ascii"),
            "local_extra_content_raw": b"\x52\x20" + refs[2].encode("ascii"),
        }
        formatted = {
            **raw,
            "sender_id": 42,
            "sender": "Alice",
            "content": "请检查这些截图",
        }
        source = LocalWeComMessageSource()
        source._refresh_identities = lambda _config: None
        try:
            with (
                patch("worker.reply_runtime.message_source.load_config", return_value={}),
                patch("worker.reply_runtime.message_source.read_messages", return_value=[raw]),
                patch("worker.reply_runtime.message_source.format_message", return_value=formatted),
            ):
                messages = source.read({"groupId": "room"}, [0, 0, 0, 0])
        finally:
            source.close()

        self.assertEqual(messages[0]["contentType"], "image")
        self.assertEqual(messages[0]["imageMd5Refs"], refs)
        self.assertEqual(len(messages[0]["images"]), 3)

    def test_wecom_zero_and_two_content_types_are_emitted_as_text(self):
        for content_type in (0, 2):
            with self.subTest(content_type=content_type):
                source = LocalWeComMessageSource()
                source._refresh_identities = lambda _config: None
                raw = {
                    "send_time": 100,
                    "sequence": 1,
                    "message_id": 7,
                    "server_id": 9,
                    "content_type": content_type,
                }
                formatted = {
                    **raw,
                    "sender_id": 42,
                    "sender": "Alice",
                    "content": "为什么订单没有生成？",
                }
                try:
                    with (
                        patch("worker.reply_runtime.message_source.load_config", return_value={}),
                        patch(
                            "worker.reply_runtime.message_source.get_conversation_state",
                            return_value={"last_message_time": 100, "last_message_id": 7},
                        ),
                        patch("worker.reply_runtime.message_source.read_messages", return_value=[raw]),
                        patch("worker.reply_runtime.message_source.format_message", return_value=formatted),
                        patch("worker.reply_runtime.message_source.FileResolver") as resolver_type,
                    ):
                        resolver_type.return_value.find_files_for_messages.return_value = {}
                        messages = source.read({"groupId": "room"}, [0, 0, 0, 0])
                finally:
                    source.close()

                self.assertEqual(messages[0]["contentType"], "text")
                self.assertEqual(messages[0]["text"], "为什么订单没有生成？")
                self.assertEqual(messages[0]["cursor"], [100, 1, 7, 9])

    def test_non_text_content_type_is_not_promoted_by_readable_payload(self):
        source = LocalWeComMessageSource()
        source._refresh_identities = lambda _config: None
        raw = {
            "send_time": 100,
            "sequence": 1,
            "message_id": 7,
            "server_id": 9,
            "content_type": 1011,
        }
        formatted = {
            **raw,
            "sender_id": 42,
            "sender": "Alice",
            "content": "readable meeting token",
        }
        try:
            with (
                patch("worker.reply_runtime.message_source.load_config", return_value={}),
                patch(
                    "worker.reply_runtime.message_source.get_conversation_state",
                    return_value={"last_message_time": 100, "last_message_id": 7},
                ),
                patch("worker.reply_runtime.message_source.read_messages", return_value=[raw]),
                patch("worker.reply_runtime.message_source.format_message", return_value=formatted),
            ):
                messages = source.read({"groupId": "room"}, [0, 0, 0, 0])
        finally:
            source.close()

        self.assertEqual(messages[0]["contentType"], "unsupported:1011")

    def test_same_tail_message_with_higher_sequence_is_not_skipped_by_fast_watermark(self):
        source = LocalWeComMessageSource()
        source._refresh_identities = lambda _config: None
        raw = {
            "send_time": 100,
            "sequence": 2,
            "message_id": 7,
            "server_id": 9,
            "content_type": 1,
        }
        formatted = {
            **raw,
            "sender_id": 42,
            "sender": "Alice",
            "content": "newest version",
        }
        try:
            with (
                patch("worker.reply_runtime.message_source.load_config", return_value={}),
                patch(
                    "worker.reply_runtime.message_source.get_conversation_state",
                    return_value={"last_message_time": 100, "last_message_id": 7},
                ),
                patch("worker.reply_runtime.message_source.read_messages", return_value=[raw]) as reader,
                patch("worker.reply_runtime.message_source.format_message", return_value=formatted),
            ):
                messages = source.read({"groupId": "room"}, [100, 1, 7, 9])
        finally:
            source.close()

        reader.assert_called_once()
        self.assertEqual([message["sequence"] for message in messages], [2])

    def test_lagging_session_watermark_does_not_hide_a_message_already_in_message_db(self):
        source = LocalWeComMessageSource()
        source._refresh_identities = lambda _config: None
        raw = {
            "send_time": 101,
            "sequence": 1,
            "message_id": 8,
            "server_id": 9,
            "content_type": 1,
        }
        formatted = {
            **raw,
            "sender_id": 42,
            "sender": "Alice",
            "content": "visible in message db",
        }
        try:
            with (
                patch("worker.reply_runtime.message_source.load_config", return_value={}),
                patch(
                    "worker.reply_runtime.message_source.get_conversation_state",
                    return_value={"last_message_time": 99, "last_message_id": 7},
                ),
                patch("worker.reply_runtime.message_source.read_messages", return_value=[raw]) as reader,
                patch("worker.reply_runtime.message_source.format_message", return_value=formatted),
            ):
                messages = source.read({"groupId": "room"}, [100, 0, 7, 9])
        finally:
            source.close()

        reader.assert_called_once()
        self.assertEqual([message["messageId"] for message in messages], ["8"])


class MessageSourceImageAvailabilityTests(unittest.TestCase):
    def test_refresh_keeps_one_result_per_image_ref_in_source_order(self):
        refs = [
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "cccccccccccccccccccccccccccccccc",
        ]
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.png"
            third = Path(directory) / "third.png"
            first.write_bytes(ONE_PIXEL_PNG)
            third.write_bytes(ONE_PIXEL_PNG)

            class Resolver:
                def __init__(self, _config):
                    pass

                def find_files_for_messages(
                    self, _group_id, _message_ids, *, image_md5_refs_by_message=None
                ):
                    self.image_md5_refs_by_message = image_md5_refs_by_message
                    return {
                        7: [
                            {
                                "category": "Image",
                                "name": "third.png",
                                "lookup_md5": refs[2],
                            },
                            {
                                "category": "Image",
                                "name": "first.png",
                                "lookup_md5": refs[0],
                            },
                        ]
                    }

                def source_paths_for(self, infos):
                    return [
                        third if info.get("lookup_md5") == refs[2] else first
                        for info in infos
                    ]

            source = LocalWeComMessageSource()
            messages = [
                {
                    "messageId": "7",
                    "groupId": "room",
                    "contentType": "image",
                    "text": "请检查这三张截图",
                    "imageMd5Refs": refs,
                    "images": [
                        {
                            "filename": "",
                            "mimeType": "image/jpeg",
                            "errorCode": "IMAGE_RESOLUTION_PENDING",
                        }
                        for _ in refs
                    ],
                }
            ]
            try:
                with (
                    patch("worker.reply_runtime.message_source.load_config", return_value={}),
                    patch("worker.reply_runtime.message_source.FileResolver", Resolver),
                ):
                    refreshed = source.refresh_images({"groupId": "room"}, messages)
            finally:
                source.close()

        self.assertEqual(len(refreshed[0]["images"]), 3)
        self.assertEqual(refreshed[0]["images"][0]["localPath"], str(first))
        self.assertEqual(
            refreshed[0]["images"][1]["errorCode"], "IMAGE_FILE_MISSING"
        )
        self.assertEqual(refreshed[0]["images"][2]["localPath"], str(third))

    def test_multi_image_filename_fallback_scans_the_cache_only_once(self):
        resolver = object.__new__(FileResolver)
        resolver.root = Path("C:/unused")
        resolver.cache_mapping_db = None
        resolver.lookup_cache_path = lambda _server_id: None
        calls = []

        def find_files(names):
            calls.append(list(names))
            return []

        resolver.find_files_by_names = find_files
        paths = resolver.source_paths_for(
            [
                {"server_id": "1", "name": "one.png", "category": "Image"},
                {"server_id": "2", "name": "two.png", "category": "Image"},
                {"server_id": "3", "name": "three.png", "category": "Image"},
            ]
        )

        self.assertEqual(paths, [None, None, None])
        self.assertEqual(calls, [["one.png", "two.png", "three.png"]])

    def test_missing_image_can_be_resolved_once_the_wecom_cache_appears(self):
        with tempfile.TemporaryDirectory() as directory:
            available = Path(directory) / "question.png"
            available.write_bytes(ONE_PIXEL_PNG)

            class Resolver:
                def __init__(self, _config):
                    pass

                def find_files_for_messages(self, _group_id, _message_ids):
                    return {7: [{"category": "Image", "name": "question.png"}]}

                def source_path_for(self, _info):
                    return available

            source = LocalWeComMessageSource()
            messages = [
                {
                    "messageId": "7",
                    "groupId": "room",
                    "contentType": "image",
                    "text": "[图片]",
                    "images": [
                        {
                            "filename": "question.png",
                            "errorCode": "IMAGE_FILE_MISSING",
                        }
                    ],
                }
            ]
            try:
                with (
                    patch("worker.reply_runtime.message_source.load_config", return_value={}),
                    patch("worker.reply_runtime.message_source.FileResolver", Resolver),
                ):
                    refreshed = source.refresh_images({"groupId": "room"}, messages)
            finally:
                source.close()

        self.assertEqual(
            refreshed[0]["images"],
            [
                {
                    "localPath": str(available),
                    "filename": "question.png",
                    "mimeType": "image/png",
                }
            ],
        )

    def test_image_evicted_during_collection_becomes_a_missing_descriptor(self):
        class Resolver:
            def __init__(self, _config):
                pass

            def find_files_for_messages(self, _group_id, _message_ids):
                return {7: [{"category": "Image", "name": "question.png"}]}

            def source_path_for(self, _info):
                return None

        source = LocalWeComMessageSource()
        messages = [
            {
                "messageId": "7",
                "groupId": "room",
                "contentType": "image",
                "text": "问题正文仍然完整",
                "images": [
                    {
                        "localPath": "Z:/cache-was-evicted/question.png",
                        "filename": "question.png",
                        "mimeType": "image/png",
                    }
                ],
            }
        ]
        try:
            with (
                patch("worker.reply_runtime.message_source.load_config", return_value={}),
                patch("worker.reply_runtime.message_source.FileResolver", Resolver),
            ):
                refreshed = source.refresh_images({"groupId": "room"}, messages)
        finally:
            source.close()

        self.assertEqual(
            refreshed[0]["images"],
            [
                {
                    "filename": "question.png",
                    "mimeType": "image/png",
                    "errorCode": "IMAGE_FILE_MISSING",
                }
            ],
        )

    def test_forwarded_text_defers_image_resolution_and_removes_attachment_decorations(self):
        resolver_constructions = []
        resolver_queries = []

        class Resolver:
            def __init__(self, config):
                resolver_constructions.append(config)

            def find_files_for_messages(self, group_id, message_ids):
                resolver_queries.append((group_id, list(message_ids)))
                return {
                    7: [
                        {"category": "Image", "name": "企业微信截图_100.png"},
                        {"category": "Image", "name": "企业微信截图_101.png"},
                    ]
                }

            def source_path_for(self, _info):
                return None

        source = LocalWeComMessageSource()
        source._refresh_identities = lambda _config: None
        raw = {
            "send_time": 100,
            "sequence": 1,
            "message_id": 7,
            "server_id": 9,
            "content_type": 2,
        }
        formatted = {
            **raw,
            "sender_id": 42,
            "sender": "Alice",
            "content": (
                "[图片][图片]企业微信截图_100.png\n"
                "为什么列表里会出现二十年前的单子？\n"
                "企业微信截图_101.png\n[二进制内容 2 字节]"
            ),
        }
        try:
            with (
                patch("worker.reply_runtime.message_source.load_config", return_value={}),
                patch(
                    "worker.reply_runtime.message_source.get_conversation_state",
                    return_value={"last_message_time": 100, "last_message_id": 7},
                ),
                patch("worker.reply_runtime.message_source.read_messages", return_value=[raw]),
                patch("worker.reply_runtime.message_source.format_message", return_value=formatted),
                patch("worker.reply_runtime.message_source.FileResolver", Resolver),
            ):
                messages = source.read({"groupId": "room"}, [0, 0, 0, 0])
                self.assertEqual(resolver_constructions, [])
                self.assertEqual(resolver_queries, [])
                self.assertEqual(
                    messages[0]["images"],
                    [
                        {
                            "filename": "",
                            "mimeType": "image/jpeg",
                            "errorCode": "IMAGE_RESOLUTION_PENDING",
                        }
                    ],
                )
                refreshed = source.refresh_images({"groupId": "room"}, messages)
        finally:
            source.close()

        self.assertEqual(messages[0]["contentType"], "text")
        self.assertEqual(messages[0]["text"], "为什么列表里会出现二十年前的单子？")
        self.assertEqual(len(resolver_constructions), 1)
        self.assertEqual(resolver_queries, [("room", [7])])
        self.assertEqual(
            [image["filename"] for image in refreshed[0]["images"]],
            ["企业微信截图_100.png", "企业微信截图_101.png"],
        )
        self.assertTrue(
            all(
                image["errorCode"] == "IMAGE_FILE_MISSING"
                for image in refreshed[0]["images"]
            )
        )

    def test_forwarded_image_marker_finalizes_pending_when_file_db_has_no_record(self):
        class Resolver:
            def __init__(self, _config):
                pass

            def find_files_for_messages(self, _group_id, _message_ids):
                return {7: []}

        source = LocalWeComMessageSource()
        source._refresh_identities = lambda _config: None
        raw = {
            "send_time": 100,
            "sequence": 1,
            "message_id": 7,
            "server_id": 9,
            "content_type": 2,
        }
        formatted = {
            **raw,
            "sender_id": 42,
            "sender": "Alice",
            "content": "[图片]为什么列表数据不对？",
        }
        try:
            with (
                patch("worker.reply_runtime.message_source.load_config", return_value={}),
                patch("worker.reply_runtime.message_source.read_messages", return_value=[raw]),
                patch("worker.reply_runtime.message_source.format_message", return_value=formatted),
                patch("worker.reply_runtime.message_source.FileResolver", Resolver),
            ):
                messages = source.read({"groupId": "room"}, [0, 0, 0, 0])
                self.assertEqual(
                    messages[0]["images"][0]["errorCode"],
                    "IMAGE_RESOLUTION_PENDING",
                )
                refreshed = source.refresh_images({"groupId": "room"}, messages)
        finally:
            source.close()

        self.assertEqual(messages[0]["contentType"], "text")
        self.assertEqual(messages[0]["text"], "为什么列表数据不对？")
        self.assertEqual(refreshed[0]["images"][0]["errorCode"], "IMAGE_FILE_MISSING")

    def test_image_message_keeps_a_missing_descriptor_when_cache_file_is_unavailable(self):
        class Resolver:
            def __init__(self, _config):
                pass

            def find_files_for_messages(self, _group_id, _message_ids):
                return {7: [{"category": "Image", "name": "question.png"}]}

            def source_path_for(self, _info):
                return None

        source = LocalWeComMessageSource()
        source._refresh_identities = lambda _config: None
        raw = {
            "send_time": 100,
            "sequence": 1,
            "message_id": 7,
            "server_id": 9,
            "content_type": 4,
        }
        formatted = {
            **raw,
            "sender_id": 42,
            "sender": "Alice",
            "content": "[图片]",
        }
        try:
            with (
                patch("worker.reply_runtime.message_source.load_config", return_value={}),
                patch(
                    "worker.reply_runtime.message_source.get_conversation_state",
                    return_value={"last_message_time": 100, "last_message_id": 7},
                ),
                patch("worker.reply_runtime.message_source.read_messages", return_value=[raw]),
                patch("worker.reply_runtime.message_source.format_message", return_value=formatted),
                patch("worker.reply_runtime.message_source.FileResolver", Resolver),
            ):
                messages = source.read({"groupId": "room"}, [0, 0, 0, 0])
                self.assertEqual(
                    messages[0]["images"],
                    [
                        {
                            "filename": "",
                            "mimeType": "image/jpeg",
                            "errorCode": "IMAGE_RESOLUTION_PENDING",
                        }
                    ],
                )
                refreshed = source.refresh_images({"groupId": "room"}, messages)
        finally:
            source.close()

        self.assertEqual(
            refreshed[0]["images"],
            [
                {
                    "filename": "question.png",
                    "mimeType": "image/png",
                    "errorCode": "IMAGE_FILE_MISSING",
                }
            ],
        )

    def test_missing_candidate_does_not_hide_an_available_image_for_the_same_message(self):
        with tempfile.TemporaryDirectory() as directory:
            available = Path(directory) / "available.png"
            available.write_bytes(ONE_PIXEL_PNG)

            class Resolver:
                def __init__(self, _config):
                    pass

                def find_files_for_messages(self, _group_id, _message_ids):
                    return {
                        7: [
                            {"category": "Image", "name": "missing.png"},
                            {"category": "Image", "name": "available.png"},
                        ]
                    }

                def source_path_for(self, info):
                    return available if info["name"] == "available.png" else None

            source = LocalWeComMessageSource()
            source._refresh_identities = lambda _config: None
            raw = {
                "send_time": 100,
                "sequence": 1,
                "message_id": 7,
                "server_id": 9,
                "content_type": 4,
            }
            formatted = {
                **raw,
                "sender_id": 42,
                "sender": "Alice",
                "content": "[图片]",
            }
            try:
                with (
                    patch("worker.reply_runtime.message_source.load_config", return_value={}),
                    patch(
                        "worker.reply_runtime.message_source.get_conversation_state",
                        return_value={"last_message_time": 100, "last_message_id": 7},
                    ),
                    patch("worker.reply_runtime.message_source.read_messages", return_value=[raw]),
                    patch("worker.reply_runtime.message_source.format_message", return_value=formatted),
                    patch("worker.reply_runtime.message_source.FileResolver", Resolver),
                ):
                    messages = source.read({"groupId": "room"}, [0, 0, 0, 0])
                    self.assertEqual(
                        messages[0]["images"],
                        [
                            {
                                "filename": "",
                                "mimeType": "image/jpeg",
                                "errorCode": "IMAGE_RESOLUTION_PENDING",
                            }
                        ],
                    )
                    refreshed = source.refresh_images({"groupId": "room"}, messages)
            finally:
                source.close()

        self.assertEqual(
            refreshed[0]["images"],
            [
                {
                    "localPath": str(available),
                    "filename": "available.png",
                    "mimeType": "image/png",
                },
                {
                    "filename": "missing.png",
                    "mimeType": "image/png",
                    "errorCode": "IMAGE_FILE_MISSING",
                },
            ],
        )

    def test_forwarded_image_resolves_by_extra_content_md5_across_conversations(self):
        image_md5 = "fff9e46803717390c7af0c39c78d4b61"
        unrelated_md5 = "0123456789abcdef0123456789abcdef"
        opaque_32hex = "11111111111111111111111111111111"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "Data"
            mapping_dir = root / "CacheMapping"
            image_path = root / "Cache" / "Image" / "2026-08" / "forwarded.png"
            data.mkdir()
            mapping_dir.mkdir()
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(ONE_PIXEL_PNG)

            file_db = data / "file.db"
            connection = sqlite3.connect(file_db)
            try:
                connection.execute(
                    """CREATE TABLE file_table4 (
                           origin INTEGER, message_id INTEGER, file_index INTEGER,
                           message_type INTEGER, extension_type INTEGER, server_id TEXT,
                           server_type INTEGER, name TEXT, size INTEGER,
                           receive_time INTEGER, sender_id INTEGER, conversation_id TEXT,
                           collection_id INTEGER, info_extension BLOB, url TEXT,
                           flags INTEGER, md5 TEXT, last_modify_time INTEGER
                       )"""
                )
                connection.execute(
                    "INSERT INTO file_table4 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        0,
                        99,
                        0,
                        4,
                        4,
                        "forwarded-image-token",
                        0,
                        "forwarded.png",
                        len(ONE_PIXEL_PNG),
                        1_000,
                        42,
                        "source-room",
                        0,
                        b"",
                        "",
                        0,
                        image_md5,
                        1_000,
                    ),
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
                connection.execute(
                    "INSERT INTO mapping VALUES (?,?,?,?,?)",
                    (
                        2,
                        "forwarded-image-token",
                        r"2026-08\forwarded.png",
                        1_000,
                        0,
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            config = {"wxwork_db_dir": str(data)}
            raw = {
                "send_time": 100,
                "sequence": 1,
                "message_id": 7,
                "server_id": 9,
                "sender_id": 42,
                "conversation_id": "target-room",
                "content_type": 2,
                "extra_content_raw": (
                    b"\x42\x20"
                    + opaque_32hex.encode("ascii")
                    + b"\x52\x20"
                    + unrelated_md5.upper().encode("ascii")
                    + b"\x52\x20"
                    + image_md5.upper().encode("ascii")
                    + b"\x52\x20"
                    + image_md5.encode("ascii")
                    + b"\x18\x01"
                ),
            }
            formatted = {
                **raw,
                "sender": "Alice",
                "content": "Please inspect this forwarded screenshot.",
            }

            @contextmanager
            def plaintext_connection(config_value, db_name, include_wal=True):
                del include_wal
                db = sqlite3.connect(Path(config_value["wxwork_db_dir"]) / db_name)
                db.row_factory = sqlite3.Row
                try:
                    yield db
                finally:
                    db.close()

            source = LocalWeComMessageSource()
            source._refresh_identities = lambda _config: None
            try:
                with (
                    patch("worker.reply_runtime.message_source.load_config", return_value=config),
                    patch("worker.reply_runtime.message_source.read_messages", return_value=[raw]),
                    patch("worker.reply_runtime.message_source.format_message", return_value=formatted),
                    patch("worker.wecom.local_db.decrypted_connection", plaintext_connection),
                ):
                    messages = source.read(
                        {"groupId": "target-room"}, [0, 0, 0, 0]
                    )
                    refreshed = source.refresh_images(
                        {"groupId": "target-room"}, messages
                    )
            finally:
                source.close()

        self.assertEqual(
            messages[0].get("imageMd5Refs"), [unrelated_md5, image_md5]
        )
        self.assertNotIn("imageMd5Refs", _without_image_bytes(messages[0]))
        self.assertEqual(
            refreshed[0]["images"],
            [
                {
                    "filename": "",
                    "mimeType": "image/jpeg",
                    "errorCode": "IMAGE_FILE_MISSING",
                },
                {
                    "localPath": str(image_path),
                    "filename": "forwarded.png",
                    "mimeType": "image/png",
                }
            ],
        )


class ModelMultimodalRequestTests(unittest.TestCase):
    def test_unavailable_image_descriptors_are_not_sent_to_the_model(self):
        available = {"base64": "aA==", "mimeType": "image/png"}
        messages = [
            {
                "text": "The screenshot is unavailable, but the question is complete.",
                "images": [
                    {"filename": "missing.png", "errorCode": "IMAGE_FILE_MISSING"},
                    available,
                ],
            }
        ]

        self.assertEqual(_images_from_messages(messages), [available])

    @staticmethod
    def _openai_adapter():
        return ConfiguredModelAdapter(
            lambda: {
                "llm": {
                    "provider": "openai_compatible",
                    "base_url": "https://model.test/v1",
                    "model": "vision-model",
                }
            }
        )

    def test_openai_sends_the_same_image_to_all_four_workflow_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "question.png"
            image_path.write_bytes(ONE_PIXEL_PNG)
            image = {"localPath": str(image_path), "mimeType": "image/png"}
            requests = []

            def respond(_url, body, _headers, _timeout):
                requests.append(body)
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "labels": ["question"],
                                        "calls": [],
                                        "answer": "ok",
                                        "supported": True,
                                    }
                                )
                            }
                        }
                    ]
                }

            adapter = ConfiguredModelAdapter(
                lambda: {
                    "llm": {
                        "provider": "openai_compatible",
                        "base_url": "https://model.test/v1",
                        "model": "vision-model",
                    }
                }
            )
            with patch("worker.reply_runtime.adapters._request_json", side_effect=respond):
                adapter.classify(
                    messages=[{"text": "请看图", "images": [image]}],
                    groupContext=[],
                )
                adapter.plan_tools(
                    question="请看图",
                    context=[],
                    tools=[],
                    systemPrompt="",
                    images=[image],
                )
                adapter.answer(
                    question="请看图",
                    context=[],
                    evidence=[],
                    systemPrompt="",
                    images=[image],
                )
                adapter.review(
                    question="请看图",
                    answer="ok",
                    evidence=[],
                    images=[image],
                )

        self.assertEqual(len(requests), 4)
        for request in requests:
            user_content = request["messages"][1]["content"]
            image_blocks = [block for block in user_content if block["type"] == "image_url"]
            self.assertEqual(len(image_blocks), 1)
            self.assertTrue(
                image_blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")
            )
        system_prompt = requests[-1]["messages"][0]["content"]
        self.assertIn("截图中可直接观察", system_prompt)
        self.assertIn("业务原因", system_prompt)
        self.assertIn("MCP", system_prompt)

    def test_anthropic_sends_the_same_image_to_all_four_workflow_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "question.png"
            image_path.write_bytes(ONE_PIXEL_PNG)
            image = {"localPath": str(image_path), "mimeType": "image/png"}
            requests = []

            def respond(_url, body, _headers, _timeout):
                requests.append(body)
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "labels": ["question"],
                                    "calls": [],
                                    "answer": "ok",
                                    "supported": True,
                                }
                            ),
                        }
                    ]
                }

            adapter = ConfiguredModelAdapter(
                lambda: {
                    "llm": {
                        "provider": "anthropic",
                        "base_url": "https://model.test/v1",
                        "model": "vision-model",
                    }
                }
            )
            with patch("worker.reply_runtime.adapters._request_json", side_effect=respond):
                adapter.classify(
                    messages=[{"text": "请看图", "images": [image]}],
                    groupContext=[],
                )
                adapter.plan_tools(
                    question="请看图", context=[], tools=[], systemPrompt="", images=[image]
                )
                adapter.answer(
                    question="请看图",
                    context=[],
                    evidence=[],
                    systemPrompt="",
                    images=[image],
                )
                adapter.review(
                    question="请看图", answer="ok", evidence=[], images=[image]
                )

        self.assertEqual(len(requests), 4)
        for request in requests:
            content = request["messages"][0]["content"]
            image_blocks = [block for block in content if block["type"] == "image"]
            self.assertEqual(len(image_blocks), 1)
            self.assertEqual(image_blocks[0]["source"]["media_type"], "image/png")
            self.assertEqual(image_blocks[0]["source"]["data"], base64.b64encode(ONE_PIXEL_PNG).decode("ascii"))

    def test_missing_image_is_reported_before_the_model_request(self):
        adapter = self._openai_adapter()
        with (
            patch("worker.reply_runtime.adapters._request_json") as request,
            self.assertRaises(RuntimeProtocolError) as raised,
        ):
            adapter.answer(
                question="请看图",
                context=[],
                evidence=[],
                systemPrompt="",
                images=[{"localPath": "Z:/definitely-missing/question.png", "mimeType": "image/png"}],
            )

        request.assert_not_called()
        self.assertEqual(raised.exception.code, "IMAGE_FILE_MISSING")

    def test_oversized_image_has_a_distinct_error(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "large.png"
            with image_path.open("wb") as stream:
                stream.truncate(20 * 1024 * 1024 + 1)
            adapter = self._openai_adapter()
            with self.assertRaises(RuntimeProtocolError) as raised:
                adapter.answer(
                    question="请看图",
                    context=[],
                    evidence=[],
                    systemPrompt="",
                    images=[{"localPath": str(image_path), "mimeType": "image/png"}],
                )

        self.assertEqual(raised.exception.code, "IMAGE_TOO_LARGE")

    def test_empty_or_invalid_image_has_an_unreadable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "empty.png"
            image_path.write_bytes(b"")
            adapter = self._openai_adapter()
            with self.assertRaises(RuntimeProtocolError) as raised:
                adapter.answer(
                    question="请看图",
                    context=[],
                    evidence=[],
                    systemPrompt="",
                    images=[{"localPath": str(image_path), "mimeType": "image/png"}],
                )

        self.assertEqual(raised.exception.code, "IMAGE_UNREADABLE")

    def test_nonempty_bytes_with_an_image_mime_are_rejected_before_model_request(self):
        adapter = self._openai_adapter()
        with (
            patch("worker.reply_runtime.adapters._request_json") as request,
            self.assertRaises(RuntimeProtocolError) as raised,
        ):
            adapter.answer(
                question="请看图",
                context=[],
                evidence=[],
                systemPrompt="",
                images=[{
                    "base64": base64.b64encode(b"xx").decode("ascii"),
                    "mimeType": "image/png",
                }],
            )

        request.assert_not_called()
        self.assertEqual(raised.exception.code, "IMAGE_UNREADABLE")

    def test_corrupted_supported_image_signatures_are_rejected_before_model_request(self):
        corrupted = {
            "png": (
                "image/png",
                b"\x89PNG\r\n\x1a\n" + b"\x00" * 4 + b"JHDR" + b"\x00" * 8,
            ),
            "jpeg": ("image/jpeg", b"\xff\xd8\xffcorrupt-without-end-marker"),
            "gif": ("image/gif", b"GIF89x" + b"\x00" * 7),
            "webp": ("image/webp", b"RIFF\x08\x00\x00\x00WEPB" + b"\x00" * 4),
            "bmp": ("image/bmp", b"BX" + b"\x00" * 24),
        }
        adapter = self._openai_adapter()

        for name, (mime_type, raw) in corrupted.items():
            with self.subTest(format=name):
                with (
                    patch("worker.reply_runtime.adapters._request_json") as request,
                    self.assertRaises(RuntimeProtocolError) as raised,
                ):
                    adapter.answer(
                        question="请看图",
                        context=[],
                        evidence=[],
                        systemPrompt="",
                        images=[{
                            "base64": base64.b64encode(raw).decode("ascii"),
                            "mimeType": mime_type,
                        }],
                    )

                request.assert_not_called()
                self.assertEqual(raised.exception.code, "IMAGE_UNREADABLE")

    def test_explicit_model_vision_rejection_keeps_a_clear_error(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "question.png"
            image_path.write_bytes(ONE_PIXEL_PNG)
            adapter = self._openai_adapter()
            rejected = RuntimeProtocolError(
                "MODEL_HTTP_ERROR",
                "model returned HTTP 400",
                details={
                    "status": 400,
                    "providerMessage": "This model does not support image input",
                },
            )
            with (
                patch("worker.reply_runtime.adapters._request_json", side_effect=rejected),
                self.assertRaises(RuntimeProtocolError) as raised,
            ):
                adapter.answer(
                    question="请看图",
                    context=[],
                    evidence=[],
                    systemPrompt="",
                    images=[{"localPath": str(image_path), "mimeType": "image/png"}],
                )

        self.assertEqual(raised.exception.code, "MODEL_VISION_UNSUPPORTED")
        self.assertIn("does not support image input", raised.exception.message)

    def test_image_url_only_supported_by_certain_models_is_a_vision_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "question.png"
            image_path.write_bytes(ONE_PIXEL_PNG)
            adapter = self._openai_adapter()
            rejected = RuntimeProtocolError(
                "MODEL_HTTP_ERROR",
                "model returned HTTP 400",
                details={
                    "status": 400,
                    "providerMessage": (
                        "Invalid content type. image_url is only supported by certain models."
                    ),
                },
            )
            with (
                patch("worker.reply_runtime.adapters._request_json", side_effect=rejected),
                self.assertRaises(RuntimeProtocolError) as raised,
            ):
                adapter.answer(
                    question="请看图",
                    context=[],
                    evidence=[],
                    systemPrompt="",
                    images=[{"localPath": str(image_path), "mimeType": "image/png"}],
                )

        self.assertEqual(raised.exception.code, "MODEL_VISION_UNSUPPORTED")

    def test_common_image_url_unsupported_messages_are_vision_rejections(self):
        provider_messages = [
            "Invalid content type: image_url is not supported by this model.",
            "This model does not accept image_url content blocks.",
            "Unsupported content type 'image_url' for the selected model.",
        ]
        adapter = self._openai_adapter()
        image = {
            "base64": base64.b64encode(ONE_PIXEL_PNG).decode("ascii"),
            "mimeType": "image/png",
        }

        for provider_message in provider_messages:
            with self.subTest(provider_message=provider_message):
                rejected = RuntimeProtocolError(
                    "MODEL_HTTP_ERROR",
                    "model returned HTTP 400",
                    details={"status": 400, "providerMessage": provider_message},
                )
                with (
                    patch("worker.reply_runtime.adapters._request_json", side_effect=rejected),
                    self.assertRaises(RuntimeProtocolError) as raised,
                ):
                    adapter.answer(
                        question="请看图",
                        context=[],
                        evidence=[],
                        systemPrompt="",
                        images=[image],
                    )

                self.assertEqual(raised.exception.code, "MODEL_VISION_UNSUPPORTED")

    def test_provider_image_blocks_reject_too_many_images(self):
        image = {
            "base64": base64.b64encode(ONE_PIXEL_PNG).decode("ascii"),
            "mimeType": "image/png",
        }

        with self.assertRaises(RuntimeProtocolError) as raised:
            _openai_image_blocks([image] * 9)

        self.assertEqual(raised.exception.code, "IMAGE_TOO_LARGE")
        self.assertEqual(raised.exception.details["maxImages"], 8)

    def test_provider_image_blocks_enforce_the_aggregate_byte_limit(self):
        image = {
            "base64": base64.b64encode(ONE_PIXEL_PNG).decode("ascii"),
            "mimeType": "image/png",
        }

        with (
            patch("worker.reply_runtime.adapters.MAX_MODEL_IMAGE_TOTAL_BYTES", 1),
            self.assertRaises(RuntimeProtocolError) as raised,
        ):
            _openai_image_blocks([image])

        self.assertEqual(raised.exception.code, "IMAGE_TOO_LARGE")
        self.assertEqual(raised.exception.details["maxTotalBytes"], 1)


class McpSessionRecoveryTests(unittest.TestCase):
    SERVER = {"id": "kb", "transportType": "sse", "url": "https://mcp.test/sse"}

    def _patch_sdk(self, harness):
        return (
            patch("mcp.ClientSession", new=harness.client_session),
            patch("mcp.client.sse.sse_client", new=harness.transport),
        )

    def test_reused_session_ping_failure_reconnects_before_calling_tool_once(self):
        harness = _McpHarness()
        harness.ping_failures[1] = OSError("stale connection")
        adapter = McpSdkAdapter(connect_timeout=2)
        sdk_patches = self._patch_sdk(harness)
        try:
            with sdk_patches[0], sdk_patches[1]:
                adapter.call(
                    server=self.SERVER,
                    toolName="first",
                    arguments={},
                    timeoutSeconds=5,
                )
                result = adapter.call(
                    server=self.SERVER,
                    toolName="second",
                    arguments={"q": "x"},
                    timeoutSeconds=5,
                )
        finally:
            adapter.close()

        self.assertFalse(result["isError"])
        self.assertEqual(
            [call for call in harness.tool_calls if call[1] == "second"],
            [(2, "second", {"q": "x"})],
        )
        self.assertLess(harness.events.index("exit:1"), harness.events.index("call:2:second"))

    def test_tool_transport_interruption_is_not_replayed(self):
        harness = _McpHarness()
        harness.tool_failures[(1, "unstable")] = OSError("stream closed")
        adapter = McpSdkAdapter(connect_timeout=2)
        sdk_patches = self._patch_sdk(harness)
        try:
            with sdk_patches[0], sdk_patches[1]:
                with self.assertRaises(RuntimeProtocolError) as raised:
                    adapter.call(
                        server=self.SERVER,
                        toolName="unstable",
                        arguments={"id": 7},
                        timeoutSeconds=5,
                    )
        finally:
            adapter.close()

        self.assertEqual(raised.exception.code, "MCP_SESSION_INTERRUPTED")
        self.assertEqual(harness.tool_calls, [(1, "unstable", {"id": 7})])

    def test_tool_error_result_has_a_distinct_error(self):
        harness = _McpHarness()
        harness.tool_results[(1, "reject")] = {
            "isError": True,
            "content": [{"type": "text", "text": "invalid arguments"}],
        }
        adapter = McpSdkAdapter(connect_timeout=2)
        sdk_patches = self._patch_sdk(harness)
        try:
            with sdk_patches[0], sdk_patches[1]:
                with self.assertRaises(RuntimeProtocolError) as raised:
                    adapter.call(
                        server=self.SERVER,
                        toolName="reject",
                        arguments={},
                        timeoutSeconds=5,
                    )
        finally:
            adapter.close()

        self.assertEqual(raised.exception.code, "MCP_TOOL_ERROR")
        self.assertEqual(harness.tool_calls, [(1, "reject", {})])

    def test_failed_ping_on_old_and_rebuilt_sessions_is_a_preflight_error(self):
        harness = _McpHarness()
        adapter = McpSdkAdapter(connect_timeout=2)
        sdk_patches = self._patch_sdk(harness)
        try:
            with sdk_patches[0], sdk_patches[1]:
                adapter.call(
                    server=self.SERVER,
                    toolName="first",
                    arguments={},
                    timeoutSeconds=5,
                )
                harness.ping_failures.update(
                    {1: OSError("old stale"), 2: OSError("new unavailable")}
                )
                with self.assertRaises(RuntimeProtocolError) as raised:
                    adapter.call(
                        server=self.SERVER,
                        toolName="never-issued",
                        arguments={},
                        timeoutSeconds=5,
                    )
        finally:
            adapter.close()

        self.assertEqual(raised.exception.code, "MCP_PREFLIGHT_FAILED")
        self.assertEqual(
            [call for call in harness.tool_calls if call[1] == "never-issued"], []
        )

    def test_concurrent_calls_share_one_reconnect_and_each_issue_once(self):
        harness = _McpHarness()
        adapter = McpSdkAdapter(connect_timeout=2)
        sdk_patches = self._patch_sdk(harness)
        errors = []
        results = {}
        barrier = threading.Barrier(3)

        def call(name):
            barrier.wait()
            try:
                results[name] = adapter.call(
                    server=self.SERVER,
                    toolName=name,
                    arguments={"name": name},
                    timeoutSeconds=5,
                )
            except Exception as exc:  # pragma: no cover - asserted through errors
                errors.append(exc)

        try:
            with sdk_patches[0], sdk_patches[1]:
                adapter.call(
                    server=self.SERVER,
                    toolName="first",
                    arguments={},
                    timeoutSeconds=5,
                )
                harness.ping_failures[1] = OSError("old stale")
                threads = [threading.Thread(target=call, args=(name,)) for name in ("a", "b")]
                for thread in threads:
                    thread.start()
                barrier.wait()
                for thread in threads:
                    thread.join(10)
        finally:
            adapter.close()

        self.assertEqual(errors, [])
        self.assertEqual(set(results), {"a", "b"})
        self.assertEqual(len(harness.sessions), 2)
        for name in ("a", "b"):
            self.assertEqual(
                [call for call in harness.tool_calls if call[1] == name],
                [(2, name, {"name": name})],
            )

    def test_tool_timeout_has_a_distinct_error_and_is_not_replayed(self):
        harness = _McpHarness()
        harness.tool_failures[(1, "slow")] = asyncio.TimeoutError()
        adapter = McpSdkAdapter(connect_timeout=2)
        sdk_patches = self._patch_sdk(harness)
        try:
            with sdk_patches[0], sdk_patches[1]:
                with self.assertRaises(RuntimeProtocolError) as raised:
                    adapter.call(
                        server=self.SERVER,
                        toolName="slow",
                        arguments={},
                        timeoutSeconds=5,
                    )
        finally:
            adapter.close()

        self.assertEqual(raised.exception.code, "MCP_TIMEOUT")
        self.assertEqual(harness.tool_calls, [(1, "slow", {})])

    def test_timed_out_reconnect_does_not_cancel_an_in_flight_tool_call(self):
        harness = _McpHarness()
        release_long_call = threading.Event()
        long_call_started = threading.Event()
        harness.tool_blockers[(1, "long")] = release_long_call
        harness.tool_started[(1, "long")] = long_call_started
        adapter = McpSdkAdapter(connect_timeout=1)
        sdk_patches = self._patch_sdk(harness)
        long_result = {}
        long_errors = []

        def run_long_call():
            try:
                long_result["value"] = adapter.call(
                    server=self.SERVER,
                    toolName="long",
                    arguments={},
                    timeoutSeconds=3,
                )
            except Exception as exc:  # pragma: no cover - asserted through long_errors
                long_errors.append(exc)

        long_thread = threading.Thread(target=run_long_call)
        try:
            with sdk_patches[0], sdk_patches[1]:
                adapter.call(
                    server=self.SERVER,
                    toolName="first",
                    arguments={},
                    timeoutSeconds=3,
                )
                long_thread.start()
                self.assertTrue(long_call_started.wait(2))
                harness.ping_failures[1] = OSError("stale while long call is active")

                with self.assertRaises(RuntimeProtocolError) as short_timeout:
                    adapter.call(
                        server=self.SERVER,
                        toolName="short",
                        arguments={},
                        timeoutSeconds=1,
                    )

                fresh_result = adapter.call(
                    server=self.SERVER,
                    toolName="fresh",
                    arguments={},
                    timeoutSeconds=1,
                )
                release_long_call.set()
                long_thread.join(0.75)
                completed_promptly = not long_thread.is_alive()
                long_thread.join(4)
        finally:
            release_long_call.set()
            long_thread.join(4)
            adapter.close()

        self.assertEqual(short_timeout.exception.code, "MCP_TIMEOUT")
        self.assertTrue(completed_promptly)
        self.assertFalse(long_thread.is_alive())
        self.assertEqual(long_errors, [])
        self.assertFalse(long_result["value"]["isError"])
        self.assertFalse(fresh_result["isError"])
        self.assertEqual(
            [call for call in harness.tool_calls if call[1] == "long"],
            [(1, "long", {})],
        )
        self.assertEqual(
            [call for call in harness.tool_calls if call[1] == "fresh"],
            [(2, "fresh", {})],
        )

    def test_preflight_time_is_deducted_from_the_tool_deadline(self):
        harness = _McpHarness()
        adapter = McpSdkAdapter(connect_timeout=1)
        sdk_patches = self._patch_sdk(harness)
        try:
            with sdk_patches[0], sdk_patches[1]:
                adapter.call(
                    server=self.SERVER,
                    toolName="first",
                    arguments={},
                    timeoutSeconds=3,
                )
                harness.ping_delays[1] = 0.75
                harness.tool_delays[(1, "late")] = 0.5

                with self.assertRaises(RuntimeProtocolError) as raised:
                    adapter.call(
                        server=self.SERVER,
                        toolName="late",
                        arguments={},
                        timeoutSeconds=1,
                    )
                threading.Event().wait(0.7)
        finally:
            adapter.close()

        self.assertEqual(raised.exception.code, "MCP_TIMEOUT")
        self.assertEqual(
            [call for call in harness.tool_calls if call[1] == "late"],
            [(1, "late", {})],
        )
        self.assertNotIn((1, "late"), harness.tool_completed)
        self.assertIn((1, "late"), harness.tool_cancelled)

    def test_deadline_expired_ping_detaches_the_draining_session(self):
        harness = _McpHarness()
        release_long_call = threading.Event()
        long_call_started = threading.Event()
        harness.tool_blockers[(1, "long")] = release_long_call
        harness.tool_started[(1, "long")] = long_call_started
        adapter = McpSdkAdapter(connect_timeout=1)
        sdk_patches = self._patch_sdk(harness)
        long_errors = []

        def run_long_call():
            try:
                adapter.call(
                    server=self.SERVER,
                    toolName="long",
                    arguments={},
                    timeoutSeconds=4,
                )
            except Exception as exc:  # pragma: no cover - asserted through long_errors
                long_errors.append(exc)

        long_thread = threading.Thread(target=run_long_call)
        try:
            with sdk_patches[0], sdk_patches[1]:
                adapter.call(
                    server=self.SERVER,
                    toolName="first",
                    arguments={},
                    timeoutSeconds=3,
                )
                long_thread.start()
                self.assertTrue(long_call_started.wait(2))
                harness.ping_delays[1] = 2

                with self.assertRaises(RuntimeProtocolError) as short_timeout:
                    adapter.call(
                        server=self.SERVER,
                        toolName="short",
                        arguments={},
                        timeoutSeconds=1,
                    )

                fresh_result = adapter.call(
                    server=self.SERVER,
                    toolName="fresh-after-slow-ping",
                    arguments={},
                    timeoutSeconds=1,
                )
                release_long_call.set()
                long_thread.join(2)
        finally:
            release_long_call.set()
            long_thread.join(5)
            adapter.close()

        self.assertEqual(short_timeout.exception.code, "MCP_TIMEOUT")
        self.assertEqual(long_errors, [])
        self.assertFalse(fresh_result["isError"])
        self.assertEqual(
            [call for call in harness.tool_calls if call[1] == "fresh-after-slow-ping"],
            [(2, "fresh-after-slow-ping", {})],
        )

    def test_tool_interruption_detaches_session_while_sibling_call_drains(self):
        harness = _McpHarness()
        release_long_call = threading.Event()
        long_call_started = threading.Event()
        harness.tool_blockers[(1, "long")] = release_long_call
        harness.tool_started[(1, "long")] = long_call_started
        harness.tool_failures[(1, "unstable")] = OSError("stream closed")
        adapter = McpSdkAdapter(connect_timeout=1)
        sdk_patches = self._patch_sdk(harness)
        long_errors = []

        def run_long_call():
            try:
                adapter.call(
                    server=self.SERVER,
                    toolName="long",
                    arguments={},
                    timeoutSeconds=4,
                )
            except Exception as exc:  # pragma: no cover - asserted through long_errors
                long_errors.append(exc)

        long_thread = threading.Thread(target=run_long_call)
        try:
            with sdk_patches[0], sdk_patches[1]:
                adapter.call(
                    server=self.SERVER,
                    toolName="first",
                    arguments={},
                    timeoutSeconds=3,
                )
                long_thread.start()
                self.assertTrue(long_call_started.wait(2))

                with self.assertRaises(RuntimeProtocolError) as interrupted:
                    adapter.call(
                        server=self.SERVER,
                        toolName="unstable",
                        arguments={},
                        timeoutSeconds=2,
                    )

                fresh_result = adapter.call(
                    server=self.SERVER,
                    toolName="fresh-after-interruption",
                    arguments={},
                    timeoutSeconds=1,
                )
                release_long_call.set()
                long_thread.join(2)
        finally:
            release_long_call.set()
            long_thread.join(5)
            adapter.close()

        self.assertEqual(interrupted.exception.code, "MCP_SESSION_INTERRUPTED")
        self.assertEqual(long_errors, [])
        self.assertFalse(fresh_result["isError"])
        self.assertEqual(
            [call for call in harness.tool_calls if call[1] == "unstable"],
            [(1, "unstable", {})],
        )
        self.assertEqual(
            [
                call
                for call in harness.tool_calls
                if call[1] == "fresh-after-interruption"
            ],
            [(2, "fresh-after-interruption", {})],
        )

    def test_close_finishes_detached_in_flight_calls_before_stopping_loop(self):
        harness = _McpHarness()
        release_long_call = threading.Event()
        long_call_started = threading.Event()
        harness.tool_blockers[(1, "long")] = release_long_call
        harness.tool_started[(1, "long")] = long_call_started
        adapter = McpSdkAdapter(connect_timeout=1)
        sdk_patches = self._patch_sdk(harness)
        long_errors = []

        def run_long_call():
            try:
                adapter.call(
                    server=self.SERVER,
                    toolName="long",
                    arguments={},
                    timeoutSeconds=4,
                )
            except Exception as exc:  # pragma: no cover - asserted through long_errors
                long_errors.append(exc)

        long_thread = threading.Thread(target=run_long_call)
        try:
            with sdk_patches[0], sdk_patches[1]:
                adapter.call(
                    server=self.SERVER,
                    toolName="first",
                    arguments={},
                    timeoutSeconds=3,
                )
                long_thread.start()
                self.assertTrue(long_call_started.wait(2))
                harness.ping_failures[1] = OSError("retire the busy session")
                with self.assertRaises(RuntimeProtocolError):
                    adapter.call(
                        server=self.SERVER,
                        toolName="short",
                        arguments={},
                        timeoutSeconds=1,
                    )

                adapter.close()
                long_thread.join(1)
                finished_before_loop_close = not long_thread.is_alive()
        finally:
            release_long_call.set()
            long_thread.join(5)
            adapter.close()

        self.assertTrue(finished_before_loop_close)
        self.assertEqual(len(long_errors), 1)
        self.assertIsInstance(long_errors[0], RuntimeProtocolError)
        self.assertEqual(long_errors[0].code, "MCP_SESSION_INTERRUPTED")
        self.assertFalse(long_errors[0].retryable)
        self.assertEqual(long_errors[0].details, {"requestIssued": True})

    def test_discovery_failure_detaches_session_while_sibling_call_drains(self):
        harness = _McpHarness()
        release_long_call = threading.Event()
        long_call_started = threading.Event()
        harness.tool_blockers[(1, "long")] = release_long_call
        harness.tool_started[(1, "long")] = long_call_started
        adapter = McpSdkAdapter(connect_timeout=1)
        sdk_patches = self._patch_sdk(harness)
        long_errors = []

        def run_long_call():
            try:
                adapter.call(
                    server=self.SERVER,
                    toolName="long",
                    arguments={},
                    timeoutSeconds=4,
                )
            except Exception as exc:  # pragma: no cover - asserted through long_errors
                long_errors.append(exc)

        long_thread = threading.Thread(target=run_long_call)
        try:
            with sdk_patches[0], sdk_patches[1]:
                adapter.call(
                    server=self.SERVER,
                    toolName="first",
                    arguments={},
                    timeoutSeconds=3,
                )
                long_thread.start()
                self.assertTrue(long_call_started.wait(2))

                with self.assertRaises(RuntimeProtocolError) as discovery_error:
                    adapter.discover(self.SERVER)

                fresh_result = adapter.call(
                    server=self.SERVER,
                    toolName="fresh-after-discovery-error",
                    arguments={},
                    timeoutSeconds=1,
                )
                release_long_call.set()
                long_thread.join(2)
        finally:
            release_long_call.set()
            long_thread.join(5)
            adapter.close()

        self.assertEqual(discovery_error.exception.code, "MCP_OPERATION_FAILED")
        self.assertEqual(long_errors, [])
        self.assertFalse(fresh_result["isError"])
        self.assertEqual(
            [
                call
                for call in harness.tool_calls
                if call[1] == "fresh-after-discovery-error"
            ],
            [(2, "fresh-after-discovery-error", {})],
        )


if __name__ == "__main__":
    unittest.main()
