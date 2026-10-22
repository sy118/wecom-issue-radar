from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from worker.pipeline import detector


class DataDirectoryDetectionTests(unittest.TestCase):
    def test_detects_account_data_dirs_and_sorts_by_db_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "WXWork"
            older = root / "1688000000000001" / "Data"
            newer = root / "1688000000000002" / "Data"
            older.mkdir(parents=True)
            newer.mkdir(parents=True)
            old_db = older / "message.db"
            new_db = newer / "message.db"
            old_db.write_bytes(b"old")
            new_db.write_bytes(b"new")
            os.utime(old_db, (1_700_000_000, 1_700_000_000))
            os.utime(new_db, (1_800_000_000, 1_800_000_000))
            time.sleep(0.01)

            result = detector.detect_data_directories([root])

            self.assertEqual(result, [newer.resolve(), older.resolve()])

    def test_accepts_data_directory_pasted_directly(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "WXWork" / "1688" / "Data"
            data_dir.mkdir(parents=True)
            (data_dir / "message.db").write_bytes(b"x")

            result = detector.detect_data_directories([data_dir])

            self.assertEqual(result, [data_dir.resolve()])

    def test_ignores_accounts_without_databases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "WXWork"
            (root / "empty" / "Data").mkdir(parents=True)
            valid = root / "valid" / "Data"
            valid.mkdir(parents=True)
            (valid / "message.db").write_bytes(b"x")

            result = detector.detect_data_directories([root])

            self.assertEqual(result, [valid.resolve()])

    def test_supports_extra_corp_level(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "WXWork"
            data_dir = root / "corp" / "account" / "Data"
            data_dir.mkdir(parents=True)
            (data_dir / "message.db").write_bytes(b"x")

            result = detector.detect_data_directories([root])

            self.assertIn(data_dir.resolve(), result)


class ExecutableDetectionTests(unittest.TestCase):
    def test_includes_program_files_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            program_files = Path(directory) / "Program Files"
            executable = program_files / "WXWork" / "WXWork.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"exe")
            env = {**os.environ, "ProgramFiles": str(program_files), "ProgramFiles(x86)": str(Path(directory) / "missing")}

            with patch.dict(os.environ, env, clear=False), \
                patch.object(detector, "detect_running_executable", return_value=None), \
                patch.object(detector, "_available_drives", return_value=[]), \
                patch.object(detector, "_registry_install_locations", return_value=[]):
                result = detector.detect_executables()

            self.assertIn(executable.resolve(), result)

    def test_detect_wecom_returns_serializable_paths(self):
        with patch.object(detector, "is_wecom_running", return_value=True), \
            patch.object(detector, "detect_executables", return_value=[Path("C:/WXWork.exe")]), \
            patch.object(detector, "detect_data_directories", return_value=[Path("E:/Data")]):
            result = detector.detect_wecom()

        self.assertTrue(result.running)
        self.assertEqual(result.executable_paths, [str(Path("C:/WXWork.exe"))])
        self.assertEqual(result.data_directories, [str(Path("E:/Data"))])


if __name__ == "__main__":
    unittest.main()
