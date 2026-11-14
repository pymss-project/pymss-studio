from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

from worker_protocol import emit, emit_error


class _EditorExportCancelled(Exception):
    pass


class _DiskMixBuffer:
    """Disk-backed channel-first mix buffer with lossless capacity growth."""

    def __init__(self, np: Any, directory: Path, prefix: str, channels: int, frames: int):
        self.np = np
        self.directory = directory
        self.prefix = prefix
        self.channels = max(1, int(channels))
        self.frames = max(1, int(frames))
        self.path, self.data = self._create(self.channels, self.frames)

    def _create(self, channels: int, frames: int) -> tuple[Path, Any]:
        descriptor, name = tempfile.mkstemp(
            prefix=self.prefix,
            suffix=".mix.part",
            dir=str(self.directory),
        )
        os.close(descriptor)
        path = Path(name)
        try:
            data = self.np.memmap(path, dtype=self.np.float32, mode="w+", shape=(channels, frames))
            data[:] = 0
            return path, data
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def ensure_capacity(self, channels: int, frames: int) -> None:
        requested_channels = max(self.channels, int(channels))
        requested_frames = max(self.frames, int(frames))
        if requested_channels == self.channels and requested_frames == self.frames:
            return
        next_channels = requested_channels
        next_frames = max(requested_frames, self.frames * 2 if requested_frames > self.frames else self.frames)
        next_path, next_data = self._create(next_channels, next_frames)
        try:
            if self.channels == 1 and next_channels > 1:
                for channel in range(next_channels):
                    next_data[channel, :self.frames] = self.data[0, :self.frames]
            else:
                next_data[:self.channels, :self.frames] = self.data[:, :self.frames]
        except Exception:
            del next_data
            next_path.unlink(missing_ok=True)
            raise
        self.data.flush()
        del self.data
        self.path.unlink(missing_ok=True)
        self.path = next_path
        self.data = next_data
        self.channels = next_channels
        self.frames = next_frames

    def add(self, start: int, segment: Any) -> None:
        segment_channels = int(segment.shape[0])
        end = int(start) + int(segment.shape[-1])
        self.ensure_capacity(segment_channels, end)
        if segment_channels == 1 and self.channels > 1:
            segment = self.np.repeat(segment, self.channels, axis=0)
        elif segment_channels < self.channels:
            segment = self.np.pad(segment, ((0, self.channels - segment_channels), (0, 0)))
        self.data[:, start:end] += segment[:self.channels]

    def block(self, start: int, end: int, channels: int) -> Any:
        return self.np.array(self.data[:channels, start:end], dtype=self.np.float32, copy=True)

    def close(self) -> None:
        try:
            self.data.flush()
        except Exception:
            pass
        try:
            del self.data
        except Exception:
            pass
        self.path.unlink(missing_ok=True)


def _rename_export_path(temporary: Path, path: Path) -> None:
    """Move a completed file without replacing a concurrent output."""
    if os.name == 'nt':
        os.rename(temporary, path)
        return
    import ctypes
    import errno

    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        rename = getattr(library, 'renamex_np', None)
        arguments = (os.fsencode(temporary), os.fsencode(path), 0x4)  # RENAME_EXCL
        signature = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    elif sys.platform.startswith('linux'):
        rename = getattr(library, 'renameat2', None)
        arguments = (-100, os.fsencode(temporary), -100, os.fsencode(path), 1)  # AT_FDCWD, RENAME_NOREPLACE
        signature = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    else:
        rename = None
    if rename is None:
        raise OSError(errno.ENOTSUP, 'Atomic output publishing is unavailable', str(path))
    rename.argtypes = signature
    rename.restype = ctypes.c_int
    if rename(*arguments) != 0:
        code = ctypes.get_errno() or errno.EIO
        raise OSError(code, os.strerror(code), str(path))


