from __future__ import annotations

import json
import os
import shutil
import sqlite3
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from worker.reply_runtime import ReplyRuntime
from worker.reply_runtime.message_source import LocalWeComMessageSource
from worker.wecom.local_db import (
    MESSAGE_SNAPSHOT_PREFIX,
    WAL_HEADER_SZ,
    MessageDatabaseSnapshot,
    _wal_checksum,
    patch_wal_frames_incremental,
    read_messages,
)


PAGE_SIZE = 4096
FRAME_SIZE = 24 + PAGE_SIZE
RAW_KEY = bytes.fromhex("11" * 32)


def _write_message_database(path: Path, rows: list[tuple]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """CREATE TABLE message_table(
                   message_id INTEGER, server_id INTEGER, sequence INTEGER,
                   sender_id INTEGER, conversation_id TEXT, content_type INTEGER,
                   send_time INTEGER, flag INTEGER, content BLOB,
                   extra_content BLOB, local_extra_content BLOB
               )"""
        )
        connection.executemany(
            "INSERT INTO message_table VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        connection.commit()
    finally:
        connection.close()


def _write_keys(path: Path) -> None:
    path.write_text(
        json.dumps({"message.db": {"enc_key": RAW_KEY.hex()}}),
        encoding="utf-8",
    )


def _wal_bytes(
    frames: list[tuple[int, int, bytes]],
    *,
    salt: tuple[int, int] = (0x10203040, 0x50607080),
    checkpoint_sequence: int = 1,
) -> tuple[bytes, list[tuple[int, int]]]:
    magic = 0x377F0682
    header = bytearray(WAL_HEADER_SZ)
    struct.pack_into(
        ">IIIIII",
        header,
        0,
        magic,
        3_007_000,
        PAGE_SIZE,
        checkpoint_sequence,
        *salt,
    )
    header_checksum = _wal_checksum(bytes(header[:24]), (0, 0), "<")
    struct.pack_into(">II", header, 24, *header_checksum)
    checksum = header_checksum
    checksums = []
    output = bytearray(header)
    for page_no, database_size, page in frames:
        if len(page) != PAGE_SIZE:
            raise AssertionError("test WAL page must be exactly one page")
        frame_header = bytearray(24)
        struct.pack_into(">IIII", frame_header, 0, page_no, database_size, *salt)
        checksum = _wal_checksum(bytes(frame_header[:8]) + page, checksum, "<")
        struct.pack_into(">II", frame_header, 16, *checksum)
        output.extend(frame_header)
        output.extend(page)
        checksums.append(checksum)
    return bytes(output), checksums


class MessageSnapshotWalTests(unittest.TestCase):
    def test_only_committed_frames_are_applied_and_incremental_resume_uses_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "plain.db"
            target.write_bytes(b"0" * PAGE_SIZE)
            wal = Path(directory) / "message.db-wal"
            first_page = b"A" * PAGE_SIZE
            uncommitted_page = b"B" * PAGE_SIZE
            committed_bytes, checksums = _wal_bytes(
                [(1, 1, first_page), (1, 0, uncommitted_page)]
            )
            wal.write_bytes(committed_bytes)

            with patch(
                "worker.wecom.local_db.decrypt_wxsqlite3_aes128_page",
                side_effect=lambda _key, page, _page_no: page,
            ):
                patched, skipped, resume = patch_wal_frames_incremental(
                    str(target), wal, RAW_KEY
                )

            self.assertEqual((patched, skipped), (1, 0))
            self.assertEqual(resume, WAL_HEADER_SZ + FRAME_SIZE)
            self.assertEqual(target.read_bytes(), first_page)

            final_page = b"C" * PAGE_SIZE
            final_bytes, final_checksums = _wal_bytes(
                [(1, 1, first_page), (1, 0, uncommitted_page), (1, 1, final_page)]
            )
            wal.write_bytes(final_bytes)
            with patch(
                "worker.wecom.local_db.decrypt_wxsqlite3_aes128_page",
                side_effect=lambda _key, page, _page_no: page,
            ):
                patched, skipped, final_resume = patch_wal_frames_incremental(
                    str(target),
                    wal,
                    RAW_KEY,
                    start_offset=resume,
                    max_size=WAL_HEADER_SZ + 3 * FRAME_SIZE,
                    expected_start_checksum=checksums[0],
                    expected_commit_checksum=final_checksums[-1],
                )

            self.assertEqual((patched, skipped), (2, 0))
            self.assertEqual(final_resume, WAL_HEADER_SZ + 3 * FRAME_SIZE)
            self.assertEqual(target.read_bytes(), final_page)

    def test_corrupt_committed_wal_fails_before_mutating_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "plain.db"
            original = b"0" * PAGE_SIZE
            target.write_bytes(original)
            wal = Path(directory) / "message.db-wal"
            content, _ = _wal_bytes([(1, 1, b"A" * PAGE_SIZE)])
            corrupt = bytearray(content)
            corrupt[WAL_HEADER_SZ + 16] ^= 0xFF
            wal.write_bytes(corrupt)

            with (
                patch(
                    "worker.wecom.local_db.decrypt_wxsqlite3_aes128_page",
                    side_effect=lambda _key, page, _page_no: page,
                ),
                self.assertRaises(sqlite3.DatabaseError),
            ):
                patch_wal_frames_incremental(str(target), wal, RAW_KEY)
            self.assertEqual(target.read_bytes(), original)

    def test_partial_trailing_frame_is_not_published(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "plain.db"
            target.write_bytes(b"0" * PAGE_SIZE)
            wal = Path(directory) / "message.db-wal"
            committed_page = b"A" * PAGE_SIZE
            committed, _ = _wal_bytes([(1, 1, committed_page)])
            wal.write_bytes(committed + b"partial-frame")

            with patch(
                "worker.wecom.local_db.decrypt_wxsqlite3_aes128_page",
                side_effect=lambda _key, page, _page_no: page,
            ):
                patched, skipped, resume = patch_wal_frames_incremental(
                    str(target), wal, RAW_KEY
                )

            self.assertEqual((patched, skipped), (1, 0))
            self.assertEqual(resume, WAL_HEADER_SZ + FRAME_SIZE)
            self.assertEqual(target.read_bytes(), committed_page)

    def test_incremental_resume_reads_only_the_appended_wal_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "plain.db"
            target.write_bytes(b"0" * PAGE_SIZE)
            wal = Path(directory) / "message.db-wal"
            prefix_frames = [(1, 1, bytes([index % 251]) * PAGE_SIZE) for index in range(128)]
            prefix, prefix_checksums = _wal_bytes(prefix_frames)
            wal.write_bytes(prefix)

            with patch(
                "worker.wecom.local_db.decrypt_wxsqlite3_aes128_page",
                side_effect=lambda _key, page, _page_no: page,
            ):
                _, _, resume = patch_wal_frames_incremental(
                    str(target), wal, RAW_KEY
                )

            final_page = b"Z" * PAGE_SIZE
            extended, extended_checksums = _wal_bytes(
                [*prefix_frames, (1, 1, final_page)]
            )
            wal.write_bytes(extended)
            bytes_read = 0
            original_path_open = Path.open

            class CountingStream:
                def __init__(self, stream):
                    self.stream = stream

                def __enter__(self):
                    self.stream.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.stream.__exit__(*args)

                def __getattr__(self, name):
                    return getattr(self.stream, name)

                def read(self, size=-1):
                    nonlocal bytes_read
                    data = self.stream.read(size)
                    bytes_read += len(data)
                    return data

            def counting_open(path, *args, **kwargs):
                stream = original_path_open(path, *args, **kwargs)
                if Path(path).resolve() == wal.resolve():
                    return CountingStream(stream)
                return stream

            with (
                patch("pathlib.Path.open", new=counting_open),
                patch(
                    "worker.wecom.local_db.decrypt_wxsqlite3_aes128_page",
                    side_effect=lambda _key, page, _page_no: page,
                ),
            ):
                patched, skipped, final_resume = patch_wal_frames_incremental(
                    str(target),
                    wal,
                    RAW_KEY,
                    start_offset=resume,
                    max_size=WAL_HEADER_SZ + 129 * FRAME_SIZE,
                    expected_start_checksum=prefix_checksums[-1],
                    expected_commit_checksum=extended_checksums[-1],
                )

            self.assertEqual((patched, skipped), (1, 0))
            self.assertEqual(final_resume, WAL_HEADER_SZ + 129 * FRAME_SIZE)
            self.assertEqual(target.read_bytes(), final_page)
            self.assertLess(bytes_read, 3 * FRAME_SIZE)


class SharedMessageSnapshotTests(unittest.TestCase):
    def _fixture(self, directory: str):
        root = Path(directory)
        database = root / "message.db"
        keys = root / "keys.json"
        _write_message_database(
            database,
            [
                (1, 11, 1, 101, "group-a", 1, 100, 0, "question A?", None, None),
                (2, 12, 1, 102, "group-b", 1, 100, 0, "question B?", None, None),
            ],
        )
        _write_keys(keys)
        return database, {
            "wxwork_db_dir": str(root),
            "wxwork_keys_file": str(keys),
        }

    def test_multiple_listeners_share_one_slow_decrypt_then_increment_only_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            database, config = self._fixture(directory)
            decrypt_count = 0
            decrypt_lock = threading.Lock()
            apply_calls: list[tuple[int, int]] = []

            def slow_decrypt(encrypted_path, _raw_key):
                nonlocal decrypt_count
                with decrypt_lock:
                    decrypt_count += 1
                time.sleep(0.08)
                fd, result = tempfile.mkstemp(dir=directory, suffix="-decrypted.db")
                os.close(fd)
                shutil.copyfile(encrypted_path, result)
                return result

            def fake_apply(
                _target,
                _wal_path,
                _raw_key,
                *,
                start_offset,
                max_size,
                **_expected,
            ):
                apply_calls.append((start_offset, max_size))
                return 0, 0, max_size

            wal = Path(str(database) + "-wal")
            first_wal, _ = _wal_bytes([(99, 2, b"W" * PAGE_SIZE)])
            wal.write_bytes(first_wal)
            snapshot = MessageDatabaseSnapshot(
                decryptor=slow_decrypt,
                wal_applier=fake_apply,
                temp_dir=directory,
            )
            source = LocalWeComMessageSource(message_snapshot=snapshot)
            source._refresh_identities = lambda _config: None
            barrier = threading.Barrier(3)
            results: dict[str, list[dict]] = {}

            def read_group(group_id: str):
                barrier.wait()
                results[group_id] = source.read_force(
                    {"groupId": group_id}, [0, 0, 0, 0]
                )

            with (
                patch("worker.reply_runtime.message_source.load_config", return_value=config),
                patch(
                    "worker.reply_runtime.message_source.get_conversation_state",
                    side_effect=lambda _config, group_id: {
                        "last_message_time": 100,
                        "last_message_id": 1 if group_id == "group-a" else 2,
                    },
                ),
            ):
                threads = [
                    threading.Thread(target=read_group, args=(group_id,))
                    for group_id in ("group-a", "group-b")
                ]
                for thread in threads:
                    thread.start()
                barrier.wait()
                for thread in threads:
                    thread.join(2)
                self.assertTrue(all(not thread.is_alive() for thread in threads))

                second_wal, _ = _wal_bytes(
                    [(99, 2, b"W" * PAGE_SIZE), (100, 2, b"X" * PAGE_SIZE)]
                )
                wal.write_bytes(second_wal)
                snapshot_path_before_append = snapshot._snapshot_path
                with patch(
                    "worker.wecom.local_db.shutil.copyfile",
                    side_effect=AssertionError("incremental WAL refresh copied the full database"),
                ):
                    source.read_force({"groupId": "group-a"}, [0, 0, 0, 0])

            self.assertEqual(decrypt_count, 1)
            self.assertEqual(
                apply_calls,
                [
                    (WAL_HEADER_SZ, WAL_HEADER_SZ + FRAME_SIZE),
                    (WAL_HEADER_SZ + FRAME_SIZE, WAL_HEADER_SZ + 2 * FRAME_SIZE),
                ],
            )
            self.assertEqual([item["messageId"] for item in results["group-a"]], ["1"])
            self.assertEqual([item["messageId"] for item in results["group-b"]], ["2"])
            self.assertEqual(snapshot._snapshot_path, snapshot_path_before_append)
            plaintext = snapshot._snapshot_path
            self.assertIsNotNone(plaintext)
            source.close()
            self.assertFalse(Path(plaintext).exists())

    def test_first_commit_after_wal_absence_uses_the_header_checksum_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            database, config = self._fixture(directory)

            def copy_decrypt(encrypted_path, _raw_key):
                fd, result = tempfile.mkstemp(dir=directory, suffix="-decrypted.db")
                os.close(fd)
                shutil.copyfile(encrypted_path, result)
                return result

            snapshot = MessageDatabaseSnapshot(
                decryptor=copy_decrypt,
                temp_dir=directory,
            )
            first = read_messages(config, "group-a", 0, 200, snapshot=snapshot)
            page_count = database.stat().st_size // PAGE_SIZE
            page_one = database.read_bytes()[:PAGE_SIZE]
            wal = Path(str(database) + "-wal")
            wal.write_bytes(_wal_bytes([(1, page_count, page_one)])[0])

            with patch(
                "worker.wecom.local_db.decrypt_wxsqlite3_aes128_page",
                side_effect=lambda _key, page, _page_no: page,
            ):
                second = read_messages(config, "group-a", 0, 200, snapshot=snapshot)
            snapshot.close()

            self.assertEqual([row["message_id"] for row in first], [1])
            self.assertEqual([row["message_id"] for row in second], [1])

    def test_same_length_wal_reset_rebuilds_instead_of_reusing_old_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            database, config = self._fixture(directory)
            decrypt_count = 0

            def copy_decrypt(encrypted_path, _raw_key):
                nonlocal decrypt_count
                decrypt_count += 1
                fd, result = tempfile.mkstemp(dir=directory, suffix="-decrypted.db")
                os.close(fd)
                shutil.copyfile(encrypted_path, result)
                return result

            def fake_apply(_target, _wal_path, _raw_key, *, max_size, **_kwargs):
                return 0, 0, max_size

            wal = Path(str(database) + "-wal")
            wal.write_bytes(_wal_bytes([(99, 2, b"A" * PAGE_SIZE)])[0])
            snapshot = MessageDatabaseSnapshot(
                decryptor=copy_decrypt,
                wal_applier=fake_apply,
                temp_dir=directory,
            )
            read_messages(config, "group-a", 0, 200, snapshot=snapshot)
            previous_mtime = wal.stat().st_mtime_ns
            wal.write_bytes(
                _wal_bytes(
                    [(99, 2, b"B" * PAGE_SIZE)],
                    salt=(0x11223344, 0x55667788),
                    checkpoint_sequence=2,
                )[0]
            )
            os.utime(wal, ns=(previous_mtime + 1_000_000, previous_mtime + 1_000_000))
            read_messages(config, "group-a", 0, 200, snapshot=snapshot)
            snapshot.close()

            self.assertEqual(decrypt_count, 2)

    def test_incremental_wal_corruption_discards_the_published_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            database, config = self._fixture(directory)

            def copy_decrypt(encrypted_path, _raw_key):
                fd, result = tempfile.mkstemp(dir=directory, suffix="-decrypted.db")
                os.close(fd)
                shutil.copyfile(encrypted_path, result)
                return result

            def fake_apply(_target, _wal_path, _raw_key, *, max_size, **_kwargs):
                return 0, 0, max_size

            wal = Path(str(database) + "-wal")
            initial, _ = _wal_bytes([(99, 2, b"A" * PAGE_SIZE)])
            wal.write_bytes(initial)
            snapshot = MessageDatabaseSnapshot(
                decryptor=copy_decrypt,
                wal_applier=fake_apply,
                temp_dir=directory,
            )
            read_messages(config, "group-a", 0, 200, snapshot=snapshot)
            plaintext = snapshot._snapshot_path
            self.assertIsNotNone(plaintext)

            extended, _ = _wal_bytes(
                [(99, 2, b"A" * PAGE_SIZE), (100, 2, b"B" * PAGE_SIZE)]
            )
            corrupt = bytearray(extended)
            corrupt[-1] ^= 0x01
            wal.write_bytes(corrupt)
            with self.assertRaises(sqlite3.DatabaseError):
                read_messages(config, "group-a", 0, 200, snapshot=snapshot)

            self.assertIsNone(snapshot._snapshot_path)
            self.assertFalse(Path(plaintext).exists())
            snapshot.close()

    def test_base_replacement_rebuilds_snapshot_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            database, config = self._fixture(directory)
            decrypt_count = 0

            def copy_decrypt(encrypted_path, _raw_key):
                nonlocal decrypt_count
                decrypt_count += 1
                fd, result = tempfile.mkstemp(dir=directory, suffix="-decrypted.db")
                os.close(fd)
                shutil.copyfile(encrypted_path, result)
                return result

            snapshot = MessageDatabaseSnapshot(
                decryptor=copy_decrypt,
                temp_dir=directory,
            )
            first = read_messages(config, "group-a", 0, 200, snapshot=snapshot)
            replacement = Path(directory) / "replacement.db"
            _write_message_database(
                replacement,
                [(9, 19, 1, 109, "group-a", 1, 150, 0, "new question?", None, None)],
            )
            os.replace(replacement, database)
            second = read_messages(config, "group-a", 0, 200, snapshot=snapshot)
            snapshot.close()

            self.assertEqual([row["message_id"] for row in first], [1])
            self.assertEqual([row["message_id"] for row in second], [9])
            self.assertEqual(decrypt_count, 2)

    def test_in_place_base_checkpoint_rebuilds_the_cached_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            database, config = self._fixture(directory)
            decrypt_count = 0

            def copy_decrypt(encrypted_path, _raw_key):
                nonlocal decrypt_count
                decrypt_count += 1
                fd, result = tempfile.mkstemp(dir=directory, suffix="-decrypted.db")
                os.close(fd)
                shutil.copyfile(encrypted_path, result)
                return result

            snapshot = MessageDatabaseSnapshot(
                decryptor=copy_decrypt,
                temp_dir=directory,
            )
            first = read_messages(config, "group-a", 0, 200, snapshot=snapshot)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "INSERT INTO message_table VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (3, 13, 2, 103, "group-a", 1, 150, 0, "new question?", None, None),
                )
                connection.commit()
            finally:
                connection.close()
            second = read_messages(config, "group-a", 0, 200, snapshot=snapshot)
            snapshot.close()

            self.assertEqual([row["message_id"] for row in first], [1])
            self.assertEqual([row["message_id"] for row in second], [3, 1])
            self.assertEqual(decrypt_count, 2)

    def test_corrupt_wal_is_not_hidden_by_a_base_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            database, config = self._fixture(directory)

            def copy_decrypt(encrypted_path, _raw_key):
                fd, result = tempfile.mkstemp(dir=directory, suffix="-decrypted.db")
                os.close(fd)
                shutil.copyfile(encrypted_path, result)
                return result

            content, _ = _wal_bytes([(1, 1, b"A" * PAGE_SIZE)])
            corrupt = bytearray(content)
            corrupt[-1] ^= 0x01
            Path(str(database) + "-wal").write_bytes(corrupt)
            snapshot = MessageDatabaseSnapshot(
                decryptor=copy_decrypt,
                temp_dir=directory,
            )
            with self.assertRaises(sqlite3.DatabaseError):
                read_messages(config, "group-a", 0, 200, snapshot=snapshot)
            self.assertIsNone(snapshot._snapshot_path)
            snapshot.close()

    def test_watermark_failure_does_not_publish_session_state_fallback(self):
        source = LocalWeComMessageSource()
        exact_tail = {
            "send_time": 200,
            "sequence": 7,
            "message_id": 42,
            "server_id": 99,
        }
        try:
            with (
                patch(
                    "worker.reply_runtime.message_source.load_config",
                    return_value={},
                ),
                patch(
                    "worker.reply_runtime.message_source.get_conversation_state",
                    return_value={"last_message_time": 200, "last_message_id": 42},
                ),
                patch(
                    "worker.reply_runtime.message_source.read_messages",
                    side_effect=[sqlite3.DatabaseError("snapshot unavailable"), [exact_tail]],
                ),
            ):
                with self.assertRaisesRegex(sqlite3.DatabaseError, "snapshot unavailable"):
                    source.watermark({"groupId": "group-a"})

                self.assertEqual(
                    source.watermark({"groupId": "group-a"}),
                    [200, 7, 42, 99],
                )
        finally:
            source.close()

    def test_runtime_close_closes_message_source_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = MessageDatabaseSnapshot(temp_dir=directory)
            source = LocalWeComMessageSource(message_snapshot=snapshot)
            runtime = ReplyRuntime(
                Path(directory) / "runtime.sqlite3",
                message_source=source,
                autostart=False,
            )
            runtime.close()
            self.assertTrue(snapshot._closed)

    def test_close_removes_plaintext_snapshot_and_sqlite_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            _database, config = self._fixture(directory)

            def copy_decrypt(encrypted_path, _raw_key):
                fd, result = tempfile.mkstemp(dir=directory, suffix="-decrypted.db")
                os.close(fd)
                shutil.copyfile(encrypted_path, result)
                return result

            snapshot = MessageDatabaseSnapshot(
                decryptor=copy_decrypt,
                temp_dir=directory,
            )
            read_messages(config, "group-a", 0, 200, snapshot=snapshot)
            plaintext = snapshot._snapshot_path
            self.assertIsNotNone(plaintext)
            artifacts = [
                Path(str(plaintext) + suffix)
                for suffix in ("", "-wal", "-shm", "-journal")
            ]
            for artifact in artifacts[1:]:
                artifact.write_bytes(b"plaintext-sidecar")

            snapshot.close()

            self.assertTrue(all(not artifact.exists() for artifact in artifacts))

    def test_startup_removes_dead_process_snapshot_residue(self):
        with tempfile.TemporaryDirectory() as directory:
            residue = Path(directory) / f"{MESSAGE_SNAPSHOT_PREFIX}999999999-dead.db"
            residue.write_bytes(b"plaintext")
            snapshot = MessageDatabaseSnapshot(temp_dir=directory)
            try:
                self.assertFalse(residue.exists())
            finally:
                snapshot.close()


if __name__ == "__main__":
    unittest.main()
