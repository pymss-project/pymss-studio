"""Built-in capabilities: DSP and channel operations registered into the plugin
system so they can be consumed by nodes, CLI, and library code uniformly.

These are NOT separate plugins — they live in pymss core but use the same
registration API as plugins. pymss itself eats its own dogfood.

Capability registry (flat, no categories):

  Channel ops:
    to_mono         (audio) -> mono audio
    split_channels  (audio) -> list of per-channel mono audios
    join_channels   (audios) -> multichannel audio

  DSP ops:
    adjust_volume   (audio, sr, gain_db) -> audio
    invert_phase    (audio) -> -audio
    normalize_peak  (audio, target_peak) -> scaled audio
    standardize     (audio) -> (audio, stats); destandardize(audio, stats) -> audio
    trim            (audio, sr, start, duration) -> audio
    concat          (audios) -> audio
    mix             (audios, mode) -> audio   (add/mean/subtract)
    empty_audio     (duration, sr, channels) -> silent audio
    eq              (audio, sr, low/mid/high gain+freq+q) -> audio (biquad)
    resample        (audio, orig_sr, target_sr) -> audio
    ensemble        (pred_track_3d, weights, algorithm) -> audio

All audio arrays are numpy float32, channel-first: shape (channels, samples)
or (samples,) for mono. This matches pymss's internal convention.
"""

from __future__ import annotations

import numpy as np

from .registry import _REGISTRY


# ---------------------------------------------------------------------------
# Channel operations
# ---------------------------------------------------------------------------


def to_mono(audio) -> np.ndarray:
    """Collapse a multichannel audio array to mono by averaging channels.

    Accepts channel-first (channels, samples) or 1-D (samples,). Returns 1-D.
    """
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim == 1:
        return audio
    # channel-first: average across axis 0
    return audio.mean(axis=0)


def split_channels(audio) -> list[np.ndarray]:
    """Split a multichannel (channel-first) array into a list of mono arrays."""
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim == 1:
        return [audio]
    return [audio[i] for i in range(audio.shape[0])]


def join_channels(audios) -> np.ndarray:
    """Stack a sequence of equal-length mono arrays into channel-first multichannel."""
    arrays = [np.asarray(a, dtype=np.float32) for a in audios]
    if not arrays:
        raise ValueError("join_channels requires at least one input")
    lengths = {a.shape[-1] for a in arrays}
    if len(lengths) != 1:
        raise ValueError(f"all inputs must have the same length; got {sorted(lengths)}")
    return np.stack(arrays, axis=0)


# ---------------------------------------------------------------------------
# DSP operations
# ---------------------------------------------------------------------------


def adjust_volume(audio, sample_rate=None, gain_db: float = 0.0) -> np.ndarray:
    """Apply a gain in decibels. gain_db=0 is unity; +6 ≈ double amplitude."""
    audio = np.asarray(audio, dtype=np.float32)
    gain = 10 ** (gain_db / 20.0)
    return audio * gain


def invert_phase(audio, sample_rate=None) -> np.ndarray:
    """Invert the phase (negate samples). Classic for 'mix - vocals = instrumental'."""
    return -np.asarray(audio, dtype=np.float32)


def normalize_peak(audio, sample_rate=None, target_peak: float = 0.99, *, reference_peak: float | None = None) -> np.ndarray:
    """Scale audio to target_peak, optionally using a peak shared by multiple stems."""
    audio = np.asarray(audio, dtype=np.float32)
    peak = float(reference_peak) if reference_peak is not None else (float(np.max(np.abs(audio))) if audio.size else 0.0)
    if peak <= 0.0 or not np.isfinite(peak):
        return audio
    return audio * (target_peak / peak)


def standardize(audio, sample_rate=None):
    """Zero-mean, unit-variance standardization (input preprocessing).

    Returns (standardized_audio, stats) where stats=(mean, std). Use
    destandardize() to reverse.
    """
    audio = np.asarray(audio, dtype=np.float32)
    mono = audio.mean(0) if audio.ndim > 1 else audio
    mean = float(mono.mean())
    std = float(mono.std())
    if std == 0:
        return audio, (mean, std)
    return (audio - mean) / std, (mean, std)


def destandardize(audio, stats) -> np.ndarray:
    """Reverse standardize(): audio * std + mean."""
    audio = np.asarray(audio, dtype=np.float32)
    mean, std = stats
    return audio * std + mean


