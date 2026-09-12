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
const DATA_ROOT_DIR_NAME: &str = ".pymss-studio";
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
    #[cfg(target_os = "macos")]
    {
        Ok(data_root_dir(app)?.join("runtime-envs"))
    }
    #[cfg(not(target_os = "macos"))]
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

pub(crate) fn write_json_file(path: &Path, data: &Value) -> AppResult<()> {
    let _write_guard = JSON_WRITE_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
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
        replace_file(&temporary, path)
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&temporary);
    }
    result
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
    for attempt in 0..4 {
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
            return Ok(());
        }
        let error = std::io::Error::last_os_error();
        // Antivirus/indexer scans and a just-released WebView handle can briefly hold the
        // destination. Keep the temp file intact and retry only those transient Win32 errors.
        let retryable = matches!(error.raw_os_error(), Some(5 | 32 | 33));
        if !retryable || attempt == 3 {
            return Err(error.into());
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
        resolve_data_root, store_file_name, write_json_file, JSON_WRITE_SEQUENCE,
    };
    use serde_json::json;
    use std::path::PathBuf;
    use std::sync::atomic::Ordering;

    fn path(name: &str) -> PathBuf {
        PathBuf::from(name)
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
