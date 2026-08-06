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
from datetime import datetime
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
    from worker.pipeline.llm_analyzer import NoAnalyzableMessagesError, analyze_day
    from worker.pipeline.smart_sheet import preview_sync
    from worker.pipeline.tasks import issue_count, prepare_day

    config_path = str(payload.get("configPath") or "")
    config, _ = load_config(config_path)
    request = payload.get("request") or {}
    start_date, end_date, start_time, end_time = normalize_run_range(request)
    groups = normalize_run_groups(request)
    if not groups:
        raise ValueError("请选择导出日期和至少一个群聊")

    # AI definitions and Smart Sheet registration use the range end date. The range directory
    # and exported documents retain both dates, so overnight runs remain distinguishable.
    result_date = end_date
    runs = []
    for index, group in enumerate(groups, start=1):
        group_id = group["id"]
        group_name = group["name"]
        progress(f"正在处理群聊 {index}/{len(groups)}：{group_name}")
        run_result = {
            "groupId": group_id,
            "groupName": group_name,
            "status": "failed",
            "error": "",
            "startDate": start_date,
            "endDate": end_date,
            "startTime": start_time,
            "endTime": end_time,
            "smartSheetDate": result_date,
            "smartSheetTemplateId": "",
            "smartSheetTemplateName": "",
            "smartSheetTemplateUrl": "",
            "dayDir": "",
            "outputs": {},
            "definitionPath": None,
            "smartSheetPreview": None,
        }
        try:
            day_dir, _cache_output = prepare_day(
                config,
                config_path,
                start_date,
                group_id=group_id,
                run_ocr=bool(request.get("runOcr")),
                end_date=end_date,
                start_time=start_time,
                end_time=end_time,
                progress=progress,
            )
            run_result["dayDir"] = str(day_dir)
            definition_path = None
            if request.get("runAnalysis"):
                definition_path = analyze_day(
                    config,
                    day_dir,
                    result_date,
                    group_name,
                    request.get("promptId"),
                    progress,
                    start_date=start_date,
                    start_time=start_time,
                    end_date=end_date,
                    end_time=end_time,
                )
                run_result["definitionPath"] = str(definition_path)
                run_result["issueCount"] = issue_count(day_dir, result_date)

            progress(f"正在生成“{group_name}”的本地导出文件…")
            run_result["outputs"] = export_day(
                day_dir,
                result_date,
                group_name,
                export_xlsx=bool(request.get("exportXlsx")),
                export_markdown=bool(request.get("exportMarkdown")),
                include_issues=bool(request.get("runAnalysis")),
                start_time=start_time,
                end_time=end_time,
                start_date=start_date,
                end_date=end_date,
                definition_path=definition_path,
            )
            preview = None
            if request.get("prepareSmartSheet"):
                preview = preview_sync(
                    config,
                    day_dir,
                    result_date,
                    str(request.get("smartSheetTemplateId") or "") or None,
                    definition_path=definition_path,
                )
            run_result.update(
                {
                    "status": "success",
                    "smartSheetTemplateId": (preview or {}).get("template_id") or "",
                    "smartSheetTemplateName": (preview or {}).get("template_name") or "",
                    "smartSheetTemplateUrl": (preview or {}).get("template_url") or "",
                    "smartSheetPreview": preview,
                }
            )
        except NoAnalyzableMessagesError as exc:
            run_result["status"] = "empty"
            run_result["error"] = str(exc)
            progress(f"已跳过“{group_name}”：{exc}")
        except Exception as exc:
            run_result["status"] = "failed"
            run_result["error"] = str(exc)
            progress(f"“{group_name}”处理失败，已继续处理后续群聊；可在结果中查看原因")
        runs.append(run_result)

    success_count = sum(run.get("status") == "success" for run in runs)
    empty_count = sum(run.get("status") == "empty" for run in runs)
    failed_count = sum(run.get("status") == "failed" for run in runs)
    if failed_count == len(runs):
        overall_status = "failed"
    elif failed_count:
        overall_status = "partial"
    elif success_count:
        overall_status = "success"
    else:
        overall_status = "empty"

    result = {
        "runs": runs,
        "status": overall_status,
        "totalCount": len(runs),
        "successCount": success_count,
        "emptyCount": empty_count,
        "failedCount": failed_count,
    }
    if len(runs) == 1:
        # Keep the original single-group response shape for older frontends.
        result.update(runs[0])
        result["status"] = overall_status
    return result


