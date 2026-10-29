use crate::error::{AppError, AppResult};
use std::fs;
use std::io::Read;
use std::path::Path;

fn failure(message: impl Into<String>) -> AppError {
    AppError::Worker(message.into())
}

fn validate_image(path: &Path) -> AppResult<()> {
    let mut header = [0; 64];
    fs::File::open(path)?.read_exact(&mut header)?;
    if &header[..4] != b"\x7fELF"
        || header[4] != 2
        || header[5] != 1
        || &header[8..11] != b"AI\x02"
        || u16::from_le_bytes([header[18], header[19]]) != 62
    {
        return Err(failure("The update is not a Linux x86_64 type 2 AppImage"));
    }
    Ok(())
}

// Both paths are on the application's filesystem. Preserve the old inode before
// a single rename replaces the name; a running AppImage keeps its existing mount.
fn replace_image(target: &Path, staged: &Path, backup: &Path) -> AppResult<()> {
    if fs::symlink_metadata(backup).is_ok() {
        return Err(failure("The application backup path already exists"));
    }
    if fs::hard_link(target, backup).is_err() {
        fs::copy(target, backup)?;
        fs::File::open(backup)?.sync_all()?;
    }
    fs::rename(staged, target)?;
    Ok(())
}

fn restore_image(target: &Path, backup: &Path) -> AppResult<()> {
    fs::rename(backup, target)?;
    Ok(())
}

#[cfg(target_os = "linux")]
mod platform {
    use super::*;
    use crate::appimage_environment::clean_environment;
    #[cfg(test)]
    use crate::appimage_environment::clean_value;
    use crate::update_sources::{UpdateProvider, UpdateSource};
    use crate::update_stream::VerifiedStream;
    use crate::{
        python::worker, storage, update_manager, update_sources, update_startup as startup,
    };
    use std::ffi::{CString, OsStr};
    use std::io::Write;
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    use std::os::unix::process::CommandExt;
    use std::path::PathBuf;
    use std::process::{Child, Command, Stdio};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::time::{Duration, Instant};
    use tauri::{AppHandle, Emitter, Manager};
    use tauri_plugin_updater::Update;

    static RUNNING: AtomicBool = AtomicBool::new(false);
    const MAX_BYTES: u64 = 2 * 1024 * 1024 * 1024 - 1;
    const EVENT: &str = update_manager::UPDATE_EVENT;
    struct UpdateGuard;
    impl Drop for UpdateGuard {
        fn drop(&mut self) {
            RUNNING.store(false, Ordering::Release);
        }
    }

    #[derive(PartialEq, Eq)]
    struct Identity {
        device: u64,
        inode: u64,
        size: u64,
        modified: (i64, i64),
    }
    fn identity(path: &Path) -> AppResult<Identity> {
        let metadata = fs::symlink_metadata(path)?;
        if !metadata.is_file() {
            return Err(failure("The installed AppImage is not a regular file"));
        }
        Ok(Identity {
            device: metadata.dev(),
            inode: metadata.ino(),
            size: metadata.len(),
            modified: (metadata.mtime(), metadata.mtime_nsec()),
        })
    }

    pub fn current_image() -> AppResult<PathBuf> {
        let path = std::env::var_os("APPIMAGE")
            .ok_or_else(|| failure("Launch the installed AppImage before updating"))?;
        let path = Path::new(&path).canonicalize()?;
        identity(&path)?;
        validate_image(&path)?;
        Ok(path)
    }

    pub fn check_location(path: &Path) -> AppResult<()> {
        let metadata = fs::symlink_metadata(path)?;
        // No privilege escalation is attempted for system-managed installations.
        if !metadata.is_file() || metadata.uid() != unsafe { libc::geteuid() } {
            return Err(failure("Move the AppImage to a directory owned by your user, then reopen it to update automatically"));
        }
        let parent = path
            .parent()
            .ok_or_else(|| failure("Invalid AppImage location"))?;
        let probe = tempfile::Builder::new().prefix(".pymss-update-check-").tempfile_in(parent)
            .map_err(|error| failure(format!("The AppImage directory is not writable. Download and replace it manually: {error}")))?;
        probe.close()?;
        Ok(())
    }

