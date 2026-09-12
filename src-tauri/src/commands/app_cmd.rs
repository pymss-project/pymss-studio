use crate::error::{AppError, AppResult};
use crate::model_dir_migration::{
    self, CancelModelDirMigrationRequest, ConfirmModelDirMigrationSwitchRequest,
    PrepareModelDirChangeRequest, RespondModelDirMigrationConflictRequest,
    StartModelDirMigrationRequest,
};
use crate::python::worker::{run_worker_once, run_worker_with_payload, spawn_worker_background};
use crate::session_log::{self, DebugLogContent, DebugLogInfo, DebugLogReport};
use crate::state::{AppState, ProxySettings};
use crate::storage;
use crate::update_manager;
use base64::Engine as _;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;
use tauri::webview::PageLoadEvent;
use tauri::{AppHandle, Emitter, Manager, State, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::DialogExt;

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BuildInfo {
    version: &'static str,
    git_commit: &'static str,
    git_tag: &'static str,
    git_ref: &'static str,
    run_id: &'static str,
    run_attempt: &'static str,
    repository: &'static str,
    repository_owner: &'static str,
    build_time: &'static str,
    target: &'static str,
    variant: &'static str,
    update_supported: bool,
    official: bool,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DebugRuntimeFileInfo {
    kind: String,
    source: String,
    path: String,
    exists: bool,
    editable: bool,
    content: Option<String>,
    backup_path: String,
    backup_exists: bool,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DebugRuntimePointersPayload {
    runtime_envs_dir: String,
    active_runtime_file: String,
    files: Vec<DebugRuntimeFileInfo>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DebugRuntimeWriteRequest {
    path: String,
    content: String,
    backup: Option<bool>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DebugRuntimeRestoreRequest {
    path: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DebugActiveRuntimeOverrideRequest {
    backend: String,
    python_path: String,
}

#[derive(Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct DebugRuntimeBackup {
    existed: bool,
    content: Option<String>,
}

const DEBUG_RUNTIME_MAX_FILE_BYTES: usize = 256 * 1024;

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
fn kill_process_tree(pid: u32) {
    let _ = Command::new("taskkill")
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .creation_flags(0x08000000)
        .status();
}

#[cfg(not(windows))]
fn kill_process_tree(pid: u32) {
    let _ = Command::new("pkill")
        .args(["-TERM", "-P", &pid.to_string()])
        .status();
}

#[tauri::command]
pub fn get_build_info() -> BuildInfo {
    BuildInfo {
        version: env!("CARGO_PKG_VERSION"),
        git_commit: option_env!("PYMSS_BUILD_GIT_COMMIT").unwrap_or(""),
        git_tag: option_env!("PYMSS_BUILD_GIT_TAG").unwrap_or(""),
        git_ref: option_env!("PYMSS_BUILD_GIT_REF").unwrap_or(""),
        run_id: option_env!("PYMSS_BUILD_RUN_ID").unwrap_or(""),
        run_attempt: option_env!("PYMSS_BUILD_RUN_ATTEMPT").unwrap_or(""),
        repository: option_env!("PYMSS_BUILD_REPOSITORY").unwrap_or(""),
        repository_owner: option_env!("PYMSS_BUILD_REPOSITORY_OWNER").unwrap_or(""),
        build_time: option_env!("PYMSS_BUILD_TIME").unwrap_or(""),
        target: option_env!("PYMSS_BUILD_TARGET").unwrap_or("dev"),
        variant: option_env!("PYMSS_BUILD_VARIANT").unwrap_or("development"),
        update_supported: option_env!("PYMSS_BUILD_UPDATE_SUPPORTED") == Some("true")
            && option_env!("PYMSS_BUILD_OFFICIAL") == Some("true")
            && update_manager::update_supported(),
        official: option_env!("PYMSS_BUILD_OFFICIAL") == Some("true"),
    }
}

#[tauri::command]
pub async fn check_managed_update(
    app: AppHandle,
    channel: update_manager::UpdateChannel,
    endpoint_override: Option<String>,
) -> AppResult<Option<update_manager::ManagedUpdateInfo>> {
    update_manager::check(&app, channel, endpoint_override).await
}

#[tauri::command]
pub async fn start_managed_update(
    app: AppHandle,
    channel: update_manager::UpdateChannel,
    endpoint_override: Option<String>,
    expected_version: String,
) -> AppResult<()> {
    update_manager::start(&app, channel, endpoint_override, expected_version).await
}

#[tauri::command]
pub async fn worker_health(app: AppHandle) -> AppResult<Value> {
    run_worker_once(&app, "health")
}

#[tauri::command]
pub async fn debug_log_info(app: AppHandle) -> AppResult<DebugLogInfo> {
    require_runtime_debug_developer_mode(&app)?;
    session_log::info(&app)
}

#[tauri::command]
pub async fn debug_log_read(app: AppHandle) -> AppResult<DebugLogContent> {
    require_runtime_debug_developer_mode(&app)?;
    session_log::read_tail(&app)
}

#[tauri::command]
pub async fn debug_log_clear(app: AppHandle) -> AppResult<DebugLogInfo> {
    require_runtime_debug_developer_mode(&app)?;
    session_log::clear(&app)
}

#[tauri::command]
pub async fn debug_log_create_report(app: AppHandle) -> AppResult<DebugLogReport> {
    require_runtime_debug_developer_mode(&app)?;
    session_log::create_diagnostic_report(&app)
}

#[tauri::command]
pub async fn get_env_info(app: AppHandle) -> AppResult<Value> {
    run_worker_once(&app, "env_info")
}

#[tauri::command]
pub async fn start_env_check(app: AppHandle) -> AppResult<Value> {
    let handle = app.clone();
    std::thread::spawn(move || {
        let result = run_worker_once(&handle, "env_info");
        match result {
            Ok(payload) => {
                let _ = handle.emit(
                    "pymss://worker-event",
                    serde_json::json!({
                        "type": "env_info",
                        "requestId": Value::Null,
                        "taskId": Value::Null,
                        "timestamp": Value::Null,
                        "payload": payload,
                    }),
                );
            }
            Err(error) => {
                let _ = handle.emit(
                    "pymss://worker-event",
                    serde_json::json!({
                        "type": "error",
                        "requestId": Value::Null,
                        "taskId": Value::Null,
                        "timestamp": Value::Null,
                        "payload": {
                            "code": "ENV_CHECK_FAILED",
                            "message": error.to_string(),
                            "recoverable": true,
                        },
                    }),
                );
            }
        }
    });

    Ok(serde_json::json!({ "started": true }))
}

#[tauri::command]
pub async fn list_models(app: AppHandle, payload: Option<Value>) -> AppResult<Value> {
    run_worker_with_payload(&app, "list_models", payload)
}

#[tauri::command]
pub async fn debug_catalog_info(app: AppHandle) -> AppResult<Value> {
    run_worker_with_payload(&app, "debug_catalog_info", None)
}

#[tauri::command]
pub async fn debug_catalog_save(app: AppHandle, payload: Value) -> AppResult<Value> {
    run_worker_with_payload(&app, "debug_catalog_save", Some(payload))
}

#[tauri::command]
pub async fn debug_catalog_reset(app: AppHandle) -> AppResult<Value> {
    run_worker_with_payload(&app, "debug_catalog_reset", None)
}

#[tauri::command]
pub async fn debug_model_config(app: AppHandle, payload: Value) -> AppResult<Value> {
    run_worker_with_payload(&app, "debug_model_config", Some(payload))
}

#[tauri::command]
pub async fn get_model_info(app: AppHandle, payload: Value) -> AppResult<Value> {
    run_worker_with_payload(&app, "model_info", Some(payload))
}

#[tauri::command]
pub async fn delete_model(app: AppHandle, payload: Value) -> AppResult<Value> {
    run_worker_with_payload(&app, "delete_model", Some(payload))
}

#[tauri::command]
pub async fn inspect_custom_model(app: AppHandle, payload: Value) -> AppResult<Value> {
    run_worker_with_payload(&app, "inspect_custom_model", Some(payload))
}

/// Import a local model. Backgrounded because copying multi-GB weights and verifying them by
/// really loading the model both take long enough to need progress and cancellation.
#[tauri::command]
pub async fn start_custom_model_import(
    app: AppHandle,
    state: State<'_, AppState>,
    payload: Value,
) -> AppResult<Value> {
    let name = payload
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    if name.trim().is_empty() {
        return Err(AppError::Worker("missing custom model name".into()));
    }
    let task_id = payload
        .get("taskId")
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| format!("custom_model_import_{}", chrono_like_timestamp()));
    // One import at a time. pymss registers by rewriting the whole registry file (read, append,
    // write), so two concurrent imports would race and silently drop one of the registrations.
    if let Ok(tasks) = state.tasks.lock() {
        if tasks.keys().any(|id| id.starts_with("custom_model_import_")) {
            return Err(AppError::Worker("a custom model import is already running".into()));
        }
    }
    let mut payload = payload;
    if let Some(object) = payload.as_object_mut() {
        object.insert("taskId".to_string(), Value::String(task_id.clone()));
    }
    spawn_worker_background(app, state, "import_custom_model", task_id.clone(), payload)?;
    Ok(serde_json::json!({ "taskId": task_id, "started": true }))
}

#[tauri::command]
pub async fn unregister_custom_model(app: AppHandle, payload: Value) -> AppResult<Value> {
    run_worker_with_payload(&app, "unregister_custom_model", Some(payload))
}

#[tauri::command]
pub async fn relink_custom_model(app: AppHandle, payload: Value) -> AppResult<Value> {
    run_worker_with_payload(&app, "relink_custom_model", Some(payload))
}

/// Rebase custom model registrations after the model directory moved. Called once a migration
/// finishes; the registry stores absolute paths and would otherwise point at the old location.
#[tauri::command]
pub async fn remap_custom_model_paths(app: AppHandle, payload: Value) -> AppResult<Value> {
    run_worker_with_payload(&app, "remap_custom_model_paths", Some(payload))
}

#[tauri::command]
pub async fn start_model_delete(
    app: AppHandle,
    state: State<'_, AppState>,
    payload: Value,
) -> AppResult<Value> {
    let model = payload
        .get("model")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::Worker("missing model".into()))?;
    let task_id = payload
        .get("taskId")
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| {
            format!(
                "delete_{}_{}",
                model.replace(|c: char| !c.is_ascii_alphanumeric(), "_"),
                chrono_like_timestamp()
            )
        });
    let mut payload = payload;
    if let Some(object) = payload.as_object_mut() {
        object.insert("taskId".to_string(), Value::String(task_id.clone()));
    }
    spawn_worker_background(app, state, "delete_model", task_id.clone(), payload)?;
    Ok(serde_json::json!({ "taskId": task_id, "started": true }))
}

#[tauri::command]
pub async fn get_model_storage_summary(app: AppHandle, payload: Value) -> AppResult<Value> {
    run_worker_with_payload(&app, "model_storage_summary", Some(payload))
}

#[tauri::command]
pub async fn delete_tool_model(app: AppHandle, payload: Value) -> AppResult<Value> {
    run_worker_with_payload(&app, "delete_tool_model", Some(payload))
}

#[tauri::command]
pub async fn cleanup_model_residual_files(app: AppHandle, payload: Value) -> AppResult<Value> {
    run_worker_with_payload(&app, "cleanup_model_residual_files", Some(payload))
}

#[tauri::command]
pub async fn start_cleanup_model_residual_files(
    app: AppHandle,
    state: State<'_, AppState>,
    payload: Value,
) -> AppResult<Value> {
    let task_id = payload
        .get("taskId")
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| format!("cleanup_residual_{}", chrono_like_timestamp()));
    let mut payload = payload;
    if let Some(object) = payload.as_object_mut() {
        object.insert("taskId".to_string(), Value::String(task_id.clone()));
    }
    spawn_worker_background(
        app,
        state,
        "cleanup_model_residual_files",
        task_id.clone(),
        payload,
    )?;
    Ok(serde_json::json!({ "taskId": task_id, "started": true }))
}

#[tauri::command]
pub async fn download_model(app: AppHandle, payload: Value) -> AppResult<Value> {
    run_worker_with_payload(&app, "download_model", Some(payload))
}

#[tauri::command]
pub async fn start_model_download(
    app: AppHandle,
    state: State<'_, AppState>,
    payload: Value,
) -> AppResult<Value> {
    let model = payload
        .get("model")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::Worker("missing model".into()))?;
    let task_id = payload
        .get("taskId")
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| {
            format!(
                "download_{}_{}",
                model.replace(|c: char| !c.is_ascii_alphanumeric(), "_"),
                chrono_like_timestamp()
            )
        });
    let mut payload = payload;
    if let Some(object) = payload.as_object_mut() {
        object.insert("taskId".to_string(), Value::String(task_id.clone()));
    }
    spawn_worker_background(app, state, "download_model", task_id.clone(), payload)?;
    Ok(serde_json::json!({ "taskId": task_id, "started": true }))
}

