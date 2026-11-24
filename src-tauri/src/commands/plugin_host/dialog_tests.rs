use super::*;
use super::dialogs::Detection;

#[cfg(windows)]
#[test]
fn minimized_app_does_not_hide_a_pending_standard_plugin_dialog_from_detection() {
    use windows_sys::Win32::Foundation::HWND;
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        CreateWindowExW, DestroyWindow, DispatchMessageW, IsIconic, IsWindowVisible, PeekMessageW,
        ShowWindow, TranslateMessage, MSG, PM_REMOVE, SW_HIDE, SW_SHOWMINNOACTIVE,
        SW_SHOWNOACTIVATE, WS_CAPTION, WS_EX_TOOLWINDOW, WS_OVERLAPPEDWINDOW, WS_POPUP, WS_SYSMENU,
    };
    struct Windows(Vec<HWND>);
    impl Drop for Windows {
        fn drop(&mut self) {
            for hwnd in self.0.iter().rev() {
                // SAFETY: each hidden fixture window belongs to this test's thread.
                unsafe {
                    DestroyWindow(*hwnd);
                }
            }
        }
    }
    let title: Vec<_> = "Plugin dialog ownership fixture"
        .encode_utf16()
        .chain(Some(0))
        .collect();
    let mut windows = Windows(Vec::new());
    let mut owner = std::ptr::null_mut();
    for (class, style) in [
        ("STATIC", WS_OVERLAPPEDWINDOW),
        ("#32770", WS_POPUP | WS_CAPTION | WS_SYSMENU),
    ] {
        let class: Vec<_> = class.encode_utf16().chain(Some(0)).collect();
        // SAFETY: system classes and valid retained strings. Offscreen tool windows never
        // activate or take focus; the parent/owned relationship matches the plugin helper.
        let hwnd = unsafe {
            CreateWindowExW(
                WS_EX_TOOLWINDOW,
                class.as_ptr(),
                title.as_ptr(),
                style,
                -30000,
                -30000,
                160,
                100,
                owner,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null(),
            )
        };
        assert!(!hwnd.is_null());
        windows.0.push(hwnd);
        owner = hwnd;
        unsafe {
            ShowWindow(hwnd, SW_SHOWNOACTIVATE);
        }
    }
    let pump = || {
        // SAFETY: MSG is writable; dispatch only messages from the current test thread.
        unsafe {
            let mut message: MSG = std::mem::zeroed();
            while PeekMessageW(&mut message, std::ptr::null_mut(), 0, 0, PM_REMOVE) != 0 {
                TranslateMessage(&message);
                DispatchMessageW(&message);
            }
        }
    };
    pump();
    let pid = std::process::id();
    assert_eq!(
        dialogs::owned_dialog(pid).as_deref(),
        Some("Plugin dialog ownership fixture")
    );
    unsafe {
        ShowWindow(windows.0[0], SW_SHOWMINNOACTIVE);
    }
    pump();
    assert_ne!(unsafe { IsIconic(windows.0[0]) }, 0);
    assert_eq!(unsafe { IsWindowVisible(windows.0[1]) }, 0);
    assert!(
        dialogs::owned_dialog(pid).is_some(),
        "the pending dialog survives minimization"
    );
    unsafe {
        ShowWindow(windows.0[0], SW_SHOWNOACTIVATE);
    }
    pump();
    assert!(dialogs::owned_dialog(pid).is_some());
    unsafe {
        ShowWindow(windows.0[1], SW_HIDE);
    }
    pump();
    assert!(
        dialogs::owned_dialog(pid).is_none(),
        "an explicitly hidden normal dialog is not a pending modal"
    );
}

#[cfg(any(unix, windows))]
struct Fixture {
    process: Option<HostProcess>,
    replies: mpsc::Sender<Result<Value, String>>,
    requests: Arc<Mutex<Vec<Value>>>,
    writer: Option<std::thread::JoinHandle<()>>,
}

