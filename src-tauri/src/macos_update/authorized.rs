use super::{applescript_quote, failure, shell_quote, startup, BUNDLE_ID};
use crate::error::AppResult;
use std::fs;
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

fn control_path(token: &str) -> AppResult<PathBuf> {
    if !startup::valid_token(token) {
        return Err(failure("Invalid authorized update token"));
    }
    Ok(Path::new("/private/tmp").join(format!("pymss-studio-authorized-{token}")))
}

fn check_control(directory: &Path) -> AppResult<bool> {
    match fs::symlink_metadata(directory) {
        Ok(metadata)
            if metadata.is_dir() && metadata.uid() == 0 && metadata.mode() & 0o022 == 0 =>
        {
            Ok(true)
        }
        Ok(_) => Err(failure(
            "The authorized update control directory has invalid ownership or permissions",
        )),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error.into()),
    }
}

fn read_status(directory: &Path) -> AppResult<Option<String>> {
    if !check_control(directory)? {
        return Ok(None);
    }
    let mut file = match fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(directory.join("status"))
    {
        Ok(file) => file,
        Err(error)
            if matches!(
                error.kind(),
                std::io::ErrorKind::NotFound | std::io::ErrorKind::PermissionDenied
            ) =>
        {
            return Ok(None)
        }
        Err(error) => return Err(error.into()),
    };
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.uid() != 0 || metadata.mode() & 0o022 != 0 {
        return Err(failure("The authorized update status file is invalid"));
    }
    let mut value = String::new();
    (&mut file).take(64).read_to_string(&mut value)?;
    Ok(Some(value))
}

fn signal(directory: &Path, value: &str) -> AppResult<()> {
    if !check_control(directory)? {
        return Err(failure(
            "The authorized update transaction is no longer active",
        ));
    }
    let mut file = fs::OpenOptions::new()
        .write(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(directory.join("outcome"))?;
    let metadata = file.metadata()?;
    // SAFETY: geteuid has no preconditions.
    let user_id = unsafe { libc::geteuid() };
    if !metadata.is_file()
        || metadata.uid() != user_id
        || metadata.mode() & 0o077 != 0
        || value.len() > 64
    {
        return Err(failure("The authorized update response file is invalid"));
    }
    file.write_all(format!("{value:<64}").as_bytes())?;
    file.sync_all()?;
    Ok(())
}

pub(super) fn report_ready(token: &str) -> AppResult<()> {
    signal(
        &control_path(token)?,
        &format!("ready:{}", std::process::id()),
    )
}

pub(super) fn command(
    target: &Path,
    archive: &Path,
    digest: &str,
    version: &str,
    token: &str,
) -> String {
    // SAFETY: geteuid has no preconditions. The GUI process remains this user throughout.
    let user_id = unsafe { libc::geteuid() };
    format!(
        "/bin/sh -c {} pymss-update {} {} {} {} {} {} {} {}",
        shell_quote(include_str!("../../../scripts/install-macos-update.sh")),
        shell_quote(&target.to_string_lossy()),
        shell_quote(&archive.to_string_lossy()),
        shell_quote(digest),
        shell_quote(version),
        shell_quote(BUNDLE_ID),
        shell_quote(token),
        user_id,
        std::process::id()
    )
}

pub(super) struct Session {
    directory: PathBuf,
    child: Child,
    finished: bool,
    log_path: PathBuf,
}

impl Session {
    pub(super) fn begin(
        target: &Path,
        archive: &Path,
        digest: &str,
        version: &str,
        token: &str,
        log_path: &Path,
    ) -> AppResult<Self> {
        let directory = control_path(token)?;
        let log = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(log_path)?;
        let script = format!(
            "do shell script {} with administrator privileges",
            applescript_quote(&command(target, archive, digest, version, token))
        );
        let child = Command::new("/usr/bin/osascript")
            .args(["-e", &script])
            .stdin(Stdio::null())
            .stdout(log.try_clone()?)
            .stderr(log)
            .spawn()?;
        let mut session = Self {
            directory,
            child,
            finished: false,
            log_path: log_path.to_path_buf(),
        };
        loop {
            match read_status(&session.directory) {
                Ok(Some(status)) if status == "installed" => return Ok(session),
                Ok(Some(status)) if matches!(status.as_str(), "failed" | "aborted" | "rollback-failed") => {
                    let _ = signal(&session.directory, "observed");
                    let _ = session.child.wait();
                    session.finished = true;
                    return Err(session.error(if status == "rollback-failed" {
                        "MACOS_UPDATE_ROLLBACK_FAILED: The previous application could not be restored; its backup has been retained"
                    } else {
                        "macOS update authorization or installation failed"
                    }));
                }
                Ok(_) => (),
                Err(error) => {
                    let _ = session.child.kill();
                    let _ = session.child.wait();
                    session.finished = true;
                    return Err(error);
                }
            }
            if session.child.try_wait()?.is_some() {
                session.finished = true;
                return Err(session.error("macOS update authorization or installation failed"));
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }

    fn error(&self, message: &str) -> crate::error::AppError {
        failure(format!(
            "{message}. See {} for details.",
            self.log_path.display()
        ))
    }

    pub(super) fn complete(&mut self, commit: bool) -> AppResult<()> {
        if self.finished {
            return Err(self.error("The authorized update transaction has already ended"));
        }
        let expected = if commit { "committed" } else { "aborted" };
        signal(&self.directory, if commit { "commit" } else { "abort" })?;
        let started = Instant::now();
        loop {
            if let Some(status) = read_status(&self.directory)? {
                if matches!(status.as_str(), "committed" | "aborted" | "failed" | "rollback-failed") {
                    let _ = signal(&self.directory, "observed");
                    let exit_status = self.child.wait()?;
                    self.finished = true;
                    return if status == expected && exit_status.success() {
                        Ok(())
                    } else {
                        Err(self.error(if status == "rollback-failed" {
                            "MACOS_UPDATE_ROLLBACK_FAILED: The previous application could not be restored; its backup has been retained"
                        } else {
                            "The authorized update could not complete the requested operation"
                        }))
                    };
                }
            }
            if let Some(status) = self.child.try_wait()? {
                self.finished = true;
                return Err(self.error(&format!(
                    "The authorized update process exited without confirming {expected}: {status}"
                )));
            }
            if started.elapsed() > Duration::from_secs(125) {
                return Err(self.error("The authorized update process did not finish in time"));
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        if !self.finished {
            if let Err(error) = self.complete(false) {
                crate::session_log::early("WARN", "update.diagnostic", &format!("Unable to finish authorized macOS update cleanup: {error}"));
            }
        }
    }
}
