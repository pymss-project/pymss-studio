//! Cross-process leases for resources also opened by the Studio CLI.
use crate::error::{AppError, AppResult};
use std::{
    fs::{self, File},
    path::Path,
};

fn lease(file: File, exclusive: bool) -> AppResult<File> {
    let result = if exclusive {
        file.try_lock()
    } else {
        file.try_lock_shared()
    };
    match result {
        Ok(()) => Ok(file),
        Err(std::fs::TryLockError::WouldBlock) => Err(AppError::Worker(
            "RUNTIME_BUSY: Studio or its command line is using this resource. Retry after the operation finishes.".into()
        )),
        Err(std::fs::TryLockError::Error(e)) => Err(e.into()),
    }
}

pub(crate) fn resource(root: &Path, name: &str, exclusive: bool) -> AppResult<File> {
    fs::create_dir_all(root)?;
    let file = File::options()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(root.join(name))?;
    lease(file, exclusive)
}

pub(crate) const RUNTIME: &str = ".pymss-cli-runtime.lock";
/// Written by the CLI while it waits for exclusive runtime access. Studio answers it by stopping
/// an idle inference process, which keeps a shared runtime lease while its modules stay loaded.
pub(crate) const RUNTIME_RELEASE: &str = ".pymss-cli-runtime.release";
/// A request older than this was left behind by a CLI process that no longer waits for it.
// Read by Studio; the standalone CLI binary only writes requests.
#[cfg_attr(feature = "studio-cli", allow(dead_code))]
pub(crate) const RUNTIME_RELEASE_TTL: std::time::Duration = std::time::Duration::from_secs(30);

// Used by the CLI, which the Windows and macOS app binary does not include.
#[cfg_attr(not(feature = "studio-cli"), allow(dead_code))]
fn is_busy(result: &AppResult<File>) -> bool {
    matches!(result, Err(AppError::Worker(message)) if message.starts_with("RUNTIME_BUSY:"))
}

/// Runtime lease for the CLI. An exclusive request first asks Studio to release its idle
/// inference process and waits briefly before reporting that the runtime is busy.
#[cfg_attr(not(feature = "studio-cli"), allow(dead_code))]
pub(crate) fn runtime(root: &Path, exclusive: bool) -> AppResult<File> {
    let result = resource(root, RUNTIME, exclusive);
    if !exclusive || !is_busy(&result) {
        return result;
    }
    let request = root.join(RUNTIME_RELEASE);
    let _ = File::create(&request);
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(15);
    let result = loop {
        std::thread::sleep(std::time::Duration::from_millis(250));
        let result = resource(root, RUNTIME, true);
        if !is_busy(&result) || std::time::Instant::now() >= deadline {
            break result;
        }
    };
    let _ = fs::remove_file(&request);
    result
}

/// Whether a CLI process is currently waiting for exclusive runtime access.
#[cfg_attr(feature = "studio-cli", allow(dead_code))]
pub(crate) fn runtime_release_requested(root: &Path) -> bool {
    fs::metadata(root.join(RUNTIME_RELEASE))
        .and_then(|metadata| metadata.modified())
        .is_ok_and(|modified| modified.elapsed().is_ok_and(|age| age < RUNTIME_RELEASE_TTL))
}
pub(crate) const MIGRATION: &str = ".pymss-cli-migration.lock";
pub(crate) const MODELS: &str = ".pymss-cli-models.lock";

#[cfg(target_os = "macos")]
pub(crate) fn installed_application(target: &Path, exclusive: bool) -> AppResult<File> {
    let parent = target.parent().ok_or_else(|| {
        AppError::Worker("Could not locate the application installation directory.".into())
    })?;
    // The updater replaces the bundle or image inode. Its parent remains the
    // same throughout replacement and is accessible to every user of the app.
    lease(File::open(parent)?, exclusive)
}

#[cfg(target_os = "linux")]
pub(crate) fn installed_application(target: &Path, exclusive: bool) -> AppResult<File> {
    // All local users resolve the same lock, including when APPIMAGE is on NFS
    // or TMPDIR and the Studio data directory differ between processes.
    linux_installation_lease(Path::new("/tmp"), target, exclusive)
}

#[cfg(target_os = "linux")]
fn linux_installation_lease(lock_dir: &Path, target: &Path, exclusive: bool) -> AppResult<File> {
    use sha2::{Digest, Sha256};
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};

    let target = target.canonicalize()?;
    let key = Sha256::digest(target.as_os_str().as_encoded_bytes());
    let path = lock_dir.join(format!("pymss-studio-appimage-{key:x}.lock"));
    let open = || {
        File::options()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(&path)
    };
    let file = match open() {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            // Set permissions before publishing. Other accounts must be able to
            // open the same inode without relying on the creator's umask.
            let pending = tempfile::NamedTempFile::new_in(lock_dir)?;
            pending
                .as_file()
                .set_permissions(fs::Permissions::from_mode(0o644))?;
            match pending.persist_noclobber(&path) {
                Ok(file) => file,
                Err(error) if error.error.kind() == std::io::ErrorKind::AlreadyExists => open()?,
                Err(error) => return Err(error.error.into()),
            }
        }
        Err(error) => return Err(error.into()),
    };
    if !file.metadata()?.is_file() {
        return Err(AppError::Worker(
            "The AppImage installation lock is not a regular file.".into(),
        ));
    }
    let lock = lease(file, exclusive)?;
    // A temporary directory cleaner can remove an idle entry between open and
    // lock. Only use the lease if the pathname still resolves to this inode.
    let opened = lock.metadata()?;
    let named = fs::symlink_metadata(&path)?;
    if (opened.dev(), opened.ino()) != (named.dev(), named.ino()) {
        return Err(AppError::Worker(
            "The AppImage installation lock changed. Retry the operation.".into(),
        ));
    }
    Ok(lock)
}

