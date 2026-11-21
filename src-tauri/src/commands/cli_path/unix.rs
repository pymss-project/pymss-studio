use super::{AppError, AppResult, CliPathAction, CliPathStatus, Registration};
use std::{
    env, fs, io,
    os::unix::fs::{symlink, PermissionsExt},
    path::{Path, PathBuf},
};

const COMMAND: &str = "pymss";

fn shell_quote(path: &Path) -> AppResult<String> {
    let path = path
        .to_str()
        .ok_or_else(|| AppError::Worker("The application path must be valid UTF-8.".into()))?;
    Ok(format!("'{}'", path.replace('\'', "'\"'\"'")))
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum EntryKind {
    Symlink,
    AppImage,
    ExtractAndRun,
}

fn linux_entry(target: &Path, kind: EntryKind) -> AppResult<String> {
    let mode = if kind == EntryKind::ExtractAndRun {
        " --appimage-extract-and-run"
    } else {
        ""
    };
    Ok(format!(
        "#!/bin/sh\nexec {}{mode} --studio-cli \"$@\"\n",
        shell_quote(target)?
    ))
}

fn write_entry(path: &Path, target: &Path, kind: EntryKind) -> AppResult<()> {
    if kind == EntryKind::Symlink {
        symlink(target, path)?;
    } else {
        fs::write(path, linux_entry(target, kind)?)?;
        fs::set_permissions(path, fs::Permissions::from_mode(0o755))?;
    }
    Ok(())
}

fn is_executable(path: &Path) -> bool {
    fs::metadata(path).is_ok_and(|file| file.is_file() && file.permissions().mode() & 0o111 != 0)
}

fn lookup(dirs: &[PathBuf]) -> Option<PathBuf> {
    dirs.iter()
        .map(|dir| dir.join(COMMAND))
        .find(|path| is_executable(path))
}

fn exists(path: &Path) -> io::Result<bool> {
    match fs::symlink_metadata(path) {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error),
    }
}

struct Entry {
    path: PathBuf,
    record: PathBuf,
    kind: EntryKind,
}

impl Entry {
    fn recorded_target(&self) -> AppResult<Option<PathBuf>> {
        match fs::read(&self.record) {
            Ok(bytes) => Ok(Some(serde_json::from_slice(&bytes)?)),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(error.into()),
        }
    }

    fn installed_kind(&self, target: &Path) -> AppResult<Option<EntryKind>> {
        if self.kind == EntryKind::Symlink {
            return Ok(fs::read_link(&self.path)
                .is_ok_and(|value| value == target)
                .then_some(EntryKind::Symlink));
        }
        let Ok(metadata) = fs::symlink_metadata(&self.path) else {
            return Ok(None);
        };
        if metadata.is_file() {
            for kind in [EntryKind::AppImage, EntryKind::ExtractAndRun] {
                let text = linux_entry(target, kind)?;
                if metadata.len() == text.len() as u64
                    && fs::read(&self.path).is_ok_and(|contents| contents == text.as_bytes())
                {
                    return Ok(Some(kind));
                }
            }
        }
        Ok(None)
    }

    fn remove(&self) -> AppResult<()> {
        fs::remove_file(&self.path)?;
        fs::remove_file(&self.record)?;
        Ok(())
    }

