from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from pymss.plugins.kit import (
    available_path as _available_path,
    emit,
    ffmpeg_path as _ffmpeg_path,
    ffprobe_path,
    require_directory as _require_directory,
    require_file as _require_file,
    run_ffmpeg as _run_ffmpeg,
    run_process,
    subprocess_detail as _subprocess_detail,
)


MEASUREMENTS = {
    "itu-bs1770-3": {"kind": "loudness", "default": -23.0, "unit": "LUFS"},
    "rms": {"kind": "rms", "default": -23.0, "unit": "dB"},
    "rms-match-file": {"kind": "rms-match", "default": 0.0, "unit": "dB"},
    "peak": {"kind": "peak", "default": -1.0, "unit": "dB"},
    "true-peak": {"kind": "true-peak", "default": -1.0, "unit": "dBTP"},
}
# EBU R128 and ATSC A/85 both measure BS.1770 integrated loudness and differ
# only in their target level, so they share one measurement. The legacy modes
# were plain RMS. Older saved state and requests keep working through aliases.
MEASUREMENT_ALIASES = {
    "atsc-a85": "itu-bs1770-3",
    "ebu-r128": "itu-bs1770-3",
    "legacy-loudness": "rms",
    "legacy-perceived": "rms",
}

_LOUDNORM_JSON = re.compile(r"\{\s*\"input_i\".*?\}", re.DOTALL)
# Measurements below this level are silence or noise floor; boosting them to a
# normal target would turn hiss into a loud, useless output.
SILENCE_FLOOR_DB = -70.0
# 4x oversampling lets the sample-peak limiter approximate an inter-sample
# (true) peak ceiling.
LIMITER_OVERSAMPLING = 4
# 4x oversampling still misses some inter-sample overs (~0.1-0.15 dB on dense
# material), so the limiter ceiling sits slightly below the requested peak.
LIMITER_SAFETY_DB = 0.2
PARTIAL_PREFIX = ".pymss-loudness-"
MAX_FALLBACK_BIT_RATE = 320000


def _audio_files(values: Any) -> list[Path]:
    if not isinstance(values, list) or not values:
        raise ValueError("At least one input audio file is required")
    if len(values) > 200:
        raise ValueError("A maximum of 200 files can be normalized at once")
    return [_require_file(value, "Input audio") for value in values]