#[tauri::command]
pub async fn update_proxy_settings(
    state: State<'_, AppState>,
    payload: Value,
) -> AppResult<Value> {
    let mode = payload
        .get("mode")
        .and_then(Value::as_str)
        .unwrap_or("system")
        .to_string();
    let url = payload
        .get("url")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let bypass = payload
        .get("bypass")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    validate_proxy_settings(&mode, &url)?;
    let next = ProxySettings { mode, url, bypass };
    {
        let mut guard = state
            .proxy_settings
            .lock()
            .map_err(|_| AppError::Worker("proxy_settings lock poisoned".into()))?;
        *guard = next.clone();
    }
    Ok(serde_json::json!({ "ok": true, "mode": next.mode }))
}

fn validate_proxy_settings(mode: &str, url: &str) -> AppResult<()> {
    if !matches!(mode, "system" | "none" | "custom") {
        return Err(AppError::Worker("INVALID_PROXY_MODE: unsupported proxy mode".into()));
    }
    if mode != "custom" {
        return Ok(());
    }
    let normalized = if url.contains("://") {
        url.trim().to_string()
    } else if url.trim().is_empty() {
        String::new()
    } else {
        format!("http://{}", url.trim())
    };
    let (scheme, authority) = normalized
        .split_once("://")
        .ok_or_else(|| AppError::Worker("INVALID_PROXY_URL: missing proxy scheme".into()))?;
    if !matches!(scheme.to_ascii_lowercase().as_str(), "http" | "https" | "socks5" | "socks5h") {
        return Err(AppError::Worker("UNSUPPORTED_PROXY_SCHEME: unsupported proxy scheme".into()));
    }
    let authority = authority.split('/').next().unwrap_or("");
    let host_port = authority.rsplit('@').next().unwrap_or("");
    let (host, port) = if host_port.starts_with('[') {
        let end = host_port
            .find(']')
            .ok_or_else(|| AppError::Worker("INVALID_PROXY_URL: invalid IPv6 host".into()))?;
        let port = host_port
            .get(end + 1..)
            .unwrap_or("")
            .strip_prefix(':')
            .unwrap_or("");
        (&host_port[1..end], port)
    } else {
        host_port
            .rsplit_once(':')
            .ok_or_else(|| AppError::Worker("INVALID_PROXY_PORT: proxy port is required".into()))?
    };
    if host.is_empty() {
        return Err(AppError::Worker("INVALID_PROXY_URL: proxy host is missing".into()));
    }
    let port = port
        .parse::<u16>()
        .map_err(|_| AppError::Worker("INVALID_PROXY_PORT: proxy port is invalid".into()))?;
    if port == 0 {
        return Err(AppError::Worker("INVALID_PROXY_PORT: proxy port is invalid".into()));
    }
    Ok(())
}

#[tauri::command]
pub async fn test_proxy_connection(app: AppHandle, payload: Value) -> AppResult<Value> {
    run_worker_with_payload(&app, "test_connection", Some(payload))
}

#[tauri::command]
pub async fn runtime_info(app: AppHandle, payload: Option<Value>) -> AppResult<Value> {
    run_worker_with_payload(&app, "runtime_info", payload)
}

#[tauri::command]
pub async fn runtime_env_sizes(app: AppHandle) -> AppResult<Value> {
    run_worker_with_payload(&app, "runtime_env_sizes", None)
}

#[tauri::command]
pub async fn runtime_core_versions(app: AppHandle) -> AppResult<Value> {
    run_worker_with_payload(&app, "runtime_core_versions", None)
}

#[tauri::command]
pub async fn optional_runtime_package_status(
    app: AppHandle,
    package: String,
) -> AppResult<Value> {
    run_worker_with_payload(
        &app,
        "optional_package_status",
        Some(serde_json::json!({ "package": package })),
    )
}

#[tauri::command]
pub async fn manage_optional_runtime_package(
    app: AppHandle,
    state: State<'_, AppState>,
    payload: Value,
) -> AppResult<Value> {
    let package = payload
        .get("package")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| AppError::Worker("missing optional runtime package".into()))?;
    let task_prefix = format!("optional_package_{package}_");
    if let Ok(tasks) = state.tasks.lock() {
        if tasks.keys().any(|id| id.starts_with(&task_prefix)) {
            return Err(AppError::Worker(format!(
                "optional package operation is already running: {package}"
            )));
        }
    }
    let task_id = payload
        .get("taskId")
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| format!("{task_prefix}{}", chrono_like_timestamp()));
    let mut payload = payload;
    if let Some(object) = payload.as_object_mut() {
        object.insert("taskId".to_string(), Value::String(task_id.clone()));
    }
    spawn_worker_background(
        app,
        state,
        "manage_optional_package",
        task_id.clone(),
        payload,
    )?;
    Ok(serde_json::json!({ "taskId": task_id, "started": true }))
}

#[tauri::command]
pub async fn start_runtime_install(
    app: AppHandle,
    state: State<'_, AppState>,
    payload: Value,
) -> AppResult<Value> {
    if let Ok(tasks) = state.tasks.lock() {
        if tasks.keys().any(|id| id.starts_with("runtime_install_")) {
            return Err(AppError::Worker("runtime installation is already running".into()));
        }
    }
    let backend = payload
        .get("backend")
        .and_then(Value::as_str)
        .unwrap_or("cpu")
        .to_string();
    let task_id = payload
        .get("taskId")
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| format!("runtime_install_{}", chrono_like_timestamp()));
    let mut payload = payload;
    if let Some(object) = payload.as_object_mut() {
        object.insert("taskId".to_string(), Value::String(task_id.clone()));
        object.insert("backend".to_string(), Value::String(backend.clone()));
    }
    spawn_worker_background(app, state, "install_runtime", task_id.clone(), payload)?;
    Ok(serde_json::json!({ "taskId": task_id, "started": true }))
}

#[tauri::command]
pub async fn cancel_runtime_install(
    app: AppHandle,
    state: State<'_, AppState>,
    task_id: String,
) -> AppResult<bool> {
    cancel_task(app, state, task_id).await
}

#[tauri::command]
pub async fn cancel_runtime_core_update(
    app: AppHandle,
    state: State<'_, AppState>,
    task_id: String,
) -> AppResult<bool> {
    cancel_task(app, state, task_id).await
}

#[tauri::command]
pub async fn activate_runtime(app: AppHandle, payload: Value) -> AppResult<Value> {
    run_worker_with_payload(&app, "activate_runtime", Some(payload))
}

#[tauri::command]
pub async fn delete_runtime(app: AppHandle, payload: Value) -> AppResult<Value> {
    run_worker_with_payload(&app, "delete_runtime", Some(payload))
}

fn debug_runtime_backup_path(path: &Path) -> AppResult<PathBuf> {
    let name = path
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or_else(|| AppError::Worker("invalid runtime debug path".into()))?;
    Ok(path.with_file_name(format!("{name}.debug-backup.json")))
}

fn debug_runtime_allowed_roots(app: &AppHandle) -> AppResult<Vec<(PathBuf, String)>> {
    Ok(vec![(storage::runtime_envs_dir(app)?, "application".to_string())])
}

fn require_runtime_debug_developer_mode(app: &AppHandle) -> AppResult<()> {
    let settings = storage::read_app_store(app, "app-settings")?;
    if settings.get("developerMode").and_then(Value::as_bool) == Some(true) {
        return Ok(());
    }
    Err(AppError::Worker("runtime debug commands require developer mode".into()))
}

fn debug_runtime_file_kind(root: &Path, path: &Path) -> Option<&'static str> {
    if path == root.join("active-runtime.json") {
        return Some("active-runtime");
    }
    let relative = path.strip_prefix(root).ok()?;
    let parts = relative.components().collect::<Vec<_>>();
    if parts.len() == 2 && relative.file_name().and_then(|value| value.to_str()) == Some("pyvenv.cfg") {
        return Some("pyvenv");
    }
    None
}

fn canonicalize_debug_runtime_path(path: &Path) -> AppResult<PathBuf> {
    if path.is_file() {
        Ok(std::fs::canonicalize(path)?)
    } else {
        let parent = path
            .parent()
            .ok_or_else(|| AppError::Worker("invalid runtime debug path".into()))?;
        let canonical_parent = std::fs::canonicalize(parent)?;
        Ok(canonical_parent.join(path.file_name().ok_or_else(|| AppError::Worker("invalid runtime debug path".into()))?))
    }
}

fn resolve_debug_runtime_path(app: &AppHandle, value: &str) -> AppResult<(PathBuf, String, String)> {
    let path = PathBuf::from(value.trim());
    if path.components().any(|component| matches!(component, std::path::Component::ParentDir)) {
        return Err(AppError::Worker("runtime debug path must not contain parent traversal".into()));
    }
    for (root, source) in debug_runtime_allowed_roots(app)? {
        if !path.starts_with(&root) {
            continue;
        }
        let canonical_root = std::fs::canonicalize(&root).unwrap_or_else(|_| root.clone());
        let canonical_path = canonicalize_debug_runtime_path(&path)?;
        if !canonical_path.starts_with(&canonical_root) {
            return Err(AppError::Worker("runtime debug path resolves outside allowed runtime files".into()));
        }
        if let Some(kind) = debug_runtime_file_kind(&canonical_root, &canonical_path) {
            return Ok((canonical_path, kind.to_string(), source));
        }
    }
    Err(AppError::Worker("runtime debug path is outside allowed runtime files".into()))
}

fn debug_runtime_file_info(kind: &str, source: &str, path: PathBuf) -> AppResult<DebugRuntimeFileInfo> {
    let backup = debug_runtime_backup_path(&path)?;
    let exists = path.is_file();
    let content = if exists { std::fs::read_to_string(&path).ok() } else { None };
    Ok(DebugRuntimeFileInfo {
        kind: kind.to_string(),
        source: source.to_string(),
        path: path.to_string_lossy().to_string(),
        exists,
        editable: !exists || content.is_some(),
        content,
        backup_path: backup.to_string_lossy().to_string(),
        backup_exists: backup.is_file(),
    })
}

fn collect_debug_runtime_files(app: &AppHandle) -> AppResult<Vec<DebugRuntimeFileInfo>> {
    let mut files = Vec::new();
    for (root, source) in debug_runtime_allowed_roots(app)? {
        files.push(debug_runtime_file_info("active-runtime", &source, root.join("active-runtime.json"))?);
        if let Ok(entries) = std::fs::read_dir(&root) {
            for entry in entries.flatten() {
                let env_dir = entry.path();
                if env_dir.is_dir() {
                    files.push(debug_runtime_file_info("pyvenv", &source, env_dir.join("pyvenv.cfg"))?);
                }
            }
        }
    }
    Ok(files)
}

fn create_debug_runtime_backup(path: &Path) -> AppResult<()> {
    let backup_path = debug_runtime_backup_path(path)?;
    if backup_path.is_file() {
        return Ok(());
    }
    let backup = if path.is_file() {
        DebugRuntimeBackup { existed: true, content: Some(std::fs::read_to_string(path)?) }
    } else {
        DebugRuntimeBackup { existed: false, content: None }
    };
    if let Some(parent) = backup_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(backup_path, serde_json::to_string_pretty(&backup)?)?;
    Ok(())
}

#[tauri::command]
pub async fn debug_runtime_pointers(app: AppHandle) -> AppResult<DebugRuntimePointersPayload> {
    require_runtime_debug_developer_mode(&app)?;
    Ok(DebugRuntimePointersPayload {
        runtime_envs_dir: storage::runtime_envs_dir(&app)?.to_string_lossy().to_string(),
        active_runtime_file: storage::active_runtime_file(&app)?.to_string_lossy().to_string(),
        files: collect_debug_runtime_files(&app)?,
    })
}