def _publish_export_path(temporary: Path, path: Path) -> Path:
    """Atomically publish a completed export without overwriting an earlier mix."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for index in range(1, 1000):
        candidate = path if index == 1 else path.with_name(f"{path.stem}_{index}{path.suffix}")
        try:
            os.link(temporary, candidate)
        except FileExistsError:
            continue
        except OSError:
            # Filesystems without hard links use the platform's exclusive
            # rename operation; never expose an empty final placeholder.
            try:
                _rename_export_path(temporary, candidate)
            except FileExistsError:
                continue
            return candidate
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # The final hard link is already complete. Worker/Rust cleanup can
            # remove a transiently locked task temp after the process exits.
            pass
        return candidate
    raise FileExistsError(f"Failed to publish a unique export filename: {path}")


def _audio_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    try:
        import av  # type: ignore
        with av.open(str(path)) as container:
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is None:
                raise RuntimeError("No audio stream found")
            duration = 0.0
            if stream.duration is not None and stream.time_base is not None:
                duration = float(stream.duration * stream.time_base)
            elif container.duration is not None:
                duration = float(container.duration / av.time_base)
            sample_rate = int(getattr(stream.codec_context, "sample_rate", 0) or 0)
            channels = int(getattr(stream.codec_context, "channels", 0) or 0)
            audio_tracks = [{
                "index": number,
                "language": str(track.metadata.get("language") or ""),
                "title": str(track.metadata.get("title") or ""),
                "channels": int(getattr(track.codec_context, "channels", 0) or 0),
            } for number, track in enumerate((s for s in container.streams if s.type == "audio"), start=1)]
            return {
                "path": str(path),
                "name": path.name,
                "duration": max(0.0, duration),
                "sampleRate": sample_rate,
                "channels": channels,
                "audioTracks": audio_tracks,
            }
    except Exception:
        try:
            import soundfile as sf  # type: ignore
            with sf.SoundFile(str(path)) as audio_file:
                frames = int(audio_file.frames)
                sample_rate = int(audio_file.samplerate)
                duration = frames / sample_rate if sample_rate else 0.0
                return {
                    "path": str(path),
                    "name": path.name,
                    "duration": max(0.0, duration),
                    "sampleRate": sample_rate,
                    "channels": int(audio_file.channels),
                }
        except Exception:
            raise


def _load_audio_channels(path: Path, sample_rate: int = 8000) -> tuple[Any, int]:
    import librosa  # type: ignore
    import numpy as np  # type: ignore

    audio, sr = librosa.load(str(path), sr=sample_rate, mono=False)
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim == 1:
        audio = audio.reshape(1, -1)
    return audio, int(sr)


def _waveform_peaks_soundfile(path: Path, resolution: int) -> tuple[list[float], list[list[float]], dict[str, Any]]:
    import numpy as np  # type: ignore
    import soundfile as sf  # type: ignore

    with sf.SoundFile(str(path)) as audio_file:
        frames = int(audio_file.frames)
        sample_rate = int(audio_file.samplerate)
        channels = int(audio_file.channels)
        duration = frames / sample_rate if sample_rate else 0.0
        bucket = max(1, math.ceil(max(1, frames) / max(1, resolution)))
        peaks: list[float] = []
        channel_peaks: list[list[float]] = [[] for _ in range(max(1, channels))]
        while True:
            block = audio_file.read(bucket, dtype="float32", always_2d=True)
            if block.size == 0:
                break
            peak = float(np.max(np.abs(block))) if block.size else 0.0
            peaks.append(round(peak, 5))
            per_channel = np.max(np.abs(block), axis=0) if block.size else []
            for index in range(len(channel_peaks)):
                value = float(per_channel[index]) if index < len(per_channel) else 0.0
                channel_peaks[index].append(round(value, 5))
    return peaks, channel_peaks, {
        "path": str(path),
        "name": path.name,
        "duration": max(0.0, duration),
        "sampleRate": sample_rate,
        "channels": channels,
    }


def _resample_audio(audio: Any, source_rate: int, target_rate: int) -> Any:
    if source_rate == target_rate:
        return audio
    import numpy as np  # type: ignore

    try:
        import soxr  # type: ignore
    except ImportError:
        import librosa  # type: ignore

        return librosa.resample(audio, orig_sr=source_rate, target_sr=target_rate)

    # Use the same HQ resampler as librosa without initializing its analysis/JIT
    # modules. SoXR expects frame-first arrays; the editor uses channel-first.
    samples = np.asarray(audio)
    if samples.shape[-1] == 0:
        return samples.copy()
    resampled = soxr.resample(samples.T, source_rate, target_rate, quality="HQ").T
    expected_frames = math.ceil(samples.shape[-1] * target_rate / source_rate)
    if resampled.shape[-1] < expected_frames:
        padding = [(0, 0)] * resampled.ndim
        padding[-1] = (0, expected_frames - resampled.shape[-1])
        resampled = np.pad(resampled, padding)
    return resampled[..., :expected_frames]


def _read_audio(path: Path, target_rate: int | None = None) -> tuple[Any, int]:
    import soundfile as sf  # type: ignore

    try:
        audio, sr = sf.read(str(path), dtype="float32", always_2d=True)
    except (OSError, RuntimeError):
        # Keep librosa's decoder fallback for formats not supported by libsndfile.
        import librosa  # type: ignore
        import numpy as np  # type: ignore

        audio, sr = librosa.load(str(path), sr=target_rate, mono=False)
        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim == 1:
            audio = audio.reshape(1, -1)
        return audio, int(sr)

    audio = audio.T
    if target_rate is not None and sr != target_rate:
        audio = _resample_audio(audio, int(sr), target_rate)
        sr = target_rate
    return audio, int(sr)


def _read_editor_audio(path: Path, target_rate: int | None = None) -> tuple[Any, int]:
    metadata = _audio_metadata(path)
    if int(metadata.get("channels") or 0) <= 2:
        return _read_audio(path, target_rate)

    # Use the preview decoder's layout-aware stereo downmix before applying
    # track gain and pan. Keeping six source channels here bypasses those controls.
    import numpy as np  # type: ignore

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to export multichannel audio")
    rate = int(target_rate or metadata.get("sampleRate") or 0)
    if rate <= 0:
        raise RuntimeError("The audio source has an invalid sample rate")
    result = subprocess.run([
        ffmpeg, "-nostdin", "-v", "error", "-threads", "1", "-i", str(path),
        "-map", "0:a:0", "-vn", "-ar", str(rate), "-ac", "2",
        "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1",
    ], capture_output=True, check=False, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[-2000:]
        raise RuntimeError(f"Unable to decode multichannel audio: {detail or result.returncode}")
    if len(result.stdout) % 8:
        raise RuntimeError("The audio decoder returned invalid stereo PCM data")
    return np.frombuffer(result.stdout, dtype="<f4").reshape(-1, 2).T.copy(), rate


def _equal_power_fade(length: int, fade_in: bool) -> Any:
    import numpy as np  # type: ignore

    if length <= 0:
        return np.ones((0,), dtype=np.float32)
    curve = np.linspace(0.0, 1.0, num=length, endpoint=True, dtype=np.float32)
    curve = np.sin(curve * math.pi / 2.0)
    return curve if fade_in else curve[::-1]


def _linear_fade(length: int, fade_in: bool) -> Any:
    import numpy as np  # type: ignore

    if length <= 0:
        return np.ones((0,), dtype=np.float32)
    curve = np.linspace(0.0, 1.0, num=length, endpoint=True, dtype=np.float32)
    return curve if fade_in else curve[::-1]


def _apply_stereo_pan(audio: Any, pan: float) -> Any:
    import numpy as np  # type: ignore

    normalized = max(-1.0, min(1.0, float(pan or 0.0)))
    if abs(normalized) <= 1e-6:
        return audio

    if audio.ndim == 1:
        audio = audio.reshape(1, -1)
    if audio.shape[0] == 1:
        audio = np.repeat(audio, 2, axis=0)

    left_gain = 1.0 if normalized <= 0 else 1.0 - normalized
    right_gain = 1.0 if normalized >= 0 else 1.0 + normalized

    output = audio.copy()
    output[0] *= left_gain
    output[1] *= right_gain
    return output


def cmd_audio_metadata(payload: dict[str, Any]) -> int:
    path = payload.get("path")
    if not path:
        return emit_error("AUDIO_METADATA_FAILED", "Missing audio path")
    try:
        emit("audio_metadata", _audio_metadata(Path(path)))
        return 0
    except Exception as exc:
        return emit_error("AUDIO_METADATA_FAILED", str(exc), traceback.format_exc())


def cmd_waveform_peaks(payload: dict[str, Any]) -> int:
    path_value = payload.get("path")
    if not path_value:
        return emit_error("WAVEFORM_PEAKS_FAILED", "Missing audio path")
    path = Path(path_value)
    resolution = int(payload.get("resolution") or 1400)
    resolution = max(80, min(12000, resolution))
    cache_dir = Path(payload.get("cacheDir") or path.parent / ".pymss-peaks")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha1(str(path.resolve()).encode("utf-8", errors="replace")).hexdigest()[:16]
    cache_name = f"{path.stem}_{cache_key}_{resolution}_v2.json"
    peaks_path = cache_dir / cache_name
    try:
        if peaks_path.is_file() and peaks_path.stat().st_mtime >= path.stat().st_mtime:
            data = json.loads(peaks_path.read_text(encoding="utf-8"))
            channel_peaks = data.get("channelPeaks") or []
            if isinstance(channel_peaks, list):
                data["channels"] = max(int(data.get("channels") or 0), len(channel_peaks))
            emit("waveform_peaks", data)
            return 0

        import numpy as np  # type: ignore

        try:
            peaks, channel_peaks, metadata = _waveform_peaks_soundfile(path, resolution)
            sr = int(metadata.get("sampleRate") or 0)
        except Exception:
            audio, sr = _load_audio_channels(path)
            total = int(audio.shape[-1])

            def build_channel_peaks(target_resolution: int) -> list[list[float]]:
                if total <= 0 or target_resolution <= 0:
                    return []
                bucket = max(1, math.ceil(total / target_resolution))
                padded = int(math.ceil(total / bucket) * bucket)
                work = audio
                if padded > total:
                    work = np.pad(audio, ((0, 0), (0, padded - total)))
                shaped = work.reshape(work.shape[0], -1, bucket)
                maxima = np.max(np.abs(shaped), axis=2)
                return [
                    [round(float(value), 5) for value in channel]
                    for channel in maxima
                ]

            channel_peaks = build_channel_peaks(resolution)
            peaks = [
                round(float(value), 5)
                for value in (np.max(np.asarray(channel_peaks, dtype=np.float32), axis=0) if channel_peaks else [])
            ]
            metadata = _audio_metadata(path)

        channel_count = max(int(metadata.get("channels", 0) or 0), len(channel_peaks))
        data = {
            "path": str(path),
            "peaksPath": str(peaks_path),
            "peaks": peaks,
            "channelPeaks": channel_peaks,
            "resolution": resolution,
            "duration": metadata.get("duration", 0),
            "sampleRate": metadata.get("sampleRate") or sr,
            "channels": channel_count,
        }
        peaks_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        emit("waveform_peaks", data)
        return 0
    except Exception as exc:
        return emit_error("WAVEFORM_PEAKS_FAILED", str(exc), traceback.format_exc())


def cmd_export_editor_mix(payload: dict[str, Any]) -> int:
    project = payload.get("project") or {}
    export_dir = Path(payload.get("exportDir") or ".")
    output_format = str(payload.get("format") or "wav").lower()
    audio_params = payload.get("audioParams") or {}
    task_id_value = str(payload.get("taskId") or payload.get("requestId") or "").strip()
    task_id = task_id_value or None
    safe_task_id = "".join(
        character if character.isascii() and (character.isalnum() or character in "-_") else "_"
        for character in (task_id or "sync")
    )[:96] or "sync"
    cancel_path = export_dir / f".pymss-export-{safe_task_id}.cancel" if task_id else None
    mix_buffer: _DiskMixBuffer | None = None

    def fail(code: str, message: str, detail: str | None = None) -> int:
        return emit_error(code, message, detail, task_id=task_id)

    def report_progress(phase: str, completed: int, current: str = "") -> None:
        emit("editor_export_progress", {
            "phase": phase,
            "completed": max(0, min(100, int(completed))),
            "total": 100,
            "current": current,
        }, task_id=task_id)

    def check_cancelled() -> None:
        if cancel_path is not None and cancel_path.exists():
            raise _EditorExportCancelled()

    if output_format not in {"wav", "flac"}:
        return fail("EDITOR_EXPORT_FORMAT_UNSUPPORTED", f"Unsupported editor export format: {output_format}")
    if not project.get("tracks"):
        return fail("EDITOR_EXPORT_FAILED", "Project has no tracks")
    try:
        import numpy as np  # type: ignore
        import soundfile as sf  # type: ignore

        if export_dir.exists() and not export_dir.is_dir():
            return fail("EDITOR_EXPORT_DIR_INVALID", "Export path is not a directory")
        export_dir.mkdir(parents=True, exist_ok=True)
        report_progress("preparing", 2)
        check_cancelled()

        for stale_file in export_dir.glob(".pymss-export-*.part"):
            try:
                if time.time() - stale_file.stat().st_mtime > 24 * 60 * 60:
                    stale_file.unlink(missing_ok=True)
            except OSError:
                pass

        sources: dict[str, dict[str, Any]] = {}
        for collection_name in ("assets", "sources"):
            for item in project.get(collection_name, []) or []:
                source_id = item.get("id")
                if source_id:
                    sources[str(source_id)] = item

        tracks = project.get("tracks", []) or []
        active_tracks = [
            track for track in tracks
            if not track.get("muted") and (track.get("sourceId") or track.get("clips"))
        ]
        has_solo = any(bool(track.get("solo")) for track in tracks)
        audio_cache: dict[str, tuple[Any, int]] = {}
        target_rate: int | None = None
        rendered_clip_count = 0
        rendered_channels = 0
        total_samples = 0

        def source_for_clip(track: dict[str, Any], clip: dict[str, Any]) -> dict[str, Any] | None:
            source_id = clip.get("assetId") or track.get("sourceId")
            if not source_id:
                return None
            return sources.get(str(source_id))

        def track_clips(track: dict[str, Any]) -> list[dict[str, Any]]:
            clips = track.get("clips")
            if isinstance(clips, list) and clips:
                return [clip for clip in clips if isinstance(clip, dict)]

            source_id = track.get("sourceId")
            source = sources.get(str(source_id)) if source_id else None
            source_duration = float(source.get("duration", 0) or 0) if source else 0.0
            return [{
                "id": f"clip_{track.get('id', 'track')}",
                "assetId": source_id,
                "start": 0,
                "offset": 0,
                "duration": source_duration,
                "volume": 1,
                "fadeIn": track.get("fadeIn", 0),
                "fadeOut": track.get("fadeOut", 0),
                "muted": False,
            }]

        def read_source_audio(source: dict[str, Any]) -> tuple[Any, int]:
            nonlocal target_rate
            source_id = str(source.get("id") or source.get("path") or "")
            if source_id in audio_cache:
                return audio_cache[source_id]

            path = Path(source.get("path") or "")
            if not path.is_file():
                raise FileNotFoundError(str(path))

            audio, sr = _read_editor_audio(path, target_rate)
            if target_rate is None:
                target_rate = sr
            elif sr != target_rate:
                channels = [_resample_audio(channel, sr, target_rate) for channel in audio]
                audio = np.stack(channels, axis=0).astype(np.float32)
                sr = target_rate
            audio_cache[source_id] = (audio, int(sr))
            return audio_cache[source_id]

        render_items: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        missing_sources: list[str] = []
        for track in active_tracks:
            if (has_solo and not track.get("solo")) or float(track.get("volume", 1.0) or 0) <= 0:
                continue
            for clip in track_clips(track):
                if clip.get("muted") or float(clip.get("volume", 1.0) or 0) <= 0:
                    continue
                source = source_for_clip(track, clip)
                if not source:
                    missing_sources.append(str(clip.get("assetId") or track.get("sourceId") or clip.get("id") or "unknown"))
                    continue
                path = Path(source.get("path") or "")
                if not path.is_file():
                    missing_sources.append(str(source.get("name") or path or source.get("id") or "unknown"))
                    continue
                render_items.append((track, clip, source))

        if missing_sources:
            unique_missing = list(dict.fromkeys(missing_sources))
            preview = ", ".join(unique_missing[:5])
            if len(unique_missing) > 5:
                preview += f" (+{len(unique_missing) - 5} more)"
            return fail("EDITOR_EXPORT_SOURCE_MISSING", f"Missing source files: {preview}")
        if not render_items:
            return fail("EDITOR_EXPORT_FAILED", "No audible clips to export")
        report_progress("preparing", 8)

        sample_rate_value = audio_params.get("sample_rate", audio_params.get("sampleRate", "auto"))
        if str(sample_rate_value).strip().lower() != "auto":
            requested_rate = int(sample_rate_value)
            if requested_rate not in {32000, 44100, 48000}:
                return fail("EDITOR_EXPORT_SAMPLE_RATE_INVALID", f"Unsupported export sample rate: {requested_rate}")
            target_rate = requested_rate
        else:
            source_rates = {
                int(source.get("sampleRate") or 0)
                for _, _, source in render_items
                if int(source.get("sampleRate") or 0) > 0
            }
            # Native plugin renders use the preview clock; keep the original sources'
            # sample-rate selection for the final automatic export encoding.
            original_rate = int(project.get("sourceSampleRate") or 0)
            if 8000 <= original_rate <= 48000:
                source_rates = {original_rate}
            if not source_rates:
                for source in {str(item[2].get("id") or item[2].get("path")): item[2] for item in render_items}.values():
                    metadata = _audio_metadata(Path(source.get("path") or ""))
                    sample_rate = int(metadata.get("sampleRate") or 0)
                    if sample_rate > 0:
                        source_rates.add(sample_rate)
            if source_rates:
                target_rate = min(max(source_rates), 48000)
        report_progress("preparing", 12)

        if target_rate:
            planned_channels = 1
            planned_samples = 0
            for track, clip, source in render_items:
                source_channels = min(2, max(1, int(source.get("channels") or 1)))
                if source_channels == 1 and abs(float(track.get("pan", 0.0) or 0.0)) > 1e-6:
                    source_channels = 2
                planned_channels = max(planned_channels, source_channels)
                start_samples = max(0, int(float(clip.get("start", 0) or 0) * target_rate))
                offset_seconds = max(0.0, float(clip.get("offset", 0) or 0))
                duration_seconds = float(clip.get("duration", 0) or 0)
                if duration_seconds <= 0:
                    duration_seconds = max(0.0, float(source.get("duration", 0) or 0) - offset_seconds)
                planned_samples = max(
                    planned_samples,
                    start_samples + max(0, int(duration_seconds * target_rate)),
                )
            if planned_samples > 0:
                mix_buffer = _DiskMixBuffer(
                    np,
                    export_dir,
                    f".pymss-export-{safe_task_id}-",
                    planned_channels,
                    planned_samples,
                )

        source_remaining: dict[str, int] = {}
        for _, _, source in render_items:
            source_key = str(source.get("id") or source.get("path") or "")
            source_remaining[source_key] = source_remaining.get(source_key, 0) + 1

        render_item_count = len(render_items)
        for render_index, (track, clip, source) in enumerate(render_items, 1):
            source_key = str(source.get("id") or source.get("path") or "")
            audio = None
            segment = None
            try:
                check_cancelled()
                report_progress(
                    "rendering",
                    12 + int((render_index - 1) / max(1, render_item_count) * 74),
                    str(source.get("name") or Path(source.get("path") or "").name),
                )
                audio, sr = read_source_audio(source)
                check_cancelled()
                start = max(0, int(float(clip.get("start", 0) or 0) * sr))
                offset = max(0, int(float(clip.get("offset", 0) or 0) * sr))
                if offset >= audio.shape[-1]:
                    continue

                clip_duration = float(clip.get("duration", 0) or 0)
                duration_samples = int(clip_duration * sr) if clip_duration > 0 else audio.shape[-1] - offset
                duration_samples = max(0, min(duration_samples, audio.shape[-1] - offset))
                if duration_samples <= 0:
                    continue

                segment = audio[:, offset:offset + duration_samples].copy()
                segment *= float(track.get("volume", 1.0) or 0) * float(clip.get("volume", 1.0) or 0)
                segment = _apply_stereo_pan(segment, float(track.get("pan", 0.0) or 0.0))

                fade_in_value = clip.get("fadeIn", track.get("fadeIn", 0))
                fade_out_value = clip.get("fadeOut", track.get("fadeOut", 0))
                fade_in_samples = min(duration_samples, int(float(fade_in_value or 0) * sr))
                fade_out_samples = min(duration_samples, int(float(fade_out_value or 0) * sr))
                if fade_in_samples > 0:
                    segment[:, :fade_in_samples] *= _linear_fade(fade_in_samples, True)
                if fade_out_samples > 0:
                    segment[:, -fade_out_samples:] *= _linear_fade(fade_out_samples, False)

                end = start + segment.shape[-1]
                segment_channels = segment.shape[0]
                rendered_channels = max(rendered_channels, segment_channels)
                if mix_buffer is None:
                    mix_buffer = _DiskMixBuffer(
                        np,
                        export_dir,
                        f".pymss-export-{safe_task_id}-",
                        segment_channels,
                        end,
                    )
                mix_buffer.add(start, segment)
                rendered_clip_count += 1
                total_samples = max(total_samples, end)
            finally:
                source_remaining[source_key] -= 1
                if source_remaining[source_key] <= 0:
                    audio_cache.pop(source_key, None)
                audio = None
                segment = None
            report_progress(
                "rendering",
                12 + int(render_index / max(1, render_item_count) * 74),
                str(source.get("name") or Path(source.get("path") or "").name),
            )

        if mix_buffer is None or rendered_clip_count <= 0 or not target_rate or total_samples <= 0:
            return fail("EDITOR_EXPORT_FAILED", "No audible clips to export")
        master_volume = float(project.get("masterVolume", 1.0) or 0)
        master_pan = float(project.get("masterPan", 0.0) or 0.0)
        channels = 2 if rendered_channels == 1 and abs(master_pan) > 1e-6 else rendered_channels
        block_frames = 256 * 1024
        block_count = max(1, math.ceil(total_samples / block_frames))

        def master_block(start: int, end: int) -> Any:
            block = mix_buffer.block(start, end, rendered_channels)
            if master_volume != 1.0:
                block *= master_volume
            return _apply_stereo_pan(block, master_pan)

        peak = 0.0
        for block_index, start in enumerate(range(0, total_samples, block_frames), 1):
            check_cancelled()
            end = min(total_samples, start + block_frames)
            block = master_block(start, end)
            if block.size:
                peak = max(peak, float(np.max(np.abs(block))))
            report_progress("finalizing", 86 + int(block_index / block_count * 6))

        peak_protection_value = audio_params.get("peak_protection", audio_params.get("peakProtection"))
        requested_wav_subtype = str(
            audio_params.get("wav_bit_depth") or audio_params.get("wavBitDepth") or "PCM_24"
        ).upper()
        float_output = output_format == "wav" and requested_wav_subtype == "FLOAT"
        peak_protection = bool(peak_protection_value)
        if not float_output and peak > 1.0 and not peak_protection:
            return fail(
                "EDITOR_EXPORT_PEAK_EXCEEDED",
                "Mix peaks exceed the output format's range. Lower the master volume, use WAV FLOAT, or enable peak protection.",
            )
        peak_adjustment_db = 0.0
        peak_protection_applied = peak_protection and peak > 1.0
        output_gain = 1.0
        if peak_protection_applied:
            output_gain = 0.98 / peak
            peak_adjustment_db = 20.0 * math.log10(output_gain)

        project_name = str(project.get("name") or project.get("id") or "editor_mix")
        requested_name = str(payload.get("fileName") or "").strip()
        name_source = requested_name or project_name
        for suffix in (".wav", ".flac"):
            if name_source.lower().endswith(suffix):
                name_source = name_source[:-len(suffix)]
                break
        safe_name = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name_source).strip(" ._") or "editor_mix"
        windows_stem = safe_name.split(".", 1)[0].upper()
        if windows_stem in {"CON", "PRN", "AUX", "NUL"} or (
            windows_stem.startswith(("COM", "LPT")) and windows_stem[3:] in set("123456789")
        ):
            safe_name = f"_{safe_name}"
        output_path = export_dir / f"{safe_name}_mix.{output_format}"
        if requested_name:
            output_path = export_dir / f"{safe_name}.{output_format}"
        subtype = None
        if output_format == "wav":
            requested = str(audio_params.get("wav_bit_depth") or audio_params.get("wavBitDepth") or "PCM_24").upper()
            subtype = requested if requested in {"PCM_16", "PCM_24", "FLOAT"} else "PCM_24"
        elif output_format == "flac":
            requested = str(audio_params.get("flac_bit_depth") or audio_params.get("flacBitDepth") or "PCM_24").upper()
            subtype = requested if requested in {"PCM_16", "PCM_24"} else "PCM_24"

        write_kwargs: dict[str, Any] = {}
        if subtype:
            write_kwargs["subtype"] = subtype
        temporary_path: Path | None = None
        try:
            check_cancelled()
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".pymss-export-{safe_task_id}-",
                suffix=f".{output_format}.part",
                dir=str(export_dir),
            )
            os.close(file_descriptor)
            temporary_path = Path(temporary_name)
            with sf.SoundFile(
                str(temporary_path),
                mode="w",
                samplerate=target_rate,
                channels=channels,
                format=output_format.upper(),
                **write_kwargs,
            ) as output_file:
                for block_index, start in enumerate(range(0, total_samples, block_frames), 1):
                    check_cancelled()
                    end = min(total_samples, start + block_frames)
                    block = master_block(start, end)
                    if output_gain != 1.0:
                        block *= output_gain
                    output_file.write(block.T)
                    report_progress(
                        "writing",
                        92 + int(block_index / block_count * 7),
                        output_path.name,
                    )
            check_cancelled()
            published_path = _publish_export_path(temporary_path, output_path)
            temporary_path = None
            output_path = published_path
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
        report_progress("completed", 100, output_path.name)
        emit("editor_mix_exported", {
            "path": str(output_path),
            "duration": total_samples / target_rate,
            "sampleRate": target_rate,
            "channels": channels,
            "format": output_path.suffix.lstrip("."),
            "peakProtectionApplied": peak_protection_applied,
            "peakAdjustmentDb": peak_adjustment_db,
        }, task_id=task_id)
        return 0
    except _EditorExportCancelled:
        emit("task_cancelled", {"message": "Cancelled"}, task_id=task_id)
        return 0
    except Exception as exc:
        return fail("EDITOR_EXPORT_FAILED", str(exc), traceback.format_exc())
    finally:
        if mix_buffer is not None:
            mix_buffer.close()
        if cancel_path is not None:
            cancel_path.unlink(missing_ok=True)
