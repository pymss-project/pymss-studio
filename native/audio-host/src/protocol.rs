use crate::engine::{Event, Session};
use crate::graph::same_plugin_instance;
use crate::timeline::{PluginSlot, SessionSpec};
use crossbeam_channel::{Receiver, bounded};
use serde_json::{Value, json};
use std::io::BufRead;
use std::path::{Path, PathBuf};
use std::thread;
use std::time::Duration;

#[cfg(windows)]
fn editor_owner(message: &Value) -> Result<usize, String> {
    match message.get("editorOwner") {
        None => Ok(0),
        Some(value) => value
            .as_u64()
            .and_then(|value| usize::try_from(value).ok())
            .ok_or_else(|| "The plugin editor's owner reference is invalid".into()),
    }
}

fn au_scan_target(path: &Path) -> Result<Option<&str>, String> {
    let Some(id) = path.to_str() else {
        return Ok(None);
    };
    if id == "au:system" {
        return Ok(Some(id));
    }
    if id.starts_with("au:") || id.starts_with("au3:") {
        return soundcraft_au_host::parse_id(id)
            .map(|_| Some(id))
            .ok_or_else(|| "Audio Unit reference is invalid".into());
    }
    Ok(None)
}

#[cfg(any(target_os = "macos", test))]
fn au_descriptor(plugin: soundcraft_au_host::AuDescriptor) -> Value {
    let codes = soundcraft_au_host::parse_id(&plugin.id);
    json!({ "id": plugin.id, "classId": plugin.id, "format": "au", "name": plugin.name,
        "vendor": plugin.vendor, "version": plugin.version, "path": plugin.id,
        "isInstrument": plugin.is_instrument, "auVersion": if plugin.is_v3 { 3 } else { 2 },
        "componentType": codes.map(|codes| codes.0), "componentSubtype": codes.map(|codes| codes.1),
        "componentManufacturer": codes.map(|codes| codes.2) })
}

fn vst2_descriptor(plugin: pymss_vst2_host::Vst2Descriptor) -> Value {
    json!({ "id": plugin.id, "classId": plugin.id, "format": "vst2", "name": plugin.name,
        "vendor": plugin.vendor, "version": plugin.version, "path": plugin.path,
        "isInstrument": plugin.is_instrument, "isShellChild": plugin.is_shell_child,
        "rawCategory": plugin.raw_category })
}

fn vst3_descriptor(plugin: soundcraft_vst3_host::Vst3Descriptor) -> Value {
    json!({ "id": plugin.id, "classId": plugin.class_id, "format": "vst3", "name": plugin.name,
        "vendor": plugin.vendor, "version": plugin.version, "path": plugin.path,
        "isInstrument": plugin.is_instrument, "category": plugin.category,
        "subCategories": plugin.sub_categories })
}

fn scan(path: &Path) -> Result<Value, String> {
    if let Some(target) = au_scan_target(path)? {
        #[cfg(target_os = "macos")]
        {
            let descriptors = if target == "au:system" {
                soundcraft_au_host::scan()
            } else {
                soundcraft_au_host::scan_component(target).map_err(|error| error.to_string())?
            };
            let plugins: Vec<Value> = descriptors
                .into_iter()
                .take(4096)
                .map(au_descriptor)
                .collect();
            return Ok(json!({"plugins": plugins}));
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = target;
            return Err("Audio Units require macOS".into());
        }
    }
    if !path.is_absolute() {
        return Err("Plugin path must be absolute".into());
    }
    if let Some(reason) = crate::discovery::unsupported_plugin_reason(path) {
        return Err(reason.into());
    }
    if !crate::discovery::is_vst3_path(path) {
        let plugins: Vec<Value> = pymss_vst2_host::scan_binaries(path)?
            .into_iter()
            .map(vst2_descriptor)
            .collect();
        return Ok(json!({"plugins": plugins}));
    }
    let plugins: Vec<Value> = soundcraft_vst3_host::load_bundle(path)
        .map_err(|e| e.to_string())?
        .into_iter()
        .map(vst3_descriptor)
        .collect();
    Ok(json!({ "plugins": plugins }))
}

