use crate::error::{AppError, AppResult};
use crate::python::protocol::WorkerEnvelope;
use crate::session_log;
use crate::state::AppState;
use crate::storage;
use serde::Deserialize;
use serde_json::Value;
use std::collections::HashSet;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Emitter, Manager, State};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

const PYTHON_TERMINAL_LOG_PREFIX: &str = "__PYMSS_STUDIO_TERMINAL_LOG__";

static PAYLOAD_FILE_COUNTER: AtomicU64 = AtomicU64::new(0);

// Core packages are updated in the active environment. Hold a shared lease for every worker
// using that interpreter, and an exclusive lease for pip, including the process-start window.
static RUNTIME_ACCESS: Mutex<RuntimeAccess> = Mutex::new(RuntimeAccess { users: 0, updating: false });

#[derive(Default)]
struct RuntimeAccess {
    users: usize,
    updating: bool,
}

struct RuntimeAccessGuard<'a> {
    access: &'a Mutex<RuntimeAccess>,
    updating: bool,
}

impl Drop for RuntimeAccessGuard<'_> {
    fn drop(&mut self) {
        if let Ok(mut access) = self.access.lock() {
            if self.updating {
                access.updating = false;
            } else {
                access.users -= 1;
            }
        }
    }
}

fn uses_bootstrap_python(command: &str) -> bool {
    matches!(command,
        "health" | "runtime_info" | "runtime_env_sizes" | "runtime_core_versions"
        | "install_runtime" | "activate_runtime" | "delete_runtime" | "update_runtime_core"
        | "test_connection"
    )
}

fn acquire_runtime_access<'a>(
    access: &'a Mutex<RuntimeAccess>, command: &str,
) -> AppResult<Option<RuntimeAccessGuard<'a>>> {
    let updating = command == "update_runtime_core";
    if !updating && uses_bootstrap_python(command) {
        return Ok(None);
    }
    let mut state = access.lock()
        .map_err(|_| AppError::Worker("runtime access lock poisoned".into()))?;
    if state.updating || (updating && state.users > 0) {
        return Err(AppError::Worker(
            "RUNTIME_BUSY: The runtime is in use. Wait for running operations to finish before updating core packages or starting another operation.".into(),
        ));
    }
    if updating {
        state.updating = true;
    } else {
        state.users += 1;
    }
    Ok(Some(RuntimeAccessGuard { access, updating }))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ActiveRuntimeRecord {
    python_path: String,
    backend: Option<String>,
    source: Option<String>,
}

fn worker_path(app: &AppHandle) -> AppResult<PathBuf> {
    // Development builds must execute the workspace worker directly. Tauri's copied
    // resource directory is not refreshed while the dev process is running and can
    // otherwise leave Python modules out of sync with the frontend and Rust code.
    if storage::is_development_executable() {
        let path = dev_worker_path();
        if path.exists() {
            return Ok(path);
        }
    }

    if let Ok(resource) = app.path().resource_dir() {
        if let Some(path) = resource_worker_path(&resource) {
            return Ok(path);
        }
    }

    let exe_dir = std::env::current_exe()?
        .parent()
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    let portable_worker = exe_dir.join("python").join("worker.py");
    if portable_worker.exists() {
        return Ok(portable_worker);
    }

    if storage::is_development_executable() {
        // Fallback: try cwd (for backward compatibility)
        let cwd = std::env::current_dir()?;
        Ok(cwd.join("python").join("worker.py"))
    } else {
        Ok(portable_worker)
    }
}

fn resource_worker_path(resource: &Path) -> Option<PathBuf> {
    storage::resource_roots(resource)
        .into_iter()
        .map(|root| root.join("python").join("worker.py"))
        .chain(std::iter::once(resource.join("worker.py")))
        .find(|path| path.exists())
}

fn dev_workspace_root() -> PathBuf {
    // CARGO_MANIFEST_DIR is set at compile time to pymss-desktop/src-tauri.
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn dev_worker_path() -> PathBuf {
    dev_workspace_root().join("python").join("worker.py")
}

fn embedded_python_path(app: &AppHandle) -> AppResult<Option<PathBuf>> {
    let resource = app.path().resource_dir().ok();
    let exe_dir = std::env::current_exe()?
        .parent()
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    Ok(embedded_python_from(resource.as_deref(), &exe_dir, cfg!(windows)))
}

fn embedded_python_from(resource: Option<&Path>, exe_dir: &Path, windows: bool) -> Option<PathBuf> {
    let mut runtime_dirs: Vec<_> = resource
        .into_iter()
        .flat_map(storage::resource_roots)
        .map(|root| root.join("python-runtime"))
        .collect();
    runtime_dirs.push(exe_dir.join("python-runtime"));

    for runtime in runtime_dirs {
        let candidates = if windows {
            vec![
                runtime.join("python.exe"),
                runtime.join("Scripts").join("python.exe"),
            ]
        } else {
            vec![
                runtime.join("bin").join("python3"),
                runtime.join("bin").join("python"),
            ]
        };
        if let Some(path) = candidates.into_iter().find(|candidate| candidate.is_file()) {
            return Some(path);
        }
    }
    None
}

fn bootstrap_python_path(app: &AppHandle) -> AppResult<String> {
    if let Ok(value) = std::env::var("PYMSS_STUDIO_PYTHON") {
        let path = PathBuf::from(&value);
        if path.is_relative() {
            if let Ok(cwd) = std::env::current_dir() {
                let abs = cwd.join(&path);
                if abs.exists() {
                    return Ok(abs.to_string_lossy().to_string());
                }
            }
            let dev_root = dev_workspace_root();
            let abs_dev = dev_root.join(&path);
            if abs_dev.exists() {
                return Ok(abs_dev.to_string_lossy().to_string());
            }
        }
        return Ok(value);
    }
    if let Some(embedded) = embedded_python_path(app)? {
        return Ok(embedded.to_string_lossy().to_string());
    }
    if cfg!(windows) {
        Ok("python".to_string())
    } else {
        Ok("python3".to_string())
    }
}

fn resolve_active_runtime_record(file: &PathBuf) -> Option<(String, Option<String>)> {
    if !file.is_file() {
        return None;
    }
    let content = std::fs::read_to_string(file).ok()?;
    let record: ActiveRuntimeRecord = serde_json::from_str(&content).ok()?;
    let python = PathBuf::from(&record.python_path);
    // Absolute path – check directly
    if python.is_absolute() {
        return python
            .canonicalize()
            .ok()
            .filter(|path| path.is_file())
            .map(|path| (path.to_string_lossy().to_string(), record.source));
    }
    // Relative path – resolve against the directory containing the file
    let parent = file.parent()?;
    let resolved = parent.join(&python);
    resolved
        .canonicalize()
        .ok()
        .filter(|path| path.is_file())
        .map(|path| (path.to_string_lossy().to_string(), record.source))
}

fn bundled_runtime_envs_dir(app: &AppHandle) -> AppResult<Option<PathBuf>> {
    let user_runtime = storage::runtime_envs_dir(app)?.canonicalize().ok();
    Ok(distinct_bundled_runtime_env(
        user_runtime,
        storage::bundled_runtime_envs_dir(app)?,
    ))
}

fn distinct_bundled_runtime_env(
    user_runtime: Option<PathBuf>, bundled: Option<PathBuf>,
) -> Option<PathBuf> {
    bundled.filter(|path| path.canonicalize().ok() != user_runtime)
}

fn active_runtime_python_path(app: &AppHandle) -> AppResult<Option<String>> {
    let user_file = storage::active_runtime_file(app)?;
    if let Some((path, _source)) = resolve_active_runtime_record(&user_file) {
        if is_user_runtime_python_path(app, &path)? && active_path_backend_matches(&user_file, &path) {
            return Ok(Some(path));
        }
    };
    if let Some(envs_dir) = bundled_runtime_envs_dir(app)? {
        let bundled_file = envs_dir.join("active-runtime.json");
        if let Some((path, _source)) = resolve_active_runtime_record(&bundled_file) {
            if is_bundled_runtime_python_path(&bundled_file, &path)? {
                return Ok(Some(path));
            }
        }
    }
    Ok(None)
}

fn is_bundled_runtime_python_path(file: &Path, python_path: &str) -> AppResult<bool> {
    let content = std::fs::read_to_string(file)?;
    let record: ActiveRuntimeRecord = serde_json::from_str(&content)?;
    let backend = record.backend.unwrap_or_default().trim().to_ascii_lowercase();
    if !matches!(backend.as_str(), "cpu" | "cuda" | "rocm" | "mlx") {
        return Ok(false);
    }
    let Some(envs_dir) = file.parent() else {
        return Ok(false);
    };
    let envs_dir = envs_dir.canonicalize()?;
    let runtime_root = envs_dir
        .parent()
        .ok_or_else(|| AppError::Worker("bundled runtime root is missing".into()))?;
    let python = PathBuf::from(python_path).canonicalize()?;
    let bootstrap_matches = [
        runtime_root.join("python.exe"),
        runtime_root.join("bin").join("python3"),
        runtime_root.join("bin").join("python"),
    ]
    .into_iter()
    .filter_map(|candidate| candidate.canonicalize().ok())
    .any(|candidate| candidate == python);
    if bootstrap_matches {
        return Ok(backend == "mlx");
    }
    let Some(env_dir) = python.parent().and_then(Path::parent) else {
        return Ok(false);
    };
    Ok(env_dir.starts_with(&envs_dir)
        && env_dir.file_name().and_then(|name| name.to_str()).is_some_and(|name| name.eq_ignore_ascii_case(&backend)))
}

fn active_path_backend_matches(file: &Path, python_path: &str) -> bool {
    let Ok(content) = std::fs::read_to_string(file) else {
        return false;
    };
    let Ok(record) = serde_json::from_str::<ActiveRuntimeRecord>(&content) else {
        return false;
    };
    let Some(backend) = record.backend.filter(|value| !value.trim().is_empty()) else {
        return false;
    };
    if !matches!(backend.trim().to_ascii_lowercase().as_str(), "cpu" | "cuda" | "rocm" | "mlx") {
        return false;
    }
    let path = PathBuf::from(python_path);
    path.parent()
        .and_then(Path::parent)
        .and_then(|env| env.file_name())
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.eq_ignore_ascii_case(backend.trim()))
}

