//! Reuse one idle inference process while keeping each job independently cancellable.
use std::collections::{BTreeMap, HashSet};
use std::ffi::OsString;
use std::io::Write;
use std::process::{ChildStdin, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicI32, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::Value;
use tauri::{AppHandle, Emitter, Manager, State};

use crate::error::{AppError, AppResult};
use crate::python::protocol::WorkerEnvelope;
use crate::session_log;
use crate::state::{AppState, SharedChild};

use super::{
    acquire_application_operation, acquire_worker_runtime_access, build_worker_command,
    emit_batch_stderr, emit_task_error_details_to_all, emit_task_error_to_all,
    forward_python_terminal_log, is_background_terminal_event, is_rocm_offload_arch_diagnostic,
    log_worker_event_with_scope,
    log_worker_parse_error, mark_task_terminal, native_fault_status, native_signal_failure,
    native_worker_failure,
    read_lossy_lines, registered_task_ids, runtime_idle_release_event, runtime_release_event,
    take_cancelled_tasks, terminate_worker, worker_error_message, BackgroundTask, BACKGROUND_TASKS,
    RUNTIME_ACCESS,
};

#[derive(PartialEq, Eq)]
struct WorkerIdentity {
    program: OsString,
    args: Vec<OsString>,
    env: BTreeMap<OsString, Option<OsString>>,
    activation: Option<Vec<u8>>,
}

impl WorkerIdentity {
    fn from_command(cmd: &Command) -> Self {
        let env: BTreeMap<_, _> = cmd
            .get_envs()
            .map(|(key, value)| (key.to_owned(), value.map(|value| value.to_owned())))
            .collect();
        let activation = env
            .get(std::ffi::OsStr::new("PYMSS_STUDIO_ACTIVE_RUNTIME_FILE"))
            .and_then(Option::as_deref)
            .and_then(|path| std::fs::read(path).ok());
        Self {
            program: cmd.get_program().to_owned(),
            args: cmd.get_args().map(|arg| arg.to_owned()).collect(),
            env,
            activation,
        }
    }
}

struct WarmSession {
    identity: WorkerIdentity,
    child: SharedChild,
    stdin: ChildStdin,
    lines: Receiver<String>,
    stderr_task_ids: Arc<Mutex<Vec<String>>>,
    stderr_fault: Arc<AtomicI32>,
    stderr_drained: Receiver<()>,
}

static IDLE_SESSION: Mutex<Option<WarmSession>> = Mutex::new(None);

impl WarmSession {
    fn exited(&self) -> AppResult<bool> {
        let mut child = self
            .child
            .lock()
            .map_err(|_| AppError::Worker("worker process lock poisoned".into()))?;
        Ok(child.try_wait()?.is_some())
    }

    fn stop(&self) -> AppResult<ExitStatus> {
        let mut child = self
            .child
            .lock()
            .map_err(|_| AppError::Worker("worker process lock poisoned".into()))?;
        Ok(terminate_worker(&mut child)?)
    }

    fn set_stderr_tasks(&self, task_ids: &[String]) {
        if let Ok(mut current) = self.stderr_task_ids.lock() {
            current.clear();
            current.extend_from_slice(task_ids);
        }
    }
}

impl Drop for WarmSession {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

pub(super) fn retire_idle_session() -> AppResult<()> {
    if let Some(session) = take_idle_session()? {
        session.stop()?;
    }
    Ok(())
}

fn take_idle_session() -> AppResult<Option<WarmSession>> {
    Ok(IDLE_SESSION
        .lock()
        .map_err(|_| AppError::Worker("warm worker slot lock poisoned".into()))?
        .take())
}

fn open_session(app: &AppHandle, operation_id: &str) -> AppResult<WarmSession> {
    let mut cmd = build_worker_command(app, "serve", None, None, operation_id)?;
    let identity = WorkerIdentity::from_command(&cmd);
    if let Some(session) = take_idle_session()? {
        if session.identity == identity && !session.exited()? {
            return Ok(session);
        }
        session.stop()?;
    }
    #[cfg(unix)]
    super::process_tree::configure_worker(&mut cmd);
    cmd.stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = cmd.spawn()?;
    let (Some(stdin), Some(stdout), Some(stderr)) =
        (child.stdin.take(), child.stdout.take(), child.stderr.take())
    else {
        let _ = terminate_worker(&mut child);
        return Err(AppError::Worker("missing inference worker pipes".into()));
    };
    let stderr_task_ids = Arc::new(Mutex::new(Vec::new()));
    let stderr_fault = Arc::new(AtomicI32::new(0));
    let (stderr_done, stderr_drained) = mpsc::channel();
    let ids_for_thread: Arc<Mutex<Vec<String>>> = Arc::clone(&stderr_task_ids);
    let fault_for_thread = Arc::clone(&stderr_fault);
    let stderr_app = app.clone();
    std::thread::spawn(move || {
        read_lossy_lines(stderr, |line| {
            if forward_python_terminal_log(&stderr_app, &line) {
                return;
            }
            if let Some(status) = native_fault_status(&line) {
                fault_for_thread.store(status, Ordering::Release);
            }
            let task_ids = ids_for_thread
                .lock()
                .map(|ids| ids.clone())
                .unwrap_or_default();
            emit_batch_stderr(&stderr_app, &task_ids, line);
        });
        let _ = stderr_done.send(());
    });
    let (tx, lines) = mpsc::channel();
    std::thread::spawn(move || {
        read_lossy_lines(stdout, |line| {
            if !line.trim().is_empty() {
                let _ = tx.send(line);
            }
        });
    });
    Ok(WarmSession {
        identity,
        child: Arc::new(Mutex::new(child)),
        stdin,
        lines,
        stderr_task_ids,
        stderr_fault,
        stderr_drained,
    })
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
    let release_event = runtime_access
        .as_ref()
        .and_then(|_| runtime_release_event("infer", &payload));
    let registered_ids = registered_task_ids(&task_id, &payload);
    let launch = Arc::new(tauri::async_runtime::Mutex::new(()));
    let launching = launch.try_lock().expect("new inference launch lock");
    reserve_job(&state, &registered_ids, Arc::clone(&launch))?;
    let session = open_session(&app, &operation_id).and_then(|session| {
        session.set_stderr_tasks(&registered_ids);
        let mut tasks = state
            .tasks
            .lock()
            .map_err(|_| AppError::Worker("task registry lock poisoned".into()))?;
        for id in &registered_ids {
            tasks.insert(id.clone(), Arc::clone(&session.child));
        }
        Ok(session)
    });
    let mut session = match session {
        Ok(session) => session,
        Err(error) => {
            unregister_job(&state, &registered_ids);
            return Err(error);
        }
    };
    drop(launching);
    let started_at = Instant::now();
    std::thread::spawn(move || {
        let mut terminal = HashSet::new();
        let mut last_event = None;
        let outcome = collect_job(
            &mut session,
            &app,
            &task_id,
            &operation_id,
            &payload,
            &registered_ids,
            &mut terminal,
            &mut last_event,
        );
        let cancelled = unregister_job(&app.state::<AppState>(), &registered_ids);
        terminal.extend(cancelled.iter().cloned());
        let missing = registered_ids
            .into_iter()
            .filter(|id| !terminal.contains(id))
            .collect::<Vec<_>>();
        let reusable = matches!(&outcome, Ok(0))
            && missing.is_empty()
            && cancelled.is_empty()
            && session.stderr_fault.load(Ordering::Acquire) == 0;
        if reusable {
            session.set_stderr_tasks(&[]);
            // Retain one idle model only when no other runtime job is still running.
            // Runtime maintenance can retire it once this job releases its lease.
            let mut idle = IDLE_SESSION.lock().unwrap_or_else(|err| err.into_inner());
            if RUNTIME_ACCESS.lock().is_ok_and(|access| access.users == 1) && idle.is_none() {
                *idle = Some(session);
            } else {
                drop(idle);
                drop(session);
            }
        } else {
            let status = session.stop().ok();
            let _ = session.stderr_drained.recv_timeout(Duration::from_secs(1));
            if !missing.is_empty() {
                let exit_code = status.and_then(|status| status.code());
                let fault = session.stderr_fault.load(Ordering::Acquire);
                if let Some(message) =
                    native_worker_failure(exit_code, (fault != 0).then_some(fault))
                        .or_else(|| status.as_ref().and_then(native_signal_failure))
                {
                    emit_task_error_details_to_all(
                        &app,
                        &missing,
                        message,
                        Some("WORKER_NATIVE_CRASH"),
                        serde_json::json!({ "exitCode": exit_code }),
                    );
                } else {
                    let message = match &outcome {
                        Ok(0) => "Separation worker finished without a result".into(),
                        Ok(code) => format!("Separation worker exited with code {code}"),
                        Err(error) => error.to_string(),
                    };
                    emit_task_error_to_all(&app, &missing, message);
                }
            }
            drop(session);
        }
        drop(runtime_access);
        drop(application_access);
        if let Some(event) = last_event {
            let _ = app.emit("pymss://worker-event", &event);
        }
        if let Some(event) = runtime_idle_release_event(&RUNTIME_ACCESS, release_event) {
            let _ = app.emit("pymss://worker-event", event);
        }
        session_log::append(
            &app,
            "INFO",
            "worker.exit",
            vec![
                ("command", "infer".into()),
                ("operationId", operation_id),
                ("taskId", task_id),
                ("cancelled", (!cancelled.is_empty()).to_string()),
                (
                    "status",
                    outcome
                        .map(|code| code.to_string())
                        .unwrap_or_else(|error| error.to_string()),
                ),
                ("durationMs", started_at.elapsed().as_millis().to_string()),
            ],
        );
    });
    Ok(())
}

fn reserve_job(
    state: &AppState,
    ids: &[String],
    launch: Arc<tauri::async_runtime::Mutex<()>>,
) -> AppResult<()> {
    let tasks = state
        .tasks
        .lock()
        .map_err(|_| AppError::Worker("task registry lock poisoned".into()))?;
    let mut background = BACKGROUND_TASKS
        .get_or_init(Default::default)
        .lock()
        .map_err(|_| AppError::Worker("background command registry lock poisoned".into()))?;
    if let Some(id) = ids
        .iter()
        .find(|id| tasks.contains_key(*id) || background.contains_key(*id))
    {
        return Err(AppError::Worker(format!("task already exists: {id}")));
    }
    let record = Arc::new(BackgroundTask {
        command: "infer".into(),
        runtime_tree: None,
        completion: None,
        inference_launch: Some(launch),
    });
    for id in ids {
        background.insert(id.clone(), Arc::clone(&record));
    }
    Ok(())
}

fn unregister_job(state: &AppState, ids: &[String]) -> Vec<String> {
    // Keep registration and cancellation atomic with respect to returning the child to the idle slot.
    let mut tasks = state.tasks.lock().unwrap_or_else(|err| err.into_inner());
    let cancelled = take_cancelled_tasks(&state.cancelled_tasks, ids);
    if let Some(registry) = BACKGROUND_TASKS.get() {
        if let Ok(mut registry) = registry.lock() {
            for id in ids {
                registry.remove(id);
            }
        }
    }
    if let Ok(mut terminal) = state.terminal_tasks.lock() {
        for id in ids {
            terminal.remove(id);
        }
    }
    for id in ids {
        tasks.remove(id);
    }
    cancelled
}

fn validate_request_event(envelope: &WorkerEnvelope, operation_id: &str) -> AppResult<()> {
    match envelope.request_id.as_deref() {
        Some(id) if id == operation_id => Ok(()),
        None if envelope.event_type == "error" => {
            Err(AppError::Worker(worker_error_message(envelope)))
        }
        _ => Err(AppError::Worker(
            "Separation worker returned an event for another request".into(),
        )),
    }
}

fn collect_job(
    session: &mut WarmSession,
    app: &AppHandle,
    task_id: &str,
    operation_id: &str,
    payload: &Value,
    registered_ids: &[String],
    terminal: &mut HashSet<String>,
    last_event: &mut Option<WorkerEnvelope>,
) -> AppResult<i64> {
    // Output written after the previous request finished belongs to no task of this request.
    while let Ok(stale) = session.lines.try_recv() {
        if !stale.trim().is_empty() {
            emit_batch_stderr(app, &[], stale);
        }
    }
    let message =
        serde_json::json!({ "requestId": operation_id, "command": "infer", "payload": payload });
    writeln!(session.stdin, "{message}")?;
    session.stdin.flush()?;
    loop {
        let line = match session.lines.recv_timeout(Duration::from_millis(250)) {
            Ok(line) => line,
            Err(RecvTimeoutError::Timeout) if !session.exited()? => continue,
            Err(_) => {
                return Err(AppError::Worker(
                    "Separation worker exited before finishing the request".into(),
                ))
            }
        };
        let mut envelope = match serde_json::from_str::<WorkerEnvelope>(&line) {
            Ok(envelope) => envelope,
            // The ROCm SDK launcher writes these diagnostics to stdout while a model loads.
            Err(_) if is_rocm_offload_arch_diagnostic(&line) => {
                emit_batch_stderr(app, registered_ids, line);
                continue;
            }
            Err(error) => {
                log_worker_parse_error(app, "infer", Some(task_id), operation_id, &error, &line);
                return Err(AppError::Worker(format!("Invalid worker event: {error}")));
            }
        };
        validate_request_event(&envelope, operation_id)?;
        if envelope.event_type == "command_finished" {
            return envelope
                .payload
                .get("code")
                .and_then(Value::as_i64)
                .ok_or_else(|| {
                    AppError::Worker("Separation worker returned an invalid exit code".into())
                });
        }
        if is_background_terminal_event("infer", &envelope.event_type) {
            if let Some(id) = envelope
                .task_id
                .as_ref()
                .filter(|id| registered_ids.contains(id))
            {
                terminal.insert(id.clone());
                mark_task_terminal(app, id);
            }
        }
        log_worker_event_with_scope(
            app,
            "infer",
            &mut envelope,
            Some(operation_id),
            Some(task_id),
        );
        if is_background_terminal_event("infer", &envelope.event_type)
            && terminal.len() == registered_ids.len()
            && last_event.is_none()
        {
            // The frontend can immediately dispatch the next job after the last terminal event.
            *last_event = Some(envelope);
        } else {
            let _ = app.emit("pymss://worker-event", &envelope);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{reserve_job, unregister_job, validate_request_event, AppState, WorkerIdentity};
    use crate::python::worker::background_inference_launch;
    use serde_json::json;
    use std::process::Command;
    use std::sync::Arc;

    #[test]
    fn warm_session_identity_changes_with_the_runtime_overlay() {
        let mut cmd = Command::new("python");
        cmd.arg("worker.py")
            .arg("serve")
            .env("PYTHONPATH", "first-overlay");
        let first = WorkerIdentity::from_command(&cmd);
        assert!(first == WorkerIdentity::from_command(&cmd));
        cmd.env("PYTHONPATH", "second-overlay");
        assert!(first != WorkerIdentity::from_command(&cmd));
    }

    #[test]
    fn warm_session_reports_unscoped_protocol_errors_without_accepting_other_frames() {
        let mut envelope = serde_json::from_value(json!({
            "type": "error", "payload": { "message": "Missing inference request ID" }
        }))
        .unwrap();
        assert!(validate_request_event(&envelope, "current")
            .unwrap_err()
            .to_string()
            .contains("Missing inference request ID"));
        envelope.request_id = Some("previous".into());
        assert!(validate_request_event(&envelope, "current")
            .unwrap_err()
            .to_string()
            .contains("another request"));
        assert!(validate_request_event(&envelope, "previous").is_ok());
    }

    #[test]
    fn warm_launch_reserves_batch_ids_before_a_process_exists() {
        let state = AppState::new();
        let ids = [
            "warm-launch-root".to_owned(),
            "warm-launch-member".to_owned(),
        ];
        let launch = Arc::new(tauri::async_runtime::Mutex::new(()));
        let starting = launch.try_lock().unwrap();
        reserve_job(&state, &ids, Arc::clone(&launch)).unwrap();
        assert!(state.tasks.lock().unwrap().is_empty());
        let member_launch = background_inference_launch(&ids[1]).unwrap();
        assert!(Arc::ptr_eq(&member_launch, &launch));
        assert!(member_launch.try_lock().is_err());
        assert!(reserve_job(
            &state,
            &ids[1..],
            Arc::new(tauri::async_runtime::Mutex::new(()))
        )
        .is_err());
        drop(starting);
        assert!(member_launch.try_lock().is_ok());
        unregister_job(&state, &ids);
        assert!(background_inference_launch(&ids[1]).is_none());
    }
}
