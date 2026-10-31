//! Keep the desktop application single-instance on every supported platform.
//!
//! The managed-update helper is started with a command-line mode and exits
//! before this check is reached, so it can still run while the main process is
//! shutting down. macOS updates hand the existing lock to the new process
//! while the original instance waits for startup confirmation.

#[cfg(windows)]
use std::os::windows::ffi::OsStrExt;
#[cfg(windows)]
use std::path::Path;

#[cfg(unix)]
use std::fs::{File, OpenOptions};
#[cfg(unix)]
use std::os::fd::AsRawFd;
#[cfg(unix)]
use std::path::{Path, PathBuf};

#[cfg(windows)]
use windows_sys::core::BOOL;
#[cfg(windows)]
use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, ERROR_ALREADY_EXISTS, HANDLE, HWND, LPARAM};
#[cfg(windows)]
use windows_sys::Win32::System::Threading::{
    CreateMutexW, OpenProcess, QueryFullProcessImageNameW, PROCESS_NAME_WIN32,
    PROCESS_QUERY_LIMITED_INFORMATION, ReleaseMutex,
};
#[cfg(windows)]
use windows_sys::Win32::UI::WindowsAndMessaging::{
    EnumWindows, GetWindowThreadProcessId, IsWindowVisible, SetForegroundWindow, ShowWindow,
    SW_RESTORE,
};

#[cfg(all(windows, not(feature = "vst-lab")))]
const INSTANCE_MUTEX_NAME: &str = r"Local\PymssStudioMainInstance";
#[cfg(all(windows, feature = "vst-lab"))]
const INSTANCE_MUTEX_NAME: &str = r"Local\PymssStudioVstLabInstance";

#[cfg(windows)]
pub(crate) struct InstanceMutex(HANDLE);

#[cfg(unix)]
pub(crate) struct InstanceMutex {
    file: File,
}

#[cfg(windows)]
impl Drop for InstanceMutex {
    fn drop(&mut self) {
        unsafe {
            ReleaseMutex(self.0);
            CloseHandle(self.0);
        }
    }
}

/// Acquire the main-process mutex.
///
/// Returns a guard for the first instance. When another copy is already
/// running, it returns `Ok(None)`, allowing the duplicate process to exit
/// before Tauri creates another WebView window. Windows also focuses the
/// existing main window.
#[cfg(windows)]
pub fn acquire_or_focus() -> Result<Option<InstanceMutex>, String> {
    let name = std::ffi::OsStr::new(INSTANCE_MUTEX_NAME)
        .encode_wide()
        .chain(Some(0))
        .collect::<Vec<_>>();
    let handle = unsafe { CreateMutexW(std::ptr::null(), 1, name.as_ptr()) };
    if handle.is_null() {
        return Err(std::io::Error::last_os_error().to_string());
    }
    if unsafe { GetLastError() } == ERROR_ALREADY_EXISTS {
        unsafe {
            CloseHandle(handle);
        }
        focus_existing_instance();
        return Ok(None);
    }
    Ok(Some(InstanceMutex(handle)))
}

#[cfg(unix)]
pub fn acquire_or_focus() -> Result<Option<InstanceMutex>, String> {
    #[cfg(any(target_os = "macos", target_os = "linux"))]
    if let Some(guard) = handoff::from_environment(&instance_lock_path()) {
        handoff::remember(&guard);
        return Ok(Some(guard));
    }
    let guard = acquire_lock(&instance_lock_path())?;
    #[cfg(any(target_os = "macos", target_os = "linux"))]
    if let Some(guard) = guard.as_ref() {
        handoff::remember(guard);
    }
    Ok(guard)
}

#[cfg(unix)]
fn instance_lock_path() -> PathBuf {
    let user_id = unsafe { libc::geteuid() };
    let instance = if cfg!(feature = "vst-lab") { "vstlab" } else { "plugin-test" };
    Path::new("/tmp").join(format!("pymss-studio-{instance}-{user_id}.lock"))
}