fn is_user_runtime_python_path(app: &AppHandle, path: &str) -> AppResult<bool> {
    let runtime_envs = storage::runtime_envs_dir(app)?;
    let runtime_root = runtime_envs.canonicalize().unwrap_or(runtime_envs);
    let python_path = PathBuf::from(&path);
    let env_dir = python_path
        .parent()
        .and_then(Path::parent)
        .map(Path::to_path_buf);
    Ok(env_dir
        .and_then(|path| path.canonicalize().ok())
        .is_some_and(|path| path.starts_with(&runtime_root))
    )
}

fn bundled_bin_dirs(app: &AppHandle) -> AppResult<Vec<PathBuf>> {
    let resource = app.path().resource_dir().ok();
    let exe_dir = std::env::current_exe()?
        .parent()
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    Ok(bundled_bin_candidates(resource.as_deref(), &exe_dir, cfg!(target_os = "macos"))
        .into_iter()
        .filter(|dir| dir.is_dir())
        .collect())
}

fn bundled_bin_candidates(resource: Option<&Path>, exe_dir: &Path, macos: bool) -> Vec<PathBuf> {
    let mut dirs: Vec<_> = resource
        .into_iter()
        .flat_map(storage::resource_roots)
        .map(|root| root.join("bin"))
        .collect();
    dirs.push(exe_dir.join("bin"));
    if macos {
        dirs.push(PathBuf::from("/opt/homebrew/bin"));
        dirs.push(PathBuf::from("/usr/local/bin"));
    }

    dirs
}

#[cfg(windows)]
fn rocm_native_tool_dir(runtime_envs_dir: &Path) -> Option<PathBuf> {
    let site_packages = runtime_envs_dir.join("rocm").join("Lib").join("site-packages");
    let entries = std::fs::read_dir(site_packages).ok()?;
    entries
        .flatten()
        .map(|entry| entry.path())
        .find_map(|package_dir| {
            let name = package_dir.file_name()?.to_str()?;
            let tool_dir = package_dir.join("lib").join("llvm").join("bin");
            (name.starts_with("_rocm_sdk_core")
                && tool_dir.join("offload-arch.exe").is_file())
            .then_some(tool_dir)
        })
}

#[cfg(windows)]
fn rocm_native_tool_dirs(app: &AppHandle) -> AppResult<Vec<PathBuf>> {
    let mut runtime_envs_dirs = vec![storage::runtime_envs_dir(app)?];
    runtime_envs_dirs.extend(bundled_runtime_envs_dir(app)?);
    Ok(rocm_native_tool_dirs_from(runtime_envs_dirs))
}

#[cfg(windows)]
fn rocm_native_tool_dirs_from(runtime_envs_dirs: Vec<PathBuf>) -> Vec<PathBuf> {
    let mut result = Vec::new();
    for runtime_envs in runtime_envs_dirs {
        let Some(tool_dir) = rocm_native_tool_dir(&runtime_envs) else {
            continue;
        };
        let sdk_bin = tool_dir
            .parent()
            .and_then(|path| path.parent())
            .and_then(|path| path.parent())
            .map(|path| path.join("bin"));
        result.push(tool_dir);
        if let Some(sdk_bin) = sdk_bin.filter(|dir| dir.is_dir()) {
            result.push(sdk_bin);
        }
    }
    result
}

#[cfg(not(windows))]
fn rocm_native_tool_dirs(_app: &AppHandle) -> AppResult<Vec<PathBuf>> {
    Ok(Vec::new())
}

#[cfg(target_os = "macos")]
fn bundled_openssl_env(app: &AppHandle) -> AppResult<Option<(PathBuf, PathBuf)>> {
    for bin_dir in bundled_bin_dirs(app)? {
        let openssl_dir = bin_dir.join("openssl");
        let openssl_conf = openssl_dir.join("openssl.cnf");
        let openssl_modules = openssl_dir.join("ossl-modules");
        if openssl_conf.is_file() && openssl_modules.is_dir() {
            return Ok(Some((openssl_conf, openssl_modules)));
        }
    }
    Ok(None)
}

