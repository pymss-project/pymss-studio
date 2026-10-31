"""pymss plugin: spectral processing — parametric EQ and filters.

Registers `parametric_eq`, `highpass`, `lowpass`, `bandpass`, `notch`,
`bandshelf`, `baxandall_eq` capabilities. Uses scipy (already a pymss dep via
librosa) for biquad-based filters and pedalboard for the multiband EQ.
"""

from __future__ import annotations

import numpy as np



def _biquad(audio, sample_rate, btype, f0, Q=0.707, gain_db=0.0):
    """Apply a single scipy biquad filter, preserving layout.

    btype: 'lowpass' | 'highpass' | 'bandpass' | 'notch' | 'lowshelf' | 'highshelf' | 'peak' | 'bandshelf'
    f0: center/corner frequency in Hz
    Q: quality factor
    gain_db: shelf/peak gain in dB (used by peak/shelf types)
    """
    from scipy.signal import lfilter


    audio = np.asarray(audio, dtype=np.float32)
    b, a = _coeffs(btype, f0, sample_rate, Q, gain_db)
    flat = audio.ndim == 1
    x = audio[None, :] if flat else audio
    out = np.stack([lfilter(b, a, ch) for ch in x])
    return out[0] if flat else out


def _coeffs(btype, f0, sr, Q, gain_db):
    """Compute biquad coefficients per the Audio EQ Cookbook."""
    import math

    sr, f0, Q, gain_db = map(float, (sr, f0, Q, gain_db))
    if not math.isfinite(sr) or sr <= 0:
        raise ValueError("Filter sample rate must be finite and positive")
    if not math.isfinite(f0) or not 0 < f0 < sr / 2:
        raise ValueError("Filter frequency must be positive and below the input audio Nyquist frequency")
    if not math.isfinite(Q) or Q <= 0:
        raise ValueError("Filter Q must be finite and positive")
    if not math.isfinite(gain_db):
        raise ValueError("Filter gain must be finite")

    w0 = 2 * math.pi * f0 / sr
    cos = math.cos(w0)
    sin = math.sin(w0)
    A = 10 ** (gain_db / 40.0)
    alpha = sin / (2 * Q)

    if btype == "lowpass":
        b0 = (1 - cos) / 2
        b1 = 1 - cos
        b2 = (1 - cos) / 2
        a0 = 1 + alpha
        a1 = -2 * cos
        a2 = 1 - alpha
    elif btype == "highpass":
        b0 = (1 + cos) / 2
        b1 = -(1 + cos)
        b2 = (1 + cos) / 2
        a0 = 1 + alpha
        a1 = -2 * cos
        a2 = 1 - alpha
    elif btype == "bandpass":
        b0 = alpha
        b1 = 0
        b2 = -alpha
        a0 = 1 + alpha
        a1 = -2 * cos
        a2 = 1 - alpha
    elif btype == "notch":
        b0 = 1
        b1 = -2 * cos
        b2 = 1
        a0 = 1 + alpha
        a1 = -2 * cos
        a2 = 1 - alpha
    elif btype == "peak":
        b0 = 1 + alpha * A
        b1 = -2 * cos
        b2 = 1 - alpha * A
        a0 = 1 + alpha / A
        a1 = -2 * cos
        a2 = 1 - alpha / A
    elif btype == "lowshelf":
        b0 = A * ((A + 1) - (A - 1) * cos + 2 * math.sqrt(A) * alpha)
        b1 = 2 * A * ((A - 1) - (A + 1) * cos)
        b2 = A * ((A + 1) - (A - 1) * cos - 2 * math.sqrt(A) * alpha)
        a0 = (A + 1) + (A - 1) * cos + 2 * math.sqrt(A) * alpha
        a1 = -2 * ((A - 1) + (A + 1) * cos)
        a2 = (A + 1) + (A - 1) * cos - 2 * math.sqrt(A) * alpha
    elif btype == "highshelf":
        b0 = A * ((A + 1) + (A - 1) * cos + 2 * math.sqrt(A) * alpha)
        b1 = -2 * A * ((A - 1) + (A + 1) * cos)
        b2 = A * ((A + 1) + (A - 1) * cos - 2 * math.sqrt(A) * alpha)
        a0 = (A + 1) - (A - 1) * cos + 2 * math.sqrt(A) * alpha
        a1 = 2 * ((A - 1) - (A + 1) * cos)
        a2 = (A + 1) - (A - 1) * cos - 2 * math.sqrt(A) * alpha
    else:
        raise ValueError(f"unknown biquad btype: {btype!r}")

    return [b0 / a0, b1 / a0, b2 / a0], [1.0, a1 / a0, a2 / a0]


def highpass(audio, sample_rate, cutoff_hz, Q=0.707):
    """First-order-ish high-pass biquad at cutoff_hz."""
    return _biquad(audio, sample_rate, "highpass", cutoff_hz, Q=Q)


def lowpass(audio, sample_rate, cutoff_hz, Q=0.707):
    """Low-pass biquad at cutoff_hz."""
    return _biquad(audio, sample_rate, "lowpass", cutoff_hz, Q=Q)


def bandpass(audio, sample_rate, center_hz, Q=1.0):
    """Band-pass biquad at center_hz."""
    return _biquad(audio, sample_rate, "bandpass", center_hz, Q=Q)


def notch(audio, sample_rate, center_hz, Q=2.0):
    """Notch (band-stop) biquad — useful for removing hum at a specific frequency."""
    return _biquad(audio, sample_rate, "notch", center_hz, Q=Q)


def peak_eq(audio, sample_rate, center_hz, gain_db, Q=1.0):
    """Peaking EQ: boost/cut a band around center_hz by gain_db."""
    return _biquad(audio, sample_rate, "peak", center_hz, Q=Q, gain_db=gain_db)


