use serde_json::{json, Value};
use std::path::Path;
use std::process::Command;
use tauri::{AppHandle, State};

use crate::{
    config,
    reply_runtime::{ReplyRuntime, ReplyRuntimeError},
    scheduler, worker,
};

#[derive(Debug, PartialEq, Eq)]
struct SmartSheetSyncGuard {
    definition_path: String,
    expected_template_revision: String,
    expected_document_revision: String,
}

fn required_payload_string(
    payload: &Value,
    key: &str,
    error_message: &str,
) -> Result<String, String> {
    payload
        .get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
        .ok_or_else(|| error_message.to_string())
}

fn smart_sheet_sync_guard(payload: &Value) -> Result<SmartSheetSyncGuard, String> {
    Ok(SmartSheetSyncGuard {
        definition_path: required_payload_string(
            payload,
            "definitionPath",
            "腾讯文档同步必须使用冻结的问题定义快照",
        )?,
        expected_template_revision: required_payload_string(
            payload,
            "expectedTemplateRevision",
            "腾讯文档同步前必须先刷新预览并确认模板 revision",
        )?,
        expected_document_revision: required_payload_string(
            payload,
            "expectedDocumentRevision",
            "腾讯文档同步前必须先刷新预览并确认问题快照 revision",
        )?,
    })
}

fn frozen_task_template_id(request: &Value) -> Result<String, String> {
    let template_id = request
        .get("smartSheetTemplateId")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or_default();
    let prepares_smart_sheet = request
        .get("prepareSmartSheet")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if prepares_smart_sheet && template_id.is_empty() {
        return Err("准备腾讯文档同步前必须冻结模板 ID".to_string());
    }
    Ok(template_id.to_string())
}

#[tauri::command]
pub fn bootstrap() -> Result<config::BootstrapPayload, String> {
    config::bootstrap_payload()
}

#[tauri::command]
pub fn save_config(config: Value) -> Result<config::BootstrapPayload, String> {
    scheduler::save_config_preserving_task_references(config)
}

#[tauri::command]
pub fn export_config_backup(path: String) -> Result<(), String> {
    config::export_config_backup(Path::new(&path))
}