    fn require_space(path: &Path, bytes: u64) -> AppResult<()> {
        let path = CString::new(path.as_os_str().as_bytes())
            .map_err(|_| failure("Invalid application directory"))?;
        let mut info = std::mem::MaybeUninit::<libc::statvfs>::uninit();
        // SAFETY: the C string and output buffer remain valid for statvfs.
        if unsafe { libc::statvfs(path.as_ptr(), info.as_mut_ptr()) } != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        let info = unsafe { info.assume_init() };
        if (info.f_bavail as u64).saturating_mul(info.f_frsize as u64)
            < bytes.saturating_add(256 * 1024 * 1024)
        {
            return Err(failure(
                "There is not enough free disk space to download and install this update",
            ));
        }
        Ok(())
    }

    async fn download(
        app: &AppHandle,
        update: &Update,
        url: url::Url,
        file: &Path,
        old_size: u64,
    ) -> AppResult<()> {
        if url.scheme() != "https" {
            return Err(failure("AppImage update downloads require HTTPS"));
        }
        let signature =
            update_manager::decode_release_signature(&update.signature, &update.version)?;
        let key = update_manager::update_public_key()?;
        let mut verifier = VerifiedStream::new(&key, &signature)?;
        let (direct, proxy) = update_manager::updater_proxy(app)?;
        let client = update_manager::configure_update_client(
            update_sources::http_client_builder(),
            direct,
            proxy,
        )
        .user_agent("Pymss-Studio-Updater")
        .build()
        .map_err(|e| failure(e.without_url().to_string()))?;
        let mut response = client
            .get(url)
            .send()
            .await
            .map_err(|e| failure(e.without_url().to_string()))?
            .error_for_status()
            .map_err(|e| failure(e.without_url().to_string()))?;
        if response.url().scheme() != "https" {
            return Err(failure(
                "AppImage updates cannot redirect to an insecure connection",
            ));
        }
        let total = response.content_length();
        if total.is_some_and(|size| size == 0 || size > MAX_BYTES) {
            return Err(failure(
                "The AppImage update exceeds the supported size limit",
            ));
        }
        require_space(
            file.parent().unwrap(),
            total.unwrap_or(MAX_BYTES).saturating_add(old_size),
        )?;
        let mut output = fs::File::create(file)?;
        let mut received = 0_u64;
        let started = Instant::now();
        let mut last_event = Instant::now();
        let _ = app.emit(
            EVENT,
            serde_json::json!({"event":"Started","data":{"contentLength":total}}),
        );
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|e| failure(e.without_url().to_string()))?
        {
            received = received.saturating_add(chunk.len() as u64);
            if received > MAX_BYTES {
                return Err(failure(
                    "The AppImage update exceeds the supported size limit",
                ));
            }
            verifier.update(&chunk);
            output.write_all(&chunk)?;
            if last_event.elapsed() >= Duration::from_millis(200) {
                let _ = app.emit(EVENT, serde_json::json!({"event":"Progress","data":{
                    "downloadedBytes":received,"contentLength":total,"speedBytesPerSecond":received as f64 / started.elapsed().as_secs_f64().max(0.001)
                }}));
                last_event = Instant::now();
            }
        }
        if received == 0 || total.is_some_and(|size| size != received) {
            return Err(failure("The AppImage update download is incomplete"));
        }
        verifier.finish()?;
        output.sync_all()?;
        drop(output);
        validate_image(file)?;
        let _ = app.emit(EVENT, serde_json::json!({"event":"Finished"}));
        Ok(())
    }

    fn extracted_mount(mount: Option<&OsStr>) -> bool {
        mount
            .and_then(|path| Path::new(path).file_name())
            .is_some_and(|name| name.as_bytes().starts_with(b"appimage_extracted_"))
    }

    fn launch(
        target: &Path,
        request: &startup::Request,
        log: &Path,
        data_root: &Path,
    ) -> AppResult<Child> {
        let mut command = Command::new(target);
        let mount = std::env::var_os("APPDIR");
        clean_environment(&mut command, mount.as_deref());
        // The runtime consumes the CLI extraction switch without exporting it.
        // Keep this mode when a FUSE-less installation restarts into the update.
        if extracted_mount(mount.as_deref()) {
            command.env("APPIMAGE_EXTRACT_AND_RUN", "1");
        }
        command
            .args(["--pymss-update-startup", &request.token])
            .current_dir(target.parent().unwrap())
            .env("PYMSS_STUDIO_DATA_ROOT", data_root)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(fs::OpenOptions::new().append(true).create(true).open(log)?)
            .process_group(0);
        crate::single_instance::configure_update_child(&mut command).map_err(failure)?;
        Ok(command.spawn()?)
    }

    fn stop_replacement(child: &mut Child) -> AppResult<()> {
        // The AppImage extraction wrapper may own a child process running the UI.
        // Stop the entire dedicated process group before restoring the old file.
        let result = unsafe { libc::kill(-(child.id() as i32), libc::SIGKILL) };
        if result != 0 && std::io::Error::last_os_error().raw_os_error() != Some(libc::ESRCH) {
            return Err(std::io::Error::last_os_error().into());
        }
        child.wait()?;
        Ok(())
    }

    pub fn confirm_startup(app: &AppHandle) -> AppResult<()> {
        static CONFIRMED: std::sync::Mutex<bool> = std::sync::Mutex::new(false);
        let mut confirmed = CONFIRMED
            .lock()
            .map_err(|_| failure("Startup confirmation lock poisoned"))?;
        if *confirmed {
            return Ok(());
        }
        let Some(token) = startup::argument_token(std::env::args().skip(1))? else {
            return Ok(());
        };
        if !token.starts_with("linux-update-startup-") {
            return Err(failure("Invalid Linux update startup token"));
        }
        let directory = storage::temp_dir(app)?.join(&token);
        let metadata = fs::symlink_metadata(&directory)?;
        if !metadata.is_dir()
            || metadata.uid() != unsafe { libc::geteuid() }
            || metadata.mode() & 0o077 != 0
        {
            return Err(failure(
                "The application startup transaction directory is invalid",
            ));
        }
        let request = startup::read_request(&directory)?;
        if request.target != current_image()?
            || request.version != env!("CARGO_PKG_VERSION")
            || request.privileged
        {
            return Err(failure(
                "The application startup request does not match the running version",
            ));
        }
        startup::acknowledge(&directory, &request, std::process::id())?;
        *confirmed = true;
        Ok(())
    }

    pub async fn start(
        app: &AppHandle,
        update: &Update,
        provider: UpdateProvider,
        source: UpdateSource,
    ) -> AppResult<()> {
        RUNNING
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| failure("Another application update is already in progress"))?;
        let _running = UpdateGuard;
        drop(worker::acquire_application_update()?);
        if app.webview_windows().keys().any(|label| label != "main") {
            return Err(failure("Save your changes and close additional editor windows before installing an application update"));
        }
        let target = current_image()?;
        check_location(&target)?;
        let original = identity(&target)?;
        let stage = tempfile::Builder::new()
            .prefix(".pymss-studio-update-")
            .tempdir_in(target.parent().unwrap())?;
        let image = stage.path().join("update.AppImage");
        let candidates = update_manager::download_candidates(
            &update.version,
            &update.download_url,
            provider,
            source,
        )?;
        let candidates = update_manager::order_download_candidates(app, candidates).await?;
        let mut last_error = None;
        let mut downloaded = false;
        for (index, (provider, url)) in candidates.into_iter().enumerate() {
            let _ = app.emit(EVENT, serde_json::json!({"event":"SourceSelected","data":{"source":provider,"fallback":index>0}}));
            match download(app, update, url, &image, original.size).await {
                Ok(()) => {
                    downloaded = true;
                    break;
                }
                Err(error) => last_error = Some(error),
            }
        }
        if !downloaded {
            return Err(
                last_error.unwrap_or_else(|| failure("No update download source is available"))
            );
        }
        let app = app.clone();
        let version = update.version.clone();
        tauri::async_runtime::spawn_blocking(move || -> AppResult<()> {
            let _workers = worker::acquire_application_update()?;
            if app.webview_windows().keys().any(|label| label != "main") { return Err(failure("Close additional editor windows before installing the application update")); }
            let state = app.state::<crate::state::AppState>();
            if !state.native_tasks.lock().map_err(|_| failure("Task registry lock poisoned"))?.is_empty()
                || !state.migrations.lock().map_err(|_| failure("Migration registry lock poisoned"))?.is_empty()
            { return Err(failure("Wait for running tasks and model migrations to finish before updating")); }
            check_location(&target)?;
            if identity(&target)? != original { return Err(failure("The installed AppImage changed. Check for updates again")); }
            let mode = fs::metadata(&target)?.permissions().mode() & 0o777;
            fs::set_permissions(&image, fs::Permissions::from_mode(mode | 0o100))?;
            let root = storage::temp_dir(&app)?;
            fs::create_dir_all(&root)?;
            let startup_directory = startup::create_directory(&root, "linux-update-startup-")?;
            let request = startup::Request { token: startup_directory.path().file_name().unwrap().to_str().unwrap().into(), target: target.clone(), version: version.clone(), privileged: false };
            startup::save_request(startup_directory.path(), &request)?;
            let logs = storage::logs_dir(&app)?;
            fs::create_dir_all(&logs)?;
            let log = logs.join("linux-update.log");
            let backup = stage.path().join("previous.AppImage");
            replace_image(&target, &image, &backup)?;
            let mut child = None;
            let started = (|| -> AppResult<()> {
                fs::File::open(target.parent().unwrap())?.sync_all()?;
                child = Some(launch(&target, &request, &log, &storage::data_root_dir(&app)?.canonicalize()?)?);
                startup::wait_for_acknowledgement(child.as_mut().unwrap(), startup_directory.path(), &request, Duration::from_secs(60))
            })();
            if let Err(error) = started {
                if let Some(child) = child.as_mut() {
                    if let Err(stop) = stop_replacement(child) {
                        let kept = stage.keep();
                        return Err(failure(format!("Application startup failed: {error}. Unable to stop the replacement: {stop}. Previous AppImage preserved at {}", kept.join("previous.AppImage").display())));
                    }
                }
                if let Err(restore) = restore_image(&target, &backup) {
                    let kept = stage.keep();
                    return Err(failure(format!("Application startup failed: {error}. Restoration failed: {restore}. Previous AppImage preserved at {}", kept.join("previous.AppImage").display())));
                }
                return Err(failure(format!("Application startup failed: {error}. The previous version has been restored. See {} for details.", log.display())));
            }
            crate::session_log::append(&app, "INFO", "update.restart", vec![("version", version)]);
            if let Err(error) = stage.close() { crate::session_log::early("WARN", "update.diagnostic", &format!("Unable to clean the AppImage update directory: {error}")); }
            if let Err(error) = startup_directory.close() { crate::session_log::early("WARN", "update.diagnostic", &format!("Unable to clean the update startup directory: {error}")); }
            app.exit(0);
            Ok(())
        }).await.map_err(|error| failure(error.to_string()))?
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn linux_update_removes_old_mount_paths_without_discarding_user_entries() {
            let mount = Path::new("/tmp/.mount_Studio");
            assert_eq!(
                clean_value(
                    OsStr::new("/tmp/.mount_Studio/usr/lib:/home/user/lib:/usr/lib"),
                    mount
                ),
                Some("/home/user/lib:/usr/lib".into())
            );
            assert_eq!(
                clean_value(
                    OsStr::new("/tmp/.mount_Studio/usr/share/glib-2.0/schemas"),
                    mount
                ),
                None
            );
            assert_eq!(
                clean_value(OsStr::new("/tmp/.mount_Studio-other/lib"), mount),
                Some("/tmp/.mount_Studio-other/lib".into())
            );
            assert_eq!(
                clean_value(OsStr::new("https://example.com/releases"), mount),
                Some("https://example.com/releases".into())
            );
        }

        #[test]
        fn linux_update_checks_directory_writability_before_downloading() {
            let root = tempfile::tempdir().unwrap();
            let image = root.path().join("Studio.AppImage");
            fs::write(&image, b"image").unwrap();
            check_location(&image).unwrap();
            if unsafe { libc::geteuid() } == 0 {
                return;
            }
            fs::set_permissions(root.path(), fs::Permissions::from_mode(0o500)).unwrap();
            let result = check_location(&image);
            fs::set_permissions(root.path(), fs::Permissions::from_mode(0o700)).unwrap();
            assert!(result.is_err());
        }

        #[test]
        fn linux_update_retains_cli_extract_and_run_mode() {
            assert!(extracted_mount(Some(OsStr::new(
                "/tmp/appimage_extracted_123456"
            ))));
            assert!(!extracted_mount(Some(OsStr::new("/tmp/.mount_Pymss123"))));
            assert!(!extracted_mount(None));
        }
    }
}