#[cfg(any(unix, windows))]
impl Fixture {
    fn new() -> Self {
        #[cfg(windows)]
        let mut command = {
            let mut command = Command::new("cmd.exe");
            command.args(["/D", "/C", "ping -n 30 127.0.0.1 >nul"]);
            command
        };
        #[cfg(unix)]
        let mut command = {
            let mut command = Command::new("sh");
            command.args(["-c", "exec sleep 30"]);
            command
        };
        command.env("PYMSS_STUDIO_DATA_ROOT", "unused-dialog-fixture");
        let mut process = HostProcess::spawn_command(command).unwrap();
        let (replies, responses) = mpsc::channel();
        process.responses = responses;
        let (input, writes) =
            mpsc::sync_channel::<(Vec<u8>, mpsc::SyncSender<Result<(), String>>)>(1);
        process.input = Some(input);
        let requests = Arc::new(Mutex::new(Vec::new()));
        let recorded = requests.clone();
        let sender = replies.clone();
        let writer = std::thread::spawn(move || {
            while let Ok((bytes, ack)) = writes.recv() {
                let request: Value = serde_json::from_slice(&bytes).unwrap();
                let id = request["id"].as_u64().unwrap();
                recorded.lock().unwrap().push(request);
                ack.send(Ok(())).unwrap();
                if id > 1 {
                    let _ = sender.send(Ok(json!({"id":id,"ok":true,"result":{"sessionId":"new-session","position":2,"states":{"effect":"fresh-state"}}})));
                }
            }
        });
        Self {
            process: Some(process),
            replies,
            requests,
            writer: Some(writer),
        }
    }
    fn process(&mut self) -> &mut HostProcess {
        self.process.as_mut().unwrap()
    }
    fn suspend(&mut self, operation: &str, timeout: Duration) {
        let result =
            self.process()
                .request_live_with(json!({"op":operation}), timeout, Detection::Early, &mut |_| {
                    Some("Authorization".into())
                });
        assert!(matches!(result, Err(HostRequestError::DialogPending(_))));
        assert!(!result.unwrap_err().is_fatal());
    }
}

#[cfg(any(unix, windows))]
impl Drop for Fixture {
    fn drop(&mut self) {
        self.process.take();
        if let Some(writer) = self.writer.take() {
            writer.join().unwrap();
        }
    }
}

#[cfg(any(unix, windows))]
#[test]
fn popup_hint_does_not_interrupt_a_reply_within_the_deadline() {
    let mut fixture = Fixture::new();
    let sender = fixture.replies.clone();
    let reply = std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(125));
        sender.send(Ok(json!({"id":1,"ok":true,"result":{"states":{}}}))).unwrap();
    });
    let result = fixture.process().request_live_with(
        json!({"op":"snapshot"}), Duration::from_secs(2), Detection::AtDeadline,
        &mut |_| panic!("A popup must only be checked after the reply deadline"),
    ).unwrap();
    reply.join().unwrap();
    assert_eq!(result["states"], json!({}));
    assert!(fixture.process().pending.is_none());
}

#[cfg(any(unix, windows))]
#[test]
fn popup_at_the_deadline_keeps_the_original_request_pending() {
    let mut fixture = Fixture::new();
    let error = fixture.process().request_live_with(
        json!({"op":"snapshot"}), Duration::from_millis(75), Detection::AtDeadline,
        &mut |_| Some(String::new()),
    ).unwrap_err();
    assert!(matches!(error, HostRequestError::DialogPending(_)));
    assert!(fixture.process().pending.as_ref().unwrap().is_suspended());
    fixture.replies.send(Ok(json!({"id":1,"ok":true,"result":{"states":{}}}))).unwrap();
    fixture.process().finish_pending_with(Detection::AtDeadline, &mut |_| {
        panic!("The original reply must be consumed before inspecting windows");
    }).unwrap();
    assert!(fixture.process().pending.is_none());
    assert_eq!(fixture.requests.lock().unwrap().len(), 1);
}

