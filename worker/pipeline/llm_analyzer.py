from __future__ import annotations

import hashlib
import json
import re
import uuid
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .config_store import (
    configured_default_issue_fields,
    ensure_smart_sheet_config,
    selected_prompt,
    selected_smart_sheet_template,
)
from .exporter import datetime_range_label, load_ocr, read_jsonl
from .issue_schema import (
    LEGACY_MIRROR_KEYS,
    build_model_contract,
    normalize_issue_fields,
    normalize_issue_values,
)


ProgressCallback = Callable[[str], None]
COMMON_MODEL_RESPONSE_WRAPPERS = ("result", "data", "output", "response")


class ModelResponseFormatError(ValueError):
    """The model service responded, but its response cannot satisfy the issue contract."""


def analyze_day(
    config: dict,
    day_dir: str | Path,
    date_text: str,
    group_name: str,
    prompt_id: str | None = None,
    progress: ProgressCallback | None = None,
    start_date: str | None = None,
    start_time: str = "00:00",
    end_date: str | None = None,
    end_time: str = "23:59",
) -> Path:
    notify = progress or (lambda _message: None)
    day_path = Path(day_dir)
    raw_path = day_path / "raw_messages.jsonl"
    messages = read_jsonl(raw_path)
    if not messages:
        raise ValueError("当天没有可分析的聊天记录")
    ocr_map = load_ocr(day_path / "grouped_issues" / "image_ocr.jsonl")
    prompt = selected_prompt(config, prompt_id)
    llm_config = config.get("llm") or {}
    max_input_chars = max(int(llm_config.get("max_input_chars") or 80000), 10000)
    compact = [compact_message(message, ocr_map) for message in messages]
    batches = chunk_messages(compact, max_input_chars=max_input_chars, overlap=8)
    notify(f"使用提示词“{prompt['name']}”，共 {len(batches)} 个分析批次")

    candidates = []
    analysis_range = datetime_range_label(
        start_date or date_text,
        start_time,
        end_date or date_text,
        end_time,
    )
    for index, batch in enumerate(batches, start=1):
        notify(f"正在调用大模型：第 {index}/{len(batches)} 批")
        instruction = build_instruction(
            prompt["content"],
            analysis_range,
            group_name,
            config,
            issue_fields=prompt.get("issue_fields"),
            batch_index=index,
            batch_count=len(batches),
        )
        parsed = analyze_batch_with_retry(
            llm_config,
            instruction,
            batch,
            batch_index=index,
            batch_count=len(batches),
            progress=notify,
        )
        candidates.extend(parsed.get("issues") or [])

    definitions = build_issue_definitions(
        messages=messages,
        model_issues=candidates,
        day_dir=day_path,
        date_text=date_text,
        prompt=prompt,
        config=config,
        start_date=start_date,
        start_time=start_time,
        end_date=end_date,
        end_time=end_time,
    )
    grouped_dir = day_path / "grouped_issues"
    grouped_dir.mkdir(parents=True, exist_ok=True)
    ymd = date_text.replace("-", "")
    serialized = json.dumps(definitions, ensure_ascii=False, indent=2) + "\n"
    snapshot_dir = grouped_dir / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot = snapshot_dir / f"issue_definitions_{ymd}_{uuid.uuid4().hex}.json"
    with snapshot.open("x", encoding="utf-8") as snapshot_file:
        snapshot_file.write(serialized)
    canonical = grouped_dir / f"issue_definitions_{ymd}.json"
    temporary = grouped_dir / f".{canonical.name}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(serialized, encoding="utf-8")
        temporary.replace(canonical)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    notify(f"大模型分析完成：识别 {len(definitions['issues'])} 个问题")
    return snapshot


def analyze_batch_with_retry(
    llm_config: dict,
    instruction: str,
    batch: list[dict],
    *,
    batch_index: int,
    batch_count: int,
    progress: ProgressCallback,
) -> dict:
    valid_message_ids = {
        as_int(message.get("message_id"))
        for message in batch
        if as_int(message.get("message_id")) > 0
    }
    retry_reason = ""
    for attempt in range(2):
        current_instruction = instruction
        if attempt:
            current_instruction = build_correction_instruction(instruction, retry_reason)
        try:
            response_text = call_model(llm_config, current_instruction, batch)
            parsed = parse_model_json(response_text)
        except ModelResponseFormatError as exc:
            if attempt == 0:
                retry_reason = "format"
                progress(
                    f"第 {batch_index}/{batch_count} 批返回格式异常，正在自动纠正重试（1/1）"
                )
                continue
            raise ModelResponseFormatError(
                f"大模型纠正重试后返回格式仍不符合要求：{exc}"
            ) from exc

        issues = parsed.get("issues") or []
        if issues and not any(
            isinstance(issue, dict)
            and as_int(issue.get("seed_message_id")) in valid_message_ids
            for issue in issues
        ):
            if attempt == 0:
                retry_reason = "message_ids"
                progress(
                    f"第 {batch_index}/{batch_count} 批候选问题的消息 ID 无效，正在自动纠正重试（1/1）"
                )
                continue
            raise ModelResponseFormatError(
                "大模型纠正重试后返回的候选问题仍未引用当前批次中的有效消息 ID"
            )
        return parsed

    raise ModelResponseFormatError("大模型返回无法完成自动纠正")


