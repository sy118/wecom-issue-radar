"""Reusable reply phrases stored in ``~/.wecom-issue-radar/phrases.json``.

The file is written atomically (temp file + ``os.replace``). Built-in phrases are
returned on the first run (empty store) but are not written to disk until the
user saves or edits something, so a fresh profile stays clean.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path

DEFAULT_CATEGORY = "自定义"

DEFAULT_PHRASES = [
    {
        "id": "builtin_leave",
        "title": "请假申请",
        "category": "人事",
        "content": (
            "{{姓名}}，您好。我因{{事由}}需要请假，时间为{{日期}}；"
            "期间工作已安排同事跟进，紧急事项可随时联系我，感谢批准。"
        ),
        "variables": ["姓名", "事由", "日期"],
    },
    {
        "id": "builtin_urge_progress",
        "title": "催进度",
        "category": "推进",
        "content": (
            "{{姓名}}，关于{{事项}}想跟您同步一下当前进度。方便的话请在{{日期}}前反馈；"
            "如果遇到阻塞请告诉我，我来协调资源，避免影响整体计划。"
        ),
        "variables": ["姓名", "事项", "日期"],
    },
    {
        "id": "builtin_decline",
        "title": "婉拒请求",
        "category": "沟通",
        "content": (
            "{{姓名}}，感谢您想到我。由于{{事由}}，这次我暂时无法承接{{事项}}；"
            "建议可以找相关同事协助，如有需要我可以提供背景信息和已有材料。"
        ),
        "variables": ["姓名", "事由", "事项"],
    },
    {
        "id": "builtin_apology",
        "title": "道歉说明",
        "category": "道歉",
        "content": (
            "{{姓名}}，非常抱歉，{{事由}}给您带来了不便。我们已经第一时间处理，"
            "预计{{日期}}恢复/解决；后续进展我会及时同步，如还有其他影响请随时告诉我。"
        ),
        "variables": ["姓名", "事由", "日期"],
    },
    {
        "id": "builtin_report",
        "title": "工作汇报",
        "category": "汇报",
        "content": (
            "{{姓名}}，{{日期}}工作进展：{{进展}}。当前风险：{{风险}}。"
            "下一步计划：{{计划}}。如需调整优先级请随时告诉我。"
        ),
        "variables": ["姓名", "日期", "进展", "风险", "计划"],
    },
]

# Same placeholder syntax as the renderer: ``{{姓名}}`` / ``{{ name }}``.
_VARIABLE_PATTERN = re.compile(r"\{\{\s*([^{}\s][^{}]*?)\s*\}\}")


def phrases_path() -> Path:
    from worker.pipeline.config_store import default_config_path

    return default_config_path().parent / "phrases.json"


def extract_variables(content: str) -> list[str]:
    variables: list[str] = []
    for match in _VARIABLE_PATTERN.finditer(str(content or "")):
        name = match.group(1).strip()
        if name and name not in variables:
            variables.append(name)
    return variables


def _normalize_variables(value: object, content: str) -> list[str]:
    variables: list[str] = []
    if isinstance(value, list):
        for item in value:
            name = str(item or "").strip()
            if name and name not in variables:
                variables.append(name)
    return variables or extract_variables(content)


def _text(value: object, default: str = "") -> str:
    return str(value if value is not None else default).strip()


def _public(phrase: dict) -> dict:
    content = _text(phrase.get("content"))
    return {
        "id": _text(phrase.get("id")),
        "title": _text(phrase.get("title")),
        "category": _text(phrase.get("category")) or DEFAULT_CATEGORY,
        "content": content,
        "variables": _normalize_variables(phrase.get("variables"), content),
    }


def _sort_phrases(phrases: list[dict]) -> list[dict]:
    return sorted(phrases, key=lambda item: (item["title"].casefold(), item["id"]))


def _normalize_phrase(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    phrase = _public(raw)
    if not phrase["title"] or not phrase["content"]:
        return None
    return phrase


def _load_stored() -> list[dict]:
    path = phrases_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(raw, dict):
        value = raw.get("phrases")
    elif isinstance(raw, list):
        value = raw
    else:
        value = []
    if not isinstance(value, list):
        return []
    phrases = []
    for item in value:
        normalized = _normalize_phrase(item)
        if normalized is not None:
            phrases.append(normalized)
    return _sort_phrases(phrases)


def _write_stored(phrases: list[dict]) -> None:
    path = phrases_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "phrases": _sort_phrases(phrases)}
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temp_path = path.with_name(path.name + ".tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise ValueError(f"保存话术失败：{exc}") from exc


def list_phrases() -> list[dict]:
    """Return saved phrases, or the built-in defaults while the store is empty."""
    stored = _load_stored()
    if stored:
        return stored
    return _sort_phrases([_public(copy.deepcopy(item)) for item in DEFAULT_PHRASES])


def _base_phrases() -> list[dict]:
    """Mutable base for a write: saved phrases, or the virtual built-in defaults."""
    stored = _load_stored()
    if stored:
        return stored
    return [_public(copy.deepcopy(item)) for item in DEFAULT_PHRASES]


def _new_phrase_id(title: str, category: str, existing: set[str]) -> str:
    digest = hashlib.sha1(f"{title}|{category}".encode("utf-8")).hexdigest()[:10]
    candidate = f"phrase_{digest}"
    suffix = 2
    while candidate in existing:
        candidate = f"phrase_{digest}_{suffix}"
        suffix += 1
    return candidate


def save_phrase(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("话术内容格式无效")
    title = _text(payload.get("title"))
    content = _text(payload.get("content"))
    if not title:
        raise ValueError("话术标题不能为空")
    if not content:
        raise ValueError("话术内容不能为空")
    category = _text(payload.get("category")) or DEFAULT_CATEGORY
    variables = _normalize_variables(payload.get("variables"), content)
    phrase_id = _text(payload.get("id"))

    stored = _base_phrases()
    existing_ids = {item["id"] for item in stored}
    if not phrase_id:
        phrase_id = _new_phrase_id(title, category, existing_ids)
    phrase = {
        "id": phrase_id,
        "title": title,
        "category": category,
        "content": content,
        "variables": variables,
    }
    replaced = False
    for index, item in enumerate(stored):
        if item["id"] == phrase_id:
            stored[index] = phrase
            replaced = True
            break
    if not replaced:
        stored.append(phrase)
    _write_stored(stored)
    return _public(phrase)


def delete_phrase(phrase_id: object) -> dict:
    target = _text(phrase_id)
    if not target:
        raise ValueError("请提供要删除的话术 ID")
    stored = _base_phrases()
    remaining = [item for item in stored if item["id"] != target]
    if len(remaining) != len(stored):
        _write_stored(remaining)
    return {"ok": True}
