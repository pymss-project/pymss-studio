//! Isolated, opt-in native audio host. The UI process never loads plugin code.
use base64::Engine as _;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Emitter, State};

mod blacklist;
mod diagnostics;
pub(crate) use diagnostics::snapshot as diagnostic_snapshot;
mod dialogs;
mod process_exit;
#[cfg(test)]
mod dialog_tests;
mod scan_control;
mod scan_pipeline;
#[cfg(test)]
mod scan_tests;
#[cfg(test)]
mod cache_tests;
#[cfg(test)]
mod progress_tests;
use blacklist::{BlacklistEntry, BlacklistReason};
use scan_control::{Decision as ScanDecision, ScanControl, ScanRun, Ticket as ScanTicket};
#[cfg(test)]
use scan_pipeline::scan_bundles;

const REQUEST_TIMEOUT: Duration = Duration::from_secs(20);
const SCAN_TIMEOUT: Duration = Duration::from_secs(10);
const RENDER_TIMEOUT: Duration = Duration::from_secs(60 * 30);
const MAX_STATE_BYTES: usize = 32 * 1024 * 1024;
const MAX_RESPONSE_BYTES: usize = 128 * 1024 * 1024;
const MAX_REQUEST_BYTES: usize = 64 * 1024 * 1024;
const MAX_CACHE_BYTES: u64 = 8 * 1024 * 1024;
const SCAN_CACHE_VERSION: u32 = 3;

#[derive(Default)]
struct Session {
    generation: u64,
    project_id: Option<String>,
    native_session_id: Option<String>,
    process: Option<HostProcess>,
}

#[derive(Clone, Default)]
pub struct PluginHostState {
    session: Arc<Mutex<Session>>,
    session_pid: Arc<ProcessLease>,
    renders: Arc<Mutex<HashMap<String, Arc<ProcessLease>>>>,
    scan_lock: Arc<Mutex<()>>,
    scan_process: Arc<ProcessLease>,
    scan_control: Arc<ScanControl>,
    scan_storage: Arc<Mutex<()>>,
    scan_sequence: Arc<AtomicU64>,
    generation: Arc<AtomicU64>,
    stopping: Arc<AtomicBool>,
}

impl PluginHostState {
    pub fn shutdown(&self) {
        self.stopping.store(true, Ordering::Release);
        self.generation.fetch_add(1, Ordering::AcqRel);
        self.session_pid.cancel();
        self.scan_process.cancel();
        self.scan_control.shutdown();
        if let Ok(renders) = self.renders.lock() {
            for pid in renders.values() {
                pid.cancel();
            }
        }
        if let Ok(mut session) = self.session.lock() {
            session.process.take();
            session.project_id = None;
        }
    }
}

#[derive(Debug, PartialEq)]
struct HostCapabilities {
    enabled: bool,
    native_editor: bool,
}

fn platform_capabilities(host: bool, platform: &str, display: bool) -> HostCapabilities {
    let enabled = host && matches!(platform, "windows" | "macos" | "linux");
    HostCapabilities {
        enabled,
        native_editor: enabled && (platform != "linux" || display),
    }
}

fn host_capabilities() -> HostCapabilities {
    platform_capabilities(
        cfg!(feature = "native-plugins"),
        std::env::consts::OS,
        std::env::var_os("DISPLAY").is_some_and(|value| !value.is_empty()),
    )
}

fn enabled() -> bool {
    host_capabilities().enabled
}

fn require_enabled() -> Result<(), String> {
    if enabled() {
        Ok(())
    } else {
        Err("Native plugin hosting requires a build with native-plugins enabled on Windows, macOS, or Linux".into())
    }
}

#[tauri::command]
pub fn plugin_host_capabilities() -> Value {
    let capabilities = host_capabilities();
    #[cfg(feature = "native-plugins")]
    let (formats, native_editor_formats) = if capabilities.enabled {
        (
            pymss_audio_host::supported_formats(),
            if capabilities.native_editor {
                pymss_audio_host::native_editor_formats()
            } else {
                Vec::new()
            },
        )
    } else {
        (Vec::new(), Vec::new())
    };
    #[cfg(not(feature = "native-plugins"))]
    let (formats, native_editor_formats): (Vec<&str>, Vec<&str>) = (Vec::new(), Vec::new());
    #[cfg(feature = "native-plugins")]
    let default_directories = if capabilities.enabled {
        pymss_audio_host::default_search_paths().into_iter().filter(|path| path.is_absolute()).collect::<Vec<_>>()
    } else { Vec::new() };
    #[cfg(not(feature = "native-plugins"))]
    let default_directories: Vec<PathBuf> = Vec::new();
    json!({
        "enabled": capabilities.enabled, "platform": std::env::consts::OS,
        "formats": formats, "nativeEditorFormats": native_editor_formats,
        "recording": capabilities.enabled,
        "nativeEditor": capabilities.native_editor, "experimental": true,
        "labBuild": cfg!(feature = "vst-lab"),
        "scanControl": capabilities.enabled,
        "catalogCache": capabilities.enabled,
        "nativeTransport": capabilities.enabled && cfg!(any(windows, target_os = "macos")),
        "defaultDirectories": default_directories,
    })
}

/// Report build capabilities before desktop, storage, logging or singleton startup.
/// This also handles builds without the optional host, so packaging checks never open a GUI.
pub fn run_capabilities_cli() -> Option<i32> {
    let mut args = std::env::args_os().skip(1);
    if args.next().as_deref() != Some(std::ffi::OsStr::new("--plugin-host-capabilities")) {
        return None;
    }
    if args.next().is_some() {
        eprintln!("The plugin capability query does not accept additional arguments");
        return Some(2);
    }
    let mut output = std::io::stdout().lock();
    let result = serde_json::to_writer(&mut output, &plugin_host_capabilities())
        .map_err(std::io::Error::other)
        .and_then(|()| output.write_all(b"\n"))
        .and_then(|()| output.flush());
    Some(match result {
        Ok(()) => 0,
        Err(error) => {
            eprintln!("Cannot write plugin host capabilities: {error}");
            1
        }
    })
}

/// Establish lab storage before logging, recovery, or Tauri initializes.
pub fn configure_lab_environment() {
    #[cfg(feature = "vst-lab")]
    if std::env::var_os("PYMSS_STUDIO_DATA_ROOT").is_none_or(|value| value.is_empty()) {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .expect("the application manifest has a project parent")
            .join("data-vst-lab");
        std::env::set_var("PYMSS_STUDIO_DATA_ROOT", root);
    }
}

/// Cancellation retains a particular process identity, never a reusable PID.
#[derive(Default)]
struct ProcessLease(Mutex<LeaseState>);

#[derive(Default)]
struct LeaseState {
    cancelled: bool,
    process: Option<Arc<ProcessControl>>,
}

impl ProcessLease {
    fn id(&self) -> u32 {
        let state = self.0.lock().unwrap_or_else(|error| error.into_inner());
        if state.cancelled {
            u32::MAX
        } else {
            state.process.as_ref().map_or(0, |process| process.pid)
        }
    }

    fn publish(&self, process: &HostProcess) -> Result<(), String> {
        let mut state = self.0.lock().unwrap_or_else(|error| error.into_inner());
        if state.cancelled {
            return Err("The audio host operation was cancelled".into());
        }
        if state.process.is_some() {
            return Err("The audio host operation already owns a process".into());
        }
        state.process = Some(process.control.clone());
        Ok(())
    }

    fn cancel(&self) -> u32 {
        let mut state = self.0.lock().unwrap_or_else(|error| error.into_inner());
        let previous = if state.cancelled {
            u32::MAX
        } else {
            state.process.as_ref().map_or(0, |process| process.pid)
        };
        state.cancelled = true;
        if let Some(process) = state.process.take() {
            process.stop();
        }
        previous
    }

    fn clear(&self) {
        *self.0.lock().unwrap_or_else(|error| error.into_inner()) = LeaseState::default();
    }

    fn release(&self) {
        // Reuse the scanner lease without clearing a concurrent shutdown.
        self.0
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .process = None;
    }
}

struct ProcessControl {
    pid: u32,
    // Signalling and reaping must hold the same guard: a reaped PID may be reused.
    active: Mutex<bool>,
}

impl ProcessControl {
    fn stop(&self) {
        let active = self
            .active
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if *active {
            terminate_helper(self.pid);
        }
    }

    fn reap(&self, child: &mut Child) {
        let mut active = self
            .active
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if *active {
            // Signal the group before waiting, including when its leader has
            // exited: decoder descendants may still keep its pipes open.
            terminate_helper(self.pid);
            let _ = child.kill();
            let _ = child.wait();
            *active = false;
        }
    }
}

#[cfg(any(unix, test))]
fn isolated_group_target(pid: u32, parent_group: i32) -> Option<i32> {
    i32::try_from(pid)
        .ok()
        .filter(|pid| *pid > 1 && *pid != parent_group)
        .map(|pid| -pid)
}

fn terminate_helper(pid: u32) {
    #[cfg(unix)]
    unsafe {
        // Every helper is spawned with process_group(0), so descendants inherit
        // a group whose leader is this unreaped child. Never signal our group.
        if let Some(group) = isolated_group_target(pid, libc::getpgrp()) {
            if libc::getpgid(pid as libc::pid_t) == pid as libc::pid_t {
                libc::kill(group, libc::SIGKILL);
            }
        }
    }
    #[cfg(windows)]
    if pid != 0 && pid != u32::MAX {
        crate::python::worker::kill_process_tree(pid);
    }
    #[cfg(not(any(unix, windows)))]
    let _ = pid;
}

#[cfg(windows)]
struct ProcessJob(isize);

#[cfg(windows)]
impl ProcessJob {
    fn attach(child: &Child) -> Result<Self, String> {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
            SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        };
        // The job outlives the child and owns all decoder descendants. Closing it
        // also terminates them if the UI exits without receiving a response.
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job.is_null() {
                return Err(std::io::Error::last_os_error().to_string());
            }
            let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &limits as *const _ as *const std::ffi::c_void,
                std::mem::size_of_val(&limits) as u32,
            ) == 0
                || AssignProcessToJobObject(job, child.as_raw_handle()) == 0
            {
                let error = std::io::Error::last_os_error().to_string();
                CloseHandle(job);
                return Err(error);
            }
            Ok(Self(job as isize))
        }
    }
}

#[cfg(windows)]
impl Drop for ProcessJob {
    fn drop(&mut self) {
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.0 as _);
        }
    }
}

#[derive(Debug)]
enum HostRequestError {
    /// A valid native response rejected this operation; its process remains usable.
    Operation(String),
    /// Local validation rejected a request before it was written to the child.
    Request(String),
    /// A live helper awaits explicit user interaction; one response is still outstanding.
    DialogPending(String),
    /// The process or JSON-lines channel can no longer be trusted.
    Transport(String),
}

impl HostRequestError {
    fn is_fatal(&self) -> bool {
        matches!(self, Self::Transport(_))
    }
}

impl std::fmt::Display for HostRequestError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let message = match self {
            Self::Operation(message) | Self::Request(message) | Self::DialogPending(message) | Self::Transport(message) => message,
        };
        formatter.write_str(message)
    }
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct NativeRenderProgress {
    phase: String,
    completed: u64,
    total: u64,
}

fn receive_render_result(
    id: u64,
    started: std::time::Instant,
    timeout: Duration,
    mut receive: impl FnMut(Duration) -> Result<Value, HostRequestError>,
    mut progress: impl FnMut(NativeRenderProgress) -> Result<(), HostRequestError>,
) -> Result<Value, HostRequestError> {
    loop {
        let remaining = timeout.checked_sub(started.elapsed()).filter(|remaining| !remaining.is_zero())
            .ok_or_else(|| HostRequestError::Transport("The audio host timed out and was terminated".into()))?;
        let response = receive(remaining)?;
        if let Some(update) = decode_render_progress(&response, id)? {
            progress(update)?;
        } else {
            return decode_response(response, id);
        }
    }
}

fn decode_render_progress(response: &Value, id: u64) -> Result<Option<NativeRenderProgress>, HostRequestError> {
    if response.get("event").is_none() { return Ok(None); }
    if response["id"].as_u64() != Some(id) || response["event"].as_str() != Some("render_progress")
        || response.get("ok").is_some() || response.get("result").is_some() {
        return Err(HostRequestError::Transport("The audio host returned an invalid progress event".into()));
    }
    let progress: NativeRenderProgress = serde_json::from_value(response["payload"].clone())
        .map_err(|_| HostRequestError::Transport("The audio host returned invalid render progress".into()))?;
    if progress.total == 0 || progress.completed > progress.total
        || !matches!(progress.phase.as_str(), "preparing" | "rendering" | "finalizing" | "completed") {
        return Err(HostRequestError::Transport("The audio host returned invalid render progress".into()));
    }
    Ok(Some(progress))
}

struct HostProcess {
    child: Child,
    control: Arc<ProcessControl>,
    input: Option<mpsc::SyncSender<(Vec<u8>, mpsc::SyncSender<Result<(), String>>)>>,
    responses: mpsc::Receiver<Result<Value, String>>,
    sequence: u64,
    pending: Option<dialogs::Pending>,
    completed_session_id: Option<String>,
    live_closed: bool,
    #[cfg(windows)]
    _job: ProcessJob,
}

impl HostProcess {
    fn spawn(args: &[&std::ffi::OsStr]) -> Result<Self, String> {
        let mut command = Command::new(std::env::current_exe().map_err(|error| error.to_string())?);
        command.args(args);
        Self::spawn_command(command)
    }

