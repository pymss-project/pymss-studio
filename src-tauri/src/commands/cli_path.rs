use crate::error::{AppError, AppResult};
use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum CliPathAction {
    Status,
    Diagnostics,
    Enable,
    Disable,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
enum Registration {
    Available,
    Enabled,
    Conflict,
    Moved,
}

#[derive(Deserialize, Serialize)]
pub struct CliPathStatus {
    status: Registration,
    owned: bool,
    path: Option<String>,
    changed: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    diagnostics: Option<CliPathDiagnostics>,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct CliPathDiagnostics {
    launcher: String,
    launcher_exists: bool,
    user_path_contains_studio: bool,
    system_path_contains_studio: bool,
    registered_directory: Option<String>,
    persisted_command: Option<String>,
    process_command: Option<String>,
}

#[tauri::command]
pub async fn manage_cli_path(
    window: tauri::WebviewWindow,
    action: CliPathAction,
) -> AppResult<CliPathStatus> {
    if window.label() != "main" {
        return Err(AppError::Worker(
            "Open Settings in the main Studio window.".into(),
        ));
    }
    #[cfg(all(windows, not(feature = "vst-lab")))]
    {
        tauri::async_runtime::spawn_blocking(move || {
            let launcher = crate::storage::cli_launcher_path()?
                .ok_or_else(|| AppError::Worker("Unable to locate the Studio directory.".into()))?;
            if !matches!(action, CliPathAction::Diagnostics) && !launcher.is_file() {
                return Err(AppError::Worker(
                    "The Studio command-line launcher is missing.".into(),
                ));
            }
            windows::apply(
                launcher
                    .parent()
                    .ok_or_else(|| AppError::Worker("Invalid launcher path".into()))?,
                action,
            )
        })
        .await
        .map_err(|e| AppError::Worker(e.to_string()))?
    }
    #[cfg(not(all(windows, not(feature = "vst-lab"))))]
    {
        let _ = action;
        Err(AppError::Worker(
            "CLI PATH registration is available in Pymss Studio for Windows.".into(),
        ))
    }
}

#[cfg(all(windows, not(feature = "vst-lab")))]
mod windows {
    use super::*;
    use std::{
        ffi::OsString,
        io::Write,
        os::windows::{ffi::OsStringExt, process::CommandExt},
        path::{Path, PathBuf},
        process::{Command, Stdio},
    };
    use windows_sys::Win32::{
        System::{SystemInformation::GetSystemDirectoryW, Threading::CREATE_NO_WINDOW},
        UI::WindowsAndMessaging::{
            SendMessageTimeoutW, HWND_BROADCAST, SMTO_ABORTIFHUNG, WM_SETTINGCHANGE,
        },
    };

    const SCRIPT: &str = include_str!("../../../installer/cli-path.ps1");

    fn powershell() -> AppResult<PathBuf> {
        let mut buffer = [0; 260];
        let len = unsafe { GetSystemDirectoryW(buffer.as_mut_ptr(), buffer.len() as u32) } as usize;
        if len == 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        if len >= buffer.len() {
            return Err(AppError::Worker(
                "Windows system directory is too long".into(),
            ));
        }
        Ok(PathBuf::from(OsString::from_wide(&buffer[..len]))
            .join(r"WindowsPowerShell\v1.0\powershell.exe"))
    }

    pub(super) fn apply(directory: &Path, action: CliPathAction) -> AppResult<CliPathStatus> {
        let verb = match action {
            CliPathAction::Status => "Status",
            CliPathAction::Diagnostics => "Diagnostics",
            CliPathAction::Enable => "Enable",
            CliPathAction::Disable => "Disable",
        };
        // Embed the same helper as Setup so portable archives and managed updates
        // work without an installer. Directory names never appear in shell code.
        let mut child = Command::new(powershell()?)
            .args(["-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                "[Console]::InputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); try { & ([scriptblock]::Create([Console]::In.ReadToEnd())) -Action $env:PYMSS_CLI_ACTION -StudioDir $env:PYMSS_CLI_DIRECTORY } catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }"])
            .env("PYMSS_CLI_ACTION", verb).env("PYMSS_CLI_DIRECTORY", directory)
            .creation_flags(CREATE_NO_WINDOW)
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn()?;
        let sent = child
            .stdin
            .take()
            .expect("piped stdin")
            .write_all(SCRIPT.as_bytes());
        let output = child.wait_with_output()?;
        if !output.status.success() {
            let reason = String::from_utf8_lossy(&output.stderr);
            return Err(AppError::Worker(format!(
                "PATH registration failed ({}): {}",
                output.status,
                reason.trim()
            )));
        }
        sent?;
        let state: CliPathStatus = serde_json::from_slice(&output.stdout)?;
        if state.changed {
            let environment = windows_sys::core::w!("Environment");
            unsafe {
                SendMessageTimeoutW(
                    HWND_BROADCAST,
                    WM_SETTINGCHANGE,
                    0,
                    environment as isize,
                    SMTO_ABORTIFHUNG,
                    200,
                    std::ptr::null_mut(),
                );
            }
        }
        Ok(state)
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn reads_registration_for_a_portable_path_without_writing_it() {
            let root = tempfile::Builder::new()
                .prefix("Studio 便携 ' ")
                .tempdir()
                .unwrap();
            let state = apply(root.path(), CliPathAction::Diagnostics).unwrap();
            assert!(!state.changed && !state.owned);
            assert!(!matches!(state.status, Registration::Enabled));
            let details = state.diagnostics.unwrap();
            assert_eq!(Path::new(&details.launcher), root.path().join("pymss.exe"));
            assert!(!details.launcher_exists);
            assert!(!details.user_path_contains_studio && !details.system_path_contains_studio);
        }
    }
}
