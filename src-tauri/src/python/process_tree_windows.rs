use std::collections::HashMap;
use std::io;
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
use std::os::windows::process::CommandExt;
use std::process::{Child, Command};
use windows_sys::Win32::Foundation::{FILETIME, INVALID_HANDLE_VALUE};
use windows_sys::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, Thread32First, Thread32Next,
    PROCESSENTRY32W, TH32CS_SNAPPROCESS, TH32CS_SNAPTHREAD, THREADENTRY32,
};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectBasicAccountingInformation,
    JobObjectExtendedLimitInformation, QueryInformationJobObject, SetInformationJobObject,
    TerminateJobObject, JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
use windows_sys::Win32::System::SystemInformation::GetSystemTimePreciseAsFileTime;
use windows_sys::Win32::System::Threading::{
    GetProcessIdOfThread, GetProcessTimes, OpenProcess, OpenThread, ResumeThread, TerminateProcess,
    CREATE_SUSPENDED, PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_TERMINATE,
    THREAD_QUERY_LIMITED_INFORMATION, THREAD_SUSPEND_RESUME,
};

use super::CREATE_NO_WINDOW;

fn ticks(time: FILETIME) -> u64 {
    (u64::from(time.dwHighDateTime) << 32) | u64::from(time.dwLowDateTime)
}

fn open_process(pid: u32) -> io::Result<(OwnedHandle, u64)> {
    unsafe {
        let handle = OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE,
            0,
            pid,
        );
        if handle.is_null() {
            return Err(io::Error::last_os_error());
        }
        let handle = OwnedHandle::from_raw_handle(handle);
        let mut created: FILETIME = std::mem::zeroed();
        let mut exited = created;
        let mut kernel = created;
        let mut user = created;
        if GetProcessTimes(
            handle.as_raw_handle(),
            &mut created,
            &mut exited,
            &mut kernel,
            &mut user,
        ) == 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok((handle, ticks(created)))
    }
}

fn process_tree(pid: u32) -> io::Result<Vec<(OwnedHandle, u64)>> {
    // Retaining handles reserves each selected identity, even after it exits.
    let root = open_process(pid)?;
    let mut cutoff: FILETIME = unsafe { std::mem::zeroed() };
    unsafe { GetSystemTimePreciseAsFileTime(&mut cutoff) };
    let cutoff = ticks(cutoff);
    let mut children: HashMap<u32, Vec<u32>> = HashMap::new();
    unsafe {
        let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if snapshot == INVALID_HANDLE_VALUE {
            return Err(io::Error::last_os_error());
        }
        let snapshot = OwnedHandle::from_raw_handle(snapshot);
        let mut entry: PROCESSENTRY32W = std::mem::zeroed();
        entry.dwSize = std::mem::size_of_val(&entry) as u32;
        let mut found = Process32FirstW(snapshot.as_raw_handle(), &mut entry);
        while found != 0 {
            children
                .entry(entry.th32ParentProcessID)
                .or_default()
                .push(entry.th32ProcessID);
            found = Process32NextW(snapshot.as_raw_handle(), &mut entry);
        }
    }
    let mut owned = vec![root];
    let mut pending = vec![(pid, owned[0].1)];
    while let Some((parent, parent_created)) = pending.pop() {
        for child_pid in children.remove(&parent).unwrap_or_default() {
            if let Ok(child) = open_process(child_pid) {
                // Ignore a recycled parent ID or an ID reused after the snapshot began.
                if child.1 >= parent_created && child.1 <= cutoff {
                    pending.push((child_pid, child.1));
                    owned.push(child);
                }
            }
        }
    }
    Ok(owned)
}

pub(super) fn terminate_tree(pid: u32) -> io::Result<()> {
    for (handle, _) in process_tree(pid)?.iter().rev() {
        unsafe { TerminateProcess(handle.as_raw_handle(), 1) };
    }
    Ok(())
}

pub(super) struct QueryJob(OwnedHandle);

impl QueryJob {
    pub(super) fn spawn(command: &mut Command) -> io::Result<(Child, Self)> {
        Self::spawn_with_setup(command, |_| Ok(()))
    }

    fn spawn_with_setup(
        command: &mut Command,
        before_assignment: impl FnOnce(&Child) -> io::Result<()>,
    ) -> io::Result<(Child, Self)> {
        unsafe {
            let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if handle.is_null() {
                return Err(io::Error::last_os_error());
            }
            let job = Self(OwnedHandle::from_raw_handle(handle));
            let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &limits as *const _ as *const std::ffi::c_void,
                std::mem::size_of_val(&limits) as u32,
            ) == 0
            {
                return Err(io::Error::last_os_error());
            }
            // No worker code (including a venv launcher) may run before the
            // process belongs to the job. All later descendants inherit it.
            command.creation_flags(CREATE_NO_WINDOW | CREATE_SUSPENDED);
            let mut child = command.spawn()?;
            let setup = before_assignment(&child).and_then(|()| {
                if AssignProcessToJobObject(handle, child.as_raw_handle()) == 0 {
                    return Err(io::Error::last_os_error());
                }
                let thread = startup_thread(child.id())?;
                let previous = ResumeThread(thread.as_raw_handle());
                if previous == u32::MAX {
                    return Err(io::Error::last_os_error());
                }
                if previous != 1 {
                    return Err(io::Error::other(
                        "Worker startup thread has an unexpected suspend count",
                    ));
                }
                Ok(())
            });
            if let Err(error) = setup {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
            Ok((child, job))
        }
    }

