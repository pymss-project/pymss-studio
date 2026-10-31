"""Built-in operations shipped with pymss (round 7).

Every DSP function in ``plugins/builtins.py`` that makes sense as a
user-runnable graph/CLI/Studio step is declared here as an Operation. The
algorithms stay in one place (builtins.py); this module only adds the
serializable spec + thin run handlers, then registers everything into the
process default runtime at import time — the same path external plugins use
via setup().

Loading is idempotent and failure-tolerant: a second import does nothing,
and each registration is independent so one bad spec never blocks the rest.
"""

from __future__ import annotations

import numpy as np

from . import OperationSpec, default_runtime

_BUILTIN_SOURCE = "pymss.builtin"

_PARAMS_OBJECT = {"type": "object", "properties": {}, "additionalProperties": False}


def _audio_ports():
    return {"audio": {"type": "audio"}}


def _adjust_volume_run(ctx, inputs, params):
    from ..plugins.builtins import adjust_volume
    from ..graph.core import AudioArtifact, audio_to_numpy, numpy_to_audio

    waveform, sr = audio_to_numpy(inputs["audio"])
    out = adjust_volume(waveform, sr, gain_db=params["gain_db"])
    return {"audio": numpy_to_audio(np.asarray(out, dtype=np.float32), sr)}


def _invert_phase_run(ctx, inputs, params):
    from ..plugins.builtins import invert_phase
    from ..graph.core import audio_to_numpy, numpy_to_audio

    waveform, sr = audio_to_numpy(inputs["audio"])
    out = invert_phase(waveform, sr)
    return {"audio": numpy_to_audio(np.asarray(out, dtype=np.float32), sr)}


def _normalize_peak_run(ctx, inputs, params):
    from ..plugins.builtins import normalize_peak
    from ..graph.core import audio_to_numpy, numpy_to_audio

    waveform, sr = audio_to_numpy(inputs["audio"])
    out = normalize_peak(waveform, sr, target_peak=params["target_peak"])
    return {"audio": numpy_to_audio(np.asarray(out, dtype=np.float32), sr)}


def _trim_run(ctx, inputs, params):
    from ..plugins.builtins import trim
    from ..graph.core import audio_to_numpy, numpy_to_audio

    waveform, sr = audio_to_numpy(inputs["audio"])
    out = trim(waveform, sr, start=params["start_seconds"], duration=params.get("duration_seconds"))
    return {"audio": numpy_to_audio(np.asarray(out, dtype=np.float32), sr)}


_SPECS = [
    (
        OperationSpec(
            id="pymss.audio.adjust_volume",
            schema_version=1,
            title="Adjust Volume",
            description="Apply gain in decibels",
            inputs=_audio_ports(),
            params={
                "type": "object",
                "properties": {
                    "gain_db": {"type": "number", "default": 0.0, "minimum": -60.0, "maximum": 60.0},
                },
                "additionalProperties": False,
            },
            outputs=_audio_ports(),
        ),
        _adjust_volume_run,
    ),
    (
        OperationSpec(
            id="pymss.audio.invert_phase",
            schema_version=1,
            title="Invert Phase",
            description="Negate samples (mix - vocals = instrumental)",
            inputs=_audio_ports(),
            params=_PARAMS_OBJECT,
            outputs=_audio_ports(),
        ),
        _invert_phase_run,
    ),
    (
        OperationSpec(
            id="pymss.audio.normalize_peak",
            schema_version=1,
            title="Normalize Peak",
            description="Scale audio to a target peak amplitude",
            inputs=_audio_ports(),
            params={
                "type": "object",
                "properties": {
                    "target_peak": {"type": "number", "default": 0.99, "minimum": 0.0, "maximum": 1.0},
                },
                "additionalProperties": False,
            },
            outputs=_audio_ports(),
        ),
        _normalize_peak_run,
    ),
    (
        OperationSpec(
            id="pymss.audio.trim",
            schema_version=1,
            title="Trim",
            description="Trim by start time and duration",
            inputs=_audio_ports(),
            params={
                "type": "object",
                "properties": {
                    "start_seconds": {"type": "number", "default": 0.0, "minimum": 0.0},
                    "duration_seconds": {"type": "number", "minimum": 0.0},
                },
                "additionalProperties": False,
            },
            outputs=_audio_ports(),
        ),
        _trim_run,
    ),
]


def register_builtin_operations() -> int:
    """Register all built-in operations into the default runtime.

    Idempotent: an already-registered id is skipped (external plugins may
    have claimed it first). Returns the number of newly registered ops.
    Includes the file-level kit operations (convert / inspect / sdr / slice /
    merge) from ``kit_builtins.py``.
    """
    runtime = default_runtime()
    registered = 0
    for spec, run in _SPECS:
        if spec.id in runtime._operations:
            continue
        runtime.register(spec=spec, run=run, source=_BUILTIN_SOURCE)
        registered += 1
    from .kit_builtins import register_kit_operations

    registered += register_kit_operations()
    return registered
