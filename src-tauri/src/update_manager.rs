use crate::error::{AppError, AppResult};
use crate::state::AppState;
use crate::update_sources::{self, UpdateProvider};
pub use crate::update_sources::UpdateSource;
use base64::Engine;
use minisign_verify::{PublicKey, Signature};
use reqwest::{NoProxy, Proxy};
use semver::Version;
use serde::{Deserialize, Serialize};
use std::fs;
use std::fs::OpenOptions;
use std::io::Cursor;
use std::io::Write;
use std::path::{Component, Path, PathBuf};
use std::process::Command;
use std::thread;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_updater::{Update, UpdaterBuilder, UpdaterExt};
use url::Url;

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
use std::os::windows::ffi::OsStrExt;

#[cfg(windows)]
use windows_sys::Win32::Storage::FileSystem::{
    MoveFileExW, ReplaceFileW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
};

#[cfg(windows)]
use windows_sys::Win32::{
    Foundation::{CloseHandle, GetLastError, ERROR_ALREADY_EXISTS, HANDLE},
    System::Threading::{CreateMutexW, ReleaseMutex},
};

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

pub(crate) const UPDATE_EVENT: &str = "pymss://managed-update-event";
static UPDATE_RESTART_REQUIRED: AtomicBool = AtomicBool::new(false);

pub fn restart_required() -> bool {
    UPDATE_RESTART_REQUIRED.load(Ordering::SeqCst)
}
const UPDATE_ROOT: &str = "https://github.com/pymss-project/pymss-studio/releases/download/updater";
const UPDATE_GENERATION: &str = "v1";
const DEFAULT_UPDATE_PUBLIC_KEY: &str = "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IEE5NzUyREY2NjAyNDVDQjAKUldTd1hDUmc5aTExcVFtUGM3ajB4R09ueU9xM3B6RG9xRDVKaUM0di9Qc1BpSGhYMXRFNUJvSWQK";
// Test fixtures can provide a matching signing key without changing the
// production key embedded in release builds.
const UPDATE_PUBLIC_KEY: &str = match option_env!("PYMSS_UPDATE_PUBLIC_KEY") {
    Some(value) => value,
    None => DEFAULT_UPDATE_PUBLIC_KEY,
};
// Replace the executable last so an interrupted update can still boot the old UI and recover.
const MANAGED_PATHS: [&str; 3] = ["python", "bin", "Pymss Studio.exe"];
const MAX_UPDATE_ARCHIVE_BYTES: u64 = 512 * 1024 * 1024;
const MAX_UPDATE_FILES: usize = 10_000;
const MAX_UPDATE_EXTRACTED_BYTES: u64 = 2 * 1024 * 1024 * 1024;
const UPDATE_MUTEX_NAME: &str = "Local\\PymssStudioManagedUpdate";
const UPDATE_HELPER_PREFIX: &str = "pymss-studio-runtime-host";
const ERROR_ELEVATION_REQUIRED: i32 = 740;
// Apply an upper bound to connection setup and stalled reads without imposing a total
// deadline on the archive download. Managed update archives can be large on slower links.
const MANAGED_UPDATE_IO_TIMEOUT: Duration = Duration::from_secs(45);
const UPDATE_CHECK_TIMEOUT: Duration = Duration::from_secs(6);
const MANAGED_UPDATE_PROCESS_WAIT_TIMEOUT: Duration = Duration::from_secs(60);

#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub enum DistributionKind {
    Inno,
    Portable,
    #[cfg(target_os = "macos")]
    MacosApp,
    #[cfg(target_os = "linux")]
    AppImage,
}

#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum UpdateChannel {
    Stable,
    Prerelease,
}