#[cfg(any(unix, windows))]
#[test]
fn popup_does_not_prevent_replacing_a_responsive_session() {
    let mut fixture = Fixture::new();
    fixture.replies.send(Ok(json!({"id":1,"ok":true,"result":{"sessionId":"old-session"}}))).unwrap();
    let process = fixture.process.take().unwrap();
    let pid = ProcessLease::default();
    pid.publish(&process).unwrap();
    let mut session = Session { process: Some(process), ..Session::default() };
    prepare_live_load_with(
        &mut session, &pid, 2, Detection::AtDeadline, &mut |_| Some(String::new()),
    ).unwrap();
    assert_eq!(fixture.requests.lock().unwrap()[0]["op"], "status");
    assert!(session.process.is_none());
    assert_eq!(pid.id(), 0);
    assert_eq!(session.generation, 2);
}

#[cfg(any(unix, windows))]
#[test]
fn modal_retains_one_request_and_drains_its_exact_id_before_another_write() {
    let mut fixture = Fixture::new();
    fixture.suspend("editor", Duration::from_millis(30));
    std::thread::sleep(Duration::from_millis(50));
    let result =
        fixture
            .process()
            .request_live_with(json!({"op":"status"}), REQUEST_TIMEOUT, Detection::Early, &mut |_| {
                Some("Authorization".into())
            });
    assert!(result
        .unwrap_err()
        .to_string()
        .starts_with(dialogs::PENDING_PREFIX));
    assert_eq!(fixture.process().sequence, 1);
    assert!(fixture.process().child.try_wait().unwrap().is_none());
    fixture
        .replies
        .send(Ok(json!({"id":1,"ok":true,"result":{"open":true}})))
        .unwrap();
    let result = fixture
        .process()
        .request_live_with(json!({"op":"status"}), Duration::from_secs(1), Detection::Early, &mut |_| {
            None
        })
        .unwrap();
    assert_eq!(result["position"], 2);
    assert_eq!(
        fixture
            .requests
            .lock()
            .unwrap()
            .iter()
            .map(|request| request["id"].as_u64().unwrap())
            .collect::<Vec<_>>(),
        vec![1, 2]
    );
    assert!(fixture.process().pending.is_none());
    assert_eq!(
        fixture.process().completed_session_id.as_deref(),
        Some("new-session")
    );
}

#[cfg(any(unix, windows))]
#[test]
fn stale_reply_and_eof_are_fatal_even_when_a_modal_was_detected() {
    for reply in [
        Ok(json!({"id":99,"ok":true,"result":{}})),
        Err("The helper exited".into()),
    ] {
        let mut fixture = Fixture::new();
        fixture.suspend("editor", Duration::from_millis(30));
        fixture.replies.send(reply).unwrap();
        let result = fixture.process().request_live_with(
            json!({"op":"status"}),
            REQUEST_TIMEOUT,
            Detection::Early,
            &mut |_| Some("Authorization".into()),
        );
        assert!(result.unwrap_err().is_fatal());
        assert_eq!(fixture.process().sequence, 1);
    }
}

#[cfg(any(unix, windows))]
#[test]
fn absent_modal_keeps_the_original_timeout_and_dismissed_modal_has_a_bounded_deadline() {
    for suspend in [false, true] {
        let mut fixture = Fixture::new();
        if suspend {
            fixture.suspend("editor", Duration::from_millis(60));
        }
        let started = std::time::Instant::now();
        let result = fixture.process().request_live_with(
            json!({"op":"status"}),
            Duration::from_millis(60),
            Detection::Early,
            &mut |_| None,
        );
        assert!(result.unwrap_err().is_fatal());
        assert!(started.elapsed() >= Duration::from_millis(50));
        assert!(started.elapsed() < Duration::from_secs(2));
        assert_eq!(fixture.process().sequence, 1);
        let retry = std::time::Instant::now();
        assert!(fixture
            .process()
            .request_live_with(json!({"op":"status"}), REQUEST_TIMEOUT, Detection::Early, &mut |_| None)
            .unwrap_err()
            .is_fatal());
        assert!(
            retry.elapsed() < Duration::from_millis(40),
            "retry must not renew a non-modal deadline"
        );
    }
}

