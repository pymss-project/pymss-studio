use crate::error::{AppError, AppResult};
use crate::log_record::{legacy_stderr, LogRecord};
use crate::python::protocol::WorkerEnvelope;
use crate::session_log;
use crate::state::AppState;
use crate::storage;
use serde::Deserialize;
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, Manager, State};

#[path = "audio_output.rs"]
mod audio_output;

pub(crate) fn recover_audio_output_temps(app: &AppHandle) {
    if let Ok(directory) = storage::temp_dir(app) {
        std::thread::spawn(move || audio_output::recover(&directory.join("audio-output-cleanup")));
    }
}

fn requires_background_result(command: &str) -> bool {
    matches!(command, "audio_tools" | "export_editor_mix" | "manage_plugin" | "run_operation")
}

fn take_cancelled_tasks(cancelled: &Mutex<HashSet<String>>, registered: &[String]) -> Vec<String> {
    let Ok(mut cancelled) = cancelled.lock() else { return Vec::new() };
    registered.iter().filter(|id| cancelled.remove(*id)).cloned().collect()
}

fn operation_output_directory(payload: &Value, default: &Path) -> AppResult<PathBuf> {
    let raw = match payload.get("outputDir") {
        None | Some(Value::Null) => "",
        Some(Value::String(value)) => value.as_str(),
        _ => return Err(AppError::Worker("outputDir must be a string".into())),
    };
    let path = PathBuf::from(raw);
    let directory = if raw.is_empty() {
        default.to_path_buf()
    } else if path.is_absolute() {
        path
    } else {
        default.parent().unwrap_or(default).join(path)
    };
    Ok(std::path::absolute(directory)?)
}

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

pub(crate) fn kill_process_tree(pid: u32) {
    #[cfg(windows)]
    let _ = Command::new("taskkill")
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .creation_flags(CREATE_NO_WINDOW)
        .status();
    #[cfg(not(windows))]
    let _ = Command::new("pkill").args(["-TERM", "-P", &pid.to_string()]).status();
}

const PYTHON_TERMINAL_LOG_PREFIX: &str = "__PYMSS_STUDIO_TERMINAL_LOG__";
static PROGRESS_LOG_TIMES: OnceLock<Mutex<HashMap<String, (Instant, u64)>>> = OnceLock::new();
const HOST_PYTHON_ENV_VARS: &[&str] = &[
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONUSERBASE",
    "PYTHONSTARTUP",
    "PYTHONEXECUTABLE",
    "__PYVENV_LAUNCHER__",
    "VIRTUAL_ENV",
    "PYENV_VERSION",
    "CONDA_PREFIX",
    "CONDA_PREFIX_1",
    "CONDA_DEFAULT_ENV",
    "CONDA_PROMPT_MODIFIER",
    "CONDA_SHLVL",
    "CONDA_EXE",
    "_CE_CONDA",
    "_CE_M",
];

static PAYLOAD_FILE_COUNTER: AtomicU64 = AtomicU64::new(0);

// Core packages are updated in the active environment. Hold a shared lease for every worker
// using that interpreter, and an exclusive lease for pip, including the process-start window.
static RUNTIME_ACCESS: Mutex<RuntimeAccess> = Mutex::new(RuntimeAccess {
    users: 0,
    updating: false,
    installing_plugins: 0,
});
// Application replacement also excludes bootstrap workers, which can otherwise keep
// importing modules from the old bundle while its directory is being replaced.
static APPLICATION_ACCESS: Mutex<RuntimeAccess> = Mutex::new(RuntimeAccess {
    users: 0,
    updating: false,
    installing_plugins: 0,
});

pub(crate) fn acquire_application_update() -> AppResult<impl Drop> {
    acquire_runtime_access(&APPLICATION_ACCESS, "update_runtime_core")
        .map_err(|error| match error {
            AppError::Worker(message) if message.starts_with("RUNTIME_BUSY:") => AppError::Worker(
                "Wait for running audio and runtime operations to finish before updating the application.".into(),
            ),
            error => error,
        })?
        .ok_or_else(|| AppError::Worker("Unable to reserve the application for updating".into()))
}

pub(crate) fn acquire_application_operation() -> AppResult<impl Drop + Send> {
    acquire_runtime_access(&APPLICATION_ACCESS, "application_operation")
        .map_err(|_| AppError::Worker("An application update is in progress. Wait for it to finish before starting another operation.".into()))?
        .ok_or_else(|| AppError::Worker("Unable to reserve the application for this operation".into()))
}

pub(crate) fn acquire_plugin_source_update() -> AppResult<impl Drop> {
    acquire_runtime_access(&RUNTIME_ACCESS, "manage_plugin")?
        .ok_or_else(|| AppError::Worker("Unable to reserve plugin source configuration".into()))
}

#[derive(Default)]
struct RuntimeAccess {
    users: usize,
    updating: bool,
    installing_plugins: usize,
}

struct RuntimeAccessGuard<'a> {
    access: &'a Mutex<RuntimeAccess>,
    updating: bool,
    installing_plugins: bool,
}

impl Drop for RuntimeAccessGuard<'_> {
    fn drop(&mut self) {
        if let Ok(mut access) = self.access.lock() {
            if self.installing_plugins {
                access.installing_plugins -= 1;
            } else if self.updating {
                access.updating = false;
            } else {
                access.users -= 1;
            }
        }
    }
}

fn requires_bootstrap_python(command: &str) -> bool {
    matches!(
        command,
        "health"
            | "plugin_source_info"
            | "runtime_info"
            | "runtime_env_sizes"
            | "runtime_core_versions"
            | "install_runtime"
            | "activate_runtime"
            | "delete_runtime"
            | "update_runtime_core"
    )
}

fn select_worker_python(
    command: &str,
    bootstrap_python: &str,
    active_runtime_python: Option<String>,
) -> AppResult<String> {
    if requires_bootstrap_python(command) {
        return Ok(bootstrap_python.to_string());
    }
    if let Some(python) = active_runtime_python {
        return Ok(python);
    }
    if command == "test_connection" {
        return Ok(bootstrap_python.to_string());
    }
    Err(AppError::Worker(
        "No active runtime environment is installed. Open Settings to install one before running this operation."
            .into(),
    ))
}

fn acquire_runtime_access<'a>(
    access: &'a Mutex<RuntimeAccess>,
    command: &str,
) -> AppResult<Option<RuntimeAccessGuard<'a>>> {
    let installing_plugins = command == "install_plugin";
    let updating = matches!(command, "update_runtime_core" | "manage_plugin" | "activate_runtime" | "delete_runtime" | "install_runtime");
    if !updating && requires_bootstrap_python(command) {
        return Ok(None);
    }
    let mut state = access
        .lock()
        .map_err(|_| AppError::Worker("runtime access lock poisoned".into()))?;
    if state.updating || ((updating || installing_plugins) && state.users > 0)
        || (!installing_plugins && state.installing_plugins > 0) {
        return Err(AppError::Worker(
            "RUNTIME_BUSY: The runtime is in use. Wait for running operations to finish before updating core packages or starting another operation.".into(),
        ));
    }
    if installing_plugins {
        state.installing_plugins += 1;
    } else if updating {
        state.updating = true;
    } else {
        state.users += 1;
    }
    Ok(Some(RuntimeAccessGuard { access, updating, installing_plugins }))
}

fn worker_scratch(command: &str) -> std::io::Result<Option<tempfile::TempDir>> {
    if matches!(command, "run_operation" | "infer_workflow") {
        tempfile::Builder::new().prefix("pymss-worker-audio-").tempdir().map(Some)
    } else {
        Ok(None)
    }
}

fn runtime_access_command<'a>(command: &'a str, payload: &Value) -> &'a str {
    if command == "manage_plugin" && payload.get("action").and_then(Value::as_str) == Some("install") {
        "install_plugin"
    } else {
        command
    }
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ActiveRuntimeRecord {
    python_path: String,
    backend: Option<String>,
    source: Option<String>,
    overlay_path: Option<String>,
    #[serde(default)]
    debug_override: bool,
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
    Ok(embedded_python_from(
        resource.as_deref(),
        &exe_dir,
        cfg!(windows),
    ))
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
        #[cfg(target_os = "linux")]
        if !storage::is_development_executable()
            && std::env::var_os("APPIMAGE").is_some_and(|path| Path::new(&path).is_file())
        {
            let runtime = embedded.parent().and_then(Path::parent)
                .ok_or_else(|| AppError::Worker("Invalid bundled Python path".into()))?;
            let cached = super::bootstrap_cache::cached_python(
                runtime, &storage::data_root_dir(app)?, env!("CARGO_PKG_VERSION"),
            )?;
            return Ok(cached.to_string_lossy().into_owned());
        }
        return Ok(embedded.to_string_lossy().to_string());
    }
    if cfg!(windows) {
        Ok("python".to_string())
    } else {
        Ok("python3".to_string())
    }
}

fn runtime_python_command_path(python: &Path) -> Option<PathBuf> {
    if !python.is_file() {
        return None;
    }
    // Resolve directory aliases while keeping a POSIX venv's executable symlink.
    // Resolving the executable itself launches the base interpreter and loses pyvenv.cfg.
    Some(
        python
            .parent()?
            .canonicalize()
            .ok()?
            .join(python.file_name()?),
    )
}

fn resolve_active_runtime_record(file: &PathBuf) -> Option<(String, Option<String>)> {
    if !file.is_file() {
        return None;
    }
    let content = std::fs::read_to_string(file).ok()?;
    let record: ActiveRuntimeRecord = serde_json::from_str(&content).ok()?;
    let python = PathBuf::from(&record.python_path);
    // Resolve the directory, but retain the interpreter filename. On Unix a venv
    // python is usually a symlink to the base interpreter; canonicalizing that
    // final component both loses the venv and breaks backend ownership checks.
    let resolved = if python.is_absolute() {
        python
    } else {
        file.parent()?.join(python)
    };
    let executable = resolved.parent()?.canonicalize().ok()?.join(resolved.file_name()?);
    executable.is_file().then(|| (executable.to_string_lossy().to_string(), record.source))
}

fn bundled_runtime_envs_dir(app: &AppHandle) -> AppResult<Option<PathBuf>> {
    let user_runtime = storage::runtime_envs_dir(app)?.canonicalize().ok();
    Ok(distinct_bundled_runtime_env(
        user_runtime,
        storage::bundled_runtime_envs_dir(app)?,
    ))
}

