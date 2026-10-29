use serde::{Deserialize, Serialize};
use std::fs;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::time::Duration;

#[derive(Deserialize, Serialize)]
pub(super) struct AudioOutputScope {
    directory: PathBuf,
    token: String,
    pid: u32,
    owner: u32,
    #[serde(skip)]
    record: PathBuf,
    #[serde(skip)]
    cleanup_on_drop: bool,
}

fn valid_token(token: &str) -> bool {
    (16..=96).contains(&token.len())
        && token.starts_with("audio-")
        && token
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}

fn process_alive(pid: u32) -> bool {
    if pid == 0 {
        return false;
    }
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::Foundation::{
            CloseHandle, GetLastError, ERROR_INVALID_PARAMETER, STILL_ACTIVE,
        };
        use windows_sys::Win32::System::Threading::{
            GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
        };
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            return GetLastError() != ERROR_INVALID_PARAMETER;
        }
        let mut code = 0;
        let known = GetExitCodeProcess(handle, &mut code) != 0;
        CloseHandle(handle);
        !known || code == STILL_ACTIVE as u32
    }
    #[cfg(unix)]
    {
        let Ok(pid) = i32::try_from(pid) else {
            return true;
        };
        // Signal zero queries liveness without delivering a signal.
        let result = unsafe { libc::kill(pid, 0) };
        result == 0 || io::Error::last_os_error().kind() == io::ErrorKind::PermissionDenied
    }
    #[cfg(not(any(windows, unix)))]
    {
        true
    }
}

impl AudioOutputScope {
    pub(super) fn create(records: &Path, directory: PathBuf) -> io::Result<Self> {
        fs::create_dir_all(records)?;
        recover_with(records, process_alive);
        let file = tempfile::Builder::new()
            .prefix("audio-")
            .rand_bytes(16)
            .suffix(".json")
            .tempfile_in(records)?;
        let token = file
            .path()
            .file_stem()
            .unwrap()
            .to_str()
            .unwrap()
            .to_string();
        let (file, record) = file.keep().map_err(|error| error.error)?;
        drop(file);
        let scope = Self {
            directory,
            token,
            pid: 0,
            owner: std::process::id(),
            record,
            cleanup_on_drop: true,
        };
        scope.save()?;
        Ok(scope)
    }

    fn save(&self) -> io::Result<()> {
        let mut file = tempfile::NamedTempFile::new_in(self.record.parent().unwrap())?;
        file.write_all(&serde_json::to_vec(self).map_err(io::Error::other)?)?;
        file.persist(&self.record).map_err(|error| error.error)?;
        Ok(())
    }

    pub(super) fn configure(&self, command: &mut std::process::Command) {
        command
            .env("PYMSS_STUDIO_AUDIO_TEMP_TOKEN", &self.token)
            .env("PYMSS_STUDIO_AUDIO_TEMP_STATE", &self.record);
    }

    pub(super) fn attach(&mut self, pid: u32) -> io::Result<()> {
        self.pid = pid;
        self.save()
    }

    pub(super) fn finish(&mut self) {
        // Explicit completion is the last cleanup owned by this task. Leave failed
        // receipts for recovery instead of retrying after its lifecycle leases end.
        self.cleanup_on_drop = false;
        for attempt in 0..3 {
            match self.clean() {
                Ok(true) => return,
                Ok(false) | Err(_) if attempt < 2 => {
                    std::thread::sleep(Duration::from_millis(40 * (attempt + 1)))
                }
                Ok(false) => return, // Remaining files retain their receipt for the next run.
                Err(error) => crate::session_log::early(
                    "WARN",
                    "audio.cleanup",
                    &format!("Audio temporary-file cleanup will be retried: {error}"),
                ),
            }
        }
    }