def trim(audio, sample_rate: float, start: float = 0.0, duration: float | None = None) -> np.ndarray:
    """Trim audio by time. start/duration in seconds. Keeps [start, start+duration)."""
    audio = np.asarray(audio, dtype=np.float32)
    sr = int(sample_rate)
    start_sample = max(0, int(start * sr))
    if duration is None:
        end_sample = audio.shape[-1]
    else:
        end_sample = min(audio.shape[-1], start_sample + int(duration * sr))
    return audio[..., start_sample:end_sample]


def concat(audios, sample_rate=None, crossfade: float = 0.0) -> np.ndarray:
    """Concatenate audios end-to-end along the time axis.

    Inputs must share channel count. crossfade (seconds) overlaps the boundary
    with a linear fade when > 0.
    """
    arrays = [np.asarray(a, dtype=np.float32) for a in audios]
    if not arrays:
        raise ValueError("concat requires at least one input")
    if crossfade and crossfade > 0 and sample_rate:
        # naive crossfade: linear overlap-add
        cf = int(crossfade * sample_rate)
        out = arrays[0]
        for nxt in arrays[1:]:
            if cf > 0 and out.shape[-1] >= cf and nxt.shape[-1] >= cf:
                fade = np.linspace(1, 0, cf, dtype=np.float32)
                tail = out[..., -cf:] * fade + nxt[..., :cf] * (1 - fade)
                out = np.concatenate([out[..., :-cf], tail, nxt[..., cf:]], axis=-1)
            else:
                out = np.concatenate([out, nxt], axis=-1)
        return out
    return np.concatenate(arrays, axis=-1)


def mix(audios, sample_rate=None, mode: str = "add") -> np.ndarray:
    """Combine equal-length audios sample-wise.

    mode:
      add       - sum (may clip)
      mean      - average
      subtract  - first minus the rest
      min       - element-wise minimum by magnitude
      max       - element-wise maximum by magnitude
    """
    arrays = [np.asarray(a, dtype=np.float32) for a in audios]
    if not arrays:
        raise ValueError("mix requires at least one input")
    stacked = np.stack(arrays, axis=0)
    if mode == "add":
        return stacked.sum(axis=0)
    if mode == "mean":
        return stacked.mean(axis=0)
    if mode == "subtract":
        result = arrays[0].copy()
        for a in arrays[1:]:
            result = result - a
        return result
    if mode == "min":
        # min by absolute value, keep sign
        idx = np.argmin(np.abs(stacked), axis=0)
        return np.take_along_axis(stacked, idx[None, ...], axis=0)[0]
    if mode == "max":
        idx = np.argmax(np.abs(stacked), axis=0)
        return np.take_along_axis(stacked, idx[None, ...], axis=0)[0]
    raise ValueError(f"unknown mix mode: {mode}")


def empty_audio(duration: float, sample_rate: float, channels: int = 2) -> np.ndarray:
    """Generate silent audio of a given duration. channel-first (channels, samples)."""
    n_samples = int(duration * sample_rate)
    if channels <= 1:
        return np.zeros(n_samples, dtype=np.float32)
    return np.zeros((channels, n_samples), dtype=np.float32)


# Audio EQ Cookbook (Robert Bristow-Johnson) biquad coefficient helpers.
# These are the standard formulas behind torchaudio.functional.bass_biquad /
# equalizer_biquad / treble_biquad, implemented with scipy.signal (already a
# pymss dependency via librosa) so no torchaudio is needed.


def _low_shelf_coeffs(freq, gain_db, sr, q=0.707):
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * freq / sr
    alpha = np.sin(w0) / (2 * q)
    cosw = np.cos(w0)
    sqA = np.sqrt(A)
    b0 = A * ((A + 1) - (A - 1) * cosw + 2 * sqA * alpha)
    b1 = 2 * A * ((A - 1) - (A + 1) * cosw)
    b2 = A * ((A + 1) - (A - 1) * cosw - 2 * sqA * alpha)
    a0 = (A + 1) + (A - 1) * cosw + 2 * sqA * alpha
    a1 = -2 * ((A - 1) + (A + 1) * cosw)
    a2 = (A + 1) + (A - 1) * cosw - 2 * sqA * alpha
    return [b0 / a0, b1 / a0, b2 / a0], [1.0, a1 / a0, a2 / a0]