#[cfg(any(unix, windows))]
#[test]
fn unload_releases_the_helper_after_a_deferred_load_is_rejected() {
    let mut fixture = Fixture::new();
    fixture.suspend("load", Duration::from_millis(30));
    fixture.replies.send(Ok(json!({"id":1,"ok":false,"error":"Plugin load failed"}))).unwrap();
    let result = fixture.process().request_live_with(
        json!({"op":"unload"}), REQUEST_TIMEOUT, Detection::Early, &mut |_| None,
    );
    assert!(result.is_ok());
    assert!(fixture.process().live_closed);
    assert_eq!(fixture.requests.lock().unwrap()[1]["op"], "unload");
}

#[cfg(any(unix, windows))]
#[test]
fn deferred_snapshot_is_drained_but_only_a_new_snapshot_is_returned_for_persistence() {
    let mut fixture = Fixture::new();
    fixture.suspend("snapshot", Duration::from_millis(30));
    fixture.replies.send(Ok(json!({"id":1,"ok":true,"result":{"sessionId":"old-session","states":{"effect":"stale-state"}}}))).unwrap();
    let pid = ProcessLease::default();
    let process = fixture.process.take().unwrap();
    pid.publish(&process).unwrap();
    let mut session = Session {
        generation: 1,
        project_id: Some("project".into()),
        native_session_id: Some("old-session".into()),
        process: Some(process),
    };
    let snapshot = request_session(
        &mut session,
        &pid,
        json!({"op":"snapshot"}),
        Duration::from_secs(1),
    )
    .unwrap();
    assert_eq!(snapshot["states"]["effect"], "fresh-state");
    assert_eq!(session.native_session_id.as_deref(), Some("new-session"));
    assert_eq!(session.process.as_ref().unwrap().sequence, 2);
}

#[cfg(any(unix, windows))]
#[test]
fn deferred_close_and_eof_reconcile_the_parent_session_before_any_further_write() {
    for closed in [false, true] {
        let mut fixture = Fixture::new();
        fixture.suspend(
            if closed { "unload" } else { "editor" },
            Duration::from_millis(30),
        );
        fixture
            .replies
            .send(if closed {
                Ok(json!({"id":1,"ok":true,"result":{"closed":true}}))
            } else {
                Err("The helper exited".into())
            })
            .unwrap();
        let process = fixture.process.take().unwrap();
        let control = process.control.clone();
        let pid = ProcessLease::default();
        pid.publish(&process).unwrap();
        let mut session = Session {
            generation: 1,
            project_id: Some("project".into()),
            native_session_id: Some("instance".into()),
            process: Some(process),
        };
        assert!(
            request_session(&mut session, &pid, json!({"op":"status"}), REQUEST_TIMEOUT).is_err()
        );
        assert!(session.process.is_none());
        assert!(session.project_id.is_none());
        assert!(session.native_session_id.is_none());
        assert_eq!(pid.id(), 0);
        assert!(!*control.active.lock().unwrap());
        assert_eq!(fixture.requests.lock().unwrap().len(), 1);
    }
}

#[cfg(any(unix, windows))]
#[test]
fn deferred_close_never_sends_another_request_to_a_closed_graph() {
    let mut fixture = Fixture::new();
    fixture.suspend("unload", Duration::from_millis(30));
    fixture
        .replies
        .send(Ok(json!({"id":1,"ok":true,"result":{"closed":true}})))
        .unwrap();
    let result =
        fixture
            .process()
            .request_live_with(json!({"op":"status"}), REQUEST_TIMEOUT, Detection::Early, &mut |_| None);
    assert!(!result.unwrap_err().is_fatal());
    assert!(fixture.process().live_closed);
    assert_eq!(fixture.process().sequence, 1);
    assert_eq!(
        fixture
            .process()
            .request_live_with(json!({"op":"unload"}), REQUEST_TIMEOUT, Detection::Early, &mut |_| None)
            .unwrap()["closed"],
        true
    );
}

