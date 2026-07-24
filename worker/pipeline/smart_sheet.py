from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import mimetypes
import os
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .config_store import ensure_smart_sheet_config, selected_smart_sheet_template
from .exporter import load_issue_document
from .issue_schema import display_value, issue_value


ProgressCallback = Callable[[str], None]
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
SYNC_LOCK_TIMEOUT_SECONDS = 30.0
SYNC_LOCK_POLL_SECONDS = 0.05


SYSTEM_SOURCES = {"$date", "$images", "$sender", "$message_time", "$issue_key"}
SYSTEM_SOURCE_TYPES = {
    "$date": "date",
    "$images": "image",
    "$sender": "text",
    "$message_time": "datetime",
    "$issue_key": "text",
}
SOURCE_TARGET_TYPES = {
    "text": {"text", "single_select", "multiple_select", "url"},
    "long_text": {"text", "single_select", "multiple_select", "url"},
    "single_select": {"text", "single_select", "multiple_select"},
    "multiple_select": {"text", "multiple_select"},
    "boolean": {"text", "boolean", "checkbox"},
    "number": {"text", "number"},
    "date": {"text", "date_time"},
    "datetime": {"text", "date_time"},
    "url": {"text", "url"},
    "image": {"image"},
}


def preview_sync(
    config: dict,
    day_dir: str | Path,
    date_text: str,
    template_id: str | None = None,
    *,
    definition_path: str | Path | None = None,
) -> dict:
    document = load_sync_document(
        day_dir,
        date_text,
        definition_path=definition_path,
    )
    revision = document_revision(document, date_text)
    issues = document["issues"]
    validate_issue_keys(issues)
    template = resolve_template(config, document, template_id)
    validation_error = ""
    try:
        validate_template(template, document["issue_fields"])
    except ValueError as exc:
        validation_error = str(exc)
    ledger = load_ledger(ledger_path(day_dir))
    synced = template_synced(ledger, template["id"])
    synced_identities = {issue_dedupe_identity(key) for key in synced}
    pending = [
        issue
        for issue in issues
        if issue_dedupe_identity(issue.get("key")) not in synced_identities
    ]
    if not validation_error:
        maps_images = any(
            mapping.get("source_key") == "$images"
            for mapping in template.get("field_mappings") or []
        )
        try:
            for issue in pending:
                values = issue_values(template, issue, date_text, [])
                validate_record_values(
                    template,
                    issue,
                    values,
                    allow_pending_images=bool(maps_images and issue.get("image_refs")),
                )
        except ValueError as exc:
            validation_error = str(exc)
    webhook_url = resolve_webhook_url(template)
    return {
        "total": len(issues),
        "pending": len(pending),
        "already_synced": len(issues) - len(pending),
        "configured": bool(webhook_url),
        "webhook_configured": bool(webhook_url),
        "template_id": template["id"],
        "template_name": template.get("name") or template["id"],
        "template_url": template.get("url") or "",
        "template_revision": template_revision(
            template,
            resolved_webhook_url=webhook_url,
        ),
        "document_revision": revision,
        "definition_path": document.get("definition_path") or "",
        "mapping_valid": not validation_error,
        "validation_error": validation_error,
    }