    fn spawn_command(mut command: Command) -> Result<Self, String> {
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        let mut child = command
            .spawn()
            .map_err(|error| format!("Unable to start the audio host: {error}"))?;
        let control = Arc::new(ProcessControl {
            pid: child.id(),
            active: Mutex::new(true),
        });
        #[cfg(windows)]
        let job = match ProcessJob::attach(&child) {
            Ok(job) => job,
            Err(error) => {
                control.reap(&mut child);
                return Err(format!("Unable to isolate the audio host process: {error}"));
            }
        };
        let Some(mut child_input) = child.stdin.take() else {
            control.reap(&mut child);
            return Err("Audio host input pipe is unavailable".into());
        };
        let Some(output) = child.stdout.take() else {
            drop(child_input);
            control.reap(&mut child);
            return Err("Audio host output pipe is unavailable".into());
        };
        if let Some(stderr) = child.stderr.take() {
            let child_pid = child.id();
            std::thread::spawn(move || {
                let mut reader = BufReader::new(stderr);
                loop {
                    let mut line = Vec::new();
                    match (&mut reader).take(4097).read_until(b'\n', &mut line) {
                        Ok(0) | Err(_) => break,
                        Ok(_) => {
                            // Persist only our bounded, structured AU lifecycle records. Other
                            // vendor output retains its original stderr destination.
                            if line.len() <= 4096 && line.starts_with(b"[audio.au]") {
                                crate::session_log::early("INFO", "audio.au.lifecycle", &format!("pid={child_pid} {}", String::from_utf8_lossy(&line).trim_end()));
                            }
                            let _ = std::io::stderr().write_all(&line);
                        }
                    }
                }
            });
        }
        let (input, writes) =
            mpsc::sync_channel::<(Vec<u8>, mpsc::SyncSender<Result<(), String>>)>(1);
        std::thread::spawn(move || {
            while let Ok((bytes, completed)) = writes.recv() {
                let result = child_input
                    .write_all(&bytes)
                    .and_then(|_| child_input.flush())
                    .map_err(|error| format!("Unable to send an audio host request: {error}"));
                let failed = result.is_err();
                let _ = completed.send(result);
                if failed {
                    break;
                }
            }
        });
        let (sender, responses) = mpsc::sync_channel(1);
        std::thread::spawn(move || {
            let mut reader = BufReader::new(output);
            loop {
                let mut line = Vec::new();
                let read = (&mut reader)
                    .take(MAX_RESPONSE_BYTES as u64 + 1)
                    .read_until(b'\n', &mut line);
                let result = match read {
                    Ok(0) => Err("The audio host exited unexpectedly".to_string()),
                    Ok(_) if line.len() > MAX_RESPONSE_BYTES => {
                        Err("The audio host response exceeded the size limit".into())
                    }
                    Ok(_) => serde_json::from_slice(&line)
                        .map_err(|_| "The audio host returned an invalid response".into()),
                    Err(error) => Err(format!("Unable to read the audio host response: {error}")),
                };
                let failed = result.is_err();
                if sender.send(result).is_err() || failed {
                    break;
                }
            }
        });
        Ok(Self {
            child,
            control,
            input: Some(input),
            responses,
            sequence: 0,
            pending: None,
            completed_session_id: None,
            live_closed: false,
            #[cfg(windows)]
            _job: job,
        })
    }

    fn request(
        &mut self,
        request: Value,
        timeout: Duration,
    ) -> Result<Value, HostRequestError> {
        let (id, started) = self.send_request(request, timeout)?;
        let response = self.receive(timeout.saturating_sub(started.elapsed()))?;
        decode_response(response, id)
    }

    fn request_with_progress(
        &mut self,
        mut request: Value,
        timeout: Duration,
        mut progress: impl FnMut(NativeRenderProgress) -> Result<(), HostRequestError>,
    ) -> Result<Value, HostRequestError> {
        request.as_object_mut().ok_or_else(|| HostRequestError::Request("Audio host request must be an object".into()))?
            .insert("progress".into(), json!(true));
        let (id, started) = self.send_request(request, timeout)?;
        receive_render_result(id, started, timeout, |remaining| self.receive(remaining), &mut progress)
    }

    fn send_request(&mut self, mut request: Value, timeout: Duration) -> Result<(u64, std::time::Instant), HostRequestError> {
        self.sequence = self.sequence.checked_add(1).ok_or_else(|| {
            HostRequestError::Transport("Audio host request sequence exhausted".into())
        })?;
        let id = self.sequence;
        request
            .as_object_mut()
            .ok_or_else(|| {
                HostRequestError::Request("Audio host request must be an object".into())
            })?
            .insert("id".into(), json!(id));
        let bytes = encode_request(&request)?;
        let started = std::time::Instant::now();
        let (completed, written) = mpsc::sync_channel(1);
        self.input
            .as_ref()
            .ok_or_else(|| HostRequestError::Transport("The audio host input pipe closed".into()))?
            .send((bytes, completed))
            .map_err(|_| HostRequestError::Transport("The audio host input pipe closed".into()))?;
        written
            .recv_timeout(timeout)
            .map_err(|_| {
                HostRequestError::Transport(
                    "The audio host input timed out and was terminated".into(),
                )
            })?
            .map_err(HostRequestError::Transport)?;
        Ok((id, started))
    }

    fn request_live(&mut self, request: Value, timeout: Duration) -> Result<Value, HostRequestError> {
        self.request_live_with(request, timeout, &mut dialogs::owned_dialog)
    }

    fn request_live_with(&mut self, request: Value, timeout: Duration, detector: &mut impl FnMut(u32) -> Option<String>) -> Result<Value, HostRequestError> {
        self.finish_pending_with(detector)?;
        let operation = request.get("op").and_then(Value::as_str).unwrap_or("").to_owned();
        if self.live_closed {
            return if matches!(operation.as_str(), "unload" | "shutdown") { Ok(json!({"closed":true})) }
                else { Err(HostRequestError::Operation("The previous request closed the audio session. Reload the project before retrying".into())) };
        }
        let (id, started) = self.send_request(request, timeout)?;
        self.pending = Some(dialogs::Pending::new(id, operation, started, timeout));
        self.receive_live_with(detector)
    }

    fn finish_pending_with(&mut self, detector: &mut impl FnMut(u32) -> Option<String>) -> Result<(), HostRequestError> {
        if self.pending.is_some() { self.receive_live_with(detector)?; }
        Ok(())
    }

    fn receive_live_with(&mut self, detector: &mut impl FnMut(u32) -> Option<String>) -> Result<Value, HostRequestError> {
        loop {
            // Always consume a completed response before inspecting windows. A dialog may
            // outlive its SDK call, and a dead helper must never be retained on that evidence.
            let response = match self.responses.try_recv() {
                Ok(value) => Some(value),
                Err(mpsc::TryRecvError::Disconnected) => return Err(HostRequestError::Transport("The audio host response pipe closed".into())),
                Err(mpsc::TryRecvError::Empty) => None,
            };
            if let Some(response) = response { return self.complete_live(response); }
            if self.pending.as_ref().is_some_and(dialogs::Pending::is_suspended) {
                if let Some(title) = detector(self.control.pid) {
                    return Err(HostRequestError::DialogPending(dialogs::message(&title)));
                }
            }
            let remaining = self.pending.as_mut().expect("a live request is outstanding").remaining();
            if remaining.is_zero() {
                if let Some(title) = detector(self.control.pid) {
                    self.pending.as_mut().expect("a live request is outstanding").suspend();
                    return Err(HostRequestError::DialogPending(dialogs::message(&title)));
                }
                return Err(HostRequestError::Transport("The audio host timed out and was terminated".into()));
            }
            // Give normal fast RPCs a chance to complete even if a plugin also owns a modeless
            // dialog. A real modal blocks the UI dispatch and leaves this response outstanding.
            match self.responses.recv_timeout(remaining.min(Duration::from_millis(50))) {
                Ok(value) => return self.complete_live(value),
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(mpsc::RecvTimeoutError::Disconnected) => return Err(HostRequestError::Transport("The audio host response pipe closed".into())),
            }
            if let Some(title) = detector(self.control.pid) {
                self.pending.as_mut().expect("a live request is outstanding").suspend();
                return Err(HostRequestError::DialogPending(dialogs::message(&title)));
            }
        }
    }

    fn complete_live(&mut self, response: Result<Value, String>) -> Result<Value, HostRequestError> {
        let pending = self.pending.take().expect("a live request is outstanding");
        let result = decode_response(response.map_err(HostRequestError::Transport)?, pending.id);
        if let Ok(value) = &result {
            if matches!(pending.operation.as_str(), "shutdown" | "unload") { self.live_closed = true; }
            if let Some(id) = value.get("sessionId").and_then(Value::as_str).filter(|id| !id.is_empty()) {
                self.completed_session_id = Some(id.to_owned());
            }
        }
        result
    }

    fn receive(&self, timeout: Duration) -> Result<Value, HostRequestError> {
        self.responses
            .recv_timeout(timeout)
            .map_err(|error| {
                HostRequestError::Transport(match error {
                    mpsc::RecvTimeoutError::Timeout => {
                        "The audio host timed out and was terminated".into()
                    }
                    mpsc::RecvTimeoutError::Disconnected => {
                        "The audio host response pipe closed".into()
                    }
                })
            })?
            .map_err(HostRequestError::Transport)
    }
}

impl Drop for HostProcess {
    fn drop(&mut self) {
        // Release the writer's sender so an idle helper also receives EOF.
        self.input.take();
        self.control.reap(&mut self.child);
    }
}

fn encode_request(request: &Value) -> Result<Vec<u8>, HostRequestError> {
    let mut bytes = serde_json::to_vec(request)
        .map_err(|error| HostRequestError::Request(error.to_string()))?;
    validate_request_length(bytes.len())?;
    bytes.push(b'\n');
    Ok(bytes)
}

fn validate_request_length(bytes: usize) -> Result<(), HostRequestError> {
    // The native reader's bound includes the terminating newline.
    if bytes >= MAX_REQUEST_BYTES {
        Err(HostRequestError::Request(
            "Audio host request exceeded the size limit".into(),
        ))
    } else {
        Ok(())
    }
}

fn decode_response(response: Value, expected_id: u64) -> Result<Value, HostRequestError> {
    if response.get("id").and_then(Value::as_u64) != Some(expected_id) {
        return Err(HostRequestError::Transport(
            "The audio host returned a stale response".into(),
        ));
    }
    match response.get("ok").and_then(Value::as_bool) {
        Some(true) => response.get("result").cloned().ok_or_else(|| {
            HostRequestError::Transport("The audio host returned an invalid result".into())
        }),
        Some(false) => match response.get("error").and_then(Value::as_str) {
            Some(error) => Err(HostRequestError::Operation(error.into())),
            None => Err(HostRequestError::Transport(
                "The audio host returned an invalid error response".into(),
            )),
        },
        None => Err(HostRequestError::Transport(
            "The audio host returned an invalid response".into(),
        )),
    }
}

fn close_session(session: &mut Session, pid: &ProcessLease) {
    pid.clear();
    session.process.take();
    session.project_id = None;
    session.native_session_id = None;
}

fn discard_session(session: &mut Session, pid: &ProcessLease, project: &str, generation: u64, native_session: &str) -> Value {
    // An old window may release its own unavailable session, but never a newer project's graph.
    let owned = session.project_id.as_deref() == Some(project) && session.generation == generation
        && session.native_session_id.as_deref() == Some(native_session);
    let absent = session.generation == generation && session.project_id.is_none()
        && session.native_session_id.is_none() && session.process.is_none();
    if owned {
        close_session(session, pid);
    }
    json!({ "closed": true, "superseded": !owned && !absent, "generation": generation })
}

fn begin_load_generation(state: &PluginHostState, expected: Option<u64>) -> Result<u64, String> {
    if let Some(expected) = expected {
        let next = expected.checked_add(1).ok_or("Audio host generation exhausted")?;
        state.generation.compare_exchange(expected, next, Ordering::AcqRel, Ordering::Acquire)
            .map(|_| next).map_err(|_| "The audio session changed before recovery could reload it".into())
    } else {
        Ok(state.generation.fetch_add(1, Ordering::AcqRel) + 1)
    }
}

fn prepare_session_request(
    session: &mut Session,
    pid: &ProcessLease,
    loading: bool,
    prepare: impl FnOnce() -> Result<(), String>,
) -> Result<(), String> {
    let result = prepare();
    // Local update validation has not touched the child. Keep its controllers
    // available so recovery can capture edits before fixing the project.
    if loading && result.is_err() {
        close_session(session, pid);
    }
    result
}

fn request_session(
    session: &mut Session,
    pid: &ProcessLease,
    request: Value,
    timeout: Duration,
) -> Result<Value, HostRequestError> {
    let operation = request.get("op").and_then(Value::as_str).unwrap_or("").to_owned();
    let closing = matches!(
        request.get("op").and_then(Value::as_str),
        Some("shutdown" | "unload")
    );
    let mut result = match session.process.as_mut() {
        Some(process) => process.request_live(request, timeout),
        None => Err(HostRequestError::Transport(
            "The audio host session is closed".into(),
        )),
    };
    if let Err(HostRequestError::Transport(message)) = &mut result {
        if let Some(process) = session.process.as_ref() {
            let grace = if matches!(message.as_str(), "The audio host exited unexpectedly" | "The audio host response pipe closed") {
                Duration::from_millis(500)
            } else { Duration::ZERO };
            let observed = {
                // Observe without reaping. Cleanup still signals the owned process group before
                // releasing its PID, and cancellation uses this same identity guard.
                let active = process.control.active.lock().unwrap_or_else(|error| error.into_inner());
                if *active { process_exit::observe(&process.child, grace) } else { Ok(None) }
            };
            match observed {
                Ok(Some(status)) => {
                    message.push_str(&format!(" ({status})"));
                    crate::session_log::early("ERROR", "audio.plugin_host.exit", &format!("pid={} operation={operation} status={status}", process.control.pid));
                }
                other => {
                    let detail = other.err().map(|error| error.to_string()).unwrap_or_else(|| "not observed before cleanup".into());
                    crate::session_log::early("ERROR", "audio.plugin_host.transport", &format!("pid={} operation={operation} exitStatus=unavailable observation={detail} reason={message}", process.control.pid));
                }
            }
        }
    }
    reconcile_live_session(session, pid, result.as_ref().is_err_and(HostRequestError::is_fatal) || (result.is_ok() && closing));
    result
}

fn reconcile_live_session(session: &mut Session, pid: &ProcessLease, closing: bool) {
    let completed = session.process.as_mut().map(|process| (process.live_closed, process.completed_session_id.take()));
    if closing || completed.as_ref().is_some_and(|(closed, _)| *closed) {
        close_session(session, pid);
    } else if let Some((_, Some(id))) = completed {
        // Deferred load/update and latency-recovery replies carry the same authoritative ID.
        session.native_session_id = Some(id);
    }
}

fn prepare_live_load_with(session: &mut Session, pid: &ProcessLease, generation: u64, detector: &mut impl FnMut(u32) -> Option<String>) -> Result<(), HostRequestError> {
    // The caller holds the session lock and has checked this load's reservation. Claim it
    // before any fallible preparation so a retired or failed host remains recoverable.
    session.generation = generation;
    let mut result = match session.process.as_mut() {
        Some(process) => process.finish_pending_with(detector),
        None => Ok(()),
    };
    if result.is_ok() {
        if let Some(title) = session.process.as_ref().and_then(|process| detector(process.control.pid)) {
            // A dialog can also originate in a plugin window message, outside any RPC.
            result = Err(HostRequestError::DialogPending(dialogs::message(&title)));
        }
    }
    let fatal = result.as_ref().is_err_and(HostRequestError::is_fatal);
    reconcile_live_session(session, pid, fatal);
    result?;
    close_session(session, pid);
    Ok(())
}