def _high_shelf_coeffs(freq, gain_db, sr, q=0.707):
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * freq / sr
    alpha = np.sin(w0) / (2 * q)
    cosw = np.cos(w0)
    sqA = np.sqrt(A)
    b0 = A * ((A + 1) + (A - 1) * cosw + 2 * sqA * alpha)
    b1 = -2 * A * ((A - 1) + (A + 1) * cosw)
    b2 = A * ((A + 1) + (A - 1) * cosw - 2 * sqA * alpha)
    a0 = (A + 1) - (A - 1) * cosw + 2 * sqA * alpha
    a1 = 2 * ((A - 1) - (A + 1) * cosw)
    a2 = (A + 1) - (A - 1) * cosw - 2 * sqA * alpha
    return [b0 / a0, b1 / a0, b2 / a0], [1.0, a1 / a0, a2 / a0]


def _peaking_coeffs(freq, gain_db, sr, q):
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * freq / sr
    alpha = np.sin(w0) / (2 * q)
    cosw = np.cos(w0)
    b0 = 1 + alpha * A
    b1 = -2 * cosw
    b2 = 1 - alpha * A
    a0 = 1 + alpha / A
    a1 = -2 * cosw
    a2 = 1 - alpha / A
    return [b0 / a0, b1 / a0, b2 / a0], [1.0, a1 / a0, a2 / a0]


def eq(
    audio,
    sample_rate: float,
    low_gain_db: float = 0.0, low_freq: int = 100,
    mid_gain_db: float = 0.0, mid_freq: int = 1000, mid_q: float = 0.707,
    high_gain_db: float = 0.0, high_freq: int = 5000,
) -> np.ndarray:
    """3-band equalizer (low shelf / peaking / high shelf) via biquad filters.

    Equivalent to ComfyUI's AudioEqualizer3Band (same parameters), implemented
    with scipy.signal.lfilter so no torchaudio dependency is needed.
    Audio is channel-first (channels, samples) or 1-D mono.
    """
    from scipy.signal import lfilter

    audio = np.asarray(audio, dtype=np.float32)
    out = audio
    if low_gain_db != 0:
        b, a = _low_shelf_coeffs(low_freq, low_gain_db, sample_rate)
        out = lfilter(b, a, out, axis=-1)
    if mid_gain_db != 0:
        b, a = _peaking_coeffs(mid_freq, mid_gain_db, sample_rate, mid_q)
        out = lfilter(b, a, out, axis=-1)
    if high_gain_db != 0:
        b, a = _high_shelf_coeffs(high_freq, high_gain_db, sample_rate)
        out = lfilter(b, a, out, axis=-1)
    return np.asarray(out, dtype=np.float32)


def resample(audio, orig_sr: float, target_sr: float) -> np.ndarray:
    """Resample audio using the file loader's PyAV/FFmpeg implementation.

    Audio is channel-first (channels, samples) or 1-D mono. Returns the same
    layout at the target sample rate.
    """
    from ..audio_io import resample_audio

    return resample_audio(audio, orig_sr, target_sr)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_builtin_capabilities() -> None:
    """Register all built-in DSP / channel capabilities into the shared registry.

    Idempotent: skips names already registered as builtin (avoids duplicate
    warnings when called more than once).
    """
    caps = {
        # channel ops
        "to_mono": (to_mono, "collapse multichannel to mono by averaging"),
        "split_channels": (split_channels, "split multichannel into per-channel mono list"),
        "join_channels": (join_channels, "stack mono arrays into multichannel"),
        # dsp ops
        "adjust_volume": (adjust_volume, "apply gain in dB"),
        "invert_phase": (invert_phase, "negate samples (phase inversion)"),
        "normalize_peak": (normalize_peak, "scale to a target peak amplitude"),
        "standardize": (standardize, "zero-mean unit-variance (input preprocessing)"),
        "destandardize": (destandardize, "reverse standardize()"),
        "trim": (trim, "trim by start time and duration"),
        "concat": (concat, "concatenate audios end-to-end"),
        "mix": (mix, "sample-wise combine (add/mean/subtract/min/max)"),
        "empty_audio": (empty_audio, "generate silent audio of a given duration"),
        "eq": (eq, "3-band equalizer (biquad, scipy-backed)"),
        "resample": (resample, "resample between sample rates (librosa)"),
    }
    with _REGISTRY._builtin_registration():
        for name, (func, desc) in caps.items():
            if name in _REGISTRY.capabilities and _REGISTRY.capabilities[name].source == "builtin":
                continue
            _REGISTRY.register_capability(name, func, source="builtin", description=desc)


__all__ = [
    "to_mono",
    "split_channels",
    "join_channels",
    "adjust_volume",
    "invert_phase",
    "normalize_peak",
    "standardize",
    "destandardize",
    "trim",
    "concat",
    "mix",
    "empty_audio",
    "eq",
    "resample",
    "register_builtin_capabilities",
]
