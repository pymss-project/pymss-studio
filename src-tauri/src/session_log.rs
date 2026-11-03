use crate::error::{AppError, AppResult};
use crate::automatic_report::{AutomaticReports, PendingReport, ReportAction};
use crate::diagnostic_context::{operation_feature, DiagnosticContext};
use crate::diagnostic_report;
use crate::log_record::LogRecord;
use crate::storage;
use crate::terminal;
use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use tauri::{AppHandle, Emitter, Manager};

const SESSION_LOG_FILE_NAME: &str = "current-session.log";
const PERSISTENT_LOG_FILE_NAME: &str = "pymss-studio.log";
const DIAGNOSTIC_REPORT_FILE_NAME: &str = "latest-diagnostic-report.md";
const MAX_LINE_CHARS: usize = 8 * 1024;
const MAX_LOG_BYTES: u64 = 5 * 1024 * 1024;
const MAX_PERSISTENT_LOG_BYTES: u64 = 10 * 1024 * 1024;
const PERSISTENT_LOG_BACKUPS: usize = 5;
const READ_TAIL_BYTES: u64 = 256 * 1024;
const REPORT_TAIL_BYTES: u64 = 96 * 1024;
const MAX_DIAGNOSTIC_VALUE_CHARS: usize = 4 * 1024;
const MAX_REPORT_STORE_BYTES: u64 = 16 * 1024 * 1024;
const MAX_REPORT_SECTION_CHARS: usize = 64 * 1024;

static LOG_CAP_REACHED: AtomicBool = AtomicBool::new(false);
static FILE_LOG_WARNING: AtomicBool = AtomicBool::new(false);
static DEVELOPER_MODE: AtomicBool = AtomicBool::new(false);
static STARTUP_DEBUG_LOG: AtomicBool = AtomicBool::new(false);
static LOG_WRITE_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
static LOG_SEQUENCE: AtomicU64 = AtomicU64::new(0);
static SESSION_ID: std::sync::Mutex<Option<String>> = std::sync::Mutex::new(None);
static LOG_APP: std::sync::OnceLock<AppHandle> = std::sync::OnceLock::new();
static TORCH_DIAGNOSTICS: std::sync::Mutex<Option<TorchDiagnostics>> = std::sync::Mutex::new(None);
static REPORT_CONTEXT: std::sync::OnceLock<std::sync::Mutex<DiagnosticContext>> = std::sync::OnceLock::new();
static REPORT_WRITE_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
static AUTOMATIC_REPORTS: std::sync::OnceLock<std::sync::Mutex<AutomaticReports>> = std::sync::OnceLock::new();

fn automatic_reports() -> &'static std::sync::Mutex<AutomaticReports> {
    AUTOMATIC_REPORTS.get_or_init(|| std::sync::Mutex::new(AutomaticReports::default()))
}

fn queue_automatic_report(app: &AppHandle, sequence: u64, trigger: &str, line: &str) {
    let start = automatic_reports().lock().map(|mut reports| reports.enqueue(sequence, trigger, line)).unwrap_or(false);
    if !start { return; }
    let handle = app.clone();
    let started = std::thread::Builder::new().name("diagnostic-report".into()).spawn(move || loop {
        let action = match automatic_reports().lock() {
            Ok(mut reports) => reports.next(now_millis()),
            Err(_) => return,
        };
        match action {
            ReportAction::Stop => return,
            ReportAction::Wait(duration) => std::thread::sleep(duration),
            ReportAction::Write(request) => write_automatic_report(&handle, &request, false),
        }
    });
    if started.is_err() {
        if let Ok(mut reports) = automatic_reports().lock() { reports.start_failed(); }
    }
}

fn write_automatic_report(app: &AppHandle, request: &PendingReport, finishing: bool) {
    let Ok(_guard) = REPORT_WRITE_LOCK.lock() else { return; };
    let current = automatic_reports().lock().map(|reports| reports.accepts(request, finishing)).unwrap_or(false);
    if current { let _ = write_diagnostic_report_with_trigger(app, &request.trigger, &request.line, None); }
}

pub(crate) fn finish_automatic_reports(app: &AppHandle) {
    let request = automatic_reports().lock().ok().and_then(|mut reports| reports.finish());
    if let Some(request) = request { write_automatic_report(app, &request, true); }
}

fn report_context() -> &'static std::sync::Mutex<DiagnosticContext> {
    REPORT_CONTEXT.get_or_init(|| std::sync::Mutex::new(DiagnosticContext::default()))
}

#[derive(Clone)]
struct TorchDiagnostics {
    available: bool,
    version: String,
    error: String,
    cuda_available: bool,
    cuda_available_error: String,
    device_count: u64,
    device_count_error: String,
    device_names: Vec<String>,
    device_names_error: String,
}

#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DebugLogInfo {
    path: String,
    logs_dir: String,
    exists: bool,
    size_bytes: u64,
    persistent_path: String,
    persistent_exists: bool,
    persistent_size_bytes: u64,
    max_bytes: u64,
    persistent_max_bytes: u64,
    report_path: String,
    report_exists: bool,
    report_size_bytes: u64,
}

#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DebugLogContent {
    path: String,
    content: String,
    size_bytes: u64,
    truncated: bool,
}

#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DebugLogReport {
    path: String,
    exists: bool,
    size_bytes: u64,
}

pub fn session_log_path(app: &AppHandle) -> AppResult<PathBuf> {
    Ok(storage::logs_dir(app)?.join(SESSION_LOG_FILE_NAME))
}

pub fn persistent_log_path(app: &AppHandle) -> AppResult<PathBuf> {
    Ok(storage::logs_dir(app)?.join(PERSISTENT_LOG_FILE_NAME))
}

pub fn diagnostic_report_path(app: &AppHandle) -> AppResult<PathBuf> {
    Ok(storage::logs_dir(app)?.join(DIAGNOSTIC_REPORT_FILE_NAME))
}

/// Apply process-local logging options before settings or the webview are loaded.
pub fn configure_startup_logging(args: impl IntoIterator<Item = std::ffi::OsString>) -> bool {
    let enabled = args
        .into_iter()
        .take_while(|arg| arg != "--")
        .any(|arg| arg == "--debug");
    STARTUP_DEBUG_LOG.store(enabled, Ordering::Relaxed);
    terminal::set_verbose(enabled);
    enabled
}

pub fn verbose_logging_enabled() -> bool {
    developer_mode_enabled() || STARTUP_DEBUG_LOG.load(Ordering::Relaxed)
}

pub(crate) fn record_operation_request(
    app: &AppHandle,
    command: &str,
    payload: &serde_json::Value,
    task_id: Option<&str>,
) -> String {
    let business_operation = operation_feature(command).is_some();
    let summary = diagnostic_report::summarize_request(command, payload);
    let mut record = LogRecord::new(
        "studio", Some(if business_operation { "info" } else { "debug" }), "studio.operations",
        if business_operation { "operation.request" } else { "worker.request" }, "Operation requested",
    );
    record.task_id = task_id.map(str::to_string).or_else(|| payload.get("taskId").and_then(serde_json::Value::as_str).map(str::to_string));
    record.request_id = payload.get("requestId").and_then(serde_json::Value::as_str).map(str::to_string);
    record.job_id = payload.get("jobId").and_then(serde_json::Value::as_str).map(str::to_string);
    record.context = summary.as_object().cloned().unwrap_or_default();
    record.context.insert("command".into(), command.into());
    append_record(app, record).id
}

pub(crate) fn record_operation_failure(app: &AppHandle, command: &str, operation_id: &str, message: &str) {
    // A worker error already owns its exception and automatic report. Only fill gaps where
    // validation, runtime reservation, payload writing or process creation failed first.
    if report_context().lock().is_ok_and(|context| context.operation_failed(operation_id)) {
        return;
    }
    let mut record = LogRecord::new("studio", Some("error"), "studio.operations", "operation.failed", message);
    record.context.insert("command".into(), command.into());
    record.context.insert("operationId".into(), operation_id.into());
    append_record(app, record);
}

pub(crate) fn record_environment_probe(command: &str, payload: &serde_json::Value) {
    if !matches!(command, "env_info" | "runtime_info" | "optional_package_status" | "runtime_core_versions" | "plugin_diagnostics" | "network_diagnostics") {
        return;
    }
    let summary = diagnostic_report::summarize_probe(command, payload);
    if let Ok(mut context) = report_context().lock() {
        context.probe(command, serde_json::json!({"observedAt": timestamp(), "summary": summary}));
    }
}