def build_correction_instruction(instruction: str, reason: str) -> str:
    if reason == "message_ids":
        correction = (
            "上一次返回的候选问题没有引用有效消息。请重新分析，并从输入消息的 "
            "message_id 中原样复制 seed_message_id、context_message_ids 和 "
            "question_message_ids；不要添加前缀或自行生成 ID。"
        )
    else:
        correction = (
            "上一次返回无法按约定 JSON 结构解析。请重新分析，只返回一个 JSON 对象，"
            "顶层必须且只能使用 issues 数组，不要添加解释、Markdown 或额外包装层。"
        )
    return f"{instruction}\n\n纠正重试要求：{correction}"


def compact_message(message: dict, ocr_map: dict[int, str]) -> dict:
    message_id = int(message.get("message_id") or 0)
    return {
        "message_id": message_id,
        "time": message.get("message_time") or "",
        "sender": message.get("sender") or "",
        "text": message.get("raw_text") or "",
        "ocr": ocr_map.get(message_id, ""),
        "image_count": int(message.get("image_count_visible") or 0),
        "type": message.get("type") or "",
    }


def chunk_messages(messages: list[dict], max_input_chars: int, overlap: int = 8) -> list[list[dict]]:
    batches: list[list[dict]] = []
    current: list[dict] = []
    current_size = 0
    for message in messages:
        size = len(json.dumps(message, ensure_ascii=False)) + 1
        if current and current_size + size > max_input_chars:
            batches.append(current)
            current = current[-overlap:] if overlap else []
            current_size = sum(len(json.dumps(item, ensure_ascii=False)) + 1 for item in current)
        current.append(message)
        current_size += size
    if current:
        batches.append(current)
    return batches


def build_instruction(
    user_prompt: str,
    date_text: str,
    group_name: str,
    config: dict,
    *,
    issue_fields: list[dict] | None = None,
    batch_index: int,
    batch_count: int,
) -> str:
    normalized_config = ensure_smart_sheet_config(config)
    fields = normalize_issue_fields(
        issue_fields,
        configured_default_issue_fields(normalized_config),
    )
    rendered = str(user_prompt or "").replace("{date}", date_text).replace("{group_name}", group_name)
    return f"""{rendered}

分析范围：{date_text}，群：{group_name}。
当前为第 {batch_index}/{batch_count} 批；只根据本批提供的消息判断，不得编造。

{build_model_contract(fields)}"""


def enum_values(config: dict, field_id: str) -> list[str]:
    try:
        schema = selected_smart_sheet_template(
            ensure_smart_sheet_config(config)
        ).get("schema") or {}
    except ValueError:
        schema = {}
    values = (schema.get(field_id) or {}).get("enum") or []
    return [str(value) for value in values]