    fn clean(&self) -> io::Result<bool> {
        let entries = match fs::read_dir(&self.directory) {
            Ok(entries) => entries,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                remove_record(&self.record)?;
                return Ok(true);
            }
            Err(error) => return Err(error),
        };
        let prefix = format!(".pymss-audio-{}-{}-", self.token, self.pid);
        let mut complete = true;
        let mut matched = 0;
        for entry in entries {
            let entry = entry?;
            if !entry.file_name().to_string_lossy().starts_with(&prefix) {
                continue;
            }
            if matched >= 1024 {
                complete = false;
                break;
            }
            matched += 1;
            if entry.file_type()?.is_file() {
                match fs::remove_file(entry.path()) {
                    Err(error) if error.kind() != io::ErrorKind::NotFound => complete = false,
                    _ => (),
                }
            }
        }
        if complete {
            remove_record(&self.record)?;
        }
        Ok(complete)
    }
}

fn remove_record(path: &Path) -> io::Result<()> {
    match fs::remove_file(path) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        result => result,
    }
}

impl Drop for AudioOutputScope {
    fn drop(&mut self) {
        if self.cleanup_on_drop && (self.pid == 0 || !process_alive(self.pid)) {
            self.finish();
        }
    }
}

fn recover_with(records: &Path, mut alive: impl FnMut(u32) -> bool) {
    // Receipts are private application state, like other files in the app data
    // directory. Match each task's random token; never sweep generic audio files.
    let Ok(entries) = fs::read_dir(records) else {
        return;
    };
    for entry in entries
        .flatten()
        .filter(|entry| entry.path().extension().is_some_and(|ext| ext == "json"))
        .take(64)
    {
        if !entry.file_type().is_ok_and(|kind| kind.is_file()) {
            continue;
        }
        let mut bytes = Vec::new();
        if fs::File::open(entry.path())
            .and_then(|file| file.take(131073).read_to_end(&mut bytes))
            .is_err()
            || bytes.len() > 131072
        {
            continue;
        }
        let Ok(mut scope) = serde_json::from_slice::<AudioOutputScope>(&bytes) else {
            continue;
        };
        if !valid_token(&scope.token)
            || entry.path().file_stem().and_then(|stem| stem.to_str()) != Some(scope.token.as_str())
            || !scope.directory.is_absolute()
        {
            continue;
        }
        // A zero pid belongs to a launcher which has not yet registered its child.
        if alive(if scope.pid == 0 {
            scope.owner
        } else {
            scope.pid
        }) {
            continue;
        }
        scope.record = entry.path();
        scope.finish();
    }
}

