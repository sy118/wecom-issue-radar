use crossbeam_channel::{Receiver, Sender};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter};

use crate::worker;

const RESTART_DELAY: Duration = Duration::from_millis(100);
const SHUTDOWN_GRACE: Duration = Duration::from_secs(2);

type CommandFactory = Arc<dyn Fn() -> Result<Command, ReplyRuntimeError> + Send + Sync>;

trait RuntimeEventSink: Send + Sync {
    fn emit(&self, payload: Value);
}

struct TauriEventSink {
    app: AppHandle,
}

impl RuntimeEventSink for TauriEventSink {
    fn emit(&self, payload: Value) {
        let _ = self.app.emit("reply-runtime-event", payload);
    }
}

#[cfg(test)]
struct NoopEventSink;

#[cfg(test)]
impl RuntimeEventSink for NoopEventSink {
    fn emit(&self, _payload: Value) {}
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ReplyRuntimeError {
    pub code: String,
    pub message: String,
    #[serde(default)]
    pub retryable: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub details: Option<Value>,
}

impl ReplyRuntimeError {
    fn new(code: &str, message: &str, retryable: bool) -> Self {
        Self {
            code: code.to_string(),
            message: message.to_string(),
            retryable,
            details: None,
        }
    }

    fn unavailable(message: &str) -> Self {
        Self::new("RUNTIME_UNAVAILABLE", message, true)
    }

    fn protocol(message: &str) -> Self {
        Self::new("RUNTIME_PROTOCOL_ERROR", message, true)
    }
}

#[derive(Clone)]
pub struct ReplyRuntime {
    inner: Arc<ReplyRuntimeInner>,
}

struct ReplyRuntimeInner {
    control: Sender<Control>,
    next_id: AtomicU64,
    stopping: AtomicBool,
    update_preparing: AtomicBool,
    request_gate: Mutex<()>,
    supervisor: Mutex<Option<JoinHandle<()>>>,
}

enum Control {
    Request {
        id: String,
        operation: &'static str,
        payload: Value,
        response: Sender<Result<Value, ReplyRuntimeError>>,
    },
    Shutdown {
        acknowledged: Sender<()>,
    },
    Pause {
        acknowledged: Sender<Result<(), ReplyRuntimeError>>,
    },
    Resume {
        acknowledged: Sender<Result<(), ReplyRuntimeError>>,
    },
}

enum WorkerOutput {
    Message(Value),
    Failed(ReplyRuntimeError),
    Eof,
}

enum SupervisorAction {
    Restart,
    Pause,
    Shutdown,
}

impl ReplyRuntime {
    pub fn start(app: AppHandle) -> Self {
        Self::spawn_supervisor(
            Arc::new(TauriEventSink { app }),
            Arc::new(|| {
                worker::reply_runtime_command()
                    .map_err(|_| ReplyRuntimeError::unavailable("无法定位回复运行时处理引擎"))
            }),
        )
    }

    #[cfg(test)]
    fn with_command_factory(
        event_sink: Arc<dyn RuntimeEventSink>,
        command_factory: CommandFactory,
    ) -> Self {
        Self::spawn_supervisor(event_sink, command_factory)
    }

    fn spawn_supervisor(
        event_sink: Arc<dyn RuntimeEventSink>,
        command_factory: CommandFactory,
    ) -> Self {
        let (control, receiver) = crossbeam_channel::unbounded();
        let supervisor = thread::Builder::new()
            .name("reply-runtime-supervisor".to_string())
            .spawn(move || supervisor_loop(receiver, event_sink, command_factory))
            .expect("reply runtime supervisor thread should start");
        Self {
            inner: Arc::new(ReplyRuntimeInner {
                control,
                next_id: AtomicU64::new(1),
                stopping: AtomicBool::new(false),
                update_preparing: AtomicBool::new(false),
                request_gate: Mutex::new(()),
                supervisor: Mutex::new(Some(supervisor)),
            }),
        }
    }

    pub async fn execute(&self, command: Value) -> Result<Value, ReplyRuntimeError> {
        self.request("execute", command).await
    }

    pub async fn query(&self, query: Value) -> Result<Value, ReplyRuntimeError> {
        self.request("query", query).await
    }

    pub async fn ensure_idle_for_update(&self) -> Result<(), ReplyRuntimeError> {
        let snapshot = self
            .request_for_update(
                "query",
                json!({
                    "protocolVersion": 1,
                    "body": { "kind": "runtime.snapshot" }
                }),
            )
            .await?;
        let active = required_snapshot_count(&snapshot, "activeRetrievals")?;
        let queued = required_snapshot_count(&snapshot, "queuedRetrievals")?;
        if active == 0 && queued == 0 {
            return Ok(());
        }

        let mut error = ReplyRuntimeError::new(
            "RUNTIME_BUSY",
            &format!(
                "回复运行时仍有 {active} 个检索中任务和 {queued} 个排队任务，请等待完成后再安装更新"
            ),
            true,
        );
        error.details = Some(json!({
            "activeRetrievals": active,
            "queuedRetrievals": queued,
        }));
        Err(error)
    }

