use crate::plugins::HostedPlugin;
use crate::timeline::{SessionSpec, Timeline, monitor_mask};
use base64::Engine;
use soundcraft_dsp::{PluginEditor, PluginStateReader, Transport};
use std::collections::HashMap;
use std::path::PathBuf;

pub(crate) const BLOCK: usize = 256;
const MAX_LATENCY: usize = 192000 * 10;

#[derive(Clone, Debug)]
pub(crate) struct LatencyChange {
    pub slot: String,
    pub previous: usize,
}

#[derive(Debug)]
pub(crate) enum GraphError {
    Failure(String),
    LatencyChanged(LatencyChange),
}

impl From<String> for GraphError {
    fn from(error: String) -> Self {
        Self::Failure(error)
    }
}

impl std::fmt::Display for GraphError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Failure(error) => f.write_str(error),
            Self::LatencyChanged(change) => write!(
                f,
                "Plugin latency changed ({}; previous latency {} samples)",
                change.slot, change.previous
            ),
        }
    }
}

#[derive(Clone, Debug, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct UnavailablePlugin {
    pub slot_id: String,
    pub message: String,
}

pub(crate) struct UiSlot {
    pub format: String,
    pub info: &'static soundcraft_dsp::PluginInfo,
    pub editor: Option<Box<dyn PluginEditor>>,
    pub reader: Option<Box<dyn PluginStateReader>>,
    pub timing: Option<soundcraft_vst3_host::Vst3TimingReader>,
    pub values: HashMap<String, f64>,
    pub plain_values: HashMap<String, f32>,
    pub was_open: bool,
}

pub(crate) struct Delay {
    samples: Vec<[f32; 2]>,
    cursor: usize,
}

impl Delay {
    pub fn new(frames: usize) -> Result<Self, String> {
        if frames > MAX_LATENCY {
            return Err("Plugin latency exceeds the supported limit".into());
        }
        Ok(Self {
            samples: vec![[0.0; 2]; frames],
            cursor: 0,
        })
    }
    pub fn reset(&mut self) {
        self.samples.fill([0.0; 2]);
        self.cursor = 0;
    }
    pub fn process(&mut self, data: &mut [Vec<f32>], frames: usize) {
        if self.samples.is_empty() {
            return;
        }
        for i in 0..frames {
            let incoming = [data[0][i], data[1][i]];
            let old = std::mem::replace(&mut self.samples[self.cursor], incoming);
            data[0][i] = old[0];
            data[1][i] = old[1];
            self.cursor = (self.cursor + 1) % self.samples.len();
        }
    }
}

struct Slot {
    id: String,
    plugin: HostedPlugin,
    bypassed: bool,
    latency: usize,
    bypass: Delay,
    dry: Vec<Vec<f32>>,
    warmup: usize,
}

fn total_latency(values: impl Iterator<Item = usize>) -> usize {
    values.fold(0, usize::saturating_add)
}

fn tail_length(chains: &[Vec<Slot>], master: &[Slot], spec: &SessionSpec) -> u64 {
    let chain_tail = |chain: &[Slot]| {
        chain
            .iter()
            .filter(|slot| !slot.bypassed)
            .map(|slot| slot.plugin.tail_samples() as u64)
            .fold(0u64, u64::saturating_add)
    };
    let solo = spec.tracks.iter().any(|track| track.solo);
    chains
        .iter()
        .zip(&spec.tracks)
        .filter(|(_, track)| !track.muted && (!solo || track.solo))
        .map(|(chain, _)| chain_tail(chain))
        .max()
        .unwrap_or(0)
        .saturating_add(chain_tail(master))
}

impl Slot {
    fn set_bypassed(&mut self, bypassed: bool) {
        if self.bypassed && !bypassed {
            // Processing is suspended during host bypass. Clear frozen wet history
            // before resuming, retaining this processor, its parameters and editor.
            self.plugin.reset();
            self.warmup = self.latency;
        }
        self.bypassed = bypassed;
    }

    fn check_latency(&mut self) -> Result<(), GraphError> {
        let current = self.plugin.refresh_latency();
        if current != self.latency {
            return Err(GraphError::LatencyChanged(LatencyChange {
                slot: self.id.clone(),
                previous: self.latency,
            }));
        }
        Ok(())
    }

