use crate::error::{AppError, AppResult};
use serde::Serialize;
use serde_json::Value;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::Duration;
use tauri::{AppHandle, Manager};

#[cfg(windows)]
use std::os::windows::ffi::OsStrExt;

#[cfg(windows)]
use windows_sys::Win32::Storage::FileSystem::ReplaceFileW;

const DATA_ROOT_ENV: &str = "PYMSS_STUDIO_DATA_ROOT";
const DATA_ROOT_DIR_NAME: &str = ".pymss-studio-plugin-test";
const LOCAL_DATA_ROOT_DIR_NAME: &str = "data";
static JSON_WRITE_SEQUENCE: AtomicU64 = AtomicU64::new(0);
// Multiple WebView windows can autosave the same store at once. Serialize the temporary-file
// replacement in the Rust process so a second writer never races the first ReplaceFileW call.
static JSON_WRITE_LOCK: Mutex<()> = Mutex::new(());
#[cfg(windows)]
const PORTABLE_MARKER_FILE_NAME: &str = "pymss-studio.portable";

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AppPathsPayload {
    pub data_root: String,
    pub settings_dir: String,
    pub models_dir: String,
    pub outputs_dir: String,
    pub editor_projects_dir: String,
    pub logs_dir: String,
    pub temp_dir: String,
}

pub fn home_dir(app: &AppHandle) -> AppResult<PathBuf> {
    app.path()
        .home_dir()
        .map_err(|error| AppError::Worker(error.to_string()))
}

fn legacy_data_root_dir(app: &AppHandle) -> AppResult<PathBuf> {
    Ok(home_dir(app)?.join(DATA_ROOT_DIR_NAME))
}

fn development_data_root_dir() -> AppResult<PathBuf> {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let project_root = manifest_dir
        .parent()
        .ok_or_else(|| AppError::Worker("failed to resolve project root".into()))?;
    Ok(project_root.join(LOCAL_DATA_ROOT_DIR_NAME))
}

fn portable_data_root_dir() -> AppResult<Option<PathBuf>> {
    #[cfg(windows)]
    {
        let exe = std::env::current_exe()?;
        let exe_dir = exe
            .parent()
            .ok_or_else(|| AppError::Worker("failed to resolve executable directory".into()))?;
        if exe_dir.join(PORTABLE_MARKER_FILE_NAME).exists() {
            return Ok(Some(exe_dir.join(LOCAL_DATA_ROOT_DIR_NAME)));
        }
    }
    Ok(None)
}

fn is_cargo_profile_executable(exe: &Path, target_dir: &Path) -> bool {
    let Some(profile_dir) = exe.parent() else {
        return false;
    };
    let profile = profile_dir.file_name().and_then(|value| value.to_str());
    if !matches!(profile, Some("debug" | "release")) {
        return false;
    }
    let Some(profile_parent) = profile_dir.parent() else {
        return false;
    };
    // Native builds live at target/{debug,release}; cross-compiled artifacts add exactly one
    // target-triple directory. Bundle contents are deeper and must use packaged semantics even
    // while CI is inspecting them in target/**/bundle.
    profile_parent == target_dir || profile_parent.parent() == Some(target_dir)
}

pub fn is_development_executable() -> bool {
    let target_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("target");
    std::env::current_exe()
        .ok()
        .and_then(|path| std::fs::canonicalize(path).ok())
        .zip(std::fs::canonicalize(target_dir).ok())
        .map(|(exe, target)| is_cargo_profile_executable(&exe, &target))
        .unwrap_or(false)
}

fn resolve_data_root(
    env_root: Option<PathBuf>,
    development_root: PathBuf,
    portable_root: Option<PathBuf>,
    legacy_root: PathBuf,
    is_development: bool,
) -> PathBuf {
    if let Some(root) = env_root {
        return root;
    }
    if is_development {
        return development_root;
    }
    if let Some(root) = portable_root {
        return root;
    }
    legacy_root
}

pub fn data_root_dir(app: &AppHandle) -> AppResult<PathBuf> {
    let env_root = std::env::var_os(DATA_ROOT_ENV)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from);
    Ok(resolve_data_root(
        env_root,
        development_data_root_dir()?,
        portable_data_root_dir()?,
        legacy_data_root_dir(app)?,
        is_development_executable(),
    ))
}

pub fn settings_dir(app: &AppHandle) -> AppResult<PathBuf> {
    Ok(data_root_dir(app)?.join("settings"))
}

pub fn models_dir(app: &AppHandle) -> AppResult<PathBuf> {
    Ok(data_root_dir(app)?.join("models"))
}

pub fn outputs_dir(app: &AppHandle) -> AppResult<PathBuf> {
    Ok(data_root_dir(app)?.join("outputs"))
}

pub fn editor_projects_dir(app: &AppHandle) -> AppResult<PathBuf> {
    Ok(data_root_dir(app)?.join("editor-projects"))
}

pub fn logs_dir(app: &AppHandle) -> AppResult<PathBuf> {
    Ok(data_root_dir(app)?.join("logs"))
}

pub fn temp_dir(app: &AppHandle) -> AppResult<PathBuf> {
    Ok(data_root_dir(app)?.join("temp"))
}

// Executable siblings and resource-specific fallbacks stay with their callers.
pub(crate) fn resource_roots(resource: &Path) -> [PathBuf; 3] {
    [
        resource.to_path_buf(),
        resource.join("_up_"),
        resource.join("resources"),
    ]
}

pub fn runtime_root_dir(app: &AppHandle) -> AppResult<PathBuf> {
    let resource = app.path().resource_dir().ok();
    let exe_dir = std::env::current_exe()?
        .parent()
        .map(PathBuf::from)
        .ok_or_else(|| AppError::Worker("failed to resolve executable directory".into()))?;
    Ok(runtime_root_from(resource.as_deref(), &exe_dir))
}

fn runtime_root_from(resource: Option<&Path>, exe_dir: &Path) -> PathBuf {
    let mut candidates: Vec<_> = resource
        .into_iter()
        .flat_map(resource_roots)
        .map(|root| root.join("python-runtime"))
        .collect();
    candidates.push(exe_dir.join("python-runtime"));
    candidates
        .into_iter()
        .find(|path| path.is_dir())
        .unwrap_or_else(|| exe_dir.join("python-runtime"))
}

