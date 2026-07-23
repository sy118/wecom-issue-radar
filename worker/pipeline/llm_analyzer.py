from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .config_store import selected_prompt
from .exporter import load_ocr, read_jsonl


ProgressCallback = Callable[[str], None]


def analyze_day(
    config: dict,
    day_dir: str | Path,
    date_text: str,
    group_name: str,
    prompt_id: str | None = None,
    progress: ProgressCallback | None = None,
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
    for index, batch in enumerate(batches, start=1):
        notify(f"正在调用大模型：第 {index}/{len(batches)} 批")
        instruction = build_instruction(
            prompt["content"],
            date_text,
            group_name,
            config,
            batch_index=index,
            batch_count=len(batches),
        )
        response_text = call_model(llm_config, instruction, batch)
        parsed = parse_model_json(response_text)
        candidates.extend(parsed.get("issues") or [])

    definitions = build_issue_definitions(
        messages=messages,
        model_issues=candidates,
        day_dir=day_path,
        date_text=date_text,
        prompt=prompt,
        config=config,
    )
    grouped_dir = day_path / "grouped_issues"
    grouped_dir.mkdir(parents=True, exist_ok=True)
    output = grouped_dir / f"issue_definitions_{date_text.replace('-', '')}.json"
    output.write_text(json.dumps(definitions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    notify(f"大模型分析完成：识别 {len(definitions['issues'])} 个问题")
    return output


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
    batch_index: int,
    batch_count: int,
) -> str:
    modules = enum_values(config, "f04Gwj")
    categories = enum_values(config, "fsFBqK")
    rendered = str(user_prompt or "").replace("{date}", date_text).replace("{group_name}", group_name)
    return f"""{rendered}

分析范围：{date_text}，群：{group_name}。
当前为第 {batch_index}/{batch_count} 批；只根据本批提供的消息判断，不得编造。

模块只能从以下值选择：{json.dumps(modules, ensure_ascii=False)}
问题分类只能从以下值选择：{json.dumps(categories, ensure_ascii=False)}

只返回 JSON，不要 Markdown 代码块，结构必须是：
{{
  "issues": [
    {{
      "seed_message_id": 123,
      "context_message_ids": [123, 124],
      "question_message_ids": [123],
      "module_text": "模块枚举值",
      "issue_category_text": "分类枚举值",
      "problem_description": "简洁、客观的问题描述",
      "issue_summary_text": "20字以内总结",
      "reason": "结论：有依据的结论或待确认\\n时间轴：\\n- HH:MM 发送人：关键内容"
    }}
  ]
}}
没有问题时返回 {{"issues": []}}。seed_message_id 和 context_message_ids 必须来自输入消息。"""


def enum_values(config: dict, field_id: str) -> list[str]:
    schema = ((config.get("smart_sheet") or {}).get("schema") or {})
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
    if provider == "anthropic":
        blocks = data.get("content") or []
        return "\n".join(str(block.get("text") or "") for block in blocks if block.get("type") == "text")
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"大模型响应中没有 choices: {json.dumps(data, ensure_ascii=False)[:500]}")
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
            raise ValueError(f"大模型没有返回有效 JSON: {value[:500]}")
        parsed = json.loads(value[start : end + 1])
    if isinstance(parsed, list):
        return {"issues": parsed}
    if not isinstance(parsed, dict) or not isinstance(parsed.get("issues"), list):
        raise ValueError("大模型 JSON 必须包含 issues 数组")
    return parsed


def build_issue_definitions(
    *,
    messages: list[dict],
    model_issues: list[dict],
    day_dir: Path,
    date_text: str,
    prompt: dict,
    config: dict | None = None,
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

    config = config or {}
    module_values = enum_values(config, "f04Gwj")
    category_values = enum_values(config, "fsFBqK")
    defaults = (config.get("smart_sheet") or {}).get("defaults") or {}
    default_module = defaults.get("module_text") or (module_values[0] if module_values else "待评估")
    default_category = defaults.get("issue_category_text") or ("待评估" if "待评估" in category_values else (category_values[0] if category_values else "待评估"))
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
        result.append(
            {
                "key": f"issue_{ordinal:03d}_{hashlib.sha256(dedupe_key.encode('utf-8')).hexdigest()[:12]}",
                "tenant": "",
                "sender": seed.get("sender") or "",
                "sender_id": seed.get("sender_id") or 0,
                "message_time": seed.get("message_time") or "",
                "problem_description": str(issue.get("problem_description") or seed.get("raw_text") or "").strip(),
                "module_text": normalize_enum(issue.get("module_text"), module_values, default_module),
                "module_inference": "configured_llm",
                "issue_category_text": normalize_enum(issue.get("issue_category_text"), category_values, default_category),
                "issue_summary_text": str(issue.get("issue_summary_text") or "").strip(),
                "reason": str(issue.get("reason") or "结论：待确认").strip(),
                "raw_message_keys": [dedupe_key],
                "context_message_keys": [message_map[item].get("dedupe_key") or "" for item in context_ids],
                "expected_image_count": expected_images,
                "image_refs": all_refs,
                "image_assignments": assignments,
                "image_status": "ready" if all_refs else ("missing_original_images" if expected_images else "not_required"),
                "missing_image_names": [],
                "timeline": timeline,
            }
        )

    return {
        "date": date_text,
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "generated_by": "configured_llm",
        "prompt": {"id": prompt.get("id"), "name": prompt.get("name")},
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
