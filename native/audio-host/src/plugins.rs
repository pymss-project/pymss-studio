use crate::timeline::PluginSlot;
use soundcraft_dsp::{Plugin, PluginEditor, PluginInfo, PluginStateReader, Transport};
use std::path::Path;

/// Format-specific ownership stays behind this boundary; the graph uses one processing,
/// state and latency contract for every supported effect.
pub(crate) enum HostedPlugin {
    Vst3(soundcraft_vst3_host::Vst3Plugin),
    Vst2(pymss_vst2_host::Vst2Plugin),
    Au(soundcraft_au_host::AuPlugin),
}

macro_rules! dispatch {
    ($this:expr, $plugin:ident, $call:expr) => {
        match $this {
            HostedPlugin::Vst3($plugin) => $call,
            HostedPlugin::Vst2($plugin) => $call,
            HostedPlugin::Au($plugin) => $call,
        }
    };
}

impl HostedPlugin {
    pub(crate) fn load(slot: &PluginSlot) -> Result<Self, String> {
        match slot.format.as_str() {
            "vst3" => {
                let descriptor = soundcraft_vst3_host::load_bundle(Path::new(&slot.path))
                    .map_err(|error| error.to_string())?
                    .into_iter()
                    .find(|plugin| plugin.id == slot.plugin_id || plugin.class_id == slot.plugin_id)
                    .ok_or_else(|| format!("Plugin class is unavailable ({})", slot.name))?;
                if descriptor.is_instrument {
                    return Err("Instrument plugins are not supported".into());
                }
                soundcraft_vst3_host::instantiate_descriptor(&descriptor)
                    .map(Self::Vst3)
                    .map_err(|error| error.to_string())
            }
            "vst2" => {
                let descriptor = pymss_vst2_host::scan_binaries(Path::new(&slot.path))?
                    .into_iter()
                    .find(|plugin| plugin.id == slot.plugin_id)
                    .ok_or_else(|| format!("Plugin class is unavailable ({})", slot.name))?;
                if descriptor.is_instrument == Some(true) {
                    return Err("Instrument plugins are not supported".into());
                }
                pymss_vst2_host::instantiate_descriptor(&descriptor).map(Self::Vst2)
            }
            "au" if cfg!(target_os = "macos") => {
                if slot.path != slot.plugin_id
                    || soundcraft_au_host::parse_id(&slot.plugin_id).is_none()
                {
                    return Err("Audio Unit reference is invalid".into());
                }
                soundcraft_au_host::instantiate_plugin(&slot.plugin_id)
                    .map(Self::Au)
                    .map_err(|error| error.to_string())
            }
            "au" => Err("Audio Units require macOS".into()),
            _ => Err(format!("Unsupported plugin format: {}", slot.format)),
        }
    }

    pub(crate) fn info(&self) -> &'static PluginInfo {
        dispatch!(self, plugin, plugin.info())
    }
    pub(crate) fn set_offline(&mut self, offline: bool) -> Result<(), String> {
        dispatch!(self, plugin, plugin.set_offline(offline))
    }
    pub(crate) fn param(&self, id: &str) -> Option<f32> {
        dispatch!(self, plugin, plugin.param(id))
    }
    pub(crate) fn editor(&mut self) -> Option<Box<dyn PluginEditor>> {
        dispatch!(self, plugin, plugin.editor())
    }
    pub(crate) fn state_reader(&mut self) -> Option<Box<dyn PluginStateReader>> {
        dispatch!(self, plugin, plugin.state_reader())
    }
    pub(crate) fn timing_reader(&mut self) -> Option<soundcraft_vst3_host::Vst3TimingReader> {
        match self {
            Self::Vst3(plugin) => Some(plugin.timing_reader()),
            _ => None,
        }
    }
    pub(crate) fn prepare_with_state(
        &mut self,
        rate: f32,
        block: usize,
        channels: usize,
        offline: bool,
        state: Option<&[u8]>,
    ) -> Result<(), String> {
        let restore = |plugin: &mut Self| {
            if state.is_some_and(|bytes| !dispatch!(plugin, effect, effect.load_state(bytes))) {
                Err("Unable to restore plugin state".to_string())
            } else {
                Ok(())
            }
        };
        let is_au = matches!(self, Self::Au(_));
        if !is_au {
            restore(self)?;
        }
        self.set_offline(offline)?;
        self.try_prepare(rate, block, channels)?;
        // Some AUs (including Ozone 12 after Master Assistant) need their DSP
        // initialized before dispatching the saved ClassInfo to the plugin.
        if is_au {
            restore(self)?;
        }
        Ok(())
    }
    pub(crate) fn reset(&mut self) {
        dispatch!(self, plugin, plugin.reset())
    }
    pub(crate) fn latency(&self) -> usize {
        dispatch!(self, plugin, plugin.latency())
    }
    pub(crate) fn tail_samples(&self) -> usize {
        dispatch!(self, plugin, plugin.tail_samples())
    }
    pub(crate) fn set_transport(&mut self, transport: &Transport) {
        dispatch!(self, plugin, plugin.set_transport(transport))
    }
    pub(crate) fn try_prepare(
        &mut self,
        rate: f32,
        block: usize,
        channels: usize,
    ) -> Result<(), String> {
        dispatch!(self, plugin, plugin.try_prepare(rate, block, channels))
    }
    pub(crate) fn try_process(&mut self, io: &mut [Vec<f32>], frames: usize) -> Result<(), String> {
        dispatch!(self, plugin, plugin.try_process(io, frames))
    }
    pub(crate) fn refresh_latency(&mut self) -> usize {
        dispatch!(self, plugin, plugin.refresh_latency())
    }
    pub(crate) fn refresh_latency_control(&mut self) -> usize {
        match self {
            Self::Vst3(plugin) => plugin.refresh_latency_control(),
            Self::Vst2(plugin) => plugin.refresh_latency_control(),
            Self::Au(plugin) => plugin.refresh_latency_control(),
        }
    }
    pub(crate) fn flush_pending(&mut self) -> Result<(), String> {
        dispatch!(self, plugin, plugin.flush_pending())
    }
    pub(crate) fn queue_normalized(&mut self, id: &str, normalized: f64, plain: f32) -> bool {
        dispatch!(self, plugin, plugin.queue_normalized(id, normalized, plain))
    }
}
