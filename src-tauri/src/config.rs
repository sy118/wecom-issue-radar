use serde::Serialize;
use serde_json::Value;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

const EXAMPLE_CONFIG: &str = include_str!("../../config.example.json");

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
    Ok(home
        .join(".wecom-issue-radar")
        .join("config.local.json"))
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
    if path.exists() {
        let text = fs::read_to_string(path)
            .map_err(|error| format!("读取配置失败（{}）：{error}", path.display()))?;
        let local: Value = serde_json::from_str(&text)
            .map_err(|error| format!("配置文件不是有效 JSON：{error}"))?;
        deep_merge(&mut config, local);
    }
    normalize_config(&mut config);
    Ok(config)
}

pub fn save_config(mut config: Value) -> Result<BootstrapPayload, String> {
    normalize_config(&mut config);
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
    file.sync_all()
        .map_err(|error| format!("保存配置失败：{error}"))?;
    file.persist(&path)
        .map_err(|error| format!("替换配置文件失败：{}", error.error))?;

    Ok(BootstrapPayload {
        config,
        config_path: path.to_string_lossy().into_owned(),
        app_version: env!("CARGO_PKG_VERSION").to_string(),
    })
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

fn ensure_prompts(config: &mut Value) {
    let fallback: Value = serde_json::from_str(EXAMPLE_CONFIG).unwrap_or(Value::Null);
    let fallback_prompts = fallback.get("prompts").cloned().unwrap_or(Value::Null);
    let Some(root) = config.as_object_mut() else {
        *config = fallback;
        return;
    };
    let prompts = root.entry("prompts").or_insert(fallback_prompts.clone());
    let valid_items = prompts
        .get("items")
        .and_then(Value::as_array)
        .is_some_and(|items| !items.is_empty());
    if !valid_items {
        *prompts = fallback_prompts;
        return;
    }
    let Some(prompt_object) = prompts.as_object_mut() else {
        *prompts = fallback_prompts;
        return;
    };
    let ids: Vec<String> = prompt_object
        .get("items")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|item| item.get("id").and_then(Value::as_str).map(str::to_owned))
        .collect();
    let default_valid = prompt_object
        .get("default_id")
        .and_then(Value::as_str)
        .is_some_and(|id| ids.iter().any(|candidate| candidate == id));
    if default_valid {
        return;
    }
    if let Some(first) = ids.first() {
        prompt_object.insert("default_id".to_string(), Value::String(first.clone()));
    }
}

fn normalize_config(config: &mut Value) {
    let fallback: Value = serde_json::from_str(EXAMPLE_CONFIG).unwrap_or(Value::Object(Default::default()));
    normalize_object_shape(config, &fallback);
    ensure_prompts(config);
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
