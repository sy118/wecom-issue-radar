use serde_json::{json, Value};
use std::path::Path;
use std::process::Command;
use tauri::AppHandle;

use crate::{config, scheduler, worker};

#[tauri::command]
pub fn bootstrap() -> Result<config::BootstrapPayload, String> {
    config::bootstrap_payload()
}

#[tauri::command]
pub fn save_config(config: Value) -> Result<config::BootstrapPayload, String> {
    config::save_config_preserving_schedules(config)
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
        worker::request(
            "run",
            json!({ "configPath": config_path, "request": request }),
        ),
    )
    .await
}

#[tauri::command]
pub fn list_schedules() -> Result<Vec<Value>, String> {
    scheduler::list_schedules()
}

#[tauri::command]
pub fn save_schedules(schedules: Vec<Value>) -> Result<Vec<Value>, String> {
    scheduler::save_schedules(schedules)
}

#[tauri::command]
pub fn run_schedule_now(app: AppHandle, schedule_id: String) -> Result<(), String> {
    scheduler::run_schedule_now(app, schedule_id)
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

#[tauri::command]
pub fn open_documentation() -> Result<(), String> {
    const DOCUMENTATION_URL: &str = "https://github.com/sy118/wecom-issue-radar";

    #[cfg(windows)]
    let mut command = {
        let mut command = Command::new("explorer.exe");
        command.arg(DOCUMENTATION_URL);
        command
    };

    #[cfg(target_os = "macos")]
    let mut command = {
        let mut command = Command::new("open");
        command.arg(DOCUMENTATION_URL);
        command
    };

    #[cfg(all(unix, not(target_os = "macos")))]
    let mut command = {
        let mut command = Command::new("xdg-open");
        command.arg(DOCUMENTATION_URL);
        command
    };

    command
        .spawn()
        .map(|_| ())
        .map_err(|_| "无法打开使用说明，请稍后重试".to_string())
}
