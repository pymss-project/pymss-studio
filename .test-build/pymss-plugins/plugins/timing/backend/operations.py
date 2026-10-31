"""pymss plugin: time-stretch and pitch-shift.

Registers `pitch_shift` and `time_stretch` capabilities, backed by librosa
(already a pymss dependency). Both accept numpy audio (1-D mono or 2-D
channels-first float32) and return audio at the same layout.
"""

from __future__ import annotations

import numpy as np



def pitch_shift(audio, sample_rate, n_steps: float, bins_per_octave: int = 12):
    """Shift pitch by `n_steps` semitones (fractional allowed).

    A positive `n_steps` raises pitch; negative lowers it. Duration is
    preserved (phase vocoder + resampling). Output keeps the input's
    channel layout.
    """
    import librosa

    audio = np.asarray(audio, dtype=np.float32)
    y = librosa.effects.pitch_shift(
        audio, sr=int(sample_rate), n_steps=float(n_steps),
        bins_per_octave=int(bins_per_octave),
    )
    return y.astype(np.float32)


def time_stretch(audio, sample_rate, rate: float):
    """Time-stretch audio by `rate` without changing pitch.

    `rate > 1` speeds up (shorter); `0 < rate < 1` slows down (longer).
    The returned length is round(len / rate). sample_rate is accepted for
    API symmetry but unused by librosa's phase vocoder.
    """
    import librosa

    audio = np.asarray(audio, dtype=np.float32)
    y = librosa.effects.time_stretch(audio, rate=float(rate))
    return y.astype(np.float32)


def setup(plugin):
    """Register pitch_shift / time_stretch as capabilities and Operations."""
    from pymss.graph.core import audio_to_numpy, numpy_to_audio

    plugin.register_capability(
        "pitch_shift", pitch_shift,
        description="Pitch-shift by fractional semitones (librosa, duration preserved)",
    )
    plugin.register_capability(
        "time_stretch", time_stretch,
        description="Time-stretch by rate without pitch change (librosa phase vocoder)",
    )

    def _wrap(fn, extra_params):
        def run(ctx, inputs, params):
            audio_in = inputs["audio"]
            waveform, sr = audio_to_numpy(audio_in)
            out = fn(waveform, sr, **{k: v for k, v in params.items() if k in extra_params})
            return {"audio": numpy_to_audio(np.asarray(out, dtype=np.float32), sr,
                source_path=audio_in.source_path, stem_name=audio_in.stem_name, channel_layout=audio_in.channel_layout)}
        return run

    plugin.register_operation(
        id="pymss.timing.pitch_shift",
        title="Pitch Shift",
        description="Shift pitch by fractional semitones (duration preserved)",
        inputs={"audio": {"type": "audio"}},
        params={"type": "object", "properties": {
            "n_steps": {"type": "number", "default": 0.0, "minimum": -24.0, "maximum": 24.0},
            "bins_per_octave": {"type": "integer", "default": 12, "minimum": 1, "maximum": 36},
        }, "additionalProperties": False},
        outputs={"audio": {"type": "audio"}},
        run=_wrap(pitch_shift, {"n_steps", "bins_per_octave"}),
    )
    plugin.register_operation(
        id="pymss.timing.time_stretch",
        title="Time Stretch",
        description="Time-stretch by rate without pitch change",
        inputs={"audio": {"type": "audio"}},
        params={"type": "object", "properties": {
            "rate": {"type": "number", "default": 1.0, "minimum": 0.25, "maximum": 4.0},
        }, "additionalProperties": False},
        outputs={"audio": {"type": "audio"}},
        run=_wrap(time_stretch, {"rate"}),
    )

    _register_graph_nodes(plugin)


def _make_processor_node(capability_name, param_names, widget_defaults):
    """Build signature+execute pair for an audio->audio processor node."""
    from pymss.graph.core import AUDIO, NodeSignature, PortSpec

    defaults = dict(widget_defaults)

    def signature(node):
        return NodeSignature(
            inputs=[PortSpec(name="audio", type=AUDIO)],
            output_names=["audio"],
            output_types=[AUDIO],
        )

    def execute(ctx, inputs):
        from pymss.graph.core import DAGError, NodeResult, audio_to_numpy, numpy_to_audio

        audio_in = inputs.get("audio")
        if audio_in is None:
            raise DAGError(f"{capability_name} requires an AUDIO input")
        waveform, sr = audio_to_numpy(audio_in)
        node = ctx.nodes_by_id[ctx.current_node_id]
        widgets = node.data.get("widgets_values", [])
        kwargs = {}
        for index, name in enumerate(param_names):
            if index < len(widgets) and widgets[index] is not None:
                kwargs[name] = widgets[index]
            elif name in defaults:
                kwargs[name] = defaults[name]
        fn = ctx.require(capability_name)
        out = fn(waveform, sr, **kwargs)
        return NodeResult(outputs={0: numpy_to_audio(np.asarray(out, dtype=np.float32), sr,
            source_path=audio_in.source_path, stem_name=audio_in.stem_name, channel_layout=audio_in.channel_layout)})

    return signature, execute


def _register_graph_nodes(plugin):
    specs = [
        ('TimingPitchShift', 'pitch_shift', ['n_steps', 'bins_per_octave'],
         {'n_steps': 0.0, 'bins_per_octave': 12}),
        ('TimingTimeStretch', 'time_stretch', ['rate'],
         {'rate': 1.0}),
    ]
    for node_type, capability, parameters, defaults in specs:
        signature, execute = _make_processor_node(capability, parameters, defaults)
        plugin.register_node(node_type, execute, signature=signature)