fn distinct_bundled_runtime_env(
    user_runtime: Option<PathBuf>,
    bundled: Option<PathBuf>,
) -> Option<PathBuf> {
    bundled.filter(|path| path.canonicalize().ok() != user_runtime)
}

fn active_runtime_python_path(app: &AppHandle) -> AppResult<Option<String>> {
    let user_file = storage::active_runtime_file(app)?;
    if let Some((path, _source)) = resolve_active_runtime_record(&user_file) {
        let is_managed = is_user_runtime_python_path(app, &path)?;
        let is_debug_override = active_debug_override_path_allowed(
            &user_file,
            &path,
            storage::is_development_executable(),
        );
        if (is_managed || is_debug_override) && active_path_backend_matches(&user_file, &path) {
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

fn resolve_runtime_overlay_for_record(
    file: &Path,
    active_python: &str,
    allowed_roots: &[PathBuf],
) -> Option<PathBuf> {
    let content = std::fs::read_to_string(file).ok()?;
    let record: ActiveRuntimeRecord = serde_json::from_str(&content).ok()?;
    let (record_python, _) = resolve_active_runtime_record(&file.to_path_buf())?;
    let expected = runtime_python_command_path(Path::new(active_python))?;
    let recorded = runtime_python_command_path(Path::new(&record_python))?;
    if expected != recorded {
        return None;
    }
    let raw_overlay = record.overlay_path?.trim().to_string();
    if raw_overlay.is_empty() {
        return None;
    }
    let overlay = PathBuf::from(raw_overlay);
    let overlay = if overlay.is_absolute() {
        overlay
    } else {
        file.parent()?.join(overlay)
    };
    let overlay = overlay.canonicalize().ok()?;
    if !overlay.is_dir() {
        return None;
    }
    allowed_roots
        .iter()
        .filter_map(|root| root.canonicalize().ok())
        .any(|root| overlay.starts_with(root.join(".overlays")))
        .then_some(overlay)
}

fn active_runtime_overlay_path(app: &AppHandle, active_python: &str) -> AppResult<Option<PathBuf>> {
    let user_runtime = storage::runtime_envs_dir(app)?;
    let mut records = vec![storage::active_runtime_file(app)?];
    let mut allowed_roots = vec![user_runtime];
    if let Some(bundled) = bundled_runtime_envs_dir(app)? {
        records.push(bundled.join("active-runtime.json"));
        allowed_roots.push(bundled);
    }
    Ok(records
        .into_iter()
        .find_map(|file| resolve_runtime_overlay_for_record(&file, active_python, &allowed_roots)))
}

fn is_bundled_runtime_python_path(file: &Path, python_path: &str) -> AppResult<bool> {
    let content = std::fs::read_to_string(file)?;
    let record: ActiveRuntimeRecord = serde_json::from_str(&content)?;
    let backend = record
        .backend
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase();
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
    let Some(python) = runtime_python_command_path(Path::new(python_path)) else {
        return Ok(false);
    };
    let bootstrap_matches = [
        runtime_root.join("python.exe"),
        runtime_root.join("bin").join("python3"),
        runtime_root.join("bin").join("python"),
    ]
    .into_iter()
    .filter_map(|candidate| runtime_python_command_path(&candidate))
    .any(|candidate| candidate == python);
    if bootstrap_matches {
        return Ok(backend == "mlx");
    }
    let Some(env_dir) = python.parent().and_then(Path::parent) else {
        return Ok(false);
    };
    Ok(env_dir.starts_with(&envs_dir)
        && env_dir
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.eq_ignore_ascii_case(&backend)))
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
    if !matches!(
        backend.trim().to_ascii_lowercase().as_str(),
        "cpu" | "cuda" | "rocm" | "mlx"
    ) {
        return false;
    }
    let path = PathBuf::from(python_path);
    path.parent()
        .and_then(Path::parent)
        .and_then(|env| env.file_name())
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.eq_ignore_ascii_case(backend.trim()))
}

fn active_debug_override_path_allowed(
    file: &Path,
    python_path: &str,
    development_executable: bool,
) -> bool {
    if !development_executable {
        return false;
    }
    let Ok(content) = std::fs::read_to_string(file) else {
        return false;
    };
    let Ok(record) = serde_json::from_str::<ActiveRuntimeRecord>(&content) else {
        return false;
    };
    record.debug_override && active_path_backend_matches(file, python_path)
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
        .is_some_and(|path| path.starts_with(&runtime_root)))
}

pub(crate) fn bundled_bin_dirs(app: &AppHandle) -> AppResult<Vec<PathBuf>> {
    let resource = app.path().resource_dir().ok();
    let exe_dir = std::env::current_exe()?
        .parent()
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    Ok(bundled_bin_candidates(resource.as_deref(), &exe_dir)
        .into_iter()
        .filter(|dir| dir.is_dir())
        .collect())
}

fn bundled_bin_candidates(resource: Option<&Path>, exe_dir: &Path) -> Vec<PathBuf> {
    let mut dirs: Vec<_> = resource
        .into_iter()
        .flat_map(storage::resource_roots)
        .map(|root| root.join("bin"))
        .collect();
    dirs.push(exe_dir.join("bin"));
    dirs
}