pub(crate) fn record_editor_project(project: &serde_json::Value) {
    let tracks = project.get("tracks").and_then(serde_json::Value::as_array);
    let sources = project.get("sources").and_then(serde_json::Value::as_array);
    let mut summary = scalar_snapshot(project, &["id", "sourceTaskId", "masterVolume", "masterPan", "createdAt", "updatedAt"]);
    summary["observedAt"] = timestamp().into();
    summary["trackCount"] = tracks.map(Vec::len).unwrap_or(0).into();
    summary["sourceCount"] = sources.map(Vec::len).unwrap_or(0).into();
    summary["clipCount"] = tracks.map(|items| items.iter().map(|track| track.get("clips").and_then(serde_json::Value::as_array).map(Vec::len).unwrap_or(0)).sum::<usize>()).unwrap_or(0).into();
    summary["recordingTrackCount"] = tracks.map(|items| items.iter().filter(|track| track["type"] == "recording").count()).unwrap_or(0).into();
    summary["source"] = "Last loaded or saved editor project; media contents and project names are omitted".into();
    if let Ok(mut context) = report_context().lock() {
        context.editor(summary);
    }
}

pub(crate) fn begin_window_diagnostics(request_id: &str) -> AppResult<()> {
    report_context().lock().map_err(|_| AppError::Worker("Diagnostic context lock failed".into()))?
        .begin_window_request(request_id);
    Ok(())
}

pub(crate) fn record_window_diagnostics(request_id: &str, label: &str, payload: &serde_json::Value) -> AppResult<bool> {
    let mut summary = diagnostic_report::summarize_window_state(payload);
    summary["observedAt"] = timestamp().into();
    Ok(report_context().lock().map_err(|_| AppError::Worker("Diagnostic context lock failed".into()))?
        .observe_window(request_id, label, summary))
}

pub(crate) fn forget_window_diagnostics(label: &str) {
    if let Ok(mut context) = report_context().lock() { context.forget_window(label); }
}

pub(crate) fn window_diagnostics(request_id: &str, expected: &[String]) -> serde_json::Value {
    match report_context().try_lock() {
        Ok(context) => context.current_window_states(request_id, expected),
        Err(error) => serde_json::json!({"status": if matches!(error, std::sync::TryLockError::WouldBlock) { "busy" } else { "error" }, "expected": expected, "observed": [], "missing": expected}),
    }
}

pub fn init_session_log(app: &AppHandle) -> AppResult<()> {
    let _ = LOG_APP.set(app.clone());
    storage::ensure_app_directories(app)?;
    LOG_CAP_REACHED.store(false, Ordering::Relaxed);
    FILE_LOG_WARNING.store(false, Ordering::Relaxed);
    LOG_SEQUENCE.store(0, Ordering::Relaxed);
    if let Ok(mut reports) = automatic_reports().lock() { reports.reset(0); }
    if let Ok(mut session) = SESSION_ID.lock() {
        *session = Some(LogRecord::new("studio", Some("info"), "studio", "", "").id);
    }
    if let Ok(mut diagnostics) = TORCH_DIAGNOSTICS.lock() {
        *diagnostics = None;
    }
    if let Ok(mut context) = report_context().lock() {
        *context = DiagnosticContext::default();
    }
    let settings = storage::read_app_store(app, "app-settings").unwrap_or_default();
    set_developer_mode(
        settings
            .get("developerMode")
            .and_then(serde_json::Value::as_bool)
            == Some(true),
    );
    let path = session_log_path(app)?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(&path, "")?;
    rotate_persistent_log_if_needed(&persistent_log_path(app)?)?;
    append(
        app,
        "INFO",
        "app.start",
        vec![
            ("version", env!("CARGO_PKG_VERSION").to_string()),
            ("mode", app_mode().to_string()),
            ("debugLogging", verbose_logging_enabled().to_string()),
            (
                "target",
                option_env!("PYMSS_BUILD_TARGET")
                    .unwrap_or("dev")
                    .to_string(),
            ),
            (
                "variant",
                option_env!("PYMSS_BUILD_VARIANT")
                    .unwrap_or("development")
                    .to_string(),
            ),
        ],
    );
    if let Ok(exe) = std::env::current_exe() {
        append(
            app,
            "INFO",
            "app.exe",
            vec![("path", exe.to_string_lossy().to_string())],
        );
    }
    append(
        app,
        "INFO",
        "app.paths",
        vec![
            (
                "dataRoot",
                storage::data_root_dir(app)?.to_string_lossy().to_string(),
            ),
            (
                "logsDir",
                storage::logs_dir(app)?.to_string_lossy().to_string(),
            ),
        ],
    );
    Ok(())
}

pub fn append(app: &AppHandle, level: &str, event: &str, fields: Vec<(&str, String)>) {
    let mut record = LogRecord::new("studio", Some(level), "studio", event, event);
    for (key, value) in fields.into_iter().filter(|(_, value)| !value.is_empty()) {
        match key {
            "message" => record.message = value,
            "detail" => record.detail = Some(value),
            "taskId" => record.task_id = Some(value),
            "requestId" => record.request_id = Some(value),
            "nodeId" => record.node_id = Some(value),
            "jobId" => record.job_id = Some(value),
            _ => {
                record
                    .context
                    .insert(key.to_string(), serde_json::Value::String(value));
            }
        }
    }
    append_record(app, record);
}

pub fn early(level: &str, event: &str, message: &str) {
    if let Some(app) = LOG_APP.get() {
        append(app, level, event, vec![("message", message.to_string())]);
    } else {
        let mut record = LogRecord::new("studio", Some(level), "studio", event, message);
        sanitize_record(&mut record);
        if let Ok(line) = serde_json::to_string(&record) {
            terminal::write_early(&format!("{line}\n"));
        }
    }
}

/// Collect once and return the exact, sanitized record used by files, terminal and UI.
pub fn append_record(app: &AppHandle, mut record: LogRecord) -> LogRecord {
    sanitize_record(&mut record);
    if let Ok(mut context) = report_context().lock() {
        context.observe(&record);
    }
    if matches!(record.level.as_deref(), Some("debug" | "trace")) && !verbose_logging_enabled() {
        return record;
    }
    let line = {
        let Ok(_guard) = LOG_WRITE_LOCK.lock() else {
            return record;
        };
        record.sequence = Some(LOG_SEQUENCE.fetch_add(1, Ordering::Relaxed) + 1);
        if let Ok(mut session) = SESSION_ID.lock() {
            let id = session
                .get_or_insert_with(|| LogRecord::new("studio", Some("info"), "studio", "", "").id);
            record.session_id = Some(id.clone());
        }
        let Ok(mut line) = serde_json::to_string(&record) else {
            return record;
        };
        line.push('\n');
        if let Ok(path) = session_log_path(app) {
            append_session_line(&path, &line);
        }
        if let Ok(path) = persistent_log_path(app) {
            append_persistent_line(&path, &line);
        }
        // Mirror while holding the same lock as the files so concurrent worker threads preserve
        // an identical line order in every destination.
        terminal::write(&line);
        let _ = app.emit("pymss://log-record", &record);
        line
    };
    if matches!(record.level.as_deref(), Some("error" | "fatal")) {
        queue_automatic_report(app, record.sequence.unwrap_or_default(), &record.event, &line);
    }
    record
}

pub fn set_developer_mode(enabled: bool) {
    DEVELOPER_MODE.store(enabled, Ordering::Relaxed);
}

pub fn record_torch_diagnostics(app: &AppHandle, payload: &serde_json::Value) {
    let diagnostics = torch_diagnostics_from_payload(payload);
    if let Ok(mut current) = TORCH_DIAGNOSTICS.lock() {
        *current = Some(diagnostics.clone());
    }
    append(
        app,
        "INFO",
        "runtime.torch",
        vec![
            ("torchVersion", diagnostics.version),
            ("torchAvailable", diagnostics.available.to_string()),
            ("torchError", diagnostics.error),
            ("cudaAvailable", diagnostics.cuda_available.to_string()),
            ("cudaAvailableError", diagnostics.cuda_available_error),
            ("cudaDeviceCount", diagnostics.device_count.to_string()),
            ("cudaDeviceCountError", diagnostics.device_count_error),
            ("cudaDeviceNames", diagnostics.device_names.join(" | ")),
            ("cudaDeviceNamesError", diagnostics.device_names_error),
        ],
    );
}