    fn write(&self, target: &Path, previous: Option<&Path>) -> AppResult<()> {
        let bin = self.path.parent().expect("CLI path has a parent");
        fs::create_dir_all(bin)?;
        let temporary = tempfile::tempdir_in(bin)?;
        let next = temporary.path().join(COMMAND);
        write_entry(&next, target, self.kind)?;
        let pending =
            tempfile::NamedTempFile::new_in(self.record.parent().expect("record has a parent"))?;
        fs::write(pending.path(), serde_json::to_vec(target)?)?;
        let replaced = if let Some(previous) = previous {
            let kind = self.installed_kind(previous)?.ok_or_else(|| {
                AppError::Worker("The existing pymss entry changed. Refresh and retry.".into())
            })?;
            fs::rename(&next, &self.path)?;
            Some((previous, kind))
        } else {
            if self.kind == EntryKind::Symlink {
                symlink(target, &self.path)?;
            } else {
                fs::hard_link(&next, &self.path)?;
            }
            None
        };
        if let Err(error) = pending.persist(&self.record) {
            if self.installed_kind(target)? == Some(self.kind) {
                if let Some((previous, kind)) = replaced {
                    write_entry(&next, previous, kind)?;
                    fs::rename(&next, &self.path)?;
                } else {
                    fs::remove_file(&self.path)?;
                }
            }
            return Err(error.error.into());
        }
        Ok(())
    }

    fn status(&self, target: &Path, dirs: &[PathBuf]) -> AppResult<CliPathStatus> {
        let recorded = self.recorded_target()?;
        let owned = if let Some(recorded) = recorded.as_ref() {
            self.installed_kind(recorded)?.is_some()
        } else {
            false
        };
        let present = exists(&self.path)?;
        let current = present
            && is_executable(&self.path)
            && (self.installed_kind(target)? == Some(self.kind)
                || (self.kind == EntryKind::Symlink && same_executable(&self.path, target)));
        let resolved = lookup(dirs);
        let external = resolved.as_ref().filter(|path| {
            !(same_executable(path, &self.path)
                || (self.kind == EntryKind::Symlink && same_executable(path, target)))
        });
        let (status, path) = if let Some(external) = external {
            (Registration::Conflict, Some(external.clone()))
        } else if current {
            (Registration::Registered, Some(self.path.clone()))
        } else if owned {
            (Registration::Moved, recorded)
        } else if present {
            (Registration::Conflict, Some(self.path.clone()))
        } else if resolved.is_some() {
            (Registration::Registered, resolved.clone())
        } else {
            (Registration::Available, None)
        };
        Ok(CliPathStatus {
            status,
            owned,
            path: path.map(|p| p.to_string_lossy().into_owned()),
            changed: false,
            diagnostics: None,
        })
    }
}

fn same_executable(path: &Path, target: &Path) -> bool {
    path == target
        || path
            .canonicalize()
            .ok()
            .zip(target.canonicalize().ok())
            .is_some_and(|(a, b)| a == b)
}

fn require_installed_target(target: &Path) -> AppResult<()> {
    if !is_executable(target) {
        return Err(AppError::Worker(
            "Open Pymss Studio from its installed location before enabling pymss.".into(),
        ));
    }
    #[cfg(target_os = "macos")]
    {
        let bundle = crate::studio_paths::macos_bundle_from_executable(target)
            .ok_or_else(|| AppError::Worker("The Studio application bundle is missing.".into()))?;
        crate::macos_update::check_location(&bundle)?;
    }
    Ok(())
}

#[cfg(target_os = "linux")]
pub(super) fn account_home() -> AppResult<PathBuf> {
    use std::{ffi::CStr, mem::MaybeUninit, os::unix::ffi::OsStrExt};

    // Portable AppImages may replace HOME for application data. Terminal
    // registration belongs to the OS account shared by all Studio installs.
    let uid = unsafe { libc::getuid() };
    let hint = unsafe { libc::sysconf(libc::_SC_GETPW_R_SIZE_MAX) };
    let mut buffer = vec![0u8; usize::try_from(hint).unwrap_or(4096).max(1024)];
    loop {
        let mut record = MaybeUninit::<libc::passwd>::uninit();
        let mut result = std::ptr::null_mut();
        let error = unsafe {
            libc::getpwuid_r(
                uid,
                record.as_mut_ptr(),
                buffer.as_mut_ptr().cast(),
                buffer.len(),
                &mut result,
            )
        };
        if error == libc::ERANGE {
            buffer.resize(buffer.len() * 2, 0);
            continue;
        }
        if error != 0 {
            return Err(io::Error::from_raw_os_error(error).into());
        }
        if !result.is_null() {
            let home = unsafe { (*result).pw_dir };
            if !home.is_null() {
                let path = Path::new(std::ffi::OsStr::from_bytes(
                    unsafe { CStr::from_ptr(home) }.to_bytes(),
                ));
                if path.is_absolute() {
                    return Ok(path.to_path_buf());
                }
            }
        }
        return Err(AppError::Worker(
            "Could not locate the Linux account home directory.".into(),
        ));
    }
}