fn path_separator() -> &'static str {
    if cfg!(windows) {
        ";"
    } else {
        ":"
    }
}

fn prepend_path(existing: Option<String>, dirs: Vec<PathBuf>) -> Option<String> {
    if dirs.is_empty() {
        return existing;
    }

    let mut parts: Vec<String> = dirs
        .into_iter()
        .map(|dir| dir.to_string_lossy().to_string())
        .collect();

    if let Some(value) = existing {
        if !value.trim().is_empty() {
            parts.push(value);
        }
    }

    Some(parts.join(path_separator()))
}

fn default_output_dir(app: &AppHandle) -> AppResult<PathBuf> {
    storage::outputs_dir(app)
}

fn make_payload_file(command: &str, task_id: Option<&str>, payload: Value) -> AppResult<PathBuf> {
    let mut path = std::env::temp_dir();
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|value| value.as_nanos())
        .unwrap_or_default();
    let sequence = PAYLOAD_FILE_COUNTER.fetch_add(1, Ordering::Relaxed);
    path.push(format!(
        "pymss-studio-payload-{}-{}-{}-{}-{}.json",
        command,
        task_id.unwrap_or("once"),
        std::process::id(),
        stamp,
        sequence
    ));
    let mut file = std::fs::File::create(&path)?;
    file.write_all(serde_json::to_string(&payload)?.as_bytes())?;
    Ok(path)
}

fn build_worker_command(
    app: &AppHandle,
    command: &str,
    payload_file: Option<&PathBuf>,
) -> AppResult<Command> {
    let worker = worker_path(app)?;
    let bootstrap_python = bootstrap_python_path(app)?;
    // Runtime management must never run inside the environment it is managing: the bootstrap
    // interpreter is the only one guaranteed to exist while an environment is being built,
    // switched, or deleted.
    let python = if uses_bootstrap_python(command) {
        bootstrap_python.clone()
    } else {
        active_runtime_python_path(app)?.ok_or_else(|| {
            AppError::Worker(
                "No active runtime environment is installed. Open Settings to install one before running this operation.".into(),
            )
        })?
    };
    let python_for_log = python.clone();
    let worker_for_log = worker.clone();
    let mut cmd = Command::new(&python);
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    cmd.arg(worker)
        .arg(command)
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("PYTHONUTF8", "1")
        .env("PYMSS_STUDIO_BOOTSTRAP_PYTHON", &bootstrap_python)
        .env(
            "PYMSS_STUDIO_DEFAULT_OUTPUT_DIR",
            default_output_dir(app)?.to_string_lossy().to_string(),
        );
    if let Some(path) = session_log::log_env_path(app) {
        cmd.env("PYMSS_STUDIO_SESSION_LOG", path)
            .env("PYMSS_STUDIO_DEBUG_LOG", "1");
    }
    if crate::terminal::is_attached() {
        cmd.env("PYMSS_STUDIO_TERMINAL_LOG", "1");
    }
    if let Some(path) = session_log::persistent_log_env_path(app) {
        cmd.env("PYMSS_STUDIO_PERSISTENT_LOG", path);
    }
    if let Ok(dir) = storage::runtime_envs_dir(app) {
        cmd.env("PYMSS_STUDIO_RUNTIME_ENVS_DIR", dir.to_string_lossy().to_string());
    }
    if let Ok(file) = storage::active_runtime_file(app) {
        cmd.env("PYMSS_STUDIO_ACTIVE_RUNTIME_FILE", file.to_string_lossy().to_string());
    }
    if let Some(dir) = bundled_runtime_envs_dir(app)? {
        cmd.env("PYMSS_STUDIO_BUNDLED_RUNTIME_ENVS_DIR", dir.to_string_lossy().to_string());
    }
    apply_proxy_env(app, &mut cmd);
    let mut tool_dirs = rocm_native_tool_dirs(app)?;
    tool_dirs.extend(bundled_bin_dirs(app)?);
    if let Some(path) = prepend_path(std::env::var("PATH").ok(), tool_dirs) {
        cmd.env("PATH", path);
    }
    #[cfg(target_os = "macos")]
    if python == bootstrap_python {
        let embedded = embedded_python_path(app)?;
        if let Some(embedded) = embedded {
            if let Some(runtime_root) = embedded.parent().and_then(|path| path.parent()) {
                cmd.env("PYTHONHOME", runtime_root.to_string_lossy().to_string());
            }
        }
    }
    #[cfg(target_os = "macos")]
    if let Some((openssl_conf, openssl_modules)) = bundled_openssl_env(app)? {
        cmd.env(
            "PYMSS_STUDIO_OPENSSL_CONF",
            openssl_conf.to_string_lossy().to_string(),
        );
        cmd.env(
            "PYMSS_STUDIO_OPENSSL_MODULES",
            openssl_modules.to_string_lossy().to_string(),
        );
    }
    if let Ok(models_dir) = storage::models_dir(app) {
        cmd.env("PYMSS_MODEL_DIR", models_dir.to_string_lossy().to_string());
    }
    // Keep the custom-model registry with the app's own state rather than in pymss's default
    // ~/.cache location, so a portable install carries its imported models with it.
    // pymss reads this at import time, which is why it has to be set before spawning.
    if let Ok(file) = storage::user_models_file(app) {
        cmd.env("PYMSS_USER_MODELS", file.to_string_lossy().to_string());
    }
    if let Ok(dir) = storage::data_root_dir(app).map(|root| root.join("debug")) {
        cmd.env("PYMSS_STUDIO_DEBUG_DIR", dir.to_string_lossy().to_string());
    }
    if let Some(path) = payload_file {
        cmd.arg("--payload").arg(path);
    }
    session_log::append(
        app,
        "INFO",
        "worker.command",
        vec![
            ("command", command.to_string()),
            ("python", python_for_log),
            ("worker", worker_for_log.to_string_lossy().to_string()),
            (
                "payloadFile",
                payload_file
                    .map(|path| path.to_string_lossy().to_string())
                    .unwrap_or_default(),
            ),
        ],
    );
    Ok(cmd)
}

fn normalize_proxy_url(url: &str) -> String {
    let trimmed = url.trim();
    if trimmed.is_empty() {
        return String::new();
    }
    if trimmed.contains("://") {
        return trimmed.to_string();
    }
    format!("http://{}", trimmed)
}

fn apply_proxy_env(app: &AppHandle, cmd: &mut Command) {
    let Some(state) = app.try_state::<AppState>() else {
        return;
    };
    let Ok(proxy) = state.proxy_settings.lock() else {
        return;
    };
    let config = serde_json::json!({
        "mode": proxy.mode,
        "url": proxy.url,
        "bypass": proxy.bypass,
    });
    cmd.env("PYMSS_STUDIO_PROXY_CONFIG", config.to_string());
    match proxy.mode.as_str() {
        "none" => {
            cmd.env("PYMSS_STUDIO_PROXY_MODE", "none")
                .env("NO_PROXY", "*")
                .env("HTTP_PROXY", "")
                .env("HTTPS_PROXY", "")
                .env("http_proxy", "")
                .env("https_proxy", "");
        }
        "custom" => {
            let url = normalize_proxy_url(proxy.url.trim());
            if !url.is_empty() {
                cmd.env("HTTP_PROXY", &url)
                    .env("HTTPS_PROXY", &url)
                    .env("http_proxy", &url)
                    .env("https_proxy", &url)
                    .env("PYMSS_STUDIO_PROXY_MODE", "custom");
            }
            let bypass = proxy.bypass.trim();
            if !bypass.is_empty() {
                cmd.env("NO_PROXY", bypass).env("no_proxy", bypass);
            }
        }
        "system" => {
            cmd.env("PYMSS_STUDIO_PROXY_MODE", "system");
        }
        _ => {
            cmd.env("PYMSS_STUDIO_PROXY_MODE", "system");
        }
    }
}

