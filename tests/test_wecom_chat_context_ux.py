"""Tests for the v4.6.4 chat-context UX backend.

Covers conversation naming (peer first, never our own name), the directory /
context caches, ``before`` pagination with ``hasMore``, runtime query parameter
passthrough and the background prewarm. No real WeCom database is touched.
"""

from __future__ import annotations

import contextlib
import threading
import time
import unittest
from unittest import mock

from worker.reply_runtime import message_source as message_source_module
from worker.reply_runtime.message_source import LocalWeComMessageSource
from worker.reply_runtime.runtime import ReplyRuntime, RuntimeProtocolError
from worker.wecom import local_db


def _row(
    conversation_id: str,
    stored_name: str,
    last_time: int = 0,
    last_id: int = 0,
) -> dict:
    return {
        "conversation_id": conversation_id,
        "name": stored_name,
        "roomname_remark": "",
        "stored_name": stored_name,
        "last_message_time": last_time,
        "last_message_id": last_id,
        "is_group": conversation_id.startswith("R:"),
    }


def _directory_entry(
    conversations=None,
    *,
    member_names=None,
    user_map=None,
    accounts=None,
    identities=None,
    self_id=0,
    self_name="",
    duplicate_titles=None,
) -> dict:
    return {
        "at": time.monotonic(),
        "conversations": list(conversations or []),
        "member_names": dict(member_names or {}),
        "user_map": dict(user_map or {}),
        "accounts": dict(accounts or {}),
        "identities": dict(identities or {}),
        "self_id": self_id,
        "self_name": self_name,
        "duplicate_titles": set(duplicate_titles or set()),
    }


def _source(*, warmup_started: bool = True) -> LocalWeComMessageSource:
    source = object.__new__(LocalWeComMessageSource)
    source.config_path = None
    source._message_snapshot = object()
    source._user_map = {}
    source._member_names = {}
    source._identities = {}
    source._identity_loaded_at = time.time()
    source._cache_lock = threading.Lock()
    source._directory_cache = None
    source._context_cache = {}
    source._warmup_started = warmup_started
    return source


def _fake_formatted(raw, *_args, **_kwargs) -> dict:
    return {
        "sender": f"用户{raw.get('sender_id')}",
        "sender_id": int(raw.get("sender_id") or 0),
        "message_id": int(raw.get("message_id") or 0),
        "send_time": int(raw.get("send_time") or 0),
        "content": f"消息{raw.get('message_id')}",
    }


def _fake_normalized(raw, formatted, **_kwargs) -> dict:
    return {
        "messageId": str(formatted.get("message_id") or ""),
        "senderId": str(formatted.get("sender_id") or ""),
        "senderName": formatted.get("sender") or "",
        "sendTime": int(formatted.get("send_time") or 0),
        "contentType": "text",
        "text": formatted.get("content") or "",
        "images": [],
    }


def _message(message_id: int, send_time: int, sender_id: int = 1) -> dict:
    return {
        "message_id": message_id,
        "send_time": send_time,
        "sequence": message_id,
        "sender_id": sender_id,
    }


@contextlib.contextmanager
def _context_env(source, read_messages_mock, directory=None, rows=None):
    directory = directory or _directory_entry([_row("R:1", "客服群")])
    if read_messages_mock is None:
        read_messages_mock = mock.Mock(return_value=list(rows or []))
    with mock.patch.object(message_source_module, "load_config", return_value={}), \
        mock.patch.object(message_source_module, "read_messages", read_messages_mock), \
        mock.patch.object(
            message_source_module, "format_message", side_effect=_fake_formatted
        ), \
        mock.patch.object(
            message_source_module, "normalize_wecom_message", side_effect=_fake_normalized
        ), \
        mock.patch.object(
            message_source_module,
            "_resolve_message_db_image_paths",
            side_effect=lambda _config, _raw, message: message,
        ), \
        mock.patch.object(source, "_directory", return_value=directory):
        yield read_messages_mock