impl UpdateChannel {
    fn endpoint_name(self) -> &'static str {
        match self {
            Self::Stable => "stable",
            Self::Prerelease => "prerelease",
        }
    }
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ManagedUpdateInfo {
    pub current_version: String,
    pub version: String,
    pub date: Option<String>,
    pub body: Option<String>,
    pub prerelease: bool,
    pub distribution: DistributionKind,
    pub auto_update_supported: bool,
    pub requires_manual_install: bool,
    pub update_message: Option<String>,
    pub manual_install_url: Option<String>,
    pub source: UpdateProvider,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ApplicationIntegrity {
    pub supported: bool,
    pub valid: Option<bool>,
    pub repair_supported: bool,
    pub detail: Option<String>,
}

pub fn check_application_integrity() -> ApplicationIntegrity {
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    {
        let result = crate::macos_update::check_integrity();
        return ApplicationIntegrity {
            supported: true,
            valid: Some(result.is_ok()),
            repair_supported: !cfg!(feature = "vst-lab") && update_supported(),
            detail: result.err().map(|error| error.to_string()),
        };
    }
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    ApplicationIntegrity { supported: false, valid: None, repair_supported: false, detail: None }
}

fn validate_repair_version(installed: &str, offered: &str) -> AppResult<()> {
    Version::parse(installed)
        .map_err(|error| AppError::Worker(format!("Invalid installed version: {error}")))?;
    if installed != offered {
        return Err(AppError::Worker("The repair package does not match the installed application version".into()));
    }
    Ok(())
}

pub async fn repair(app: &AppHandle, source: UpdateSource) -> AppResult<()> {
    if cfg!(feature = "vst-lab") || !cfg!(all(target_os = "macos", target_arch = "aarch64")) || !update_supported() {
        return Err(AppError::Worker("Application repair is available only for official macOS installations".into()));
    }
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    {
        ensure_update_idle(&app.state::<AppState>(), app.webview_windows().keys().any(|label| label != "main"))?;
        let checked = check_repair_sources(app, source).await?;
        validate_repair_version(env!("CARGO_PKG_VERSION"), &checked.update.version)?;
        // This is the complete, signed bundle for the running version. The
        // cross-version capability flag must not prevent restoring that bundle.
        return crate::macos_update::start(app, &checked.update, checked.provider, source).await;
    }
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    {
        let _ = (app, source);
        Err(AppError::Worker("Application repair is unavailable on this platform".into()))
    }
}

#[cfg(any(all(target_os = "macos", target_arch = "aarch64"), test))]
async fn check_repair_sources(app: &AppHandle, source: UpdateSource) -> AppResult<CheckedUpdate> {
    let version = env!("CARGO_PKG_VERSION");
    // Every release publishes this manifest, including numeric versions marked
    // as prereleases. The exact version check still prevents channel switching.
    let channel = UpdateChannel::Prerelease;
    let filename = update_sources::metadata_name(channel.endpoint_name(), UPDATE_GENERATION);
    let mut requests = Vec::new();
    for provider in source.providers() {
        let app = app.clone();
        let provider = *provider;
        let endpoints = update_sources::repair_manifest_endpoints(version, &filename, provider)?;
        requests.push(tauri::async_runtime::spawn(async move {
            let mut errors = Vec::new();
            for endpoint in endpoints {
                match check_endpoint(&app, endpoint, channel, provider, Some(version), true).await {
                    Ok(Some(checked)) => return Ok(checked),
                    Ok(None) => errors.push("The current release package is unavailable".to_string()),
                    Err(error) => errors.push(error.to_string()),
                }
            }
            Err(AppError::Worker(errors.join("; ")))
        }));
    }
    let mut best: Option<CheckedUpdate> = None;
    let mut errors = Vec::new();
    for request in requests {
        match request.await {
            Ok(Ok(checked)) => {
                if best.as_ref().map_or(true, |current| checked.elapsed < current.elapsed) {
                    best = Some(checked);
                }
            }
            Ok(Err(error)) => errors.push(error.to_string()),
            Err(error) => errors.push(error.to_string()),
        }
    }
    best.ok_or_else(|| AppError::Worker(format!(
        "APPLICATION_REPAIR_UNAVAILABLE: The signed package for the installed version could not be retrieved. Install it from its release page. {}", errors.join("; ")
    )))
}

#[derive(Debug, Deserialize, Serialize)]
struct UpdateTransaction {
    backup_dir: String,
    phase: UpdatePhase,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
enum UpdatePhase {
    Prepared,
    Replacing,
}

#[cfg(windows)]
struct UpdateMutex(HANDLE);

#[cfg(not(windows))]
struct UpdateMutex;

#[cfg(windows)]
impl Drop for UpdateMutex {
    fn drop(&mut self) {
        unsafe {
            ReleaseMutex(self.0);
            CloseHandle(self.0);
        }
    }
}

#[cfg(windows)]
fn acquire_update_mutex() -> AppResult<UpdateMutex> {
    let name = std::ffi::OsStr::new(UPDATE_MUTEX_NAME)
        .encode_wide()
        .chain(Some(0))
        .collect::<Vec<_>>();
    let handle = unsafe { CreateMutexW(std::ptr::null(), 1, name.as_ptr()) };
    if handle.is_null() {
        return Err(std::io::Error::last_os_error().into());
    }
    let already_exists = unsafe { GetLastError() } == ERROR_ALREADY_EXISTS;
    if already_exists {
        unsafe {
            CloseHandle(handle);
        }
        return Err(AppError::Worker("Another managed update is already in progress".into()));
    }
    Ok(UpdateMutex(handle))
}

#[cfg(not(windows))]
fn acquire_update_mutex() -> AppResult<UpdateMutex> {
    Ok(UpdateMutex)
}

pub fn current_distribution() -> Option<DistributionKind> {
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    {
        return crate::macos_update::current_bundle().ok().map(|_| DistributionKind::MacosApp);
    }
    #[cfg(target_os = "linux")]
    {
        return std::env::var_os("APPIMAGE")
            .filter(|path| Path::new(path).is_file())
            .map(|_| DistributionKind::AppImage);
    }
    #[cfg(not(any(target_os = "linux", all(target_os = "macos", target_arch = "aarch64"))))]
    {
        let root = current_app_root().ok()?;
        distribution_at(&root)
    }
}

fn distribution_at(root: &Path) -> Option<DistributionKind> {
    if root.join("pymss-studio.inno-install").is_file() {
        return Some(DistributionKind::Inno);
    }
    if root.join("pymss-studio.portable").is_file() {
        return Some(DistributionKind::Portable);
    }
    None
}

pub fn update_supported() -> bool {
    cfg!(any(windows, all(target_os = "macos", target_arch = "aarch64"), all(target_os = "linux", target_arch = "x86_64")))
        && is_official_build() && current_distribution().is_some()
}

pub async fn check(
    app: &AppHandle,
    channel: UpdateChannel,
    endpoint_override: Option<String>,
    source: UpdateSource,
) -> AppResult<Option<ManagedUpdateInfo>> {
    let Some(distribution) = current_distribution() else {
        return Ok(None);
    };
    let update = check_sources(app, channel, endpoint_override, source).await?;
    #[cfg(target_os = "macos")]
    let installation_error = crate::macos_update::current_bundle()
        .and_then(|bundle| crate::macos_update::check_location(&bundle))
        .and_then(|_| update.as_ref().map_or(Ok(()), |checked| crate::macos_update::check_runtime_contract(app, &checked.update.raw_json)))
        .err().map(|e| e.to_string());
    #[cfg(target_os = "linux")]
    let installation_error = crate::linux_update::current_image()
        .and_then(|image| crate::linux_update::check_location(&image))
        .err().map(|error| error.to_string());
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    let installation_error: Option<String> = None;
    Ok(update.map(|checked| {
        let update = checked.update;
        let auto_update_supported = installation_error.is_none() && update_auto_install_supported(&update.raw_json);
        let requires_manual_install = installation_error.is_some() || update_requires_manual_install(&update.raw_json);
        let manual_install_url = if checked.provider == UpdateProvider::Modelscope {
            update_sources::release_page(&update.version)
        } else {
            update.raw_json.get("pymss_manual_install_url").and_then(|value| value.as_str()).map(str::to_string)
        };
        ManagedUpdateInfo {
            current_version: update.current_version,
            prerelease: update.version.contains('-') || update.raw_json.get("prerelease").and_then(|value| value.as_bool()) == Some(true),
            version: update.version,
            date: update.date.map(|date| date.to_string()),
            body: update.body,
            distribution,
            auto_update_supported,
            requires_manual_install,
            update_message: if installation_error.is_some() {
                installation_error.clone()
            } else if requires_manual_install {
                Some(update_manual_install_message(&update.raw_json))
            } else {
                update.raw_json
                    .get("pymss_update_message")
                    .and_then(|value| value.as_str())
                    .map(str::to_string)
            },
            manual_install_url,
            source: checked.provider,
        }
    }))
}

fn ensure_update_idle(state: &AppState, additional_windows: bool) -> AppResult<()> {
    if additional_windows {
        return Err(AppError::Worker("APPLICATION_UPDATE_WINDOWS: Close additional editor windows before installing the application update".into()));
    }
    if !state.tasks.lock().map_err(|_| AppError::Worker("Task registry lock poisoned".into()))?.is_empty()
        || !state.native_tasks.lock().map_err(|_| AppError::Worker("Task registry lock poisoned".into()))?.is_empty()
        || !state.migrations.lock().map_err(|_| AppError::Worker("Migration registry lock poisoned".into()))?.is_empty()
    {
        return Err(AppError::Worker("APPLICATION_UPDATE_BUSY: Wait for running tasks and model migrations to finish before updating".into()));
    }
    Ok(())
}

pub async fn start(
    app: &AppHandle,
    channel: UpdateChannel,
    endpoint_override: Option<String>,
    expected_version: String,
    source: UpdateSource,
) -> AppResult<()> {
    let Some(_) = current_distribution() else {
        return Err(AppError::Worker("This application directory is not managed by an installer or portable package".into()));
    };
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    let application_guard = {
        let guard = crate::python::worker::acquire_application_update()
            .map_err(|error| AppError::Worker(format!("APPLICATION_UPDATE_BUSY: {error}")))?;
        ensure_update_idle(&app.state::<AppState>(), app.webview_windows().keys().any(|label| label != "main"))?;
        guard
    };
    let Some(checked) = check_sources(app, channel, endpoint_override, source).await?
    else {
        return Err(AppError::Worker("No newer managed update is available".into()));
    };
    let update = checked.update;
    if update_requires_manual_install(&update.raw_json) {
        return Err(AppError::Worker(
            update_manual_install_message(&update.raw_json),
        ));
    }
    if update.version != expected_version {
        return Err(AppError::Worker("The available update changed. Check for updates again before installing.".into()));
    }

    #[cfg(target_os = "macos")]
    {
        return crate::macos_update::start(app, &update, checked.provider, source).await;
    }
    #[cfg(target_os = "linux")]
    {
        return crate::linux_update::start(app, &update, checked.provider, source).await;
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        let candidates = download_candidates(&update.version, &update.download_url, checked.provider, source)?;
        let candidates = order_download_candidates(app, candidates).await?;
        let mut last_error = None;
        for (index, (provider, download_url)) in candidates.into_iter().enumerate() {
            let _ = app.emit(UPDATE_EVENT, serde_json::json!({
                "event": "SourceSelected", "data": { "source": provider, "fallback": index > 0 },
            }));
            let mut candidate = update.clone();
            candidate.download_url = download_url;
            match download_archive(app, &candidate).await {
                Ok(bytes) => {
                    let root = current_app_root()?;
                    stage_update(&root, &update.version, &update.signature, &bytes)?;
                    UPDATE_RESTART_REQUIRED.store(true, Ordering::SeqCst);
                    let _ = app.emit(UPDATE_EVENT, serde_json::json!({ "event": "RestartRequired" }));
                    // The helper is now waiting for this process to exit. Keep
                    // workers excluded through frontend persistence and exit.
                    std::mem::forget(application_guard);
                    return Ok(());
                }
                Err(error) => last_error = Some(error),
            }
        }
        Err(last_error.unwrap_or_else(|| AppError::Worker("No update download source is available".into())))
    }
}

#[cfg(not(target_os = "macos"))]
async fn download_archive(app: &AppHandle, update: &Update) -> AppResult<Vec<u8>> {
    let mut emitted_start = false;
    let mut downloaded_bytes = 0_u64;
    let download_started_at = Instant::now();
    let app_for_progress = app.clone();
    let bytes = update
        .download(
            move |chunk_length, content_length| {
                if !emitted_start {
                    emitted_start = true;
                    let _ = app_for_progress.emit(UPDATE_EVENT, serde_json::json!({
                        "event": "Started",
                        "data": { "contentLength": content_length, "downloadedBytes": 0 },
                    }));
                }
                downloaded_bytes = downloaded_bytes.saturating_add(chunk_length as u64);
                let elapsed = download_started_at.elapsed().as_secs_f64().max(0.001);
                let speed_bytes_per_second = downloaded_bytes as f64 / elapsed;
                let _ = app_for_progress.emit(UPDATE_EVENT, serde_json::json!({
                    "event": "Progress",
                    "data": {
                        "chunkLength": chunk_length,
                        "downloadedBytes": downloaded_bytes,
                        "contentLength": content_length,
                        "speedBytesPerSecond": speed_bytes_per_second,
                    },
                }));
            },
            || {},
        )
        .await
        .map_err(updater_request_error)?;
    if bytes.len() as u64 > MAX_UPDATE_ARCHIVE_BYTES {
        return Err(AppError::Worker("The downloaded update archive exceeds the supported size limit".into()));
    }

    let _ = app.emit(UPDATE_EVENT, serde_json::json!({ "event": "Finished" }));
    Ok(bytes)
}

fn updater_request_error(error: tauri_plugin_updater::Error) -> AppError {
    let message = match error {
        tauri_plugin_updater::Error::Reqwest(error) => error.without_url().to_string(),
        error => error.to_string(),
    };
    AppError::Worker(message)
}

struct CheckedUpdate {
    update: Update,
    provider: UpdateProvider,
    elapsed: Duration,
}

fn release_is_preferred(version: &str, elapsed: Duration, current: &str, current_elapsed: Duration) -> bool {
    match (Version::parse(version), Version::parse(current)) {
        (Ok(next), Ok(previous)) => next > previous || (next == previous && elapsed < current_elapsed),
        _ => false,
    }
}

async fn check_sources(
    app: &AppHandle,
    channel: UpdateChannel,
    endpoint_override: Option<String>,
    source: UpdateSource,
) -> AppResult<Option<CheckedUpdate>> {
    let endpoint = resolve_update_endpoint(channel, endpoint_override.clone())?;
    if endpoint_override.as_deref().is_some_and(|value| !value.trim().is_empty()) {
        return check_endpoint(app, endpoint, channel, UpdateProvider::Custom, None, false).await;
    }
    let mut requests = Vec::new();
    for provider in source.providers() {
        let app = app.clone();
        let provider = *provider;
        let github = endpoint.clone();
        requests.push(tauri::async_runtime::spawn(async move {
            let started = Instant::now();
            let checked = if provider == UpdateProvider::Modelscope {
                let client = source_client(&app)?;
                let filename = update_sources::metadata_name(channel.endpoint_name(), UPDATE_GENERATION);
                let manifest = update_sources::modelscope_manifest(&client, &filename).await?;
                check_endpoint(&app, manifest.endpoint, channel, provider, Some(&manifest.version), false).await?
            } else {
                check_endpoint(&app, github, channel, provider, None, false).await?
            };
            Ok::<_, AppError>(checked.map(|mut checked| { checked.elapsed = started.elapsed(); checked }))
        }));
    }
    let mut best: Option<CheckedUpdate> = None;
    let mut successful_check = false;
    let mut errors = Vec::new();
    for request in requests {
        match request.await {
            Ok(Ok(checked)) => {
                successful_check = true;
                if let Some(checked) = checked {
                    if best.as_ref().map_or(true, |current| release_is_preferred(
                        &checked.update.version, checked.elapsed, &current.update.version, current.elapsed,
                    )) {
                        best = Some(checked);
                    }
                }
            }
            Ok(Err(error)) => errors.push(error.to_string()),
            Err(error) => errors.push(error.to_string()),
        }
    }
    if successful_check { return Ok(best); }
    Err(AppError::Worker(format!("Unable to check update sources: {}", errors.join("; "))))
}

async fn check_endpoint(
    app: &AppHandle,
    endpoint: Url,
    channel: UpdateChannel,
    provider: UpdateProvider,
    expected_version: Option<&str>,
    repair: bool,
) -> AppResult<Option<CheckedUpdate>> {
    let mut builder = app.updater_builder().endpoints(vec![endpoint])
        .map_err(|error| AppError::Worker(error.to_string()))?;
    if repair {
        builder = builder.version_comparator(|current, remote| current == remote.version);
    }
    let updater = configure_updater(app, builder.timeout(UPDATE_CHECK_TIMEOUT))?
        .build().map_err(|error| AppError::Worker(error.to_string()))?;
    let started = Instant::now();
    let checked = updater.check().await.map_err(updater_request_error)?;
    checked.map(|mut update| {
        if repair {
            validate_repair_version(env!("CARGO_PKG_VERSION"), &update.version)?;
        }
        if expected_version.is_some_and(|expected| expected != update.version) {
            return Err(AppError::Worker("The update manifest does not match its release directory".into()));
        }
        if provider != UpdateProvider::Custom {
            validate_update_channel(channel, &update.version, &update.raw_json)?;
        }
        decode_release_signature(&update.signature, &update.version)?;
        // Metadata requests have a short deadline. Archive downloads retain the existing
        // connection/read limits without imposing a total deadline on a large signed ZIP.
        update.timeout = None;
        Ok(CheckedUpdate { update, provider, elapsed: started.elapsed() })
    }).transpose()
}

fn validate_update_channel(channel: UpdateChannel, version: &str, raw_json: &serde_json::Value) -> AppResult<()> {
    let version = Version::parse(version)
        .map_err(|error| AppError::Worker(format!("Invalid update version: {error}")))?;
    if raw_json.get("channel").and_then(|value| value.as_str()) != Some(channel.endpoint_name()) {
        return Err(AppError::Worker("The update manifest does not match the selected update channel".into()));
    }
    let prerelease = raw_json.get("prerelease").and_then(|value| value.as_bool())
        .ok_or_else(|| AppError::Worker("The update manifest is missing a valid prerelease flag".into()))?;
    // The signed archive name binds the version, so stable must reject SemVer
    // prereleases even when unsigned metadata claims the release is stable.
    if matches!(channel, UpdateChannel::Stable) && (!version.pre.is_empty() || prerelease) {
        return Err(AppError::Worker("The stable update channel cannot install a prerelease version".into()));
    }
    if !version.pre.is_empty() && !prerelease {
        return Err(AppError::Worker("The update manifest prerelease flag does not match its version".into()));
    }
    Ok(())
}

pub(crate) fn download_candidates(version: &str, original: &Url, provider: UpdateProvider, source: UpdateSource) -> AppResult<Vec<(UpdateProvider, Url)>> {
    if provider == UpdateProvider::Custom {
        return Ok(vec![(provider, original.clone())]);
    }
    let providers = if source == UpdateSource::Auto {
        vec![provider, if provider == UpdateProvider::Github { UpdateProvider::Modelscope } else { UpdateProvider::Github }]
    } else { vec![provider] };
    let candidates: Vec<_> = providers.into_iter().filter_map(|provider| {
        update_sources::archive_url(original, version, provider).map(|url| (provider, url))
    }).collect();
    if candidates.is_empty() {
        return Err(AppError::Worker("The update archive URL is not a supported official release URL".into()));
    }
    Ok(candidates)
}

pub(crate) async fn order_download_candidates(app: &AppHandle, candidates: Vec<(UpdateProvider, Url)>) -> AppResult<Vec<(UpdateProvider, Url)>> {
    if candidates.len() < 2 { return Ok(candidates); }
    let client = source_client(app)?;
    let mut probes = Vec::new();
    for candidate in candidates {
        let client = client.clone();
        probes.push(tauri::async_runtime::spawn(async move {
            let started = Instant::now();
            let available = client.head(candidate.1.clone()).timeout(Duration::from_secs(3)).send().await
                .is_ok_and(|response| response.status().is_success());
            (candidate, available, started.elapsed())
        }));
    }
    let mut results = Vec::new();
    for probe in probes {
        results.push(probe.await.map_err(|error| AppError::Worker(error.to_string()))?);
    }
    results.sort_by_key(|(_, available, elapsed)| (!*available, *elapsed));
    Ok(results.into_iter().map(|(candidate, _, _)| candidate).collect())
}

pub fn run_helper_from_args() -> Option<i32> {
    let mut args = std::env::args_os();
    let _ = args.next();
    let mode = args.next()?.to_string_lossy().to_string();
    let result = match mode.as_str() {
        "--apply-managed-update" | "--apply-managed-update-elevated" => {
            let pid = parse_pid(args.next())?;
            let root = PathBuf::from(args.next()?);
            let archive = PathBuf::from(args.next()?);
            let version = args.next()?.to_string_lossy().to_string();
            let signature = args.next()?.to_string_lossy().to_string();
            let result = apply_managed_update(pid, root.clone(), archive, version, signature, mode == "--apply-managed-update-elevated");
            if let Err(error) = &result {
                crate::session_log::early("WARN", "update.diagnostic", &format!("Managed update helper failed: {error}"));
                append_helper_log(&root, &format!("update failed: {error}"));
                if let Err(recovery_error) = recover_or_relaunch(&root, mode == "--apply-managed-update-elevated") {
                    crate::session_log::early("WARN", "update.diagnostic", &format!("Managed update recovery failed: {recovery_error}"));
                    append_helper_log(&root, &format!("recovery failed: {recovery_error}"));
                }
            }
            result
        }
        "--recover-managed-update" | "--recover-managed-update-elevated" => {
            let pid = parse_pid(args.next())?;
            let root = PathBuf::from(args.next()?);
            let backup = PathBuf::from(args.next()?);
            let elevated = mode == "--recover-managed-update-elevated";
            recover_interrupted_update(pid, root, backup, elevated)
        }
        _ => return None,
    };
    schedule_helper_cleanup();
    Some(if result.is_ok() { 0 } else { 1 })
}

pub fn recover_interrupted_update_from_startup() -> Option<i32> {
    let root = current_app_root().ok()?;
    let transaction = match read_update_transaction(&root) {
        Ok(Some(transaction)) => transaction,
        Ok(None) => return None,
        Err(error) => {
            crate::session_log::early("WARN", "update.diagnostic", &format!("Unable to read interrupted update transaction: {error}"));
            if let Err(quarantine_error) = quarantine_update_transaction(&root) {
                crate::session_log::early("WARN", "update.diagnostic", &format!("Unable to quarantine the invalid update transaction: {quarantine_error}"));
            }
            return None;
        }
    };
    let _update_mutex = match acquire_update_mutex() {
        Ok(mutex) => mutex,
        Err(AppError::Worker(message)) if message == "Another managed update is already in progress" => {
            crate::session_log::early("WARN", "update.diagnostic", &format!("Managed update recovery is waiting for another update process: {message}"));
            // The active updater holds this mutex while it launches the new
            // application and verifies the replacement.  Let the new process
            // continue booting; exiting here makes the helper treat the
            // update as failed and roll the files back to the old version.
            return None;
        }
        Err(error) => {
            crate::session_log::early("WARN", "update.diagnostic", &format!("Unable to acquire the managed update mutex during startup recovery: {error}"));
            return Some(1);
        }
    };
    let backup = PathBuf::from(&transaction.backup_dir);
    if transaction.phase == UpdatePhase::Prepared {
        if let Err(error) = cleanup_prepared_transaction(&root, &backup) {
            crate::session_log::early("WARN", "update.diagnostic", &format!("Unable to clear prepared update transaction: {error}"));
        }
        return None;
    }
    let helper = spawn_recovery_helper(&root, &backup);
    if helper.is_ok() {
        Some(0)
    } else {
        crate::session_log::early("WARN", "update.diagnostic", &format!("Unable to start interrupted update recovery; continuing with the current application"));
        if transaction.phase == UpdatePhase::Prepared {
            let _ = quarantine_update_transaction(&root);
            None
        } else {
            Some(1)
        }
    }
}

fn spawn_recovery_helper(root: &Path, backup: &Path) -> AppResult<()> {
    let elevated = matches!(distribution_at(root), Some(DistributionKind::Inno)) || !app_root_is_writable(root);
    let mode = if elevated {
        "--recover-managed-update-elevated"
    } else {
        "--recover-managed-update"
    };
    spawn_helper(
        &[
            mode.into(),
            std::process::id().to_string().into(),
            root.as_os_str().to_os_string(),
            backup.as_os_str().to_os_string(),
        ],
        elevated,
    )
}

fn cleanup_prepared_transaction(root: &Path, backup: &Path) -> AppResult<()> {
    if let Err(error) = validate_backup_path(root, backup) {
        crate::session_log::early("WARN", "update.diagnostic", &format!("Prepared update transaction has an invalid backup path: {error}"));
        quarantine_update_transaction(root)?;
        return Ok(());
    }
    if backup.is_dir() {
        fs::remove_dir_all(backup)?;
    }
    quarantine_update_transaction(root)?;
    Ok(())
}

fn managed_endpoint(channel: UpdateChannel) -> Url {
    Url::parse(&format!("{UPDATE_ROOT}/{}", update_sources::metadata_name(channel.endpoint_name(), UPDATE_GENERATION)))
        .expect("managed update endpoint is valid")
}

fn update_auto_install_supported(raw_json: &serde_json::Value) -> bool {
    // A missing capability flag must never authorize a cross-generation install.
    raw_json
        .get("pymss_update_supported")
        .and_then(|value| value.as_bool())
        .unwrap_or(false)
}

fn update_requires_manual_install(raw_json: &serde_json::Value) -> bool {
    raw_json
        .get("pymss_requires_manual_install")
        .and_then(|value| value.as_bool())
        .unwrap_or(false)
        || !update_auto_install_supported(raw_json)
}

fn update_manual_install_message(raw_json: &serde_json::Value) -> String {
    raw_json
        .get("pymss_update_message")
        .and_then(|value| value.as_str())
        .filter(|value| !value.trim().is_empty())
        .unwrap_or("This update requires manual installation from its release page because it cannot be applied safely in place.")
        .to_string()
}

fn resolve_update_endpoint(channel: UpdateChannel, endpoint_override: Option<String>) -> AppResult<Url> {
    if !is_official_build() {
        return Err(AppError::Worker("Managed updates are available only in official builds".into()));
    }
    if let Some(endpoint) = endpoint_override.filter(|value| !value.trim().is_empty()) {
        return Url::parse(endpoint.trim())
            .map_err(|error| AppError::Worker(format!("Invalid update endpoint: {error}")));
    }
    Ok(managed_endpoint(channel))
}

fn configure_updater_client_timeouts(client: reqwest::ClientBuilder) -> reqwest::ClientBuilder {
    client
        .connect_timeout(MANAGED_UPDATE_IO_TIMEOUT)
        .read_timeout(MANAGED_UPDATE_IO_TIMEOUT)
}

fn configure_updater(app: &AppHandle, builder: UpdaterBuilder) -> AppResult<UpdaterBuilder> {
    let (direct, proxy) = updater_proxy(app)?;
    Ok(builder.configure_client(move |client| configure_update_client(client, direct, proxy.clone())))
}

fn source_client(app: &AppHandle) -> AppResult<reqwest::Client> {
    let (direct, proxy) = updater_proxy(app)?;
    configure_update_client(update_sources::http_client_builder(), direct, proxy)
        .user_agent("Pymss-Studio-Updater")
        .timeout(UPDATE_CHECK_TIMEOUT)
        .build()
        .map_err(|error| AppError::Worker(error.to_string()))
}

pub(crate) fn configure_update_client(client: reqwest::ClientBuilder, direct: bool, proxy: Option<Proxy>) -> reqwest::ClientBuilder {
    let client = configure_updater_client_timeouts(client);
    if direct { client.no_proxy() }
    else if let Some(proxy) = proxy { client.proxy(proxy) }
    else { client }
}

pub(crate) fn updater_proxy(app: &AppHandle) -> AppResult<(bool, Option<Proxy>)> {
    let Some(state) = app.try_state::<AppState>() else {
        return Ok((false, None));
    };
    let proxy_settings = state
        .proxy_settings
        .lock()
        .map_err(|_| AppError::Worker("proxy_settings lock poisoned".into()))?
        .clone();
    match proxy_settings.mode.as_str() {
        "none" => Ok((true, None)),
        "custom" => {
            let value = proxy_settings.url.trim();
            if value.is_empty() {
                return Ok((true, None));
            }
            let normalized = if value.contains("://") {
                value.to_string()
            } else {
                format!("http://{value}")
            };
            let proxy_url = Url::parse(&normalized)
                .map_err(|error| AppError::Worker(format!("Invalid updater proxy URL: {error}")))?;
            let proxy = Proxy::all(proxy_url.as_str())
                .map_err(|error| AppError::Worker(format!("Invalid updater proxy URL: {error}")))?;
            let no_proxy = NoProxy::from_string(&proxy_settings.bypass);
            Ok((false, Some(proxy.no_proxy(no_proxy))))
        }
        _ => Ok((false, None)),
    }
}

fn is_official_build() -> bool {
    option_env!("PYMSS_BUILD_OFFICIAL") == Some("true")
}

fn current_app_root() -> AppResult<PathBuf> {
    std::env::current_exe()?
        .parent()
        .map(PathBuf::from)
        .ok_or_else(|| AppError::Worker("Unable to resolve the application directory".into()))
}

fn stage_update(root: &Path, version: &str, signature: &str, bytes: &[u8]) -> AppResult<()> {
    decode_release_signature(signature, version)?;
    if read_update_transaction(root)?.is_some() {
        return Err(AppError::Worker("A previous managed update requires recovery before another update can start".into()));
    }
    let update_mutex = acquire_update_mutex()?;
    let elevated = root.join("pymss-studio.inno-install").is_file() || !app_root_is_writable(root);
    let archive = if elevated {
        pending_archive_path()
    } else {
        update_archive_path(root, version)
    };
    if let Some(parent) = archive.parent() {
        fs::create_dir_all(parent)?;
    }
    if archive.exists() {
        fs::remove_file(&archive)?;
    }
    if let Err(error) = fs::write(&archive, bytes) {
        let _ = fs::remove_file(&archive);
        return Err(error.into());
    }
    let mode = if elevated {
        "--apply-managed-update-elevated"
    } else {
        "--apply-managed-update"
    };
    let result = spawn_helper(&[
        mode.into(),
        std::process::id().to_string().into(),
        root.as_os_str().to_os_string(),
        archive.clone().into_os_string(),
        version.into(),
        signature.into(),
    ], elevated);
    if result.is_err() {
        let _ = fs::remove_file(archive);
    } else {
        // The helper acquires this mutex after the main process exits.
        std::mem::forget(update_mutex);
    }
    result
}

fn spawn_helper(args: &[std::ffi::OsString], elevated: bool) -> AppResult<()> {
    let helper = temp_path(UPDATE_HELPER_PREFIX, "exe");
    let current_exe = std::env::current_exe()?;
    if let Err(error) = fs::copy(&current_exe, &helper) {
        let _ = fs::remove_file(&helper);
        return Err(error.into());
    }
    let result = if elevated {
        spawn_elevated_helper(&helper, args)
    } else {
        match Command::new(&helper).args(args).spawn() {
            Ok(_) => Ok(()),
            Err(error) if is_elevation_required(&error) => {
                if let Some(elevated_args) = elevated_helper_args(args) {
                    spawn_elevated_helper(&helper, &elevated_args)
                } else {
                    Err(error.into())
                }
            }
            Err(error) => Err(error.into()),
        }
    };
    if result.is_err() {
        let _ = fs::remove_file(helper);
    }
    result
}

fn recover_interrupted_update(pid: u32, root: PathBuf, backup: PathBuf, elevated: bool) -> AppResult<()> {
    wait_for_process_exit(pid, &root.join("Pymss Studio.exe"))?;
    let _update_mutex = acquire_update_mutex()?;
    recover_interrupted_update_with(&root, &backup, || relaunch_application_if_needed(&root, elevated))
}

fn recover_interrupted_update_with(
    root: &Path, backup: &Path, relaunch: impl FnOnce() -> AppResult<()>,
) -> AppResult<()> {
    if distribution_at(root).is_none() {
        return Err(AppError::Worker("The recovery target is not a managed Pymss Studio installation".into()));
    }
    let transaction = read_update_transaction(root)?
        .ok_or_else(|| AppError::Worker("Interrupted update transaction is missing".into()))?;
    if PathBuf::from(&transaction.backup_dir).as_path() != backup {
        return Err(AppError::Worker("Interrupted update backup does not match its transaction".into()));
    }
    recover_transaction_files(
        root,
        backup,
        transaction.phase,
        "Interrupted update backup is missing; the transaction marker was retained for recovery",
    )?;
    relaunch()?;
    Ok(())
}

fn recover_or_relaunch(root: &Path, elevated: bool) -> AppResult<()> {
    let _update_mutex = acquire_update_mutex()?;
    recover_or_relaunch_with(root, || relaunch_application_if_needed(root, elevated))
}

fn recover_or_relaunch_with(root: &Path, relaunch: impl FnOnce() -> AppResult<()>) -> AppResult<()> {
    if distribution_at(root).is_none() {
        return Err(AppError::Worker("The recovery target is not a managed Pymss Studio installation".into()));
    }
    if let Some(transaction) = read_update_transaction(root)? {
        let backup = PathBuf::from(transaction.backup_dir);
        recover_transaction_files(
            root,
            &backup,
            transaction.phase,
            "Update backup is missing; the transaction marker was retained for recovery",
        )?;
    }
    relaunch()?;
    Ok(())
}

fn recover_transaction_files(
    root: &Path, backup: &Path, phase: UpdatePhase, missing_backup_error: &'static str,
) -> AppResult<()> {
    validate_backup_path(root, backup)?;
    if !backup.is_dir() {
        if phase == UpdatePhase::Prepared {
            return quarantine_update_transaction(root);
        }
        return Err(AppError::Worker(missing_backup_error.into()));
    }
    restore_portable_backup(root, backup)?;
    clear_recovered_transaction(root)?;
    let _ = fs::remove_dir_all(backup);
    Ok(())
}

fn append_helper_log(root: &Path, message: &str) {
    let path = root.join("data").join("logs").join("update-helper.log");
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(path) {
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        let _ = writeln!(file, "{timestamp} {message}");
    }
}

fn temp_path(prefix: &str, extension: &str) -> PathBuf {
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    std::env::temp_dir().join(format!("{prefix}-{}-{stamp}.{extension}", std::process::id()))
}

#[cfg(windows)]
fn schedule_helper_cleanup() {
    let Ok(helper) = std::env::current_exe() else {
        return;
    };
    let escaped = helper.to_string_lossy().replace('"', "\"\"");
    let _ = Command::new("cmd.exe")
        .args(["/C", &format!("ping 127.0.0.1 -n 2 > nul & del /F /Q \"{escaped}\"")])
        .spawn();
}

#[cfg(not(windows))]
fn schedule_helper_cleanup() {}

fn extract_portable_payload(bytes: &[u8], stage: &Path) -> AppResult<()> {
    if bytes.len() as u64 > MAX_UPDATE_ARCHIVE_BYTES {
        return Err(AppError::Worker("Portable update archive exceeds the supported size limit".into()));
    }
    let mut archive = zip::ZipArchive::new(Cursor::new(bytes))
        .map_err(|error| AppError::Worker(format!("Invalid portable update archive: {error}")))?;
    if archive.len() > MAX_UPDATE_FILES {
        return Err(AppError::Worker("Portable update archive contains too many files".into()));
    }
    fs::create_dir_all(stage)?;
    let mut extracted_bytes = 0_u64;
    for index in 0..archive.len() {
        let mut entry = archive
            .by_index(index)
            .map_err(|error| AppError::Worker(format!("Unable to read portable update archive: {error}")))?;
        let relative = entry
            .enclosed_name()
            .ok_or_else(|| AppError::Worker("Portable update archive contains an unsafe path".into()))?
            .to_path_buf();
        validate_payload_path(&relative)?;
        extracted_bytes = extracted_bytes.saturating_add(entry.size());
        if extracted_bytes > MAX_UPDATE_EXTRACTED_BYTES {
            return Err(AppError::Worker("Portable update archive exceeds the extracted size limit".into()));
        }
        let destination = stage.join(relative);
        if entry.is_dir() {
            fs::create_dir_all(destination)?;
            continue;
        }
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut output = fs::File::create(destination)?;
        std::io::copy(&mut entry, &mut output)?;
    }
    for required in ["Pymss Studio.exe", "python/worker.py"] {
        if !stage.join(required).is_file() {
            let _ = fs::remove_dir_all(stage);
            return Err(AppError::Worker(format!("Portable update archive is missing {required}")));
        }
    }
    Ok(())
}

fn validate_payload_path(path: &Path) -> AppResult<()> {
    let mut components = path.components();
    let Some(Component::Normal(top)) = components.next() else {
        return Err(AppError::Worker("Portable update archive contains an invalid path".into()));
    };
    if !matches!(top.to_str(), Some("Pymss Studio.exe" | "python" | "bin")) {
        return Err(AppError::Worker("Portable update archive contains an unmanaged file".into()));
    }
    if path.components().any(|component| !matches!(component, Component::Normal(_))) {
        return Err(AppError::Worker("Portable update archive contains an invalid path".into()));
    }
    Ok(())
}

fn parse_pid(value: Option<std::ffi::OsString>) -> Option<u32> {
    value?.to_string_lossy().parse().ok()
}

fn apply_managed_update(
    pid: u32,
    root: PathBuf,
    archive: PathBuf,
    version: String,
    signature: String,
    elevated: bool,
) -> AppResult<()> {
    wait_for_process_exit(pid, &root.join("Pymss Studio.exe"))?;
    let _update_mutex = acquire_update_mutex()?;
    validate_update_version(&version)?;
    let distribution = distribution_at(&root)
        .ok_or_else(|| AppError::Worker("The update target is not a managed Pymss Studio installation".into()))?;
    let inno_install = matches!(distribution, DistributionKind::Inno);
    if !elevated && (inno_install || !app_root_is_writable(&root)) {
        return Err(AppError::Worker("The application update helper was not started with the required administrator permission".into()));
    }
    let previous_inno_version = if elevated && inno_install {
        match read_inno_display_version() {
            Ok(version) => Some(version),
            Err(error) => {
                let _ = fs::remove_file(&archive);
                return Err(error);
            }
        }
    } else {
        None
    };
    let payload = match fs::read(&archive) {
        Ok(payload) => payload,
        Err(error) => {
            let _ = fs::remove_file(&archive);
            return Err(error.into());
        }
    };
    if let Err(error) = verify_payload_signature(&payload, &signature, &version) {
        let _ = fs::remove_file(&archive);
        return Err(error);
    }
    // Keep the extracted payload on the target volume so replacement remains atomic on Windows.
    let stage = update_stage_path(&root, &version);
    let extraction = extract_portable_payload(&payload, &stage);
    let _ = fs::remove_file(&archive);
    if let Err(error) = extraction {
        let _ = fs::remove_dir_all(&stage);
        return Err(error);
    }
    let backup = root.join(format!(
        ".pymss-studio-update-backup-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos()
    ));
    if let Err(error) = fs::create_dir(&backup) {
        let _ = fs::remove_dir_all(&stage);
        return Err(error.into());
    }
    if let Err(error) = write_update_transaction(&root, &backup, UpdatePhase::Prepared) {
        let _ = fs::remove_dir_all(&backup);
        let _ = fs::remove_dir_all(&stage);
        return Err(error);
    }
    set_update_phase(&root, &backup, UpdatePhase::Replacing)?;
    let mut moved = Vec::new();
    let mut replaced = Vec::new();
    let result = (|| -> AppResult<()> {
        for name in MANAGED_PATHS {
            // Bundled tools are omitted from normal update archives and remain in place.
            if !stage.join(name).exists() {
                continue;
            }
            let current = root.join(name);
            if current.exists() {
                if name == "Pymss Studio.exe" {
                    replace_executable(&current, &stage.join(name), Some(&backup.join(name)))?;
                    moved.push(name);
                    replaced.push(name);
                    continue;
                }
                fs::rename(&current, backup.join(name))?;
                moved.push(name);
            }
            fs::rename(stage.join(name), root.join(name))?;
            replaced.push(name);
        }
        Ok(())
    })();
    if let Err(error) = result {
        let rollback = rollback_partial_replacement(&root, &backup, &replaced, &moved);
        let _ = fs::remove_dir_all(&stage);
        if let Err(rollback_error) = rollback {
            return Err(AppError::Worker(format!("{error}; rollback failed: {rollback_error}")));
        }
        remove_update_transaction(&root)?;
        let _ = fs::remove_dir_all(&backup);
        return Err(error);
    }
    let _ = fs::remove_dir_all(&stage);
    if elevated && inno_install {
        if let Err(error) = update_inno_display_version(&version) {
            if let Err(rollback_error) = rollback_managed_update(&root, &backup, previous_inno_version.as_deref()) {
                return Err(AppError::Worker(format!("{error}; rollback failed: {rollback_error}")));
            }
            return Err(error);
        }
    }
    let updated = match launch_application(&root, elevated) {
        Ok(child) => child,
        Err(error) => {
            if let Err(rollback_error) = rollback_managed_update(&root, &backup, previous_inno_version.as_deref()) {
                return Err(AppError::Worker(format!("{error}; rollback failed: {rollback_error}")));
            }
            return Err(error);
        }
    };
    if let Err(error) = verify_updated_application(&root, elevated, updated) {
        if let Err(rollback_error) = rollback_managed_update(&root, &backup, previous_inno_version.as_deref()) {
            return Err(AppError::Worker(format!("{error}; rollback failed: {rollback_error}")));
        }
        return Err(error);
    }
    remove_update_transaction(&root)?;
    if let Err(error) = fs::remove_dir_all(&backup) {
        crate::session_log::early("WARN", "update.diagnostic", &format!("Managed update succeeded but backup cleanup failed at {}: {error}", backup.display()));
    }
    Ok(())
}

#[cfg(windows)]
fn replace_executable(current: &Path, replacement: &Path, backup: Option<&Path>) -> AppResult<()> {
    let current_wide = current.as_os_str().encode_wide().chain(Some(0)).collect::<Vec<_>>();
    let replacement_wide = replacement.as_os_str().encode_wide().chain(Some(0)).collect::<Vec<_>>();
    let backup_wide = backup.map(|path| path.as_os_str().encode_wide().chain(Some(0)).collect::<Vec<_>>());
    for attempt in 0..4 {
        let replaced = unsafe {
            ReplaceFileW(
                current_wide.as_ptr(),
                replacement_wide.as_ptr(),
                backup_wide.as_ref().map_or(std::ptr::null(), |path| path.as_ptr()),
                0,
                std::ptr::null(),
                std::ptr::null(),
            )
        };
        if replaced != 0 {
            return Ok(());
        }
        let error = std::io::Error::last_os_error();
        // The updater helper runs immediately after the main process exits. Windows Defender,
        // indexers, or a just-released process handle can briefly keep the executable locked;
        // retry those transient sharing errors before rolling the update back.
        let retryable = matches!(error.raw_os_error(), Some(5 | 32 | 33));
        if !retryable || attempt == 3 {
            // ReplaceFileW can be rejected by some writable locations (for example, when the
            // volume does not allow creating the optional backup in the same operation). Keep
            // the rollback contract by moving the old executable to the transaction backup,
            // then moving the staged executable into place.
            match replace_executable_with_moves(current, replacement, backup) {
                Ok(()) => return Ok(()),
                Err(fallback_error) => {
                    crate::session_log::early("WARN", "update.diagnostic", &format!("Executable replacement failed with {error}; fallback also failed with {fallback_error}"));
                }
            }
            return Err(error.into());
        }
        thread::sleep(Duration::from_millis(80 * (attempt + 1) as u64));
    }
    unreachable!("ReplaceFileW retry loop must return")
}

#[cfg(windows)]
fn replace_executable_with_moves(current: &Path, replacement: &Path, backup: Option<&Path>) -> AppResult<()> {
    let current = current.as_os_str().encode_wide().chain(Some(0)).collect::<Vec<_>>();
    let replacement = replacement.as_os_str().encode_wide().chain(Some(0)).collect::<Vec<_>>();
    if let Some(backup) = backup {
        let backup = backup.as_os_str().encode_wide().chain(Some(0)).collect::<Vec<_>>();
        let moved_old = unsafe { MoveFileExW(current.as_ptr(), backup.as_ptr(), MOVEFILE_WRITE_THROUGH) };
        if moved_old == 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        let moved_new = unsafe { MoveFileExW(replacement.as_ptr(), current.as_ptr(), MOVEFILE_WRITE_THROUGH) };
        if moved_new == 0 {
            let replacement_error = std::io::Error::last_os_error();
            let restored = unsafe {
                MoveFileExW(
                    backup.as_ptr(),
                    current.as_ptr(),
                    MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
                )
            };
            if restored == 0 {
                crate::session_log::early("WARN", "update.diagnostic", &format!("Unable to restore executable after fallback replacement failed: {}", std::io::Error::last_os_error()));
            }
            return Err(replacement_error.into());
        }
        return Ok(());
    }
    let moved = unsafe {
        MoveFileExW(
            replacement.as_ptr(),
            current.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };
    if moved == 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(())
}

#[cfg(not(windows))]
fn replace_executable(current: &Path, replacement: &Path, backup: Option<&Path>) -> AppResult<()> {
    if let Some(backup) = backup {
        fs::rename(current, backup)?;
    } else {
        fs::remove_file(current)?;
    }
    fs::rename(replacement, current)?;
    Ok(())
}

fn transaction_path(root: &Path) -> PathBuf {
    root.join(".pymss-studio-update-transaction.json")
}

fn write_update_transaction(root: &Path, backup: &Path, phase: UpdatePhase) -> AppResult<()> {
    let path = transaction_path(root);
    let temporary = root.join(format!(".pymss-studio-update-transaction-{}.tmp", std::process::id()));
    let content = serde_json::to_vec(&UpdateTransaction {
        backup_dir: backup.to_string_lossy().to_string(),
        phase,
    })?;
    let mut file = fs::File::create(&temporary)?;
    file.write_all(&content)?;
    file.sync_all()?;
    let result = replace_transaction_file(&temporary, &path);
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

fn is_elevation_required(error: &std::io::Error) -> bool {
    cfg!(windows) && error.raw_os_error() == Some(ERROR_ELEVATION_REQUIRED)
}

fn elevated_helper_args(args: &[std::ffi::OsString]) -> Option<Vec<std::ffi::OsString>> {
    let elevated_mode = match args.first()?.to_str()? {
        "--apply-managed-update" => "--apply-managed-update-elevated",
        "--recover-managed-update" => "--recover-managed-update-elevated",
        _ => return None,
    };
    let mut elevated_args = args.to_vec();
    elevated_args[0] = elevated_mode.into();
    Some(elevated_args)
}

fn replace_transaction_file(temporary: &Path, destination: &Path) -> AppResult<()> {
    #[cfg(windows)]
    {
        // std::fs::rename does not replace an existing destination on Windows. MoveFileExW
        // performs the marker swap in place without requiring the executable replacement path.
        let destination = destination.as_os_str().encode_wide().chain(Some(0)).collect::<Vec<_>>();
        let temporary = temporary.as_os_str().encode_wide().chain(Some(0)).collect::<Vec<_>>();
        for attempt in 0..4 {
            let replaced = unsafe {
                MoveFileExW(
                    temporary.as_ptr(),
                    destination.as_ptr(),
                    MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
                )
            };
            if replaced != 0 {
                return Ok(());
            }
            let error = std::io::Error::last_os_error();
            let retryable = matches!(error.raw_os_error(), Some(5 | 32 | 33));
            if !retryable || attempt == 3 {
                return Err(error.into());
            }
            thread::sleep(Duration::from_millis(40 * (attempt + 1) as u64));
        }
        unreachable!("MoveFileExW retry loop must return");
    }

    #[cfg(not(windows))]
    {
        fs::rename(temporary, destination)?;
        Ok(())
    }
}

fn set_update_phase(root: &Path, backup: &Path, phase: UpdatePhase) -> AppResult<()> {
    write_update_transaction(root, backup, phase)
}

fn read_update_transaction(root: &Path) -> AppResult<Option<UpdateTransaction>> {
    let path = transaction_path(root);
    if !path.is_file() {
        return Ok(None);
    }
    let transaction = serde_json::from_slice(&fs::read(path)?)?;
    Ok(Some(transaction))
}

fn remove_update_transaction(root: &Path) -> AppResult<()> {
    let path = transaction_path(root);
    if path.exists() {
        fs::remove_file(path)?;
    }
    Ok(())
}

fn clear_recovered_transaction(root: &Path) -> AppResult<()> {
    if let Err(error) = remove_update_transaction(root) {
        crate::session_log::early("WARN", "update.diagnostic", &format!("Recovered update files, but could not remove the transaction marker: {error}"));
        quarantine_update_transaction(root)?;
    }
    Ok(())
}

fn quarantine_update_transaction(root: &Path) -> AppResult<()> {
    let source = transaction_path(root);
    if !source.exists() {
        return Ok(());
    }
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let destination = root.join(format!(".pymss-studio-update-transaction.invalid-{stamp}"));
    fs::rename(source, destination)?;
    Ok(())
}

fn validate_update_version(version: &str) -> AppResult<()> {
    Version::parse(version)
        .map(|_| ())
        .map_err(|error| AppError::Worker(format!("Invalid managed update version: {error}")))
}

fn validate_backup_path(root: &Path, backup: &Path) -> AppResult<()> {
    if backup.parent() != Some(root) {
        return Err(AppError::Worker("Update backup is outside the application directory".into()));
    }
    let valid_name = backup
        .file_name()
        .and_then(|value| value.to_str())
        .is_some_and(|value| value.starts_with(".pymss-studio-update-backup-"));
    if !valid_name {
        return Err(AppError::Worker("Update backup has an invalid directory name".into()));
    }
    Ok(())
}

pub(crate) fn decode_release_signature(release_signature: &str, version: &str) -> AppResult<Signature> {
    validate_update_version(version)?;
    let signature = base64::engine::general_purpose::STANDARD.decode(release_signature)
        .map_err(|error| AppError::Worker(format!("Invalid update signature: {error}")))?;
    let signature = String::from_utf8(signature)
        .map_err(|error| AppError::Worker(format!("Invalid update signature: {error}")))?;
    let signature = Signature::decode(&signature)
        .map_err(|error| AppError::Worker(format!("Invalid update signature: {error}")))?;
    let expected = update_sources::archive_name(version);
    let mut filenames = signature.trusted_comment().split('\t').filter_map(|part| part.strip_prefix("file:"));
    if filenames.next() != Some(expected.as_str()) || filenames.next().is_some() {
        return Err(AppError::Worker("The update signature does not match the expected release archive".into()));
    }
    Ok(signature)
}

pub(crate) fn update_public_key() -> AppResult<PublicKey> {
    let public_key = base64::engine::general_purpose::STANDARD
        .decode(UPDATE_PUBLIC_KEY)
        .map_err(|error| AppError::Worker(format!("Invalid embedded update public key: {error}")))?;
    let public_key = String::from_utf8(public_key)
        .map_err(|error| AppError::Worker(format!("Invalid embedded update public key: {error}")))?;
    let public_key = PublicKey::decode(&public_key)
        .map_err(|error| AppError::Worker(format!("Invalid update public key: {error}")))?;
    Ok(public_key)
}

pub(crate) fn verify_payload_signature(payload: &[u8], release_signature: &str, version: &str) -> AppResult<()> {
    let public_key = update_public_key()?;
    let signature = decode_release_signature(release_signature, version)?;
    public_key
        .verify(payload, &signature, true)
        .map_err(|error| AppError::Worker(format!("Update signature verification failed: {error}")))?;
    Ok(())
}

fn update_stage_path(root: &Path, version: &str) -> PathBuf {
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis();
    root.join("update").join(format!("pymss-studio-{version}-{stamp}"))
}

fn update_archive_path(root: &Path, version: &str) -> PathBuf {
    let _ = version;
    root.join("update").join("pymss-studio-pending.zip")
}

fn pending_archive_path() -> PathBuf {
    std::env::temp_dir().join("pymss-studio-pending.zip")
}

fn verify_updated_application(root: &Path, elevated: bool, mut child: std::process::Child) -> AppResult<()> {
    if elevated {
        return verify_elevated_application(root);
    }
    for _ in 0..80 {
        if child.try_wait()?.is_some() {
            return Err(AppError::Worker("Updated application stopped during its startup verification window".into()));
        }
        thread::sleep(Duration::from_millis(250));
    }
    Ok(())
}

#[cfg(windows)]
fn launch_application(root: &Path, elevated: bool) -> AppResult<std::process::Child> {
    if elevated {
        // Reuse the user's Explorer process so the normal UI does not inherit the helper's UAC token.
        return Ok(Command::new("explorer.exe").arg(root.join("Pymss Studio.exe")).spawn()?);
    }
    Ok(Command::new(root.join("Pymss Studio.exe")).spawn()?)
}

#[cfg(windows)]
fn verify_elevated_application(root: &Path) -> AppResult<()> {
    let exe = root.join("Pymss Studio.exe");
    let mut observed = false;
    for _ in 0..80 {
        if application_is_running(&exe)? {
            observed = true;
        } else if observed {
            return Err(AppError::Worker("Updated application stopped during its startup verification window".into()));
        }
        thread::sleep(Duration::from_millis(250));
    }
    if observed {
        Ok(())
    } else {
        Err(AppError::Worker("Updated application did not start during its startup verification window".into()))
    }
}

#[cfg(windows)]
fn application_is_running(exe: &Path) -> AppResult<bool> {
    let path = quote_powershell_arg(&exe.to_string_lossy());
    let command = format!(
        "$process = Get-Process -Name 'Pymss Studio' -ErrorAction SilentlyContinue | Where-Object {{ $_.Path -eq {path} }} | Select-Object -First 1; if ($null -ne $process) {{ exit 0 }}; exit 1"
    );
    let status = Command::new("powershell.exe")
        .args(["-NoProfile", "-NonInteractive", "-Command", &command])
        .creation_flags(CREATE_NO_WINDOW)
        .status()?;
    Ok(status.success())
}

#[cfg(not(windows))]
fn launch_application(root: &Path, _elevated: bool) -> AppResult<std::process::Child> {
    Ok(Command::new(root.join("Pymss Studio.exe")).spawn()?)
}

#[cfg(not(windows))]
fn verify_elevated_application(_root: &Path) -> AppResult<()> {
    Err(AppError::Worker("Managed updates are supported only on Windows".into()))
}

#[cfg(windows)]
fn wait_for_process_exit(pid: u32, executable: &Path) -> AppResult<()> {
    let path = quote_powershell_arg(&executable.to_string_lossy());
    let timeout_ms = MANAGED_UPDATE_PROCESS_WAIT_TIMEOUT.as_millis();
    let command_text = format!(
        "$deadline = [DateTime]::UtcNow.AddMilliseconds({timeout_ms}); do {{ $main = Get-Process -Id {pid} -ErrorAction SilentlyContinue; $samePath = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {{ try {{ $_.Path -eq {path} }} catch {{ $false }} }}); if ($null -eq $main -and $samePath.Count -eq 0) {{ exit 0 }}; Start-Sleep -Milliseconds 100 }} while ([DateTime]::UtcNow -lt $deadline); exit 1"
    );
    let mut command = Command::new("powershell.exe");
    command
        .args(["-NoProfile", "-NonInteractive", "-Command", &command_text])
        .creation_flags(CREATE_NO_WINDOW);
    let status = command.status()?;
    if status.success() {
        Ok(())
    } else {
        Err(AppError::Worker("Timed out waiting for all application processes to exit before updating".into()))
    }
}

fn relaunch_application_if_needed(root: &Path, elevated: bool) -> AppResult<()> {
    #[cfg(windows)]
    if application_is_running(&root.join("Pymss Studio.exe"))? {
        // A second copy can still be alive after an update attempt failed. Do not
        // launch another copy on top of it; repeated retries would otherwise
        // accumulate processes and keep the executable locked.
        return Ok(());
    }
    let _ = launch_application(root, elevated)?;
    Ok(())
}

#[cfg(not(windows))]
fn wait_for_process_exit(_pid: u32, _executable: &Path) -> AppResult<()> {
    Err(AppError::Worker("Managed updates are supported only on Windows".into()))
}

fn app_root_is_writable(root: &Path) -> bool {
    let probe = root.join(format!(".pymss-studio-update-probe-{}", std::process::id()));
    match fs::write(&probe, b"update") {
        Ok(()) => {
            let _ = fs::remove_file(probe);
            true
        }
        Err(_) => false,
    }
}

#[cfg(windows)]
fn spawn_elevated_helper(helper: &Path, args: &[std::ffi::OsString]) -> AppResult<()> {
    let arguments = args
        .iter()
        .map(|arg| quote_windows_arg(arg))
        .collect::<Vec<_>>()
        .join(" ");
    let mut command = Command::new("powershell.exe");
    command
        .args([
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            &format!(
                "$ErrorActionPreference = 'Stop'; $process = Start-Process -FilePath {} -ArgumentList {} -Verb RunAs -PassThru; if ($null -eq $process) {{ exit 1 }}",
                quote_windows_arg(helper.as_os_str()),
                quote_powershell_arg(&arguments),
            ),
        ])
        .creation_flags(CREATE_NO_WINDOW);
    let status = command.status()?;
    if !status.success() {
        return Err(AppError::Worker("Administrator permission is required to update this application directory".into()));
    }
    Ok(())
}

#[cfg(not(windows))]
fn spawn_elevated_helper(_helper: &Path, _args: &[std::ffi::OsString]) -> AppResult<()> {
    Err(AppError::Worker("This application directory requires elevated update permission".into()))
}

#[cfg(windows)]
fn read_inno_display_version() -> AppResult<String> {
    let output = Command::new("powershell.exe")
        .args([
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "(Get-ItemProperty -LiteralPath 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{6A208087-F154-4C62-8916-E3D40B7C0F24}_is1' -Name DisplayVersion -ErrorAction Stop).DisplayVersion",
        ])
        .creation_flags(CREATE_NO_WINDOW)
        .output()?;
    if !output.status.success() {
        return Err(AppError::Worker(format!(
            "Unable to read the Inno uninstall DisplayVersion (PowerShell exited with {})",
            output.status
        )));
    }
    let version = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if version.is_empty() {
        return Err(AppError::Worker("The Inno uninstall DisplayVersion is empty".into()));
    }
    Ok(version)
}

#[cfg(not(windows))]
fn read_inno_display_version() -> AppResult<String> {
    Err(AppError::Worker("Managed updates are supported only on Windows".into()))
}

#[cfg(windows)]
fn update_inno_display_version(version: &str) -> AppResult<()> {
    let status = Command::new("reg.exe")
        .args([
            "ADD",
            r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{6A208087-F154-4C62-8916-E3D40B7C0F24}_is1",
            "/v",
            "DisplayVersion",
            "/t",
            "REG_SZ",
            "/d",
            version,
            "/f",
            "/reg:64",
        ])
        .creation_flags(CREATE_NO_WINDOW)
        .status()?;
    if status.success() {
        Ok(())
    } else {
        Err(AppError::Worker(format!(
            "Unable to update the Inno uninstall DisplayVersion (reg.exe exited with {status})"
        )))
    }
}

#[cfg(not(windows))]
fn update_inno_display_version(_version: &str) -> AppResult<()> {
    Err(AppError::Worker("Managed updates are supported only on Windows".into()))
}

#[cfg(windows)]
fn quote_windows_arg(value: &std::ffi::OsStr) -> String {
    format!("\"{}\"", value.to_string_lossy().replace('"', "\\\""))
}

#[cfg(windows)]
fn quote_powershell_arg(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

fn restore_portable_backup(root: &Path, backup: &Path) -> AppResult<()> {
    for name in MANAGED_PATHS {
        let previous = backup.join(name);
        // A transaction may be interrupted before this path is moved. Never delete its original file.
        if !previous.exists() {
            continue;
        }
        let replacement = root.join(name);
        if name == "Pymss Studio.exe" && replacement.exists() {
            replace_executable(&replacement, &previous, None)?;
            continue;
        }
        if replacement.exists() {
            if replacement.is_dir() {
                fs::remove_dir_all(&replacement)?;
            } else {
                fs::remove_file(&replacement)?;
            }
        }
        fs::rename(previous, replacement)?;
    }
    Ok(())
}

fn rollback_partial_replacement(
    root: &Path,
    backup: &Path,
    replaced: &[&str],
    moved: &[&str],
) -> AppResult<()> {
    let mut failures = Vec::new();
    for name in replaced {
        let replacement = root.join(name);
        if !replacement.exists() {
            continue;
        }
        if *name == "Pymss Studio.exe" {
            continue;
        }
        let result = if replacement.is_dir() {
            fs::remove_dir_all(&replacement)
        } else {
            fs::remove_file(&replacement)
        };
        if let Err(error) = result {
            failures.push(format!("unable to remove replacement {name}: {error}"));
        }
    }
    for name in moved {
        let previous = backup.join(name);
        let destination = root.join(name);
        if !previous.exists() {
            continue;
        }
        if destination.exists() {
            if *name == "Pymss Studio.exe" {
                if let Err(error) = replace_executable(&destination, &previous, None) {
                    failures.push(format!("unable to restore {name}: {error}"));
                }
                continue;
            }
            failures.push(format!("unable to restore {name}: replacement still exists"));
            continue;
        }
        if let Err(error) = fs::rename(previous, destination) {
            failures.push(format!("unable to restore {name}: {error}"));
        }
    }
    if !failures.is_empty() {
        return Err(AppError::Worker(failures.join("; ")));
    }
    Ok(())
}

fn rollback_managed_update(
    root: &Path,
    backup: &Path,
    previous_inno_version: Option<&str>,
) -> AppResult<()> {
    restore_portable_backup(root, backup)?;
    if let Some(version) = previous_inno_version {
        update_inno_display_version(version)?;
    }
    remove_update_transaction(root)?;
    let _ = fs::remove_dir_all(backup);
    Ok(())
}

#[cfg(test)]
#[path = "update_recovery_tests.rs"]
mod recovery_tests;

#[cfg(test)]
mod tests {
    #[test]
    fn repair_rejects_upgrade_downgrade_and_build_substitution() {
        assert!(super::validate_repair_version("1.2.3", "1.2.3").is_ok());
        for offered in ["1.2.4", "1.2.2", "1.2.3+other", "1.2.3-rc.1", "invalid"] {
            assert!(super::validate_repair_version("1.2.3", offered).is_err());
        }
    }

    #[test]
    fn application_update_rejects_native_tasks_and_additional_windows() {
        let state = crate::state::AppState::new();
        assert!(super::ensure_update_idle(&state, false).is_ok());
        assert!(super::ensure_update_idle(&state, true).unwrap_err().to_string().contains("APPLICATION_UPDATE_WINDOWS"));
        state.native_tasks.lock().unwrap().insert("audio-task".into(), std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false)));
        assert!(super::ensure_update_idle(&state, false).unwrap_err().to_string().contains("APPLICATION_UPDATE_BUSY"));
        state.native_tasks.lock().unwrap().clear();
        assert!(super::ensure_update_idle(&state, false).is_ok());
    }

    use super::{
        distribution_at, elevated_helper_args, is_elevation_required, managed_endpoint,
        pending_archive_path, read_update_transaction, restore_portable_backup,
        update_archive_path, update_auto_install_supported, update_manual_install_message,
        update_requires_manual_install, update_stage_path, validate_payload_path,
        validate_update_version, write_update_transaction, DistributionKind, UpdateChannel,
        UpdatePhase, ERROR_ELEVATION_REQUIRED, MAX_UPDATE_ARCHIVE_BYTES,
    };
    use serde_json::json;
    use base64::Engine;
    use std::fs;
    use std::path::{Path, PathBuf};

    #[test]
    fn update_payload_rejects_unmanaged_paths() {
        assert!(validate_payload_path(Path::new("data/settings/app.json")).is_err());
        assert!(validate_payload_path(Path::new("../Pymss Studio.exe")).is_err());
        assert!(validate_payload_path(Path::new("python/../worker.py")).is_err());
        assert!(validate_payload_path(Path::new("python/worker.py")).is_ok());
    }

    #[test]
    fn update_version_must_be_semver_before_it_is_used_in_paths() {
        assert!(validate_update_version("1.2.3").is_ok());
        assert!(validate_update_version("1.2.3-rc.1+build.4").is_ok());
        assert!(validate_update_version("../data").is_err());
        assert!(validate_update_version("1.2.3/../../data").is_err());
    }

    #[test]
    fn update_archive_limit_matches_release_gate() {
        assert_eq!(MAX_UPDATE_ARCHIVE_BYTES, 512 * 1024 * 1024);
    }

    #[test]
    fn helper_modes_can_be_retried_with_elevation() {
        for (mode, elevated_mode) in [
            ("--apply-managed-update", "--apply-managed-update-elevated"),
            (
                "--recover-managed-update",
                "--recover-managed-update-elevated",
            ),
        ] {
            let args = vec![mode.into(), "1234".into()];
            let elevated_args = elevated_helper_args(&args).unwrap();
            assert_eq!(elevated_args[0], std::ffi::OsStr::new(elevated_mode));
            assert_eq!(elevated_args[1], std::ffi::OsStr::new("1234"));
        }
        assert!(elevated_helper_args(&["--unknown".into()]).is_none());
    }

    #[cfg(windows)]
    #[test]
    fn windows_error_740_requests_elevation_fallback() {
        assert!(is_elevation_required(&std::io::Error::from_raw_os_error(
            ERROR_ELEVATION_REQUIRED,
        )));
        assert!(!is_elevation_required(&std::io::Error::from_raw_os_error(5)));
    }

    #[test]
    fn manual_install_metadata_disables_in_place_updates() {
        let payload = json!({
            "pymss_update_supported": false,
            "pymss_requires_manual_install": true,
            "pymss_update_message": "Install this release from GitHub.",
        });
        assert!(!update_auto_install_supported(&payload));
        assert!(update_requires_manual_install(&payload));
        assert_eq!(update_manual_install_message(&payload), "Install this release from GitHub.");
    }

    #[test]
    fn missing_manual_install_metadata_fails_closed() {
        let payload = json!({"version": "1.2.3"});
        assert!(!update_auto_install_supported(&payload));
        assert!(update_requires_manual_install(&payload));
    }

    #[test]
    fn managed_updates_use_the_v1_endpoint() {
        assert_eq!(
            managed_endpoint(UpdateChannel::Stable).as_str(),
            format!("https://github.com/pymss-project/pymss-studio/releases/download/updater/{}", crate::update_sources::metadata_name("stable", "v1")),
        );
    }

    #[test]
    fn source_selection_prefers_new_versions_before_response_speed() {
        use std::time::Duration;
        assert!(super::release_is_preferred("0.0.21", Duration::from_secs(3), "0.0.20", Duration::from_millis(10)));
        assert!(!super::release_is_preferred("0.0.19", Duration::from_millis(1), "0.0.20", Duration::from_secs(3)));
        assert!(super::release_is_preferred("0.0.20", Duration::from_millis(10), "0.0.20", Duration::from_secs(1)));
        assert!(super::release_is_preferred("1.0.0", Duration::from_secs(2), "1.0.0-rc.1", Duration::from_millis(1)));
        assert!(!super::release_is_preferred("invalid", Duration::ZERO, "0.0.20", Duration::ZERO));
    }

    #[test]
    fn stable_channel_rejects_prerelease_versions_and_metadata() {
        for (version, metadata) in [
            ("0.0.21-rc.1", json!({"channel": "stable", "prerelease": false})),
            ("0.0.21-rc.1", json!({"channel": "stable", "prerelease": true})),
            ("0.0.21", json!({"channel": "stable", "prerelease": true})),
            ("0.0.21", json!({"channel": "prerelease", "prerelease": false})),
        ] {
            assert!(super::validate_update_channel(UpdateChannel::Stable, version, &metadata).is_err());
        }
        assert!(super::validate_update_channel(
            UpdateChannel::Stable, "0.0.21+build.1", &json!({"channel": "stable", "prerelease": false}),
        ).is_ok());
    }

    #[test]
    fn channel_metadata_must_match_the_requested_channel() {
        for metadata in [
            json!({}),
            json!({"prerelease": false}),
            json!({"channel": "stable"}),
            json!({"channel": "stable", "prerelease": "false"}),
            json!({"channel": "prerelease", "prerelease": true}),
        ] {
            assert!(super::validate_update_channel(UpdateChannel::Stable, "0.0.21", &metadata).is_err());
        }
        assert!(super::validate_update_channel(
            UpdateChannel::Prerelease, "0.0.21", &json!({"channel": "stable", "prerelease": false}),
        ).is_err());
    }

    #[test]
    fn prerelease_channel_accepts_prerelease_and_stable_releases() {
        for (version, prerelease) in [("0.0.21-rc.1", true), ("0.0.21", true), ("0.0.21", false)] {
            assert!(super::validate_update_channel(
                UpdateChannel::Prerelease, version, &json!({"channel": "prerelease", "prerelease": prerelease}),
            ).is_ok());
        }
        assert!(super::validate_update_channel(
            UpdateChannel::Prerelease, "0.0.21-rc.1", &json!({"channel": "prerelease", "prerelease": false}),
        ).is_err());
    }

    #[test]
    fn release_signature_cannot_be_reused_for_another_version() {
        let signature = "untrusted comment: signature from tauri secret key\nRUSwXCRg9i11qZVeGcC91Q+ga6LNd9cnA5apzJD88EF9u5pWcmzJSRISDih2gqePAAT6XxRH4N73pv9RKnYnQ8E4Pca958Nf+Ag=\ntrusted comment: timestamp:1792132803\tfile:Pymss-Studio-0.0.20-windows-x64-update.zip\nKgHDaCK/VlKdFjhEV5Z/G+YmfoHBwkhRzSiwXXh/ZCcxmp6BDLxEw/9MucO2waR2s2FWAUa+0h1isXVdymJHBw==\n";
        // This test checks filename binding before cryptographic verification.
        let signature = signature.replace("Pymss-Studio-0.0.20-windows-x64-update.zip", &crate::update_sources::archive_name("0.0.20"));
        let encoded = base64::engine::general_purpose::STANDARD.encode(&signature);
        assert!(super::decode_release_signature(&encoded, "0.0.20").is_ok());
        assert!(super::decode_release_signature(&encoded, "99.0.0").is_err());
        let duplicate = signature.replace("\nKgHD", "\tfile:Pymss-Studio-0.0.20-windows-x64-update.zip\nKgHD");
        assert!(super::decode_release_signature(&base64::engine::general_purpose::STANDARD.encode(duplicate), "0.0.20").is_err());
    }

    #[test]
    fn automatic_download_fallbacks_keep_the_same_release_and_explicit_sources_stay_explicit() {
        use crate::update_sources::{UpdateProvider, UpdateSource};
        let original = url::Url::parse(&format!("https://github.com/pymss-project/pymss-studio/releases/download/v0.0.20/{}", crate::update_sources::archive_name("0.0.20"))).unwrap();
        let candidates = super::download_candidates("0.0.20", &original, UpdateProvider::Github, UpdateSource::Auto).unwrap();
        assert_eq!(candidates.len(), 2);
        assert_eq!(candidates[0], (UpdateProvider::Github, original.clone()));
        assert_eq!(candidates[1].0, UpdateProvider::Modelscope);
        let manual = super::download_candidates("0.0.20", &original, UpdateProvider::Modelscope, UpdateSource::Modelscope).unwrap();
        assert_eq!(manual, vec![candidates[1].clone()]);
        assert!(super::download_candidates("0.0.21", &original, UpdateProvider::Modelscope, UpdateSource::Auto).is_err());
        let custom = url::Url::parse("http://localhost/custom.zip").unwrap();
        assert!(super::download_candidates("0.0.20", &custom, UpdateProvider::Modelscope, UpdateSource::Modelscope).is_err());
        assert_eq!(super::download_candidates("0.0.20", &custom, UpdateProvider::Custom, UpdateSource::Auto).unwrap(), vec![(UpdateProvider::Custom, custom)]);
    }

    #[test]
    #[ignore = "Downloads and verifies the public signed update ZIP without installing it"]
    fn live_modelscope_signed_package() {
        tauri::async_runtime::block_on(async {
            let client = crate::update_sources::http_client_builder().user_agent("Pymss-Studio-Updater").timeout(std::time::Duration::from_secs(45)).build().unwrap();
            let manifest = crate::update_sources::modelscope_manifest(&client, "prerelease-v1-windows-x64-update.json").await.unwrap();
            let raw: serde_json::Value = client.get(manifest.endpoint).send().await.unwrap().json().await.unwrap();
            let version = raw["version"].as_str().unwrap();
            let signature = raw["platforms"]["windows-x86_64"]["signature"].as_str().unwrap();
            let original = url::Url::parse(raw["platforms"]["windows-x86_64"]["url"].as_str().unwrap()).unwrap();
            let mirror = crate::update_sources::archive_url(&original, version, crate::update_sources::UpdateProvider::Modelscope).unwrap();
            let bytes = client.get(mirror).header(reqwest::header::ACCEPT, "application/octet-stream").send().await.unwrap().error_for_status().unwrap().bytes().await.unwrap();
            super::verify_payload_signature(&bytes, signature, version).unwrap();
            assert!(super::verify_payload_signature(&bytes, signature, "99.0.0").is_err());
            let text = String::from_utf8(base64::engine::general_purpose::STANDARD.decode(signature).unwrap()).unwrap();
            let tampered = text.replace(&format!("file:{}", crate::update_sources::archive_name(version)), &format!("file:{}", crate::update_sources::archive_name("99.0.0")));
            let tampered = base64::engine::general_purpose::STANDARD.encode(tampered);
            assert!(super::verify_payload_signature(&bytes, &tampered, "99.0.0").is_err());
            println!("Verified ModelScope update {version}: {} bytes; replay and comment tampering rejected", bytes.len());
        });
    }

    #[test]
    fn update_transaction_can_advance_from_prepared_to_replacing() {
        let root = std::env::temp_dir().join(format!(
            "pymss-update-transaction-test-{}",
            std::process::id()
        ));
        let backup = root.join(".pymss-studio-update-backup-test");
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();

        write_update_transaction(&root, &backup, UpdatePhase::Prepared).unwrap();
        write_update_transaction(&root, &backup, UpdatePhase::Replacing).unwrap();

        let transaction = read_update_transaction(&root).unwrap().unwrap();
        assert_eq!(transaction.phase, UpdatePhase::Replacing);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn update_payload_stage_stays_in_application_directory() {
        let root = PathBuf::from("C:/Pymss Studio");
        let stage = update_stage_path(&root, "1.2.3");
        assert_eq!(stage.parent().and_then(Path::parent), Some(root.as_path()));
        assert_eq!(stage.parent().and_then(Path::file_name), Some(std::ffi::OsStr::new("update")));
    }

    #[test]
    fn update_archive_stays_in_application_update_directory() {
        let root = PathBuf::from("C:/Pymss Studio");
        let archive = update_archive_path(&root, "1.2.3");
        assert_eq!(archive.parent().and_then(Path::parent), Some(root.as_path()));
        assert_eq!(archive.parent().and_then(Path::file_name), Some(std::ffi::OsStr::new("update")));
        assert_eq!(archive.extension(), Some(std::ffi::OsStr::new("zip")));
    }

    #[test]
    fn protected_update_uses_a_stable_pending_archive_name() {
        assert_eq!(pending_archive_path().file_name().and_then(|name| name.to_str()), Some("pymss-studio-pending.zip"));
    }

    #[test]
    fn only_marked_directories_are_managed_update_targets() {
        let root = std::env::temp_dir().join(format!("pymss-studio-update-target-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        assert!(distribution_at(&root).is_none());

        fs::write(root.join("pymss-studio.portable"), "portable").unwrap();
        assert!(matches!(distribution_at(&root), Some(DistributionKind::Portable)));

        fs::write(root.join("pymss-studio.inno-install"), "managed").unwrap();
        assert!(matches!(distribution_at(&root), Some(DistributionKind::Inno)));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn interrupted_update_restores_only_paths_that_were_backed_up() {
        let root = std::env::temp_dir().join(format!("pymss-studio-recovery-{}", std::process::id()));
        let backup = root.join(".pymss-studio-update-backup-test");
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("python")).unwrap();
        fs::create_dir_all(&backup).unwrap();
        fs::write(root.join("python").join("original.txt"), "original").unwrap();
        fs::create_dir_all(root.join("bin")).unwrap();
        fs::write(root.join("bin").join("original.txt"), "original-bin").unwrap();
        fs::write(backup.join("Pymss Studio.exe"), "old-exe").unwrap();

        restore_portable_backup(&root, &backup).unwrap();

        assert_eq!(fs::read_to_string(root.join("Pymss Studio.exe")).unwrap(), "old-exe");
        assert_eq!(fs::read_to_string(root.join("python").join("original.txt")).unwrap(), "original");
        assert_eq!(fs::read_to_string(root.join("bin").join("original.txt")).unwrap(), "original-bin");
        let _ = fs::remove_dir_all(root);
    }
}