    pub(super) fn terminate_running(&self) -> bool {
        unsafe {
            let mut info: JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = std::mem::zeroed();
            let queried = QueryInformationJobObject(
                self.0.as_raw_handle(),
                JobObjectBasicAccountingInformation,
                &mut info as *mut _ as *mut std::ffi::c_void,
                std::mem::size_of_val(&info) as u32,
                std::ptr::null_mut(),
            );
            if queried != 0 && info.ActiveProcesses == 0 {
                return false;
            }
            TerminateJobObject(self.0.as_raw_handle(), 1);
            true
        }
    }
}

fn startup_thread(pid: u32) -> io::Result<OwnedHandle> {
    unsafe {
        let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
        if snapshot == INVALID_HANDLE_VALUE {
            return Err(io::Error::last_os_error());
        }
        let snapshot = OwnedHandle::from_raw_handle(snapshot);
        let mut entry: THREADENTRY32 = std::mem::zeroed();
        entry.dwSize = std::mem::size_of_val(&entry) as u32;
        let mut found = Thread32First(snapshot.as_raw_handle(), &mut entry);
        let mut thread_id = None;
        while found != 0 {
            if entry.th32OwnerProcessID == pid && thread_id.replace(entry.th32ThreadID).is_some() {
                return Err(io::Error::other(
                    "Suspended worker has more than one startup thread",
                ));
            }
            found = Thread32Next(snapshot.as_raw_handle(), &mut entry);
        }
        let thread_id = thread_id.ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::NotFound,
                "Worker startup thread was not found",
            )
        })?;
        let handle = OpenThread(
            THREAD_SUSPEND_RESUME | THREAD_QUERY_LIMITED_INFORMATION,
            0,
            thread_id,
        );
        if handle.is_null() {
            return Err(io::Error::last_os_error());
        }
        let handle = OwnedHandle::from_raw_handle(handle);
        if GetProcessIdOfThread(handle.as_raw_handle()) != pid {
            return Err(io::Error::other("Worker startup thread identity changed"));
        }
        Ok(handle)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::windows::io::AsHandle;
    use std::path::PathBuf;
    use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
    use windows_sys::Win32::Foundation::WAIT_OBJECT_0;
    use windows_sys::Win32::System::Threading::{SuspendThread, WaitForSingleObject};

    struct Marker(PathBuf);

    impl Marker {
        fn new() -> Self {
            let unique = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let root = std::env::temp_dir().join(format!(
                "pymss-query-startup-{}-{unique}",
                std::process::id()
            ));
            std::fs::create_dir(&root).unwrap();
            Self(root.join("started"))
        }

        fn command(&self) -> Command {
            let test_module = module_path!().split_once("::").unwrap().1;
            let mut command = Command::new(std::env::current_exe().unwrap());
            command.args([
                "--exact",
                &format!("{test_module}::startup_fixture"),
                "--ignored",
            ]);
            command
                .env("PYMSS_QUERY_STARTUP_MARKER", &self.0)
                .stdin(std::process::Stdio::null())
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null());
            command
        }
    }

    impl Drop for Marker {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.0);
            let _ = std::fs::remove_dir(self.0.parent().unwrap());
        }
    }

    #[test]
    #[ignore = "child process fixture"]
    fn startup_fixture() {
        let marker = std::env::var_os("PYMSS_QUERY_STARTUP_MARKER").unwrap();
        std::fs::write(marker, b"started").unwrap();
        std::thread::sleep(Duration::from_secs(60));
    }

    #[test]
    fn worker_is_suspended_before_job_assignment() {
        let marker = Marker::new();
        let result = QueryJob::spawn_with_setup(&mut marker.command(), |child| {
            let thread = startup_thread(child.id())?;
            let previous = unsafe { SuspendThread(thread.as_raw_handle()) };
            if previous == u32::MAX {
                return Err(io::Error::last_os_error());
            }
            let restored = unsafe { ResumeThread(thread.as_raw_handle()) };
            if previous != 1 || restored != 2 {
                return Err(io::Error::other(
                    "Worker was not suspended before job assignment",
                ));
            }
            std::thread::sleep(Duration::from_millis(100));
            if marker.0.exists() {
                return Err(io::Error::other("Worker executed before job assignment"));
            }
            Ok(())
        });
        let (mut child, job) = result.unwrap();
        let until = Instant::now() + Duration::from_secs(5);
        while !marker.0.exists() && Instant::now() < until {
            std::thread::sleep(Duration::from_millis(5));
        }
        let executed = marker.0.exists();
        job.terminate_running();
        child.wait().unwrap();
        assert!(executed, "The owned worker was not resumed");
    }

    #[test]
    fn failed_startup_reaps_the_suspended_worker_without_executing_it() {
        let marker = Marker::new();
        let mut retained = None;
        let result = QueryJob::spawn_with_setup(&mut marker.command(), |child| {
            retained = Some(child.as_handle().try_clone_to_owned()?);
            Err(io::Error::other("Rejected startup fixture"))
        });
        if let Ok((mut child, job)) = result {
            job.terminate_running();
            let _ = child.wait();
            panic!("The failed startup was accepted");
        }
        let retained = retained.unwrap();
        assert_eq!(
            unsafe { WaitForSingleObject(retained.as_raw_handle(), 0) },
            WAIT_OBJECT_0
        );
        assert!(!marker.0.exists());
    }
}