pub(super) fn apply(home: &Path, target: &Path, action: CliPathAction) -> AppResult<CliPathStatus> {
    let state = home.join(".pymss-studio");
    let _lease = if matches!(action, CliPathAction::Enable | CliPathAction::Disable) {
        Some(crate::cli_access::resource(
            &state,
            ".pymss-cli-registration.lock",
            true,
        )?)
    } else {
        None
    };
    #[cfg(target_os = "linux")]
    let kind = if crate::studio_paths::appimage_extract_and_run() {
        EntryKind::ExtractAndRun
    } else {
        EntryKind::AppImage
    };
    #[cfg(not(target_os = "linux"))]
    let kind = EntryKind::Symlink;
    let entry = Entry {
        path: home.join(".local/bin").join(COMMAND),
        record: state.join("cli-registration.json"),
        kind,
    };
    // Studio sees its own PATH. Shell startup files can resolve a different command.
    let dirs: Vec<_> = env::split_paths(&env::var_os("PATH").unwrap_or_default())
        .filter(|path| path.is_absolute())
        .collect();
    let status = entry.status(target, &dirs)?;
    match action {
        CliPathAction::Enable
            if matches!(status.status, Registration::Available | Registration::Moved) =>
        {
            require_installed_target(target)?;
            let previous = if status.owned {
                entry.recorded_target()?
            } else {
                None
            };
            entry.write(target, previous.as_deref())?;
        }
        CliPathAction::Disable if status.owned => entry.remove()?,
        _ => return Ok(status),
    }
    let mut updated = entry.status(target, &dirs)?;
    updated.changed = true;
    Ok(updated)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(target_os = "linux")]
    #[test]
    fn linux_registration_home_ignores_portable_home() {
        const EXPECTED: &str = "PYMSS_CLI_ACCOUNT_HOME_FIXTURE";
        if let Some(expected) = env::var_os(EXPECTED) {
            assert_eq!(account_home().unwrap(), PathBuf::from(expected));
            assert_ne!(env::home_dir().unwrap(), account_home().unwrap());
            return;
        }

        let portable = tempfile::tempdir().unwrap();
        let module = module_path!().split_once("::").unwrap().1;
        let test = format!("{module}::linux_registration_home_ignores_portable_home");
        let result = std::process::Command::new(env::current_exe().unwrap())
            .args(["--exact", &test])
            .env(EXPECTED, account_home().unwrap())
            .env("HOME", portable.path())
            .status()
            .unwrap();
        assert!(result.success());
    }

    #[test]
    fn owned_entry_can_move_and_external_entry_is_preserved() {
        for kind in [
            EntryKind::Symlink,
            EntryKind::AppImage,
            EntryKind::ExtractAndRun,
        ] {
            let temp = tempfile::tempdir().unwrap();
            let root = temp.path();
            let entry = Entry {
                path: root.join("bin/pymss"),
                record: root.join("registration.json"),
                kind,
            };
            let first = root.join("old app");
            let second = root.join("new ' app");
            fs::write(&second, b"launcher").unwrap();
            fs::set_permissions(&second, fs::Permissions::from_mode(0o755)).unwrap();
            entry.write(&first, None).unwrap();
            assert!(matches!(
                entry.status(&second, &[]).unwrap().status,
                Registration::Moved
            ));
            entry.write(&second, Some(&first)).unwrap();
            let without_path = entry.status(&second, &[]).unwrap();
            let with_path = entry
                .status(&second, &[entry.path.parent().unwrap().into()])
                .unwrap();
            assert!(without_path.owned && with_path.owned);
            assert!(matches!(without_path.status, Registration::Registered));
            assert!(matches!(with_path.status, Registration::Registered));
            entry.remove().unwrap();
            symlink(&second, &entry.path).unwrap();
            assert!(!entry.status(&second, &[]).unwrap().owned);
            assert!(entry.write(&first, None).is_err());
            assert_eq!(fs::read_link(&entry.path).unwrap(), second);
        }
    }

    #[test]
    fn linux_wrapper_preserves_arguments_and_exit_code() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("Studio's copy.AppImage");
        fs::write(
            &target,
            b"#!/bin/sh
printf '%s\n' \"$@\"
exit 7
",
        )
        .unwrap();
        fs::set_permissions(&target, fs::Permissions::from_mode(0o755)).unwrap();
        let mut entry = Entry {
            path: root.path().join("bin/pymss"),
            record: root.path().join("registration.json"),
            kind: EntryKind::AppImage,
        };
        entry.write(&target, None).unwrap();
        let result = std::process::Command::new(&entry.path)
            .args(["two words", "quote's", ""])
            .output()
            .unwrap();
        assert_eq!(result.status.code(), Some(7));
        assert_eq!(
            result.stdout,
            b"--studio-cli
two words
quote's

"
        );
        entry.kind = EntryKind::ExtractAndRun;
        let status = entry.status(&target, &[]).unwrap();
        assert!(status.owned && matches!(status.status, Registration::Moved));
        entry.write(&target, Some(&target)).unwrap();
        let result = std::process::Command::new(&entry.path)
            .args(["two words", "quote's", ""])
            .output()
            .unwrap();
        assert_eq!(result.status.code(), Some(7));
        assert_eq!(
            result.stdout,
            b"--appimage-extract-and-run\n--studio-cli\ntwo words\nquote's\n\n"
        );
        assert!(matches!(
            entry.status(&target, &[]).unwrap().status,
            Registration::Registered
        ));
    }

    #[test]
    fn a_direct_link_to_an_appimage_does_not_select_cli_mode() {
        let root = tempfile::tempdir().unwrap();
        let external = root.path().join("external");
        fs::create_dir(&external).unwrap();
        let target = root.path().join("image.AppImage");
        fs::write(&target, b"launcher").unwrap();
        fs::set_permissions(&target, fs::Permissions::from_mode(0o755)).unwrap();
        symlink(&target, external.join(COMMAND)).unwrap();
        let entry = Entry {
            path: root.path().join("bin/pymss"),
            record: root.path().join("registration.json"),
            kind: EntryKind::AppImage,
        };
        let status = entry.status(&target, &[external]).unwrap();
        assert!(matches!(status.status, Registration::Conflict));
        assert!(!status.owned);
    }

    #[test]
    fn earlier_external_command_is_reported_without_losing_ownership() {
        let root = tempfile::tempdir().unwrap();
        let dir = root.path().join("external");
        fs::create_dir(&dir).unwrap();
        let other = dir.join(COMMAND);
        fs::write(&other, b"other").unwrap();
        fs::set_permissions(&other, fs::Permissions::from_mode(0o755)).unwrap();
        let entry = Entry {
            path: root.path().join("bin/pymss"),
            record: root.path().join("registration.json"),
            kind: EntryKind::AppImage,
        };
        let target = root.path().join("image.AppImage");
        entry.write(&target, None).unwrap();
        let status = entry
            .status(&target, &[dir, entry.path.parent().unwrap().into()])
            .unwrap();
        assert!(matches!(status.status, Registration::Conflict));
        assert!(status.owned);
        assert_eq!(status.path.as_deref(), other.to_str());
    }
}