class LocalDirectoryIntegrationTests(unittest.TestCase):
    """Exercise the real SQL against in-memory session.db / user.db fixtures."""

    def setUp(self) -> None:
        import sqlite3
        import tempfile
        from pathlib import Path

        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        root = Path(self._temporary.name)
        self._session_path = root / "session.db"
        self._user_path = root / "user.db"
        self._session_path.write_bytes(b"fixture")
        self._user_path.write_bytes(b"fixture")

        session = sqlite3.connect(":memory:")
        session.row_factory = sqlite3.Row
        self.addCleanup(session.close)
        session.execute(
            "CREATE TABLE conversation_table ("
            "id TEXT PRIMARY KEY, name TEXT, roomname_remark TEXT, "
            "last_message_time INTEGER, last_message_id INTEGER, con_numeric_id INTEGER)"
        )
        session.execute(
            "CREATE TABLE conversation_user_table ("
            "conversation_id TEXT, user_id INTEGER, nick_name TEXT)"
        )
        session.execute(
            "CREATE TABLE conversation_member_nickname_table ("
            "room_id INTEGER, userid INTEGER, nickname TEXT)"
        )
        session.executemany(
            "INSERT INTO conversation_table VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("S:zhangsan_42", "张三", "", 1700000000000, 7, 1),
                ("S:zhangsan_101", "王五", "", 1699999999, 6, 2),
                ("S:zhangsan_102", "张三", "", 1699999900, 5, 3),
                ("S:zhangsan_201", "我的名字", "", 1699999800, 4, 4),
                ("S:zhangsan_202", "我的名字", "", 1699999700, 3, 5),
                ("S:zhangsan_203", "我的名字", "", 1699999600, 2, 6),
                ("S:zhangsan_301", "", "", 1699999500, 1, 7),
                ("R:1", "客服群", "客服群备注", 1699999000, 9, 8),
                ("R:2", "研发群", "", 1699998000, 8, 9),
            ],
        )
        session.executemany(
            "INSERT INTO conversation_user_table VALUES (?, ?, ?)",
            [
                ("S:zhangsan_42", 42, "李总群昵称"),
                ("S:zhangsan_42", 5, "我自己的昵称"),
                ("S:zhangsan_301", 301, "王姐"),
            ],
        )
        self._session = session

        user = sqlite3.connect(":memory:")
        user.row_factory = sqlite3.Row
        self.addCleanup(user.close)
        user.execute(
            "CREATE TABLE user_table ("
            "id INTEGER PRIMARY KEY, name TEXT, real_name TEXT, account TEXT, "
            "external_corp_name TEXT, mobile TEXT)"
        )
        user.executemany(
            "INSERT INTO user_table VALUES (?, ?, ?, ?, ?, ?)",
            [
                (5, "张三", "张三", "zhangsan", "", "13800000000"),
                (42, "李总", "李总", "lizong", "", ""),
            ],
        )
        user.execute(
            "CREATE TABLE external_user_relation_v3 ("
            "user_id INTEGER, remarks TEXT, real_remarks TEXT, corp_remark TEXT)"
        )
        self._user = user

        @contextlib.contextmanager
        def fake_connection(_config, db_name, **_kwargs):
            yield self._user if db_name == "user.db" else self._session

        patcher_connection = mock.patch.object(
            local_db, "decrypted_connection", fake_connection
        )
        patcher_db_path = mock.patch.object(
            local_db,
            "db_path",
            side_effect=lambda _config, name: (
                self._user_path if name == "user.db" else self._session_path
            ),
        )
        patcher_connection.start()
        patcher_db_path.start()
        self.addCleanup(patcher_connection.stop)
        self.addCleanup(patcher_db_path.stop)

    def test_conversation_directory_reads_rows_and_member_names(self):
        directory = local_db.load_conversation_directory({})

        self.assertEqual(len(directory["conversations"]), 9)
        self.assertEqual(
            directory["member_names"]["S:zhangsan_301"][301], "王姐"
        )
        by_id = {
            row["conversation_id"]: row for row in directory["conversations"]
        }
        self.assertEqual(by_id["S:zhangsan_42"]["last_message_time"], 1700000000)
        self.assertTrue(by_id["R:1"]["is_group"])

    def test_user_directory_maps_accounts_and_identities(self):
        directory = local_db.load_user_directory({})

        self.assertEqual(directory["user_map"][42], "李总")
        self.assertEqual(directory["accounts"]["zhangsan"], 5)
        self.assertEqual(directory["accounts"]["lizong"], 42)
        self.assertEqual(directory["identities"][5]["mobile"], "13800000000")

    def test_list_conversations_resolves_peer_names_and_groups(self):
        rows = local_db.list_conversations({}, limit=20, include_direct=True)
        by_id = {row["conversation_id"]: row["display_name"] for row in rows}

        self.assertEqual(by_id["S:zhangsan_42"], "李总")
        self.assertEqual(by_id["S:zhangsan_101"], "王五")
        self.assertEqual(by_id["S:zhangsan_102"], "私聊会话")
        for duplicated in ("S:zhangsan_201", "S:zhangsan_202", "S:zhangsan_203"):
            self.assertEqual(by_id[duplicated], "私聊会话")
        self.assertEqual(by_id["S:zhangsan_301"], "王姐")
        self.assertEqual(by_id["R:1"], "客服群备注")
        self.assertEqual(by_id["R:2"], "研发群")
        self.assertNotIn("张三", by_id.values())
        self.assertNotIn("S:zhangsan_42", by_id.values())

    def test_get_conversation_state_uses_peer_name(self):
        state = local_db.get_conversation_state({}, "S:zhangsan_42")
        self.assertEqual(state["display_name"], "李总")
        self.assertEqual(state["last_message_id"], 7)
        group = local_db.get_conversation_state({}, "R:1")
        self.assertEqual(group["display_name"], "客服群备注")

    def test_user_directory_tolerates_old_user_table_schema(self):
        import sqlite3

        user = sqlite3.connect(":memory:")
        user.row_factory = sqlite3.Row
        self.addCleanup(user.close)
        user.execute("CREATE TABLE user_table (id INTEGER, name TEXT, account TEXT)")
        user.execute("INSERT INTO user_table VALUES (5, '张三', 'zhangsan')")

        @contextlib.contextmanager
        def fake_connection(_config, _name, **_kwargs):
            yield user

        with mock.patch.object(
            local_db, "decrypted_connection", fake_connection
        ), mock.patch.object(
            local_db, "db_path", return_value=self._user_path
        ):
            directory = local_db.load_user_directory({})

        self.assertEqual(directory["user_map"][5], "张三")
        self.assertEqual(directory["accounts"]["zhangsan"], 5)
        self.assertEqual(directory["identities"][5]["display_name"], "张三")

    def test_conversation_directory_tolerates_missing_member_tables(self):
        import sqlite3

        session = sqlite3.connect(":memory:")
        session.row_factory = sqlite3.Row
        self.addCleanup(session.close)
        session.execute(
            "CREATE TABLE conversation_table ("
            "id TEXT, name TEXT, roomname_remark TEXT, "
            "last_message_time INTEGER, last_message_id INTEGER)"
        )
        session.execute(
            "INSERT INTO conversation_table VALUES ('R:1', '研发群', '', 100, 2)"
        )

        @contextlib.contextmanager
        def fake_connection(_config, _name, **_kwargs):
            yield session

        with mock.patch.object(
            local_db, "decrypted_connection", fake_connection
        ), mock.patch.object(
            local_db, "db_path", return_value=self._session_path
        ):
            directory = local_db.load_conversation_directory({})

        self.assertEqual(len(directory["conversations"]), 1)
        self.assertEqual(directory["member_names"], {})