def normalize_run_range(request: dict) -> tuple[str, str, str, str]:
    legacy_date = str(request.get("date") or "").strip()
    start_date = str(request.get("startDate") or legacy_date).strip()
    end_date = str(request.get("endDate") or start_date).strip()
    start_time = str(request.get("startTime") or "00:00").strip()
    end_time = str(request.get("endTime") or "23:59").strip()
    if not start_date or not end_date:
        raise ValueError("请选择导出日期和至少一个群聊")
    try:
        start = datetime.strptime(f"{start_date} {start_time}", "%Y-%m-%d %H:%M")
        end = datetime.strptime(f"{end_date} {end_time}", "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError("导出日期或时间格式无效，请使用 YYYY-MM-DD 和 HH:MM") from exc
    if end < start:
        raise ValueError("导出结束日期时间不能早于开始日期时间")
    return start_date, end_date, start_time, end_time


def normalize_run_groups(request: dict) -> list[dict[str, str]]:
    raw_groups = request.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raw_groups = [
            {
                "id": request.get("groupId"),
                "name": request.get("groupName"),
            }
        ]

    groups = []
    seen = set()
    for raw_group in raw_groups:
        if not isinstance(raw_group, dict):
            raise ValueError("群聊配置无效：每个群聊都必须包含 id 和 name")
        group_id = str(raw_group.get("id") or "").strip()
        if not group_id:
            raise ValueError("群聊配置无效：群聊 id 不能为空")
        if group_id in seen:
            continue
        seen.add(group_id)
        groups.append(
            {
                "id": group_id,
                "name": str(raw_group.get("name") or group_id).strip() or group_id,
            }
        )
    return groups


def handle_sync(payload: dict) -> dict:
    from worker.pipeline.config_store import load_config
    from worker.pipeline.smart_sheet import sync_issues

    config_path = str(payload.get("configPath") or "")
    config, _ = load_config(config_path)
    result = sync_issues(
        config,
        str(payload.get("dayDir") or ""),
        str(payload.get("date") or ""),
        template_id=str(payload.get("templateId") or "") or None,
        upload_images=bool(payload.get("uploadImages", True)),
        allow_missing_images=bool(payload.get("allowMissingImages", False)),
        definition_path=str(payload.get("definitionPath") or "") or None,
        expected_template_revision=(
            str(payload.get("expectedTemplateRevision") or "") or None
        ),
        expected_document_revision=(
            str(payload.get("expectedDocumentRevision") or "") or None
        ),
        progress=progress,
    )
    return result


def handle_preview(payload: dict) -> dict:
    from worker.pipeline.config_store import load_config
    from worker.pipeline.smart_sheet import preview_sync

    config_path = str(payload.get("configPath") or "")
    config, _ = load_config(config_path)
    return preview_sync(
        config,
        str(payload.get("dayDir") or ""),
        str(payload.get("date") or ""),
        str(payload.get("templateId") or "") or None,
        definition_path=str(payload.get("definitionPath") or "") or None,
    )


def dispatch(request: dict) -> dict:
    action = str(request.get("action") or "")
    payload = request.get("payload") or {}
    if action == "detect":
        return handle_detect(payload)
    if action == "groups":
        return handle_groups(payload)
    if action == "run":
        return handle_run(payload)
    if action == "preview":
        return handle_preview(payload)
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
    except Exception:
        print("\n密钥提取未完成。")
        print("请确认企业微信已登录并保持运行，然后重新尝试。")
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
    try:
        from worker.reply_runtime import ReplyRuntime as _ReplyRuntime  # noqa: F401

        has_reply_runtime = True
    except Exception:
        has_reply_runtime = False
    try:
        from langchain_core.messages import AIMessage
        from worker.reply_runtime.agent import LangGraphMcpAgent

        class SelfCheckToolModel:
            def __init__(self):
                self.invocations = 0
                self.tools = []

            def bind_tools(self, tools):
                self.tools = list(tools)
                return self

            def invoke(self, _messages):
                self.invocations += 1
                if self.invocations == 1:
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": self.tools[0].name,
                                "args": {},
                                "id": "self-check-call",
                                "type": "tool_call",
                            }
                        ],
                    )
                return AIMessage(content="", tool_calls=[])

        self_check_model = SelfCheckToolModel()
        agent_result = LangGraphMcpAgent(
            lambda _timeout: self_check_model
        ).retrieve(
            question="self-check",
            context=[],
            tools=[
                {
                    "serverId": "self-check",
                    "toolName": "ping",
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }
            ],
            system_prompt="",
            image_content=[],
            invoke_tool=lambda *_args: {"ok": True},
            has_evidence=lambda value: value == {"ok": True},
            max_rounds=2,
            timeout_seconds=5,
        )
        has_mcp_agent = bool(
            agent_result.evidence
            and agent_result.rounds == 2
            and agent_result.tool_calls == 1
        )
    except Exception:
        has_mcp_agent = False
    try:
        from mcp import ClientSession as _ClientSession  # noqa: F401

        has_mcp_sdk = True
    except Exception:
        has_mcp_sdk = False
    result = {
        "python": sys.version,
        "openssl": ssl.OPENSSL_VERSION,
        "pbkdf2_hmac": has_pbkdf2,
        "https_handler": has_https,
        "reply_runtime": has_reply_runtime,
        "mcp_agent": has_mcp_agent,
        "mcp_sdk": has_mcp_sdk,
    }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return (
        0
        if has_pbkdf2
        and has_https
        and has_reply_runtime
        and has_mcp_agent
        and has_mcp_sdk
        else 1
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WeCom Issue Radar worker")
    parser.add_argument("--request", default="", help="JSON request file")
    parser.add_argument("--extract-keys", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--reply-runtime", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.reply_runtime:
        from worker.reply_runtime.stdio import run_default_reply_runtime

        return run_default_reply_runtime()
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
