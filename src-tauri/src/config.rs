use chrono::{SecondsFormat, Utc};
use serde::Serialize;
use serde_json::{json, Value};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

const EXAMPLE_CONFIG: &str = include_str!("../../config.example.json");
const CURRENT_CONFIG_VERSION: u64 = 3;
const LEGACY_CONFIG_MIGRATION_VERSION: u64 = 2;
const CONFIG_BACKUP_FORMAT: &str = "wecom-issue-radar-config-backup";
const CONFIG_BACKUP_VERSION: u64 = 1;
const MAX_CONFIG_BACKUP_BYTES: u64 = 16 * 1024 * 1024;
const MACHINE_LOCAL_CONFIG_KEYS: [&str; 3] =
    ["wxwork_db_dir", "wxwork_keys_file", "default_workspace"];
const WECOM_CREDENTIAL_KEYS: [&str; 8] = [
    "corpid",
    "corp_id",
    "corpsecret",
    "corp_secret",
    "wecom_token",
    "wecom_access_token",
    "wxwork_token",
    "wxwork_access_token",
];

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BootstrapPayload {
    pub config: Value,
    pub config_path: String,
    pub app_version: String,
}

pub fn bootstrap_payload() -> Result<BootstrapPayload, String> {
    let path = config_path()?;
    migrate_legacy_config(&path)?;
    let config = load_config(&path)?;
    Ok(BootstrapPayload {
        config,
        config_path: path.to_string_lossy().into_owned(),
        app_version: env!("CARGO_PKG_VERSION").to_string(),
    })
}

pub fn config_path() -> Result<PathBuf, String> {
    let explicit = std::env::var("WECOM_ISSUE_RADAR_CONFIG")
        .or_else(|_| std::env::var("WECOM_DAILY_PIPELINE_CONFIG"));
    if let Some(explicit) = explicit.ok().filter(|value| !value.trim().is_empty()) {
        let path = PathBuf::from(explicit);
        return if path.is_absolute() {
            Ok(path)
        } else {
            std::env::current_dir()
                .map(|directory| directory.join(path))
                .map_err(|error| format!("无法解析配置文件路径：{error}"))
        };
    }
    default_config_path()
}

fn default_config_path() -> Result<PathBuf, String> {
    let home = dirs::home_dir().ok_or_else(|| "无法确定当前 Windows 用户目录".to_string())?;
    Ok(home.join(".wecom-issue-radar").join("config.local.json"))
}

fn migrate_legacy_config(path: &Path) -> Result<(), String> {
    if path.exists() || path != default_config_path()? {
        return Ok(());
    }
    let home = dirs::home_dir().ok_or_else(|| "无法确定当前 Windows 用户目录".to_string())?;
    let legacy_dir = home.join(".wecom-daily-issue-pipeline");
    let legacy_config = legacy_dir.join("config.local.json");
    if !legacy_config.is_file() {
        return Ok(());
    }
    let target_dir = path
        .parent()
        .ok_or_else(|| "新配置文件路径无效".to_string())?;
    fs::create_dir_all(target_dir).map_err(|error| format!("创建新配置目录失败：{error}"))?;
    let legacy_keys = legacy_dir.join("wxwork_keys.json");
    if legacy_keys.is_file() {
        fs::copy(legacy_keys, target_dir.join("wxwork_keys.json"))
            .map_err(|error| format!("迁移企业微信密钥失败：{error}"))?;
    }
    fs::copy(&legacy_config, path).map_err(|error| format!("迁移旧版配置失败：{error}"))?;
    Ok(())
}

pub fn load_config(path: &Path) -> Result<Value, String> {
    let mut config: Value = serde_json::from_str(EXAMPLE_CONFIG)
        .map_err(|error| format!("内置默认配置无效：{error}"))?;
    let mut migrate_legacy = true;
    if path.exists() {
        let text = fs::read_to_string(path)
            .map_err(|error| format!("读取配置失败（{}）：{error}", path.display()))?;
        let local: Value = serde_json::from_str(&text)
            .map_err(|error| format!("配置文件不是有效 JSON：{error}"))?;
        migrate_legacy = requires_legacy_migration(&local);
        deep_merge(&mut config, local);
    }
    normalize_config(&mut config, migrate_legacy);
    validate_unique_config_ids(&config)?;
    Ok(config)
}

pub fn save_config(mut config: Value) -> Result<BootstrapPayload, String> {
    let migrate_legacy = requires_legacy_migration(&config);
    normalize_config(&mut config, migrate_legacy);
    validate_unique_config_ids(&config)?;
    let path = config_path()?;
    let parent = path
        .parent()
        .ok_or_else(|| "配置文件路径无效".to_string())?;
    fs::create_dir_all(parent).map_err(|error| format!("创建配置目录失败：{error}"))?;

    let mut file = tempfile::NamedTempFile::new_in(parent)
        .map_err(|error| format!("创建临时配置失败：{error}"))?;
    let content = serde_json::to_string_pretty(&config)
        .map_err(|error| format!("序列化配置失败：{error}"))?;
    file.write_all(content.as_bytes())
        .map_err(|error| format!("写入配置失败：{error}"))?;
    file.write_all(b"\n")
        .map_err(|error| format!("写入配置失败：{error}"))?;
    file.as_file()
        .sync_all()
        .map_err(|error| format!("保存配置失败：{error}"))?;
    file.persist(&path)
        .map_err(|error| format!("替换配置文件失败：{}", error.error))?;

    Ok(BootstrapPayload {
        config,
        config_path: path.to_string_lossy().into_owned(),
        app_version: env!("CARGO_PKG_VERSION").to_string(),
    })
}

/// Saves settings edited by the general settings and prompts pages without allowing a stale
/// frontend snapshot to replace schedules or runtime compatibility partitions that are managed
/// independently.
pub fn save_config_preserving_schedules(mut incoming: Value) -> Result<BootstrapPayload, String> {
    let path = config_path()?;
    if path.exists() {
        let persisted = load_config(&path)?;
        preserve_persisted_schedules(&mut incoming, &persisted)?;
        preserve_persisted_runtime_partitions(&mut incoming, &persisted);
    }
    save_config(incoming)
}

