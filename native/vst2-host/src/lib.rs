//! Effect-only VST 2.4 adapter for the isolated Pymss audio-host process.
//!
//! The small ABI boundary follows RustAudio's published VST 2.4 definitions, rather than its
//! deprecated host loader (whose callback mutex is unsuitable for our processing thread).
//! Native code remains trusted inside the helper process. This crate cannot make a corrupt
//! native plugin memory-safe; scanning must run in the separate scan helper.
//! Editors use HWND/NSView or an X11 Window ID on Linux. XWayland requires DISPLAY; native
//! Wayland and the old Linux _XEventProc/shared-Display extension are not hosted. Plugins drive
//! their own toolkit connection from effEditIdle; the shared window host pumps XEmbed events.
//! VST2 shells enumerate bounded child IDs/names without opening every child. A child is
//! selected through per-instance audioMasterCurrentId; actual identity and effect support are
//! checked on creation. Shell categories remain unknown in scan metadata until then.
#![deny(unsafe_code)]

#[allow(unsafe_code)]
mod ffi;

use soundcraft_dsp::{Plugin, PluginEditor, PluginInfo, PluginStateReader, Transport};
use std::path::{Path, PathBuf};
use std::sync::Arc;

/// One standalone effect or shell child exported by a VST2 binary.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct Vst2Descriptor {
    pub id: String,
    pub name: String,
    pub vendor: String,
    pub version: String,
    pub path: PathBuf,
    /// Raw effGetPlugCategory value; shell children remain unknown until instantiated.
    #[serde(default)]
    pub raw_category: Option<i32>,
    /// A shell's lightweight enumeration does not reveal the child's category.
    pub is_instrument: Option<bool>,
    #[serde(default)]
    pub is_shell_child: bool,
}

/// Parse an eight-digit VST2 identifier without loading its binary.
pub fn parse_id(id: &str) -> Option<i32> {
    let hex = id.strip_prefix("vst2:")?;
    (hex.len() == 8 && hex.bytes().all(|byte| byte.is_ascii_hexdigit()))
        .then(|| u32::from_str_radix(hex, 16).ok().map(|id| id as i32))?
}

/// Inspect one library/bundle. Shell children are listed without instantiating each child.
pub fn scan_binaries(path: &Path) -> Result<Vec<Vst2Descriptor>, String> {
    let instance = ffi::Instance::load(path)?;
    let descriptors = instance.scan_descriptors(path);
    drop(instance);
    pump_ui();
    descriptors
}

pub fn instantiate_descriptor(descriptor: &Vst2Descriptor) -> Result<Vst2Plugin, String> {
    let selected = parse_id(&descriptor.id).ok_or("The VST2 plugin identifier is invalid")?;
    let instance = ffi::Instance::load_selected(&descriptor.path, selected)?;
    let actual = instance.descriptor(&descriptor.path);
    if parse_id(&actual.id) != Some(selected) {
        return Err("The VST2 binary exports a different plugin identifier".into());
    }
    instance.validate_effect()?;
    let info = instance.plugin_info()?;
    let pending = instance.shared();
    Ok(Vst2Plugin {
        instance,
        info,
        pending,
        inputs: Vec::new(),
        outputs: Vec::new(),
        max_block: 0,
        channels: 2,
        latency: 0,
        tail: 0,
        transport: Transport::default(),
        failure: None,
    })
}

/// Release retired instances/windows on their creating thread. Call in the helper UI loop.
pub fn pump_ui() {
    ffi::pump_ui();
}

/// Drain retired instances after dropping the audio graph and before leaving the UI loop.
pub fn shutdown() {
    pump_ui();
}

/// A floating-point VST2 effect with a mono or stereo main signal. Input pins beyond the main
/// pair carry a sidechain and receive silence. Parameter values use normalized 0..1 ranges.
pub struct Vst2Plugin {
    instance: ffi::Instance,
    info: &'static PluginInfo,
    pending: Arc<ffi::Shared>,
    inputs: Vec<Vec<f32>>,
    outputs: Vec<Vec<f32>>,
    max_block: usize,
    channels: usize,
    latency: usize,
    tail: usize,
    transport: Transport,
    failure: Option<String>,
}

impl Vst2Plugin {
    /// Select the processing callback's realtime/offline level while audio processing is stopped.
    /// The same instance, state, parameters and editor remain attached; VST2 batch-file/offline
    /// interfaces are not used. May be called before the first prepare.
    pub fn set_offline(&mut self, offline: bool) -> Result<(), String> {
        self.instance.set_offline(offline)
    }

    pub fn is_offline(&self) -> bool {
        self.instance.is_offline()
    }

    pub fn try_prepare(
        &mut self,
        sample_rate: f32,
        max_block: usize,
        channels: usize,
    ) -> Result<(), String> {
        if !sample_rate.is_finite()
            || !(8_000.0..=768_000.0).contains(&sample_rate)
            || !(1..=65_536).contains(&max_block)
            || !(1..=2).contains(&channels)
        {
            return Err(
                "VST2 requires a valid sample rate, block size, and mono/stereo layout".into(),
            );
        }
        self.max_block = 0;
        self.instance.prepare(sample_rate, max_block)?;
        let (inputs, outputs) = self.instance.channels();
        self.inputs = vec![vec![0.0; max_block]; inputs];
        self.outputs = vec![vec![0.0; max_block]; outputs];
        self.channels = channels;
        self.max_block = max_block;
        self.latency = self.instance.latency();
        self.tail = self.instance.tail();
        self.failure = None;
        Ok(())
    }