fn torch_diagnostics_from_payload(payload: &serde_json::Value) -> TorchDiagnostics {
    let device_names = payload
        .get("cudaDevices")
        .and_then(serde_json::Value::as_array)
        .map(|devices| {
            devices
                .iter()
                .filter_map(|device| device.get("name").and_then(serde_json::Value::as_str))
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    TorchDiagnostics {
        available: payload
            .get("torchAvailable")
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(false),
        version: payload
            .get("torchVersion")
            .and_then(serde_json::Value::as_str)
            .unwrap_or_default()
            .to_string(),
        error: payload
            .get("torchError")
            .and_then(serde_json::Value::as_str)
            .unwrap_or_default()
            .to_string(),
        cuda_available: payload
            .get("cudaAvailable")
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(false),
        cuda_available_error: payload
            .get("cudaAvailableError")
            .and_then(serde_json::Value::as_str)
            .unwrap_or_default()
            .to_string(),
        device_count: payload
            .get("cudaDeviceCount")
            .and_then(serde_json::Value::as_u64)
            .unwrap_or_default(),
        device_count_error: payload
            .get("cudaDeviceCountError")
            .and_then(serde_json::Value::as_str)
            .unwrap_or_default()
            .to_string(),
        device_names,
        device_names_error: payload
            .get("cudaDeviceNamesError")
            .and_then(serde_json::Value::as_str)
            .unwrap_or_default()
            .to_string(),
    }
}

pub fn developer_mode_enabled() -> bool {
    DEVELOPER_MODE.load(Ordering::Relaxed)
}

pub fn info(app: &AppHandle) -> AppResult<DebugLogInfo> {
    let path = session_log_path(app)?;
    let persistent_path = persistent_log_path(app)?;
    let report_path = diagnostic_report_path(app)?;
    let meta = std::fs::metadata(&path).ok();
    let persistent_meta = std::fs::metadata(&persistent_path).ok();
    let report_meta = std::fs::metadata(&report_path).ok();
    Ok(DebugLogInfo {
        path: path.to_string_lossy().to_string(),
        logs_dir: storage::logs_dir(app)?.to_string_lossy().to_string(),
        exists: meta.is_some(),
        size_bytes: meta.map(|value| value.len()).unwrap_or(0),
        persistent_path: persistent_path.to_string_lossy().to_string(),
        persistent_exists: persistent_meta.is_some(),
        persistent_size_bytes: persistent_meta.map(|value| value.len()).unwrap_or(0),
        max_bytes: MAX_LOG_BYTES,
        persistent_max_bytes: MAX_PERSISTENT_LOG_BYTES,
        report_path: report_path.to_string_lossy().to_string(),
        report_exists: report_meta.is_some(),
        report_size_bytes: report_meta.map(|value| value.len()).unwrap_or(0),
    })
}

pub fn read_tail(app: &AppHandle) -> AppResult<DebugLogContent> {
    read_tail_path(persistent_log_path(app)?, READ_TAIL_BYTES)
}

fn read_tail_path(path: PathBuf, tail_bytes: u64) -> AppResult<DebugLogContent> {
    let meta = std::fs::metadata(&path).ok();
    let size_bytes = meta.as_ref().map(|value| value.len()).unwrap_or(0);
    if !path.is_file() {
        return Ok(DebugLogContent {
            path: path.to_string_lossy().to_string(),
            content: String::new(),
            size_bytes,
            truncated: false,
        });
    }
    let start = size_bytes.saturating_sub(tail_bytes);
    let mut file = std::fs::File::open(&path)?;
    use std::io::{Read, Seek, SeekFrom};
    file.seek(SeekFrom::Start(start))?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)?;
    let mut content = String::from_utf8_lossy(&bytes).into_owned();
    if start > 0 {
        if let Some(index) = content.find('\n') {
            content = content[index + 1..].to_string();
        }
    }
    Ok(DebugLogContent {
        path: path.to_string_lossy().to_string(),
        content,
        size_bytes,
        truncated: start > 0,
    })
}

pub fn clear(app: &AppHandle) -> AppResult<DebugLogInfo> {
    let path = session_log_path(app)?;
    let _report_guard = REPORT_WRITE_LOCK.lock()
        .map_err(|_| AppError::Worker("diagnostic report lock poisoned".into()))?;
    {
        let _guard = LOG_WRITE_LOCK
            .lock()
            .map_err(|_| AppError::Worker("session log lock poisoned".into()))?;
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(&path, "")?;
        std::fs::write(persistent_log_path(app)?, "")?;
        let persistent_path = persistent_log_path(app)?;
        for index in 1..=PERSISTENT_LOG_BACKUPS {
            let _ = std::fs::remove_file(persistent_backup_path(&persistent_path, index));
        }
        let _ = std::fs::remove_file(diagnostic_report_path(app)?);
        LOG_CAP_REACHED.store(false, Ordering::Relaxed);
        if let Ok(mut reports) = automatic_reports().lock() { reports.reset(LOG_SEQUENCE.load(Ordering::Relaxed).saturating_add(1)); }
        if let Ok(mut diagnostics) = TORCH_DIAGNOSTICS.lock() {
            *diagnostics = None;
        }
        if let Ok(mut context) = report_context().lock() {
            *context = DiagnosticContext::default();
        }
    }
    append(app, "INFO", "log.clear", Vec::new());
    info(app)
}

pub fn create_diagnostic_report(app: &AppHandle, snapshot: Option<&serde_json::Value>) -> AppResult<DebugLogReport> {
    create_diagnostic_report_with_trigger(app, "manual", "manual diagnostic report request", snapshot)
}

fn append_session_line(path: &PathBuf, line: &str) {
    if LOG_CAP_REACHED.load(Ordering::Relaxed) {
        return;
    }
    if let Ok(meta) = std::fs::metadata(path) {
        if meta.len() >= MAX_LOG_BYTES {
            if !LOG_CAP_REACHED.swap(true, Ordering::Relaxed) {
                let mut record = LogRecord::new(
                    "studio",
                    Some("warn"),
                    "studio.logs",
                    "log.truncated",
                    "Current session log reached its size limit",
                );
                record
                    .context
                    .insert("maxBytes".into(), MAX_LOG_BYTES.into());
                if let Ok(line) = serde_json::to_string(&record) {
                    let _ = append_line(path, &format!("{line}\n"));
                }
            }
            return;
        }
    }
    let _ = append_line(path, line);
}

fn append_persistent_line(path: &PathBuf, line: &str) {
    let _ = rotate_persistent_log_if_needed(path);
    let _ = append_line(path, line);
}

fn append_line(path: &PathBuf, line: &str) -> AppResult<()> {
    let result = (|| -> AppResult<()> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let mut file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)?;
        file.write_all(line.as_bytes())?;
        Ok(())
    })();
    if let Err(error) = &result {
        if !FILE_LOG_WARNING.swap(true, Ordering::Relaxed) {
            let mut record = LogRecord::new(
                "studio",
                Some("warn"),
                "studio.logging",
                "log.write_failed",
                "Unable to persist application logs",
            );
            record.detail = Some(error.to_string());
            sanitize_record(&mut record);
            if let Ok(line) = serde_json::to_string(&record) {
                terminal::write_early(&format!("{line}\n"));
            }
        }
    }
    result
}

fn rotate_persistent_log_if_needed(path: &PathBuf) -> AppResult<()> {
    if std::fs::metadata(path).map(|meta| meta.len()).unwrap_or(0) < MAX_PERSISTENT_LOG_BYTES {
        return Ok(());
    }
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    for index in (1..=PERSISTENT_LOG_BACKUPS).rev() {
        let from = persistent_backup_path(path, index);
        let to = persistent_backup_path(path, index + 1);
        if index == PERSISTENT_LOG_BACKUPS {
            let _ = std::fs::remove_file(&from);
        } else if from.exists() {
            let _ = std::fs::rename(&from, &to);
        }
    }
    if path.exists() {
        let _ = std::fs::rename(path, persistent_backup_path(path, 1));
    }
    Ok(())
}

fn persistent_backup_path(path: &PathBuf, index: usize) -> PathBuf {
    path.with_file_name(format!("pymss-studio.{index}.log"))
}

fn create_diagnostic_report_with_trigger(
    app: &AppHandle,
    trigger: &str,
    trigger_line: &str,
    snapshot: Option<&serde_json::Value>,
) -> AppResult<DebugLogReport> {
    let _report_guard = REPORT_WRITE_LOCK.lock()
        .map_err(|_| AppError::Worker("diagnostic report lock poisoned".into()))?;
    write_diagnostic_report_with_trigger(app, trigger, trigger_line, snapshot)
}