    fn process(
        &mut self,
        io: &mut [Vec<f32>],
        frames: usize,
        transport: &Transport,
    ) -> Result<(), GraphError> {
        self.check_latency()?;
        for (dry, input) in self.dry.iter_mut().zip(io.iter()) {
            dry[..frames].copy_from_slice(&input[..frames]);
        }
        // Keep the compensated dry path warm while wet processing is active.
        self.bypass.process(&mut self.dry, frames);
        if self.bypassed {
            for (output, dry) in io.iter_mut().zip(&self.dry) {
                output[..frames].copy_from_slice(&dry[..frames]);
            }
            return Ok(());
        }
        self.plugin.set_transport(transport);
        self.plugin
            .try_process(io, frames)
            .map_err(|e| format!("{}: {e}", self.id))?;
        self.check_latency()?;
        // A resumed processor needs its reported latency to refill. Use aligned
        // dry samples during that prefix rather than outputting its cold buffer.
        let warmup = self.warmup.min(frames);
        self.warmup -= warmup;
        for (output, dry) in io.iter_mut().zip(&self.dry) {
            output[..warmup].copy_from_slice(&dry[..warmup]);
        }
        if io
            .iter()
            .take(2)
            .any(|c| c[..frames].iter().any(|v| !v.is_finite()))
        {
            return Err(GraphError::Failure(format!(
                "Plugin produced invalid audio ({})",
                self.id
            )));
        }
        Ok(())
    }
}

pub(crate) struct Graph {
    pub spec: SessionSpec,
    pub sample_rate: u32,
    offline: bool,
    pub duration: u64,
    pub latency: usize,
    pub tail: u64,
    pub tail_limited: bool,
    pub unavailable_plugins: Vec<UnavailablePlugin>,
    timeline: Timeline,
    tracks: Vec<Vec<Vec<f32>>>,
    chains: Vec<Vec<Slot>>,
    suppressed: Vec<bool>,
    master: Vec<Slot>,
    limiter: crate::limiter::MasterLimiter,
    delays: Vec<Delay>,
    pub mix: Vec<Vec<f32>>,
    pub levels: Vec<[f32; 2]>,
}

fn chain(
    slots: &[crate::timeline::PluginSlot],
    rate: u32,
    offline: bool,
    ui: &mut HashMap<String, UiSlot>,
    unavailable: &mut Vec<UnavailablePlugin>,
) -> Result<Vec<Slot>, String> {
    let mut result = Vec::new();
    for slot in slots {
        // Live bypass retains controllers for editing. A fresh offline graph has
        // no reason to load disabled processors or compensate their latency.
        if offline && slot.bypassed {
            continue;
        }
        if slot.bypassed && slot.skip_load {
            unavailable.push(UnavailablePlugin { slot_id: slot.id.clone(), message: "Plugin loading is suspended until it is enabled again".into() });
            continue;
        }
        let load = || -> Result<HostedPlugin, String> {
            if !crate::discovery::supports_format(&slot.format) {
                return Err(format!("Plugin format is unavailable: {}", slot.format));
            }
            if slot.bypassed {
                if let Some(warning) = &slot.bypass_warning {
                    return Err(warning.clone());
                }
            }
            let state = slot
                .state
                .as_ref()
                .map(|state| {
                    let bytes = base64::engine::general_purpose::STANDARD
                        .decode(state)
                        .map_err(|_| "Invalid plugin state")?;
                    if bytes.len() > 32 * 1024 * 1024 {
                        return Err("Plugin state exceeds the supported size");
                    }
                    Ok(bytes)
                })
                .transpose()?;
            let mut plugin = HostedPlugin::load(slot)?;
            plugin.prepare_with_state(rate as f32, BLOCK, 2, offline, state.as_deref())?;
            Ok(plugin)
        };
        let mut plugin = match load() {
            Ok(plugin) => plugin,
            Err(message) if slot.bypassed => {
                // Preserve the slot and original state in the project, but do not
                // synthesize a default state for an instance that could not load.
                unavailable.push(UnavailablePlugin {
                    slot_id: slot.id.clone(),
                    message,
                });
                continue;
            }
            Err(message) => return Err(format!("{} ({}): {message}", slot.name, slot.id)),
        };
        let latency = plugin.latency();
        let mut editor = plugin.editor();
        // Initialization callbacks predate the restored component/controller state.
        // Discard them in this adapter, preserving the upstream editor API contract.
        if let Some(editor) = &mut editor {
            let _ = editor.idle();
        }
        let values = plugin
            .info()
            .params
            .iter()
            .filter_map(|parameter| {
                let value = plugin.param(parameter.id)?;
                let normalized = editor.as_ref()?.normalize(parameter.id, value)?;
                Some((parameter.id.to_string(), normalized))
            })
            .collect();
        let plain_values = plugin
            .info()
            .params
            .iter()
            .filter_map(|parameter| {
                let value = plugin.param(parameter.id)?;
                value
                    .is_finite()
                    .then(|| (parameter.id.to_string(), parameter.clamp(value)))
            })
            .collect();
        ui.insert(
            slot.id.clone(),
            UiSlot {
                format: slot.format.clone(),
                info: plugin.info(),
                editor,
                reader: plugin.state_reader(),
                timing: plugin.timing_reader(),
                values,
                plain_values,
                was_open: false,
            },
        );
        result.push(Slot {
            id: slot.id.clone(),
            bypassed: slot.bypassed,
            latency,
            bypass: Delay::new(latency)?,
            dry: vec![vec![0.0; BLOCK]; 2],
            warmup: 0,
            plugin,
        });
    }
    Ok(result)
}