def sync_issues(
    config: dict,
    day_dir: str | Path,
    date_text: str,
    *,
    template_id: str | None = None,
    upload_images: bool = True,
    force: bool = False,
    definition_path: str | Path | None = None,
    expected_template_revision: str | None = None,
    expected_document_revision: str | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    notify = progress or (lambda _message: None)
    document = load_sync_document(
        day_dir,
        date_text,
        definition_path=definition_path,
    )
    revision = document_revision(document, date_text)
    if (
        expected_document_revision is not None
        and str(expected_document_revision) != revision
    ):
        raise ValueError("问题清单已变化，请刷新预览后再确认")
    issues = document["issues"]
    validate_issue_keys(issues)
    template = resolve_template(config, document, template_id)
    webhook_url = resolve_webhook_url(template)
    revision = template_revision(template, resolved_webhook_url=webhook_url)
    if (
        expected_template_revision is not None
        and str(expected_template_revision) != revision
    ):
        raise ValueError("腾讯文档模板配置已变化，请刷新预览后再确认")
    validate_template(template, document["issue_fields"])
    if not webhook_url:
        raise ValueError(f"请先为腾讯文档模板“{template.get('name') or template['id']}”配置 Webhook URL")
    state_path = ledger_path(day_dir)
    with ledger_sync_lock(state_path):
        return _sync_issues_locked(
            config,
            day_dir,
            date_text,
            upload_images=upload_images,
            force=force,
            notify=notify,
            issues=issues,
            template=template,
            webhook_url=webhook_url,
            state_path=state_path,
            frozen_image_manifest=document.get("image_manifest"),
        )


def _sync_issues_locked(
    config: dict,
    day_dir: str | Path,
    date_text: str,
    *,
    upload_images: bool,
    force: bool,
    notify: ProgressCallback,
    issues: list[dict],
    template: dict,
    webhook_url: str,
    state_path: Path,
    frozen_image_manifest: dict | None,
) -> dict:
    ledger = load_ledger(state_path)
    synced = template_synced(ledger, template["id"])
    synced_identities = {issue_dedupe_identity(key) for key in synced}
    pending = issues if force else [
        issue
        for issue in issues
        if issue_dedupe_identity(issue.get("key")) not in synced_identities
    ]
    if not pending:
        return {
            "total": len(issues),
            "synced": 0,
            "skipped": len(issues),
            "state": str(state_path),
            "template_id": template["id"],
            "template_name": template.get("name") or template["id"],
        }

    maps_images = any(
        mapping.get("source_key") == "$images"
        for mapping in template.get("field_mappings") or []
    )
    manifest = (
        load_sync_image_manifest(day_dir, frozen_image_manifest)
        if upload_images and maps_images
        else {}
    )
    prepared_issues = []
    for issue in pending:
        prepared_images: list[tuple[Path, int, int]] = []
        if upload_images and maps_images:
            refs = list(
                dict.fromkeys(
                    str(ref).strip()
                    for ref in issue.get("image_refs") or []
                    if str(ref).strip()
                )
            )
            missing_refs = [
                ref
                for ref in refs
                if ref not in manifest or not manifest[ref].is_file()
            ]
            if missing_refs:
                raise ValueError(
                    f"问题 {issue.get('key')} 的截图引用找不到本地文件："
                    f"{'、'.join(missing_refs)}"
                )
            for image_path in resolve_issue_images(issue, manifest):
                try:
                    width, height = image_dimensions(image_path)
                except OSError as exc:
                    raise ValueError(
                        f"问题 {issue.get('key')} 的截图本地文件无法读取：{image_path}"
                    ) from exc
                if width <= 0 or height <= 0:
                    raise ValueError(
                        f"问题 {issue.get('key')} 的截图无法识别有效尺寸：{image_path}"
                    )
                prepared_images.append((image_path, width, height))
        preflight_values = issue_values(template, issue, date_text, [])
        validate_record_values(
            template,
            issue,
            preflight_values,
            allow_pending_images=bool(upload_images and prepared_images),
        )
        prepared_issues.append((issue, prepared_images))

    records = []
    for index, (issue, prepared_images) in enumerate(prepared_issues, start=1):
        notify(f"正在准备腾讯文档记录 {index}/{len(pending)}…")
        image_values = []
        if prepared_images:
            for image_path, _width, _height in prepared_images:
                image_values.append(
                    {
                        "title": image_path.name,
                        "image_base64": base64.b64encode(
                            image_path.read_bytes()
                        ).decode("ascii"),
                    }
                )
        values = issue_values(template, issue, date_text, image_values)
        validate_record_values(template, issue, values)
        records.append({"issue": issue, "values": values})

    batch_size = max(1, min(int(template.get("batch_size") or 50), 50))
    successful = 0
    for offset in range(0, len(records), batch_size):
        batch = records[offset : offset + batch_size]
        notify(f"正在写入腾讯文档：第 {offset + 1}-{offset + len(batch)} 条…")
        payload = {
            "schema": template.get("schema") or {},
            "add_records": [{"values": record["values"]} for record in batch],
        }
        response = post_json(webhook_url, payload)
        if int(response.get("errcode", -1)) != 0:
            raise RuntimeError(f"腾讯文档写入失败: {json.dumps(response, ensure_ascii=False)}")
        remote_records = response.get("add_records")
        if not isinstance(remote_records, list):
            remote_records = []
        confirmed = 0
        for batch_index, remote in enumerate(remote_records[: len(batch)]):
            if not isinstance(remote, dict):
                continue
            record_id = str(remote.get("record_id") or "").strip()
            if not record_id:
                continue
            issue_key = str(batch[batch_index]["issue"].get("key") or "")
            synced[issue_key] = {
                "record_id": record_id,
                "synced_at": datetime.now().astimezone().isoformat(),
                "date": date_text,
            }
            successful += 1
            confirmed += 1
        if confirmed:
            ledger.update({"date": date_text, "updated_at": datetime.now().astimezone().isoformat()})
            save_ledger(state_path, ledger)
        if len(remote_records) != len(batch):
            raise RuntimeError(
                f"腾讯文档仅确认写入 {confirmed}/{len(batch)} 条；已保存确认记录，其余保持待同步"
            )
        if confirmed != len(batch):
            raise RuntimeError(
                f"腾讯文档仅返回 {confirmed}/{len(batch)} 个有效记录 ID；已保存确认记录，其余保持待同步"
            )

    return {
        "total": len(issues),
        "synced": successful,
        "skipped": len(issues) - len(pending),
        "state": str(state_path),
        "template_id": template["id"],
        "template_name": template.get("name") or template["id"],
    }


def load_issue_definitions(
    day_dir: str | Path,
    date_text: str,
    definition_path: str | Path | None = None,
) -> list[dict]:
    return load_sync_document(
        day_dir,
        date_text,
        definition_path=definition_path,
    )["issues"]


def load_sync_document(
    day_dir: str | Path,
    date_text: str,
    definition_path: str | Path | None = None,
) -> dict:
    if definition_path is None or not str(definition_path).strip():
        raise ValueError("缺少不可变问题清单快照，请重新执行大模型分析后再同步")
    day_path = Path(day_dir)
    candidate = Path(definition_path)
    if not candidate.is_absolute():
        candidate = day_path / candidate
    candidate = candidate.resolve()
    snapshot_root = (day_path / "grouped_issues" / "snapshots").resolve()
    try:
        candidate.relative_to(snapshot_root)
    except ValueError as exc:
        raise ValueError(
            f"同步必须使用运行时生成的不可变问题清单快照: {candidate}"
        ) from exc
    document = load_issue_document(
        day_path,
        date_text,
        definition_path=candidate,
    )
    if not isinstance(document.get("image_manifest"), dict):
        raise ValueError("问题清单快照缺少冻结图片清单，请重新执行大模型分析")
    normalized_manifest = load_sync_image_manifest(
        day_path,
        document["image_manifest"],
    )
    document["image_manifest"] = {
        ref: str(path)
        for ref, path in sorted(normalized_manifest.items())
    }
    return document


def issue_values(template: dict, issue: dict, date_text: str, image_values: list[dict]) -> dict:
    result = {}
    schema = template.get("schema") or {}
    for mapping in template.get("field_mappings") or []:
        target = str(mapping.get("target_field_id") or "")
        if not target:
            continue
        source = str(mapping.get("source_key") or "")
        value = mapping_source_value(source, issue, date_text, image_values)
        if is_empty(value) and not is_empty(mapping.get("default_value")):
            value = mapping.get("default_value")
        target_schema = schema.get(target) if isinstance(schema.get(target), dict) else {}
        target_type = str(mapping.get("target_type") or target_schema.get("type") or "text")
        converted = convert_target_value(value, target_type, target_schema, date_text)
        required = bool(mapping.get("required")) or str(
            target_schema.get("title") or ""
        ).startswith("*")
        if is_empty(converted) and not required:
            continue
        result[target] = converted
    return result


def resolve_template(config: dict, document: dict, template_id: str | None = None) -> dict:
    normalized = ensure_smart_sheet_config(config)
    if template_id:
        return selected_smart_sheet_template(normalized, template_id)
    prompt_default = str(
        ((document.get("prompt") or {}).get("default_smart_sheet_template_id") or "")
    )
    if prompt_default:
        return selected_smart_sheet_template(normalized, prompt_default)
    return selected_smart_sheet_template(normalized)


def resolve_webhook_url(template: dict) -> str:
    direct = str(template.get("webhook_url") or "").strip()
    if direct:
        return direct
    env_name = str(template.get("webhook_url_env") or "").strip()
    return str(os.environ.get(env_name) or "").strip() if env_name else ""


def template_revision(
    template: dict,
    *,
    resolved_webhook_url: str | None = None,
) -> str:
    revision_source = {
        "id": str(template.get("id") or ""),
        "name": str(template.get("name") or ""),
        "url": str(template.get("url") or ""),
        "webhook_url": (
            resolve_webhook_url(template)
            if resolved_webhook_url is None
            else resolved_webhook_url
        ),
        "batch_size": template.get("batch_size") or 50,
        "schema": template.get("schema") or {},
        "field_mappings": template.get("field_mappings") or [],
    }
    canonical = json.dumps(
        revision_source,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def document_revision(document: dict, date_text: str = "") -> str:
    revision_source = {
        "date": date_text or document.get("date") or "",
        "image_manifest": document.get("image_manifest"),
        "issue_fields": document.get("issue_fields") or [],
        "issues": document.get("issues") or [],
        "prompt": document.get("prompt") or {},
    }
    canonical = json.dumps(
        revision_source,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_template(template: dict, issue_fields: list[dict]) -> None:
    schema = template.get("schema") or {}
    if not isinstance(schema, dict) or not schema:
        raise ValueError(f"腾讯文档模板“{template.get('name') or template.get('id')}”尚未定义字段 Schema")
    mappings = template.get("field_mappings") or []
    if not isinstance(mappings, list) or not mappings:
        raise ValueError(f"腾讯文档模板“{template.get('name') or template.get('id')}”尚未配置字段映射")
    source_fields = {
        str(field.get("key") or ""): field
        for field in issue_fields
        if isinstance(field, dict) and field.get("key")
    }
    allowed_sources = set(source_fields) | SYSTEM_SOURCES
    targets = set()
    for mapping in mappings:
        target = str(mapping.get("target_field_id") or "")
        source = str(mapping.get("source_key") or "")
        if not target or target not in schema:
            raise ValueError(f"腾讯字段映射目标无效：{target or '未填写'}")
        if target in targets:
            raise ValueError(f"腾讯字段重复映射：{target}")
        target_schema = schema.get(target) if isinstance(schema.get(target), dict) else {}
        required = bool(mapping.get("required")) or str(
            target_schema.get("title") or ""
        ).startswith("*")
        declared_type = str(mapping.get("target_type") or "text").replace("multi_select", "multiple_select")
        schema_type = str(target_schema.get("type") or declared_type).replace("multi_select", "multiple_select")
        if declared_type != schema_type:
            raise ValueError(f"腾讯字段 {target} 的映射类型与 Schema 不一致")
        source_type = SYSTEM_SOURCE_TYPES.get(source)
        if source in source_fields:
            source_type = str(source_fields[source].get("type") or "text").replace(
                "multi_select", "multiple_select"
            )
        if source_type:
            validate_mapping_compatibility(
                source,
                source_type,
                declared_type,
                source_fields.get(source) or {},
                target_schema,
            )
        if (
            source not in allowed_sources
            and required
            and is_empty(mapping.get("default_value"))
        ):
            raise ValueError(f"字段映射来源不存在：{source or '未填写'}")
        targets.add(target)
    required_targets = {
        field_id
        for field_id, definition in schema.items()
        if isinstance(definition, dict) and str(definition.get("title") or "").startswith("*")
    }
    missing = required_targets - targets
    if missing:
        titles = [str((schema.get(field_id) or {}).get("title") or field_id) for field_id in missing]
        raise ValueError(f"腾讯模板缺少必填字段映射：{'、'.join(titles)}")


def validate_mapping_compatibility(
    source: str,
    source_type: str,
    target_type: str,
    source_field: dict,
    target_schema: dict,
) -> None:
    allowed_targets = SOURCE_TARGET_TYPES.get(source_type, {"text"})
    if target_type not in allowed_targets:
        title = str(target_schema.get("title") or target_type)
        raise ValueError(
            f"字段映射类型不兼容：{source}（{source_type}）不能写入“{title}”（{target_type}）"
        )
    source_options = {str(item) for item in source_field.get("options") or []}
    target_options = {str(item) for item in target_schema.get("enum") or []}
    unsupported = source_options - target_options if source_options and target_options else set()
    if unsupported:
        title = str(target_schema.get("title") or target_type)
        raise ValueError(
            f"字段 {source} 的选项未被腾讯字段“{title}”覆盖：{'、'.join(sorted(unsupported))}"
        )


def validate_record_values(
    template: dict,
    issue: dict,
    values: dict,
    *,
    allow_pending_images: bool = False,
) -> None:
    schema = template.get("schema") or {}
    for mapping in template.get("field_mappings") or []:
        target = str(mapping.get("target_field_id") or "")
        target_schema = schema.get(target) if isinstance(schema.get(target), dict) else {}
        required = bool(mapping.get("required")) or str(
            target_schema.get("title") or ""
        ).startswith("*")
        if not required:
            continue
        if is_empty(values.get(target)):
            if mapping.get("source_key") == "$images" and allow_pending_images:
                continue
            title = str(target_schema.get("title") or target)
            raise ValueError(f"问题 {issue.get('key')} 的必填字段“{title}”为空")


def mapping_source_value(source: str, issue: dict, date_text: str, image_values: list[dict]):
    if source == "$date":
        return date_text
    if source == "$images":
        return image_values
    if source == "$sender":
        return issue.get("sender") or ""
    if source == "$message_time":
        return issue.get("message_time") or ""
    if source == "$issue_key":
        return issue.get("key") or ""
    return issue_value(issue, source)


def convert_target_value(value, target_type: str, target_schema: dict, date_text: str):
    if target_type == "single_select":
        text = str(value or "").strip()
        validate_enum_value(text, target_schema)
        return single_select(text)
    if target_type in {"multiple_select", "multi_select"}:
        items = value if isinstance(value, list) else ([] if is_empty(value) else [value])
        texts = [str(item).strip() for item in items if str(item).strip()]
        for text in texts:
            validate_enum_value(text, target_schema)
        return multiple_select(texts)
    if target_type == "image":
        return value if isinstance(value, list) else []
    if target_type == "date_time":
        return date_time_millis(value) if not is_empty(value) else ""
    if target_type == "number":
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else ""
    if target_type in {"boolean", "checkbox"}:
        if is_empty(value):
            return ""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in {"true", "1", "yes", "y", "是"}:
            return True
        if text in {"false", "0", "no", "n", "否"}:
            return False
        return ""
    return display_value(value)


def validate_enum_value(value: str, target_schema: dict) -> None:
    allowed = [str(item) for item in target_schema.get("enum") or []]
    if value and allowed and value not in allowed:
        raise ValueError(f"值“{value}”不在腾讯字段“{target_schema.get('title') or ''}”的选项中")


def validate_issue_keys(issues: list[dict]) -> None:
    keys = [str(issue.get("key") or "").strip() for issue in issues]
    if any(not key for key in keys):
        raise ValueError("问题清单包含空的问题 Key，无法安全去重同步")
    if len(keys) != len(set(keys)):
        raise ValueError("问题清单包含重复的问题 Key，已阻止重复同步")
    identities = [issue_dedupe_identity(key) for key in keys]
    if len(identities) != len(set(identities)):
        raise ValueError("问题清单包含指向同一来源消息的问题 Key，已阻止重复同步")


def issue_dedupe_identity(value) -> str:
    key = str(value or "").strip()
    match = re.fullmatch(r"issue_\d+_([0-9a-fA-F]{12})", key)
    if match:
        return f"seed_hash:{match.group(1).lower()}"
    return f"issue_key:{key}"


def is_empty(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def single_select(value) -> list[dict]:
    text = str(value or "").strip()
    return [{"text": text}] if text else []


def multiple_select(values) -> list[dict]:
    return [{"text": str(value)} for value in values if str(value).strip()]


def utc_date_millis(date_text: str) -> str:
    value = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return str(int(value.timestamp() * 1000))


def date_time_millis(value) -> str:
    text = str(value or "").strip()
    if text.isdigit():
        return text
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.strptime(text[:10], "%Y-%m-%d")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return str(int(parsed.timestamp() * 1000))


def load_manifest(day_dir: str | Path) -> dict[str, Path]:
    path = Path(day_dir) / "raw_attachments" / "_bulk_hd_cache" / "hd_cache_manifest.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"截图 manifest 结构无效: {path}")
    result = {}
    for record in data.get("records") or []:
        if not isinstance(record, dict) or record.get("accepted") is False:
            continue
        filename = str(record.get("filename") or "")
        local_path = str(record.get("local_path") or "").strip()
        parts = filename.split("_", 2)
        if len(parts) >= 2 and local_path:
            ref = f"bulk:{parts[0]}_{parts[1]}"
            result[ref] = resolve_local_image_path(day_dir, local_path, ref)
    return result


def load_sync_image_manifest(
    day_dir: str | Path,
    frozen_manifest: dict | None,
) -> dict[str, Path]:
    if frozen_manifest is None:
        raise ValueError("问题清单快照缺少冻结图片清单，请重新执行大模型分析")
    result = {}
    for raw_ref, raw_value in frozen_manifest.items():
        ref = str(raw_ref)
        if isinstance(raw_value, dict):
            local_path = raw_value.get("local_path")
        elif isinstance(raw_value, str):
            local_path = raw_value
        else:
            raise ValueError(f"截图引用 {ref} 的冻结本地路径格式无效")
        path_text = str(local_path or "").strip()
        if not path_text:
            continue
        result[ref] = resolve_local_image_path(day_dir, path_text, ref)
    return result


def resolve_local_image_path(
    day_dir: str | Path,
    value: str,
    ref: str,
) -> Path:
    day_root = Path(day_dir).resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = day_root / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(day_root)
    except ValueError as exc:
        raise ValueError(
            f"截图引用 {ref} 的本地文件必须位于运行目录内: {candidate}"
        ) from exc
    return candidate


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


class LedgerLoadError(ValueError):
    """Raised when an existing sync ledger cannot be trusted."""


def load_ledger(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": 2, "templates": {}}
    except OSError as exc:
        raise LedgerLoadError(f"无法读取同步账本 {path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LedgerLoadError(f"同步账本 JSON 已损坏 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise LedgerLoadError(f"同步账本结构无效 {path}: 根节点必须是对象")

    if "templates" in data:
        templates = data.get("templates")
        if not isinstance(templates, dict):
            raise LedgerLoadError(f"同步账本结构无效 {path}: templates 必须是对象")
        for template_id, state in templates.items():
            if not isinstance(state, dict):
                raise LedgerLoadError(
                    f"同步账本结构无效 {path}: 模板 {template_id} 状态必须是对象"
                )
            if not isinstance(state.get("synced"), dict):
                raise LedgerLoadError(
                    f"同步账本结构无效 {path}: 模板 {template_id} 的 synced 必须是对象"
                )
            validate_synced_entries(
                state["synced"],
                path,
                f"模板 {template_id}",
            )
        data["version"] = 2
        return data

    if "synced" not in data or not isinstance(data.get("synced"), dict):
        raise LedgerLoadError(f"同步账本结构无效 {path}: 缺少有效的 synced 对象")
    validate_synced_entries(data["synced"], path, "旧版账本")
    return {
        "version": 2,
        "date": data.get("date") or "",
        "updated_at": data.get("updated_at") or "",
        "templates": {
            "default": {
                "synced": data["synced"],
            }
        },
    }


def validate_synced_entries(synced: dict, path: Path, scope: str) -> None:
    for issue_key, entry in synced.items():
        record_id = entry.get("record_id") if isinstance(entry, dict) else None
        if (
            not str(issue_key).strip()
            or not isinstance(record_id, str)
            or not record_id.strip()
        ):
            raise LedgerLoadError(
                f"同步账本结构无效 {path}: {scope} 的条目 {issue_key!r} "
                "必须是包含非空 record_id 的对象"
            )


def ledger_lock_path(path: Path) -> Path:
    resolved = path.resolve()
    return resolved.with_name(f"{resolved.name}.lock")


@contextmanager
def ledger_sync_lock(path: Path):
    lock_path = ledger_lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise RuntimeError(f"无法打开同步账本锁 {lock_path}: {exc}") from exc

    acquired = False
    deadline = time.monotonic() + SYNC_LOCK_TIMEOUT_SECONDS
    last_error: OSError | None = None
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        while not acquired:
            try:
                try_lock_file(descriptor)
                acquired = True
            except OSError as exc:
                last_error = exc
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"同步账本正在被另一个进程使用，等待超时: {path}"
                    ) from last_error
                time.sleep(SYNC_LOCK_POLL_SECONDS)
        yield
    finally:
        os.close(descriptor)


def try_lock_file(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def template_synced(ledger: dict, template_id: str) -> dict:
    templates = ledger.setdefault("templates", {})
    template_state = templates.setdefault(template_id, {})
    synced = template_state.get("synced")
    if not isinstance(synced, dict):
        synced = {}
        template_state["synced"] = synced
    return synced


def save_ledger(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