def lowshelf(audio, sample_rate, cutoff_hz, gain_db, Q=0.707):
    """Low-shelf: boost/cut everything below cutoff_hz by gain_db."""
    return _biquad(audio, sample_rate, "lowshelf", cutoff_hz, Q=Q, gain_db=gain_db)


def highshelf(audio, sample_rate, cutoff_hz, gain_db, Q=0.707):
    """High-shelf: boost/cut everything above cutoff_hz by gain_db."""
    return _biquad(audio, sample_rate, "highshelf", cutoff_hz, Q=Q, gain_db=gain_db)


def parametric_eq(audio, sample_rate, bands):
    """Parametric EQ with an arbitrary number of biquad bands.

    `bands` is a list of dicts, each: {type, freq, gain_db?, Q?} where type is
    one of lowpass/highpass/peak/lowshelf/highshelf/notch. Bands are applied
    in sequence (cascade).
    """
    out = np.asarray(audio, dtype=np.float32)
    for band in bands:
        btype = band["type"]
        f0 = float(band["freq"])
        gain = float(band.get("gain_db", 0.0))
        Q = float(band.get("Q", 0.707))
        out = _biquad(out, sample_rate, btype, f0, Q=Q, gain_db=gain)
    return out


def baxandall_eq(audio, sample_rate, bass_db=0.0, treble_db=0.0,
                 bass_corner_hz=300.0, treble_corner_hz=3000.0):
    """Baxandall tone stack: shelf-style bass and treble control.

    bass_db / treble_db boost or cut. A classic hi-fi tone-control curve.
    """
    out = audio
    if bass_db != 0.0:
        out = lowshelf(out, sample_rate, bass_corner_hz, bass_db)
    if treble_db != 0.0:
        out = highshelf(out, sample_rate, treble_corner_hz, treble_db)
    return out


_CAPABILITIES = [
    ("highpass", highpass),
    ("lowpass", lowpass),
    ("bandpass", bandpass),
    ("notch", notch),
    ("peak_eq", peak_eq),
    ("lowshelf", lowshelf),
    ("highshelf", highshelf),
    ("parametric_eq", parametric_eq),
    ("baxandall_eq", baxandall_eq),
]


def setup(plugin):
    """Register the filter capabilities and expose the most-used filters as Operations."""
    from pymss.graph.core import audio_to_numpy, numpy_to_audio

    for name, fn in _CAPABILITIES:
        plugin.register_capability(name, fn)

    def _wrap(fn, frequency_parameter):
        def run(ctx, inputs, params):
            audio_in = inputs["audio"]
            waveform, sr = audio_to_numpy(audio_in)
            kwargs = dict(params)
            frequency = kwargs.pop("f0")
            if not 0 < frequency < sr / 2:
                raise ValueError("Filter frequency must be below the input audio Nyquist frequency")
            kwargs[frequency_parameter] = frequency
            out = fn(waveform, sr, **kwargs)
            return {"audio": numpy_to_audio(np.asarray(out, dtype=np.float32), sr,
                source_path=audio_in.source_path, stem_name=audio_in.stem_name, channel_layout=audio_in.channel_layout)}
        return run

    plugin.register_operation(
        id="pymss.spectral.highpass",
        title="Highpass Filter",
        description="Remove low frequencies below a cutoff",
        inputs={"audio": {"type": "audio"}},
        params={"type": "object", "properties": {
            "f0": {"type": "number", "default": 80.0, "minimum": 10.0, "maximum": 2000.0},
            "Q": {"type": "number", "default": 0.707, "minimum": 0.1, "maximum": 8.0},
        }, "additionalProperties": False},
        outputs={"audio": {"type": "audio"}},
        run=_wrap(highpass, "cutoff_hz"),
    )
    plugin.register_operation(
        id="pymss.spectral.lowpass",
        title="Lowpass Filter",
        description="Remove high frequencies above a cutoff",
        inputs={"audio": {"type": "audio"}},
        params={"type": "object", "properties": {
            "f0": {"type": "number", "default": 8000.0, "minimum": 100.0, "maximum": 20000.0},
            "Q": {"type": "number", "default": 0.707, "minimum": 0.1, "maximum": 8.0},
        }, "additionalProperties": False},
        outputs={"audio": {"type": "audio"}},
        run=_wrap(lowpass, "cutoff_hz"),
    )
    plugin.register_operation(
        id="pymss.spectral.peak_eq",
        title="Peak EQ",
        description="Parametric peak filter at a frequency",
        inputs={"audio": {"type": "audio"}},
        params={"type": "object", "properties": {
            "f0": {"type": "number", "default": 1000.0, "minimum": 20.0, "maximum": 20000.0},
            "gain_db": {"type": "number", "default": 0.0, "minimum": -24.0, "maximum": 24.0},
            "Q": {"type": "number", "default": 1.0, "minimum": 0.1, "maximum": 8.0},
        }, "additionalProperties": False},
        outputs={"audio": {"type": "audio"}},
        run=_wrap(peak_eq, "center_hz"),
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
        ('SpectralHighpass', 'highpass', ['cutoff_hz', 'Q'],
         {'cutoff_hz': 80.0, 'Q': 0.707}),
        ('SpectralLowpass', 'lowpass', ['cutoff_hz', 'Q'],
         {'cutoff_hz': 8000.0, 'Q': 0.707}),
        ('SpectralPeakEQ', 'peak_eq', ['center_hz', 'gain_db', 'Q'],
         {'center_hz': 1000.0, 'gain_db': 0.0, 'Q': 1.0}),
    ]
    for node_type, capability, parameters, defaults in specs:
        signature, execute = _make_processor_node(capability, parameters, defaults)
        plugin.register_node(node_type, execute, signature=signature)