impl Graph {
    #[cfg(test)]
    pub fn load(
        spec: SessionSpec,
        ffmpeg: PathBuf,
        sample_rate: u32,
    ) -> Result<(Self, HashMap<String, UiSlot>), String> {
        Self::load_mode(spec, ffmpeg, sample_rate, false)
    }

    pub fn load_mode(
        spec: SessionSpec,
        ffmpeg: PathBuf,
        sample_rate: u32,
        offline: bool,
    ) -> Result<(Self, HashMap<String, UiSlot>), String> {
        spec.validate()?;
        let mut ui = HashMap::new();
        let mut unavailable_plugins = Vec::new();
        let mut chains = Vec::new();
        for track in &spec.tracks {
            chains.push(
                chain(
                    &track.plugin_chain,
                    sample_rate,
                    offline,
                    &mut ui,
                    &mut unavailable_plugins,
                )
                .map_err(|message| {
                    let label = if track.name.trim().is_empty() { &track.id } else { &track.name };
                    format!("Track {label:?}: {message}")
                })?,
            );
        }
        let master = chain(
            &spec.plugin_chain,
            sample_rate,
            offline,
            &mut ui,
            &mut unavailable_plugins,
        )
        .map_err(|message| format!("Master output: {message}"))?;
        let track_latencies: Vec<usize> = chains
            .iter()
            .map(|c| total_latency(c.iter().map(|s| s.latency)))
            .collect();
        let max_latency = track_latencies.iter().copied().max().unwrap_or(0);
        let limiter = crate::limiter::MasterLimiter::new(
            spec.master_limiter.clone().unwrap_or_default(),
            sample_rate,
        );
        let latency = max_latency
            .saturating_add(total_latency(master.iter().map(|s| s.latency)))
            .saturating_add(limiter.latency());
        if latency > MAX_LATENCY {
            return Err("Combined plugin latency exceeds the supported limit".into());
        }
        let tail_cap = u64::from(sample_rate) * 30;
        let tail = tail_length(&chains, &master, &spec);
        let duration = (spec.duration() * f64::from(sample_rate)).round() as u64;
        let delays = track_latencies
            .into_iter()
            .map(|l| Delay::new(max_latency - l))
            .collect::<Result<_, _>>()?;
        let tracks = spec
            .tracks
            .iter()
            .map(|_| vec![vec![0.0; BLOCK]; 2])
            .collect();
        let levels = vec![[0.0; 2]; spec.tracks.len()];
        let suppressed = vec![false; spec.tracks.len()];
        let timeline = Timeline::new(spec.clone(), ffmpeg, sample_rate)?;
        Ok((
            Self {
                spec,
                sample_rate,
                offline,
                duration,
                latency,
                tail: tail.min(tail_cap),
                tail_limited: tail > tail_cap,
                unavailable_plugins,
                timeline,
                tracks,
                chains,
                suppressed,
                master,
                limiter,
                delays,
                mix: vec![vec![0.0; BLOCK]; 2],
                levels,
            },
            ui,
        ))
    }

    pub fn is_offline(&self) -> bool {
        self.offline
    }

    pub fn reset(&mut self) {
        self.timeline.clear_pending();
        self.limiter.reset();
        for slot in self
            .chains
            .iter_mut()
            .flatten()
            .chain(self.master.iter_mut())
        {
            slot.plugin.reset();
            slot.bypass.reset();
            slot.warmup = 0;
        }
        for delay in &mut self.delays {
            delay.reset();
        }
    }

    pub fn suppressed_track_ids(&self) -> Vec<String> {
        self.spec
            .tracks
            .iter()
            .zip(&self.suppressed)
            .filter(|(_, suppressed)| **suppressed)
            .map(|(track, _)| track.id.clone())
            .collect()
    }

    pub fn monitor_mask(&self) -> &[bool] {
        &self.suppressed
    }

