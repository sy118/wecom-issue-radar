"""Action dispatch for the personal WeChat backend.

The desktop shell calls :func:`handle_wechat_action` with a verb such as
``wechat_detect`` (the ``wechat_`` prefix is optional) and a JSON payload, and
receives one JSON object back.  Read-only WeChat databases are the only data
source; filling a reply into the WeChat window is *not* implemented here (the
Rust input helper owns it).
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

from . import legacy_import
from .errors import ProxyTalkError
from .runtime import REWRITE_INSTRUCTIONS
from .service import (
    DEFAULT_PROVIDER_ID,
    describe_llm_config,
    get_runtime,
)

_PREFIXES = ("wechat_", "wechat.", "wechat:")

REWRITE_STYLES = {
    "shorter": "shorter",
    "更简短": "shorter",
    "brief": "shorter",
    "politer": "polite",
    "polite": "polite",
    "更礼貌": "polite",
    "casual": "casual",
    "更随意": "casual",
    "another": "regenerate",
    "regenerate": "regenerate",
    "换一个": "regenerate",
}

_SETTINGS_ALLOWED = (
    "wechatDbDir",
    "pollIntervalSeconds",
    "contextMessageLimit",
    "contextCharacterLimit",
    "voiceTranscription",
    "voiceCommand",
    "voiceModelPath",
    "voiceArguments",
    "imageSearchRoots",
    "replyStyles",
    "defaultTone",
    "defaultTriggerMode",
)

DRAFT_ERROR_MESSAGES = {
    "MODEL_NOT_CONFIGURED": "还没有可用的模型连接，请先在设置中配置模型后再生成草稿。",
    "CONTEXT_EMPTY": "这个项目还没有可用于生成回复的聊天记录。",
    "MODEL_HTTP_ERROR": "模型服务返回错误，请检查模型连接后重试。",
    "MODEL_NETWORK_ERROR": "无法连接模型服务，请检查网络或 Base URL。",
    "MODEL_EMPTY_RESPONSE": "模型没有返回任何草稿内容，请重试。",
    "MODEL_INVALID_RESPONSE": "模型返回格式无法解析，请检查模型是否兼容。",
}

_MAX_MEDIA_BYTES = 12 * 1024 * 1024
_MIN_MEDIA_BYTES = 64 * 1024


# ---------------------------------------------------------------------------
# public entry point


def handle_wechat_action(action: str, payload: dict | None = None) -> dict:
    """Run one personal WeChat action and return its JSON payload."""

    if not isinstance(action, str) or not action.strip():
        raise ValueError("个人微信命令缺少 action")
    verb = _normalize_action(action)
    handler = _HANDLERS.get(verb)
    if handler is None:
        raise ValueError(f"不支持的微信动作：{action}")
    body = payload if isinstance(payload, dict) else {}
    return handler(get_runtime(), body)


def _normalize_action(action: str) -> str:
    value = action.strip().lower().replace(".", "_").replace(":", "_").replace("-", "_")
    changed = True
    while changed:
        changed = False
        for prefix in _PREFIXES:
            prefix = prefix.replace(".", "_").replace(":", "_")
            if value.startswith(prefix) and len(value) > len(prefix):
                value = value[len(prefix) :]
                changed = True
    return value


# ---------------------------------------------------------------------------
# environment / account actions


def _detect(runtime, payload: dict) -> dict:
    from .discovery import evaluate as evaluate_candidate
    from .discovery import scan as scan_candidates

    force = bool(payload.get("force"))
    configured = str(runtime.store.get_setting("wechatDbDir", "") or "")
    configured_path = _configured_db_dir(configured)
    cached = bool(configured_path) and not force
    if cached:
        # Fast path: the configured directory is already validated, so skip the
        # multi-root walk unless the caller explicitly asks for a full rescan.
        candidate = evaluate_candidate(configured_path, "当前配置", configured=True)
        candidate["selected"] = True
        discovery = {
            "candidates": [candidate],
            "checked": [],
            "defaultDbDir": str(configured_path),
            "reason": "",
            "scannedAt": _now_iso(),
        }
    else:
        discovery = scan_candidates(configured=configured or None)
    candidates = discovery.get("candidates") or []
    try:
        status = runtime.source.inspect()
    except ProxyTalkError as exc:
        status = {"platformSupported": False, "running": False, "error": exc.message}
    valid = [item for item in candidates if item.get("valid")]
    selected_path = configured or str(discovery.get("defaultDbDir") or "")
    accounts = [
        {
            "accountId": str(item.get("accountId") or ""),
            "dbDir": str(item.get("path") or ""),
            "root": str(item.get("root") or ""),
            "selected": _same_path_text(item.get("path"), selected_path),
            "score": int(item.get("score") or 0),
            "reason": str(item.get("reason") or ""),
        }
        for item in valid
    ]
    if configured and not any(_same_path_text(item.get("dbDir"), configured) for item in accounts):
        root = Path(configured).parent
        accounts.insert(
            0,
            {
                "accountId": root.name,
                "dbDir": configured,
                "root": str(root),
                "selected": True,
                "score": 0,
                "reason": "当前设置（本次扫描未发现同目录）",
            },
        )
    db_dir = selected_path
    found = bool(valid) or bool(configured_path)
    return {
        "found": found,
        "dbDir": db_dir,
        "accounts": accounts,
        "candidates": candidates,
        "running": bool(status.get("running")),
        "version": _wechat_version(),
        "platformSupported": bool(status.get("platformSupported", True)),
        "selected": bool(db_dir),
        "ready": bool(status.get("ready")),
        "cached": cached,
        "forced": force,
        "reason": "" if found else str(discovery.get("reason") or "未检测到可用的个人微信数据目录。"),
        "checkedPaths": discovery.get("checked") or [],
        "scannedAt": discovery.get("scannedAt") or "",
        "keyCount": int(status.get("keyCount") or 0),
        "databaseCount": int(status.get("databaseCount") or 0),
        "coveredDatabaseCount": int(status.get("coveredDatabaseCount") or 0),
        "imageKeyConfigured": bool(status.get("imageKeyConfigured")),
        "imageKeyVerified": bool(status.get("imageKeyVerified")),
    }


def _configured_db_dir(configured: str) -> Path | None:
    """Return the configured directory when it still looks like a db_storage."""

    if not configured:
        return None
    try:
        path = Path(configured).expanduser()
        if not path.is_dir() or not (path / "session" / "session.db").is_file():
            return None
        return path.resolve()
    except OSError:
        return None


def _image_key_scan(runtime, payload: dict) -> dict:
    try:
        status = runtime.source.scan_image_key()
    except ProxyTalkError as exc:
        return {"ok": False, "message": exc.message, "code": exc.code, "retryable": exc.retryable}
    return {
        "ok": True,
        "message": "已扫描并通过真实图片验证图片密钥。",
        "imageKeyConfigured": bool(status.get("imageKeyConfigured")),
        "imageKeyVerified": bool(status.get("imageKeyVerified")),
        "wechat": status,
    }


def _image_key_save(runtime, payload: dict) -> dict:
    key = str(payload.get("imageKey") or payload.get("key") or "").strip()
    try:
        status = runtime.source.save_image_key(key)
    except ProxyTalkError as exc:
        return {"ok": False, "message": exc.message, "code": exc.code, "retryable": exc.retryable}
    return {
        "ok": True,
        "message": "图片 AES Key 已保存并通过验证。" if key else "已清除图片 AES Key。",
        "imageKeyConfigured": bool(status.get("imageKeyConfigured")),
        "imageKeyVerified": bool(status.get("imageKeyVerified")),
        "wechat": status,
    }


def _select(runtime, payload: dict) -> dict:
    db_dir = str(payload.get("dbDir") or "").strip()
    if not db_dir:
        raise ProxyTalkError("WECHAT_DB_NOT_FOUND", "请选择包含 session/session.db 的 db_storage 目录。")
    status = runtime.source.select_root(db_dir)
    return {
        "selected": True,
        "dbDir": str(status.get("configuredDbDir") or db_dir),
        "ready": bool(status.get("ready")),
        "wechat": status,
    }


def _scan_keys(runtime, payload: dict) -> dict:
    from .discovery import scan as scan_candidates
    from .wechat_source import wechat_is_running

    db_dir = str(payload.get("dbDir") or "").strip()
    if db_dir:
        try:
            runtime.source.select_root(db_dir)
        except ProxyTalkError as exc:
            return {"ok": False, "message": exc.message, "code": exc.code}
    current = str(runtime.store.get_setting("wechatDbDir", "") or "")
    if not current:
        discovery = scan_candidates()
        current = str(discovery.get("defaultDbDir") or "")
        if not current:
            return {
                "ok": False,
                "message": str(discovery.get("reason") or "未检测到个人微信数据目录，请先在配置中心选择 db_storage 目录。"),
                "code": "WECHAT_DB_NOT_FOUND",
                "candidates": discovery.get("candidates") or [],
            }
    if not wechat_is_running():
        return {
            "ok": False,
            "message": "未检测到正在运行的微信，请先打开并登录微信后重试。",
            "code": "WECHAT_NOT_RUNNING",
        }
    try:
        status = runtime.source.unlock(current)
    except ProxyTalkError as exc:
        return {"ok": False, "message": exc.message, "code": exc.code, "retryable": exc.retryable}
    account_id = Path(current).parent.name
    covered = int(status.get("coveredDatabaseCount") or 0)
    total = int(status.get("databaseCount") or 0)
    return {
        "ok": True,
        "message": f"已读取并保存微信数据库密钥（{covered}/{total} 个数据库）。",
        "accountId": account_id,
        "wechat": status,
    }


def _chats(runtime, payload: dict) -> dict:
    return {"chats": runtime.source.list_chats()}


def _bootstrap(runtime, payload: dict) -> dict:
    """One worker round-trip for the personal-WeChat first screen.

    Images are deliberately excluded: media stays on the existing lazy
    ``media_preview`` path so the bootstrap response stays small.
    """
    project_id = str(payload.get("projectId") or "").strip()
    result: dict = {
        "detect": _detect(runtime, payload),
        "workspace": _projects(runtime, {}),
        "settings": _settings_snapshot(runtime),
        "providers": _providers(runtime, {}),
    }
    if project_id:
        limit = min(120, max(1, int(payload.get("messageLimit") or 50)))
        messages = _messages(runtime, {"projectId": project_id, "limit": limit})
        result.update(messages)
    return result


# ---------------------------------------------------------------------------
# project actions


def _projects(runtime, payload: dict) -> dict:
    default = runtime.ensure_default_provider()
    projects = []
    for project in runtime.store.projects():
        item = dict(project)
        draft = runtime.store.draft(project["id"])
        item["draft"] = _draft_summary(draft)
        projects.append(item)
    return {
        "projects": projects,
        "defaultProviderId": default["id"],
        "llm": describe_llm_config(),
        "warnings": runtime.warnings(),
    }


def _project_save(runtime, payload: dict) -> dict:
    project = payload.get("project")
    if not isinstance(project, dict):
        raise ProxyTalkError("INVALID_PROJECT", "项目数据必须是 JSON 对象。")
    if not str(project.get("providerId") or "").strip():
        default_id = runtime.ensure_default_provider()["id"]
        if default_id:
            project = {**project, "providerId": default_id}
    result = runtime.handle({"kind": "project.save", "project": project})
    saved = result["project"]
    project_id = str(saved["id"])
    # Keep both shapes: the desktop api normalizes the raw object, while the
    # documented action contract nests it under ``project``.
    return {**saved, "project": saved, "draft": runtime.store.draft(project_id)}


def _project_delete(runtime, payload: dict) -> dict:
    project_id = _required_project_id(payload)
    runtime.handle({"kind": "project.delete", "projectId": project_id})
    return {"deleted": True, "projectId": project_id}


def _project_pause(runtime, payload: dict) -> dict:
    project_id = _required_project_id(payload)
    project = runtime.store.project(project_id)
    enabled = bool(payload.get("enabled", not project.get("enabled")))
    updated = runtime.handle(
        {"kind": "project.save", "project": {**project, "enabled": enabled}}
    )["project"]
    return {"project": updated}


# ---------------------------------------------------------------------------
# messages / media


def _messages(runtime, payload: dict) -> dict:
    project_id = _required_project_id(payload)
    limit = min(500, max(1, int(payload.get("limit") or 120)))
    before = int(payload["before"]) if payload.get("before") else None
    messages = runtime.store.messages(project_id, limit=limit, before=before)
    for message in messages:
        media = message.get("media") or []
        message["hasMedia"] = bool(media)
        message["mediaCount"] = len(media)
    return {"messages": messages, "hasMore": len(messages) >= limit}


def _media_preview(runtime, payload: dict) -> dict:
    project_id = _required_project_id(payload)
    message_id = str(payload.get("messageId") or "").strip()
    if not message_id:
        raise ProxyTalkError("INVALID_MESSAGE_ID", "缺少消息 ID。")
    project = runtime.store.project(project_id)
    message = runtime.store.message(project_id, message_id)
    try:
        max_bytes = int(payload.get("maxBytes") or _MAX_MEDIA_BYTES)
    except (TypeError, ValueError):
        max_bytes = _MAX_MEDIA_BYTES
    max_bytes = min(_MAX_MEDIA_BYTES, max(_MIN_MEDIA_BYTES, max_bytes))
    try:
        preview = runtime.source.image_preview(project, message, max_bytes=max_bytes)
    except ProxyTalkError as exc:
        return {
            "available": False,
            "dataUrl": "",
            "mime": "",
            "reason": exc.message,
            "code": exc.code,
        }
    except Exception:  # noqa: BLE001 - image probing is local and best effort
        return {
            "available": False,
            "dataUrl": "",
            "mime": "",
            "reason": "本机微信图片读取失败。请到「个人微信 → 配置中心」检查图片密钥、微信数据目录和图片搜索目录后重试。",
            "code": "WECHAT_IMAGE_READ_FAILED",
        }
    if not preview.get("available"):
        return {
            "available": False,
            "dataUrl": "",
            "mime": "",
            "reason": str(preview.get("reason") or "图片暂时无法预览。"),
        }
    mime = str(preview.get("mimeType") or "image/jpeg")
    data = str(preview.get("dataBase64") or "")
    return {
        "available": True,
        "dataUrl": f"data:{mime};base64,{data}",
        "mime": mime,
        "reason": "",
    }


# ---------------------------------------------------------------------------
# drafts


def _draft_generate(runtime, payload: dict) -> dict:
    project_id = _required_project_id(payload)
    _require_provider(runtime, project_id)
    draft = runtime.generate_sync(project_id, timeout=_timeout(payload))
    _raise_on_draft_failure(draft)
    return {"draft": _decorate_draft(draft), "pending": draft.get("status") == "generating"}


def _draft_rewrite(runtime, payload: dict) -> dict:
    project_id = _required_project_id(payload)
    instruction = str(payload.get("instruction") or "").strip()[:500]
    style_id = str(payload.get("styleId") or payload.get("style") or "").strip()
    if not instruction:
        key = REWRITE_STYLES.get(style_id) or REWRITE_STYLES.get(style_id.lower())
        if key:
            instruction = REWRITE_INSTRUCTIONS[key]
    if not instruction:
        for style in _reply_styles(runtime):
            if style_id and style_id in {str(style.get("id") or ""), str(style.get("label") or "")}:
                instruction = str(style.get("instruction") or "").strip()[:500]
                break
    if not instruction:
        raise ProxyTalkError("INVALID_REWRITE", "不支持的草稿改写方式。")
    _require_provider(runtime, project_id)
    draft = runtime.generate_sync(
        project_id, instruction=instruction, timeout=_timeout(payload)
    )
    _raise_on_draft_failure(draft)
    return {"draft": _decorate_draft(draft), "pending": draft.get("status") == "generating"}


def _reply_styles(runtime) -> list[dict]:
    settings = runtime.store.settings()
    styles = settings.get("replyStyles")
    if isinstance(styles, list) and styles:
        return [item for item in styles if isinstance(item, dict)]
    from .store import default_reply_styles

    return default_reply_styles()


def _draft_save(runtime, payload: dict) -> dict:
    project_id = _required_project_id(payload)
    content = payload.get("content")
    if not isinstance(content, str):
        raise ProxyTalkError("INVALID_DRAFT", "草稿内容必须是文本。")
    runtime.store.project(project_id)
    draft = runtime.store.save_draft(
        project_id,
        content=content[:50_000],
        status="ready" if content.strip() else "idle",
        stale=False,
        errorCode="",
    )
    return {"draft": _decorate_draft(draft)}


# ---------------------------------------------------------------------------
# poll / export / settings / providers


def _poll(runtime, payload: dict) -> dict:
    result = runtime.poll_once()
    for entry in result.get("projects") or []:
        draft = entry.get("draft")
        if isinstance(draft, dict):
            entry["draft"] = _decorate_draft(draft)
    return result


def _export(runtime, payload: dict) -> dict:
    project_id = _required_project_id(payload)
    project = runtime.store.project(project_id)
    destination = payload.get("destination") or payload.get("dir")
    if destination:
        target = Path(str(destination)).expanduser()
        target.mkdir(parents=True, exist_ok=True)
    else:
        base = runtime.state_root / "exports"
        base.mkdir(parents=True, exist_ok=True)
        target = base / f"{_safe_name(project['name'])}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        target.mkdir(parents=True, exist_ok=True)
    result = runtime.exporter.export(
        project_id,
        str(target),
        start_time=_epoch(payload.get("start") or payload.get("startTime")),
        end_time=_epoch(payload.get("end") or payload.get("endTime")),
    )
    directory = Path(str(result["path"]))
    files = []
    if directory.is_dir():
        files = sorted(
            path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()
        )
    return {
        "dir": str(directory),
        "files": files,
        "messageCount": int(result.get("messageCount") or 0),
        "mediaCount": int(result.get("mediaCount") or 0),
    }


def _settings(runtime, payload: dict) -> dict:
    # The desktop settings drawer sends ``{settings}`` to this verb as well; accept it
    # as a save so a stale contract cannot silently drop user preferences.
    if isinstance(payload.get("settings"), dict) and payload["settings"]:
        _apply_settings(runtime, payload)
    return _settings_snapshot(runtime)


def _settings_save(runtime, payload: dict) -> dict:
    _apply_settings(runtime, payload)
    return {"settings": runtime.store.settings(), "warnings": runtime.warnings()}


def _settings_snapshot(runtime) -> dict:
    default = runtime.ensure_default_provider()
    return {
        "settings": runtime.store.settings(),
        "vaultMode": runtime.vault_mode,
        "defaultProviderId": default["id"],
        "llm": describe_llm_config(),
        "dataRoot": str(runtime.state_root),
        "database": str(runtime.store.path),
        "warnings": runtime.warnings(),
    }


def _settings_values(payload: dict) -> dict | None:
    raw = payload.get("settings")
    if not isinstance(raw, dict):
        return None
    values = {key: raw[key] for key in _SETTINGS_ALLOWED if key in raw}
    if "dbDir" in raw and "wechatDbDir" not in values:
        values["wechatDbDir"] = raw["dbDir"]
    return values


def _apply_settings(runtime, payload: dict) -> None:
    values = _settings_values(payload)
    if values is None:
        raise ProxyTalkError("INVALID_SETTINGS", "设置内容必须是 JSON 对象。")
    requested = values.pop("wechatDbDir", None) if "wechatDbDir" in values else None
    if requested is not None:
        current = str(runtime.store.get_setting("wechatDbDir", "") or "")
        requested_text = str(requested or "")
        if requested_text != current:
            if not requested_text:
                raise ProxyTalkError("INVALID_SETTINGS", "请通过微信数据页切换账号目录。")
            runtime.source.select_root(requested_text)
    if values:
        runtime.store.save_settings(values)


def _providers(runtime, payload: dict) -> dict:
    default = runtime.ensure_default_provider()
    mcp_servers = []
    for server in runtime.store.mcp_servers():
        mcp_servers.append({**server, "toolCount": len(server.get("catalog") or [])})
    return {
        "providers": runtime.store.providers(),
        "mcpServers": mcp_servers,
        "defaultProviderId": default["id"],
        "llm": describe_llm_config(),
        "vaultMode": runtime.vault_mode,
        "warnings": runtime.warnings(),
    }


def _provider_save(runtime, payload: dict) -> dict:
    provider = payload.get("provider")
    if not isinstance(provider, dict):
        raise ProxyTalkError("INVALID_PROVIDER", "模型配置必须是 JSON 对象。")
    if not str(provider.get("id") or "").strip():
        raise ProxyTalkError("INVALID_PROVIDER", "模型配置 ID 不能为空。")
    result = runtime.handle(
        {
            "kind": "provider.save",
            "provider": provider,
            "apiKey": payload.get("apiKey"),
            "clearSecret": bool(payload.get("clearSecret")),
        }
    )
    return {"provider": result["provider"], "warnings": runtime.warnings()}


def _provider_test(runtime, payload: dict) -> dict:
    provider_id = str(payload.get("providerId") or "").strip() or DEFAULT_PROVIDER_ID
    result = runtime.handle({"kind": "provider.test", "providerId": provider_id})
    return {"providerId": provider_id, **result}


def _import_proxytalk(runtime, payload: dict) -> dict:
    return legacy_import.import_proxytalk(runtime, payload)


_HANDLERS = {
    "detect": _detect,
    "select": _select,
    "scan_keys": _scan_keys,
    "unlock": _scan_keys,
    "chats": _chats,
    "chat_list": _chats,
    "projects": _projects,
    "project_list": _projects,
    "project_save": _project_save,
    "project_delete": _project_delete,
    "project_remove": _project_delete,
    "project_pause": _project_pause,
    "messages": _messages,
    "messages_list": _messages,
    "media_preview": _media_preview,
    "media_image": _media_preview,
    "image_preview": _media_preview,
    "image_key_scan": _image_key_scan,
    "image_key_save": _image_key_save,
    "scan_image_key": _image_key_scan,
    "save_image_key": _image_key_save,
    "draft_generate": _draft_generate,
    "generate": _draft_generate,
    "draft_rewrite": _draft_rewrite,
    "rewrite": _draft_rewrite,
    "draft_save": _draft_save,
    "draft_update": _draft_save,
    "poll": _poll,
    "export": _export,
    "export_run": _export,
    "settings": _settings,
    "settings_get": _settings,
    "settings_save": _settings_save,
    "providers": _providers,
    "provider_list": _providers,
    "provider_save": _provider_save,
    "provider_test": _provider_test,
    "bootstrap": _bootstrap,
    "import_proxytalk": _import_proxytalk,
    "import_legacy": _import_proxytalk,
}


# ---------------------------------------------------------------------------
# helpers


def _required_project_id(payload: dict) -> str:
    project_id = str(payload.get("projectId") or payload.get("id") or "").strip()
    if not project_id:
        raise ProxyTalkError("INVALID_PROJECT_ID", "缺少项目 ID。")
    return project_id


def _same_path_text(left, right) -> bool:
    if not left or not right:
        return False
    try:
        left_path = Path(str(left)).expanduser().resolve(strict=False)
        right_path = Path(str(right)).expanduser().resolve(strict=False)
        return str(left_path).casefold() == str(right_path).casefold()
    except (OSError, RuntimeError):
        return str(left).casefold() == str(right).casefold()


def _require_provider(runtime, project_id: str) -> None:
    project = runtime.store.project(project_id)
    if not str(project.get("providerId") or "").strip():
        raise ProxyTalkError("MODEL_NOT_CONFIGURED", DRAFT_ERROR_MESSAGES["MODEL_NOT_CONFIGURED"])


def _raise_on_draft_failure(draft: dict) -> None:
    if draft.get("status") != "error":
        return
    code = str(draft.get("errorCode") or "DRAFT_FAILED")
    message = DRAFT_ERROR_MESSAGES.get(code) or f"草稿生成失败（{code}），请稍后重试。"
    raise ProxyTalkError(code, message, retryable=True)


def _draft_summary(draft: dict) -> dict:
    code = str(draft.get("errorCode") or "")
    return {
        "content": str(draft.get("content") or ""),
        "status": str(draft.get("status") or "idle"),
        "stale": bool(draft.get("stale")),
        "errorCode": code,
        "errorMessage": DRAFT_ERROR_MESSAGES.get(code, "") if draft.get("status") == "error" else "",
        "model": str(draft.get("model") or ""),
        "updatedAt": str(draft.get("updatedAt") or ""),
        "hasContent": bool(str(draft.get("content") or "").strip()),
    }


def _decorate_draft(draft: dict) -> dict:
    value = dict(draft)
    code = str(value.get("errorCode") or "")
    if value.get("status") == "error" and code:
        value["errorMessage"] = DRAFT_ERROR_MESSAGES.get(code) or f"草稿生成失败（{code}）。"
    else:
        value.setdefault("errorMessage", "")
    return value


def _timeout(payload: dict) -> float:
    try:
        value = float(payload.get("timeoutSeconds") or 180)
    except (TypeError, ValueError):
        value = 180.0
    return min(600.0, max(10.0, value))


def _epoch(value) -> int:
    """Accept epoch seconds/milliseconds, numeric strings and local ISO datetimes."""

    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip()
    if not text:
        return 0
    try:
        return max(0, int(float(text)))
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return max(0, int(parsed.timestamp()))


def _safe_name(value: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", str(value or "")).strip(" .")[:80] or "chat"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# WeChat version (best effort, never raises)


def _wechat_version() -> str:
    if sys.platform != "win32":
        return ""
    try:
        from .wechat_crypto import _process_executable_path
        from .wechat_image import wechat_process_ids

        for pid in wechat_process_ids():
            path = _process_executable_path(int(pid))
            if path is None or path.name.lower() not in {"weixin.exe", "wechat.exe"}:
                continue
            version = _file_version(str(path))
            if version:
                return version
    except Exception:
        return ""
    return ""


def _file_version(path: str) -> str:
    try:
        import ctypes
        from ctypes import wintypes

        version_dll = ctypes.WinDLL("version", use_last_error=True)
        version_dll.GetFileVersionInfoSizeW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        version_dll.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        version_dll.GetFileVersionInfoW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        version_dll.GetFileVersionInfoW.restype = wintypes.BOOL
        version_dll.VerQueryValueW.argtypes = [
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.UINT),
        ]
        version_dll.VerQueryValueW.restype = wintypes.BOOL

        size = version_dll.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        buffer = ctypes.create_string_buffer(size)
        if not version_dll.GetFileVersionInfoW(path, 0, size, buffer):
            return ""
        for block in ("040904b0", "080404b0"):
            value = ctypes.c_void_p()
            length = wintypes.UINT()
            query = f"\\StringFileInfo\\{block}\\FileVersion"
            if version_dll.VerQueryValueW(buffer, query, ctypes.byref(value), ctypes.byref(length)) and length.value:
                text = ctypes.wstring_at(value.value, max(0, int(length.value) - 1)).strip()
                if text:
                    return text
        value = ctypes.c_void_p()
        length = wintypes.UINT()
        if version_dll.VerQueryValueW(buffer, "\\", ctypes.byref(value), ctypes.byref(length)) and length.value >= 16:
            info = ctypes.cast(value, ctypes.POINTER(wintypes.DWORD * 4)).contents
            file_version_ms = int(info[2])
            file_version_ls = int(info[3])
            return (
                f"{file_version_ms >> 16}.{file_version_ms & 0xFFFF}."
                f"{file_version_ls >> 16}.{file_version_ls & 0xFFFF}"
            )
    except Exception:
        return ""
    return ""


__all__ = ["handle_wechat_action"]
