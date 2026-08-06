from __future__ import annotations

import os
from pathlib import Path

from worker.pipeline.config_store import default_config_path, load_config

from .adapters import ConfiguredModelAdapter, McpSdkAdapter, WeComWebhookAdapter
from .dify import DifyChatflowAnswerEngine
from .message_source import LocalWeComMessageSource
from .runtime import ReplyRuntime


DEFAULT_RUNTIME_DATABASE = Path.home() / ".wecom-issue-radar" / "reply-runtime.sqlite3"


def build_default_runtime(*, event_sink=None, autostart: bool = True) -> ReplyRuntime:
    config_path = Path(
        os.environ.get("WECOM_ISSUE_RADAR_CONFIG") or default_config_path()
    ).expanduser().resolve()
    database_path = Path(
        os.environ.get("WECOM_ISSUE_RADAR_REPLY_RUNTIME_DB") or DEFAULT_RUNTIME_DATABASE
    ).expanduser().resolve()

    def config_loader() -> dict:
        config, _ = load_config(config_path)
        return config

    return ReplyRuntime(
        database_path,
        model=ConfiguredModelAdapter(config_loader),
        mcp=McpSdkAdapter(connect_timeout=15),
        dify=DifyChatflowAnswerEngine(),
        webhook=WeComWebhookAdapter(),
        message_source=LocalWeComMessageSource(config_path),
        event_sink=event_sink,
        config_path=config_path,
        autostart=autostart,
    )
