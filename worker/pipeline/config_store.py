from __future__ import annotations

import copy
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

from worker.jev_config import normalize_jev_config

from .issue_schema import DEFAULT_ISSUE_FIELDS, normalize_issue_fields


RUNTIME_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
EXAMPLE_CONFIG = RUNTIME_ROOT / "config.example.json"
DEFAULT_USER_DIR = Path.home() / ".wecom-issue-radar"
DEFAULT_USER_CONFIG = DEFAULT_USER_DIR / "config.local.json"
LEGACY_MIGRATION_KEY = "_migrate_legacy_config_v2"
CURRENT_CONFIG_VERSION = 4

# Config v4 splits "what the AI is asked to do" (ai_strategies) from "where the
# result is written" (publish_targets). The v3 ``prompts`` and ``smart_sheet``
# partitions are kept as read-only mirrors so llm_analyzer/smart_sheet/tasks keep
# working without changes until M1 finishes migrating the UI.
AI_STRATEGY_SCENES = ("issue_analysis", "reply", "eq_reply")
ISSUE_ANALYSIS_SCENE = "issue_analysis"
TENCENT_TARGET_KIND = "tencent_smartsheet"
TENCENT_TARGET_PREFIX = "tencent:"
PUBLISH_TARGET_KINDS = ("excel", "markdown", "tencent_smartsheet", "feishu_doc")
BASE_PUBLISH_TARGET_KINDS = ("excel", "markdown")
PUBLISH_TRANSFORM_MODES = ("direct", "constant", "template", "date", "enum")
LEGACY_SMART_SHEET_KEYS = (
    "url",
    "webhook_url_env",
    "webhook_url",
    "batch_size",
    "schema",
    "defaults",
)
_UNSET = object()


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


DEFAULT_SCENE_STRATEGIES = [
    {
        "id": "reply_draft",
        "name": "群回复草稿",
        "description": "根据群聊上下文起草一条可直接发送的回复。",
        "scene": "reply",
        "content": (
            "你是企业微信群的回复助手。请结合群聊上下文和已知业务事实，"
            "起草一条可以直接发送的中文回复：先回应对方最关心的问题，"
            "再给出可执行的下一步；不确定的信息明确写“需要确认”，不要编造。"
        ),
        "issue_fields": [],
    },
    {
        "id": "eq_reply",
        "name": "高情商回复",
        "description": "把直白或情绪化的回复改写成体面、清晰、可执行的说法。",
        "scene": "eq_reply",
        "content": (
            "你是沟通顾问。请把用户的原始回复改写成高情商版本："
            "先表达理解与感谢，再清楚说明结论或边界，最后给出下一步；"
            "保持简洁、不卑不亢，不承诺无法做到的事情。"
        ),
        "issue_fields": [],
    },
]