#[tauri::command]
pub async fn debug_runtime_write_file(app: AppHandle, payload: DebugRuntimeWriteRequest) -> AppResult<DebugRuntimePointersPayload> {
    require_runtime_debug_developer_mode(&app)?;
    if payload.content.len() > DEBUG_RUNTIME_MAX_FILE_BYTES {
        return Err(AppError::Worker("runtime debug file is too large".into()));
    }
    let (path, _kind, _source) = resolve_debug_runtime_path(&app, &payload.path)?;
    if payload.backup.unwrap_or(true) {
        create_debug_runtime_backup(&path)?;
    }
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(&path, payload.content)?;
    debug_runtime_pointers(app).await
}

#[tauri::command]
pub async fn debug_runtime_restore_file(app: AppHandle, payload: DebugRuntimeRestoreRequest) -> AppResult<DebugRuntimePointersPayload> {
    require_runtime_debug_developer_mode(&app)?;
    let (path, _kind, _source) = resolve_debug_runtime_path(&app, &payload.path)?;
    let backup_path = debug_runtime_backup_path(&path)?;
    if !backup_path.is_file() {
        return Err(AppError::Worker("runtime debug backup does not exist".into()));
    }
    let backup: DebugRuntimeBackup = serde_json::from_str(&std::fs::read_to_string(&backup_path)?)?;
    if backup.existed {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(&path, backup.content.unwrap_or_default())?;
    } else if path.is_file() {
        std::fs::remove_file(&path)?;
    }
    std::fs::remove_file(backup_path)?;
    debug_runtime_pointers(app).await
}

#[tauri::command]
pub async fn debug_runtime_override_active(app: AppHandle, payload: DebugActiveRuntimeOverrideRequest) -> AppResult<DebugRuntimePointersPayload> {
    require_runtime_debug_developer_mode(&app)?;
    let backend = payload.backend.trim().to_lowercase();
    let python_path = payload.python_path.trim().to_string();
    if !matches!(backend.as_str(), "cpu" | "cuda" | "rocm" | "mlx") {
        return Err(AppError::Worker("runtime debug backend is unsupported".into()));
    }
    if python_path.is_empty() {
        return Err(AppError::Worker("runtime debug python path is required".into()));
    }
    let path = storage::active_runtime_file(&app)?;
    create_debug_runtime_backup(&path)?;
    let content = serde_json::json!({
        "backend": backend,
        "pythonPath": python_path,
    });
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(&path, serde_json::to_string_pretty(&content)?)?;
    debug_runtime_pointers(app).await
}

fn chrono_like_timestamp() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|value| value.as_millis())
        .unwrap_or_default()
}

#[tauri::command]
pub async fn start_separation(
    app: AppHandle,
    state: State<'_, AppState>,
    payload: Value,
) -> AppResult<Value> {
    let task_id = payload
        .get("taskId")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::Worker("missing taskId".into()))?
        .to_string();
    session_log::append(
        &app,
        "INFO",
        "task.start_requested",
        vec![("kind", "separation".to_string()), ("taskId", task_id.clone())],
    );
    spawn_worker_background(app, state, "infer", task_id.clone(), payload)?;
    Ok(serde_json::json!({ "taskId": task_id, "started": true }))
}

#[tauri::command]
pub async fn start_workflow_inference(
    app: AppHandle,
    state: State<'_, AppState>,
    payload: Value,
) -> AppResult<Value> {
    let task_id = payload
        .get("taskId")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::Worker("missing taskId".into()))?
        .to_string();
    session_log::append(
        &app,
        "INFO",
        "task.start_requested",
        vec![("kind", "workflow".to_string()), ("taskId", task_id.clone())],
    );
    spawn_worker_background(app, state, "infer_workflow", task_id.clone(), payload)?;
    Ok(serde_json::json!({ "taskId": task_id, "started": true }))
}

#[tauri::command]
pub async fn get_app_paths(app: AppHandle) -> AppResult<storage::AppPathsPayload> {
    session_log::append(&app, "DEBUG", "app.paths_requested", Vec::new());
    storage::app_paths_payload(&app)
}

#[tauri::command]
pub async fn load_app_store(app: AppHandle, name: String) -> AppResult<Value> {
    session_log::append(&app, "DEBUG", "app.store.load", vec![("name", name.clone())]);
    storage::read_app_store(&app, &name)
}

#[tauri::command]
pub async fn save_app_store(app: AppHandle, name: String, data: Value) -> AppResult<()> {
    session_log::append(
        &app,
        "DEBUG",
        "app.store.save",
        vec![("name", name.clone()), ("valueType", value_type(&data).to_string())],
    );
    storage::write_app_store(&app, &name, &data)?;
    if name == "app-settings" {
        session_log::set_developer_mode(
            data.get("developerMode").and_then(Value::as_bool) == Some(true),
        );
    }
    Ok(())
}

fn value_type(value: &Value) -> &'static str {
    match value {
        Value::Null => "null",
        Value::Bool(_) => "bool",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}

fn editor_projects_root(app: &AppHandle) -> AppResult<PathBuf> {
    storage::editor_projects_dir(app)
}

fn safe_file_name(value: &str) -> String {
    let name: String = value
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_' | '.') {
                ch
            } else {
                '_'
            }
        })
        .collect();
    let trimmed = name.trim_matches('_');
    if trimmed.is_empty() {
        "project".to_string()
    } else {
        trimmed.chars().take(96).collect()
    }
}

fn editor_project_dir(app: &AppHandle, project_id: &str) -> AppResult<PathBuf> {
    Ok(editor_projects_root(app)?.join(safe_file_name(project_id)))
}

fn editor_project_id_for_task_id(task_id: &str) -> String {
    format!("edit_{}", safe_file_name(task_id))
}

fn editor_project_ids_for_task_ids(task_ids: &[String]) -> Vec<String> {
    let mut seen = std::collections::HashSet::new();
    task_ids
        .iter()
        .map(|task_id| task_id.trim())
        .filter(|task_id| !task_id.is_empty())
        .map(editor_project_id_for_task_id)
        .filter(|project_id| seen.insert(project_id.clone()))
        .collect()
}

fn editor_project_path(app: &AppHandle, project_id: &str) -> AppResult<PathBuf> {
    Ok(editor_project_dir(app, project_id)?.join("project.json"))
}

fn editor_recordings_dir(app: &AppHandle, project_id: &str) -> AppResult<PathBuf> {
    Ok(editor_project_dir(app, project_id)?.join("recordings"))
}

fn validate_recording_id(recording_id: &str) -> AppResult<&str> {
    if recording_id.starts_with("recording_")
        && recording_id.len() <= 96
        && safe_file_name(recording_id) == recording_id
    {
        Ok(recording_id)
    } else {
        Err(AppError::Worker("invalid recording id".into()))
    }
}

