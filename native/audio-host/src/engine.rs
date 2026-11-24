use crate::graph::{BLOCK, Graph, GraphError, LatencyChange, UiSlot, UnavailablePlugin};
use crate::output::{self, Clock, Frame, Output};
use crate::timeline::{PreparedMixUpdate, SessionSpec, monitor_mask, prepare_mix_update};
use base64::Engine;
use crossbeam_channel::{Receiver, Sender, bounded};
use rtrb::{Consumer, Producer};
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

enum Control {
    Play(u64, bool, u64),
    Pause,
    Seek(u64, u64),
    Mix(SessionSpec, PreparedMixUpdate, Sender<Result<(), String>>),
    Monitor(Vec<String>, Sender<Result<(), String>>),
    Param(String, String, f64, f32),
    Params(Vec<(String, String, f64, f32)>),
    Barrier(Sender<Result<(), String>>, Arc<AtomicBool>),
    Renegotiate(Sender<Result<u64, String>>),
    Render(PathBuf),
}

/// A capture owns the pause; releasing it cannot be blocked by a full command
/// queue. In particular, an expired request must not leave a late barrier held.
struct SnapshotPause(Arc<AtomicBool>);

/// A rare stopped-worker timing handoff. The lock covers the control-thread SDK calls,
/// so timeout/cancellation cannot let DSP resume concurrently with an in-flight query.
pub(crate) struct TimingRequest {
    active: Arc<Mutex<bool>>,
    cancel: Arc<AtomicBool>,
    deadline: Instant,
    reply: Sender<Result<(), String>>,
}

impl TimingRequest {
    fn service(self, refresh: impl FnOnce() -> Result<(), String>) {
        let mut active = self.active.lock().unwrap_or_else(PoisonError::into_inner);
        if !*active {
            return;
        }
        if self.cancel.load(Ordering::Acquire) || Instant::now() >= self.deadline {
            *active = false;
            let error = if self.cancel.load(Ordering::Acquire) {
                "Audio rendering cancelled"
            } else {
                "Plugin timing refresh timed out"
            };
            let _ = self.reply.try_send(Err(error.into()));
            return;
        }
        let result = refresh();
        *active = false;
        let _ = self.reply.try_send(result);
    }
}

fn request_timing_refresh(
    events: &Sender<Event>,
    cancel: &Arc<AtomicBool>,
    timeout: Duration,
) -> Result<(), String> {
    if cancel.load(Ordering::Acquire) {
        return Err("Audio rendering cancelled".into());
    }
    let active = Arc::new(Mutex::new(true));
    let deadline = Instant::now() + timeout;
    let (reply, completed) = bounded(1);
    events
        .try_send(Event::Timing(TimingRequest {
            active: Arc::clone(&active),
            cancel: Arc::clone(cancel),
            deadline,
            reply,
        }))
        .map_err(|_| "Plugin timing request queue is unavailable".to_string())?;
    let result = loop {
        if cancel.load(Ordering::Acquire) {
            break Err("Audio rendering cancelled".into());
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break Err("Plugin timing refresh timed out".into());
        }
        match completed.recv_timeout(remaining.min(Duration::from_millis(20))) {
            Ok(result) => break result,
            Err(crossbeam_channel::RecvTimeoutError::Disconnected) => {
                break Err("Plugin timing response is unavailable".into());
            }
            Err(crossbeam_channel::RecvTimeoutError::Timeout) => {}
        }
    };
    // If service already entered the SDK, wait for it to leave even after a timeout.
    // Otherwise invalidate the ticket before any late UI event can touch the processor.
    *active.lock().unwrap_or_else(PoisonError::into_inner) = false;
    if cancel.load(Ordering::Acquire) {
        Err("Audio rendering cancelled".into())
    } else {
        result
    }
}

fn collect_editor_edits(id: &str, slot: &mut UiSlot, edits: &mut Vec<(String, String, f64, f32)>) {
    let Some(editor) = &mut slot.editor else {
        return;
    };
    for (param, value) in editor.idle() {
        if !value.is_finite() {
            continue;
        }
        let Some(normalized) = editor
            .normalize(&param, value)
            .filter(|value| value.is_finite())
        else {
            continue;
        };
        slot.plain_values.insert(param.clone(), value);
        if slot.values.get(&param) == Some(&normalized) {
            continue;
        }
        edits.push((id.into(), param.clone(), normalized, value));
        slot.values.insert(param, normalized);
    }
}

impl SnapshotPause {
    fn acquire(commands: &Sender<Control>, timeout: Duration) -> Result<Self, String> {
        let pause = Self(Arc::new(AtomicBool::new(true)));
        let (reply, received) = bounded(1);
        commands
            .try_send(Control::Barrier(reply, Arc::clone(&pause.0)))
            .map_err(|_| "Audio command queue is unavailable".to_string())?;
        received
            .recv_timeout(timeout)
            .map_err(|_| "Audio processing did not stop for state capture".to_string())??;
        Ok(pause)
    }
}

impl Drop for SnapshotPause {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

#[derive(Clone, Copy, serde::Serialize)]
pub(crate) struct RenderProgress {
    pub phase: &'static str,
    pub completed: u64,
    pub total: u64,
}

pub(crate) enum Event {
    Failure(String),
    Timing(TimingRequest),
    LatencyChanged {
        change: LatencyChange,
        graph_revision: u64,
    },
    RenderProgress(RenderProgress),
    Render(Result<Value, String>),
}

struct Meter {
    left: AtomicU32,
    right: AtomicU32,
}

#[derive(Default)]
struct LatencyRecovery {
    vst_used: bool,
    au_attempts: u8,
    last_au_completed: Option<Instant>,
}

impl LatencyRecovery {
    fn begin(&mut self, now: Instant, is_au: bool) -> bool {
        if !is_au {
            return !std::mem::replace(&mut self.vst_used, true);
        }
        // A single AU edit can publish several latency changes as its processing chain
        // settles. Bound back-to-back recovery while allowing subsequent edits.
        const MAX_AU_ATTEMPTS: u8 = 4;
        const AU_QUIET_PERIOD: Duration = Duration::from_secs(2);
        if self
            .last_au_completed
            .is_some_and(|last| now.duration_since(last) >= AU_QUIET_PERIOD)
        {
            self.au_attempts = 0;
        }
        if self.au_attempts >= MAX_AU_ATTEMPTS {
            return false;
        }
        self.au_attempts += 1;
        true
    }

    fn complete(&mut self, now: Instant, is_au: bool) {
        if is_au {
            self.last_au_completed = Some(now);
        } else {
            // The snapshot taken during recovery can collect editor callbacks.
            // Those events are part of the recovery that just completed.
            self.vst_used = true;
        }
    }
}

pub(crate) struct Session {
    pub spec: SessionSpec,
    pub sample_rate: u32,
    offline: bool,
    rendering: bool,
    ffmpeg: PathBuf,
    tx: Sender<Control>,
    pub events: Receiver<Event>,
    clock: Arc<Clock>,
    meters: Arc<Vec<Meter>>,
    master_meter: Arc<Meter>,
    cancel: Arc<AtomicBool>,
    worker: Option<JoinHandle<Graph>>,
    ui: HashMap<String, UiSlot>,
    consumer: Option<Consumer<Frame>>,
    output: Option<Output>,
    pub error: Option<String>,
    pub looping: bool,
    pub session_id: String,
    state_revision: u64,
    latency_recovery: LatencyRecovery,
    transport_playing: bool,
    graph_revision: u64,
    unavailable_plugins: Vec<UnavailablePlugin>,
    suppressed_track_ids: Vec<String>,
}

fn session_id() -> String {
    static SEQUENCE: AtomicU64 = AtomicU64::new(0);
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    format!(
        "{}-{stamp}-{}",
        std::process::id(),
        SEQUENCE.fetch_add(1, Ordering::Relaxed)
    )
}

impl Session {
    #[cfg(test)]
    pub fn load(spec: SessionSpec, ffmpeg: PathBuf) -> Result<Self, String> {
        Self::load_mode(spec, ffmpeg, false)
    }

    pub fn load_mode(spec: SessionSpec, ffmpeg: PathBuf, offline: bool) -> Result<Self, String> {
        use cpal::traits::{DeviceTrait, HostTrait};
        let rate = cpal::default_host()
            .default_output_device()
            .and_then(|d| d.default_output_config().ok())
            .map(|c| c.sample_rate())
            .unwrap_or(48000);
        Self::load_at_rate(spec, ffmpeg, rate, offline)
    }