fn emit_worker_stderr(app: &AppHandle, line: String) {
    session_log::append(
        app,
        "WARN",
        "worker.stderr",
        vec![("message", line.clone())],
    );
    let _ = app.emit(
        "pymss://worker-event",
        serde_json::json!({
            "type": "worker_stderr",
            "payload": { "message": line }
        }),
    );
}

fn forward_python_terminal_log(line: &str) -> bool {
    let Some(line) = line.strip_prefix(PYTHON_TERMINAL_LOG_PREFIX) else {
        return false;
    };
    crate::terminal::write(&format!("{line}\n"));
    true
}

fn emit_task_log(app: &AppHandle, task_id: &str, level: &str, message: String) {
    session_log::append(
        app,
        if level.eq_ignore_ascii_case("error") { "ERROR" } else { "WARN" },
        "worker.stderr",
        vec![("taskId", task_id.to_string()), ("message", message.clone())],
    );
    let _ = app.emit(
        "pymss://worker-event",
        serde_json::json!({
            "type": "task_log",
            "taskId": task_id,
            "payload": { "level": level, "message": message }
        }),
    );
}

fn emit_task_error(app: &AppHandle, task_id: &str, message: String) {
    let _ = app.emit(
        "pymss://worker-event",
        serde_json::json!({
            "type": "error",
            "taskId": task_id,
            "payload": { "message": message }
        }),
    );
}
fn emit_task_error_to_all(app: &AppHandle, task_ids: &[String], message: String) {
    for task_id in task_ids {
        emit_task_error(app, task_id, message.clone());
    }
}
fn worker_error_message(envelope: &WorkerEnvelope) -> String {
    envelope
        .payload
        .get("message")
        .and_then(Value::as_str)
        .filter(|message| !message.trim().is_empty())
        .unwrap_or("Worker failed")
        .to_string()
}

fn log_worker_event(app: &AppHandle, command: &str, envelope: &WorkerEnvelope) {
    if envelope.event_type == "env_info" {
        session_log::record_torch_diagnostics(app, &envelope.payload);
    }
    let level = if envelope.event_type == "error" {
        "ERROR"
    } else if is_noisy_worker_event(&envelope.event_type) {
        "DEBUG"
    } else {
        "INFO"
    };
    if level == "DEBUG" && !session_log::developer_mode_enabled() {
        return;
    }
    let payload = &envelope.payload;
    session_log::append(
        app,
        level,
        "worker.event",
        vec![
            ("command", command.to_string()),
            ("type", envelope.event_type.clone()),
            ("taskId", envelope.task_id.clone().unwrap_or_default()),
            ("requestId", envelope.request_id.clone().unwrap_or_default()),
            ("code", payload.get("code").and_then(Value::as_str).unwrap_or_default().to_string()),
            ("stage", payload.get("stage").and_then(Value::as_str).unwrap_or_default().to_string()),
            ("message", payload.get("message").and_then(Value::as_str).unwrap_or_default().to_string()),
            ("detail", payload.get("detail").and_then(Value::as_str).unwrap_or_default().to_string()),
            ("logPath", payload.get("logPath").and_then(Value::as_str).unwrap_or_default().to_string()),
        ],
    );
}

fn is_noisy_worker_event(event_type: &str) -> bool {
    let lower = event_type.to_ascii_lowercase();
    lower.contains("progress") || lower.contains("chunk") || lower.contains("heartbeat")
}

fn summarize_payload(payload: &Value) -> String {
    let Some(object) = payload.as_object() else {
        return payload_type(payload).to_string();
    };
    let mut parts = Vec::new();
    parts.push(format!("keys={}", object.len()));
    for key in ["taskId", "model", "modelName", "backend", "device", "outputFormat", "saveMode"] {
        if let Some(value) = object.get(key).and_then(Value::as_str).filter(|value| !value.is_empty()) {
            parts.push(format!("{key}={value}"));
        }
    }
    for key in ["inputs", "tasks", "models"] {
        if let Some(count) = object.get(key).and_then(Value::as_array).map(|items| items.len()) {
            parts.push(format!("{key}Count={count}"));
        }
    }
    parts.join(" ")
}

fn payload_type(payload: &Value) -> &'static str {
    match payload {
        Value::Null => "null",
        Value::Bool(_) => "bool",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}

fn log_worker_parse_error(
    app: &AppHandle,
    command: &str,
    task_id: Option<&str>,
    err: &serde_json::Error,
    line: &str,
) {
    session_log::append(
        app,
        "ERROR",
        "worker.invalid_stdout",
        vec![
            ("command", command.to_string()),
            ("taskId", task_id.unwrap_or_default().to_string()),
            ("error", err.to_string()),
            ("raw", line.to_string()),
        ],
    );
}

fn is_background_terminal_event(command: &str, event_type: &str) -> bool {
    match command {
        "delete_model" => matches!(
            event_type,
            "error" | "model_delete_done" | "model_delete_failed"
        ),
        "cleanup_model_residual_files" => matches!(
            event_type,
            "error" | "model_residual_cleanup_done" | "model_residual_cleanup_failed"
        ),
        "download_model" => matches!(event_type, "error" | "download_done" | "task_cancelled"),
        "install_runtime" => matches!(
            event_type,
            "error" | "runtime_install_finished" | "task_cancelled"
        ),
        "update_runtime_core" => matches!(
            event_type,
            "error" | "runtime_core_update_finished" | "task_cancelled"
        ),
        "manage_optional_package" => {
            matches!(event_type, "error" | "optional_package_status" | "task_cancelled")
        }
        "import_custom_model" => matches!(
            event_type,
            "error" | "custom_model_import_finished" | "task_cancelled"
        ),
        "infer" | "infer_workflow" => {
            matches!(event_type, "error" | "task_done" | "task_cancelled")
        }
        "audio_tools" => matches!(event_type, "error" | "audio_tool_result" | "task_cancelled"),
        _ => matches!(event_type, "error"),
    }
}

fn read_lossy_lines<R: Read>(reader: R, mut on_line: impl FnMut(String)) {
    let mut reader = BufReader::new(reader);
    let mut buf = Vec::new();
    loop {
        buf.clear();
        match reader.read_until(b'\n', &mut buf) {
            Ok(0) => break,
            Ok(_) => {
                while matches!(buf.last(), Some(b'\n' | b'\r')) {
                    buf.pop();
                }
                on_line(String::from_utf8_lossy(&buf).into_owned());
            }
            Err(_) => break,
        }
    }
}

