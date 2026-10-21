"""One-shot worker commands for loading, saving and backing up the config.

The Electron shell owns path resolution, process management and the dialogs; the
normalization, de-duplication and credential handling stay here so the TypeScript
side never has to re-implement the v3 config semantics. The behaviour mirrors
``src-tauri/src/config.rs`` (the v3.5 implementation) with the M0 trade-offs
documented in ``openspec/changes/v4-electron-fusion-redesign/design.md`` (D2/D6):

* ``config_version`` stays ``3``; the v4 structure (models/publish_targets/...)
  is M1 work.
* A backup is a portable JSON envelope with ``_backup_version: 2``. Credential
  and machine-local values keep their key structure but are blanked; importing
  restores them from the current active config by key (not by list index).
  Machine-local paths are never carried across installations, matching
  ``config.rs``.
* Saving from the settings page may carry a stale snapshot; missing runtime
  partitions are inherited from the persisted config unless the caller opts out
  with ``preserveSchedules=False`` (used by the scheduler's save path).

The worker protocol is unchanged: each function returns the payload that
``worker/main.py`` emits as ``{"type": "result", "data": ...}``.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .config_store import (
    default_config_path,
    ensure_prompt_config,
    ensure_smart_sheet_config,
    load_config,
    save_config as _write_config,
    validate_unique_config_ids,
)

CONFIG_BACKUP_FORMAT = "wecom-issue-radar-config-backup"
# Backup envelope version. v1 is the format written by the v3.5 Tauri app; v2
# keeps the key structure while blanking secret values.
CONFIG_BACKUP_VERSION = 2
SUPPORTED_BACKUP_VERSIONS = (1, 2)
MAX_CONFIG_BACKUP_BYTES = 16 * 1024 * 1024

RUNTIME_PARTITIONS = ("schedules", "mcp_servers", "reply_listeners")

# Keys whose values must never leave the machine in a backup. Compared after
# normalizing the key to lowercase ASCII alphanumerics, so api_key/apiKey and
# webhook_url/webhookUrl share one rule. webhook_url_env is intentionally not
# sensitive: it stores an environment variable *name*, not a secret value.
SENSITIVE_KEY_NAMES = frozenset(
    {
        "apikey",
        "secret",
        "clientsecret",
        "corpsecret",
        "webhookurl",
        "token",
        "accesstoken",
        "password",
        "cookies",
        "authorization",
        # Credentials carried by older v3 configs. config.rs removed these from
        # backups as well; keeping them blank here means the same protection but
        # the key structure stays visible in the exported file.
        "corpid",
        "wecomtoken",
        "wecomaccesstoken",
        "wxworktoken",
        "wxworkaccesstoken",
    }
)

# Containers whose *values* are credentials (MCP header/env key/value pairs).
# Their keys are user-visible structure and are kept in backups.
SECRET_CONTAINER_KEY_NAMES = frozenset({"headers", "header", "env", "environment"})

# Machine-local paths and state that belong to this installation only. They are
# blanked in backups and restored from the active config on import so importing
# a backup made on another machine cannot point the app at a missing directory
# (mirrors MACHINE_LOCAL_CONFIG_KEYS in config.rs).
MACHINE_LOCAL_KEY_NAMES = frozenset(
    {
        "wxworkdbdir",
        "wxworkkeysfile",
        "defaultworkspace",
        "diagnostics",
    }
)


def normalize_key_name(key: object) -> str:
    """Normalize a JSON key for credential matching (``api_key`` -> ``apikey``)."""
    return "".join(
        character
        for character in str(key)
        if character.isascii() and character.isalnum()
    ).lower()


def _is_sensitive_key(key: object) -> bool:
    return normalize_key_name(key) in SENSITIVE_KEY_NAMES


def _is_machine_local_key(key: object) -> bool:
    return normalize_key_name(key) in MACHINE_LOCAL_KEY_NAMES


def _is_redacted_key(key: object) -> bool:
    """Keys blanked in exports and restored from the active config on import."""
    return _is_sensitive_key(key) or _is_machine_local_key(key)


def _is_secret_container_key(key: object) -> bool:
    return normalize_key_name(key) in SECRET_CONTAINER_KEY_NAMES


def _blank_value(value: object) -> object:
    """Return an empty value of the same JSON shape for a redacted key."""
    if isinstance(value, dict):
        return {}
    if isinstance(value, list):
        return []
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return 0
    if value is None:
        return None
    return ""


def _blank_container(value: object) -> object:
    """Keep a header/env container's keys but blank every leaf value."""
    if isinstance(value, dict):
        return {key: _blank_container(item) for key, item in value.items()}
    if isinstance(value, list):
        return []
    return _blank_value(value)