class ConversationNameTests(unittest.TestCase):
    def test_peer_name_wins_over_stored_title(self):
        name = local_db.conversation_display_name(
            "S:zhangsan_42",
            "客户备注",
            user_map={5: "张三", 42: "李总"},
            accounts={"zhangsan": 5},
        )
        self.assertEqual(name, "李总")

    def test_member_nickname_is_the_peer_fallback(self):
        name = local_db.conversation_display_name(
            "S:zhangsan_42",
            "客户备注",
            member_names={"S:zhangsan_42": {42: "王姐"}},
            accounts={"zhangsan": 5},
        )
        self.assertEqual(name, "王姐")

    def test_own_title_is_filtered_via_account_mapping(self):
        accounts = {"zhangsan": 5}
        self.assertEqual(
            local_db.conversation_display_name(
                "S:zhangsan_42",
                "张三",
                user_map={5: "张三"},
                accounts=accounts,
            ),
            "私聊会话",
        )
        self.assertEqual(
            local_db.conversation_display_name(
                "S:zn_42",
                "张三",
                user_map={5: "张三"},
                accounts={"zn": 5},
            ),
            "私聊会话",
        )

    def test_duplicate_title_fallback_discards_own_name(self):
        conversations = [
            _row("S:zn_1", "张三"),
            _row("S:zn_2", "张三"),
            _row("S:zn_3", "张三"),
            _row("S:zn_4", "李总"),
            _row("R:1", "张三"),
        ]
        duplicated = local_db.duplicated_direct_titles(conversations)
        self.assertEqual(duplicated, {"张三"})
        self.assertEqual(
            local_db.conversation_display_name(
                "S:zn_1", "张三", duplicate_titles=duplicated
            ),
            "私聊会话",
        )
        self.assertEqual(
            local_db.conversation_display_name("S:zn_4", "李总"),
            "李总",
        )

    def test_unresolved_direct_chat_never_shows_raw_id(self):
        self.assertEqual(
            local_db.conversation_display_name("S:zn_7", ""),
            "私聊会话",
        )
        self.assertEqual(
            local_db.conversation_display_name("S:zn_7", "S:zn_7"),
            "私聊会话",
        )

    def test_group_name_is_unchanged(self):
        self.assertEqual(
            local_db.conversation_display_name("R:1", "客服群"), "客服群"
        )
        self.assertEqual(local_db.conversation_display_name("R:1", ""), "")

    def test_list_conversations_uses_corrected_names(self):
        conversations = [
            _row("S:zn_42", "张三", 300, 42),
            _row("S:zn_43", "王五(外部)", 200, 43),
            _row("R:1", "客服群", 100, 1),
        ]
        directory = {"conversations": conversations, "member_names": {}}
        user_directory = {
            "user_map": {5: "张三", 42: "李总", 43: "王五"},
            "accounts": {"zn": 5},
            "identities": {},
        }
        with mock.patch.object(
            local_db, "load_conversation_directory", return_value=directory
        ), mock.patch.object(
            local_db, "load_user_directory", return_value=user_directory
        ):
            rows = local_db.list_conversations({}, limit=10, include_direct=True)

        by_id = {row["conversation_id"]: row for row in rows}
        self.assertEqual(by_id["S:zn_42"]["display_name"], "李总")
        self.assertEqual(by_id["S:zn_43"]["display_name"], "王五")
        self.assertEqual(by_id["R:1"]["display_name"], "客服群")
        self.assertNotIn("张三", [row["display_name"] for row in rows])
        self.assertEqual([row["conversation_id"] for row in rows], [
            "S:zn_42", "S:zn_43", "R:1"
        ])

    def test_get_conversation_state_reuses_corrected_names(self):
        state_row = {
            "id": "S:zn_42",
            "name": "张三",
            "roomname_remark": "",
            "last_message_time": 100,
            "last_message_id": 7,
        }

        class _FakeCursor:
            def __init__(self, rows):
                self._rows = list(rows)

            def fetchone(self):
                return self._rows[0] if self._rows else None

            def fetchall(self):
                return list(self._rows)

        class _FakeConnection:
            def execute(self, sql, _params=()):
                text = str(sql)
                if "FROM conversation_table WHERE id = ?" in text:
                    return _FakeCursor([state_row])
                if "FROM conversation_table" in text:
                    return _FakeCursor([state_row])
                return _FakeCursor([])

        @contextlib.contextmanager
        def fake_connection(_config, _name, **_kwargs):
            yield _FakeConnection()

        with mock.patch.object(
            local_db, "decrypted_connection", fake_connection
        ), mock.patch.object(
            local_db,
            "load_user_directory",
            return_value={
                "user_map": {5: "张三", 42: "李总"},
                "accounts": {"zn": 5},
                "identities": {},
            },
        ), mock.patch.object(
            local_db, "table_exists", return_value=False
        ):
            state = local_db.get_conversation_state({}, "S:zn_42")

        self.assertEqual(state["display_name"], "李总")
        self.assertEqual(state["last_message_id"], 7)

    def test_recent_conversations_never_shows_own_name(self):
        source = _source()
        rows = [
            _row("S:zhangsan_42", "张三", 300, 42),
            _row("S:zhangsan_43", "", 200, 43),
            _row("R:1", "客服群", 100, 1),
        ]
        directory = _directory_entry(
            rows,
            user_map={5: "张三", 42: "李总", 43: "王姐"},
            accounts={"zhangsan": 5},
            self_id=5,
            self_name="张三",
        )
        with mock.patch.object(source, "_directory", return_value=directory), \
            mock.patch.object(source, "_schedule_warmup"):
            result = source.recent_conversations(10)

        names = [chat["displayName"] for chat in result["chats"]]
        self.assertEqual(names, ["李总", "王姐", "客服群"])
        self.assertNotIn("张三", names)
        self.assertEqual(result["selfSenderId"], "5")
        self.assertEqual(result["selfSenderName"], "张三")