    pub(crate) fn load_at_rate(
        spec: SessionSpec,
        ffmpeg: PathBuf,
        rate: u32,
        offline: bool,
    ) -> Result<Self, String> {
        let (graph, ui) = Graph::load_mode(spec.clone(), ffmpeg.clone(), rate, offline)?;
        let unavailable_plugins = graph.unavailable_plugins.clone();
        let clock = Arc::new(Clock::default());
        let meters: Arc<Vec<Meter>> = Arc::new(
            spec.tracks
                .iter()
                .map(|_| Meter {
                    left: AtomicU32::new(0),
                    right: AtomicU32::new(0),
                })
                .collect(),
        );
        let cancel = Arc::new(AtomicBool::new(false));
        let master_meter = Arc::new(Meter {
            left: AtomicU32::new(0),
            right: AtomicU32::new(0),
        });
        let (tx, rx) = bounded(256);
        let (events_tx, events) = bounded(32);
        let (producer, consumer) = output::audio_ring();
        let state = Arc::clone(&clock);
        let worker_meters = Arc::clone(&meters);
        let worker_master = Arc::clone(&master_meter);
        let stop = Arc::clone(&cancel);
        let worker = thread::Builder::new()
            .name("vst-audio-render".into())
            .spawn(move || {
                process(
                    graph,
                    rx,
                    events_tx,
                    producer,
                    state,
                    worker_meters,
                    worker_master,
                    stop,
                )
            })
            .map_err(|e| e.to_string())?;
        Ok(Self {
            spec,
            sample_rate: rate,
            offline,
            rendering: false,
            ffmpeg,
            tx,
            events,
            clock,
            meters,
            master_meter,
            cancel,
            worker: Some(worker),
            ui,
            consumer: Some(consumer),
            output: None,
            error: None,
            looping: false,
            session_id: session_id(),
            state_revision: 0,
            latency_recovery: LatencyRecovery::default(),
            transport_playing: false,
            graph_revision: 0,
            unavailable_plugins,
            suppressed_track_ids: Vec::new(),
        })
    }

    fn send(&self, command: Control) -> Result<(), String> {
        self.tx
            .try_send(command)
            .map_err(|_| "Audio command queue is unavailable".into())
    }

    pub fn status(&self) -> Value {
        let levels: serde_json::Map<String, Value> = self
            .spec
            .tracks
            .iter()
            .zip(self.meters.iter())
            .map(|(t, m)| {
                (
                    t.id.clone(),
                    json!([
                        f32::from_bits(m.left.load(Ordering::Relaxed)),
                        f32::from_bits(m.right.load(Ordering::Relaxed))
                    ]),
                )
            })
            .collect();
        json!({ "projectId": self.spec.id, "sessionId": self.session_id, "stateRevision": self.state_revision,
            "openEditors": self.open_editors(), "unavailablePlugins": self.unavailable_plugins, "position": self.clock.position() as f64 / f64::from(self.sample_rate),
            "duration": self.spec.duration(), "sampleRate": self.sample_rate, "offline": self.offline,
            "playing": self.playback_intent(), "recovering": self.clock.recovering.load(Ordering::Acquire), "levels": levels,
            "masterLevel": [f32::from_bits(self.master_meter.left.load(Ordering::Relaxed)), f32::from_bits(self.master_meter.right.load(Ordering::Relaxed))],
            "loop": self.looping, "suppressedTrackIds": self.suppressed_track_ids,
            "underruns": self.clock.underruns.load(Ordering::Relaxed), "error": self.error })
    }

    pub fn play(&mut self, position: f64, looping: bool) -> Result<Value, String> {
        if self.offline {
            return Err(
                "Offline sessions cannot play to an audio device; reload the project for preview"
                    .into(),
            );
        }
        if let Some(error) = &self.error {
            return Err(error.clone());
        }
        if self.output.is_none() {
            let consumer = self
                .consumer
                .take()
                .ok_or("Audio output must be reloaded")?;
            let output = Output::open(consumer, Arc::clone(&self.clock))?;
            if output.sample_rate != self.sample_rate {
                return Err("Audio device changed. Reload the project before playback".into());
            }
            self.output = Some(output);
        }
        let frame = self.frame(position)?;
        self.looping = looping;
        self.transport_playing = true;
        let generation = self.clock.transition(frame, true);
        if let Err(error) = self.send(Control::Play(frame, looping, generation)) {
            self.fail(error.clone());
            return Err(error);
        }
        Ok(self.status())
    }

    fn frame(&self, position: f64) -> Result<u64, String> {
        if !position.is_finite() || position < 0.0 {
            return Err("Invalid playback position".into());
        }
        Ok((position.min(self.spec.duration()) * f64::from(self.sample_rate)).round() as u64)
    }

    pub fn pause(&mut self) -> Result<Value, String> {
        self.transport_playing = false;
        self.clock.transition(self.clock.position(), false);
        self.send(Control::Pause)?;
        Ok(self.status())
    }

    #[cfg(test)]
    pub(crate) fn start_test_transport(&self, frame: u64) {
        self.clock.transition(frame, true);
    }

    pub fn seek(&mut self, position: f64) -> Result<Value, String> {
        let frame = self.frame(position)?;
        self.transport_playing = self.playback_intent();
        let generation = self.clock.transition(frame, self.transport_playing);
        if let Err(error) = self.send(Control::Seek(frame, generation)) {
            self.fail(error.clone());
            return Err(error);
        }
        Ok(self.status())
    }

    pub fn editor(&mut self, slot: &str, open: bool) -> Result<Value, String> {
        self.idle();
        // AU view factories and teardown may inspect unit properties. Keep these operations
        // outside concurrent rendering, and flush already-observed edits before creating views.
        let _pause = if self.ui.get(slot).is_some_and(|entry| entry.format == "au") {
            Some(SnapshotPause::acquire(&self.tx, Duration::from_secs(5))?)
        } else {
            None
        };
        let entry = self
            .ui
            .get_mut(slot)
            .ok_or("Plugin instance is unavailable")?;
        let editor = entry
            .editor
            .as_mut()
            .ok_or("This plugin does not provide an editor")?;
        if open {
            editor.open()?;
        } else {
            editor.close();
        }
        let changed = entry.was_open && !editor.is_open();
        entry.was_open = editor.is_open();
        if changed {
            self.state_revision = self.state_revision.saturating_add(1);
        }
        Ok(json!({ "open": editor.is_open() }))
    }

    pub fn parameters(&self, slot: &str) -> Result<Value, String> {
        let slot = self.ui.get(slot).ok_or("Plugin instance is unavailable")?;
        Ok(
            json!({ "params": slot.info.params, "values": slot.plain_values,
            "projectId": self.spec.id, "sessionId": self.session_id }),
        )
    }

    pub fn param(&mut self, slot: &str, id: &str, value: f32) -> Result<Value, String> {
        if !value.is_finite() {
            return Err("Invalid parameter value".into());
        }
        let entry = self
            .ui
            .get_mut(slot)
            .ok_or("Plugin controller is unavailable")?;
        let value = entry
            .info
            .param(id)
            .ok_or("Unknown plugin parameter")?
            .clamp(value);
        let editor = entry
            .editor
            .as_mut()
            .ok_or("Plugin controller is unavailable")?;
        let normalized = editor
            .normalize(id, value)
            .ok_or("Unknown plugin parameter")?;
        self.tx
            .try_send(Control::Param(slot.into(), id.into(), normalized, value))
            .map_err(|_| "Plugin parameter queue is unavailable".to_string())?;
        editor.set_param(id, value);
        entry.plain_values.insert(id.into(), value);
        if entry.values.insert(id.into(), normalized) != Some(normalized) {
            self.state_revision = self.state_revision.saturating_add(1);
            self.latency_recovery = LatencyRecovery::default();
        }
        Ok(
            json!({ "updated": true, "value": value, "projectId": self.spec.id, "sessionId": self.session_id, "stateRevision": self.state_revision }),
        )
    }

