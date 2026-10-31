"""Built-in file-level audio operations (round 8): convert / inspect / SDR / slice / merge.

These mirror pymss-studio's audio-tools implementations (ffmpeg/ffprobe-backed,
file-in → files-out) so CLI, workflows, and Studio's plugin-ops page all share
one code path. The in-memory DSP operations stay in ``builtins.py``; everything
here operates on files and returns file paths / report dicts.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import OperationSpec, default_runtime

_BUILTIN_SOURCE = "pymss.builtin"

_OUTPUT_FORMATS = {"wav", "flac", "mp3", "ogg"}
_SAMPLE_RATES = {32000, 44100, 48000}


def _ffmpeg() -> str:
    import shutil

    binary = shutil.which("ffmpeg")
    if binary is None:
        raise RuntimeError("ffmpeg is required for pymss.kit.* file operations but was not found on PATH")
    return binary


def _ffprobe() -> str:
    import shutil

    binary = shutil.which("ffprobe")
    if binary is None:
        raise RuntimeError("ffprobe is required for pymss.kit.inspect but was not found on PATH")
    return binary


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()[-1500:]
        raise RuntimeError(detail or f"command exited with status {error.returncode}") from error


def _require_output_dir(value: Any) -> Path:
    path = Path(str(value or "")).expanduser()
    if not str(value or "").strip():
        raise ValueError("output_dir is required")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _available_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for index in range(1, 10_000):
        candidate = path.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"unable to allocate a free filename for {path.name}")


# ---------------------------------------------------------------------------
# convert
# ---------------------------------------------------------------------------


def _convert_run(ctx, inputs, params):
    source = _audio_source(inputs["audio"])
    output_dir = _require_output_dir(params["output_dir"])
    fmt = str(params.get("format", "wav"))
    if fmt not in _OUTPUT_FORMATS:
        raise ValueError(f"unsupported output format {fmt!r}")
    sample_rate = int(params.get("sample_rate", 44100))
    if sample_rate not in _SAMPLE_RATES:
        raise ValueError(f"unsupported sample rate {sample_rate}")
    channels = int(params.get("channels", 2))
    if channels not in (1, 2):
        raise ValueError("channels must be 1 or 2")

    command = ["-i", str(source), "-vn", "-ar", str(sample_rate), "-ac", str(channels)]
    if fmt == "wav":
        command += ["-c:a", {"PCM-16": "pcm_s16le", "PCM-24": "pcm_s24le", "PCM-32": "pcm_s32le"}[params.get("wav_bit_depth", "PCM-24")]]
        ext, detail = "wav", params.get("wav_bit_depth", "PCM-24")
    elif fmt == "flac":
        sample_format = {"16-bit": "s16", "32-bit": "s32"}[params.get("flac_bit_depth", "16-bit")]
        command += ["-sample_fmt", sample_format, "-compression_level", "5"]
        ext, detail = "flac", params.get("flac_bit_depth", "16-bit")
    elif fmt == "mp3":
        command += ["-b:a", str(params.get("mp3_bit_rate", "320k"))]
        ext, detail = "mp3", params.get("mp3_bit_rate", "320k")
    else:
        command += ["-b:a", str(params.get("ogg_bit_rate", "320k"))]
        ext, detail = "ogg", params.get("ogg_bit_rate", "320k")

    output = _available_path(output_dir / f"{source.stem}_{sample_rate}_{detail}.{ext}".replace(" ", "_"))
    _run([_ffmpeg(), *command, "-y", str(output)])
    if ctx is not None and getattr(ctx, "progress", None):
        ctx.report_progress(1.0, f"converted {source.name}")
    return {"audio_file": str(output), "format": fmt, "sample_rate": sample_rate}


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


def _inspect_run(ctx, inputs, params):
    source = _audio_source(inputs["audio"])
    completed = _run([
        _ffprobe(), "-v", "error", "-of", "json",
        "-show_format", "-show_streams", str(source),
    ])
    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("ffprobe returned invalid JSON") from error
    format_data = raw.get("format") if isinstance(raw.get("format"), dict) else {}
    streams = []
    for stream in raw.get("streams") or []:
        if not isinstance(stream, dict) or stream.get("codec_type") != "audio":
            continue
        streams.append({
            "codec": stream.get("codec_name") or "",
            "sample_format": stream.get("sample_fmt") or "",
            "sample_rate": int(stream["sample_rate"]) if str(stream.get("sample_rate", "")).isdigit() else None,
            "channels": stream.get("channels"),
            "channel_layout": stream.get("channel_layout") or "",
            "bit_rate": int(stream["bit_rate"]) if str(stream.get("bit_rate", "")).isdigit() else None,
            "duration_seconds": float(stream["duration"]) if stream.get("duration") else None,
        })
    if not streams:
        raise ValueError("the input file does not contain an audio stream")
    return {"report": {
        "file_name": source.name,
        "file_size": source.stat().st_size,
        "container": format_data.get("format_name") or "",
        "duration_seconds": float(format_data["duration"]) if format_data.get("duration") else None,
        "bit_rate": int(format_data["bit_rate"]) if str(format_data.get("bit_rate", "")).isdigit() else None,
        "streams": streams,
    }}


# ---------------------------------------------------------------------------
# SDR
# ---------------------------------------------------------------------------


def _sdr_run(ctx, inputs, params):
    import numpy as np

    reference_path = _audio_source(inputs["reference"])
    estimated_path = _audio_source(inputs["estimated"])

    def load_stereo(path: Path, sample_rate: int | None = None) -> tuple[np.ndarray, int]:
        from ..audio_io import load_audio

        audio, sr = load_audio(str(path), sr=sample_rate, mono=False)
        audio = np.asarray(audio, dtype=np.float64)
        if audio.ndim == 1:
            audio = np.stack([audio, audio])
        elif audio.shape[0] == 1:
            audio = np.repeat(audio, 2, axis=0)
        return audio[:2], int(sr)

    # Compare on one shared timeline: resample the estimated track to the
    # reference's rate when they differ (same signal at 44.1k vs 48k must
    # compare as identical, not as a sample-index misalignment).
    reference, reference_sr = load_stereo(reference_path)
    estimated, estimated_sr = load_stereo(estimated_path)
    if estimated_sr != reference_sr:
        # One shared timeline: resample the estimated track to the reference's
        # rate (same signal at 44.1k vs 48k must compare as identical).
        estimated, _ = load_stereo(estimated_path, sample_rate=reference_sr)
    channels = min(reference.shape[0], estimated.shape[0])
    samples = min(reference.shape[1], estimated.shape[1])
    if channels <= 0 or samples <= 0:
        raise ValueError("SDR inputs do not contain audio samples")
    reference = reference[:channels, :samples]
    estimated = estimated[:channels, :samples]
    # Scale the epsilon floor with signal energy: identical files must report a
    # very high (not eps-capped) SDR, while true zero-energy inputs stay finite.
    reference_energy = np.sum(reference * reference, axis=1)
    error_energy = np.sum((reference - estimated) ** 2, axis=1)
    eps = np.maximum(reference_energy * 1e-12, 1e-12)
    sdr = 10.0 * np.log10((reference_energy + eps) / (error_energy + eps))
    scale = np.sum(estimated * reference, axis=1, keepdims=True) / (
        np.sum(reference * reference, axis=1, keepdims=True) + eps[:, None]
    )
    target = scale * reference
    noise = estimated - target
    si_sdr = 10.0 * np.log10(
        (np.sum(target * target, axis=1) + eps) / (np.sum(noise * noise, axis=1) + eps)
    )
    return {"report": {
        "sdr": [round(float(value), 4) for value in sdr],
        "average_sdr": round(float(np.mean(sdr)), 4),
        "si_sdr": [round(float(value), 4) for value in si_sdr],
        "average_si_sdr": round(float(np.mean(si_sdr)), 4),
    }}


# ---------------------------------------------------------------------------
# slice (silence-aware segmentation)
# ---------------------------------------------------------------------------


def _slice_intervals(audio: Any, sample_rate: int, *, threshold_db: float, min_length_ms: int,
                     min_interval_ms: int, hop_ms: int, max_silence_ms: int):
    """Energy-gated segmentation (same algorithm family as the studio slicer)."""
    import numpy as np

    if audio.ndim == 1:
        audio = audio[None, :]
    mono = audio.mean(axis=0)
    hop = max(1, int(sample_rate * hop_ms / 1000))
    frames = len(mono) // hop
    if frames == 0:
        return []
    # Per-frame energy over the hop window only: a wide analysis window would
    # bleed loud energy across silence gaps and merge adjacent segments.
    energies = np.array([
        float(np.mean((mono[i * hop:(i + 1) * hop] ** 2)))
        for i in range(frames)
    ])
    db = 10.0 * np.log10(energies + 1e-10)
    loud = db > threshold_db

    min_length_frames = max(1, min_length_ms // hop_ms)
    max_sil_frames = max(0, max_silence_ms // hop_ms)

    intervals: list[tuple[float, float]] = []
    start: int | None = None
    last_loud: int | None = None
    silence = 0
    for index, is_loud in enumerate(loud):
        if is_loud:
            if start is None:
                start = index
            last_loud = index
            silence = 0
        elif start is not None:
            silence += 1
            if silence >= max_sil_frames:
                if last_loud is not None and last_loud - start + 1 >= min_length_frames:
                    intervals.append((start * hop / sample_rate, (last_loud + 1) * hop / sample_rate))
                start = None
                last_loud = None
                silence = 0
    if start is not None and last_loud is not None and last_loud - start + 1 >= min_length_frames:
        intervals.append((start * hop / sample_rate, (last_loud + 1) * hop / sample_rate))
    return intervals


def _slice_run(ctx, inputs, params):
    import numpy as np

    source = _audio_source(inputs["audio"])
    output_dir = _require_output_dir(params["output_dir"])
    fmt = str(params.get("format", "wav"))
    codec = {"wav": ["-c:a", "pcm_s24le"], "flac": ["-c:a", "flac"], "mp3": ["-c:a", "libmp3lame", "-b:a", "320k"]}.get(fmt)
    if codec is None:
        raise ValueError(f"unsupported slice output format {fmt!r}")

    from ..audio_io import load_audio

    audio, sample_rate = load_audio(str(source), sr=None, mono=False)
    audio = np.asarray(audio)
    sample_rate = int(sample_rate)
    intervals = _slice_intervals(
        audio, int(sample_rate),
        threshold_db=float(params.get("threshold_db", -40.0)),
        min_length_ms=int(params.get("min_length_ms", 5000)),
        min_interval_ms=int(params.get("min_interval_ms", 300)),
        hop_ms=int(params.get("hop_size_ms", 10)),
        max_silence_ms=int(params.get("max_silence_kept_ms", 500)),
    )
    if not intervals:
        raise ValueError("no non-silent audio segments were detected")

    files: list[str] = []
    total = len(intervals)
    for index, (start, end) in enumerate(intervals, start=1):
        output = _available_path(output_dir / f"{source.stem}_segment_{index:03d}.{fmt}")
        _run([_ffmpeg(), "-ss", f"{start:.6f}", "-i", str(source), "-t", f"{max(0.0, end - start):.6f}",
              "-vn", *codec, "-y", str(output)])
        files.append(str(output))
        if ctx is not None and getattr(ctx, "progress", None):
            ctx.report_progress(index / total, f"segment {index}/{total}")
    return {"files": files, "count": total, "report": {"intervals": [{"start": s, "end": e} for s, e in intervals]}}


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


def _merge_run(ctx, inputs, params):
    source = _audio_source(inputs["audio"])
    # Additional inputs ride the params (schema-typed as a list of paths).
    raw_extras = str(params.get("additional_inputs") or "")
    extras = [Path(part.strip()) for part in raw_extras.split(";") if part.strip()]
    ordered = [source, *extras]
    output_dir = _require_output_dir(params["output_dir"])
    output = _available_path(output_dir / f"merged_{source.stem}.wav")

    with tempfile.TemporaryDirectory(prefix="pymss-merge-") as temp:
        segments: list[Path] = []
        for index, path in enumerate(ordered, start=1):
            segment = Path(temp) / f"segment_{index:06d}.wav"
            _run([_ffmpeg(), "-i", str(path), "-vn", "-ar", "44100", "-ac", "2", "-c:a", "pcm_s24le", "-y", str(segment)])
            segments.append(segment)
            if ctx is not None and getattr(ctx, "progress", None):
                ctx.report_progress(index / (len(ordered) + 1), f"normalized {path.name}")
        listing = Path(temp) / "segments.txt"
        listing.write_text("".join(f"file '{str(p)}'\n" for p in segments), encoding="utf-8")
        _run([_ffmpeg(), "-f", "concat", "-safe", "0", "-i", str(listing), "-c:a", "pcm_s24le", "-y", str(output)])
    return {"audio_file": str(output), "count": len(ordered)}


# ---------------------------------------------------------------------------
# shared input handling
# ---------------------------------------------------------------------------


def _audio_source(audio_in: Any) -> Path:
    """Accept a path string or an audio artifact backed by a file."""
    source = getattr(audio_in, "source_path", None)
    if source and Path(str(source)).is_file():
        return Path(str(source))
    if isinstance(audio_in, (str, Path)) and Path(audio_in).is_file():
        return Path(audio_in)
    raise ValueError(
        "this operation requires a file-backed audio input; "
        "pass a load node / file-backed artifact instead of an in-memory-only buffer"
    )


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

_AUDIO_INPUT = {"audio": {"type": "audio"}}

_KIT_SPECS: list[dict[str, Any]] = [
    {
        "id": "pymss.kit.convert",
        "title": "Convert Audio Format",
        "description": "Re-encode an audio file (wav/flac/mp3/ogg) with sample-rate, channel and quality control",
        "inputs": _AUDIO_INPUT,
        "params": {
            "type": "object",
            "properties": {
                "output_dir": {"type": "string"},
                "format": {"type": "string", "enum": ["wav", "flac", "mp3", "ogg"], "default": "wav"},
                "sample_rate": {"type": "integer", "enum": [32000, 44100, 48000], "default": 44100},
                "channels": {"type": "integer", "enum": [1, 2], "default": 2},
                "wav_bit_depth": {"type": "string", "enum": ["PCM-16", "PCM-24", "PCM-32"], "default": "PCM-24"},
                "flac_bit_depth": {"type": "string", "enum": ["16-bit", "32-bit"], "default": "16-bit"},
                "mp3_bit_rate": {"type": "string", "enum": ["192k", "256k", "320k"], "default": "320k"},
                "ogg_bit_rate": {"type": "string", "enum": ["192k", "256k", "320k", "450k"], "default": "320k"},
            },
            "required": ["output_dir"],
            "additionalProperties": False,
        },
        "outputs": {"audio_file": {"type": "string"}, "format": {"type": "string"}, "sample_rate": {"type": "integer"}},
        "run": _convert_run,
    },
    {
        "id": "pymss.kit.inspect",
        "title": "Inspect Audio Metadata",
        "description": "Probe an audio file's container, streams, codecs, sample rate and duration (ffprobe)",
        "inputs": _AUDIO_INPUT,
        "params": {"type": "object", "properties": {}, "additionalProperties": False},
        "outputs": {"report": {"type": "object"}},
        "run": _inspect_run,
    },
    {
        "id": "pymss.kit.sdr",
        "title": "Calculate SDR",
        "description": "Signal-to-Distortion Ratio (and SI-SDR) between a reference and an estimated audio file",
        "inputs": {"reference": {"type": "audio"}, "estimated": {"type": "audio"}},
        "params": {"type": "object", "properties": {}, "additionalProperties": False},
        "outputs": {"report": {"type": "object"}},
        "run": _sdr_run,
    },
    {
        "id": "pymss.kit.slice",
        "title": "Slice by Silence",
        "description": "Split an audio file into segments at silence boundaries (energy-gated)",
        "inputs": _AUDIO_INPUT,
        "params": {
            "type": "object",
            "properties": {
                "output_dir": {"type": "string"},
                "format": {"type": "string", "enum": ["wav", "flac", "mp3"], "default": "wav"},
                "threshold_db": {"type": "number", "default": -40.0, "minimum": -100.0, "maximum": 0.0},
                "min_length_ms": {"type": "integer", "default": 5000, "minimum": 100},
                "min_interval_ms": {"type": "integer", "default": 300, "minimum": 10},
                "hop_size_ms": {"type": "integer", "default": 10, "minimum": 1},
                "max_silence_kept_ms": {"type": "integer", "default": 500, "minimum": 0},
            },
            "required": ["output_dir"],
            "additionalProperties": False,
        },
        "outputs": {"files": {"type": "array"}, "count": {"type": "integer"}, "report": {"type": "object"}},
        "run": _slice_run,
    },
    {
        "id": "pymss.kit.merge",
        "title": "Merge Audio Files",
        "description": "Concatenate audio files (44.1kHz stereo normalized) into one WAV",
        "inputs": _AUDIO_INPUT,
        "params": {
            "type": "object",
            "properties": {
                "output_dir": {"type": "string"},
                "additional_inputs": {"type": "string", "description": "semicolon-separated extra audio file paths"},
            },
            "required": ["output_dir"],
            "additionalProperties": False,
        },
        "outputs": {"audio_file": {"type": "string"}, "count": {"type": "integer"}},
        "run": _merge_run,
    },
]


def register_kit_operations() -> int:
    """Register the file-level kit operations into the default runtime (idempotent)."""
    runtime = default_runtime()
    registered = 0
    for definition in _KIT_SPECS:
        try:
            runtime.register(
                spec=OperationSpec(
                    id=definition["id"],
                    title=definition["title"],
                    description=definition.get("description", ""),
                    schema_version=1,
                    inputs=definition["inputs"],
                    params=definition["params"],
                    outputs=definition["outputs"],
                ),
                run=definition["run"],
                source=_BUILTIN_SOURCE,
            )
            registered += 1
        except Exception:  # noqa: BLE001 — one bad spec never blocks the rest
            continue
    return registered