#[cfg(windows)]
fn rocm_native_tool_dir(runtime_envs_dir: &Path) -> Option<PathBuf> {
    let site_packages = runtime_envs_dir
        .join("rocm")
        .join("Lib")
        .join("site-packages");
    let entries = std::fs::read_dir(site_packages).ok()?;
    entries
        .flatten()
        .map(|entry| entry.path())
        .find_map(|package_dir| {
            let name = package_dir.file_name()?.to_str()?;
            let tool_dir = package_dir.join("lib").join("llvm").join("bin");
            (name.starts_with("_rocm_sdk_core") && tool_dir.join("offload-arch.exe").is_file())
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

fn isolate_python_environment(cmd: &mut Command, cache_root: &Path) {
    for key in HOST_PYTHON_ENV_VARS {
        cmd.env_remove(key);
    }
    cmd.env("PYTHONNOUSERSITE", "1")
        .env("NUMBA_CACHE_DIR", cache_root.join("numba-cache"))
        .env("PIP_CACHE_DIR", cache_root.join("pip-cache"));
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

fn apply_plugin_source_override(cmd: &mut Command, developer_mode: bool, source: &Value) -> AppResult<()> {
    cmd.env_remove("PYMSS_STUDIO_PLUGIN_SOURCE_OVERRIDE");
    if developer_mode && !source.is_null() {
        cmd.env("PYMSS_STUDIO_PLUGIN_SOURCE_OVERRIDE", serde_json::to_string(source)?);
    }
    Ok(())
}

fn build_worker_command(
    app: &AppHandle,
    command: &str,
    payload_file: Option<&PathBuf>,
    task_id: Option<&str>,
    operation_id: &str,
) -> AppResult<Command> {
    let worker = worker_path(app)?;
    let bootstrap_python = bootstrap_python_path(app)?;
    // Runtime management must never run inside the environment it is managing: the bootstrap
    // interpreter is the only one guaranteed to exist while an environment is being built,
    // switched, or deleted.
    let active_runtime_python = if requires_bootstrap_python(command) {
        None
    } else {
        active_runtime_python_path(app)?
    };
    let active_overlay = active_runtime_python
        .as_deref()
        .map(|python| active_runtime_overlay_path(app, python))
        .transpose()?
        .flatten();
    let python = select_worker_python(command, &bootstrap_python, active_runtime_python)?;
    let python_for_log = python.clone();
    let worker_for_log = worker.clone();
    let mut cmd = Command::new(&python);
    #[cfg(unix)]
    if command == "manage_plugin" {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    isolate_python_environment(&mut cmd, &storage::temp_dir(app)?);
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    cmd.arg(worker)
        .arg(command)
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("PYTHONUTF8", "1")
        .env("PYMSS_STUDIO_LOG_TRANSPORT", "protocol")
        .env(
            "PYMSS_STUDIO_DEBUG_LOG",
            if session_log::verbose_logging_enabled() {
                "1"
            } else {
                "0"
            },
        )
        .env("PYMSS_STUDIO_BOOTSTRAP_PYTHON", &bootstrap_python)
        .env(
            "PYMSS_STUDIO_DEFAULT_OUTPUT_DIR",
            default_output_dir(app)?.to_string_lossy().to_string(),
        );
    if let Some(overlay) = active_overlay {
        cmd.env("PYTHONPATH", overlay.to_string_lossy().to_string());
    }
    // Source overrides belong to developer settings and affect only new workers.
    let developer_mode = session_log::developer_mode_enabled();
    let source = if developer_mode { storage::read_app_store(app, "plugin-source")? } else { Value::Null };
    apply_plugin_source_override(&mut cmd, developer_mode, &source)?;
    // The collector owns persistence and terminal output. Never inherit legacy file writers.
    cmd.env_remove("PYMSS_STUDIO_AUDIO_TEMP_TOKEN")
        .env_remove("PYMSS_STUDIO_AUDIO_TEMP_STATE")
        .env_remove("PYMSS_STUDIO_SESSION_LOG")
        .env_remove("PYMSS_STUDIO_PERSISTENT_LOG")
        .env_remove("PYMSS_STUDIO_TERMINAL_LOG");
    if let Ok(dir) = storage::runtime_envs_dir(app) {
        cmd.env(
            "PYMSS_STUDIO_RUNTIME_ENVS_DIR",
            dir.to_string_lossy().to_string(),
        );
    }
    if let Ok(file) = storage::active_runtime_file(app) {
        cmd.env(
            "PYMSS_STUDIO_ACTIVE_RUNTIME_FILE",
            file.to_string_lossy().to_string(),
        );
    }
    if storage::is_development_executable() {
        cmd.env("PYMSS_STUDIO_ALLOW_DEBUG_RUNTIME_OVERRIDE", "1");
    }
    if let Some(dir) = bundled_runtime_envs_dir(app)? {
        cmd.env(
            "PYMSS_STUDIO_BUNDLED_RUNTIME_ENVS_DIR",
            dir.to_string_lossy().to_string(),
        );
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
        "DEBUG",
        "worker.command",
        vec![
            ("command", command.to_string()),
            ("taskId", task_id.unwrap_or_default().to_string()),
            ("operationId", operation_id.to_string()),
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

fn emit_worker_stderr(app: &AppHandle, line: String, command: &str, operation_id: &str) {
    let mut record = legacy_stderr(&line);
    record.context.insert("command".into(), command.into());
    record.context.insert("operationId".into(), operation_id.into());
    let record = session_log::append_record(app, record);
    let _ = app.emit(
        "pymss://worker-event",
        serde_json::json!({
            "type": "worker_stderr",
            "payload": { "message": record.message, "level": record.level, "logRecord": record }
        }),
    );
}

fn forward_python_terminal_log(app: &AppHandle, line: &str) -> bool {
    let Some(line) = line.strip_prefix(PYTHON_TERMINAL_LOG_PREFIX) else {
        return false;
    };
    session_log::append_record(app, legacy_stderr(line));
    true
}

fn emit_batch_stderr(app: &AppHandle, task_ids: &[String], line: String) {
    let mut record = legacy_stderr(&line);
    if task_ids.len() == 1 {
        record.task_id = task_ids.first().cloned();
    }
    if task_ids.len() > 1 {
        record
            .context
            .insert("taskIds".into(), serde_json::json!(task_ids));
    }
    let record = session_log::append_record(app, record);
    for task_id in task_ids {
        let _ = app.emit(
            "pymss://worker-event",
            serde_json::json!({
                "type": "task_log", "taskId": task_id,
                "payload": { "level": record.level, "message": record.message, "logRecord": record }
            }),
        );
    }
}

fn emit_task_error_to_all(app: &AppHandle, task_ids: &[String], message: String) {
    let mut record = LogRecord::new(
        "studio",
        Some("error"),
        "studio.worker",
        "worker.failed",
        &message,
    );
    if task_ids.len() == 1 {
        record.task_id = task_ids.first().cloned();
    }
    if task_ids.len() > 1 {
        record
            .context
            .insert("taskIds".into(), serde_json::json!(task_ids));
    }
    let record = session_log::append_record(app, record);
    for task_id in task_ids {
        let _ = app.emit(
            "pymss://worker-event",
            serde_json::json!({
                "type": "error", "taskId": task_id,
                "payload": { "message": record.message, "logRecord": record }
            }),
        );
    }
}

fn broadcast_existing_task_error(
    app: &AppHandle,
    task_ids: &[String],
    message: String,
    record: Option<&Value>,
) {
    // A received error envelope/parse failure already has its collector record.
    for task_id in task_ids {
        let _ = app.emit(
            "pymss://worker-event",
            serde_json::json!({
            "type": "error", "taskId": task_id, "payload": { "message": message, "logRecord": record }
            }),
        );
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

fn is_log_envelope(envelope: &WorkerEnvelope) -> bool {
    matches!(
        envelope.event_type.as_str(),
        "worker_log"
            | "task_log"
            | "download_log"
            | "optional_package_log"
            | "runtime_install_log"
            | "runtime_core_update_log"
    )
}

fn bind_operation_scope(record: &mut LogRecord, operation_id: Option<&str>, fallback_task_id: Option<&str>) {
    if let Some(id) = operation_id {
        record.context.insert("operationId".into(), id.into());
    }
    if record.task_id.is_none() && record.request_id.is_none()
        && record.context.get("taskScope").and_then(Value::as_str) != Some("operation") {
        record.task_id = fallback_task_id.map(str::to_string);
    }
}

fn bind_log_envelope_scope(record: &mut LogRecord, envelope: &WorkerEnvelope) {
    if record.task_id.is_none()
        && record.context.get("taskScope").and_then(Value::as_str) != Some("operation") {
        record.task_id = envelope.task_id.clone();
    }
    if record.request_id.is_none() {
        record.request_id = envelope.request_id.clone();
    }
}

pub(crate) fn log_worker_event(app: &AppHandle, command: &str, envelope: &mut WorkerEnvelope) {
    log_worker_event_with_scope(app, command, envelope, None, None);
}

fn log_worker_event_with_scope(
    app: &AppHandle,
    command: &str,
    envelope: &mut WorkerEnvelope,
    operation_id: Option<&str>,
    fallback_task_id: Option<&str>,
) {
    if envelope
        .payload
        .get("logRecord")
        .and_then(|record| record.get("schemaVersion"))
        .and_then(Value::as_u64)
        == Some(1)
    {
        return;
    }
    if envelope.event_type == "env_info" {
        session_log::record_torch_diagnostics(app, &envelope.payload);
    }
    if is_log_envelope(envelope) {
        let mut record = LogRecord::from_payload(&envelope.payload).unwrap_or_else(|| {
            LogRecord::new(
                "worker",
                envelope.payload.get("level").and_then(Value::as_str),
                "worker",
                "worker.log",
                envelope
                    .payload
                    .get("message")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            )
        });
        bind_log_envelope_scope(&mut record, envelope);
        record
            .context
            .entry("command".to_string())
            .or_insert_with(|| command.into());
        bind_operation_scope(&mut record, operation_id, fallback_task_id);
        let record = session_log::append_record(app, record);
        if let Some(payload) = envelope.payload.as_object_mut() {
            payload.insert("logRecord".into(), serde_json::json!(record));
        }
        return;
    }
    let execution = envelope
        .payload
        .get("event")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let level = if envelope.event_type == "error" || envelope.event_type.ends_with("_failed") {
        "ERROR"
    } else if envelope.event_type == "audio_tool_result"
        && envelope.payload.get("failed").and_then(Value::as_array).is_some_and(|items| !items.is_empty()) {
        "WARN"
    } else if envelope.event_type == "workflow_execution" && !execution.ends_with("progress") {
        // The terminal error event owns the exception; node lifecycle is context.
        "INFO"
    } else if is_noisy_worker_event(&envelope.event_type) {
        "DEBUG"
    } else if is_significant_worker_event(&envelope.event_type) {
        "INFO"
    } else {
        "DEBUG"
    };
    let mut record = record_worker_event(command, envelope, level);
    bind_operation_scope(&mut record, operation_id, fallback_task_id);
    if is_noisy_worker_event(&envelope.event_type) || execution.ends_with("progress") {
        if let Ok(mut samples) = PROGRESS_LOG_TIMES
            .get_or_init(|| Mutex::new(HashMap::new()))
            .lock()
        {
            let key = format!(
                "{command}:{}:{}",
                envelope.task_id.as_deref().or(fallback_task_id).or(operation_id).unwrap_or_default(),
                record.node_id.as_deref().unwrap_or_default()
            );
            let Some(skipped) = sample_progress_log(&mut samples, key, Instant::now()) else {
                return;
            };
            if skipped > 0 {
                record
                    .context
                    .insert("progressUpdatesSkipped".into(), skipped.into());
            }
        }
    }
    let record = session_log::append_record(app, record);
    if let Some(payload) = envelope.payload.as_object_mut() {
        payload.insert("logRecord".into(), serde_json::json!(record));
    }
}

fn sample_progress_log(
    samples: &mut HashMap<String, (Instant, u64)>,
    key: String,
    now: Instant,
) -> Option<u64> {
    if let Some((last, skipped)) = samples.get_mut(&key) {
        if now.saturating_duration_since(*last) < Duration::from_secs(1) {
            *skipped = skipped.saturating_add(1);
            return None;
        }
        let dropped = *skipped;
        *last = now;
        *skipped = 0;
        return Some(dropped);
    }
    // Keep diagnostic sampling bounded across many short-lived tasks.
    if samples.len() >= 256 {
        samples.clear();
    }
    samples.insert(key, (now, 0));
    Some(0)
}

fn record_worker_event(command: &str, envelope: &WorkerEnvelope, level: &str) -> LogRecord {
    let payload = &envelope.payload;
    let mut record = LogRecord::new(
        "worker",
        Some(level),
        "worker",
        &envelope.event_type,
        payload
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or(&envelope.event_type),
    );
    record.task_id = envelope.task_id.clone();
    record.request_id = envelope.request_id.clone();
    record.node_id = payload
        .get("nodeId")
        .and_then(Value::as_str)
        .map(str::to_string);
    record.job_id = payload
        .get("jobId")
        .and_then(Value::as_str)
        .map(str::to_string);
    record.detail = payload
        .get("detail")
        .and_then(Value::as_str)
        .map(str::to_string);
    record.context.insert("command".into(), command.into());
    for key in [
        "code", "stage", "phase", "logPath", "operation", "action", "event", "version",
        "nodeType", "nodeIndex", "nodeCount", "nodeProgress", "overallFraction", "errorType",
        "audioIndex", "audioCount", "progress", "done", "unit",
        "completed", "total", "succeeded", "backend", "model", "format", "outputFormat",
    ] {
        if let Some(value) = payload.get(key).filter(|value| !value.is_null()) {
            if value.is_string() || value.is_boolean() || value.is_number() {
                record.context.insert(key.into(), value.clone());
            }
        }
    }
    for (key, count_key) in [("failed", "failedCount"), ("skipped", "skippedCount"),
        ("warnings", "warningCount"), ("outputs", "outputCount"), ("items", "resultCount")] {
        if let Some(items) = payload.get(key).and_then(Value::as_array) {
            record.context.insert(count_key.into(), items.len().into());
        }
    }
    record
}

fn is_significant_worker_event(event_type: &str) -> bool {
    ["started", "finished", "done", "failed", "cancelled"]
        .iter()
        .any(|suffix| event_type.ends_with(suffix))
        || matches!(event_type, "task_start" | "audio_tool_result" | "editor_mix_exported")
}

fn worker_lifecycle_level(command: &str) -> &'static str {
    if matches!(
        command,
        "infer"
            | "separate"
            | "separate_batch"
            | "infer_workflow"
            | "infer_workflow_batch"
            | "audio_tools"
            | "export_editor_mix"
            | "install_runtime"
            | "update_runtime_core"
            | "download_model"
    ) {
        "INFO"
    } else {
        "DEBUG"
    }
}

fn is_noisy_worker_event(event_type: &str) -> bool {
    let lower = event_type.to_ascii_lowercase();
    lower.contains("progress") || lower.contains("chunk") || lower.contains("heartbeat")
}

fn log_worker_parse_error(
    app: &AppHandle,
    command: &str,
    task_id: Option<&str>,
    operation_id: &str,
    err: &serde_json::Error,
    line: &str,
) -> LogRecord {
    let mut record = LogRecord::new(
        "studio",
        Some("error"),
        "studio.worker",
        "worker.invalid_stdout",
        "Invalid worker event",
    );
    record.task_id = task_id.map(str::to_string);
    record.detail = Some(err.to_string());
    record.context.insert("command".into(), command.into());
    record.context.insert("operationId".into(), operation_id.into());
    record.context.insert("raw".into(), line.into());
    session_log::append_record(app, record)
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
            "error"
                | "runtime_core_update_finished"
                | "runtime_core_update_plan"
                | "task_cancelled"
        ),
        "manage_optional_package" => {
            matches!(
                event_type,
                "error" | "optional_package_status" | "task_cancelled"
            )
        }
        "manage_plugin" => {
            matches!(event_type, "error" | "plugin_manage_result" | "task_cancelled")
        }
        "import_custom_model" => matches!(
            event_type,
            "error" | "custom_model_import_finished" | "task_cancelled"
        ),
        "infer" | "infer_workflow" => {
            matches!(event_type, "error" | "task_done" | "task_cancelled")
        }
        "audio_tools" => matches!(event_type, "error" | "audio_tool_result" | "task_cancelled"),
        "run_operation" => {
            matches!(event_type, "error" | "operation_result" | "task_cancelled")
        }
        "list_nodes" => matches!(event_type, "error" | "nodes_list"),
        "export_editor_mix" => {
            matches!(
                event_type,
                "error" | "editor_mix_exported" | "task_cancelled"
            )
        }
        _ => matches!(event_type, "error"),
    }
}

fn mark_task_terminal(app: &AppHandle, task_id: &str) {
    if let Ok(mut terminal) = app.state::<AppState>().terminal_tasks.lock() {
        terminal.insert(task_id.to_string());
    }
}

pub(crate) fn editor_export_task_token(task_id: &str) -> String {
    let token = task_id
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() || matches!(character, '-' | '_') {
                character
            } else {
                '_'
            }
        })
        .take(96)
        .collect::<String>();
    if token.is_empty() {
        "sync".to_string()
    } else {
        token
    }
}

pub(crate) fn cleanup_editor_export_temp_files(directory: &Path, task_id: &str) {
    let safe_task_id = editor_export_task_token(task_id);
    let prefix = format!(".pymss-export-{}-", &safe_task_id);
    let cancel_name = format!(".pymss-export-{safe_task_id}.cancel");
    let Ok(entries) = std::fs::read_dir(directory) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let matches_task = path
            .file_name()
            .and_then(|value| value.to_str())
            .is_some_and(|name| {
                (name.starts_with(&prefix) && name.ends_with(".part")) || name == cancel_name
            });
        if matches_task {
            let _ = std::fs::remove_file(path);
        }
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

fn worker_stdout_diagnostic(command: &str, operation_id: &str, line: &str) -> LogRecord {
    let mut record = LogRecord::new("third_party", None, "stdout", "process.stdout", line);
    record.context.insert("command".into(), command.into());
    record.context.insert("operationId".into(), operation_id.into());
    record
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
    let operation_id = session_log::record_operation_request(app, command, payload.as_ref().unwrap_or(&Value::Null), None);
    let result = run_worker_with_payload_inner(app, command, payload, &operation_id);
    match &result {
        Ok(value) => session_log::record_environment_probe(command, value),
        Err(error) => session_log::record_operation_failure(app, command, &operation_id, &error.to_string()),
    }
    result
}

fn run_worker_with_payload_inner(
    app: &AppHandle,
    command: &str,
    payload: Option<Value>,
    operation_id: &str,
) -> AppResult<Value> {
    let _application_access = acquire_application_operation()?;
    let _runtime_access = acquire_runtime_access(&RUNTIME_ACCESS, runtime_access_command(command, payload.as_ref().unwrap_or(&Value::Null)))?;
    let scratch = worker_scratch(command)?;
    let payload_file = match payload {
        Some(value) => Some(make_payload_file(command, None, value)?),
        None => None,
    };
    let mut cmd = match build_worker_command(app, command, payload_file.as_ref(), None, operation_id) {
        Ok(command) => command,
        Err(error) => {
            if let Some(path) = payload_file.as_ref() {
                let _ = std::fs::remove_file(path);
            }
            return Err(error);
        }
    };
    if let Some(scratch) = &scratch { cmd.env("PYMSS_SCRATCH_DIR", scratch.path()); }
    let started_at = std::time::Instant::now();
    session_log::append(
        app,
        "DEBUG",
        "worker.spawn",
        vec![("command", command.to_string())],
    );
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
    let stderr_operation_id = operation_id.to_string();
    let stderr_command = command.to_string();
    let stderr_lines: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let stderr_lines_for_thread = Arc::clone(&stderr_lines);
    let stderr_handle = stderr.map(|stderr| {
        std::thread::spawn(move || {
            read_lossy_lines(stderr, |line| {
                if forward_python_terminal_log(&stderr_app, &line) {
                    return;
                }
                if let Ok(mut lines) = stderr_lines_for_thread.lock() {
                    lines.push(line.clone());
                    if lines.len() > 20 {
                        lines.remove(0);
                    }
                }
                emit_worker_stderr(&stderr_app, line, &stderr_command, &stderr_operation_id);
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
            Ok(mut envelope) => {
                let is_log = is_log_envelope(&envelope);
                if !is_log {
                    last_payload = envelope.payload.clone();
                }
                log_worker_event_with_scope(app, command, &mut envelope, Some(operation_id), None);
                // Environment probes return through IPC or a correlated background event.
                // An untagged broadcast would bypass the caller's request ordering.
                if command != "env_info" || is_log {
                    let _ = app.emit("pymss://worker-event", &envelope);
                }
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
                if is_rocm_offload_arch_diagnostic(&line) {
                    session_log::append_record(app, worker_stdout_diagnostic(command, operation_id, &line));
                    emit_worker_stdout(app, line);
                } else {
                    log_worker_parse_error(app, command, None, operation_id, &err, &line);
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
        if status.success() {
            worker_lifecycle_level(command)
        } else {
            "INFO"
        },
        "worker.exit",
        vec![
            ("command", command.to_string()),
            ("status", status.to_string()),
            ("operationId", operation_id.to_string()),
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
    let operation_id = session_log::record_operation_request(&app, command, &payload, Some(&task_id));
    let log_app = app.clone();
    let result = spawn_worker_background_inner(app, state, command, task_id, payload, operation_id.clone());
    if let Err(error) = &result {
        session_log::record_operation_failure(&log_app, command, &operation_id, &error.to_string());
    }
    result
}

fn spawn_worker_background_inner(
    app: AppHandle,
    state: State<'_, AppState>,
    command: &str,
    task_id: String,
    mut payload: Value,
    operation_id: String,
) -> AppResult<()> {
    let mut application_access = Some(acquire_application_operation()?);
    let mut runtime_access = acquire_runtime_access(&RUNTIME_ACCESS, runtime_access_command(command, &payload))?;
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
    let scratch = worker_scratch(command)?;
    let mut audio_scope = if command == "run_operation" {
        let directory = operation_output_directory(&payload, &default_output_dir(&app)?)?;
        payload.as_object_mut()
            .ok_or_else(|| AppError::Worker("invalid operation request".into()))?
            .insert("outputDir".into(), Value::String(directory.to_string_lossy().into_owned()));
        Some(audio_output::AudioOutputScope::create(
            &storage::temp_dir(&app)?.join("audio-output-cleanup"), directory,
        )?)
    } else { None };
    let editor_export_directory = (command == "export_editor_mix")
        .then(|| {
            payload
                .get("exportDir")
                .and_then(Value::as_str)
                .map(PathBuf::from)
        })
        .flatten();
    let payload_file = make_payload_file(command, Some(&task_id), payload)?;
    let mut cmd = match build_worker_command(&app, command, Some(&payload_file), Some(&task_id), &operation_id) {
        Ok(command) => command,
        Err(error) => {
            let _ = std::fs::remove_file(&payload_file);
            return Err(error);
        }
    };
    if let Some(scope) = &audio_scope { scope.configure(&mut cmd); }
    if let Some(scratch) = &scratch { cmd.env("PYMSS_SCRATCH_DIR", scratch.path()); }
    let started_at = std::time::Instant::now();
    session_log::append(
        &app,
        "DEBUG",
        "worker.spawn",
        vec![
            ("command", command.to_string()),
            ("taskId", task_id.clone()),
        ],
    );
    let mut child: Child = match cmd.stdout(Stdio::piped()).stderr(Stdio::piped()).spawn() {
        Ok(child) => child,
        Err(error) => {
            let _ = std::fs::remove_file(&payload_file);
            return Err(error.into());
        }
    };
    if let Some(scope) = &mut audio_scope {
        if let Err(error) = scope.attach(child.id()) {
            kill_process_tree(child.id());
            let _ = child.kill();
            let _ = child.wait();
            let _ = std::fs::remove_file(&payload_file);
            return Err(error.into());
        }
    }
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
                if forward_python_terminal_log(&stderr_app, &line) {
                    return;
                }
                emit_batch_stderr(&stderr_app, &stderr_task_ids, line);
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
                Ok(mut envelope) => {
                    if envelope.event_type == "error" {
                        log_worker_event_with_scope(&app, &command_name, &mut envelope, Some(&operation_id), Some(&task_id));
                    }
                    if is_background_terminal_event(&command_name, envelope.event_type.as_str()) {
                        if let Some(envelope_task_id) = envelope.task_id.as_deref() {
                            if registered_task_ids.iter().any(|id| id == envelope_task_id) {
                                terminal_task_ids.insert(envelope_task_id.to_string());
                                mark_task_terminal(&app, envelope_task_id);
                            }
                        } else if envelope.event_type == "error" {
                            if let Some(request_id) = envelope.request_id.as_deref().filter(|id| {
                                registered_task_ids.iter().any(|task_id| task_id == id)
                            }) {
                                terminal_task_ids.insert(request_id.to_string());
                                mark_task_terminal(&app, request_id);
                            } else {
                                let message = worker_error_message(&envelope);
                                if matches!(command_name.as_str(), "update_runtime_core" | "manage_plugin") {
                                    core_terminal_error =
                                        Some((message, envelope.payload.get("logRecord").cloned()));
                                } else {
                                    broadcast_existing_task_error(
                                        &app,
                                        &registered_task_ids,
                                        message,
                                        envelope.payload.get("logRecord"),
                                    );
                                }
                                terminal_task_ids.extend(registered_task_ids.iter().cloned());
                                for registered_task_id in &registered_task_ids {
                                    mark_task_terminal(&app, registered_task_id);
                                }
                            }
                        } else {
                            terminal_task_ids.insert(task_id.clone());
                            mark_task_terminal(&app, &task_id);
                        }
                    }
                    if matches!(command_name.as_str(), "update_runtime_core" | "manage_plugin")
                        && is_background_terminal_event(&command_name, envelope.event_type.as_str())
                    {
                        core_terminal_events.push(envelope);
                        return;
                    }
                    log_worker_event_with_scope(&app, &command_name, &mut envelope, Some(&operation_id), Some(&task_id));
                    let _ = app.emit("pymss://worker-event", &envelope);
                }
                Err(err) => {
                    if is_rocm_offload_arch_diagnostic(&line) {
                        emit_batch_stderr(&app, &registered_task_ids, line.clone());
                    } else {
                        let record = log_worker_parse_error(
                            &app,
                            &command_name,
                            Some(&task_id),
                            &operation_id,
                            &err,
                            &line,
                        );
                        let record = serde_json::to_value(record).ok();
                        broadcast_existing_task_error(
                            &app,
                            &registered_task_ids,
                            format!("Invalid worker event: {err}"),
                            record.as_ref(),
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
        if let Some(directory) = editor_export_directory.as_deref() {
            cleanup_editor_export_temp_files(directory, &task_id);
        }
        if let Some(mut scope) = audio_scope.take() {
            // Dropping here also handles a failed wait while both leases are held.
            if exit_status.is_some() { scope.finish(); }
        }
        // The parent owns scratch so forced worker cancellation also removes
        // temporary AUDIO files, even when Python cannot execute its finally.
        drop(scratch);
        drop(runtime_access.take());
        drop(application_access.take());
        // Finish Python cleanup before the terminal event starts the UI's environment refresh.
        if let Some((message, record)) = core_terminal_error {
            broadcast_existing_task_error(&app, &registered_task_ids, message, record.as_ref());
        }
        for mut envelope in core_terminal_events {
            log_worker_event_with_scope(&app, &command_name, &mut envelope, Some(&operation_id), Some(&task_id));
            let _ = app.emit("pymss://worker-event", &envelope);
        }
        // Retain the consumed IDs for notification: draining them once for logging
        // and again for emission loses the plugin supervisor's cancellation ACK.
        let mut cancelled_task_ids = take_cancelled_tasks(
            &app.state::<AppState>().cancelled_tasks, &registered_task_ids,
        );
        let was_cancelled = !cancelled_task_ids.is_empty();
        for id in &cancelled_task_ids {
            terminal_task_ids.insert(id.clone());
        }
        session_log::append(
            &app,
            match exit_status.as_ref() {
                _ if was_cancelled => "INFO",
                Some(status) if status.success() => worker_lifecycle_level(&command_name),
                _ => "INFO",
            },
            "worker.exit",
            vec![
                ("command", command_name.clone()),
                ("operationId", operation_id.clone()),
                ("taskId", task_id.clone()),
                ("cancelled", was_cancelled.to_string()),
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
        cancelled_task_ids.extend(take_cancelled_tasks(
            &app.state::<AppState>().cancelled_tasks, &registered_task_ids,
        ));
        for registered_task_id in cancelled_task_ids {
            terminal_task_ids.insert(registered_task_id.clone());
            if command_name == "manage_plugin" {
                let _ = app.emit("pymss://worker-event", serde_json::json!({
                    "type": "task_cancelled", "taskId": registered_task_id,
                    "payload": { "message": "Cancelled" }
                }));
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
                Some(status)
                    if status.success()
                        && requires_background_result(&command_name) =>
                {
                    emit_task_error_to_all(
                        &app,
                        &missing_terminal_task_ids,
                        "worker exited without a result".to_string(),
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
            for registered_task_id in &registered_task_ids {
                if tasks
                    .get(registered_task_id)
                    .map(|registered| Arc::ptr_eq(registered, &shared_child))
                    .unwrap_or(false)
                {
                    tasks.remove(registered_task_id);
                }
            }
        }
        if let Ok(mut terminal) = cleanup_state.terminal_tasks.lock() {
            for registered_task_id in &registered_task_ids {
                terminal.remove(registered_task_id);
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
    fn worker_scratch_is_scoped_and_cleans_orphaned_child_files() {
        let first = super::worker_scratch("infer_workflow").unwrap().unwrap();
        let second = super::worker_scratch("run_operation").unwrap().unwrap();
        assert_ne!(first.path(), second.path());
        assert!(super::worker_scratch("list_nodes").unwrap().is_none());
        let path = first.path().to_path_buf();
        let nested = path.join("pymss-audio-aborted");
        fs::create_dir_all(&nested).unwrap();
        fs::write(nested.join("vocals.wav"), b"partial audio").unwrap();
        drop(first);
        assert!(!path.exists());
        assert!(second.path().exists());
    }

    #[test]
    fn cancelled_task_ids_survive_logging_for_supervisor_notification() {
        use std::collections::HashSet;
        use std::sync::Mutex;
        let registered = vec!["plugin_manage_test".to_string(), "linked".to_string()];
        let cancelled = Mutex::new(HashSet::from([registered[0].clone(), "foreign".into()]));
        let mut notifications = super::take_cancelled_tasks(&cancelled, &registered);
        assert!(!notifications.is_empty(), "exit logging must see cancellation");
        // A cancellation arriving during exit logging is included in the same ACK batch.
        cancelled.lock().unwrap().insert(registered[1].clone());
        notifications.extend(super::take_cancelled_tasks(&cancelled, &registered));
        assert_eq!(notifications, registered);
        assert!(super::take_cancelled_tasks(&cancelled, &registered).is_empty());
        assert!(cancelled.lock().unwrap().contains("foreign"));
    }

    #[test]
    fn plugin_source_info_uses_bootstrap_without_an_active_runtime() {
        assert_eq!(super::select_worker_python("plugin_source_info", "bootstrap", None).unwrap(), "bootstrap");
    }

    #[test]
    fn plugin_source_overrides_require_developer_mode_and_snapshot_the_settings() {
        let source = json!({"mode": "local", "localPath": "C:/plugins"});
        let mut command = std::process::Command::new("python");
        command.env("PYMSS_STUDIO_PLUGIN_SOURCE_OVERRIDE", "inherited");
        super::apply_plugin_source_override(&mut command, false, &source).unwrap();
        let value = command.get_envs().find(|(key, _)| *key == "PYMSS_STUDIO_PLUGIN_SOURCE_OVERRIDE").unwrap().1;
        assert!(value.is_none());
        super::apply_plugin_source_override(&mut command, true, &source).unwrap();
        let value = command.get_envs().find(|(key, _)| *key == "PYMSS_STUDIO_PLUGIN_SOURCE_OVERRIDE").unwrap().1.unwrap();
        assert_eq!(serde_json::from_str::<serde_json::Value>(&value.to_string_lossy()).unwrap(), source);
        super::apply_plugin_source_override(&mut command, true, &serde_json::Value::Null).unwrap();
        assert!(command.get_envs().find(|(key, _)| *key == "PYMSS_STUDIO_PLUGIN_SOURCE_OVERRIDE").unwrap().1.is_none());
    }

    #[test]
    fn operation_output_scopes_use_the_same_directory_as_python() {
        let root = std::env::temp_dir().join("pymss-operation-scope");
        let default = root.join("outputs");
        for payload in [json!({}), json!({"outputDir": null}), json!({"outputDir": ""})] {
            assert_eq!(super::operation_output_directory(&payload, &default).unwrap(), std::path::absolute(&default).unwrap());
        }
        assert_eq!(super::operation_output_directory(&json!({"outputDir": "relative"}), &default).unwrap(), std::path::absolute(root.join("relative")).unwrap());
        assert_eq!(super::operation_output_directory(&json!({"outputDir": " spaced "}), &default).unwrap(), std::path::absolute(root.join(" spaced ")).unwrap());
        assert_eq!(super::operation_output_directory(&json!({"outputDir": &default}), &default).unwrap(), std::path::absolute(&default).unwrap());
        assert!(super::operation_output_directory(&json!({"outputDir": []}), &default).is_err());
    }

    #[test]
    fn operation_workers_must_emit_a_terminal_result_even_after_a_successful_exit() {
        assert!(super::requires_background_result("run_operation"));
        assert!(super::requires_background_result("manage_plugin"));
        assert!(!super::requires_background_result("list_operations"));
    }

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
        for command in [
            "infer",
            "infer_workflow",
            "env_info",
            "list_models",
            "audio_tools",
            "test_connection",
            "update_runtime_core",
        ] {
            let result = super::acquire_runtime_access(&access, command);
            assert!(result.is_err(), "{command} must wait for the core update");
            assert!(result.err().unwrap().to_string().contains("RUNTIME_BUSY"));
        }
        for command in [
            "health",
            "runtime_info",
            "runtime_core_versions",
            "runtime_env_sizes",
        ] {
            assert!(super::acquire_runtime_access(&access, command)
                .unwrap()
                .is_none());
        }
        drop(update);
        assert!(super::acquire_runtime_access(&access, "infer").is_ok());
    }

    #[test]
    fn plugin_management_excludes_execution_and_runtime_switching() {
        let access = std::sync::Mutex::new(super::RuntimeAccess::default());
        let operation = super::acquire_runtime_access(&access, "run_operation").unwrap();
        assert!(super::acquire_runtime_access(&access, "manage_plugin").is_err());
        drop(operation);
        let management = super::acquire_runtime_access(&access, "manage_plugin").unwrap();
        for command in ["infer", "list_operations", "list_nodes", "plugin_marketplace", "manage_plugin",
                        "activate_runtime", "delete_runtime", "update_runtime_core"] {
            assert!(super::acquire_runtime_access(&access, command).is_err(), "{command}");
        }
        drop(management);
        assert!(super::acquire_runtime_access(&access, "plugin_marketplace").is_ok());
    }

    #[test]
    fn plugin_management_result_is_terminal_but_logs_are_not() {
        assert!(is_background_terminal_event("manage_plugin", "plugin_manage_result"));
        assert!(is_background_terminal_event("manage_plugin", "error"));
        assert!(is_background_terminal_event("manage_plugin", "task_cancelled"));
        assert!(!is_background_terminal_event("manage_plugin", "plugin_stage"));
        assert!(!is_background_terminal_event("manage_plugin", "plugin_log"));
    }

    #[test]
    fn plugin_installs_share_access_and_keep_other_operations_excluded_until_all_exit() {
        let access = std::sync::Mutex::new(super::RuntimeAccess::default());
        let install = super::runtime_access_command("manage_plugin", &json!({"action": "install"}));
        let first = super::acquire_runtime_access(&access, install).unwrap();
        let second = super::acquire_runtime_access(&access, install).unwrap();
        for command in ["infer", "run_operation", "manage_plugin", "plugin_marketplace", "activate_runtime", "delete_runtime", "update_runtime_core"] {
            assert!(super::acquire_runtime_access(&access, command).is_err(), "{command}");
        }
        drop(first);
        assert!(super::acquire_runtime_access(&access, "run_operation").is_err());
        drop(second);
        let reader = super::acquire_runtime_access(&access, "run_operation").unwrap();
        assert!(super::acquire_runtime_access(&access, install).is_err());
        drop(reader);
        let update = super::acquire_runtime_access(&access, "update_runtime_core").unwrap();
        assert!(super::acquire_runtime_access(&access, install).is_err());
        drop(update);
        assert_eq!(super::runtime_access_command("manage_plugin", &json!({"action":"update"})), "manage_plugin");
        assert_eq!(super::runtime_access_command("run_operation", &json!({"action":"install"})), "run_operation");
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
            Err(crate::error::AppError::Worker(
                "process start failed".into(),
            ))
        };
        assert!(fail_start().is_err());
        assert!(super::acquire_runtime_access(&access, "update_runtime_core").is_ok());
    }

    #[test]
    fn application_update_excludes_native_operations_for_their_thread_lifetime() {
        let access = std::sync::Mutex::new(super::RuntimeAccess::default());
        let operation = super::acquire_runtime_access(&access, "application_operation").unwrap();
        assert!(super::acquire_runtime_access(&access, "update_runtime_core").is_err());
        std::thread::scope(|scope| scope.spawn(move || drop(operation)).join().unwrap());
        let update = super::acquire_runtime_access(&access, "update_runtime_core").unwrap();
        assert!(super::acquire_runtime_access(&access, "application_operation").is_err());
        drop(update);
        assert!(super::acquire_runtime_access(&access, "application_operation").is_ok());
    }

    #[test]
    fn editor_creation_and_update_reservations_exclude_each_other() {
        let update = super::acquire_application_update().unwrap();
        std::thread::scope(|scope| {
            scope.spawn(|| {
                // A delayed open request arriving after update reservation must fail.
                assert!(super::acquire_application_operation().is_err());
            }).join().unwrap();
        });
        drop(update);

        std::thread::scope(|scope| {
            let (started, pending) = std::sync::mpsc::channel();
            let (resume, wait) = std::sync::mpsc::channel();
            let opening = scope.spawn(move || {
                let _creation_access = super::acquire_application_operation().unwrap();
                started.send(()).unwrap();
                // Cover an open request awaiting the shared window-creation gate.
                wait.recv().unwrap();
            });
            pending.recv().unwrap();
            let blocked = super::acquire_application_update().is_err();
            resume.send(()).unwrap();
            opening.join().unwrap();
            assert!(blocked);
        });
        assert!(super::acquire_application_update().is_ok());
    }

    #[test]
    fn connection_test_prefers_active_runtime_and_falls_back_to_bootstrap() {
        assert_eq!(
            super::select_worker_python(
                "test_connection",
                "bootstrap-python",
                Some("active-python".into()),
            )
            .unwrap(),
            "active-python",
        );
        assert_eq!(
            super::select_worker_python("test_connection", "bootstrap-python", None).unwrap(),
            "bootstrap-python",
        );
    }

    #[test]
    fn runtime_management_ignores_active_runtime_but_inference_requires_it() {
        assert_eq!(
            super::select_worker_python(
                "install_runtime",
                "bootstrap-python",
                Some("active-python".into()),
            )
            .unwrap(),
            "bootstrap-python",
        );
        assert!(super::select_worker_python("infer", "bootstrap-python", None).is_err());
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
            assert_eq!(
                super::resource_worker_path(&resource),
                Some(candidate.clone())
            );
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
            let names = if windows {
                ["python.exe", "Scripts/python.exe"]
            } else {
                ["bin/python3", "bin/python"]
            };
            assert_eq!(
                super::embedded_python_from(Some(&resource), &exe, windows),
                None
            );
            for root in roots.iter().rev() {
                for name in names.iter().rev() {
                    let candidate = root.join(name);
                    fixture.file(&candidate);
                    assert_eq!(
                        super::embedded_python_from(Some(&resource), &exe, windows),
                        Some(candidate)
                    );
                }
            }
            assert_eq!(
                super::embedded_python_from(None, &exe, windows),
                Some(roots[3].join(names[0]))
            );
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
        assert_eq!(
            super::embedded_python_from(Some(&resource), &exe, true),
            Some(python)
        );
    }

    #[test]
    fn bin_candidates_only_include_app_owned_directories() {
        let fixture = ResourceFixture::new("bin-layout");
        let resource = fixture.0.join("resources");
        let exe = fixture.0.join("portable");
        let expected = vec![
            resource.join("bin"),
            resource.join("_up_/bin"),
            resource.join("resources/bin"),
            exe.join("bin"),
        ];
        assert_eq!(
            super::bundled_bin_candidates(Some(&resource), &exe),
            expected
        );
        fs::create_dir_all(&expected[1]).unwrap();
        fs::create_dir_all(&expected[3]).unwrap();
        fixture.file(&expected[2]);
        let hits = super::bundled_bin_candidates(Some(&resource), &exe)
            .into_iter()
            .filter(|dir| dir.is_dir())
            .collect::<Vec<_>>();
        assert_eq!(hits, vec![expected[1].clone(), expected[3].clone()]);
        assert_eq!(
            super::bundled_bin_candidates(None, &exe),
            vec![exe.join("bin")]
        );
        let duplicate = super::bundled_bin_candidates(Some(&exe), &exe);
        assert_eq!(duplicate[0], duplicate[3]);
    }

    #[test]
    fn worker_python_environment_drops_host_python_and_conda_overrides() {
        let mut command = std::process::Command::new("python");
        for key in super::HOST_PYTHON_ENV_VARS {
            command.env(key, "host-runtime");
        }

        command.env("NUMBA_CACHE_DIR", "host-runtime");
        command.env("PIP_CACHE_DIR", "host-runtime");
        let cache_root = Path::new("user-data/temp");
        super::isolate_python_environment(&mut command, cache_root);

        for key in super::HOST_PYTHON_ENV_VARS {
            let (_, value) = command
                .get_envs()
                .find(|(name, _)| *name == std::ffi::OsStr::new(key))
                .unwrap_or_else(|| panic!("{key} was not explicitly removed"));
            assert!(value.is_none(), "{key} still leaks into the Worker");
        }
        let (_, no_user_site) = command
            .get_envs()
            .find(|(name, _)| *name == std::ffi::OsStr::new("PYTHONNOUSERSITE"))
            .expect("PYTHONNOUSERSITE was not configured");
        assert_eq!(no_user_site, Some(std::ffi::OsStr::new("1")));
        for (key, directory) in [
            ("NUMBA_CACHE_DIR", "numba-cache"),
            ("PIP_CACHE_DIR", "pip-cache"),
        ] {
            let (_, value) = command
                .get_envs()
                .find(|(name, _)| *name == std::ffi::OsStr::new(key))
                .unwrap();
            assert_eq!(value, Some(cache_root.join(directory).as_os_str()));
        }
    }

    #[test]
    fn bundled_env_selection_preserves_canonical_path_overlap_and_missing_path_behavior() {
        let fixture = ResourceFixture::new("env-overlap");
        let user = fixture.0.join("user");
        let bundled = fixture.0.join("bundled");
        fs::create_dir_all(&user).unwrap();
        fs::create_dir_all(&bundled).unwrap();
        let canonical_user = user.canonicalize().ok();
        assert_eq!(
            super::distinct_bundled_runtime_env(canonical_user.clone(), Some(user.join("."))),
            None
        );
        assert_eq!(
            super::distinct_bundled_runtime_env(canonical_user.clone(), Some(bundled.clone())),
            Some(bundled.clone())
        );
        assert_eq!(
            super::distinct_bundled_runtime_env(canonical_user.clone(), None),
            None
        );
        assert_eq!(
            super::distinct_bundled_runtime_env(None, Some(bundled.clone())),
            Some(bundled)
        );
        let missing = fixture.0.join("missing");
        assert_eq!(
            super::distinct_bundled_runtime_env(None, Some(missing.clone())),
            None
        );
        assert_eq!(
            super::distinct_bundled_runtime_env(canonical_user, Some(missing.clone())),
            Some(missing)
        );
    }

    #[cfg(unix)]
    #[test]
    fn symlinked_venv_interpreter_remains_usable_by_business_commands() {
        use std::os::unix::fs::symlink;
        let unique = temp_root("venv-python-symlink");
        let root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../.tmp").join(unique.file_name().unwrap());
        fs::create_dir_all(&root).unwrap();
        let fixture = ResourceFixture(root);
        let base = fixture.0.join("base/bin/python3");
        let python = fixture.0.join("runtime-envs/mlx/bin/python");
        fixture.file(&base);
        fs::create_dir_all(python.parent().unwrap()).unwrap();
        symlink(&base, &python).unwrap();
        let pointer = fixture.0.join("runtime-envs/active-runtime.json");
        let expected = python.parent().unwrap().canonicalize().unwrap().join("python");
        for recorded in [python.to_string_lossy().to_string(), "mlx/bin/python".to_string()] {
            fs::write(&pointer, serde_json::to_vec(&json!({
                "pythonPath": recorded, "backend": "mlx"
            })).unwrap()).unwrap();
            let resolved = super::resolve_active_runtime_record(&pointer).unwrap().0;
            let active = super::active_path_backend_matches(&pointer, &resolved).then_some(resolved);
            let selected = super::select_worker_python("plugin_marketplace", "bootstrap", active);
            assert!(selected.is_ok(), "an installed MLX venv was rejected: {selected:?}");
            assert_eq!(selected.unwrap(), expected.to_string_lossy());
        }
    }

    #[test]
    fn active_pointer_resolution_accepts_existing_relative_and_absolute_paths_without_rewriting() {
        let fixture = ResourceFixture::new("pointer-resolution");
        let python = fixture.0.join("cpu/bin/python");
        fixture.file(&python);
        let pointer = fixture.0.join("active-runtime.json");
        for path in [
            "cpu/bin/python".to_string(),
            python.to_string_lossy().to_string(),
        ] {
            let bytes = serde_json::to_vec(
                &json!({"pythonPath": path, "backend": "cpu", "source": "bundled", "custom": true}),
            )
            .unwrap();
            fs::write(&pointer, &bytes).unwrap();
            assert_eq!(
                super::resolve_active_runtime_record(&pointer),
                Some((
                    python.canonicalize().unwrap().to_string_lossy().to_string(),
                    Some("bundled".into())
                ))
            );
            assert_eq!(fs::read(&pointer).unwrap(), bytes);
        }
        for bytes in [
            b"{}".as_slice(),
            b"not json",
            br#"{"pythonPath":"missing/python"}"#,
            br#"{"pythonPath":"cpu"}"#,
        ] {
            fs::write(&pointer, bytes).unwrap();
            assert_eq!(super::resolve_active_runtime_record(&pointer), None);
            assert_eq!(fs::read(&pointer).unwrap(), bytes);
        }
    }

    #[cfg(unix)]
    #[test]
    fn active_pointer_keeps_the_posix_venv_executable_link() {
        let fixture = ResourceFixture::new("venv-pointer");
        let bootstrap = fixture.0.join("bundle/python-runtime/bin/python3");
        fixture.file(&bootstrap);
        let runtime_root = fixture.0.join("user/runtime-envs");
        let python = runtime_root.join("cpu/bin/python");
        fs::create_dir_all(python.parent().unwrap()).unwrap();
        std::os::unix::fs::symlink("../../../../bundle/python-runtime/bin/python3", &python)
            .unwrap();
        let pointer = runtime_root.join("active-runtime.json");
        let expected = python
            .parent()
            .unwrap()
            .canonicalize()
            .unwrap()
            .join("python");
        for path in [
            "cpu/bin/python".to_string(),
            python.to_string_lossy().to_string(),
        ] {
            fs::write(
                &pointer,
                serde_json::to_vec(&json!({"backend": "cpu", "pythonPath": path})).unwrap(),
            )
            .unwrap();
            let (resolved, _) = super::resolve_active_runtime_record(&pointer).unwrap();
            assert_eq!(Path::new(&resolved), expected);
            assert_ne!(Path::new(&resolved), python.canonicalize().unwrap());
            assert!(super::active_path_backend_matches(&pointer, &resolved));
            assert_eq!(
                super::select_worker_python(
                    "infer",
                    &bootstrap.to_string_lossy(),
                    Some(resolved.clone())
                )
                .unwrap(),
                resolved,
            );
        }
    }

    #[cfg(unix)]
    #[test]
    fn venv_links_sharing_an_executable_do_not_share_runtime_overlays() {
        let fixture = ResourceFixture::new("venv-overlay");
        let bootstrap = fixture.0.join("python-runtime/bin/python3");
        fixture.file(&bootstrap);
        let runtime_root = fixture.0.join("python-runtime/runtime-envs");
        let cpu_python = runtime_root.join("cpu/bin/python");
        let mlx_python = runtime_root.join("mlx/bin/python");
        for python in [&cpu_python, &mlx_python] {
            fs::create_dir_all(python.parent().unwrap()).unwrap();
            std::os::unix::fs::symlink("../../../bin/python3", python).unwrap();
        }
        let overlay = runtime_root.join(".overlays/cpu/generation/site-packages");
        fs::create_dir_all(&overlay).unwrap();
        let pointer = runtime_root.join("active-runtime.json");
        fs::write(
            &pointer,
            serde_json::to_vec(
                &json!({"backend": "cpu", "pythonPath": "cpu/bin/python", "overlayPath": overlay}),
            )
            .unwrap(),
        )
        .unwrap();
        assert!(
            super::is_bundled_runtime_python_path(&pointer, &cpu_python.to_string_lossy()).unwrap()
        );
        assert_eq!(
            super::resolve_runtime_overlay_for_record(
                &pointer,
                &cpu_python.to_string_lossy(),
                std::slice::from_ref(&runtime_root),
            ),
            Some(overlay.canonicalize().unwrap()),
        );
        assert_eq!(
            super::resolve_runtime_overlay_for_record(
                &pointer,
                &mlx_python.to_string_lossy(),
                &[runtime_root],
            ),
            None,
        );
    }

    #[test]
    fn bundled_bootstrap_uses_user_overlay_without_changing_bundle() {
        let fixture = ResourceFixture::new("bundled-user-overlay");
        let bundled = fixture
            .0
            .join("Installed.app/Contents/Resources/python-runtime");
        let python = bundled.join("bin/python3");
        fixture.file(&python);
        let bundled_pointer = bundled.join("runtime-envs/active-runtime.json");
        fs::create_dir_all(bundled_pointer.parent().unwrap()).unwrap();
        let bundled_state = serde_json::to_vec(&json!({
            "backend": "mlx", "source": "bundled", "pythonPath": "../bin/python3",
        }))
        .unwrap();
        fs::write(&bundled_pointer, &bundled_state).unwrap();
        let user_root = fixture.0.join("user/runtime-envs");
        let overlay = user_root.join(".overlays/mlx/generation/site-packages");
        fs::create_dir_all(&overlay).unwrap();
        let user_pointer = user_root.join("active-runtime.json");
        fs::write(
            &user_pointer,
            serde_json::to_vec(&json!({
                "backend": "mlx", "source": "bundled", "pythonPath": python,
                "overlayPath": overlay,
            }))
            .unwrap(),
        )
        .unwrap();
        let active_python = super::resolve_active_runtime_record(&bundled_pointer)
            .unwrap()
            .0;
        assert!(super::is_bundled_runtime_python_path(&bundled_pointer, &active_python).unwrap());
        assert_eq!(
            super::resolve_runtime_overlay_for_record(
                &user_pointer,
                &active_python,
                &[user_root.clone()],
            ),
            Some(overlay.canonicalize().unwrap())
        );
        fs::write(
            &user_pointer,
            serde_json::to_vec(&json!({
                "backend": "mlx", "source": "bundled", "pythonPath": python,
            }))
            .unwrap(),
        )
        .unwrap();
        assert_eq!(
            super::resolve_runtime_overlay_for_record(&user_pointer, &active_python, &[user_root],),
            None
        );
        assert_eq!(fs::read(bundled_pointer).unwrap(), bundled_state);
    }

    #[test]
    fn runtime_overlay_must_match_the_active_python_and_runtime_root() {
        let fixture = ResourceFixture::new("runtime-overlay");
        let runtime_root = fixture.0.join("runtime-envs");
        let python = runtime_root.join("cpu/bin/python");
        let overlay = runtime_root.join(".overlays/cpu/generation/site-packages");
        let outside = fixture.0.join("outside/site-packages");
        fixture.file(&python);
        fs::create_dir_all(&overlay).unwrap();
        fs::create_dir_all(&outside).unwrap();
        let pointer = runtime_root.join("active-runtime.json");

        fs::write(
            &pointer,
            serde_json::to_vec(&json!({
                "pythonPath": python,
                "backend": "cpu",
                "overlayPath": overlay,
            }))
            .unwrap(),
        )
        .unwrap();
        assert_eq!(
            super::resolve_runtime_overlay_for_record(
                &pointer,
                &python.to_string_lossy(),
                std::slice::from_ref(&runtime_root),
            ),
            Some(overlay.canonicalize().unwrap()),
        );

        fs::write(
            &pointer,
            serde_json::to_vec(&json!({
                "pythonPath": python,
                "backend": "cpu",
                "overlayPath": outside,
            }))
            .unwrap(),
        )
        .unwrap();
        assert_eq!(
            super::resolve_runtime_overlay_for_record(
                &pointer,
                &python.to_string_lossy(),
                std::slice::from_ref(&runtime_root),
            ),
            None,
        );
        assert_eq!(
            super::resolve_runtime_overlay_for_record(
                &pointer,
                &fixture.0.join("other-python").to_string_lossy(),
                &[runtime_root],
            ),
            None,
        );
    }

    #[cfg(windows)]
    #[test]
    fn rocm_tools_keep_user_before_bundled_then_regular_bins_and_existing_path() {
        let fixture = ResourceFixture::new("rocm-path-order");
        let user = fixture.0.join("user");
        let bundled = fixture.0.join("bundled");
        let mut expected = Vec::new();
        for root in [&user, &bundled] {
            let package = root
                .join("rocm")
                .join("Lib")
                .join("site-packages")
                .join("_rocm_sdk_core_1");
            let tool = package.join("lib").join("llvm").join("bin");
            fixture.file(&tool.join("offload-arch.exe"));
            let sdk_bin = package.join("bin");
            fs::create_dir_all(&sdk_bin).unwrap();
            expected.extend([tool, sdk_bin]);
            fixture
                .file(&root.join("rocm/Lib/site-packages/unrelated/lib/llvm/bin/offload-arch.exe"));
        }
        let mut dirs =
            super::rocm_native_tool_dirs_from(vec![user, fixture.0.join("absent"), bundled]);
        assert_eq!(dirs, expected);
        let bin = fixture.0.join("bin");
        fs::create_dir_all(&bin).unwrap();
        dirs.extend(super::bundled_bin_candidates(None, &fixture.0));
        expected.push(bin);
        let joined = expected
            .iter()
            .map(|p| p.to_string_lossy())
            .collect::<Vec<_>>()
            .join(";");
        assert_eq!(
            super::prepend_path(Some("existing-path".into()), dirs),
            Some(format!("{joined};existing-path"))
        );
    }

    #[test]
    fn audio_tool_result_is_a_background_terminal_event() {
        assert!(is_background_terminal_event(
            "audio_tools",
            "audio_tool_result"
        ));
        assert!(is_background_terminal_event("audio_tools", "error"));
        assert!(!is_background_terminal_event(
            "audio_tools",
            "audio_tool_progress"
        ));
    }

    #[test]
    fn operation_result_is_a_background_terminal_event() {
        assert!(is_background_terminal_event("run_operation", "operation_result"));
        assert!(is_background_terminal_event("run_operation", "error"));
        assert!(is_background_terminal_event("run_operation", "task_cancelled"));
        assert!(!is_background_terminal_event(
            "run_operation",
            "operation_progress"
        ));
    }

    #[test]
    fn editor_export_result_is_a_background_terminal_event() {
        assert!(is_background_terminal_event(
            "export_editor_mix",
            "editor_mix_exported"
        ));
        assert!(is_background_terminal_event("export_editor_mix", "error"));
        assert!(!is_background_terminal_event(
            "export_editor_mix",
            "editor_export_progress"
        ));
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
        let python = root
            .join("runtime-envs")
            .join("cuda")
            .join("bin")
            .join("python");
        fs::create_dir_all(python.parent().unwrap()).unwrap();
        fs::write(&python, "stub").unwrap();
        let python = fs::canonicalize(python).unwrap();
        fs::write(
            &active,
            serde_json::to_vec(&json!({"backend": "cuda", "pythonPath": python.to_string_lossy()}))
                .unwrap(),
        )
        .unwrap();
        assert!(super::active_path_backend_matches(
            &active,
            &python.to_string_lossy()
        ));

        fs::write(
            &active,
            serde_json::to_vec(&json!({"backend": "mlx", "pythonPath": python.to_string_lossy()}))
                .unwrap(),
        )
        .unwrap();
        assert!(!super::active_path_backend_matches(
            &active,
            &python.to_string_lossy()
        ));

        fs::write(
            &active,
            serde_json::to_vec(
                &json!({"backend": "../outside", "pythonPath": python.to_string_lossy()}),
            )
            .unwrap(),
        )
        .unwrap();
        assert!(!super::active_path_backend_matches(
            &active,
            &python.to_string_lossy()
        ));

        fs::write(
            &active,
            serde_json::to_vec(&json!({"pythonPath": python.to_string_lossy()})).unwrap(),
        )
        .unwrap();
        assert!(!super::active_path_backend_matches(
            &active,
            &python.to_string_lossy()
        ));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn external_runtime_pointer_requires_a_development_debug_override() {
        let root = temp_root("external-debug-runtime");
        let active = root.join("active-runtime.json");
        let python = root
            .join("external")
            .join("cuda")
            .join("bin")
            .join("python");
        fs::create_dir_all(python.parent().unwrap()).unwrap();
        fs::write(&python, "stub").unwrap();
        let python = fs::canonicalize(python).unwrap();

        fs::write(
            &active,
            serde_json::to_vec(&json!({
                "backend": "cuda",
                "pythonPath": python.to_string_lossy(),
                "debugOverride": true,
            }))
            .unwrap(),
        )
        .unwrap();
        assert!(super::active_debug_override_path_allowed(
            &active,
            &python.to_string_lossy(),
            true,
        ));
        assert!(!super::active_debug_override_path_allowed(
            &active,
            &python.to_string_lossy(),
            false,
        ));

        fs::write(
            &active,
            serde_json::to_vec(&json!({
                "backend": "cuda",
                "pythonPath": python.to_string_lossy(),
            }))
            .unwrap(),
        )
        .unwrap();
        assert!(!super::active_debug_override_path_allowed(
            &active,
            &python.to_string_lossy(),
            true,
        ));
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
        assert!(!super::is_rocm_offload_arch_diagnostic(
            "unrelated Python traceback"
        ));
    }

    #[test]
    fn logging_envelopes_never_replace_command_results() {
        for event_type in [
            "worker_log",
            "task_log",
            "download_log",
            "optional_package_log",
            "runtime_install_log",
            "runtime_core_update_log",
        ] {
            let envelope: super::WorkerEnvelope = serde_json::from_value(
                serde_json::json!({"type": event_type, "payload": {"message": "line"}}),
            )
            .unwrap();
            assert!(super::is_log_envelope(&envelope));
            assert!(!super::is_background_terminal_event(
                "infer_workflow",
                event_type
            ));
        }
        let result: super::WorkerEnvelope = serde_json::from_value(
            serde_json::json!({"type": "models", "payload": {"schemaVersion": 1, "models": []}}),
        )
        .unwrap();
        assert!(!super::is_log_envelope(&result));
    }

    #[test]
    fn rocm_stdout_diagnostics_preserve_raw_message_and_operation_scope() {
        for line in [
            "Fatal error in launcher: Unable to create process using 'D:/runtime/python.exe offload-arch'",
            "offload-arch failed with return code 1",
            "[rocm_sdk] offload-arch probe failed",
            "[stderr]",
        ] {
            assert!(super::is_rocm_offload_arch_diagnostic(line));
            let record = super::worker_stdout_diagnostic("env_info", "operation-a", line);
            assert_eq!(record.source, "third_party");
            assert!(record.level.is_none());
            assert_eq!(record.event, "process.stdout");
            assert_eq!(record.logger, "stdout");
            assert_eq!(record.message, line);
            assert_eq!(record.context["command"], "env_info");
            assert_eq!(record.context["operationId"], "operation-a");
            assert!(record.detail.is_none());
        }
    }

    #[test]
    fn progress_sampling_preserves_task_boundaries_and_reports_suppressed_updates() {
        let mut samples = std::collections::HashMap::new();
        let start = std::time::Instant::now();
        assert_eq!(
            super::sample_progress_log(&mut samples, "task-a".into(), start),
            Some(0)
        );
        for _ in 0..50 {
            assert_eq!(
                super::sample_progress_log(&mut samples, "task-a".into(), start),
                None
            );
        }
        assert_eq!(
            super::sample_progress_log(&mut samples, "task-b".into(), start),
            Some(0)
        );
        assert_eq!(
            super::sample_progress_log(
                &mut samples,
                "task-a".into(),
                start + std::time::Duration::from_secs(1)
            ),
            Some(50)
        );
        for index in 0..1000 {
            super::sample_progress_log(&mut samples, format!("task-{index}"), start);
        }
        assert!(samples.len() <= 256);
    }

    #[test]
    fn worker_events_keep_error_context_outside_message() {
        let envelope: super::WorkerEnvelope = serde_json::from_value(serde_json::json!({
            "type": "error", "taskId": "task-a", "requestId": "request-a",
            "payload": {"message": "Allocation failed", "detail": "Traceback\nMemoryError", "nodeId": "node-a", "jobId": "job-a", "code": "OOM"}
        })).unwrap();
        let record = super::record_worker_event("infer_workflow", &envelope, "ERROR");
        assert_eq!(record.message, "Allocation failed");
        assert_eq!(record.source, "worker");
        assert_eq!(record.level.as_deref(), Some("error"));
        assert_eq!(record.detail.as_deref(), Some("Traceback\nMemoryError"));
        assert_eq!(record.node_id.as_deref(), Some("node-a"));
        assert_eq!(record.context["code"], "OOM");
    }

    #[test]
    fn report_events_retain_workflow_lifecycle_and_tool_counts_without_result_contents() {
        let envelope: super::WorkerEnvelope = serde_json::from_value(serde_json::json!({
            "type": "workflow_execution", "taskId": "task-a",
            "payload": {"event": "node_started", "nodeId": "separate-1", "nodeType": "mss_separate",
                "nodeIndex": 2, "nodeCount": 5, "nodeProgress": 0.25, "overallFraction": 0.1}
        })).unwrap();
        let record = super::record_worker_event("infer_workflow", &envelope, "INFO");
        assert_eq!(record.context["event"], "node_started");
        assert_eq!(record.context["nodeCount"], 5);
        assert_eq!(record.context["nodeProgress"], 0.25);
        let tool: super::WorkerEnvelope = serde_json::from_value(serde_json::json!({
            "type": "audio_tool_result", "requestId": "tool-1",
            "payload": {"operation": "asr", "action": "transcribe", "succeeded": 1,
                "items": [{"text": "private transcript", "path": "private-input.wav"}],
                "failed": [{"path": "other-private-input.wav"}], "warnings": ["warning"]}
        })).unwrap();
        let record = super::record_worker_event("audio_tools", &tool, "WARN");
        assert_eq!(record.context["operation"], "asr");
        assert_eq!(record.context["resultCount"], 1);
        assert_eq!(record.context["failedCount"], 1);
        assert_eq!(record.context["warningCount"], 1);
        let json = serde_json::to_string(&record).unwrap();
        assert!(!json.contains("private transcript"));
        assert!(!json.contains("private-input.wav"));
    }

    #[test]
    fn untagged_worker_failures_bind_to_the_request_without_changing_business_envelopes() {
        let envelope: super::WorkerEnvelope = serde_json::from_value(serde_json::json!({
            "type": "error", "payload": {"code": "UNKNOWN_ERROR", "message": "Worker initialization failed"}
        })).unwrap();
        let mut record = super::record_worker_event("infer_workflow", &envelope, "ERROR");
        super::bind_operation_scope(&mut record, Some("operation-a"), Some("batch-parent"));
        assert_eq!(record.context["operationId"], "operation-a");
        assert_eq!(record.task_id.as_deref(), Some("batch-parent"));
        assert!(envelope.task_id.is_none());
        assert!(envelope.request_id.is_none());
    }

    #[test]
    fn operation_scoped_batch_logs_never_inherit_the_first_file_task() {
        let mut record = super::LogRecord::new("pymss", Some("info"), "pymss", "log", "Preparing shared model");
        record.context.insert("taskScope".into(), "operation".into());
        let mut envelope: super::WorkerEnvelope = serde_json::from_value(serde_json::json!({
            "type": "task_log", "taskId": "first-file", "payload": {}
        })).unwrap();
        super::bind_log_envelope_scope(&mut record, &envelope);
        super::bind_operation_scope(&mut record, Some("batch-operation"), Some("first-file"));
        assert!(record.task_id.is_none());
        assert_eq!(record.context["operationId"], "batch-operation");
        envelope.request_id = Some("batch-request".into());
        super::bind_log_envelope_scope(&mut record, &envelope);
        assert!(record.task_id.is_none());
        assert_eq!(record.request_id.as_deref(), Some("batch-request"));

        let mut file_record = super::LogRecord::new("pymss", Some("info"), "pymss", "log", "Separating file");
        super::bind_log_envelope_scope(&mut file_record, &envelope);
        super::bind_operation_scope(&mut file_record, Some("batch-operation"), Some("first-file"));
        assert_eq!(file_record.task_id.as_deref(), Some("first-file"));
        assert_eq!(file_record.context["operationId"], "batch-operation");
    }
}