    pub fn idle(&mut self) {
        // A render worker can be awaiting a control-thread timing reply. AU close/capture
        // housekeeping may synchronously wait for that same worker; defer it until export ends.
        if self.rendering {
            return;
        }
        if self.clock.device_failed.swap(false, Ordering::AcqRel) {
            let _ = self.send(Control::Pause);
            self.fail("Audio output device failed. Reload the project to reconnect".into());
        }
        let mut edits = Vec::new();
        let mut closed_au = Vec::new();
        for (id, slot) in &mut self.ui {
            collect_editor_edits(id, slot, &mut edits);
            if let Some(editor) = &mut slot.editor {
                let open = editor.is_open();
                if slot.was_open && !open {
                    self.state_revision = self.state_revision.saturating_add(1);
                    if slot.format == "au" {
                        closed_au.push(id.clone());
                    }
                }
                slot.was_open = open;
            }
        }
        self.apply_editor_edits(edits);
        if !closed_au.is_empty() {
            match SnapshotPause::acquire(&self.tx, Duration::from_secs(5)) {
                Ok(pause) => {
                    let mut final_edits = Vec::new();
                    for id in closed_au {
                        if let Some(slot) = self.ui.get_mut(&id) {
                            if let Some(editor) = &mut slot.editor {
                                editor.close();
                            }
                            collect_editor_edits(&id, slot, &mut final_edits);
                        }
                    }
                    drop(pause);
                    self.apply_editor_edits(final_edits);
                }
                Err(error) => self.fail(error),
            }
        }
    }

    fn apply_editor_edits(&mut self, edits: Vec<(String, String, f64, f32)>) {
        if !edits.is_empty() {
            let count = edits.len() as u64;
            if self.tx.try_send(Control::Params(edits)).is_err() {
                self.fail("Plugin parameter batch queue is unavailable".into());
            } else {
                self.state_revision = self.state_revision.saturating_add(count);
                // An AU can emit these callbacks while its latency is settling. Keep
                // its recovery budget until the quiet period or an explicit host edit.
                self.latency_recovery.vst_used = false;
            }
        }
    }

    pub fn snapshot(&mut self) -> Result<Value, String> {
        self.idle();
        let _pause = SnapshotPause::acquire(&self.tx, Duration::from_secs(5))?;
        let result = (|| {
            let mut states = serde_json::Map::new();
            for (id, slot) in &mut self.ui {
                let bytes = slot
                    .reader
                    .as_mut()
                    .and_then(|r| r.save_state())
                    .ok_or_else(|| format!("Unable to capture plugin state ({id})"))?;
                if bytes.len() > 32 * 1024 * 1024 {
                    return Err("Plugin state exceeds the supported size".into());
                }
                states.insert(
                    id.clone(),
                    Value::String(base64::engine::general_purpose::STANDARD.encode(bytes)),
                );
            }
            Ok(
                json!({ "projectId": self.spec.id, "sessionId": self.session_id, "stateRevision": self.state_revision, "unavailablePlugins": self.unavailable_plugins, "states": states }),
            )
        })();
        result
    }

    pub fn update_mix(&mut self, spec: SessionSpec) -> Result<Value, String> {
        let prepared = prepare_mix_update(spec.clone(), self.sample_rate)?;
        let previous_latency = self.spec.master_limiter.clone().unwrap_or_default().latency(self.sample_rate);
        let next_latency = spec.master_limiter.clone().unwrap_or_default().latency(self.sample_rate);
        let latency_changed = previous_latency != next_latency;
        let pause = if latency_changed {
            Some(SnapshotPause::acquire(&self.tx, Duration::from_secs(5))?)
        } else {
            None
        };
        let (reply, applied) = bounded(1);
        self.send(Control::Mix(spec.clone(), prepared, reply))?;
        // Admit one prepared update at a time. Large effect histories must not
        // accumulate in the command queue behind a slow or blocked processor.
        let result = applied
            .recv_timeout(Duration::from_secs(5))
            .map_err(|_| "Audio processing did not apply the mix update".to_string())
            .and_then(|result| result);
        if let Err(error) = result {
            self.fail(error.clone());
            return Err(error);
        }
        if self
            .spec
            .slots()
            .iter()
            .zip(spec.slots())
            .any(|(old, new)| old.bypassed != new.bypassed)
        {
            self.latency_recovery = LatencyRecovery::default();
        }
        if latency_changed {
            let frame = self.clock.position().min((spec.duration() * f64::from(self.sample_rate)).round() as u64);
            self.transport_playing = self.playback_intent();
            let generation = self.clock.transition(frame, self.transport_playing);
            if let Err(error) = self.send(Control::Seek(frame, generation)) {
                self.fail(error.clone());
                return Err(error);
            }
        }
        self.spec = spec;
        drop(pause);
        Ok(self.status())
    }

    pub fn suppressed_track_ids(&self) -> &[String] {
        &self.suppressed_track_ids
    }

    /// A transient monitor update affects subsequent rendering, without changing project mute
    /// flags or transport. Recording starts a fresh playback epoch after this acknowledgement.
    pub fn monitor(&mut self, ids: Vec<String>) -> Result<Value, String> {
        monitor_mask(&self.spec, &ids)?;
        let (reply, applied) = bounded(1);
        self.send(Control::Monitor(ids.clone(), reply))?;
        let result = applied
            .recv_timeout(Duration::from_secs(5))
            .map_err(|_| "Audio processing did not apply the monitor update".to_string())
            .and_then(|result| result);
        if let Err(error) = result {
            self.fail(error.clone());
            return Err(error);
        }
        self.suppressed_track_ids = ids;
        Ok(self.status())
    }

    pub fn can_update_in_place(&self, spec: &SessionSpec) -> bool {
        // A missing slot must be loaded before it can be enabled. Failed graphs
        // still use the full recovery path so bypassing a fault can restore playback.
        self.error.is_none()
            && !self.clock.recovering.load(Ordering::Acquire)
            && spec
                .slots()
                .iter()
                .all(|slot| slot.bypassed || self.ui.contains_key(&slot.id))
    }

    pub fn start_render(&mut self, path: PathBuf) -> Result<(), String> {
        if !self.offline {
            return Err("Rendering requires an offline session".into());
        }
        self.pause()?;
        self.send(Control::Render(path))?;
        self.rendering = true;
        Ok(())
    }

    pub(crate) fn finish_render(&mut self) {
        self.rendering = false;
    }

    pub fn is_offline(&self) -> bool {
        self.offline
    }

    /// Build on the helper UI thread: plugin setup/state APIs must not run on the DSP worker.
    /// The preview graph and its controllers retain their mode and ownership.
    pub fn offline_copy(&mut self) -> Result<Self, String> {
        self.pause()?;
        let snapshot = self.snapshot()?;
        let mut spec = self.spec.clone();
        for slot in spec.plugin_chain.iter_mut().chain(
            spec.tracks
                .iter_mut()
                .flat_map(|track| &mut track.plugin_chain),
        ) {
            if let Some(state) = snapshot["states"].get(&slot.id).and_then(Value::as_str) {
                slot.state = Some(state.into());
            }
        }
        Self::load_at_rate(spec, self.ffmpeg.clone(), self.sample_rate, true)
    }

    pub fn open_editors(&self) -> Vec<String> {
        self.ui
            .iter()
            .filter(|(_, s)| s.editor.as_ref().is_some_and(|e| e.is_open()))
            .map(|(id, _)| id.clone())
            .collect()
    }

    pub fn has_editor(&self, slot: &str) -> bool {
        self.ui
            .get(slot)
            .is_some_and(|entry| entry.editor.is_some())
    }

    fn playback_intent(&self) -> bool {
        if self.clock.recovering.load(Ordering::Acquire) {
            self.transport_playing
        } else {
            self.clock.is_playing()
        }
    }

    pub fn fail(&mut self, error: String) {
        self.transport_playing = false;
        self.clock.recovering.store(false, Ordering::Release);
        self.clock.transition(self.clock.position(), false);
        self.error = Some(error);
    }

    pub fn recover_latency(
        &mut self,
        change: LatencyChange,
        failed_graph_revision: u64,
    ) -> Result<(), String> {
        if failed_graph_revision < self.graph_revision {
            return Ok(());
        }
        if let Some(error) = &self.error {
            return Err(error.clone());
        }
        let is_au = self
            .ui
            .get(&change.slot)
            .is_some_and(|slot| slot.format == "au");
        if !self.latency_recovery.begin(Instant::now(), is_au) {
            return Err(format!(
                "Plugin latency remained unstable after automatic recovery ({}; previous latency {} samples). Reload the project before retrying",
                change.slot, change.previous
            ));
        }
        // Explicit controls may have arrived after the worker's notification.
        // Their current cursor and intent always take precedence over the old
        // failed block's position or playing state.
        let resume = self.playback_intent();
        let position = self.clock.position();
        self.pause()?;
        let snapshot = self.snapshot()?;
        let mut spec = self.spec.clone();
        for slot in spec.plugin_chain.iter_mut().chain(
            spec.tracks
                .iter_mut()
                .flat_map(|track| &mut track.plugin_chain),
        ) {
            if let Some(state) = snapshot["states"].get(&slot.id).and_then(Value::as_str) {
                slot.state = Some(state.to_string());
            }
        }
        let looping = self.looping;
        let seconds = position as f64 / f64::from(self.sample_rate);
        {
            let _pause = SnapshotPause::acquire(&self.tx, Duration::from_secs(5))?;
            self.refresh_timing()?;
        }
        let (reply, completed) = bounded(1);
        self.send(Control::Renegotiate(reply))?;
        self.graph_revision = completed
            .recv_timeout(Duration::from_secs(5))
            .map_err(|_| "Audio processing did not finish latency negotiation")??;
        self.spec = spec;
        self.session_id = session_id();
        self.state_revision = 0;
        self.clock.recovering.store(false, Ordering::Release);
        if resume {
            self.play(seconds, looping)?;
        } else {
            self.seek(seconds)?;
        }
        self.latency_recovery.complete(Instant::now(), is_au);
        Ok(())
    }

