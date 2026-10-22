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


def detect_running_executable() -> Path | None:
    """Best-effort path of the running WXWork.exe (covers portable/custom installs)."""
    if not is_wecom_running():
        return None
    command = [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "(Get-Process WXWork -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Path)",
    ]
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=8,
            creationflags=flags,
            check=False,
        )
        value = (result.stdout or "").strip().strip('"')
        if value and Path(value).is_file():
            return Path(value).resolve()
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _install_roots() -> list[Path]:
    roots: list[Path] = []
    for env_name in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
        value = os.environ.get(env_name)
        if value:
            roots.append(Path(value))
            roots.append(Path(value) / "Programs")
    for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
        value = os.environ.get(env_name)
        if value:
            roots.append(Path(value) / "Tencent")
    return roots


def _executable_candidates(root: Path) -> list[Path]:
    if root.name.lower() == "wxwork.exe":
        return [root]
    return [
        root / "WXWork" / "WXWork.exe",
        root / "WXWork" / "WXWorkApp.exe",
        root / "WXWork.exe",
        root / "WXWorkApp.exe",
        root / "Tencent" / "WXWork" / "WXWork.exe",
        root / "Tencent" / "WXWork" / "WXWorkApp.exe",
    ]


def _registry_install_locations() -> list[Path]:
    candidates: list[Path] = []
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
                            for value_name in ("InstallLocation", "InstallPath", "DisplayIcon"):
                                try:
                                    raw = str(winreg.QueryValueEx(sub_key, value_name)[0]).strip('"')
                                except OSError:
                                    continue
                                if raw:
                                    candidates.append(Path(raw))
                    except OSError:
                        continue
        except OSError:
            continue

    # Tencent sometimes records the install path outside the uninstall list.
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key_path in (r"SOFTWARE\Tencent\WXWork", r"SOFTWARE\WOW6432Node\Tencent\WXWork", r"SOFTWARE\WXWork"):
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    for value_name in ("InstallPath", "InstallDir", "InstallLocation", "Path"):
                        try:
                            raw = str(winreg.QueryValueEx(key, value_name)[0]).strip('"')
                        except OSError:
                            continue
                        if raw:
                            candidates.append(Path(raw))
            except OSError:
                continue
    return candidates


def detect_executables() -> list[Path]:
    candidates: list[Path] = []
    for root in _install_roots():
        candidates.extend(_executable_candidates(root))

    for location in _registry_install_locations():
        candidates.extend(_executable_candidates(location))
        if location.name and location.suffix.lower() == ".exe":
            candidates.append(location)
        if location.is_dir():
            candidates.extend(location.glob("WXWork*.exe"))

    running = detect_running_executable()
    if running is not None:
        candidates.insert(0, running)

    # Portable installs and secondary drives: <drive>:\Program Files\... only.
    for drive in _available_drives():
        for name in ("Program Files", "Program Files (x86)"):
            base = drive / name
            candidates.extend(_executable_candidates(base))
            candidates.extend(_executable_candidates(base / "Tencent"))

    return unique_existing(candidates)


def _available_drives() -> list[Path]:
    drives: list[Path] = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        drive = Path(f"{letter}:\\")
        try:
            if drive.is_dir():
                drives.append(drive)
        except OSError:
            continue
    return drives


def _data_roots() -> list[Path]:
    roots: list[Path] = []
    user_profile = Path(os.environ.get("USERPROFILE") or Path.home())
    roots.extend(
        [
            user_profile / "Documents" / "WXWork",
            user_profile / "My Documents" / "WXWork",
        ]
    )
    for env_name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        value = os.environ.get(env_name)
        if value:
            roots.append(Path(value) / "Documents" / "WXWork")
    try:
        roots.extend(user_profile.glob("OneDrive*/Documents/WXWork"))
    except OSError:
        pass
    configured = os.environ.get("WXWORK_DATA_DIR")
    if configured:
        roots.append(Path(configured))
    for drive in _available_drives():
        roots.extend(
            [
                drive / "WXWork",
                drive / "Documents" / "WXWork",
                drive / "Tencent" / "WXWork",
            ]
        )
    return roots


def _account_data_dirs(root: Path) -> list[Path]:
    """Return ``.../<account>/Data`` dirs under a WXWork root.

    Also accepts the Data dir itself because users often paste the inner path.
    """
    if not root.exists():
        return []
    if root.name.lower() == "data" and _has_databases(root):
        return [root]
    matches: list[Path] = []
    direct = root / "Data"
    if _has_databases(direct):
        matches.append(direct)
    try:
        children = list(root.iterdir())
    except OSError:
        return matches
    for child in children[:500]:
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        data_dir = child / "Data"
        if _has_databases(data_dir):
            matches.append(data_dir)
            continue
        # Some deployments add one more corp/account level.
        try:
            for nested in list(child.iterdir())[:50]:
                nested_data = nested / "Data"
                if nested.is_dir() and _has_databases(nested_data):
                    matches.append(nested_data)
        except OSError:
            continue
    return matches


def _has_databases(path: Path) -> bool:
    try:
        return path.is_dir() and any(path.glob("*.db"))
    except OSError:
        return False


def detect_data_directories(roots: list[Path] | None = None) -> list[Path]:
    candidates: list[Path] = []
    for root in roots if roots is not None else _data_roots():
        candidates.extend(_account_data_dirs(root))
    candidates = unique_existing(candidates)
    candidates.sort(key=latest_db_mtime, reverse=True)
    return candidates


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
