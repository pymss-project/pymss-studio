use std::{
    fs::File,
    io,
    os::{
        fd::AsRawFd,
        unix::process::{CommandExt, ExitStatusExt},
    },
    process::{Command, ExitStatus},
    sync::atomic::{AtomicBool, AtomicI32, Ordering::Relaxed},
    thread,
    time::Duration,
};

const POLL_INTERVAL: Duration = Duration::from_millis(20);
const CANCEL_GRACE: Duration = Duration::from_secs(1);
const CANCEL_SIGNALS: [i32; 4] = [libc::SIGHUP, libc::SIGINT, libc::SIGQUIT, libc::SIGTERM];

struct Signals {
    cancel: AtomicI32,
    suspend: AtomicBool,
}

static SIGNALS: Signals = Signals {
    cancel: AtomicI32::new(0),
    suspend: AtomicBool::new(false),
};

#[cfg(target_os = "linux")]
static EXTRACTOR: AtomicI32 = AtomicI32::new(0);

#[cfg(target_os = "linux")]
pub(super) fn set_extractor(pid: libc::pid_t) {
    EXTRACTOR.store(pid, Relaxed);
}

fn extractor() -> Option<libc::pid_t> {
    #[cfg(target_os = "linux")]
    {
        let pid = EXTRACTOR.load(Relaxed);
        if pid != 0 && pid == unsafe { libc::getppid() } {
            return Some(pid);
        }
    }
    None
}

extern "C" fn record_signal(signal: libc::c_int) {
    if signal == libc::SIGTSTP {
        SIGNALS.suspend.store(true, Relaxed);
    } else {
        let _ = SIGNALS.cancel.compare_exchange(0, signal, Relaxed, Relaxed);
    }
}

pub(super) fn install() -> io::Result<()> {
    // waitid must be able to observe the child even if the caller ignored SIGCHLD.
    if unsafe { libc::signal(libc::SIGCHLD, libc::SIG_DFL) } == libc::SIG_ERR {
        return Err(io::Error::last_os_error());
    }
    for signal in CANCEL_SIGNALS.into_iter().chain([libc::SIGTSTP]) {
        let previous =
            unsafe { libc::signal(signal, record_signal as *const () as libc::sighandler_t) };
        if previous == libc::SIG_ERR {
            return Err(io::Error::last_os_error());
        }
        if signal == libc::SIGHUP && previous == libc::SIG_IGN {
            // Preserve nohup's inherited disposition in the worker and its tools.
            unsafe { libc::signal(signal, libc::SIG_IGN) };
        }
    }
    #[cfg(target_os = "linux")]
    adopt_worker_tools()?;
    Ok(())
}

pub(super) fn pending_signal() -> Option<i32> {
    let signal = SIGNALS.cancel.load(Relaxed);
    (signal != 0).then_some(signal)
}

pub(super) fn terminate(signal: i32) -> ! {
    // The AppImage extractor cleans up after a normal exit. It maps a child
    // killed by a signal to 127, so pass the conventional status to it.
    if extractor().is_none() {
        unsafe {
            libc::signal(signal, libc::SIG_DFL);
            let mut mask = std::mem::zeroed();
            libc::sigemptyset(&mut mask);
            libc::sigaddset(&mut mask, signal);
            libc::pthread_sigmask(libc::SIG_UNBLOCK, &mask, std::ptr::null_mut());
            libc::raise(signal);
        }
    }
    std::process::exit(128 + signal)
}

pub(super) fn status(command: &mut Command) -> io::Result<ExitStatus> {
    if let Some(signal) = pending_signal() {
        return Ok(ExitStatus::from_raw(signal));
    }
    let mut child = command.process_group(0).spawn()?;
    let pid = child.id() as libc::pid_t;
    let tty = Terminal::new(pid);
    let outcome = supervise(pid, &tty);
    tty.restore();

    // The leader still reserves the process group ID here, including when it
    // has already exited. Finish the group before reaping it.
    signal_group(pid, libc::SIGKILL)?;
    let status = child.wait()?;
    while worker_tools_running(pid)? {
        thread::sleep(POLL_INTERVAL);
    }
    Ok(outcome?.map_or(status, ExitStatus::from_raw))
}

fn supervise(pid: libc::pid_t, tty: &Terminal) -> io::Result<Option<i32>> {
    loop {
        if let Some(signal) = pending_signal() {
            signal_group(pid, signal)?;
            signal_group(pid, libc::SIGCONT)?;
            return Ok(Some(allow_cleanup(tty, signal)));
        }

        // A running background job can become foreground without SIGCONT.
        tty.give_to_worker();
        let suspend = SIGNALS.suspend.swap(false, Relaxed);
        if let Some(event) = child_event(pid, libc::WEXITED | libc::WSTOPPED | libc::WNOWAIT)? {
            let status = unsafe { event.si_status() };
            match event.si_code {
                libc::CLD_KILLED | libc::CLD_DUMPED if CANCEL_SIGNALS.contains(&status) => {
                    // With a foreground terminal the worker receives Ctrl+C
                    // directly. Give any remaining tools time to finish.
                    return Ok(Some(allow_cleanup(tty, status)));
                }
                libc::CLD_STOPPED => {
                    child_event(pid, libc::WSTOPPED)?;
                    if matches!(status, libc::SIGTTIN | libc::SIGTTOU)
                        && tty.worker_has_terminal()
                        && !suspend
                    {
                        // The worker tried using the terminal before handoff.
                        signal_group(pid, libc::SIGCONT)?;
                    } else {
                        tty.suspend_job()?;
                    }
                }
                _ => return Ok(None),
            }
        } else if suspend {
            tty.suspend_job()?;
        }
        thread::sleep(POLL_INTERVAL);
    }
}

