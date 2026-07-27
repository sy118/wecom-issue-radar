use chrono::{DateTime, Datelike, NaiveDate, SecondsFormat, Utc};
use chrono_tz::Asia::Shanghai;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{LazyLock, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Emitter};

use crate::{config, worker};

const POLL_INTERVAL: Duration = Duration::from_secs(30);
const MAX_SCHEDULE_EXECUTION_HISTORY: usize = 500;
const MAX_SCHEDULE_HISTORY_PAGE_SIZE: usize = 50;
static STATE_FILE_LOCK: Mutex<()> = Mutex::new(());
static IN_FLIGHT_REFERENCES: LazyLock<Mutex<HashMap<InFlightReferenceKey, usize>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ScheduleDefinition {
    pub id: String,
    pub name: String,
    pub enabled: bool,
    pub run_at: String,
    pub weekdays: Vec<u32>,
    pub date_mode: ScheduleDateMode,
    pub fixed_date: String,
    pub start_time: String,
    pub end_time: String,
    pub groups: Vec<TaskGroup>,
    pub prompt_id: String,
    #[serde(default)]
    pub smart_sheet_template_id: String,
    pub run_ocr: bool,
    pub run_analysis: bool,
    pub export_xlsx: bool,
    pub export_markdown: bool,
    pub prepare_smart_sheet: bool,
    #[serde(default)]
    pub auto_sync_smart_sheet: bool,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ScheduleDateMode {
    Today,
    Yesterday,
    Fixed,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct TaskGroup {
    pub id: String,
    pub name: String,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct ScheduleEventPayload {
    schedule_id: String,
    schedule_name: String,
    message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    success: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    history_persisted: Option<bool>,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
enum ScheduleExecutionTrigger {
    Manual,
    Automatic,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
enum ScheduleExecutionStatus {
    Success,
    Partial,
    Empty,
    Failed,
}

struct ScheduleCompletion {
    message: String,
    success: bool,
    status: ScheduleExecutionStatus,
    result: Option<Value>,
}

impl ScheduleCompletion {
    fn failed(message: String) -> Self {
        Self {
            message,
            success: false,
            status: ScheduleExecutionStatus::Failed,
            result: None,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ScheduleExecutionHistoryItem {
    execution_id: String,
    schedule_id: String,
    schedule_name: String,
    trigger: ScheduleExecutionTrigger,
    started_at: String,
    finished_at: String,
    success: bool,
    status: ScheduleExecutionStatus,
    message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    result: Option<Value>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ScheduleExecutionHistoryPage {
    items: Vec<ScheduleExecutionHistoryItem>,
    page: usize,
    page_size: usize,
    total: usize,
    total_pages: usize,
}

#[derive(Debug, Default, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct ScheduleState {
    #[serde(default)]
    last_runs: HashMap<String, String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pending_smart_sheet_syncs: Vec<PendingScheduleSync>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    execution_history: Vec<ScheduleExecutionHistoryItem>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PendingScheduleSync {
    pending_id: String,
    schedule_id: String,
    schedule_name: String,
    created_at: String,
    result: Value,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct PendingTemplateReference {
    schedule_name: String,
    template_id: String,
    template_name: String,
}

#[derive(Clone, Debug, Hash, PartialEq, Eq)]
struct InFlightReferenceKey {
    schedule_id: Option<String>,
    owner_label: String,
    template_id: String,
}

pub struct InFlightTemplateGuard {
    key: InFlightReferenceKey,
}

impl Drop for InFlightTemplateGuard {
    fn drop(&mut self) {
        let Ok(mut in_flight) = IN_FLIGHT_REFERENCES.lock() else {
            return;
        };
        let should_remove = match in_flight.get_mut(&self.key) {
            Some(count) if *count > 1 => {
                *count -= 1;
                false
            }
            Some(_) => true,
            None => false,
        };
        if should_remove {
            in_flight.remove(&self.key);
        }
    }
}

fn register_in_flight_reference(
    in_flight: &mut HashMap<InFlightReferenceKey, usize>,
    key: InFlightReferenceKey,
) -> InFlightTemplateGuard {
    *in_flight.entry(key.clone()).or_insert(0) += 1;
    InFlightTemplateGuard { key }
}

fn register_current_schedule(
    schedule_id: &str,
) -> Result<(ScheduleDefinition, InFlightTemplateGuard), String> {
    let mut in_flight = IN_FLIGHT_REFERENCES
        .lock()
        .map_err(|_| "运行中任务引用锁已损坏".to_string())?;
    let schedule = find_schedule(schedule_id)?;
    let key = InFlightReferenceKey {
        schedule_id: Some(schedule.id.clone()),
        owner_label: format!("正在运行的定时任务“{}”", schedule.name),
        template_id: schedule.smart_sheet_template_id.trim().to_string(),
    };
    let guard = register_in_flight_reference(&mut in_flight, key);
    Ok((schedule, guard))
}

pub fn register_in_flight_template_reference(
    template_id: &str,
    owner_label: &str,
) -> Result<InFlightTemplateGuard, String> {
    let template_id = template_id.trim();
    let mut in_flight = IN_FLIGHT_REFERENCES
        .lock()
        .map_err(|_| "运行中任务引用锁已损坏".to_string())?;
    if !template_id.is_empty() {
        let path = config::config_path()?;
        let current = config::load_config(&path)?;
        if !configured_template_ids(&current).contains(template_id) {
            return Err(format!("腾讯文档模板“{template_id}”不存在，无法启动任务"));
        }
    }
    Ok(register_in_flight_reference(
        &mut in_flight,
        InFlightReferenceKey {
            schedule_id: None,
            owner_label: owner_label.to_string(),
            template_id: template_id.to_string(),
        },
    ))
}

pub fn start(app: AppHandle) {
    std::mem::drop(tauri::async_runtime::spawn(async move {
        loop {
            if let Err(error) = tick(app.clone()) {
                eprintln!("定时任务检查失败：{error}");
            }
            tokio::time::sleep(POLL_INTERVAL).await;
        }
    }));
}

pub fn list_schedules() -> Result<Vec<Value>, String> {
    let path = config::config_path()?;
    let current = config::load_config(&path)?;
    Ok(current
        .get("schedules")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default())
}

pub fn save_schedules(schedules: Vec<Value>) -> Result<Vec<Value>, String> {
    let mut incoming_ids = HashSet::new();
    let mut parsed_schedules = Vec::with_capacity(schedules.len());
    for value in &schedules {
        let schedule = parse_schedule(value.clone())?;
        validate_schedule(&schedule)?;
        if !incoming_ids.insert(schedule.id.clone()) {
            return Err(format!("定时任务 ID 重复：{}", schedule.id));
        }
        parsed_schedules.push(schedule);
    }

    let in_flight = IN_FLIGHT_REFERENCES
        .lock()
        .map_err(|_| "运行中任务引用锁已损坏".to_string())?;
    let pending_syncs = list_pending_smart_sheet_syncs()?;
    let path = config::config_path()?;
    let mut current = config::load_config(&path)?;
    validate_auto_sync_template_references(&parsed_schedules, &current)?;
    let current_ids = schedule_ids_from_config(&current);
    let in_flight_references = in_flight.keys().cloned().collect::<Vec<_>>();
    let protected_ids = protected_schedule_ids(&pending_syncs, &in_flight_references);
    let blocked_ids = protected_schedule_deletions(&current_ids, &incoming_ids, &protected_ids);
    if let Some(reference) = in_flight.keys().find(|reference| {
        reference
            .schedule_id
            .as_ref()
            .is_some_and(|schedule_id| blocked_ids.contains(schedule_id))
    }) {
        return Err(format!("{}，请等待任务完成后再删除", reference.owner_label));
    }
    if let Some(pending) = pending_syncs
        .iter()
        .find(|pending| blocked_ids.contains(&pending.schedule_id))
    {
        return Err(format!(
            "定时任务“{}”仍有腾讯文档待确认结果，请先同步或放弃后再删除",
            pending.schedule_name
        ));
    }

    let root = current
        .as_object_mut()
        .ok_or_else(|| "配置文件根节点必须是对象".to_string())?;
    root.insert("schedules".to_string(), Value::Array(schedules));
    let saved = config::save_config(current)?;
    drop(in_flight);
    Ok(saved
        .config
        .get("schedules")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default())
}

fn schedule_ids_from_config(config: &Value) -> HashSet<String> {
    config
        .get("schedules")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|schedule| schedule.get("id").and_then(Value::as_str))
        .map(str::to_string)
        .collect()
}

fn protected_schedule_deletions(
    current_ids: &HashSet<String>,
    incoming_ids: &HashSet<String>,
    pending_ids: &HashSet<String>,
) -> HashSet<String> {
    current_ids
        .difference(incoming_ids)
        .filter(|schedule_id| pending_ids.contains(*schedule_id))
        .cloned()
        .collect()
}

fn protected_schedule_ids(
    pending_syncs: &[PendingScheduleSync],
    in_flight_references: &[InFlightReferenceKey],
) -> HashSet<String> {
    pending_syncs
        .iter()
        .map(|pending| pending.schedule_id.clone())
        .chain(
            in_flight_references
                .iter()
                .filter_map(|reference| reference.schedule_id.clone()),
        )
        .collect()
}

pub fn run_schedule_now(app: AppHandle, schedule_id: String) -> Result<(), String> {
    let (schedule, in_flight_guard) = register_current_schedule(&schedule_id)?;
    std::mem::drop(tauri::async_runtime::spawn(async move {
        execute_schedule(app, schedule, ScheduleExecutionTrigger::Manual).await;
        drop(in_flight_guard);
    }));
    Ok(())
}

fn tick(app: AppHandle) -> Result<(), String> {
    let now = Utc::now().with_timezone(&Shanghai);
    let today = now.date_naive();
    let date_text = today.format("%Y-%m-%d").to_string();
    let minute = now.format("%H:%M").to_string();
    let weekday = now.weekday().number_from_monday();

    for candidate in valid_schedules()? {
        if !is_due(&candidate, weekday, &minute) {
            continue;
        }
        let (schedule, in_flight_guard) = match register_current_schedule(&candidate.id) {
            Ok(registered) => registered,
            Err(error) => {
                eprintln!("定时任务启动前复核失败：{error}");
                continue;
            }
        };
        if !is_due(&schedule, weekday, &minute) {
            drop(in_flight_guard);
            continue;
        }
        if !claim_automatic_run(&schedule.id, &date_text, &minute)? {
            drop(in_flight_guard);
            continue;
        }
        let task_app = app.clone();
        std::mem::drop(tauri::async_runtime::spawn(async move {
            execute_schedule(task_app, schedule, ScheduleExecutionTrigger::Automatic).await;
            drop(in_flight_guard);
        }));
    }
    Ok(())
}

async fn execute_schedule(
    app: AppHandle,
    schedule: ScheduleDefinition,
    trigger: ScheduleExecutionTrigger,
) {
    let started_at = Utc::now();
    let today = Utc::now().with_timezone(&Shanghai).date_naive();
    let (start_date, end_date) = match resolve_export_range(&schedule, today) {
        Ok(range) => range,
        Err(error) => {
            emit_completed(
                &app,
                &schedule,
                trigger,
                started_at,
                ScheduleCompletion::failed(error),
            );
            return;
        }
    };
    let range_label = if start_date == end_date {
        format!("{start_date} {}–{}", schedule.start_time, schedule.end_time)
    } else {
        format!(
            "{start_date} {}–{end_date} {}",
            schedule.start_time, schedule.end_time
        )
    };
    let message = format!(
        "正在导出 {range_label} 的 {} 个群聊…",
        schedule.groups.len()
    );
    emit_progress(&app, &schedule, message);

    let config_path = match config::config_path() {
        Ok(path) => path.to_string_lossy().into_owned(),
        Err(error) => {
            emit_completed(
                &app,
                &schedule,
                trigger,
                started_at,
                ScheduleCompletion::failed(error),
            );
            return;
        }
    };
    let request = build_worker_run_request(&schedule, start_date, end_date);
    let worker_request = worker::request(
        "run",
        json!({ "configPath": config_path, "request": request }),
    );
    match worker::run_worker(app.clone(), worker_request).await {
        Ok(mut result) => {
            if schedule.auto_sync_smart_sheet {
                result = automatically_sync_smart_sheet_runs(&app, &schedule, &config_path, result)
                    .await;
                emit_completed(
                    &app,
                    &schedule,
                    trigger,
                    started_at,
                    automatic_sync_completion(result),
                );
                return;
            }
            if should_persist_pending_sync(&schedule, &result) {
                if let Err(error) = persist_pending_smart_sheet_sync(&schedule, result.clone()) {
                    emit_completed(
                        &app,
                        &schedule,
                        trigger,
                        started_at,
                        ScheduleCompletion {
                            message: format!("任务已完成，但保存腾讯文档待确认结果失败：{error}"),
                            success: false,
                            status: ScheduleExecutionStatus::Failed,
                            result: Some(result),
                        },
                    );
                    return;
                }
            }
            let (status, success, message) = worker_completion(&result);
            emit_completed(
                &app,
                &schedule,
                trigger,
                started_at,
                ScheduleCompletion {
                    message: message.to_string(),
                    success,
                    status,
                    result: Some(result),
                },
            );
        }
        Err(error) => emit_completed(
            &app,
            &schedule,
            trigger,
            started_at,
            ScheduleCompletion::failed(error),
        ),
    }
}

async fn automatically_sync_smart_sheet_runs(
    app: &AppHandle,
    schedule: &ScheduleDefinition,
    config_path: &str,
    mut result: Value,
) -> Value {
    for target in automatic_sync_targets(&result, config_path) {
        emit_progress(
            app,
            schedule,
            format!("正在自动同步“{}”到腾讯文档…", target.group_name),
        );
        let payload = match target.payload {
            Ok(payload) => payload,
            Err(error) => {
                apply_automatic_sync_failure(
                    &mut result,
                    target.run_index,
                    &target.group_name,
                    &error,
                );
                continue;
            }
        };
        match worker::run_worker(app.clone(), worker::request("sync", payload)).await {
            Ok(response) => {
                apply_automatic_sync_success(&mut result, target.run_index, &response);
            }
            Err(error) => {
                apply_automatic_sync_failure(
                    &mut result,
                    target.run_index,
                    &target.group_name,
                    &error,
                );
            }
        }
    }
    result
}

fn build_worker_run_request(
    schedule: &ScheduleDefinition,
    start_date: String,
    end_date: String,
) -> Value {
    json!({
        "date": start_date,
        "startDate": start_date,
        "endDate": end_date,
        "startTime": schedule.start_time,
        "endTime": schedule.end_time,
        "groups": schedule.groups,
        "promptId": schedule.prompt_id,
        "smartSheetTemplateId": schedule.smart_sheet_template_id,
        "runOcr": schedule.run_ocr,
        "runAnalysis": schedule.run_analysis,
        "exportXlsx": schedule.export_xlsx,
        "exportMarkdown": schedule.export_markdown,
        // The run action only prepares a preview. The scheduler then either queues manual
        // confirmation or invokes the same guarded sync contract in automatic mode.
        "prepareSmartSheet": schedule.prepare_smart_sheet,
    })
}

#[derive(Debug)]
struct AutomaticSyncTarget {
    run_index: usize,
    group_name: String,
    payload: Result<Value, String>,
}

fn automatic_sync_targets(result: &Value, config_path: &str) -> Vec<AutomaticSyncTarget> {
    result
        .get("runs")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .enumerate()
        .filter(|(_, run)| {
            matches!(
                run.get("status").and_then(Value::as_str),
                Some("success") | None
            ) && run
                .get("smartSheetPreview")
                .and_then(|preview| preview.get("pending"))
                .and_then(Value::as_u64)
                .is_some_and(|pending| pending > 0)
        })
        .map(|(run_index, run)| {
            let group_name = run
                .get("groupName")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|name| !name.is_empty())
                .or_else(|| run.get("groupId").and_then(Value::as_str).map(str::trim))
                .filter(|name| !name.is_empty())
                .unwrap_or("未命名群聊")
                .to_string();
            let payload = build_automatic_sync_payload(run, config_path);
            AutomaticSyncTarget {
                run_index,
                group_name,
                payload,
            }
        })
        .collect()
}

fn build_automatic_sync_payload(run: &Value, config_path: &str) -> Result<Value, String> {
    let preview = run
        .get("smartSheetPreview")
        .and_then(Value::as_object)
        .ok_or_else(|| "缺少腾讯文档预览".to_string())?;
    let required_run = |key: &str, label: &str| {
        run.get(key)
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_string)
            .ok_or_else(|| format!("缺少{label}"))
    };
    let required_preview = |key: &str, label: &str| {
        preview
            .get(key)
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_string)
            .ok_or_else(|| format!("缺少{label}"))
    };
    let date = ["smartSheetDate", "endDate", "startDate"]
        .iter()
        .find_map(|key| {
            run.get(*key)
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
        })
        .map(str::to_string)
        .ok_or_else(|| "缺少腾讯文档同步日期".to_string())?;
    let template_id = run
        .get("smartSheetTemplateId")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .or_else(|| {
            preview
                .get("template_id")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
        })
        .map(str::to_string)
        .ok_or_else(|| "缺少冻结的腾讯文档模板 ID".to_string())?;

    Ok(json!({
        "configPath": config_path,
        "dayDir": required_run("dayDir", "本地结果目录")?,
        "date": date,
        "templateId": template_id,
        "uploadImages": true,
        "definitionPath": required_run("definitionPath", "冻结的问题定义快照")?,
        "expectedTemplateRevision": required_preview("template_revision", "模板 revision")?,
        "expectedDocumentRevision": required_preview("document_revision", "问题快照 revision")?,
    }))
}

fn apply_automatic_sync_success(result: &mut Value, run_index: usize, response: &Value) {
    let Some(run) = result
        .get_mut("runs")
        .and_then(Value::as_array_mut)
        .and_then(|runs| runs.get_mut(run_index))
        .and_then(Value::as_object_mut)
    else {
        return;
    };
    let pending = run
        .get("smartSheetPreview")
        .and_then(|preview| preview.get("pending"))
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let synced = response
        .get("synced")
        .and_then(Value::as_u64)
        .unwrap_or(pending);
    if let Some(preview) = run
        .get_mut("smartSheetPreview")
        .and_then(Value::as_object_mut)
    {
        let previous = preview
            .get("already_synced")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        let already_synced = response
            .get("total")
            .and_then(Value::as_u64)
            .unwrap_or(previous.saturating_add(synced));
        preview.insert("pending".to_string(), Value::from(0));
        preview.insert("already_synced".to_string(), Value::from(already_synced));
    }
    run.insert(
        "smartSheetSync".to_string(),
        json!({ "mode": "automatic", "status": "success", "synced": synced }),
    );
    recompute_result_summary(result);
}

fn apply_automatic_sync_failure(
    result: &mut Value,
    run_index: usize,
    group_name: &str,
    error: &str,
) {
    let message = format!("群聊“{}”腾讯文档自动同步失败：{}", group_name, error.trim());
    let Some(run) = result
        .get_mut("runs")
        .and_then(Value::as_array_mut)
        .and_then(|runs| runs.get_mut(run_index))
        .and_then(Value::as_object_mut)
    else {
        return;
    };
    run.insert("status".to_string(), Value::String("failed".to_string()));
    run.insert("error".to_string(), Value::String(message.clone()));
    run.insert(
        "smartSheetSync".to_string(),
        json!({ "mode": "automatic", "status": "failed", "error": message }),
    );
    recompute_result_summary(result);
}

fn recompute_result_summary(result: &mut Value) {
    let Some(runs) = result.get("runs").and_then(Value::as_array) else {
        return;
    };
    if runs.is_empty() {
        return;
    }
    let total_count = runs.len() as u64;
    let mut success_count = 0_u64;
    let mut empty_count = 0_u64;
    let mut failed_count = 0_u64;
    for run in runs {
        match run.get("status").and_then(Value::as_str) {
            Some("empty") => empty_count += 1,
            Some("failed") => failed_count += 1,
            Some("success") | None => success_count += 1,
            Some(_) => failed_count += 1,
        }
    }
    let status = if failed_count == total_count {
        "failed"
    } else if failed_count > 0 {
        "partial"
    } else if success_count > 0 {
        "success"
    } else {
        "empty"
    };
    let legacy_fields = (runs.len() == 1).then(|| {
        let run = &runs[0];
        (
            run.get("error").cloned(),
            run.get("smartSheetPreview").cloned(),
            run.get("smartSheetSync").cloned(),
        )
    });

    let Some(root) = result.as_object_mut() else {
        return;
    };
    root.insert("status".to_string(), Value::String(status.to_string()));
    root.insert("totalCount".to_string(), Value::from(total_count));
    root.insert("successCount".to_string(), Value::from(success_count));
    root.insert("emptyCount".to_string(), Value::from(empty_count));
    root.insert("failedCount".to_string(), Value::from(failed_count));
    if let Some((error, preview, sync)) = legacy_fields {
        if let Some(error) = error {
            root.insert("error".to_string(), error);
        }
        if let Some(preview) = preview {
            root.insert("smartSheetPreview".to_string(), preview);
        }
        if let Some(sync) = sync {
            root.insert("smartSheetSync".to_string(), sync);
        }
    }
}

fn automatic_sync_completion(result: Value) -> ScheduleCompletion {
    let runs = result
        .get("runs")
        .and_then(Value::as_array)
        .into_iter()
        .flatten();
    let failure_groups = runs
        .clone()
        .filter(|run| {
            run.get("smartSheetSync")
                .and_then(|sync| sync.get("status"))
                .and_then(Value::as_str)
                == Some("failed")
        })
        .map(|run| {
            run.get("groupName")
                .and_then(Value::as_str)
                .or_else(|| run.get("groupId").and_then(Value::as_str))
                .unwrap_or("未命名群聊")
                .to_string()
        })
        .collect::<Vec<_>>();
    let successful_sync_count = runs
        .filter(|run| {
            run.get("smartSheetSync")
                .and_then(|sync| sync.get("status"))
                .and_then(Value::as_str)
                == Some("success")
        })
        .count();
    let (status, success, message) = worker_completion(&result);
    if failure_groups.is_empty() {
        return ScheduleCompletion {
            message: if successful_sync_count > 0 {
                format!("{message}，腾讯文档已自动同步")
            } else {
                message.to_string()
            },
            success,
            status,
            result: Some(result),
        };
    }
    ScheduleCompletion {
        message: format!(
            "腾讯文档自动同步失败：{}；请查看各群执行结果",
            failure_groups.join("、")
        ),
        success: false,
        status,
        result: Some(result),
    }
}

fn should_persist_pending_sync(schedule: &ScheduleDefinition, result: &Value) -> bool {
    schedule.prepare_smart_sheet
        && !schedule.auto_sync_smart_sheet
        && result_has_pending_smart_sheet_records(result)
}

fn execution_status_from_worker_result(result: &Value) -> ScheduleExecutionStatus {
    match result.get("status").and_then(Value::as_str) {
        Some("partial") => ScheduleExecutionStatus::Partial,
        Some("empty") => ScheduleExecutionStatus::Empty,
        Some("failed") => ScheduleExecutionStatus::Failed,
        Some("success") | None => ScheduleExecutionStatus::Success,
        Some(_) => ScheduleExecutionStatus::Failed,
    }
}

fn worker_completion(result: &Value) -> (ScheduleExecutionStatus, bool, &'static str) {
    let status = execution_status_from_worker_result(result);
    match status {
        ScheduleExecutionStatus::Success => (status, true, "任务执行完成"),
        ScheduleExecutionStatus::Partial => (status, true, "任务部分完成，请查看失败群聊"),
        ScheduleExecutionStatus::Empty => (status, true, "任务执行完成，所选群聊均无可分析记录"),
        ScheduleExecutionStatus::Failed => (status, false, "任务执行失败，请查看执行结果"),
    }
}

fn emit_progress(app: &AppHandle, schedule: &ScheduleDefinition, message: String) {
    let _ = app.emit(
        "schedule-progress",
        ScheduleEventPayload {
            schedule_id: schedule.id.clone(),
            schedule_name: schedule.name.clone(),
            message,
            success: None,
            result: None,
            history_persisted: None,
        },
    );
}

fn emit_completed(
    app: &AppHandle,
    schedule: &ScheduleDefinition,
    trigger: ScheduleExecutionTrigger,
    started_at: DateTime<Utc>,
    completion: ScheduleCompletion,
) {
    let ScheduleCompletion {
        mut message,
        success,
        status,
        result,
    } = completion;
    let history_persisted = match persist_schedule_execution_history(
        schedule,
        trigger,
        started_at,
        success,
        status,
        &message,
        result.as_ref(),
    ) {
        Ok(()) => true,
        Err(error) => {
            eprintln!("保存定时任务执行记录失败：{error}");
            message.push_str("；本次执行记录未能保存，请检查配置目录权限或磁盘空间");
            false
        }
    };
    let _ = app.emit(
        "schedule-completed",
        ScheduleEventPayload {
            schedule_id: schedule.id.clone(),
            schedule_name: schedule.name.clone(),
            message,
            success: Some(success),
            result,
            history_persisted: Some(history_persisted),
        },
    );
}

fn valid_schedules() -> Result<Vec<ScheduleDefinition>, String> {
    let mut valid = Vec::new();
    for value in list_schedules()? {
        match parse_schedule(value).and_then(|schedule| {
            validate_schedule(&schedule)?;
            Ok(schedule)
        }) {
            Ok(schedule) => valid.push(schedule),
            Err(error) => eprintln!("已跳过无效的定时任务：{error}"),
        }
    }
    Ok(valid)
}

fn find_schedule(schedule_id: &str) -> Result<ScheduleDefinition, String> {
    let value = list_schedules()?
        .into_iter()
        .find(|value| value.get("id").and_then(Value::as_str) == Some(schedule_id))
        .ok_or_else(|| format!("找不到定时任务：{schedule_id}"))?;
    let schedule = parse_schedule(value)?;
    validate_schedule(&schedule)?;
    Ok(schedule)
}

fn parse_schedule(value: Value) -> Result<ScheduleDefinition, String> {
    serde_json::from_value(value).map_err(|error| format!("定时任务配置无效：{error}"))
}

fn validate_schedule(schedule: &ScheduleDefinition) -> Result<(), String> {
    if schedule.id.trim().is_empty() {
        return Err("定时任务 ID 不能为空".to_string());
    }
    if schedule.name.trim().is_empty() {
        return Err(format!("定时任务 {} 的名称不能为空", schedule.id));
    }
    parse_clock(&schedule.run_at)
        .ok_or_else(|| format!("任务“{}”的执行时间无效", schedule.name))?;
    if schedule.weekdays.is_empty()
        || schedule
            .weekdays
            .iter()
            .any(|weekday| !(1..=7).contains(weekday))
    {
        return Err(format!("任务“{}”的执行日无效", schedule.name));
    }
    parse_clock(&schedule.start_time)
        .ok_or_else(|| format!("任务“{}”的开始时间无效", schedule.name))?;
    parse_clock(&schedule.end_time)
        .ok_or_else(|| format!("任务“{}”的结束时间无效", schedule.name))?;
    if matches!(schedule.date_mode, ScheduleDateMode::Fixed) {
        NaiveDate::parse_from_str(&schedule.fixed_date, "%Y-%m-%d")
            .map_err(|_| format!("任务“{}”的固定日期无效", schedule.name))?;
    }
    if schedule.groups.is_empty()
        || schedule
            .groups
            .iter()
            .any(|group| group.id.trim().is_empty())
    {
        return Err(format!("任务“{}”至少需要一个有效群聊", schedule.name));
    }
    if schedule.auto_sync_smart_sheet && !schedule.run_analysis {
        return Err(format!(
            "任务“{}”自动同步 Smart Sheet 前必须启用大模型分析",
            schedule.name
        ));
    }
    if schedule.auto_sync_smart_sheet && !schedule.prepare_smart_sheet {
        return Err(format!(
            "任务“{}”自动同步 Smart Sheet 前必须启用 Smart Sheet",
            schedule.name
        ));
    }
    if schedule.auto_sync_smart_sheet && schedule.smart_sheet_template_id.trim().is_empty() {
        return Err(format!(
            "任务“{}”自动同步 Smart Sheet 前必须冻结腾讯文档模板",
            schedule.name
        ));
    }
    if schedule.prepare_smart_sheet && !schedule.run_analysis {
        return Err(format!(
            "任务“{}”准备 Smart Sheet 前必须启用大模型分析",
            schedule.name
        ));
    }
    if schedule.prepare_smart_sheet && schedule.smart_sheet_template_id.trim().is_empty() {
        return Err(format!(
            "任务“{}”准备 Smart Sheet 前必须冻结腾讯文档模板",
            schedule.name
        ));
    }
    Ok(())
}

fn validate_auto_sync_template_references(
    schedules: &[ScheduleDefinition],
    current_config: &Value,
) -> Result<(), String> {
    let template_ids = configured_template_ids(current_config);
    for schedule in schedules
        .iter()
        .filter(|schedule| schedule.auto_sync_smart_sheet)
    {
        let template_id = schedule.smart_sheet_template_id.trim();
        if !template_ids.contains(template_id) {
            return Err(format!(
                "任务“{}”自动同步引用了不存在的腾讯文档模板“{}”",
                schedule.name, template_id
            ));
        }
    }
    Ok(())
}

fn parse_clock(value: &str) -> Option<u32> {
    if value.len() != 5 || value.as_bytes().get(2) != Some(&b':') {
        return None;
    }
    let (hour, minute) = value.split_once(':')?;
    let hour: u32 = hour.parse().ok()?;
    let minute: u32 = minute.parse().ok()?;
    (hour < 24 && minute < 60).then_some(hour * 60 + minute)
}

fn is_due(schedule: &ScheduleDefinition, weekday: u32, minute: &str) -> bool {
    schedule.enabled && schedule.run_at == minute && schedule.weekdays.contains(&weekday)
}

fn resolve_export_date(schedule: &ScheduleDefinition, today: NaiveDate) -> Result<String, String> {
    let date = match schedule.date_mode {
        ScheduleDateMode::Today => today,
        ScheduleDateMode::Yesterday => today
            .pred_opt()
            .ok_or_else(|| "无法计算前一天日期".to_string())?,
        ScheduleDateMode::Fixed => NaiveDate::parse_from_str(&schedule.fixed_date, "%Y-%m-%d")
            .map_err(|_| format!("固定日期无效：{}", schedule.fixed_date))?,
    };
    Ok(date.format("%Y-%m-%d").to_string())
}

fn resolve_export_range(
    schedule: &ScheduleDefinition,
    today: NaiveDate,
) -> Result<(String, String), String> {
    let start_date_text = resolve_export_date(schedule, today)?;
    let start_date = NaiveDate::parse_from_str(&start_date_text, "%Y-%m-%d")
        .map_err(|_| format!("开始日期无效：{start_date_text}"))?;
    let start_clock = parse_clock(&schedule.start_time)
        .ok_or_else(|| format!("开始时间无效：{}", schedule.start_time))?;
    let end_clock = parse_clock(&schedule.end_time)
        .ok_or_else(|| format!("结束时间无效：{}", schedule.end_time))?;
    let end_date = if end_clock < start_clock {
        start_date
            .succ_opt()
            .ok_or_else(|| "无法计算跨天任务的结束日期".to_string())?
    } else {
        start_date
    };
    Ok((
        start_date.format("%Y-%m-%d").to_string(),
        end_date.format("%Y-%m-%d").to_string(),
    ))
}

fn schedule_state_path() -> Result<PathBuf, String> {
    let config_path = config::config_path()?;
    let directory = config_path
        .parent()
        .ok_or_else(|| "配置文件路径没有父目录".to_string())?;
    Ok(directory.join("schedule-state.json"))
}

pub fn list_schedule_execution_history(
    page: usize,
    page_size: usize,
    schedule_id: Option<String>,
) -> Result<ScheduleExecutionHistoryPage, String> {
    validate_history_pagination(page, page_size)?;
    let _guard = STATE_FILE_LOCK
        .lock()
        .map_err(|_| "定时任务状态锁已损坏".to_string())?;
    let path = schedule_state_path()?;
    let state = load_state(&path)?;
    paginate_schedule_execution_history(&state, page, page_size, schedule_id.as_deref())
}

fn validate_history_pagination(page: usize, page_size: usize) -> Result<(), String> {
    if page == 0 {
        return Err("执行记录页码必须从 1 开始".to_string());
    }
    if !(1..=MAX_SCHEDULE_HISTORY_PAGE_SIZE).contains(&page_size) {
        return Err(format!(
            "执行记录每页数量必须在 1 到 {MAX_SCHEDULE_HISTORY_PAGE_SIZE} 之间"
        ));
    }
    Ok(())
}

fn paginate_schedule_execution_history(
    state: &ScheduleState,
    page: usize,
    page_size: usize,
    schedule_id: Option<&str>,
) -> Result<ScheduleExecutionHistoryPage, String> {
    validate_history_pagination(page, page_size)?;
    let schedule_id = schedule_id.map(str::trim).filter(|value| !value.is_empty());
    let mut matches = state
        .execution_history
        .iter()
        .filter(|item| schedule_id.is_none_or(|id| item.schedule_id == id))
        .cloned()
        .collect::<Vec<_>>();
    // Executions may finish concurrently and acquire the state lock out of order.
    // Sort by the recorded completion time instead of relying on append order.
    matches.sort_by(|left, right| {
        right
            .finished_at
            .cmp(&left.finished_at)
            .then_with(|| right.execution_id.cmp(&left.execution_id))
    });
    let total = matches.len();
    let total_pages = if total == 0 {
        0
    } else {
        total.div_ceil(page_size)
    };
    let start = (page - 1).saturating_mul(page_size);
    let items = matches.into_iter().skip(start).take(page_size).collect();
    Ok(ScheduleExecutionHistoryPage {
        items,
        page,
        page_size,
        total,
        total_pages,
    })
}

pub fn list_pending_smart_sheet_syncs() -> Result<Vec<PendingScheduleSync>, String> {
    let _guard = STATE_FILE_LOCK
        .lock()
        .map_err(|_| "定时任务状态锁已损坏".to_string())?;
    let path = schedule_state_path()?;
    Ok(load_state(&path)?.pending_smart_sheet_syncs)
}

pub fn save_config_preserving_task_references(
    config_value: Value,
) -> Result<config::BootstrapPayload, String> {
    save_config_with_task_reference_guard(
        config_value,
        false,
        config::save_config_preserving_schedules,
    )
}

pub fn import_config_backup(path: &Path) -> Result<config::BootstrapPayload, String> {
    let config_value = config::read_config_backup(path)?;
    validate_imported_config(&config_value)?;
    save_config_with_task_reference_guard(config_value, true, config::save_config)
}

fn save_config_with_task_reference_guard(
    config_value: Value,
    replaces_schedules: bool,
    save: impl FnOnce(Value) -> Result<config::BootstrapPayload, String>,
) -> Result<config::BootstrapPayload, String> {
    let in_flight = IN_FLIGHT_REFERENCES
        .lock()
        .map_err(|_| "运行中任务引用锁已损坏".to_string())?;
    let pending_syncs = list_pending_smart_sheet_syncs()?;
    if let Some(reference) = missing_pending_template_reference(&config_value, &pending_syncs) {
        let template_label = if reference.template_name.is_empty() {
            reference.template_id.clone()
        } else {
            format!("{}（{}）", reference.template_name, reference.template_id)
        };
        return Err(format!(
            "腾讯文档模板“{template_label}”仍被定时任务“{}”的待确认结果使用，请先完成同步或放弃该结果后再删除模板",
            reference.schedule_name
        ));
    }
    let in_flight_references = in_flight.keys().cloned().collect::<Vec<_>>();
    if let Some(reference) =
        missing_in_flight_template_reference(&config_value, &in_flight_references)
    {
        return Err(format!(
            "腾讯文档模板“{}”仍被{}使用，请等待任务完成后再删除模板",
            reference.template_id, reference.owner_label
        ));
    }
    if replaces_schedules {
        let current = config::load_config(&config::config_path()?)?;
        let current_ids = schedule_ids_from_config(&current);
        let incoming_ids = schedule_ids_from_config(&config_value);
        let protected_ids = protected_schedule_ids(&pending_syncs, &in_flight_references);
        let blocked_ids = protected_schedule_deletions(&current_ids, &incoming_ids, &protected_ids);
        if let Some(reference) = in_flight.keys().find(|reference| {
            reference
                .schedule_id
                .as_ref()
                .is_some_and(|schedule_id| blocked_ids.contains(schedule_id))
        }) {
            return Err(format!(
                "{}，请等待任务完成后再导入配置",
                reference.owner_label
            ));
        }
        if let Some(pending) = pending_syncs
            .iter()
            .find(|pending| blocked_ids.contains(&pending.schedule_id))
        {
            return Err(format!(
                "定时任务“{}”仍有腾讯文档待确认结果，请先同步或放弃后再导入配置",
                pending.schedule_name
            ));
        }
    }
    let saved = save(config_value);
    drop(in_flight);
    saved
}

fn validate_imported_config(config_value: &Value) -> Result<(), String> {
    let prompt_ids: HashSet<String> = config_value
        .get("prompts")
        .and_then(|prompts| prompts.get("items"))
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|prompt| prompt.get("id").and_then(Value::as_str))
        .map(str::trim)
        .filter(|id| !id.is_empty())
        .map(str::to_string)
        .collect();
    let template_ids = configured_template_ids(config_value);
    let mut schedule_ids = HashSet::new();

    for value in config_value
        .get("schedules")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let schedule = parse_schedule(value.clone())?;
        validate_schedule(&schedule)?;
        if !schedule_ids.insert(schedule.id.clone()) {
            return Err(format!("配置备份中的定时任务 ID 重复：{}", schedule.id));
        }
        let prompt_id = schedule.prompt_id.trim();
        if !prompt_id.is_empty() && !prompt_ids.contains(prompt_id) {
            return Err(format!(
                "配置备份中的定时任务“{}”引用了不存在的提示词“{}”",
                schedule.name, prompt_id
            ));
        }
        let template_id = schedule.smart_sheet_template_id.trim();
        if !template_id.is_empty() && !template_ids.contains(template_id) {
            return Err(format!(
                "配置备份中的定时任务“{}”引用了不存在的腾讯文档模板“{}”",
                schedule.name, template_id
            ));
        }
    }
    Ok(())
}

fn configured_template_ids(config: &Value) -> HashSet<String> {
    config
        .get("smart_sheet")
        .and_then(|smart_sheet| smart_sheet.get("templates"))
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|template| template.get("id").and_then(Value::as_str))
        .map(str::trim)
        .filter(|template_id| !template_id.is_empty())
        .map(str::to_string)
        .collect()
}

fn missing_in_flight_template_reference(
    config: &Value,
    references: &[InFlightReferenceKey],
) -> Option<InFlightReferenceKey> {
    let configured_template_ids = configured_template_ids(config);
    references
        .iter()
        .find(|reference| {
            !reference.template_id.is_empty()
                && !configured_template_ids.contains(&reference.template_id)
        })
        .cloned()
}

fn missing_pending_template_reference(
    config: &Value,
    pending_syncs: &[PendingScheduleSync],
) -> Option<PendingTemplateReference> {
    let available_template_ids = configured_template_ids(config);

    pending_template_references(pending_syncs)
        .into_iter()
        .find(|reference| !available_template_ids.contains(reference.template_id.as_str()))
}

fn pending_template_references(
    pending_syncs: &[PendingScheduleSync],
) -> Vec<PendingTemplateReference> {
    let mut seen = HashSet::new();
    let mut references = Vec::new();
    for pending in pending_syncs {
        let runs = pending
            .result
            .get("runs")
            .and_then(Value::as_array)
            .filter(|runs| !runs.is_empty());
        let candidates = runs
            .map(|runs| runs.iter().collect::<Vec<_>>())
            .unwrap_or_else(|| vec![&pending.result]);
        for run in candidates {
            let preview = run.get("smartSheetPreview");
            let has_pending_records = preview
                .and_then(|value| value.get("pending"))
                .and_then(Value::as_u64)
                .is_some_and(|pending| pending > 0);
            if !has_pending_records {
                continue;
            }
            let template_id = run
                .get("smartSheetTemplateId")
                .and_then(Value::as_str)
                .or_else(|| {
                    preview
                        .and_then(|value| value.get("template_id"))
                        .and_then(Value::as_str)
                })
                .map(str::trim)
                .unwrap_or_default();
            if template_id.is_empty() || !seen.insert((pending.pending_id.as_str(), template_id)) {
                continue;
            }
            let template_name = run
                .get("smartSheetTemplateName")
                .and_then(Value::as_str)
                .or_else(|| {
                    preview
                        .and_then(|value| value.get("template_name"))
                        .and_then(Value::as_str)
                })
                .map(str::trim)
                .unwrap_or_default();
            references.push(PendingTemplateReference {
                schedule_name: pending.schedule_name.clone(),
                template_id: template_id.to_string(),
                template_name: template_name.to_string(),
            });
        }
    }
    references
}

pub fn clear_pending_smart_sheet_syncs(pending_ids: Vec<String>) -> Result<(), String> {
    if pending_ids.is_empty() {
        return Ok(());
    }
    let pending_ids = pending_ids.into_iter().collect::<HashSet<_>>();
    let _guard = STATE_FILE_LOCK
        .lock()
        .map_err(|_| "定时任务状态锁已损坏".to_string())?;
    let path = schedule_state_path()?;
    let mut state = load_state(&path)?;
    if clear_pending_in_state(&mut state, &pending_ids) {
        save_state(&path, &state)?;
    }
    Ok(())
}

fn clear_pending_in_state(state: &mut ScheduleState, pending_ids: &HashSet<String>) -> bool {
    let previous_len = state.pending_smart_sheet_syncs.len();
    state
        .pending_smart_sheet_syncs
        .retain(|pending| !pending_ids.contains(&pending.pending_id));
    state.pending_smart_sheet_syncs.len() != previous_len
}

fn persist_schedule_execution_history(
    schedule: &ScheduleDefinition,
    trigger: ScheduleExecutionTrigger,
    started_at: DateTime<Utc>,
    success: bool,
    status: ScheduleExecutionStatus,
    message: &str,
    result: Option<&Value>,
) -> Result<(), String> {
    let _guard = STATE_FILE_LOCK
        .lock()
        .map_err(|_| "定时任务状态锁已损坏".to_string())?;
    let path = schedule_state_path()?;
    let mut state = load_state(&path)?;
    push_schedule_execution_history(
        &mut state,
        schedule,
        trigger,
        started_at,
        Utc::now(),
        success,
        status,
        message,
        result,
    );
    save_state(&path, &state)
}

#[allow(clippy::too_many_arguments)]
fn push_schedule_execution_history(
    state: &mut ScheduleState,
    schedule: &ScheduleDefinition,
    trigger: ScheduleExecutionTrigger,
    started_at: DateTime<Utc>,
    finished_at: DateTime<Utc>,
    success: bool,
    status: ScheduleExecutionStatus,
    message: &str,
    result: Option<&Value>,
) {
    let id_prefix = format!("{}:{}", schedule.id, started_at.timestamp_micros());
    let mut execution_id = id_prefix.clone();
    let mut suffix = 2;
    while state
        .execution_history
        .iter()
        .any(|item| item.execution_id == execution_id)
    {
        execution_id = format!("{id_prefix}:{suffix}");
        suffix += 1;
    }
    state.execution_history.push(ScheduleExecutionHistoryItem {
        execution_id,
        schedule_id: schedule.id.clone(),
        schedule_name: schedule.name.clone(),
        trigger,
        started_at: started_at.to_rfc3339_opts(SecondsFormat::Millis, true),
        finished_at: finished_at.to_rfc3339_opts(SecondsFormat::Millis, true),
        success,
        status,
        message: message.to_string(),
        result: result.cloned(),
    });
    state.execution_history.sort_by(|left, right| {
        left.finished_at
            .cmp(&right.finished_at)
            .then_with(|| left.execution_id.cmp(&right.execution_id))
    });
    let excess = state
        .execution_history
        .len()
        .saturating_sub(MAX_SCHEDULE_EXECUTION_HISTORY);
    if excess > 0 {
        state.execution_history.drain(..excess);
    }
}

fn persist_pending_smart_sheet_sync(
    schedule: &ScheduleDefinition,
    result: Value,
) -> Result<(), String> {
    let _guard = STATE_FILE_LOCK
        .lock()
        .map_err(|_| "定时任务状态锁已损坏".to_string())?;
    let path = schedule_state_path()?;
    let mut state = load_state(&path)?;
    push_pending_smart_sheet_sync(&mut state, schedule, result);
    save_state(&path, &state)
}

fn push_pending_smart_sheet_sync(
    state: &mut ScheduleState,
    schedule: &ScheduleDefinition,
    result: Value,
) {
    let now = Utc::now();
    let id_prefix = format!("{}:{}", schedule.id, now.timestamp_micros());
    let mut pending_id = id_prefix.clone();
    let mut suffix = 2;
    while state
        .pending_smart_sheet_syncs
        .iter()
        .any(|pending| pending.pending_id == pending_id)
    {
        pending_id = format!("{id_prefix}:{suffix}");
        suffix += 1;
    }
    state.pending_smart_sheet_syncs.push(PendingScheduleSync {
        pending_id,
        schedule_id: schedule.id.clone(),
        schedule_name: schedule.name.clone(),
        created_at: now.to_rfc3339_opts(SecondsFormat::Millis, true),
        result,
    });
}

fn result_has_pending_smart_sheet_records(result: &Value) -> bool {
    fn preview_has_pending(value: &Value) -> bool {
        value
            .get("smartSheetPreview")
            .and_then(|preview| preview.get("pending"))
            .and_then(Value::as_u64)
            .is_some_and(|pending| pending > 0)
    }

    preview_has_pending(result)
        || result
            .get("runs")
            .and_then(Value::as_array)
            .is_some_and(|runs| runs.iter().any(preview_has_pending))
}

fn claim_automatic_run(schedule_id: &str, date: &str, minute: &str) -> Result<bool, String> {
    let _guard = STATE_FILE_LOCK
        .lock()
        .map_err(|_| "定时任务状态锁已损坏".to_string())?;
    let path = schedule_state_path()?;
    let mut state = load_state(&path)?;
    if !claim_in_state(&mut state, schedule_id, date, minute) {
        return Ok(false);
    }
    save_state(&path, &state)?;
    Ok(true)
}

fn claim_in_state(state: &mut ScheduleState, schedule_id: &str, date: &str, minute: &str) -> bool {
    if state
        .last_runs
        .get(schedule_id)
        .and_then(|slot| slot.split_once(' '))
        .is_some_and(|(run_date, _)| run_date == date)
    {
        return false;
    }
    state
        .last_runs
        .insert(schedule_id.to_string(), format!("{date} {minute}"));
    true
}

fn load_state(path: &Path) -> Result<ScheduleState, String> {
    if !path.exists() {
        return Ok(ScheduleState::default());
    }
    let text = fs::read_to_string(path)
        .map_err(|error| format!("读取定时任务状态失败（{}）：{error}", path.display()))?;
    serde_json::from_str(&text).map_err(|error| format!("定时任务状态文件无效：{error}"))
}

fn save_state(path: &Path, state: &ScheduleState) -> Result<(), String> {
    let directory = path
        .parent()
        .ok_or_else(|| "定时任务状态路径没有父目录".to_string())?;
    fs::create_dir_all(directory).map_err(|error| format!("创建配置目录失败：{error}"))?;
    let mut file = tempfile::NamedTempFile::new_in(directory)
        .map_err(|error| format!("创建临时状态文件失败：{error}"))?;
    serde_json::to_writer_pretty(&mut file, state)
        .map_err(|error| format!("生成定时任务状态失败：{error}"))?;
    file.write_all(b"\n")
        .map_err(|error| format!("写入定时任务状态失败：{error}"))?;
    file.as_file()
        .sync_all()
        .map_err(|error| format!("保存定时任务状态失败：{error}"))?;
    file.persist(path)
        .map_err(|error| format!("替换定时任务状态失败：{}", error.error))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_schedule() -> ScheduleDefinition {
        ScheduleDefinition {
            id: "weekday_export".to_string(),
            name: "工作日导出".to_string(),
            enabled: true,
            run_at: "18:30".to_string(),
            weekdays: vec![1, 2, 3, 4, 5],
            date_mode: ScheduleDateMode::Today,
            fixed_date: "2026-07-20".to_string(),
            start_time: "00:00".to_string(),
            end_time: "23:59".to_string(),
            groups: vec![TaskGroup {
                id: "R:group".to_string(),
                name: "产品群".to_string(),
            }],
            prompt_id: "daily".to_string(),
            smart_sheet_template_id: "default".to_string(),
            run_ocr: true,
            run_analysis: true,
            export_xlsx: true,
            export_markdown: true,
            prepare_smart_sheet: true,
            auto_sync_smart_sheet: false,
        }
    }

    fn utc_time(value: &str) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(value)
            .expect("test timestamp is valid")
            .with_timezone(&Utc)
    }

    fn sample_history_item(
        execution_id: &str,
        schedule_id: &str,
        finished_at: &str,
    ) -> ScheduleExecutionHistoryItem {
        ScheduleExecutionHistoryItem {
            execution_id: execution_id.to_string(),
            schedule_id: schedule_id.to_string(),
            schedule_name: format!("任务 {schedule_id}"),
            trigger: ScheduleExecutionTrigger::Manual,
            started_at: "2026-07-27T01:00:00.000Z".to_string(),
            finished_at: finished_at.to_string(),
            success: true,
            status: ScheduleExecutionStatus::Success,
            message: "任务执行完成".to_string(),
            result: Some(json!({ "status": "success", "runs": [] })),
        }
    }

    #[test]
    fn serializes_frontend_camel_case_contract() {
        let value = serde_json::to_value(sample_schedule()).expect("schedule serializes");
        assert_eq!(value["runAt"], "18:30");
        assert_eq!(value["dateMode"], "today");
        assert_eq!(value["prepareSmartSheet"], Value::Bool(true));
        assert_eq!(value["autoSyncSmartSheet"], Value::Bool(false));
        assert_eq!(value["smartSheetTemplateId"], "default");
    }

    #[test]
    fn automatic_smart_sheet_sync_defaults_to_false_for_existing_schedules() {
        let mut existing = serde_json::to_value(sample_schedule()).expect("schedule serializes");
        existing
            .as_object_mut()
            .expect("schedule object")
            .remove("autoSyncSmartSheet");
        let parsed = parse_schedule(existing).expect("existing schedule remains compatible");
        let serialized = serde_json::to_value(parsed).expect("schedule serializes");

        assert_eq!(serialized["autoSyncSmartSheet"], Value::Bool(false));
    }

    #[test]
    fn automatic_smart_sheet_sync_requires_preview_generation() {
        let mut value = serde_json::to_value(sample_schedule()).expect("schedule serializes");
        value["autoSyncSmartSheet"] = Value::Bool(true);
        value["prepareSmartSheet"] = Value::Bool(false);
        let parsed = parse_schedule(value).expect("schedule parses");

        let error =
            validate_schedule(&parsed).expect_err("automatic sync needs Smart Sheet preview");

        assert!(error.contains("自动同步"));
        assert!(error.contains("Smart Sheet"));
    }

    #[test]
    fn automatic_smart_sheet_sync_requires_analysis_and_a_frozen_template() {
        let mut schedule = sample_schedule();
        schedule.auto_sync_smart_sheet = true;
        schedule.run_analysis = false;
        let error = validate_schedule(&schedule).expect_err("automatic sync needs analysis");
        assert!(error.contains("自动同步"));
        assert!(error.contains("大模型分析"));

        schedule.run_analysis = true;
        schedule.smart_sheet_template_id = "  ".to_string();
        let error = validate_schedule(&schedule).expect_err("automatic sync needs a template");
        assert!(error.contains("自动同步"));
        assert!(error.contains("模板"));
    }

    #[test]
    fn automatic_smart_sheet_sync_template_must_exist_in_current_config() {
        let mut schedule = sample_schedule();
        schedule.auto_sync_smart_sheet = true;
        let current = json!({
            "smart_sheet": { "templates": [{ "id": "default" }] }
        });
        validate_auto_sync_template_references(&[schedule.clone()], &current)
            .expect("configured template is valid");

        schedule.smart_sheet_template_id = "missing".to_string();
        let error = validate_auto_sync_template_references(&[schedule.clone()], &current)
            .expect_err("missing template must be rejected");
        assert!(error.contains("工作日导出"));
        assert!(error.contains("missing"));

        schedule.auto_sync_smart_sheet = false;
        validate_auto_sync_template_references(&[schedule], &current)
            .expect("manual mode keeps the existing save behavior");
    }

    #[test]
    fn automatic_sync_targets_reuse_the_frozen_preview_contract() {
        let result = json!({
            "status": "success",
            "runs": [{
                "groupId": "sales",
                "groupName": "销售群",
                "status": "success",
                "dayDir": "D:/exports/sales",
                "smartSheetDate": "2026-07-27",
                "smartSheetTemplateId": "daily",
                "definitionPath": "D:/exports/sales/snapshots/issues.json",
                "smartSheetPreview": {
                    "pending": 2,
                    "already_synced": 1,
                    "template_revision": "template-r1",
                    "document_revision": "document-r1"
                }
            }, {
                "groupId": "support",
                "groupName": "客服群",
                "status": "success",
                "dayDir": "D:/exports/support",
                "smartSheetPreview": { "pending": 0, "already_synced": 3 }
            }, {
                "groupId": "failed",
                "groupName": "失败群",
                "status": "failed",
                "dayDir": "D:/exports/failed",
                "smartSheetPreview": { "pending": 9, "already_synced": 0 }
            }]
        });

        let targets = automatic_sync_targets(&result, "D:/config.local.json");

        assert_eq!(targets.len(), 1);
        assert_eq!(targets[0].run_index, 0);
        assert_eq!(targets[0].group_name, "销售群");
        let payload = targets[0]
            .payload
            .as_ref()
            .expect("complete frozen data builds a sync request");
        assert_eq!(payload["configPath"], "D:/config.local.json");
        assert_eq!(payload["dayDir"], "D:/exports/sales");
        assert_eq!(payload["date"], "2026-07-27");
        assert_eq!(payload["templateId"], "daily");
        assert_eq!(
            payload["definitionPath"],
            "D:/exports/sales/snapshots/issues.json"
        );
        assert_eq!(payload["expectedTemplateRevision"], "template-r1");
        assert_eq!(payload["expectedDocumentRevision"], "document-r1");
        assert_eq!(payload["uploadImages"], true);
    }

    #[test]
    fn automatic_sync_success_clears_pending_and_failure_recomputes_multi_group_result() {
        let mut result = json!({
            "status": "success",
            "totalCount": 2,
            "successCount": 2,
            "emptyCount": 0,
            "failedCount": 0,
            "runs": [{
                "groupId": "sales",
                "groupName": "销售群",
                "status": "success",
                "error": "",
                "dayDir": "D:/exports/sales",
                "outputs": { "xlsx": "D:/exports/sales/issues.xlsx" },
                "smartSheetPreview": { "pending": 2, "already_synced": 1 }
            }, {
                "groupId": "support",
                "groupName": "客服群",
                "status": "success",
                "error": "",
                "dayDir": "D:/exports/support",
                "outputs": {},
                "smartSheetPreview": { "pending": 1, "already_synced": 0 }
            }]
        });

        apply_automatic_sync_success(&mut result, 0, &json!({ "total": 3, "synced": 2 }));
        apply_automatic_sync_failure(&mut result, 1, "客服群", "webhook timeout");

        assert_eq!(result["runs"][0]["smartSheetPreview"]["pending"], 0);
        assert_eq!(result["runs"][0]["smartSheetPreview"]["already_synced"], 3);
        assert_eq!(result["runs"][0]["smartSheetSync"]["status"], "success");
        assert_eq!(result["runs"][0]["smartSheetSync"]["synced"], 2);
        assert_eq!(result["runs"][1]["status"], "failed");
        assert!(result["runs"][1]["error"]
            .as_str()
            .expect("failure is readable")
            .contains("客服群"));
        assert_eq!(result["runs"][1]["smartSheetSync"]["status"], "failed");
        assert_eq!(result["status"], "partial");
        assert_eq!(result["totalCount"], 2);
        assert_eq!(result["successCount"], 1);
        assert_eq!(result["emptyCount"], 0);
        assert_eq!(result["failedCount"], 1);

        let completion = automatic_sync_completion(result);
        assert!(
            !completion.success,
            "any automatic sync failure fails the completion"
        );
        assert_eq!(completion.status, ScheduleExecutionStatus::Partial);
        assert!(completion.message.contains("客服群"));
        assert!(completion.result.is_some());
    }

    #[test]
    fn automatic_sync_failure_recomputes_single_group_legacy_summary() {
        let mut result = json!({
            "status": "success",
            "totalCount": 1,
            "successCount": 1,
            "emptyCount": 0,
            "failedCount": 0,
            "groupId": "sales",
            "groupName": "销售群",
            "dayDir": "D:/exports/sales",
            "outputs": {},
            "smartSheetPreview": { "pending": 1, "already_synced": 0 },
            "runs": [{
                "groupId": "sales",
                "groupName": "销售群",
                "status": "success",
                "error": "",
                "dayDir": "D:/exports/sales",
                "outputs": {},
                "smartSheetPreview": { "pending": 1, "already_synced": 0 }
            }]
        });

        apply_automatic_sync_failure(&mut result, 0, "销售群", "invalid revision");

        assert_eq!(result["status"], "failed");
        assert_eq!(result["successCount"], 0);
        assert_eq!(result["failedCount"], 1);
        assert_eq!(result["runs"][0]["status"], "failed");
        assert_eq!(result["smartSheetSync"]["status"], "failed");
        assert!(result["error"]
            .as_str()
            .expect("legacy top-level error is preserved")
            .contains("销售群"));
    }

    #[test]
    fn automatic_sync_success_updates_single_group_legacy_preview_and_completion_message() {
        let mut result = json!({
            "status": "success",
            "totalCount": 1,
            "successCount": 1,
            "emptyCount": 0,
            "failedCount": 0,
            "smartSheetPreview": { "pending": 2, "already_synced": 0 },
            "runs": [{
                "groupId": "sales",
                "groupName": "销售群",
                "status": "success",
                "error": "",
                "dayDir": "D:/exports/sales",
                "outputs": {},
                "smartSheetPreview": { "pending": 2, "already_synced": 0 }
            }]
        });

        apply_automatic_sync_success(&mut result, 0, &json!({ "total": 2, "synced": 2 }));

        assert_eq!(result["smartSheetPreview"]["pending"], 0);
        assert_eq!(result["smartSheetPreview"]["already_synced"], 2);
        assert_eq!(result["status"], "success");
        assert_eq!(result["successCount"], 1);
        assert_eq!(result["failedCount"], 0);
        let completion = automatic_sync_completion(result);
        assert!(completion.success);
        assert!(completion.message.contains("已自动同步"));
    }

    #[test]
    fn automatic_mode_never_queues_manual_confirmation() {
        let result = json!({
            "runs": [{ "smartSheetPreview": { "pending": 2, "already_synced": 0 } }]
        });
        let mut schedule = sample_schedule();
        schedule.auto_sync_smart_sheet = false;
        assert!(should_persist_pending_sync(&schedule, &result));

        schedule.auto_sync_smart_sheet = true;
        assert!(!should_persist_pending_sync(&schedule, &result));
    }

    #[test]
    fn imported_config_validates_schedule_references() {
        let schedule = serde_json::to_value(sample_schedule()).expect("schedule serializes");
        let valid = json!({
            "prompts": { "items": [{ "id": "daily" }] },
            "smart_sheet": { "templates": [{ "id": "default" }] },
            "schedules": [schedule]
        });
        validate_imported_config(&valid).expect("matching references are valid");

        let mut missing_prompt = valid.clone();
        missing_prompt["prompts"]["items"] = json!([{ "id": "another" }]);
        let error = validate_imported_config(&missing_prompt)
            .expect_err("missing prompt must reject the backup");
        assert!(error.contains("不存在的提示词"));

        let mut missing_template = valid.clone();
        missing_template["smart_sheet"]["templates"] = json!([{ "id": "another" }]);
        let error = validate_imported_config(&missing_template)
            .expect_err("missing template must reject the backup");
        assert!(error.contains("不存在的腾讯文档模板"));
    }

    #[test]
    fn due_check_respects_enabled_time_and_weekday() {
        let schedule = sample_schedule();
        assert!(is_due(&schedule, 3, "18:30"));
        assert!(!is_due(&schedule, 6, "18:30"));
        assert!(!is_due(&schedule, 3, "18:31"));
    }

    #[test]
    fn resolves_dynamic_and_fixed_dates() {
        let today = NaiveDate::from_ymd_opt(2026, 7, 23).expect("valid date");
        let mut schedule = sample_schedule();
        assert_eq!(resolve_export_date(&schedule, today).unwrap(), "2026-07-23");
        schedule.date_mode = ScheduleDateMode::Yesterday;
        assert_eq!(resolve_export_date(&schedule, today).unwrap(), "2026-07-22");
        schedule.date_mode = ScheduleDateMode::Fixed;
        schedule.fixed_date = "2026-07-01".to_string();
        assert_eq!(resolve_export_date(&schedule, today).unwrap(), "2026-07-01");
    }

    #[test]
    fn overnight_schedule_resolves_end_on_the_next_calendar_day() {
        let today = NaiveDate::from_ymd_opt(2026, 7, 23).expect("valid date");
        let mut schedule = sample_schedule();
        schedule.start_time = "23:00".to_string();
        schedule.end_time = "01:00".to_string();

        let (start_date, end_date) =
            resolve_export_range(&schedule, today).expect("range resolves");

        assert_eq!(start_date, "2026-07-23");
        assert_eq!(end_date, "2026-07-24");
    }

    #[test]
    fn schedule_worker_request_includes_cross_day_contract() {
        let schedule = sample_schedule();
        let request = build_worker_run_request(
            &schedule,
            "2026-07-23".to_string(),
            "2026-07-24".to_string(),
        );

        assert_eq!(request["date"], "2026-07-23");
        assert_eq!(request["startDate"], "2026-07-23");
        assert_eq!(request["endDate"], "2026-07-24");
        assert_eq!(request["groups"][0]["id"], "R:group");
        assert_eq!(request["smartSheetTemplateId"], "default");
    }

    #[test]
    fn old_schedule_without_template_id_still_deserializes() {
        let mut value = serde_json::to_value(sample_schedule()).expect("schedule serializes");
        value
            .as_object_mut()
            .expect("schedule object")
            .remove("smartSheetTemplateId");

        let parsed = parse_schedule(value).expect("old schedule remains compatible");

        assert_eq!(parsed.smart_sheet_template_id, "");
        assert!(validate_schedule(&parsed).is_err());
    }

    #[test]
    fn state_claim_allows_only_one_automatic_run_per_day() {
        let mut state = ScheduleState::default();
        assert!(claim_in_state(
            &mut state,
            "weekday_export",
            "2026-07-23",
            "18:30"
        ));
        assert!(!claim_in_state(
            &mut state,
            "weekday_export",
            "2026-07-23",
            "18:31"
        ));
        assert!(claim_in_state(
            &mut state,
            "weekday_export",
            "2026-07-24",
            "18:30"
        ));
    }

    #[test]
    fn old_schedule_state_without_pending_syncs_remains_compatible() {
        let state: ScheduleState = serde_json::from_value(json!({
            "lastRuns": { "weekday_export": "2026-07-24 18:30" }
        }))
        .expect("old state deserializes");

        assert_eq!(
            state.last_runs.get("weekday_export").map(String::as_str),
            Some("2026-07-24 18:30")
        );
        assert!(state.pending_smart_sheet_syncs.is_empty());
        assert!(state.execution_history.is_empty());
    }

    #[test]
    fn worker_result_status_controls_completion_success_without_dropping_result() {
        for (status, expected_status, expected_success) in [
            ("success", ScheduleExecutionStatus::Success, true),
            ("partial", ScheduleExecutionStatus::Partial, true),
            ("empty", ScheduleExecutionStatus::Empty, true),
            ("failed", ScheduleExecutionStatus::Failed, false),
        ] {
            let result = json!({ "status": status, "runs": [{ "groupId": "group-a" }] });
            let (actual_status, success, _message) = worker_completion(&result);
            assert_eq!(actual_status, expected_status);
            assert_eq!(success, expected_success);
        }

        let (legacy_status, legacy_success, _) = worker_completion(&json!({ "runs": [] }));
        assert_eq!(legacy_status, ScheduleExecutionStatus::Success);
        assert!(legacy_success);

        let (invalid_status, invalid_success, _) =
            worker_completion(&json!({ "status": "unexpected", "runs": [] }));
        assert_eq!(invalid_status, ScheduleExecutionStatus::Failed);
        assert!(!invalid_success);
    }

    #[test]
    fn execution_history_filters_then_pages_newest_first() {
        let state = ScheduleState {
            execution_history: vec![
                sample_history_item("daily-2", "daily", "2026-07-27T01:03:00.000Z"),
                sample_history_item("daily-1", "daily", "2026-07-27T01:01:00.000Z"),
                sample_history_item("daily-3", "daily", "2026-07-27T01:04:00.000Z"),
                sample_history_item("other-1", "other", "2026-07-27T01:02:00.000Z"),
                sample_history_item("daily-4", "daily", "2026-07-27T01:05:00.000Z"),
            ],
            ..ScheduleState::default()
        };

        let page =
            paginate_schedule_execution_history(&state, 2, 2, Some(" daily ")).expect("valid page");

        assert_eq!(page.page, 2);
        assert_eq!(page.page_size, 2);
        assert_eq!(page.total, 4);
        assert_eq!(page.total_pages, 2);
        assert_eq!(
            page.items
                .iter()
                .map(|item| item.execution_id.as_str())
                .collect::<Vec<_>>(),
            vec!["daily-2", "daily-1"]
        );

        let beyond = paginate_schedule_execution_history(&state, 3, 2, Some("daily"))
            .expect("an out-of-range page is an empty page");
        assert_eq!(beyond.page, 3);
        assert_eq!(beyond.total_pages, 2);
        assert!(beyond.items.is_empty());

        let unfiltered = paginate_schedule_execution_history(&state, 1, 10, Some("   "))
            .expect("blank filters are ignored");
        assert_eq!(unfiltered.total, 5);
    }

    #[test]
    fn execution_history_rejects_invalid_page_arguments() {
        let state = ScheduleState::default();
        assert!(paginate_schedule_execution_history(&state, 0, 10, None).is_err());
        assert!(paginate_schedule_execution_history(&state, 1, 0, None).is_err());
        assert!(paginate_schedule_execution_history(
            &state,
            1,
            MAX_SCHEDULE_HISTORY_PAGE_SIZE + 1,
            None,
        )
        .is_err());
    }

    #[test]
    fn execution_history_is_bounded_without_touching_pending_or_last_runs() {
        let schedule = sample_schedule();
        let mut state = ScheduleState::default();
        state
            .last_runs
            .insert(schedule.id.clone(), "2026-07-27 18:30".to_string());
        state.pending_smart_sheet_syncs.push(PendingScheduleSync {
            pending_id: "weekday_export:pending".to_string(),
            schedule_id: schedule.id.clone(),
            schedule_name: schedule.name.clone(),
            created_at: "2026-07-27T10:30:00.000Z".to_string(),
            result: json!({ "runs": [{ "smartSheetPreview": { "pending": 1 } }] }),
        });
        let base = utc_time("2026-07-27T10:00:00.000Z");

        for index in 0..=MAX_SCHEDULE_EXECUTION_HISTORY {
            let instant = base + chrono::Duration::microseconds(index as i64);
            push_schedule_execution_history(
                &mut state,
                &schedule,
                ScheduleExecutionTrigger::Automatic,
                instant,
                instant,
                true,
                ScheduleExecutionStatus::Success,
                "任务执行完成",
                None,
            );
        }

        assert_eq!(
            state.execution_history.len(),
            MAX_SCHEDULE_EXECUTION_HISTORY
        );
        assert_eq!(
            state.last_runs.get(&schedule.id).map(String::as_str),
            Some("2026-07-27 18:30")
        );
        assert_eq!(state.pending_smart_sheet_syncs.len(), 1);
        assert_eq!(
            state.pending_smart_sheet_syncs[0].pending_id,
            "weekday_export:pending"
        );
        assert_eq!(
            state.execution_history[0].execution_id,
            format!(
                "{}:{}",
                schedule.id,
                (base + chrono::Duration::microseconds(1)).timestamp_micros()
            )
        );

        let history_ids = state
            .execution_history
            .iter()
            .map(|item| item.execution_id.clone())
            .collect::<Vec<_>>();
        assert!(clear_pending_in_state(
            &mut state,
            &HashSet::from(["weekday_export:pending".to_string()]),
        ));
        assert!(state.pending_smart_sheet_syncs.is_empty());
        assert_eq!(
            state
                .execution_history
                .iter()
                .map(|item| item.execution_id.clone())
                .collect::<Vec<_>>(),
            history_ids
        );
    }

    #[test]
    fn execution_history_ids_are_unique_and_serialize_the_public_contract() {
        let schedule = sample_schedule();
        let mut state = ScheduleState::default();
        let started_at = utc_time("2026-07-27T10:00:00.123456Z");
        let finished_at = utc_time("2026-07-27T10:00:05.789Z");
        let result = json!({
            "status": "partial",
            "successCount": 1,
            "failedCount": 1,
            "runs": []
        });

        for trigger in [
            ScheduleExecutionTrigger::Manual,
            ScheduleExecutionTrigger::Automatic,
        ] {
            push_schedule_execution_history(
                &mut state,
                &schedule,
                trigger,
                started_at,
                finished_at,
                true,
                ScheduleExecutionStatus::Partial,
                "任务部分完成，请查看失败群聊",
                Some(&result),
            );
        }

        assert_ne!(
            state.execution_history[0].execution_id,
            state.execution_history[1].execution_id
        );
        assert!(state.execution_history[1].execution_id.ends_with(":2"));
        let serialized =
            serde_json::to_value(&state.execution_history[1]).expect("history serializes");
        assert_eq!(serialized["scheduleId"], schedule.id);
        assert_eq!(serialized["scheduleName"], schedule.name);
        assert_eq!(serialized["trigger"], "automatic");
        assert_eq!(serialized["status"], "partial");
        assert_eq!(serialized["success"], true);
        assert_eq!(serialized["startedAt"], "2026-07-27T10:00:00.123Z");
        assert_eq!(serialized["finishedAt"], "2026-07-27T10:00:05.789Z");
        assert_eq!(serialized["result"], result);

        let round_trip: ScheduleState =
            serde_json::from_value(serde_json::to_value(&state).expect("state serializes"))
                .expect("state round-trips");
        assert_eq!(round_trip.execution_history.len(), 2);
    }

    #[test]
    fn only_newly_deleted_current_schedules_with_pending_results_are_blocked() {
        let ids = |values: &[&str]| {
            values
                .iter()
                .map(|value| (*value).to_string())
                .collect::<HashSet<_>>()
        };
        let current_ids = ids(&["kept", "removed_with_pending", "removed_without_pending"]);
        let incoming_ids = ids(&["kept", "new_schedule"]);
        let pending_ids = ids(&["kept", "removed_with_pending", "already_orphaned"]);

        let blocked = protected_schedule_deletions(&current_ids, &incoming_ids, &pending_ids);

        assert_eq!(blocked, ids(&["removed_with_pending"]));
        assert!(!blocked.contains("already_orphaned"));
        assert!(!blocked.contains("kept"));

        let in_flight_ids = ids(&["removed_without_pending", "unrelated-manual-task"]);
        assert_eq!(
            protected_schedule_deletions(&current_ids, &incoming_ids, &in_flight_ids),
            ids(&["removed_without_pending"])
        );
    }

    #[test]
    fn in_flight_reference_guard_is_counted_until_the_last_run_finishes() {
        let key = InFlightReferenceKey {
            schedule_id: Some("raii-test-schedule".to_string()),
            owner_label: "RAII 测试任务".to_string(),
            template_id: "raii-test-template".to_string(),
        };
        let (first, second) = {
            let mut in_flight = IN_FLIGHT_REFERENCES.lock().expect("registry locks");
            let first = register_in_flight_reference(&mut in_flight, key.clone());
            let second = register_in_flight_reference(&mut in_flight, key.clone());
            assert_eq!(in_flight.get(&key), Some(&2));
            (first, second)
        };

        drop(first);
        assert_eq!(
            IN_FLIGHT_REFERENCES
                .lock()
                .expect("registry locks")
                .get(&key),
            Some(&1)
        );
        drop(second);
        assert!(!IN_FLIGHT_REFERENCES
            .lock()
            .expect("registry locks")
            .contains_key(&key));
    }

    #[test]
    fn schedule_stays_protected_across_in_flight_to_pending_transition() {
        let in_flight = InFlightReferenceKey {
            schedule_id: Some("transitioning".to_string()),
            owner_label: "正在运行的定时任务“过渡测试”".to_string(),
            template_id: "template-a".to_string(),
        };
        let pending = PendingScheduleSync {
            pending_id: "transitioning:1".to_string(),
            schedule_id: "transitioning".to_string(),
            schedule_name: "过渡测试".to_string(),
            created_at: "2026-07-24T12:00:00Z".to_string(),
            result: json!({ "runs": [] }),
        };
        let expected = HashSet::from(["transitioning".to_string()]);

        assert_eq!(
            protected_schedule_ids(&[], std::slice::from_ref(&in_flight)),
            expected
        );
        assert_eq!(
            protected_schedule_ids(
                std::slice::from_ref(&pending),
                std::slice::from_ref(&in_flight),
            ),
            expected
        );
        assert_eq!(
            protected_schedule_ids(std::slice::from_ref(&pending), &[]),
            expected
        );
    }

    #[test]
    fn in_flight_template_reference_blocks_only_actual_template_deletion() {
        let references = vec![
            InFlightReferenceKey {
                schedule_id: None,
                owner_label: "正在运行的手动任务".to_string(),
                template_id: "template-a".to_string(),
            },
            InFlightReferenceKey {
                schedule_id: Some("daily".to_string()),
                owner_label: "正在运行的定时任务“日报”".to_string(),
                template_id: "template-b".to_string(),
            },
            InFlightReferenceKey {
                schedule_id: Some("local-only".to_string()),
                owner_label: "无腾讯模板任务".to_string(),
                template_id: String::new(),
            },
        ];
        let preserving_config = json!({
            "smart_sheet": { "templates": [{ "id": "template-a" }, { "id": "template-b" }] }
        });
        let deleting_b = json!({
            "smart_sheet": { "templates": [{ "id": "template-a" }] }
        });

        assert!(missing_in_flight_template_reference(&preserving_config, &references).is_none());
        assert_eq!(
            missing_in_flight_template_reference(&deleting_b, &references),
            Some(references[1].clone())
        );
    }

    #[test]
    fn pending_frozen_template_must_remain_in_saved_config() {
        let pending_syncs = vec![PendingScheduleSync {
            pending_id: "deleted:1".to_string(),
            schedule_id: "deleted".to_string(),
            schedule_name: "已删除任务".to_string(),
            created_at: "2026-07-24T12:00:00Z".to_string(),
            result: json!({
                "runs": [{
                    "smartSheetTemplateId": "template-a",
                    "smartSheetTemplateName": "冻结模板 A",
                    "smartSheetPreview": {
                        "pending": 2,
                        "already_synced": 0,
                        "template_id": "template-a",
                        "template_name": "冻结模板 A"
                    }
                }, {
                    "smartSheetTemplateId": "already-synced-template",
                    "smartSheetTemplateName": "已同步旧模板",
                    "smartSheetPreview": {
                        "pending": 0,
                        "already_synced": 5,
                        "template_id": "already-synced-template",
                        "template_name": "已同步旧模板"
                    }
                }]
            }),
        }];
        let preserving_config = json!({
            "smart_sheet": { "templates": [{ "id": "template-a" }, { "id": "template-b" }] }
        });
        let deleting_config = json!({
            "smart_sheet": { "templates": [{ "id": "template-b" }] }
        });

        assert!(missing_pending_template_reference(&preserving_config, &pending_syncs).is_none());
        assert_eq!(
            missing_pending_template_reference(&deleting_config, &pending_syncs),
            Some(PendingTemplateReference {
                schedule_name: "已删除任务".to_string(),
                template_id: "template-a".to_string(),
                template_name: "冻结模板 A".to_string(),
            })
        );
    }

    #[test]
    fn pending_preview_is_persisted_with_schedule_context_and_can_be_cleared() {
        let schedule = sample_schedule();
        let result = json!({
            "runs": [{
                "groupId": "R:group",
                "groupName": "产品群",
                "dayDir": "D:/exports/2026-07-24",
                "outputs": {},
                "smartSheetPreview": { "pending": 2, "already_synced": 0 }
            }]
        });
        assert!(result_has_pending_smart_sheet_records(&result));

        let mut state = ScheduleState::default();
        push_pending_smart_sheet_sync(&mut state, &schedule, result.clone());
        push_pending_smart_sheet_sync(&mut state, &schedule, result);

        assert_eq!(state.pending_smart_sheet_syncs.len(), 2);
        assert_ne!(
            state.pending_smart_sheet_syncs[0].pending_id,
            state.pending_smart_sheet_syncs[1].pending_id
        );
        let serialized = serde_json::to_value(&state).expect("state serializes");
        assert_eq!(
            serialized["pendingSmartSheetSyncs"][0]["scheduleId"],
            "weekday_export"
        );
        assert_eq!(
            serialized["pendingSmartSheetSyncs"][0]["scheduleName"],
            "工作日导出"
        );

        let first_id = state.pending_smart_sheet_syncs[0].pending_id.clone();
        assert!(clear_pending_in_state(
            &mut state,
            &HashSet::from([first_id])
        ));
        assert_eq!(state.pending_smart_sheet_syncs.len(), 1);
    }

    #[test]
    fn result_without_pending_preview_is_not_queued_for_confirmation() {
        assert!(!result_has_pending_smart_sheet_records(&json!({
            "runs": [{
                "smartSheetPreview": { "pending": 0, "already_synced": 4 }
            }]
        })));
        assert!(!result_has_pending_smart_sheet_records(&json!({
            "runs": [{ "smartSheetPreview": null }]
        })));
    }
}
