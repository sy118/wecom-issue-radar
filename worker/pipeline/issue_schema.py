from __future__ import annotations

import copy
import json
import math
import re
from datetime import datetime


FIELD_TYPES = {
    "text",
    "long_text",
    "single_select",
    "multiple_select",
    "boolean",
    "number",
    "date",
    "datetime",
    "url",
}

RESERVED_FIELD_KEYS = {
    "key",
    "values",
    "seed_message_id",
    "context_message_ids",
    "question_message_ids",
    "image_refs",
    "timeline",
    "tenant",
    "sender",
    "sender_id",
    "message_time",
    "raw_message_keys",
    "context_message_keys",
    "expected_image_count",
    "image_assignments",
    "image_status",
    "missing_image_names",
    "module_inference",
}

LEGACY_MIRROR_KEYS = {
    "problem_description",
    "module_text",
    "issue_category_text",
    "issue_summary_text",
    "reason",
}

LEGACY_FIELD_ALIASES = {
    "module_text": ("模块",),
    "issue_category_text": ("问题分类",),
    "problem_description": ("问题描述",),
    "issue_summary_text": ("问题总结",),
    "reason": ("原因",),
}

DEFAULT_ISSUE_FIELDS = [
    {
        "key": "problem_description",
        "label": "问题描述",
        "type": "long_text",
        "required": True,
        "instruction": "简洁、客观地描述问题现象，保留必要的业务上下文。",
        "options": [],
        "default_value": "",
    },
    {
        "key": "module_text",
        "label": "模块",
        "type": "single_select",
        "required": True,
        "instruction": "选择问题所属的产品或业务模块。",
        "options": [
            "订单/售后",
            "回收",
            "财务",
            "库存/采购",
            "人事/薪酬",
            "运营/组织",
            "统计报表",
            "资产管理",
            "前端",
            "APP",
            "数仓/研发",
            "网站",
            "OA",
        ],
        "default_value": "订单/售后",
    },
    {
        "key": "issue_category_text",
        "label": "问题分类",
        "type": "single_select",
        "required": False,
        "instruction": "按照问题性质选择最匹配的分类；信息不足时选择“待评估”。",
        "options": [
            "提示不清晰",
            "逻辑不合理",
            "逻辑不闭环",
            "功能缺陷",
            "交互不一致",
            "数据异常",
            "线上问题",
            "性能问题",
            "需求",
            "操作问题",
            "数仓/研发",
            "三方系统问题",
            "待评估",
        ],
        "default_value": "待评估",
    },
    {
        "key": "issue_summary_text",
        "label": "问题总结",
        "type": "text",
        "required": False,
        "instruction": "用不超过 20 个汉字概括问题。",
        "options": [],
        "default_value": "",
    },
    {
        "key": "reason",
        "label": "原因/结论",
        "type": "long_text",
        "required": False,
        "instruction": "先给出有聊天依据的结论，再按时间列出关键进展；无法确认时明确写待确认。",
        "options": [],
        "default_value": "结论：待确认",
    },
]


def normalize_issue_fields(value, fallback: list[dict] | None = None) -> list[dict]:
    """Return the canonical issue field schema used by prompts, exports and sync mappings."""
    source = value if isinstance(value, list) and value else (fallback or DEFAULT_ISSUE_FIELDS)
    normalized: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(source, start=1):
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("key") or "").strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key):
            continue
        if key in RESERVED_FIELD_KEYS or key in seen:
            continue
        field_type = str(raw.get("type") or "text").strip().lower()
        if field_type == "multi_select":
            field_type = "multiple_select"
        if field_type not in FIELD_TYPES:
            field_type = "text"
        options = []
        raw_options = raw.get("options") or raw.get("enum") or []
        if not isinstance(raw_options, list):
            raw_options = []
        for option in raw_options:
            text = str(option).strip()
            if text and text not in options:
                options.append(text)
        field = {
            "key": key,
            "label": str(raw.get("label") or raw.get("title") or key).strip() or key,
            "type": field_type,
            "required": bool(raw.get("required")),
            "instruction": str(raw.get("instruction") or "").strip(),
            "options": options if field_type in {"single_select", "multiple_select"} else [],
            "default_value": copy.deepcopy(raw.get("default_value", raw.get("default", ""))),
        }
        field["default_value"] = normalize_field_value(field["default_value"], field, use_default=False)
        normalized.append(field)
        seen.add(key)
    if normalized:
        return normalized
    if source is DEFAULT_ISSUE_FIELDS:
        return copy.deepcopy(DEFAULT_ISSUE_FIELDS)
    return normalize_issue_fields(fallback or DEFAULT_ISSUE_FIELDS)


def validate_issue_fields(fields: list[dict]) -> None:
    if not isinstance(fields, list) or not fields:
        raise ValueError("问题清单至少需要一个有效字段")
    if any(not isinstance(item, dict) for item in fields):
        raise ValueError("问题清单字段格式无效")
    raw_keys = [str(item.get("key") or "").strip() for item in fields]
    if any(
        not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key) or key in RESERVED_FIELD_KEYS
        for key in raw_keys
    ):
        raise ValueError("问题清单字段 Key 无效或占用了系统字段")
    if len(raw_keys) != len(set(raw_keys)):
        raise ValueError("问题清单字段 Key 不能重复")
    invalid_types = [
        str(item.get("type") or "text")
        for item in fields
        if str(item.get("type") or "text") not in FIELD_TYPES | {"multi_select"}
    ]
    if invalid_types:
        raise ValueError(f"问题清单包含不支持的字段类型：{invalid_types[0]}")