def _sanitize_config_value(value: object) -> object:
    if isinstance(value, dict):
        sanitized: dict = {}
        for key, item in value.items():
            if _is_redacted_key(key):
                sanitized[key] = _blank_value(item)
            elif _is_secret_container_key(key):
                sanitized[key] = _blank_container(item)
            else:
                sanitized[key] = _sanitize_config_value(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_config_value(item) for item in value]
    return value


def sanitize_backup_config(config: dict) -> dict:
    """Return a copy of ``config`` with credentials and local paths blanked."""
    if not isinstance(config, dict):
        raise ValueError("配置内容无效：config 必须是一个 JSON 对象")
    return _sanitize_config_value(copy.deepcopy(config))


def _has_restorable_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list)):
        return bool(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return True


def _restore_secret_container(imported_value: object, current_value: object) -> object:
    """Restore header/env values for keys that exist in the active config.

    Keys unknown to the active config stay blank so a hand-edited backup cannot
    inject a credential that was never configured on this machine.
    """
    if isinstance(imported_value, dict) and isinstance(current_value, dict):
        restored: dict = {}
        for key, value in imported_value.items():
            current_item = current_value.get(key)
            if _has_restorable_value(current_item):
                restored[key] = copy.deepcopy(current_item)
            elif isinstance(value, dict):
                restored[key] = _restore_secret_container(value, current_item)
            else:
                restored[key] = value
        return restored
    return imported_value


def _restore_config_value(imported: object, current: object) -> None:
    if isinstance(imported, dict) and isinstance(current, dict):
        _restore_config_dict(imported, current)
    elif isinstance(imported, list) and isinstance(current, list):
        _restore_config_list(imported, current)


def _restore_config_dict(imported: dict, current: dict) -> None:
    # v1 backups removed the affected keys entirely; re-add them when the
    # active config still has a value so an old backup stays importable.
    for key, current_value in current.items():
        if key in imported:
            continue
        if _is_redacted_key(key) or _is_secret_container_key(key):
            if _has_restorable_value(current_value):
                imported[key] = copy.deepcopy(current_value)

    for key, imported_value in imported.items():
        if _is_redacted_key(key):
            if _has_restorable_value(current.get(key)):
                imported[key] = copy.deepcopy(current[key])
            continue
        if _is_secret_container_key(key):
            imported[key] = _restore_secret_container(imported_value, current.get(key))
            continue
        if key in current:
            _restore_config_value(imported_value, current[key])


def _restore_config_list(imported: list, current: list) -> None:
    current_by_id: dict[str, dict] = {}
    for item in current:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        if item_id:
            current_by_id[item_id] = item

    for index, imported_item in enumerate(imported):
        if isinstance(imported_item, dict):
            item_id = str(imported_item.get("id") or "").strip()
            if item_id:
                # Match partitions by ID, never by position: the backup may
                # reorder or drop entries.
                matched = current_by_id.get(item_id)
                if matched is not None:
                    _restore_config_dict(imported_item, matched)
                continue
        if index < len(current):
            _restore_config_value(imported_item, current[index])


def restore_sensitive_values(imported: dict, current: dict) -> dict:
    """Restore blanked credentials in ``imported`` from the active config."""
    if not isinstance(imported, dict):
        raise ValueError("配置备份中的 config 必须是 JSON 对象")
    if not isinstance(current, dict):
        raise ValueError("当前配置不是 JSON 对象，无法恢复敏感项")
    restored = copy.deepcopy(imported)
    _restore_config_value(restored, current)
    return restored


def resolve_config_path(config_path: str | os.PathLike | None) -> Path:
    """Resolve an explicit config path, falling back to the user default."""
    text = str(config_path or "").strip()
    if not text:
        return default_config_path()
    try:
        return Path(text).expanduser().resolve()
    except OSError as exc:
        raise ValueError(f"配置文件路径无效：{exc}") from exc


def _resolve_backup_path(backup_path: str | os.PathLike | None) -> Path:
    text = str(backup_path or "").strip()
    if not text:
        raise ValueError("请选择配置备份文件路径")
    try:
        return Path(text).expanduser().resolve()
    except OSError as exc:
        raise ValueError(f"配置备份路径无效：{exc}") from exc


def _load_current_config(config_path: Path) -> dict:
    try:
        config, _loaded_path = load_config(config_path)
        return config
    except ValueError as exc:
        raise ValueError(f"读取当前配置失败，配置文件可能已损坏（{config_path}）：{exc}") from exc
    except OSError as exc:
        raise ValueError(f"读取当前配置失败（{config_path}）：{exc}") from exc


def _normalized_probe(config: dict) -> dict:
    """Run the normalization pass on a copy to validate effective IDs."""
    probe = copy.deepcopy(config)
    try:
        return ensure_prompt_config(ensure_smart_sheet_config(probe))
    except ValueError:
        raise
    except (AttributeError, KeyError, TypeError) as exc:
        raise ValueError(
            "配置结构无效，无法归一化 prompts/smart_sheet 分区，请检查后重试"
        ) from exc


def _prepare_runtime_partitions(
    incoming: dict,
    current: dict | None,
    *,
    preserve_schedules: bool,
) -> None:
    """Fill missing/empty partitions from the persisted config when allowed."""
    for partition in RUNTIME_PARTITIONS:
        value = incoming.get(partition)
        inherited = current.get(partition) if isinstance(current, dict) else None
        if partition == "schedules" and not preserve_schedules:
            # The scheduler's save path replaces schedules verbatim, including
            # an empty list used to delete the last remaining schedule.
            incoming[partition] = value if isinstance(value, list) else []
            continue
        if isinstance(value, list) and value:
            continue
        if isinstance(inherited, list) and inherited:
            incoming[partition] = copy.deepcopy(inherited)
        elif not isinstance(value, list):
            incoming[partition] = copy.deepcopy(inherited) if isinstance(inherited, list) else []


def _validate_schedule_references(config: dict) -> None:
    prompts = config.get("prompts")
    prompt_ids = {
        str(item.get("id") or "").strip()
        for item in (prompts.get("items") if isinstance(prompts, dict) else []) or []
        if isinstance(item, dict)
    } - {""}
    smart_sheet = config.get("smart_sheet")
    template_ids = {
        str(item.get("id") or "").strip()
        for item in (
            smart_sheet.get("templates") if isinstance(smart_sheet, dict) else []
        )
        or []
        if isinstance(item, dict)
    } - {""}
    for schedule in config.get("schedules") or []:
        if not isinstance(schedule, dict):
            continue
        name = str(schedule.get("name") or "未命名定时任务")
        prompt_id = str(schedule.get("promptId") or "").strip()
        if prompt_id and prompt_id not in prompt_ids:
            raise ValueError(f"提示词仍被定时任务“{name}”使用，请先修改该任务")
        template_id = str(schedule.get("smartSheetTemplateId") or "").strip()
        if template_id and template_id not in template_ids:
            raise ValueError(f"腾讯文档模板仍被定时任务“{name}”使用，请先修改该任务")


def _persist_config(config: dict, config_path: Path) -> dict:
    """Normalize and atomically write ``config``, then return the saved result."""
    probe = _normalized_probe(config)
    validate_unique_config_ids(probe)
    _validate_schedule_references(probe)
    try:
        saved_path = _write_config(config, config_path)
        saved_config, loaded_path = load_config(saved_path)
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(f"保存配置失败：{exc}") from exc
    return {"config": saved_config, "configPath": str(loaded_path)}


def bootstrap_config(config_path: str | os.PathLike | None = None) -> dict:
    """Load and normalize the active config without writing it."""
    path = resolve_config_path(config_path)
    try:
        config, loaded_path = load_config(path)
    except ValueError as exc:
        raise ValueError(f"读取配置失败，配置文件可能已损坏（{path}）：{exc}") from exc
    except OSError as exc:
        raise ValueError(f"读取配置失败（{path}）：{exc}") from exc
    return {"config": config, "configPath": str(loaded_path)}


def save_config_command(
    config: object,
    config_path: str | os.PathLike | None = None,
    *,
    preserve_schedules: bool = True,
) -> dict:
    """Persist a config edited by the settings UI.

    ``schedules``/``mcp_servers``/``reply_listeners`` missing (or empty) in
    ``config`` are inherited from the active config file so a stale settings
    snapshot cannot wipe runtime data. ``preserve_schedules=False`` is the
    scheduler path: the passed ``schedules`` array replaces the stored one
    verbatim, including an empty array.
    """
    if not isinstance(config, dict):
        raise ValueError("配置内容无效：config 必须是一个 JSON 对象")
    if not isinstance(preserve_schedules, bool):
        raise ValueError("preserveSchedules 必须是布尔值")
    path = resolve_config_path(config_path)
    incoming = copy.deepcopy(config)
    current = _load_current_config(path) if path.exists() else None
    _prepare_runtime_partitions(
        incoming, current, preserve_schedules=preserve_schedules
    )
    return _persist_config(incoming, path)


def _backup_envelope(config: dict) -> dict:
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "format": CONFIG_BACKUP_FORMAT,
        "_backup_version": CONFIG_BACKUP_VERSION,
        "created_at": created_at,
        "config": config,
    }


