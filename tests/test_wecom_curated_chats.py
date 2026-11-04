"""Tests for the v4.6.5 curated chat list commands/catalog and multi-message reply.

Covers the runtime SQLite table (idempotent creation), ``chat.chats.*`` write
commands, ``chat.catalog`` / ``chat.recent`` query contracts and the
``focusMessages`` prompt/fallback path of the reply assistant. No real WeCom
database is touched.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

from worker.reply_assistant import model as assistant_model
from worker.reply_assistant import prompts as assistant_prompts
from worker.reply_assistant.actions import handle_assistant_action
from worker.reply_runtime.errors import RuntimeProtocolError
from worker.reply_runtime.runtime import ReplyRuntime
from worker.reply_runtime.store import RuntimeStore


def _chat(
    conversation_id: str,
    display_name: str,
    last_message_time: int,
    last_message_id: int,
    *,
    is_group: bool | None = None,
) -> dict:
    return {
        "conversationId": conversation_id,
        "displayName": display_name,
        "lastMessageTime": last_message_time,
        "lastMessageId": last_message_id,
        "isGroup": (
            conversation_id.startswith("R:") if is_group is None else is_group
        ),
    }


DEFAULT_CHATS = [
    _chat("R:100", "研发群", 3000, 30),
    _chat("S:5_42", "李总", 2000, 20),
    _chat("R:200", "客服群", 1000, 10),
]


class _FakeChatSource:
    """Directory double exposing the message_source chat helpers."""

    def __init__(self, chats=None):
        self.chats = [dict(chat) for chat in (chats or DEFAULT_CHATS)]
        self.invalidations = 0
        self.lookup_calls: list[list[str]] = []
        self.catalog_calls = 0

    @staticmethod
    def _payload(chats) -> dict:
        return {
            "chats": [dict(chat) for chat in chats],
            "selfSenderId": "5",
            "selfSenderName": "张三",
        }

    def conversation_catalog(self, *, refresh=False):
        self.catalog_calls += 1
        return self._payload(self.chats)

    def lookup_conversations(self, conversation_ids):
        self.lookup_calls.append(list(conversation_ids))
        wanted = {str(value) for value in conversation_ids}
        return self._payload(
            [chat for chat in self.chats if chat["conversationId"] in wanted]
        )

    def recent_conversations(self, limit=40, *, refresh=False):
        return self._payload(self.chats[:limit])

    def invalidate_caches(self):
        self.invalidations += 1


class CuratedChatCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.database_path = Path(self._temporary.name) / "runtime.sqlite3"
        self.source = _FakeChatSource()
        self.runtime = ReplyRuntime(
            self.database_path, message_source=self.source, autostart=False
        )
        self.addCleanup(self.runtime.store.close)

    def _execute(
        self,
        body: dict,
        *,
        expected_revision: int | None = None,
        command_id: str | None = None,
    ) -> dict:
        if expected_revision is None:
            expected_revision = self.runtime.store.revision()
        return self.runtime.execute(
            {
                "protocolVersion": 1,
                "commandId": command_id or f"cmd-{uuid.uuid4()}",
                "expectedRevision": expected_revision,
                "body": body,
            }
        )

    def _add(self, *chats: dict, **kwargs) -> dict:
        return self._execute(
            {"kind": "chat.chats.add", "chats": list(chats)}, **kwargs
        )

    def _recent(self) -> dict:
        return self.runtime.query({"kind": "chat.recent"})

    def _catalog(self, **query) -> dict:
        return self.runtime.query({"kind": "chat.catalog", **query})


class StoreMigrationTests(unittest.TestCase):
    def test_schema_creation_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            first = RuntimeStore(path)
            first.close()
            second = RuntimeStore(path)

            try:
                tables = {
                    str(row[0])
                    for row in second.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                self.assertIn("reply_assistant_chats", tables)
                second.connection.execute(
                    "INSERT INTO reply_assistant_chats(conversation_id, alias, added_at) "
                    "VALUES ('R:1', '', 1.0)"
                )
                row = second.connection.execute(
                    "SELECT conversation_id FROM reply_assistant_chats"
                ).fetchone()
                self.assertEqual(row["conversation_id"], "R:1")
            finally:
                second.close()

    def test_curated_rows_survive_a_store_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            store = RuntimeStore(path)
            with store.transaction() as db:
                db.execute(
                    "INSERT INTO reply_assistant_chats(conversation_id, alias, added_at) "
                    "VALUES ('R:100', '研发群别名', 10.0)"
                )
            store.close()

            reopened = RuntimeStore(path)
            try:
                row = reopened.connection.execute(
                    "SELECT alias FROM reply_assistant_chats WHERE conversation_id = 'R:100'"
                ).fetchone()
                self.assertEqual(row["alias"], "研发群别名")
            finally:
                reopened.close()


class CuratedChatCommandTests(CuratedChatCase):
    def test_add_returns_new_revision_and_invalidates_caches(self):
        result = self._add({"conversationId": "R:100"})

        self.assertEqual(result, {"revision": 1})
        self.assertEqual(self.runtime.store.revision(), 1)
        self.assertEqual(self.source.invalidations, 1)
        self.assertEqual(self._recent()["chats"][0]["conversationId"], "R:100")

    def test_duplicate_add_without_alias_keeps_existing_alias(self):
        self._add({"conversationId": "R:100", "alias": "研发"})
        self._add({"conversationId": "R:100"})

        chats = self._recent()["chats"]
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["alias"], "研发")
        self.assertEqual(chats[0]["displayName"], "研发")

    def test_duplicate_add_with_alias_updates_the_alias(self):
        self._add({"conversationId": "R:100", "alias": "旧别名"})
        self._add({"conversationId": "R:100", "alias": "新别名"})

        chats = self._recent()["chats"]
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["alias"], "新别名")

    def test_add_deduplicates_the_array_by_conversation_id(self):
        self._add(
            {"conversationId": "R:100"},
            {"conversationId": "R:100", "alias": "忽略"},
            {"conversationId": "R:200"},
        )

        rows = self.runtime.store.connection.execute(
            "SELECT conversation_id, alias FROM reply_assistant_chats ORDER BY conversation_id"
        ).fetchall()
        self.assertEqual([row["conversation_id"] for row in rows], ["R:100", "R:200"])

    def test_rename_sets_and_clears_alias(self):
        self._add({"conversationId": "R:100"})
        self._execute(
            {"kind": "chat.chats.rename", "conversationId": "R:100", "alias": " 研发对接 "}
        )

        chat = self._recent()["chats"][0]
        self.assertEqual(chat["alias"], "研发对接")
        self.assertEqual(chat["displayName"], "研发对接")

        self._execute(
            {"kind": "chat.chats.rename", "conversationId": "R:100", "alias": "   "}
        )
        chat = self._recent()["chats"][0]
        self.assertEqual(chat["alias"], "")
        self.assertEqual(chat["displayName"], "研发群")

    def test_rename_requires_an_added_chat(self):
        with self.assertRaises(RuntimeProtocolError) as raised:
            self._execute(
                {"kind": "chat.chats.rename", "conversationId": "R:100", "alias": "x"}
            )
        self.assertEqual(raised.exception.code, "INVALID_CHAT")

    def test_remove_deletes_chats_and_is_idempotent_for_unknown_ids(self):
        self._add(
            {"conversationId": "R:100"}, {"conversationId": "S:5_42"}
        )
        self._execute(
            {"kind": "chat.chats.remove", "conversationIds": ["R:100", "R:100"]}
        )

        chats = self._recent()["chats"]
        self.assertEqual(
            [chat["conversationId"] for chat in chats], ["S:5_42"]
        )
        self._execute(
            {"kind": "chat.chats.remove", "conversationIds": ["R:999"]}
        )
        self.assertEqual(len(self._recent()["chats"]), 1)

    def test_invalid_conversation_ids_are_rejected(self):
        for value in ("", "R100", "T:1", "X:123"):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeProtocolError) as raised:
                    self._add({"conversationId": value})
                self.assertEqual(raised.exception.code, "INVALID_CHAT")
        with self.assertRaises(RuntimeProtocolError) as raised:
            self._add({"conversationId": "R:" + "1" * 200})
        self.assertEqual(raised.exception.code, "INVALID_CHAT")

    def test_alias_length_is_limited_to_forty_characters(self):
        self._add({"conversationId": "R:100", "alias": "a" * 40})
        with self.assertRaises(RuntimeProtocolError) as raised:
            self._add({"conversationId": "R:200", "alias": "a" * 41})
        self.assertEqual(raised.exception.code, "INVALID_CHAT")

    def test_invalid_command_shapes_are_rejected(self):
        with self.assertRaises(RuntimeProtocolError) as raised:
            self._execute({"kind": "chat.chats.add", "chats": []})
        self.assertEqual(raised.exception.code, "INVALID_CHAT")
        with self.assertRaises(RuntimeProtocolError):
            self._execute({"kind": "chat.chats.add", "chats": ["R:100"]})
        with self.assertRaises(RuntimeProtocolError):
            self._execute({"kind": "chat.chats.remove", "conversationIds": []})

    def test_revision_conflict_is_reported(self):
        self._add({"conversationId": "R:100"})
        with self.assertRaises(RuntimeProtocolError) as raised:
            self._add({"conversationId": "R:200"}, expected_revision=0)
        self.assertEqual(raised.exception.code, "REVISION_CONFLICT")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.details["actualRevision"], 1)

    def test_command_id_is_idempotent(self):
        command_id = f"cmd-{uuid.uuid4()}"
        revision = self.runtime.store.revision()
        first = self._add(
            {"conversationId": "R:100"},
            command_id=command_id,
            expected_revision=revision,
        )
        second = self._add(
            {"conversationId": "R:100"},
            command_id=command_id,
            expected_revision=revision,
        )

        self.assertEqual(first, second)
        self.assertEqual(self.runtime.store.revision(), 1)
        self.assertEqual(self.source.invalidations, 1)

    def test_commands_are_visible_to_catalog_and_recent_immediately(self):
        catalog_before = self._catalog()
        self.assertTrue(all(not chat["added"] for chat in catalog_before["chats"]))
        self.assertEqual(self._recent()["chats"], [])

        self._add({"conversationId": "R:100", "alias": "研发"})

        catalog = self._catalog()
        by_id = {chat["conversationId"]: chat for chat in catalog["chats"]}
        self.assertTrue(by_id["R:100"]["added"])
        self.assertEqual(by_id["R:100"]["alias"], "研发")
        self.assertFalse(by_id["R:200"]["added"])
        recent = self._recent()
        self.assertEqual(recent["chats"][0]["conversationId"], "R:100")
        self.assertEqual(recent["revision"], self.runtime.store.revision())


class CuratedChatQueryTests(CuratedChatCase):
    def test_recent_returns_only_added_chats_sorted_and_with_revision(self):
        self._add(
            {"conversationId": "R:200"},
            {"conversationId": "S:5_42"},
            {"conversationId": "R:100"},
        )

        result = self._recent()

        self.assertEqual(
            [chat["conversationId"] for chat in result["chats"]],
            ["R:100", "S:5_42", "R:200"],
        )
        self.assertEqual(result["revision"], self.runtime.store.revision())
        self.assertEqual(result["selfSenderId"], "5")
        self.assertEqual(result["selfSenderName"], "张三")
        for chat in result["chats"]:
            self.assertTrue(chat["available"])
            self.assertTrue(chat["isGroup"] == chat["conversationId"].startswith("R:"))

    def test_recent_sorts_ties_by_last_message_id(self):
        self.source.chats = [
            _chat("R:1", "A", 3000, 10),
            _chat("R:2", "B", 3000, 30),
            _chat("R:3", "C", 2000, 99),
        ]
        self._add(
            {"conversationId": "R:1"},
            {"conversationId": "R:2"},
            {"conversationId": "R:3"},
        )

        self.assertEqual(
            [chat["conversationId"] for chat in self._recent()["chats"]],
            ["R:2", "R:1", "R:3"],
        )

    def test_recent_keeps_missing_chats_as_unavailable(self):
        self._add(
            {"conversationId": "R:999", "alias": "已删除的群"},
            {"conversationId": "R:100"},
        )

        result = self._recent()
        by_id = {chat["conversationId"]: chat for chat in result["chats"]}
        missing = by_id["R:999"]
        self.assertFalse(missing["available"])
        self.assertEqual(missing["displayName"], "已删除的群")
        self.assertEqual(missing["alias"], "已删除的群")
        self.assertEqual(missing["lastMessageTime"], 0)
        self.assertEqual(missing["lastMessageId"], 0)
        self.assertTrue(missing["isGroup"])
        self.assertTrue(by_id["R:100"]["available"])

    def test_recent_missing_chat_without_alias_shows_placeholder(self):
        self._add({"conversationId": "S:5_999"})

        chat = self._recent()["chats"][0]
        self.assertFalse(chat["available"])
        self.assertEqual(chat["displayName"], "会话不可用")
        self.assertEqual(chat["alias"], "")
        self.assertFalse(chat["isGroup"])

    def test_empty_curated_list_returns_no_chats_and_skips_directory_lookup(self):
        result = self._recent()

        self.assertEqual(result["chats"], [])
        self.assertEqual(result["revision"], 0)
        self.assertEqual(result["selfSenderId"], "")
        self.assertEqual(result["selfSenderName"], "")
        self.assertEqual(self.source.lookup_calls, [])
        self.assertEqual(self.source.catalog_calls, 0)

    def test_catalog_lists_all_directory_chats_with_added_and_alias(self):
        self._add({"conversationId": "S:5_42", "alias": "VIP 李总"})

        result = self._catalog()

        self.assertEqual(
            [chat["conversationId"] for chat in result["chats"]],
            ["R:100", "S:5_42", "R:200"],
        )
        by_id = {chat["conversationId"]: chat for chat in result["chats"]}
        self.assertTrue(by_id["S:5_42"]["added"])
        self.assertEqual(by_id["S:5_42"]["alias"], "VIP 李总")
        self.assertFalse(by_id["R:100"]["added"])
        self.assertEqual(by_id["R:100"]["alias"], "")
        self.assertEqual(result["revision"], self.runtime.store.revision())
        self.assertEqual(result["selfSenderName"], "张三")

    def test_catalog_search_is_case_insensitive_over_name_alias_and_id(self):
        self._add({"conversationId": "S:5_42", "alias": "VIP Customer"})

        by_alias = self._catalog(search="vip customer")
        self.assertEqual(
            [chat["conversationId"] for chat in by_alias["chats"]], ["S:5_42"]
        )

        by_name = self._catalog(search="研发")
        self.assertEqual(
            [chat["conversationId"] for chat in by_name["chats"]], ["R:100"]
        )

        by_id = self._catalog(search="r:200")
        self.assertEqual(
            [chat["conversationId"] for chat in by_id["chats"]], ["R:200"]
        )

        self.assertEqual(self._catalog(search="nobody")["chats"], [])

    def test_catalog_limit_is_clamped(self):
        self.source.chats = [
            _chat(f"R:{index}", f"群{index}", 10_000 - index, index + 1)
            for index in range(400)
        ]

        self.assertEqual(len(self._catalog(limit=5)["chats"]), 5)
        self.assertEqual(len(self._catalog(limit=999)["chats"]), 300)
        self.assertEqual(len(self._catalog(limit=0)["chats"]), 1)

    def test_legacy_source_without_curated_helpers_still_returns_chats(self):
        class _LegacySource:
            def recent_conversations(self, limit, *, refresh=False):
                return {
                    "chats": [_chat("R:1", "旧群", 100, 1)],
                    "selfSenderId": "1",
                    "selfSenderName": "我",
                }

        runtime = object.__new__(ReplyRuntime)
        runtime.store = None
        runtime.message_source = _LegacySource()
        result = runtime._query_chat_recent({})

        self.assertEqual(result["chats"][0]["conversationId"], "R:1")
        self.assertEqual(result["chats"][0]["alias"], "")
        self.assertTrue(result["chats"][0]["available"])


class MessageSourceCuratedHelpersTests(unittest.TestCase):
    def _source(self):
        from worker.reply_runtime.message_source import LocalWeComMessageSource

        source = object.__new__(LocalWeComMessageSource)
        source.config_path = None
        source._message_snapshot = object()
        source._user_map = {}
        source._member_names = {}
        source._identities = {}
        source._identity_loaded_at = 0.0
        source._cache_lock = threading.Lock()
        source._directory_cache = None
        source._context_cache = {}
        source._warmup_started = True
        return source

    @staticmethod
    def _directory():
        return {
            "conversations": [],
            "member_names": {},
            "user_map": {},
            "accounts": {},
            "identities": {},
            "self_id": 5,
            "self_name": "张三",
            "duplicate_titles": set(),
        }

    def test_catalog_and_lookup_share_the_directory_payload(self):
        source = self._source()
        with mock.patch.object(
            source, "_directory", return_value=self._directory()
        ), mock.patch.object(
            source,
            "_directory_chats",
            return_value=[
                _chat("R:100", "研发群", 3000, 30),
                _chat("S:5_42", "李总", 2000, 20),
            ],
        ):
            catalog = source.conversation_catalog()
            lookup = source.lookup_conversations(["S:5_42", "R:missing"])

        self.assertEqual(
            [chat["conversationId"] for chat in catalog["chats"]],
            ["R:100", "S:5_42"],
        )
        self.assertEqual(
            [chat["conversationId"] for chat in lookup["chats"]], ["S:5_42"]
        )
        self.assertEqual(catalog["selfSenderId"], "5")
        self.assertEqual(lookup["selfSenderName"], "张三")

    def test_invalidate_caches_clears_directory_and_context(self):
        source = self._source()
        source._directory_cache = {"at": 0.0, "conversations": []}
        source._context_cache[("R:1", 20, 21600, 0)] = (0.0, {"messages": []})

        source.invalidate_caches()

        self.assertIsNone(source._directory_cache)
        self.assertEqual(source._context_cache, {})


class ReplyAssistantFocusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.config_path = self.root / "config.local.json"
        self._write_config()
        self.env_patch = mock.patch.dict(
            os.environ, {"WECOM_ISSUE_RADAR_CONFIG": str(self.config_path)}
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _write_config(self, llm: dict | None = None) -> None:
        config = {
            "config_version": 4,
            "timezone": "Asia/Shanghai",
            "llm": llm
            or {"base_url": "", "model": "", "api_key": "", "provider": "openai_compatible"},
        }
        self.config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def _configure_llm(self) -> None:
        self._write_config(
            llm={
                "base_url": "https://llm.example/v1",
                "model": "fake-model",
                "api_key": "fake-key",
                "provider": "openai_compatible",
            }
        )

    @staticmethod
    def _payload() -> str:
        return json.dumps(
            {
                "styles": {"steady": "稳重", "warm": "亲和", "brief": "简短"},
                "interpretation": {
                    "summary": "模型解读",
                    "signals": [{"label": "询问点", "detail": "时间"}],
                },
                "check": {"risks": ["风险"], "suggestions": ["建议"]},
            },
            ensure_ascii=False,
        )

    def test_prompt_lists_focus_messages(self):
        system_prompt, user_prompt = assistant_prompts.eq_reply_prompt(
            None,
            "",
            focus_messages=[
                {"senderName": "李总", "timeLabel": "09:30", "text": "什么时候上线？"},
                {"senderName": "王姐", "timeLabel": "09:31", "text": "麻烦今天确认"},
            ],
        )

        self.assertIn("需要回复的消息（共 2 条）", user_prompt)
        self.assertIn("- [09:30] 李总：什么时候上线？", user_prompt)
        self.assertIn("- [09:31] 王姐：麻烦今天确认", user_prompt)
        self.assertIn("请综合回应以上消息，不要逐条机械回复", user_prompt)
        self.assertIn('"styles"', system_prompt)

    def test_prompt_without_focus_keeps_the_single_message_shape(self):
        _, user_prompt = assistant_prompts.eq_reply_prompt(None, "单条消息")

        self.assertIn("需要回复的消息：\n单条消息", user_prompt)
        self.assertNotIn("共 ", user_prompt)
        self.assertNotIn("请综合回应以上消息", user_prompt)

    def test_prompt_skips_empty_focus_text_and_omits_missing_time(self):
        _, user_prompt = assistant_prompts.eq_reply_prompt(
            None,
            "",
            focus_messages=[
                {"senderName": "A", "timeLabel": "", "text": "第一条"},
                {"senderName": "B", "timeLabel": "10:00", "text": "   "},
            ],
        )

        self.assertIn("需要回复的消息（共 1 条）", user_prompt)
        self.assertIn("- A：第一条", user_prompt)
        self.assertNotIn("B", user_prompt)

    def test_action_passes_focus_messages_to_the_model_prompt(self):
        self._configure_llm()
        focus = [
            {"senderName": "李总", "timeLabel": "09:30", "text": "什么时候上线？"},
            {"senderName": "王姐", "timeLabel": "09:31", "text": "麻烦今天确认"},
        ]
        with mock.patch.object(
            assistant_model, "call_llm", return_value=self._payload()
        ) as call:
            result = handle_assistant_action(
                "eq_reply", {"text": "", "focusMessages": focus, "recipient": "李总"}
            )

        self.assertFalse(result["fallback"])
        user_prompt = call.call_args.args[2]
        self.assertIn("需要回复的消息（共 2 条）", user_prompt)
        self.assertIn("- [09:30] 李总：什么时候上线？", user_prompt)
        self.assertIn("- [09:31] 王姐：麻烦今天确认", user_prompt)
        self.assertIn("请综合回应以上消息，不要逐条机械回复", user_prompt)

    def test_action_caps_and_filters_focus_messages(self):
        self._configure_llm()
        focus = [
            {"senderName": f"成员{index}", "timeLabel": "10:00", "text": f"消息{index}"}
            for index in range(25)
        ]
        focus.insert(3, {"senderName": "空", "timeLabel": "", "text": "   "})
        with mock.patch.object(
            assistant_model, "call_llm", return_value=self._payload()
        ) as call:
            handle_assistant_action("eq_reply", {"text": "", "focusMessages": focus})

        user_prompt = call.call_args.args[2]
        self.assertIn("需要回复的消息（共 20 条）", user_prompt)
        self.assertIn("消息19", user_prompt)
        self.assertNotIn("消息20", user_prompt)

    def test_action_without_model_uses_concatenated_focus_text_for_rules(self):
        result = handle_assistant_action(
            "eq_reply",
            {
                "focusMessages": [
                    {"senderName": "客户", "timeLabel": "09:00", "text": "今天能上线吗？"},
                    {"senderName": "客户", "timeLabel": "09:01", "text": "麻烦今天确认。"},
                ],
                "recipient": "客户",
            },
        )

        self.assertTrue(result["fallback"])
        summary = result["interpretation"]["summary"]
        self.assertIn("今天能上线吗", summary)
        self.assertTrue(result["check"]["risks"])
        self.assertEqual(len(result["styles"]), 3)
        # The concatenated text feeds the deterministic rules, not the placeholder.
        self.assertNotEqual(summary, "对方发来了一段需要回复的消息。")

    def test_action_requires_text_or_focus_messages(self):
        with self.assertRaisesRegex(ValueError, "请输入需要回复的消息内容"):
            handle_assistant_action("eq_reply", {})
        with self.assertRaisesRegex(ValueError, "请输入需要回复的消息内容"):
            handle_assistant_action("eq_reply", {"text": "  ", "focusMessages": []})
        with self.assertRaisesRegex(ValueError, "请输入需要回复的消息内容"):
            handle_assistant_action(
                "eq_reply", {"focusMessages": [{"senderName": "A", "text": "  "}]}
            )

    def test_action_keeps_text_rules_when_text_is_present(self):
        result = handle_assistant_action(
            "eq_reply",
            {
                "text": "客户在催上线时间",
                "focusMessages": [
                    {"senderName": "客户", "timeLabel": "09:00", "text": "今天能上线吗？"}
                ],
            },
        )

        self.assertTrue(result["fallback"])
        self.assertIn("客户在催上线时间", result["interpretation"]["summary"])

    def test_prompt_context_only_mode_asks_to_integrate_the_context(self):
        system_prompt, user_prompt = assistant_prompts.eq_reply_prompt(
            None, "", context="[09:00] 客户：今天能上线吗？"
        )

        self.assertIn("需要回复的消息：未指定", user_prompt)
        self.assertIn("请根据以下群聊上下文判断需要回应什么", user_prompt)
        self.assertIn("综合给出得体回复", user_prompt)
        self.assertIn("不要臆测上下文里没有的信息", user_prompt)
        self.assertIn("相关上下文：\n[09:00] 客户：今天能上线吗？", user_prompt)
        self.assertIn('"styles"', system_prompt)

    def test_action_allows_context_only_generation_with_rule_fallback(self):
        result = handle_assistant_action(
            "eq_reply",
            {"context": "客户在群里问今天能否上线，麻烦尽快确认。"},
        )

        self.assertTrue(result["fallback"])
        self.assertIn(
            "客户在群里问今天能否上线", result["interpretation"]["summary"]
        )
        self.assertTrue(result["check"]["risks"])
        self.assertEqual(len(result["styles"]), 3)
        for style in result["styles"]:
            self.assertTrue(style["content"])

    def test_action_context_only_prompt_is_sent_to_the_model(self):
        self._configure_llm()
        context = "[09:00] 客户：今天能上线吗？\n[09:05] 同事：还在回归。"
        with mock.patch.object(
            assistant_model, "call_llm", return_value=self._payload()
        ) as call:
            result = handle_assistant_action("eq_reply", {"context": context})

        self.assertFalse(result["fallback"])
        user_prompt = call.call_args.args[2]
        self.assertIn("需要回复的消息：未指定", user_prompt)
        self.assertIn("请根据以下群聊上下文判断需要回应什么", user_prompt)
        self.assertIn("综合给出得体回复", user_prompt)
        self.assertIn(context, user_prompt)

    def test_action_requires_context_when_text_and_focus_are_missing(self):
        with self.assertRaisesRegex(ValueError, "请输入需要回复的消息内容"):
            handle_assistant_action("eq_reply", {"context": "   "})
        with self.assertRaisesRegex(ValueError, "请输入需要回复的消息内容"):
            handle_assistant_action("eq_reply", {"text": "", "focusMessages": []})


if __name__ == "__main__":
    unittest.main()