    pub fn set_monitor(&mut self, ids: &[String]) -> Result<(), String> {
        let next = monitor_mask(&self.spec, ids)?;
        self.timeline.set_monitor_mask(next.clone())?;
        for (index, suppressed) in next.iter().copied().enumerate() {
            if self.suppressed[index] == suppressed {
                continue;
            }
            self.delays[index].reset();
            for channel in &mut self.tracks[index] {
                channel.fill(0.0);
            }
            for slot in &mut self.chains[index] {
                slot.plugin.reset();
                slot.bypass.reset();
                slot.warmup = slot.latency;
                for channel in &mut slot.dry {
                    channel.fill(0.0);
                }
            }
            self.levels[index] = [0.0; 2];
        }
        self.suppressed = next;
        Ok(())
    }

    pub fn flush_params(&mut self) -> Result<(), String> {
        for slot in self
            .chains
            .iter_mut()
            .flatten()
            .chain(self.master.iter_mut())
        {
            slot.plugin
                .flush_pending()
                .map_err(|e| format!("{}: {e}", slot.id))?;
        }
        Ok(())
    }

    /// Runs on the stopped worker after VST3 timing was published by the control thread.
    /// VST3 reads its cache here; other adapters refresh their stopped-processing metadata.
    /// Rebuild PDC on the same instances and include tail-only preset changes.
    pub fn renegotiate_latency(&mut self) -> Result<(), String> {
        self.flush_params()?;
        let track_latencies: Vec<Vec<usize>> = self
            .chains
            .iter_mut()
            .map(|chain| {
                chain
                    .iter_mut()
                    .map(|slot| slot.plugin.refresh_latency_control())
                    .collect()
            })
            .collect();
        let master_latencies: Vec<usize> = self
            .master
            .iter_mut()
            .map(|slot| slot.plugin.refresh_latency_control())
            .collect();
        let totals: Vec<usize> = track_latencies
            .iter()
            .map(|values| total_latency(values.iter().copied()))
            .collect();
        let maximum = totals.iter().copied().max().unwrap_or(0);
        let combined = maximum
            .saturating_add(total_latency(master_latencies.iter().copied()))
            .saturating_add(self.limiter.latency());
        if combined > MAX_LATENCY {
            return Err("Combined plugin latency exceeds the supported limit".into());
        }
        let delays = totals
            .into_iter()
            .map(|value| Delay::new(maximum - value))
            .collect::<Result<Vec<_>, _>>()?;
        for (chain, values) in self.chains.iter_mut().zip(track_latencies) {
            for (slot, value) in chain.iter_mut().zip(values) {
                slot.bypass = Delay::new(value)?;
                slot.latency = value;
            }
        }
        for (slot, value) in self.master.iter_mut().zip(master_latencies) {
            slot.bypass = Delay::new(value)?;
            slot.latency = value;
        }
        self.delays = delays;
        self.latency = combined;
        let tail = tail_length(&self.chains, &self.master, &self.spec);
        let tail_cap = u64::from(self.sample_rate) * 30;
        self.tail = tail.min(tail_cap);
        self.tail_limited = tail > tail_cap;
        self.reset();
        Ok(())
    }

    pub fn update_mix(
        &mut self,
        spec: SessionSpec,
        prepared: crate::timeline::PreparedMixUpdate,
    ) -> Result<(), String> {
        if spec.tracks.len() != self.tracks.len()
            || spec
                .tracks
                .iter()
                .zip(&self.spec.tracks)
                .any(|(next, old)| next.id != old.id)
        {
            return Err("Audio graph topology changed".into());
        }
        let next_limiter = spec.master_limiter.clone().unwrap_or_default();
        let maximum = self.chains.iter()
            .map(|chain| total_latency(chain.iter().map(|slot| slot.latency)))
            .max().unwrap_or(0);
        let latency = maximum
            .saturating_add(total_latency(self.master.iter().map(|slot| slot.latency)))
            .saturating_add(next_limiter.latency(self.sample_rate));
        if latency > MAX_LATENCY {
            return Err("Combined plugin latency exceeds the supported limit".into());
        }
        self.timeline.apply_mix_update(prepared)?;
        self.limiter.configure(next_limiter);
        self.latency = latency;
        for (slots, track) in self.chains.iter_mut().zip(&spec.tracks) {
            for slot in slots {
                if let Some(next) = track.plugin_chain.iter().find(|s| s.id == slot.id) {
                    slot.set_bypassed(next.bypassed);
                }
            }
        }
        for slot in &mut self.master {
            if let Some(next) = spec.plugin_chain.iter().find(|s| s.id == slot.id) {
                slot.set_bypassed(next.bypassed);
            }
        }
        self.duration = (spec.duration() * f64::from(self.sample_rate)).round() as u64;
        self.spec = spec;
        let tail = tail_length(&self.chains, &self.master, &self.spec);
        let cap = u64::from(self.sample_rate) * 30;
        self.tail = tail.min(cap);
        self.tail_limited = tail > cap;
        Ok(())
    }