#[cfg(any(unix, windows))]
#[test]
fn replacement_waits_for_modal_but_explicit_discard_and_shutdown_reclaim_the_owned_helper() {
    for shutdown in [false, true] {
        let mut fixture = Fixture::new();
        fixture.suspend("editor", Duration::from_millis(30));
        let state = PluginHostState::default();
        let live = state.live_session("editor-project", || true).unwrap();
        let process = fixture.process.take().unwrap();
        let control = process.control.clone();
        live.session_pid.publish(&process).unwrap();
        {
            let mut session = live.session.lock().unwrap();
            *session = Session {
                generation: 1,
                project_id: Some("project".into()),
                native_session_id: Some("instance".into()),
                process: Some(process),
            };
            let result = prepare_live_load_with(&mut session, &live.session_pid, 2, Detection::Early, &mut |_| {
                Some("Authorization".into())
            });
            assert!(matches!(result, Err(HostRequestError::DialogPending(_))));
            assert_eq!(session.generation, 2);
            assert_eq!(session.project_id.as_deref(), Some("project"));
            if !shutdown {
                discard_session(&mut session, &live.session_pid, "project", 2, "instance");
                assert!(session.process.is_none());
            }
        }
        if shutdown {
            state.shutdown();
        }
        assert!(!*control.active.lock().unwrap());
        assert!(live.session.lock().unwrap().process.is_none());
    }
}