/// Writes a portable business-configuration backup. Machine-local paths, credentials, API keys,
/// MCP headers/environment values, and webhook URLs are deliberately omitted, while non-secret
/// model, prompt, Tencent document, and schedule settings remain portable.
pub fn export_config_backup(path: &Path) -> Result<(), String> {
    let config_path = config_path()?;
    if path == config_path {
        return Err("备份文件不能覆盖当前正在使用的配置文件".to_string());
    }
    let parent = path
        .parent()
        .ok_or_else(|| "备份文件路径无效".to_string())?;
    fs::create_dir_all(parent).map_err(|error| format!("创建备份目录失败：{error}"))?;

    let mut portable = load_config(&config_path)?;
    sanitize_backup_config(&mut portable);
    let backup = json!({
        "format": CONFIG_BACKUP_FORMAT,
        "backupVersion": CONFIG_BACKUP_VERSION,
        "createdAt": Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true),
        "appVersion": env!("CARGO_PKG_VERSION"),
        "config": portable,
    });
    let content = serde_json::to_string_pretty(&backup)
        .map_err(|error| format!("序列化配置备份失败：{error}"))?;
    let mut file = tempfile::NamedTempFile::new_in(parent)
        .map_err(|error| format!("创建临时备份文件失败：{error}"))?;
    file.write_all(content.as_bytes())
        .map_err(|error| format!("写入配置备份失败：{error}"))?;
    file.write_all(b"\n")
        .map_err(|error| format!("写入配置备份失败：{error}"))?;
    file.as_file()
        .sync_all()
        .map_err(|error| format!("保存配置备份失败：{error}"))?;
    file.persist(path)
        .map_err(|error| format!("替换配置备份失败：{}", error.error))?;
    Ok(())
}

/// Reads a backup and restores protected machine-local values from the active config.
/// The returned value is not persisted until scheduler/task-reference validation succeeds.
pub fn read_config_backup(path: &Path) -> Result<Value, String> {
    let metadata = fs::metadata(path)
        .map_err(|error| format!("读取配置备份失败（{}）：{error}", path.display()))?;
    if !metadata.is_file() {
        return Err("选择的配置备份不是文件".to_string());
    }
    if metadata.len() > MAX_CONFIG_BACKUP_BYTES {
        return Err("配置备份超过 16 MB，已拒绝导入".to_string());
    }
    let text = fs::read_to_string(path)
        .map_err(|error| format!("读取配置备份失败（{}）：{error}", path.display()))?;
    let backup: Value =
        serde_json::from_str(&text).map_err(|error| format!("配置备份不是有效 JSON：{error}"))?;
    if backup.get("format").and_then(Value::as_str) != Some(CONFIG_BACKUP_FORMAT) {
        return Err("这不是企微问题雷达导出的配置备份".to_string());
    }
    let version = backup
        .get("backupVersion")
        .and_then(Value::as_u64)
        .ok_or_else(|| "配置备份缺少版本信息".to_string())?;
    if version != CONFIG_BACKUP_VERSION {
        return Err(format!("暂不支持版本为 {version} 的配置备份"));
    }
    let mut imported = backup
        .get("config")
        .filter(|value| value.is_object())
        .cloned()
        .ok_or_else(|| "配置备份缺少有效的 config 对象".to_string())?;
    // Sanitize again so a hand-edited file cannot inject protected local values.
    sanitize_backup_config(&mut imported);
    let current_path = config_path()?;
    let current = load_config(&current_path)?;
    restore_protected_config(&mut imported, &current)?;
    Ok(imported)
}

fn sanitize_backup_config(config: &mut Value) {
    let Some(root) = config.as_object_mut() else {
        return;
    };
    for key in MACHINE_LOCAL_CONFIG_KEYS {
        root.remove(key);
    }
    for key in WECOM_CREDENTIAL_KEYS {
        root.remove(key);
    }
    for section in ["llm", "ocr"] {
        if let Some(settings) = root.get_mut(section).and_then(Value::as_object_mut) {
            settings.remove("api_key");
            settings.remove("apiKey");
        }
    }
    if let Some(smart_sheet) = root.get_mut("smart_sheet").and_then(Value::as_object_mut) {
        for key in WECOM_CREDENTIAL_KEYS {
            smart_sheet.remove(key);
        }
        if let Some(upload) = smart_sheet.get_mut("upload").and_then(Value::as_object_mut) {
            for key in WECOM_CREDENTIAL_KEYS {
                upload.remove(key);
            }
        }
    }
    if let Some(servers) = root.get_mut("mcp_servers") {
        remove_mcp_secrets(servers);
    }
    if let Some(servers) = root
        .get_mut("mcp")
        .and_then(Value::as_object_mut)
        .and_then(|mcp| mcp.get_mut("servers"))
    {
        remove_mcp_secrets(servers);
    }
    remove_webhook_secrets(config);
}

fn restore_protected_config(imported: &mut Value, current: &Value) -> Result<(), String> {
    let imported_root = imported
        .as_object_mut()
        .ok_or_else(|| "配置备份的 config 顶层必须是对象".to_string())?;
    let current_root = current
        .as_object()
        .ok_or_else(|| "当前配置顶层必须是对象".to_string())?;
    for key in MACHINE_LOCAL_CONFIG_KEYS {
        if let Some(value) = current_root.get(key) {
            imported_root.insert(key.to_string(), value.clone());
        }
    }
    for key in WECOM_CREDENTIAL_KEYS {
        if let Some(value) = current_root.get(key) {
            imported_root.insert(key.to_string(), value.clone());
        }
    }
    restore_section_secret(imported_root, current_root, "llm", "api_key");
    restore_section_secret(imported_root, current_root, "ocr", "api_key");

    restore_matched_partition_secrets(
        imported_root.get_mut("mcp_servers"),
        current_root.get("mcp_servers"),
        RuntimeSecretKind::Mcp,
    );
    restore_matched_partition_secrets(
        imported_root.get_mut("reply_listeners"),
        current_root.get("reply_listeners"),
        RuntimeSecretKind::Webhook,
    );
    if let (Some(imported_servers), Some(current_servers)) = (
        imported_root
            .get_mut("mcp")
            .and_then(Value::as_object_mut)
            .and_then(|mcp| mcp.get_mut("servers")),
        current_root
            .get("mcp")
            .and_then(Value::as_object)
            .and_then(|mcp| mcp.get("servers")),
    ) {
        restore_matched_partition_secrets(
            Some(imported_servers),
            Some(current_servers),
            RuntimeSecretKind::Mcp,
        );
    }

    let imported_smart_sheet = imported_root
        .entry("smart_sheet".to_string())
        .or_insert_with(|| json!({}));
    if !imported_smart_sheet.is_object() {
        *imported_smart_sheet = json!({});
    }
    let imported_smart_sheet = imported_smart_sheet
        .as_object_mut()
        .expect("smart_sheet object was normalized");
    if let Some(current_smart_sheet) = current.get("smart_sheet").and_then(Value::as_object) {
        for key in WECOM_CREDENTIAL_KEYS {
            if let Some(value) = current_smart_sheet.get(key) {
                imported_smart_sheet.insert(key.to_string(), value.clone());
            }
        }
        restore_secret_fields_in_object(
            imported_smart_sheet,
            current_smart_sheet,
            RuntimeSecretKind::Webhook,
        );
        restore_matched_partition_secrets(
            imported_smart_sheet.get_mut("templates"),
            current_smart_sheet.get("templates"),
            RuntimeSecretKind::Webhook,
        );
    }
    let imported_upload = imported_smart_sheet
        .entry("upload".to_string())
        .or_insert_with(|| json!({}));
    if !imported_upload.is_object() {
        *imported_upload = json!({});
    }
    if let Some(current_upload) = current
        .get("smart_sheet")
        .and_then(|smart_sheet| smart_sheet.get("upload"))
        .and_then(Value::as_object)
    {
        let imported_upload = imported_upload
            .as_object_mut()
            .expect("upload object was normalized");
        for key in WECOM_CREDENTIAL_KEYS {
            if let Some(value) = current_upload.get(key) {
                imported_upload.insert(key.to_string(), value.clone());
            }
        }
    }
    Ok(())
}