fn require_generation(state: &PluginHostState, generation: u64) -> Result<(), String> {
    if !state.stopping.load(Ordering::Acquire)
        && state.generation.load(Ordering::Acquire) == generation
    {
        Ok(())
    } else {
        Err("The audio host request was superseded by a newer session".into())
    }
}

fn project_id(request: &Value) -> Result<&str, String> {
    request
        .get("projectId")
        .and_then(Value::as_str)
        .filter(|id| !id.trim().is_empty() && id.len() <= 256)
        .ok_or_else(|| "A project identifier is required for the audio host session".into())
}

fn state_path(root: &Path, token: &str) -> Result<PathBuf, String> {
    let digest = token
        .strip_prefix("sha256:")
        .filter(|value| {
            value.len() == 64
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
        .ok_or("Invalid plugin state reference")?;
    Ok(root.join("plugin-states").join(format!("{digest}.bin")))
}

fn persist_state(root: &Path, encoded: &str) -> Result<String, String> {
    if encoded.len() > MAX_STATE_BYTES.div_ceil(3) * 4 {
        return Err("Plugin state exceeded the size limit".into());
    }
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(encoded)
        .map_err(|_| "The plugin returned invalid state data")?;
    if bytes.len() > MAX_STATE_BYTES {
        return Err("Plugin state exceeded the size limit".into());
    }
    let token = format!("sha256:{:x}", Sha256::digest(&bytes));
    let path = state_path(root, &token)?;
    let directory = path.parent().unwrap();
    fs::create_dir_all(directory).map_err(|error| error.to_string())?;
    if !path.exists() {
        let mut file =
            tempfile::NamedTempFile::new_in(directory).map_err(|error| error.to_string())?;
        file.write_all(&bytes)
            .and_then(|_| file.as_file().sync_all())
            .map_err(|error| error.to_string())?;
        if let Err(error) = file.persist_noclobber(&path) {
            if error.error.kind() != std::io::ErrorKind::AlreadyExists {
                return Err(error.error.to_string());
            }
        }
    }
    Ok(token)
}

fn resolve_state(root: &Path, state: &str) -> Result<String, String> {
    if !state.starts_with("sha256:") {
        return Ok(state.to_string());
    }
    let path = state_path(root, state)?;
    let mut bytes = Vec::new();
    fs::File::open(path)
        .map_err(|error| format!("Unable to restore plugin state: {error}"))?
        .take(MAX_STATE_BYTES as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|error| error.to_string())?;
    if bytes.len() > MAX_STATE_BYTES {
        return Err("Stored plugin state exceeded the size limit".into());
    }
    if format!("sha256:{:x}", Sha256::digest(&bytes)) != state {
        return Err("Stored plugin state failed its integrity check".into());
    }
    Ok(base64::engine::general_purpose::STANDARD.encode(bytes))
}

fn plugin_warning(message: &str) -> String {
    message
        .chars()
        .filter(|character| !character.is_control())
        .take(512)
        .collect()
}

fn supported_state_format(format: &str) -> bool {
    matches!(format, "vst3" | "vst2") || (cfg!(target_os = "macos") && format == "au")
}

fn omit_bypassed_render_plugins(project: &mut Value) {
    fn retain_active(chain: Option<&mut Value>) {
        if let Some(Value::Array(slots)) = chain {
            slots.retain(|slot| slot.get("bypassed").and_then(Value::as_bool) != Some(true));
        }
    }
    retain_active(project.get_mut("pluginChain"));
    if let Some(Value::Array(tracks)) = project.get_mut("tracks") {
        for track in tracks {
            retain_active(track.get_mut("pluginChain"));
        }
    }
}

fn resolve_states(root: &Path, value: &mut Value) -> Result<(), String> {
    match value {
        Value::Object(object) => {
            let bypassed = object.get("bypassed").and_then(Value::as_bool) == Some(true);
            if bypassed && object.get("skipLoad").and_then(Value::as_bool) == Some(true) {
                return Ok(());
            }
            let unsupported = object
                .get("format")
                .and_then(Value::as_str)
                .is_some_and(|format| !supported_state_format(format));
            if bypassed && unsupported {
                // This is a child-process copy: retain the original reference in
                // the editor project and never interpret another format's blob.
                if object.remove("state").is_some() {
                    object.insert("stateUnavailable".into(), json!(true));
                    object.insert(
                        "bypassWarning".into(),
                        json!("The bypassed plugin format is unavailable in this host"),
                    );
                }
            } else if let Some(Value::String(state)) = object.get("state") {
                match resolve_state(root, state) {
                    Ok(encoded) => {
                        object.insert("state".into(), json!(encoded));
                        object.remove("stateUnavailable");
                        object.remove("bypassWarning");
                    }
                    Err(error) if bypassed => {
                        object.remove("state");
                        object.insert("stateUnavailable".into(), json!(true));
                        object.insert("bypassWarning".into(), json!(plugin_warning(&error)));
                    }
                    Err(error) => return Err(error),
                }
            }
            for child in object.values_mut() {
                resolve_states(root, child)?;
            }
        }
        Value::Array(values) => {
            for child in values {
                resolve_states(root, child)?;
            }
        }
        _ => (),
    }
    Ok(())
}

fn persist_snapshot(root: &Path, mut snapshot: Value) -> Result<Value, String> {
    let states = snapshot
        .get_mut("states")
        .and_then(Value::as_object_mut)
        .ok_or("The audio host returned an invalid state snapshot")?;
    for state in states.values_mut() {
        let encoded = state
            .as_str()
            .ok_or("The audio host returned invalid plugin state")?;
        *state = Value::String(persist_state(root, encoded)?);
    }
    Ok(snapshot)
}

fn ffmpeg_path(app: &AppHandle) -> Result<PathBuf, String> {
    let name = if cfg!(windows) {
        "ffmpeg.exe"
    } else {
        "ffmpeg"
    };
    let bundled =
        crate::python::worker::bundled_bin_dirs(app).map_err(|error| error.to_string())?;
    bundled
        .into_iter()
        .chain(
            std::env::var_os("PATH")
                .map(|paths| std::env::split_paths(&paths).collect::<Vec<_>>())
                .unwrap_or_default(),
        )
        .map(|directory| directory.join(name))
        .find(|path| path.is_file())
        .ok_or_else(|| "FFmpeg is required for the native audio host".to_string())?
        .canonicalize()
        .map_err(|error| error.to_string())
}

fn supported_operation(operation: &str) -> bool {
    matches!(
        operation,
        "load"
            | "update"
            | "play"
            | "pause"
            | "seek"
            | "editor"
            | "snapshot"
            | "status"
            | "unload"
            | "shutdown"
            | "param"
            | "parameters"
            | "monitor"
    )
}

fn plugin_load_timeout(project: &Value) -> Duration {
    // AUv3 extensions and some legacy components instantiate asynchronously,
    // with a bounded ten-second wait per component. The registry-only async
    // flag is unavailable in projects, so reserve a budget for each AU. Bypassed
    // slots are also initialized to retain state/controller access. Keep the
    // overall wait bounded even for projects with many components.
    let units = std::iter::once(project)
        .chain(
            project
                .get("tracks")
                .and_then(Value::as_array)
                .into_iter()
                .flatten(),
        )
        .flat_map(|owner| {
            owner
                .get("pluginChain")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
        })
        .filter(|slot| {
            slot.get("format").and_then(Value::as_str) == Some("au")
                && slot
                    .get("pluginId")
                    .and_then(Value::as_str)
                    .and_then(audio_unit_version)
                    .is_some()
        })
        .take(10)
        .count();
    REQUEST_TIMEOUT + Duration::from_secs(units as u64 * 10)
}

fn bind_editor_owner(request: &mut Value, owner: Option<usize>) -> Result<(), String> {
    let object = request.as_object_mut().ok_or("Audio host request must be an object")?;
    object.remove("editorOwner");
    if let Some(owner) = owner {
        if owner == 0 { return Err("The editor window is unavailable".into()); }
        object.insert("editorOwner".into(), json!(owner));
    }
    Ok(())
}

#[tauri::command]
pub async fn plugin_host_request(
    app: AppHandle,
    window: tauri::WebviewWindow,
    state: State<'_, PluginHostState>,
    mut request: Value,
) -> Result<Value, String> {
    require_enabled()?;
    let op = request
        .get("op")
        .and_then(Value::as_str)
        .ok_or("An audio host operation is required")?
        .to_string();
    if !supported_operation(&op) {
        return Err("Unsupported audio host operation".into());
    }
    let opening_editor = op == "editor" && request.get("open").and_then(Value::as_bool).unwrap_or(true);
    if opening_editor && !host_capabilities().native_editor {
        return Err("Native plugin editors require an X11 or XWayland display on Linux".into());
    }
    let id = project_id(&request)?.to_string();
    #[cfg(windows)]
    let owner = if opening_editor {
        Some(window.hwnd().map_err(|error| error.to_string())?.0 as usize)
    } else { None };
    #[cfg(not(windows))]
    let owner = None;
    bind_editor_owner(&mut request, owner)?;
    request["transportOwner"] = json!(window.label());
    let state = state.inner().clone();
    let root = crate::storage::data_root_dir(&app).map_err(|error| error.to_string())?;
    let recovery_load = op == "load" && request.get("expectedGeneration").is_some();
    let generation = if op == "load" {
        let expected = match request.get("expectedGeneration") {
            None => None,
            Some(value) => Some(value.as_u64().ok_or("Invalid expected audio session generation")?),
        };
        begin_load_generation(&state, expected)?
    } else {
        state.generation.load(Ordering::Acquire)
    };
    let result = tauri::async_runtime::spawn_blocking(move || {
        require_generation(&state, generation)?;
        let mut session = state
            .session
            .lock()
            .map_err(|_| "Audio host session lock failed")?;
        require_generation(&state, generation)?;
        if op == "unload" && request.get("discardUnsaved").and_then(Value::as_bool) == Some(true) {
            let expected = request.get("expectedSessionId").and_then(Value::as_str)
                .filter(|id| !id.is_empty()).ok_or("A native session identifier is required to discard changes")?;
            return Ok(discard_session(&mut session, &state.session_pid, &id, generation, expected));
        }
        if op != "load"
            && (session.project_id.as_deref() != Some(id.as_str())
                || session.generation != generation)
        {
            return Err("The audio host session belongs to another project or is closed".into());
        }
        if op == "load" {
            prepare_live_load_with(&mut session, &state.session_pid, generation, &mut dialogs::owned_dialog)
                .map_err(|error| error.to_string())?;
        }
        if op == "load" || op == "update" {
            prepare_session_request(&mut session, &state.session_pid, op == "load", || {
                request["ffmpeg"] = json!(ffmpeg_path(&app)?);
                resolve_states(&root, &mut request)
            })?;
        }
        require_generation(&state, generation)?;
        if session.process.is_none() {
            let process = HostProcess::spawn(&[std::ffi::OsStr::new("--audio-host")])?;
            state.session_pid.publish(&process)?;
            session.process = Some(process);
            session.project_id = Some(id.clone());
        }
        let timeout = if op == "load" || op == "update" {
            plugin_load_timeout(&request["project"])
        } else {
            REQUEST_TIMEOUT
        };
        let response = request_session(&mut session, &state.session_pid, request, timeout)
            .map_err(|error| error.to_string())?;
        require_generation(&state, generation)?;
        if op == "snapshot" {
            persist_snapshot(&root, response)
        } else {
            Ok(response)
        }
    })
    .await
    .map_err(|error| error.to_string())
    .and_then(|result| result);
    if recovery_load {
        result.map_err(|error| format!("PLUGIN_HOST_RECOVERY_LOAD_FAILED:{generation}:{error}"))
    } else { result }
}

fn bundle_fingerprint(path: &Path) -> Result<String, String> {
    fn append(path: &Path, hash: &mut Sha256) -> Result<Option<fs::Metadata>, String> {
        hash.update(blacklist::path_key(&path.to_string_lossy()).as_bytes());
        let metadata = match fs::metadata(path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                hash.update([0]);
                return Ok(None);
            }
            Err(error) => return Err(error.to_string()),
        };
        hash.update([1]);
        hash.update(metadata.len().to_le_bytes());
        if let Ok(modified) = metadata.modified().and_then(|time| {
            time.duration_since(std::time::UNIX_EPOCH).map_err(std::io::Error::other)
        }) { hash.update(modified.as_nanos().to_le_bytes()); }
        Ok(Some(metadata))
    }
    let mut hash = Sha256::new();
    let metadata = append(path, &mut hash)?.ok_or("Plugin package is missing")?;
    if metadata.is_dir() {
        // Only executable and identity metadata affect identification. Do not walk skins,
        // presets, samples or other large resource trees on every incremental refresh.
        for relative in ["Contents", "Contents/Info.plist", "Contents/Resources",
            "Contents/Resources/moduleinfo.json", "Contents/moduleinfo.json"] {
            append(&path.join(relative), &mut hash)?;
        }
        let mut count = 0;
        for relative in ["Contents/MacOS", "Contents/x86_64-win", "Contents/aarch64-win",
            "Contents/arm64ec-win", "Contents/x86_64-linux", "Contents/aarch64-linux"] {
            let directory = path.join(relative);
            if append(&directory, &mut hash)?.is_some_and(|metadata| metadata.is_dir()) {
                let mut children = fs::read_dir(&directory).map_err(|error| error.to_string())?
                    .take(4097).map(|entry| entry.map(|entry| entry.path()))
                    .collect::<Result<Vec<_>, _>>().map_err(|error| error.to_string())?;
                children.sort();
                count += children.len();
                if count > 4096 { return Err("Plugin bundle contains too many executable files".into()); }
                for child in children { append(&child, &mut hash)?; }
            }
        }
    }
    Ok(format!("metadata-v1:{:x}", hash.finalize()))
}

// Upgrade existing cache fingerprints without forcing all installed plugins to be loaded again.
fn legacy_bundle_fingerprint(path: &Path) -> Result<String, String> {
    fn visit(path: &Path, hash: &mut Sha256, count: &mut usize) -> Result<(), String> {
        *count += 1;
        if *count > 4096 {
            return Err("Plugin bundle contains too many files".into());
        }
        let metadata = fs::symlink_metadata(path).map_err(|error| error.to_string())?;
        if metadata.file_type().is_symlink() {
            return Ok(());
        }
        hash.update(path.to_string_lossy().as_bytes());
        hash.update(metadata.len().to_le_bytes());
        if let Ok(modified) = metadata.modified().and_then(|time| {
            time.duration_since(std::time::UNIX_EPOCH)
                .map_err(std::io::Error::other)
        }) {
            hash.update(modified.as_nanos().to_le_bytes());
        }
        if metadata.is_dir() {
            let mut children = fs::read_dir(path)
                .map_err(|error| error.to_string())?
                .map(|entry| entry.map(|entry| entry.path()))
                .collect::<Result<Vec<_>, _>>()
                .map_err(|error| error.to_string())?;
            children.sort();
            for child in children {
                visit(&child, hash, count)?;
            }
        }
        Ok(())
    }
    let mut hash = Sha256::new();
    visit(path, &mut hash, &mut 0)?;
    Ok(format!("{:x}", hash.finalize()))
}

#[derive(Default, serde::Serialize)]
#[serde(rename_all = "camelCase")]
struct ScanReport {
    plugins: Vec<Value>,
    failures: Vec<ScanFailure>,
    scanned: usize,
    cached: usize,
    blacklisted: Vec<BlacklistEntry>,
    cancelled: bool,
}

#[derive(serde::Serialize)]
struct ScanFailure {
    path: String,
    message: String,
}

fn is_plugin_path(path: &Path) -> bool {
    #[cfg(feature = "native-plugins")]
    {
        pymss_audio_host::is_plugin_path(path)
    }
    #[cfg(not(feature = "native-plugins"))]
    {
        path.extension()
            .and_then(|extension| extension.to_str())
            .is_some_and(|extension| {
                extension.eq_ignore_ascii_case("vst3")
                    || extension.eq_ignore_ascii_case("aaxplugin")
                    || (cfg!(windows) && extension.eq_ignore_ascii_case("dll"))
                    || (cfg!(target_os = "linux") && extension.eq_ignore_ascii_case("so"))
                    || (cfg!(target_os = "macos") && extension.eq_ignore_ascii_case("vst"))
            })
    }
}

fn is_virtual_scan_target(path: &Path) -> bool {
    path == Path::new("au:system")
}

fn unsupported_scan_reason(path: &Path) -> Option<&'static str> {
    #[cfg(feature = "native-plugins")]
    {
        pymss_audio_host::unsupported_plugin_reason(path)
    }
    #[cfg(not(feature = "native-plugins"))]
    {
        path.ancestors()
            .any(|path| {
                path.extension()
                    .is_some_and(|extension| extension.eq_ignore_ascii_case("aaxplugin"))
            })
            .then_some("AAX plugin hosting is not supported in this build")
    }
}