fn pcm_wav_header(data_len: u32, sample_rate: u32, channels: u16) -> AppResult<[u8; 44]> {
    if !(8_000..=192_000).contains(&sample_rate) || !(1..=2).contains(&channels) {
        return Err(AppError::Worker("invalid recording audio format".into()));
    }
    let byte_rate = sample_rate
        .checked_mul(channels as u32)
        .and_then(|value| value.checked_mul(2))
        .ok_or_else(|| AppError::Worker("recording audio format is too large".into()))?;
    let riff_size = data_len
        .checked_add(36)
        .ok_or_else(|| AppError::Worker("recording is too large".into()))?;
    let block_align = channels * 2;
    let mut header = [0u8; 44];
    header[0..4].copy_from_slice(b"RIFF");
    header[4..8].copy_from_slice(&riff_size.to_le_bytes());
    header[8..12].copy_from_slice(b"WAVE");
    header[12..16].copy_from_slice(b"fmt ");
    header[16..20].copy_from_slice(&16u32.to_le_bytes());
    header[20..22].copy_from_slice(&1u16.to_le_bytes());
    header[22..24].copy_from_slice(&channels.to_le_bytes());
    header[24..28].copy_from_slice(&sample_rate.to_le_bytes());
    header[28..32].copy_from_slice(&byte_rate.to_le_bytes());
    header[32..34].copy_from_slice(&block_align.to_le_bytes());
    header[34..36].copy_from_slice(&16u16.to_le_bytes());
    header[36..40].copy_from_slice(b"data");
    header[40..44].copy_from_slice(&data_len.to_le_bytes());
    Ok(header)
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EditorRecordingHandle {
    recording_id: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EditorRecordingResult {
    path: String,
    name: String,
    duration: f64,
    sample_rate: u32,
    channels: u16,
}

#[derive(Default, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EditorProjectCleanupResult {
    deleted_project_ids: Vec<String>,
    missing_project_ids: Vec<String>,
    blocked_project_ids: Vec<String>,
    failed_project_ids: Vec<String>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct OrphanedEditorProject {
    project_id: String,
    source_task_id: String,
    name: String,
}

fn remove_editor_project_dirs(root: &Path, project_ids: &[String]) -> EditorProjectCleanupResult {
    let mut result = EditorProjectCleanupResult::default();
    for project_id in project_ids {
        let project_dir = root.join(safe_file_name(project_id));
        if !project_dir.exists() {
            result.missing_project_ids.push(project_id.clone());
            continue;
        }
        match std::fs::remove_dir_all(project_dir) {
            Ok(()) => result.deleted_project_ids.push(project_id.clone()),
            Err(_) => result.failed_project_ids.push(project_id.clone()),
        }
    }
    result
}

fn open_editor_project_ids(app: &AppHandle, project_ids: &[String]) -> Vec<String> {
    project_ids
        .iter()
        .filter(|project_id| {
            let label = format!("editor-{}", safe_file_name(project_id));
            app.get_webview_window(&label).is_some()
        })
        .cloned()
        .collect()
}

fn orphaned_editor_projects(
    root: &Path,
    active_task_ids: &[String],
) -> AppResult<Vec<OrphanedEditorProject>> {
    let active_task_ids: std::collections::HashSet<&str> = active_task_ids
        .iter()
        .map(|task_id| task_id.trim())
        .filter(|task_id| !task_id.is_empty())
        .collect();
    if !root.is_dir() {
        return Ok(Vec::new());
    }

    let mut projects = Vec::new();
    for entry in std::fs::read_dir(root)? {
        let entry = entry?;
        if !entry.file_type()?.is_dir() {
            continue;
        }
        let project_id = entry.file_name().to_string_lossy().to_string();
        let project_path = entry.path().join("project.json");
        let Ok(content) = std::fs::read_to_string(project_path) else {
            continue;
        };
        let Ok(project) = serde_json::from_str::<Value>(&content) else {
            continue;
        };
        let Some(source_task_id) = project
            .get("sourceTaskId")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|source_task_id| !source_task_id.is_empty())
        else {
            continue;
        };
        if active_task_ids.contains(source_task_id) {
            continue;
        }
        let name = project
            .get("name")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|name| !name.is_empty())
            .unwrap_or(&project_id)
            .to_string();
        projects.push(OrphanedEditorProject {
            project_id,
            source_task_id: source_task_id.to_string(),
            name,
        });
    }
    projects.sort_by(|left, right| {
        left.name
            .cmp(&right.name)
            .then_with(|| left.project_id.cmp(&right.project_id))
    });
    Ok(projects)
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EditorProjectSummary {
    id: String,
    name: String,
    source_task_id: Option<String>,
    source_result_dir: Option<String>,
    created_at: u64,
    updated_at: u64,
    #[serde(rename = "type")]
    project_type: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LinkedEditorAsset {
    path: String,
    name: String,
    origin_kind: String,
    origin_root: Option<String>,
    relative_path: Option<String>,
    missing: bool,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ImportEditorAssetsResult {
    pub files: Vec<LinkedEditorAsset>,
    pub warnings: Vec<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RelinkEditorSourcesResult {
    pub project: Value,
    pub relinked: usize,
    pub unresolved: Vec<String>,
}

fn now_millis() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|value| value.as_millis())
        .unwrap_or_default()
}

fn file_name_from_path(path: &str) -> String {
    Path::new(path)
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or(path)
        .to_string()
}

fn stem_from_path(path: &str) -> String {
    if let Some(stem) = Path::new(path).file_stem().and_then(|value| value.to_str()) {
        stem.to_string()
    } else {
        file_name_from_path(path)
    }
}

fn summary_from_project_value(project: &Value, fallback_id: &str) -> EditorProjectSummary {
    let id = project
        .get("id")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .unwrap_or(fallback_id)
        .to_string();
    let source_task_id = project
        .get("sourceTaskId")
        .and_then(Value::as_str)
        .map(str::to_string)
        .filter(|value| !value.trim().is_empty());
    let source_result_dir = project
        .get("sourceResultDir")
        .and_then(Value::as_str)
        .map(str::to_string)
        .filter(|value| !value.trim().is_empty());
    let project_type = if source_task_id.is_some() || source_result_dir.is_some() {
        "task"
    } else {
        "blank"
    }
    .to_string();

    EditorProjectSummary {
        id: id.clone(),
        name: project
            .get("name")
            .and_then(Value::as_str)
            .filter(|value| !value.trim().is_empty())
            .unwrap_or("Untitled Project")
            .to_string(),
        source_task_id,
        source_result_dir,
        created_at: project
            .get("createdAt")
            .and_then(Value::as_u64)
            .unwrap_or_default(),
        updated_at: project
            .get("updatedAt")
            .and_then(Value::as_u64)
            .unwrap_or_default(),
        project_type,
    }
}

fn stem_rank(stem: &str) -> usize {
    let lower = stem.to_ascii_lowercase();
    if lower.contains("vocal") || lower.contains("voice") {
        0
    } else if lower.contains("instrument")
        || lower.contains("accompaniment")
        || lower.contains("karaoke")
    {
        1
    } else if lower.contains("drum") {
        2
    } else if lower.contains("bass") {
        3
    } else if lower.contains("other") {
        4
    } else {
        9
    }
}

fn display_stem_name(stem: &str) -> String {
    let lower = stem.to_ascii_lowercase();
    if lower.contains("vocal") || lower.contains("voice") {
        "人声".to_string()
    } else if lower.contains("instrument")
        || lower.contains("accompaniment")
        || lower.contains("karaoke")
    {
        "伴奏".to_string()
    } else if lower.contains("drum") {
        "鼓组".to_string()
    } else if lower.contains("bass") {
        "贝斯".to_string()
    } else if lower.contains("other") {
        "其他".to_string()
    } else {
        stem.to_string()
    }
}

fn write_editor_project(app: &AppHandle, project: &Value) -> AppResult<Value> {
    let project_id = project
        .get("id")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::Worker("missing editor project id".into()))?;
    let dir = editor_project_dir(app, project_id)?;
    storage::write_json_file(&dir.join("project.json"), project)?;
    Ok(project.clone())
}


#[tauri::command]
pub async fn close_current_window(window: tauri::WebviewWindow) -> AppResult<()> {
    let label = window.label().to_string();
    let app_handle = window.app_handle().clone();
    window
        .destroy()
        .map_err(|error| AppError::Worker(error.to_string()))?;
    if label.starts_with("workflow-node-editor") {
        let _ = app_handle.emit_to(
            "main",
            "pymss://workflow-node-editor-closed",
            serde_json::json!({ "label": label }),
        );
    } else if label.starts_with("workflow-simple-editor") {
        let _ = app_handle.emit_to(
            "main",
            "pymss://workflow-simple-editor-closed",
            serde_json::json!({ "label": label }),
        );
    }
    Ok(())
}

#[tauri::command]
pub async fn minimize_current_window(window: tauri::WebviewWindow) -> AppResult<()> {
    window
        .minimize()
        .map_err(|error| AppError::Worker(error.to_string()))
}

#[tauri::command]
pub async fn toggle_maximize_current_window(window: tauri::WebviewWindow) -> AppResult<bool> {
    let maximized = window
        .is_maximized()
        .map_err(|error| AppError::Worker(error.to_string()))?;
    if maximized {
        window
            .unmaximize()
            .map_err(|error| AppError::Worker(error.to_string()))?;
        Ok(false)
    } else {
        window
            .maximize()
            .map_err(|error| AppError::Worker(error.to_string()))?;
        Ok(true)
    }
}

#[tauri::command]
pub async fn is_current_window_maximized(window: tauri::WebviewWindow) -> AppResult<bool> {
    window
        .is_maximized()
        .map_err(|error| AppError::Worker(error.to_string()))
}

#[tauri::command]
pub async fn start_drag_current_window(window: tauri::WebviewWindow) -> AppResult<()> {
    window
        .start_dragging()
        .map_err(|error| AppError::Worker(error.to_string()))
}

#[tauri::command]
pub async fn open_workflow_node_editor_window(app: AppHandle, payload: Value) -> AppResult<Value> {
    let workflow_id = payload
        .get("workflowId")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let new_workflow = payload
        .get("newWorkflow")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let label = if workflow_id.trim().is_empty() {
        "workflow-node-editor-new".to_string()
    } else {
        format!("workflow-node-editor-{}", safe_file_name(&workflow_id))
    };
    if let Some(window) = app.get_webview_window(&label) {
        let _ = window.set_focus();
        return Ok(
            serde_json::json!({ "workflowId": workflow_id, "label": label, "opened": true, "reused": true }),
        );
    }

    let url = if workflow_id.trim().is_empty() {
        if new_workflow {
            "index.html#/workflow-node-editor?new=1".to_string()
        } else {
            "index.html#/workflow-node-editor".to_string()
        }
    } else {
        format!("index.html#/workflow-node-editor?workflowId={}", workflow_id)
    };
    WebviewWindowBuilder::new(&app, &label, WebviewUrl::App(url.into()))
        .title("Pymss Studio Workflow Node Editor")
        .inner_size(1440.0, 900.0)
        .min_inner_size(1180.0, 720.0)
        .resizable(true)
        .minimizable(true)
        .maximizable(true)
        .closable(true)
        .decorations(cfg!(target_os = "macos"))
        .visible(false)
        .focused(true)
        .on_page_load(|window, payload| {
            if payload.event() == PageLoadEvent::Finished {
                let _ = window.show();
                let _ = window.set_focus();
            }
        })
        .build()
        .map_err(|error| AppError::Worker(error.to_string()))?;
    Ok(
        serde_json::json!({ "workflowId": workflow_id, "label": label, "opened": true, "reused": false }),
    )
}

#[tauri::command]
pub async fn open_workflow_simple_editor_window(app: AppHandle, payload: Value) -> AppResult<Value> {
    let workflow_id = payload
        .get("workflowId")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let new_workflow = payload
        .get("newWorkflow")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let label = if workflow_id.trim().is_empty() {
        "workflow-simple-editor-new".to_string()
    } else {
        format!("workflow-simple-editor-{}", safe_file_name(&workflow_id))
    };
    if let Some(window) = app.get_webview_window(&label) {
        let _ = window.set_focus();
        return Ok(serde_json::json!({ "workflowId": workflow_id, "label": label, "opened": true, "reused": true }));
    }
    let url = if workflow_id.trim().is_empty() {
        if new_workflow {
            "index.html#/workflow-simple-editor?new=1".to_string()
        } else {
            "index.html#/workflow-simple-editor".to_string()
        }
    } else {
        let encoded_id: String = url::form_urlencoded::byte_serialize(workflow_id.as_bytes()).collect();
        format!("index.html#/workflow-simple-editor?workflowId={}", encoded_id)
    };
    WebviewWindowBuilder::new(&app, &label, WebviewUrl::App(url.into()))
        .title("Pymss Studio Simple Workflow Editor")
        .inner_size(1440.0, 900.0)
        .min_inner_size(1180.0, 720.0)
        .resizable(true)
        .minimizable(true)
        .maximizable(true)
        .closable(true)
        .decorations(cfg!(target_os = "macos"))
        .visible(false)
        .focused(true)
        .on_page_load(|window, payload| {
            if payload.event() == PageLoadEvent::Finished {
                let _ = window.show();
                let _ = window.set_focus();
            }
        })
        .build()
        .map_err(|error| AppError::Worker(error.to_string()))?;
    Ok(serde_json::json!({ "workflowId": workflow_id, "label": label, "opened": true, "reused": false }))
}

#[tauri::command]
pub async fn open_editor_window(app: AppHandle, payload: Value) -> AppResult<Value> {
    let project_id = payload
        .get("projectId")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::Worker("missing projectId".into()))?
        .to_string();
    let label = format!("editor-{}", safe_file_name(&project_id));
    if let Some(window) = app.get_webview_window(&label) {
        let _ = window.emit(
            "pymss://editor-open-project",
            serde_json::json!({ "projectId": project_id, "label": label }),
        );
        let _ = window.set_focus();
        return Ok(
            serde_json::json!({ "projectId": project_id, "label": label, "opened": true, "reused": true }),
        );
    }

    let url = format!(
        "index.html#/editor?projectId={}&windowLabel={}",
        project_id, label
    );
    WebviewWindowBuilder::new(&app, &label, WebviewUrl::App(url.into()))
        .title("Pymss Studio Editor")
        .inner_size(1440.0, 900.0)
        .min_inner_size(1180.0, 720.0)
        .resizable(true)
        .minimizable(true)
        .maximizable(true)
        .closable(true)
        .decorations(cfg!(target_os = "macos"))
        .visible(false)
        .focused(true)
        .on_page_load(|window, payload| {
            if payload.event() == PageLoadEvent::Finished {
                let _ = window.show();
                let _ = window.set_focus();
            }
        })
        .build()
        .map_err(|error| AppError::Worker(error.to_string()))?;
    Ok(
        serde_json::json!({ "projectId": project_id, "label": label, "opened": true, "reused": false }),
    )
}

#[tauri::command]
pub async fn create_editor_project_from_task(app: AppHandle, payload: Value) -> AppResult<Value> {
    let task_id = payload
        .get("taskId")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::Worker("missing taskId".into()))?;

    let input = payload
        .get("input")
        .and_then(Value::as_str)
        .unwrap_or("Untitled");
    let output_dir = payload
        .get("outputDir")
        .and_then(Value::as_str)
        .unwrap_or("");
    let outputs = payload
        .get("outputs")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut paths: Vec<(String, String)> = outputs
        .iter()
        .filter_map(|item| {
            let path = item.get("path").and_then(Value::as_str)?;
            let stem = item
                .get("stem")
                .and_then(Value::as_str)
                .map(str::to_string)
                .unwrap_or_else(|| stem_from_path(path));
            Some((stem, path.to_string()))
        })
        .collect();

    if paths.is_empty() && !output_dir.is_empty() {
        let scanned = scan_audio_paths(vec![output_dir.to_string()]).await?;
        paths = scanned
            .files
            .into_iter()
            .map(|path| (stem_from_path(&path), path))
            .collect();
    }
    paths.sort_by(|a, b| {
        stem_rank(&a.0)
            .cmp(&stem_rank(&b.0))
            .then_with(|| a.0.cmp(&b.0))
    });

    let project_id = editor_project_id_for_task_id(task_id);
    let timestamp = now_millis();
    let output_root = if output_dir.trim().is_empty() {
        None
    } else {
        Some(PathBuf::from(output_dir))
    };

    let linked_paths: Vec<(String, LinkedEditorAsset)> = paths
        .into_iter()
        .map(|(stem, path)| {
            let source_path = PathBuf::from(&path);
            let relative = output_root
                .as_ref()
                .and_then(|root| source_path.strip_prefix(root).ok());
            (
                stem,
                linked_asset_from_path(
                    &source_path,
                    "task-result",
                    output_root.as_deref(),
                    relative,
                ),
            )
        })
        .collect();

    let sources: Vec<Value> = linked_paths
        .iter()
        .enumerate()
        .map(|(index, (stem, asset))| {
            serde_json::json!({
                "id": format!("source_{}_{}", index, safe_file_name(stem)),
                "role": "stem",
                "stemKey": stem,
                "path": asset.path,
                "name": asset.name,
                "duration": 0,
                "sampleRate": 0,
                "channels": 0,
                "peaksPath": Value::Null,
                "peaks": [],
                "originKind": asset.origin_kind,
                "originRoot": asset.origin_root,
                "relativePath": asset.relative_path,
                "missing": asset.missing,
            })
        })
        .collect();
    let tracks: Vec<Value> = linked_paths
        .iter()
        .enumerate()
        .map(|(index, (stem, _))| {
            serde_json::json!({
                "id": format!("track_{}", index),
                "sourceId": format!("source_{}_{}", index, safe_file_name(stem)),
                "role": "stem",
                "name": display_stem_name(stem),
                "color": Value::Null,
                "volume": 1,
                "muted": false,
                "solo": false,
                "fadeIn": 0,
                "fadeOut": 0
            })
        })
        .collect();

    let project = serde_json::json!({
        "version": 2,
        "id": project_id,
        "name": file_name_from_path(input),
        "sourceTaskId": task_id,
        "sourceResultDir": output_dir,
        "masterVolume": 1,
        "sources": sources,
        "tracks": tracks,
        "createdAt": timestamp,
        "updatedAt": timestamp
    });
    write_editor_project(&app, &project)
}