class DirectoryCacheTests(unittest.TestCase):
    @staticmethod
    def _full_window() -> list[dict]:
        """Exactly one page, so the initial read does not issue the older probe."""

        return [_message(index + 1, 1000 - index) for index in range(20)]

    def _directory_patches(self):
        directory = {
            "conversations": [_row("R:1", "客服群", 100, 1)],
            "member_names": {},
        }
        return directory

    def test_directory_cache_reuses_one_read(self):
        source = _source()
        directory = self._directory_patches()
        with mock.patch.object(
            message_source_module,
            "load_conversation_directory",
            return_value=directory,
        ) as load_directory, mock.patch.object(
            message_source_module, "load_user_directory", return_value={}
        ), mock.patch.object(source, "_schedule_warmup"):
            first = source.recent_conversations(10)
            second = source.recent_conversations(10)

        self.assertEqual(load_directory.call_count, 1)
        self.assertEqual(first["chats"][0]["conversationId"], "R:1")
        self.assertEqual(first, second)

    def test_directory_cache_expires_after_ttl(self):
        source = _source()
        directory = self._directory_patches()
        with mock.patch.object(
            message_source_module,
            "load_conversation_directory",
            return_value=directory,
        ) as load_directory, mock.patch.object(
            message_source_module, "load_user_directory", return_value={}
        ), mock.patch.object(source, "_schedule_warmup"):
            source.recent_conversations(10)
            source._directory_cache["at"] -= source._DIRECTORY_TTL_SECONDS + 1
            source.recent_conversations(10)

        self.assertEqual(load_directory.call_count, 2)

    def test_directory_refresh_bypasses_cache(self):
        source = _source()
        directory = self._directory_patches()
        with mock.patch.object(
            message_source_module,
            "load_conversation_directory",
            return_value=directory,
        ) as load_directory, mock.patch.object(
            message_source_module, "load_user_directory", return_value={}
        ), mock.patch.object(source, "_schedule_warmup"):
            source.recent_conversations(10, refresh=True)
            source.recent_conversations(10, refresh=True)

        self.assertEqual(load_directory.call_count, 2)

    def test_context_cache_reuses_one_read(self):
        source = _source()
        rows = self._full_window()
        with _context_env(source, None, rows=rows) as reader:
            first = source.read_context("R:1")
            second = source.read_context("R:1")

        self.assertEqual(reader.call_count, 1)
        self.assertEqual(first, second)

    def test_context_cache_expires_after_ttl(self):
        source = _source()
        rows = self._full_window()
        with _context_env(source, None, rows=rows) as reader:
            source.read_context("R:1")
            for key, (at, payload) in list(source._context_cache.items()):
                source._context_cache[key] = (
                    at - source._CONTEXT_TTL_SECONDS - 1,
                    payload,
                )
            source.read_context("R:1")

        self.assertEqual(reader.call_count, 2)

    def test_context_refresh_bypasses_cache(self):
        source = _source()
        rows = self._full_window()
        with _context_env(source, None, rows=rows) as reader:
            source.read_context("R:1", refresh=True)
            source.read_context("R:1", refresh=True)

        self.assertEqual(reader.call_count, 2)

    def test_context_cache_keeps_pages_apart(self):
        source = _source()
        first_rows = self._full_window()
        second_rows = [_message(5, 200)]
        side_effect = [first_rows, second_rows, second_rows]
        reader = mock.Mock(side_effect=side_effect)
        with _context_env(source, reader):
            source.read_context("R:1")
            source.read_context("R:1", before=200)
            source.read_context("R:1", before=200)

        self.assertEqual(reader.call_count, 2)

    def test_read_context_does_not_decrypt_session_db_for_the_name(self):
        source = _source()
        directory = _directory_entry([_row("R:1", "客服群")])
        with _context_env(source, None, rows=[_message(1, 100)], directory=directory), \
            mock.patch.object(
                message_source_module,
                "get_conversation_state",
                side_effect=AssertionError("read_context must use the directory cache"),
            ):
            source.read_context("R:1")

    def test_recent_and_context_share_one_directory_read(self):
        source = _source()
        conversations = [_row("R:1", "客服群", 100, 1)]
        directory = {"conversations": conversations, "member_names": {}}
        with mock.patch.object(
            message_source_module,
            "load_conversation_directory",
            return_value=directory,
        ) as load_directory, mock.patch.object(
            message_source_module, "load_user_directory", return_value={}
        ), mock.patch.object(
            message_source_module, "load_config", return_value={}
        ), mock.patch.object(
            message_source_module, "read_messages", return_value=[_message(1, 100)]
        ), mock.patch.object(
            message_source_module, "format_message", side_effect=_fake_formatted
        ), mock.patch.object(
            message_source_module, "normalize_wecom_message", side_effect=_fake_normalized
        ), mock.patch.object(
            message_source_module,
            "_resolve_message_db_image_paths",
            side_effect=lambda _config, _raw, message: message,
        ), mock.patch.object(source, "_schedule_warmup"):
            source.recent_conversations(10)
            source.read_context("R:1")

        self.assertEqual(load_directory.call_count, 1)