def _run_measurement(input_path: Path, filter_name: str) -> str:
    completed = subprocess.run(
        [_ffmpeg_path(), "-nostdin", "-hide_banner", "-loglevel", "info", "-i", str(input_path), "-map", "0:a:0", "-vn",
         "-af", filter_name, "-f", "null", "-"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(detail[-2000:] or f"FFmpeg exited with status {completed.returncode}")
    return completed.stderr


def _loudnorm_stats(input_path: Path) -> dict[str, float]:
    # Measurement-only pass: the statistics do not depend on the target, so use
    # a fixed in-range target instead of the user's value.
    stderr = _run_measurement(input_path, "loudnorm=I=-23:LRA=7:TP=0:print_format=json")
    matches = list(_LOUDNORM_JSON.finditer(stderr))
    if not matches:
        raise RuntimeError("FFmpeg did not return loudness measurement data")
    try:
        value = json.loads(matches[-1].group(0))
        return {key: float(value[key]) for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("FFmpeg returned invalid loudness measurement data") from error


def _volume_stats(input_path: Path) -> tuple[float, float]:
    stderr = _run_measurement(input_path, "volumedetect")
    # volumedetect emits mean_volume before max_volume. Keep the parser tied to
    # the labels rather than relying on a fixed number of diagnostic lines.
    mean_match = re.search(r"mean_volume:\s*(-?(?:\d+(?:\.\d*)?|\.\d+|inf))\s*dB", stderr, re.IGNORECASE)
    max_match = re.search(r"max_volume:\s*(-?(?:\d+(?:\.\d*)?|\.\d+|inf))\s*dB", stderr, re.IGNORECASE)
    if not mean_match or not max_match:
        raise RuntimeError("FFmpeg did not return RMS and peak measurement data")
    return float(mean_match.group(1)), float(max_match.group(1))


def _flag(value: Any, name: str, *, default: bool) -> bool:
    # bool("false") is True; accept real booleans and common string spellings only.
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off", ""):
        return False
    raise ValueError(f"{name} must be a boolean")


def _require_audible(measured: float) -> None:
    if not math.isfinite(measured) or measured < SILENCE_FLOOR_DB:
        raise ValueError(f"Input audio is silent or below {SILENCE_FLOOR_DB:.0f} dB and cannot be normalized")


def _audio_format(input_path: Path) -> dict[str, Any]:
    completed = run_process([
        ffprobe_path(), "-v", "error", "-select_streams", "a", "-of", "json",
        "-show_entries", "stream=codec_name,sample_rate,bits_per_raw_sample,bits_per_sample,bit_rate:format=bit_rate", str(input_path),
    ])
    probe = json.loads(completed.stdout or "{}")
    streams = probe.get("streams") or []
    if not streams:
        raise RuntimeError("No audio stream was found in the input file")
    stream = streams[0]
    try:
        bits = int(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 0)
    except (TypeError, ValueError):
        bits = 0
    try:
        sample_rate = int(stream.get("sample_rate") or 0)
    except (TypeError, ValueError):
        sample_rate = 0
    # Ogg/Opus streams often omit bit_rate. The container-level value is the
    # closest stand-in, but it also counts cover art and container overhead,
    # so cap it at a rate every lossy encoder accepts.
    bit_rate = _positive_int(stream.get("bit_rate"))
    if not bit_rate:
        bit_rate = min(_positive_int((probe.get("format") or {}).get("bit_rate")), MAX_FALLBACK_BIT_RATE)
    return {
        "codec": str(stream.get("codec_name") or ""), "sampleRate": sample_rate,
        "bits": bits, "bitRate": bit_rate, "audioStreamCount": len(streams),
    }


def _positive_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _encoder_arguments(audio_format: dict[str, Any]) -> list[str]:
    # Without explicit arguments FFmpeg falls back to 16-bit PCM for WAV/AIFF;
    # keep the source codec, bit depth and sample rate instead.
    arguments: list[str] = []
    codec = audio_format["codec"]
    high_depth = audio_format["bits"] > 16
    if codec.startswith("pcm_"):
        arguments += ["-c:a", codec]
    elif codec == "flac":
        arguments += ["-c:a", "flac", "-sample_fmt", "s32" if high_depth else "s16"]
    elif codec == "alac":
        # The .m4a muxer defaults to AAC, which would turn a lossless source lossy.
        arguments += ["-c:a", "alac", "-sample_fmt", "s32p" if high_depth else "s16p"]
    elif codec == "wavpack":
        arguments += ["-c:a", "wavpack"]
    elif audio_format.get("bitRate", 0) > 0:
        # Lossy sources keep the muxer's default encoder but not its default
        # bitrate, which is often far below the source.
        arguments += ["-b:a", str(audio_format["bitRate"])]
    if audio_format["sampleRate"] > 0:
        arguments += ["-ar", str(audio_format["sampleRate"])]
    return arguments


def _gain_filter(gain_db: float) -> str:
    if not math.isfinite(gain_db):
        raise ValueError("Calculated normalization gain is invalid")
    return f"volume={gain_db:.8f}dB"


def _limiter_filters(max_true_peak: float, lookahead: float, release: float, sample_rate: int) -> list[str]:
    limit = 10 ** ((max_true_peak - LIMITER_SAFETY_DB) / 20.0)
    # FFmpeg's default level_out=1 adds the headroom back after limiting.
    # Keep the requested true-peak ceiling as the limiter output level too.
    limiter = f"alimiter=limit={limit:.8f}:level_out={limit:.8f}:attack={lookahead:.3f}:release={release:.3f}:latency=1"
    if sample_rate <= 0:
        return [limiter]
    return [f"aresample={sample_rate * LIMITER_OVERSAMPLING}", limiter, f"aresample={sample_rate}"]


def _output_path(output_dir: Path, input_path: Path) -> Path:
    return _available_path(output_dir / f"{input_path.stem}_loudness{input_path.suffix.lower()}")


def _partial_path(output_path: Path, index: int) -> Path:
    # Keep the real extension so FFmpeg picks the same muxer as the final file.
    return output_path.with_name(f"{PARTIAL_PREFIX}{os.getpid()}-{index}{output_path.suffix}")


def _remove_stale_partials(output_dir: Path) -> None:
    # A cancelled run kills the worker before it can clean up; drop leftovers
    # from earlier runs so they never look like finished output.
    for path in output_dir.glob(f"{PARTIAL_PREFIX}*"):
        if path.is_file():
            path.unlink(missing_ok=True)


def _render(input_path: Path, output_path: Path, filters: list[str], audio_format: dict[str, Any]) -> None:
    _run_ffmpeg([
        "-i", str(input_path), "-map", "0:a:0", "-vn", "-map_metadata", "0", "-af", ",".join(filters),
        *_encoder_arguments(audio_format), "-y", str(output_path),
    ])


def _loudness_audio(payload: dict[str, Any]) -> dict[str, Any]:
    inputs = _audio_files(payload.get("inputs"))
    output_dir = _require_directory(payload.get("outputDir"), "Output directory", create=True)
    measurement = str(payload.get("measurement") or "itu-bs1770-3").strip().lower()
    measurement = MEASUREMENT_ALIASES.get(measurement, measurement)
    if measurement not in MEASUREMENTS:
        raise ValueError("Unsupported loudness measurement")
    config = MEASUREMENTS[measurement]
    target = float(payload.get("target") if payload.get("target") is not None else config["default"])
    tolerance = float(payload.get("tolerance") if payload.get("tolerance") is not None else 0.5)
    max_true_peak = float(payload.get("maxTruePeak") if payload.get("maxTruePeak") is not None else -1.0)
    use_limiter = _flag(payload.get("useLimiter"), "useLimiter", default=True)
    lookahead = float(payload.get("lookahead") if payload.get("lookahead") is not None else 12)
    release = float(payload.get("release") if payload.get("release") is not None else 200)

    target_max = -5.0 if config["kind"] == "loudness" else 0.0
    if not math.isfinite(target) or not -70 <= target <= target_max:
        raise ValueError(f"Target level must be between -70 and {target_max:g} {config['unit']}")
    if not math.isfinite(tolerance) or not 0 <= tolerance <= 20:
        raise ValueError("Tolerance must be between 0 and 20 dB")
    if not math.isfinite(max_true_peak) or not -9 <= max_true_peak <= 0:
        raise ValueError("Maximum true peak must be between -9 and 0 dBTP")
    if not math.isfinite(lookahead) or not math.isfinite(release) or not 0.1 <= lookahead <= 80 or not 10 <= release <= 5000:
        raise ValueError("Limiter timing parameters are outside the supported range")

    reference_level: float | None = None
    if config["kind"] == "rms-match":
        reference = _require_file(payload.get("referencePath"), "Reference audio")
        reference_level, _ = _volume_stats(reference)
        _require_audible(reference_level)
        target = reference_level
    _remove_stale_partials(output_dir)

    output_paths: list[str] = []
    failures: list[dict[str, str]] = []
    measurements: list[dict[str, Any]] = []
    for index, input_path in enumerate(inputs, start=1):
        emit("audio_tool_progress", {
            "operation": "loudness", "phase": "normalizing", "completed": index - 1,
            "total": len(inputs), "current": input_path.name,
        })
        output_path = _output_path(output_dir, input_path)
        partial_path = _partial_path(output_path, index)
        try:
            kind = config["kind"]
            measured_kind = kind
            unit = config["unit"]
            limited = use_limiter and kind in ("loudness", "rms", "rms-match")
            if kind == "loudness":
                measured = _loudnorm_stats(input_path)["input_i"]
                # BS.1770 gating returns -inf for very short clips. Fall back
                # to RMS for a measurable short clip; silence still fails below.
                if not math.isfinite(measured) or measured < SILENCE_FLOOR_DB:
                    measured, _ = _volume_stats(input_path)
                    measured_kind = "rms"
                    unit = "dB"
            elif kind == "rms" or kind == "rms-match":
                measured, _ = _volume_stats(input_path)
            elif kind == "peak":
                _, measured = _volume_stats(input_path)
            else:
                measured = _loudnorm_stats(input_path)["input_tp"]
            _require_audible(measured)
            within_tolerance = abs(measured - target) <= tolerance + 1e-7
            audio_format = _audio_format(input_path)
            copy_audio = within_tolerance and not limited
            preserve_file = copy_audio and audio_format["audioStreamCount"] == 1

            # Preserve the encoded audio when it already satisfies the range and
            # no limiter work is needed. Output is written to a hidden partial
            # file first so a cancelled run never leaves a truncated result.
            if preserve_file:
                shutil.copy2(input_path, partial_path)
            elif copy_audio:
                # A multitrack container must still select the same stream as
                # the measurement and encoding paths, without re-encoding it.
                _run_ffmpeg([
                    "-i", str(input_path), "-map", "0:a:0", "-vn", "-map_metadata", "0",
                    "-c:a", "copy", "-y", str(partial_path),
                ])
            else:
                # Apply a single linear gain; loudnorm's render mode silently
                # switches to dynamic processing and upsamples to 192 kHz.
                filters = [_gain_filter(target - measured)]
                if limited:
                    filters += _limiter_filters(max_true_peak, lookahead, release, audio_format["sampleRate"])
                emit("audio_tool_progress", {
                    "operation": "loudness", "phase": "writing_output", "completed": index - 1,
                    "total": len(inputs), "current": output_path.name,
                })
                _render(input_path, partial_path, filters, audio_format)
            output_stats = {}
            if not preserve_file and (limited or measured_kind == "loudness" or kind == "true-peak"):
                output_stats = _loudnorm_stats(partial_path)
            if preserve_file:
                output_level = measured
            elif measured_kind == "loudness":
                output_level = output_stats["input_i"]
            elif kind == "true-peak":
                output_level = output_stats["input_tp"]
            else:
                mean_level, peak_level = _volume_stats(partial_path)
                output_level = peak_level if kind == "peak" else mean_level
            if not math.isfinite(output_level):
                raise RuntimeError("The output audio level could not be measured")
            measurement_result = {
                "path": str(output_path), "inputLevel": measured, "outputLevel": output_level,
                "target": target, "unit": unit,
                "withinTolerance": abs(output_level - target) <= tolerance + 1e-7,
            }
            if limited:
                true_peak = output_stats["input_tp"]
                if math.isnan(true_peak) or true_peak == math.inf:
                    raise RuntimeError("The output true peak could not be measured")
                measurement_result.update({
                    # Silence has a -inf peak; JSON represents that as null.
                    "outputTruePeak": true_peak if math.isfinite(true_peak) else None,
                    "maxTruePeak": max_true_peak,
                    "withinTruePeakLimit": true_peak <= max_true_peak + 1e-7,
                })
            os.replace(partial_path, output_path)
            output_paths.append(str(output_path))
            measurements.append(measurement_result)
        except subprocess.CalledProcessError as error:
            failures.append({"path": str(input_path), "message": _subprocess_detail(error)})
            partial_path.unlink(missing_ok=True)
        except Exception as error:
            failures.append({"path": str(input_path), "message": str(error)})
            partial_path.unlink(missing_ok=True)

    if not output_paths:
        detail = failures[0]["message"] if failures else "No files were normalized"
        raise RuntimeError(detail)
    warnings: list[str] = []
    if any(not item["withinTolerance"] for item in measurements):
        warnings.append("loudness_target_not_reached")
    if any(item.get("withinTruePeakLimit") is False for item in measurements):
        warnings.append("loudness_true_peak_exceeded")
    return {
        "operation": "loudness",
        "measurement": measurement,
        "target": target,
        "referenceLevel": reference_level,
        "outputDir": str(output_dir),
        "outputPaths": output_paths,
        "succeeded": len(output_paths),
        "failed": failures,
        "measurements": measurements,
        "warnings": warnings,
    }
