from __future__ import annotations

import json
import mimetypes
import os
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


ProgressCallback = Callable[[str], None]


def preview_sync(config: dict, day_dir: str | Path, date_text: str) -> dict:
    issues = load_issue_definitions(day_dir, date_text)
    ledger = load_ledger(ledger_path(day_dir))
    synced = ledger.get("synced") or {}
    pending = [issue for issue in issues if issue.get("key") not in synced]
    return {
        "total": len(issues),
        "pending": len(pending),
        "already_synced": len(issues) - len(pending),
        "webhook_configured": bool((config.get("smart_sheet") or {}).get("webhook_url")),
    }


def sync_issues(
    config: dict,
    day_dir: str | Path,
    date_text: str,
    *,
    upload_images: bool = True,
    force: bool = False,
    progress: ProgressCallback | None = None,
) -> dict:
    notify = progress or (lambda _message: None)
    smart_config = config.get("smart_sheet") or {}
    webhook_url = str(smart_config.get("webhook_url") or "").strip()
    if not webhook_url:
        raise ValueError("请先在设置页配置腾讯文档 Smart Sheet Webhook URL")
    issues = load_issue_definitions(day_dir, date_text)
    state_path = ledger_path(day_dir)
    ledger = load_ledger(state_path)
    synced = ledger.setdefault("synced", {})
    pending = issues if force else [issue for issue in issues if issue.get("key") not in synced]
    if not pending:
        return {"total": len(issues), "synced": 0, "skipped": len(issues), "state": str(state_path)}

    upload = smart_config.get("upload") or {}
    token = None
    if upload_images and any(issue.get("image_refs") for issue in pending):
        corpid = str(upload.get("corpid") or os.environ.get(upload.get("corpid_env") or "WECOM_CORPID") or "")
        corpsecret = str(upload.get("corpsecret") or os.environ.get(upload.get("corpsecret_env") or "WECOM_CORP_SECRET") or "")
        if not corpid or not corpsecret:
            raise ValueError("已选择上传截图，但 Smart Sheet 图片上传的 corpid/corpsecret 未配置")
        notify("正在获取企业微信图片上传凭证…")
        token = get_access_token(upload, corpid, corpsecret)

    manifest = load_manifest(day_dir)
    records = []
    for index, issue in enumerate(pending, start=1):
        notify(f"正在准备腾讯文档记录 {index}/{len(pending)}…")
        image_values = []
        if token:
            for image_index, image_path in enumerate(resolve_issue_images(issue, manifest), start=1):
                image_url = upload_image(upload, token, image_path)
                width, height = image_dimensions(image_path)
                image_values.append(
                    {
                        "id": f"desktop_{index:03d}_{image_index:02d}_{uuid.uuid4().hex[:8]}",
                        "title": image_path.name,
                        "image_url": image_url,
                        "width": width,
                        "height": height,
                    }
                )
                delay_ms = int(upload.get("delay_ms_between_image_uploads") or 400)
                if delay_ms > 0:
                    time.sleep(delay_ms / 1000)
        records.append({"issue": issue, "values": issue_values(config, issue, date_text, image_values)})

    batch_size = max(1, min(int(smart_config.get("batch_size") or 50), 50))
    successful = 0
    for offset in range(0, len(records), batch_size):
        batch = records[offset : offset + batch_size]
        notify(f"正在写入腾讯文档：第 {offset + 1}-{offset + len(batch)} 条…")
        payload = {
            "schema": smart_config.get("schema") or {},
            "add_records": [{"values": record["values"]} for record in batch],
        }
        response = post_json(webhook_url, payload)
        if int(response.get("errcode", -1)) != 0:
            raise RuntimeError(f"腾讯文档写入失败: {json.dumps(response, ensure_ascii=False)}")
        remote_records = response.get("add_records") or []
        for batch_index, record in enumerate(batch):
            issue_key = str(record["issue"].get("key") or "")
            remote = remote_records[batch_index] if batch_index < len(remote_records) else {}
            synced[issue_key] = {
                "record_id": remote.get("record_id") or "",
                "synced_at": datetime.now().astimezone().isoformat(),
                "date": date_text,
            }
            successful += 1
        ledger.update({"date": date_text, "updated_at": datetime.now().astimezone().isoformat()})
        save_ledger(state_path, ledger)

    return {
        "total": len(issues),
        "synced": successful,
        "skipped": len(issues) - len(pending),
        "state": str(state_path),
    }