fn emit_worker_stdout(app: &AppHandle, line: String) {
    let _ = app.emit(
        "pymss://worker-event",
        serde_json::json!({
            "type": "worker_stdout",
            "payload": { "message": line }
        }),
    );
}

fn is_rocm_offload_arch_diagnostic(line: &str) -> bool {
    let message = line.trim();
    message.starts_with("Fatal error in launcher: Unable to create process using")
        || message.contains("offload-arch failed with return code")
        || message == "[stderr]"
        || message.starts_with("[rocm_sdk] offload-arch")
}

pub fn run_worker_once(app: &AppHandle, command: &str) -> AppResult<Value> {
    run_worker_with_payload(app, command, None)
}

pub fn run_worker_with_payload(
    app: &AppHandle,
    command: &str,
    payload: Option<Value>,
) -> AppResult<Value> {
    let _runtime_access = acquire_runtime_access(&RUNTIME_ACCESS, command)?;
    let payload_summary = payload.as_ref().map(summarize_payload).unwrap_or_else(|| "none".to_string());
    session_log::append(
        app,
        "INFO",
        "worker.request",
        vec![("command", command.to_string()), ("payload", payload_summary)],
    );
    let payload_file = match payload {
        Some(value) => Some(make_payload_file(command, None, value)?),
        None => None,
    };
    let mut cmd = match build_worker_command(app, command, payload_file.as_ref()) {
        Ok(command) => command,
        Err(error) => {
            if let Some(path) = payload_file.as_ref() {
                let _ = std::fs::remove_file(path);
            }
            return Err(error);
        }
    };
    let started_at = std::time::Instant::now();
    session_log::append(app, "INFO", "worker.spawn", vec![("command", command.to_string())]);
    let mut child = match cmd.stdout(Stdio::piped()).stderr(Stdio::piped()).spawn() {
        Ok(child) => child,
        Err(error) => {
            if let Some(path) = payload_file.as_ref() {
                let _ = std::fs::remove_file(path);
            }
            return Err(error.into());
        }
    };

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| AppError::Worker("missing worker stdout".into()))?;
    let stderr = child.stderr.take();
    let stderr_app = app.clone();
    let stderr_lines: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let stderr_lines_for_thread = Arc::clone(&stderr_lines);
    let stderr_handle = stderr.map(|stderr| {
        std::thread::spawn(move || {
            read_lossy_lines(stderr, |line| {
                if forward_python_terminal_log(&line) {
                    return;
                }
                if let Ok(mut lines) = stderr_lines_for_thread.lock() {
                    lines.push(line.clone());
                    if lines.len() > 20 {
                        lines.remove(0);
                    }
                }
                emit_worker_stderr(&stderr_app, line);
            });
        })
    });
    let mut last_payload = Value::Null;
    let mut worker_error: Option<AppError> = None;
    read_lossy_lines(stdout, |line| {
        if line.trim().is_empty() || worker_error.is_some() {
            return;
        }
        match serde_json::from_str::<WorkerEnvelope>(&line) {
            Ok(envelope) => {
                last_payload = envelope.payload.clone();
                log_worker_event(app, command, &envelope);
                let _ = app.emit("pymss://worker-event", &envelope);
                if envelope.event_type == "error" {
                    worker_error = Some(AppError::Worker(
                        envelope
                            .payload
                            .get("message")
                            .and_then(Value::as_str)
                            .unwrap_or("worker error")
                            .to_string(),
                    ));
                }
            }
            Err(err) => {
                log_worker_parse_error(app, command, None, &err, &line);
                if is_rocm_offload_arch_diagnostic(&line) {
                    emit_worker_stdout(app, line);
                } else {
                    worker_error = Some(AppError::Worker(format!(
                        "Invalid worker event: {err}; raw={line}"
                    )));
                }
            }
        }
    });
    let status = child.wait()?;
    session_log::append(
        app,
        if status.success() { "INFO" } else { "ERROR" },
        "worker.exit",
        vec![
            ("command", command.to_string()),
            ("status", status.to_string()),
            ("durationMs", started_at.elapsed().as_millis().to_string()),
        ],
    );
    if let Some(handle) = stderr_handle {
        let _ = handle.join();
    }
    if let Some(path) = payload_file.as_ref() {
        let _ = std::fs::remove_file(path);
    }
    if let Some(err) = worker_error {
        session_log::append(
            app,
            "ERROR",
            "worker.error",
            vec![("command", command.to_string()), ("message", err.to_string())],
        );
        return Err(err);
    }
    if !status.success() {
        let detail = stderr_lines
            .lock()
            .ok()
            .map(|lines| lines.join("\n"))
            .filter(|value| !value.trim().is_empty())
            .unwrap_or_else(|| status.to_string());
        return Err(AppError::Worker(format!("worker exited with {detail}")));
    }
    Ok(last_payload)
}