pub fn bundled_runtime_envs_dir(app: &AppHandle) -> AppResult<Option<PathBuf>> {
    let resource = app.path().resource_dir().ok();
    let exe_dir = std::env::current_exe()?
        .parent()
        .map(PathBuf::from)
        .ok_or_else(|| AppError::Worker("failed to resolve executable directory".into()))?;
    Ok(bundled_runtime_envs_from(resource.as_deref(), &exe_dir))
}

fn bundled_runtime_envs_from(resource: Option<&Path>, exe_dir: &Path) -> Option<PathBuf> {
    let mut candidates: Vec<_> = resource
        .into_iter()
        .flat_map(resource_roots)
        .map(|root| root.join("python-runtime").join("runtime-envs"))
        .collect();
    candidates.push(exe_dir.join("python-runtime").join("runtime-envs"));
    candidates
        .into_iter()
        .find(|path| path.is_dir())
}

pub fn runtime_envs_dir(app: &AppHandle) -> AppResult<PathBuf> {
    #[cfg(any(feature = "vst-lab", target_os = "macos", target_os = "linux"))]
    {
        Ok(data_root_dir(app)?.join("runtime-envs"))
    }
    #[cfg(not(any(feature = "vst-lab", target_os = "macos", target_os = "linux")))]
    {
        Ok(runtime_root_dir(app)?.join("runtime-envs"))
    }
}

pub fn active_runtime_file(app: &AppHandle) -> AppResult<PathBuf> {
    Ok(runtime_envs_dir(app)?.join("active-runtime.json"))
}

/// pymss's registry of imported ("custom") models.
///
/// Kept under `settings/` rather than with the models themselves: the registry stores absolute
/// paths to files that usually live outside the model directory, so it is app state, not model
/// data, and must not be caught up in a model-directory migration.
pub fn user_models_file(app: &AppHandle) -> AppResult<PathBuf> {
    Ok(settings_dir(app)?.join("user_models.json"))
}

pub fn ensure_app_directories(app: &AppHandle) -> AppResult<()> {
    for dir in [
        data_root_dir(app)?,
        settings_dir(app)?,
        models_dir(app)?,
        outputs_dir(app)?,
        editor_projects_dir(app)?,
        logs_dir(app)?,
        temp_dir(app)?,
    ] {
        std::fs::create_dir_all(dir)?;
    }
    Ok(())
}

pub fn app_paths_payload(app: &AppHandle) -> AppResult<AppPathsPayload> {
    ensure_app_directories(app)?;
    Ok(AppPathsPayload {
        data_root: data_root_dir(app)?.to_string_lossy().to_string(),
        settings_dir: settings_dir(app)?.to_string_lossy().to_string(),
        models_dir: models_dir(app)?.to_string_lossy().to_string(),
        outputs_dir: outputs_dir(app)?.to_string_lossy().to_string(),
        editor_projects_dir: editor_projects_dir(app)?.to_string_lossy().to_string(),
        logs_dir: logs_dir(app)?.to_string_lossy().to_string(),
        temp_dir: temp_dir(app)?.to_string_lossy().to_string(),
    })
}

fn store_file_name(name: &str) -> AppResult<&'static str> {
    match name {
        "app-settings" => Ok("app.json"),
        "task-history" => Ok("tasks.json"),
        "model-state" => Ok("model-cache.json"),
        "editor-ui" => Ok("editor-ui.json"),
        "audio-tools" => Ok("audio-tools.json"),
        "plugin-tools" => Ok("plugin-tools.json"),
        "plugin-source" => Ok("plugin-source.json"),
        "workflow-state" => Ok("workflows.json"),
        "separate-state" => Ok("separate.json"),
        "update-state" => Ok("update.json"),
        _ => Err(AppError::Worker(format!("unknown app store: {name}"))),
    }
}

pub fn app_store_path(app: &AppHandle, name: &str) -> AppResult<PathBuf> {
    Ok(settings_dir(app)?.join(store_file_name(name)?))
}

pub fn read_app_store(app: &AppHandle, name: &str) -> AppResult<Value> {
    ensure_app_directories(app)?;
    let path = app_store_path(app, name)?;
    if !path.is_file() {
        return Ok(Value::Null);
    }
    let content = std::fs::read_to_string(path)?;
    Ok(serde_json::from_str(&content)?)
}

pub fn write_app_store(app: &AppHandle, name: &str, data: &Value) -> AppResult<()> {
    ensure_app_directories(app)?;
    let path = app_store_path(app, name)?;
    write_json_file(&path, data)
}

fn plugin_tool_key(plugin_name: &str, view_id: &str) -> AppResult<String> {
    let valid = |value: &str, limit: usize| {
        !value.is_empty() && value.len() <= limit
            && value.bytes().all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
    };
    if !valid(plugin_name, 100) || !valid(view_id, 200) {
        return Err(AppError::Worker("Invalid plugin tool settings namespace".into()));
    }
    Ok(format!("{plugin_name}:{view_id}"))
}

pub fn load_plugin_tool_settings(app: &AppHandle, plugin_name: &str, view_id: &str) -> AppResult<Value> {
    let key = plugin_tool_key(plugin_name, view_id)?;
    let state = read_app_store(app, "plugin-tools")?;
    Ok(state.get(&key).cloned().unwrap_or_else(|| serde_json::json!({})))
}

pub fn save_plugin_tool_settings(app: &AppHandle, plugin_name: &str, view_id: &str, data: &Value) -> AppResult<()> {
    ensure_app_directories(app)?;
    save_plugin_tool_settings_file(&app_store_path(app, "plugin-tools")?, plugin_name, view_id, data)
}

fn save_plugin_tool_settings_file(path: &Path, plugin_name: &str, view_id: &str, data: &Value) -> AppResult<()> {
    let key = plugin_tool_key(plugin_name, view_id)?;
    if !data.is_object() || serde_json::to_vec(data)?.len() > 2 * 1024 * 1024 {
        return Err(AppError::Worker("Plugin tool settings must be an object of at most 2 MiB".into()));
    }
    // Keep the read/merge/write under the same lock used for atomic JSON publication.
    let _guard = JSON_WRITE_LOCK.lock().unwrap_or_else(|error| error.into_inner());
    let mut state = if path.is_file() {
        serde_json::from_str::<Value>(&std::fs::read_to_string(path)?)?
    } else {
        serde_json::json!({})
    };
    let object = state.as_object_mut().ok_or_else(|| AppError::Worker("Invalid plugin tool settings store".into()))?;
    object.insert(key, data.clone());
    write_json_file_locked(path, &state)
}

