use serde_json::{json, Value};
use std::path::Path;
use std::process::Command;
use tauri::AppHandle;

use crate::{config, worker};

#[tauri::command]
pub fn bootstrap() -> Result<config::BootstrapPayload, String> {
    config::bootstrap_payload()
}

#[tauri::command]
pub fn save_config(config: Value) -> Result<config::BootstrapPayload, String> {
    config::save_config(config)
}

#[tauri::command]
pub async fn detect_environment(app: AppHandle) -> Result<Value, String> {
    worker::run_worker(app, worker::request("detect", json!({}))).await
}

#[tauri::command]
pub async fn list_groups(app: AppHandle) -> Result<Value, String> {
    let config_path = config::config_path()?.to_string_lossy().into_owned();
    worker::run_worker(
        app,
        worker::request("groups", json!({ "configPath": config_path, "limit": 150 })),
    )
    .await
}

#[tauri::command]
pub async fn run_task(app: AppHandle, request: Value) -> Result<Value, String> {
    let config_path = config::config_path()?.to_string_lossy().into_owned();
    worker::run_worker(
        app,
        worker::request("run", json!({ "configPath": config_path, "request": request })),
    )
    .await
}

#[tauri::command]
pub async fn sync_smart_sheet(app: AppHandle, payload: Value) -> Result<Value, String> {
    let config_path = config::config_path()?.to_string_lossy().into_owned();
    let day_dir = payload.get("dayDir").cloned().unwrap_or(Value::Null);
    let date = payload.get("date").cloned().unwrap_or(Value::Null);
    let upload_images = payload
        .get("uploadImages")
        .cloned()
        .unwrap_or(Value::Bool(true));
    worker::run_worker(
        app,
        worker::request(
            "sync",
            json!({
                "configPath": config_path,
                "dayDir": day_dir,
                "date": date,
                "uploadImages": upload_images,
            }),
        ),
    )
    .await
}

#[tauri::command]
pub fn launch_key_extraction() -> Result<(), String> {
    worker::launch_key_extraction()
}

#[tauri::command]
pub fn open_path(path: String) -> Result<(), String> {
    let target = Path::new(&path);
    if !target.exists() {
        return Err(format!("文件或目录不存在：{path}"));
    }
    #[cfg(windows)]
    {
        let mut command = Command::new("explorer.exe");
        if target.is_file() {
            command.arg(format!("/select,{}", target.display()));
        } else {
            command.arg(target);
        }
        command
            .spawn()
            .map_err(|error| format!("打开路径失败：{error}"))?;
        Ok(())
    }
    #[cfg(not(windows))]
    {
        let program = if cfg!(target_os = "macos") {
            "open"
        } else {
            "xdg-open"
        };
        Command::new(program)
            .arg(target)
            .spawn()
            .map_err(|error| format!("打开路径失败：{error}"))?;
        Ok(())
    }
}
