//! Keep one inference process alive so the next job can reuse the loaded model.
use std::io::Write;
use std::process::{Child, ChildStdin, Stdio};
use std::sync::atomic::{AtomicI32, Ordering};
use std::sync::mpsc::{self, Receiver};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Instant;

use serde_json::Value;
use tauri::{AppHandle, Emitter, Manager, State};

use crate::error::{AppError, AppResult};
use crate::python::protocol::WorkerEnvelope;
use crate::session_log;
use crate::state::AppState;

use super::{
    acquire_application_operation, acquire_worker_runtime_access, build_worker_command,
    emit_batch_stderr, forward_python_terminal_log, log_worker_event_with_scope,
    native_fault_status, read_lossy_lines, runtime_idle_release_event, runtime_release_event,
    terminate_worker, BackgroundTask, BACKGROUND_TASKS,
};

struct WarmSession {
    python: String,
    child: Arc<Mutex<Child>>,
    stdin: ChildStdin,
    lines: Receiver<String>,
    stderr_fault: Arc<AtomicI32>,
}

static SESSION: OnceLock<Mutex<Option<WarmSession>>> = OnceLock::new();

fn session_slot() -> &'static Mutex<Option<WarmSession>> {
    SESSION.get_or_init(|| Mutex::new(None))
}

fn process_alive(child: &Arc<Mutex<Child>>) -> bool {
    child.lock().ok().and_then(|mut process| process.try_wait().ok()).flatten().is_none()
}

fn stop_session(session: &mut Option<WarmSession>) {
    if let Some(current) = session.take() {
        if let Ok(mut child) = current.child.lock() {
            let _ = terminate_worker(&mut child);
        }
        drop(current.stdin);
    }
}

fn ensure_session(app: &AppHandle, python_hint: &str, slot: &mut Option<WarmSession>) -> AppResult<()> {
    let reusable = slot.as_ref().is_some_and(|session| session.python == python_hint && process_alive(&session.child));
    if reusable {
        return Ok(());
    }
    stop_session(slot);
    let mut cmd = build_worker_command(app, "serve", None, None, "warm-infer")?;
    let python = cmd.get_program().to_string_lossy().into_owned();
    cmd.env("PYMSS_STUDIO_KEEP_MODEL", "1");
    cmd.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = cmd.spawn()?;
    let stdin = child.stdin.take().ok_or_else(|| AppError::Worker("missing warm worker stdin".into()))?;
    let stdout = child.stdout.take().ok_or_else(|| AppError::Worker("missing warm worker stdout".into()))?;
    let stderr = child.stderr.take();
    let stderr_fault = Arc::new(AtomicI32::new(0));
    let stderr_fault_for_thread = Arc::clone(&stderr_fault);
    let stderr_app = app.clone();
    if let Some(stderr) = stderr {
        std::thread::spawn(move || {
            read_lossy_lines(stderr, |line| {
                if forward_python_terminal_log(&stderr_app, &line) {
                    return;
                }
                if let Some(status) = native_fault_status(&line) {
                    stderr_fault_for_thread.store(status, Ordering::Release);
                }
                emit_batch_stderr(&stderr_app, &[], line);
            });
        });
    }
    let (tx, lines) = mpsc::channel();
    std::thread::spawn(move || {
        read_lossy_lines(stdout, |line| {
            if !line.trim().is_empty() {
                let _ = tx.send(line);
            }
        });
    });
    *slot = Some(WarmSession {
        python,
        child: Arc::new(Mutex::new(child)),
        stdin,
        lines,
        stderr_fault,
    });
    Ok(())
}