fn audio_unit_version(id: &str) -> Option<u8> {
    fn field(value: &str) -> Option<(u32, &str)> {
        let bytes = value.as_bytes();
        if bytes.first() == Some(&b'#') {
            if let Some(hex) = value.get(1..9) {
                if hex.bytes().all(|byte| byte.is_ascii_hexdigit())
                    && matches!(bytes.get(9), None | Some(b':'))
                {
                    return Some((u32::from_str_radix(hex, 16).ok()?, value.get(9..)?));
                }
            }
        }
        let code: [u8; 4] = bytes.get(..4)?.try_into().ok()?;
        code.iter()
            .all(|byte| (0x20..=0x7e).contains(byte))
            .then_some((u32::from_be_bytes(code), value.get(4..)?))
    }
    let (version, value) = if let Some(value) = id.strip_prefix("au3:") {
        (3, value)
    } else {
        (2, id.strip_prefix("au:")?)
    };
    let (kind, value) = field(value)?;
    let (_, value) = field(value.strip_prefix(':')?)?;
    let (_, remainder) = field(value.strip_prefix(':')?)?;
    (remainder.is_empty()
        && [*b"aufx", *b"aumf", *b"aumu"]
            .map(u32::from_be_bytes)
            .contains(&kind))
    .then_some(version)
}

fn normalize_descriptor(
    descriptor: &mut Value,
    path: &str,
    virtual_target: bool,
) -> Result<(), String> {
    let object = descriptor
        .as_object_mut()
        .ok_or("The plugin scanner returned an invalid result")?;
    let id = object.get("id").and_then(Value::as_str).unwrap_or("");
    let au_version = audio_unit_version(id);
    let inferred = if id.starts_with("vst2:") {
        "vst2"
    } else if au_version.is_some() {
        "au"
    } else {
        "vst3"
    };
    let format = object
        .get("format")
        .and_then(Value::as_str)
        .unwrap_or(inferred)
        .to_string();
    let descriptor_path = if virtual_target || format == "au" {
        if format != "au" || au_version.is_none() {
            return Err("The Audio Unit scanner returned an invalid component identity".into());
        }
        id.to_string()
    } else {
        path.to_string()
    };
    if format == "au" {
        object.insert("auVersion".into(), json!(au_version));
        object.insert("classId".into(), json!(descriptor_path));
    }
    object.insert("format".into(), json!(format));
    object.insert("path".into(), json!(descriptor_path));
    Ok(())
}

fn scan_paths(paths: Vec<String>) -> Result<(Vec<PathBuf>, Vec<ScanFailure>), String> {
    let paths: Vec<_> = paths.into_iter().map(PathBuf::from).collect();
    if paths
        .iter()
        .any(|path| !is_virtual_scan_target(path) && !path.is_absolute())
    {
        return Err("Plugin scan directories must be absolute".into());
    }
    let mut valid = Vec::new();
    let mut failures = Vec::new();
    for path in paths {
        let result = (|| {
            if is_virtual_scan_target(&path) {
                return if cfg!(all(feature = "native-plugins", target_os = "macos")) {
                    Ok(())
                } else {
                    Err("Audio Unit discovery is only available on macOS".into())
                };
            }
            let metadata = fs::metadata(&path).map_err(|error| error.to_string())?;
            let bundle = is_plugin_path(&path);
            if !bundle && !metadata.is_dir() {
                return Err("The selected path is not a directory or a recognized plugin".into());
            }
            // Validate access here: the recursive bundle finder skips unreadable roots.
            if metadata.is_dir() && !bundle {
                fs::read_dir(&path).map_err(|error| error.to_string())?;
            }
            Ok(())
        })();
        match result {
            Ok(()) => valid.push(path),
            Err(message) => failures.push(ScanFailure {
                path: path.to_string_lossy().into_owned(),
                message,
            }),
        }
    }
    Ok((valid, failures))
}

fn selected_scan_targets(paths: Vec<PathBuf>) -> Vec<PathBuf> {
    let audio_units = cfg!(all(feature = "native-plugins", target_os = "macos"))
        && paths.iter().any(|path| is_virtual_scan_target(path));
    let roots: Vec<_> = paths
        .into_iter()
        .filter(|path| !is_virtual_scan_target(path))
        .collect();
    // Never let an empty source list reach the native finder's default fallback.
    #[cfg(feature = "native-plugins")]
    let mut bundles = if roots.is_empty() {
        Vec::new()
    } else {
        pymss_audio_host::find_bundles(&roots)
    };
    #[cfg(not(feature = "native-plugins"))]
    let mut bundles: Vec<PathBuf> = {
        let _ = roots;
        Vec::new()
    };
    if audio_units {
        bundles.push(PathBuf::from("au:system"));
    }
    bundles
}

#[derive(Debug)]
enum ScanProbeError {
    Failure(String),
    Skipped(BlacklistReason),
    Cancelled,
}

impl From<String> for ScanProbeError {
    fn from(message: String) -> Self {
        Self::Failure(message)
    }
}

impl From<&str> for ScanProbeError {
    fn from(message: &str) -> Self {
        Self::Failure(message.into())
    }
}

async fn scan_plugins(
    app: AppHandle,
    state: PluginHostState,
    paths: Vec<String>,
    force: bool,
    scan_id: Option<String>,
) -> Result<ScanReport, String> {
    require_enabled()?;
    if paths.is_empty() {
        return Ok(ScanReport::default());
    }
    let root = crate::storage::data_root_dir(&app).map_err(|error| error.to_string())?;
    let scan_id = scan_id.unwrap_or_else(|| format!("legacy-{}", state.scan_sequence.fetch_add(1, Ordering::Relaxed)));
    // Register before waiting for the worker/scan lock, so disposing a queued request can cancel it.
    let run = state.scan_control.start(scan_id)?;
    tauri::async_runtime::spawn_blocking(move || {
        let _guard = state
            .scan_lock
            .lock()
            .map_err(|_| "Plugin scan lock failed")?;
        if state.scan_process.id() == u32::MAX {
            return Err("Plugin scanning was cancelled".into());
        }
        let (paths, failures) = scan_paths(paths)?;
        let bundles = selected_scan_targets(paths);
        scan_pipeline::scan(&root, bundles, failures, force,
          scan_pipeline::Context { run: Some(&run), storage: &state.scan_storage }, |bundle, ticket| {
            if state.scan_process.id() == u32::MAX {
                return Err(ScanProbeError::Cancelled);
            }
            let ticket = ticket.expect("a controlled scan owns each package ticket");
            let process =
                HostProcess::spawn(&[std::ffi::OsStr::new("--plugin-scan"), bundle.as_os_str()])?;
            if let Err(message) = state.scan_process.publish(&process) {
                return Err(if state.scan_process.id() == u32::MAX {
                    ScanProbeError::Cancelled
                } else {
                    ScanProbeError::Failure(message)
                });
            }
            run.attach(ticket, process.control.clone());
            let result = receive_scan(&process, &run, ticket, SCAN_TIMEOUT)
                .and_then(|result| {
                    result
                        .get("plugins")
                        .and_then(Value::as_array)
                        .cloned()
                        .ok_or_else(|| ScanProbeError::Failure("The plugin scanner returned an invalid result".into()))
                });
            drop(process);
            state.scan_process.release();
            if state.scan_process.id() == u32::MAX {
                Err(ScanProbeError::Cancelled)
            } else {
                result
            }
        })
    })
    .await
    .map_err(|error| error.to_string())?
}

fn receive_scan(process: &HostProcess, run: &ScanRun, ticket: ScanTicket, timeout: Duration) -> Result<Value, ScanProbeError> {
    loop {
        match run.decision(Some(ticket)) {
            ScanDecision::Skip => return Err(ScanProbeError::Skipped(BlacklistReason::Manual)),
            ScanDecision::Cancel => return Err(ScanProbeError::Cancelled),
            ScanDecision::Continue => {}
        }
        let remaining = timeout.saturating_sub(ticket.started.elapsed());
        if remaining.is_zero() { return Err(ScanProbeError::Skipped(BlacklistReason::Timeout)); }
        match process.responses.recv_timeout(remaining.min(Duration::from_millis(50))) {
            Ok(value) => return value.map_err(ScanProbeError::Failure),
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(mpsc::RecvTimeoutError::Disconnected) => return Err(ScanProbeError::Failure("The plugin scanner response pipe closed".into())),
        }
    }
}

#[tauri::command]
pub fn plugin_host_scan_status(state: State<'_, PluginHostState>, scan_id: String) -> Result<Value, String> {
    require_enabled()?;
    serde_json::to_value(state.scan_control.status(&scan_id)).map_err(|error| error.to_string())
}

#[tauri::command]
pub async fn plugin_host_skip_scan(app: AppHandle, state: State<'_, PluginHostState>, scan_id: String, path: String) -> Result<Value, String> {
    require_enabled()?;
    let root = crate::storage::data_root_dir(&app).map_err(|error| error.to_string())?;
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || skip_scan(&root, &state, &scan_id, &path))
        .await.map_err(|error| error.to_string())?
}

fn skip_scan(root: &Path, state: &PluginHostState, scan_id: &str, path: &str) -> Result<Value, String> {
    // Claim and persist in one storage order. A later restore must win over both actions.
    let _storage = state.scan_storage.lock().map_err(|_| "Plugin scan storage lock failed")?;
    let skipped = state.scan_control.skip(scan_id, path);
    if skipped {
        // Persist immediately, independently of later cache commit or whole-scan cancellation.
        blacklist::add(root, path, BlacklistReason::Manual)?;
    }
    Ok(json!({ "skipped": skipped }))
}

#[tauri::command]
pub async fn plugin_host_cancel_scan(state: State<'_, PluginHostState>, scan_id: String) -> Result<Value, String> {
    require_enabled()?;
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || json!({ "cancelled": state.scan_control.cancel(&scan_id) }))
        .await.map_err(|error| error.to_string())
}

#[tauri::command]
pub async fn plugin_host_scan_blacklist(app: AppHandle, state: State<'_, PluginHostState>) -> Result<Value, String> {
    require_enabled()?;
    let root = crate::storage::data_root_dir(&app).map_err(|error| error.to_string())?;
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let _storage = state.scan_storage.lock().map_err(|_| "Plugin scan storage lock failed")?;
        serde_json::to_value(blacklist::load(&root)?).map_err(|error| error.to_string())
    }).await.map_err(|error| error.to_string())?
}

fn remove_cached_scan_entry(root: &Path, path: &str) -> Result<(), String> {
    let file = root.join("plugin-cache.json");
    let mut cache: Value = fs::File::open(&file).ok()
        .and_then(|file| serde_json::from_reader(file.take(MAX_CACHE_BYTES)).ok()).unwrap_or(Value::Null);
    if let Some(entries) = cache.get_mut("entries").and_then(Value::as_array_mut) {
        let previous = entries.len();
        entries.retain(|entry| !entry["path"].as_str().is_some_and(|candidate| blacklist::path_matches(candidate, path)));
        if entries.len() != previous {
            crate::storage::write_json_file(&file, &cache).map_err(|error| error.to_string())?;
        }
    }
    Ok(())
}

#[tauri::command]
pub async fn plugin_host_remove_scan_blacklist(app: AppHandle, state: State<'_, PluginHostState>, path: String) -> Result<(), String> {
    require_enabled()?;
    let root = crate::storage::data_root_dir(&app).map_err(|error| error.to_string())?;
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || restore_scan_entry(&root, &state, &path))
        .await.map_err(|error| error.to_string())?
}

fn restore_scan_entry(root: &Path, state: &PluginHostState, path: &str) -> Result<(), String> {
    let _storage = state.scan_storage.lock().map_err(|_| "Plugin scan storage lock failed")?;
    state.scan_control.invalidate(path);
    // Invalidate first: a storage error must leave the exclusion active rather than reuse a failed cache.
    remove_cached_scan_entry(root, path)?;
    blacklist::remove(root, path)
}

