"""Resident personal-WeChat worker (NDJSON over stdin/stdout).

Wire protocol:
  stdin:  {"id": "...", "op": "execute", "action": "messages", "payload": {...}}
  stdout: {"id": "...", "ok": true, "data": ...}
          {"id": "...", "ok": false, "error": {"code": "...", "message": "..."}}

Only JSON belongs on stdout; tracebacks and library logging go to stderr so the
Electron supervisor can parse each line safely.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Callable, TextIO

from .actions import handle_wechat_action


def _as_utf8(stream: TextIO) -> TextIO:
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    return stream


def _error_payload(exc: BaseException) -> dict:
    code = str(getattr(exc, "code", "") or "WECHAT_ACTION_FAILED")
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": str(exc) or "个人微信操作失败。",
            "retryable": bool(getattr(exc, "retryable", False)),
        },
    }


def serve_wechat_runtime(
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    handler: Callable[[str, dict], dict] = handle_wechat_action,
) -> int:
    input_stream = _as_utf8(input_stream)
    output_stream = _as_utf8(output_stream)

    def write(value: dict) -> None:
        output_stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        output_stream.flush()

    for raw in input_stream:
        line = raw.strip()
        if not line:
            continue
        request: object = None
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            write({"id": None, **_error_payload(ValueError("请求不是合法 JSON"))})
            continue
        if not isinstance(request, dict):
            write({"id": None, **_error_payload(ValueError("请求必须是 JSON 对象"))})
            continue
        request_id = request.get("id")
        if request_id is None:
            write({"id": None, **_error_payload(ValueError("请求缺少 id"))})
            continue
        action = str(request.get("action") or "").strip()
        if not action:
            write({"id": request_id, **_error_payload(ValueError("请求缺少 action"))})
            continue
        payload = request.get("payload")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            write({"id": request_id, **_error_payload(ValueError("payload 必须是 JSON 对象"))})
            continue
        try:
            data = handler(action, payload)
        except Exception as exc:  # noqa: BLE001 - report every failure as structured JSON
            print(
                f"wechat runtime action failed: {action}: {exc}\n{traceback.format_exc()}",
                file=sys.stderr,
                flush=True,
            )
            write({"id": request_id, **_error_payload(exc)})
            continue
        write({"id": request_id, "ok": True, "data": data})
    return 0


def run_default_wechat_runtime() -> int:
    return serve_wechat_runtime(sys.stdin, sys.stdout)


__all__ = ["serve_wechat_runtime", "run_default_wechat_runtime"]
