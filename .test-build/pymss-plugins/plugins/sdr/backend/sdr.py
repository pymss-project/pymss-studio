from __future__ import annotations

from pathlib import Path
from typing import Any

from pymss.plugins.kit import emit, require_file as _require_file


def _stereo_audio(path: Path, sample_rate: int = 44100) -> Any:
    import librosa  # type: ignore
    import numpy as np  # type: ignore

    audio, _ = librosa.load(str(path), sr=sample_rate, mono=False)
    audio = np.asarray(audio, dtype=np.float64)
    if audio.ndim == 1:
        audio = np.stack([audio, audio], axis=0)
    elif audio.shape[0] == 1:
        audio = np.repeat(audio, 2, axis=0)
    return audio[:2]


def _calculate_sdr_arrays(reference: Any, estimated: Any) -> tuple[list[float], float, list[float], float]:
    import numpy as np  # type: ignore

    reference = np.asarray(reference, dtype=np.float64)
    estimated = np.asarray(estimated, dtype=np.float64)
    if reference.ndim != 2 or estimated.ndim != 2:
        raise ValueError("SDR inputs must contain channel and sample dimensions")
    channels = min(reference.shape[0], estimated.shape[0])
    samples = min(reference.shape[1], estimated.shape[1])
    if channels <= 0 or samples <= 0:
        raise ValueError("SDR inputs do not contain audio samples")
    reference = reference[:channels, :samples]
    estimated = estimated[:channels, :samples]
    eps = 1e-7

    reference_energy = np.sum(reference * reference, axis=1)
    error_energy = np.sum((reference - estimated) ** 2, axis=1)
    sdr = 10.0 * np.log10((reference_energy + eps) / (error_energy + eps))

    scale = np.sum(estimated * reference, axis=1, keepdims=True) / (
        np.sum(reference * reference, axis=1, keepdims=True) + eps
    )
    target = scale * reference
    noise = estimated - target
    si_sdr = 10.0 * np.log10(
        (np.sum(target * target, axis=1) + eps) / (np.sum(noise * noise, axis=1) + eps)
    )
    sdr_values = [round(float(value), 4) for value in sdr]
    si_sdr_values = [round(float(value), 4) for value in si_sdr]
    return (
        sdr_values,
        round(float(np.mean(sdr)), 4),
        si_sdr_values,
        round(float(np.mean(si_sdr)), 4),
    )


def _calculate_sdr(payload: dict[str, Any]) -> dict[str, Any]:
    reference_path = _require_file(payload.get("referencePath"), "Reference audio")
    estimated_path = _require_file(payload.get("estimatedPath"), "Estimated audio")
    emit("audio_tool_progress", {
        "operation": "sdr",
        "phase": "loading_reference",
        "completed": 0,
        "total": 2,
        "current": reference_path.name,
    })
    reference = _stereo_audio(reference_path)
    emit("audio_tool_progress", {
        "operation": "sdr",
        "phase": "loading_estimated",
        "completed": 1,
        "total": 2,
        "current": estimated_path.name,
    })
    estimated = _stereo_audio(estimated_path)
    emit("audio_tool_progress", {
        "operation": "sdr",
        "phase": "calculating",
        "completed": 0,
        "total": 1,
        "current": "",
    })
    sdr, average_sdr, si_sdr, average_si_sdr = _calculate_sdr_arrays(reference, estimated)
    return {
        "operation": "sdr",
        "referencePath": str(reference_path),
        "estimatedPath": str(estimated_path),
        "sampleRate": 44100,
        "sdr": sdr,
        "averageSdr": average_sdr,
        "siSdr": si_sdr,
        "averageSiSdr": average_si_sdr,
    }
