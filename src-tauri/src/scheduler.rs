use chrono::{Datelike, NaiveDate, Utc};
use chrono_tz::Asia::Shanghai;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;
use tauri::{AppHandle, Emitter};

use crate::{config, worker};

const POLL_INTERVAL: Duration = Duration::from_secs(30);
static STATE_FILE_LOCK: Mutex<()> = Mutex::new(());

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
    pub run_ocr: bool,
    pub run_analysis: bool,
    pub export_xlsx: bool,
    pub export_markdown: bool,
    pub prepare_smart_sheet: bool,
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
}

#[derive(Debug, Default, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct ScheduleState {
    #[serde(default)]
    last_runs: HashMap<String, String>,
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
    let mut ids = HashSet::new();
    for value in &schedules {
        let schedule = parse_schedule(value.clone())?;
        validate_schedule(&schedule)?;
        if !ids.insert(schedule.id.clone()) {
            return Err(format!("定时任务 ID 重复：{}", schedule.id));
        }
    }

    let path = config::config_path()?;
    let mut current = config::load_config(&path)?;
    let root = current
        .as_object_mut()
        .ok_or_else(|| "配置文件根节点必须是对象".to_string())?;
    root.insert("schedules".to_string(), Value::Array(schedules));
    let saved = config::save_config(current)?;
    Ok(saved
        .config
        .get("schedules")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default())
}

pub fn run_schedule_now(app: AppHandle, schedule_id: String) -> Result<(), String> {
    let schedule = find_schedule(&schedule_id)?;
    std::mem::drop(tauri::async_runtime::spawn(async move {
        execute_schedule(app, schedule).await;
    }));
    Ok(())
}

fn tick(app: AppHandle) -> Result<(), String> {
    let now = Utc::now().with_timezone(&Shanghai);
    let today = now.date_naive();
    let date_text = today.format("%Y-%m-%d").to_string();
    let minute = now.format("%H:%M").to_string();
    let weekday = now.weekday().number_from_monday();

    for schedule in valid_schedules()? {
        if !is_due(&schedule, weekday, &minute) {
            continue;
        }
        if !claim_automatic_run(&schedule.id, &date_text, &minute)? {
            continue;
        }
        let task_app = app.clone();
        std::mem::drop(tauri::async_runtime::spawn(async move {
            execute_schedule(task_app, schedule).await;
        }));
    }
    Ok(())
}

async fn execute_schedule(app: AppHandle, schedule: ScheduleDefinition) {
    let today = Utc::now().with_timezone(&Shanghai).date_naive();
    let (start_date, end_date) = match resolve_export_range(&schedule, today) {
        Ok(range) => range,
        Err(error) => {
            emit_completed(&app, &schedule, error, false, None);
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
            emit_completed(&app, &schedule, error, false, None);
            return;
        }
    };
    let request = build_worker_run_request(&schedule, start_date, end_date);
    let worker_request = worker::request(
        "run",
        json!({ "configPath": config_path, "request": request }),
    );
    match worker::run_worker(app.clone(), worker_request).await {
        Ok(result) => emit_completed(
            &app,
            &schedule,
            "任务执行完成".to_string(),
            true,
            Some(result),
        ),
        Err(error) => emit_completed(&app, &schedule, error, false, None),
    }
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
        "runOcr": schedule.run_ocr,
        "runAnalysis": schedule.run_analysis,
        "exportXlsx": schedule.export_xlsx,
        "exportMarkdown": schedule.export_markdown,
        // The run action only prepares a preview. Cloud writes still require the explicit sync action.
        "prepareSmartSheet": schedule.prepare_smart_sheet,
    })
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
        },
    );
}

fn emit_completed(
    app: &AppHandle,
    schedule: &ScheduleDefinition,
    message: String,
    success: bool,
    result: Option<Value>,
) {
    let _ = app.emit(
        "schedule-completed",
        ScheduleEventPayload {
            schedule_id: schedule.id.clone(),
            schedule_name: schedule.name.clone(),
            message,
            success: Some(success),
            result,
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
    if schedule.prepare_smart_sheet && !schedule.run_analysis {
        return Err(format!(
            "任务“{}”准备 Smart Sheet 前必须启用大模型分析",
            schedule.name
        ));
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
            run_ocr: true,
            run_analysis: true,
            export_xlsx: true,
            export_markdown: true,
            prepare_smart_sheet: true,
        }
    }

    #[test]
    fn serializes_frontend_camel_case_contract() {
        let value = serde_json::to_value(sample_schedule()).expect("schedule serializes");
        assert_eq!(value["runAt"], "18:30");
        assert_eq!(value["dateMode"], "today");
        assert_eq!(value["prepareSmartSheet"], Value::Bool(true));
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
}