#[cfg(any(unix, windows))]
#[test]
fn window_destruction_returns_before_background_cleanup_and_reaps_the_host() {
    let mut fixture = Fixture::new();
    let root = tempfile::tempdir().unwrap();
    let host = PluginHostState::default();
    let live = host.live_session("editor-project", || true).unwrap();
    let process = fixture.process.take().unwrap();
    let control = process.control.clone();
    live.session_pid.publish(&process).unwrap();
    live.session.lock().unwrap().process = Some(process);

    let process_guard = control.active.lock().unwrap();
    let (closed, returned) = mpsc::channel();
    let closer = {
        let host = host.clone();
        std::thread::spawn(move || {
            host.release_window("editor-project");
            closed.send(()).unwrap();
        })
    };
    let returned_before_unlock = returned.recv_timeout(Duration::from_secs(2)).is_ok();
    let registered_for_cleanup = host.sessions.lock().unwrap().closing.iter()
        .any(|session| Arc::ptr_eq(session, &live));
    let closing_snapshot = diagnostic_snapshot(root.path(), &host);
    let busy_snapshot = {
        let _busy = live.session.lock().unwrap();
        diagnostic_snapshot(root.path(), &host)
    };
    drop(process_guard);
    closer.join().unwrap();

    assert!(returned_before_unlock, "window destruction waited for the host process lock");
    assert!(registered_for_cleanup);
    assert_eq!(closing_snapshot["session"]["hasProcess"], true);
    assert_eq!(closing_snapshot["session"]["activeCount"], 1);
    assert_eq!(busy_snapshot["session"]["busyCount"], 1);
    assert!(busy_snapshot["session"].get("hasProcess").is_none());

    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    while !host.sessions.lock().unwrap().closing.is_empty() {
        assert!(std::time::Instant::now() < deadline, "background window cleanup did not finish");
        std::thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(live.session_pid.id(), u32::MAX);
    assert!(!*control.active.lock().unwrap());
    assert!(live.session.lock().unwrap().process.is_none());
    let reaped_snapshot = diagnostic_snapshot(root.path(), &host);
    assert_eq!(reaped_snapshot["session"]["status"], "idle");
    assert_eq!(reaped_snapshot["session"]["hasProcess"], false);
}

#[cfg(any(unix, windows))]
#[test]
fn shutdown_reaps_a_closing_host_before_its_background_cleanup_starts() {
    let mut fixture = Fixture::new();
    let host = PluginHostState::default();
    let live = host.live_session("editor-project", || true).unwrap();
    let process = fixture.process.take().unwrap();
    let control = process.control.clone();
    live.session_pid.publish(&process).unwrap();
    live.session.lock().unwrap().process = Some(process);

    let closing = host.sessions.lock().unwrap().begin_close("editor-project").unwrap();
    assert!(Arc::ptr_eq(&closing, &live));
    host.shutdown();

    assert_eq!(live.session_pid.id(), u32::MAX);
    assert!(!*control.active.lock().unwrap());
    assert!(live.session.lock().unwrap().process.is_none());
    assert!(host.sessions.lock().unwrap().closing.is_empty());
}

#[cfg(any(unix, windows))]
#[test]
fn two_editor_windows_can_snapshot_and_release_their_own_hosts() {
    let mut first_host = Fixture::new();
    let mut second_host = Fixture::new();
    let host = PluginHostState::default();
    let first = host.live_session("editor-first", || true).unwrap();
    let second = host.live_session("editor-second", || true).unwrap();
    for (live, fixture, project_id, native_id) in [
        (&first, &mut first_host, "first", "first-native"),
        (&second, &mut second_host, "second", "second-native"),
    ] {
        let mut session = live.session.lock().unwrap();
        let generation = begin_load_generation(live, None).unwrap();
        prepare_live_load_with(&mut session, &live.session_pid, generation, Detection::Early, &mut |_| None).unwrap();
        let process = fixture.process.take().unwrap();
        live.session_pid.publish(&process).unwrap();
        session.process = Some(process);
        session.project_id = Some(project_id.into());
        fixture
            .replies
            .send(Ok(json!({"id":1,"ok":true,"result":{"sessionId":native_id}})))
            .unwrap();
        request_session(
            &mut session,
            &live.session_pid,
            json!({"op":"load"}),
            Duration::from_secs(2),
        )
        .unwrap();
    }

    let root = tempfile::tempdir().unwrap();
    {
        let _busy = second.session.lock().unwrap();
        let snapshot = diagnostic_snapshot(root.path(), &host);
        assert_eq!(snapshot["session"]["hasProcess"], true);
        assert_eq!(snapshot["session"]["activeCount"], 1);
        assert_eq!(snapshot["session"]["busyCount"], 1);
    }

    let first = host.live_session("editor-first", || true).unwrap();
    let first_generation = first.generation.load(Ordering::Acquire);
    assert!(require_generation(&first, first_generation).is_ok());
    let mut session = first.session.lock().unwrap();
    assert_eq!(session.project_id.as_deref(), Some("first"));
    let snapshot = request_session(
        &mut session,
        &first.session_pid,
        json!({"op":"snapshot"}),
        Duration::from_secs(2),
    )
    .unwrap();
    assert_eq!(snapshot["states"]["effect"], "fresh-state");
    request_session(
        &mut session,
        &first.session_pid,
        json!({"op":"unload"}),
        Duration::from_secs(2),
    )
    .unwrap();
    assert!(session.process.is_none());
    drop(session);
    host.release_window("editor-first");

    let second = host.live_session("editor-second", || true).unwrap();
    assert!(require_generation(&second, second.generation.load(Ordering::Acquire)).is_ok());
    let mut session = second.session.lock().unwrap();
    assert_eq!(session.project_id.as_deref(), Some("second"));
    assert_eq!(session.native_session_id.as_deref(), Some("second-native"));
    let status = request_session(
        &mut session,
        &second.session_pid,
        json!({"op":"status"}),
        Duration::from_secs(2),
    )
    .unwrap();
    assert_eq!(status["position"], 2);
}

#[cfg(any(unix, windows))]
#[test]
fn fatal_load_preparation_retains_the_claimed_generation_for_recovery() {
    let mut fixture = Fixture::new();
    fixture.suspend("editor", Duration::from_millis(30));
    fixture.replies.send(Err("The helper exited".into())).unwrap();
    let state = LiveSession::default();
    state.generation.store(1, Ordering::Release);
    let process = fixture.process.take().unwrap();
    state.session_pid.publish(&process).unwrap();
    let mut session = state.session.lock().unwrap();
    *session = Session {
        generation: 1,
        project_id: Some("project".into()),
        native_session_id: Some("instance".into()),
        process: Some(process),
    };
    let generation = begin_load_generation(&state, None).unwrap();
    let error = prepare_live_load_with(&mut session, &state.session_pid, generation, Detection::Early, &mut |_| None)
        .unwrap_err();
    assert!(error.is_fatal());
    assert!(session.process.is_none());
    assert_eq!(state.session_pid.id(), 0);
    assert_eq!(session.generation, generation);
    let reply = discard_session(&mut session, &state.session_pid, "project", generation, "instance");
    assert_eq!(reply["superseded"], false);
    assert!(require_generation(&state, generation).is_ok());
}

#[cfg(windows)]
#[test]
#[ignore = "opt-in: observes the owned helper's real plugin authorization dialog for 22 seconds"]
fn real_plugin_authorization_keeps_its_owned_session_alive() {
    // Explicitly supplied installed plugin and host paths; never discover or interact with
    // existing application/plugin windows. Dropping HostProcess reaps only this owned child.
    let required = |key: &str| {
        std::env::var_os(key).unwrap_or_else(|| panic!("Set {key} to run this ignored test"))
    };
    let executable = required("PYMSS_REAL_PLUGIN_HOST_EXE");
    let plugin = required("PYMSS_REAL_PLUGIN_PATH");
    let plugin_id = required("PYMSS_REAL_PLUGIN_ID")
        .to_string_lossy()
        .into_owned();
    let ffmpeg = required("PYMSS_REAL_PLUGIN_FFMPEG");
    let root = tempfile::tempdir().unwrap();
    let mut command = Command::new(executable);
    command
        .arg("--audio-host")
        .env("PYMSS_STUDIO_DATA_ROOT", root.path());
    let mut process = HostProcess::spawn_command(command).unwrap();
    let project = json!({"id":"authorization-observation", "name":"Authorization observation", "sources":[], "tracks":[],
        "pluginChain":[{"id":"effect","format":"vst3","pluginId":plugin_id,"name":"Effect","path":PathBuf::from(plugin),"bypassed":false}], "masterVolume":1});
    process.request_live(json!({"op":"load","projectId":"authorization-observation","project":project,"ffmpeg":PathBuf::from(ffmpeg)}), REQUEST_TIMEOUT).unwrap();
    let opened = process.request_live(json!({"op":"editor","projectId":"authorization-observation","slotId":"effect","open":true}), REQUEST_TIMEOUT);
    assert!(opened.is_ok() || matches!(opened, Err(HostRequestError::DialogPending(_))));
    let appearing = std::time::Instant::now();
    let title = loop {
        if let Some(title) = dialogs::owned_dialog(process.control.pid) {
            break title;
        }
        assert!(
            appearing.elapsed() < Duration::from_secs(2),
            "the owned helper did not open an authorization dialog"
        );
        std::thread::sleep(Duration::from_millis(25));
    };
    eprintln!("Observed owned plugin dialog: {title}");
    let started = std::time::Instant::now();
    while started.elapsed() < Duration::from_secs(22) {
        let result = process.request_live(
            json!({"op":"status","projectId":"authorization-observation"}),
            REQUEST_TIMEOUT,
        );
        let error = result
            .expect_err("this opt-in test requires an outstanding plugin authorization dialog");
        assert!(matches!(error, HostRequestError::DialogPending(_)));
        assert!(!error.is_fatal());
        assert!(process.child.try_wait().unwrap().is_none());
        std::thread::sleep(Duration::from_millis(250));
    }
    let control = process.control.clone();
    drop(process);
    assert!(!*control.active.lock().unwrap());
}