def build_model_contract(fields: list[dict]) -> str:
    canonical = normalize_issue_fields(fields)
    descriptions = []
    example = {}
    for field in canonical:
        item = {
            "key": field["key"],
            "label": field["label"],
            "type": field["type"],
            "required": field["required"],
        }
        if field["options"]:
            item["options"] = field["options"]
        if field["instruction"]:
            item["instruction"] = field["instruction"]
        descriptions.append(item)
        example[field["key"]] = example_value(field)
    return (
        f"业务字段定义：\n{json.dumps(descriptions, ensure_ascii=False, indent=2)}\n\n"
        "只返回 JSON，不要 Markdown 代码块，结构必须是：\n"
        "{\n"
        '  "issues": [\n'
        "    {\n"
        '      "seed_message_id": 123,\n'
        '      "context_message_ids": [123, 124],\n'
        '      "question_message_ids": [123],\n'
        f'      "values": {json.dumps(example, ensure_ascii=False)}\n'
        "    }\n"
        "  ]\n"
        "}\n"
        '没有问题时返回 {"issues": []}。seed_message_id、context_message_ids 和 '
        "question_message_ids 必须来自输入消息；values 只能使用上述字段 Key。"
    )


def normalize_issue_values(
    raw_issue: dict,
    fields: list[dict],
    fallback_values: dict | None = None,
) -> dict:
    canonical = normalize_issue_fields(fields)
    nested = raw_issue.get("values") if isinstance(raw_issue.get("values"), dict) else {}
    fallbacks = fallback_values or {}
    result = {}
    for field in canonical:
        key = field["key"]
        if key in nested:
            raw_value = nested[key]
        elif key in raw_issue:
            raw_value = raw_issue[key]
        elif key in fallbacks:
            raw_value = fallbacks[key]
        else:
            raw_value = None
        result[key] = normalize_field_value(raw_value, field)
    validate_required_issue_values(result, canonical, raw_issue)
    return result


def validate_required_issue_values(
    values: dict,
    fields: list[dict],
    issue: dict | None = None,
) -> None:
    issue = issue or {}
    identifier = issue.get("key") or issue.get("seed_message_id") or "未知"
    for field in fields:
        if not field.get("required"):
            continue
        value = values.get(field["key"])
        if value is None or value == "" or value == [] or value == {}:
            label = field.get("label") or field["key"]
            raise ValueError(
                f"问题 {identifier} 的必填字段“{label}”（{field['key']}）为空或无效"
            )


def normalize_field_value(value, field: dict, *, use_default: bool = True):
    field_type = str(field.get("type") or "text")
    options = [str(option) for option in field.get("options") or []]
    default = copy.deepcopy(field.get("default_value", "")) if use_default else ""

    if field_type == "multiple_select":
        raw_values = value if isinstance(value, list) else ([] if value in (None, "") else [value])
        result = []
        for item in raw_values:
            text = str(item).strip()
            if text and (not options or text in options) and text not in result:
                result.append(text)
        if result:
            return result
        if use_default and default not in (None, ""):
            return normalize_field_value(default, field, use_default=False)
        return []

    if field_type == "single_select":
        text = str(value or "").strip()
        if text and (not options or text in options):
            return text
        if use_default and default not in (None, ""):
            fallback = str(default).strip()
            if fallback and (not options or fallback in options):
                return fallback
        return ""

    if field_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value or "").strip().lower()
        if text in {"true", "1", "yes", "y", "是"}:
            return True
        if text in {"false", "0", "no", "n", "否"}:
            return False
        if use_default and default not in (None, ""):
            return normalize_field_value(default, field, use_default=False)
        return ""

    if field_type == "number":
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return value
        text = str(value or "").strip()
        try:
            number = float(text)
            if not math.isfinite(number):
                raise ValueError
            return int(number) if number.is_integer() else number
        except ValueError:
            if use_default and default not in (None, ""):
                return normalize_field_value(default, field, use_default=False)
            return ""

    text = str(value or "").strip()
    if field_type == "date" and text:
        try:
            datetime.strptime(text[:10], "%Y-%m-%d")
            return text[:10]
        except ValueError:
            text = ""
    if field_type == "datetime" and text:
        candidate = text.replace("Z", "+00:00")
        try:
            datetime.fromisoformat(candidate)
        except ValueError:
            text = ""
    if field_type == "url" and text and not re.match(r"^https?://", text, flags=re.IGNORECASE):
        text = ""
    if text:
        return text
    if use_default and default not in (None, ""):
        return str(default).strip()
    return ""


def issue_value(issue: dict, key: str, default=""):
    values = issue.get("values")
    if isinstance(values, dict) and key in values:
        return values.get(key)
    if key in issue:
        return issue.get(key)
    for alias in LEGACY_FIELD_ALIASES.get(key, ()):
        if alias in issue:
            return issue.get(alias)
    return default


def display_value(value) -> str:
    if isinstance(value, list):
        return "、".join(str(item) for item in value)
    if isinstance(value, bool):
        return "是" if value else "否"
    return "" if value is None else str(value)


def example_value(field: dict):
    field_type = field["type"]
    default = field.get("default_value", "")
    has_default = default not in (None, "")
    if has_default:
        normalized = normalize_field_value(default, field, use_default=False)
        if normalized is not None and normalized != "" and normalized != [] and normalized != {}:
            return normalized
    return [] if field_type == "multiple_select" else None
