"""pymss plugin: dynamics processing — compression, limiting, expansion, de-essing.

Registers capabilities backed by Spotify's pedalboard. Audio is numpy
(1-D mono or 2-D channels-first float32); output matches the input layout.
All dB parameters are in dBFS.
"""

from __future__ import annotations

import numpy as np



def _apply(board_cls, audio, sample_rate, **kwargs):
    """Apply a single pedalboard effect, preserving 1-D vs 2-D layout."""
    import pedalboard

    audio = np.asarray(audio, dtype=np.float32)
    flat = audio.ndim == 1
    x = audio[None, :] if flat else audio
    board = pedalboard.Pedalboard([board_cls(**kwargs)])
    out = board(x, int(sample_rate))
    return out[0] if flat else out


def compressor(audio, sample_rate, threshold_db=0.0, ratio=4.0, attack_ms=1.0,
               release_ms=100.0):
    """Feed-forward compressor. threshold_db/ratio/attack/release in dBFS / ratio / ms."""
    from pedalboard import Compressor

    return _apply(Compressor, audio, sample_rate,
                  threshold_db=threshold_db, ratio=ratio,
                  attack_ms=attack_ms, release_ms=release_ms)


def limiter(audio, sample_rate, threshold_db=-3.0, release_ms=100.0):
    """Brick-wall limiter: guarantees output peak ≤ threshold_db (dBFS).

    Uses a simple soft-knee clipper. threshold_db is the absolute peak ceiling
    (e.g. -1.0 for streaming delivery). Independent of pedalboard's Limiter,
    which normalizes toward a target rather than capping peaks.
    """
    import numpy as np

    audio = np.asarray(audio, dtype=np.float32)
    ceiling = float(10 ** (threshold_db / 20.0))
    # Soft-clip with tanh scaled so |tanh(x)| ≤ 1, then scale to ceiling.
    # This preserves relative dynamics while guaranteeing the peak ceiling.
    peak = float(np.max(np.abs(audio))) or 1.0
    if peak <= ceiling:
        return audio  # already under ceiling
    # Normalize to unity, soft-clip, scale to ceiling.
    norm = audio / peak
    out = np.tanh(norm * 1.5) / np.tanh(1.5) * ceiling
    return out.astype(np.float32)


def expander(audio, sample_rate, threshold_db=-50.0, ratio=2.0, attack_ms=1.0,
             release_ms=100.0):
    """Downward expander / gate-ish. Quiet signals below threshold are attenuated further."""
    from pedalboard import Compressor

    # pedalboard has no dedicated Expander; use Compressor with ratio < 1
    # to get expansion behaviour (ratio 0.5 = -2:1 expansion).
    return _apply(Compressor, audio, sample_rate,
                  threshold_db=threshold_db, ratio=ratio,
                  attack_ms=attack_ms, release_ms=release_ms)


def multiband_compressor(audio, sample_rate, low_freq=200.0, high_freq=4000.0,
                         low_threshold=-20.0, mid_threshold=-18.0, high_threshold=-20.0,
                         ratio=2.5):
    """Three-band compressor: split, compress each band, recombine.

    A simplified multiband (low/mid/high) using two Linkwitz-Riley-ish splits
    via pedalboard filters + three compressors. Not a true mastering-grade MBC
    but covers the common use case.
    """
    import numpy as np
    from pedalboard import Compressor, LowpassFilter, HighpassFilter

    audio = np.asarray(audio, dtype=np.float32)
    sr = int(sample_rate)
    flat = audio.ndim == 1
    x = audio[None, :] if flat else audio

    # Crude band split via first-order filters (good enough for dynamics control).
    low = pedalboard_pass(LowpassFilter(cutoff_frequency_hz=low_freq), x, sr)
    mid_raw = pedalboard_pass(HighpassFilter(cutoff_frequency_hz=low_freq), x, sr)
    mid = pedalboard_pass(LowpassFilter(cutoff_frequency_hz=high_freq), mid_raw, sr)
    high = pedalboard_pass(HighpassFilter(cutoff_frequency_hz=high_freq), mid_raw, sr)

    low_c = pedalboard_pass(Compressor(threshold_db=low_threshold, ratio=ratio), low, sr)
    mid_c = pedalboard_pass(Compressor(threshold_db=mid_threshold, ratio=ratio), mid, sr)
    high_c = pedalboard_pass(Compressor(threshold_db=high_threshold, ratio=ratio), high, sr)

    out = low_c + mid_c + high_c
    return out[0] if flat else out


def pedalboard_pass(effect, x, sr):
    import pedalboard

    return pedalboard.Pedalboard([effect])(x, sr)