pub fn spawn_worker_background(
    app: AppHandle,
    state: State<'_, AppState>,
    command: &str,
    task_id: String,
    payload: Value,
) -> AppResult<()> {
    let mut runtime_access = acquire_runtime_access(&RUNTIME_ACCESS, command)?;
    let mut registered_task_ids = vec![task_id.clone()];
    if let Some(tasks) = payload.get("tasks").and_then(Value::as_array) {
        for item in tasks {
            if let Some(child_task_id) = item.get("taskId").and_then(Value::as_str) {
                if !registered_task_ids.iter().any(|id| id == child_task_id) {
                    registered_task_ids.push(child_task_id.to_string());
                }
            }
        }
    }
    {
        let tasks = state
            .tasks
            .lock()
            .map_err(|_| AppError::Worker("task registry lock poisoned".into()))?;
        if let Some(existing) = registered_task_ids
            .iter()
            .find(|id| tasks.contains_key(*id))
        {
            return Err(AppError::Worker(format!(
                "task already exists: {}",
                existing
            )));
        }
    }

    let command_name = command.to_string();
    let payload_summary = summarize_payload(&payload);
    session_log::append(
        &app,
        "INFO",
        "worker.request",
        vec![
            ("command", command.to_string()),
            ("taskId", task_id.clone()),
            ("payload", payload_summary),
        ],
    );
    let payload_file = make_payload_file(command, Some(&task_id), payload)?;
    let mut cmd = match build_worker_command(&app, command, Some(&payload_file)) {
        Ok(command) => command,
        Err(error) => {
            let _ = std::fs::remove_file(&payload_file);
            return Err(error);
        }
    };
    let started_at = std::time::Instant::now();
    session_log::append(
        &app,
        "INFO",
        "worker.spawn",
        vec![("command", command.to_string()), ("taskId", task_id.clone())],
    );
    let mut child: Child = match cmd.stdout(Stdio::piped()).stderr(Stdio::piped()).spawn() {
        Ok(child) => child,
        Err(error) => {
            let _ = std::fs::remove_file(&payload_file);
            return Err(error.into());
        }
    };
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| AppError::Worker("missing worker stdout".into()))?;
    let stderr = child.stderr.take();
    let stderr_app = app.clone();
    let stderr_task_ids = registered_task_ids.clone();
    let stderr_handle = stderr.map(|stderr| {
        std::thread::spawn(move || {
            read_lossy_lines(stderr, |line| {
                if forward_python_terminal_log(&line) {
                    return;
                }
                for stderr_task_id in &stderr_task_ids {
                    emit_task_log(&stderr_app, stderr_task_id, "warning", line.clone());
                }
            });
        })
    });
    let shared_child = Arc::new(Mutex::new(child));
    {
        let mut tasks = state
            .tasks
            .lock()
            .map_err(|_| AppError::Worker("task registry lock poisoned".into()))?;
        for registered_task_id in &registered_task_ids {
            tasks.insert(registered_task_id.clone(), shared_child.clone());
        }
    }
    std::thread::spawn(move || {
        let mut terminal_task_ids: HashSet<String> = HashSet::new();
        let mut core_terminal_events = Vec::new();
        let mut core_terminal_error = None;
        read_lossy_lines(stdout, |line| {
            if line.trim().is_empty() {
                return;
            }
            match serde_json::from_str::<WorkerEnvelope>(&line) {
                Ok(envelope) => {
                    if is_background_terminal_event(&command_name, envelope.event_type.as_str()) {
                        if let Some(envelope_task_id) = envelope.task_id.as_deref() {
                            if registered_task_ids.iter().any(|id| id == envelope_task_id) {
                                terminal_task_ids.insert(envelope_task_id.to_string());
                            }
                        } else if envelope.event_type == "error" {
                            if let Some(request_id) = envelope
                                .request_id
                                .as_deref()
                                .filter(|id| registered_task_ids.iter().any(|task_id| task_id == id))
                            {
                                terminal_task_ids.insert(request_id.to_string());
                            } else {
                                let message = worker_error_message(&envelope);
                                if command_name == "update_runtime_core" {
                                    core_terminal_error = Some(message);
                                } else {
                                    emit_task_error_to_all(&app, &registered_task_ids, message);
                                }
                                terminal_task_ids.extend(registered_task_ids.iter().cloned());
                            }
                        } else {
                            terminal_task_ids.insert(task_id.clone());
                        }
                    }
                    if command_name == "update_runtime_core"
                        && is_background_terminal_event(&command_name, envelope.event_type.as_str())
                    {
                        core_terminal_events.push(envelope);
                        return;
                    }
                    log_worker_event(&app, &command_name, &envelope);
                    let _ = app.emit("pymss://worker-event", &envelope);
                }
                Err(err) => {
                    log_worker_parse_error(&app, &command_name, Some(&task_id), &err, &line);
                    if is_rocm_offload_arch_diagnostic(&line) {
                        for registered_task_id in &registered_task_ids {
                            emit_task_log(&app, registered_task_id, "warning", line.clone());
                        }
                    } else {
                        emit_task_error_to_all(
                            &app,
                            &registered_task_ids,
                            format!("Invalid worker event: {err}"),
                        );
                        terminal_task_ids.extend(registered_task_ids.iter().cloned());
                    }
                }
            }
        });
        let exit_status = if let Ok(mut child) = shared_child.lock() {
            child.wait().ok()
        } else {
            None
        };
        drop(runtime_access.take());
        // Finish Python cleanup before the terminal event starts the UI's environment refresh.
        if let Some(message) = core_terminal_error {
            emit_task_error_to_all(&app, &registered_task_ids, message);
        }
        for envelope in core_terminal_events {
            log_worker_event(&app, &command_name, &envelope);
            let _ = app.emit("pymss://worker-event", &envelope);
        }
        session_log::append(
            &app,
            match exit_status.as_ref() {
                Some(status) if status.success() => "INFO",
                _ => "ERROR",
            },
            "worker.exit",
            vec![
                ("command", command_name.clone()),
                ("taskId", task_id.clone()),
                (
                    "status",
                    exit_status
                        .as_ref()
                        .map(|status| status.to_string())
                        .unwrap_or_else(|| "missing".to_string()),
                ),
                ("durationMs", started_at.elapsed().as_millis().to_string()),
            ],
        );
        if let Ok(mut cancelled) = app.state::<AppState>().cancelled_tasks.lock() {
            for registered_task_id in &registered_task_ids {
                if cancelled.remove(registered_task_id) {
                    terminal_task_ids.insert(registered_task_id.clone());
                }
            }
        }
        let missing_terminal_task_ids = registered_task_ids
            .iter()
            .filter(|task_id| !terminal_task_ids.contains(*task_id))
            .cloned()
            .collect::<Vec<_>>();
        if !missing_terminal_task_ids.is_empty() {
            match exit_status {
                Some(status) if !status.success() => {
                    emit_task_error_to_all(
                        &app,
                        &missing_terminal_task_ids,
                        format!("worker exited with {status}"),
                    );
                }
                None => {
                    emit_task_error_to_all(
                        &app,
                        &missing_terminal_task_ids,
                        "worker exited unexpectedly".to_string(),
                    );
                }
                Some(status) if status.success() && command_name == "audio_tools" => {
                    emit_task_error_to_all(
                        &app,
                        &missing_terminal_task_ids,
                        "worker exited without an audio tool result".to_string(),
                    );
                }
                _ => {}
            }
        }
        if let Some(handle) = stderr_handle {
            let _ = handle.join();
        }
        let _ = std::fs::remove_file(payload_file);
        let cleanup_state = app.state::<AppState>();
        if let Ok(mut tasks) = cleanup_state.tasks.lock() {
            for registered_task_id in registered_task_ids {
                if tasks
                    .get(&registered_task_id)
                    .map(|registered| Arc::ptr_eq(registered, &shared_child))
                    .unwrap_or(false)
                {
                    tasks.remove(&registered_task_id);
                }
            }
        };
    });

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::is_background_terminal_event;
    use serde_json::json;
    use std::fs;
    use std::path::Path;

    #[test]
    fn core_update_excludes_parallel_runtime_users_in_both_directions() {
        let access = std::sync::Mutex::new(super::RuntimeAccess::default());
        let inference = super::acquire_runtime_access(&access, "infer").unwrap();
        let workflow = super::acquire_runtime_access(&access, "infer_workflow").unwrap();
        assert!(super::acquire_runtime_access(&access, "update_runtime_core").is_err());
        drop(inference);
        assert!(super::acquire_runtime_access(&access, "update_runtime_core").is_err());
        drop(workflow);

        let update = super::acquire_runtime_access(&access, "update_runtime_core").unwrap();
        for command in ["infer", "infer_workflow", "env_info", "list_models", "audio_tools", "update_runtime_core"] {
            let result = super::acquire_runtime_access(&access, command);
            assert!(result.is_err(), "{command} must wait for the core update");
            assert!(result.err().unwrap().to_string().contains("RUNTIME_BUSY"));
        }
        for command in ["health", "runtime_info", "runtime_core_versions", "runtime_env_sizes"] {
            assert!(super::acquire_runtime_access(&access, command).unwrap().is_none());
        }
        drop(update);
        assert!(super::acquire_runtime_access(&access, "infer").is_ok());
    }

    #[test]
    fn runtime_access_lease_can_follow_background_process_and_release_on_failure() {
        let access = std::sync::Mutex::new(super::RuntimeAccess::default());
        let lease = super::acquire_runtime_access(&access, "update_runtime_core").unwrap();
        std::thread::scope(|scope| {
            scope.spawn(move || drop(lease)).join().unwrap();
        });
        let fail_start = || -> crate::error::AppResult<()> {
            let _lease = super::acquire_runtime_access(&access, "infer")?;
            Err(crate::error::AppError::Worker("process start failed".into()))
        };
        assert!(fail_start().is_err());
        assert!(super::acquire_runtime_access(&access, "update_runtime_core").is_ok());
    }

    fn temp_root(label: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "pymss-worker-{label}-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ))
    }

    struct ResourceFixture(std::path::PathBuf);

    impl ResourceFixture {
        fn new(label: &str) -> Self {
            let root = temp_root(label);
            fs::create_dir(&root).unwrap();
            Self(root)
        }

        fn file(&self, path: &Path) {
            assert!(path.starts_with(&self.0));
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, b"resource").unwrap();
        }
    }

    impl Drop for ResourceFixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn worker_resource_layouts_keep_the_flat_fallback_last() {
        let fixture = ResourceFixture::new("worker-layout");
        let resource = fixture.0.join("resources");
        assert_eq!(super::resource_worker_path(&resource), None);
        let candidates = [
            resource.join("python/worker.py"),
            resource.join("_up_/python/worker.py"),
            resource.join("resources/python/worker.py"),
            resource.join("worker.py"),
        ];
        for candidate in candidates.iter().rev() {
            fixture.file(candidate);
            assert_eq!(super::resource_worker_path(&resource), Some(candidate.clone()));
        }
    }

    #[test]
    fn worker_lookup_retains_its_existing_exists_check() {
        let fixture = ResourceFixture::new("worker-exists");
        let worker = fixture.0.join("python/worker.py");
        fs::create_dir_all(&worker).unwrap();
        assert_eq!(super::resource_worker_path(&fixture.0), Some(worker));
    }

    #[test]
    fn embedded_python_preserves_root_and_platform_specific_filename_priority() {
        for windows in [true, false] {
            let fixture = ResourceFixture::new("embedded-layout");
            let resource = fixture.0.join("Pymss.app/Contents/Resources");
            let exe = fixture.0.join("Pymss.app/Contents/MacOS");
            let roots = [
                resource.join("python-runtime"),
                resource.join("_up_/python-runtime"),
                resource.join("resources/python-runtime"),
                exe.join("python-runtime"),
            ];
            let names = if windows { ["python.exe", "Scripts/python.exe"] } else { ["bin/python3", "bin/python"] };
            assert_eq!(super::embedded_python_from(Some(&resource), &exe, windows), None);
            for root in roots.iter().rev() {
                for name in names.iter().rev() {
                    let candidate = root.join(name);
                    fixture.file(&candidate);
                    assert_eq!(super::embedded_python_from(Some(&resource), &exe, windows), Some(candidate));
                }
            }
            assert_eq!(super::embedded_python_from(None, &exe, windows), Some(roots[3].join(names[0])));
        }
    }

    #[test]
    fn embedded_python_skips_directories_named_like_interpreters() {
        let fixture = ResourceFixture::new("embedded-file-type");
        let resource = fixture.0.join("resources");
        let exe = fixture.0.join("portable");
        fs::create_dir_all(resource.join("python-runtime/python.exe")).unwrap();
        let python = exe.join("python-runtime/python.exe");
        fixture.file(&python);
        assert_eq!(super::embedded_python_from(Some(&resource), &exe, true), Some(python));
    }

    #[test]
    fn bin_candidates_keep_all_hits_and_platform_tail_in_order() {
        let fixture = ResourceFixture::new("bin-layout");
        let resource = fixture.0.join("resources");
        let exe = fixture.0.join("portable");
        let expected = vec![resource.join("bin"), resource.join("_up_/bin"), resource.join("resources/bin"), exe.join("bin")];
        assert_eq!(super::bundled_bin_candidates(Some(&resource), &exe, false), expected);
        let mut mac = expected.clone();
        mac.extend([std::path::PathBuf::from("/opt/homebrew/bin"), std::path::PathBuf::from("/usr/local/bin")]);
        assert_eq!(super::bundled_bin_candidates(Some(&resource), &exe, true), mac);
        fs::create_dir_all(&expected[1]).unwrap();
        fs::create_dir_all(&expected[3]).unwrap();
        fixture.file(&expected[2]);
        let hits = super::bundled_bin_candidates(Some(&resource), &exe, false)
            .into_iter().filter(|dir| dir.is_dir()).collect::<Vec<_>>();
        assert_eq!(hits, vec![expected[1].clone(), expected[3].clone()]);
        assert_eq!(super::bundled_bin_candidates(None, &exe, false), vec![exe.join("bin")]);
        let duplicate = super::bundled_bin_candidates(Some(&exe), &exe, false);
        assert_eq!(duplicate[0], duplicate[3]);
    }

    #[test]
    fn bundled_env_selection_preserves_canonical_path_overlap_and_missing_path_behavior() {
        let fixture = ResourceFixture::new("env-overlap");
        let user = fixture.0.join("user");
        let bundled = fixture.0.join("bundled");
        fs::create_dir_all(&user).unwrap();
        fs::create_dir_all(&bundled).unwrap();
        let canonical_user = user.canonicalize().ok();
        assert_eq!(super::distinct_bundled_runtime_env(canonical_user.clone(), Some(user.join("."))), None);
        assert_eq!(super::distinct_bundled_runtime_env(canonical_user.clone(), Some(bundled.clone())), Some(bundled.clone()));
        assert_eq!(super::distinct_bundled_runtime_env(canonical_user.clone(), None), None);
        assert_eq!(super::distinct_bundled_runtime_env(None, Some(bundled.clone())), Some(bundled));
        let missing = fixture.0.join("missing");
        assert_eq!(super::distinct_bundled_runtime_env(None, Some(missing.clone())), None);
        assert_eq!(super::distinct_bundled_runtime_env(canonical_user, Some(missing.clone())), Some(missing));
    }

    #[test]
    fn active_pointer_resolution_accepts_existing_relative_and_absolute_paths_without_rewriting() {
        let fixture = ResourceFixture::new("pointer-resolution");
        let python = fixture.0.join("cpu/bin/python");
        fixture.file(&python);
        let pointer = fixture.0.join("active-runtime.json");
        for path in ["cpu/bin/python".to_string(), python.to_string_lossy().to_string()] {
            let bytes = serde_json::to_vec(&json!({"pythonPath": path, "backend": "cpu", "source": "bundled", "custom": true})).unwrap();
            fs::write(&pointer, &bytes).unwrap();
            assert_eq!(super::resolve_active_runtime_record(&pointer), Some((python.canonicalize().unwrap().to_string_lossy().to_string(), Some("bundled".into()))));
            assert_eq!(fs::read(&pointer).unwrap(), bytes);
        }
        for bytes in [b"{}".as_slice(), b"not json", br#"{"pythonPath":"missing/python"}"#, br#"{"pythonPath":"cpu"}"#] {
            fs::write(&pointer, bytes).unwrap();
            assert_eq!(super::resolve_active_runtime_record(&pointer), None);
            assert_eq!(fs::read(&pointer).unwrap(), bytes);
        }
    }

    #[cfg(windows)]
    #[test]
    fn rocm_tools_keep_user_before_bundled_then_regular_bins_and_existing_path() {
        let fixture = ResourceFixture::new("rocm-path-order");
        let user = fixture.0.join("user");
        let bundled = fixture.0.join("bundled");
        let mut expected = Vec::new();
        for root in [&user, &bundled] {
            let package = root.join("rocm").join("Lib").join("site-packages").join("_rocm_sdk_core_1");
            let tool = package.join("lib").join("llvm").join("bin");
            fixture.file(&tool.join("offload-arch.exe"));
            let sdk_bin = package.join("bin");
            fs::create_dir_all(&sdk_bin).unwrap();
            expected.extend([tool, sdk_bin]);
            fixture.file(&root.join("rocm/Lib/site-packages/unrelated/lib/llvm/bin/offload-arch.exe"));
        }
        let mut dirs = super::rocm_native_tool_dirs_from(vec![user, fixture.0.join("absent"), bundled]);
        assert_eq!(dirs, expected);
        let bin = fixture.0.join("bin");
        fs::create_dir_all(&bin).unwrap();
        dirs.extend(super::bundled_bin_candidates(None, &fixture.0, false));
        expected.push(bin);
        let joined = expected.iter().map(|p| p.to_string_lossy()).collect::<Vec<_>>().join(";");
        assert_eq!(super::prepend_path(Some("existing-path".into()), dirs), Some(format!("{joined};existing-path")));
    }

    #[test]
    fn audio_tool_result_is_a_background_terminal_event() {
        assert!(is_background_terminal_event("audio_tools", "audio_tool_result"));
        assert!(is_background_terminal_event("audio_tools", "error"));
        assert!(!is_background_terminal_event("audio_tools", "audio_tool_progress"));
    }

    fn write_bundled_pointer(root: &Path, backend: &str, python_path: &str) -> std::path::PathBuf {
        let envs = root.join("python-runtime").join("runtime-envs");
        fs::create_dir_all(&envs).unwrap();
        let active = envs.join("active-runtime.json");
        fs::write(
            &active,
            serde_json::to_vec(&json!({
                "backend": backend,
                "pythonPath": python_path,
                "source": "bundled",
            }))
            .unwrap(),
        )
        .unwrap();
        active
    }

    #[test]
    fn accepts_bundled_mlx_bootstrap_pointer() {
        let root = temp_root("bundled-mlx-pointer");
        let runtime = root.join("python-runtime");
        let python = runtime.join("bin").join("python3");
        fs::create_dir_all(python.parent().unwrap()).unwrap();
        fs::write(&python, "stub").unwrap();
        let active = write_bundled_pointer(&root, "mlx", "../bin/python3");

        assert!(super::is_bundled_runtime_python_path(
            &active,
            &fs::canonicalize(&python).unwrap().to_string_lossy(),
        )
        .unwrap());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn rejects_bundled_bootstrap_pointer_for_non_mlx_backend() {
        let root = temp_root("bundled-invalid-bootstrap");
        let runtime = root.join("python-runtime");
        let python = runtime.join("bin").join("python3");
        fs::create_dir_all(python.parent().unwrap()).unwrap();
        fs::write(&python, "stub").unwrap();
        let active = write_bundled_pointer(&root, "cpu", "../bin/python3");

        assert!(!super::is_bundled_runtime_python_path(
            &active,
            &fs::canonicalize(&python).unwrap().to_string_lossy(),
        )
        .unwrap());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn rejects_bundled_pointer_that_escapes_the_package() {
        let root = temp_root("bundled-outside-pointer");
        let outside = root.join("outside").join("python3");
        fs::create_dir_all(outside.parent().unwrap()).unwrap();
        fs::write(&outside, "stub").unwrap();
        let active = write_bundled_pointer(&root, "mlx", "../../outside/python3");

        assert!(!super::is_bundled_runtime_python_path(
            &active,
            &fs::canonicalize(&outside).unwrap().to_string_lossy(),
        )
        .unwrap());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn rejects_bundled_environment_with_the_wrong_backend_directory() {
        let root = temp_root("bundled-wrong-backend");
        let envs = root.join("python-runtime").join("runtime-envs");
        let python = envs.join("cpu").join("bin").join("python");
        fs::create_dir_all(python.parent().unwrap()).unwrap();
        fs::write(&python, "stub").unwrap();
        let active = write_bundled_pointer(&root, "cuda", "cpu/bin/python");

        assert!(!super::is_bundled_runtime_python_path(
            &active,
            &fs::canonicalize(&python).unwrap().to_string_lossy(),
        )
        .unwrap());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn active_user_pointer_requires_a_matching_known_backend() {
        let root = temp_root("active-pointer-backend");
        let active = root.join("active-runtime.json");
        let python = root.join("runtime-envs").join("cuda").join("bin").join("python");
        fs::create_dir_all(python.parent().unwrap()).unwrap();
        fs::write(&python, "stub").unwrap();
        let python = fs::canonicalize(python).unwrap();
        fs::write(
            &active,
            serde_json::to_vec(&json!({"backend": "cuda", "pythonPath": python.to_string_lossy()})).unwrap(),
        )
        .unwrap();
        assert!(super::active_path_backend_matches(&active, &python.to_string_lossy()));

        fs::write(
            &active,
            serde_json::to_vec(&json!({"backend": "mlx", "pythonPath": python.to_string_lossy()})).unwrap(),
        )
        .unwrap();
        assert!(!super::active_path_backend_matches(&active, &python.to_string_lossy()));

        fs::write(
            &active,
            serde_json::to_vec(&json!({"backend": "../outside", "pythonPath": python.to_string_lossy()})).unwrap(),
        )
        .unwrap();
        assert!(!super::active_path_backend_matches(&active, &python.to_string_lossy()));

        fs::write(
            &active,
            serde_json::to_vec(&json!({"pythonPath": python.to_string_lossy()})).unwrap(),
        )
        .unwrap();
        assert!(!super::active_path_backend_matches(&active, &python.to_string_lossy()));
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn finds_rocm_native_offload_arch_tool() {
        let root = std::env::temp_dir().join(format!(
            "pymss-worker-rocm-tool-test-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let tool_dir = root
            .join("rocm")
            .join("Lib")
            .join("site-packages")
            .join("_rocm_sdk_core")
            .join("lib")
            .join("llvm")
            .join("bin");
        std::fs::create_dir_all(&tool_dir).unwrap();
        std::fs::write(tool_dir.join("offload-arch.exe"), "stub").unwrap();

        assert_eq!(super::rocm_native_tool_dir(&root), Some(tool_dir));

        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn recognizes_only_known_rocm_offload_arch_diagnostics() {
        for line in [
            "Fatal error in launcher: Unable to create process using 'D:\\a\\python.exe'",
            "[WARNING] offload-arch failed with return code 1",
            "[stderr]",
            "[rocm_sdk] offload-arch not found",
        ] {
            assert!(super::is_rocm_offload_arch_diagnostic(line));
        }
        assert!(!super::is_rocm_offload_arch_diagnostic("unrelated Python traceback"));
    }

}