def call_model(llm_config: dict, instruction: str, messages: list[dict]) -> str:
    provider = str(llm_config.get("provider") or "openai_compatible").lower()
    base_url = str(llm_config.get("base_url") or "").rstrip("/")
    api_key = str(llm_config.get("api_key") or "")
    model = str(llm_config.get("model") or "")
    timeout = max(int(llm_config.get("timeout_seconds") or 180), 10)
    max_tokens = max(int(llm_config.get("max_output_tokens") or 12000), 1000)
    if not base_url or not model:
        raise ValueError("请先在设置页填写大模型 Base URL 和模型名称")

    content = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    if provider == "anthropic":
        if base_url.endswith("/v1/messages"):
            endpoint = base_url
        elif base_url.endswith("/v1"):
            endpoint = f"{base_url}/messages"
        else:
            endpoint = f"{base_url}/v1/messages"
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": float(llm_config.get("temperature") or 0.1),
            "system": instruction,
            "messages": [{"role": "user", "content": f"以下是按时间排序的群聊 JSON：\n{content}"}],
        }
        headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        if api_key:
            headers["x-api-key"] = api_key
    else:
        if base_url.endswith("/chat/completions"):
            endpoint = base_url
        elif base_url.endswith("/v1"):
            endpoint = f"{base_url}/chat/completions"
        else:
            endpoint = f"{base_url}/v1/chat/completions"
        body = {
            "model": model,
            "temperature": float(llm_config.get("temperature") or 0.1),
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": f"以下是按时间排序的群聊 JSON：\n{content}"},
            ],
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

    try:
        data = request_json(endpoint, body, headers, timeout)
    except RuntimeError as exc:
        if provider != "anthropic" and "response_format" in body and "HTTP 400" in str(exc):
            body.pop("response_format", None)
            data = request_json(endpoint, body, headers, timeout)
        else:
            raise
    if not isinstance(data, dict):
        raise ModelResponseFormatError("大模型响应顶层必须是 JSON 对象")
    if provider == "anthropic":
        blocks = data.get("content") or []
        return "\n".join(str(block.get("text") or "") for block in blocks if block.get("type") == "text")
    choices = data.get("choices") or []
    if not choices:
        raise ModelResponseFormatError(
            f"大模型响应中没有 choices: {json.dumps(data, ensure_ascii=False)[:500]}"
        )
    content_value = (choices[0].get("message") or {}).get("content") or ""
    if isinstance(content_value, list):
        return "\n".join(str(item.get("text") or item.get("content") or "") for item in content_value if isinstance(item, dict))
    return str(content_value)


def request_json(url: str, body: dict, headers: dict[str, str], timeout: int) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"大模型请求失败 HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接大模型服务: {exc.reason}") from exc