    pub fn param(
        &mut self,
        slot_id: &str,
        id: &str,
        normalized: f64,
        plain: f32,
    ) -> Result<(), String> {
        if let Some(slot) = self
            .chains
            .iter_mut()
            .flatten()
            .chain(self.master.iter_mut())
            .find(|s| s.id == slot_id)
        {
            if slot.plugin.queue_normalized(id, normalized, plain) {
                return Ok(());
            }
            return Err(format!(
                "Plugin parameter could not be queued ({slot_id}, {id})"
            ));
        }
        Err(format!("Plugin instance is unavailable ({slot_id})"))
    }

    pub fn render(
        &mut self,
        position: u64,
        frames: usize,
        playing: bool,
    ) -> Result<bool, GraphError> {
        if !self
            .timeline
            .render_dry(position, frames, &mut self.tracks)?
        {
            return Ok(false);
        }
        self.mix[0][..frames].fill(0.0);
        self.mix[1][..frames].fill(0.0);
        let seconds = position as f64 / f64::from(self.sample_rate);
        let quarters = seconds * 2.0;
        let transport = Transport {
            playing,
            samples: position as i64,
            seconds,
            tempo: 120.0,
            numerator: 4,
            denominator: 4,
            quarters,
            bar_start: (quarters / 4.0).floor() * 4.0,
            bar: (quarters / 4.0).floor() as i64,
            ..Transport::default()
        };
        let solo = self.spec.tracks.iter().any(|t| t.solo);
        for ((index, io), (chain, delay)) in self
            .tracks
            .iter_mut()
            .enumerate()
            .zip(self.chains.iter_mut().zip(&mut self.delays))
        {
            if self.suppressed[index] {
                io[0][..frames].fill(0.0);
                io[1][..frames].fill(0.0);
            }
            for slot in chain {
                slot.process(io, frames, &transport)?;
            }
            if self.suppressed[index]
                || self.spec.tracks[index].muted
                || (solo && !self.spec.tracks[index].solo)
            {
                io[0][..frames].fill(0.0);
                io[1][..frames].fill(0.0);
                delay.reset();
            }
            delay.process(io, frames);
            for channel in 0..2 {
                self.levels[index][channel] = io[channel][..frames]
                    .iter()
                    .map(|v| v.abs())
                    .fold(0.0f32, f32::max);
                for i in 0..frames {
                    self.mix[channel][i] += io[channel][i];
                }
            }
        }
        let pan = self.spec.master_pan as f32;
        for channel in 0..2 {
            let gain = self.spec.master_volume as f32
                * if channel == 0 {
                    1.0 - pan.max(0.0)
                } else {
                    1.0 + pan.min(0.0)
                };
            for value in &mut self.mix[channel][..frames] {
                *value *= gain;
            }
        }
        for slot in &mut self.master {
            slot.process(&mut self.mix, frames, &transport)?;
        }
        if self
            .mix
            .iter()
            .any(|channel| channel[..frames].iter().any(|sample| !sample.is_finite()))
        {
            return Err(GraphError::Failure(
                "Audio graph produced invalid samples".into(),
            ));
        }
        self.limiter.process(&mut self.mix, frames);
        Ok(true)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn limiter_latency_survives_update_renegotiation_and_reset_without_adding_a_tail() {
        let mut spec: SessionSpec = serde_json::from_value(serde_json::json!({
            "id":"limiter", "name":"Audio", "sources":[], "tracks":[],
            "masterLimiter":{"enabled":true,"attackMs":100.0}
        })).unwrap();
        let (mut graph, _) = Graph::load(spec.clone(), "ffmpeg".into(), 48000).unwrap();
        assert_eq!(graph.latency, 4800);
        assert_eq!(graph.tail, 0);
        graph.renegotiate_latency().unwrap(); assert_eq!(graph.latency, 4800);
        spec.master_limiter.as_mut().unwrap().attack_ms = 3.0;
        let prepared = crate::timeline::prepare_mix_update(spec.clone(), 48000).unwrap();
        graph.update_mix(spec.clone(), prepared).unwrap(); assert_eq!(graph.latency, 144);
        graph.reset(); assert_eq!(graph.latency, 144);
        spec.master_limiter.as_mut().unwrap().enabled = false;
        let prepared = crate::timeline::prepare_mix_update(spec.clone(), 48000).unwrap();
        graph.update_mix(spec, prepared).unwrap(); assert_eq!(graph.latency, 0);
        graph.renegotiate_latency().unwrap(); assert_eq!(graph.latency, 0);
    }

    #[test]
    fn monitor_suppression_survives_mix_updates_and_ignores_legacy_effects() {
        // Legacy projects may still carry the removed built-in `effects` object;
        // it must be tolerated and ignored rather than rejected.
        let spec: SessionSpec = serde_json::from_value(serde_json::json!({
            "id":"monitor", "name":"Audio", "sources":[], "tracks":[
                {"id":"voice", "clips":[], "effects":{"delay":1.0,"delayTime":0.05}},
                {"id":"backing", "clips":[], "effects":{"reverb":0.4}}
            ]
        }))
        .unwrap();
        let (mut graph, _) = Graph::load(spec.clone(), "ffmpeg".into(), 8000).unwrap();
        assert_eq!(graph.tail, 0, "Tracks without plugins contribute no tail");
        let latency = graph.latency;
        graph.set_monitor(&["voice".into()]).unwrap();
        assert_eq!(graph.suppressed_track_ids(), ["voice"]);
        assert!(graph.render(BLOCK as u64, BLOCK, true).unwrap());
        assert_eq!(graph.levels[0], [0.0; 2]);
        assert_eq!(graph.latency, latency);
        assert_eq!(
            serde_json::to_value(&graph.spec).unwrap(),
            serde_json::to_value(&spec).unwrap()
        );
        assert!(graph.set_monitor(&["missing".into()]).is_err());
        assert_eq!(graph.suppressed_track_ids(), ["voice"]);
        let mut next = spec.clone();
        next.master_volume = 0.5;
        let prepared =
            crate::timeline::prepare_mix_update(next.clone(), 8000).unwrap();
        graph.update_mix(next, prepared).unwrap();
        assert_eq!(graph.suppressed_track_ids(), ["voice"]);
        graph.set_monitor(&[]).unwrap();
        assert!(graph.suppressed_track_ids().is_empty());
        assert!(graph.render((BLOCK * 2) as u64, BLOCK, true).unwrap());
    }

    #[test]
    fn invalid_dynamic_latency_cannot_wrap_into_an_accepted_delay() {
        assert_eq!(total_latency([usize::MAX, 1].into_iter()), usize::MAX);
        assert!(total_latency([MAX_LATENCY, 1].into_iter()) > MAX_LATENCY);
        assert_eq!(total_latency([3, 7].into_iter()), 10);
    }

    fn unavailable_project(path: &std::path::Path, bypassed: bool) -> SessionSpec {
        serde_json::from_value(serde_json::json!({
            "id": "missing-project", "name": "Audio", "sources": [], "tracks": [],
            "pluginChain": [{ "id": "missing-slot", "format": "vst3",
                "pluginId": "vst3:00000000000000000000000000000001", "name": "Effect",
                "path": path, "bypassed": bypassed, "state": "opaque-original-state" }]
        }))
        .unwrap()
    }

    #[test]
    fn bypassed_missing_plugin_remains_in_the_project_without_a_synthetic_state() {
        let directory = tempfile::tempdir().unwrap();
        let spec = unavailable_project(&directory.path().join("missing.vst3"), true);
        let (graph, ui) = Graph::load(spec, PathBuf::from("ffmpeg"), 48000).unwrap();
        assert!(ui.is_empty());
        assert_eq!(graph.latency, 0);
        assert_eq!(graph.unavailable_plugins.len(), 1);
        assert_eq!(graph.unavailable_plugins[0].slot_id, "missing-slot");
        assert!(!graph.unavailable_plugins[0].message.is_empty());
        assert_eq!(
            graph.spec.plugin_chain[0].state.as_deref(),
            Some("opaque-original-state")
        );
    }

    #[test]
    fn offline_graph_omits_disabled_master_and_track_plugins_without_loading_or_warning() {
        let directory = tempfile::tempdir().unwrap();
        let mut spec = unavailable_project(&directory.path().join("missing.vst3"), true);
        let mut track_slot = spec.plugin_chain[0].clone();
        track_slot.id = "track-fx".into();
        spec.tracks = serde_json::from_value(serde_json::json!([
            {"id":"track", "clips":[]}
        ])).unwrap();
        spec.tracks[0].plugin_chain.push(track_slot);
        let (graph, ui) = Graph::load_mode(spec, PathBuf::from("ffmpeg"), 48000, true).unwrap();
        assert!(ui.is_empty());
        assert!(graph.unavailable_plugins.is_empty(), "disabled plugins must never be loaded");
        assert!(graph.master.is_empty());
        assert!(graph.chains[0].is_empty());
        assert_eq!(graph.latency, 0);
        assert_eq!(graph.tail, 0);
        assert_eq!(graph.spec.plugin_chain[0].state.as_deref(), Some("opaque-original-state"));
        assert_eq!(graph.spec.tracks[0].plugin_chain[0].state.as_deref(), Some("opaque-original-state"));
    }

    #[test]
    fn quarantined_bypasses_keep_their_state_without_instantiating_and_enable_still_retries() {
        let directory = tempfile::tempdir().unwrap();
        let mut spec = unavailable_project(&directory.path().join("missing.vst3"), true);
        spec.plugin_chain[0].skip_load = true;
        let (graph, ui) = Graph::load(spec.clone(), "ffmpeg".into(), 48000).unwrap();
        assert!(ui.is_empty());
        assert!(graph.unavailable_plugins[0].message.contains("suspended"));
        assert_eq!(graph.spec.plugin_chain[0].state.as_deref(), Some("opaque-original-state"));
        spec.plugin_chain[0].bypassed = false;
        assert!(Graph::load(spec, "ffmpeg".into(), 48000).is_err());
    }

    #[test]
    fn enabled_missing_plugins_fail_with_the_instance_identity() {
        let directory = tempfile::tempdir().unwrap();
        let spec = unavailable_project(&directory.path().join("missing.vst3"), false);
        let result = Graph::load(spec, PathBuf::from("ffmpeg"), 48000);
        assert!(matches!(result, Err(message) if message.starts_with("Master output: ") && message.contains("missing-slot")));
    }

    #[test]
    fn track_plugin_load_errors_identify_the_chain_that_failed() {
        let directory = tempfile::tempdir().unwrap();
        let mut spec = unavailable_project(&directory.path().join("missing.vst3"), false);
        let mut slot = spec.plugin_chain.remove(0);
        slot.name = "Pro-C 3".into();
        spec.tracks = serde_json::from_value(serde_json::json!([
            {"id":"voice", "name":"Vocals", "clips":[]},
            {"id":"drums", "name":"Drums", "clips":[], "pluginChain":[slot]}
        ])).unwrap();
        for label in ["Drums", "drums"] {
            if label == "drums" { spec.tracks[1].name.clear(); }
            let result = Graph::load(spec.clone(), "ffmpeg".into(), 48000);
            assert!(matches!(result, Err(message) if message.starts_with(&format!("Track {label:?}: "))
                && message.contains("Pro-C 3 (missing-slot)")));
        }
    }

    #[test]
    fn unavailable_saved_state_bypasses_without_loading_a_default_instance() {
        let directory = tempfile::tempdir().unwrap();
        let mut spec = unavailable_project(&directory.path().join("effect.vst3"), true);
        spec.plugin_chain[0].state = None;
        spec.plugin_chain[0].bypass_warning = Some("Saved plugin state is unavailable".into());
        let (graph, ui) = Graph::load(spec, PathBuf::from("ffmpeg"), 48000).unwrap();
        assert!(ui.is_empty());
        assert_eq!(
            graph.unavailable_plugins[0].message,
            "Saved plugin state is unavailable"
        );
    }

    #[test]
    fn delay_compensation_is_sample_exact_across_blocks_and_reset() {
        let mut delay = Delay::new(3).unwrap();
        let mut io = vec![vec![1., 2., 3., 4.], vec![10., 20., 30., 40.]];
        delay.process(&mut io, 4);
        assert_eq!(io[0], [0., 0., 0., 1.]);
        let mut next = vec![vec![5., 6., 7., 8.], vec![50., 60., 70., 80.]];
        delay.process(&mut next, 4);
        assert_eq!(next[0], [2., 3., 4., 5.]);
        delay.reset();
        delay.process(&mut next, 4);
        assert_eq!(&next[0][..3], &[0., 0., 0.]);
    }

    #[test]
    fn bypass_routes_aligned_dry_and_refills_wet_across_partial_blocks() {
        const CHILD_BUNDLE: &str = "PYMSS_BYPASS_SIGNAL_BUNDLE";
        if let Some(bundle) = std::env::var_os(CHILD_BUNDLE) {
            // This subprocess owns its fixture mode and loads no UI or audio device.
            let descriptors =
                soundcraft_vst3_host::load_bundle(std::path::Path::new(&bundle)).unwrap();
            let descriptor = descriptors
                .iter()
                .find(|plugin| !plugin.is_instrument)
                .unwrap();
            let mut plugin = HostedPlugin::Vst3(
                soundcraft_vst3_host::instantiate_descriptor(descriptor).unwrap(),
            );
            plugin.try_prepare(48000.0, BLOCK, 2).unwrap();
            let latency = plugin.latency();
            assert_eq!(
                latency, 3,
                "The fixture must exercise a partial-block latency"
            );
            let mut slot = Slot {
                id: "gain".into(),
                plugin,
                bypassed: false,
                latency,
                bypass: Delay::new(latency).unwrap(),
                dry: vec![vec![0.0; BLOCK]; 2],
                warmup: 0,
            };
            fn check(slot: &mut Slot, cursor: &mut usize, frames: usize, dry_prefix: usize) {
                let mut io = vec![vec![0.0; BLOCK]; 2];
                for channel in 0..2 {
                    for index in 0..frames {
                        io[channel][index] = (*cursor + index + 1) as f32
                            * 0.01
                            * if channel == 0 { 1.0 } else { -1.0 };
                    }
                }
                slot.process(&mut io, frames, &Transport::default())
                    .unwrap();
                for channel in 0..2 {
                    for index in 0..frames {
                        let source = (*cursor + index + 1).saturating_sub(3) as f32 * 0.01;
                        let gain = if index < dry_prefix { 1.0 } else { 0.5 };
                        let expected = source * gain * if channel == 0 { 1.0 } else { -1.0 };
                        assert!(
                            (io[channel][index] - expected).abs() < 1e-6,
                            "frame {} channel {channel}: {} != {expected}",
                            *cursor + index,
                            io[channel][index]
                        );
                    }
                }
                *cursor += frames;
            }
            let mut cursor = 0;
            check(&mut slot, &mut cursor, 5, 0);
            slot.set_bypassed(true);
            check(&mut slot, &mut cursor, 2, 2);
            check(&mut slot, &mut cursor, 5, 5);
            slot.set_bypassed(false);
            check(&mut slot, &mut cursor, 2, 2);
            check(&mut slot, &mut cursor, 5, 1);
            drop(slot);
            soundcraft_vst3_host::shutdown();
            return;
        }

        let executable = std::env::current_exe().unwrap();
        let target = executable
            .parent()
            .unwrap()
            .join("bypass-signal-fixture-target");
        assert!(
            std::process::Command::new(std::env::var_os("CARGO").unwrap_or_else(|| "cargo".into()))
                .args(["build", "--locked", "--quiet", "--manifest-path"])
                .arg(
                    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                        .join("../vendor/soundcraft-vst3-host/tests/fixture/Cargo.toml")
                )
                .arg("--target-dir")
                .arg(&target)
                .env_remove("RUSTFLAGS")
                .status()
                .unwrap()
                .success()
        );
        let directory = tempfile::tempdir_in(executable.parent().unwrap()).unwrap();
        let bundle = directory.path().join("Signal.vst3");
        let (library, folder, binary) = if cfg!(windows) {
            ("vst3_fixture.dll", "x86_64-win", "Signal.vst3")
        } else if cfg!(target_os = "macos") {
            ("libvst3_fixture.dylib", "MacOS", "Signal")
        } else {
            (
                "libvst3_fixture.so",
                if cfg!(target_arch = "aarch64") {
                    "aarch64-linux"
                } else {
                    "x86_64-linux"
                },
                "Signal.so",
            )
        };
        let binary_directory = bundle.join("Contents").join(folder);
        std::fs::create_dir_all(&binary_directory).unwrap();
        std::fs::copy(
            target.join("debug").join(library),
            binary_directory.join(binary),
        )
        .unwrap();
        let mut child = std::process::Command::new(executable)
            .args([
                "--exact",
                "graph::tests::bypass_routes_aligned_dry_and_refills_wet_across_partial_blocks",
                "--nocapture",
            ])
            .env(CHILD_BUNDLE, &bundle)
            .env("PYMSS_VST_FIXTURE_BEHAVIOR", "delay")
            .spawn()
            .unwrap();
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
        loop {
            if let Some(status) = child.try_wait().unwrap() {
                assert!(status.success(), "The bypass signal subprocess failed");
                break;
            }
            if std::time::Instant::now() >= deadline {
                let _ = child.kill();
                let _ = child.wait();
                panic!("The bypass signal subprocess timed out");
            }
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
    }
}