class ContextPaginationTests(unittest.TestCase):
    def test_first_page_requests_limit_plus_one_and_sets_has_more(self):
        source = _source()
        rows = [_message(index + 1, 1000 - index) for index in range(21)]
        with _context_env(source, None, rows=rows) as reader:
            result = source.read_context("R:1")

        self.assertEqual(reader.call_args.kwargs["limit"], 21)
        self.assertTrue(result["hasMore"])
        self.assertEqual(len(result["messages"]), 20)
        self.assertEqual(result["messages"][0]["messageId"], "20")
        self.assertEqual(result["messages"][-1]["messageId"], "1")
        times = [message["sendTime"] for message in result["messages"]]
        self.assertEqual(times, sorted(times))
        self.assertFalse(result["windowFallback"])

    def test_first_page_without_extra_row_has_no_more(self):
        source = _source()
        rows = [_message(index + 1, 1000 - index) for index in range(20)]
        with _context_env(source, None, rows=rows) as reader:
            result = source.read_context("R:1")

        self.assertEqual(reader.call_args.kwargs["limit"], 21)
        self.assertFalse(result["hasMore"])
        self.assertEqual(len(result["messages"]), 20)

    def test_first_page_has_more_when_older_messages_exist_past_the_window(self):
        source = _source()
        window_rows = [_message(3, 300), _message(2, 200), _message(1, 100)]
        older_row = _message(0, 50)
        calls = []

        def read_messages(_config, _cid, start, end, *, limit, snapshot=None):
            calls.append((start, end, limit))
            if limit > 1:
                return list(window_rows)
            return [older_row]

        with _context_env(source, mock.Mock(side_effect=read_messages)):
            result = source.read_context("R:1", limit=5, window_seconds=21600)

        self.assertTrue(result["hasMore"])
        self.assertFalse(result["windowFallback"])
        self.assertEqual(len(result["messages"]), 3)
        self.assertEqual(len(calls), 2)
        window_start, _window_end, window_limit = calls[0]
        probe_start, probe_end, probe_limit = calls[1]
        self.assertEqual(window_limit, 6)
        self.assertEqual(probe_limit, 1)
        self.assertEqual(probe_end, window_start - 1)
        self.assertLess(probe_start, probe_end)

    def test_first_page_has_more_false_without_older_messages(self):
        source = _source()
        window_rows = [_message(1, 100)]

        def read_messages(_config, _cid, start, end, *, limit, snapshot=None):
            return list(window_rows) if limit > 1 else []

        with _context_env(source, mock.Mock(side_effect=read_messages)):
            result = source.read_context("R:1", limit=5)

        self.assertFalse(result["hasMore"])
        self.assertFalse(result["windowFallback"])
        self.assertEqual(len(result["messages"]), 1)

    def test_full_window_page_does_not_probe_past_the_window(self):
        source = _source()
        rows = [_message(index + 1, 1000 - index) for index in range(3)]
        reader = mock.Mock(return_value=rows)
        with _context_env(source, reader):
            result = source.read_context("R:1", limit=3)

        # Exactly ``limit`` messages in the window: paging upwards would only
        # repeat the same boundary page, so no probe is issued.
        self.assertEqual(reader.call_count, 1)
        self.assertFalse(result["hasMore"])
        self.assertEqual(len(result["messages"]), 3)

    def test_default_limit_is_twenty(self):
        source = _source()
        with _context_env(source, None, rows=[]) as reader:
            result = source.read_context("R:1")

        self.assertEqual(reader.call_args.kwargs["limit"], 21)
        self.assertEqual(result["messages"], [])

    def test_older_page_uses_before_as_inclusive_end_and_30_day_window(self):
        source = _source()
        before = 1_700_000_000
        rows = [_message(1, before - 100)]
        reader = mock.Mock(return_value=rows)
        with _context_env(source, reader):
            result = source.read_context("R:1", before=before)

        args = reader.call_args.args
        self.assertEqual(args[3], before)  # end_ts is the before boundary
        self.assertEqual(args[2], before - 30 * 86400)  # 30-day window
        self.assertFalse(result["windowFallback"])
        self.assertFalse(result["hasMore"])
        self.assertEqual([message["messageId"] for message in result["messages"]], ["1"])

    def test_before_boundary_keeps_repeated_send_times(self):
        source = _source()
        rows = [
            _message(5, 500),
            _message(4, 500),
            _message(3, 500),
            _message(2, 400),
        ]
        reader = mock.Mock(return_value=rows)
        with _context_env(source, reader):
            result = source.read_context("R:1", before=500, limit=3)

        self.assertEqual(reader.call_args.kwargs["limit"], 4)
        self.assertEqual(reader.call_args.args[3], 500)
        self.assertTrue(result["hasMore"])
        # All boundary messages are kept and ordered by (time, sequence).
        self.assertEqual(
            [message["messageId"] for message in result["messages"]],
            ["3", "4", "5"],
        )

    def test_older_page_has_more_when_extra_row_is_returned(self):
        source = _source()
        rows = [_message(1, 400), _message(2, 399)]
        reader = mock.Mock(return_value=rows)
        with _context_env(source, reader):
            result = source.read_context("R:1", before=500, limit=1)

        self.assertTrue(result["hasMore"])
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(result["messages"][0]["messageId"], "1")

    def test_window_fallback_marks_quiet_conversation(self):
        source = _source()
        fallback_rows = [_message(1, 100)]
        reader = mock.Mock(side_effect=[[], fallback_rows])
        with _context_env(source, reader):
            result = source.read_context("S:zn_42")

        self.assertTrue(result["windowFallback"])
        self.assertFalse(result["hasMore"])
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(result["messages"][0]["messageId"], "1")

    def test_window_fallback_has_more_when_older_messages_exist(self):
        source = _source()
        fallback_rows = [_message(3, 300), _message(2, 200), _message(1, 100)]
        calls = []

        def read_messages(_config, _cid, start, end, *, limit, snapshot=None):
            calls.append(limit)
            return [] if len(calls) == 1 else list(fallback_rows)

        with _context_env(source, mock.Mock(side_effect=read_messages)):
            result = source.read_context("S:zn_42", limit=2)

        self.assertTrue(result["windowFallback"])
        self.assertTrue(result["hasMore"])
        self.assertEqual(len(result["messages"]), 2)