pub(crate) fn write_json_file(path: &Path, data: &Value) -> AppResult<()> {
    let _write_guard = JSON_WRITE_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    write_json_file_locked(path, data)
}

pub(crate) fn write_editor_project_file(path: &Path, data: &Value) -> AppResult<()> {
    let _write_guard = JSON_WRITE_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    backup_pre_plugin_project(path, data).map_err(|error| {
        AppError::Worker(format!("Could not preserve the original editor project at {}: {error}", path.display()))
    })?;
    write_json_file_locked(path, data)
}

fn backup_pre_plugin_project(path: &Path, data: &Value) -> AppResult<()> {
    let backup = path.with_file_name("project.pre-plugins.json");
    match std::fs::symlink_metadata(&backup) {
        Ok(metadata) if metadata.is_file() => return Ok(()),
        Ok(_) => return Err(AppError::Worker(format!("The project backup is not a regular file: {}", backup.display()))),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    let content = match std::fs::read(path) {
        Ok(content) => content,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error.into()),
    };
    let previous: Value = serde_json::from_slice(&content)?;
    let has_legacy_effects = previous.get("tracks").and_then(Value::as_array)
        .is_some_and(|tracks| tracks.iter().any(|track| track.get("effects").is_some_and(Value::is_object)));
    if previous.get("version").and_then(Value::as_u64).unwrap_or(0) >= 4
        || (!has_legacy_effects && data.get("version").and_then(Value::as_u64).unwrap_or(0) < 4)
    {
        return Ok(());
    }

    // Publish a fully synced copy before replacing the project. A second writer
    // cannot replace the first upgrade snapshot, including across processes.
    let mut file = tempfile::NamedTempFile::new_in(path.parent().unwrap_or_else(|| Path::new(".")))?;
    file.write_all(&content)?;
    file.as_file().sync_all()?;
    match file.persist_noclobber(&backup) {
        Ok(_) => Ok(()),
        Err(error) if error.error.kind() == std::io::ErrorKind::AlreadyExists => {
            if std::fs::symlink_metadata(&backup)?.is_file() {
                Ok(())
            } else {
                Err(error.error.into())
            }
        }
        Err(error) => Err(error.error.into()),
    }
}

fn write_json_file_locked(path: &Path, data: &Value) -> AppResult<()> {
    write_json_file_with_replace(path, data, replace_file)
}

fn write_json_file_with_replace(
    path: &Path,
    data: &Value,
    replace: impl FnOnce(&Path, &Path) -> AppResult<()>,
) -> AppResult<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let content = serde_json::to_vec_pretty(data)?;
    let sequence = JSON_WRITE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let file_name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("store.json");
    let temporary = path.with_file_name(format!(
        ".{file_name}.{}.{}.tmp",
        std::process::id(),
        sequence,
    ));
    let result = (|| -> AppResult<()> {
        let mut file = std::fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temporary)?;
        file.write_all(&content)?;
        file.sync_all()?;
        drop(file);
        replace(&temporary, path)
    })();
    #[cfg(windows)]
    if let Err(AppError::Io(error)) = &result {
        // ReplaceFileW can remove/rename the old file before reporting these errors.
        // The complete, synced replacement is then the only copy of the latest save.
        if matches!(error.raw_os_error(), Some(1176 | 1177)) {
            return Err(AppError::Worker(format!(
                "Failed to save {}: {error}. Recovery file retained at {}",
                path.display(),
                temporary.display(),
            )));
        }
    }
    if result.is_err() {
        let _ = std::fs::remove_file(&temporary);
    }
    result
}

pub fn mutate_workflow_store(app: &AppHandle, mutation: &Value) -> AppResult<Value> {
    ensure_app_directories(app)?;
    let path = app_store_path(app, "workflow-state")?;
    let _write_guard = JSON_WRITE_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());

    let state = if path.is_file() {
        let content = std::fs::read_to_string(&path)?;
        serde_json::from_str::<Value>(&content)?
    } else {
        serde_json::json!({ "workflows": [], "selectedWorkflowId": "" })
    };
    let response = apply_workflow_mutation(state, mutation)?;
    if response.get("conflict").is_none() {
        let next_state = response
            .get("state")
            .ok_or_else(|| AppError::Worker("workflow mutation returned no state".into()))?;
        write_json_file_locked(&path, next_state)?;
    }
    Ok(response)
}

fn missing_workflow_id(item: &Value) -> bool {
    item.is_object()
        && item
            .get("id")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .is_none()
}

fn legacy_workflow_index(workflow_list: &[Value], mutation: &Value) -> Option<usize> {
    let legacy_index = mutation
        .get("legacyIndex")
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok());
    let legacy_entry = mutation.get("legacyEntry");
    let is_match = |item: &Value| {
        missing_workflow_id(item)
            && legacy_entry
                .map(|expected| expected == item)
                .unwrap_or(true)
    };
    legacy_index
        .filter(|index| workflow_list.get(*index).is_some_and(&is_match))
        .or_else(|| legacy_entry.and_then(|_| workflow_list.iter().position(is_match)))
}

