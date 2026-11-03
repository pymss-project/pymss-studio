use std::io;
use std::process::{Child, Command, ExitStatus};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

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
    let _ = Command::new("taskkill")
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .creation_flags(CREATE_NO_WINDOW)
        .status();
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
}