#[tauri::command]
pub async fn plugin_host_scan(
    app: AppHandle,
    state: State<'_, PluginHostState>,
    paths: Vec<String>,
) -> Result<Vec<Value>, String> {
    Ok(scan_plugins(app, state.inner().clone(), paths, false, None)
        .await?
        .plugins)
}

#[tauri::command]
pub async fn plugin_host_cached_scan_report(
    app: AppHandle,
    state: State<'_, PluginHostState>,
    paths: Vec<String>,
) -> Result<Value, String> {
    require_enabled()?;
    let root = crate::storage::data_root_dir(&app).map_err(|error| error.to_string())?;
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        // Reading the last complete snapshot must not queue behind a long native scan.
        let _guard = state.scan_storage.lock().map_err(|_| "Plugin scan storage lock failed")?;
        scan_pipeline::cached_report(&root, &paths)
    }).await.map_err(|error| error.to_string())?
}

#[tauri::command]
pub async fn plugin_host_scan_report(
    app: AppHandle,
    state: State<'_, PluginHostState>,
    paths: Vec<String>,
    force: Option<bool>,
    scan_id: Option<String>,
) -> Result<Value, String> {
    serde_json::to_value(
        scan_plugins(app, state.inner().clone(), paths, force.unwrap_or(false), scan_id).await?,
    )
    .map_err(|error| error.to_string())
}

fn render_directory(root: &Path, task_id: &str) -> Result<PathBuf, String> {
    if task_id.is_empty()
        || task_id.len() > 128
        || !task_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
    {
        return Err("Invalid plugin render task identifier".into());
    }
    let temp = root.join("temp");
    if fs::symlink_metadata(&temp).is_ok_and(|metadata| metadata.file_type().is_symlink()) {
        return Err("The audio temporary directory cannot be a symbolic link".into());
    }
    let parent = temp.join("vst-renders");
    if fs::symlink_metadata(&parent).is_ok_and(|metadata| metadata.file_type().is_symlink()) {
        return Err("The plugin render directory cannot be a symbolic link".into());
    }
    let path = parent.join(task_id);
    if fs::symlink_metadata(&path).is_ok_and(|metadata| metadata.file_type().is_symlink()) {
        return Err("The plugin render task directory cannot be a symbolic link".into());
    }
    Ok(path)
}

struct RenderTask {
    task_id: String,
    pid: Arc<ProcessLease>,
    renders: Arc<Mutex<HashMap<String, Arc<ProcessLease>>>>,
}

impl RenderTask {
    fn require_active(&self) -> Result<(), String> {
        if self.pid.id() == u32::MAX {
            Err("Plugin rendering was cancelled".into())
        } else {
            Ok(())
        }
    }
}

impl Drop for RenderTask {
    fn drop(&mut self) {
        self.pid.clear();
        if let Ok(mut renders) = self.renders.lock() {
            // A lease may only remove its own registration, even if an identifier is reused.
            if renders
                .get(&self.task_id)
                .is_some_and(|pid| Arc::ptr_eq(pid, &self.pid))
            {
                renders.remove(&self.task_id);
            }
        }
    }
}

fn register_render(state: &PluginHostState, task_id: String) -> Result<RenderTask, String> {
    let pid = Arc::new(ProcessLease::default());
    {
        let mut renders = state
            .renders
            .lock()
            .map_err(|_| "Plugin render lock failed")?;
        if state.stopping.load(Ordering::Acquire) {
            return Err("The audio host is shutting down".into());
        }
        if renders.contains_key(&task_id) {
            return Err("This plugin render task is already running".into());
        }
        renders.insert(task_id.clone(), pid.clone());
    }
    Ok(RenderTask {
        task_id,
        pid,
        renders: state.renders.clone(),
    })
}

fn cancel_render(state: &PluginHostState, task_id: &str) -> Result<Option<u32>, String> {
    let renders = state
        .renders
        .lock()
        .map_err(|_| "Plugin render lock failed")?;
    Ok(renders.get(task_id).map(|pid| pid.cancel()))
}

fn cleanup_render(state: &PluginHostState, root: &Path, task_id: &str) -> Result<(), String> {
    let directory = render_directory(root, task_id)?;
    let renders = state
        .renders
        .lock()
        .map_err(|_| "Plugin render lock failed")?;
    if renders.contains_key(task_id) {
        return Err("The plugin render task is still running".into());
    }
    match fs::remove_dir_all(directory) {
        Err(error) if error.kind() != std::io::ErrorKind::NotFound => Err(error.to_string()),
        _ => Ok(()),
    }
}

#[tauri::command]
pub async fn plugin_host_render(
    app: AppHandle,
    window: tauri::WebviewWindow,
    state: State<'_, PluginHostState>,
    mut project: Value,
    task_id: String,
    sample_rate: Option<u32>,
) -> Result<Value, String> {
    require_enabled()?;
    if sample_rate.is_some_and(|rate| !(8000..=48000).contains(&rate)) {
        return Err("The render sample rate must be between 8000 and 48000 Hz".into());
    }
    let root = crate::storage::data_root_dir(&app).map_err(|error| error.to_string())?;
    let directory = render_directory(&root, &task_id)?;
    let state = state.inner().clone();
    // Register before queueing blocking work so immediate cancellation is remembered.
    let task = register_render(&state, task_id)?;
    let ffmpeg = ffmpeg_path(&app)?;
    tauri::async_runtime::spawn_blocking(move || {
        let mut created_directory = false;
        let result = (|| {
            task.require_active()?;
            let mut emit_progress = |progress: NativeRenderProgress| -> Result<(), HostRequestError> {
                task.require_active().map_err(HostRequestError::Operation)?;
                let _ = app.emit_to(window.label(), "pymss://worker-event", json!({
                    "type":"editor_plugin_render_progress", "taskId":task.task_id.as_str(), "payload":progress,
                }));
                Ok(())
            };
            emit_progress(NativeRenderProgress { phase: "preparing".into(), completed: 0, total: 1 })
                .map_err(|error| error.to_string())?;
            omit_bypassed_render_plugins(&mut project);
            resolve_states(&root, &mut project)?;
            task.require_active()?;
            fs::create_dir_all(directory.parent().unwrap()).map_err(|error| error.to_string())?;
            fs::create_dir(&directory).map_err(|error| error.to_string())?;
            created_directory = true;
            let output = directory.join("mix.wav");
            task.require_active()?;
            let mut process = HostProcess::spawn(&[std::ffi::OsStr::new("--audio-host")])?;
            if task.pid.publish(&process).is_err() {
                return Err("Plugin rendering was cancelled".into());
            }
            let project_id = project.get("id").and_then(Value::as_str).unwrap_or("render").to_string();
            let load_timeout = plugin_load_timeout(&project);
            let mut load = json!({"op":"load","projectId":project_id,"project":project,"ffmpeg":ffmpeg,"offline":true});
            if let Some(rate) = sample_rate { load["sampleRate"] = json!(rate); }
            process.request(load, load_timeout)
                .map_err(|error| error.to_string())?;
            let mut result = process.request_with_progress(json!({"op":"render","projectId":project_id,"path":output}), RENDER_TIMEOUT, &mut emit_progress)
                .map_err(|error| error.to_string())?;
            task.require_active()?;
            if !output.is_file() { return Err("The audio host did not produce a rendered mix".into()); }
            result["path"] = json!(output);
            Ok(result)
        })();
        if result.is_err() && created_directory { let _ = fs::remove_dir_all(&directory); }
        // Retain the registration until the child and failed output have both been cleaned.
        drop(task);
        result
    }).await.map_err(|error| error.to_string())?
}

#[tauri::command]
pub async fn plugin_host_cancel_render(
    state: State<'_, PluginHostState>,
    task_id: String,
) -> Result<bool, String> {
    require_enabled()?;
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        cancel_render(&state, &task_id).map(|pid| pid.is_some())
    })
    .await
    .map_err(|error| error.to_string())?
}