fn respond(id: u64, result: Result<Value, String>) {
    let response = match result {
        Ok(value) => json!({ "id": id, "ok": true, "result": value }),
        Err(error) => json!({ "id": id, "ok": false, "error": error }),
    };
    let _ = crate::protocol_output::write(&response);
}

fn inputs() -> Receiver<Result<Value, String>> {
    let (tx, rx) = bounded(32);
    thread::spawn(move || {
        let stdin = std::io::stdin();
        let mut stdin = stdin.lock();
        loop {
            let mut line = String::new();
            let read = std::io::Read::take(&mut stdin, 64 * 1024 * 1024 + 1).read_line(&mut line);
            match read {
                Ok(0) => break,
                Ok(_) if line.len() > 64 * 1024 * 1024 => {
                    let _ = tx.send(Err("Audio request exceeds the supported size".into()));
                    break;
                }
                Ok(_) => {
                    if tx
                        .send(
                            serde_json::from_str(&line)
                                .map_err(|e| format!("Invalid audio request: {e}")),
                        )
                        .is_err()
                    {
                        break;
                    }
                }
                Err(_) => break,
            }
        }
    });
    rx
}

fn same_plugin_topology(previous: &SessionSpec, next: &SessionSpec) -> bool {
    let same_chain = |old: &[PluginSlot], new: &[PluginSlot]| {
        old.len() == new.len()
            && old.iter().zip(new).all(|(old, new)| same_plugin_instance(old, new))
    };
    same_chain(&previous.plugin_chain, &next.plugin_chain)
        && previous.tracks.iter().zip(&next.tracks)
            .all(|(old, new)| same_chain(&old.plugin_chain, &new.plugin_chain))
}

fn project(request: &Value) -> Result<SessionSpec, String> {
    serde_json::from_value(
        request
            .get("project")
            .cloned()
            .ok_or("Audio project is required")?,
    )
    .map_err(|e| format!("Invalid audio project: {e}"))
}

fn monitor_tracks(request: &Value) -> Result<Vec<String>, String> {
    let tracks = request
        .get("suppressedTrackIds")
        .and_then(Value::as_array)
        .ok_or("Suppressed track identifiers must be an array of strings")?;
    if tracks.len() > 256 {
        return Err("Monitor suppression cannot exceed 256 tracks".into());
    }
    tracks
        .iter()
        .map(|track| {
            track
                .as_str()
                .map(str::to_owned)
                .ok_or_else(|| "Suppressed track identifiers must be an array of strings".into())
        })
        .collect()
}

#[cfg(test)]
fn preserve_states(next: &mut SessionSpec, previous: &SessionSpec, snapshot: &Value) {
    preserve_states_except(next, previous, snapshot, &[]);
}

fn preserve_states_except(
    next: &mut SessionSpec,
    previous: &SessionSpec,
    snapshot: &Value,
    restore: &[String],
) {
    // Update edits the live graph. Its controller state is authoritative for an
    // existing slot; a project load is the explicit state-restoration boundary.
    let old = previous.slots();
    let apply = |slots: &mut [PluginSlot]| {
        for slot in slots {
            if restore.contains(&slot.id) {
                continue;
            }
            if old
                .iter()
                .any(|s| s.id == slot.id && s.path == slot.path && s.plugin_id == slot.plugin_id)
            {
                if let Some(state) = snapshot
                    .get("states")
                    .and_then(|s| s.get(&slot.id))
                    .and_then(Value::as_str)
                {
                    slot.state = Some(state.into());
                }
            }
        }
    };
    apply(&mut next.plugin_chain);
    for track in &mut next.tracks {
        apply(&mut track.plugin_chain);
    }
}

#[derive(Clone, Copy)]
struct RenderRequest { id: u64, progress: bool }

fn host() -> i32 {
    let input = inputs();
    let mut session: Option<Session> = None;
    let mut render_request: Option<RenderRequest> = None;
    let mut render_session: Option<Session> = None;
    while soundcraft_vst3_host::with_ui_context(|| {
        host_tick(
            &input,
            &mut session,
            &mut render_request,
            &mut render_session,
        )
    }) {}
    soundcraft_vst3_host::with_ui_context(|| {
        drop(render_session);
        drop(session);
        soundcraft_au_host::shutdown_ui();
        pymss_vst2_host::shutdown();
        soundcraft_vst3_host::shutdown();
    });
    0
}

