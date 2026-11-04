use std::io;
use std::process::{Child, Command, ExitStatus};
use std::sync::{mpsc, Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

#[cfg(windows)]
#[path = "process_tree_windows.rs"]
mod windows;

#[cfg(windows)]
pub(super) const CREATE_NO_WINDOW: u32 = 0x08000000;

#[cfg(unix)]
pub(super) fn configure_worker(command: &mut Command) {
    use std::os::unix::process::CommandExt;
    command.process_group(0);
}

#[cfg(any(unix, test))]
fn isolated_group_target(pid: u32, parent_group: i32) -> Option<i32> {
    i32::try_from(pid)
        .ok()
        .filter(|pid| *pid > 1 && *pid != parent_group)
        .map(|pid| -pid)
}

pub(crate) fn kill_process_tree(pid: u32) {
    #[cfg(windows)]
    let _ = windows::terminate_tree(pid);
    #[cfg(unix)]
    unsafe {
        // The caller retains its unreaped Child, keeping this group leader's
        // identity reserved. Never signal a shared or the application's group.
        if let Some(group) = isolated_group_target(pid, libc::getpgrp()) {
            if libc::getpgid(pid as libc::pid_t) == pid as libc::pid_t {
                libc::kill(group, libc::SIGKILL);
            }
        }
        // pymss native gates own separate sessions. Leave those gates alive:
        // the worker's closed control pipe makes each gate reap its own group.
    }
}

pub(crate) fn terminate_worker(child: &mut Child) -> io::Result<ExitStatus> {
    // A finished task can race a late cancel request. Do not signal its old
    // PID after another thread has reaped it and released its process identity.
    if child.try_wait()?.is_none() {
        kill_process_tree(child.id());
        let _ = child.kill();
    }
    child.wait()
}

pub(super) struct QueryDeadline {
    child: Arc<Mutex<Child>>,
    stop: mpsc::Sender<()>,
    watchdog: Option<JoinHandle<bool>>,
    #[cfg(windows)]
    _job: Arc<windows::QueryJob>,
}

impl QueryDeadline {
    pub(super) fn spawn(command: &mut Command, timeout: Duration) -> io::Result<Self> {
        #[cfg(windows)]
        let (child, job) = windows::QueryJob::spawn(command)?;
        #[cfg(windows)]
        let job = Arc::new(job);
        #[cfg(unix)]
        configure_worker(command);
        #[cfg(not(windows))]
        let child = command.spawn()?;
        let child = Arc::new(Mutex::new(child));
        #[cfg(windows)]
        let watchdog_job = job.clone();
        let (stop, receive) = mpsc::channel();
        let watched_child = child.clone();
        let watchdog = thread::spawn(move || {
            if receive.recv_timeout(timeout) != Err(mpsc::RecvTimeoutError::Timeout) {
                return false;
            }
            let Ok(mut child) = watched_child.lock() else {
                return false;
            };
            #[cfg(windows)]
            let stopped = watchdog_job.terminate_running();
            #[cfg(not(windows))]
            let stopped = false;
            if child.try_wait().ok().flatten().is_some() {
                return stopped;
            }
            let _ = terminate_worker(&mut child);
            true
        });
        Ok(Self {
            child,
            stop,
            watchdog: Some(watchdog),
            #[cfg(windows)]
            _job: job,
        })
    }

    pub(super) fn child(&self) -> Arc<Mutex<Child>> {
        self.child.clone()
    }

    pub(super) fn finish(mut self) -> bool {
        let _ = self.stop.send(());
        self.watchdog.take().unwrap().join().unwrap_or(false)
    }
}

impl Drop for QueryDeadline {
    fn drop(&mut self) {
        let _ = self.stop.send(());
        if let Some(watchdog) = self.watchdog.take() {
            let _ = watchdog.join();
        }
        if let Ok(mut child) = self.child.lock() {
            let _ = terminate_worker(&mut child);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_invalid_and_shared_process_groups() {
        for pid in [0, 1, 42, i32::MAX as u32 + 1, u32::MAX] {
            assert_eq!(isolated_group_target(pid, 42), None);
        }
    }

    #[test]
    fn targets_only_the_owned_group() {
        assert_eq!(isolated_group_target(43, 42), Some(-43));
        assert_eq!(isolated_group_target(i32::MAX as u32, 42), Some(-i32::MAX));
    }

    fn query_command() -> Command {
        let mut command = Command::new(std::env::current_exe().unwrap());
        let test_module = module_path!().split_once("::").unwrap().1;
        command.args([
            "--exact",
            &format!("{test_module}::query_deadline_fixture"),
            "--ignored",
        ]);
        command
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::null());
        command
    }

    #[test]
    #[ignore = "child process fixture"]
    fn query_deadline_fixture() {
        if std::env::var_os("PYMSS_QUERY_FIXTURE_EXIT").is_none() {
            thread::sleep(Duration::from_secs(60));
        }
    }

    #[test]
    fn query_deadline_stops_blocked_output_and_allows_a_fresh_query() {
        use std::io::Read;

        for _ in 0..2 {
            let deadline =
                QueryDeadline::spawn(&mut query_command(), Duration::from_millis(100)).unwrap();
            let child = deadline.child();
            let mut stdout = child.lock().unwrap().stdout.take().unwrap();
            stdout.read_to_end(&mut Vec::new()).unwrap();
            assert!(deadline.finish());
            assert!(child.lock().unwrap().try_wait().unwrap().is_some());
        }
    }

    #[test]
    fn completed_query_cancels_its_watchdog_without_waiting_for_the_deadline() {
        let deadline = QueryDeadline::spawn(&mut query_command(), Duration::from_secs(10)).unwrap();
        let child = deadline.child();
        let mut child = child.lock().unwrap();
        terminate_worker(&mut child).unwrap();
        drop(child);
        assert!(!deadline.finish());
    }

    #[test]
    fn late_query_deadline_does_not_signal_a_reaped_child() {
        let deadline =
            QueryDeadline::spawn(&mut query_command(), Duration::from_millis(100)).unwrap();
        let child = deadline.child();
        terminate_worker(&mut child.lock().unwrap()).unwrap();
        thread::sleep(Duration::from_millis(150));
        assert!(!deadline.finish());
    }

    #[test]
    fn dropping_an_unfinished_query_reaps_its_worker() {
        let deadline = QueryDeadline::spawn(&mut query_command(), Duration::from_secs(10)).unwrap();
        let child = deadline.child();
        drop(deadline);
        assert!(child.lock().unwrap().try_wait().unwrap().is_some());
    }

    #[test]
    fn query_can_finish_before_its_watchdog_is_attached() {
        let mut command = query_command();
        command.env("PYMSS_QUERY_FIXTURE_EXIT", "1");
        let deadline = QueryDeadline::spawn(&mut command, Duration::from_secs(10)).unwrap();
        let child = deadline.child();
        let status = loop {
            if let Some(status) = child.lock().unwrap().try_wait().unwrap() {
                break status;
            }
            thread::sleep(Duration::from_millis(5));
        };
        assert!(status.success());
        assert!(!deadline.finish());
    }
}