DEFAULT_PUBLISH_TARGETS = [
    {
        "id": "excel",
        "name": "Excel 表格",
        "kind": "excel",
        "config": {},
    },
    {
        "id": "markdown",
        "name": "Markdown 文档",
        "kind": "markdown",
        "config": {},
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


def ensure_jev_config(config: dict, *, strict: bool = False) -> dict:
    """Merge the optional ``jev`` block over its defaults.

    Configs written before Jev existed have no ``jev`` key; they must keep
    loading unchanged with Jev disabled rather than gaining one implicitly.

    With ``strict=True`` (the save path) an unusable enabled configuration is
    rejected so the user gets actionable feedback instead of a silently inert
    feature. Loading stays permissive: an unusable block simply resolves to
    "not configured" and every capability fails open.
    """
    if "jev" not in config:
        return config
    normalized = normalize_jev_config(config.get("jev"))
    if strict:
        _validate_jev_config(normalized)
    config["jev"] = normalized
    return config


def _validate_jev_config(jev: dict) -> None:
    if not jev["enabled"] or jev["mode"] == "off":
        return
    if not jev["base_url"]:
        raise ValueError("启用 Jev 时必须配置 base_url。")
    if not jev["api_key"]:
        raise ValueError("启用 Jev 时必须配置 api_key。")
    parsed = urllib.parse.urlsplit(jev["base_url"])
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Jev base_url 必须是完整的 http(s) 地址。")
    if parsed.scheme != "https" and (parsed.hostname or "").lower() not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("Jev base_url 必须使用 HTTPS（仅 localhost 允许明文 HTTP）。")


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
        scene = str(item.get("scene") or "").strip()
        if scene and scene != ISSUE_ANALYSIS_SCENE:
            # Non-issue strategies have no issue-field schema; do not let the
            # prompt-level fallback invent one for them.
            raw_fields = item.get("issue_fields")
            issue_fields = (
                normalize_issue_fields(raw_fields)
                if isinstance(raw_fields, list) and raw_fields
                else []
            )
        else:
            issue_fields = normalize_issue_fields(
                item.get("issue_fields"),
                prompt_config["default_issue_fields"],
            )
        normalized_item = {
            "id": prompt_id,
            "name": str(item.get("name") or prompt_id),
            "description": str(item.get("description") or ""),
            "content": str(item.get("content") or ""),
            "issue_fields": issue_fields,
            "default_smart_sheet_template_id": str(
                item.get("default_smart_sheet_template_id") or ""
            ).strip(),
        }
        if scene in AI_STRATEGY_SCENES:
            normalized_item["scene"] = scene
        normalized.append(normalized_item)
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
                    "transforms": normalize_transforms(raw.get("transforms")),
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
                "transforms": {},
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
    config["config_version"] = CURRENT_CONFIG_VERSION
    return config


def _clamp_batch_size(value) -> int:
    try:
        return max(1, min(int(value or 50), 50))
    except (TypeError, ValueError):
        return 50


def _normalize_enum_transform_map(value) -> dict[str, str]:
    """Accept the renderer's ``enum_map`` object plus legacy ``源值=目标值`` lines."""
    if isinstance(value, dict):
        return {
            str(source): str(target if target is not None else "")
            for source, target in value.items()
        }
    if isinstance(value, str):
        lines = value.splitlines()
    elif isinstance(value, list):
        lines = [str(item) for item in value]
    else:
        return {}
    result: dict[str, str] = {}
    for line in lines:
        if "=" not in line:
            continue
        source, target = line.split("=", 1)
        result[source.strip()] = target.strip()
    return result


def normalize_transforms(value) -> dict[str, dict]:
    """Normalize ``publish_targets[].config.transforms`` (keyed by target field ID).

    Canonical shape matches the renderer:
    ``{"mode": "...", "constant"|"template"|"date_format"|"enum_map": ...}``.
    Legacy ``type``/``value``/``format``/``values`` keys are accepted so old
    drafts keep working; unknown modes are dropped instead of breaking a save.
    """
    if not isinstance(value, dict):
        return {}
    transforms: dict[str, dict] = {}
    for raw_target, raw_transform in value.items():
        target_id = str(raw_target or "").strip()
        if not target_id or not isinstance(raw_transform, dict):
            continue
        mode = str(
            raw_transform.get("mode") or raw_transform.get("type") or ""
        ).strip().lower()
        if mode not in PUBLISH_TRANSFORM_MODES:
            continue
        transform: dict = {"mode": mode}
        if mode == "constant":
            constant = raw_transform.get("constant", raw_transform.get("value", ""))
            transform["constant"] = "" if constant is None else str(constant)
        elif mode == "template":
            template = raw_transform.get("template", raw_transform.get("value", ""))
            transform["template"] = "" if template is None else str(template)
        elif mode == "date":
            date_format = str(
                raw_transform.get("date_format", raw_transform.get("format", "")) or ""
            ).strip()
            transform["date_format"] = date_format or "YYYY-MM-DD"
        elif mode == "enum":
            transform["enum_map"] = _normalize_enum_transform_map(
                raw_transform.get("enum_map", raw_transform.get("values"))
            )
        transforms[target_id] = transform
    return transforms


def _section_list(config: dict | None, section: str, key: str | None = None) -> list | None:
    if not isinstance(config, dict):
        return None
    value = config.get(section)
    if key is not None:
        value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, list) else None


def _normalize_scene(value) -> str:
    scene = str(value or "").strip()
    return scene if scene in AI_STRATEGY_SCENES else ISSUE_ANALYSIS_SCENE


def _issue_fields_for_scene(value, scene: str, default_fields: list | None) -> list[dict]:
    if scene == ISSUE_ANALYSIS_SCENE:
        return normalize_issue_fields(value, default_fields)
    return normalize_issue_fields(value) if isinstance(value, list) and value else []


def _strategy_from_prompt_item(
    item: dict, default_fields: list | None, default_scene: str | None = None
) -> dict:
    prompt_id = str(item.get("id") or "").strip()
    scene = _normalize_scene(item.get("scene") or default_scene)
    return {
        "id": prompt_id,
        "name": str(item.get("name") or prompt_id).strip() or prompt_id,
        "description": str(item.get("description") or ""),
        "scene": scene,
        "content": str(item.get("content") or ""),
        "issue_fields": _issue_fields_for_scene(
            item.get("issue_fields"), scene, default_fields
        ),
    }


def _strategies_from_prompts(prompt_items: list, default_fields: list | None) -> list[dict]:
    strategies = []
    for item in prompt_items:
        if isinstance(item, dict):
            strategies.append(_strategy_from_prompt_item(item, default_fields))
    return strategies


def _with_default_scene_strategies(strategies: list[dict]) -> list[dict]:
    """Add the built-in reply/eq strategies for scenes without one."""
    result = list(strategies)
    used_ids = {str(strategy.get("id") or "").strip() for strategy in result}
    existing_scenes = {str(strategy.get("scene") or "") for strategy in result}
    for default in DEFAULT_SCENE_STRATEGIES:
        if default["scene"] in existing_scenes:
            continue
        strategy = copy.deepcopy(default)
        candidate = strategy["id"]
        suffix = 2
        while candidate in used_ids:
            candidate = f"{strategy['id']}_{suffix}"
            suffix += 1
        strategy["id"] = candidate
        used_ids.add(candidate)
        result.append(strategy)
    return result


def _normalize_strategy_list(raw: list, default_fields: list | None) -> list[dict]:
    strategies = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        scene = _normalize_scene(item.get("scene"))
        strategy_id = str(item.get("id") or "").strip() or f"strategy_{index}"
        strategies.append(
            {
                "id": strategy_id,
                "name": str(item.get("name") or strategy_id).strip() or strategy_id,
                "description": str(item.get("description") or ""),
                "scene": scene,
                "content": str(item.get("content") or ""),
                "issue_fields": _issue_fields_for_scene(
                    item.get("issue_fields"), scene, default_fields
                ),
            }
        )
    return strategies


def _apply_prompt_edits(
    strategies: list[dict], prompt_items: list, default_fields: list | None
) -> list[dict]:
    """Fold edits made on the legacy prompts mirror into the v4 strategies."""
    by_id = {
        str(item.get("id") or "").strip(): item
        for item in prompt_items
        if isinstance(item, dict)
    }
    updated = []
    seen_ids: set[str] = set()
    for strategy in strategies:
        item = by_id.get(strategy["id"])
        if item is None:
            continue
        updated_strategy = _strategy_from_prompt_item(
            item, default_fields, strategy.get("scene")
        )
        if strategy.get("scene") != ISSUE_ANALYSIS_SCENE:
            # Non-issue strategies keep their own (usually empty) field schema.
            updated_strategy["issue_fields"] = list(strategy.get("issue_fields") or [])
        updated.append(updated_strategy)
        seen_ids.add(strategy["id"])
    for item in prompt_items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        if item_id and item_id not in seen_ids:
            updated.append(_strategy_from_prompt_item(item, default_fields))
    return updated


def _ensure_issue_strategy(
    strategies: list[dict], prompt_items: list, default_fields: list | None
) -> list[dict]:
    """Guarantee at least one issue_analysis strategy so the prompts mirror is usable."""
    if any(strategy.get("scene") == ISSUE_ANALYSIS_SCENE for strategy in strategies):
        return strategies
    source = next(
        (
            item
            for item in prompt_items
            if isinstance(item, dict)
            and _normalize_scene(item.get("scene")) == ISSUE_ANALYSIS_SCENE
        ),
        DEFAULT_PROMPTS[0],
    )
    fallback = _strategy_from_prompt_item(source, default_fields)
    fallback["scene"] = ISSUE_ANALYSIS_SCENE
    used_ids = {str(strategy.get("id") or "").strip() for strategy in strategies}
    candidate = fallback["id"] or "daily_issue_standard"
    fallback["id"] = candidate
    suffix = 2
    while fallback["id"] in used_ids:
        fallback["id"] = f"{candidate}_{suffix}"
        suffix += 1
    return [fallback, *strategies]


def _prompts_from_strategies(
    strategies: list[dict],
    default_fields: list | None,
    previous_items: list,
    default_template_id: str,
) -> list[dict]:
    previous = {
        str(item.get("id") or "").strip(): item
        for item in previous_items
        if isinstance(item, dict)
    }
    items = []
    for strategy in strategies:
        previous_item = previous.get(strategy["id"]) or {}
        template_id = (
            str(previous_item.get("default_smart_sheet_template_id") or "").strip()
            or default_template_id
        )
        items.append(
            {
                "id": strategy["id"],
                "name": strategy["name"] or strategy["id"],
                "description": strategy.get("description") or "",
                "content": strategy.get("content") or "",
                "scene": strategy.get("scene") or ISSUE_ANALYSIS_SCENE,
                "issue_fields": _issue_fields_for_scene(
                    strategy.get("issue_fields"),
                    _normalize_scene(strategy.get("scene")),
                    default_fields,
                ),
                "default_smart_sheet_template_id": template_id,
            }
        )
    return items


def _tencent_target_id(template_id: str, used_ids: set[str] | None = None) -> str:
    """Prefer the legacy template ID so the frontend mirror matches by ID."""
    used = used_ids if used_ids is not None else set()
    base = template_id or "default"
    candidate = base
    if candidate in BASE_PUBLISH_TARGET_KINDS or candidate in used:
        candidate = f"{TENCENT_TARGET_PREFIX}{base}"
    suffix = 2
    unique = candidate
    while unique in used:
        unique = f"{candidate}_{suffix}"
        suffix += 1
    return unique


def _target_from_template(template: dict, target_id: str | None = None) -> dict:
    template_id = str(template.get("id") or "").strip() or "default"
    return {
        "id": target_id or _tencent_target_id(template_id),
        "name": str(template.get("name") or template_id),
        "kind": TENCENT_TARGET_KIND,
        "config": {
            "template_id": template_id,
            "url": str(template.get("url") or ""),
            "webhook_url": str(template.get("webhook_url") or ""),
            "webhook_url_env": str(template.get("webhook_url_env") or ""),
            "batch_size": _clamp_batch_size(template.get("batch_size")),
            "schema": copy.deepcopy(template.get("schema"))
            if isinstance(template.get("schema"), dict)
            else {},
            "field_mappings": normalize_field_mappings(
                template.get("field_mappings"),
                (template.get("schema") if isinstance(template.get("schema"), dict) else {}),
            ),
            "transforms": normalize_transforms(template.get("transforms")),
        },
    }


def _targets_from_templates(templates: list) -> list[dict]:
    targets = []
    used_ids: set[str] = set()
    for template in templates:
        if not isinstance(template, dict):
            continue
        template_id = str(template.get("id") or "").strip() or "default"
        target_id = _tencent_target_id(template_id, used_ids)
        used_ids.add(target_id)
        targets.append(_target_from_template(template, target_id))
    return targets


def _normalize_tencent_target_config(config: dict, target_id: str) -> dict:
    template_id = str(config.get("template_id") or "").strip()
    if not template_id:
        if target_id.startswith(TENCENT_TARGET_PREFIX):
            template_id = target_id[len(TENCENT_TARGET_PREFIX) :]
        else:
            template_id = target_id
    schema = copy.deepcopy(config.get("schema")) if isinstance(config.get("schema"), dict) else {}
    normalized = {
        key: copy.deepcopy(value)
        for key, value in config.items()
        if key
        not in {
            "template_id",
            "url",
            "webhook_url",
            "webhook_url_env",
            "batch_size",
            "schema",
            "field_mappings",
            "transforms",
        }
    }
    normalized.update(
        {
            "template_id": template_id,
            "url": str(config.get("url") or ""),
            "webhook_url": str(config.get("webhook_url") or ""),
            "webhook_url_env": str(config.get("webhook_url_env") or ""),
            "batch_size": _clamp_batch_size(config.get("batch_size")),
            "schema": schema,
            "field_mappings": normalize_field_mappings(
                config.get("field_mappings"), schema
            ),
            "transforms": normalize_transforms(config.get("transforms")),
        }
    )
    return normalized


def _normalize_target_list(raw: list) -> list[dict]:
    targets = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        target_id = str(item.get("id") or "").strip() or f"target_{index}"
        kind = str(item.get("kind") or "").strip()
        if not kind:
            kind = target_id if target_id in BASE_PUBLISH_TARGET_KINDS else "excel"
        config = item.get("config")
        config = copy.deepcopy(config) if isinstance(config, dict) else {}
        if kind == TENCENT_TARGET_KIND:
            config = _normalize_tencent_target_config(config, target_id)
        targets.append(
            {
                "id": target_id,
                "name": str(item.get("name") or target_id).strip() or target_id,
                "kind": kind,
                "config": config,
            }
        )
    return targets


def _target_template_id(target: dict) -> str:
    config = target.get("config") if isinstance(target.get("config"), dict) else {}
    template_id = str(config.get("template_id") or "").strip()
    if template_id:
        return template_id
    target_id = str(target.get("id") or "")
    if target_id.startswith(TENCENT_TARGET_PREFIX):
        return target_id[len(TENCENT_TARGET_PREFIX) :]
    return target_id


def _apply_template_edits(targets: list[dict], templates: list) -> list[dict]:
    """Fold edits made on the legacy smart sheet page into tencent targets."""
    by_template_id = {
        str(template.get("id") or "").strip(): template
        for template in templates
        if isinstance(template, dict)
    }
    updated = []
    seen: set[str] = set()
    used_ids = {str(target.get("id") or "") for target in targets}
    for target in targets:
        if target.get("kind") != TENCENT_TARGET_KIND:
            updated.append(target)
            continue
        template = by_template_id.get(_target_template_id(target))
        if template is None:
            continue
        replacement = _target_from_template(template)
        # Keep the existing target ID stable so schedules keep their bindings.
        replacement["id"] = target["id"]
        template_transforms = normalize_transforms(template.get("transforms"))
        if not template_transforms:
            # The renderer's legacy mirror does not carry transforms; keep the
            # transforms the publish target already has instead of erasing them.
            existing_config = (
                target.get("config") if isinstance(target.get("config"), dict) else {}
            )
            existing_transforms = normalize_transforms(existing_config.get("transforms"))
            if existing_transforms:
                replacement["config"]["transforms"] = existing_transforms
        updated.append(replacement)
        seen.add(str(template.get("id") or "").strip())
    for template in templates:
        if not isinstance(template, dict):
            continue
        template_id = str(template.get("id") or "").strip()
        if template_id in seen:
            continue
        new_id = _tencent_target_id(template_id or "default", used_ids)
        used_ids.add(new_id)
        updated.append(_target_from_template(template, new_id))
    return updated


def _ensure_base_targets(targets: list[dict]) -> list[dict]:
    """Always expose one Excel and one Markdown target (ids ``excel``/``markdown``)."""
    remaining = list(targets)
    result = []
    for default in DEFAULT_PUBLISH_TARGETS:
        match = next(
            (target for target in remaining if target.get("kind") == default["kind"]),
            None,
        )
        if match is not None:
            remaining.remove(match)
            result.append(match)
        else:
            result.append(copy.deepcopy(default))
    result.extend(remaining)
    return result


def _templates_from_targets(targets: list[dict]) -> list[dict]:
    templates = []
    for target in targets:
        if target.get("kind") != TENCENT_TARGET_KIND:
            continue
        config = target.get("config") if isinstance(target.get("config"), dict) else {}
        schema = (
            copy.deepcopy(config.get("schema"))
            if isinstance(config.get("schema"), dict)
            else {}
        )
        template_id = _target_template_id(target) or str(target.get("id") or "default")
        templates.append(
            {
                "id": template_id,
                "name": str(target.get("name") or template_id),
                "url": str(config.get("url") or ""),
                "webhook_url_env": str(config.get("webhook_url_env") or ""),
                "webhook_url": str(config.get("webhook_url") or ""),
                "batch_size": _clamp_batch_size(config.get("batch_size")),
                "schema": schema,
                "field_mappings": normalize_field_mappings(
                    config.get("field_mappings"), schema
                ),
                "transforms": normalize_transforms(config.get("transforms")),
            }
        )
    return templates


def _normalize_schedules_v4(
    config: dict, targets: list[dict], *, migrating: bool
) -> None:
    schedules = config.get("schedules")
    if not isinstance(schedules, list):
        schedules = []
    target_ids = {str(target.get("id") or "") for target in targets}
    template_to_target: dict[str, str] = {}
    target_to_template: dict[str, str] = {}
    for target in targets:
        if target.get("kind") != TENCENT_TARGET_KIND:
            continue
        template_id = _target_template_id(target)
        if template_id:
            template_to_target[template_id] = str(target["id"])
            target_to_template[str(target["id"])] = template_id
    fallback_target = "excel" if "excel" in target_ids else next(iter(target_ids), "excel")
    for schedule in schedules:
        if not isinstance(schedule, dict):
            continue
        template_id = str(schedule.get("smartSheetTemplateId") or "").strip()
        target_id = str(schedule.get("publish_target_id") or "").strip()
        if not target_id:
            target_id = template_to_target.get(template_id, fallback_target)
        elif target_id not in target_ids and migrating:
            target_id = template_to_target.get(template_id, fallback_target)
        schedule["publish_target_id"] = target_id
        if target_id in target_to_template:
            schedule["smartSheetTemplateId"] = target_to_template[target_id]
    config["schedules"] = schedules


def _normalize_bindings_v4(
    config: dict, prompt_config: dict, targets: list[dict], strategies: list[dict]
) -> None:
    bindings = config.get("bindings")
    if not isinstance(bindings, dict):
        bindings = {}
    issue_ids = [
        strategy["id"]
        for strategy in strategies
        if strategy.get("scene") == ISSUE_ANALYSIS_SCENE
    ]
    default_prompt = str(prompt_config.get("default_id") or "").strip()
    if default_prompt not in issue_ids:
        default_prompt = issue_ids[0] if issue_ids else "daily_issue_standard"
    target_ids = [str(target.get("id") or "") for target in targets]
    requested_target = str(bindings.get("default_publish_target_id") or "").strip()
    if requested_target not in target_ids:
        requested_target = (
            "excel" if "excel" in target_ids else (target_ids[0] if target_ids else "excel")
        )
    bindings["default_prompt_id"] = default_prompt
    bindings["default_publish_target_id"] = requested_target
    config["bindings"] = bindings


def _prompts_mirror_matches(
    strategies: list[dict], prompt_items: list, default_fields: list | None
) -> bool:
    derived = _prompts_from_strategies(strategies, default_fields, prompt_items, "")
    derived_signature = [
        (
            item["id"],
            item["name"],
            item["description"],
            item["content"],
            item["scene"],
            item["issue_fields"],
        )
        for item in derived
    ]
    actual_signature = []
    for item in prompt_items:
        if not isinstance(item, dict):
            return False
        scene = _normalize_scene(item.get("scene"))
        actual_signature.append(
            (
                str(item.get("id") or "").strip(),
                str(item.get("name") or ""),
                str(item.get("description") or ""),
                str(item.get("content") or ""),
                scene,
                _issue_fields_for_scene(item.get("issue_fields"), scene, default_fields),
            )
        )
    return derived_signature == actual_signature


def _template_signature(template: dict) -> tuple:
    return (
        str(template.get("id") or "").strip(),
        str(template.get("name") or ""),
        str(template.get("url") or ""),
        str(template.get("webhook_url") or ""),
        str(template.get("webhook_url_env") or ""),
        _clamp_batch_size(template.get("batch_size")),
        template.get("schema") if isinstance(template.get("schema"), dict) else {},
        template.get("field_mappings") if isinstance(template.get("field_mappings"), list) else [],
        normalize_transforms(template.get("transforms")),
    )


def _templates_mirror_matches(targets: list[dict], templates: list) -> bool:
    derived = [_template_signature(template) for template in _templates_from_targets(targets)]
    actual = [
        _template_signature(template)
        for template in templates
        if isinstance(template, dict)
    ]
    return derived == actual


def normalize_config_v4(
    config: dict,
    *,
    original_version: int | None = None,
    persisted: dict | None = None,
    prefer_mirrors: bool = False,
) -> dict:
    """Upgrade/normalize a config to the v4 structure in place.

    ``persisted`` is the last normalized config on disk (when any). It is only
    used to decide which side won when the legacy mirror (``prompts`` /
    ``smart_sheet``) and the new partition were edited simultaneously: the new
    partitions are canonical, but when only the legacy mirror changed (old
    settings pages still write it) its edits are folded into the new partitions.

    With no baseline at all (``persisted is None``), ``prefer_mirrors=True``
    treats an inconsistent legacy mirror as a hand-edited backup and folds it
    into the new partitions. It is used by backup import, where the mirror and
    the v4 partitions are otherwise exported together consistently.
    """
    if original_version is None:
        original_version = int(config.get("config_version") or 0)
    migrating = original_version < CURRENT_CONFIG_VERSION

    prompt_config = config.get("prompts")
    if not isinstance(prompt_config, dict):
        prompt_config = {}
        config["prompts"] = prompt_config
    default_fields = prompt_config.get("default_issue_fields")
    prompt_items = _section_list(prompt_config, "items") or []
    smart_sheet = config.get("smart_sheet")
    if not isinstance(smart_sheet, dict):
        smart_sheet = {}
        config["smart_sheet"] = smart_sheet
    templates = _section_list(smart_sheet, "templates") or []

    persisted_prompts = _section_list(persisted, "prompts", "items")
    persisted_templates = _section_list(persisted, "smart_sheet", "templates")
    persisted_strategies = _section_list(persisted, "ai_strategies")
    persisted_targets = _section_list(persisted, "publish_targets")

    raw_strategies = _section_list(config, "ai_strategies")
    strategies = (
        _normalize_strategy_list(raw_strategies, default_fields)
        if raw_strategies
        else None
    )
    if migrating or not strategies:
        strategies = _with_default_scene_strategies(
            _strategies_from_prompts(prompt_items, default_fields)
        )
    elif (
        persisted is not None
        and persisted_prompts is not None
        and persisted_strategies is not None
    ):
        if prompt_items != persisted_prompts and strategies == persisted_strategies:
            strategies = _apply_prompt_edits(strategies, prompt_items, default_fields)
    elif prefer_mirrors and not _prompts_mirror_matches(
        strategies, prompt_items, default_fields
    ):
        strategies = _apply_prompt_edits(strategies, prompt_items, default_fields)
    strategies = _ensure_issue_strategy(strategies, prompt_items, default_fields)
    config["ai_strategies"] = strategies

    raw_targets = _section_list(config, "publish_targets")
    targets = _normalize_target_list(raw_targets) if raw_targets else None
    if migrating or not targets:
        targets = _targets_from_templates(templates)
    elif (
        persisted is not None
        and persisted_templates is not None
        and persisted_targets is not None
    ):
        if templates != persisted_templates and targets == persisted_targets:
            targets = _apply_template_edits(targets, templates)
    elif prefer_mirrors and not _templates_mirror_matches(targets, templates):
        targets = _apply_template_edits(targets, templates)
    targets = _ensure_base_targets(targets)
    config["publish_targets"] = targets

    # Rebuild the legacy smart_sheet mirror from tencent publish targets.
    mirror_templates = _templates_from_targets(targets)
    requested_template = str(smart_sheet.get("default_template_id") or "").strip()
    template_ids = [str(template["id"]) for template in mirror_templates]
    if requested_template not in template_ids:
        requested_template = template_ids[0] if template_ids else "default"
    smart_sheet_mirror = {
        key: copy.deepcopy(value)
        for key, value in smart_sheet.items()
        if key not in LEGACY_SMART_SHEET_KEYS
    }
    smart_sheet_mirror["templates"] = mirror_templates
    smart_sheet_mirror["default_template_id"] = requested_template
    config["smart_sheet"] = smart_sheet_mirror

    # Rebuild the legacy prompts mirror from all strategies (with `scene`), while
    # `default_id` keeps pointing at an issue_analysis strategy for the pipeline.
    prompt_items_mirror = _prompts_from_strategies(
        strategies, default_fields, prompt_items, requested_template
    )
    prompt_config["items"] = prompt_items_mirror
    issue_ids = [
        strategy["id"]
        for strategy in strategies
        if strategy.get("scene") == ISSUE_ANALYSIS_SCENE
    ]
    mirror_ids = [str(item["id"]) for item in prompt_items_mirror]
    requested_prompt = str(prompt_config.get("default_id") or "").strip()
    if requested_prompt not in issue_ids:
        if issue_ids:
            requested_prompt = issue_ids[0]
        elif mirror_ids:
            requested_prompt = mirror_ids[0]
        else:
            requested_prompt = "daily_issue_standard"
    prompt_config["default_id"] = requested_prompt

    _normalize_schedules_v4(config, targets, migrating=migrating)
    _normalize_bindings_v4(config, prompt_config, targets, strategies)
    config["config_version"] = CURRENT_CONFIG_VERSION
    return config


def normalize_config(
    config: dict, *, persisted: dict | None = None, prefer_mirrors: bool = False
) -> dict:
    """Run the full v2→v3→v4 normalization pipeline without writing the file."""
    original_version = int(config.get("config_version") or 0)
    if original_version < 2:
        config[LEGACY_MIGRATION_KEY] = True
    config = ensure_smart_sheet_config(config)
    config = ensure_prompt_config(config)
    return normalize_config_v4(
        config,
        original_version=original_version,
        persisted=persisted,
        prefer_mirrors=prefer_mirrors,
    )


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
    config = ensure_jev_config(normalize_config(config))
    config.pop(LEGACY_MIGRATION_KEY, None)
    config["config_version"] = CURRENT_CONFIG_VERSION
    for partition in ("mcp_servers", "reply_listeners"):
        if not isinstance(config.get(partition), list):
            config[partition] = []
    return config, config_path


def _load_persisted_config(config_path: Path) -> dict | None:
    """Best-effort read of the last normalized config used as a merge baseline."""
    if not config_path.exists():
        return None
    try:
        config, _loaded_path = load_config(config_path)
        return config
    except (OSError, ValueError):
        return None


def save_config(
    config: dict,
    path: str | os.PathLike | None = None,
    *,
    persisted: dict | None | object = _UNSET,
    prefer_mirrors: bool = False,
) -> Path:
    config_path = Path(path).expanduser().resolve() if path else default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    candidate = copy.deepcopy(config)
    if int(candidate.get("config_version") or 0) < 2:
        candidate[LEGACY_MIGRATION_KEY] = True
    if persisted is _UNSET:
        baseline = _load_persisted_config(config_path)
    else:
        baseline = persisted if isinstance(persisted, dict) else None
    normalized = ensure_jev_config(
        normalize_config(candidate, persisted=baseline, prefer_mirrors=prefer_mirrors),
        strict=True,
    )
    normalized.pop(LEGACY_MIGRATION_KEY, None)
    normalized["config_version"] = CURRENT_CONFIG_VERSION
    for partition in ("mcp_servers", "reply_listeners"):
        if not isinstance(normalized.get(partition), list):
            normalized[partition] = []
    clean = {key: value for key, value in normalized.items() if not key.startswith("_")}
    temp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(clean, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temp_path.replace(config_path)
    return config_path


UNIQUE_ID_SECTIONS = (
    ("ai_strategies", None, "AI 策略"),
    ("publish_targets", None, "发布目标"),
    ("prompts", "items", "提示词"),
    ("smart_sheet", "templates", "腾讯文档模板"),
    ("schedules", None, "定时任务"),
    ("mcp_servers", None, "MCP 服务器"),
    ("reply_listeners", None, "回复监听"),
)


def _unique_id_items(config: dict, section: str, key: str | None) -> list:
    value = config.get(section)
    if key is not None:
        value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, list) else []


def validate_unique_config_ids(config: dict) -> None:
    """Reject duplicate explicit IDs before a config is written.

    ``ensure_prompt_config``/``ensure_smart_sheet_config`` already reject
    duplicate prompt/template IDs while they fill in fallbacks. This check adds
    the partitions that are persisted as-is (schedules, MCP servers, reply
    listeners) so a stale or hand-edited config cannot silently corrupt the
    runtime, mirroring the old Rust ``validate_unique_config_ids``.
    """
    for section, key, label in UNIQUE_ID_SECTIONS:
        seen: set[str] = set()
        for item in _unique_id_items(config, section, key):
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "").strip()
            if not item_id:
                if section == "schedules":
                    raise ValueError("定时任务 ID 不能为空，请为该定时任务设置唯一 ID")
                continue
            if item_id in seen:
                raise ValueError(
                    f"{label} ID 不能重复：{item_id}，请为每一项设置唯一 ID"
                )
            seen.add(item_id)


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