#[tauri::command]
pub async fn plugin_host_cleanup_render(
    app: AppHandle,
    state: State<'_, PluginHostState>,
    task_id: String,
) -> Result<(), String> {
    require_enabled()?;
    let root = crate::storage::data_root_dir(&app).map_err(|error| error.to_string())?;
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || cleanup_render(&state, &root, &task_id))
        .await
        .map_err(|error| error.to_string())?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn explicit_discard_releases_only_its_own_session_and_tolerates_an_already_closed_host() {
        let pid = ProcessLease::default();
        let mut session = Session { project_id: Some("old".into()), native_session_id: Some("instance-new".into()), generation: 4, ..Session::default() };
        discard_session(&mut session, &pid, "old", 3, "instance-new");
        assert_eq!(session.project_id.as_deref(), Some("old"));
        discard_session(&mut session, &pid, "other", 4, "instance-new");
        assert_eq!(session.project_id.as_deref(), Some("old"));
        discard_session(&mut session, &pid, "old", 4, "instance-old");
        assert_eq!(session.project_id.as_deref(), Some("old"));
        assert_eq!(discard_session(&mut session, &pid, "old", 4, "instance-new")["closed"], true);
        assert!(session.project_id.is_none());
        assert_eq!(discard_session(&mut session, &pid, "old", 4, "instance-new")["closed"], true);
    }

    #[test]
    fn recovery_ack_rejects_replacement_sessions_and_fenced_load_cannot_supersede_them() {
        let state = PluginHostState::default();
        state.generation.store(9, Ordering::Release);
        let mut session = Session { project_id: Some("new".into()), native_session_id: Some("instance-new".into()), generation: 9, ..Session::default() };
        let reply = discard_session(&mut session, &state.session_pid, "old", 9, "instance-old");
        assert_eq!(reply["superseded"], true);
        assert_eq!(session.project_id.as_deref(), Some("new"));
        close_session(&mut session, &state.session_pid);
        let reply = discard_session(&mut session, &state.session_pid, "old", 9, "instance-old");
        assert_eq!(reply["superseded"], false);
        assert_eq!(reply["generation"], 9);
        assert_eq!(begin_load_generation(&state, Some(9)).unwrap(), 10);
        assert!(begin_load_generation(&state, Some(9)).is_err());
        assert_eq!(state.generation.load(Ordering::Acquire), 10);
    }

    #[test]
    fn empty_session_cannot_authorize_recovery_over_a_pending_load() {
        let state = PluginHostState::default();
        state.generation.store(8, Ordering::Release);
        let mut session = Session { generation: 8, ..Session::default() };
        // A different window reserves a load before its blocking task obtains the session lock.
        let pending = begin_load_generation(&state, None).unwrap();
        assert_eq!(pending, 9);
        let reply = discard_session(&mut session, &state.session_pid, "old", pending, "old-native");
        assert_eq!(reply["superseded"], true);
        assert_eq!(session.generation, 8);
        assert!(require_generation(&state, pending).is_ok());
    }

    #[test]
    fn failed_load_preflight_retains_the_claimed_generation_for_recovery() {
        let state = PluginHostState::default();
        state.generation.store(8, Ordering::Release);
        let mut session = Session { generation: 8, ..Session::default() };
        let generation = begin_load_generation(&state, None).unwrap();
        assert!(require_generation(&state, generation).is_ok());
        prepare_live_load_with(&mut session, &state.session_pid, generation, &mut |_| None).unwrap();
        let root = tempfile::tempdir().unwrap();
        let mut request = json!({"project":{"pluginChain":[{
            "format":"vst3", "bypassed":false,
            "state":format!("sha256:{}", "a".repeat(64))
        }]}});
        assert!(prepare_session_request(&mut session, &state.session_pid, true, || {
            resolve_states(root.path(), &mut request)
        }).is_err());
        assert!(session.process.is_none());
        assert_eq!(session.generation, generation);
        let reply = discard_session(&mut session, &state.session_pid, "project", generation, "old-native");
        assert_eq!(reply["superseded"], false);
        assert_eq!(begin_load_generation(&state, Some(generation)).unwrap(), generation + 1);
    }

    #[test]
    fn editor_owner_comes_from_the_calling_window_and_never_from_frontend_json() {
        let mut request = json!({"op":"editor", "editorOwner":999});
        bind_editor_owner(&mut request, Some(12345)).unwrap();
        assert_eq!(request["editorOwner"], 12345);
        bind_editor_owner(&mut request, None).unwrap();
        assert!(request.get("editorOwner").is_none());
        request["editorOwner"] = json!(999);
        assert!(bind_editor_owner(&mut request, Some(0)).is_err());
        assert!(request.get("editorOwner").is_none());
    }

    #[test]
    fn build_capabilities_separate_native_host_support_from_lab_identity() {
        let capabilities = plugin_host_capabilities();
        let available = cfg!(feature = "native-plugins")
            && matches!(std::env::consts::OS, "windows" | "macos" | "linux");
        assert_eq!(capabilities["enabled"], available);
        assert_eq!(capabilities["labBuild"], cfg!(feature = "vst-lab"));
        assert_eq!(capabilities["experimental"], true);
        let formats = capabilities["formats"].as_array().unwrap();
        if available {
            assert!(formats.contains(&json!("vst2")));
            assert!(formats.contains(&json!("vst3")));
            assert_eq!(formats.contains(&json!("au")), cfg!(target_os = "macos"));
        } else {
            assert!(formats.is_empty());
        }
        assert!(!formats.contains(&json!("aax")));
        let directories = capabilities["defaultDirectories"].as_array().unwrap();
        #[cfg(feature = "native-plugins")]
        if available {
            let expected: Vec<PathBuf> = pymss_audio_host::default_search_paths().into_iter()
                .filter(|path| path.is_absolute()).collect();
            assert_eq!(capabilities["defaultDirectories"], json!(expected));
            assert!(!directories.is_empty());
            assert_eq!(directories.iter().map(|path| PathBuf::from(path.as_str().unwrap())).collect::<std::collections::HashSet<_>>().len(), directories.len());
        }
        if !available {
            assert!(directories.is_empty());
        }
        assert!(directories.iter().all(|path| Path::new(path.as_str().unwrap()).is_absolute()));
    }

    #[test]
    fn platform_support_keeps_headless_linux_audio_enabled_without_an_editor() {
        for platform in ["windows", "macos", "linux"] {
            assert_eq!(
                platform_capabilities(true, platform, true),
                HostCapabilities {
                    enabled: true,
                    native_editor: true,
                }
            );
            assert_eq!(
                platform_capabilities(false, platform, true),
                HostCapabilities {
                    enabled: false,
                    native_editor: false,
                }
            );
        }
        assert_eq!(
            platform_capabilities(true, "linux", false),
            HostCapabilities {
                enabled: true,
                native_editor: false,
            }
        );
        for platform in ["windows", "macos"] {
            assert!(platform_capabilities(true, platform, false).native_editor);
        }
        assert!(!platform_capabilities(true, "freebsd", true).enabled);
    }

    #[test]
    fn group_signals_reject_special_ids_parent_group_and_overflow() {
        for pid in [0, 1, 42, u32::MAX, i32::MAX as u32 + 1] {
            assert_eq!(isolated_group_target(pid, 42), None);
        }
        assert_eq!(isolated_group_target(43, 42), Some(-43));
        assert_eq!(isolated_group_target(i32::MAX as u32, 42), Some(-i32::MAX));
    }

    #[test]
    fn a_retired_process_identity_cannot_be_signalled_by_late_cancellation() {
        let control = Arc::new(ProcessControl {
            pid: std::process::id(),
            active: Mutex::new(false),
        });
        let lease = ProcessLease(Mutex::new(LeaseState {
            cancelled: false,
            process: Some(control.clone()),
        }));
        assert_eq!(lease.cancel(), std::process::id());
        assert_eq!(lease.id(), u32::MAX);
        assert_eq!(lease.cancel(), u32::MAX);
        assert!(!*control.active.lock().unwrap());
    }

    #[test]
    fn shutdown_rejects_new_work_and_cancels_queued_scan_and_render_leases() {
        let state = PluginHostState::default();
        let queued = register_render(&state, "queued-task".into()).unwrap();
        state.shutdown();
        assert!(queued.require_active().is_err());
        assert!(register_render(&state, "later-task".into()).is_err());
        assert_eq!(state.scan_process.id(), u32::MAX);
        assert!(require_generation(&state, state.generation.load(Ordering::Acquire)).is_err());
    }

    #[cfg(any(unix, windows))]
    #[test]
    fn active_render_cancellation_holds_its_child_identity_until_reaping() {
        let state = PluginHostState::default();
        let task = register_render(&state, "running-task".into()).unwrap();
        #[cfg(unix)]
        let mut command = Command::new("/bin/sh");
        #[cfg(unix)]
        command.args(["-c", "exec sleep 30"]);
        #[cfg(windows)]
        let mut command = Command::new("cmd.exe");
        #[cfg(windows)]
        command.args(["/D", "/C", "ping -n 30 127.0.0.1 >nul"]);
        let process = HostProcess::spawn_command(command).unwrap();
        let pid = process.child.id();
        let control = process.control.clone();
        task.pid.publish(&process).unwrap();
        assert!(task.pid.publish(&process).is_err());
        assert_eq!(cancel_render(&state, "running-task").unwrap(), Some(pid));
        assert!(task.require_active().is_err());
        assert!(task.pid.publish(&process).is_err());
        drop(process);
        assert!(!*control.active.lock().unwrap());
        assert_eq!(
            cancel_render(&state, "running-task").unwrap(),
            Some(u32::MAX)
        );
        drop(task);
        assert_eq!(cancel_render(&state, "running-task").unwrap(), None);
    }

    #[cfg(unix)]
    #[test]
    fn unix_helpers_have_an_owned_process_group_and_reaped_identity() {
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "exec sleep 30"]);
        let process = HostProcess::spawn_command(command).unwrap();
        let lease = ProcessLease::default();
        lease.publish(&process).unwrap();
        let control = process.control.clone();
        let pid = process.child.id();
        unsafe {
            assert_eq!(libc::getpgid(pid as libc::pid_t), pid as libc::pid_t);
            assert_ne!(pid as libc::pid_t, libc::getpgrp());
        }
        drop(process);
        assert!(!*control.active.lock().unwrap());
        assert_eq!(lease.cancel(), pid);
        unsafe {
            assert_eq!(
                libc::waitpid(pid as libc::pid_t, std::ptr::null_mut(), libc::WNOHANG),
                -1
            );
        }
        assert_eq!(
            std::io::Error::last_os_error().raw_os_error(),
            Some(libc::ECHILD)
        );
    }

    #[test]
    fn response_ids_reject_late_or_malformed_responses() {
        assert!(decode_response(json!({"id":1,"ok":true,"result":7}), 2).is_err());
        assert!(
            decode_response(json!({"id":2,"ok":false,"error":"Plugin failed"}), 2)
                .unwrap_err()
                .to_string()
                .contains("Plugin failed")
        );
        assert_eq!(
            decode_response(json!({"id":2,"ok":true,"result":7}), 2).unwrap(),
            json!(7)
        );
    }

    #[test]
    fn state_references_round_trip_and_reject_corruption_and_traversal() {
        let root = tempfile::tempdir().unwrap();
        let encoded = base64::engine::general_purpose::STANDARD.encode(b"plugin-state");
        let token = persist_state(root.path(), &encoded).unwrap();
        assert_eq!(persist_state(root.path(), &encoded).unwrap(), token);
        let mut project =
            json!({"tracks":[{"pluginChain":[{"state":token,"unknown":"preserved"}]}]});
        resolve_states(root.path(), &mut project).unwrap();
        assert_eq!(project["tracks"][0]["pluginChain"][0]["state"], encoded);
        assert_eq!(
            project["tracks"][0]["pluginChain"][0]["unknown"],
            "preserved"
        );
        assert!(state_path(root.path(), "sha256:../../secret").is_err());
        fs::write(state_path(root.path(), &token).unwrap(), b"tampered").unwrap();
        assert!(resolve_states(root.path(), &mut json!({"state":token})).is_err());
    }

    #[test]
    fn quarantined_bypass_states_remain_opaque_until_explicitly_enabled() {
        let root = tempfile::tempdir().unwrap();
        let mut slot = json!({"format":"au", "bypassed":true, "skipLoad":true, "state":"sha256:../../unresolved"});
        resolve_states(root.path(), &mut slot).unwrap();
        assert_eq!(slot["state"], "sha256:../../unresolved");
        slot["bypassed"] = json!(false);
        assert!(resolve_states(root.path(), &mut slot).is_err());
    }

    #[test]
    fn render_copy_omits_disabled_plugins_before_resolving_states_and_preserves_the_project() {
        let root = tempfile::tempdir().unwrap();
        let project = json!({"id":"render", "pluginChain":[
            {"id":"off", "bypassed":true, "state":"sha256:../../invalid"},
            {"id":"on", "bypassed":false},
            {"id":"default"}
        ], "tracks":[{"id":"track", "pluginChain":[
            {"id":"track-off", "bypassed":true, "state":"sha256:missing"},
            {"id":"track-on", "bypassed":false}
        ]}, {"id":"plain"}], "metadata":{"bypassed":true,"keep":1}});
        let mut render = project.clone();
        omit_bypassed_render_plugins(&mut render);
        resolve_states(root.path(), &mut render).unwrap();
        assert_eq!(render["pluginChain"], json!([{"id":"on","bypassed":false},{"id":"default"}]));
        assert_eq!(render["tracks"][0]["pluginChain"], json!([{"id":"track-on","bypassed":false}]));
        assert_eq!(render["tracks"][1], project["tracks"][1]);
        assert_eq!(render["metadata"], project["metadata"]);
        assert_eq!(project["pluginChain"][0]["state"], "sha256:../../invalid");
        assert_eq!(project["tracks"][0]["pluginChain"][0]["state"], "sha256:missing");
    }

    #[test]
    fn bypassed_missing_or_corrupt_states_are_only_removed_from_the_child_copy() {
        let root = tempfile::tempdir().unwrap();
        let encoded = base64::engine::general_purpose::STANDARD.encode(b"saved-state");
        let token = persist_state(root.path(), &encoded).unwrap();
        for corrupt in [false, true] {
            let path = state_path(root.path(), &token).unwrap();
            if corrupt {
                fs::write(&path, b"corrupt").unwrap();
            } else {
                fs::remove_file(&path).unwrap();
            }
            let original = json!({"pluginChain":[{
                "id":"bypassed", "format":"vst3", "bypassed":true,
                "state":token, "vendorMetadata":{"preserved":true}
            }]});
            let mut child = original.clone();
            resolve_states(root.path(), &mut child).unwrap();
            assert_eq!(original["pluginChain"][0]["state"], token);
            let slot = &child["pluginChain"][0];
            assert!(slot.get("state").is_none());
            assert_eq!(slot["stateUnavailable"], true);
            assert!(slot["bypassWarning"]
                .as_str()
                .is_some_and(|value| !value.is_empty()));
            assert_eq!(
                slot["vendorMetadata"],
                original["pluginChain"][0]["vendorMetadata"]
            );
        }
    }

    #[test]
    fn enabled_missing_states_and_invalid_references_remain_strict() {
        let root = tempfile::tempdir().unwrap();
        for bypassed in [false, true] {
            let mut slot =
                json!({"format":"vst3", "bypassed":bypassed, "state":"sha256:../../invalid"});
            let result = resolve_states(root.path(), &mut slot);
            assert_eq!(result.is_ok(), bypassed);
            if bypassed {
                assert_eq!(slot["stateUnavailable"], true);
                assert!(slot.get("state").is_none());
            }
        }
        let missing = format!("sha256:{}", "a".repeat(64));
        assert!(
            resolve_states(root.path(), &mut json!({"bypassed":false, "state":missing})).is_err()
        );
    }

    #[test]
    fn bypassed_unknown_formats_never_resolve_their_opaque_state() {
        let root = tempfile::tempdir().unwrap();
        let original =
            json!({"format":"aax", "bypassed":true, "state":"sha256:opaque-other-format"});
        let mut child = original.clone();
        resolve_states(root.path(), &mut child).unwrap();
        assert_eq!(original["state"], "sha256:opaque-other-format");
        assert!(child.get("state").is_none());
        assert_eq!(child["stateUnavailable"], true);
        assert!(!root.path().join("plugin-states").exists());
    }

    #[test]
    fn valid_bypassed_state_is_restored_and_clears_stale_unavailable_metadata() {
        let root = tempfile::tempdir().unwrap();
        let token = persist_state(root.path(), "YQ==").unwrap();
        let mut child = json!({"format":"vst3", "bypassed":true, "state":token,
            "stateUnavailable":true,"bypassWarning":"old failure"});
        resolve_states(root.path(), &mut child).unwrap();
        assert_eq!(child["state"], "YQ==");
        assert!(child.get("stateUnavailable").is_none());
        assert!(child.get("bypassWarning").is_none());
        assert_eq!(plugin_warning("first\nsecond\0"), "firstsecond");
    }

    #[test]
    fn every_supported_format_resolves_persisted_state_without_losing_metadata() {
        let root = tempfile::tempdir().unwrap();
        let encoded = base64::engine::general_purpose::STANDARD.encode(b"opaque-plugin-state");
        let token = persist_state(root.path(), &encoded).unwrap();
        for format in ["vst3", "vst2", "au"]
            .into_iter()
            .filter(|format| supported_state_format(format))
        {
            let mut slot =
                json!({"format":format,"bypassed":true,"state":token,"metadata":{"keep":true}});
            resolve_states(root.path(), &mut slot).unwrap();
            assert_eq!(slot["state"], encoded);
            assert_eq!(slot["metadata"]["keep"], true);
            assert!(slot.get("bypassWarning").is_none());
        }
    }

    #[test]
    fn virtual_audio_unit_catalog_is_saved_for_display_but_reenumerated_on_refresh() {
        let root = tempfile::tempdir().unwrap();
        let target = PathBuf::from("au:system");
        for count in [1, 2] {
            let report = scan_bundles(
                root.path(),
                vec![target.clone()],
                Vec::new(),
                false,
                |path| {
                    assert_eq!(path, Path::new("au:system"));
                    Ok((0..count)
                        .map(|index| json!({"id":format!("au:aufx:{index:04}:test"), "name":"Effect"}))
                        .collect())
                },
            )
            .unwrap();
            assert_eq!(report.scanned, 1);
            assert_eq!(report.cached, 0);
            assert_eq!(report.plugins.len(), count);
            assert_eq!(report.plugins[0]["format"], "au");
            assert_eq!(report.plugins[0]["path"], report.plugins[0]["id"]);
            let cache: Value =
                serde_json::from_slice(&fs::read(root.path().join("plugin-cache.json")).unwrap())
                    .unwrap();
            assert_eq!(cache["entries"][0]["path"], "au:system");
            let cached = scan_pipeline::cached_report(root.path(), &["au:system".into()]).unwrap();
            assert_eq!(cached["plugins"].as_array().unwrap().len(), count);
            assert_eq!(cached["scanned"], 0);
        }
        let empty = scan_bundles(root.path(), vec![target], Vec::new(), false, |_| {
            Ok(Vec::new())
        })
        .unwrap();
        assert!(empty.failures.is_empty());
    }

    #[test]
    fn scan_descriptors_keep_explicit_formats_and_infer_legacy_prefixes() {
        for (id, expected) in [
            ("vst2:gain", "vst2"),
            ("au:aufx:gain:test", "au"),
            ("au3:aufx:gain:test", "au"),
            ("legacy-class", "vst3"),
        ] {
            let mut descriptor = json!({"id":id});
            normalize_descriptor(&mut descriptor, "plugin.path", false).unwrap();
            assert_eq!(descriptor["format"], expected);
        }
        let mut explicit = json!({"id":"gain", "format":"vst2"});
        normalize_descriptor(&mut explicit, "plugin.path", false).unwrap();
        assert_eq!(explicit["format"], "vst2");
        assert!(normalize_descriptor(&mut explicit, "au:system", true).is_err());
    }

    #[test]
    fn audio_unit_identity_parser_accepts_sdk_fourccs_and_rejects_partial_prefixes() {
        for version in ["au", "au3"] {
            assert_eq!(
                audio_unit_version(&format!("{version}:aufx:A:B::Test")),
                Some(if version == "au" { 2 } else { 3 })
            );
            assert!(audio_unit_version(&format!("{version}:#61756678:#00000001:test")).is_some());
            for suffix in [
                "system",
                "aufx:x:test",
                "aufx:gain:test:extra",
                "xxxx:gain:test",
                "aufx:éabc:test",
                "aufx:#0000000Z:test",
            ] {
                let id = format!("{version}:{suffix}");
                assert!(audio_unit_version(&id).is_none(), "{id}");
                assert!(normalize_descriptor(
                    &mut json!({"id":id,"format":"au"}),
                    "au:system",
                    true
                )
                .is_err());
            }
        }
    }

    #[test]
    fn audio_unit_catalog_preserves_v2_v3_and_nullable_shell_metadata() {
        let root = tempfile::tempdir().unwrap();
        let report = scan_bundles(root.path(), vec![PathBuf::from("au:system")], Vec::new(), false, |_| Ok(vec![
            json!({"id":"au:aufx:gain:test","name":"Gain","isInstrument":false,"vendorMetadata":{"keep":true}}),
            json!({"id":"au3:aufx:gain:test","name":"Gain","isInstrument":false,"vendorMetadata":{"keep":true}}),
        ])).unwrap();
        assert_eq!(report.plugins.len(), 2);
        for (index, version) in [2, 3].into_iter().enumerate() {
            assert_eq!(report.plugins[index]["auVersion"], version);
            assert_eq!(report.plugins[index]["path"], report.plugins[index]["id"]);
            assert_eq!(
                report.plugins[index]["classId"],
                report.plugins[index]["id"]
            );
            assert_eq!(report.plugins[index]["vendorMetadata"]["keep"], true);
        }
        assert_ne!(report.plugins[0]["id"], report.plugins[1]["id"]);
        let mut child = json!({"id":"vst2:12345678","isInstrument":null,"isShellChild":true,"unknown":{"keep":true}});
        normalize_descriptor(&mut child, "shell.dll", false).unwrap();
        assert!(child["isInstrument"].is_null());
        assert_eq!(child["isShellChild"], true);
        assert_eq!(child["unknown"]["keep"], true);
    }

    #[test]
    fn asynchronous_load_budget_includes_bypassed_master_and_track_units_and_is_bounded() {
        let extension = json!({"format":"au", "pluginId":"au3:aufx:gain:test", "bypassed":false});
        let project = json!({"pluginChain":[extension], "tracks":[{"pluginChain":[
            extension,
            {"format":"au", "pluginId":"au3:aufx:gain:test", "bypassed":true},
            {"format":"au", "pluginId":"au:aufx:gain:test"},
            {"format":"vst3", "pluginId":"au3:aufx:gain:test"}
        ]}]});
        assert_eq!(plugin_load_timeout(&project), Duration::from_secs(60));
        assert_eq!(plugin_load_timeout(&json!({})), REQUEST_TIMEOUT);
        assert_eq!(
            plugin_load_timeout(&json!({"pluginChain":vec![extension; 100]})),
            Duration::from_secs(120)
        );
    }

    #[test]
    fn legacy_unsupported_shell_cache_is_reprobed_with_the_current_contract() {
        let root = tempfile::tempdir().unwrap();
        let binary = root.path().join("shell.dll");
        fs::write(&binary, "shell-binary").unwrap();
        let path = binary
            .canonicalize()
            .unwrap()
            .to_string_lossy()
            .into_owned();
        let fingerprint = bundle_fingerprint(&binary).unwrap();
        fs::write(root.path().join("plugin-cache.json"), serde_json::to_vec(&json!({"version":1,"entries":[{
            "path":path,"fingerprint":fingerprint,"plugins":[],"error":"VST2 shell containers are not supported"
        }]})).unwrap()).unwrap();
        let report = scan_bundles(root.path(), vec![binary], Vec::new(), false, |_| {
            Ok(vec![
                json!({"id":"vst2:12345678","isInstrument":null,"isShellChild":true}),
            ])
        })
        .unwrap();
        assert_eq!(report.scanned, 1);
        assert_eq!(report.cached, 0);
        assert!(report.failures.is_empty());
        assert_eq!(report.plugins[0]["isShellChild"], true);
    }

    #[test]
    fn old_scan_cache_is_reprobed_and_current_category_metadata_survives_reuse() {
        let root = tempfile::tempdir().unwrap();
        let binary = root.path().join("effect.vst3");
        fs::write(&binary, "plugin-binary").unwrap();
        let path = binary
            .canonicalize()
            .unwrap()
            .to_string_lossy()
            .into_owned();
        let fingerprint = bundle_fingerprint(&binary).unwrap();
        fs::write(
            root.path().join("plugin-cache.json"),
            serde_json::to_vec(&json!({
                "version":2,"entries":[{"path":path,"fingerprint":fingerprint,
                    "plugins":[{"id":"gain","isInstrument":false}],"error":null}]
            }))
            .unwrap(),
        )
        .unwrap();
        let report = scan_bundles(root.path(), vec![binary.clone()], Vec::new(), false, |_| {
            Ok(vec![
                json!({"id":"gain","format":"vst3","isInstrument":false,
                "category":"Eq","subCategories":["Fx","EQ"]}),
            ])
        })
        .unwrap();
        assert_eq!(report.scanned, 1);
        assert_eq!(report.cached, 0);
        let cached = scan_bundles(root.path(), vec![binary], Vec::new(), false, |_| {
            panic!("current metadata should be reused without loading the plugin")
        })
        .unwrap();
        assert_eq!(cached.cached, 1);
        assert_eq!(cached.plugins[0]["category"], "Eq");
        assert_eq!(cached.plugins[0]["subCategories"], json!(["Fx", "EQ"]));
    }

    #[test]
    fn scan_paths_only_validates_explicit_sources() {
        let root = tempfile::tempdir().unwrap();
        let missing = root.path().join("missing");
        let file = root.path().join("readme.txt");
        fs::write(&file, "readme").unwrap();
        let valid = root.path().join("plugins");
        fs::create_dir(&valid).unwrap();
        let (paths, failures) = scan_paths(
            [&missing, &file, &valid]
                .map(|path| path.to_string_lossy().into_owned())
                .to_vec(),
        )
        .unwrap();
        assert_eq!(paths, vec![valid]);
        assert_eq!(failures.len(), 2);
        let (paths, failures) = scan_paths(Vec::new()).unwrap();
        assert!(paths.is_empty());
        assert!(failures.is_empty());
        assert!(scan_paths(vec!["relative/plugins".into()]).is_err());
    }

    #[test]
    fn empty_sources_never_probe_or_reuse_an_old_catalog() {
        let root = tempfile::tempdir().unwrap();
        fs::write(
            root.path().join("plugin-cache.json"),
            serde_json::to_vec(&json!({"version":SCAN_CACHE_VERSION,"entries":[{
                "path":"old-fixture.vst3","plugins":[{"id":"old-fixture"}]
            }]}))
            .unwrap(),
        )
        .unwrap();
        let (paths, failures) = scan_paths(Vec::new()).unwrap();
        let bundles = selected_scan_targets(paths);
        assert!(bundles.is_empty());
        let report = scan_bundles(root.path(), bundles, failures, false, |_| {
            panic!("empty sources must never load a plugin")
        })
        .unwrap();
        assert!(report.plugins.is_empty());
        assert!(report.failures.is_empty());
        assert_eq!(report.scanned, 0);
        assert_eq!(report.cached, 0);
    }

    #[cfg(feature = "native-plugins")]
    #[test]
    fn selected_directory_does_not_include_other_sources_or_audio_units() {
        let root = tempfile::tempdir().unwrap();
        let selected = root.path().join("selected");
        let other = root.path().join("other");
        fs::create_dir(&selected).unwrap();
        fs::create_dir(&other).unwrap();
        let plugin = selected.join("Gain.vst3");
        fs::create_dir(&plugin).unwrap();
        fs::create_dir(other.join("Unselected.vst3")).unwrap();
        let (paths, failures) = scan_paths(vec![selected.to_string_lossy().into_owned()]).unwrap();
        assert!(failures.is_empty());
        assert_eq!(selected_scan_targets(paths), vec![plugin]);
    }

    #[test]
    fn audio_unit_catalog_requires_an_explicit_supported_source() {
        let (paths, failures) = scan_paths(vec!["au:system".into(), "au:system".into()]).unwrap();
        if cfg!(all(feature = "native-plugins", target_os = "macos")) {
            assert!(failures.is_empty());
            assert_eq!(
                selected_scan_targets(paths),
                vec![PathBuf::from("au:system")]
            );
        } else {
            assert!(paths.is_empty());
            assert_eq!(failures.len(), 2);
            assert!(failures[0].message.contains("only available on macOS"));
        }
        assert!(scan_paths(vec!["au:unknown".into()]).is_err());
    }

    #[test]
    fn scan_paths_accepts_the_platform_vst2_plugin_file() {
        let root = tempfile::tempdir().unwrap();
        let extension = if cfg!(windows) {
            "dll"
        } else if cfg!(target_os = "macos") {
            "vst"
        } else {
            "so"
        };
        let plugin = root.path().join(format!("effect.{extension}"));
        fs::write(&plugin, "plugin-binary").unwrap();
        let (paths, failures) = scan_paths(vec![plugin.to_string_lossy().into_owned()]).unwrap();
        assert_eq!(paths, vec![plugin]);
        assert!(failures.is_empty());
    }

    #[test]
    fn aax_scan_reports_the_format_boundary_without_loading_or_traversing_contents() {
        let directory = tempfile::tempdir().unwrap();
        let bundle = directory.path().join("Effect.aaxplugin");
        fs::create_dir(&bundle).unwrap();
        let (paths, failures) = scan_paths(vec![bundle.to_string_lossy().into_owned()]).unwrap();
        assert!(failures.is_empty());
        let report = scan_bundles(directory.path(), paths, Vec::new(), true, |_| {
            panic!("an unsupported format must never reach the VST loader")
        })
        .unwrap();
        assert!(report.plugins.is_empty());
        assert_eq!(report.scanned, 0);
        assert_eq!(report.failures.len(), 1);
        assert!(report.failures[0]
            .message
            .contains("AAX plugin hosting is not supported"));
    }

    #[test]
    fn scan_report_retains_successes_and_reuses_success_and_error_fingerprints() {
        let root = tempfile::tempdir().unwrap();
        let good = root.path().join("good.vst3");
        let bad = root.path().join("bad.vst3");
        fs::write(&good, "good").unwrap();
        fs::write(&bad, "bad").unwrap();
        let bundles = vec![good.clone(), bad.clone()];
        let mut calls = 0;
        let first = scan_bundles(root.path(), bundles.clone(), Vec::new(), false, |path| {
            calls += 1;
            if path.file_name().unwrap() == "bad.vst3" {
                Err("Plugin process exited".into())
            } else {
                Ok(vec![
                    json!({"id":"gain","vendorMetadata":{"longValue":"keep"}}),
                ])
            }
        })
        .unwrap();
        assert_eq!(calls, 2);
        assert_eq!(first.scanned, 2);
        assert_eq!(first.cached, 0);
        assert_eq!(first.plugins.len(), 1);
        assert_eq!(first.failures.len(), 1);
        assert_eq!(first.plugins[0]["vendorMetadata"]["longValue"], "keep");
        assert_eq!(
            first.plugins[0]["path"],
            good.canonicalize().unwrap().to_string_lossy().as_ref()
        );
        let second = scan_bundles(root.path(), bundles.clone(), Vec::new(), false, |_| {
            panic!("unchanged successes and errors must both be cached")
        })
        .unwrap();
        assert_eq!(second.scanned, 0);
        assert_eq!(second.cached, 2);
        assert_eq!(second.failures[0].message, "Plugin process exited");
        let forced = scan_bundles(root.path(), bundles.clone(), Vec::new(), true, |_| {
            Ok(vec![json!({"id":"recovered"})])
        })
        .unwrap();
        assert_eq!(forced.scanned, 2);
        assert_eq!(forced.cached, 0);
        assert_eq!(forced.plugins.len(), 2);
        assert!(forced.failures.is_empty());
        fs::write(&bad, "updated plugin bytes").unwrap();
        let changed = scan_bundles(root.path(), bundles, Vec::new(), false, |_| {
            Ok(vec![json!({"id":"updated"})])
        })
        .unwrap();
        assert_eq!(changed.scanned, 1);
        assert_eq!(changed.cached, 1);
    }

    #[test]
    fn scan_report_exposes_invalid_or_empty_bundle_results_and_fingerprint_errors() {
        let root = tempfile::tempdir().unwrap();
        let bad = root.path().join("bad.vst3");
        fs::write(&bad, "bad").unwrap();
        for invalid in [vec![], vec![json!("invalid descriptor")]] {
            let report = scan_bundles(root.path(), vec![bad.clone()], Vec::new(), true, |_| {
                Ok(invalid.clone())
            })
            .unwrap();
            assert!(report.plugins.is_empty());
            assert_eq!(report.failures.len(), 1);
            assert_eq!(report.scanned, 1);
        }
        fs::remove_file(&bad).unwrap();
        let report = scan_bundles(root.path(), vec![bad], Vec::new(), false, |_| {
            panic!("a missing bundle must never be probed")
        })
        .unwrap();
        assert_eq!(report.failures.len(), 1);
        assert_eq!(report.scanned, 0);
        assert_eq!(report.cached, 0);
    }

    #[test]
    fn scan_bundle_limit_and_cache_size_remain_bounded() {
        let root = tempfile::tempdir().unwrap();
        assert!(scan_bundles(
            root.path(),
            vec![root.path().join("bad.vst3"); 4097],
            Vec::new(),
            false,
            |_| { panic!("bundle limit must be checked before probing") }
        )
        .is_err());
        let bundle = root.path().join("large.vst3");
        fs::write(&bundle, "binary").unwrap();
        let error = scan_bundles(root.path(), vec![bundle], Vec::new(), false, |_| {
            Ok(vec![
                json!({"id":"large", "data":"x".repeat(MAX_CACHE_BYTES as usize)}),
            ])
        })
        .err()
        .unwrap();
        assert!(error.contains("size limit"));
        assert!(!root.path().join("plugin-cache.json").exists());
    }

    #[test]
    fn scan_cancellation_preserves_the_previous_cache_and_stops_remaining_probes() {
        let root = tempfile::tempdir().unwrap();
        let cache = root.path().join("plugin-cache.json");
        let previous = br#"{"version":1,"entries":[]}"#;
        fs::write(&cache, previous).unwrap();
        let bundle = root.path().join("effect.vst3");
        fs::write(&bundle, "binary").unwrap();
        let lease = ProcessLease::default();
        let mut calls = 0;
        let result = scan_bundles(
            root.path(),
            vec![bundle.clone(), bundle],
            Vec::new(),
            true,
            |_| {
                calls += 1;
                lease.cancel();
                lease.release();
                Err(ScanProbeError::Cancelled)
            },
        );
        assert!(result.err().unwrap().contains("cancelled"));
        assert_eq!(calls, 1);
        assert_eq!(lease.id(), u32::MAX);
        assert_eq!(fs::read(cache).unwrap(), previous);
    }

    #[test]
    fn snapshot_references_are_content_addressed_without_losing_unknown_slots() {
        let root = tempfile::tempdir().unwrap();
        let saved = persist_snapshot(
            root.path(),
            json!({"states":{"missing-slot":"","known-slot":"YQ=="},"projectId":"one","sessionId":"native-session","stateRevision":7}),
        )
        .unwrap();
        assert!(saved["states"]["missing-slot"]
            .as_str()
            .unwrap()
            .starts_with("sha256:"));
        assert_eq!(saved["projectId"], "one");
        assert_eq!(saved["sessionId"], "native-session");
        assert_eq!(saved["stateRevision"], 7);
    }

    #[test]
    fn render_paths_are_limited_to_a_single_task_directory() {
        let root = tempfile::tempdir().unwrap();
        for invalid in ["", "..", "../other", "a/b", r"a\b", "C:other"] {
            assert!(render_directory(root.path(), invalid).is_err());
        }
        assert_eq!(
            render_directory(root.path(), "task-01_2").unwrap(),
            root.path().join("temp/vst-renders/task-01_2")
        );
    }

    #[cfg(windows)]
    #[test]
    fn crashed_child_reports_failure_without_waiting_for_the_request_timeout() {
        let mut command = Command::new("cmd.exe");
        command.args(["/D", "/C", "exit /b 23"]);
        let process = HostProcess::spawn_command(command).unwrap();
        assert!(process.receive(Duration::from_secs(2)).is_err());
    }

    #[cfg(windows)]
    #[test]
    fn a_child_that_stops_reading_cannot_block_the_input_timeout() {
        let mut command = Command::new("cmd.exe");
        command.args(["/D", "/C", "ping -n 30 127.0.0.1 >nul"]);
        let started = std::time::Instant::now();
        let mut process = HostProcess::spawn_command(command).unwrap();
        let error = process
            .request(
                json!({"op":"load", "data":"x".repeat(1024 * 1024)}),
                Duration::from_millis(80),
            )
            .unwrap_err();
        assert!(error.to_string().contains("timed out"));
        drop(process);
        assert!(started.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn a_new_load_invalidates_old_requests_even_when_the_project_id_is_reused() {
        let state = PluginHostState::default();
        let first = state.generation.fetch_add(1, Ordering::AcqRel) + 1;
        assert!(require_generation(&state, first).is_ok());
        let second = state.generation.fetch_add(1, Ordering::AcqRel) + 1;
        assert!(require_generation(&state, first).is_err());
        assert!(require_generation(&state, second).is_ok());
    }

    #[test]
    fn project_identifiers_are_required() {
        assert!(project_id(&json!({})).is_err());
        assert!(project_id(&json!({"projectId":" "})).is_err());
        assert_eq!(project_id(&json!({"projectId":"one"})).unwrap(), "one");
    }
    #[cfg(windows)]
    fn scripted_session(responses: &[&str]) -> (tempfile::TempDir, Session, ProcessLease) {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("host.cmd");
        let mut script = String::from("@echo off");
        script.push(char::from(10));
        for response in responses {
            script.push_str("set /p fixture_input=");
            script.push(char::from(10));
            script.push_str("echo ");
            script.push_str(response);
            script.push(char::from(10));
        }
        script.push_str("set /p fixture_input=");
        script.push(char::from(10));
        fs::write(&path, script).unwrap();
        let mut command = Command::new("cmd.exe");
        command.args(["/D", "/Q", "/C"]).arg(&path);
        let process = HostProcess::spawn_command(command).unwrap();
        let pid = ProcessLease::default();
        pid.publish(&process).unwrap();
        (
            root,
            Session {
                generation: 9,
                project_id: Some("fixture".into()),
                native_session_id: Some("fixture-session".into()),
                process: Some(process),
            },
            pid,
        )
    }

    #[cfg(windows)]
    #[test]
    fn status_after_native_recovery_updates_the_identity_required_for_discard() {
        let (_root, mut session, pid) = scripted_session(&[
            r#"{"id":1,"ok":true,"result":{"sessionId":"recovered-session","stateRevision":8}}"#,
        ]);
        request_session(&mut session, &pid, json!({"op":"status"}), Duration::from_secs(2)).unwrap();
        assert_eq!(session.native_session_id.as_deref(), Some("recovered-session"));
        discard_session(&mut session, &pid, "fixture", 9, "fixture-session");
        assert!(session.process.is_some());
        discard_session(&mut session, &pid, "fixture", 9, "recovered-session");
        assert!(session.process.is_none());
        assert!(session.native_session_id.is_none());
        assert_eq!(pid.id(), 0);
    }

    #[cfg(windows)]
    #[test]
    fn operation_failure_preserves_session_and_allows_snapshot_retry_in_the_same_process() {
        let (_root, mut session, pid) = scripted_session(&[
            r#"{"id":1,"ok":false,"error":"Snapshot temporarily unavailable"}"#,
            r#"{"id":2,"ok":true,"result":{"states":{},"sessionId":"fixture-session","stateRevision":7}}"#,
        ]);
        let original_pid = pid.id();
        let error = request_session(
            &mut session,
            &pid,
            json!({"op":"snapshot"}),
            Duration::from_secs(2),
        )
        .unwrap_err();
        assert!(matches!(error, HostRequestError::Operation(_)));
        assert_eq!(session.project_id.as_deref(), Some("fixture"));
        assert_eq!(session.generation, 9);
        assert_eq!(pid.id(), original_pid);
        assert!(session
            .process
            .as_mut()
            .unwrap()
            .child
            .try_wait()
            .unwrap()
            .is_none());
        let retried = request_session(
            &mut session,
            &pid,
            json!({"op":"snapshot"}),
            Duration::from_secs(2),
        )
        .unwrap();
        assert_eq!(retried["stateRevision"], 7);
        assert_eq!(retried["sessionId"], "fixture-session");
        assert_eq!(session.process.as_ref().unwrap().child.id(), original_pid);
    }

    #[cfg(windows)]
    #[test]
    fn local_update_preparation_failure_keeps_the_old_graph_for_recovery() {
        let (root, mut session, pid) = scripted_session(&[
            r#"{"id":1,"ok":true,"result":{"states":{"healthy":"YQ=="}}}"#,
            r#"{"id":2,"ok":true,"result":{"playing":false,"unavailablePlugins":[{"slotId":"missing","message":"Saved state is unavailable"}]}}"#,
        ]);
        let original_pid = pid.id();
        let state = PluginHostState::default();
        state.generation.store(9, Ordering::Release);
        let mut update = json!({"projectId":"fixture","project":{"pluginChain":[{
            "id":"missing", "format":"vst3", "bypassed":false,
            "state":format!("sha256:{}", "a".repeat(64))
        }]}});
        assert!(prepare_session_request(&mut session, &pid, false, || {
            resolve_states(root.path(), &mut update)
        })
        .is_err());
        assert_eq!(session.project_id.as_deref(), Some("fixture"));
        assert_eq!(session.generation, 9);
        assert!(require_generation(&state, 9).is_ok());
        assert_eq!(pid.id(), original_pid);
        assert_eq!(session.process.as_ref().unwrap().child.id(), original_pid);
        let snapshot = request_session(
            &mut session,
            &pid,
            json!({"op":"snapshot","projectId":"fixture"}),
            Duration::from_secs(2),
        )
        .unwrap();
        assert_eq!(snapshot["states"]["healthy"], "YQ==");
        update["project"]["pluginChain"][0]["bypassed"] = json!(true);
        prepare_session_request(&mut session, &pid, false, || {
            resolve_states(root.path(), &mut update)
        })
        .unwrap();
        assert_eq!(
            update["project"]["pluginChain"][0]["stateUnavailable"],
            true
        );
        update["op"] = json!("update");
        let response = request_session(&mut session, &pid, update, Duration::from_secs(2)).unwrap();
        assert_eq!(response["unavailablePlugins"][0]["slotId"], "missing");
        assert_eq!(session.process.as_ref().unwrap().child.id(), original_pid);
    }

    #[cfg(windows)]
    #[test]
    fn local_load_preparation_failure_still_closes_the_previous_session() {
        let (_root, mut session, pid) = scripted_session(&[]);
        let error = prepare_session_request(&mut session, &pid, true, || {
            Err("Stored plugin state failed its integrity check".into())
        })
        .unwrap_err();
        assert!(error.contains("integrity check"));
        assert!(session.process.is_none());
        assert!(session.project_id.is_none());
        assert_eq!(pid.id(), 0);
    }

    #[cfg(windows)]
    #[test]
    fn local_request_rejection_keeps_the_native_process_usable() {
        let (_root, mut session, pid) =
            scripted_session(&[r#"{"id":2,"ok":true,"result":{"states":{}}}"#]);
        let original_pid = pid.id();
        let error = request_session(&mut session, &pid, json!("invalid"), Duration::from_secs(2))
            .unwrap_err();
        assert!(matches!(error, HostRequestError::Request(_)));
        assert_eq!(pid.id(), original_pid);
        request_session(
            &mut session,
            &pid,
            json!({"op":"snapshot"}),
            Duration::from_secs(2),
        )
        .unwrap();
        assert_eq!(session.process.as_ref().unwrap().child.id(), original_pid);
    }

    #[cfg(windows)]
    #[test]
    fn malformed_json_and_response_ids_clear_the_session_and_reap_the_child() {
        for response in ["invalid-json", r#"{"id":99,"ok":true,"result":{}}"#] {
            let (_root, mut session, pid) = scripted_session(&[response]);
            let error = request_session(
                &mut session,
                &pid,
                json!({"op":"snapshot"}),
                Duration::from_secs(2),
            )
            .unwrap_err();
            assert!(error.is_fatal());
            assert!(session.process.is_none());
            assert!(session.project_id.is_none());
            assert_eq!(pid.id(), 0);
        }
    }

    #[cfg(windows)]
    #[test]
    fn blocked_or_crashed_child_clears_the_preview_session() {
        for action in ["ping -n 30 127.0.0.1 >nul", "exit /b 23"] {
            let mut command = Command::new("cmd.exe");
            command.args(["/D", "/C", action]);
            let process = HostProcess::spawn_command(command).unwrap();
            let pid = ProcessLease::default();
            pid.publish(&process).unwrap();
            let mut session = Session {
                generation: 1,
                project_id: Some("fixture".into()),
                native_session_id: Some("fixture-session".into()),
                process: Some(process),
            };
            let error = request_session(
                &mut session,
                &pid,
                json!({"op":"snapshot"}),
                Duration::from_millis(80),
            )
            .unwrap_err();
            assert!(error.is_fatal());
            assert!(session.process.is_none());
            assert!(session.project_id.is_none());
            assert_eq!(pid.id(), 0);
        }
    }

    #[test]
    fn request_limit_matches_the_native_reader_and_is_nonfatal() {
        assert!(validate_request_length(MAX_REQUEST_BYTES - 1).is_ok());
        let error = validate_request_length(MAX_REQUEST_BYTES).unwrap_err();
        assert!(matches!(error, HostRequestError::Request(_)));
        assert!(!error.is_fatal());
        assert!(validate_request_length(MAX_RESPONSE_BYTES).is_err());
    }

    #[test]
    fn queued_render_cancellation_prevents_launch_and_cleanup_waits_for_its_lease() {
        let state = PluginHostState::default();
        let root = tempfile::tempdir().unwrap();
        let task = register_render(&state, "queued-task".into()).unwrap();
        assert_eq!(cancel_render(&state, "queued-task").unwrap(), Some(0));
        assert!(task.require_active().is_err());
        assert!(cleanup_render(&state, root.path(), "queued-task").is_err());
        assert!(!render_directory(root.path(), "queued-task")
            .unwrap()
            .exists());
        drop(task);
        assert_eq!(cancel_render(&state, "queued-task").unwrap(), None);
        assert!(cleanup_render(&state, root.path(), "queued-task").is_ok());
    }

    #[test]
    fn duplicate_registration_and_old_leases_cannot_remove_another_render() {
        let state = PluginHostState::default();
        let first = register_render(&state, "same-task".into()).unwrap();
        assert!(register_render(&state, "same-task".into()).is_err());
        assert!(Arc::ptr_eq(
            state.renders.lock().unwrap().get("same-task").unwrap(),
            &first.pid
        ));
        // A stale lease must not remove a replacement registration after an identifier is reused.
        state.renders.lock().unwrap().remove("same-task");
        let replacement = register_render(&state, "same-task".into()).unwrap();
        drop(first);
        assert!(Arc::ptr_eq(
            state.renders.lock().unwrap().get("same-task").unwrap(),
            &replacement.pid
        ));
        drop(replacement);
        assert!(state.renders.lock().unwrap().is_empty());
    }
    #[test]
    fn public_operations_include_parameter_control_without_bypassing_render_isolation() {
        assert!(supported_operation("param"));
        assert!(supported_operation("parameters"));
        // Rendering must retain its dedicated task directory and cancellation lifetime.
        for private_operation in ["render", "--audio-host", "--plugin-scan", "unknown"] {
            assert!(!supported_operation(private_operation));
        }
    }

    #[cfg(windows)]
    #[test]
    fn invalid_parameter_preserves_session_for_parameter_reads_and_subsequent_edits() {
        let (_root, mut session, pid) = scripted_session(&[
            r#"{"id":1,"ok":false,"error":"Parameter value is outside the supported range"}"#,
            r#"{"id":2,"ok":true,"result":{"parameters":[{"id":"7","value":0.5}]}}"#,
            r#"{"id":3,"ok":true,"result":{"stateRevision":8}}"#,
        ]);
        let original_pid = pid.id();
        let error = request_session(
            &mut session,
            &pid,
            json!({"op":"param","slotId":"gain","paramId":"7","value":-1}),
            Duration::from_secs(2),
        )
        .unwrap_err();
        assert!(matches!(error, HostRequestError::Operation(_)));
        assert_eq!(session.project_id.as_deref(), Some("fixture"));
        assert_eq!(session.generation, 9);
        assert_eq!(pid.id(), original_pid);
        let parameters = request_session(
            &mut session,
            &pid,
            json!({"op":"parameters","slotId":"gain"}),
            Duration::from_secs(2),
        )
        .unwrap();
        assert_eq!(parameters["parameters"][0]["id"], "7");
        let edited = request_session(
            &mut session,
            &pid,
            json!({"op":"param","slotId":"gain","paramId":"7","value":0.75}),
            Duration::from_secs(2),
        )
        .unwrap();
        assert_eq!(edited["stateRevision"], 8);
        assert_eq!(session.process.as_ref().unwrap().child.id(), original_pid);
    }
}
