from __future__ import annotations

import copy
import json
import os
import re
import sys
from pathlib import Path

from .issue_schema import DEFAULT_ISSUE_FIELDS, normalize_issue_fields


RUNTIME_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
EXAMPLE_CONFIG = RUNTIME_ROOT / "config.example.json"
DEFAULT_USER_DIR = Path.home() / ".wecom-issue-radar"
DEFAULT_USER_CONFIG = DEFAULT_USER_DIR / "config.local.json"
LEGACY_MIGRATION_KEY = "_migrate_legacy_config_v2"


DEFAULT_PROMPTS = [
    {
        "id": "daily_issue_standard",
        "name": "标准问题盘点",
        "description": "识别咨询、故障和需求，整理问题、结论与时间轴。",
        "content": (
            "你是一名企业软件产品经理。请从当天群聊中识别真正需要跟踪的问题，"
            "排除寒暄、通知和简单确认。合并属于同一件事的连续对话，结合回复和截图 OCR，"
            "输出事实清楚的问题描述、模块、问题分类、20 字以内总结以及有依据的结论和时间轴。"
            "信息不足时使用‘待评估’，不要编造群聊中没有出现的原因。"
        ),
    },
    {
        "id": "customer_voice",
        "name": "客户声音与需求",
        "description": "偏重识别需求、体验问题和高频抱怨。",
        "content": (
            "你是一名客户体验产品经理。重点识别客户提出的新需求、使用阻碍、"
            "提示不清晰、交互不一致和重复抱怨。区分产品缺陷、操作问题与需求，"
            "保留客户原意，并在结论中说明当前是否已有答复或解决方案。"
        ),
    },
    {
        "id": "incident_review",
        "name": "线上故障复盘",
        "description": "偏重线上异常、影响、处置过程和恢复结论。",
        "content": (
            "你是一名线上故障复盘负责人。重点识别报错、失败、性能下降、数据异常和服务不可用，"
            "按事件合并消息，提取首次反馈时间、关键处置动作、恢复结论和未解决项。"
            "没有明确根因时写‘根因待确认’，不要推测。"
        ),
    },
]


def deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def default_config_path() -> Path:
    explicit = os.environ.get("WECOM_ISSUE_RADAR_CONFIG") or os.environ.get(
        "WECOM_DAILY_PIPELINE_CONFIG"
    )
    if explicit:
        return Path(explicit).expanduser().resolve()
    return DEFAULT_USER_CONFIG.resolve()


def load_example() -> dict:
    with EXAMPLE_CONFIG.open("r", encoding="utf-8") as file:
        return json.load(file)


def ensure_prompt_config(config: dict) -> dict:
    prompt_config = config.setdefault("prompts", {})
    configured_fields = configured_default_issue_fields(config)
    if config.get(LEGACY_MIGRATION_KEY):
        prompt_config["default_issue_fields"] = normalize_issue_fields(configured_fields)
    else:
        prompt_config["default_issue_fields"] = normalize_issue_fields(
            prompt_config.get("default_issue_fields"),
            configured_fields,
        )
    items = prompt_config.get("items")
    source_items = items if isinstance(items, list) and items else copy.deepcopy(DEFAULT_PROMPTS)
    seen: set[str] = set()
    normalized = []
    for index, item in enumerate(source_items, start=1):
        if not isinstance(item, dict):
            continue
        explicit_id = str(item.get("id") or "").strip()
        prompt_id = explicit_id or f"prompt_{index}"
        if prompt_id in seen:
            raise ValueError(f"提示词 ID 不能重复：{prompt_id}")
        seen.add(prompt_id)
        normalized.append(
            {
                "id": prompt_id,
                "name": str(item.get("name") or prompt_id),
                "description": str(item.get("description") or ""),
                "content": str(item.get("content") or ""),
                "issue_fields": normalize_issue_fields(
                    item.get("issue_fields"),
                    prompt_config["default_issue_fields"],
                ),
                "default_smart_sheet_template_id": str(
                    item.get("default_smart_sheet_template_id") or ""
                ).strip(),
            }
        )
    if not normalized:
        prompt_config["items"] = copy.deepcopy(DEFAULT_PROMPTS)
        return ensure_prompt_config(config)
    prompt_config["items"] = normalized

    ids = {item["id"] for item in prompt_config["items"]}
    requested_default = str(prompt_config.get("default_id") or "").strip()
    if requested_default not in ids:
        prompt_config["default_id"] = prompt_config["items"][0]["id"]
    else:
        prompt_config["default_id"] = requested_default
    return config