#[tauri::command]
pub async fn list_editor_projects(app: AppHandle) -> AppResult<Vec<EditorProjectSummary>> {
    let root = editor_projects_root(&app)?;
    std::fs::create_dir_all(&root)?;

    let mut items = Vec::new();
    for entry in std::fs::read_dir(root)? {
        let entry = entry?;
        if !entry.file_type()?.is_dir() {
            continue;
        }

        let fallback_id = entry.file_name().to_string_lossy().to_string();
        let project_path = entry.path().join("project.json");
        if !project_path.is_file() {
            continue;
        }

        let content = match std::fs::read_to_string(&project_path) {
            Ok(content) => content,
            Err(_) => continue,
        };
        let project: Value = match serde_json::from_str(&content) {
            Ok(project) => project,
            Err(_) => continue,
        };
        items.push(summary_from_project_value(&project, &fallback_id));
    }

    items.sort_by(|a, b| {
        b.updated_at
            .cmp(&a.updated_at)
            .then_with(|| b.created_at.cmp(&a.created_at))
            .then_with(|| a.name.cmp(&b.name))
    });
    Ok(items)
}

#[tauri::command]
pub async fn create_blank_editor_project(app: AppHandle, payload: Value) -> AppResult<Value> {
    let custom_name = payload
        .get("name")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty());
    let locale = payload
        .get("locale")
        .and_then(Value::as_str)
        .unwrap_or("zh-CN");
    let default_name = if locale == "en" {
        "Untitled Blank Project"
    } else {
        "未命名空工程"
    };
    let timestamp = now_millis() as u64;
    let project_id = format!("edit_blank_{}", timestamp);
    let project = serde_json::json!({
        "version": 2,
        "id": project_id,
        "name": custom_name.unwrap_or(default_name),
        "masterVolume": 1,
        "sources": [],
        "tracks": [],
        "createdAt": timestamp,
        "updatedAt": timestamp
    });
    write_editor_project(&app, &project)
}

#[tauri::command]
pub async fn delete_editor_project(app: AppHandle, project_id: String) -> AppResult<bool> {
    let label = format!("editor-{}", safe_file_name(&project_id));
    if app.get_webview_window(&label).is_some() {
        return Err(AppError::Worker("请先关闭该工程窗口后再删除".into()));
    }

    let project_dir = editor_project_dir(&app, &project_id)?;
    if !project_dir.exists() {
        return Ok(false);
    }

    std::fs::remove_dir_all(project_dir)?;
    Ok(true)
}

#[tauri::command]
pub async fn delete_editor_projects_for_tasks(
    app: AppHandle,
    task_ids: Vec<String>,
) -> AppResult<EditorProjectCleanupResult> {
    let project_ids = editor_project_ids_for_task_ids(&task_ids);
    let blocked_project_ids = open_editor_project_ids(&app, &project_ids);
    if !blocked_project_ids.is_empty() {
        return Ok(EditorProjectCleanupResult {
            blocked_project_ids,
            ..EditorProjectCleanupResult::default()
        });
    }

    let root = editor_projects_root(&app)?;
    Ok(remove_editor_project_dirs(&root, &project_ids))
}

#[tauri::command]
pub async fn list_open_editor_projects_for_tasks(
    app: AppHandle,
    task_ids: Vec<String>,
) -> AppResult<Vec<String>> {
    let project_ids = editor_project_ids_for_task_ids(&task_ids);
    Ok(open_editor_project_ids(&app, &project_ids))
}

#[tauri::command]
pub async fn list_orphaned_editor_projects(
    app: AppHandle,
    active_task_ids: Vec<String>,
) -> AppResult<Vec<OrphanedEditorProject>> {
    let root = editor_projects_root(&app)?;
    orphaned_editor_projects(&root, &active_task_ids)
}

#[tauri::command]
pub async fn delete_orphaned_editor_projects(
    app: AppHandle,
    active_task_ids: Vec<String>,
) -> AppResult<EditorProjectCleanupResult> {
    let root = editor_projects_root(&app)?;
    let project_ids: Vec<String> = orphaned_editor_projects(&root, &active_task_ids)?
        .into_iter()
        .map(|project| project.project_id)
        .collect();
    let blocked_project_ids = open_editor_project_ids(&app, &project_ids);
    if !blocked_project_ids.is_empty() {
        return Ok(EditorProjectCleanupResult {
            blocked_project_ids,
            ..EditorProjectCleanupResult::default()
        });
    }
    Ok(remove_editor_project_dirs(&root, &project_ids))
}

#[tauri::command]
pub async fn scan_editor_assets(paths: Vec<String>) -> AppResult<ScanAudioPathsResult> {
    scan_audio_paths(paths).await
}

#[tauri::command]
pub async fn import_editor_assets(
    _app: AppHandle,
    _project_id: String,
    paths: Vec<String>,
) -> AppResult<ImportEditorAssetsResult> {
    let mut imported = Vec::new();
    let mut warnings = Vec::new();
    for raw in paths {
        let target = PathBuf::from(&raw);
        if target.is_file() {
            if is_audio_file(&target) {
                let origin_root = target.parent().map(PathBuf::from);
                imported.push(linked_asset_from_path(
                    &target,
                    "external",
                    origin_root.as_deref(),
                    target.file_name().map(Path::new),
                ));
            } else {
                warnings.push(format!("unsupported file: {}", target.display()));
            }
            continue;
        }

        if target.is_dir() {
            let scanned = scan_audio_paths(vec![raw.clone()]).await?;
            warnings.extend(scanned.warnings);
            for file in scanned.files {
                let file_path = PathBuf::from(&file);
                let relative = file_path.strip_prefix(&target).ok();
                imported.push(linked_asset_from_path(
                    &file_path,
                    "external",
                    Some(target.as_path()),
                    relative,
                ));
            }
            continue;
        }

        warnings.push(format!("path not found: {}", raw));
    }
    imported.sort_by(|a, b| normalize_path_key(&a.path).cmp(&normalize_path_key(&b.path)));
    imported.dedup_by(|a, b| normalize_path_key(&a.path) == normalize_path_key(&b.path));
    Ok(ImportEditorAssetsResult { files: imported, warnings })
}

#[tauri::command]
pub async fn get_audio_metadata(app: AppHandle, payload: Value) -> AppResult<Value> {
    run_worker_with_payload(&app, "audio_metadata", Some(payload))
}

#[tauri::command]
pub async fn generate_waveform_peaks(app: AppHandle, payload: Value) -> AppResult<Value> {
    let mut payload = payload;
    if let Some(object) = payload.as_object_mut() {
        if !object.contains_key("cacheDir") {
            let project_id = object
                .get("projectId")
                .and_then(Value::as_str)
                .unwrap_or("shared");
            let cache_dir = editor_project_dir(&app, project_id)?.join("peaks");
            std::fs::create_dir_all(&cache_dir)?;
            object.insert(
                "cacheDir".into(),
                Value::String(cache_dir.to_string_lossy().to_string()),
            );
        }
    }
    run_worker_with_payload(&app, "waveform_peaks", Some(payload))
}

#[tauri::command]
pub async fn save_editor_project(app: AppHandle, project: Value) -> AppResult<Value> {
    let mut project = project;
    if let Some(object) = project.as_object_mut() {
        object.insert("updatedAt".into(), Value::from(now_millis() as u64));
    }
    write_editor_project(&app, &project)
}

#[tauri::command]
pub async fn load_editor_project(app: AppHandle, project_id: String) -> AppResult<Value> {
    let path = editor_project_path(&app, &project_id)?;
    let content = std::fs::read_to_string(path)?;
    let mut project: Value = serde_json::from_str(&content)?;
    if enrich_editor_project_sources(&mut project) {
        write_editor_project(&app, &project)?;
    }
    Ok(project)
}

#[tauri::command]
pub async fn relink_editor_sources(
    app: AppHandle,
    payload: Value,
) -> AppResult<RelinkEditorSourcesResult> {
    let project_id = payload
        .get("projectId")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::Worker("missing projectId".into()))?;
    let source_id = payload
        .get("sourceId")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::Worker("missing sourceId".into()))?;
    let picked_path = payload
        .get("pickedPath")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::Worker("missing pickedPath".into()))?;

    let path = editor_project_path(&app, project_id)?;
    let content = std::fs::read_to_string(&path)?;
    let mut project: Value = serde_json::from_str(&content)?;
    enrich_editor_project_sources(&mut project);

    let sources = project
        .get_mut("sources")
        .and_then(Value::as_array_mut)
        .ok_or_else(|| AppError::Worker("editor project has no sources".into()))?;
    let anchor_source = sources
        .iter()
        .find(|source| source.get("id").and_then(Value::as_str) == Some(source_id))
        .cloned()
        .ok_or_else(|| AppError::Worker("source not found".into()))?;

    let anchor_relative = anchor_source.get("relativePath").and_then(Value::as_str);
    let picked_path_buf = PathBuf::from(picked_path);
    if !picked_path_buf.is_file() {
        return Err(AppError::Worker("picked relink file does not exist".into()));
    }

    let relink_root = derive_relink_root(&picked_path_buf, anchor_relative)
        .or_else(|| picked_path_buf.parent().map(PathBuf::from))
        .ok_or_else(|| AppError::Worker("failed to resolve relink root".into()))?;
    let file_name_index = build_file_name_index(&relink_root);

    let mut relinked = 0usize;
    let mut unresolved = Vec::new();

    for source in sources.iter_mut() {
        let Some(source_object) = source.as_object_mut() else {
            continue;
        };
        // Only attempt to repoint sources that are actually missing.
        // Healthy sources must keep their existing path/originRoot untouched,
        // otherwise a bulk relink could silently steal them into the relink root.
        if !source_object
            .get("missing")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            continue;
        }
        let relative_path = source_object.get("relativePath").and_then(Value::as_str);
        let fallback_name = source_object
            .get("path")
            .and_then(Value::as_str)
            .and_then(|value| Path::new(value).file_name().and_then(|name| name.to_str()));
        let candidate = build_relink_candidate(&relink_root, relative_path, &file_name_index)
            .or_else(|| {
                let file_name = fallback_name?.to_ascii_lowercase();
                let candidates = file_name_index.get(&file_name)?;
                if candidates.len() == 1 {
                    candidates.first().cloned()
                } else {
                    preferred_match_by_relative_path(candidates, relative_path).cloned()
                }
            });

        let Some(next_path) = candidate else {
            if source_object.get("missing").and_then(Value::as_bool).unwrap_or(false) {
                unresolved.push(
                    source_object
                        .get("id")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string(),
                );
            }
            continue;
        };

        let next_path_string = path_to_string(&next_path);
        let current_path = source_object
            .get("path")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let current_missing = source_object
            .get("missing")
            .and_then(Value::as_bool)
            .unwrap_or(false);

        if current_path != next_path_string || current_missing {
            source_object.insert("path".into(), Value::String(next_path_string));
            source_object.insert("missing".into(), Value::Bool(false));
            relinked += 1;
        }

        source_object.insert(
            "originRoot".into(),
            Value::String(path_to_string(&relink_root)),
        );

        if source_object
            .get("originKind")
            .and_then(Value::as_str)
            .unwrap_or_default()
            == "legacy"
        {
            source_object.insert("originKind".into(), Value::String("external".into()));
        }

    }

    enrich_editor_project_sources(&mut project);
    let saved = write_editor_project(&app, &project)?;
    Ok(RelinkEditorSourcesResult {
        project: saved,
        relinked,
        unresolved,
    })
}