#[cfg(unix)]
fn acquire_lock(path: &Path) -> Result<Option<InstanceMutex>, String> {
    let file = OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .open(&path)
        .map_err(|error| error.to_string())?;
    let result = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if result == 0 {
        return Ok(Some(InstanceMutex { file }));
    }
    let error = std::io::Error::last_os_error();
    if error
        .raw_os_error()
        .is_some_and(|code| code == libc::EWOULDBLOCK || code == libc::EAGAIN)
    {
        return Ok(None);
    }
    Err(error.to_string())
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
mod handoff {
    use super::*;
    use std::os::fd::FromRawFd;
    use std::os::unix::fs::MetadataExt;
    use std::os::unix::process::CommandExt;
    use std::sync::atomic::{AtomicI32, Ordering};

    const ENV: &str = "PYMSS_STUDIO_INSTANCE_FD";
    static LOCK_FD: AtomicI32 = AtomicI32::new(-1);

    pub(super) fn remember(guard: &InstanceMutex) {
        LOCK_FD.store(guard.file.as_raw_fd(), Ordering::Release);
    }

    pub(super) fn adopt(fd: i32, path: &Path) -> Option<InstanceMutex> {
        if fd < 3 {
            return None;
        }
        // SAFETY: fcntl validates the untrusted descriptor and returns a new owned FD.
        let duplicate = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, 3) };
        if duplicate < 0 {
            return None;
        }
        // SAFETY: duplicate is a fresh valid FD owned by this function.
        let file = unsafe { File::from_raw_fd(duplicate) };
        let actual = file.metadata().ok()?;
        let expected = std::fs::metadata(path).ok()?;
        if !actual.is_file() || actual.dev() != expected.dev() || actual.ino() != expected.ino() {
            return None;
        }
        // SAFETY: this is a valid FD. The nonblocking call also rejects an unrelated OFD
        // when the original application already owns the lock.
        if unsafe { libc::flock(duplicate, libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return None;
        }
        Some(InstanceMutex { file })
    }

    pub(super) fn from_environment(path: &Path) -> Option<InstanceMutex> {
        let fd = std::env::var(ENV).ok()?.parse::<i32>().ok()?;
        let guard = adopt(fd, path)?;
        // SAFETY: only after identity validation do we close the original inherited FD.
        // The owned duplicate above keeps the same OFD locked and restores CLOEXEC.
        unsafe {
            libc::close(fd);
        }
        std::env::remove_var(ENV);
        Some(guard)
    }

    pub(crate) fn configure_child(command: &mut std::process::Command) -> Result<(), String> {
        let fd = LOCK_FD.load(Ordering::Acquire);
        if fd < 3 {
            return Err("The application instance lock is unavailable for restart".into());
        }
        command.env(ENV, fd.to_string());
        // SAFETY: only async-signal-safe fcntl calls run between fork and exec. The
        // parent's guard remains alive throughout startup confirmation and rollback.
        unsafe {
            command.pre_exec(move || {
                let flags = libc::fcntl(fd, libc::F_GETFD);
                if flags < 0 || libc::fcntl(fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC) < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        Ok(())
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn inherited_lock_survives_parent_drop_and_restores_cloexec() {
            let root = tempfile::tempdir().unwrap();
            let path = root.path().join("instance.lock");
            let parent = acquire_lock(&path).unwrap().unwrap();
            let adopted = adopt(parent.file.as_raw_fd(), &path).unwrap();
            let flags = unsafe { libc::fcntl(adopted.file.as_raw_fd(), libc::F_GETFD) };
            assert_ne!(flags & libc::FD_CLOEXEC, 0);
            drop(parent);
            assert!(acquire_lock(&path).unwrap().is_none());
            drop(adopted);
            super::super::tests::assert_lock_released(&path);
        }

        #[test]
        fn invalid_handoff_does_not_close_the_callers_descriptor() {
            let root = tempfile::tempdir().unwrap();
            let path = root.path().join("instance.lock");
            let other = root.path().join("other.lock");
            let guard = acquire_lock(&path).unwrap().unwrap();
            std::fs::write(&other, b"").unwrap();
            assert!(adopt(guard.file.as_raw_fd(), &other).is_none());
            assert!(adopt(-1, &path).is_none());
            assert!(guard.file.metadata().is_ok());
            assert!(acquire_lock(&path).unwrap().is_none());
        }

        #[test]
        fn child_exec_inherits_the_lock_and_failed_launch_keeps_the_parent_locked() {
            let root = tempfile::tempdir().unwrap();
            let path = root.path().join("instance.lock");
            let guard = acquire_lock(&path).unwrap().unwrap();
            remember(&guard);
            let mut failed = std::process::Command::new(root.path().join("missing-executable"));
            configure_child(&mut failed).unwrap();
            assert!(failed.spawn().is_err());
            assert!(acquire_lock(&path).unwrap().is_none());
            let mut command = std::process::Command::new("/bin/sleep");
            command.arg("10");
            configure_child(&mut command).unwrap();
            let mut child = command.spawn().unwrap();
            drop(guard);
            assert!(acquire_lock(&path).unwrap().is_none());
            child.kill().unwrap();
            child.wait().unwrap();
            super::super::tests::assert_lock_released(&path);
        }
    }
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
pub(crate) use handoff::configure_child as configure_update_child;

#[cfg(all(test, unix))]
mod tests {
    use super::{acquire_lock, instance_lock_path};

    pub(super) fn assert_lock_released(path: &std::path::Path) {
        // A concurrent test can fork while this test owns the lock. CLOEXEC
        // closes that inherited FD at exec, so dropping our guard alone need
        // not make the lock immediately available to another open file.
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
        loop {
            if acquire_lock(path).unwrap().is_some() {
                return;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "Instance lock was not released: {}",
                path.display(),
            );
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
    }

    #[test]
    fn unix_instance_lock_path_does_not_depend_on_process_temp_environment() {
        assert_eq!(
            instance_lock_path().parent(),
            Some(std::path::Path::new("/tmp")),
        );
    }

    #[test]
    fn unix_lock_excludes_another_instance_until_the_guard_is_dropped() {
        let path = std::env::temp_dir().join(format!(
            "pymss-studio-single-instance-test-{}",
            std::process::id(),
        ));
        let first = acquire_lock(&path).unwrap().unwrap();
        assert!(acquire_lock(&path).unwrap().is_none());
        drop(first);
        assert_lock_released(&path);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn unix_lock_stays_held_until_inherited_descriptors_close() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("instance.lock");
        let guard = acquire_lock(&path).unwrap().unwrap();
        // A duplicate shares the same open file description as a forked child.
        let inherited = guard.file.try_clone().unwrap();
        drop(guard);
        assert!(acquire_lock(&path).unwrap().is_none());
        let closing = std::thread::spawn(move || {
            std::thread::sleep(std::time::Duration::from_millis(50));
            drop(inherited);
        });
        assert_lock_released(&path);
        closing.join().unwrap();
    }
}

#[cfg(windows)]
struct WindowSearch {
    executable: String,
    hwnd: HWND,
}

#[cfg(windows)]
unsafe extern "system" fn find_window_callback(hwnd: HWND, lparam: LPARAM) -> BOOL {
    let search = &mut *(lparam as *mut WindowSearch);
    if !search.hwnd.is_null() || IsWindowVisible(hwnd) == 0 {
        return 1;
    }
    let mut pid = 0_u32;
    if GetWindowThreadProcessId(hwnd, &mut pid) == 0 {
        return 1;
    }
    if process_executable(pid).as_deref() == Some(search.executable.as_str()) {
        search.hwnd = hwnd;
        return 0;
    }
    1
}

#[cfg(windows)]
fn focus_existing_instance() {
    let Ok(executable) = std::env::current_exe() else {
        return;
    };
    let mut search = WindowSearch {
        executable: normalize_path(&executable),
        hwnd: std::ptr::null_mut(),
    };
    unsafe {
        EnumWindows(
            Some(find_window_callback),
            &mut search as *mut WindowSearch as LPARAM,
        );
        if !search.hwnd.is_null() {
            // Restore a minimized window before requesting foreground focus.
            ShowWindow(search.hwnd, SW_RESTORE);
            SetForegroundWindow(search.hwnd);
        }
    }
}

#[cfg(windows)]
fn process_executable(pid: u32) -> Option<String> {
    let process = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
    if process.is_null() {
        return None;
    }
    let mut buffer = vec![0_u16; 32_768];
    let mut length = buffer.len() as u32;
    let success = unsafe {
        QueryFullProcessImageNameW(
            process,
            PROCESS_NAME_WIN32,
            buffer.as_mut_ptr(),
            &mut length,
        )
    } != 0;
    unsafe {
        CloseHandle(process);
    }
    if !success {
        return None;
    }
    Some(normalize_path(Path::new(
        &String::from_utf16_lossy(&buffer[..length as usize]),
    )))
}

#[cfg(windows)]
fn normalize_path(path: &Path) -> String {
    path.to_string_lossy()
        .replace('/', "\\")
        .trim_start_matches(r"\\?\")
        .to_ascii_lowercase()
}