pub(super) fn recover(records: &Path) {
    recover_with(records, process_alive);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cleanup_is_scoped_and_survives_a_parent_restart() {
        let root = tempfile::tempdir().unwrap();
        let records = root.path().join("records");
        let output = root.path().join("output");
        fs::create_dir(&output).unwrap();
        let mut scope = AudioOutputScope::create(&records, output.clone()).unwrap();
        scope.attach(std::process::id()).unwrap();
        let owned = output.join(format!(
            ".pymss-audio-{}-{}-partial.wav",
            scope.token, scope.pid
        ));
        let another = output.join(".pymss-audio-another-task.wav");
        let result = output.join("result.wav");
        fs::write(&owned, b"partial").unwrap();
        fs::write(&another, b"other").unwrap();
        fs::write(&result, b"complete").unwrap();
        recover_with(&records, |_| true);
        assert!(owned.exists());
        let record = scope.record.clone();
        recover_with(&records, |_| false);
        assert!(!owned.exists());
        assert!(!record.exists());
        assert!(another.exists());
        assert!(result.exists());
    }

    #[test]
    fn cleanup_does_not_follow_links_or_remove_matching_directories() {
        let root = tempfile::tempdir().unwrap();
        let output = root.path().join("output");
        fs::create_dir(&output).unwrap();
        let mut scope =
            AudioOutputScope::create(&root.path().join("records"), output.clone()).unwrap();
        scope.attach(std::process::id()).unwrap();
        let directory = output.join(format!(
            ".pymss-audio-{}-{}-folder.wav",
            scope.token, scope.pid
        ));
        fs::create_dir(&directory).unwrap();
        fs::write(directory.join("keep"), b"keep").unwrap();
        scope.finish();
        assert!(directory.join("keep").exists());
        #[cfg(unix)]
        {
            let linked = output.join(format!(
                ".pymss-audio-{}-{}-link.wav",
                scope.token, scope.pid
            ));
            std::os::unix::fs::symlink(directory.join("keep"), &linked).unwrap();
            scope.finish();
            assert!(directory.join("keep").exists());
            assert!(linked.exists());
        }
    }

    #[test]
    fn invalid_receipts_cannot_trigger_cleanup_while_being_discarded() {
        let root = tempfile::tempdir().unwrap();
        let output = root.path().join("output");
        let records = root.path().join("records");
        fs::create_dir(&output).unwrap();
        fs::create_dir(&records).unwrap();
        let keep = output.join(".pymss-audio-bad-0-keep.wav");
        fs::write(&keep, b"keep").unwrap();
        fs::write(
            records.join("bad.json"),
            serde_json::to_vec(
                &serde_json::json!({"directory":output,"token":"bad","pid":0,"owner":0}),
            )
            .unwrap(),
        )
        .unwrap();
        recover_with(&records, |_| false);
        assert!(keep.exists());
    }

    #[cfg(windows)]
    #[test]
    fn occupied_outputs_keep_a_receipt_and_are_recovered_after_release() {
        use std::os::windows::fs::OpenOptionsExt;
        let root = tempfile::tempdir().unwrap();
        let output = root.path().join("output");
        let records = root.path().join("records");
        fs::create_dir(&output).unwrap();
        let mut scope = AudioOutputScope::create(&records, output.clone()).unwrap();
        scope.attach(std::process::id()).unwrap();
        let partial = output.join(format!(
            ".pymss-audio-{}-{}-locked.wav",
            scope.token, scope.pid
        ));
        let held = fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .share_mode(0)
            .open(&partial)
            .unwrap();
        scope.finish();
        assert!(scope.record.exists());
        let record = scope.record.clone();
        drop(held);
        drop(scope);
        assert!(
            partial.exists(),
            "Drop must not retry cleanup after explicit completion"
        );
        recover_with(&records, |_| false);
        assert!(!partial.exists());
        assert!(!record.exists());
    }

    #[test]
    fn killed_python_worker_outputs_are_removed_by_the_parent() {
        use std::io::BufRead;
        use std::process::{Command, Stdio};
        let root = tempfile::tempdir().unwrap();
        let output = root.path().join("output");
        let records = root.path().join("records");
        fs::create_dir(&output).unwrap();
        let mut scope = AudioOutputScope::create(&records, output.clone()).unwrap();
        let mut command = Command::new(if cfg!(windows) { "python" } else { "python3" });
        command.args(["-c", "from pathlib import Path\nimport sys,time\nfrom pymss.plugins.kit import render_output\ndef render(path):\n path.write_bytes(b'partial')\n print('ready',flush=True)\n time.sleep(60)\nrender_output(Path(sys.argv[1])/'final.wav',render)"])
            .arg(&output)
            .stdout(Stdio::piped()).stderr(Stdio::piped());
        scope.configure(&mut command);
        let mut child = command.spawn().unwrap();
        scope.attach(child.id()).unwrap();
        let mut line = String::new();
        let read = std::io::BufReader::new(child.stdout.take().unwrap()).read_line(&mut line);
        let _ = child.kill();
        let _ = child.wait();
        scope.finish();
        assert!(read.is_ok());
        assert_eq!(line.trim(), "ready");
        assert!(!scope.record.exists());
        assert_eq!(fs::read_dir(&output).unwrap().count(), 0);
    }
}