def _extract_backup_config(backup: object) -> dict:
    if not isinstance(backup, dict):
        raise ValueError("配置备份必须是 JSON 对象，请选择本应用导出的备份文件")
    backup_format = backup.get("format")
    if backup_format is not None and str(backup_format) != CONFIG_BACKUP_FORMAT:
        raise ValueError("这不是企微问题雷达导出的配置备份")
    version = backup.get("_backup_version")
    if version is None:
        version = backup.get("backupVersion")
    if version is not None:
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError("配置备份的版本信息无效，请重新导出后再导入")
        if version not in SUPPORTED_BACKUP_VERSIONS:
            raise ValueError(f"暂不支持版本为 {version} 的配置备份，请升级应用后再导入")
    config_value = backup.get("config")
    if not isinstance(config_value, dict):
        raise ValueError("配置备份缺少有效的 config 对象，无法导入")
    return copy.deepcopy(config_value)


def export_config_backup(
    backup_path: str | os.PathLike | None,
    config_path: str | os.PathLike | None = None,
) -> dict:
    """Write a sanitized, portable backup of the active config."""
    path = _resolve_backup_path(backup_path)
    active_path = resolve_config_path(config_path)
    if path == active_path:
        raise ValueError("备份文件不能覆盖当前正在使用的配置文件")
    if path.is_dir():
        raise ValueError("备份文件路径指向了一个文件夹，请选择一个文件")
    current = _load_current_config(active_path)
    envelope = _backup_envelope(sanitize_backup_config(current))
    content = json.dumps(envelope, ensure_ascii=False, indent=2) + "\n"
    temp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temp_path.open("w", encoding="utf-8", newline="\n") as file:
            file.write(content)
        temp_path.replace(path)
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError(f"写入配置备份失败：{exc}") from exc
    return {"path": str(path)}


def import_config_backup(
    backup_path: str | os.PathLike | None,
    config_path: str | os.PathLike | None = None,
    *,
    max_backup_bytes: int = MAX_CONFIG_BACKUP_BYTES,
) -> dict:
    """Import a backup, restoring credentials from the active config."""
    path = _resolve_backup_path(backup_path)
    active_path = resolve_config_path(config_path)
    if path.is_dir():
        raise ValueError("选择的配置备份不是文件，请重新选择")
    if not path.is_file():
        raise ValueError("找不到配置备份文件，请确认文件是否已被移动或删除")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"读取配置备份失败：{exc}") from exc
    if size > int(max_backup_bytes):
        raise ValueError("配置备份文件过大（超过 16 MB），已拒绝导入")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"读取配置备份失败：{exc}") from exc
    try:
        backup = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"配置备份不是有效的 JSON 文件（第 {exc.lineno} 行附近），"
            "请确认选择的是本应用导出的备份"
        ) from exc

    current = _load_current_config(active_path)
    imported = sanitize_backup_config(_extract_backup_config(backup))
    imported = restore_sensitive_values(imported, current)
    return _persist_config(imported, active_path)