fn apply_workflow_mutation(mut state: Value, mutation: &Value) -> AppResult<Value> {
    if !state.is_object() {
        state = serde_json::json!({ "workflows": [], "selectedWorkflowId": "" });
    }
    let state_object = state
        .as_object_mut()
        .ok_or_else(|| AppError::Worker("workflow store must be an object".into()))?;
    let selected_before = state_object
        .get("selectedWorkflowId")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let workflows = state_object
        .entry("workflows")
        .or_insert_with(|| Value::Array(Vec::new()));
    if !workflows.is_array() {
        *workflows = Value::Array(Vec::new());
    }
    let workflow_list = workflows
        .as_array_mut()
        .ok_or_else(|| AppError::Worker("workflow list must be an array".into()))?;
    let action = mutation.get("action").and_then(Value::as_str).unwrap_or("");
    let mut conflict = None;

    match action {
        "upsert" => {
            let mut entry = mutation
                .get("entry")
                .cloned()
                .filter(Value::is_object)
                .ok_or_else(|| AppError::Worker("workflow entry is required".into()))?;
            let workflow_id = entry
                .get("id")
                .and_then(Value::as_str)
                .filter(|value| !value.trim().is_empty())
                .ok_or_else(|| AppError::Worker("workflow id is required".into()))?
                .to_string();
            let mut existing_index = workflow_list.iter().position(|item| {
                item.get("id").and_then(Value::as_str) == Some(workflow_id.as_str())
            });
            let has_legacy_reference =
                mutation.get("legacyIndex").is_some() || mutation.get("legacyEntry").is_some();
            let mut legacy_target_mismatch = false;
            if existing_index.is_none() {
                existing_index = legacy_workflow_index(workflow_list, mutation);
                if existing_index.is_none() && has_legacy_reference {
                    legacy_target_mismatch = true;
                }
            }
            let actual_updated_at = existing_index
                .and_then(|index| workflow_list[index].get("updatedAt"))
                .and_then(Value::as_u64)
                .unwrap_or(0);
            let expected_updated_at = mutation.get("expectedUpdatedAt").and_then(Value::as_u64);
            let force = mutation
                .get("force")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            if !force
                && (legacy_target_mismatch
                    || (expected_updated_at.is_some()
                        && expected_updated_at.unwrap_or(0) != actual_updated_at))
            {
                conflict = Some(serde_json::json!({
                    "workflowId": workflow_id,
                    "expectedUpdatedAt": expected_updated_at.unwrap_or(0),
                    "actualUpdatedAt": actual_updated_at,
                }));
            } else {
                if let Some(index) = existing_index {
                    let created_at = workflow_list[index]
                        .get("createdAt")
                        .and_then(Value::as_u64)
                        .unwrap_or(0);
                    if created_at > 0 {
                        entry["createdAt"] = Value::from(created_at);
                    }
                }
                let requested_updated_at =
                    entry.get("updatedAt").and_then(Value::as_u64).unwrap_or(0);
                entry["updatedAt"] =
                    Value::from(requested_updated_at.max(actual_updated_at.saturating_add(1)));
                if let Some(index) = existing_index {
                    workflow_list[index] = entry;
                } else {
                    workflow_list.push(entry);
                }
                workflow_list.sort_by(|left, right| {
                    let left_value = left.get("updatedAt").and_then(Value::as_u64).unwrap_or(0);
                    let right_value = right.get("updatedAt").and_then(Value::as_u64).unwrap_or(0);
                    right_value.cmp(&left_value)
                });
                state_object.insert("selectedWorkflowId".into(), Value::String(workflow_id));
            }
        }
        "delete" => {
            let workflow_id = mutation
                .get("workflowId")
                .and_then(Value::as_str)
                .unwrap_or("");
            let index = workflow_list
                .iter()
                .position(|item| item.get("id").and_then(Value::as_str) == Some(workflow_id))
                .or_else(|| legacy_workflow_index(workflow_list, mutation));
            let actual_updated_at = index
                .and_then(|index| workflow_list[index].get("updatedAt"))
                .and_then(Value::as_u64)
                .unwrap_or(0);
            let expected_updated_at = mutation.get("expectedUpdatedAt").and_then(Value::as_u64);
            let has_legacy_reference =
                mutation.get("legacyIndex").is_some() || mutation.get("legacyEntry").is_some();
            if (index.is_none() && has_legacy_reference)
                || (expected_updated_at.is_some()
                    && expected_updated_at.unwrap_or(0) != actual_updated_at)
            {
                conflict = Some(serde_json::json!({
                    "workflowId": workflow_id,
                    "expectedUpdatedAt": expected_updated_at.unwrap_or(0),
                    "actualUpdatedAt": actual_updated_at,
                }));
            } else {
                let removed_selected = index
                    .map(|index| workflow_list.remove(index))
                    .and_then(|item| {
                        item.get("id")
                            .and_then(Value::as_str)
                            .map(str::to_string)
                    })
                    .is_some_and(|removed_id| removed_id == selected_before);
                if selected_before == workflow_id || removed_selected {
                    let replacement = workflow_list
                        .first()
                        .and_then(|item| item.get("id"))
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string();
                    state_object.insert("selectedWorkflowId".into(), Value::String(replacement));
                }
            }
        }
        "select" => {
            let workflow_id = mutation
                .get("workflowId")
                .and_then(Value::as_str)
                .unwrap_or("");
            let existing_index = workflow_list
                .iter()
                .position(|item| item.get("id").and_then(Value::as_str) == Some(workflow_id));
            let legacy_index = existing_index
                .is_none()
                .then(|| legacy_workflow_index(workflow_list, mutation))
                .flatten();
            let has_legacy_reference =
                mutation.get("legacyIndex").is_some() || mutation.get("legacyEntry").is_some();
            if !workflow_id.is_empty()
                && existing_index.is_none()
                && legacy_index.is_none()
                && has_legacy_reference
            {
                conflict = Some(serde_json::json!({
                    "workflowId": workflow_id,
                    "expectedUpdatedAt": 0,
                    "actualUpdatedAt": 0,
                }));
            } else {
                if let Some(index) = legacy_index {
                    workflow_list[index]["id"] = Value::String(workflow_id.to_string());
                }
                if workflow_id.is_empty() || existing_index.is_some() || legacy_index.is_some() {
                    state_object.insert(
                        "selectedWorkflowId".into(),
                        Value::String(workflow_id.to_string()),
                    );
                }
            }
        }
        _ => {
            return Err(AppError::Worker(format!(
                "unknown workflow mutation: {action}"
            )))
        }
    }

    if let Some(conflict) = conflict {
        return Ok(serde_json::json!({ "state": state, "conflict": conflict }));
    }
    Ok(serde_json::json!({ "state": state }))
}

#[cfg(windows)]
fn replace_file(temporary: &Path, destination: &Path) -> AppResult<()> {
    if !destination.exists() {
        std::fs::rename(temporary, destination)?;
        return Ok(());
    }
    let destination = destination
        .as_os_str()
        .encode_wide()
        .chain(Some(0))
        .collect::<Vec<_>>();
    let temporary = temporary
        .as_os_str()
        .encode_wide()
        .chain(Some(0))
        .collect::<Vec<_>>();
    retry_file_replacement(|| {
        let replaced = unsafe {
            ReplaceFileW(
                destination.as_ptr(),
                temporary.as_ptr(),
                std::ptr::null(),
                0,
                std::ptr::null(),
                std::ptr::null(),
            )
        };
        if replaced != 0 {
            Ok(())
        } else {
            Err(std::io::Error::last_os_error())
        }
    })?;
    Ok(())
}