    /// Called only after a capture barrier or while the render worker awaits this ticket.
    fn refresh_timing(&mut self) -> Result<(), String> {
        for (id, slot) in &mut self.ui {
            if let Some(reader) = &mut slot.timing {
                // A getter can publish a fresh restart after returning its previous value.
                // Complete one further cycle while DSP remains stopped; bound noisy plugins.
                for _ in 0..2 {
                    reader.refresh().map_err(|error| format!("{id}: {error}"))?;
                    if !reader.is_pending() {
                        break;
                    }
                }
                if reader.is_pending() {
                    return Err(format!(
                        "Plugin timing remained unstable ({id}); reload the plugin"
                    ));
                }
            }
        }
        Ok(())
    }

    pub(crate) fn service_timing(&mut self, request: TimingRequest) {
        request.service(|| self.refresh_timing());
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        self.clock.stop();
        self.output.take();
        self.cancel.store(true, Ordering::Release);
        // Hold processor ownership until all native views have detached, with rendering already
        // stopped. AU factories/views may access the unit while they are being removed.
        let graph = self.worker.take().and_then(|worker| worker.join().ok());
        for slot in self.ui.values_mut() {
            if let Some(editor) = &mut slot.editor {
                editor.close();
            }
        }
        drop(graph);
        self.ui.clear();
    }
}

#[cfg(test)]
fn render_file(
    graph: &mut Graph,
    path: PathBuf,
    cancel: &AtomicBool,
    refresh: &mut impl FnMut() -> Result<(), String>,
) -> Result<Value, String> {
    render_file_with_progress(graph, path, cancel, refresh, &mut |_| {})
}

fn render_file_with_progress(
    graph: &mut Graph,
    path: PathBuf,
    cancel: &AtomicBool,
    refresh: &mut impl FnMut() -> Result<(), String>,
    progress: &mut impl FnMut(RenderProgress),
) -> Result<Value, String> {
    if !graph.is_offline() {
        return Err("Rendering requires offline plugin configuration".into());
    }
    let suppressed = graph.suppressed_track_ids();
    graph.set_monitor(&[])?;
    let result = render_unmasked_file(graph, path, cancel, refresh, progress);
    let restored = graph.set_monitor(&suppressed);
    graph.reset();
    match (result, restored) {
        (Err(error), _) => Err(error),
        (Ok(_), Err(error)) => Err(error),
        (Ok(value), Ok(())) => Ok(value),
    }
}

fn render_unmasked_file(
    graph: &mut Graph,
    path: PathBuf,
    cancel: &AtomicBool,
    refresh: &mut impl FnMut() -> Result<(), String>,
    progress: &mut impl FnMut(RenderProgress),
) -> Result<Value, String> {
    if !path.is_absolute() || path.exists() {
        return Err("Render output must be a new absolute path".into());
    }
    progress(RenderProgress { phase: "preparing", completed: 0, total: 1 });
    // Apply pending edits before querying control-only latency/tail properties. Rebuild PDC
    // on these same instances, then run the startup probes against the refreshed timings.
    refresh_render_timing(graph, refresh)?;
    let started = Instant::now();
    // Detect parameter/startup latency changes before creating an output file.
    // These trial blocks are reset and never advance the final render.
    let mut trial = 0;
    let mut renegotiated = HashSet::new();
    while trial < 2 {
        loop {
            if cancel.load(Ordering::Acquire) {
                return Err("Audio rendering cancelled".into());
            }
            match graph.render(trial * BLOCK as u64, BLOCK, true) {
                Ok(true) => {
                    trial += 1;
                    break;
                }
                Ok(false) if started.elapsed() < Duration::from_secs(30) => {
                    thread::sleep(Duration::from_millis(2))
                }
                Ok(false) => return Err("Audio decoder timed out during render preparation".into()),
                Err(GraphError::LatencyChanged(change))
                    if renegotiated.insert(change.slot.clone()) =>
                {
                    refresh_render_timing(graph, refresh)?;
                    trial = 0;
                }
                Err(error) => {
                    return Err(format!(
                        "Audio render preparation failed: {error}. Reload the plugin state before exporting"
                    ));
                }
            }
        }
    }
    // Some processors expose their new tail only after processing. Capture it after the
    // successful probes and before reset; querying after reset could discard startup timing.
    refresh_render_timing(graph, refresh)?;
    let handle = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)
        .map_err(|e| e.to_string())?;
    struct IncompleteRender {
        path: PathBuf,
        complete: bool,
    }
    impl Drop for IncompleteRender {
        fn drop(&mut self) {
            if !self.complete {
                let _ = std::fs::remove_file(&self.path);
            }
        }
    }
    let mut cleanup = IncompleteRender {
        path: path.clone(),
        complete: false,
    };
    let spec = hound::WavSpec {
        channels: 2,
        sample_rate: graph.sample_rate,
        bits_per_sample: 32,
        sample_format: hound::SampleFormat::Float,
    };
    let writer =
        hound::WavWriter::new(std::io::BufWriter::new(handle), spec).map_err(|e| e.to_string())?;
    let mut writer = crate::render_tail::TailWriter::new(writer, graph.duration, graph.sample_rate);
    let total = graph.duration.saturating_add(graph.tail);
    if total > (u32::MAX as u64 - 128) / 8 {
        return Err("Rendered audio exceeds WAV file size limit".into());
    }
    let mut input = 0u64;
    let mut rendered = 0u64;
    let mut warmup = graph.latency;
    let mut waiting = Instant::now();
    let mut last_progress = Instant::now();
    let mut reported_frames = 0;
    progress(RenderProgress { phase: if warmup > 0 { "preparing" } else { "rendering" }, completed: 0, total: total.max(1) });
    while rendered < total {
        if cancel.load(Ordering::Acquire) {
            return Err("Audio rendering cancelled".into());
        }
        let frames = BLOCK.min((total - rendered).saturating_add(warmup as u64) as usize);
        if !graph
            .render(input, frames, true)
            .map_err(|error| error.to_string())?
        {
            if waiting.elapsed() > Duration::from_secs(30) {
                return Err("Audio decoder timed out".into());
            }
            thread::sleep(Duration::from_millis(2));
            continue;
        }
        waiting = Instant::now();
        let skip = warmup.min(frames);
        warmup -= skip;
        for i in skip..frames {
            writer.write_frame([graph.mix[0][i], graph.mix[1][i]])?;
            rendered += 1;
        }
        input += frames as u64;
        if rendered > reported_frames && (reported_frames == 0 || last_progress.elapsed() >= Duration::from_millis(100)) {
            progress(RenderProgress { phase: "rendering", completed: rendered, total: total.max(1) });
            reported_frames = rendered;
            last_progress = Instant::now();
        }
    }
    progress(RenderProgress { phase: "finalizing", completed: total, total: total.max(1) });
    let summary = writer.finish()?;
    progress(RenderProgress { phase: "completed", completed: total.max(1), total: total.max(1) });
    cleanup.complete = true;
    Ok(
        json!({ "path": path, "sampleRate": graph.sample_rate, "channels": 2,
        "duration": summary.frames as f64 / f64::from(graph.sample_rate),
        "tailProcessedFrames": rendered.saturating_sub(graph.duration),
        "tailTrimmedFrames": rendered.saturating_sub(summary.frames),
        "tailLimited": graph.tail_limited && summary.audible_at_limit }),
    )
}