fn host_tick(
    input: &Receiver<Result<Value, String>>,
    session: &mut Option<Session>,
    render_request: &mut Option<RenderRequest>,
    render_session: &mut Option<Session>,
) -> bool {
    soundcraft_vst3_host::pump_ui();
    soundcraft_au_host::pump_ui();
    pymss_vst2_host::pump_ui();
    let service = |current: &mut Session, request: &mut Option<RenderRequest>| {
        let mut finished = false;
        current.idle();
        while let Ok(event) = current.events.try_recv() {
            match event {
                Event::Failure(error) => current.fail(error),
                Event::Timing(request) => current.service_timing(request),
                Event::LatencyChanged {
                    change,
                    graph_revision,
                } => {
                    if let Err(error) = current.recover_latency(change, graph_revision) {
                        current.fail(error);
                    }
                }
                Event::RenderProgress(progress) => {
                    if let Some(request) = request.as_ref().filter(|request| request.progress) {
                        let _ = crate::protocol_output::write(&json!({"id":request.id,"event":"render_progress","payload":progress}));
                    }
                }
                Event::Render(result) => {
                    current.finish_render();
                    if let Some(request) = request.take() {
                        respond(request.id, result);
                    }
                    finished = true;
                }
            }
        }
        finished
    };
    if let Some(current) = session.as_mut() {
        service(current, render_request);
    }
    if render_session
        .as_mut()
        .is_some_and(|exporter| service(exporter, render_request))
    {
        // Join and release processors/controllers on the helper UI thread.
        render_session.take();
    }
    let message = match input.recv_timeout(Duration::from_millis(8)) {
        Ok(Ok(value)) => value,
        Ok(Err(error)) => {
            respond(0, Err(error));
            return true;
        }
        Err(crossbeam_channel::RecvTimeoutError::Timeout) => return true,
        Err(_) => return false,
    };
    let id = message.get("id").and_then(Value::as_u64).unwrap_or(0);
    let op = message.get("op").and_then(Value::as_str).unwrap_or("");
    if op == "shutdown" {
        respond(id, Ok(json!({ "closed": true })));
        return false;
    }
    if render_request.is_some() {
        respond(id, Err("Audio export is already running".into()));
        return true;
    }
    let result = (|| -> Result<Option<Value>, String> {
        if op == "load" || op == "update" {
            let offline = match message.get("offline") {
                None => op == "update" && session.as_ref().is_some_and(Session::is_offline),
                Some(value) => value.as_bool().ok_or("Offline mode must be a boolean")?,
            };
            let sample_rate = match message.get("sampleRate") {
                None => None,
                Some(value) => {
                    let rate = value.as_u64().filter(|rate| (8000..=48000).contains(rate))
                        .ok_or("Render sample rate must be an integer between 8000 and 48000 Hz")?;
                    if !offline { return Err("An explicit sample rate requires offline mode".into()); }
                    Some(rate as u32)
                }
            };
            let mut spec = project(&message)?;
            spec.validate()?;
            let restore: Vec<String> = match message.get("stateRestoreSlotIds") {
                Some(value) => serde_json::from_value(value.clone()).map_err(
                    |_| "State restore instance identifiers must be an array of strings",
                )?,
                None => Vec::new(),
            };
            if restore
                .iter()
                .any(|id| !spec.slots().iter().any(|slot| slot.id == *id))
            {
                return Err("A state restore instance is unavailable in this project".into());
            }
            let ffmpeg = PathBuf::from(message.get("ffmpeg").and_then(Value::as_str).unwrap_or(""));
            if !ffmpeg.is_absolute() || !ffmpeg.is_file() {
                return Err("FFmpeg executable is unavailable".into());
            }
            if op == "update" {
                if let Some(current) = session.as_mut() {
                    if current.spec.id != spec.id {
                        return Err("Audio project changed".into());
                    }
                    if current.accepts_in_place_update(&spec, &ffmpeg)
                        && current.is_offline() == offline
                        && sample_rate.is_none_or(|rate| rate == current.sample_rate)
                    {
                        if restore.is_empty()
                            && same_plugin_topology(&current.spec, &spec)
                            && current.can_update_in_place(&spec)
                        {
                            return current.update_mix(spec).map(Some);
                        }
                        if !offline {
                            return current.update_plugins(spec, &restore).map(Some);
                        }
                    }
                }
            }
            let mut looping = false;
            let mut open = Vec::new();
            let mut suppressed = Vec::new();
            if let Some(current) = session.as_mut() {
                looping = current.looping;
                if op == "update" {
                    suppressed = current
                        .suppressed_track_ids()
                        .iter()
                        .filter(|id| {
                            spec.tracks
                                .iter()
                                .any(|track| track.id.as_str() == id.as_str())
                        })
                        .cloned()
                        .collect();
                    let snapshot = current.snapshot()?;
                    preserve_states_except(&mut spec, &current.spec, &snapshot, &restore);
                    open = current.open_editors();
                } else {
                    current.pause()?;
                }
            }
            let mut new_session = match sample_rate {
                Some(rate) => Session::load_at_rate(spec, ffmpeg, rate, offline)?,
                None => Session::load_mode(spec, ffmpeg, offline)?,
            };
            if !suppressed.is_empty() {
                new_session.monitor(suppressed)?;
            }
            // Keep the old graph running while the replacement is prepared and
            // transfer its latest cursor when swapping outputs.
            let previous = session
                .as_ref()
                .filter(|_| op == "update")
                .map(Session::status);
            let resume = !offline
                && previous
                    .as_ref()
                    .and_then(|status| status["playing"].as_bool())
                    .unwrap_or(false);
            let position = previous
                .as_ref()
                .and_then(|status| status["position"].as_f64())
                .unwrap_or(0.0);
            *session = Some(new_session);
            let current = session.as_mut().ok_or("Audio session is unavailable")?;
            if resume {
                current.play(position, looping)?;
            } else if op == "update" {
                current.looping = looping;
                current.seek(position)?;
            }
            current.reopen_editors(open);
            return Ok(Some(current.status()));
        }
        if op == "unload" && session.is_none() {
            return Ok(Some(json!({ "closed": true })));
        }
        let current = session.as_mut().ok_or("Audio project is not loaded")?;
        if message
            .get("projectId")
            .and_then(Value::as_str)
            .is_some_and(|p| p != current.spec.id)
        {
            return Err("Audio project changed".into());
        }
        let value = match op {
            "status" => current.status(),
            "monitor" => current.monitor(monitor_tracks(&message)?)?,
            "play" => current.play(
                message
                    .get("position")
                    .and_then(Value::as_f64)
                    .unwrap_or(0.0),
                message
                    .get("loop")
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
            )?,
            "pause" => current.pause()?,
            "seek" => current.seek(
                message
                    .get("position")
                    .and_then(Value::as_f64)
                    .ok_or("Playback position is required")?,
            )?,
            "editor" => {
                let open = message.get("open").and_then(Value::as_bool).unwrap_or(true);
                #[cfg(windows)]
                if open {
                    soundcraft_vst3_host::set_editor_owner(editor_owner(&message)?)?;
                }
                if open {
                    soundcraft_vst3_host::set_editor_transport_owner(message.get("transportOwner").and_then(Value::as_str).unwrap_or(""))?;
                }
                current.editor(
                    message.get("slotId").and_then(Value::as_str).unwrap_or(""),
                    open,
                )?
            }
            "param" => current.param(
                message.get("slotId").and_then(Value::as_str).unwrap_or(""),
                message.get("paramId").and_then(Value::as_str).unwrap_or(""),
                message
                    .get("value")
                    .and_then(Value::as_f64)
                    .ok_or("Parameter value is required")? as f32,
            )?,
            "parameters" => {
                current.parameters(message.get("slotId").and_then(Value::as_str).unwrap_or(""))?
            }
            "snapshot" => current.snapshot()?,
            "render" => {
                let progress = match message.get("progress") {
                    None => false,
                    Some(value) => value.as_bool().ok_or("Render progress option must be a boolean")?,
                };
                let path = PathBuf::from(
                    message
                        .get("path")
                        .and_then(Value::as_str)
                        .ok_or("Render output path is required")?,
                );
                if current.is_offline() {
                    current.start_render(path)?;
                } else {
                    let mut exporter = current.offline_copy()?;
                    exporter.start_render(path)?;
                    *render_session = Some(exporter);
                }
                *render_request = Some(RenderRequest { id, progress });
                return Ok(None);
            }
            "unload" => {
                *session = None;
                json!({ "closed": true })
            }
            _ => return Err("Unsupported audio operation".into()),
        };
        Ok(Some(value))
    })();
    match result {
        Ok(Some(mut value)) => {
            let owner = message.get("transportOwner").and_then(Value::as_str).unwrap_or("");
            value["transportOwner"] = json!(owner);
            value["transportRevision"] = json!(soundcraft_vst3_host::editor_transport_revision(owner));
            respond(id, Ok(value));
        }
        Err(error) => {
            // An update can leave the old graph running if replacement fails.
            // Stop it before replying, including when a later RPC drains this error.
            if op == "update"
                && let Some(current) = session.as_mut()
                && message.pointer("/project/id").and_then(Value::as_str)
                    == Some(current.spec.id.as_str())
                && let Err(cause) = current.pause()
            {
                current.fail(cause);
            }
            respond(id, Err(error));
        }
        Ok(None) => {}
    }
    true
}