fn write_diagnostic_report_with_trigger(
    app: &AppHandle,
    trigger: &str,
    trigger_line: &str,
    snapshot: Option<&serde_json::Value>,
) -> AppResult<DebugLogReport> {
    let path = diagnostic_report_path(app)?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let info = info(app)?;
    let activity = report_context().lock().map(|context| context.snapshot())
        .unwrap_or_else(|_| serde_json::json!({"status": "unavailable", "reason": "activity context lock poisoned"}));
    let metadata = serde_json::json!({
        "reportVersion": 2, "generatedAt": timestamp(), "trigger": trigger,
        "version": env!("CARGO_PKG_VERSION"), "mode": app_mode(),
        "binaryProfile": if cfg!(debug_assertions) { "debug" } else { "release" },
        "target": option_env!("PYMSS_BUILD_TARGET").unwrap_or("unknown"),
        "variant": option_env!("PYMSS_BUILD_VARIANT").unwrap_or("development"),
        "gitCommit": option_env!("PYMSS_BUILD_GIT_COMMIT"),
        "gitRef": option_env!("PYMSS_BUILD_GIT_REF"), "buildTime": option_env!("PYMSS_BUILD_TIME"),
        "os": std::env::consts::OS, "arch": std::env::consts::ARCH,
        "processId": std::process::id(),
        "sessionId": SESSION_ID.lock().ok().and_then(|id| id.clone()),
        "developerMode": developer_mode_enabled(), "verboseLogging": verbose_logging_enabled(),
        "commandLineDebug": STARTUP_DEBUG_LOG.load(Ordering::Relaxed),
        "paths": {"dataRoot": storage::data_root_dir(app)?.to_string_lossy(),
            "logsDir": info.logs_dir, "sessionLog": info.path, "sessionLogBytes": info.size_bytes,
            "persistentLog": info.persistent_path, "persistentLogBytes": info.persistent_size_bytes,
            "report": path.to_string_lossy()},
        "limits": {"storeReadBytes": MAX_REPORT_STORE_BYTES,
            "sectionChars": MAX_REPORT_SECTION_CHARS, "logTailBytes": REPORT_TAIL_BYTES},
        "activityLimits": activity.get("limits"),
    });
    let mut sections = vec![
        ("Current Windows and Feature Navigation", serde_json::json!({
            "windows": activity.get("windows"), "recentVisits": activity.get("navigation"),
            "omittedVisits": activity["omitted"]["navigation"]})),
        ("Recent Operations and Running Parameters", serde_json::json!({
            "scope": activity.get("scope"), "operations": activity.get("operations"),
            "omitted": activity["omitted"]["operations"]})),
        ("Recent Issues and Lifecycle Events", serde_json::json!({
            "issues": activity.get("issues"), "events": activity.get("events"),
            "observedRecordCounts": activity.get("recordCounts"), "omitted": activity.get("omitted")})),
        ("Active Backend Tasks", backend_tasks_snapshot(app)),
        ("Cached Runtime and Capability Probes", serde_json::json!({
            "source": "Last successful observations in this process; probes are not rerun when generating a report",
            "observations": activity.get("environment")})),
        ("Last Observed Editor Project", activity.get("editor").cloned().filter(|value| !value.is_null()).unwrap_or_else(|| serde_json::json!({"status": "not_observed"}))),
        ("Last Observed Window Diagnostics", serde_json::json!(activity["windowStates"].as_array().map(Vec::as_slice).unwrap_or_default().iter().take(8).map(diagnostic_report::summarize_window_report).collect::<Vec<_>>())),
        ("Native Plugin Host", diagnostic_report::summarize_native_host(&crate::commands::plugin_host::diagnostic_snapshot(&storage::data_root_dir(app)?, &app.state::<crate::commands::plugin_host::PluginHostState>()))),
        ("Persisted Separation and Workflow Tasks", report_store(app, "task-history", diagnostic_report::summarize_task_history)),
        ("Workflow Library", report_store(app, "workflow-state", diagnostic_report::summarize_workflows)),
        ("Application Settings", report_store(app, "app-settings", diagnostic_report::summarize_settings)),
        ("Selected Separation Options", report_store(app, "separate-state", |value| scalar_snapshot(value, &[
            "runMode", "ensembleEnabled", "ensembleStem", "ensembleType", "outputLayout", "useTta",
        ]))),
        ("Audio Tool Preferences", report_store(app, "audio-tools", audio_tool_preferences)),
        ("Models and Downloads", report_store(app, "model-state", model_download_snapshot)),
        ("Saved Application Update Choices", report_store(app, "update-state", |value| scalar_snapshot(value, &[
            "deferredVersion", "deferredAt", "lastAcceptedVersion",
        ]))),
    ];
    if let Some(snapshot) = snapshot {
        sections.push(("Diagnostic Page Snapshot", diagnostic_report::summarize_diagnostic_page(snapshot)));
    }
    let data = DiagnosticReportData {
        metadata, sections, torch: torch_diagnostics_markdown(), trigger: trigger_line.to_string(),
        session: read_tail_path(session_log_path(app)?, REPORT_TAIL_BYTES)?,
        persistent: read_tail_path(persistent_log_path(app)?, REPORT_TAIL_BYTES)?,
    };
    let content = render_diagnostic_report(data);
    std::fs::write(&path, content)?;
    let meta = std::fs::metadata(&path).ok();
    Ok(DebugLogReport {
        path: path.to_string_lossy().to_string(),
        exists: meta.is_some(),
        size_bytes: meta.map(|value| value.len()).unwrap_or(0),
    })
}

struct DiagnosticReportData {
    metadata: serde_json::Value,
    sections: Vec<(&'static str, serde_json::Value)>,
    torch: String,
    trigger: String,
    session: DebugLogContent,
    persistent: DebugLogContent,
}

fn scalar_snapshot(value: &serde_json::Value, keys: &[&str]) -> serde_json::Value {
    let fields = keys.iter().filter_map(|key| {
        let value = value.get(*key)?;
        let value = match value {
            serde_json::Value::String(text) => {
                let clean = diagnostic_report::sanitize_text(text);
                let mut output: String = clean.chars().take(600).collect();
                if clean.chars().count() > 600 { output.push_str(" ...[truncated]"); }
                serde_json::Value::String(output)
            }
            serde_json::Value::Bool(_) | serde_json::Value::Number(_) => value.clone(),
            _ => return None,
        };
        Some(((*key).to_string(), value))
    }).collect();
    serde_json::Value::Object(fields)
}

fn audio_tool_preferences(value: &serde_json::Value) -> serde_json::Value {
    scalar_snapshot(value, &[
        "activeTool", "mergeSortBy", "mergeSortDirection", "asrModelMode", "asrPreset", "asrLanguage",
        "loudnessMeasurement", "loudnessTarget", "loudnessTolerance", "loudnessMaxTruePeak",
        "loudnessUseLimiter", "loudnessLookahead", "loudnessRelease",
    ])
}

fn model_download_snapshot(value: &serde_json::Value) -> serde_json::Value {
    let models = value.get("models").and_then(serde_json::Value::as_array);
    let downloads = value.get("downloadTasks").and_then(serde_json::Value::as_object);
    let mut selected: Vec<_> = downloads.into_iter().flat_map(|values| values.values()).collect();
    selected.sort_by_key(|item| {
        let priority = match item.get("status").and_then(serde_json::Value::as_str).unwrap_or_default() {
            "preparing" | "downloading" => 0,
            "failed" | "error" => 1,
            _ => 2,
        };
        (priority, std::cmp::Reverse(item.get("updatedAt").and_then(serde_json::Value::as_i64).unwrap_or(0)))
    });
    let items: Vec<_> = selected.into_iter().take(12)
        .map(|item| scalar_snapshot(item, &["taskId", "model", "name", "status", "stage", "progress", "error",
            "downloadedBytes", "totalBytes", "updatedAt", "source", "method"])).collect();
    serde_json::json!({
        "catalogModelCount": models.map(Vec::len).unwrap_or(0),
        "downloadedModelCount": models.map(|items| items.iter().filter(|item| item.get("downloaded").and_then(serde_json::Value::as_bool) == Some(true)).count()).unwrap_or(0),
        "customModelCount": models.map(|items| items.iter().filter(|item| item["source"] == "user").count()).unwrap_or(0),
        "downloadTaskCount": downloads.map(|values| values.len()).unwrap_or(0),
        "downloadTasks": items, "downloadTasksTruncated": downloads.is_some_and(|values| values.len() > 12),
    })
}

fn report_store(app: &AppHandle, name: &str, summarize: impl FnOnce(&serde_json::Value) -> serde_json::Value) -> serde_json::Value {
    match storage::app_store_path(app, name) {
        Ok(path) => report_store_path(&path, summarize),
        Err(error) => serde_json::json!({"status": "unavailable", "reason": error.to_string()}),
    }
}

fn report_store_path(path: &std::path::Path, summarize: impl FnOnce(&serde_json::Value) -> serde_json::Value) -> serde_json::Value {
    use std::io::Read;
    let file = match std::fs::File::open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return serde_json::json!({"status": "not_recorded"}),
        Err(error) => return serde_json::json!({"status": "unavailable", "reason": error.to_string()}),
    };
    let modified = file.metadata().ok().and_then(|meta| meta.modified().ok())
        .map(|time| chrono::DateTime::<chrono::Utc>::from(time).to_rfc3339());
    let mut bytes = Vec::new();
    if let Err(error) = file.take(MAX_REPORT_STORE_BYTES + 1).read_to_end(&mut bytes) {
        return serde_json::json!({"status": "unavailable", "reason": error.to_string()});
    }
    if bytes.len() as u64 > MAX_REPORT_STORE_BYTES {
        return serde_json::json!({"status": "omitted", "reason": "store exceeds diagnostic read limit", "limitBytes": MAX_REPORT_STORE_BYTES});
    }
    match serde_json::from_slice::<serde_json::Value>(&bytes) {
        Ok(value) if !value.is_null() => serde_json::json!({"status": "available", "savedAt": modified,
            "source": "Persisted snapshot; may lag an active operation", "summary": summarize(&value)}),
        Ok(_) => serde_json::json!({"status": "not_recorded", "savedAt": modified}),
        Err(error) => serde_json::json!({"status": "invalid", "reason": error.to_string(), "savedAt": modified}),
    }
}