    pub fn try_process(&mut self, io: &mut [Vec<f32>], frames: usize) -> Result<(), String> {
        if self.max_block == 0 {
            return Err("The VST2 plugin is not prepared".into());
        }
        self.instance.check_layout()?;
        let channels = self.channels.min(io.len());
        let frames = io
            .iter()
            .take(channels)
            .map(Vec::len)
            .fold(frames, usize::min);
        if channels == 0 || frames == 0 {
            return Ok(());
        }
        self.instance.flush_pending();
        let mut offset = 0;
        while offset < frames {
            let count = (frames - offset).min(self.max_block);
            let (inputs, outputs) = self.instance.channels();
            if inputs == 1 && channels >= 2 {
                for i in 0..count {
                    self.inputs[0][i] = 0.5 * (io[0][offset + i] + io[1][offset + i]);
                }
            } else {
                for (channel, pin) in self.inputs.iter_mut().enumerate().take(inputs) {
                    if channel < ffi::MAIN_PINS {
                        pin[..count]
                            .copy_from_slice(&io[channel.min(channels - 1)][offset..offset + count]);
                    } else {
                        pin[..count].fill(0.0);
                    }
                }
            }
            for channel in self.outputs.iter_mut().take(outputs) {
                channel[..count].fill(0.0);
            }
            self.instance
                .process(&self.inputs, &mut self.outputs, count, &self.transport);
            for (channel, destination) in io.iter_mut().take(channels).enumerate() {
                let destination = &mut destination[offset..offset + count];
                if channels == 1 && outputs == 2 {
                    for (i, sample) in destination.iter_mut().enumerate() {
                        *sample = 0.5 * (self.outputs[0][i] + self.outputs[1][i]);
                    }
                } else {
                    destination.copy_from_slice(&self.outputs[channel.min(outputs - 1)][..count]);
                }
            }
            self.transport.samples = self.transport.samples.saturating_add(count as i64);
            self.transport.seconds += count as f64 / self.instance.sample_rate();
            self.transport.quarters +=
                count as f64 / self.instance.sample_rate() * self.transport.tempo / 60.0;
            offset += count;
        }
        self.refresh_latency();
        self.instance.check_layout()
    }

    pub fn flush_pending(&mut self) -> Result<(), String> {
        self.instance.check_layout()?;
        self.instance.flush_pending();
        self.refresh_latency();
        Ok(())
    }

    pub fn refresh_latency(&mut self) -> usize {
        self.latency = self.instance.latency();
        self.latency
    }

    /// Refresh timing properties while processing is stopped. Tail length may change after
    /// a parameter/preset edit without an audioMasterIOChanged notification.
    pub fn refresh_latency_control(&mut self) -> usize {
        self.latency = self.instance.latency_control();
        self.tail = self.instance.tail();
        self.latency
    }

    pub fn queue_normalized(&mut self, id: &str, normalized: f64, plain: f32) -> bool {
        if !normalized.is_finite() || !plain.is_finite() {
            return false;
        }
        self.pending.queue(id, normalized as f32)
    }
}

impl Plugin for Vst2Plugin {
    fn info(&self) -> &'static PluginInfo {
        self.info
    }

    fn prepare(&mut self, sample_rate: f32, max_block: usize, channels: usize) {
        self.failure = self.try_prepare(sample_rate, max_block, channels).err();
    }

    fn reset(&mut self) {
        self.instance.reset();
        self.transport.samples = 0;
        self.transport.seconds = 0.0;
        self.transport.quarters = 0.0;
    }

    fn set_param(&mut self, id: &str, value: f32) -> bool {
        self.pending.queue(id, value)
    }

    fn param(&self, id: &str) -> Option<f32> {
        self.pending.value(id)
    }

    fn process(&mut self, io: &mut [Vec<f32>], frames: usize) {
        self.failure = self.try_process(io, frames).err();
    }

    fn latency(&self) -> usize {
        self.latency
    }
    fn tail_samples(&self) -> usize {
        self.tail
    }
    fn set_transport(&mut self, transport: &Transport) {
        self.transport = *transport;
    }

    fn save_state(&mut self) -> Option<Vec<u8>> {
        self.instance.flush_pending();
        self.instance.save_state()
    }

    fn load_state(&mut self, data: &[u8]) -> bool {
        self.instance.load_state(data)
    }

    fn open_editor(&mut self) -> Result<(), String> {
        self.instance.open_editor(self.info.name)
    }
    fn close_editor(&mut self) {
        self.instance.close_editor();
    }

    fn editor(&mut self) -> Option<Box<dyn PluginEditor>> {
        Some(self.instance.editor(self.info.name))
    }

    fn state_reader(&mut self) -> Option<Box<dyn PluginStateReader>> {
        Some(self.instance.state_reader())
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn plugin_ids_preserve_signed_abi_bits_and_reject_malformed_identifiers() {
        assert_eq!(super::parse_id("vst2:12345678"), Some(0x1234_5678));
        assert_eq!(
            super::parse_id("vst2:87654321"),
            Some(0x8765_4321u32 as i32)
        );
        assert_eq!(super::parse_id("vst2:FFFFFFFF"), Some(-1));
        for malformed in [
            "vst2:",
            "vst2:1234567",
            "vst2:123456789",
            "vst2:Z2345678",
            "vst2:+1234567",
            "vst3:12345678",
            "VST2:12345678",
        ] {
            assert_eq!(super::parse_id(malformed), None);
        }
    }
}