def parse_model_json(text: str) -> dict:
    value = str(text or "").strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            raise ModelResponseFormatError(f"大模型没有返回有效 JSON: {value[:500]}")
        try:
            parsed = json.loads(value[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ModelResponseFormatError(
                f"大模型没有返回有效 JSON: {value[:500]}"
            ) from exc
    normalized = normalize_model_response(parsed)
    if normalized is None:
        raise ModelResponseFormatError("大模型 JSON 必须包含 issues 数组")
    return normalized


def normalize_model_response(value, depth: int = 0) -> dict | None:
    if depth > 4:
        return None
    if isinstance(value, list):
        return {"issues": value}
    if isinstance(value, str):
        try:
            nested = json.loads(value)
        except json.JSONDecodeError:
            return None
        return normalize_model_response(nested, depth + 1)
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("issues"), list):
        return value
    for key in COMMON_MODEL_RESPONSE_WRAPPERS:
        if key not in value:
            continue
        normalized = normalize_model_response(value[key], depth + 1)
        if normalized is not None:
            return normalized
    return None


def build_issue_definitions(
    *,
    messages: list[dict],
    model_issues: list[dict],
    day_dir: Path,
    date_text: str,
    prompt: dict,
    config: dict | None = None,
    start_date: str | None = None,
    start_time: str = "00:00",
    end_date: str | None = None,
    end_time: str = "23:59",
) -> dict:
    message_map = {int(message.get("message_id") or 0): message for message in messages}
    image_refs = load_image_refs(day_dir)
    deduped: dict[int, dict] = {}
    for issue in model_issues:
        if not isinstance(issue, dict):
            continue
        seed_id = as_int(issue.get("seed_message_id"))
        if seed_id not in message_map:
            continue
        if seed_id not in deduped:
            deduped[seed_id] = issue

    config = ensure_smart_sheet_config(config or {})
    fields = normalize_issue_fields(
        prompt.get("issue_fields"),
        configured_default_issue_fields(config),
    )
    result = []
    for ordinal, seed_id in enumerate(sorted(deduped, key=lambda item: int(message_map[item].get("send_time") or 0)), start=1):
        issue = deduped[seed_id]
        seed = message_map[seed_id]
        context_ids = [as_int(value) for value in issue.get("context_message_ids") or []]
        context_ids = [value for value in context_ids if value in message_map]
        if seed_id not in context_ids:
            context_ids.insert(0, seed_id)
        context_ids = sorted(set(context_ids), key=lambda item: int(message_map[item].get("send_time") or 0))
        question_ids = {as_int(value) for value in issue.get("question_message_ids") or []}
        question_ids.add(seed_id)
        timeline = []
        all_refs = []
        assignments = []
        for message_id in context_ids:
            message = message_map[message_id]
            refs = image_refs.get(message_id, [])
            all_refs.extend(refs)
            role = "question" if message_id in question_ids else "reply"
            if refs and message_id not in question_ids:
                role = "image_context"
            timeline.append(
                {
                    "message_time": message.get("message_time") or "",
                    "send_time": int(message.get("send_time") or 0),
                    "sender": message.get("sender") or "",
                    "sender_id": message.get("sender_id") or 0,
                    "role": role,
                    "raw_text": message.get("raw_text") or "",
                    "raw_message_key": message.get("dedupe_key") or "",
                    "message_id": message_id,
                    "server_id": message.get("server_id") or 0,
                    "image_refs": refs,
                    "image_count_visible": int(message.get("image_count_visible") or 0),
                }
            )
            for ref in refs:
                assignments.append(
                    {
                        "ref": ref,
                        "method": "configured_llm_context_match",
                        "source_message_id": message_id,
                        "message_time": message.get("message_time") or "",
                        "sender": message.get("sender") or "",
                    }
                )
        all_refs = list(dict.fromkeys(all_refs))
        expected_images = sum(int(message_map[item].get("image_count_visible") or 0) for item in context_ids)
        dedupe_key = str(seed.get("dedupe_key") or seed_id)
        values = normalize_issue_values(
            issue,
            fields,
            {"problem_description": seed.get("raw_text") or ""},
        )
        record = {
            "key": f"issue_{ordinal:03d}_{hashlib.sha256(dedupe_key.encode('utf-8')).hexdigest()[:12]}",
            "tenant": "",
            "sender": seed.get("sender") or "",
            "sender_id": seed.get("sender_id") or 0,
            "message_time": seed.get("message_time") or "",
            "values": values,
            "module_inference": "configured_llm" if "module_text" in values else "",
            "raw_message_keys": [dedupe_key],
            "context_message_keys": [message_map[item].get("dedupe_key") or "" for item in context_ids],
            "expected_image_count": expected_images,
            "image_refs": all_refs,
            "image_assignments": assignments,
            "image_status": "ready" if all_refs else ("missing_original_images" if expected_images else "not_required"),
            "missing_image_names": [],
            "timeline": timeline,
        }
        for key in LEGACY_MIRROR_KEYS:
            if key in values:
                record[key] = values[key]
        result.append(record)

    referenced_images = {
        str(ref)
        for issue in result
        for ref in issue.get("image_refs") or []
    }
    available_images = load_image_manifest_snapshot(day_dir)
    return {
        "date": date_text,
        "range": {
            "startDate": start_date or date_text,
            "startTime": start_time,
            "endDate": end_date or date_text,
            "endTime": end_time,
        },
        "schema_version": 2,
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "generated_by": "configured_llm",
        "prompt": {
            "id": prompt.get("id"),
            "name": prompt.get("name"),
            "default_smart_sheet_template_id": prompt.get(
                "default_smart_sheet_template_id"
            )
            or "",
        },
        "issue_fields": fields,
        "image_manifest": {
            ref: available_images[ref]
            for ref in sorted(referenced_images)
            if ref in available_images
        },
        "issues": result,
    }


def load_image_refs(day_dir: Path) -> dict[int, list[str]]:
    manifest = day_dir / "raw_attachments" / "_bulk_hd_cache" / "hd_cache_manifest.json"
    if not manifest.exists():
        return {}
    data = json.loads(manifest.read_text(encoding="utf-8"))
    result: dict[int, list[str]] = {}
    for record in data.get("records") or []:
        if record.get("accepted") is False:
            continue
        message_id = as_int(record.get("source_message_id"))
        filename = str(record.get("filename") or "")
        match = re.match(r"^(\d+_\d{2})_", filename)
        if message_id and match:
            result.setdefault(message_id, []).append(f"bulk:{match.group(1)}")
    return result


def load_image_manifest_snapshot(day_dir: Path) -> dict[str, str]:
    manifest = day_dir / "raw_attachments" / "_bulk_hd_cache" / "hd_cache_manifest.json"
    if not manifest.exists():
        return {}
    data = json.loads(manifest.read_text(encoding="utf-8"))
    result = {}
    for record in data.get("records") or []:
        if record.get("accepted") is False:
            continue
        filename = str(record.get("filename") or "")
        local_path = str(record.get("local_path") or "").strip()
        match = re.match(r"^(\d+_\d{2})_", filename)
        if match and local_path:
            result[f"bulk:{match.group(1)}"] = local_path
    return result


def as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_enum(value, allowed: list[str], fallback: str) -> str:
    text = str(value or "").strip()
    if not allowed or text in allowed:
        return text or fallback
    return fallback
