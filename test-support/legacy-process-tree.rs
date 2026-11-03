use std::process::Command;
#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
pub(super) const CREATE_NO_WINDOW:u32=0x08000000;
#[cfg(unix)]
pub(super) fn configure_worker(_command:&mut Command) {}
pub(crate) fn kill_process_tree(pid: u32) {
    #[cfg(windows)]
    let _ = Command::new("taskkill")
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .creation_flags(CREATE_NO_WINDOW)
        .status();
    #[cfg(not(windows))]
    let _ = Command::new("pkill").args(["-TERM", "-P", &pid.to_string()]).status();
}

pub(crate) fn terminate_worker(child:&mut std::process::Child)->std::io::Result<std::process::ExitStatus> {
kill_process_tree(child.id());
let _=child.kill();
child.wait()
}