#[derive(Clone, Copy)]
enum RuntimeSecretKind {
    Mcp,
    Webhook,
}

fn remove_mcp_secrets(value: &mut Value) {
    match value {
        Value::Object(object) => {
            object.retain(|key, _| !is_mcp_secret_key(key));
            for child in object.values_mut() {
                remove_mcp_secrets(child);
            }
        }
        Value::Array(items) => {
            for item in items {
                remove_mcp_secrets(item);
            }
        }
        _ => {}
    }
}

fn remove_webhook_secrets(value: &mut Value) {
    match value {
        Value::Object(object) => {
            object.retain(|key, _| !is_webhook_secret_key(key));
            for child in object.values_mut() {
                remove_webhook_secrets(child);
            }
        }
        Value::Array(items) => {
            for item in items {
                remove_webhook_secrets(item);
            }
        }
        _ => {}
    }
}

fn normalized_secret_key(key: &str) -> String {
    key.chars()
        .filter(|character| character.is_ascii_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect()
}

fn is_webhook_secret_key(key: &str) -> bool {
    normalized_secret_key(key).contains("webhook")
}

fn is_mcp_secret_key(key: &str) -> bool {
    matches!(
        normalized_secret_key(key).as_str(),
        "headers" | "header" | "env" | "environment"
    ) || is_webhook_secret_key(key)
}

fn restore_section_secret(
    imported_root: &mut serde_json::Map<String, Value>,
    current_root: &serde_json::Map<String, Value>,
    section: &str,
    secret_key: &str,
) {
    let Some(secret) = current_root
        .get(section)
        .and_then(Value::as_object)
        .and_then(|settings| settings.get(secret_key))
        .cloned()
    else {
        return;
    };
    let imported_section = imported_root
        .entry(section.to_string())
        .or_insert_with(|| json!({}));
    if !imported_section.is_object() {
        *imported_section = json!({});
    }
    imported_section
        .as_object_mut()
        .expect("section was normalized")
        .insert(secret_key.to_string(), secret);
}

fn restore_matched_partition_secrets(
    imported: Option<&mut Value>,
    current: Option<&Value>,
    kind: RuntimeSecretKind,
) {
    let (Some(imported), Some(current)) = (imported, current) else {
        return;
    };
    let (Some(imported_items), Some(current_items)) = (imported.as_array_mut(), current.as_array())
    else {
        return;
    };
    for imported_item in imported_items {
        let Some(id) = imported_item
            .get("id")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|id| !id.is_empty())
        else {
            continue;
        };
        let Some(current_item) = current_items
            .iter()
            .find(|item| item.get("id").and_then(Value::as_str).map(str::trim) == Some(id))
        else {
            continue;
        };
        restore_secret_fields(imported_item, current_item, kind);
    }
}

fn restore_secret_fields(imported: &mut Value, current: &Value, kind: RuntimeSecretKind) {
    let (Some(imported_object), Some(current_object)) =
        (imported.as_object_mut(), current.as_object())
    else {
        return;
    };
    restore_secret_fields_in_object(imported_object, current_object, kind);
}

fn restore_secret_fields_in_object(
    imported: &mut serde_json::Map<String, Value>,
    current: &serde_json::Map<String, Value>,
    kind: RuntimeSecretKind,
) {
    for (key, current_value) in current {
        let is_secret = match kind {
            RuntimeSecretKind::Mcp => is_mcp_secret_key(key),
            RuntimeSecretKind::Webhook => is_webhook_secret_key(key),
        };
        if is_secret {
            imported.insert(key.clone(), current_value.clone());
            continue;
        }
        if let Some(imported_value) = imported.get_mut(key) {
            restore_secret_fields(imported_value, current_value, kind);
        }
    }
}

fn preserve_persisted_schedules(incoming: &mut Value, persisted: &Value) -> Result<(), String> {
    if let (Some(incoming_root), Some(schedules)) =
        (incoming.as_object_mut(), persisted.get("schedules"))
    {
        validate_schedule_references(incoming_root, schedules)?;
        incoming_root.insert("schedules".to_string(), schedules.clone());
    }
    Ok(())
}

fn preserve_persisted_runtime_partitions(incoming: &mut Value, persisted: &Value) {
    let Some(incoming_root) = incoming.as_object_mut() else {
        return;
    };
    for key in ["mcp_servers", "reply_listeners"] {
        if let Some(partition) = persisted.get(key) {
            incoming_root.insert(key.to_string(), partition.clone());
        }
    }
}

fn validate_schedule_references(
    incoming_root: &serde_json::Map<String, Value>,
    schedules: &Value,
) -> Result<(), String> {
    let prompt_ids: std::collections::HashSet<&str> = incoming_root
        .get("prompts")
        .and_then(|prompts| prompts.get("items"))
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|prompt| prompt.get("id").and_then(Value::as_str).map(str::trim))
        .collect();
    let template_ids: std::collections::HashSet<&str> = incoming_root
        .get("smart_sheet")
        .and_then(|smart| smart.get("templates"))
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|template| template.get("id").and_then(Value::as_str).map(str::trim))
        .collect();
    for schedule in schedules.as_array().into_iter().flatten() {
        let name = schedule
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("未命名定时任务");
        if let Some(prompt_id) = schedule.get("promptId").and_then(Value::as_str) {
            let prompt_id = prompt_id.trim();
            if !prompt_id.is_empty() && !prompt_ids.contains(prompt_id) {
                return Err(format!("提示词仍被定时任务“{name}”使用，请先修改该任务"));
            }
        }
        if let Some(template_id) = schedule.get("smartSheetTemplateId").and_then(Value::as_str) {
            let template_id = template_id.trim();
            if !template_id.is_empty() && !template_ids.contains(template_id) {
                return Err(format!(
                    "腾讯文档模板仍被定时任务“{name}”使用，请先修改该任务"
                ));
            }
        }
    }
    Ok(())
}