fn backend_tasks_snapshot(app: &AppHandle) -> serde_json::Value {
    let Some(state) = app.try_state::<crate::state::AppState>() else {
        return serde_json::json!({"status": "not_initialized"});
    };
    // Automatic reports can run while a native task holds its registry lock to emit an error.
    // A report must never block that task or report an unobserved registry as empty.
    let tasks = match state.tasks.try_lock() {
        Ok(tasks) => serde_json::json!({"count": tasks.len(), "taskIds": tasks.keys().take(24).collect::<Vec<_>>(), "truncated": tasks.len() > 24}),
        Err(_) => serde_json::json!({"status": "busy"}),
    };
    let native = match state.native_tasks.try_lock() {
        Ok(tasks) => serde_json::json!({"count": tasks.len(), "taskIds": tasks.keys().take(24).collect::<Vec<_>>(), "truncated": tasks.len() > 24}),
        Err(_) => serde_json::json!({"status": "busy"}),
    };
    serde_json::json!({"workers": tasks, "nativeTasks": native})
}

fn report_text(value: &str) -> String {
    let mut text = value.lines().map(|line| {
        // Historical JSONL can contain escaped quoted credentials. Redact decoded values
        // before serializing again so an escaped quote cannot expose the rest of a secret.
        if let Ok(mut value) = serde_json::from_str::<serde_json::Value>(line) {
            if value.is_object() {
                redact_report_value(&mut value);
                return value.to_string();
            }
        }
        diagnostic_report::sanitize_text(line)
    }).collect::<Vec<_>>().join("\n");
    for key in ["USERPROFILE", "HOME"] {
        if let Some(home) = std::env::var_os(key).map(|value| value.to_string_lossy().into_owned()).filter(|value| value.len() > 3) {
            for path in [home.clone(), home.replace('\\', "/"), home.replace('\\', "\\\\")] {
                text = text.replace(&path, "<USER_HOME>");
            }
        }
    }
    text
}

fn redact_report_value(value: &mut serde_json::Value) {
    match value {
        serde_json::Value::String(text) => *text = diagnostic_report::sanitize_text(text),
        serde_json::Value::Array(items) => items.iter_mut().for_each(redact_report_value),
        serde_json::Value::Object(fields) => {
            for (key, value) in fields {
                if is_sensitive_key(key) {
                    *value = "<redacted>".into();
                } else if matches!(key.to_ascii_lowercase().as_str(), "raw" | "payload" | "snapshot" | "transcript" | "text" | "audiodata" | "tags") {
                    *value = "<content omitted from diagnostic report>".into();
                } else {
                    redact_report_value(value);
                }
            }
        }
        _ => {}
    }
}

fn append_report_json(output: &mut String, title: &str, mut value: serde_json::Value) {
    redact_report_value(&mut value);
    append_report_block(output, title, "json", &serde_json::to_string_pretty(&value).unwrap_or_default(), false);
}

fn append_report_block(output: &mut String, title: &str, language: &str, content: &str, keep_tail: bool) {
    let content = report_text(content);
    let length = content.chars().count();
    let omitted = length.saturating_sub(MAX_REPORT_SECTION_CHARS);
    let content = if keep_tail {
        content.chars().skip(omitted).collect::<String>()
    } else { content.chars().take(MAX_REPORT_SECTION_CHARS).collect::<String>() };
    let longest_run = |marker: char| content.split(|ch| ch != marker).map(str::len).max().unwrap_or(0);
    let (marker, width) = if longest_run('`') <= longest_run('~') { ('`', longest_run('`') + 1) } else { ('~', longest_run('~') + 1) };
    let fence = marker.to_string().repeat(width.max(3));
    output.push_str(&format!("\n## {title}\n\n"));
    if omitted > 0 {
        output.push_str(&format!("Section truncated: {omitted} characters omitted; {} retained.\n\n", if keep_tail { "latest text" } else { "beginning of summary" }));
    }
    output.push_str(&format!("{fence}{language}\n{content}\n{fence}\n"));
}

fn render_diagnostic_report(data: DiagnosticReportData) -> String {
    let mut output = "# Pymss Studio Diagnostic Report\n\nReport schema: 2. Runtime observations and navigation cover the current process. Task history and preferences come from saved snapshots and may lag active work. Empty observations mean no data was captured, not that the feature was never used.\n\nRequest summaries omit credentials, transcript text, audio contents and arbitrary graph text. Collection and section limits are reported below. Check custom names and remaining paths before sharing.\n".to_string();
    append_report_json(&mut output, "Build, Session and Paths", data.metadata);
    for (title, summary) in data.sections {
        append_report_json(&mut output, title, summary);
    }
    append_report_block(&mut output, "Torch Diagnostics", "text", &data.torch, false);
    append_report_block(&mut output, "Trigger Record", "text", &data.trigger, false);
    for (title, tail) in [("Recent Current Session Log", data.session), ("Recent Persistent Log", data.persistent)] {
        output.push_str(&format!("\nLog source: {} bytes; read tail truncated: {}.\n", tail.size_bytes, tail.truncated));
        append_report_block(&mut output, title, "text", &tail.content, true);
    }
    output
}

fn torch_diagnostics_markdown() -> String {
    let diagnostics = TORCH_DIAGNOSTICS
        .lock()
        .ok()
        .and_then(|current| current.clone());
    torch_diagnostics_markdown_for(diagnostics.as_ref())
}

fn torch_diagnostics_markdown_for(diagnostics: Option<&TorchDiagnostics>) -> String {
    let Some(diagnostics) = diagnostics else {
        return "- Environment probe: not recorded in this session".to_string();
    };
    let device_names = if diagnostics.device_names.is_empty() {
        "None".to_string()
    } else {
        diagnostics.device_names.join(", ")
    };
    format!(
        "- Torch available: {}\n- Torch version: {}\n- Torch error: {}\n- CUDA/ROCm available: {}\n- CUDA/ROCm availability error: {}\n- Device count: {}\n- Device count error: {}\n- Device names: {}\n- Device names error: {}",
        diagnostics.available,
        diagnostic_value(&diagnostics.version, "Unknown"),
        diagnostic_value(&diagnostics.error, "None"),
        diagnostics.cuda_available,
        diagnostic_value(&diagnostics.cuda_available_error, "None"),
        diagnostics.device_count,
        diagnostic_value(&diagnostics.device_count_error, "None"),
        diagnostic_value(&device_names, "None"),
        diagnostic_value(&diagnostics.device_names_error, "None"),
    )
}

