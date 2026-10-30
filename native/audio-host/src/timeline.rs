//! Bounded, asynchronous decoding and the dry portion of the editor signal path.
//!
//! `render_dry` never waits for a decoder. Its caller must keep the transport at
//! the same frame while it returns `false`, then retry that block. Filesystem and
//! process I/O happen only when creating a timeline or on decoder worker threads.

pub use crate::limiter::MasterLimiterSpec;

use crossbeam_channel::{Receiver, Sender, TrySendError, bounded};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::thread;
use std::time::{Duration, Instant};

const CHUNK_SECONDS: u64 = 4;
const PREFETCH_SECONDS: u64 = 8;
const CACHE_CHUNKS: usize = 48;
const DECODE_WORKERS: usize = 4;
const MAX_BLOCK_FRAMES: usize = 1024;
const MAX_DURATION_SECONDS: f64 = 86_400.0;
const MAX_TRACKS: usize = 256;
const MAX_SOURCES: usize = 4096;
const MAX_CLIPS: usize = 65_536;
const MAX_PLUGIN_SLOTS: usize = 1024;
const DECODE_TIMEOUT: Duration = Duration::from_secs(30);

fn unity() -> f64 {
    1.0
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PluginSlot {
    pub id: String,
    pub format: String,
    pub plugin_id: String,
    pub name: String,
    pub path: String,
    #[serde(default)]
    pub bypassed: bool,
    #[serde(default)]
    pub skip_load: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub state: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub bypass_warning: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SourceSpec {
    pub id: String,
    pub path: String,
    #[serde(default)]
    pub name: String,
    pub duration: f64,
    #[serde(default)]
    pub sample_rate: u32,
    #[serde(default)]
    pub channels: u32,
    #[serde(default)]
    pub missing: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ClipSpec {
    pub id: String,
    pub asset_id: String,
    pub start: f64,
    pub offset: f64,
    pub duration: f64,
    #[serde(default = "unity")]
    pub volume: f64,
    #[serde(default)]
    pub fade_in: f64,
    #[serde(default)]
    pub fade_out: f64,
    #[serde(default)]
    pub muted: bool,
    #[serde(default)]
    pub locked: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TrackSpec {
    pub id: String,
    #[serde(default)]
    pub source_id: String,
    #[serde(default)]
    pub name: String,
    #[serde(default = "unity")]
    pub volume: f64,
    #[serde(default)]
    pub pan: f64,
    #[serde(default)]
    pub muted: bool,
    #[serde(default)]
    pub solo: bool,
    #[serde(default)]
    pub fade_in: f64,
    #[serde(default)]
    pub fade_out: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub clips: Option<Vec<ClipSpec>>,
    #[serde(default)]
    pub plugin_chain: Vec<PluginSlot>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionSpec {
    pub id: String,
    pub name: String,
    #[serde(default = "unity")]
    pub master_volume: f64,
    #[serde(default)]
    pub master_pan: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub master_limiter: Option<MasterLimiterSpec>,
    pub sources: Vec<SourceSpec>,
    pub tracks: Vec<TrackSpec>,
    #[serde(default)]
    pub plugin_chain: Vec<PluginSlot>,
}

fn valid_id(id: &str, kind: &str) -> Result<(), String> {
    if id.trim().is_empty() || id.len() > 256 || id.chars().any(char::is_control) {
        return Err(format!("{kind} identifier is empty or invalid"));
    }
    Ok(())
}

fn in_range(value: f64, minimum: f64, maximum: f64, field: &str) -> Result<(), String> {
    if !value.is_finite() || value < minimum || value > maximum {
        return Err(format!(
            "{field} must be a finite number between {minimum} and {maximum}"
        ));
    }
    Ok(())
}

fn has_clips(track: &TrackSpec) -> bool {
    track.clips.as_ref().is_some_and(|clips| !clips.is_empty())
}

impl SessionSpec {
    /// Validates structure and the files that could actually be heard. Unused,
    /// muted, and solo-excluded offline assets remain recoverable in a project.
    pub fn validate(&self) -> Result<(), String> {
        valid_id(&self.id, "Session")?;
        in_range(self.master_volume, 0.0, 2.0, "Master volume")?;
        in_range(self.master_pan, -1.0, 1.0, "Master pan")?;
        if let Some(limiter) = &self.master_limiter {
            limiter.validate()?;
        }
        if self.tracks.len() > MAX_TRACKS || self.sources.len() > MAX_SOURCES {
            return Err(format!(
                "A session supports at most {MAX_TRACKS} tracks and {MAX_SOURCES} sources"
            ));
        }
        let mut sources = HashMap::with_capacity(self.sources.len());
        for source in &self.sources {
            valid_id(&source.id, "Source")?;
            if sources.insert(source.id.as_str(), source).is_some() {
                return Err(format!("Duplicate source identifier: {}", source.id));
            }
            in_range(
                source.duration,
                0.0,
                MAX_DURATION_SECONDS,
                "Source duration",
            )?;
        }
        let mut tracks = HashSet::with_capacity(self.tracks.len());
        let mut clips = HashSet::new();
        let mut used_sources = HashSet::new();
        let has_solo = self.tracks.iter().any(|track| track.solo);
        for track in &self.tracks {
            valid_id(&track.id, "Track")?;
            if !tracks.insert(track.id.as_str()) {
                return Err(format!("Duplicate track identifier: {}", track.id));
            }
            in_range(track.volume, 0.0, 2.0, "Track volume")?;
            in_range(track.pan, -1.0, 1.0, "Track pan")?;
            in_range(track.fade_in, 0.0, MAX_DURATION_SECONDS, "Track fade-in")?;
            in_range(track.fade_out, 0.0, MAX_DURATION_SECONDS, "Track fade-out")?;
            let audible = !track.muted && (!has_solo || track.solo);
            if has_clips(track) {
                for clip in track.clips.as_ref().expect("non-empty clips") {
                    valid_id(&clip.id, "Clip")?;
                    if !clips.insert(clip.id.as_str()) {
                        return Err(format!("Duplicate clip identifier: {}", clip.id));
                    }
                    if clips.len() > MAX_CLIPS {
                        return Err(format!("A session supports at most {MAX_CLIPS} clips"));
                    }
                    let source = sources.get(clip.asset_id.as_str()).ok_or_else(|| {
                        format!(
                            "Clip {} references an unknown source: {}",
                            clip.id, clip.asset_id
                        )
                    })?;
                    in_range(clip.start, 0.0, MAX_DURATION_SECONDS, "Clip start")?;
                    in_range(clip.offset, 0.0, source.duration, "Clip offset")?;
                    in_range(clip.duration, 0.0, MAX_DURATION_SECONDS, "Clip duration")?;
                    in_range(clip.volume, 0.0, 2.0, "Clip volume")?;
                    in_range(clip.fade_in, 0.0, clip.duration, "Clip fade-in")?;
                    in_range(clip.fade_out, 0.0, clip.duration, "Clip fade-out")?;
                    if clip.start + clip.duration > MAX_DURATION_SECONDS {
                        return Err(format!(
                            "Clip {} extends beyond the 24-hour timeline limit",
                            clip.id
                        ));
                    }
                    // Metadata can round the final source sample. Permit at
                    // most one original-rate sample of rounding discrepancy.
                    let tolerance = 1.0 / f64::from(source.sample_rate.max(8000));
                    if clip.offset + clip.duration > source.duration + tolerance {
                        return Err(format!(
                            "Clip {} extends beyond source {}",
                            clip.id, source.id
                        ));
                    }
                    if audible && !clip.muted && clip.duration > 0.0 {
                        used_sources.insert(source.id.as_str());
                    }
                }
            } else if !track.source_id.is_empty() {
                let source = sources.get(track.source_id.as_str()).ok_or_else(|| {
                    format!(
                        "Track {} references an unknown source: {}",
                        track.id, track.source_id
                    )
                })?;
                if track.fade_in > source.duration || track.fade_out > source.duration {
                    return Err(format!(
                        "Track {} has a fade longer than its source",
                        track.id
                    ));
                }
                if audible && source.duration > 0.0 {
                    used_sources.insert(source.id.as_str());
                }
            }
        }
        for source in self
            .sources
            .iter()
            .filter(|source| used_sources.contains(source.id.as_str()))
        {
            if source.missing {
                return Err(format!(
                    "Audio source {} is offline; relink it before using the native audio backend",
                    source.id
                ));
            }
            let path = Path::new(&source.path);
            if source.path.contains('\0') || !path.is_absolute() || !path.is_file() {
                return Err(format!(
                    "Audio source {} must reference an existing absolute file path",
                    source.id
                ));
            }
            if !(8000..=768_000).contains(&source.sample_rate) {
                return Err(format!(
                    "Audio source {} has missing or unsupported sample-rate metadata",
                    source.id
                ));
            }
            if !(1..=64).contains(&source.channels) {
                return Err(format!(
                    "Audio source {} has missing or unsupported channel-count metadata",
                    source.id
                ));
            }
        }
        let slots = self.slots();
        if slots.len() > MAX_PLUGIN_SLOTS {
            return Err(format!(
                "A session supports at most {MAX_PLUGIN_SLOTS} plugin instances"
            ));
        }
        let mut slot_ids = HashSet::with_capacity(slots.len());
        for slot in slots {
            valid_id(&slot.id, "Plugin instance")?;
            valid_id(&slot.plugin_id, "Plugin")?;
            if !slot_ids.insert(slot.id.as_str()) {
                return Err(format!("Duplicate plugin instance identifier: {}", slot.id));
            }
            if !slot.bypassed && !crate::discovery::supports_format(&slot.format) {
                if slot.format == "aax" {
                    return Err(format!(
                        "Plugin {} uses an unsupported format: aax. {}",
                        slot.id,
                        crate::discovery::AAX_HOST_UNAVAILABLE
                    ));
                }
                return Err(format!(
                    "Plugin {} uses an unsupported format: {}",
                    slot.id, slot.format
                ));
            }
            let valid_reference = if slot.format == "au" {
                slot.path == slot.plugin_id
                    && soundcraft_au_host::parse_id(&slot.plugin_id).is_some()
            } else {
                !slot.path.contains('\0') && Path::new(&slot.path).is_absolute()
            };
            if !slot.bypassed && !valid_reference {
                return Err(format!(
                    "Plugin {} must reference a valid plugin location",
                    slot.id
                ));
            }
        }
        Ok(())
    }

    /// Matches the existing editor duration: muted clips and tracks still
    /// occupy their timeline range, and solo does not shorten the project.
    pub fn duration(&self) -> f64 {
        let mut duration: f64 = 0.0;
        for track in &self.tracks {
            if has_clips(track) {
                for clip in track.clips.as_ref().expect("non-empty clips") {
                    duration = duration.max(clip.start + clip.duration);
                }
            } else if let Some(source) = self
                .sources
                .iter()
                .find(|source| source.id == track.source_id)
            {
                duration = duration.max(source.duration);
            }
        }
        duration
    }

    pub fn slots(&self) -> Vec<&PluginSlot> {
        self.plugin_chain
            .iter()
            .chain(self.tracks.iter().flat_map(|track| &track.plugin_chain))
            .collect()
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
struct ChunkKey {
    source: usize,
    index: u64,
}

struct DecodeSource {
    path: PathBuf,
    id: String,
    // Original metadata controls mono duplication; decoded chunks are always stereo.
    source_channels: u32,
    frames: u64,
}

struct DecodeJob {
    key: ChunkKey,
    source: Arc<DecodeSource>,
    generation: u64,
}

struct DecodeResult {
    key: ChunkKey,
    generation: u64,
    samples: Result<Vec<f32>, String>,
}

struct CachedChunk {
    samples: Result<Vec<f32>, String>,
    last_used: u64,
}

struct RuntimeClip {
    source: usize,
    start: u64,
    end: u64,
    offset: u64,
    volume: f64,
    fade_in_frames: f64,
    fade_out_frames: f64,
    muted: bool,
}

struct RuntimeTrack {
    audible: bool,
    gain_left: f64,
    gain_right: f64,
    clips: Vec<RuntimeClip>,
}

/// Constructed on the control thread, then transferred to the rendering thread.
/// All path checks, source lookup maps, and clip allocations have already run.
pub struct PreparedMixUpdate {
    spec: SessionSpec,
    tracks: Vec<RuntimeTrack>,
    sample_rate: u32,
}

pub struct Timeline {
    spec: SessionSpec,
    sample_rate: u32,
    chunk_frames: u64,
    sources: Vec<Arc<DecodeSource>>,
    tracks: Vec<RuntimeTrack>,
    suppressed: Vec<bool>,
    jobs: Option<Sender<DecodeJob>>,
    results: Receiver<DecodeResult>,
    pending: HashSet<ChunkKey>,
    ready: HashMap<ChunkKey, CachedChunk>,
    required: Vec<ChunkKey>,
    prefetch: Vec<ChunkKey>,
    generation: Arc<AtomicU64>,
    shutdown: Arc<AtomicBool>,
    clock: u64,
}

/// Validates a transient recording-monitor mask without modifying the saved mixer state.
pub(crate) fn monitor_mask(spec: &SessionSpec, ids: &[String]) -> Result<Vec<bool>, String> {
    if ids.len() > 256 {
        return Err("Monitor suppression cannot exceed 256 tracks".into());
    }
    let mut mask = vec![false; spec.tracks.len()];
    for id in ids {
        let index = spec
            .tracks
            .iter()
            .position(|track| track.id == *id)
            .ok_or_else(|| format!("Monitor track is unavailable ({id})"))?;
        if mask[index] {
            return Err("Monitor track identifiers must be unique".into());
        }
        mask[index] = true;
    }
    Ok(mask)
}

fn retained_monitor_mask(previous: &SessionSpec, mask: &[bool], next: &SessionSpec) -> Vec<bool> {
    next.tracks
        .iter()
        .map(|track| {
            previous
                .tracks
                .iter()
                .position(|old| old.id == track.id)
                .is_some_and(|index| mask.get(index).copied().unwrap_or(false))
        })
        .collect()
}

fn frame_at(seconds: f64, sample_rate: u32) -> u64 {
    (seconds * f64::from(sample_rate)).round() as u64
}

fn compile_sources(spec: &SessionSpec, sample_rate: u32) -> Vec<Arc<DecodeSource>> {
    spec.sources
        .iter()
        .map(|source| {
            Arc::new(DecodeSource {
                path: PathBuf::from(&source.path),
                id: source.id.clone(),
                source_channels: source.channels,
                frames: frame_at(source.duration, sample_rate),
            })
        })
        .collect()
}

fn compile_tracks(
    spec: &SessionSpec,
    sources: &[Arc<DecodeSource>],
    sample_rate: u32,
) -> Vec<RuntimeTrack> {
    let source_indices: HashMap<_, _> = spec
        .sources
        .iter()
        .enumerate()
        .map(|(index, source)| (source.id.as_str(), index))
        .collect();
    let has_solo = spec.tracks.iter().any(|track| track.solo);
    spec.tracks
        .iter()
        .map(|track| {
            let compile_clip = |clip: &ClipSpec| RuntimeClip {
                source: source_indices[clip.asset_id.as_str()],
                start: frame_at(clip.start, sample_rate),
                end: frame_at(clip.start, sample_rate) + frame_at(clip.duration, sample_rate),
                offset: frame_at(clip.offset, sample_rate),
                volume: clip.volume,
                fade_in_frames: clip.fade_in * f64::from(sample_rate),
                fade_out_frames: clip.fade_out * f64::from(sample_rate),
                muted: clip.muted,
            };
            let clips = if has_clips(track) {
                track
                    .clips
                    .as_ref()
                    .expect("non-empty clips")
                    .iter()
                    .map(compile_clip)
                    .collect()
            } else if let Some(&source) = source_indices.get(track.source_id.as_str()) {
                vec![RuntimeClip {
                    source,
                    start: 0,
                    end: sources[source].frames,
                    offset: 0,
                    volume: 1.0,
                    fade_in_frames: track.fade_in * f64::from(sample_rate),
                    fade_out_frames: track.fade_out * f64::from(sample_rate),
                    muted: false,
                }]
            } else {
                Vec::new()
            };
            RuntimeTrack {
                audible: !track.muted && (!has_solo || track.solo),
                gain_left: track.volume * (1.0 - track.pan.max(0.0)),
                gain_right: track.volume * (1.0 + track.pan.min(0.0)),
                clips,
            }
        })
        .collect()
}

fn same_sources(previous: &SessionSpec, next: &SessionSpec) -> bool {
    previous.sources.len() == next.sources.len()
        && previous
            .sources
            .iter()
            .zip(&next.sources)
            .all(|(old, new)| {
                old.id == new.id
                    && old.path == new.path
                    && old.duration == new.duration
                    && old.sample_rate == new.sample_rate
                    && old.channels == new.channels
            })
}

pub fn prepare_mix_update(
    spec: SessionSpec,
    sample_rate: u32,
) -> Result<PreparedMixUpdate, String> {
    spec.validate()?;
    if !(8000..=192_000).contains(&sample_rate) {
        return Err("Native audio sample rate must be between 8000 and 192000 Hz".into());
    }
    let sources = compile_sources(&spec, sample_rate);
    let tracks = compile_tracks(&spec, &sources, sample_rate);
    Ok(PreparedMixUpdate {
        spec,
        tracks,
        sample_rate,
    })
}

impl Timeline {
    pub fn new(spec: SessionSpec, ffmpeg: PathBuf, sample_rate: u32) -> Result<Self, String> {
        spec.validate()?;
        if !(8000..=192_000).contains(&sample_rate) {
            return Err("Native audio sample rate must be between 8000 and 192000 Hz".into());
        }
        if ffmpeg.as_os_str().is_empty() {
            return Err("FFmpeg executable path is empty".into());
        }
        let sources = compile_sources(&spec, sample_rate);
        let tracks = compile_tracks(&spec, &sources, sample_rate);
        let (jobs_tx, jobs_rx) = bounded::<DecodeJob>(CACHE_CHUNKS);
        let (results_tx, results_rx) = bounded::<DecodeResult>(DECODE_WORKERS * 2);
        let generation = Arc::new(AtomicU64::new(0));
        let shutdown = Arc::new(AtomicBool::new(false));
        let ffmpeg = Arc::new(ffmpeg);
        for worker in 0..DECODE_WORKERS {
            let jobs = jobs_rx.clone();
            let results = results_tx.clone();
            let generation = Arc::clone(&generation);
            let shutdown = Arc::clone(&shutdown);
            let ffmpeg = Arc::clone(&ffmpeg);
            thread::Builder::new()
                .name(format!("audio-decode-{worker}"))
                .spawn(move || {
                    decoder_worker(jobs, results, ffmpeg, sample_rate, generation, shutdown);
                })
                .map_err(|error| {
                    // A partially created pool must not outlive a failed timeline.
                    // Already started workers also observe sender disconnection.
                    format!("Could not start an audio decoder worker: {error}")
                })?;
        }
        let suppressed = vec![false; tracks.len()];
        Ok(Self {
            spec,
            sample_rate,
            chunk_frames: CHUNK_SECONDS * u64::from(sample_rate),
            sources,
            tracks,
            suppressed,
            jobs: Some(jobs_tx),
            results: results_rx,
            pending: HashSet::with_capacity(CACHE_CHUNKS),
            ready: HashMap::with_capacity(CACHE_CHUNKS),
            required: Vec::with_capacity(CACHE_CHUNKS),
            prefetch: Vec::with_capacity(CACHE_CHUNKS),
            generation,
            shutdown,
            clock: 0,
        })
    }

    pub fn spec(&self) -> &SessionSpec {
        &self.spec
    }

    /// Changes edits and mixer settings without restarting decoders. Sources
    /// are index-addressed in queued work, so a source-list or media metadata
    /// change invalidates old work while ordinary gain/clip edits keep caches.
    pub fn update(&mut self, spec: SessionSpec) -> Result<(), String> {
        spec.validate()?;
        let suppressed = retained_monitor_mask(&self.spec, &self.suppressed, &spec);
        if !same_sources(&self.spec, &spec) {
            self.clear_pending();
            self.ready.clear();
            self.required.clear();
            self.prefetch.clear();
            self.sources = compile_sources(&spec, self.sample_rate);
        }
        self.tracks = compile_tracks(&spec, &self.sources, self.sample_rate);
        self.suppressed = suppressed;
        self.spec = spec;
        Ok(())
    }

    /// Applies a control-thread preparation without filesystem I/O, source
    /// lookups, decoding, or recompilation. Source changes require rebuilding
    /// the graph on the control thread rather than invalidating it mid-block.
    pub fn apply_mix_update(&mut self, prepared: PreparedMixUpdate) -> Result<(), String> {
        if prepared.sample_rate != self.sample_rate || !same_sources(&self.spec, &prepared.spec) {
            return Err("Audio sources or sample rate changed; reload the audio graph".into());
        }
        self.suppressed = retained_monitor_mask(&self.spec, &self.suppressed, &prepared.spec);
        self.tracks = prepared.tracks;
        self.spec = prepared.spec;
        Ok(())
    }

    pub(crate) fn set_monitor_mask(&mut self, mask: Vec<bool>) -> Result<(), String> {
        if mask.len() != self.tracks.len() {
            return Err("Monitor track topology changed".into());
        }
        self.suppressed = mask;
        self.required.clear();
        self.prefetch.clear();
        // In-flight chunks may also serve an accompanying track. Preserve their jobs/cache;
        // subsequent required/prefetch collection omits every suppressed track.
        Ok(())
    }

    /// Cancels queued/in-flight old decode work after a seek. Already cached
    /// chunks are reusable. No worker is joined or process waited on here.
    pub fn clear_pending(&mut self) {
        self.generation.fetch_add(1, Ordering::AcqRel);
        self.pending.clear();
    }

    /// Fills one planar stereo bus per track before plugins and master gain.
    /// Output is cleared even when data is not ready, so partial blocks cannot
    /// escape. The caller must not advance its frame on `Ok(false)`.
    pub fn render_dry(
        &mut self,
        frame: u64,
        frames: usize,
        output: &mut [Vec<Vec<f32>>],
    ) -> Result<bool, String> {
        if frames > MAX_BLOCK_FRAMES {
            return Err(format!(
                "Native audio blocks cannot exceed {MAX_BLOCK_FRAMES} frames"
            ));
        }
        if output.len() < self.tracks.len()
            || output
                .iter()
                .take(self.tracks.len())
                .any(|bus| bus.len() < 2 || bus[0].len() < frames || bus[1].len() < frames)
        {
            return Err(
                "Native audio output buses must contain two preallocated channels per track".into(),
            );
        }
        for bus in output.iter_mut().take(self.tracks.len()) {
            bus[0][..frames].fill(0.0);
            bus[1][..frames].fill(0.0);
        }
        if frames == 0 {
            return Ok(true);
        }
        let end = frame
            .checked_add(frames as u64)
            .ok_or("Native audio frame position overflow")?;
        self.clock = self.clock.wrapping_add(1);
        self.required.clear();
        self.prefetch.clear();
        for (index, track) in self.tracks.iter().enumerate() {
            if self.suppressed[index]
                || !track.audible
                || (track.gain_left == 0.0 && track.gain_right == 0.0)
            {
                continue;
            }
            for clip in &track.clips {
                collect_chunks(
                    clip,
                    frame,
                    end,
                    &self.sources,
                    self.chunk_frames,
                    &mut self.required,
                    CACHE_CHUNKS,
                )?;
            }
        }
        let lookahead_end = end.saturating_add(PREFETCH_SECONDS * u64::from(self.sample_rate));
        let prefetch_limit = CACHE_CHUNKS - self.required.len();
        // Prefetch failure does not prevent the current block from playing.
        // The combined desired working set never exceeds cache capacity.
        for (index, track) in self.tracks.iter().enumerate() {
            if self.suppressed[index]
                || !track.audible
                || (track.gain_left == 0.0 && track.gain_right == 0.0)
            {
                continue;
            }
            for clip in &track.clips {
                collect_prefetch(
                    clip,
                    frame,
                    lookahead_end,
                    &self.sources,
                    self.chunk_frames,
                    &self.required,
                    &mut self.prefetch,
                    prefetch_limit,
                );
            }
        }
        while let Ok(result) = self.results.try_recv() {
            if result.generation != self.generation.load(Ordering::Acquire) {
                continue;
            }
            self.pending.remove(&result.key);
            if self.required.contains(&result.key) || self.prefetch.contains(&result.key) {
                self.cache_chunk(result.key, result.samples);
            }
        }
        let mut complete = true;
        for index in 0..self.required.len() {
            let key = self.required[index];
            if let Some(chunk) = self.ready.get_mut(&key) {
                chunk.last_used = self.clock;
                if let Err(error) = &chunk.samples {
                    return Err(error.clone());
                }
            } else {
                complete = false;
                self.schedule(key)?;
            }
        }
        for index in 0..self.prefetch.len() {
            let key = self.prefetch[index];
            if !self.ready.contains_key(&key) {
                // A required decode already detects a disconnected decoder.
                // An unavailable speculative decode must not break a ready block.
                let _ = self.schedule(key);
            }
        }
        if !complete {
            return Ok(false);
        }
        for (index, (track, bus)) in self.tracks.iter().zip(output.iter_mut()).enumerate() {
            if self.suppressed[index]
                || !track.audible
                || (track.gain_left == 0.0 && track.gain_right == 0.0)
            {
                continue;
            }
            let (left, right) = bus.split_at_mut(1);
            for clip in &track.clips {
                if clip.muted || clip.volume == 0.0 {
                    continue;
                }
                let begin = frame.max(clip.start);
                let stop = end.min(clip.end);
                let mut position = begin;
                while position < stop {
                    let source_frame = clip.offset + position - clip.start;
                    if source_frame >= self.sources[clip.source].frames {
                        break;
                    }
                    let key = ChunkKey {
                        source: clip.source,
                        index: source_frame / self.chunk_frames,
                    };
                    let chunk_offset = (source_frame % self.chunk_frames) as usize;
                    let available = (self.chunk_frames - source_frame % self.chunk_frames)
                        .min(self.sources[clip.source].frames - source_frame)
                        .min(stop - position) as usize;
                    // Completeness was checked before mixing any bus.
                    let samples = self.ready[&key]
                        .samples
                        .as_ref()
                        .expect("ready chunk was checked");
                    for local in 0..available {
                        let timeline_frame = position + local as u64;
                        let relative = (timeline_frame - clip.start) as f64;
                        let mut gain = clip.volume;
                        if clip.fade_in_frames > 0.0 {
                            gain *= (relative / clip.fade_in_frames).clamp(0.0, 1.0);
                        }
                        if clip.fade_out_frames > 0.0 {
                            gain *= ((clip.end - timeline_frame) as f64 / clip.fade_out_frames)
                                .clamp(0.0, 1.0);
                        }
                        let out = (timeline_frame - frame) as usize;
                        let input = (chunk_offset + local) * 2;
                        left[0][out] += samples[input] * (gain * track.gain_left) as f32;
                        right[0][out] += samples[input + 1] * (gain * track.gain_right) as f32;
                    }
                    position += available as u64;
                }
            }
        }
        Ok(true)
    }

    fn schedule(&mut self, key: ChunkKey) -> Result<(), String> {
        if self.pending.contains(&key) || self.pending.len() >= CACHE_CHUNKS {
            return Ok(());
        }
        let job = DecodeJob {
            key,
            source: Arc::clone(&self.sources[key.source]),
            generation: self.generation.load(Ordering::Acquire),
        };
        let sender = self.jobs.as_ref().ok_or("Audio decoder pool is closed")?;
        match sender.try_send(job) {
            Ok(()) => {
                self.pending.insert(key);
                Ok(())
            }
            Err(TrySendError::Full(_)) => Ok(()),
            Err(TrySendError::Disconnected(_)) => {
                Err("Audio decoder workers are unavailable".into())
            }
        }
    }

    fn cache_chunk(&mut self, key: ChunkKey, samples: Result<Vec<f32>, String>) {
        if self.ready.len() >= CACHE_CHUNKS && !self.ready.contains_key(&key) {
            let victim = self
                .ready
                .iter()
                .filter(|(candidate, _)| {
                    !self.required.contains(candidate) && !self.prefetch.contains(candidate)
                })
                .min_by_key(|(_, chunk)| chunk.last_used)
                .map(|(candidate, _)| *candidate);
            if let Some(victim) = victim {
                self.ready.remove(&victim);
            } else {
                return;
            }
        }
        self.ready.insert(
            key,
            CachedChunk {
                samples,
                last_used: self.clock,
            },
        );
    }
}

impl Drop for Timeline {
    fn drop(&mut self) {
        self.shutdown.store(true, Ordering::Release);
        self.jobs.take();
        // Detached workers observe shutdown and terminate any active FFmpeg
        // child. Dropping a timeline never joins a decoder on the UI thread.
    }
}

fn clip_source_range(
    clip: &RuntimeClip,
    begin: u64,
    end: u64,
    source: &DecodeSource,
) -> Option<(u64, u64)> {
    if clip.muted || clip.volume == 0.0 {
        return None;
    }
    let begin = begin.max(clip.start);
    let end = end.min(clip.end);
    if begin >= end {
        return None;
    }
    let first = clip.offset + begin - clip.start;
    let last_exclusive = (clip.offset + end - clip.start).min(source.frames);
    (first < last_exclusive).then_some((first, last_exclusive))
}

fn collect_chunks(
    clip: &RuntimeClip,
    begin: u64,
    end: u64,
    sources: &[Arc<DecodeSource>],
    chunk_frames: u64,
    keys: &mut Vec<ChunkKey>,
    limit: usize,
) -> Result<(), String> {
    if let Some((first, last)) = clip_source_range(clip, begin, end, &sources[clip.source]) {
        for index in first / chunk_frames..=(last - 1) / chunk_frames {
            let key = ChunkKey {
                source: clip.source,
                index,
            };
            if !keys.contains(&key) {
                if keys.len() >= limit {
                    return Err(format!(
                        "This audio block requires more than {CACHE_CHUNKS} simultaneous source chunks; reduce overlapping sources"
                    ));
                }
                keys.push(key);
            }
        }
    }
    Ok(())
}

fn collect_prefetch(
    clip: &RuntimeClip,
    begin: u64,
    end: u64,
    sources: &[Arc<DecodeSource>],
    chunk_frames: u64,
    required: &[ChunkKey],
    keys: &mut Vec<ChunkKey>,
    limit: usize,
) {
    if let Some((first, last)) = clip_source_range(clip, begin, end, &sources[clip.source]) {
        for index in first / chunk_frames..=(last - 1) / chunk_frames {
            if keys.len() >= limit {
                break;
            }
            let key = ChunkKey {
                source: clip.source,
                index,
            };
            if !required.contains(&key) && !keys.contains(&key) {
                keys.push(key);
            }
        }
    }
}

fn decoder_worker(
    jobs: Receiver<DecodeJob>,
    results: Sender<DecodeResult>,
    ffmpeg: Arc<PathBuf>,
    sample_rate: u32,
    generation: Arc<AtomicU64>,
    shutdown: Arc<AtomicBool>,
) {
    while !shutdown.load(Ordering::Acquire) {
        let job = match jobs.recv_timeout(Duration::from_millis(50)) {
            Ok(job) => job,
            Err(crossbeam_channel::RecvTimeoutError::Timeout) => continue,
            Err(crossbeam_channel::RecvTimeoutError::Disconnected) => break,
        };
        if job.generation != generation.load(Ordering::Acquire) {
            continue;
        }
        let samples = decode_chunk(&ffmpeg, sample_rate, &job, &generation, &shutdown);
        if shutdown.load(Ordering::Acquire) || job.generation != generation.load(Ordering::Acquire)
        {
            continue;
        }
        let mut result = DecodeResult {
            key: job.key,
            generation: job.generation,
            samples,
        };
        loop {
            if shutdown.load(Ordering::Acquire)
                || job.generation != generation.load(Ordering::Acquire)
            {
                break;
            }
            match results.send_timeout(result, Duration::from_millis(50)) {
                Ok(()) => break,
                Err(crossbeam_channel::SendTimeoutError::Timeout(value)) => result = value,
                Err(crossbeam_channel::SendTimeoutError::Disconnected(_)) => return,
            }
        }
    }
}

fn decode_chunk(
    ffmpeg: &Path,
    sample_rate: u32,
    job: &DecodeJob,
    generation: &AtomicU64,
    shutdown: &AtomicBool,
) -> Result<Vec<f32>, String> {
    let chunk_frames = CHUNK_SECONDS * u64::from(sample_rate);
    let first = job
        .key
        .index
        .checked_mul(chunk_frames)
        .ok_or("Audio chunk position overflow")?;
    let frames = chunk_frames.min(job.source.frames.saturating_sub(first));
    if frames == 0 {
        return Ok(Vec::new());
    }
    // Warm the resampler with preceding audio before trimming at an exact
    // output sample. Independent chunk decodes otherwise reset its filter
    // history at each four-second boundary and can introduce audible seams.
    let preroll = first.min(u64::from(sample_rate) / 10);
    let mut command = Command::new(ffmpeg);
    command
        .args([
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-xerror",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-ss",
        ])
        .arg(format!(
            "{:.12}",
            (first - preroll) as f64 / f64::from(sample_rate)
        ))
        .arg("-i")
        .arg(&job.source.path)
        .args(["-map", "0:a:0", "-vn", "-sn", "-dn"]);
    let mut filter = String::new();
    if job.source.source_channels == 1 {
        // The default FFmpeg mono upmix attenuates both channels. The editor
        // treats mono as duplicated unity-gain left/right samples instead.
        filter.push_str("pan=stereo|c0=c0|c1=c0,");
    }
    filter.push_str(&format!(
        "aresample={sample_rate},atrim=start_sample={preroll},asetpts=PTS-STARTPTS"
    ));
    command
        .arg("-af")
        .arg(filter)
        // Match the editor preview downmix for multichannel files. Keep the
        // original channel count in the session; cached PCM has a stereo stride.
        .args(["-ac", "2", "-ar"])
        .arg(sample_rate.to_string())
        .arg("-t")
        .arg(format!("{:.12}", frames as f64 / f64::from(sample_rate)))
        .args(["-f", "f32le", "pipe:1"])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let mut child = command.spawn().map_err(|error| {
        format!(
            "Could not start FFmpeg for audio source {}: {error}",
            job.source.id
        )
    })?;
    let stdout = child
        .stdout
        .take()
        .ok_or("FFmpeg did not expose its audio output")?;
    let stderr = child
        .stderr
        .take()
        .ok_or("FFmpeg did not expose its error output")?;
    let maximum_bytes = frames as usize * 2 * size_of::<f32>();
    let audio_reader = match thread::Builder::new()
        .name("audio-decode-pipe".into())
        .spawn(move || {
            let mut bytes = Vec::with_capacity(maximum_bytes + 1);
            stdout
                .take(maximum_bytes as u64 + 1)
                .read_to_end(&mut bytes)
                .map(|_| bytes)
        }) {
        Ok(reader) => reader,
        Err(error) => {
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!("Could not read FFmpeg output: {error}"));
        }
    };
    let error_reader = match thread::Builder::new()
        .name("audio-decode-errors".into())
        .spawn(move || {
            let mut stderr = stderr;
            let mut saved = Vec::with_capacity(4096);
            let mut buffer = [0_u8; 2048];
            loop {
                match stderr.read(&mut buffer) {
                    Ok(0) | Err(_) => break,
                    Ok(count) => {
                        let retained = count.min(4096 - saved.len());
                        saved.extend_from_slice(&buffer[..retained]);
                    }
                }
            }
            String::from_utf8_lossy(&saved).trim().to_owned()
        }) {
        Ok(reader) => reader,
        Err(error) => {
            let _ = child.kill();
            let _ = child.wait();
            let _ = audio_reader.join();
            return Err(format!("Could not read FFmpeg errors: {error}"));
        }
    };
    let started = Instant::now();
    let status = loop {
        if shutdown.load(Ordering::Acquire) || job.generation != generation.load(Ordering::Acquire)
        {
            let _ = child.kill();
            let _ = child.wait();
            break Err("Audio decoding was cancelled".to_owned());
        }
        if started.elapsed() >= DECODE_TIMEOUT {
            let _ = child.kill();
            let _ = child.wait();
            break Err(format!(
                "Audio source {} exceeded the FFmpeg decode timeout",
                job.source.id
            ));
        }
        match child.try_wait() {
            Ok(Some(status)) => break Ok(status),
            Ok(None) => thread::sleep(Duration::from_millis(10)),
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                break Err(format!(
                    "Could not monitor FFmpeg for audio source {}: {error}",
                    job.source.id
                ));
            }
        }
    };
    let audio = audio_reader
        .join()
        .map_err(|_| "FFmpeg audio reader stopped unexpectedly".to_owned())?;
    let errors = error_reader.join().unwrap_or_default();
    let status = status?;
    if !status.success() {
        let detail = if errors.is_empty() {
            status.to_string()
        } else {
            errors
        };
        return Err(format!(
            "FFmpeg could not decode audio source {}: {detail}",
            job.source.id
        ));
    }
    let bytes =
        audio.map_err(|error| format!("Could not read audio source {}: {error}", job.source.id))?;
    if bytes.len() > maximum_bytes || bytes.len() % 8 != 0 {
        return Err(format!(
            "FFmpeg returned an invalid audio block for source {}",
            job.source.id
        ));
    }
    if bytes.is_empty() {
        return Err(format!(
            "Audio source {} ended before its declared duration",
            job.source.id
        ));
    }
    let mut samples = Vec::with_capacity(maximum_bytes / 4);
    for word in bytes.chunks_exact(4) {
        let value = f32::from_le_bytes([word[0], word[1], word[2], word[3]]);
        if !value.is_finite() {
            return Err(format!(
                "Audio source {} contains non-finite samples",
                job.source.id
            ));
        }
        samples.push(value);
    }
    // Container duration and resampling may differ at the final sample. Cache
    // the finite padded chunk once instead of repeatedly requesting EOF.
    samples.resize(maximum_bytes / 4, 0.0);
    Ok(samples)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn fixture(duration: f64) -> (tempfile::TempDir, SessionSpec) {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("source.wav");
        let mut writer = hound::WavWriter::create(
            &path,
            hound::WavSpec {
                channels: 1,
                sample_rate: 8000,
                bits_per_sample: 16,
                sample_format: hound::SampleFormat::Int,
            },
        )
        .unwrap();
        for _ in 0..frame_at(duration, 8000) {
            writer.write_sample(8192_i16).unwrap();
        }
        writer.finalize().unwrap();
        let spec = serde_json::from_value(json!({
            "id": "session", "name": "Session", "sources": [{
                "id": "source", "path": path, "name": "Audio", "duration": duration,
                "sampleRate": 8000, "channels": 1, "role": "external"
            }], "tracks": [{"id": "track", "sourceId": "source"}]
        }))
        .unwrap();
        (directory, spec)
    }

    fn output(tracks: usize, frames: usize) -> Vec<Vec<Vec<f32>>> {
        vec![vec![vec![9.0; frames], vec![9.0; frames]]; tracks]
    }

    fn cached(spec: SessionSpec, left: f32, right: f32) -> Timeline {
        let mut timeline = Timeline::new(spec, PathBuf::from("unused-ffmpeg"), 8000).unwrap();
        for source in 0..timeline.sources.len() {
            let frames = timeline.sources[source].frames.min(timeline.chunk_frames) as usize;
            let mut samples = Vec::with_capacity(frames * 2);
            for _ in 0..frames {
                samples.extend_from_slice(&[left, right]);
            }
            timeline.ready.insert(
                ChunkKey { source, index: 0 },
                CachedChunk {
                    samples: Ok(samples),
                    last_used: 0,
                },
            );
        }
        timeline
    }

    #[test]
    fn suppressed_monitor_tracks_skip_decode_prefetch_and_keep_saved_mute_unchanged() {
        let (_directory, spec) = fixture(1.0);
        let mut timeline = Timeline::new(spec.clone(), "unused-ffmpeg".into(), 8000).unwrap();
        timeline
            .set_monitor_mask(monitor_mask(&spec, &["track".into()]).unwrap())
            .unwrap();
        let mut buses = output(1, 32);
        assert!(timeline.render_dry(0, 32, &mut buses).unwrap());
        assert!(timeline.required.is_empty());
        assert!(timeline.prefetch.is_empty());
        assert!(timeline.pending.is_empty());
        assert!(
            buses
                .iter()
                .flatten()
                .flatten()
                .all(|sample| *sample == 0.0)
        );
        assert!(!timeline.spec().tracks[0].muted);
        assert_eq!(
            serde_json::to_value(timeline.spec()).unwrap(),
            serde_json::to_value(spec).unwrap()
        );
    }

    #[test]
    fn monitor_survives_mix_update_and_clear_restores_cached_audio() {
        let (_directory, spec) = fixture(1.0);
        let mut timeline = cached(spec.clone(), 0.25, -0.5);
        timeline
            .set_monitor_mask(monitor_mask(&spec, &["track".into()]).unwrap())
            .unwrap();
        let mut next = spec;
        next.tracks[0].volume = 0.5;
        let prepared = prepare_mix_update(next.clone(), 8000).unwrap();
        timeline.apply_mix_update(prepared).unwrap();
        let mut buses = output(1, 32);
        assert!(timeline.render_dry(0, 32, &mut buses).unwrap());
        assert!(
            buses
                .iter()
                .flatten()
                .flatten()
                .all(|sample| *sample == 0.0)
        );
        timeline
            .set_monitor_mask(monitor_mask(&next, &[]).unwrap())
            .unwrap();
        assert!(timeline.render_dry(0, 32, &mut buses).unwrap());
        assert!(buses[0][0].iter().all(|sample| *sample == 0.125));
        assert!(buses[0][1].iter().all(|sample| *sample == -0.25));
        assert!(!timeline.spec().tracks[0].muted);
    }

    #[test]
    fn monitor_ids_reject_unknown_duplicate_and_oversized_lists() {
        let (_directory, spec) = fixture(1.0);
        assert_eq!(monitor_mask(&spec, &[]).unwrap(), vec![false]);
        assert_eq!(monitor_mask(&spec, &["track".into()]).unwrap(), vec![true]);
        assert!(monitor_mask(&spec, &["missing".into()]).is_err());
        assert!(monitor_mask(&spec, &["track".into(), "track".into()]).is_err());
        assert!(monitor_mask(&spec, &vec!["track".into(); 257]).is_err());
    }

    #[test]
    fn legacy_defaults_unknown_metadata_and_duration_include_muted_clips() {
        let (_directory, mut spec) = fixture(1.0);
        spec.tracks[0].muted = true;
        spec.tracks[0].clips = Some(vec![ClipSpec {
            id: "clip".into(),
            asset_id: "source".into(),
            start: 3.0,
            offset: 0.0,
            duration: 1.0,
            volume: 1.0,
            fade_in: 0.0,
            fade_out: 0.0,
            muted: true,
            locked: false,
        }]);
        assert_eq!(spec.master_volume, 1.0);
        assert_eq!(spec.tracks[0].volume, 1.0);
        assert_eq!(spec.duration(), 4.0);
        assert!(spec.validate().is_ok());
        spec.tracks[0].clips = Some(Vec::new());
        assert_eq!(spec.duration(), 1.0);
    }

    #[test]
    fn validation_rejects_nonfinite_ranges_duplicate_ids_and_bad_references() {
        let (_directory, original) = fixture(1.0);
        let mut spec = original.clone();
        spec.master_pan = f64::NAN;
        assert!(spec.validate().unwrap_err().contains("finite"));
        spec = original.clone();
        spec.tracks.push(spec.tracks[0].clone());
        assert!(spec.validate().unwrap_err().contains("Duplicate track"));
        spec = original.clone();
        spec.sources.push(spec.sources[0].clone());
        assert!(spec.validate().unwrap_err().contains("Duplicate source"));
        spec = original;
        spec.tracks[0].source_id = "unknown".into();
        assert!(spec.validate().unwrap_err().contains("unknown source"));
    }

    #[test]
    fn validation_blocks_active_offline_sources_or_invalid_audio_metadata() {
        let (_directory, mut spec) = fixture(1.0);
        spec.sources[0].missing = true;
        assert!(spec.validate().unwrap_err().contains("offline"));
        spec.tracks[0].muted = true;
        assert!(spec.validate().is_ok());
        spec.tracks[0].muted = false;
        spec.sources[0].missing = false;
        for channels in [0, 65] {
            spec.sources[0].channels = channels;
            assert!(spec.validate().unwrap_err().contains("channel-count"));
        }
        spec.sources[0].channels = 2;
        spec.sources[0].sample_rate = 0;
        assert!(spec.validate().unwrap_err().contains("sample-rate"));
        spec.sources[0].sample_rate = 8000;
        spec.sources[0].path = "relative.wav".into();
        assert!(spec.validate().unwrap_err().contains("absolute file path"));
    }

    #[test]
    fn multichannel_source_metadata_survives_compile_and_invalidates_changed_cache() {
        let (_directory, mut spec) = fixture(0.01);
        spec.sources[0].channels = 6;
        let mut timeline = cached(spec.clone(), 0.25, -0.5);
        assert_eq!(timeline.spec().sources[0].channels, 6);
        assert_eq!(timeline.sources[0].source_channels, 6);
        spec.tracks[0].volume = 0.5;
        timeline
            .apply_mix_update(prepare_mix_update(spec.clone(), 8000).unwrap())
            .unwrap();
        let mut buses = output(1, 8);
        assert!(timeline.render_dry(0, 8, &mut buses).unwrap());
        assert_eq!(buses[0][0], vec![0.125; 8]);
        assert_eq!(buses[0][1], vec![-0.25; 8]);
        assert_eq!(timeline.ready.len(), 1);
        spec.sources[0].channels = 8;
        assert!(timeline
            .apply_mix_update(prepare_mix_update(spec.clone(), 8000).unwrap())
            .is_err());
        timeline.update(spec).unwrap();
        assert_eq!(timeline.spec().sources[0].channels, 8);
        assert_eq!(timeline.sources[0].source_channels, 8);
        assert!(timeline.ready.is_empty());
        assert!(timeline.pending.is_empty());
        assert_eq!(timeline.generation.load(Ordering::Acquire), 1);
    }

    #[test]
    fn legacy_built_in_effect_fields_are_ignored() {
        let (_directory, spec) = fixture(0.01);
        let mut value = serde_json::to_value(&spec).unwrap();
        value["tracks"][0]["effects"] = json!({"delay":1.0,"delayTime":1.2,"compressor":7});
        let legacy: SessionSpec = serde_json::from_value(value).unwrap();
        assert!(legacy.validate().is_ok());
        assert!(prepare_mix_update(legacy.clone(), 48000).is_ok());
        assert!(
            serde_json::to_value(&legacy).unwrap()["tracks"][0]
                .get("effects")
                .is_none()
        );
    }

    #[test]
    fn plugin_instance_ids_are_global_and_bypassed_unknown_formats_are_preserved() {
        let (_directory, mut spec) = fixture(1.0);
        let slot = PluginSlot {
            id: "instance".into(),
            format: "aax".into(),
            plugin_id: "plugin".into(),
            name: "Effect".into(),
            path: String::new(),
            bypassed: true,
            skip_load: false,
            state: Some("state".into()),
            bypass_warning: None,
        };
        spec.plugin_chain.push(slot.clone());
        assert!(spec.validate().is_ok());
        spec.tracks[0].plugin_chain.push(slot);
        assert!(
            spec.validate()
                .unwrap_err()
                .contains("Duplicate plugin instance")
        );
        spec.tracks[0].plugin_chain.clear();
        spec.plugin_chain[0].bypassed = false;
        assert!(spec.validate().unwrap_err().contains("unsupported format"));
    }

    #[test]
    fn native_formats_validate_platform_specific_references_and_keep_foreign_bypasses() {
        let (directory, mut spec) = fixture(1.0);
        spec.plugin_chain.push(PluginSlot {
            id: "legacy".into(),
            format: "vst2".into(),
            plugin_id: "vst2:12345678".into(),
            name: "Effect".into(),
            path: directory
                .path()
                .join("Effect.dll")
                .to_string_lossy()
                .into_owned(),
            bypassed: false,
            skip_load: false,
            state: None,
            bypass_warning: None,
        });
        assert!(spec.validate().is_ok());
        spec.plugin_chain[0].path = "relative.dll".into();
        assert!(spec.validate().unwrap_err().contains("plugin location"));
        let slot = &mut spec.plugin_chain[0];
        slot.format = "au".into();
        slot.plugin_id = "au:aufx:dely:appl".into();
        slot.path = slot.plugin_id.clone();
        assert_eq!(spec.validate().is_ok(), cfg!(target_os = "macos"));
        spec.plugin_chain[0].bypassed = true;
        spec.plugin_chain[0].path = "au:foreign-location".into();
        assert!(spec.validate().is_ok());
    }

    #[test]
    fn mix_applies_track_gain_pan_and_overlapping_clip_gains_without_master_gain() {
        let (_directory, mut spec) = fixture(0.01);
        spec.master_volume = 0.0;
        spec.master_pan = -1.0;
        spec.tracks[0].volume = 0.5;
        spec.tracks[0].pan = 0.5;
        let clip = |id: &str, start, offset, duration, volume| ClipSpec {
            id: id.into(),
            asset_id: "source".into(),
            start,
            offset,
            duration,
            volume,
            fade_in: 0.0,
            fade_out: 0.0,
            muted: false,
            locked: false,
        };
        spec.tracks[0].clips = Some(vec![
            clip("first", 0.0, 0.0, 4.0 / 8000.0, 1.0),
            clip("second", 2.0 / 8000.0, 4.0 / 8000.0, 4.0 / 8000.0, 2.0),
        ]);
        let mut timeline = cached(spec, 1.0, 2.0);
        let mut buses = output(1, 8);
        assert!(timeline.render_dry(0, 8, &mut buses).unwrap());
        assert_eq!(
            buses[0][0],
            vec![0.25, 0.25, 0.75, 0.75, 0.5, 0.5, 0.0, 0.0]
        );
        assert_eq!(buses[0][1], vec![1.0, 1.0, 3.0, 3.0, 2.0, 2.0, 0.0, 0.0]);
    }

    #[test]
    fn fades_multiply_when_overlapping_and_seek_uses_source_offset() {
        let (_directory, mut spec) = fixture(0.01);
        spec.tracks[0].clips = Some(vec![ClipSpec {
            id: "clip".into(),
            asset_id: "source".into(),
            start: 2.0 / 8000.0,
            offset: 4.0 / 8000.0,
            duration: 8.0 / 8000.0,
            volume: 1.0,
            fade_in: 8.0 / 8000.0,
            fade_out: 8.0 / 8000.0,
            muted: false,
            locked: false,
        }]);
        let mut timeline = cached(spec, 1.0, 1.0);
        // A source ramp makes offset/seek mistakes observable.
        let samples = timeline
            .ready
            .get_mut(&ChunkKey {
                source: 0,
                index: 0,
            })
            .unwrap()
            .samples
            .as_mut()
            .unwrap();
        for (frame, channels) in samples.chunks_exact_mut(2).enumerate() {
            channels.fill(frame as f32);
        }
        let mut buses = output(1, 4);
        assert!(timeline.render_dry(4, 4, &mut buses).unwrap());
        assert_eq!(
            buses[0][0],
            vec![
                6.0 * 0.25 * 0.75,
                7.0 * 0.375 * 0.625,
                8.0 * 0.5 * 0.5,
                9.0 * 0.625 * 0.375
            ]
        );
        assert_eq!(buses[0][0], buses[0][1]);
    }

    #[test]
    fn mute_solo_and_clip_mute_produce_silent_buses() {
        let (_directory, mut spec) = fixture(0.01);
        let mut second = spec.tracks[0].clone();
        second.id = "second".into();
        second.solo = true;
        spec.tracks.push(second);
        let mut third = spec.tracks[1].clone();
        third.id = "third".into();
        third.muted = true;
        spec.tracks.push(third);
        let mut timeline = cached(spec, 1.0, 1.0);
        let mut buses = output(3, 4);
        assert!(timeline.render_dry(0, 4, &mut buses).unwrap());
        assert_eq!(buses[0][0], vec![0.0; 4]);
        assert_eq!(buses[1][0], vec![1.0; 4]);
        assert_eq!(buses[2][0], vec![0.0; 4]);
    }

    #[test]
    fn missing_decode_never_leaks_a_partially_mixed_block() {
        let (_directory, mut spec) = fixture(0.01);
        let mut second_source = spec.sources[0].clone();
        second_source.id = "second-source".into();
        spec.sources.push(second_source);
        let mut second_track = spec.tracks[0].clone();
        second_track.id = "second-track".into();
        second_track.source_id = "second-source".into();
        spec.tracks.push(second_track);
        let mut timeline = cached(spec, 1.0, 1.0);
        timeline.ready.remove(&ChunkKey {
            source: 1,
            index: 0,
        });
        let mut buses = output(2, 4);
        assert!(!timeline.render_dry(0, 4, &mut buses).unwrap());
        assert!(
            buses
                .iter()
                .flatten()
                .flatten()
                .all(|sample| *sample == 0.0)
        );
    }

    #[test]
    fn cache_is_bounded_and_current_chunks_cannot_be_evicted() {
        let (_directory, spec) = fixture(0.01);
        let mut timeline = cached(spec, 1.0, 1.0);
        let pinned = ChunkKey {
            source: 0,
            index: 0,
        };
        timeline.required.push(pinned);
        for index in 1..(CACHE_CHUNKS as u64 * 3) {
            timeline.clock = index;
            timeline.cache_chunk(ChunkKey { source: 0, index }, Ok(vec![1.0, 1.0]));
            assert!(timeline.ready.len() <= CACHE_CHUNKS);
            assert!(timeline.ready.contains_key(&pinned));
        }
        assert_eq!(timeline.ready.len(), CACHE_CHUNKS);
    }

    #[test]
    fn mixer_updates_preserve_cached_audio_and_source_changes_invalidate_old_jobs() {
        let (_directory, spec) = fixture(0.01);
        let mut timeline = cached(spec.clone(), 1.0, 1.0);
        let mut next = spec;
        next.tracks[0].volume = 0.25;
        timeline.update(next.clone()).unwrap();
        let mut buses = output(1, 8);
        assert!(timeline.render_dry(0, 8, &mut buses).unwrap());
        assert_eq!(buses[0][0], vec![0.25; 8]);
        assert_eq!(timeline.ready.len(), 1);
        assert_eq!(timeline.generation.load(Ordering::Acquire), 0);
        next.sources[0].sample_rate = 16000;
        timeline.update(next).unwrap();
        assert!(timeline.ready.is_empty());
        assert!(timeline.pending.is_empty());
        assert_eq!(timeline.generation.load(Ordering::Acquire), 1);
    }

    #[test]
    fn prepared_update_does_not_revalidate_files_and_preserves_decoded_cache() {
        let (_directory, spec) = fixture(0.01);
        let mut timeline = cached(spec.clone(), 1.0, 1.0);
        let mut next = spec;
        next.tracks[0].volume = 0.5;
        let prepared = prepare_mix_update(next.clone(), 8000).unwrap();
        // Control-thread validation already ran. Rendering must not perform
        // filesystem calls or fail after a file disappears behind cached audio.
        std::fs::remove_file(&next.sources[0].path).unwrap();
        timeline.apply_mix_update(prepared).unwrap();
        let mut buses = output(1, 8);
        assert!(timeline.render_dry(0, 8, &mut buses).unwrap());
        assert_eq!(buses[0][0], vec![0.5; 8]);
        assert_eq!(timeline.ready.len(), 1);
    }

    #[test]
    fn prepared_update_rejects_source_or_output_rate_changes_without_mutating_timeline() {
        let (_directory, spec) = fixture(0.01);
        let mut timeline = cached(spec.clone(), 1.0, 1.0);
        let mut next = spec.clone();
        next.sources[0].sample_rate = 16000;
        let prepared = prepare_mix_update(next, 8000).unwrap();
        assert!(
            timeline
                .apply_mix_update(prepared)
                .unwrap_err()
                .contains("reload")
        );
        let prepared = prepare_mix_update(spec, 16000).unwrap();
        assert!(
            timeline
                .apply_mix_update(prepared)
                .unwrap_err()
                .contains("reload")
        );
        assert_eq!(timeline.spec().sources[0].sample_rate, 8000);
        assert_eq!(timeline.ready.len(), 1);
    }

    #[test]
    fn zero_track_gain_does_not_request_audio_or_read_an_absent_cache_entry() {
        let (_directory, mut spec) = fixture(0.01);
        spec.tracks[0].volume = 0.0;
        let mut timeline = Timeline::new(spec, PathBuf::from("unused-ffmpeg"), 8000).unwrap();
        let mut buses = output(1, 8);
        assert!(timeline.render_dry(0, 8, &mut buses).unwrap());
        assert!(timeline.pending.is_empty());
        assert_eq!(buses[0][0], vec![0.0; 8]);
    }

    #[test]
    fn decoder_failure_is_retained_and_does_not_start_an_infinite_retry_loop() {
        let (_directory, spec) = fixture(0.01);
        let mut timeline =
            Timeline::new(spec, PathBuf::from("pymss-missing-ffmpeg-executable"), 8000).unwrap();
        let mut buses = output(1, 8);
        assert!(!timeline.render_dry(0, 8, &mut buses).unwrap());
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            match timeline.render_dry(0, 8, &mut buses) {
                Ok(false) => {
                    assert!(Instant::now() < deadline);
                    thread::sleep(Duration::from_millis(10));
                }
                Err(error) => {
                    assert!(error.contains("Could not start FFmpeg"));
                    break;
                }
                Ok(true) => panic!("A missing decoder must not produce a successful block"),
            }
        }
        assert!(timeline.pending.is_empty());
        assert!(timeline.render_dry(0, 8, &mut buses).is_err());
        assert!(timeline.pending.is_empty());
        assert_eq!(timeline.ready.len(), 1);
    }

    #[test]
    fn ffmpeg_decode_is_async_duplicates_mono_and_reuses_ready_chunk() {
        let ffmpeg = std::env::var_os("PYMSS_TEST_FFMPEG")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("ffmpeg"));
        if Command::new(&ffmpeg)
            .arg("-version")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .is_err()
        {
            eprintln!(
                "FFmpeg is unavailable; set PYMSS_TEST_FFMPEG to enable decoder integration coverage"
            );
            return;
        }
        let (_directory, spec) = fixture(0.02);
        let mut timeline = Timeline::new(spec, ffmpeg, 8000).unwrap();
        let mut buses = output(1, 16);
        assert!(!timeline.render_dry(0, 16, &mut buses).unwrap());
        let deadline = Instant::now() + Duration::from_secs(15);
        while !timeline.render_dry(0, 16, &mut buses).unwrap() {
            assert!(
                Instant::now() < deadline,
                "FFmpeg decoder did not become ready"
            );
            thread::sleep(Duration::from_millis(10));
        }
        assert!(
            buses[0][0]
                .iter()
                .all(|sample| (*sample - 0.25).abs() < 1e-6)
        );
        assert_eq!(buses[0][0], buses[0][1]);
        assert!(timeline.pending.is_empty());
        assert!(timeline.render_dry(16, 16, &mut buses).unwrap());
        assert!(timeline.pending.is_empty());
        assert_eq!(timeline.ready.len(), 1);
    }

    #[test]
    fn ffmpeg_multichannel_decode_matches_stereo_downmix_across_chunk_boundary() {
        let ffmpeg = std::env::var_os("PYMSS_TEST_FFMPEG")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("ffmpeg"));
        if Command::new(&ffmpeg)
            .arg("-version")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .is_err()
        {
            eprintln!("FFmpeg is unavailable; skipping multichannel decoder coverage");
            return;
        }
        for (channels, layout) in [(6, "5.1(side)"), (8, "7.1")] {
            let (_directory, mut spec) = fixture(4.01);
            spec.sources[0].channels = channels;
            spec.sources[0].sample_rate = 48000;
            let expressions = (0..channels)
                .map(|channel| format!("0.04*sin(2*PI*{}*t)", 233 + channel * 71))
                .collect::<Vec<_>>()
                .join("|");
            let created = Command::new(&ffmpeg)
                .args(["-v", "error", "-y", "-f", "lavfi", "-i"])
                .arg(format!("aevalsrc={expressions}:s=48000:d=4.01:c={layout}"))
                .args(["-c:a", "pcm_f32le"])
                .arg(&spec.sources[0].path)
                .output()
                .unwrap();
            assert!(created.status.success(), "{}", String::from_utf8_lossy(&created.stderr));
            let reference = Command::new(&ffmpeg)
                .args(["-v", "error", "-i"])
                .arg(&spec.sources[0].path)
                .args(["-ac", "2", "-ar", "48000", "-f", "f32le", "pipe:1"])
                .output()
                .unwrap();
            assert!(reference.status.success());
            let reference = reference.stdout
                .chunks_exact(4)
                .map(|word| f32::from_le_bytes(word.try_into().unwrap()))
                .collect::<Vec<_>>();
            let mut timeline = Timeline::new(spec.clone(), ffmpeg.clone(), 48000).unwrap();
            let mut buses = output(1, 16);
            for frame in [0, 4 * 48000 - 8] {
                let deadline = Instant::now() + Duration::from_secs(15);
                while !timeline.render_dry(frame, 16, &mut buses).unwrap() {
                    assert!(Instant::now() < deadline, "Multichannel decode timed out");
                    thread::sleep(Duration::from_millis(10));
                }
                for channel in 0..2 {
                    for offset in 0..16 {
                        let expected = reference[(frame as usize + offset) * 2 + channel];
                        assert!((buses[0][channel][offset] - expected).abs() < 1e-6,
                            "Incorrect {layout} downmix at frame {frame}, channel {channel}");
                    }
                }
            }
            assert_eq!(timeline.spec().sources[0].channels, channels);
            assert_eq!(timeline.ready.len(), 2);
            spec.tracks[0].volume = 0.5;
            timeline.apply_mix_update(prepare_mix_update(spec, 48000).unwrap()).unwrap();
            assert!(timeline.render_dry(0, 16, &mut buses).unwrap());
            assert!(timeline.pending.is_empty());
            for channel in 0..2 {
                for offset in 0..16 {
                    assert!((buses[0][channel][offset] - reference[offset * 2 + channel] * 0.5).abs() < 1e-6);
                }
            }
        }
    }

    #[test]
    fn ffmpeg_decodes_across_chunk_boundaries_and_seek_reuses_only_matching_source_frames() {
        let ffmpeg = std::env::var_os("PYMSS_TEST_FFMPEG")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("ffmpeg"));
        if Command::new(&ffmpeg)
            .arg("-version")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .is_err()
        {
            return;
        }
        let (_directory, spec) = fixture(8.01);
        let mut writer = hound::WavWriter::create(
            &spec.sources[0].path,
            hound::WavSpec {
                channels: 1,
                sample_rate: 8000,
                bits_per_sample: 16,
                sample_format: hound::SampleFormat::Int,
            },
        )
        .unwrap();
        for frame in 0..frame_at(spec.sources[0].duration, 8000) {
            writer
                .write_sample(((frame % 20_000) as i32 - 10_000) as i16)
                .unwrap();
        }
        writer.finalize().unwrap();
        let mut timeline = Timeline::new(spec, ffmpeg, 8000).unwrap();
        let mut buses = output(1, 16);
        let frame = 4 * 8000 - 8;
        assert!(!timeline.render_dry(frame, 16, &mut buses).unwrap());
        assert_eq!(timeline.required.len(), 2);
        let deadline = Instant::now() + Duration::from_secs(15);
        while !timeline.render_dry(frame, 16, &mut buses).unwrap() {
            assert!(
                Instant::now() < deadline,
                "Cross-boundary decode did not become ready"
            );
            thread::sleep(Duration::from_millis(10));
        }
        for (index, sample) in buses[0][0].iter().enumerate() {
            let expected = (((frame + index as u64) % 20_000) as f32 - 10_000.0) / 32768.0;
            assert!((*sample - expected).abs() < 1e-6);
        }
        timeline.clear_pending();
        let final_frame = 8 * 8000;
        while !timeline.render_dry(final_frame, 16, &mut buses).unwrap() {
            assert!(
                Instant::now() < deadline,
                "Seek decode did not become ready"
            );
            thread::sleep(Duration::from_millis(10));
        }
        for (index, sample) in buses[0][0].iter().enumerate() {
            let expected = (((final_frame + index as u64) % 20_000) as f32 - 10_000.0) / 32768.0;
            assert!((*sample - expected).abs() < 1e-6);
        }
        assert_eq!(buses[0][0], buses[0][1]);
        assert!(timeline.ready.len() <= CACHE_CHUNKS);
    }
}