class RuntimeChatQueryTests(unittest.TestCase):
    def _runtime(self, source) -> ReplyRuntime:
        runtime = object.__new__(ReplyRuntime)
        runtime.message_source = source
        runtime.config_path = None
        return runtime

    def test_chat_recent_passes_refresh_and_returns_identity(self):
        calls = []

        class _Source:
            def recent_conversations(self, limit, *, refresh=False):
                calls.append((limit, refresh))
                return {
                    "chats": [{"conversationId": "R:1", "displayName": "群"}],
                    "selfSenderId": "5",
                    "selfSenderName": "张三",
                }

        result = self._runtime(_Source())._query_chat_recent(
            {"limit": 25, "refresh": True}
        )
        self.assertEqual(calls, [(25, True)])
        self.assertEqual(result["chats"][0]["conversationId"], "R:1")
        self.assertEqual(result["selfSenderId"], "5")
        self.assertEqual(result["selfSenderName"], "张三")

    def test_chat_context_passes_before_limit_and_refresh(self):
        calls = []

        class _Source:
            def read_context(
                self, conversation_id, *, limit, window_seconds, before=None, refresh=False
            ):
                calls.append(
                    (conversation_id, limit, window_seconds, before, refresh)
                )
                return {
                    "conversationId": conversation_id,
                    "displayName": "群",
                    "isGroup": True,
                    "windowFallback": False,
                    "hasMore": True,
                    "selfSenderId": "5",
                    "messages": [],
                }

        result = self._runtime(_Source())._query_chat_context(
            {
                "conversationId": "R:1",
                "limit": 20,
                "windowSeconds": 7200,
                "before": 1700000000,
                "refresh": True,
            }
        )
        self.assertEqual(
            calls, [("R:1", 20, 7200, 1700000000, True)]
        )
        self.assertTrue(result["hasMore"])
        self.assertEqual(result["selfSenderId"], "5")

    def test_chat_context_defaults_match_v464_contract(self):
        calls = []

        class _Source:
            def read_context(
                self, conversation_id, *, limit, window_seconds, before=None, refresh=False
            ):
                calls.append((limit, window_seconds, before, refresh))
                return {"conversationId": conversation_id, "messages": []}

        result = self._runtime(_Source())._query_chat_context(
            {"conversationId": "R:1"}
        )
        self.assertEqual(calls, [(20, 21600, None, False)])
        self.assertTrue(result["windowFallback"] is False)
        self.assertTrue(result["hasMore"] is False)
        self.assertEqual(result["selfSenderId"], "")

    def test_chat_context_rejects_missing_conversation(self):
        runtime = self._runtime(object())
        with self.assertRaises(RuntimeProtocolError):
            runtime._query_chat_context({})