pub(super) fn spawn_background(
    app: AppHandle,
    state: State<'_, AppState>,
    task_id: String,
    payload: Value,
    operation_id: String,
) -> AppResult<()> {
    let application_access = acquire_application_operation()?;
    let runtime_access = acquire_worker_runtime_access(&app, "infer", &payload)?;
    let release_event = runtime_access.as_ref().and_then(|_| runtime_release_event("infer", &payload));
    let registered_task_ids = vec![task_id.clone()];
    {
        let tasks = state.tasks.lock().map_err(|_| AppError::Worker("task registry lock poisoned".into()))?;
        if tasks.contains_key(&task_id) {
            return Err(AppError::Worker(format!("task already exists: {task_id}")));
        }
    }
    let started_at = Instant::now();
    session_log::append(&app, "DEBUG", "worker.spawn", vec![
        ("command", "infer".into()),
        ("taskId", task_id.clone()),
        ("warm", "1".into()),
    ]);
    std::thread::spawn(move || {
        let _application_access = application_access;
        let _runtime_access = runtime_access;
        let outcome = run_job(&app, &task_id, &operation_id, &payload);
        if let Err(error) = &outcome {
            super::broadcast_existing_task_error(&app, &registered_task_ids, error.to_string(), None);
        }
        if let Some(event) = runtime_idle_release_event(&super::RUNTIME_ACCESS, release_event) {
            let _ = app.emit("pymss://worker-event", event);
        }
        session_log::append(&app, "INFO", "worker.exit", vec![
            ("command", "infer".into()),
            ("operationId", operation_id),
            ("taskId", task_id.clone()),
            ("warm", "1".into()),
            ("status", outcome.unwrap_or_else(|error| error.to_string())),
            ("durationMs", started_at.elapsed().as_millis().to_string()),
        ]);
        let cleanup_state = app.state::<AppState>();
        if let Some(registry) = BACKGROUND_TASKS.get() {
            if let Ok(mut registry) = registry.lock() {
                registry.remove(&task_id);
            }
        }
        if let Ok(mut tasks) = cleanup_state.tasks.lock() {
            tasks.remove(&task_id);
        }
        if let Ok(mut terminal) = cleanup_state.terminal_tasks.lock() {
            terminal.remove(&task_id);
        };
    });
    Ok(())
}

fn run_job(
    app: &AppHandle,
    task_id: &str,
    operation_id: &str,
    payload: &Value,
) -> AppResult<String> {
    let mut slot = session_slot().lock().map_err(|_| AppError::Worker("warm worker lock poisoned".into()))?;
    let python_hint = build_worker_command(app, "serve", None, None, operation_id)?
        .get_program()
        .to_string_lossy()
        .into_owned();
    ensure_session(app, &python_hint, &mut slot)?;
    let session = slot.as_mut().ok_or_else(|| AppError::Worker("warm worker was not started".into()))?;
    let child = Arc::clone(&session.child);
    {
        let state = app.state::<AppState>();
        let mut tasks = state.tasks.lock().map_err(|_| AppError::Worker("task registry lock poisoned".into()))?;
        let mut background = BACKGROUND_TASKS.get_or_init(|| Mutex::new(std::collections::HashMap::new()))
            .lock()
            .map_err(|_| AppError::Worker("background command registry lock poisoned".into()))?;
        let record = Arc::new(BackgroundTask { command: "infer".into(), runtime_tree: None, completion: None });
        background.insert(task_id.to_string(), record);
        tasks.insert(task_id.to_string(), child);
    }
    let message = serde_json::json!({"command": "infer", "payload": payload});
    writeln!(session.stdin, "{message}")?;
    session.stdin.flush()?;
    loop {
        let line = session.lines.recv().map_err(|_| AppError::Worker("warm worker exited while separating".into()))?;
        let mut envelope = match serde_json::from_str::<WorkerEnvelope>(&line) {
            Ok(envelope) => envelope,
            Err(error) => {
                return Err(AppError::Worker(format!("Invalid worker event: {error}; raw={line}")));
            }
        };
        if envelope.event_type == "command_finished" {
            let code = envelope.payload.get("code").and_then(Value::as_i64).unwrap_or(1);
            let fault = session.stderr_fault.load(Ordering::Acquire);
            if fault != 0 {
                stop_session(&mut slot);
                return Err(AppError::Worker(format!("WORKER_NATIVE_CRASH: warm inference fault {fault}")));
            }
            if !process_alive(&session.child) {
                stop_session(&mut slot);
            }
            return if code == 0 {
                Ok("ok".into())
            } else {
                Ok(format!("exit {code}"))
            };
        }
        log_worker_event_with_scope(app, "infer", &mut envelope, Some(operation_id), Some(task_id));
        let _ = app.emit("pymss://worker-event", &envelope);
    }
}