#[tauri::command]
pub fn import_config_backup(path: String) -> Result<config::BootstrapPayload, String> {
    scheduler::import_config_backup(Path::new(&path))
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
    let template_id = frozen_task_template_id(&request)?;
    let in_flight_guard =
        scheduler::register_in_flight_template_reference(&template_id, "正在运行的手动任务")?;
    let config_path = config::config_path()?.to_string_lossy().into_owned();
    let result = worker::run_worker(
        app,
        worker::request(
            "run",
            json!({ "configPath": config_path, "request": request }),
        ),
    )
    .await;
    drop(in_flight_guard);
    result
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
pub fn list_schedule_execution_history(
    page: usize,
    page_size: usize,
    schedule_id: Option<String>,
) -> Result<scheduler::ScheduleExecutionHistoryPage, String> {
    scheduler::list_schedule_execution_history(page, page_size, schedule_id)
}

#[tauri::command]
pub fn list_pending_smart_sheet_syncs() -> Result<Vec<scheduler::PendingScheduleSync>, String> {
    scheduler::list_pending_smart_sheet_syncs()
}

#[tauri::command]
pub fn clear_pending_smart_sheet_syncs(pending_ids: Vec<String>) -> Result<(), String> {
    scheduler::clear_pending_smart_sheet_syncs(pending_ids)
}

#[tauri::command]
pub async fn preview_smart_sheet(app: AppHandle, payload: Value) -> Result<Value, String> {
    let config_path = config::config_path()?.to_string_lossy().into_owned();
    let day_dir = payload.get("dayDir").cloned().unwrap_or(Value::Null);
    let date = payload.get("date").cloned().unwrap_or(Value::Null);
    let template_id = payload
        .get("templateId")
        .cloned()
        .unwrap_or(Value::String(String::new()));
    let definition_path = payload
        .get("definitionPath")
        .cloned()
        .unwrap_or(Value::String(String::new()));
    worker::run_worker(
        app,
        worker::request(
            "preview",
            json!({
                "configPath": config_path,
                "dayDir": day_dir,
                "date": date,
                "templateId": template_id,
                "definitionPath": definition_path,
            }),
        ),
    )
    .await
}

#[tauri::command]
pub async fn sync_smart_sheet(app: AppHandle, payload: Value) -> Result<Value, String> {
    let guard = smart_sheet_sync_guard(&payload)?;
    let config_path = config::config_path()?.to_string_lossy().into_owned();
    let day_dir = payload.get("dayDir").cloned().unwrap_or(Value::Null);
    let date = payload.get("date").cloned().unwrap_or(Value::Null);
    let upload_images = payload
        .get("uploadImages")
        .cloned()
        .unwrap_or(Value::Bool(true));
    let template_id = payload
        .get("templateId")
        .cloned()
        .unwrap_or(Value::String(String::new()));
    worker::run_worker(
        app,
        worker::request(
            "sync",
            json!({
                "configPath": config_path,
                "dayDir": day_dir,
                "date": date,
                "templateId": template_id,
                "uploadImages": upload_images,
                "definitionPath": guard.definition_path,
                "expectedTemplateRevision": guard.expected_template_revision,
                "expectedDocumentRevision": guard.expected_document_revision,
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
pub async fn reply_runtime_execute(
    runtime: State<'_, ReplyRuntime>,
    command: Value,
) -> Result<Value, ReplyRuntimeError> {
    runtime.execute(command).await
}

#[tauri::command]
pub async fn reply_runtime_query(
    runtime: State<'_, ReplyRuntime>,
    query: Value,
) -> Result<Value, ReplyRuntimeError> {
    runtime.query(query).await
}

#[tauri::command]
pub async fn prepare_update_install(runtime: State<'_, ReplyRuntime>) -> Result<(), String> {
    runtime
        .prepare_for_update()
        .await
        .map_err(reply_runtime_error_message)?;
    if let Err(error) = worker::prepare_update_install() {
        let _ = runtime.resume_after_update();
        return Err(error);
    }
    Ok(())
}

fn reply_runtime_error_message(error: ReplyRuntimeError) -> String {
    format!("{}: {}", error.code, error.message)
}

#[tauri::command]
pub fn cancel_update_install(runtime: State<'_, ReplyRuntime>) -> Result<(), String> {
    let worker_result = worker::cancel_update_install();
    let runtime_result = runtime
        .resume_after_update()
        .map_err(|error| error.message.clone());
    worker_result.and(runtime_result)
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sync_guard_requires_frozen_snapshot_and_both_revisions() {
        let payload = json!({
            "definitionPath": " D:/exports/issues.json ",
            "expectedTemplateRevision": " template-r1 ",
            "expectedDocumentRevision": " document-r2 "
        });

        assert_eq!(
            smart_sheet_sync_guard(&payload).expect("complete guard is accepted"),
            SmartSheetSyncGuard {
                definition_path: "D:/exports/issues.json".to_string(),
                expected_template_revision: "template-r1".to_string(),
                expected_document_revision: "document-r2".to_string(),
            }
        );

        for key in [
            "definitionPath",
            "expectedTemplateRevision",
            "expectedDocumentRevision",
        ] {
            let mut missing = payload.clone();
            missing[key] = Value::String("   ".to_string());
            assert!(
                smart_sheet_sync_guard(&missing).is_err(),
                "{key} must fail closed when empty"
            );
        }
    }

    #[test]
    fn task_template_is_required_only_when_smart_sheet_is_prepared() {
        assert!(frozen_task_template_id(&json!({
            "prepareSmartSheet": true,
            "smartSheetTemplateId": "   "
        }))
        .is_err());
        assert_eq!(
            frozen_task_template_id(&json!({
                "prepareSmartSheet": true,
                "smartSheetTemplateId": " template-a "
            }))
            .expect("template is frozen"),
            "template-a"
        );
        assert_eq!(
            frozen_task_template_id(&json!({ "prepareSmartSheet": false }))
                .expect("plain exports need no template"),
            ""
        );
    }
}
