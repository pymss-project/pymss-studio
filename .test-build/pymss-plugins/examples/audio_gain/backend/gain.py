"""Gain processing through the public pymss Operation interface."""
from __future__ import annotations

import math

import numpy as np


OPERATION_ID = "example.audio_gain.apply"


def apply_gain(context, inputs, params):
    from pymss.graph.core import AudioArtifact, audio_to_numpy

    gain_db = params.get("gain_db", 0.0)
    if isinstance(gain_db, bool) or not isinstance(gain_db, (int, float)) or not math.isfinite(gain_db) or not -24 <= gain_db <= 12:
        raise ValueError("Gain must be a finite number between -24 and 12 dB")
    protect_peak = params.get("protect_peak", True)
    if not isinstance(protect_peak, bool):
        raise ValueError("Peak protection must be a boolean")
    waveform, sample_rate = audio_to_numpy(inputs["audio"])
    audio = np.asarray(waveform, dtype=np.float32)
    if audio.ndim not in (1, 2) or not audio.size or not np.isfinite(audio).all():
        raise ValueError("Input audio must contain finite, non-empty samples")
    context.check_cancelled()
    gain = 10 ** (float(gain_db) / 20)
    peak_before = float(np.max(np.abs(audio)))
    limited = protect_peak and peak_before * gain > 0.999
    applied_gain = 0.999 / peak_before if limited else gain
    output = np.empty_like(audio)
    frames = audio.shape[-1]
    for start in range(0, frames, 262144):
        context.check_cancelled()
        stop = min(frames, start + 262144)
        try:
            with np.errstate(over="raise", invalid="raise"):
                output[..., start:stop] = audio[..., start:stop] * applied_gain
        except FloatingPointError as error:
            raise ValueError("Gain would produce non-finite output samples") from error
        context.report_progress(0.9 * stop / frames, "processing")
    context.check_cancelled()
    result = {
        "audio": AudioArtifact(output, sample_rate, channel_layout=inputs["audio"].channel_layout),
        "report": {"gain_db": float(gain_db), "peak_before": peak_before,
                   "peak_after": float(np.max(np.abs(output))), "peak_limited": limited},
    }
    context.report_progress(1.0, "completed")
    return result


def setup(plugin):
    plugin.register_operation(
        id=OPERATION_ID, title="Apply gain",
        description="Adjust gain without changing sample rate or channel layout",
        inputs={"audio": {"type": "audio"}},
        params={"type": "object", "properties": {
            "gain_db": {"type": "number", "minimum": -24, "maximum": 12, "default": 0.0},
            "protect_peak": {"type": "boolean", "default": True},
        }, "additionalProperties": False},
        outputs={"audio": {"type": "audio"}, "report": {"type": "object"}}, run=apply_gain,
    )
