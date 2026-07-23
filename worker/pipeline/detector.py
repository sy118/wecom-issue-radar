from __future__ import annotations

import csv
import io
import os
import subprocess
import winreg
from dataclasses import dataclass
from pathlib import Path


@dataclass
class WeComDetection:
    running: bool
    executable_paths: list[str]
    data_directories: list[str]


def detect_wecom() -> WeComDetection:
    return WeComDetection(
        running=is_wecom_running(),
        executable_paths=[str(path) for path in detect_executables()],
        data_directories=[str(path) for path in detect_data_directories()],
    )


def is_wecom_running() -> bool:
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq WXWork.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=8,
            creationflags=flags,
            check=False,
        )
        rows = list(csv.reader(io.StringIO(result.stdout)))
        return any(row and row[0].lower() == "wxwork.exe" for row in rows)
    except (OSError, subprocess.SubprocessError):
        return False


def detect_executables() -> list[Path]:
    candidates: list[Path] = []
    for env_name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        root = os.environ.get(env_name)
        if not root:
            continue
        candidates.extend(
            [
                Path(root) / "WXWork" / "WXWork.exe",
                Path(root) / "Tencent" / "WXWork" / "WXWork.exe",
                Path(root) / "WXWork" / "WXWorkApp.exe",
            ]
        )

    uninstall_roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, key_path in uninstall_roots:
        try:
            with winreg.OpenKey(hive, key_path) as root_key:
                for index in range(winreg.QueryInfoKey(root_key)[0]):
                    try:
                        with winreg.OpenKey(root_key, winreg.EnumKey(root_key, index)) as sub_key:
                            display_name = str(winreg.QueryValueEx(sub_key, "DisplayName")[0])
                            if "企业微信" not in display_name and "WXWork" not in display_name:
                                continue
                            install_location = str(winreg.QueryValueEx(sub_key, "InstallLocation")[0])
                            if install_location:
                                candidates.append(Path(install_location) / "WXWork.exe")
                    except OSError:
                        continue
        except OSError:
            continue

    return unique_existing(candidates)


def detect_data_directories() -> list[Path]:
    roots: list[Path] = []
    user_profile = Path(os.environ.get("USERPROFILE") or Path.home())
    roots.extend(
        [
            user_profile / "Documents" / "WXWork",
            user_profile / "My Documents" / "WXWork",
        ]
    )
    candidates: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for account_dir in root.iterdir():
            data_dir = account_dir / "Data"
            if data_dir.is_dir() and any(data_dir.glob("*.db")):
                candidates.append(data_dir)
    candidates.sort(key=latest_db_mtime, reverse=True)
    return unique_existing(candidates)


def latest_db_mtime(path: Path) -> float:
    latest = 0.0
    try:
        for db_file in path.glob("*.db*"):
            try:
                latest = max(latest, db_file.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        return 0.0
    return latest


def unique_existing(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result = []
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        key = str(resolved).lower()
        if key in seen or not resolved.exists():
            continue
        seen.add(key)
        result.append(resolved)
    return result
