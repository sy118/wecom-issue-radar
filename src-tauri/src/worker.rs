use serde::Serialize;
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use tauri::{AppHandle, Emitter};

use crate::config;

#[derive(Clone, Serialize)]
pub struct ProgressPayload {
    pub message: String,
}

struct LaunchSpec {
    program: PathBuf,
    prefix_args: Vec<String>,
    working_dir: Option<PathBuf>,
}

pub async fn run_worker(app: AppHandle, request: Value) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || run_worker_blocking(&app, request))
        .await
        .map_err(|error| format!("后台任务异常终止：{error}"))?
}

fn run_worker_blocking(app: &AppHandle, request: Value) -> Result<Value, String> {
    let mut request_file = tempfile::Builder::new()
        .prefix("wecom-issue-radar-")
        .suffix(".json")
        .tempfile()
        .map_err(|error| format!("创建任务请求失败：{error}"))?;
    serde_json::to_writer(&mut request_file, &request)
        .map_err(|error| format!("生成任务请求失败：{error}"))?;
    request_file
        .flush()
        .map_err(|error| format!("写入任务请求失败：{error}"))?;

    let spec = resolve_launch_spec()?;
    let mut command = Command::new(&spec.program);
    command
        .args(&spec.prefix_args)
        .arg("--request")
        .arg(request_file.path());
    if let Some(directory) = &spec.working_dir {
        command.current_dir(directory);
    }
    command
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUTF8", "1");
    if let Ok(config_path) = config::config_path() {
        command.env("WECOM_ISSUE_RADAR_CONFIG", config_path);
    }
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    #[cfg(windows)]
    apply_no_window(&mut command);

    let mut child = command
        .spawn()
        .map_err(|error| format!("无法启动处理引擎（{}）：{error}", spec.program.display()))?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "无法读取处理引擎输出".to_string())?;
    let mut stderr = child
        .stderr
        .take()
        .ok_or_else(|| "无法读取处理引擎错误输出".to_string())?;
    let stderr_thread = std::thread::spawn(move || {
        let mut text = String::new();
        let _ = stderr.read_to_string(&mut text);
        text
    });

    let mut result: Option<Value> = None;
    let mut reported_error: Option<String> = None;
    for line in BufReader::new(stdout).lines() {
        let line = line.map_err(|error| format!("读取处理进度失败：{error}"))?;
        if line.trim().is_empty() {
            continue;
        }
        let Ok(event) = serde_json::from_str::<Value>(&line) else {
            let _ = app.emit(
                "pipeline-progress",
                ProgressPayload {
                    message: line.trim().to_string(),
                },
            );
            continue;
        };
        match event.get("type").and_then(Value::as_str) {
            Some("progress") => {
                if let Some(message) = event.get("message").and_then(Value::as_str) {
                    let _ = app.emit(
                        "pipeline-progress",
                        ProgressPayload {
                            message: message.to_string(),
                        },
                    );
                }
            }
            Some("result") => result = event.get("data").cloned(),
            Some("error") => {
                let message = event
                    .get("message")
                    .and_then(Value::as_str)
                    .unwrap_or("处理引擎执行失败");
                let detail = event.get("detail").and_then(Value::as_str).unwrap_or("");
                reported_error = Some(if detail.is_empty() {
                    message.to_string()
                } else {
                    format!("{message}\n{detail}")
                });
            }
            _ => {}
        }
    }

    let status = child
        .wait()
        .map_err(|error| format!("等待处理引擎结束失败：{error}"))?;
    let stderr_text = stderr_thread.join().unwrap_or_default();
    if let Some(error) = reported_error {
        return Err(error);
    }
    if !status.success() {
        let detail = stderr_text.trim();
        return Err(if detail.is_empty() {
            format!("处理引擎退出，状态码：{}", status.code().unwrap_or(-1))
        } else {
            format!("处理引擎执行失败：{detail}")
        });
    }
    result.ok_or_else(|| "处理引擎没有返回结果".to_string())
}

pub fn launch_key_extraction() -> Result<(), String> {
    let spec = resolve_launch_spec()?;
    let mut command = Command::new(&spec.program);
    command.args(&spec.prefix_args).arg("--extract-keys");
    if let Some(directory) = &spec.working_dir {
        command.current_dir(directory);
    }
    command
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUTF8", "1");
    if let Ok(config_path) = config::config_path() {
        command.env("WECOM_ISSUE_RADAR_CONFIG", config_path);
    }
    #[cfg(windows)]
    apply_new_console(&mut command);
    command
        .spawn()
        .map_err(|error| format!("启动密钥提取窗口失败：{error}"))?;
    Ok(())
}

fn resolve_launch_spec() -> Result<LaunchSpec, String> {
    if let Some(directory) = std::env::current_exe()
        .ok()
        .and_then(|executable| executable.parent().map(Path::to_path_buf))
    {
        let bundled = directory.join(worker_executable_name());
        if bundled.is_file() {
            return Ok(LaunchSpec {
                program: bundled,
                prefix_args: Vec::new(),
                working_dir: Some(directory),
            });
        }
    }

    let project_root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .ok_or_else(|| "无法定位项目目录".to_string())?
        .to_path_buf();
    let windows_python = project_root
        .join(".venv")
        .join("Scripts")
        .join("python.exe");
    let python = if windows_python.is_file() {
        windows_python
    } else {
        PathBuf::from("python")
    };
    let worker_script = project_root.join("worker").join("main.py");
    if !worker_script.is_file() {
        return Err("找不到 worker/main.py 或已打包的处理引擎".to_string());
    }
    Ok(LaunchSpec {
        program: python,
        prefix_args: vec![worker_script.to_string_lossy().into_owned()],
        working_dir: Some(project_root),
    })
}

#[cfg(windows)]
fn worker_executable_name() -> &'static str {
    "wecom-issue-radar-worker.exe"
}

#[cfg(not(windows))]
fn worker_executable_name() -> &'static str {
    "wecom-issue-radar-worker"
}

#[cfg(windows)]
fn apply_no_window(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    command.creation_flags(0x0800_0000);
}

#[cfg(windows)]
fn apply_new_console(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    command.creation_flags(0x0000_0010);
}

pub fn request(action: &str, payload: Value) -> Value {
    json!({ "action": action, "payload": payload })
}