def load_issue_definitions(day_dir: str | Path, date_text: str) -> list[dict]:
    path = Path(day_dir) / "grouped_issues" / f"issue_definitions_{date_text.replace('-', '')}.json"
    if not path.exists():
        raise FileNotFoundError(f"未找到 AI 问题清单，请先执行大模型分析: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    issues = data if isinstance(data, list) else data.get("issues")
    if not isinstance(issues, list):
        raise ValueError(f"问题清单格式无效: {path}")
    return issues


def issue_values(config: dict, issue: dict, date_text: str, image_values: list[dict]) -> dict:
    smart = config.get("smart_sheet") or {}
    defaults = smart.get("defaults") or {}
    return {
        "f04Gwj": single_select(issue.get("module_text") or defaults.get("module_text") or "待评估"),
        "ftk5Tx": issue.get("problem_description") or "",
        "fMAfWQ": issue.get("reason") or "",
        "fn8TJd": image_values,
        "fb19Ra": issue.get("review_text") or defaults.get("review_text") or "",
        "fIgBdy": single_select(defaults.get("status_text") or "待评估"),
        "fsFBqK": single_select(issue.get("issue_category_text") or defaults.get("issue_category_text") or "待评估"),
        "fOXTRh": multiple_select(issue.get("typical_case_texts") or defaults.get("typical_case_texts") or []),
        "ftQMc5": utc_date_millis(date_text),
        "fgIJEu": issue.get("online_issue_text") or defaults.get("online_issue_text") or "",
        "fhK1MH": issue.get("jira_url") or defaults.get("jira_url") or "",
        "fs9xhZ": issue.get("issue_summary_text") or "",
        "fV6BDR": issue.get("start_time_text") or defaults.get("start_time_text") or "",
        "fDLv3b": issue.get("end_time_text") or defaults.get("end_time_text") or "",
    }


def single_select(value) -> list[dict]:
    text = str(value or "").strip()
    return [{"text": text}] if text else []


def multiple_select(values) -> list[dict]:
    return [{"text": str(value)} for value in values if str(value).strip()]


def utc_date_millis(date_text: str) -> str:
    value = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return str(int(value.timestamp() * 1000))


def load_manifest(day_dir: str | Path) -> dict[str, Path]:
    path = Path(day_dir) / "raw_attachments" / "_bulk_hd_cache" / "hd_cache_manifest.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    result = {}
    for record in data.get("records") or []:
        filename = str(record.get("filename") or "")
        local_path = Path(str(record.get("local_path") or ""))
        parts = filename.split("_", 2)
        if len(parts) >= 2 and local_path.exists():
            result[f"bulk:{parts[0]}_{parts[1]}"] = local_path
    return result


def resolve_issue_images(issue: dict, manifest: dict[str, Path]) -> list[Path]:
    result = []
    seen = set()
    for ref in issue.get("image_refs") or []:
        path = manifest.get(str(ref))
        if path and str(path).lower() not in seen:
            seen.add(str(path).lower())
            result.append(path)
    return result


def get_access_token(upload: dict, corpid: str, corpsecret: str) -> str:
    endpoint = str(upload.get("token_endpoint") or "https://qyapi.weixin.qq.com/cgi-bin/gettoken")
    query = urllib.parse.urlencode({"corpid": corpid, "corpsecret": corpsecret})
    data = get_json(f"{endpoint}?{query}")
    if int(data.get("errcode", -1)) != 0 or not data.get("access_token"):
        raise RuntimeError(f"企业微信 access_token 获取失败: {json.dumps(data, ensure_ascii=False)}")
    return str(data["access_token"])


def upload_image(upload: dict, token: str, path: Path) -> str:
    endpoint = str(upload.get("image_upload_endpoint") or "https://qyapi.weixin.qq.com/cgi-bin/media/uploadimg")
    field = str(upload.get("image_form_field") or "media")
    boundary = f"----wecom{uuid.uuid4().hex}"
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body = bytearray()
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(f'Content-Disposition: form-data; name="{field}"; filename="{path.name}"\r\n'.encode("utf-8"))
    body.extend(f"Content-Type: {content_type}\r\n\r\n".encode())
    body.extend(path.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        f"{endpoint}?{urllib.parse.urlencode({'access_token': token})}",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    data = open_json(request)
    if int(data.get("errcode", -1)) != 0 or not data.get("url"):
        raise RuntimeError(f"截图上传失败 {path.name}: {json.dumps(data, ensure_ascii=False)}")
    return str(data["url"])


def get_json(url: str) -> dict:
    return open_json(urllib.request.Request(url, method="GET"))


def post_json(url: str, payload: dict) -> dict:
    return open_json(
        urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    )


def open_json(request: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"腾讯接口 HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接腾讯接口: {exc.reason}") from exc


def image_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                break
            marker = data[index + 1]
            length = int.from_bytes(data[index + 2 : index + 4], "big")
            if marker in range(0xC0, 0xC4):
                return int.from_bytes(data[index + 7 : index + 9], "big"), int.from_bytes(data[index + 5 : index + 7], "big")
            index += 2 + max(length, 2)
    return 0, 0


def ledger_path(day_dir: str | Path) -> Path:
    return Path(day_dir) / "smartsheet_desktop_sync_state.json"


def load_ledger(path: Path) -> dict:
    if not path.exists():
        return {"synced": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"synced": {}}
    except (OSError, json.JSONDecodeError):
        return {"synced": {}}


def save_ledger(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