fn deliver_render_result(events: &Sender<Event>, result: Result<Value, String>, cancel: &AtomicBool) {
    let mut event = Event::Render(result);
    loop {
        match events.send_timeout(event, Duration::from_millis(100)) {
            Ok(()) | Err(crossbeam_channel::SendTimeoutError::Disconnected(_)) => break,
            Err(crossbeam_channel::SendTimeoutError::Timeout(pending)) => {
                if cancel.load(Ordering::Acquire) { break; }
                event = pending;
            }
        }
    }
}

fn refresh_render_timing(
    graph: &mut Graph,
    refresh: &mut impl FnMut() -> Result<(), String>,
) -> Result<(), String> {
    graph.flush_params()?;
    refresh()?;
    graph.renegotiate_latency()
}

fn process(
    mut graph: Graph,
    commands: Receiver<Control>,
    events: Sender<Event>,
    mut output: Producer<Frame>,
    clock: Arc<Clock>,
    meters: Arc<Vec<Meter>>,
    master_meter: Arc<Meter>,
    cancel: Arc<AtomicBool>,
) -> Graph {
    let mut active = false;
    let mut suspended: Option<Arc<AtomicBool>> = None;
    let mut looping = false;
    let mut generation = 0;
    let mut graph_revision = 0_u64;
    let mut input = 0;
    let mut position = 0;
    let mut warmup = graph.latency;
    let mut waiting = Instant::now();
    while !cancel.load(Ordering::Acquire) {
        for command in commands.try_iter() {
            match command {
                Control::Play(frame, repeat, epoch) => {
                    graph.reset();
                    input = frame;
                    position = frame;
                    generation = epoch;
                    warmup = graph.latency;
                    looping = repeat;
                    active = true;
                    waiting = Instant::now();
                }
                Control::Pause => active = false,
                Control::Seek(frame, epoch) => {
                    graph.reset();
                    input = frame;
                    position = frame;
                    generation = epoch;
                    warmup = graph.latency;
                    waiting = Instant::now();
                    active = clock.is_playing();
                }
                Control::Param(slot, id, normalized, plain) => {
                    if let Err(error) = graph.param(&slot, &id, normalized, plain) {
                        let _ = events.try_send(Event::Failure(error));
                        active = false;
                        clock.transition(clock.position(), false);
                    }
                }
                Control::Params(edits) => {
                    for (slot, id, normalized, plain) in edits {
                        // A preset may change more parameters than one plugin's
                        // bounded point queue holds. Flush without advancing audio
                        // once, then retry; invalid IDs still fail explicitly.
                        let result = graph.param(&slot, &id, normalized, plain).or_else(|_| {
                            graph.flush_params()?;
                            graph.param(&slot, &id, normalized, plain)
                        });
                        if let Err(error) = result {
                            let _ = events.try_send(Event::Failure(error));
                            active = false;
                            clock.transition(clock.position(), false);
                            break;
                        }
                    }
                }
                Control::Mix(spec, prepared, reply) => {
                    let result = graph.update_mix(spec, prepared);
                    if let Err(e) = &result {
                        let _ = events.try_send(Event::Failure(e.clone()));
                        active = false;
                        clock.finish(generation);
                    }
                    let _ = reply.try_send(result);
                }
                Control::Monitor(ids, reply) => {
                    let result = graph.set_monitor(&ids);
                    if result.is_ok() {
                        for (meter, suppressed) in meters.iter().zip(graph.monitor_mask()) {
                            if *suppressed {
                                meter.left.store(0, Ordering::Relaxed);
                                meter.right.store(0, Ordering::Relaxed);
                            }
                        }
                    }
                    let _ = reply.try_send(result);
                }
                Control::Barrier(reply, hold) => {
                    suspended = Some(hold);
                    let _ = reply.try_send(graph.flush_params());
                }
                Control::Renegotiate(reply) => {
                    active = false;
                    suspended = None;
                    let result = graph.renegotiate_latency().and_then(|()| {
                        graph_revision = graph_revision
                            .checked_add(1)
                            .ok_or("Audio graph revision exhausted")?;
                        Ok(graph_revision)
                    });
                    let _ = reply.try_send(result);
                }
                Control::Render(path) => {
                    active = false;
                    let result = render_file_with_progress(&mut graph, path, &cancel, &mut || {
                        request_timing_refresh(&events, &cancel, Duration::from_secs(5))
                    }, &mut |progress| { let _ = events.try_send(Event::RenderProgress(progress)); });
                    deliver_render_result(&events, result, &cancel);
                }
            }
        }
        if suspended
            .as_ref()
            .is_some_and(|hold| hold.load(Ordering::Acquire))
            || !active
            || output.slots() < BLOCK
        {
            thread::sleep(Duration::from_millis(1));
            continue;
        }
        let end = graph
            .duration
            .saturating_add(if looping { 0 } else { graph.tail });
        if position >= end {
            if looping && graph.duration > 0 {
                graph.reset();
                input = 0;
                position = 0;
                warmup = graph.latency;
            } else {
                active = false;
                clock.finish(generation);
                continue;
            }
        }
        let frames = BLOCK.min((end - position).saturating_add(warmup as u64) as usize);
        match graph.render(input, frames, true) {
            Ok(false) => {
                if waiting.elapsed() > Duration::from_secs(30) {
                    let _ = events.try_send(Event::Failure("Audio decoder timed out".into()));
                    active = false;
                    clock.finish(generation);
                }
                thread::sleep(Duration::from_millis(2));
                continue;
            }
            Err(GraphError::LatencyChanged(change)) => {
                // The callback compares epochs before output and cursor commits.
                // Invalidate every old queued frame before the UI rebuild starts.
                clock.begin_recovery(generation);
                let _ = events.try_send(Event::LatencyChanged {
                    change,
                    graph_revision,
                });
                active = false;
                continue;
            }
            Err(GraphError::Failure(error)) => {
                let _ = events.try_send(Event::Failure(error));
                active = false;
                clock.transition(clock.position(), false);
                continue;
            }
            Ok(true) => {}
        }
        waiting = Instant::now();
        let skip = warmup.min(frames);
        warmup -= skip;
        for i in skip..frames {
            position += 1;
            let last = !looping && position >= end;
            let _ = output.push(Frame {
                samples: [graph.mix[0][i], graph.mix[1][i]],
                position: position.min(graph.duration),
                generation,
                last,
            });
        }
        if warmup == 0 && (output.slots() <= 4096 - 1024 || (!looping && position >= end)) {
            clock.prefilled(generation);
        }
        for (meter, level) in meters.iter().zip(&graph.levels) {
            meter.left.store(level[0].to_bits(), Ordering::Relaxed);
            meter.right.store(level[1].to_bits(), Ordering::Relaxed);
        }
        master_meter.left.store(
            graph.mix[0][..frames]
                .iter()
                .map(|v| v.abs())
                .fold(0.0f32, f32::max)
                .to_bits(),
            Ordering::Relaxed,
        );
        master_meter.right.store(
            graph.mix[1][..frames]
                .iter()
                .map(|v| v.abs())
                .fold(0.0f32, f32::max)
                .to_bits(),
            Ordering::Relaxed,
        );
        input += frames as u64;
        // The callback owns completion; queued final frames must remain audible.
        if !looping && position >= end {
            active = false;
        }
    }
    graph
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn expired_timing_tickets_never_call_back_into_a_resumed_processor() {
        let (events, received) = bounded(1);
        assert!(
            request_timing_refresh(&events, &Arc::new(AtomicBool::new(false)), Duration::ZERO)
                .unwrap_err()
                .contains("timed out")
        );
        let Event::Timing(request) = received.recv().unwrap() else {
            panic!("Expected one timing request");
        };
        request.service(|| panic!("An expired ticket must not query the plugin"));
        // An already cancelled worker does not publish another ticket.
        assert!(
            request_timing_refresh(
                &events,
                &Arc::new(AtomicBool::new(true)),
                Duration::from_secs(5)
            )
            .unwrap_err()
            .contains("cancelled")
        );
        assert!(received.is_empty());
        events
            .send(Event::Failure("earlier failure".into()))
            .unwrap();
        assert!(
            request_timing_refresh(
                &events,
                &Arc::new(AtomicBool::new(false)),
                Duration::from_secs(5)
            )
            .unwrap_err()
            .contains("queue is unavailable")
        );
    }

    #[test]
    fn timing_handoff_propagates_native_errors_and_abandoned_responses() {
        for respond in [true, false] {
            let (events, received) = bounded(1);
            let worker = thread::spawn(move || {
                request_timing_refresh(
                    &events,
                    &Arc::new(AtomicBool::new(false)),
                    Duration::from_secs(1),
                )
            });
            let Event::Timing(request) = received.recv_timeout(Duration::from_secs(1)).unwrap()
            else {
                panic!("Expected one timing request");
            };
            if respond {
                request.service(|| Err("effect: native activation failed".into()));
            } else {
                drop(request);
            }
            let error = worker.join().unwrap().unwrap_err();
            assert!(error.contains(if respond {
                "native activation failed"
            } else {
                "response is unavailable"
            }));
        }
    }

    #[test]
    fn timing_service_checks_deadline_and_cancellation_before_worker_invalidation() {
        for cancelled in [false, true] {
            let active = Arc::new(Mutex::new(true));
            let (reply, completed) = bounded(1);
            let request = TimingRequest {
                active: Arc::clone(&active),
                cancel: Arc::new(AtomicBool::new(cancelled)),
                deadline: Instant::now()
                    + if cancelled {
                        Duration::from_secs(5)
                    } else {
                        Duration::ZERO
                    },
                reply,
            };
            request.service(|| panic!("The ticket must expire without entering the SDK"));
            let error = completed.recv().unwrap().unwrap_err();
            assert!(error.contains(if cancelled { "cancelled" } else { "timed out" }));
            assert!(!*active.lock().unwrap());
        }
    }

    #[test]
    fn timing_handoff_cancels_promptly_and_invalidates_a_queued_ui_request() {
        let (events, received) = bounded(1);
        let cancel = Arc::new(AtomicBool::new(false));
        let stop = Arc::clone(&cancel);
        let worker =
            thread::spawn(move || request_timing_refresh(&events, &stop, Duration::from_secs(30)));
        let Event::Timing(request) = received.recv_timeout(Duration::from_secs(1)).unwrap() else {
            panic!("Expected one timing request");
        };
        let started = Instant::now();
        cancel.store(true, Ordering::Release);
        assert!(worker.join().unwrap().unwrap_err().contains("cancelled"));
        assert!(started.elapsed() < Duration::from_secs(1));
        request.service(|| panic!("A cancelled ticket must not query the plugin"));
    }

    #[test]
    fn timing_cancellation_cannot_resume_dsp_until_an_inflight_ui_query_finishes() {
        let (events, received) = bounded(1);
        let (result, completed) = bounded(1);
        let cancel = Arc::new(AtomicBool::new(false));
        let stop = Arc::clone(&cancel);
        let worker = thread::spawn(move || {
            let outcome = request_timing_refresh(&events, &stop, Duration::from_secs(30));
            result.send(outcome).unwrap();
        });
        let Event::Timing(request) = received.recv_timeout(Duration::from_secs(1)).unwrap() else {
            panic!("Expected one timing request");
        };
        let (entered, reading) = bounded(1);
        let (release, released) = bounded(1);
        let ui = thread::spawn(move || {
            request.service(|| {
                entered.send(()).unwrap();
                released.recv_timeout(Duration::from_secs(2)).unwrap();
                Ok(())
            });
        });
        reading.recv_timeout(Duration::from_secs(1)).unwrap();
        cancel.store(true, Ordering::Release);
        thread::sleep(Duration::from_millis(80));
        assert!(
            completed.try_recv().is_err(),
            "DSP must remain stopped during the query"
        );
        release.send(()).unwrap();
        ui.join().unwrap();
        assert!(
            completed
                .recv_timeout(Duration::from_secs(1))
                .unwrap()
                .unwrap_err()
                .contains("cancelled")
        );
        worker.join().unwrap();
    }

    #[test]
    fn final_render_result_survives_progress_backpressure_and_cancellation_unblocks_it() {
        let (tx, rx) = bounded(1);
        assert!(tx.send(Event::RenderProgress(RenderProgress { phase: "rendering", completed: 1, total: 10 })).is_ok());
        let worker = thread::spawn(move || deliver_render_result(&tx, Ok(json!({"done":true})), &AtomicBool::new(false)));
        assert!(matches!(rx.recv_timeout(Duration::from_secs(1)).unwrap(), Event::RenderProgress(_)));
        let Event::Render(result) = rx.recv_timeout(Duration::from_secs(1)).unwrap() else { panic!("missing terminal result") };
        assert_eq!(result.unwrap()["done"], true);
        worker.join().unwrap();
        let (tx, _rx) = bounded(1);
        assert!(tx.send(Event::RenderProgress(RenderProgress { phase: "rendering", completed: 1, total: 10 })).is_ok());
        deliver_render_result(&tx, Err("cancelled".into()), &AtomicBool::new(true));
    }

    #[test]
    fn monitor_acknowledgement_commits_only_applied_transient_state() {
        let spec: SessionSpec =
            serde_json::from_value(json!({"id":"monitor", "name":"Audio", "sources":[],
            "tracks":[{"id":"voice","clips":[]},{"id":"backing","clips":[]}]}))
            .unwrap();
        let saved = serde_json::to_value(&spec).unwrap();
        let mut session = Session::load(spec, "ffmpeg".into()).unwrap();
        assert!(session.monitor(vec!["missing".into()]).is_err());
        assert!(session.suppressed_track_ids().is_empty());
        assert!(session.error.is_none());
        let before_revision = session.state_revision;
        let status = session.monitor(vec!["voice".into()]).unwrap();
        assert_eq!(status["suppressedTrackIds"], json!(["voice"]));
        assert_eq!(session.state_revision, before_revision);
        assert_eq!(serde_json::to_value(&session.spec).unwrap(), saved);
        let (commands, receives) = bounded(1);
        session.tx = commands;
        let rejected = thread::spawn(move || match receives.recv().unwrap() {
            Control::Monitor(_, reply) => reply
                .send(Err("Monitor update was rejected".into()))
                .unwrap(),
            _ => panic!("Expected one monitor command"),
        });
        assert!(session.monitor(vec!["backing".into()]).is_err());
        rejected.join().unwrap();
        assert_eq!(session.suppressed_track_ids(), ["voice"]);
        assert_eq!(serde_json::to_value(&session.spec).unwrap(), saved);
    }

    #[test]
    fn realtime_graph_cannot_render_or_mutate_its_preview_mask() {
        let spec: SessionSpec = serde_json::from_value(json!({"id":"preview","name":"Audio",
            "sources":[],"tracks":[{"id":"voice","clips":[]}]}))
        .unwrap();
        let (mut graph, _) = Graph::load(spec, "ffmpeg".into(), 8000).unwrap();
        graph.set_monitor(&["voice".into()]).unwrap();
        let directory = tempfile::tempdir().unwrap();
        let output = directory.path().join("preview.wav");
        let error = render_file(
            &mut graph,
            output.clone(),
            &AtomicBool::new(false),
            &mut || Ok(()),
        )
        .unwrap_err();
        assert!(error.contains("offline plugin configuration"));
        assert!(!output.exists());
        assert_eq!(graph.suppressed_track_ids(), ["voice"]);
    }

    #[test]
    fn cancelled_or_invalid_offline_export_restores_preview_monitor() {
        let spec: SessionSpec =
            serde_json::from_value(json!({"id":"monitor", "name":"Audio", "sources":[],
            "tracks":[{"id":"voice","clips":[]},{"id":"backing","clips":[]}]}))
            .unwrap();
        let saved = serde_json::to_value(&spec).unwrap();
        let (mut graph, _) = Graph::load_mode(spec, "ffmpeg".into(), 8000, true).unwrap();
        graph.set_monitor(&["voice".into()]).unwrap();
        let cancelled = Arc::new(AtomicBool::new(true));
        let directory = tempfile::tempdir().unwrap();
        let output = directory.path().join("cancelled.wav");
        assert!(
            render_file(&mut graph, output.clone(), &cancelled, &mut || Ok(()))
                .unwrap_err()
                .contains("cancelled")
        );
        assert!(!output.exists());
        assert_eq!(graph.suppressed_track_ids(), ["voice"]);
        assert!(
            render_file(
                &mut graph,
                "relative.wav".into(),
                &cancelled,
                &mut || Ok(())
            )
            .is_err()
        );
        assert_eq!(graph.suppressed_track_ids(), ["voice"]);
        assert_eq!(serde_json::to_value(&graph.spec).unwrap(), saved);
    }

    struct ParameterController;
    impl soundcraft_dsp::PluginEditor for ParameterController {
        fn open(&mut self) -> Result<(), String> {
            Err("Native editor is unavailable".into())
        }
        fn close(&mut self) {}
        fn is_open(&self) -> bool {
            false
        }
        fn idle(&mut self) -> Vec<(String, f32)> {
            Vec::new()
        }
        fn normalize(&self, id: &str, value: f32) -> Option<f64> {
            (id == "gain" && value.is_finite()).then_some(f64::from(value.clamp(0.0, 2.0)) / 2.0)
        }
    }

    struct ClosingController {
        stopped: Arc<AtomicBool>,
        closed: Arc<AtomicBool>,
    }

    struct FinalValueController {
        closed: bool,
        delivered: bool,
    }
    impl soundcraft_dsp::PluginEditor for FinalValueController {
        fn open(&mut self) -> Result<(), String> {
            Ok(())
        }
        fn close(&mut self) {
            self.closed = true;
        }
        fn is_open(&self) -> bool {
            false
        }
        fn idle(&mut self) -> Vec<(String, f32)> {
            if self.closed && !self.delivered {
                self.delivered = true;
                vec![("gain".into(), 2.0)]
            } else {
                Vec::new()
            }
        }
        fn normalize(&self, id: &str, value: f32) -> Option<f64> {
            (id == "gain").then_some(f64::from(value) / 2.0)
        }
    }

    #[test]
    fn user_closed_au_views_deliver_their_final_parameter_values_in_the_same_idle() {
        use soundcraft_dsp::{Category, PluginInfo};
        static INFO: PluginInfo = PluginInfo {
            id: "effect",
            name: "Effect",
            short_name: "Effect",
            category: Category::Other,
            is_instrument: false,
            audiosuite: true,
            params: &[],
        };
        let spec: SessionSpec = serde_json::from_value(
            json!({"id":"closed", "name":"Audio", "sources":[], "tracks":[]}),
        )
        .unwrap();
        let mut session = Session::load(spec, PathBuf::from("ffmpeg")).unwrap();
        session.ui.insert(
            "slot".into(),
            UiSlot {
                format: "au".into(),
                info: &INFO,
                editor: Some(Box::new(FinalValueController {
                    closed: false,
                    delivered: false,
                })),
                reader: None,
                timing: None,
                values: HashMap::from([("gain".into(), 0.5)]),
                plain_values: HashMap::from([("gain".into(), 1.0)]),
                was_open: true,
            },
        );
        let before = session.state_revision;
        session.idle();
        assert_eq!(session.parameters("slot").unwrap()["values"]["gain"], 2.0);
        assert_eq!(session.ui["slot"].values["gain"], 1.0);
        assert_eq!(session.state_revision, before + 2);
        session.idle();
        assert_eq!(session.state_revision, before + 2);
    }

    #[test]
    fn exporting_defers_au_close_barriers_until_timing_tickets_have_finished() {
        use soundcraft_dsp::{Category, PluginInfo};
        static INFO: PluginInfo = PluginInfo {
            id: "effect",
            name: "Effect",
            short_name: "Effect",
            category: Category::Other,
            is_instrument: false,
            audiosuite: true,
            params: &[],
        };
        let spec: SessionSpec = serde_json::from_value(json!({"id":"export-close","name":"Audio",
            "sources":[],"tracks":[]}))
        .unwrap();
        let mut session = Session::load_at_rate(spec, "ffmpeg".into(), 8000, true).unwrap();
        session.ui.insert(
            "slot".into(),
            UiSlot {
                format: "au".into(),
                info: &INFO,
                editor: Some(Box::new(FinalValueController {
                    closed: false,
                    delivered: false,
                })),
                reader: None,
                timing: None,
                values: HashMap::from([("gain".into(), 0.5)]),
                plain_values: HashMap::from([("gain".into(), 1.0)]),
                was_open: true,
            },
        );
        let directory = tempfile::tempdir().unwrap();
        let output = directory.path().join("export.wav");
        session.start_render(output.clone()).unwrap();
        let mut queries = 0;
        loop {
            match session.events.recv_timeout(Duration::from_secs(1)).unwrap() {
                Event::Timing(request) => {
                    let started = Instant::now();
                    session.idle();
                    assert!(started.elapsed() < Duration::from_secs(1));
                    assert_eq!(session.ui["slot"].plain_values["gain"], 1.0);
                    session.service_timing(request);
                    queries += 1;
                }
                Event::Render(result) => {
                    result.unwrap();
                    session.finish_render();
                    break;
                }
                Event::RenderProgress(_) => {},
                _ => panic!("Unexpected export event"),
            }
        }
        assert_eq!(
            queries, 2,
            "Both preflight and post-probe replies must complete"
        );
        assert!(output.is_file());
        session.idle();
        assert_eq!(session.ui["slot"].plain_values["gain"], 2.0);
    }

    impl soundcraft_dsp::PluginEditor for ClosingController {
        fn open(&mut self) -> Result<(), String> {
            Ok(())
        }
        fn close(&mut self) {
            assert!(
                self.stopped.load(Ordering::Acquire),
                "view teardown must follow DSP shutdown"
            );
            self.closed.store(true, Ordering::Release);
        }
        fn is_open(&self) -> bool {
            true
        }
        fn idle(&mut self) -> Vec<(String, f32)> {
            Vec::new()
        }
    }

    #[test]
    fn session_drop_stops_rendering_before_detaching_native_views() {
        use soundcraft_dsp::{Category, PluginInfo};
        static INFO: PluginInfo = PluginInfo {
            id: "effect",
            name: "Effect",
            short_name: "Effect",
            category: Category::Other,
            is_instrument: false,
            audiosuite: true,
            params: &[],
        };
        let spec: SessionSpec = serde_json::from_value(
            json!({"id":"shutdown", "name":"Audio", "sources":[], "tracks":[]}),
        )
        .unwrap();
        let mut session = Session::load(spec, PathBuf::from("ffmpeg")).unwrap();
        let worker = session.worker.take().unwrap();
        let stopped = Arc::new(AtomicBool::new(false));
        let closed = Arc::new(AtomicBool::new(false));
        let returned = Arc::clone(&stopped);
        session.worker = Some(thread::spawn(move || {
            let graph = worker.join().unwrap();
            returned.store(true, Ordering::Release);
            graph
        }));
        session.ui.insert(
            "slot".into(),
            UiSlot {
                format: "au".into(),
                info: &INFO,
                editor: Some(Box::new(ClosingController {
                    stopped,
                    closed: Arc::clone(&closed),
                })),
                reader: None,
                timing: None,
                values: HashMap::new(),
                plain_values: HashMap::new(),
                was_open: true,
            },
        );
        drop(session);
        assert!(closed.load(Ordering::Acquire));
    }

    #[test]
    fn generic_parameters_report_plain_values_and_do_not_apply_failed_queue_writes() {
        use soundcraft_dsp::{Category, ParamInfo, PluginInfo, Taper, Unit};
        static INFO: PluginInfo = PluginInfo {
            id: "effect",
            name: "Effect",
            short_name: "Effect",
            category: Category::Other,
            is_instrument: false,
            audiosuite: true,
            params: &[ParamInfo {
                id: "gain",
                name: "Gain",
                min: 0.0,
                max: 2.0,
                default: 1.0,
                unit: Unit::None,
                taper: Taper::Linear,
                choices: &[],
            }],
        };
        let spec: SessionSpec = serde_json::from_value(
            json!({"id":"params", "name":"Audio", "sources":[], "tracks":[]}),
        )
        .unwrap();
        let mut session = Session::load(spec, PathBuf::from("ffmpeg")).unwrap();
        let (tx, controls) = bounded(1);
        session.tx = tx;
        session.ui.insert(
            "slot".into(),
            UiSlot {
                format: "vst2".into(),
                info: &INFO,
                editor: Some(Box::new(ParameterController)),
                reader: None,
                timing: None,
                values: HashMap::from([("gain".into(), 0.5)]),
                plain_values: HashMap::from([("gain".into(), 1.0)]),
                was_open: false,
            },
        );
        let response = session.param("slot", "gain", 8.0).unwrap();
        assert_eq!(response["value"], 2.0);
        assert_eq!(session.parameters("slot").unwrap()["values"]["gain"], 2.0);
        let revision = session.state_revision;
        assert!(
            session
                .param("slot", "gain", 0.0)
                .unwrap_err()
                .contains("queue")
        );
        assert!(session.param("slot", "gain", f32::NAN).is_err());
        assert!(session.param("slot", "missing", 1.0).is_err());
        assert_eq!(session.state_revision, revision);
        assert_eq!(session.parameters("slot").unwrap()["values"]["gain"], 2.0);
        let Control::Param(slot, parameter, normalized, plain) = controls.recv().unwrap() else {
            panic!("expected parameter change")
        };
        assert_eq!(
            (slot.as_str(), parameter.as_str(), normalized, plain),
            ("slot", "gain", 1.0, 2.0)
        );
    }

    #[test]
    fn timed_out_capture_releases_a_late_barrier_even_when_the_command_queue_is_full() {
        let (commands, received) = bounded(1);
        let result = SnapshotPause::acquire(&commands, Duration::ZERO);
        assert!(result.is_err());
        // No worker drained the queue before timeout; a Resume command would
        // have failed to enqueue. The already-queued barrier must be released.
        let Control::Barrier(reply, hold) = received.recv().unwrap() else {
            panic!("expected capture barrier")
        };
        assert!(!hold.load(Ordering::Acquire));
        assert!(reply.try_send(Ok(())).is_err());
        assert!(received.is_empty());
    }

    #[test]
    fn capture_pause_is_held_only_until_capture_completes_or_flush_fails() {
        let (commands, received) = bounded(1);
        let worker = std::thread::spawn(move || {
            let Control::Barrier(reply, hold) = received.recv().unwrap() else {
                panic!("expected capture barrier")
            };
            assert!(hold.load(Ordering::Acquire));
            reply.send(Ok(())).unwrap();
            hold
        });
        let capture = SnapshotPause::acquire(&commands, Duration::from_secs(1)).unwrap();
        let hold = worker.join().unwrap();
        assert!(hold.load(Ordering::Acquire));
        drop(capture);
        assert!(!hold.load(Ordering::Acquire));

        let (commands, received) = bounded(1);
        let worker = std::thread::spawn(move || {
            let Control::Barrier(reply, hold) = received.recv().unwrap() else {
                panic!("expected capture barrier")
            };
            reply
                .send(Err("processor rejected parameter flush".into()))
                .unwrap();
            hold
        });
        let result = SnapshotPause::acquire(&commands, Duration::from_secs(1));
        assert!(matches!(result, Err(error) if error == "processor rejected parameter flush"));
        assert!(!worker.join().unwrap().load(Ordering::Acquire));
    }

    #[test]
    fn au_latency_recovery_accepts_staged_changes_and_bounds_unstable_bursts() {
        let start = Instant::now();
        let mut recovery = LatencyRecovery::default();
        for index in 0..4 {
            let now = start + Duration::from_millis(index * 100);
            assert!(recovery.begin(now, true));
            recovery.complete(now, true);
        }
        assert!(!recovery.begin(start + Duration::from_millis(400), true));
        assert!(recovery.begin(start + Duration::from_secs(3), true));
    }

    #[test]
    fn native_parameter_callbacks_cannot_renew_an_au_latency_recovery_burst() {
        let spec = serde_json::from_value(json!({
            "id": "latency-budget", "name": "Audio", "sources": [], "tracks": []
        })).unwrap();
        let mut session = Session::load_at_rate(spec, "ffmpeg".into(), 48000, true).unwrap();
        let (commands, received) = bounded(1);
        session.tx = commands;
        let start = Instant::now();
        assert!(session.latency_recovery.begin(start, false));
        for index in 0..4 {
            let now = start + Duration::from_millis(index * 100);
            assert!(session.latency_recovery.begin(now, true));
            session.latency_recovery.complete(now, true);
            session.apply_editor_edits(vec![("slot".into(), "gain".into(), 0.5, 0.0)]);
            assert!(matches!(received.try_recv(), Ok(Control::Params(_))));
        }
        assert!(!session.latency_recovery.begin(start + Duration::from_millis(400), true));
        assert!(session.latency_recovery.begin(start + Duration::from_millis(400), false));
    }

    #[test]
    fn au_recovery_does_not_spend_the_vst_recovery_attempt() {
        let start = Instant::now();
        let mut recovery = LatencyRecovery::default();
        assert!(recovery.begin(start, true));
        recovery.complete(start, true);
        assert!(recovery.begin(start, false));
        recovery.complete(start, false);
        assert!(!recovery.begin(start + Duration::from_secs(3), false));
    }

    #[test]
    fn pending_recovery_preserves_later_explicit_pause_and_seek_without_a_device_stream() {
        let spec: SessionSpec = serde_json::from_value(json!({"id":"transport", "name":"Audio",
            "sources":[{"id":"offline", "path":"", "duration":1}],
            "tracks":[{"id":"muted", "sourceId":"offline", "muted":true}]}))
        .unwrap();
        let mut session = Session::load(spec, PathBuf::from("ffmpeg")).unwrap();
        session.looping = true;
        session.transport_playing = true;
        let failed = session.clock.transition(100, true);
        session.clock.begin_recovery(failed);
        assert_eq!(session.status()["playing"], true);
        assert_eq!(session.status()["recovering"], true);
        // These commands occur after DSP failure, before the main thread
        // handles its notification. Recovery must use this latest intent.
        session.pause().unwrap();
        session.seek(0.75).unwrap();
        let previous = session.session_id.clone();
        session
            .recover_latency(
                LatencyChange {
                    slot: "processor".into(),
                    previous: 3,
                },
                0,
            )
            .unwrap();
        let status = session.status();
        assert_eq!(status["position"], 0.75);
        assert_eq!(status["playing"], false);
        assert_eq!(status["loop"], true);
        assert_eq!(status["recovering"], false);
        assert_ne!(session.session_id, previous);
        let repaired = session.session_id.clone();
        // A second notification queued by that same old graph must not spend
        // another recovery budget or overwrite the newer paused seek.
        session
            .recover_latency(
                LatencyChange {
                    slot: "processor".into(),
                    previous: 3,
                },
                0,
            )
            .unwrap();
        assert_eq!(session.session_id, repaired);
        assert_eq!(session.status()["position"], 0.75);
        assert_eq!(session.status()["playing"], false);
    }

    #[test]
    fn failed_recovering_or_unavailable_graphs_cannot_skip_the_reload_path() {
        let spec: SessionSpec = serde_json::from_value(json!({"id":"reuse", "name":"Audio",
            "sources":[], "tracks":[], "pluginChain":[{"id":"missing", "format":"aax",
                "pluginId":"aax:opaque", "name":"Effect", "path":"missing.aaxplugin", "bypassed":true}]})).unwrap();
        let mut session = Session::load(spec.clone(), PathBuf::from("ffmpeg")).unwrap();
        assert!(session.can_update_in_place(&spec));
        let mut enabled = spec.clone();
        enabled.plugin_chain[0].bypassed = false;
        assert!(!session.can_update_in_place(&enabled));
        session.clock.recovering.store(true, Ordering::Release);
        assert!(!session.can_update_in_place(&spec));
        session.clock.recovering.store(false, Ordering::Release);
        session.fail("Plugin processing failed".into());
        assert!(!session.can_update_in_place(&spec));
    }

    #[test]
    fn limiter_mix_updates_reuse_the_session_and_preserve_paused_transport_position() {
        let mut spec: SessionSpec = serde_json::from_value(json!({"id":"limiter", "name":"Audio",
            "sources":[{"id":"offline", "path":"", "duration":1}],
            "tracks":[{"id":"muted", "sourceId":"offline", "muted":true}]})).unwrap();
        let mut session = Session::load(spec.clone(), "ffmpeg".into()).unwrap();
        session.seek(0.5).unwrap();
        let id = session.session_id.clone();
        for (enabled, attack_ms) in [(true, 3.0), (true, 100.0), (false, 100.0)] {
            spec.master_limiter = Some(crate::limiter::MasterLimiterSpec { enabled, attack_ms, ..Default::default() });
            let status = session.update_mix(spec.clone()).unwrap();
            assert_eq!(status["position"], 0.5);
            assert_eq!(status["playing"], false);
            assert_eq!(session.session_id, id);
            assert!(session.error.is_none());
        }
    }

    #[test]
    fn rejected_worker_mix_does_not_commit_a_different_session_spec() {
        let spec: SessionSpec =
            serde_json::from_value(json!({"id":"ack", "name":"Audio", "sources":[], "tracks":[]}))
                .unwrap();
        let mut session = Session::load(spec.clone(), "ffmpeg".into()).unwrap();
        let next: SessionSpec = serde_json::from_value(json!({"id":"ack", "name":"Audio",
            "sources":[{"id":"unused", "path":"", "duration":0}], "tracks":[]}))
        .unwrap();
        let error = session.update_mix(next).unwrap_err();
        assert!(error.contains("reload"));
        assert!(session.spec.sources.is_empty());
        assert!(session.error.is_some());
    }
}
