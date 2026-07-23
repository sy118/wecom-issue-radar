from __future__ import annotations

import os
import sys
from pathlib import Path


RUNTIME_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
USER_CONFIG_DIR = Path(
    os.environ.get("WECOM_ISSUE_RADAR_HOME", Path.home() / ".wecom-issue-radar")
)
USER_CONFIG_PATH = USER_CONFIG_DIR / "config.local.json"
ENV_CONFIG = "WECOM_ISSUE_RADAR_CONFIG"
LEGACY_ENV_CONFIG = "WECOM_DAILY_PIPELINE_CONFIG"


def resolve_config_path(
    config_path: str | os.PathLike | None = None,
    *,
    allow_example: bool = True,
) -> Path:
    candidates: list[Path] = []
    if config_path:
        candidates.append(Path(config_path))
    env_path = os.environ.get(ENV_CONFIG) or os.environ.get(LEGACY_ENV_CONFIG)
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(USER_CONFIG_PATH)
    if allow_example:
        candidates.append(RUNTIME_ROOT / "config.example.json")

    for path in candidates:
        if path.exists():
            return path.expanduser().resolve()
    return candidates[0].expanduser().resolve()


def config_base_dir(config_path: str | os.PathLike) -> Path:
    return Path(config_path).expanduser().resolve().parent


def resolve_config_relative_path(
    value: str | os.PathLike,
    config_path: str | os.PathLike,
) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return config_base_dir(config_path) / path