fn diagnostic_value(value: &str, fallback: &str) -> String {
    let value = value.trim();
    if value.is_empty() {
        return fallback.to_string();
    }
    let mut value = redact_sensitive_text(&value.replace(['\r', '\n'], " "));
    if value.chars().count() > MAX_DIAGNOSTIC_VALUE_CHARS {
        value = value
            .chars()
            .take(MAX_DIAGNOSTIC_VALUE_CHARS)
            .collect::<String>();
        value.push_str(" ...[truncated]");
    }
    value
}

fn timestamp() -> String {
    crate::log_record::timestamp()
}

fn now_millis() -> u64 {
    static CLOCK: std::sync::OnceLock<std::time::Instant> = std::sync::OnceLock::new();
    CLOCK.get_or_init(std::time::Instant::now).elapsed().as_millis()
        .min(u128::from(u64::MAX)) as u64
}

fn app_mode() -> &'static str {
    if storage::is_development_executable() {
        "development"
    } else {
        "packaged"
    }
}

fn bounded_text(value: &str, max: usize) -> String {
    let value = redact_sensitive_text(&crate::log_record::strip_ansi(value));
    let mut result: String = value
        .chars()
        .filter(|ch| !ch.is_control() || matches!(ch, '\n' | '\t'))
        .take(max)
        .collect();
    if value.chars().count() > max {
        result.push_str(" ...[truncated]");
    }
    result
}

fn sanitize_record(record: &mut LogRecord) {
    record.schema_version = 1;
    if !matches!(
        record.source.as_str(),
        "studio" | "worker" | "pymss" | "third_party"
    ) {
        record.source = "third_party".into();
    }
    record.level = crate::log_record::normalize_level(record.level.as_deref());
    record
        .context
        .entry("processId".to_string())
        .or_insert_with(|| serde_json::json!(std::process::id()));
    record.timestamp = chrono::DateTime::parse_from_rfc3339(&record.timestamp)
        .map(|time| {
            time.with_timezone(&chrono::Utc)
                .to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
        })
        .unwrap_or_else(|_| crate::log_record::timestamp());
    record.id = bounded_text(&record.id, 256);
    record.message = bounded_text(&record.message, MAX_LINE_CHARS);
    record.logger = bounded_text(&record.logger, 256);
    record.event = bounded_text(&record.event, 256);
    record.detail = record
        .detail
        .as_ref()
        .map(|value| bounded_text(value, 32 * 1024));
    for value in [
        &mut record.task_id,
        &mut record.request_id,
        &mut record.node_id,
        &mut record.job_id,
    ] {
        *value = value.as_ref().map(|text| bounded_text(text, 256));
    }
    if let Some(location) = &mut record.location {
        location.file = bounded_text(&location.file, 1024);
        location.function = location
            .function
            .as_ref()
            .map(|text| bounded_text(text, 256));
    }
    let mut budget = 16 * 1024;
    let value = sanitize_context(
        serde_json::Value::Object(std::mem::take(&mut record.context)),
        "",
        0,
        if record.event == "operation.request" && record.logger == "studio.operations" { 10 } else { 5 },
        &mut budget,
    );
    record.context = value.as_object().cloned().unwrap_or_default();
    if record.event == "operation.request" && context_was_truncated(&value) {
        record.context.insert("_contextTruncated".into(), true.into());
    }
}

fn context_was_truncated(value: &serde_json::Value) -> bool {
    match value {
        serde_json::Value::String(text) => text == "[truncated]" || text.ends_with(" ...[truncated]"),
        serde_json::Value::Array(items) => items.iter().any(context_was_truncated),
        serde_json::Value::Object(fields) => fields.values().any(context_was_truncated),
        _ => false,
    }
}

fn sanitize_context(
    value: serde_json::Value,
    key: &str,
    depth: usize,
    max_depth: usize,
    budget: &mut usize,
) -> serde_json::Value {
    use serde_json::Value;
    if is_sensitive_key(key) {
        return Value::String("<redacted>".into());
    }
    if depth > max_depth || *budget == 0 {
        return Value::String("[truncated]".into());
    }
    match value {
        Value::String(text) => {
            let text = bounded_text(&text, (*budget).min(4 * 1024));
            *budget = budget.saturating_sub(text.chars().count());
            Value::String(text)
        }
        Value::Object(object) => Value::Object(
            object
                .into_iter()
                .take(32)
                .map(|(key, value)| {
                    let sanitized = sanitize_context(value, &key, depth + 1, max_depth, budget);
                    (bounded_text(&key, 128), sanitized)
                })
                .collect(),
        ),
        Value::Array(values) => Value::Array(
            values
                .into_iter()
                .take(32)
                .map(|value| sanitize_context(value, key, depth + 1, max_depth, budget))
                .collect(),
        ),
        value => value,
    }
}

fn is_sensitive_key(key: &str) -> bool {
    let lower = key.to_ascii_lowercase();
    [
        "password",
        "passwd",
        "token",
        "secret",
        "authorization",
        "api_key",
        "api-key",
        "apikey",
        "credential",
        "proxy-pass",
        "proxy_pass",
    ]
    .iter()
    .any(|needle| lower.contains(needle))
}

fn redact_sensitive_text(value: &str) -> String {
    let mut text = redact_authorization_value(&redact_url_credentials(value));
    for marker in [
        "password",
        "passwd",
        "token",
        "secret",
        "api_key",
        "api-key",
        "apikey",
        "credential",
        "proxy-pass",
        "proxy_pass",
    ] {
        text = redact_marker_value(&text, marker);
    }
    text
}

fn redact_authorization_value(value: &str) -> String {
    redact_marker_value_until(value, "authorization", |ch| matches!(ch, ',' | ';' | '&'))
}

fn redact_marker_value(value: &str, marker: &str) -> String {
    redact_marker_value_until(value, marker, |ch| {
        ch.is_whitespace() || matches!(ch, ',' | ';' | '&')
    })
}

fn redact_marker_value_until(value: &str, marker: &str, is_end: impl Fn(char) -> bool) -> String {
    let lower = value.to_ascii_lowercase();
    let mut output = String::with_capacity(value.len());
    let mut cursor = 0;
    while let Some(relative) = lower[cursor..].find(marker) {
        let start = cursor + relative;
        let after_marker = start + marker.len();
        let mut separator_start = value[after_marker..]
            .find(|ch: char| !ch.is_whitespace())
            .map(|index| after_marker + index)
            .unwrap_or(after_marker);
        // A rendered exception may contain a JSON/dict credential, e.g. "token": "...".
        if matches!(value[separator_start..].chars().next(), Some('"' | '\'')) {
            separator_start += 1;
            separator_start = value[separator_start..]
                .find(|ch: char| !ch.is_whitespace())
                .map(|index| separator_start + index)
                .unwrap_or(separator_start);
        }
        let Some(separator) = value[separator_start..].chars().next() else {
            break;
        };
        if !matches!(separator, '=' | ':') {
            output.push_str(&value[cursor..after_marker]);
            cursor = after_marker;
            continue;
        }
        let value_start = separator_start + separator.len_utf8();
        let mut value_start = value[value_start..]
            .find(|ch: char| !ch.is_whitespace())
            .map(|index| value_start + index)
            .unwrap_or(value_start);
        let quote = value[value_start..]
            .chars()
            .next()
            .filter(|ch| matches!(ch, '"' | '\''));
        if quote.is_some() {
            value_start += 1;
        }
        output.push_str(&value[cursor..value_start]);
        output.push_str("<redacted>");
        let rest = &value[value_start..];
        let mut escaped = false;
        let value_end = rest
            .char_indices()
            .find_map(|(index, ch)| {
                if let Some(quote) = quote {
                    if escaped {
                        escaped = false;
                        return None;
                    }
                    if ch == '\\' {
                        escaped = true;
                        return None;
                    }
                    (ch == quote).then_some(value_start + index)
                } else {
                    is_end(ch).then_some(value_start + index)
                }
            })
            .unwrap_or(value.len());
        cursor = value_end;
    }
    output.push_str(&value[cursor..]);
    output
}