def ensure_smart_sheet_config(config: dict) -> dict:
    migrate_legacy = bool(config.get(LEGACY_MIGRATION_KEY)) or int(
        config.get("config_version") or 0
    ) < 2
    if migrate_legacy:
        config[LEGACY_MIGRATION_KEY] = True
    smart = config.setdefault("smart_sheet", {})
    legacy = {
        key: copy.deepcopy(smart.get(key))
        for key in ("url", "webhook_url_env", "webhook_url", "batch_size", "schema", "defaults")
    }
    raw_templates = smart.get("templates")
    templates = []
    seen: set[str] = set()
    if isinstance(raw_templates, list):
        for index, raw in enumerate(raw_templates, start=1):
            if not isinstance(raw, dict):
                continue
            explicit_id = str(raw.get("id") or "").strip()
            template_id = explicit_id or f"template_{index}"
            if template_id in seen:
                raise ValueError(f"腾讯文档模板 ID 不能重复：{template_id}")
            seen.add(template_id)
            schema = copy.deepcopy(raw.get("schema")) if isinstance(raw.get("schema"), dict) else {}
            templates.append(
                {
                    "id": template_id,
                    "name": str(raw.get("name") or template_id),
                    "url": str(raw.get("url") or ""),
                    "webhook_url_env": str(raw.get("webhook_url_env") or ""),
                    "webhook_url": str(raw.get("webhook_url") or ""),
                    "batch_size": max(1, min(int(raw.get("batch_size") or 50), 50)),
                    "schema": schema,
                    "field_mappings": normalize_field_mappings(raw.get("field_mappings"), schema),
                }
            )

    if not templates:
        templates = [
            {
                "id": "default",
                "name": "默认问题清单",
                "url": "",
                "webhook_url_env": "",
                "webhook_url": "",
                "batch_size": 50,
                "schema": {},
                "field_mappings": [],
            }
        ]

    requested_default = str(smart.get("default_template_id") or "").strip()
    default_template = next(
        (item for item in templates if item["id"] == requested_default),
        templates[0],
    )
    if migrate_legacy:
        if not default_template["url"]:
            default_template["url"] = str(legacy.get("url") or "")
        if not default_template["webhook_url"]:
            default_template["webhook_url"] = str(legacy.get("webhook_url") or "")
        if not default_template["webhook_url_env"]:
            default_template["webhook_url_env"] = str(
                legacy.get("webhook_url_env") or "WECOM_SMARTSHEET_WEBHOOK_URL"
            )
        if not default_template["schema"] and isinstance(legacy.get("schema"), dict):
            default_template["schema"] = copy.deepcopy(legacy["schema"])
        if not default_template["field_mappings"]:
            default_template["field_mappings"] = legacy_field_mappings(
                default_template["schema"],
                legacy.get("defaults") if isinstance(legacy.get("defaults"), dict) else {},
            )
        if legacy.get("batch_size") and default_template["batch_size"] == 50:
            default_template["batch_size"] = max(1, min(int(legacy["batch_size"]), 50))

    smart["templates"] = templates
    smart["default_template_id"] = default_template["id"]
    for key in ("url", "webhook_url_env", "webhook_url", "batch_size", "schema", "defaults"):
        smart.pop(key, None)
    config["config_version"] = 2
    return config


def normalize_field_mappings(value, schema: dict) -> list[dict]:
    if isinstance(value, dict):
        value = [
            {
                "target_field_id": target,
                **(mapping if isinstance(mapping, dict) else {"source_key": mapping}),
            }
            for target, mapping in value.items()
        ]
    if not isinstance(value, list):
        return []
    result = []
    seen = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        target = str(raw.get("target_field_id") or raw.get("target") or "").strip()
        source = str(raw.get("source_key") or raw.get("source") or "").strip()
        if not target or not source or target in seen:
            continue
        target_schema = schema.get(target) if isinstance(schema.get(target), dict) else {}
        result.append(
            {
                "source_key": source,
                "target_field_id": target,
                "target_type": str(raw.get("target_type") or target_schema.get("type") or "text"),
                "required": bool(raw.get("required"))
                or str(target_schema.get("title") or "").startswith("*"),
                "default_value": copy.deepcopy(
                    raw.get("default_value", raw.get("default", ""))
                ),
            }
        )
        seen.add(target)
    return result