    pub async fn prepare_for_update(&self) -> Result<(), ReplyRuntimeError> {
        {
            let _request_gate = self
                .inner
                .request_gate
                .lock()
                .map_err(|_| ReplyRuntimeError::unavailable("回复运行时更新请求锁不可用"))?;
            if self.inner.update_preparing.swap(true, Ordering::AcqRel) {
                return Err(ReplyRuntimeError::new(
                    "RUNTIME_BUSY",
                    "回复运行时已经在准备应用更新",
                    true,
                ));
            }
        }

        let result = async {
            self.ensure_idle_for_update().await?;
            self.ensure_background_listeners_stopped_for_update()
                .await?;
            self.pause_for_update()
        }
        .await;
        if result.is_err() {
            self.inner.update_preparing.store(false, Ordering::Release);
        }
        result
    }

    async fn ensure_background_listeners_stopped_for_update(
        &self,
    ) -> Result<(), ReplyRuntimeError> {
        let response = self
            .request_for_update(
                "query",
                json!({
                    "protocolVersion": 1,
                    "body": { "kind": "listener.list" }
                }),
            )
            .await?;
        let listeners = response
            .get("listeners")
            .and_then(Value::as_array)
            .ok_or_else(|| ReplyRuntimeError::protocol("回复运行时未返回有效的监听器列表"))?;
        let ready_listeners = listeners
            .iter()
            .filter(|listener| listener_can_start_background_work(listener))
            .collect::<Vec<_>>();
        if ready_listeners.is_empty() {
            return Ok(());
        }

        let names = ready_listeners
            .iter()
            .filter_map(|listener| listener.get("name").and_then(Value::as_str))
            .take(5)
            .collect::<Vec<_>>();
        let mut error = ReplyRuntimeError::new(
            "RUNTIME_BUSY",
            "群监听仍在运行。请先在“群监听回复”中停用监听器，待检索完成后再安装更新",
            true,
        );
        error.details = Some(json!({
            "activeListeners": ready_listeners.len(),
            "listenerNames": names,
        }));
        Err(error)
    }

    async fn request(
        &self,
        operation: &'static str,
        payload: Value,
    ) -> Result<Value, ReplyRuntimeError> {
        self.request_with_update_access(operation, payload, false)
            .await
    }

    async fn request_for_update(
        &self,
        operation: &'static str,
        payload: Value,
    ) -> Result<Value, ReplyRuntimeError> {
        self.request_with_update_access(operation, payload, true)
            .await
    }

    async fn request_with_update_access(
        &self,
        operation: &'static str,
        payload: Value,
        allow_during_update: bool,
    ) -> Result<Value, ReplyRuntimeError> {
        let request_gate = if allow_during_update {
            None
        } else {
            Some(
                self.inner
                    .request_gate
                    .lock()
                    .map_err(|_| ReplyRuntimeError::unavailable("回复运行时请求锁不可用"))?,
            )
        };
        if self.inner.stopping.load(Ordering::Acquire) {
            return Err(ReplyRuntimeError::new(
                "RUNTIME_STOPPED",
                "回复运行时已经停止",
                false,
            ));
        }
        if !allow_during_update && self.inner.update_preparing.load(Ordering::Acquire) {
            return Err(ReplyRuntimeError::new(
                "RUNTIME_PAUSED",
                "回复运行时正在准备应用更新",
                true,
            ));
        }
        let id = self
            .inner
            .next_id
            .fetch_add(1, Ordering::Relaxed)
            .to_string();
        let (response, receiver) = crossbeam_channel::bounded(1);
        self.inner
            .control
            .send(Control::Request {
                id,
                operation,
                payload,
                response,
            })
            .map_err(|_| ReplyRuntimeError::unavailable("回复运行时监督线程不可用"))?;
        drop(request_gate);

        tauri::async_runtime::spawn_blocking(move || {
            receiver
                .recv()
                .unwrap_or_else(|_| Err(ReplyRuntimeError::unavailable("回复运行时连接已断开")))
        })
        .await
        .map_err(|_| ReplyRuntimeError::unavailable("等待回复运行时响应的任务异常终止"))?
    }

    pub fn shutdown(&self) {
        if self.inner.stopping.swap(true, Ordering::AcqRel) {
            return;
        }
        let (acknowledged, receiver) = crossbeam_channel::bounded(1);
        let _ = self.inner.control.send(Control::Shutdown { acknowledged });
        let _ = receiver.recv_timeout(SHUTDOWN_GRACE);
        if let Ok(mut supervisor) = self.inner.supervisor.lock() {
            if let Some(supervisor) = supervisor.take() {
                let _ = supervisor.join();
            }
        }
    }

    pub fn pause_for_update(&self) -> Result<(), ReplyRuntimeError> {
        self.lifecycle_control(|acknowledged| Control::Pause { acknowledged })
    }

    pub fn resume_after_update(&self) -> Result<(), ReplyRuntimeError> {
        let result = self.lifecycle_control(|acknowledged| Control::Resume { acknowledged });
        if result.is_ok() {
            self.inner.update_preparing.store(false, Ordering::Release);
        }
        result
    }