fn deep_merge(base: &mut Value, override_value: Value) {
    match (base, override_value) {
        (Value::Object(base_map), Value::Object(override_map)) => {
            for (key, value) in override_map {
                match base_map.get_mut(&key) {
                    Some(existing) => deep_merge(existing, value),
                    None => {
                        base_map.insert(key, value);
                    }
                }
            }
        }
        (base_slot, value) => *base_slot = value,
    }
}

fn requires_legacy_migration(config: &Value) -> bool {
    config
        .get("config_version")
        .and_then(Value::as_u64)
        .is_none_or(|version| version < LEGACY_CONFIG_MIGRATION_VERSION)
}

fn validate_unique_config_ids(config: &Value) -> Result<(), String> {
    validate_unique_ids(
        config
            .get("prompts")
            .and_then(|prompts| prompts.get("items")),
        "提示词",
    )?;
    validate_unique_ids(
        config
            .get("smart_sheet")
            .and_then(|smart| smart.get("templates")),
        "腾讯文档模板",
    )
}

fn validate_unique_ids(items: Option<&Value>, label: &str) -> Result<(), String> {
    let mut ids = std::collections::HashSet::new();
    for id in items
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|item| item.get("id").and_then(Value::as_str).map(str::trim))
    {
        if !ids.insert(id) {
            return Err(format!(
                "配置中存在重复的{label} ID“{id}”，请为每项使用唯一 ID"
            ));
        }
    }
    Ok(())
}

fn ensure_prompts(config: &mut Value, migrate_legacy: bool) {
    let fallback: Value = serde_json::from_str(EXAMPLE_CONFIG).unwrap_or(Value::Null);
    let fallback_prompts = fallback.get("prompts").cloned().unwrap_or(Value::Null);
    let configured_issue_fields =
        configured_default_issue_fields(config, &fallback_prompts, migrate_legacy);
    let Some(root) = config.as_object_mut() else {
        *config = fallback;
        return;
    };
    let mut prompts = root
        .get("prompts")
        .cloned()
        .unwrap_or_else(|| fallback_prompts.clone());
    let valid_items = prompts
        .get("items")
        .and_then(Value::as_array)
        .is_some_and(|items| !items.is_empty());
    if !valid_items {
        if let Some(items) = fallback_prompts.get("items") {
            prompts["items"] = items.clone();
        }
    }
    let Some(prompt_object) = prompts.as_object_mut() else {
        root.insert("prompts".to_string(), fallback_prompts);
        return;
    };
    let default_issue_fields = configured_issue_fields;
    prompt_object.insert(
        "default_issue_fields".to_string(),
        default_issue_fields.clone(),
    );
    if let Some(items) = prompt_object.get_mut("items").and_then(Value::as_array_mut) {
        for (index, item) in items.iter_mut().enumerate() {
            let Some(item_object) = item.as_object_mut() else {
                continue;
            };
            let prompt_id = item_object
                .get("id")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|id| !id.is_empty())
                .map(str::to_owned)
                .unwrap_or_else(|| format!("prompt_{}", index + 1));
            item_object.insert("id".to_string(), json!(prompt_id));
            let valid_fields = item_object
                .get("issue_fields")
                .and_then(Value::as_array)
                .is_some_and(|fields| !fields.is_empty());
            if !valid_fields {
                item_object.insert("issue_fields".to_string(), default_issue_fields.clone());
            }
            let template_id = item_object
                .get("default_smart_sheet_template_id")
                .and_then(Value::as_str)
                .map(str::trim)
                .unwrap_or("")
                .to_string();
            item_object.insert(
                "default_smart_sheet_template_id".to_string(),
                json!(template_id),
            );
        }
    }
    let ids: Vec<String> = prompt_object
        .get("items")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|item| item.get("id").and_then(Value::as_str).map(str::to_owned))
        .collect();
    let requested_default = prompt_object
        .get("default_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("")
        .to_string();
    if ids.iter().any(|candidate| candidate == &requested_default) {
        prompt_object.insert("default_id".to_string(), json!(requested_default));
    } else {
        if let Some(first) = ids.first() {
            prompt_object.insert("default_id".to_string(), Value::String(first.clone()));
        }
    }
    root.insert("prompts".to_string(), prompts);
}

