from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import ssl
import sys
import traceback
import urllib.request
from pathlib import Path


RUNTIME_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def emit(kind: str, **payload) -> None:
    print(json.dumps({"type": kind, **payload}, ensure_ascii=False), flush=True)


def progress(message: str) -> None:
    emit("progress", message=message)


def load_request(path: str | os.PathLike) -> dict:
    with Path(path).open("r", encoding="utf-8") as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise ValueError("worker request must be a JSON object")
    return value


def handle_groups(payload: dict) -> dict:
    from worker.pipeline.tasks import list_groups

    config_path = str(payload.get("configPath") or "")
    progress("正在读取企业微信群列表…")
    rows = list_groups(config_path, limit=int(payload.get("limit") or 100))
    return {"groups": rows}


def handle_detect(_payload: dict) -> dict:
    from worker.pipeline.detector import detect_wecom

    progress("正在检测企业微信安装位置和数据目录…")
    detected = detect_wecom()
    return {
        "running": detected.running,
        "executablePaths": detected.executable_paths,
        "dataDirectories": detected.data_directories,
    }


def handle_run(payload: dict) -> dict:
    from worker.pipeline.config_store import load_config
    from worker.pipeline.exporter import export_day
    from worker.pipeline.llm_analyzer import analyze_day
    from worker.pipeline.smart_sheet import preview_sync
    from worker.pipeline.tasks import prepare_day

    config_path = str(payload.get("configPath") or "")
    config, _ = load_config(config_path)
    request = payload.get("request") or {}
    date_text = str(request.get("date") or "")
    group_id = str(request.get("groupId") or "")
    group_name = str(request.get("groupName") or group_id)
    if not date_text or not group_id:
        raise ValueError("date and groupId are required")

    day_dir, _cache_output = prepare_day(
        config,
        config_path,
        date_text,
        group_id=group_id,
        run_ocr=bool(request.get("runOcr")),
        progress=progress,
    )
    definition_path = None
    if request.get("runAnalysis"):
        definition_path = analyze_day(
            config,
            day_dir,
            date_text,
            group_name,
            request.get("promptId"),
            progress,
        )

    progress("正在生成本地导出文件…")
    outputs = export_day(
        day_dir,
        date_text,
        group_name,
        export_xlsx=bool(request.get("exportXlsx")),
        export_markdown=bool(request.get("exportMarkdown")),
        include_issues=bool(request.get("runAnalysis")),
    )
    preview = None
    if request.get("prepareSmartSheet"):
        preview = preview_sync(config, day_dir, date_text)
    return {
        "dayDir": str(day_dir),
        "outputs": outputs,
        "definitionPath": str(definition_path) if definition_path else None,
        "smartSheetPreview": preview,
    }


def handle_sync(payload: dict) -> dict:
    from worker.pipeline.config_store import load_config
    from worker.pipeline.smart_sheet import sync_issues

    config_path = str(payload.get("configPath") or "")
    config, _ = load_config(config_path)
    result = sync_issues(
        config,
        str(payload.get("dayDir") or ""),
        str(payload.get("date") or ""),
        upload_images=bool(payload.get("uploadImages", True)),
        progress=progress,
    )
    return result


def dispatch(request: dict) -> dict:
    action = str(request.get("action") or "")
    payload = request.get("payload") or {}
    if action == "detect":
        return handle_detect(payload)
    if action == "groups":
        return handle_groups(payload)
    if action == "run":
        return handle_run(payload)
    if action == "sync":
        return handle_sync(payload)
    raise ValueError(f"unknown worker action: {action}")


def extract_keys_console() -> int:
    from worker.wecom.extract_keys import main as extract_main

    if sys.platform == "win32" and getattr(sys, "frozen", False):
        ctypes.windll.kernel32.SetConsoleTitleW("企微问题雷达 - 提取数据库密钥")
    code = 1
    try:
        code = int(extract_main() or 0)
    except Exception as exc:
        print(f"\n密钥提取失败：{exc}")
        traceback.print_exc()
    finally:
        try:
            input("\n按回车键关闭此窗口…")
        except (EOFError, OSError):
            pass
    return code


def runtime_self_check() -> int:
    has_pbkdf2 = hasattr(hashlib, "pbkdf2_hmac")
    has_https = any(
        type(handler).__name__ == "HTTPSHandler"
        for handler in urllib.request.build_opener().handlers
    )
    result = {
        "python": sys.version,
        "openssl": ssl.OPENSSL_VERSION,
        "pbkdf2_hmac": has_pbkdf2,
        "https_handler": has_https,
    }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if has_pbkdf2 and has_https else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WeCom Issue Radar worker")
    parser.add_argument("--request", default="", help="JSON request file")
    parser.add_argument("--extract-keys", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_check:
        return runtime_self_check()
    if args.extract_keys:
        return extract_keys_console()
    try:
        result = dispatch(load_request(args.request))
        emit("result", data=result)
        return 0
    except Exception as exc:
        emit("error", message=str(exc), detail=traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