    fn lifecycle_control(
        &self,
        command: impl FnOnce(Sender<Result<(), ReplyRuntimeError>>) -> Control,
    ) -> Result<(), ReplyRuntimeError> {
        if self.inner.stopping.load(Ordering::Acquire) {
            return Err(ReplyRuntimeError::new(
                "RUNTIME_STOPPED",
                "回复运行时已经停止",
                false,
            ));
        }
        let (acknowledged, receiver) = crossbeam_channel::bounded(1);
        self.inner
            .control
            .send(command(acknowledged))
            .map_err(|_| ReplyRuntimeError::unavailable("回复运行时监督线程不可用"))?;
        receiver
            .recv_timeout(SHUTDOWN_GRACE + Duration::from_secs(1))
            .map_err(|_| ReplyRuntimeError::unavailable("回复运行时生命周期操作超时"))?
    }
}

impl Drop for ReplyRuntimeInner {
    fn drop(&mut self) {
        if !self.stopping.swap(true, Ordering::AcqRel) {
            let (acknowledged, _receiver) = crossbeam_channel::bounded(1);
            let _ = self.control.send(Control::Shutdown { acknowledged });
        }
        if let Ok(supervisor) = self.supervisor.get_mut() {
            if let Some(supervisor) = supervisor.take() {
                let _ = supervisor.join();
            }
        }
    }
}

fn supervisor_loop(
    control: Receiver<Control>,
    event_sink: Arc<dyn RuntimeEventSink>,
    command_factory: CommandFactory,
) {
    let mut last_event_seq = 0_u64;
    loop {
        let mut command = match command_factory() {
            Ok(command) => command,
            Err(error) => {
                match wait_after_launch_failure(&control, error) {
                    SupervisorAction::Shutdown => return,
                    SupervisorAction::Pause => {
                        if wait_while_paused(&control) {
                            return;
                        }
                    }
                    SupervisorAction::Restart => {}
                }
                continue;
            }
        };
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        apply_no_window(&mut command);

        let mut child = match command.spawn() {
            Ok(child) => child,
            Err(_error) => {
                match wait_after_launch_failure(
                    &control,
                    ReplyRuntimeError::unavailable("无法启动回复运行时"),
                ) {
                    SupervisorAction::Shutdown => return,
                    SupervisorAction::Pause => {
                        if wait_while_paused(&control) {
                            return;
                        }
                    }
                    SupervisorAction::Restart => {}
                }
                continue;
            }
        };
        let Some(stdin) = child.stdin.take() else {
            stop_child(&mut child, None);
            continue;
        };
        let Some(stdout) = child.stdout.take() else {
            stop_child(&mut child, Some(stdin));
            continue;
        };
        if let Some(mut stderr) = child.stderr.take() {
            let _ = thread::Builder::new()
                .name("reply-runtime-stderr".to_string())
                .spawn(move || {
                    let _ = std::io::copy(&mut stderr, &mut std::io::sink());
                });
        }
        let (output, outputs) = crossbeam_channel::unbounded();
        let _ = thread::Builder::new()
            .name("reply-runtime-stdout".to_string())
            .spawn(move || read_worker_output(stdout, output));

        match serve_connection(
            &control,
            &outputs,
            &event_sink,
            &mut last_event_seq,
            stdin,
            &mut child,
        ) {
            SupervisorAction::Shutdown => return,
            SupervisorAction::Pause => {
                if wait_while_paused(&control) {
                    return;
                }
            }
            SupervisorAction::Restart => {}
        }
    }
}

fn serve_connection(
    control: &Receiver<Control>,
    outputs: &Receiver<WorkerOutput>,
    event_sink: &Arc<dyn RuntimeEventSink>,
    last_event_seq: &mut u64,
    mut stdin: ChildStdin,
    child: &mut Child,
) -> SupervisorAction {
    let mut pending = HashMap::<String, Sender<Result<Value, ReplyRuntimeError>>>::new();
    loop {
        crossbeam_channel::select! {
            recv(control) -> message => match message {
                Ok(Control::Request { id, operation, payload, response }) => {
                    pending.insert(id.clone(), response);
                    let envelope = json!({ "id": id, "op": operation, "payload": payload });
                    if serde_json::to_writer(&mut stdin, &envelope).is_err()
                        || stdin.write_all(b"\n").is_err()
                        || stdin.flush().is_err()
                    {
                        fail_pending(&mut pending, ReplyRuntimeError::unavailable("无法写入回复运行时"));
                        stop_child(child, Some(stdin));
                        return SupervisorAction::Restart;
                    }
                }
                Ok(Control::Shutdown { acknowledged }) => {
                    fail_pending(
                        &mut pending,
                        ReplyRuntimeError::new("RUNTIME_STOPPED", "应用正在退出", false),
                    );
                    stop_child(child, Some(stdin));
                    let _ = acknowledged.send(());
                    return SupervisorAction::Shutdown;
                }
                Ok(Control::Pause { acknowledged }) => {
                    if pending.is_empty() {
                        stop_child(child, Some(stdin));
                        let _ = acknowledged.send(Ok(()));
                        return SupervisorAction::Pause;
                    }
                    let _ = acknowledged.send(Err(ReplyRuntimeError::new(
                        "RUNTIME_BUSY",
                        "回复运行时仍有请求正在处理",
                        true,
                    )));
                }
                Ok(Control::Resume { acknowledged }) => {
                    let _ = acknowledged.send(Ok(()));
                }
                Err(_) => {
                    fail_pending(
                        &mut pending,
                        ReplyRuntimeError::new("RUNTIME_STOPPED", "应用正在退出", false),
                    );
                    stop_child(child, Some(stdin));
                    return SupervisorAction::Shutdown;
                }
            },
            recv(outputs) -> output => match output {
                Ok(WorkerOutput::Message(message)) => {
                    route_worker_message(message, &mut pending, event_sink, last_event_seq);
                }
                Ok(WorkerOutput::Failed(error)) => {
                    fail_pending(&mut pending, error);
                    stop_child(child, Some(stdin));
                    return SupervisorAction::Restart;
                }
                Ok(WorkerOutput::Eof) | Err(_) => {
                    fail_pending(
                        &mut pending,
                        ReplyRuntimeError::unavailable("回复运行时意外退出"),
                    );
                    stop_child(child, Some(stdin));
                    return SupervisorAction::Restart;
                }
            }
        }
    }
}

fn route_worker_message(
    message: Value,
    pending: &mut HashMap<String, Sender<Result<Value, ReplyRuntimeError>>>,
    event_sink: &Arc<dyn RuntimeEventSink>,
    last_event_seq: &mut u64,
) {
    if message.get("type").and_then(Value::as_str) == Some("event") {
        if let Some(seq) = message.get("seq").and_then(Value::as_u64) {
            if seq > *last_event_seq {
                *last_event_seq = seq;
                let mut safe_message = message;
                redact_event_secrets(&mut safe_message);
                event_sink.emit(safe_message);
            }
        }
        return;
    }
    let Some(id) = response_id(&message) else {
        return;
    };
    let Some(response) = pending.remove(&id) else {
        return;
    };
    let result = match message.get("ok").and_then(Value::as_bool) {
        Some(true) => Ok(message.get("data").cloned().unwrap_or(Value::Null)),
        Some(false) => Err(worker_error(message.get("error"))),
        None => Err(ReplyRuntimeError::protocol(
            "回复运行时响应缺少布尔类型的 ok 字段",
        )),
    };
    let _ = response.send(result);
}

fn redact_event_secrets(value: &mut Value) {
    match value {
        Value::Object(object) => {
            object.retain(|key, _| !is_event_secret_key(key));
            for child in object.values_mut() {
                redact_event_secrets(child);
            }
        }
        Value::Array(items) => {
            for item in items {
                redact_event_secrets(item);
            }
        }
        _ => {}
    }
}

fn is_event_secret_key(key: &str) -> bool {
    let normalized = key
        .chars()
        .filter(|character| character.is_ascii_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect::<String>();
    matches!(
        normalized.as_str(),
        "apikey"
            | "token"
            | "accesstoken"
            | "refreshtoken"
            | "secret"
            | "password"
            | "authorization"
            | "header"
            | "headers"
            | "env"
            | "environment"
            | "webhook"
            | "webhookkey"
    ) || normalized.starts_with("webhookurl")
}

fn response_id(message: &Value) -> Option<String> {
    match message.get("id")? {
        Value::String(id) if !id.is_empty() => Some(id.clone()),
        Value::Number(id) => Some(id.to_string()),
        _ => None,
    }
}

fn worker_error(error: Option<&Value>) -> ReplyRuntimeError {
    let Some(error) = error else {
        return ReplyRuntimeError::protocol("回复运行时返回失败但没有错误信息");
    };
    if let Some(message) = error.as_str() {
        return ReplyRuntimeError::new("RUNTIME_REQUEST_FAILED", message, false);
    }
    let code = error
        .get("code")
        .and_then(Value::as_str)
        .unwrap_or("RUNTIME_REQUEST_FAILED");
    let message = error
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or("回复运行时请求失败");
    ReplyRuntimeError {
        code: code.to_string(),
        message: message.to_string(),
        retryable: error
            .get("retryable")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        details: error.get("details").cloned(),
    }
}

fn required_snapshot_count(snapshot: &Value, field: &str) -> Result<u64, ReplyRuntimeError> {
    snapshot
        .get(field)
        .and_then(Value::as_u64)
        .ok_or_else(|| ReplyRuntimeError::protocol(&format!("回复运行时快照缺少非负整数 {field}")))
}

fn listener_can_start_background_work(listener: &Value) -> bool {
    if !listener
        .get("enabled")
        .and_then(Value::as_bool)
        .unwrap_or(true)
    {
        return false;
    }
    match listener.pointer("/health/status").and_then(Value::as_str) {
        Some("ready") | None => true,
        Some(_) => false,
    }
}

fn read_worker_output(stdout: impl std::io::Read, output: Sender<WorkerOutput>) {
    for line in BufReader::new(stdout).lines() {
        let line = match line {
            Ok(line) => line,
            Err(_) => {
                let _ = output.send(WorkerOutput::Failed(ReplyRuntimeError::protocol(
                    "读取回复运行时输出失败",
                )));
                return;
            }
        };
        if line.trim().is_empty() {
            continue;
        }
        match serde_json::from_str::<Value>(&line) {
            Ok(message) if message.is_object() => {
                if output.send(WorkerOutput::Message(message)).is_err() {
                    return;
                }
            }
            _ => {
                let _ = output.send(WorkerOutput::Failed(ReplyRuntimeError::protocol(
                    "回复运行时输出了无效 NDJSON",
                )));
                return;
            }
        }
    }
    let _ = output.send(WorkerOutput::Eof);
}

fn fail_pending(
    pending: &mut HashMap<String, Sender<Result<Value, ReplyRuntimeError>>>,
    error: ReplyRuntimeError,
) {
    for (_, response) in pending.drain() {
        let _ = response.send(Err(error.clone()));
    }
}

fn wait_after_launch_failure(
    control: &Receiver<Control>,
    error: ReplyRuntimeError,
) -> SupervisorAction {
    let deadline = crossbeam_channel::after(RESTART_DELAY);
    loop {
        crossbeam_channel::select! {
            recv(control) -> message => match message {
                Ok(Control::Request { response, .. }) => {
                    let _ = response.send(Err(error.clone()));
                }
                Ok(Control::Shutdown { acknowledged }) => {
                    let _ = acknowledged.send(());
                    return SupervisorAction::Shutdown;
                }
                Ok(Control::Pause { acknowledged }) => {
                    let _ = acknowledged.send(Ok(()));
                    return SupervisorAction::Pause;
                }
                Ok(Control::Resume { acknowledged }) => {
                    let _ = acknowledged.send(Ok(()));
                }
                Err(_) => return SupervisorAction::Shutdown,
            },
            recv(deadline) -> _ => return SupervisorAction::Restart,
        }
    }
}

fn wait_while_paused(control: &Receiver<Control>) -> bool {
    loop {
        match control.recv() {
            Ok(Control::Request { response, .. }) => {
                let _ = response.send(Err(ReplyRuntimeError::new(
                    "RUNTIME_PAUSED",
                    "回复运行时已为应用更新暂停",
                    true,
                )));
            }
            Ok(Control::Pause { acknowledged }) => {
                let _ = acknowledged.send(Ok(()));
            }
            Ok(Control::Resume { acknowledged }) => {
                let _ = acknowledged.send(Ok(()));
                return false;
            }
            Ok(Control::Shutdown { acknowledged }) => {
                let _ = acknowledged.send(());
                return true;
            }
            Err(_) => return true,
        }
    }
}

fn stop_child(child: &mut Child, stdin: Option<ChildStdin>) {
    drop(stdin);
    let deadline = Instant::now() + SHUTDOWN_GRACE;
    while Instant::now() < deadline {
        match child.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) => thread::sleep(Duration::from_millis(20)),
            Err(_) => return,
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

#[cfg(windows)]
fn apply_no_window(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    command.creation_flags(0x0800_0000);
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::process::Command;
    use std::sync::atomic::{AtomicUsize, Ordering as AtomicOrdering};
    use std::sync::{Arc, Mutex};

    #[derive(Default)]
    struct RecordingEventSink {
        payloads: Mutex<Vec<Value>>,
    }

    impl RuntimeEventSink for RecordingEventSink {
        fn emit(&self, payload: Value) {
            self.payloads.lock().expect("event sink lock").push(payload);
        }
    }

    #[test]
    fn concurrent_execute_and_query_responses_are_matched_by_request_id() {
        let runtime = ReplyRuntime::with_command_factory(
            Arc::new(NoopEventSink),
            Arc::new(|| {
                let mut command = Command::new(test_python());
                command.args([
                    "-u",
                    "-c",
                    r#"
import json, sys
first = json.loads(sys.stdin.readline())
second = json.loads(sys.stdin.readline())
for request in (second, first):
    print(json.dumps({
        "id": request["id"],
        "ok": True,
        "data": {"op": request["op"], "marker": request["payload"]["marker"]},
    }), flush=True)
for _ in sys.stdin:
    pass
"#,
                ]);
                Ok(command)
            }),
        );

        let execute_runtime = runtime.clone();
        let execute = std::thread::spawn(move || {
            tauri::async_runtime::block_on(execute_runtime.execute(json!({ "marker": "execute" })))
        });
        let query_runtime = runtime.clone();
        let query = std::thread::spawn(move || {
            tauri::async_runtime::block_on(query_runtime.query(json!({ "marker": "query" })))
        });

        assert_eq!(
            execute
                .join()
                .expect("execute thread")
                .expect("execute result"),
            json!({ "op": "execute", "marker": "execute" })
        );
        assert_eq!(
            query.join().expect("query thread").expect("query result"),
            json!({ "op": "query", "marker": "query" })
        );
        runtime.shutdown();
    }

    #[test]
    fn only_strictly_monotonic_runtime_events_are_forwarded() {
        let sink = Arc::new(RecordingEventSink::default());
        let runtime = ReplyRuntime::with_command_factory(
            sink.clone(),
            Arc::new(|| {
                let mut command = Command::new(test_python());
                command.args([
                    "-u",
                    "-c",
                    r#"
import json, sys
request = json.loads(sys.stdin.readline())
for seq in (7, 7, 6, 8):
    print(json.dumps({"type": "event", "seq": seq, "event": {"kind": "tick"}}), flush=True)
print(json.dumps({"id": request["id"], "ok": True, "data": {"done": True}}), flush=True)
for _ in sys.stdin:
    pass
"#,
                ]);
                Ok(command)
            }),
        );

        let result = tauri::async_runtime::block_on(runtime.query(json!({ "kind": "snapshot" })))
            .expect("query result");
        assert_eq!(result, json!({ "done": true }));
        assert_eq!(
            sink.payloads
                .lock()
                .expect("event sink lock")
                .iter()
                .filter_map(|payload| payload.get("seq").and_then(Value::as_u64))
                .collect::<Vec<_>>(),
            vec![7, 8]
        );
        runtime.shutdown();
    }

    #[test]
    fn runtime_events_are_redacted_before_they_reach_the_frontend() {
        let sink = Arc::new(RecordingEventSink::default());
        let runtime = ReplyRuntime::with_command_factory(
            sink.clone(),
            Arc::new(|| {
                let mut command = Command::new(test_python());
                command.args([
                    "-u",
                    "-c",
                    r#"
import json, sys
request = json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "event",
    "seq": 1,
    "event": {
        "kind": "listener.changed",
        "webhookUrl": "https://qyapi.weixin.qq.com/private",
        "webhookConfigured": True,
        "headers": {"Authorization": "Bearer private"},
        "env": {"MCP_TOKEN": "private"},
        "api_key": "private-model-key",
        "token": "private-access-token",
        "password": "private-password",
    },
}), flush=True)
print(json.dumps({"id": request["id"], "ok": True, "data": {}}), flush=True)
for _ in sys.stdin:
    pass
"#,
                ]);
                Ok(command)
            }),
        );

        tauri::async_runtime::block_on(runtime.query(json!({ "kind": "snapshot" })))
            .expect("query result");
        let payloads = sink.payloads.lock().expect("event sink lock");
        let event = payloads[0].get("event").expect("event payload");
        assert!(event.get("webhookUrl").is_none());
        assert!(event.get("headers").is_none());
        assert!(event.get("env").is_none());
        assert!(event.get("api_key").is_none());
        assert!(event.get("token").is_none());
        assert!(event.get("password").is_none());
        assert_eq!(event["webhookConfigured"], true);
        drop(payloads);
        runtime.shutdown();
    }