fn configured_default_issue_fields(
    config: &Value,
    fallback_prompts: &Value,
    migrate_legacy: bool,
) -> Value {
    if !migrate_legacy {
        if let Some(fields) = config
            .get("prompts")
            .and_then(|prompts| prompts.get("default_issue_fields"))
            .filter(|fields| {
                fields
                    .as_array()
                    .is_some_and(|configured| !configured.is_empty())
            })
        {
            return fields.clone();
        }
    }
    let mut fields = fallback_prompts
        .get("default_issue_fields")
        .cloned()
        .unwrap_or_else(|| Value::Array(Vec::new()));
    let Some(smart) = config.get("smart_sheet") else {
        return fields;
    };
    let default_id = smart
        .get("default_template_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    let Some(template) = smart
        .get("templates")
        .and_then(Value::as_array)
        .and_then(|templates| {
            templates
                .iter()
                .find(|template| template.get("id").and_then(Value::as_str) == Some(default_id))
                .or_else(|| templates.first())
        })
    else {
        return fields;
    };
    let Some(schema) = template.get("schema") else {
        return fields;
    };
    let mappings = template
        .get("field_mappings")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let Some(field_items) = fields.as_array_mut() else {
        return fields;
    };
    for (key, target) in [("module_text", "f04Gwj"), ("issue_category_text", "fsFBqK")] {
        let Some(field) = field_items
            .iter_mut()
            .find(|field| field.get("key").and_then(Value::as_str) == Some(key))
            .and_then(Value::as_object_mut)
        else {
            continue;
        };
        if let Some(options) = schema.get(target).and_then(|value| value.get("enum")) {
            if options.as_array().is_some_and(|items| !items.is_empty()) {
                field.insert("options".to_string(), options.clone());
            }
        }
        if let Some(default_value) = mappings.iter().find_map(|mapping| {
            (mapping.get("source_key").and_then(Value::as_str) == Some(key))
                .then(|| mapping.get("default_value").cloned())
                .flatten()
        }) {
            field.insert("default_value".to_string(), default_value);
        }
    }
    fields
}

fn ensure_smart_sheet(config: &mut Value, migrate_legacy: bool) {
    let fallback: Value = serde_json::from_str(EXAMPLE_CONFIG).unwrap_or(Value::Null);
    let fallback_smart = fallback
        .get("smart_sheet")
        .cloned()
        .unwrap_or_else(|| Value::Object(Default::default()));
    let Some(root) = config.as_object_mut() else {
        return;
    };
    let mut smart = root
        .get("smart_sheet")
        .cloned()
        .unwrap_or_else(|| fallback_smart.clone());
    normalize_object_shape(&mut smart, &fallback_smart);
    let Some(smart_object) = smart.as_object_mut() else {
        root.insert("smart_sheet".to_string(), fallback_smart);
        return;
    };

    let legacy_url = smart_object.get("url").cloned().unwrap_or(Value::Null);
    let legacy_webhook = smart_object
        .get("webhook_url")
        .cloned()
        .unwrap_or(Value::Null);
    let legacy_webhook_env = smart_object
        .get("webhook_url_env")
        .cloned()
        .unwrap_or(Value::Null);
    let legacy_batch_size = smart_object
        .get("batch_size")
        .cloned()
        .unwrap_or_else(|| json!(50));
    let legacy_schema = smart_object
        .get("schema")
        .cloned()
        .unwrap_or_else(|| Value::Object(Default::default()));
    let legacy_defaults = smart_object
        .get("defaults")
        .cloned()
        .unwrap_or_else(|| Value::Object(Default::default()));

    let fallback_template = fallback_smart
        .get("templates")
        .and_then(Value::as_array)
        .and_then(|items| items.first())
        .cloned()
        .unwrap_or_else(|| {
            json!({
                "id": "default",
                "name": "默认问题清单",
                "url": "",
                "webhook_url_env": "WECOM_SMARTSHEET_WEBHOOK_URL",
                "webhook_url": "",
                "batch_size": 50,
                "schema": {},
                "field_mappings": []
            })
        });
    let valid_templates = smart_object
        .get("templates")
        .and_then(Value::as_array)
        .is_some_and(|items| !items.is_empty());
    if !valid_templates {
        smart_object.insert(
            "templates".to_string(),
            Value::Array(vec![fallback_template.clone()]),
        );
    }
    let requested_default = smart_object
        .get("default_template_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("default")
        .to_string();
    let templates = smart_object
        .get_mut("templates")
        .and_then(Value::as_array_mut)
        .expect("templates were normalized");
    for (index, template) in templates.iter_mut().enumerate() {
        if !template.is_object() {
            *template = Value::Object(Default::default());
        }
        let Some(template_object) = template.as_object_mut() else {
            continue;
        };
        let template_id = template_object
            .get("id")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|id| !id.is_empty())
            .map(str::to_owned)
            .unwrap_or_else(|| format!("template_{}", index + 1));
        template_object.insert("id".to_string(), json!(template_id));
        for key in [
            "name",
            "url",
            "webhook_url_env",
            "webhook_url",
            "batch_size",
            "schema",
            "field_mappings",
        ] {
            if !template_object.contains_key(key) {
                template_object.insert(
                    key.to_string(),
                    fallback_template.get(key).cloned().unwrap_or(Value::Null),
                );
            }
        }
    }
    let default_index = templates
        .iter()
        .position(|item| item.get("id").and_then(Value::as_str) == Some(&requested_default))
        .unwrap_or(0);
    let default_template = templates[default_index]
        .as_object_mut()
        .expect("template object was normalized");
    if migrate_legacy {
        copy_if_blank(default_template, "url", legacy_url);
        copy_if_blank(default_template, "webhook_url", legacy_webhook);
        copy_if_blank_or_fallback(
            default_template,
            "webhook_url_env",
            legacy_webhook_env,
            &fallback_template,
        );
        if default_template
            .get("schema")
            .and_then(Value::as_object)
            .is_none_or(|schema| schema.is_empty())
        {
            default_template.insert("schema".to_string(), legacy_schema.clone());
        }
        if default_template
            .get("field_mappings")
            .and_then(Value::as_array)
            .is_none_or(|mappings| mappings.is_empty())
        {
            default_template.insert(
                "field_mappings".to_string(),
                legacy_field_mappings(&legacy_schema, &legacy_defaults),
            );
        }
        if default_template.get("batch_size") == Some(&json!(50)) {
            default_template.insert("batch_size".to_string(), legacy_batch_size);
        }
    }
    let default_id = default_template
        .get("id")
        .and_then(Value::as_str)
        .unwrap_or("default")
        .to_string();
    smart_object.insert("default_template_id".to_string(), json!(default_id));
    for key in [
        "url",
        "webhook_url_env",
        "webhook_url",
        "batch_size",
        "schema",
        "defaults",
    ] {
        smart_object.remove(key);
    }
    root.insert("smart_sheet".to_string(), smart);
}

fn copy_if_blank(target: &mut serde_json::Map<String, Value>, key: &str, value: Value) {
    let blank = target
        .get(key)
        .and_then(Value::as_str)
        .is_none_or(str::is_empty);
    if blank && !value.is_null() {
        target.insert(key.to_string(), value);
    }
}

fn copy_if_blank_or_fallback(
    target: &mut serde_json::Map<String, Value>,
    key: &str,
    value: Value,
    fallback: &Value,
) {
    let current = target.get(key);
    let should_copy = current.is_none()
        || current.and_then(Value::as_str).is_some_and(str::is_empty)
        || current == fallback.get(key);
    if should_copy && !value.is_null() {
        target.insert(key.to_string(), value);
    }
}

fn legacy_field_mappings(schema: &Value, defaults: &Value) -> Value {
    let candidates = [
        (
            "module_text",
            "f04Gwj",
            defaults
                .get("module_text")
                .cloned()
                .unwrap_or_else(|| json!("订单/售后")),
        ),
        ("problem_description", "ftk5Tx", json!("")),
        ("reason", "fMAfWQ", json!("")),
        ("$images", "fn8TJd", json!([])),
        (
            "review_text",
            "fb19Ra",
            defaults
                .get("review_text")
                .cloned()
                .unwrap_or_else(|| json!("")),
        ),
        (
            "status_text",
            "fIgBdy",
            defaults
                .get("status_text")
                .cloned()
                .unwrap_or_else(|| json!("待评估")),
        ),
        (
            "issue_category_text",
            "fsFBqK",
            defaults
                .get("issue_category_text")
                .cloned()
                .unwrap_or_else(|| json!("待评估")),
        ),
        (
            "typical_case_texts",
            "fOXTRh",
            defaults
                .get("typical_case_texts")
                .cloned()
                .unwrap_or_else(|| json!([])),
        ),
        ("$date", "ftQMc5", json!("")),
        (
            "online_issue_text",
            "fgIJEu",
            defaults
                .get("online_issue_text")
                .cloned()
                .unwrap_or_else(|| json!("")),
        ),
        (
            "jira_url",
            "fhK1MH",
            defaults
                .get("jira_url")
                .cloned()
                .unwrap_or_else(|| json!("")),
        ),
        (
            "issue_summary_text",
            "fs9xhZ",
            defaults
                .get("issue_summary_text")
                .cloned()
                .unwrap_or_else(|| json!("")),
        ),
        (
            "start_time_text",
            "fV6BDR",
            defaults
                .get("start_time_text")
                .cloned()
                .unwrap_or_else(|| json!("")),
        ),
        (
            "end_time_text",
            "fDLv3b",
            defaults
                .get("end_time_text")
                .cloned()
                .unwrap_or_else(|| json!("")),
        ),
    ];
    Value::Array(
        candidates
            .into_iter()
            .filter_map(|(source, target, default_value)| {
                let field = schema.get(target)?;
                let target_type = field.get("type").and_then(Value::as_str).unwrap_or("text");
                let required = field
                    .get("title")
                    .and_then(Value::as_str)
                    .is_some_and(|title| title.starts_with('*'));
                Some(json!({
                    "source_key": source,
                    "target_field_id": target,
                    "target_type": target_type,
                    "required": required,
                    "default_value": default_value,
                }))
            })
            .collect(),
    )
}

fn ensure_schedule_template_ids(config: &mut Value) {
    let default_template_id = config
        .get("smart_sheet")
        .and_then(|smart| smart.get("default_template_id"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let Some(schedules) = config.get_mut("schedules").and_then(Value::as_array_mut) else {
        return;
    };
    for schedule in schedules {
        if let Some(schedule_object) = schedule.as_object_mut() {
            schedule_object
                .entry("smartSheetTemplateId".to_string())
                .or_insert_with(|| json!(default_template_id.clone()));
            for key in ["promptId", "smartSheetTemplateId"] {
                if let Some(reference) = schedule_object
                    .get(key)
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .map(str::to_owned)
                {
                    schedule_object.insert(key.to_string(), json!(reference));
                }
            }
        }
    }
}

fn normalize_config(config: &mut Value, migrate_legacy: bool) {
    let fallback: Value =
        serde_json::from_str(EXAMPLE_CONFIG).unwrap_or(Value::Object(Default::default()));
    normalize_object_shape(config, &fallback);
    ensure_smart_sheet(config, migrate_legacy);
    ensure_prompts(config, migrate_legacy);
    ensure_schedule_template_ids(config);
    if let Some(root) = config.as_object_mut() {
        for key in ["mcp_servers", "reply_listeners"] {
            let partition = root.entry(key.to_string()).or_insert_with(|| json!([]));
            if !partition.is_array() {
                *partition = json!([]);
            }
        }
        root.insert("config_version".to_string(), json!(CURRENT_CONFIG_VERSION));
    }
}

fn normalize_object_shape(value: &mut Value, fallback: &Value) {
    let (Some(value_object), Some(fallback_object)) = (value.as_object_mut(), fallback.as_object())
    else {
        *value = fallback.clone();
        return;
    };
    for (key, fallback_value) in fallback_object {
        if !fallback_value.is_object() {
            continue;
        }
        let current = value_object
            .entry(key.clone())
            .or_insert_with(|| fallback_value.clone());
        normalize_object_shape(current, fallback_value);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn legacy_smart_sheet_and_schedules_migrate_idempotently() {
        let mut config = json!({
            "prompts": {
                "default_id": "custom",
                "items": [{
                    "id": "custom",
                    "name": "自定义",
                    "description": "",
                    "content": "分析问题"
                }]
            },
            "schedules": [{
                "id": "daily"
            }],
            "smart_sheet": {
                "url": "https://docs.qq.com/sheet/legacy",
                "webhook_url": "https://example.invalid/hook",
                "webhook_url_env": "LEGACY_SMART_SHEET_WEBHOOK",
                "schema": {
                    "f04Gwj": {
                        "title": "*模块",
                        "type": "single_select",
                        "enum": ["自定义模块", "待评估"]
                    }
                },
                "defaults": {
                    "module_text": "自定义模块"
                }
            }
        });

        assert!(requires_legacy_migration(&config));
        normalize_config(&mut config, true);
        let once = config.clone();
        assert!(!requires_legacy_migration(&config));
        normalize_config(&mut config, false);

        assert_eq!(config, once);
        assert_eq!(config["config_version"], 3);
        assert_eq!(config["mcp_servers"], json!([]));
        assert_eq!(config["reply_listeners"], json!([]));
        assert_eq!(config["smart_sheet"]["default_template_id"], "default");
        assert_eq!(
            config["smart_sheet"]["templates"][0]["webhook_url"],
            "https://example.invalid/hook"
        );
        assert_eq!(
            config["smart_sheet"]["templates"][0]["webhook_url_env"],
            "LEGACY_SMART_SHEET_WEBHOOK"
        );
        assert_eq!(
            config["prompts"]["items"][0]["issue_fields"][1]["options"][0],
            "自定义模块"
        );
        assert_eq!(config["schedules"][0]["smartSheetTemplateId"], "default");
        assert!(config["smart_sheet"].get("webhook_url").is_none());
    }

    #[test]
    fn version_two_normalizes_ids_and_preserves_custom_default_issue_fields() {
        let custom_fields = json!([{
            "key": "customer_impact",
            "label": "客户影响",
            "type": "long_text",
            "required": true,
            "instruction": "描述影响范围",
            "options": [],
            "default_value": "待确认"
        }]);
        let local_config = json!({
            "config_version": 2,
            "prompts": {
                "default_id": "  Prompt With Spaces  ",
                "default_issue_fields": custom_fields.clone(),
                "items": [
                    {
                        "id": "  Prompt With Spaces  ",
                        "name": "自定义",
                        "description": "",
                        "content": "分析问题",
                        "issue_fields": [],
                        "default_smart_sheet_template_id": "  Incident Sheet  "
                    },
                    {
                        "name": "缺失 ID",
                        "description": "",
                        "content": "分析问题",
                        "issue_fields": [],
                        "default_smart_sheet_template_id": "  Missing Template  "
                    }
                ]
            },
            "schedules": [
                {
                    "id": "valid",
                    "promptId": "  Prompt With Spaces  ",
                    "smartSheetTemplateId": "  Incident Sheet  "
                },
                {
                    "id": "invalid",
                    "promptId": "  Missing Prompt  ",
                    "smartSheetTemplateId": "  Missing Template  "
                },
                {
                    "id": "legacy",
                    "promptId": "prompt_2"
                }
            ],
            "smart_sheet": {
                "default_template_id": "  Incident Sheet  ",
                "templates": [
                    {
                        "id": "  Incident Sheet  ",
                        "name": "空模板",
                        "url": "",
                        "webhook_url_env": "",
                        "webhook_url": "",
                        "batch_size": 25,
                        "schema": {},
                        "field_mappings": []
                    },
                    {
                        "name": "缺失 ID",
                        "schema": {},
                        "field_mappings": []
                    }
                ]
            }
        });
        let directory = tempfile::tempdir().expect("temporary config directory");
        let path = directory.path().join("config.local.json");
        fs::write(
            &path,
            serde_json::to_vec(&local_config).expect("serialize local config"),
        )
        .expect("write local config");

        let config = load_config(&path).expect("load version two config");

        assert_eq!(config["config_version"], 3);
        assert_eq!(config["mcp_servers"], json!([]));
        assert_eq!(config["reply_listeners"], json!([]));
        assert_eq!(config["prompts"]["default_issue_fields"], custom_fields);
        assert_eq!(config["prompts"]["default_id"], "Prompt With Spaces");
        assert_eq!(
            config["prompts"]["items"][0]["issue_fields"],
            config["prompts"]["default_issue_fields"]
        );
        assert_eq!(config["prompts"]["items"][0]["id"], "Prompt With Spaces");
        assert_eq!(config["prompts"]["items"][1]["id"], "prompt_2");
        assert_eq!(
            config["prompts"]["items"][0]["default_smart_sheet_template_id"],
            "Incident Sheet"
        );
        assert_eq!(
            config["prompts"]["items"][1]["default_smart_sheet_template_id"],
            "Missing Template"
        );
        assert_eq!(
            config["smart_sheet"]["default_template_id"],
            "Incident Sheet"
        );
        assert_eq!(
            config["smart_sheet"]["templates"][0]["id"],
            "Incident Sheet"
        );
        assert_eq!(config["smart_sheet"]["templates"][1]["id"], "template_2");
        assert_eq!(config["smart_sheet"]["templates"][0]["schema"], json!({}));
        assert_eq!(
            config["smart_sheet"]["templates"][0]["field_mappings"],
            json!([])
        );
        assert_eq!(config["schedules"][0]["promptId"], "Prompt With Spaces");
        assert_eq!(
            config["schedules"][0]["smartSheetTemplateId"],
            "Incident Sheet"
        );
        assert_eq!(config["schedules"][1]["promptId"], "Missing Prompt");
        assert_eq!(
            config["schedules"][1]["smartSheetTemplateId"],
            "Missing Template"
        );
        assert_eq!(
            config["schedules"][2]["smartSheetTemplateId"],
            "Incident Sheet"
        );
    }

    #[test]
    fn version_two_empty_issue_fields_inherit_built_in_defaults() {
        let mut config = json!({
            "config_version": 2,
            "prompts": {
                "default_id": "  missing prompt  ",
                "default_issue_fields": [],
                "items": [{
                    "id": "empty",
                    "name": "空字段",
                    "description": "",
                    "content": "分析问题",
                    "issue_fields": [],
                    "default_smart_sheet_template_id": "  missing template  "
                }]
            },
            "schedules": [{
                "id": "invalid references",
                "promptId": "  missing prompt  ",
                "smartSheetTemplateId": "  missing template  "
            }],
            "smart_sheet": {
                "default_template_id": "  missing template  ",
                "templates": [{
                    "id": "empty",
                    "name": "空模板",
                    "schema": {},
                    "field_mappings": []
                }]
            }
        });
        let fallback: Value = serde_json::from_str(EXAMPLE_CONFIG).expect("parse example config");
        let expected_fields = fallback["prompts"]["default_issue_fields"].clone();

        normalize_config(&mut config, false);

        assert_eq!(config["prompts"]["default_issue_fields"], expected_fields);
        assert_eq!(
            config["prompts"]["items"][0]["issue_fields"],
            config["prompts"]["default_issue_fields"]
        );
        assert_eq!(config["prompts"]["default_id"], "empty");
        assert_eq!(config["smart_sheet"]["default_template_id"], "empty");
        assert_eq!(
            config["prompts"]["items"][0]["default_smart_sheet_template_id"],
            "missing template"
        );
        assert_eq!(config["schedules"][0]["promptId"], "missing prompt");
        assert_eq!(
            config["schedules"][0]["smartSheetTemplateId"],
            "missing template"
        );
        assert_eq!(config["smart_sheet"]["templates"][0]["schema"], json!({}));
        assert_eq!(
            config["smart_sheet"]["templates"][0]["field_mappings"],
            json!([])
        );
    }

    #[test]
    fn persisted_schedule_references_block_prompt_and_template_deletion() {
        let persisted = json!({
            "schedules": [{
                "id": "daily",
                "name": "每日盘点",
                "promptId": "used-prompt",
                "smartSheetTemplateId": "used-template"
            }]
        });
        let mut missing_prompt = json!({
            "prompts": { "items": [{ "id": "another-prompt" }] },
            "smart_sheet": { "templates": [{ "id": "used-template" }] }
        });
        let prompt_error = preserve_persisted_schedules(&mut missing_prompt, &persisted)
            .expect_err("deleting a referenced prompt must fail");
        assert!(prompt_error.contains("提示词"));
        assert!(prompt_error.contains("每日盘点"));

        let mut missing_template = json!({
            "prompts": { "items": [{ "id": "used-prompt" }] },
            "smart_sheet": { "templates": [{ "id": "another-template" }] }
        });
        let template_error = preserve_persisted_schedules(&mut missing_template, &persisted)
            .expect_err("deleting a referenced template must fail");
        assert!(template_error.contains("腾讯文档模板"));
        assert!(template_error.contains("每日盘点"));
    }

    #[test]
    fn stale_general_settings_cannot_replace_runtime_compatibility_partitions() {
        let persisted = json!({
            "mcp_servers": [{ "id": "current-mcp", "name": "Current MCP" }],
            "reply_listeners": [{ "id": "current-listener", "groupId": "group-1" }]
        });
        let mut incoming = json!({
            "llm": { "model": "new-model" },
            "mcp_servers": [{ "id": "stale-mcp" }],
            "reply_listeners": []
        });

        preserve_persisted_runtime_partitions(&mut incoming, &persisted);

        assert_eq!(incoming["llm"]["model"], "new-model");
        assert_eq!(incoming["mcp_servers"], persisted["mcp_servers"]);
        assert_eq!(incoming["reply_listeners"], persisted["reply_listeners"]);
    }

    #[test]
    fn duplicate_ids_are_rejected_after_trimming_but_case_is_preserved() {
        let distinct = json!({
            "prompts": {
                "items": [{ "id": "Prompt One" }, { "id": "prompt one" }]
            },
            "smart_sheet": {
                "templates": [{ "id": "Incident Sheet" }, { "id": "incident sheet" }]
            }
        });
        validate_unique_config_ids(&distinct)
            .expect("case-sensitive IDs containing spaces must remain valid");

        let duplicate_prompt = json!({
            "prompts": {
                "items": [{ "id": "  Prompt One  " }, { "id": "Prompt One" }]
            },
            "smart_sheet": { "templates": [] }
        });
        let prompt_error = validate_unique_config_ids(&duplicate_prompt)
            .expect_err("duplicate prompt IDs must fail validation");
        assert!(prompt_error.contains("Prompt One"));

        let duplicate_template = json!({
            "prompts": { "items": [] },
            "smart_sheet": {
                "templates": [{ "id": "  Incident Sheet  " }, { "id": "Incident Sheet" }]
            }
        });
        let template_error = validate_unique_config_ids(&duplicate_template)
            .expect_err("duplicate template IDs must fail validation");
        assert!(template_error.contains("Incident Sheet"));
    }

    #[test]
    fn portable_backup_excludes_all_machine_local_credentials_and_runtime_secrets() {
        let mut config = json!({
            "wxwork_db_dir": "C:/WXWork/Data",
            "wxwork_keys_file": "C:/private/wxwork_keys.json",
            "default_workspace": "D:/exports",
            "wecom_access_token": "temporary-wecom-token",
            "corpid": "legacy-root-corp-id",
            "llm": {
                "base_url": "https://model.example/v1",
                "api_key": "model-api-key",
                "model": "analysis-model"
            },
            "ocr": { "api_key": "ocr-api-key", "model": "vision-model" },
            "mcp_servers": [{
                "id": "knowledge",
                "transport": "streamable-http",
                "url": "https://mcp.example/api",
                "headers": { "Authorization": "Bearer private" },
                "env": { "MCP_TOKEN": "private" },
                "webhook": "https://example.invalid/mcp-hook"
            }],
            "reply_listeners": [{
                "id": "support",
                "groupId": "group-1",
                "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=private"
            }],
            "prompts": { "default_id": "custom", "items": [{ "id": "custom" }] },
            "schedules": [{ "id": "daily" }],
            "smart_sheet": {
                "corpsecret": "legacy-smart-sheet-secret",
                "templates": [{
                    "id": "issues",
                    "webhook_url": "https://qyapi.weixin.qq.com/hook-with-secret",
                    "schema": { "field": { "type": "text" } }
                }],
                "upload": {
                    "corpid": "private-corp-id",
                    "corpsecret": "private-corp-secret",
                    "token_endpoint": "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
                }
            }
        });

        sanitize_backup_config(&mut config);

        assert!(config.get("wxwork_db_dir").is_none());
        assert!(config.get("wxwork_keys_file").is_none());
        assert!(config.get("default_workspace").is_none());
        assert!(config.get("wecom_access_token").is_none());
        assert!(config.get("corpid").is_none());
        assert!(config["llm"].get("api_key").is_none());
        assert!(config["ocr"].get("api_key").is_none());
        assert!(config["mcp_servers"][0].get("headers").is_none());
        assert!(config["mcp_servers"][0].get("env").is_none());
        assert!(config["mcp_servers"][0].get("webhook").is_none());
        assert_eq!(config["mcp_servers"][0]["url"], "https://mcp.example/api");
        assert!(config["reply_listeners"][0].get("webhookUrl").is_none());
        assert!(config["smart_sheet"]["templates"][0]
            .get("webhook_url")
            .is_none());
        assert_eq!(
            config["smart_sheet"]["upload"]["token_endpoint"],
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        );
        assert!(config["smart_sheet"]["upload"].get("corpid").is_none());
        assert!(config["smart_sheet"]["upload"].get("corpsecret").is_none());
        assert!(config["smart_sheet"].get("corpsecret").is_none());
        assert_eq!(config["prompts"]["default_id"], "custom");
        assert_eq!(config["schedules"][0]["id"], "daily");
    }

    #[test]
    fn backup_import_preserves_active_machine_values() {
        let current = json!({
            "wxwork_db_dir": "C:/current/Data",
            "wxwork_keys_file": "C:/current/keys.json",
            "default_workspace": "D:/current/exports",
            "wecom_token": "current-token",
            "llm": { "api_key": "current-model-key", "model": "current-model" },
            "ocr": { "api_key": "current-ocr-key", "model": "current-vision" },
            "mcp_servers": [{
                "id": "knowledge",
                "headers": { "Authorization": "Bearer current" },
                "env": { "MCP_TOKEN": "current" },
                "url": "https://current.invalid"
            }],
            "reply_listeners": [{
                "id": "support",
                "groupId": "current-group",
                "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=current"
            }],
            "smart_sheet": {
                "corp_id": "current-legacy-corp-id",
                "templates": [{
                    "id": "shared-template",
                    "webhook_url": "https://qyapi.weixin.qq.com/current-template-hook"
                }],
                "upload": {
                    "corpid": "current-corp-id",
                    "corpsecret": "current-corp-secret"
                }
            }
        });
        let mut imported = json!({
            "wxwork_db_dir": "C:/backup/Data",
            "wxwork_keys_file": "C:/backup/keys.json",
            "default_workspace": "D:/backup/exports",
            "llm": { "api_key": "backup-model-key", "model": "imported-model" },
            "ocr": { "api_key": "backup-ocr-key", "model": "imported-vision" },
            "mcp_servers": [{
                "id": "knowledge",
                "headers": { "Authorization": "Bearer backup" },
                "env": { "MCP_TOKEN": "backup" },
                "url": "https://imported.example"
            }],
            "reply_listeners": [{
                "id": "support",
                "groupId": "imported-group",
                "webhookUrl": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=backup"
            }],
            "smart_sheet": {
                "templates": [{
                    "id": "shared-template",
                    "name": "Imported template",
                    "webhook_url": "https://qyapi.weixin.qq.com/backup-template-hook"
                }],
                "upload": {}
            }
        });

        sanitize_backup_config(&mut imported);
        restore_protected_config(&mut imported, &current).expect("protected values are restored");

        assert_eq!(imported["wxwork_db_dir"], "C:/current/Data");
        assert_eq!(imported["wxwork_keys_file"], "C:/current/keys.json");
        assert_eq!(imported["default_workspace"], "D:/current/exports");
        assert_eq!(imported["wecom_token"], "current-token");
        assert_eq!(imported["llm"]["api_key"], "current-model-key");
        assert_eq!(imported["ocr"]["api_key"], "current-ocr-key");
        assert_eq!(
            imported["mcp_servers"][0]["headers"]["Authorization"],
            "Bearer current"
        );
        assert_eq!(imported["mcp_servers"][0]["env"]["MCP_TOKEN"], "current");
        assert_eq!(
            imported["mcp_servers"][0]["url"],
            "https://imported.example"
        );
        assert_eq!(
            imported["reply_listeners"][0]["webhookUrl"],
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=current"
        );
        assert_eq!(imported["smart_sheet"]["corp_id"], "current-legacy-corp-id");
        assert_eq!(
            imported["smart_sheet"]["upload"]["corpid"],
            "current-corp-id"
        );
        assert_eq!(
            imported["smart_sheet"]["upload"]["corpsecret"],
            "current-corp-secret"
        );
        assert_eq!(imported["llm"]["model"], "imported-model");
        assert_eq!(
            imported["smart_sheet"]["templates"][0]["webhook_url"],
            "https://qyapi.weixin.qq.com/current-template-hook"
        );
        assert_eq!(
            imported["smart_sheet"]["templates"][0]["name"],
            "Imported template"
        );
    }
}
