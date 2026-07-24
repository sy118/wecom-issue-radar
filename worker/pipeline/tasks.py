from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def list_groups(config_path: str | os.PathLike, limit: int = 100) -> list[dict]:
    from worker.wecom.local_db import list_conversations, load_config

    config = load_config(str(config_path))
    return list_conversations(config, limit=limit, include_direct=False)


def prepare_day(
    config: dict,
    config_path: str | os.PathLike,
    date_text: str,
    *,
    group_id: str,
    run_ocr: bool,
    end_date: str | None = None,
    start_time: str = "00:00",
    end_time: str = "23:59",
    progress=None,
) -> tuple[Path, str]:
    notify = progress or (lambda _message: None)
    from worker.wecom import cache_messages

    workspace_root = Path(config.get("default_workspace") or PROJECT_ROOT / "work").expanduser().resolve()
    group_folder = re.sub(r"[^a-zA-Z0-9_.-]+", "_", group_id).strip("_") or "default_group"
    workspace = workspace_root / "groups" / group_folder
    effective_end_date = end_date or date_text
    range_dir_name = cache_messages.range_directory_name(date_text, effective_end_date)
    day_dir = workspace / "work" / range_dir_name
    notify("正在从本地企业微信数据库提取聊天记录和附件…")
    output = invoke_main(
        cache_messages.main,
        [
            "cache_wecom_messages.py",
            "--config",
            str(config_path),
            "--workspace",
            str(workspace),
            "--start-date",
            date_text,
            "--end-date",
            effective_end_date,
            "--conversation-id",
            group_id,
            "--start-time",
            start_time,
            "--end-time",
            end_time,
        ],
    )
    if run_ocr:
        notify("正在识别聊天截图文字…")
        from worker.ocr import run_ocr

        manifest = day_dir / "raw_attachments" / "_bulk_hd_cache" / "hd_cache_manifest.json"
        grouped_dir = day_dir / "grouped_issues"
        run_ocr(config, manifest, grouped_dir, notify)
    return day_dir, output


def invoke_main(main_func, argv: list[str]) -> str:
    previous = sys.argv[:]
    buffer = io.StringIO()
    try:
        sys.argv = argv
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            result = main_func()
        if result not in (None, 0):
            raise RuntimeError(f"任务执行失败，退出码 {result}\n{buffer.getvalue()}")
        return buffer.getvalue()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if code:
            raise RuntimeError(f"任务执行失败：{exc}\n{buffer.getvalue()}") from exc
        return buffer.getvalue()
    finally:
        sys.argv = previous


def issue_count(day_dir: str | Path, date_text: str) -> int:
    path = Path(day_dir) / "grouped_issues" / f"issue_definitions_{date_text.replace('-', '')}.json"
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    issues = data if isinstance(data, list) else data.get("issues") or []
    return len(issues)