fn redact_url_credentials(value: &str) -> String {
    let mut output = String::with_capacity(value.len());
    let mut cursor = 0;
    // Visit markers in the original text so scanning one authority cannot skip an adjacent
    // URL. Apostrophes are valid userinfo characters; surrounding quotes remain untouched.
    for (scheme_end, _) in value.match_indices("://") {
        let authority_start = scheme_end + 3;
        let authority_end = value[authority_start..]
            .find(|ch: char| ch.is_whitespace() || matches!(ch, '/' | '?' | '#' | '"' | '\\'))
            .map(|index| authority_start + index)
            .unwrap_or(value.len());
        if let Some(at_relative) = value[authority_start..authority_end].rfind('@') {
            let at = authority_start + at_relative;
            output.push_str(&value[cursor..authority_start]);
            cursor = at + 1;
        }
    }
    output.push_str(&value[cursor..]);
    output
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn diagnostic_tail(content: &str) -> DebugLogContent {
        DebugLogContent { path: "log.jsonl".into(), content: content.into(), size_bytes: content.len() as u64, truncated: false }
    }

    #[test]
    fn report_store_failures_are_explicit_and_do_not_prevent_other_sections() {
        let temporary = tempfile::tempdir().unwrap();
        let path = temporary.path().join("tasks.json");
        let summarize = diagnostic_report::summarize_task_history;
        assert_eq!(report_store_path(&path, summarize)["status"], "not_recorded");
        std::fs::write(&path, "{invalid").unwrap();
        assert_eq!(report_store_path(&path, summarize)["status"], "invalid");
        std::fs::write(&path, r#"{"tasks":[{"id":"task-1","status":"failed","runConfig":{"runMode":"workflow","workflowId":"flow-1","inferenceParams":{"batch_size":1}}}]}"#).unwrap();
        let result = report_store_path(&path, summarize);
        assert_eq!(result["status"], "available");
        assert_eq!(result["summary"]["tasks"]["items"][0]["runConfig"]["workflowId"], "flow-1");
        let file = std::fs::OpenOptions::new().write(true).open(&path).unwrap();
        file.set_len(MAX_REPORT_STORE_BYTES + 1).unwrap();
        assert_eq!(report_store_path(&path, summarize)["status"], "omitted");
    }

    #[test]
    fn rendered_report_retains_structured_context_and_redacts_decoded_log_values() {
        let old_log = json!({"message": "token=\"private multiword value\"", "context": {
            "password": "old-password",
            "mirrors": "[\"https://first:credential@a.example/file\",\"https://second:credential@b.example/file?X-Amz-Signature=second-private-signature\"]"
        }}).to_string();
        let content = render_diagnostic_report(DiagnosticReportData {
            metadata: json!({"reportVersion":2,"trigger":"manual","customUrl":"https://user:password@example.com/update?signature=private-signature"}),
            sections: vec![
                ("Recent Operations and Running Parameters", json!({"feature":"workflow","taskId":"task-1","nodeId":"step-7","inferenceParams":{"batch_size":1}})),
                ("Persisted Separation and Workflow Tasks", json!({"status":"invalid","reason":"damaged task store"})),
                ("Cached Runtime and Capability Probes", json!({"backend":"cuda","torchVersion":"2.9.1"})),
            ],
            torch: "- Torch available: true".into(),
            trigger: "Allocation failed\n```\n## embedded heading\n~~~\nRuntimeError: OOM".into(),
            session: diagnostic_tail(&old_log), persistent: diagnostic_tail("legacy INFO app.start version=0.0.12"),
        });
        for value in ["Recent Operations and Running Parameters", "task-1", "step-7", "batch_size", "damaged task store", "torchVersion", "Allocation failed", "RuntimeError: OOM", "Recent Persistent Log"] {
            assert!(content.contains(value), "report omitted {value}");
        }
        for secret in ["private multiword value", "multiword", "old-password", "private-signature", "user:password", "second-private-signature", "second:credential"] {
            assert!(!content.contains(secret), "report leaked {secret}");
        }
        let mut fence: Option<String> = None;
        for line in content.lines() {
            if let Some(open) = &fence {
                if line == open { fence = None; }
            } else if line.starts_with("```") || line.starts_with("~~~") {
                let marker = line.chars().next().unwrap();
                fence = Some(line.chars().take_while(|ch| *ch == marker).collect());
            }
        }
        assert!(fence.is_none(), "diagnostic Markdown contains an unterminated code block");
    }

    #[test]
    fn report_section_limits_keep_latest_logs_and_close_the_code_block() {
        let mut output = String::new();
        let content = format!("{}\nLATEST_ERROR", "older log\n".repeat(MAX_REPORT_SECTION_CHARS));
        append_report_block(&mut output, "Recent Log", "text", &content, true);
        assert!(output.contains("Section truncated:"));
        assert!(output.contains("LATEST_ERROR"));
        assert!(output.ends_with("```\n"));
        assert!(output.chars().count() < MAX_REPORT_SECTION_CHARS + 512);
    }

    #[test]
    fn historical_report_text_redacts_basic_digest_and_hyphenated_secret_keys() {
        let text = report_text("Authorization: Basic dXNlcjpwYXNz\nNext record\nAuthorization: Digest username=example, response=private-response");
        assert!(!text.contains("dXNlcjpwYXNz"));
        assert!(!text.contains("private-response"));
        assert!(text.contains("Next record"));
        let line = json!({"context":{"api-key":"private-key","credential":"private-credential",
            "raw":"private transcript payload","snapshot":{"text":"private transcript"}}}).to_string();
        let text = report_text(&line);
        assert!(!text.contains("private-key"));
        assert!(!text.contains("private-credential"));
        assert!(!text.contains("private transcript"));
    }

    #[test]
    fn url_userinfo_redaction_preserves_apostrophes_wrappers_queries_and_ipv6() {
        for (input, expected) in [
            ("https://alice:p'ass@example.com/file?signature=keep#fragment", "https://example.com/file?signature=keep#fragment"),
            ("'https://alice:p'ass@[::1]:8443/file?signature=keep'", "'https://[::1]:8443/file?signature=keep'"),
            ("\"https://alice:p'ass@example.com/file\"", "\"https://example.com/file\""),
            ("https://first.example,https://bob:s'ecret@second.example", "https://first.example,https://second.example"),
            ("['https://first.example','https://bob:s'ecret@second.example']", "['https://first.example','https://second.example']"),
            ("https://alice:p'ass@example.com/it's-a-file", "https://example.com/it's-a-file"),
        ] {
            assert_eq!(redact_url_credentials(input), expected);
        }
    }

    #[test]
    fn rendered_report_redacts_valid_quoted_url_credentials_after_log_preprocessing() {
        let message = "Download failed: https://alice:p'ass@example.com/file?signature='private-signature'";
        let mut record = LogRecord::new("worker", Some("error"), "worker", "error", message);
        sanitize_record(&mut record);
        assert!(!record.message.contains("alice"));
        assert!(!record.message.contains("p'ass"));
        let line = serde_json::to_string(&record).unwrap();
        let content = render_diagnostic_report(DiagnosticReportData {
            metadata: json!({"reportVersion": 2}), sections: vec![], torch: String::new(),
            trigger: line.clone(), session: diagnostic_tail(&line), persistent: diagnostic_tail(&line),
        });
        for secret in ["alice", "p'ass", "private-signature"] {
            assert!(!content.contains(secret), "report leaked {secret}");
        }
        assert!(content.contains("https://example.com/file"));
    }

    #[test]
    fn quoted_credentials_redact_escaped_quotes_without_hiding_following_fields() {
        for secret in ["prefix\"private-suffix", "prefix\\private-suffix", "prefix\\"] {
            let input = json!({"password": secret, "zStatus": "public"}).to_string();
            let redacted = redact_sensitive_text(&input);
            assert!(!redacted.contains("prefix"));
            assert!(!redacted.contains("private-suffix"));
            assert_eq!(serde_json::from_str::<serde_json::Value>(&redacted).unwrap()["zStatus"], "public");
        }
        let input = "{'password': 'prefix\\'private-suffix', 'status': 'public'}";
        assert_eq!(redact_sensitive_text(input), "{'password': '<redacted>', 'status': 'public'}");
    }

    #[test]
    fn rendered_report_keeps_escaped_credential_suffixes_out_of_messages_and_tracebacks() {
        let credentials = json!({"password": "prefix\"private-suffix", "status": "public"});
        let mut record = LogRecord::new("worker", Some("error"), "worker", "error", &format!("Request rejected: {credentials}"));
        record.detail = Some("Traceback:\nValueError: {'password': 'prefix\\'private-tail', 'status': 'public'}".into());
        sanitize_record(&mut record);
        let line = serde_json::to_string(&record).unwrap();
        let content = render_diagnostic_report(DiagnosticReportData {
            metadata: json!({"reportVersion": 2}), sections: vec![], torch: String::new(),
            trigger: line.clone(), session: diagnostic_tail(&line), persistent: diagnostic_tail(&line),
        });
        for secret in ["prefix", "private-suffix", "private-tail"] {
            assert!(!line.contains(secret), "record leaked {secret}");
            assert!(!content.contains(secret), "report leaked {secret}");
        }
        assert!(content.contains("public"));
        assert!(content.contains("Request rejected"));
        assert!(content.contains("Traceback"));
    }

    #[test]
    fn current_workflow_request_keeps_node_details_under_the_bounded_context_sanitizer() {
        let payload = json!({"workflow": {"nodes":[{"id":7,"type":"mss_separate",
            "widgets_values":["model.ckpt","cuda",true,"modelscope","0",false]}],"links":[]}});
        let mut record = LogRecord::new("studio", Some("info"), "studio.operations", "operation.request", "Operation requested");
        record.context = diagnostic_report::summarize_request("infer_workflow", &payload).as_object().unwrap().clone();
        sanitize_record(&mut record);
        let node = &record.context["configuration"]["workflow"]["nodes"]["items"][0];
        assert_eq!(node["id"], 7);
        assert_eq!(node["type"], "mss_separate");
        assert_eq!(node["parameters"]["model"], "model.ckpt");
        assert_eq!(node["parameters"]["device"], "cuda");
        record.context.insert("oversized".into(), json!("x".repeat(32 * 1024)));
        sanitize_record(&mut record);
        assert_eq!(record.context["_contextTruncated"], true);
    }

    #[test]
    fn command_line_debug_logging_survives_settings_changes_without_enabling_developer_mode() {
        let previous_developer_mode = developer_mode_enabled();
        let previous_startup_debug = STARTUP_DEBUG_LOG.load(Ordering::Relaxed);
        struct RestoreLoggingMode(bool, bool);
        impl Drop for RestoreLoggingMode {
            fn drop(&mut self) {
                set_developer_mode(self.0);
                STARTUP_DEBUG_LOG.store(self.1, Ordering::Relaxed);
                terminal::set_verbose(self.1);
            }
        }
        let _restore = RestoreLoggingMode(previous_developer_mode, previous_startup_debug);

        set_developer_mode(false);
        assert!(configure_startup_logging(["--debug".into()]));
        assert!(verbose_logging_enabled());
        assert!(!developer_mode_enabled());

        // Loading and saving settings must not clear the command-line override.
        set_developer_mode(false);
        assert!(verbose_logging_enabled());
        set_developer_mode(true);
        set_developer_mode(false);
        assert!(verbose_logging_enabled());
        assert!(!developer_mode_enabled());

        for args in [
            vec![],
            vec!["--debug=false".into()],
            vec!["--debugging".into()],
            vec!["--".into(), "--debug".into()],
        ] {
            assert!(!configure_startup_logging(args));
            assert!(!verbose_logging_enabled());
        }
        set_developer_mode(true);
        assert!(verbose_logging_enabled());
        set_developer_mode(false);
        assert!(!verbose_logging_enabled());
    }

    #[test]
    fn structured_records_redact_nested_fields_and_preserve_exception_lines() {
        let mut record = LogRecord::new(
            "pymss",
            Some("error"),
            "pymss.graph",
            "infer.failed",
            "proxy=http://user:password@example.com token=hidden {\"secret\": \"private value\"}",
        );
        record.detail = Some("Traceback\n  graph.py:780\nMemoryError".into());
        record.context =
            json!({"nested": {"authorization": "Bearer private", "items": ["password=private"]}})
                .as_object()
                .unwrap()
                .clone();
        sanitize_record(&mut record);
        let json = serde_json::to_string(&record).unwrap();
        assert!(!json.contains("private"));
        assert!(!json.contains("user:password"));
        assert!(!json.contains("hidden"));
        let reread: LogRecord = serde_json::from_str(&json).unwrap();
        assert_eq!(
            reread.detail.as_deref(),
            Some("Traceback\n  graph.py:780\nMemoryError")
        );
        assert_eq!(reread.level.as_deref(), Some("error"));
    }

    #[test]
    fn limits_apply_before_json_serialization() {
        let mut record = LogRecord::new(
            "worker",
            Some("info"),
            "worker",
            "log",
            &"模型\"\n".repeat(MAX_LINE_CHARS),
        );
        record
            .context
            .insert("large".into(), json!("中文".repeat(20 * 1024)));
        sanitize_record(&mut record);
        let line = serde_json::to_string(&record).unwrap();
        let parsed: LogRecord = serde_json::from_str(&line).unwrap();
        assert!(parsed.message.contains("[truncated]"));
        assert!(parsed.message.chars().count() < MAX_LINE_CHARS + 32);
        assert!(parsed.context["large"].as_str().unwrap().chars().count() < 4 * 1024 + 32);
    }

    #[test]
    fn stored_records_remain_parseable_alongside_legacy_text_and_after_rotation() {
        let temporary = tempfile::tempdir().unwrap();
        let path = temporary.path().join("pymss-studio.log");
        append_line(&path, "1792407070.344Z INFO app.start version=0.0.21\n").unwrap();
        let mut record = LogRecord::new(
            "pymss",
            Some("error"),
            "logger",
            "model.failed",
            "模型加载失败",
        );
        record.detail =
            Some("Traceback:\n  model.forward()\nRuntimeError: allocation failed".into());
        sanitize_record(&mut record);
        let serialized = serde_json::to_string(&record).unwrap() + "\n";
        append_line(&path, &serialized).unwrap();
        let content = read_tail_path(path.clone(), READ_TAIL_BYTES)
            .unwrap()
            .content;
        assert!(content.lines().next().unwrap().contains("app.start"));
        let stored: LogRecord = serde_json::from_str(content.lines().nth(1).unwrap()).unwrap();
        assert_eq!(stored.id, record.id);
        assert_eq!(stored.level.as_deref(), Some("error"));
        assert_eq!(stored.detail, record.detail);
        std::fs::OpenOptions::new()
            .write(true)
            .open(&path)
            .unwrap()
            .set_len(MAX_PERSISTENT_LOG_BYTES)
            .unwrap();
        rotate_persistent_log_if_needed(&path).unwrap();
        assert!(persistent_backup_path(&path, 1).exists());
        append_line(&path, &serialized).unwrap();
        let latest: LogRecord = serde_json::from_str(
            read_tail_path(path, READ_TAIL_BYTES)
                .unwrap()
                .content
                .trim(),
        )
        .unwrap();
        assert_eq!(latest.id, record.id);
    }

    #[test]
    fn torch_diagnostics_preserve_probe_errors_for_logs_and_reports() {
        let diagnostics = torch_diagnostics_from_payload(&json!({
            "torchAvailable": true,
            "torchVersion": "2.9.1+rocm",
            "cudaAvailable": false,
            "cudaAvailableError": "availability query failed",
            "cudaDeviceCount": 2,
            "cudaDeviceCountError": "count query failed",
            "cudaDevices": [{"id": 1, "name": "AMD GPU"}],
            "cudaDeviceNamesError": "device 0: name query failed"
        }));

        let report = torch_diagnostics_markdown_for(Some(&diagnostics));

        assert!(report.contains("Torch version: 2.9.1+rocm"));
        assert!(report.contains("availability query failed"));
        assert!(report.contains("count query failed"));
        assert!(report.contains("Device names: AMD GPU"));
        assert!(report.contains("device 0: name query failed"));
    }

    #[test]
    fn torch_diagnostics_redact_and_flatten_error_text() {
        let diagnostics = TorchDiagnostics {
            available: false,
            version: String::new(),
            error: "token=secret-value\nsecond line".to_string(),
            cuda_available: false,
            cuda_available_error: String::new(),
            device_count: 0,
            device_count_error: String::new(),
            device_names: Vec::new(),
            device_names_error: String::new(),
        };

        let report = torch_diagnostics_markdown_for(Some(&diagnostics));

        assert!(report.contains("Torch error: token=<redacted> second line"));
        assert!(!report.contains("secret-value"));
    }

    #[test]
    fn torch_diagnostics_sanitize_and_bound_device_names() {
        let diagnostics = TorchDiagnostics {
            available: true,
            version: "test".to_string(),
            error: String::new(),
            cuda_available: true,
            cuda_available_error: String::new(),
            device_count: 1,
            device_count_error: String::new(),
            device_names: vec![format!(
                "token=device-secret\n{}",
                "x".repeat(MAX_DIAGNOSTIC_VALUE_CHARS)
            )],
            device_names_error: String::new(),
        };

        let report = torch_diagnostics_markdown_for(Some(&diagnostics));

        assert!(report.contains("Device names: token=<redacted>"));
        assert!(!report.contains("device-secret"));
        assert!(report.contains("...[truncated]"));
        assert!(!report.contains("\nxxxxxxxx"));
    }
}
