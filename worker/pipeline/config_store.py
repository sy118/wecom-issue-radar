from __future__ import annotations

import copy
import json
import os
import re
import sys
from pathlib import Path


RUNTIME_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
EXAMPLE_CONFIG = RUNTIME_ROOT / "config.example.json"
DEFAULT_USER_DIR = Path.home() / ".wecom-issue-radar"
DEFAULT_USER_CONFIG = DEFAULT_USER_DIR / "config.local.json"


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
    items = prompt_config.get("items")
    if not isinstance(items, list) or not items:
        prompt_config["items"] = copy.deepcopy(DEFAULT_PROMPTS)
    else:
        seen: set[str] = set()
        normalized = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            prompt_id = slugify(item.get("id") or item.get("name") or f"prompt_{index}")
            while prompt_id in seen:
                prompt_id = f"{prompt_id}_{index}"
            seen.add(prompt_id)
            normalized.append(
                {
                    "id": prompt_id,
                    "name": str(item.get("name") or prompt_id),
                    "description": str(item.get("description") or ""),
                    "content": str(item.get("content") or ""),
                }
            )
        prompt_config["items"] = normalized or copy.deepcopy(DEFAULT_PROMPTS)

    ids = {item["id"] for item in prompt_config["items"]}
    if prompt_config.get("default_id") not in ids:
        prompt_config["default_id"] = prompt_config["items"][0]["id"]
    return config


def load_config(path: str | os.PathLike | None = None) -> tuple[dict, Path]:
    config_path = Path(path).expanduser().resolve() if path else default_config_path()
    example = load_example()
    local = {}
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as file:
            local = json.load(file)
    config = ensure_prompt_config(deep_merge(example, local))
    return config, config_path


def save_config(config: dict, path: str | os.PathLike | None = None) -> Path:
    config_path = Path(path).expanduser().resolve() if path else default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    clean = {key: value for key, value in ensure_prompt_config(copy.deepcopy(config)).items() if not key.startswith("_")}
    temp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(clean, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temp_path.replace(config_path)
    return config_path


def selected_prompt(config: dict, prompt_id: str | None = None) -> dict:
    prompt_config = ensure_prompt_config(config).get("prompts", {})
    target = prompt_id or prompt_config.get("default_id")
    for item in prompt_config.get("items", []):
        if item.get("id") == target:
            return item
    return prompt_config["items"][0]


def slugify(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "_", str(value or "").strip())
    return text.strip("_").lower() or "prompt"