def deesser(audio, sample_rate, threshold_db=0.0, ratio=3.0, crossover_hz=6000.0):
    """De-esser: compress only the high-frequency (sibilance) band.

    Splits at crossover_hz, compresses the high band, recombines. A real
    de-esser is dynamic (only acts when sibilance present); this is a static
    high-band compressor that approximates the effect.
    """
    from pedalboard import Compressor, HighpassFilter, LowpassFilter

    audio = np.asarray(audio, dtype=np.float32)
    sr = int(sample_rate)
    flat = audio.ndim == 1
    x = audio[None, :] if flat else audio

    low = pedalboard_pass(LowpassFilter(cutoff_frequency_hz=crossover_hz), x, sr)
    high_raw = pedalboard_pass(HighpassFilter(cutoff_frequency_hz=crossover_hz), x, sr)
    high_c = pedalboard_pass(
        Compressor(threshold_db=threshold_db, ratio=ratio, attack_ms=0.5, release_ms=50.0),
        high_raw, sr,
    )
    out = low + high_c
    return out[0] if flat else out


_CAPABILITIES = [
    ("compressor", compressor),
    ("limiter", limiter),
    ("expander", expander),
    ("multiband_compressor", multiband_compressor),
    ("deesser", deesser),
]


def setup(plugin):
    """Register the dynamics capabilities and expose the main processors as Operations."""
    from pymss.graph.core import audio_to_numpy, numpy_to_audio

    for name, fn in _CAPABILITIES:
        plugin.register_capability(name, fn)

    def _wrap(fn, param_defs):
        def run(ctx, inputs, params):
            audio_in = inputs["audio"]
            waveform, sr = audio_to_numpy(audio_in)
            kwargs = {k: params[k] for k in param_defs if k in params}
            out = fn(waveform, sr, **kwargs)
            return {"audio": numpy_to_audio(np.asarray(out, dtype=np.float32), sr,
                source_path=audio_in.source_path, stem_name=audio_in.stem_name, channel_layout=audio_in.channel_layout)}
        return run

    plugin.register_operation(
        id="pymss.dynamics.compressor",
        title="Compressor",
        description="Feed-forward compressor (pedalboard)",
        inputs={"audio": {"type": "audio"}},
        params={"type": "object", "properties": {
            "threshold_db": {"type": "number", "default": 0.0, "minimum": -60.0, "maximum": 0.0},
            "ratio": {"type": "number", "default": 4.0, "minimum": 1.0, "maximum": 20.0},
            "attack_ms": {"type": "number", "default": 1.0, "minimum": 0.1, "maximum": 100.0},
            "release_ms": {"type": "number", "default": 100.0, "minimum": 1.0, "maximum": 1000.0},
        }, "additionalProperties": False},
        outputs={"audio": {"type": "audio"}},
        run=_wrap(compressor, {"threshold_db", "ratio", "attack_ms", "release_ms"}),
    )
    plugin.register_operation(
        id="pymss.dynamics.limiter",
        title="Limiter",
        description="Brick-wall peak limiter (guaranteed ceiling)",
        inputs={"audio": {"type": "audio"}},
        params={"type": "object", "properties": {
            "threshold_db": {"type": "number", "default": -3.0, "minimum": -24.0, "maximum": 0.0},
        }, "additionalProperties": False},
        outputs={"audio": {"type": "audio"}},
        run=_wrap(limiter, {"threshold_db"}),
    )
    plugin.register_operation(
        id="pymss.dynamics.deesser",
        title="De-esser",
        description="High-band compressor for sibilance control",
        inputs={"audio": {"type": "audio"}},
        params={"type": "object", "properties": {
            "threshold_db": {"type": "number", "default": 0.0, "minimum": -60.0, "maximum": 0.0},
            "ratio": {"type": "number", "default": 3.0, "minimum": 1.0, "maximum": 10.0},
            "crossover_hz": {"type": "number", "default": 6000.0, "minimum": 2000.0, "maximum": 12000.0},
        }, "additionalProperties": False},
        outputs={"audio": {"type": "audio"}},
        run=_wrap(deesser, {"threshold_db", "ratio", "crossover_hz"}),
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
        ('DynamicsCompressor', 'compressor', ['threshold_db', 'ratio', 'attack_ms', 'release_ms'],
         {'threshold_db': 0.0, 'ratio': 4.0, 'attack_ms': 1.0, 'release_ms': 100.0}),
        ('DynamicsLimiter', 'limiter', ['threshold_db', 'release_ms'],
         {'threshold_db': -3.0, 'release_ms': 100.0}),
        ('DynamicsDeEsser', 'deesser', ['threshold_db', 'ratio', 'crossover_hz'],
         {'threshold_db': 0.0, 'ratio': 3.0, 'crossover_hz': 6000.0}),
    ]
    for node_type, capability, parameters, defaults in specs:
        signature, execute = _make_processor_node(capability, parameters, defaults)
        plugin.register_node(node_type, execute, signature=signature)