#[tauri::command]
pub async fn editor_project_exists(app: AppHandle, project_id: String) -> AppResult<bool> {
    let path = editor_project_path(&app, &project_id)?;
    Ok(path.exists())
}

#[tauri::command]
pub async fn export_editor_mix(app: AppHandle, payload: Value) -> AppResult<Value> {
    let mut payload = payload;
    if let Some(object) = payload.as_object_mut() {
        let project_id = object
            .get("project")
            .and_then(|project| project.get("id"))
            .and_then(Value::as_str)
            .unwrap_or("shared");
        if !object.contains_key("exportDir") {
            let export_dir = editor_project_dir(&app, project_id)?.join("exports");
            std::fs::create_dir_all(&export_dir)?;
            object.insert(
                "exportDir".into(),
                Value::String(export_dir.to_string_lossy().to_string()),
            );
        }
    }
    run_worker_with_payload(&app, "export_editor_mix", Some(payload))
}

#[tauri::command]
pub async fn start_audio_tool(
    app: AppHandle,
    state: State<'_, AppState>,
    payload: Value,
) -> AppResult<Value> {
    let task_id = payload
        .get("requestId")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| AppError::Worker("missing audio tool request id".into()))?
        .to_string();
    spawn_worker_background(app, state, "audio_tools", task_id.clone(), payload)?;
    Ok(serde_json::json!({ "taskId": task_id, "started": true }))
}

#[tauri::command]
pub async fn cancel_task(
    app: AppHandle,
    state: State<'_, AppState>,
    task_id: String,
) -> AppResult<bool> {
    let child = state
        .tasks
        .lock()
        .ok()
        .and_then(|mut tasks| tasks.remove(&task_id));
    if let Some(child) = child {
        let mut cancelled_task_ids = vec![task_id.clone()];
        if let Ok(mut tasks) = state.tasks.lock() {
            let linked_ids: Vec<String> = tasks
                .iter()
                .filter_map(|(id, registered)| {
                    if std::sync::Arc::ptr_eq(registered, &child) {
                        Some(id.clone())
                    } else {
                        None
                    }
                })
                .collect();
            for id in linked_ids {
                tasks.remove(&id);
                cancelled_task_ids.push(id);
            }
        }
        if let Ok(mut cancelled) = state.cancelled_tasks.lock() {
            cancelled.extend(cancelled_task_ids.iter().cloned());
        }
        if let Ok(mut child) = child.lock() {
            let pid = child.id();
            kill_process_tree(pid);
            let _ = child.kill();
        }
        for cancelled_task_id in cancelled_task_ids {
            let _ = app.emit(
                "pymss://worker-event",
                serde_json::json!({
                    "type": "task_cancelled",
                    "taskId": cancelled_task_id,
                    "payload": { "message": "Cancelled" }
                }),
            );
        }
        Ok(true)
    } else {
        Ok(false)
    }
}

#[tauri::command]
pub async fn pick_audio_files(app: AppHandle) -> AppResult<Vec<String>> {
    let files = app
        .dialog()
        .file()
        .add_filter(
            "Audio",
            &["wav", "mp3", "flac", "m4a", "aac", "ogg", "opus"],
        )
        .blocking_pick_files()
        .unwrap_or_default();
    Ok(files.into_iter().map(|p| p.to_string()).collect())
}

/// Pick a model weights file. Extensions mirror what pymss can actually load with `torch.load`;
/// `.safetensors` is deliberately absent because pymss has no safetensors path at all.
///
/// `title` is supplied by the caller so it can be localised, and so that back-to-back weights
/// and config dialogs are told apart by more than their file filter.
#[tauri::command]
pub async fn pick_model_weights_file(app: AppHandle, title: Option<String>) -> AppResult<Option<String>> {
    let mut builder = app
        .dialog()
        .file()
        .add_filter("Model weights", &["ckpt", "pth", "th", "pt", "bin"]);
    if let Some(title) = title.as_deref().filter(|value| !value.trim().is_empty()) {
        builder = builder.set_title(title);
    }
    Ok(builder.blocking_pick_file().map(|p| p.to_string()))
}

#[tauri::command]
pub async fn pick_model_config_file(app: AppHandle, title: Option<String>) -> AppResult<Option<String>> {
    let mut builder = app.dialog().file().add_filter("Model config", &["yaml", "yml"]);
    if let Some(title) = title.as_deref().filter(|value| !value.trim().is_empty()) {
        builder = builder.set_title(title);
    }
    Ok(builder.blocking_pick_file().map(|p| p.to_string()))
}

#[tauri::command]
pub async fn pick_single_audio_file(app: AppHandle) -> AppResult<Option<String>> {
    Ok(app
        .dialog()
        .file()
        .add_filter(
            "Audio",
            &["wav", "mp3", "flac", "m4a", "aac", "ogg", "opus"],
        )
        .blocking_pick_file()
        .map(|p| p.to_string()))
}

fn path_to_string(path: &Path) -> String {
    path.to_string_lossy().to_string()
}

fn normalize_path_key(value: &str) -> String {
    value.replace('\\', "/").to_ascii_lowercase()
}

fn normalized_relative_path(path: &Path) -> Option<String> {
    let parts: Vec<String> = path
        .components()
        .filter_map(|component| match component {
            std::path::Component::Normal(value) => value.to_str().map(str::to_string),
            _ => None,
        })
        .filter(|part| !part.is_empty())
        .collect();
    if parts.is_empty() {
        None
    } else {
        Some(parts.join("/"))
    }
}

fn relative_path_from_root(root: &Path, target: &Path) -> Option<String> {
    target
        .strip_prefix(root)
        .ok()
        .and_then(normalized_relative_path)
}

fn linked_asset_from_path(
    path: &Path,
    origin_kind: &str,
    origin_root: Option<&Path>,
    relative_path: Option<&Path>,
) -> LinkedEditorAsset {
    let relative = relative_path
        .and_then(normalized_relative_path)
        .or_else(|| origin_root.and_then(|root| relative_path_from_root(root, path)))
        .or_else(|| path.file_name().and_then(|value| value.to_str()).map(str::to_string));
    LinkedEditorAsset {
        path: path_to_string(path),
        name: file_name_from_path(&path_to_string(path)),
        origin_kind: origin_kind.to_string(),
        origin_root: origin_root.map(path_to_string),
        relative_path: relative,
        missing: !path.is_file(),
    }
}

fn detect_editor_source_origin(project: &Value, source: &Value) -> (String, Option<String>, Option<String>) {
    let role = source.get("role").and_then(Value::as_str).unwrap_or("reference");
    let path = source.get("path").and_then(Value::as_str).unwrap_or_default();
    let source_path = PathBuf::from(path);

    let stored_kind = source
        .get("originKind")
        .and_then(Value::as_str)
        .map(str::to_string)
        .filter(|value| !value.trim().is_empty());
    let stored_root = source
        .get("originRoot")
        .and_then(Value::as_str)
        .map(str::to_string)
        .filter(|value| !value.trim().is_empty());
    let stored_relative = source
        .get("relativePath")
        .and_then(Value::as_str)
        .map(str::to_string)
        .filter(|value| !value.trim().is_empty());

    if stored_kind.is_some() || stored_root.is_some() || stored_relative.is_some() {
        return (
            stored_kind.unwrap_or_else(|| if role == "stem" { "task-result" } else { "external" }.to_string()),
            stored_root,
            stored_relative.or_else(|| source_path.file_name().and_then(|value| value.to_str()).map(str::to_string)),
        );
    }

    if role == "stem" {
        if let Some(result_dir) = project
            .get("sourceResultDir")
            .and_then(Value::as_str)
            .map(PathBuf::from)
        {
            let relative = relative_path_from_root(&result_dir, &source_path)
                .or_else(|| source_path.file_name().and_then(|value| value.to_str()).map(str::to_string));
            return ("task-result".to_string(), Some(path_to_string(&result_dir)), relative);
        }
    }

    let parent = source_path.parent().map(path_to_string);
    let relative = source_path.file_name().and_then(|value| value.to_str()).map(str::to_string);
    ("legacy".to_string(), parent, relative)
}

fn enrich_editor_project_sources(project: &mut Value) -> bool {
    let project_snapshot = project.clone();
    let Some(sources) = project.get_mut("sources").and_then(Value::as_array_mut) else {
        return false;
    };

    let mut changed = false;
    for source in sources.iter_mut() {
        let Some(source_object) = source.as_object_mut() else {
            continue;
        };
        let path = source_object
            .get("path")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let source_path = PathBuf::from(&path);
        let missing = !source_path.is_file();
        if source_object.get("missing").and_then(Value::as_bool) != Some(missing) {
            source_object.insert("missing".into(), Value::Bool(missing));
            changed = true;
        }

        let source_value = Value::Object(source_object.clone());
        let (origin_kind, origin_root, relative_path) =
            detect_editor_source_origin(&project_snapshot, &source_value);

        if source_object
            .get("originKind")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .trim()
            .is_empty()
        {
            source_object.insert("originKind".into(), Value::String(origin_kind));
            changed = true;
        }

        if !source_object.contains_key("originRoot") {
            source_object.insert(
                "originRoot".into(),
                origin_root.map(Value::String).unwrap_or(Value::Null),
            );
            changed = true;
        }

        if !source_object.contains_key("relativePath") {
            source_object.insert(
                "relativePath".into(),
                relative_path.map(Value::String).unwrap_or(Value::Null),
            );
            changed = true;
        }
    }

    changed
}

fn source_relative_parts(relative_path: &str) -> Vec<String> {
    relative_path
        .split(['/', '\\'])
        .filter(|part| !part.trim().is_empty())
        .map(str::to_string)
        .collect()
}

fn path_tail_matches(path: &Path, relative_path: &str) -> bool {
    let rel_parts = source_relative_parts(relative_path);
    if rel_parts.is_empty() {
        return false;
    }
    let path_parts: Vec<String> = path
        .components()
        .filter_map(|component| match component {
            std::path::Component::Normal(value) => value.to_str().map(str::to_string),
            _ => None,
        })
        .collect();
    if path_parts.len() < rel_parts.len() {
        return false;
    }
    let tail = &path_parts[path_parts.len() - rel_parts.len()..];
    tail.iter()
        .map(|part| part.to_ascii_lowercase())
        .eq(rel_parts.iter().map(|part| part.to_ascii_lowercase()))
}

fn derive_relink_root(picked_path: &Path, relative_path: Option<&str>) -> Option<PathBuf> {
    let relative_path = relative_path?;
    if !path_tail_matches(picked_path, relative_path) {
        return None;
    }
    let component_count = source_relative_parts(relative_path).len();
    if component_count == 0 {
        return None;
    }
    let mut cursor = picked_path.to_path_buf();
    for _ in 0..component_count {
        cursor = cursor.parent()?.to_path_buf();
    }
    Some(cursor)
}

fn build_file_name_index(root: &Path) -> std::collections::HashMap<String, Vec<PathBuf>> {
    let mut files = Vec::new();
    let mut warnings = Vec::new();
    collect_audio_files(root, &mut files, &mut warnings);
    let mut index = std::collections::HashMap::<String, Vec<PathBuf>>::new();
    for file in files {
        let path = PathBuf::from(&file);
        if let Some(name) = path.file_name().and_then(|value| value.to_str()) {
            index.entry(name.to_ascii_lowercase()).or_default().push(path);
        }
    }
    index
}

fn preferred_match_by_relative_path<'a>(candidates: &'a [PathBuf], relative_path: Option<&str>) -> Option<&'a PathBuf> {
    let relative_path = relative_path?;
    let mut filtered = candidates.iter().filter(|path| path_tail_matches(path, relative_path));
    let first = filtered.next()?;
    if filtered.next().is_some() {
        None
    } else {
        Some(first)
    }
}

fn build_relink_candidate(
    relink_root: &Path,
    relative_path: Option<&str>,
    file_name_index: &std::collections::HashMap<String, Vec<PathBuf>>,
) -> Option<PathBuf> {
    if let Some(relative) = relative_path {
        // Join segment-by-segment so PathBuf inserts the platform separator,
        // instead of hardcoding a Windows backslash.
        let mut direct = relink_root.to_path_buf();
        for part in source_relative_parts(relative) {
            direct.push(part);
        }
        if direct.is_file() {
            return Some(direct);
        }
    }

    let file_name = relative_path
        .and_then(|value| source_relative_parts(value).last().cloned())
        .map(|value| value.to_ascii_lowercase())?;
    let candidates = file_name_index.get(&file_name)?;
    if let Some(preferred) = preferred_match_by_relative_path(candidates, relative_path) {
        return Some(preferred.clone());
    }
    if candidates.len() == 1 {
        return candidates.first().cloned();
    }
    None
}