    #[test]
    fn shutdown_closes_worker_stdin_and_allows_a_prompt_graceful_exit() {
        let runtime = ReplyRuntime::with_command_factory(
            Arc::new(NoopEventSink),
            Arc::new(|| {
                let mut command = Command::new(test_python());
                command.args([
                    "-u",
                    "-c",
                    r#"
import json, sys
request = json.loads(sys.stdin.readline())
print(json.dumps({"id": request["id"], "ok": True, "data": {"ready": True}}), flush=True)
for _ in sys.stdin:
    pass
"#,
                ]);
                Ok(command)
            }),
        );
        tauri::async_runtime::block_on(runtime.query(json!({ "kind": "ready" })))
            .expect("runtime is ready");

        let started = Instant::now();
        runtime.shutdown();

        assert!(
            started.elapsed() < Duration::from_secs(1),
            "EOF-aware worker should exit without waiting for the kill grace period"
        );
    }

    #[test]
    fn update_pause_rejects_new_requests_and_resume_starts_the_runtime_again() {
        let runtime = ReplyRuntime::with_command_factory(
            Arc::new(NoopEventSink),
            Arc::new(|| {
                let mut command = Command::new(test_python());
                command.args([
                    "-u",
                    "-c",
                    r#"
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"id": request["id"], "ok": True, "data": {"alive": True}}), flush=True)
"#,
                ]);
                Ok(command)
            }),
        );
        tauri::async_runtime::block_on(runtime.query(json!({ "kind": "before-pause" })))
            .expect("runtime starts");

        runtime.pause_for_update().expect("idle runtime pauses");
        let error = tauri::async_runtime::block_on(runtime.query(json!({ "kind": "paused" })))
            .expect_err("paused runtime rejects requests");
        assert_eq!(error.code, "RUNTIME_PAUSED");

        runtime.resume_after_update().expect("runtime resumes");
        let result =
            tauri::async_runtime::block_on(runtime.query(json!({ "kind": "after-resume" })))
                .expect("runtime restarts");
        assert_eq!(result, json!({ "alive": true }));
        runtime.shutdown();
    }

    #[test]
    fn update_guard_queries_the_runtime_snapshot_and_allows_an_idle_runtime() {
        let runtime = ReplyRuntime::with_command_factory(
            Arc::new(NoopEventSink),
            Arc::new(|| {
                let mut command = Command::new(test_python());
                command.args([
                    "-u",
                    "-c",
                    r#"
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    payload = request["payload"]
    body = payload.get("body", {})
    if body.get("kind") == "runtime.snapshot":
        data = {
            "activeRetrievals": 0 if payload.get("protocolVersion") == 1 else 9,
            "queuedRetrievals": 0,
        }
    else:
        data = {"alive": True}
    print(json.dumps({"id": request["id"], "ok": True, "data": data}), flush=True)
"#,
                ]);
                Ok(command)
            }),
        );

        tauri::async_runtime::block_on(runtime.ensure_idle_for_update())
            .expect("idle snapshot allows update preparation");
        let result = tauri::async_runtime::block_on(runtime.query(json!({ "kind": "health" })))
            .expect("guard does not pause the runtime itself");
        assert_eq!(result, json!({ "alive": true }));
        runtime.shutdown();
    }

    #[test]
    fn update_guard_reports_background_retrievals_and_leaves_runtime_running() {
        let runtime = ReplyRuntime::with_command_factory(
            Arc::new(NoopEventSink),
            Arc::new(|| {
                let mut command = Command::new(test_python());
                command.args([
                    "-u",
                    "-c",
                    r#"
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    body = request["payload"].get("body", {})
    data = (
        {"activeRetrievals": 2, "queuedRetrievals": 1}
        if body.get("kind") == "runtime.snapshot"
        else {"alive": True}
    )
    print(json.dumps({"id": request["id"], "ok": True, "data": data}), flush=True)
"#,
                ]);
                Ok(command)
            }),
        );

        let error = tauri::async_runtime::block_on(runtime.ensure_idle_for_update())
            .expect_err("background retrievals block update preparation");
        assert_eq!(error.code, "RUNTIME_BUSY");
        assert!(error.message.contains("2 个检索中任务"));
        assert_eq!(
            error.details,
            Some(json!({
                "activeRetrievals": 2,
                "queuedRetrievals": 1,
            }))
        );

        let result = tauri::async_runtime::block_on(runtime.query(json!({ "kind": "health" })))
            .expect("busy guard must not pause or stop the runtime");
        assert_eq!(result, json!({ "alive": true }));
        runtime.shutdown();
    }

    #[test]
    fn update_preparation_refuses_a_ready_listener_and_keeps_runtime_available() {
        let runtime = ReplyRuntime::with_command_factory(
            Arc::new(NoopEventSink),
            Arc::new(|| {
                let mut command = Command::new(test_python());
                command.args([
                    "-u",
                    "-c",
                    r#"
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    body = request["payload"].get("body", {})
    if body.get("kind") == "listener.list":
        data = {"listeners": [{"name": "Support", "enabled": True, "health": {"status": "ready"}}]}
    elif body.get("kind") == "runtime.snapshot":
        data = {"activeRetrievals": 0, "queuedRetrievals": 0}
    else:
        data = {"alive": True}
    print(json.dumps({"id": request["id"], "ok": True, "data": data}), flush=True)
"#,
                ]);
                Ok(command)
            }),
        );

        let error = tauri::async_runtime::block_on(runtime.prepare_for_update())
            .expect_err("a ready listener can start retrieval after an idle snapshot");
        assert_eq!(error.code, "RUNTIME_BUSY");
        assert_eq!(
            error.details,
            Some(json!({
                "activeListeners": 1,
                "listenerNames": ["Support"],
            }))
        );

        let result = tauri::async_runtime::block_on(runtime.query(json!({ "kind": "health" })))
            .expect("a rejected preparation reopens normal requests");
        assert_eq!(result, json!({ "alive": true }));
        runtime.shutdown();
    }

    #[test]
    fn update_preparation_reports_active_retrievals_before_ready_listeners() {
        let runtime = ReplyRuntime::with_command_factory(
            Arc::new(NoopEventSink),
            Arc::new(|| {
                let mut command = Command::new(test_python());
                command.args([
                    "-u",
                    "-c",
                    r#"
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    body = request["payload"].get("body", {})
    if body.get("kind") == "listener.list":
        data = {"listeners": [{"name": "Support", "enabled": True, "health": {"status": "ready"}}]}
    elif body.get("kind") == "runtime.snapshot":
        data = {"activeRetrievals": 1, "queuedRetrievals": 2}
    else:
        data = {"alive": True}
    print(json.dumps({"id": request["id"], "ok": True, "data": data}), flush=True)
"#,
                ]);
                Ok(command)
            }),
        );

        let error = tauri::async_runtime::block_on(runtime.prepare_for_update())
            .expect_err("active retrievals take priority over ready listeners");
        assert_eq!(error.code, "RUNTIME_BUSY");
        assert_eq!(
            error.details,
            Some(json!({
                "activeRetrievals": 1,
                "queuedRetrievals": 2,
            }))
        );

        let result = tauri::async_runtime::block_on(runtime.query(json!({ "kind": "health" })))
            .expect("a rejected preparation reopens normal requests");
        assert_eq!(result, json!({ "alive": true }));
        runtime.shutdown();
    }

    #[test]
    fn update_preparation_gates_requests_before_pausing_a_quiescent_runtime() {
        let runtime = ReplyRuntime::with_command_factory(
            Arc::new(NoopEventSink),
            Arc::new(|| {
                let mut command = Command::new(test_python());
                command.args([
                    "-u",
                    "-c",
                    r#"
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    body = request["payload"].get("body", {})
    if body.get("kind") == "listener.list":
        data = {"listeners": [{"name": "Stopped", "enabled": False, "health": {"status": "ready"}}]}
    elif body.get("kind") == "runtime.snapshot":
        data = {"activeRetrievals": 0, "queuedRetrievals": 0}
    else:
        data = {"alive": True}
    print(json.dumps({"id": request["id"], "ok": True, "data": data}), flush=True)
"#,
                ]);
                Ok(command)
            }),
        );

        tauri::async_runtime::block_on(runtime.prepare_for_update())
            .expect("a stopped and idle runtime can be paused safely");
        let error = tauri::async_runtime::block_on(runtime.query(json!({ "kind": "blocked" })))
            .expect_err("normal requests stay gated during installation");
        assert_eq!(error.code, "RUNTIME_PAUSED");

        runtime.resume_after_update().expect("runtime resumes");
        let result = tauri::async_runtime::block_on(runtime.query(json!({ "kind": "health" })))
            .expect("requests resume after a cancelled installation");
        assert_eq!(result, json!({ "alive": true }));
        runtime.shutdown();
    }

    #[test]
    fn worker_errors_keep_their_structured_public_contract() {
        let runtime = ReplyRuntime::with_command_factory(
            Arc::new(NoopEventSink),
            Arc::new(|| {
                let mut command = Command::new(test_python());
                command.args([
                    "-u",
                    "-c",
                    r#"
import json, sys
request = json.loads(sys.stdin.readline())
print(json.dumps({
    "id": request["id"],
    "ok": False,
    "error": {
        "code": "REVISION_CONFLICT",
        "message": "configuration changed",
        "retryable": True,
        "details": {"actualRevision": 9},
    },
}), flush=True)
for _ in sys.stdin:
    pass
"#,
                ]);
                Ok(command)
            }),
        );

        let error = tauri::async_runtime::block_on(runtime.execute(json!({ "kind": "save" })))
            .expect_err("worker rejects stale revision");

        assert_eq!(error.code, "REVISION_CONFLICT");
        assert_eq!(error.message, "configuration changed");
        assert!(error.retryable);
        assert_eq!(error.details, Some(json!({ "actualRevision": 9 })));
        runtime.shutdown();
    }

    #[test]
    fn a_crashed_worker_fails_in_flight_work_and_is_supervised_for_the_next_request() {
        let launches = Arc::new(AtomicUsize::new(0));
        let runtime = ReplyRuntime::with_command_factory(
            Arc::new(NoopEventSink),
            Arc::new({
                let launches = launches.clone();
                move || {
                    let launch = launches.fetch_add(1, AtomicOrdering::SeqCst);
                    let script = if launch == 0 {
                        r#"
import json, sys
json.loads(sys.stdin.readline())
raise SystemExit(17)
"#
                    } else {
                        r#"
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"id": request["id"], "ok": True, "data": {"restarted": True}}), flush=True)
"#
                    };
                    let mut command = Command::new(test_python());
                    command.args(["-u", "-c", script]);
                    Ok(command)
                }
            }),
        );

        let error = tauri::async_runtime::block_on(runtime.query(json!({ "kind": "crash" })))
            .expect_err("in-flight request fails when worker exits");
        assert_eq!(error.code, "RUNTIME_UNAVAILABLE");

        let result = tauri::async_runtime::block_on(runtime.query(json!({ "kind": "next" })))
            .expect("supervisor starts a replacement worker");
        assert_eq!(result, json!({ "restarted": true }));
        assert!(launches.load(AtomicOrdering::SeqCst) >= 2);
        runtime.shutdown();
    }

    #[test]
    fn update_pause_does_not_interrupt_a_long_running_runtime_request() {
        let sink = Arc::new(RecordingEventSink::default());
        let runtime = ReplyRuntime::with_command_factory(
            sink.clone(),
            Arc::new(|| {
                let mut command = Command::new(test_python());
                command.args([
                    "-u",
                    "-c",
                    r#"
import json, sys, time
request = json.loads(sys.stdin.readline())
print(json.dumps({"type": "event", "seq": 1, "event": {"kind": "request.started"}}), flush=True)
time.sleep(0.4)
print(json.dumps({"id": request["id"], "ok": True, "data": {"finished": True}}), flush=True)
for _ in sys.stdin:
    pass
"#,
                ]);
                Ok(command)
            }),
        );
        let request_runtime = runtime.clone();
        let request = std::thread::spawn(move || {
            tauri::async_runtime::block_on(request_runtime.query(json!({ "kind": "slow" })))
        });
        let deadline = Instant::now() + Duration::from_secs(2);
        while sink.payloads.lock().expect("event sink lock").is_empty() {
            assert!(
                Instant::now() < deadline,
                "worker did not start the request"
            );
            std::thread::sleep(Duration::from_millis(10));
        }

        let error = runtime
            .pause_for_update()
            .expect_err("active request blocks update pause");
        assert_eq!(error.code, "RUNTIME_BUSY");
        assert_eq!(
            request
                .join()
                .expect("request thread")
                .expect("request result"),
            json!({ "finished": true })
        );
        runtime.pause_for_update().expect("idle runtime pauses");
        runtime.resume_after_update().expect("runtime resumes");
        runtime.shutdown();
    }

    fn test_python() -> String {
        let project_root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .expect("project root");
        let venv_python = project_root
            .join(".venv")
            .join("Scripts")
            .join("python.exe");
        if venv_python.is_file() {
            venv_python.to_string_lossy().into_owned()
        } else {
            "python".to_string()
        }
    }
}
