from __future__ import annotations

import math
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from pymss.plugins.kit import (
    available_path as _available_path,
    emit,
    ffmpeg_path as _ffmpeg_path,
    render_output as _render_output,
    require_directory as _require_directory,
    require_file as _require_file,
    run_ffmpeg as _run_ffmpeg,
)
from .slicer_core import Slicer

OUTPUT_FORMATS = {"wav", "flac", "mp3"}
ANALYSIS_SAMPLE_RATE = 16000


def _number_parameter(payload: dict[str, Any], name: str, default: int, *, integer: bool = False) -> float | int:
    value = payload.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{name} must be a finite number")
    if integer:
        if value != int(value):
            raise ValueError(f"{name} must be an integer number of milliseconds")
        return int(value)
    return float(value)


def _decode_analysis_audio(input_path: Path, raw_path: Path) -> None:
    with raw_path.open("wb") as output:
        completed = subprocess.run(
            [_ffmpeg_path(), "-nostdin", "-hide_banner", "-loglevel", "error",
             "-i", str(input_path), "-vn", "-ac", "1", "-ar", str(ANALYSIS_SAMPLE_RATE),
             "-f", "f32le", "-acodec", "pcm_f32le", "-"],
            stdout=output,
            stderr=subprocess.PIPE,
        )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail[-2000:] or f"FFmpeg exited with status {completed.returncode}")


def _slice_intervals(input_path: Path, raw_path: Path, *, threshold: float, min_length: int,
                     min_interval: int, hop_size: int, max_sil_kept: int) -> list[tuple[float, float]]:
    import numpy as np  # type: ignore

    _decode_analysis_audio(input_path, raw_path)
    sample_count = raw_path.stat().st_size // 4
    if sample_count <= 0:
        raise ValueError("The selected file does not contain decodable audio samples")
    waveform = np.memmap(raw_path, dtype="<f4", mode="r", shape=(sample_count,))
    try:
        slicer = Slicer(
            sr=ANALYSIS_SAMPLE_RATE,
            threshold=threshold,
            min_length=min_length,
            min_interval=min_interval,
            hop_size=hop_size,
            max_sil_kept=max_sil_kept,
        )
        chunks = slicer.slice(waveform)
        return [
            (float(chunk["offset"]), float(chunk["offset"]) + float(chunk["waveform"].shape[-1]) / ANALYSIS_SAMPLE_RATE)
            for chunk in chunks
            if chunk["waveform"].shape[-1] > 0
        ]
    finally:
        waveform._mmap.close()


def _render_segment(input_path: Path, output_path: Path, start: float, end: float, output_format: str) -> None:
    codec = {
        "wav": ["-c:a", "pcm_s24le"],
        "flac": ["-c:a", "flac"],
        "mp3": ["-c:a", "libmp3lame", "-b:a", "320k"],
    }[output_format]
    _run_ffmpeg([
        "-ss", f"{start:.6f}", "-i", str(input_path), "-t", f"{max(0.0, end - start):.6f}",
        "-vn", *codec, "-y", str(output_path),
    ])


def _slice_audio(payload: dict[str, Any]) -> dict[str, Any]:
    raw_inputs = payload.get("inputs")
    if not isinstance(raw_inputs, list) or not raw_inputs:
        raise ValueError("At least one input audio file is required")
    if len(raw_inputs) > 200:
        raise ValueError("A maximum of 200 files can be sliced at once")
    output_format = str(payload.get("outputFormat") or "wav").lower()
    threshold = _number_parameter(payload, "threshold", -40)
    min_length = _number_parameter(payload, "minLength", 5000, integer=True)
    min_interval = _number_parameter(payload, "minInterval", 300, integer=True)
    hop_size = _number_parameter(payload, "hopSize", 10, integer=True)
    max_sil_kept = _number_parameter(payload, "maxSilKept", 500, integer=True)
    if output_format not in OUTPUT_FORMATS:
        raise ValueError("Unsupported slice output format")
    if not -100 <= threshold <= 0:
        raise ValueError("Silence threshold must be between -100 and 0 dB")
    if not min_length >= min_interval >= hop_size > 0:
        raise ValueError("The following condition must be satisfied: minLength >= minInterval >= hopSize > 0")
    if max_sil_kept < hop_size:
        raise ValueError("maxSilKept must be greater than or equal to hopSize")
    inputs = [_require_file(value, "Input audio") for value in raw_inputs]
    output_dir = _require_directory(payload.get("outputDir"), "Output directory", create=True)

    analyzed: list[tuple[Path, list[tuple[float, float]]]] = []
    with tempfile.TemporaryDirectory(prefix="pymss-slicer-") as temp_value:
        temp_dir = Path(temp_value)
        for index, input_path in enumerate(inputs, start=1):
            emit("audio_tool_progress", {
                "operation": "slicer", "phase": "analyzing_silence",
                "completed": index - 1, "total": len(inputs), "current": input_path.name,
            })
            analysis_path = temp_dir / f"analysis-{index}.f32"
            try:
                intervals = _slice_intervals(
                    input_path, analysis_path,
                    threshold=threshold, min_length=min_length, min_interval=min_interval,
                    hop_size=hop_size, max_sil_kept=max_sil_kept,
                )
            finally:
                # Only the intervals are needed after analysis. Releasing each decoded PCM file
                # here keeps folder jobs from accumulating the full batch in temporary storage.
                analysis_path.unlink(missing_ok=True)
            analyzed.append((input_path, intervals))

    total_segments = sum(len(intervals) for _, intervals in analyzed)
    if total_segments == 0:
        raise ValueError("No non-silent audio segments were detected")
    output_paths: list[str] = []
    segments: list[dict[str, Any]] = []
    written = 0
    for input_path, intervals in analyzed:
        for index, (start, end) in enumerate(intervals, start=1):
            emit("audio_tool_progress", {
                "operation": "slicer", "phase": "writing_segments",
                "completed": written, "total": total_segments, "current": input_path.name,
            })
            name = f"{input_path.stem}_{index:04d}_{round(start * 1000):08d}-{round(end * 1000):08d}.{output_format}"
            output_path = _available_path(output_dir / name)
            output_path = _render_output(output_path, lambda temporary: _render_segment(input_path, temporary, start, end, output_format))
            written += 1
            output_paths.append(str(output_path))
            segments.append({"sourcePath": str(input_path), "outputPath": str(output_path), "start": start, "end": end, "duration": end - start})
    emit("audio_tool_progress", {
        "operation": "slicer", "phase": "writing_segments",
        "completed": written, "total": total_segments, "current": "",
    })
    return {
        "operation": "slicer", "outputDir": str(output_dir), "outputPaths": output_paths,
        "succeeded": written, "sourceCount": len(inputs), "segments": segments,
        "keptDuration": sum(item["duration"] for item in segments),
    }