/// Enter helper modes before Tauri, startup recovery and the application singleton.
pub fn run_cli() -> Option<i32> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    let mode = args.first()?.to_str()?;
    if mode != "--audio-host" && mode != "--plugin-scan" && mode != "--aax-probe" {
        return None;
    }
    if let Err(error) = crate::protocol_output::initialize() {
        eprintln!("Cannot initialize audio protocol output: {error}");
        return Some(1);
    }
    #[cfg(windows)]
    // SAFETY: initialize the process UI thread's apartment before creating controllers.
    let com = unsafe {
        windows_sys::Win32::System::Com::CoInitializeEx(
            std::ptr::null(),
            windows_sys::Win32::System::Com::COINIT_APARTMENTTHREADED as u32,
        )
    };
    let code = soundcraft_vst3_host::with_ui_context(|| {
        if mode == "--plugin-scan" || mode == "--aax-probe" {
            let result = args
                .get(1)
                .map(PathBuf::from)
                .ok_or_else(|| "Plugin path is required".to_string())
                .and_then(|p| {
                    if mode == "--aax-probe" {
                        pymss_aax_probe::probe(&p).and_then(|report| {
                            serde_json::to_value(report).map_err(|error| error.to_string())
                        })
                    } else {
                        scan(&p)
                    }
                });
            match result {
                Ok(value) => match crate::protocol_output::write(&value) {
                    Ok(()) => 0,
                    Err(error) => {
                        eprintln!("Cannot write plugin scan result: {error}");
                        1
                    }
                },
                Err(error) => {
                    eprintln!("{error}");
                    1
                }
            }
        } else {
            match soundcraft_vst3_host::initialize_ui() {
                Ok(()) => host(),
                Err(error) => {
                    eprintln!("{error}");
                    1
                }
            }
        }
    });
    soundcraft_vst3_host::with_ui_context(|| {
        soundcraft_au_host::shutdown_ui();
        pymss_vst2_host::shutdown();
        soundcraft_vst3_host::shutdown();
        soundcraft_vst3_host::shutdown_ui();
    });
    #[cfg(windows)]
    if com >= 0 {
        // SAFETY: balanced with successful initialization above, after every plugin is gone.
        unsafe {
            windows_sys::Win32::System::Com::CoUninitialize();
        }
    }
    Some(code)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(windows)]
    #[test]
    fn editor_owner_references_preserve_standalone_requests_and_reject_invalid_numbers() {
        assert_eq!(editor_owner(&json!({})).unwrap(), 0);
        assert_eq!(editor_owner(&json!({"editorOwner": 12345})).unwrap(), 12345);
        for value in [
            json!(null),
            json!(-1),
            json!(1.5),
            json!("12345"),
            json!([]),
        ] {
            assert!(editor_owner(&json!({"editorOwner": value})).is_err());
        }
    }

    #[test]
    fn monitor_request_requires_a_bounded_string_array() {
        assert!(monitor_tracks(&json!({})).is_err());
        for value in [
            Value::Null,
            json!("track"),
            json!(["track", 2]),
            json!([false]),
        ] {
            assert!(monitor_tracks(&json!({"suppressedTrackIds":value})).is_err());
        }
        assert_eq!(
            monitor_tracks(&json!({"suppressedTrackIds":[]})).unwrap(),
            Vec::<String>::new()
        );
        assert_eq!(
            monitor_tracks(&json!({"suppressedTrackIds":["voice"]})).unwrap(),
            ["voice"]
        );
        assert!(monitor_tracks(&json!({"suppressedTrackIds": vec!["voice";257]})).is_err());
    }

    #[test]
    fn au_scan_targets_keep_versioned_registry_ids_and_reject_malformed_references() {
        for target in [
            "au:system",
            "au:aufx:dely:appl",
            "au3:aufx:dely:appl",
            "au3:aufx:#00000001:abcd",
        ] {
            assert_eq!(au_scan_target(Path::new(target)).unwrap(), Some(target));
        }
        for target in ["au3:system", "au:aufx:bad:appl", "au3:aumu:dely:appl:extra"] {
            assert!(au_scan_target(Path::new(target)).is_err());
        }
        assert_eq!(au_scan_target(Path::new("effect.vst3")).unwrap(), None);
    }

    #[test]
    fn au_scan_metadata_distinguishes_components_with_the_same_fourcc() {
        let descriptor = |is_v3| soundcraft_au_host::AuDescriptor {
            id: soundcraft_au_host::format_variant_id(
                soundcraft_au_host::TYPE_EFFECT,
                u32::from_be_bytes(*b"dely"),
                u32::from_be_bytes(*b"appl"),
                is_v3,
            ),
            name: "Delay".into(),
            vendor: "Apple".into(),
            version: "1.0.0".into(),
            component_type: "aufx".into(),
            subtype: "dely".into(),
            manufacturer: "appl".into(),
            category: soundcraft_dsp::Category::Delay,
            is_instrument: false,
            is_v3,
        };
        let legacy = au_descriptor(descriptor(false));
        let extension = au_descriptor(descriptor(true));
        assert_eq!(legacy["auVersion"], 2);
        assert_eq!(extension["auVersion"], 3);
        assert_eq!(extension["format"], "au");
        assert_eq!(extension["path"], extension["id"]);
        assert_ne!(legacy["id"], extension["id"]);
    }

    #[test]
    fn vst3_scan_metadata_preserves_sdk_categories() {
        let descriptor = vst3_descriptor(soundcraft_vst3_host::Vst3Descriptor {
            id: "vst3:0123456789ABCDEF0123456789ABCDEF".into(),
            class_id: "0123456789ABCDEF0123456789ABCDEF".into(),
            name: "Audio Processor".into(),
            vendor: "Vendor".into(),
            version: "1.0.0".into(),
            sdk_version: "VST 3.7".into(),
            sub_categories: vec!["Fx".into(), "EQ".into(), "Filter".into()],
            category: soundcraft_dsp::Category::Eq,
            is_instrument: false,
            path: PathBuf::from("processor.vst3"),
        });
        assert_eq!(descriptor["category"], "Eq");
        assert_eq!(descriptor["subCategories"], json!(["Fx", "EQ", "Filter"]));
        assert_eq!(descriptor["format"], "vst3");
        assert_eq!(descriptor["isInstrument"], false);
    }

    #[test]
    fn vst2_scan_metadata_preserves_the_reported_raw_category() {
        let descriptor = vst2_descriptor(pymss_vst2_host::Vst2Descriptor {
            id: "vst2:1234ABCD".into(),
            name: "Equalizer".into(),
            vendor: "Vendor".into(),
            version: "1".into(),
            path: PathBuf::from("processor.dll"),
            raw_category: Some(6),
            is_instrument: Some(false),
            is_shell_child: false,
        });
        assert_eq!(descriptor["rawCategory"], 6);
        assert!(descriptor.get("category").is_none());
        assert!(descriptor.get("subCategories").is_none());
    }

    #[test]
    fn au_scan_metadata_exposes_component_codes_without_inferred_categories() {
        let descriptor = au_descriptor(soundcraft_au_host::AuDescriptor {
            id: "au:aufx:abcd:ABCD".into(),
            name: "Reverb".into(),
            vendor: "Vendor".into(),
            version: "1.0.0".into(),
            component_type: "aufx".into(),
            subtype: "abcd".into(),
            manufacturer: "ABCD".into(),
            category: soundcraft_dsp::Category::Reverb,
            is_instrument: false,
            is_v3: false,
        });
        assert_eq!(descriptor["componentType"], u32::from_be_bytes(*b"aufx"));
        assert_eq!(descriptor["componentSubtype"], u32::from_be_bytes(*b"abcd"));
        assert_eq!(
            descriptor["componentManufacturer"],
            u32::from_be_bytes(*b"ABCD")
        );
        assert!(descriptor.get("category").is_none());
        assert!(descriptor.get("subCategories").is_none());
    }

    #[test]
    fn shell_scan_metadata_preserves_unknown_classification() {
        let descriptor = vst2_descriptor(pymss_vst2_host::Vst2Descriptor {
            id: "vst2:1234ABCD".into(),
            name: "Effect".into(),
            vendor: "Vendor".into(),
            version: String::new(),
            path: PathBuf::from("shell.dll"),
            raw_category: None,
            is_instrument: None,
            is_shell_child: true,
        });
        assert!(descriptor["isInstrument"].is_null());
        assert!(descriptor["rawCategory"].is_null());
        assert_eq!(descriptor["isShellChild"], true);
    }

    fn session(state: &str) -> SessionSpec {
        serde_json::from_value(json!({ "id": "one", "name": "Audio", "sources": [], "tracks": [],
            "pluginChain": [{ "id": "slot", "format": "vst3", "pluginId": "vst3:ABC",
                "name": "Effect", "path": "C:/plugins/effect.vst3", "state": state, "bypassed": false }]
        })).unwrap()
    }

    #[test]
    fn closed_protocol_input_exits_even_with_a_pending_render() {
        let (tx, input) = bounded(1);
        drop(tx);
        let mut session = None;
        let mut pending = Some(RenderRequest { id: 12, progress: false });
        assert!(!host_tick(&input, &mut session, &mut pending, &mut None));
    }

    #[test]
    fn shutdown_is_accepted_while_a_render_request_is_pending() {
        let (tx, input) = bounded(1);
        tx.send(Ok(json!({"id": 13, "op": "shutdown"}))).unwrap();
        let mut session = None;
        let mut pending = Some(RenderRequest { id: 12, progress: false });
        assert!(!host_tick(&input, &mut session, &mut pending, &mut None));
    }

    #[test]
    fn rejected_update_stops_the_matching_graph_at_its_current_position() {
        let mut spec = session("unused");
        spec.plugin_chain.clear();
        let active = Session::load_at_rate(spec, PathBuf::from("ffmpeg"), 48000, false).unwrap();
        active.start_test_transport(12000);
        let mut session = Some(active);
        let (tx, input) = bounded(1);
        for (project, playing) in [("other", true), ("one", false)] {
            tx.send(Ok(json!({"id": 1, "op": "update", "sampleRate": 0, "project": {"id": project}}))).unwrap();
            assert!(host_tick(&input, &mut session, &mut None, &mut None));
            let status = session.as_ref().unwrap().status();
            assert_eq!(status["playing"], playing);
            assert_eq!(status["position"], 0.25);
        }
    }

    #[test]
    fn state_and_bypass_changes_keep_topology_but_relocation_requires_a_rebuild() {
        let one = session("old-state");
        let mut two = session("new-state");
        assert!(same_plugin_topology(&one, &two));
        two.plugin_chain[0].bypassed = true;
        assert!(same_plugin_topology(&one, &two));
        two.plugin_chain[0].path = "C:/plugins/relocated.vst3".into();
        assert!(!same_plugin_topology(&one, &two));
    }

    #[test]
    fn repeated_updates_keep_live_state_despite_an_older_frontend_snapshot() {
        let old = session("previous-host-capture");
        let mut next = session("older-frontend-reference");
        preserve_states(
            &mut next,
            &old,
            &json!({"states": {"slot": "latest-controller-state"}}),
        );
        assert_eq!(
            next.plugin_chain[0].state.as_deref(),
            Some("latest-controller-state")
        );
        next.plugin_chain[0].plugin_id = "vst3:DIFFERENT".into();
        preserve_states(
            &mut next,
            &old,
            &json!({"states": {"slot": "unrelated-state"}}),
        );
        assert_eq!(
            next.plugin_chain[0].state.as_deref(),
            Some("latest-controller-state")
        );
    }
}