class WarmupTests(unittest.TestCase):
    def test_recent_conversations_starts_warmup_once(self):
        source = _source(warmup_started=False)
        rows = [_row("R:1", "客服群", 300, 1), _row("R:2", "销售群", 200, 2)]
        directory = _directory_entry(rows)
        spawned = []

        def fake_spawn(target, argument, *, name):
            spawned.append((target, argument, name))

        with mock.patch.object(source, "_directory", return_value=directory), \
            mock.patch.object(
                message_source_module, "_spawn_background_thread", side_effect=fake_spawn
            ):
            source.recent_conversations(10)
            source.recent_conversations(10)

        self.assertEqual(len(spawned), 1)
        target, argument, name = spawned[0]
        self.assertEqual(target, source.warmup)
        self.assertEqual(argument, "R:1")
        self.assertTrue(name)
        self.assertTrue(source._warmup_started)

    def test_warmup_spawn_failure_does_not_break_chat_list(self):
        source = _source(warmup_started=False)
        directory = _directory_entry([_row("R:1", "客服群", 300, 1)])
        with mock.patch.object(source, "_directory", return_value=directory), \
            mock.patch.object(
                message_source_module,
                "_spawn_background_thread",
                side_effect=RuntimeError("thread unavailable"),
            ):
            result = source.recent_conversations(10)

        self.assertEqual(result["chats"][0]["conversationId"], "R:1")

    def test_warmup_reads_messages_and_swallows_failures(self):
        source = _source()
        with mock.patch.object(
            message_source_module, "load_config", return_value={}
        ), mock.patch.object(
            message_source_module, "read_messages", side_effect=OSError("busy")
        ):
            self.assertIsNone(source.warmup("R:1"))

        reader = mock.Mock(return_value=[])
        with mock.patch.object(
            message_source_module, "load_config", return_value={}
        ), mock.patch.object(message_source_module, "read_messages", reader):
            source.warmup("R:1")

        self.assertIs(reader.call_args.kwargs["snapshot"], source._message_snapshot)
        self.assertEqual(reader.call_args.kwargs["limit"], 40)

    def test_warmup_uses_newest_conversation_when_not_provided(self):
        source = _source()
        directory = _directory_entry(
            [_row("R:1", "旧群", 100, 1), _row("R:2", "新群", 300, 2)]
        )
        reader = mock.Mock(return_value=[])
        with mock.patch.object(source, "_directory", return_value=directory), \
            mock.patch.object(message_source_module, "load_config", return_value={}), \
            mock.patch.object(message_source_module, "read_messages", reader):
            source.warmup()

        self.assertEqual(reader.call_args.args[1], "R:2")

    def test_warmup_runs_on_a_daemon_thread(self):
        source = _source()
        created = {}

        class _FakeThread:
            def __init__(self, *, target, args, name, daemon):
                created["target"] = target
                created["args"] = args
                created["name"] = name
                created["daemon"] = daemon
                created["started"] = False

            def start(self):
                created["started"] = True

        with mock.patch.object(
            message_source_module.threading, "Thread", _FakeThread
        ):
            message_source_module._spawn_background_thread(
                source.warmup, "R:1", name="warmup-test"
            )

        self.assertTrue(created["started"])
        self.assertTrue(created["daemon"])
        self.assertEqual(created["args"], ("R:1",))
        self.assertEqual(created["name"], "warmup-test")
        self.assertEqual(created["target"], source.warmup)


if __name__ == "__main__":
    unittest.main()