#[cfg(target_os = "linux")]
pub use platform::{check_location, confirm_startup, current_image, start};

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn linux_update_accepts_only_x64_type_two_images() {
        let root = tempfile::tempdir().unwrap();
        let file = root.path().join("update.AppImage");
        let mut header = [0_u8; 64];
        header[..6].copy_from_slice(b"\x7fELF\x02\x01");
        header[8..11].copy_from_slice(b"AI\x02");
        header[18] = 62;
        fs::write(&file, header).unwrap();
        validate_image(&file).unwrap();
        header[18] = 183;
        fs::write(&file, header).unwrap();
        assert!(validate_image(&file).is_err());
        fs::write(&file, b"incomplete download").unwrap();
        assert!(validate_image(&file).is_err());
    }

    #[test]
    fn linux_update_replaces_and_restores_the_exact_previous_image() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("Application with spaces.AppImage");
        let staged = root.path().join("new.AppImage");
        let backup = root.path().join("previous.AppImage");
        fs::write(&target, b"old image").unwrap();
        fs::write(&staged, b"new image").unwrap();
        replace_image(&target, &staged, &backup).unwrap();
        assert_eq!(fs::read(&target).unwrap(), b"new image");
        assert_eq!(fs::read(&backup).unwrap(), b"old image");
        restore_image(&target, &backup).unwrap();
        assert_eq!(fs::read(&target).unwrap(), b"old image");
    }

    #[test]
    fn linux_update_failed_replacement_keeps_the_original_file() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("AppImage");
        let backup = root.path().join("previous");
        fs::write(&target, b"old image").unwrap();
        assert!(replace_image(&target, &root.path().join("missing"), &backup).is_err());
        assert_eq!(fs::read(&target).unwrap(), b"old image");
        assert_eq!(fs::read(&backup).unwrap(), b"old image");
    }

    #[cfg(unix)]
    #[test]
    fn linux_update_keeps_the_running_inode_and_restores_executable_permissions() {
        use std::os::unix::fs::PermissionsExt;
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("AppImage");
        let staged = root.path().join("new");
        let backup = root.path().join("previous");
        fs::write(&target, b"running image").unwrap();
        fs::set_permissions(&target, fs::Permissions::from_mode(0o751)).unwrap();
        let mut mounted = fs::File::open(&target).unwrap();
        fs::write(&staged, b"updated image").unwrap();
        replace_image(&target, &staged, &backup).unwrap();
        let mut original = Vec::new();
        mounted.read_to_end(&mut original).unwrap();
        assert_eq!(original, b"running image");
        restore_image(&target, &backup).unwrap();
        assert_eq!(
            fs::metadata(&target).unwrap().permissions().mode() & 0o777,
            0o751
        );
    }
}