#[cfg(windows)]
fn retry_file_replacement(mut replace: impl FnMut() -> std::io::Result<()>) -> std::io::Result<()> {
    for attempt in 0..6 {
        match replace() {
            Ok(()) => return Ok(()),
            Err(error) => {
                // 1175 (ERROR_UNABLE_TO_REMOVE_REPLACED) leaves both names intact, so it
                // is safe to retry just like sharing violations. Never retry 1176/1177:
                // those indicate a partially completed replacement.
                let retryable = matches!(error.raw_os_error(), Some(5 | 32 | 33 | 1175));
                if !retryable || attempt == 5 {
                    return Err(error);
                }
            }
        }
        std::thread::sleep(Duration::from_millis(40 * (attempt + 1) as u64));
    }
    unreachable!("ReplaceFileW retry loop must return")
}

#[cfg(not(windows))]
fn replace_file(temporary: &Path, destination: &Path) -> AppResult<()> {
    std::fs::rename(temporary, destination)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        apply_workflow_mutation, resolve_data_root, store_file_name, write_json_file,
        JSON_WRITE_SEQUENCE,
    };
    use serde_json::json;
    use std::path::PathBuf;
    use std::sync::atomic::Ordering;

    fn path(name: &str) -> PathBuf {
        PathBuf::from(name)
    }

    #[test]
    fn plugin_tool_settings_merge_concurrent_views_without_lost_updates() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("plugin-tools.json");
        let barrier = std::sync::Arc::new(std::sync::Barrier::new(8));
        let threads = (0..8).map(|index| {
            let path = path.clone();
            let barrier = barrier.clone();
            std::thread::spawn(move || {
                barrier.wait();
                super::save_plugin_tool_settings_file(&path, "convert", &format!("view-{index}"), &json!({"rate": index})).unwrap();
            })
        }).collect::<Vec<_>>();
        for thread in threads { thread.join().unwrap(); }
        let state: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(state.as_object().unwrap().len(), 8);
        for index in 0..8 { assert_eq!(state[format!("convert:view-{index}")]["rate"], index); }
    }

    #[test]
    fn plugin_tool_settings_reject_invalid_names_and_preserve_corrupt_store() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("plugin-tools.json");
        assert!(super::save_plugin_tool_settings_file(&path, "../outside", "tool", &json!({})).is_err());
        assert!(super::save_plugin_tool_settings_file(&path, "convert", "tool", &json!([])).is_err());
        assert!(!path.exists());
        std::fs::write(&path, "invalid JSON").unwrap();
        assert!(super::save_plugin_tool_settings_file(&path, "convert", "tool", &json!({})).is_err());
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "invalid JSON");
    }

    #[test]
    fn workflow_mutations_merge_different_editor_saves_into_the_latest_state() {
        let initial = json!({
            "workflows": [
                { "id": "first", "name": "First", "createdAt": 1, "updatedAt": 10 },
                { "id": "second", "name": "Second", "createdAt": 2, "updatedAt": 10 },
            ],
            "selectedWorkflowId": "first",
        });
        let first = apply_workflow_mutation(
            initial,
            &json!({
                "action": "upsert",
                "entry": { "id": "first", "name": "First changed", "createdAt": 1, "updatedAt": 20 },
                "expectedUpdatedAt": 10,
            }),
        )
        .unwrap();
        let second = apply_workflow_mutation(
            first["state"].clone(),
            &json!({
                "action": "upsert",
                "entry": { "id": "second", "name": "Second changed", "createdAt": 2, "updatedAt": 21 },
                "expectedUpdatedAt": 10,
            }),
        )
        .unwrap();
        let workflows = second["state"]["workflows"].as_array().unwrap();
        assert_eq!(workflows.len(), 2);
        assert!(workflows
            .iter()
            .any(|item| item["id"] == "first" && item["name"] == "First changed"));
        assert!(workflows
            .iter()
            .any(|item| item["id"] == "second" && item["name"] == "Second changed"));
    }

    #[test]
    fn workflow_mutation_reports_a_real_persisted_revision_conflict() {
        let state = json!({
            "workflows": [{ "id": "first", "name": "Remote", "createdAt": 1, "updatedAt": 20 }],
            "selectedWorkflowId": "first",
        });
        let response = apply_workflow_mutation(
            state.clone(),
            &json!({
                "action": "upsert",
                "entry": { "id": "first", "name": "Stale", "createdAt": 1, "updatedAt": 21 },
                "expectedUpdatedAt": 10,
            }),
        )
        .unwrap();
        assert_eq!(response["conflict"]["actualUpdatedAt"], 20);
        assert_eq!(response["state"], state);
    }

    #[test]
    fn workflow_mutation_can_clear_the_selected_workflow() {
        let state = json!({
            "workflows": [{ "id": "first", "name": "First", "createdAt": 1, "updatedAt": 10 }],
            "selectedWorkflowId": "first",
        });
        let response = apply_workflow_mutation(
            state,
            &json!({ "action": "select", "workflowId": "" }),
        )
        .unwrap();
        assert_eq!(response["state"]["selectedWorkflowId"], "");
        assert_eq!(response["state"]["workflows"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn workflow_mutation_migrates_a_legacy_entry_without_creating_a_duplicate() {
        let state = json!({
            "workflows": [{ "name": "Legacy", "definition": {}, "createdAt": 1 }],
            "selectedWorkflowId": "",
        });
        let response = apply_workflow_mutation(
            state,
            &json!({
                "action": "upsert",
                "entry": {
                    "id": "workflow_generated",
                    "name": "Migrated",
                    "definition": {},
                    "createdAt": 2,
                    "updatedAt": 10,
                },
                "expectedUpdatedAt": 0,
                "legacyIndex": 0,
            }),
        )
        .unwrap();
        let workflows = response["state"]["workflows"].as_array().unwrap();
        assert_eq!(workflows.len(), 1);
        assert_eq!(workflows[0]["id"], "workflow_generated");
        assert_eq!(workflows[0]["name"], "Migrated");
    }

    #[test]
    fn stale_legacy_index_conflicts_instead_of_creating_a_duplicate() {
        let state = json!({
            "workflows": [{
                "id": "workflow_saved_elsewhere",
                "name": "Remote",
                "definition": {},
                "createdAt": 1,
                "updatedAt": 20,
            }],
            "selectedWorkflowId": "workflow_saved_elsewhere",
        });
        let response = apply_workflow_mutation(
            state.clone(),
            &json!({
                "action": "upsert",
                "entry": {
                    "id": "workflow_generated_locally",
                    "name": "Local",
                    "definition": {},
                    "createdAt": 2,
                    "updatedAt": 10,
                },
                "expectedUpdatedAt": 0,
                "legacyIndex": 0,
            }),
        )
        .unwrap();
        assert!(response.get("conflict").is_some());
        assert_eq!(response["state"], state);
    }

    #[test]
    fn legacy_entry_identity_survives_reordering_before_migration() {
        let target = json!({ "name": "Target legacy", "definition": {}, "updatedAt": 5 });
        let state = json!({
            "workflows": [
                { "name": "Other legacy", "definition": {}, "updatedAt": 7 },
                target.clone(),
            ],
            "selectedWorkflowId": "",
        });
        let response = apply_workflow_mutation(
            state,
            &json!({
                "action": "upsert",
                "entry": {
                    "id": "workflow_target",
                    "name": "Migrated target",
                    "definition": {},
                    "createdAt": 2,
                    "updatedAt": 10,
                },
                "expectedUpdatedAt": 5,
                "legacyIndex": 0,
                "legacyEntry": target,
            }),
        )
        .unwrap();
        let workflows = response["state"]["workflows"].as_array().unwrap();
        assert_eq!(workflows.len(), 2);
        assert!(workflows
            .iter()
            .any(|item| item["id"] == "workflow_target" && item["name"] == "Migrated target"));
        assert!(workflows
            .iter()
            .any(|item| item["name"] == "Other legacy" && item.get("id").is_none()));
    }

    #[test]
    fn legacy_entries_support_select_and_delete_mutations() {
        let first = json!({ "name": "First legacy", "definition": {} });
        let second = json!({ "name": "Second legacy", "definition": {} });
        let state = json!({
            "workflows": [first.clone(), second.clone()],
            "selectedWorkflowId": "",
        });
        let selected = apply_workflow_mutation(
            state,
            &json!({
                "action": "select",
                "workflowId": "workflow_second",
                "legacyIndex": 1,
                "legacyEntry": second,
            }),
        )
        .unwrap();
        assert_eq!(selected["state"]["selectedWorkflowId"], "workflow_second");
        assert_eq!(selected["state"]["workflows"][1]["id"], "workflow_second");

        let deleted = apply_workflow_mutation(
            selected["state"].clone(),
            &json!({
                "action": "delete",
                "workflowId": "workflow_first",
                "legacyIndex": 0,
                "legacyEntry": first,
            }),
        )
        .unwrap();
        let workflows = deleted["state"]["workflows"].as_array().unwrap();
        assert_eq!(workflows.len(), 1);
        assert_eq!(workflows[0]["id"], "workflow_second");
    }

    #[test]
    fn stale_legacy_delete_and_select_report_conflicts() {
        let legacy = json!({ "name": "Legacy", "definition": {} });
        let migrated = json!({
            "id": "workflow_remote",
            "name": "Legacy",
            "definition": {},
            "updatedAt": 20,
        });
        let state = json!({
            "workflows": [migrated],
            "selectedWorkflowId": "workflow_remote",
        });
        for action in ["delete", "select"] {
            let response = apply_workflow_mutation(
                state.clone(),
                &json!({
                    "action": action,
                    "workflowId": "workflow_stale",
                    "legacyIndex": 0,
                    "legacyEntry": legacy,
                }),
            )
            .unwrap();
            assert!(response.get("conflict").is_some(), "{action}");
            assert_eq!(response["state"], state, "{action}");
        }
    }

    #[test]
    fn stale_delete_does_not_remove_a_newer_workflow_revision() {
        let state = json!({
            "workflows": [{
                "id": "workflow",
                "name": "Updated elsewhere",
                "definition": {},
                "createdAt": 1,
                "updatedAt": 20,
            }],
            "selectedWorkflowId": "workflow",
        });
        let response = apply_workflow_mutation(
            state.clone(),
            &json!({
                "action": "delete",
                "workflowId": "workflow",
                "expectedUpdatedAt": 10,
            }),
        )
        .unwrap();
        assert_eq!(response["conflict"]["actualUpdatedAt"], 20);
        assert_eq!(response["state"], state);
    }

    struct ResourceFixture(PathBuf);

    impl ResourceFixture {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!(
                "pymss-resource-layout-{}-{}",
                std::process::id(),
                JSON_WRITE_SEQUENCE.fetch_add(1, Ordering::Relaxed),
            ));
            std::fs::create_dir(&root).unwrap();
            Self(root)
        }
    }

    impl Drop for ResourceFixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn runtime_layouts_keep_resource_up_resources_and_exe_priority() {
        let fixture = ResourceFixture::new();
        let resource = fixture.0.join("Pymss.app/Contents/Resources");
        let exe = fixture.0.join("Pymss.app/Contents/MacOS");
        let candidates = [
            resource.join("python-runtime"),
            resource.join("_up_/python-runtime"),
            resource.join("resources/python-runtime"),
            exe.join("python-runtime"),
        ];
        for candidate in candidates.iter().rev() {
            std::fs::create_dir_all(candidate).unwrap();
            assert_eq!(super::runtime_root_from(Some(&resource), &exe), *candidate);
            let envs = candidate.join("runtime-envs");
            std::fs::create_dir_all(&envs).unwrap();
            assert_eq!(super::bundled_runtime_envs_from(Some(&resource), &exe), Some(envs));
        }
    }

    #[test]
    fn bundled_env_discovery_does_not_assume_the_first_runtime_root_contains_envs() {
        let fixture = ResourceFixture::new();
        let resource = fixture.0.join("resources");
        let exe = fixture.0.join("portable");
        std::fs::create_dir_all(resource.join("python-runtime")).unwrap();
        let envs = exe.join("python-runtime/runtime-envs");
        std::fs::create_dir_all(&envs).unwrap();
        assert_eq!(super::runtime_root_from(Some(&resource), &exe), resource.join("python-runtime"));
        assert_eq!(super::bundled_runtime_envs_from(Some(&resource), &exe), Some(envs));
    }

    #[test]
    fn missing_runtime_root_has_a_fixed_fallback_but_bundled_envs_are_optional() {
        let fixture = ResourceFixture::new();
        let resource = fixture.0.join("resources");
        let exe = fixture.0.join("installer");
        for resource in [Some(resource.as_path()), None] {
            assert_eq!(super::runtime_root_from(resource, &exe), exe.join("python-runtime"));
            assert_eq!(super::bundled_runtime_envs_from(resource, &exe), None);
        }
        assert!(!exe.exists());
    }

    #[test]
    fn runtime_directory_selection_ignores_files_and_accepts_missing_resource_api() {
        let fixture = ResourceFixture::new();
        let resource = fixture.0.join("resources");
        let exe = fixture.0.join("portable");
        std::fs::create_dir_all(&resource).unwrap();
        std::fs::write(resource.join("python-runtime"), b"not a directory").unwrap();
        let envs = exe.join("python-runtime/runtime-envs");
        std::fs::create_dir_all(&envs).unwrap();
        for resource in [Some(resource.as_path()), None, Some(exe.as_path())] {
            assert_eq!(super::runtime_root_from(resource, &exe), exe.join("python-runtime"));
            assert_eq!(super::bundled_runtime_envs_from(resource, &exe), Some(envs.clone()));
        }
    }

    #[test]
    fn development_detection_does_not_treat_packaged_bundle_paths_as_a_checkout() {
        let target = path("workspace/src-tauri/target");
        for relative in ["debug/pymss.exe", "release/pymss.exe", "aarch64-apple-darwin/release/pymss"] {
            assert!(super::is_cargo_profile_executable(&target.join(relative), &target));
        }
        for relative in [
            "release/bundle/Pymss.app/Contents/MacOS/pymss",
            "release/bundle/portable/pymss.exe",
            "debug/deps/pymss.exe",
            "unrelated/pymss.exe",
        ] {
            assert!(!super::is_cargo_profile_executable(&target.join(relative), &target));
        }
    }

    #[test]
    fn separate_state_has_a_store_file() {
        assert_eq!(store_file_name("separate-state").unwrap(), "separate.json");
    }

    #[test]
    fn audio_tools_has_a_store_file() {
        assert_eq!(store_file_name("audio-tools").unwrap(), "audio-tools.json");
    }

    #[test]
    fn update_state_has_a_store_file() {
        assert_eq!(store_file_name("update-state").unwrap(), "update.json");
    }

    #[test]
    fn json_writes_replace_existing_content_without_leaving_temporary_files() {
        let root = std::env::temp_dir().join(format!(
            "pymss-storage-test-{}-{}",
            std::process::id(),
            JSON_WRITE_SEQUENCE.fetch_add(1, Ordering::Relaxed),
        ));
        let path = root.join("settings.json");
        write_json_file(&path, &json!({ "value": 1 })).unwrap();
        write_json_file(&path, &json!({ "value": 2 })).unwrap();

        let stored: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(stored, json!({ "value": 2 }));
        assert_eq!(std::fs::read_dir(&root).unwrap().count(), 1);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn editor_upgrade_backup_preserves_original_bytes_and_is_never_overwritten() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("project.json");
        let backup = root.path().join("project.pre-plugins.json");
        let original = b"{\n  \"id\": \"old\", \"version\": 3, \"tracks\": [{\"effects\": {\"delay\": 0.3}}], \"future\": [1, 2]\n}\n";
        std::fs::write(&path, original).unwrap();
        let mut next: serde_json::Value = serde_json::from_slice(original).unwrap();
        // Autosave/enrichment can run before the user adds their first plugin.
        next["name"] = json!("Renamed");
        super::write_editor_project_file(&path, &next).unwrap();
        assert_eq!(std::fs::read(&backup).unwrap(), original);
        next["version"] = json!(4);
        next["pluginChain"] = json!([{ "id": "slot", "state": "sha256:retained" }]);
        super::write_editor_project_file(&path, &next).unwrap();
        super::write_editor_project_file(&path, &next).unwrap();
        assert_eq!(std::fs::read(&backup).unwrap(), original);
        let saved: serde_json::Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(saved, next);
        assert_eq!(std::fs::read_dir(root.path()).unwrap().count(), 2);
    }

    #[test]
    fn editor_upgrade_backup_covers_v3_without_effects_but_not_new_or_existing_v4_projects() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("project.json");
        let backup = root.path().join("project.pre-plugins.json");
        let old = json!({ "id": "project", "version": 3, "tracks": [] });
        super::write_editor_project_file(&path, &old).unwrap();
        super::write_editor_project_file(&path, &old).unwrap();
        assert!(!backup.exists());
        let upgraded = json!({ "id": "project", "version": 4, "masterLimiter": { "enabled": true } });
        super::write_editor_project_file(&path, &upgraded).unwrap();
        let stored: serde_json::Value = serde_json::from_slice(&std::fs::read(&backup).unwrap()).unwrap();
        assert_eq!(stored, old);
        let new_root = tempfile::tempdir().unwrap();
        let new_path = new_root.path().join("project.json");
        super::write_editor_project_file(&new_path, &upgraded).unwrap();
        super::write_editor_project_file(&new_path, &upgraded).unwrap();
        assert!(!new_root.path().join("project.pre-plugins.json").exists());
    }

    #[test]
    fn editor_upgrade_backup_failure_keeps_original_project_until_retry() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("project.json");
        let backup = root.path().join("project.pre-plugins.json");
        let old = b"{\"id\":\"project\",\"version\":3}";
        std::fs::write(&path, old).unwrap();
        std::fs::create_dir(&backup).unwrap();
        let next = json!({ "id": "project", "version": 4 });
        let error = super::write_editor_project_file(&path, &next).unwrap_err();
        assert!(error.to_string().contains("Could not preserve the original editor project"));
        assert_eq!(std::fs::read(&path).unwrap(), old);
        std::fs::remove_dir(&backup).unwrap();
        super::write_editor_project_file(&path, &next).unwrap();
        assert_eq!(std::fs::read(&backup).unwrap(), old);
    }

    #[test]
    fn editor_upgrade_backup_serializes_concurrent_first_saves() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("project.json");
        let old = b"{\"id\":\"project\",\"version\":3}";
        std::fs::write(&path, old).unwrap();
        std::thread::scope(|scope| {
            for revision in 1..=4 {
                let path = &path;
                scope.spawn(move || super::write_editor_project_file(path, &json!({
                    "id": "project", "version": 4, "revision": revision
                })).unwrap());
            }
        });
        assert_eq!(std::fs::read(root.path().join("project.pre-plugins.json")).unwrap(), old);
        let saved: serde_json::Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(saved["version"], 4);
        assert!(saved["revision"].as_u64().is_some_and(|revision| (1..=4).contains(&revision)));
    }

    #[cfg(windows)]
    #[test]
    fn json_save_retries_unable_to_remove_replaced_without_touching_the_old_file() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("project.json");
        let old = br#"{"revision":1}"#;
        std::fs::write(&path, old).unwrap();
        let mut attempts = 0;
        super::write_json_file_with_replace(&path, &json!({ "revision": 2 }), |temp, dest| {
            super::retry_file_replacement(|| {
                attempts += 1;
                assert_eq!(std::fs::read(dest).unwrap(), old);
                let pending: serde_json::Value =
                    serde_json::from_slice(&std::fs::read(temp).unwrap()).unwrap();
                assert_eq!(pending, json!({ "revision": 2 }));
                if attempts < 3 {
                    Err(std::io::Error::from_raw_os_error(1175))
                } else {
                    std::fs::rename(temp, dest)
                }
            })?;
            Ok(())
        })
        .unwrap();
        assert_eq!(attempts, 3);
        let stored: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(stored, json!({ "revision": 2 }));
        assert_eq!(std::fs::read_dir(root.path()).unwrap().count(), 1);
    }

    #[cfg(windows)]
    #[test]
    fn persistent_replacement_failure_is_bounded_and_keeps_the_previous_save() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("project.json");
        let old = br#"{"revision":1}"#;
        std::fs::write(&path, old).unwrap();
        let mut attempts = 0;
        let error = super::write_json_file_with_replace(&path, &json!({ "revision": 2 }), |_, _| {
            super::retry_file_replacement(|| {
                attempts += 1;
                Err(std::io::Error::from_raw_os_error(1175))
            })?;
            Ok(())
        })
        .unwrap_err();
        assert_eq!(attempts, 6);
        assert!(matches!(error,
            crate::error::AppError::Io(error) if error.raw_os_error() == Some(1175)));
        assert_eq!(std::fs::read(&path).unwrap(), old);
        assert_eq!(std::fs::read_dir(root.path()).unwrap().count(), 1);
    }

    #[cfg(windows)]
    #[test]
    fn partial_replacements_are_not_retried_and_keep_a_complete_recovery_file() {
        for code in [1176, 1177] {
            let root = tempfile::tempdir().unwrap();
            let path = root.path().join("project.json");
            std::fs::write(&path, br#"{"revision":1}"#).unwrap();
            let mut recovery = PathBuf::new();
            let mut attempts = 0;
            let error = super::write_json_file_with_replace(
                &path,
                &json!({ "revision": 2 }),
                |temp, dest| {
                    recovery = temp.to_path_buf();
                    super::retry_file_replacement(|| {
                        attempts += 1;
                        if code == 1176 {
                            std::fs::remove_file(dest)?;
                        } else {
                            std::fs::rename(dest, root.path().join("original.json"))?;
                        }
                        Err(std::io::Error::from_raw_os_error(code))
                    })?;
                    Ok(())
                },
            )
            .unwrap_err();
            assert_eq!(attempts, 1);
            assert!(!path.exists());
            assert!(error.to_string().contains(&recovery.display().to_string()));
            let pending: serde_json::Value =
                serde_json::from_slice(&std::fs::read(recovery).unwrap()).unwrap();
            assert_eq!(pending, json!({ "revision": 2 }));
        }
    }

    #[cfg(windows)]
    #[test]
    fn non_transient_file_errors_fail_without_retrying() {
        for code in [2, 87, 112] {
            let mut attempts = 0;
            let error = super::retry_file_replacement(|| {
                attempts += 1;
                Err(std::io::Error::from_raw_os_error(code))
            })
            .unwrap_err();
            assert_eq!(attempts, 1);
            assert_eq!(error.raw_os_error(), Some(code));
        }
    }

    #[cfg(windows)]
    #[test]
    fn json_save_succeeds_after_a_windows_reader_releases_its_handle() {
        use std::os::windows::fs::OpenOptionsExt;
        use windows_sys::Win32::Storage::FileSystem::{FILE_SHARE_READ, FILE_SHARE_WRITE};

        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("project.json");
        write_json_file(&path, &json!({ "revision": 1 })).unwrap();
        let reader = std::fs::OpenOptions::new()
            .read(true)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
            .open(&path)
            .unwrap();
        let release = std::thread::spawn(move || {
            std::thread::sleep(std::time::Duration::from_millis(100));
            drop(reader);
        });
        let result = write_json_file(&path, &json!({ "revision": 2 }));
        release.join().unwrap();
        result.unwrap();
        let stored: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(stored, json!({ "revision": 2 }));
        assert_eq!(std::fs::read_dir(root.path()).unwrap().count(), 1);
    }


    #[test]
    fn env_root_has_highest_priority() {
        let root = resolve_data_root(
            Some(path("env-data")),
            path("dev-data"),
            Some(path("portable-data")),
            path("legacy-data"),
            true,
        );

        assert_eq!(root, path("env-data"));
    }

    #[test]
    fn development_uses_project_local_data() {
        let root = resolve_data_root(
            None,
            path("dev-data"),
            Some(path("portable-data")),
            path("legacy-data"),
            true,
        );

        assert_eq!(root, path("dev-data"));
    }

    #[test]
    fn release_portable_uses_portable_data() {
        let root = resolve_data_root(
            None,
            path("dev-data"),
            Some(path("portable-data")),
            path("legacy-data"),
            false,
        );

        assert_eq!(root, path("portable-data"));
    }

    #[test]
    fn release_without_portable_marker_uses_legacy_data() {
        let root = resolve_data_root(None, path("dev-data"), None, path("legacy-data"), false);

        assert_eq!(root, path("legacy-data"));
    }
}