#[cfg(windows)]
pub(crate) fn application(root: &Path, exclusive: bool) -> AppResult<Option<File>> {
    // Both distribution markers survive application replacement, including rollback.
    for name in ["pymss-studio.inno-install", "pymss-studio.portable"] {
        match File::open(root.join(name)) {
            Ok(file) => return lease(file, exclusive).map(Some),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
            Err(e) => return Err(e.into()),
        }
    }
    Ok(None)
}

#[cfg(all(test, windows))]
mod tests {
    #[test]
    fn an_exclusive_cli_request_asks_an_idle_holder_to_release_the_runtime() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().to_path_buf();
        let idle = super::resource(&root, super::RUNTIME, false).unwrap();
        assert!(!super::runtime_release_requested(&root));
        let holder = std::thread::spawn({
            let root = root.clone();
            move || {
                while !super::runtime_release_requested(&root) {
                    std::thread::sleep(std::time::Duration::from_millis(20));
                }
                drop(idle);
            }
        });

        let lease = super::runtime(&root, true).unwrap();

        holder.join().unwrap();
        assert!(!root.join(super::RUNTIME_RELEASE).exists());
        assert!(super::resource(&root, super::RUNTIME, false).is_err());
        drop(lease);
    }

    #[test]
    fn a_request_left_by_an_exited_cli_is_ignored() {
        let tmp = tempfile::tempdir().unwrap();
        let request = std::fs::File::create(tmp.path().join(super::RUNTIME_RELEASE)).unwrap();
        assert!(super::runtime_release_requested(tmp.path()));
        request
            .set_modified(std::time::SystemTime::now() - super::RUNTIME_RELEASE_TTL * 2)
            .unwrap();
        assert!(!super::runtime_release_requested(tmp.path()));
    }

    #[test]
    fn a_shared_cli_lease_does_not_ask_for_release() {
        let tmp = tempfile::tempdir().unwrap();
        let _idle = super::resource(tmp.path(), super::RUNTIME, false).unwrap();
        let _reader = super::runtime(tmp.path(), false).unwrap();
        assert!(!tmp.path().join(super::RUNTIME_RELEASE).exists());
    }

    #[test]
    fn users_share_the_marker_and_maintenance_excludes_them() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(tmp.path().join("pymss-studio.portable"), "portable").unwrap();
        let first = super::application(tmp.path(), false).unwrap();
        let second = super::application(tmp.path(), false).unwrap();
        assert!(super::application(tmp.path(), true).is_err());
        drop((first, second));
        assert!(super::application(tmp.path(), true).is_ok());
    }
}

#[cfg(all(test, unix))]
mod unix_tests {
    use super::*;

    #[cfg(target_os = "linux")]
    #[test]
    fn appimage_lease_uses_a_local_regular_file_per_image() {
        use std::os::unix::fs::symlink;

        let temp = tempfile::tempdir().unwrap();
        let install_dir = temp.path().join("images");
        let lock_dir = temp.path().join("local");
        fs::create_dir(&install_dir).unwrap();
        fs::create_dir(&lock_dir).unwrap();
        let first = install_dir.join("first.AppImage");
        let second = install_dir.join("second.AppImage");
        let alias = temp.path().join("alias.AppImage");
        fs::write(&first, b"first").unwrap();
        fs::write(&second, b"second").unwrap();
        symlink(&first, &alias).unwrap();

        let lock = linux_installation_lease(&lock_dir, &first, true).unwrap();
        assert!(linux_installation_lease(&lock_dir, &alias, false).is_err());
        let lock_path = fs::read_dir(&lock_dir)
            .unwrap()
            .next()
            .unwrap()
            .unwrap()
            .path();
        assert!(lock_path.is_file());
        assert!(linux_installation_lease(&lock_dir, &second, true).is_ok());
        assert_eq!(fs::read_dir(&install_dir).unwrap().count(), 2);

        drop(lock);
        fs::remove_file(&lock_path).unwrap();
        symlink(&second, &lock_path).unwrap();
        assert!(linux_installation_lease(&lock_dir, &first, false).is_err());
    }

    #[test]
    fn application_lease_survives_replacement_and_excludes_other_users_of_the_installation() {
        let temp = tempfile::tempdir().unwrap();
        let dir = temp.path().join("install");
        let other = temp.path().join("other");
        fs::create_dir(&dir).unwrap();
        fs::create_dir(&other).unwrap();
        let target = dir.join("Pymss Studio");
        let replacement = dir.join("replacement");
        let other_target = other.join("Pymss Studio");
        fs::write(&target, b"old").unwrap();
        fs::write(&replacement, b"new").unwrap();
        fs::write(&other_target, b"other").unwrap();
        #[cfg(target_os = "macos")]
        let take = installed_application;
        #[cfg(target_os = "linux")]
        let take = |path: &Path, exclusive| linux_installation_lease(temp.path(), path, exclusive);

        let reader = take(&target, false).unwrap();
        assert!(take(&target, false).is_ok());
        assert!(take(&target, true).is_err());
        assert!(take(&other_target, true).is_ok());
        fs::rename(&replacement, &target).unwrap();
        assert!(take(&target, true).is_err());
        drop(reader);

        let update = take(&target, true).unwrap();
        assert!(take(&target, false).is_err());
        drop(update);
        assert!(take(&target, false).is_ok());
    }
}