def legacy_field_mappings(schema: dict, defaults: dict) -> list[dict]:
    sources = [
        ("module_text", "f04Gwj", defaults.get("module_text") or "订单/售后"),
        ("problem_description", "ftk5Tx", ""),
        ("reason", "fMAfWQ", ""),
        ("$images", "fn8TJd", []),
        ("review_text", "fb19Ra", defaults.get("review_text") or ""),
        ("status_text", "fIgBdy", defaults.get("status_text") or "待评估"),
        ("issue_category_text", "fsFBqK", defaults.get("issue_category_text") or "待评估"),
        ("typical_case_texts", "fOXTRh", defaults.get("typical_case_texts") or []),
        ("$date", "ftQMc5", ""),
        ("online_issue_text", "fgIJEu", defaults.get("online_issue_text") or ""),
        ("jira_url", "fhK1MH", defaults.get("jira_url") or ""),
        ("issue_summary_text", "fs9xhZ", defaults.get("issue_summary_text") or ""),
        ("start_time_text", "fV6BDR", defaults.get("start_time_text") or ""),
        ("end_time_text", "fDLv3b", defaults.get("end_time_text") or ""),
    ]
    return normalize_field_mappings(
        [
            {
                "source_key": source,
                "target_field_id": target,
                "default_value": default,
            }
            for source, target, default in sources
            if target in schema
        ],
        schema,
    )


def configured_default_issue_fields(config: dict) -> list[dict]:
    fields = copy.deepcopy(DEFAULT_ISSUE_FIELDS)
    try:
        template = selected_smart_sheet_template(config)
    except ValueError:
        return fields
    schema = template.get("schema") or {}
    mappings = {
        item.get("source_key"): item for item in template.get("field_mappings") or []
    }
    field_by_key = {field["key"]: field for field in fields}
    for key, target in (("module_text", "f04Gwj"), ("issue_category_text", "fsFBqK")):
        target_schema = schema.get(target) if isinstance(schema.get(target), dict) else {}
        options = [str(value) for value in target_schema.get("enum") or []]
        if options:
            field_by_key[key]["options"] = options
        mapping = mappings.get(key) or {}
        if mapping.get("default_value") not in (None, ""):
            field_by_key[key]["default_value"] = copy.deepcopy(mapping["default_value"])
    return fields


def load_config(path: str | os.PathLike | None = None) -> tuple[dict, Path]:
    config_path = Path(path).expanduser().resolve() if path else default_config_path()
    example = load_example()
    local = {}
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as file:
            local = json.load(file)
    migrate_legacy = not config_path.exists() or int(local.get("config_version") or 0) < 2
    config = deep_merge(example, local)
    if migrate_legacy:
        config[LEGACY_MIGRATION_KEY] = True
    config = ensure_prompt_config(ensure_smart_sheet_config(config))
    config.pop(LEGACY_MIGRATION_KEY, None)
    config["config_version"] = 2
    return config, config_path


def save_config(config: dict, path: str | os.PathLike | None = None) -> Path:
    config_path = Path(path).expanduser().resolve() if path else default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    candidate = copy.deepcopy(config)
    if int(candidate.get("config_version") or 0) < 2:
        candidate[LEGACY_MIGRATION_KEY] = True
    normalized = ensure_prompt_config(ensure_smart_sheet_config(candidate))
    normalized.pop(LEGACY_MIGRATION_KEY, None)
    normalized["config_version"] = 2
    clean = {key: value for key, value in normalized.items() if not key.startswith("_")}
    temp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(clean, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temp_path.replace(config_path)
    return config_path


def selected_prompt(config: dict, prompt_id: str | None = None) -> dict:
    prompt_config = ensure_prompt_config(ensure_smart_sheet_config(config)).get("prompts", {})
    requested = str(prompt_id or "").strip()
    target = requested or prompt_config.get("default_id")
    for item in prompt_config.get("items", []):
        if item.get("id") == target:
            return item
    if requested:
        raise ValueError(f"找不到提示词：{requested}")
    return prompt_config["items"][0]


def selected_smart_sheet_template(config: dict, template_id: str | None = None) -> dict:
    smart = ensure_smart_sheet_config(config).get("smart_sheet") or {}
    templates = smart.get("templates") or []
    if not templates:
        raise ValueError("请先配置至少一个腾讯文档模板")
    requested = str(template_id or "").strip()
    target = requested or smart.get("default_template_id")
    for template in templates:
        if template.get("id") == target:
            return template
    if requested:
        raise ValueError(f"找不到腾讯文档模板：{requested}")
    return templates[0]


def slugify(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "_", str(value or "").strip())
    return text.strip("_").lower() or "prompt"