#[tauri::command]
pub async fn pick_media_files(app: AppHandle) -> AppResult<Vec<String>> {
    let files = app
        .dialog()
        .file()
        .add_filter(
            "Media / 媒体",
            &[
                "wav", "mp3", "flac", "m4a", "aac", "ogg", "opus", "mp4", "mkv", "mov", "avi",
                "webm", "flv",
            ],
        )
        .blocking_pick_files()
        .unwrap_or_default();
    Ok(files.into_iter().map(|p| p.to_string()).collect())
}

#[tauri::command]
pub async fn pick_input_folder(app: AppHandle) -> AppResult<Option<String>> {
    Ok(app
        .dialog()
        .file()
        .blocking_pick_folder()
        .map(|p| p.to_string()))
}

#[tauri::command]
pub async fn pick_output_folder(app: AppHandle) -> AppResult<Option<String>> {
    Ok(app
        .dialog()
        .file()
        .blocking_pick_folder()
        .map(|p| p.to_string()))
}

/// Opens a "Save As" dialog seeded with `default_name` and writes `content` to
/// the chosen path. Returns the saved path, or `None` if the user cancelled.
#[tauri::command]
pub async fn save_text_file_dialog(
    app: AppHandle,
    default_name: String,
    content: String,
) -> AppResult<Option<String>> {
    let mut builder = app.dialog().file();
    if !default_name.trim().is_empty() {
        builder = builder.set_file_name(&default_name);
        if let Some(ext) = std::path::Path::new(&default_name)
            .extension()
            .and_then(|value| value.to_str())
        {
            builder = builder.add_filter(ext.to_uppercase(), &[ext]);
        }
    }
    let Some(path) = builder.blocking_save_file() else {
        return Ok(None);
    };
    let path_string = path.to_string();
    let path_buf = PathBuf::from(&path_string);
    if let Some(parent) = path_buf.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(&path_buf, content.as_bytes())?;
    Ok(Some(path_string))
}

#[tauri::command]
pub async fn prepare_model_dir_change(
    app: AppHandle,
    payload: PrepareModelDirChangeRequest,
) -> AppResult<model_dir_migration::PrepareModelDirChangePayload> {
    model_dir_migration::prepare_model_dir_change(&app, payload)
}

#[tauri::command]
pub async fn start_model_dir_migration(
    app: AppHandle,
    state: State<'_, AppState>,
    payload: StartModelDirMigrationRequest,
) -> AppResult<Value> {
    model_dir_migration::start_model_dir_migration(app, state, payload)
}

#[tauri::command]
pub async fn respond_model_dir_migration_conflict(
    state: State<'_, AppState>,
    payload: RespondModelDirMigrationConflictRequest,
) -> AppResult<Value> {
    model_dir_migration::respond_model_dir_migration_conflict(state, payload)
}

#[tauri::command]
pub async fn confirm_model_dir_migration_switch(
    app: AppHandle,
    state: State<'_, AppState>,
    payload: ConfirmModelDirMigrationSwitchRequest,
) -> AppResult<Value> {
    model_dir_migration::confirm_model_dir_migration_switch(app, state, payload)
}

#[tauri::command]
pub async fn cancel_model_dir_migration(
    state: State<'_, AppState>,
    payload: CancelModelDirMigrationRequest,
) -> AppResult<Value> {
    model_dir_migration::cancel_model_dir_migration(state, payload)
}

const AUDIO_EXTENSIONS: &[&str] = &["wav", "mp3", "flac", "m4a", "aac", "ogg", "opus"];
const VIDEO_EXTENSIONS: &[&str] = &["mp4", "mkv", "mov", "avi", "webm", "flv"];

#[derive(Debug, Serialize)]
pub struct ScanAudioPathsResult {
    pub files: Vec<String>,
    pub warnings: Vec<String>,
}

fn is_audio_file(path: &Path) -> bool {
    path.extension()
        .and_then(|ext| ext.to_str())
        .map(|ext| AUDIO_EXTENSIONS.contains(&ext.to_lowercase().as_str()))
        .unwrap_or(false)
}

fn is_media_file(path: &Path) -> bool {
    path.extension()
        .and_then(|ext| ext.to_str())
        .map(|ext| {
            let ext = ext.to_lowercase();
            AUDIO_EXTENSIONS.contains(&ext.as_str()) || VIDEO_EXTENSIONS.contains(&ext.as_str())
        })
        .unwrap_or(false)
}

fn collect_audio_files(dir: &Path, results: &mut Vec<String>, warnings: &mut Vec<String>) {
    let entries = match std::fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(error) => {
            warnings.push(format!("{}: {}", dir.display(), error));
            return;
        }
    };
    for entry in entries {
        match entry {
            Ok(entry) => {
                let path = entry.path();
                if path.is_dir() {
                    collect_audio_files(&path, results, warnings);
                } else if path.is_file() && is_audio_file(&path) {
                    results.push(path.to_string_lossy().to_string());
                }
            }
            Err(error) => warnings.push(format!("{}: {}", dir.display(), error)),
        }
    }
}

fn collect_media_files(dir: &Path, results: &mut Vec<String>, warnings: &mut Vec<String>) {
    let entries = match std::fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(error) => {
            warnings.push(format!("{}: {}", dir.display(), error));
            return;
        }
    };
    for entry in entries {
        match entry {
            Ok(entry) => {
                let path = entry.path();
                if path.is_dir() {
                    collect_media_files(&path, results, warnings);
                } else if path.is_file() && is_media_file(&path) {
                    results.push(path.to_string_lossy().to_string());
                }
            }
            Err(error) => warnings.push(format!("{}: {}", dir.display(), error)),
        }
    }
}

fn collect_audio_files_shallow(dir: &Path, results: &mut Vec<String>, warnings: &mut Vec<String>) {
    let entries = match std::fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(error) => {
            warnings.push(format!("{}: {}", dir.display(), error));
            return;
        }
    };
    for entry in entries {
        match entry {
            Ok(entry) => {
                let path = entry.path();
                if path.is_file() && is_audio_file(&path) {
                    results.push(path.to_string_lossy().to_string());
                }
            }
            Err(error) => warnings.push(format!("{}: {}", dir.display(), error)),
        }
    }
}

fn stable_dedup(paths: Vec<String>) -> Vec<String> {
    let mut seen = std::collections::HashSet::new();
    let mut deduped = Vec::with_capacity(paths.len());
    for path in paths {
        if seen.insert(path.clone()) {
            deduped.push(path);
        }
    }
    deduped
}

#[tauri::command]
pub async fn scan_audio_paths(paths: Vec<String>) -> AppResult<ScanAudioPathsResult> {
    let mut files = Vec::new();
    let mut warnings = Vec::new();
    for raw in paths {
        let target = Path::new(&raw);
        if target.is_file() {
            if is_audio_file(target) {
                files.push(target.to_string_lossy().to_string());
            } else {
                warnings.push(format!("unsupported file: {}", target.display()));
            }
        } else if target.is_dir() {
            collect_audio_files(target, &mut files, &mut warnings);
        } else {
            warnings.push(format!("path not found: {}", raw));
        }
    }
    files.sort();
    files.dedup();
    Ok(ScanAudioPathsResult { files, warnings })
}

#[tauri::command]
pub async fn scan_audio_paths_with_options(
    paths: Vec<String>,
    recursive: Option<bool>,
    sort_files: Option<bool>,
) -> AppResult<ScanAudioPathsResult> {
    let recursive = recursive.unwrap_or(true);
    let sort_files = sort_files.unwrap_or(true);
    let mut files = Vec::new();
    let mut warnings = Vec::new();
    for raw in paths {
        let target = Path::new(&raw);
        if target.is_file() {
            if is_audio_file(target) {
                files.push(target.to_string_lossy().to_string());
            } else {
                warnings.push(format!("unsupported file: {}", target.display()));
            }
        } else if target.is_dir() {
            if recursive {
                collect_audio_files(target, &mut files, &mut warnings);
            } else {
                collect_audio_files_shallow(target, &mut files, &mut warnings);
            }
        } else {
            warnings.push(format!("path not found: {}", raw));
        }
    }
    if sort_files {
        files.sort();
        files.dedup();
    } else {
        files = stable_dedup(files);
    }
    Ok(ScanAudioPathsResult { files, warnings })
}

#[tauri::command]
pub async fn scan_media_paths(paths: Vec<String>) -> AppResult<ScanAudioPathsResult> {
    let mut files = Vec::new();
    let mut warnings = Vec::new();
    for raw in paths {
        let target = Path::new(&raw);
        if target.is_file() {
            if is_media_file(target) {
                files.push(target.to_string_lossy().to_string());
            } else {
                warnings.push(format!("unsupported file: {}", target.display()));
            }
        } else if target.is_dir() {
            collect_media_files(target, &mut files, &mut warnings);
        } else {
            warnings.push(format!("path not found: {}", raw));
        }
    }
    files.sort();
    files.dedup();
    Ok(ScanAudioPathsResult { files, warnings })
}

#[tauri::command]
pub async fn list_audio_files(path: String) -> AppResult<Vec<String>> {
    Ok(scan_audio_paths(vec![path]).await?.files)
}

fn resolve_reveal_path(app: &AppHandle, path: &str) -> AppResult<PathBuf> {
    let target = PathBuf::from(path);
    if target.is_absolute() {
        return Ok(target);
    }

    // Worker output paths may be entered as a relative directory. Keep this
    // resolution identical to the worker's PYMSS_STUDIO_DEFAULT_OUTPUT_DIR
    // handling so Explorer opens the directory where inference wrote files.
    let output_root = storage::outputs_dir(app)?;
    Ok(output_root
        .parent()
        .unwrap_or(output_root.as_path())
        .join(target))
}

#[tauri::command]
pub async fn reveal_path(app: AppHandle, path: String) -> AppResult<()> {
    let resolved = resolve_reveal_path(&app, &path)?;
    let target = resolved.as_path();
    let reveal_target = reveal_target_path(target);
    #[cfg(target_os = "windows")]
    {
        Command::new("explorer").arg(reveal_target).spawn()?;
    }
    #[cfg(target_os = "macos")]
    {
        Command::new("open").arg(reveal_target).spawn()?;
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        Command::new("xdg-open").arg(reveal_target).spawn()?;
    }
    Ok(())
}

#[tauri::command]
pub async fn begin_editor_recording(
    app: AppHandle,
    project_id: String,
) -> AppResult<EditorRecordingHandle> {
    let recordings_dir = editor_recordings_dir(&app, &project_id)?;
    std::fs::create_dir_all(&recordings_dir)?;
    let base = now_millis();
    for suffix in 0..1000u16 {
        let recording_id = if suffix == 0 {
            format!("recording_{base}")
        } else {
            format!("recording_{base}_{suffix}")
        };
        let partial_path = recordings_dir.join(format!("{recording_id}.pcm.partial"));
        match OpenOptions::new().write(true).create_new(true).open(partial_path) {
            Ok(_) => return Ok(EditorRecordingHandle { recording_id }),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error.into()),
        }
    }
    Err(AppError::Worker("could not allocate a recording file".into()))
}

#[tauri::command]
pub async fn append_editor_recording_chunk(
    app: AppHandle,
    project_id: String,
    recording_id: String,
    data_base64: String,
) -> AppResult<()> {
    let recording_id = validate_recording_id(&recording_id)?;
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(data_base64)
        .map_err(|error| AppError::Worker(format!("invalid recording chunk: {error}")))?;
    if bytes.is_empty() || bytes.len() > 2 * 1024 * 1024 || bytes.len() % 2 != 0 {
        return Err(AppError::Worker("invalid recording chunk size".into()));
    }
    let partial_path = editor_recordings_dir(&app, &project_id)?
        .join(format!("{recording_id}.pcm.partial"));
    let mut file = OpenOptions::new().append(true).open(partial_path)?;
    file.write_all(&bytes)?;
    Ok(())
}