fn allow_cleanup(tty: &Terminal, signal: i32) -> i32 {
    tty.restore();
    // The same group cancellation may reach us directly and via AppImage.
    thread::sleep(CANCEL_GRACE);
    signal
}

fn child_event(pid: libc::pid_t, flags: libc::c_int) -> io::Result<Option<libc::siginfo_t>> {
    loop {
        let mut event: libc::siginfo_t = unsafe { std::mem::zeroed() };
        if unsafe {
            libc::waitid(
                libc::P_PID,
                pid as libc::id_t,
                &mut event,
                flags | libc::WNOHANG,
            )
        } == 0
        {
            return Ok((unsafe { event.si_pid() } != 0).then_some(event));
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
}

fn signal_group(pid: libc::pid_t, signal: libc::c_int) -> io::Result<bool> {
    if unsafe { libc::kill(-pid, signal) } == 0 {
        return Ok(true);
    }
    let error = io::Error::last_os_error();
    match error.raw_os_error() {
        Some(libc::ESRCH) => Ok(false),
        Some(libc::EPERM) if signal == 0 => Ok(true),
        _ => Err(error),
    }
}

struct Terminal {
    tty: Option<File>,
    job: libc::pid_t,
    worker: libc::pid_t,
    owns_job_group: bool,
}

impl Terminal {
    fn new(worker: libc::pid_t) -> Self {
        let job = unsafe { libc::getpgrp() };
        let parent_group = unsafe { libc::getpgid(libc::getppid()) };
        let owns_job_group = job == unsafe { libc::getpid() }
            || extractor() == Some(job)
            || (parent_group > 0 && parent_group != job);
        // Shells with job control disabled can launch asynchronous commands
        // in their own group with stdin redirected from /dev/null.
        let tty = if owns_job_group || unsafe { libc::isatty(libc::STDIN_FILENO) } == 1 {
            File::open("/dev/tty").ok()
        } else {
            None
        };
        Self {
            tty,
            job,
            worker,
            owns_job_group,
        }
    }

    fn foreground(&self) -> Option<libc::pid_t> {
        let fd = self.tty.as_ref()?.as_raw_fd();
        let group = unsafe { libc::tcgetpgrp(fd) };
        (group > 0).then_some(group)
    }

    fn set_foreground(&self, from: libc::pid_t, to: libc::pid_t) {
        let Some(tty) = &self.tty else { return };
        if self.foreground() != Some(from) {
            return;
        }
        // Returning the terminal is a background terminal operation while
        // the worker is in the foreground. Suppress SIGTTOU on this thread.
        unsafe {
            let mut blocked = std::mem::zeroed();
            let mut previous = std::mem::zeroed();
            libc::sigemptyset(&mut blocked);
            libc::sigaddset(&mut blocked, libc::SIGTTOU);
            if libc::pthread_sigmask(libc::SIG_BLOCK, &blocked, &mut previous) == 0 {
                libc::tcsetpgrp(tty.as_raw_fd(), to);
                libc::pthread_sigmask(libc::SIG_SETMASK, &previous, std::ptr::null_mut());
            }
        }
    }

    fn give_to_worker(&self) {
        self.set_foreground(self.job, self.worker);
    }

    fn restore(&self) {
        self.set_foreground(self.worker, self.job);
    }

    fn worker_has_terminal(&self) -> bool {
        self.foreground() == Some(self.worker)
    }

    fn suspend_job(&self) -> io::Result<()> {
        let worker_was_foreground = self.worker_has_terminal();
        signal_group(self.worker, libc::SIGSTOP)?;
        self.restore();
        if self.owns_job_group {
            signal_group(self.job, libc::SIGSTOP)?;
        } else if worker_was_foreground {
            // An outer AppImage or shell wrapper may own the job group. Replay
            // the terminal's stop while respecting the original dispositions
            // of the members of that group.
            let previous = unsafe { libc::signal(libc::SIGTSTP, libc::SIG_DFL) };
            if previous == libc::SIG_ERR {
                return Err(io::Error::last_os_error());
            }
            let result = signal_group(self.job, libc::SIGTSTP);
            unsafe { libc::signal(libc::SIGTSTP, previous) };
            result?;
        } else if unsafe { libc::raise(libc::SIGSTOP) } != 0 {
            return Err(io::Error::last_os_error());
        }
        self.give_to_worker();
        signal_group(self.worker, libc::SIGCONT)?;
        Ok(())
    }
}

#[cfg(target_os = "linux")]
fn adopt_worker_tools() -> io::Result<()> {
    // Reap orphaned tools even on systems with an init that does not do so.
    if unsafe { libc::prctl(libc::PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) } == -1 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn worker_tools_running(pid: libc::pid_t) -> io::Result<bool> {
    loop {
        match unsafe { libc::waitpid(-pid, std::ptr::null_mut(), libc::WNOHANG) } {
            0 => return Ok(true),
            -1 => {
                let error = io::Error::last_os_error();
                match error.raw_os_error() {
                    Some(libc::ECHILD) => return Ok(false),
                    Some(libc::EINTR) => continue,
                    _ => return Err(error),
                }
            }
            _ => {}
        }
    }
}

#[cfg(target_os = "macos")]
fn worker_tools_running(pid: libc::pid_t) -> io::Result<bool> {
    signal_group(pid, 0)
}