#[tauri::command]
pub async fn finish_editor_recording(
    app: AppHandle,
    project_id: String,
    recording_id: String,
    sample_rate: u32,
    channels: u16,
) -> AppResult<EditorRecordingResult> {
    let recording_id = validate_recording_id(&recording_id)?;
    let recordings_dir = editor_recordings_dir(&app, &project_id)?;
    let partial_path = recordings_dir.join(format!("{recording_id}.pcm.partial"));
    let data_len_u64 = std::fs::metadata(&partial_path)?.len();
    if data_len_u64 == 0 || data_len_u64 % 2 != 0 || data_len_u64 > u32::MAX as u64 - 36 {
        return Err(AppError::Worker("recording does not contain valid PCM audio".into()));
    }
    let data_len = data_len_u64 as u32;
    let output_path = recordings_dir.join(format!("{recording_id}.wav"));
    let output_partial_path = recordings_dir.join(format!("{recording_id}.wav.partial"));
    let mut input = std::fs::File::open(&partial_path)?;
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&output_partial_path)?;
    let write_result = (|| -> AppResult<()> {
        output.write_all(&pcm_wav_header(data_len, sample_rate, channels)?)?;
        std::io::copy(&mut input, &mut output)?;
        output.flush()?;
        output.sync_all()?;
        Ok(())
    })();
    drop(output);
    if let Err(error) = write_result {
        let _ = std::fs::remove_file(&output_partial_path);
        return Err(error);
    }
    std::fs::rename(&output_partial_path, &output_path)?;
    std::fs::remove_file(partial_path)?;

    let duration = data_len as f64 / (sample_rate as f64 * channels as f64 * 2.0);
    Ok(EditorRecordingResult {
        path: output_path.to_string_lossy().to_string(),
        name: format!("{recording_id}.wav"),
        duration,
        sample_rate,
        channels,
    })
}

#[tauri::command]
pub async fn cancel_editor_recording(
    app: AppHandle,
    project_id: String,
    recording_id: String,
) -> AppResult<()> {
    let recording_id = validate_recording_id(&recording_id)?;
    let partial_path = editor_recordings_dir(&app, &project_id)?
        .join(format!("{recording_id}.pcm.partial"));
    let wav_partial_path = editor_recordings_dir(&app, &project_id)?
        .join(format!("{recording_id}.wav.partial"));
    let mut first_error = None;
    for path in [partial_path, wav_partial_path] {
        if let Err(error) = std::fs::remove_file(path) {
            if error.kind() != std::io::ErrorKind::NotFound && first_error.is_none() {
                first_error = Some(error);
            }
        }
    }
    match first_error {
        Some(error) => Err(error.into()),
        None => Ok(()),
    }
}

#[tauri::command]
pub async fn discard_editor_recording(
    app: AppHandle,
    project_id: String,
    recording_id: String,
) -> AppResult<()> {
    let recording_id = validate_recording_id(&recording_id)?;
    let output_path = editor_recordings_dir(&app, &project_id)?
        .join(format!("{recording_id}.wav"));
    match std::fs::remove_file(output_path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.into()),
    }
}

fn reveal_target_path(path: &Path) -> &Path {
    if path.is_file() {
        return path.parent().unwrap_or(path);
    }
    if path.is_dir() {
        return path;
    }
    path.ancestors()
        .skip(1)
        .find(|ancestor| ancestor.is_dir())
        .unwrap_or_else(|| path.parent().unwrap_or(path))
}

#[derive(Serialize)]
pub struct TrashResult {
    pub trashed: Vec<String>,
    pub failed: Vec<String>,
}

#[tauri::command]
pub async fn start_runtime_core_update(
    app: AppHandle,
    state: State<'_, AppState>,
    payload: Value,
) -> AppResult<Value> {
    let task_id = payload
        .get("taskId")
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| format!("runtime_core_update_{}", chrono_like_timestamp()));
    let mut payload = payload;
    if let Some(object) = payload.as_object_mut() {
        object.insert("taskId".to_string(), Value::String(task_id.clone()));
    }
    spawn_worker_background(app, state, "update_runtime_core", task_id.clone(), payload)?;
    Ok(serde_json::json!({ "taskId": task_id, "started": true }))
}

fn is_ignored_empty_dir_file(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name == ".DS_Store")
}

fn is_effectively_empty_dir(path: &Path) -> bool {
    let entries = match std::fs::read_dir(path) {
        Ok(entries) => entries,
        Err(_) => return false,
    };
    for entry in entries {
        let Ok(entry) = entry else {
            return false;
        };
        let child = entry.path();
        if child.is_dir() {
            if !is_effectively_empty_dir(&child) {
                return false;
            }
            continue;
        }
        if !child.is_file() || !is_ignored_empty_dir_file(&child) {
            return false;
        }
    }
    true
}

fn normalize_trash_empty_dirs(paths: Vec<String>) -> Vec<String> {
    let mut seen = std::collections::HashSet::new();
    let mut dirs: Vec<String> = paths
        .into_iter()
        .filter(|path| !path.trim().is_empty())
        .filter(|path| seen.insert(path.to_lowercase()))
        .collect();
    dirs.sort_by_key(|path| {
        std::cmp::Reverse(Path::new(path).components().count())
    });
    dirs
}

#[tauri::command]
pub async fn move_paths_to_trash(paths: Vec<String>, empty_dirs: Option<Vec<String>>) -> AppResult<TrashResult> {
    let mut trashed = Vec::new();
    let mut failed = Vec::new();
    for path in paths {
        let target = Path::new(&path);
        // 已不存在的路径视为已删除，无需报错
        if !target.exists() {
            trashed.push(path);
            continue;
        }
        match trash::delete(target) {
            Ok(()) => trashed.push(path),
            Err(_) => failed.push(path),
        }
    }
    for path in normalize_trash_empty_dirs(empty_dirs.unwrap_or_default()) {
        let target = Path::new(&path);
        if !target.exists() {
            trashed.push(path);
            continue;
        }
        if !target.is_dir() {
            continue;
        }
        if !is_effectively_empty_dir(target) {
            continue;
        }
        match trash::delete(target) {
            Ok(()) => trashed.push(path),
            Err(_) => failed.push(path),
        }
    }
    Ok(TrashResult { trashed, failed })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_test_dir(name: &str) -> PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system time should be after unix epoch")
            .as_nanos();
        std::env::temp_dir().join(format!("pymss-studio-{name}-{unique}"))
    }

    #[test]
    fn effectively_empty_dir_allows_ds_store_and_empty_children() {
        let root = temp_test_dir("empty-dir-ds-store");
        let child = root.join("album").join("track");
        fs::create_dir_all(&child).expect("create nested test dirs");
        fs::write(root.join(".DS_Store"), b"finder metadata").expect("write ds store");
        fs::write(child.join(".DS_Store"), b"finder metadata").expect("write nested ds store");

        assert!(is_effectively_empty_dir(&root));

        fs::remove_dir_all(&root).expect("remove test dir");
    }

    #[test]
    fn effectively_empty_dir_rejects_user_files() {
        let root = temp_test_dir("empty-dir-user-file");
        fs::create_dir_all(&root).expect("create test dir");
        fs::write(root.join("vocals.wav"), b"audio").expect("write user file");

        assert!(!is_effectively_empty_dir(&root));

        fs::remove_dir_all(&root).expect("remove test dir");
    }

    #[test]
    fn reveal_target_falls_back_to_existing_parent_for_missing_file() {
        let root = temp_test_dir("reveal-missing-file");
        let missing = root.join("runtime").join("install.log");
        fs::create_dir_all(missing.parent().unwrap()).expect("create reveal parent");

        assert_eq!(reveal_target_path(&missing), missing.parent().unwrap());

        fs::remove_dir_all(&root).expect("remove reveal test dir");
    }

    #[test]
    fn reveal_target_uses_the_file_parent_for_existing_file() {
        let root = temp_test_dir("reveal-existing-file");
        let file = root.join("install.log");
        fs::create_dir_all(&root).expect("create reveal root");
        fs::write(&file, b"log").expect("write reveal file");

        assert_eq!(reveal_target_path(&file), root.as_path());

        fs::remove_dir_all(&root).expect("remove reveal test dir");
    }

    #[test]
    fn pcm_wav_header_describes_mono_pcm16_audio() {
        let header = pcm_wav_header(96_000, 48_000, 1).expect("create wav header");

        assert_eq!(&header[0..4], b"RIFF");
        assert_eq!(u32::from_le_bytes(header[4..8].try_into().unwrap()), 96_036);
        assert_eq!(&header[8..12], b"WAVE");
        assert_eq!(u16::from_le_bytes(header[20..22].try_into().unwrap()), 1);
        assert_eq!(u16::from_le_bytes(header[22..24].try_into().unwrap()), 1);
        assert_eq!(u32::from_le_bytes(header[24..28].try_into().unwrap()), 48_000);
        assert_eq!(u32::from_le_bytes(header[28..32].try_into().unwrap()), 96_000);
        assert_eq!(u16::from_le_bytes(header[34..36].try_into().unwrap()), 16);
        assert_eq!(u32::from_le_bytes(header[40..44].try_into().unwrap()), 96_000);
    }

    #[test]
    fn recording_id_validation_rejects_path_components() {
        assert!(validate_recording_id("recording_1720000000000").is_ok());
        assert!(validate_recording_id("../recording_1720000000000").is_err());
        assert!(validate_recording_id("recording_foo/bar").is_err());
        assert!(validate_recording_id("other_1720000000000").is_err());
    }

    #[test]
    fn editor_project_ids_for_tasks_are_safe_and_deduplicated() {
        let task_ids = vec![
            " task/one ".to_string(),
            "task/one".to_string(),
            "".to_string(),
            "task-two".to_string(),
        ];

        assert_eq!(
            editor_project_ids_for_task_ids(&task_ids),
            vec!["edit_task_one".to_string(), "edit_task-two".to_string()]
        );
    }

    #[test]
    fn editor_project_cleanup_removes_existing_dirs_and_reports_missing_ones() {
        let root = temp_test_dir("editor-project-cleanup");
        let existing_id = "edit_existing".to_string();
        let missing_id = "edit_missing".to_string();
        let recordings = root.join(&existing_id).join("recordings");
        fs::create_dir_all(&recordings).expect("create editor project directory");
        fs::write(recordings.join("take.wav"), b"audio").expect("write editor recording");

        let result = remove_editor_project_dirs(
            &root,
            &[existing_id.clone(), missing_id.clone()],
        );

        assert_eq!(result.deleted_project_ids, vec![existing_id.clone()]);
        assert_eq!(result.missing_project_ids, vec![missing_id]);
        assert!(result.blocked_project_ids.is_empty());
        assert!(result.failed_project_ids.is_empty());
        assert!(!root.join(existing_id).exists());
        fs::remove_dir_all(&root).expect("remove editor cleanup test root");
    }

    #[test]
    fn orphaned_editor_projects_only_returns_unlinked_task_projects() {
        let root = temp_test_dir("orphaned-editor-projects");
        let active_dir = root.join("edit_active");
        let orphan_dir = root.join("edit_orphan");
        let blank_dir = root.join("edit_blank_1");
        fs::create_dir_all(&active_dir).expect("create active editor project");
        fs::create_dir_all(&orphan_dir).expect("create orphaned editor project");
        fs::create_dir_all(&blank_dir).expect("create blank editor project");
        fs::write(
            active_dir.join("project.json"),
            r#"{"id":"edit_active","name":"Active","sourceTaskId":"task-active"}"#,
        )
        .expect("write active editor project");
        fs::write(
            orphan_dir.join("project.json"),
            r#"{"id":"edit_orphan","name":"Orphan","sourceTaskId":"task-orphan"}"#,
        )
        .expect("write orphaned editor project");
        fs::write(
            blank_dir.join("project.json"),
            r#"{"id":"edit_blank_1","name":"Blank"}"#,
        )
        .expect("write blank editor project");

        let projects = orphaned_editor_projects(&root, &["task-active".to_string()])
            .expect("list orphaned editor projects");

        assert_eq!(projects.len(), 1);
        assert_eq!(projects[0].project_id, "edit_orphan");
        assert_eq!(projects[0].source_task_id, "task-orphan");
        fs::remove_dir_all(&root).expect("remove orphaned project test root");
    }
}
