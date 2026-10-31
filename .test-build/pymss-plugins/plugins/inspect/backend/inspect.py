from __future__ import annotations

import json
import subprocess
from typing import Any

from pymss.plugins.kit import emit, ffprobe_path, require_file as _require_file, run_process


def _number(value: Any, *, integer: bool = False) -> int | float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return int(value) if integer else float(value)
    except (TypeError, ValueError):
        return None


def _inspect_audio(payload: dict[str, Any]) -> dict[str, Any]:
    input_path = _require_file(payload.get("inputPath"), "Input audio")
    emit("audio_tool_progress", {
        "operation": "inspect", "phase": "probing", "completed": 0,
        "total": 1, "current": input_path.name,
    })
    try:
        completed = run_process([
            ffprobe_path(), "-v", "error", "-of", "json",
            "-show_format", "-show_streams", "-show_chapters", str(input_path),
        ])
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        raise RuntimeError(detail[-2000:] or f"FFprobe exited with status {error.returncode}") from error
    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("FFprobe returned invalid JSON") from error

    format_data = raw.get("format") if isinstance(raw.get("format"), dict) else {}
    audio_streams: list[dict[str, Any]] = []
    for stream in raw.get("streams") or []:
        if not isinstance(stream, dict) or stream.get("codec_type") != "audio":
            continue
        audio_streams.append({
            "index": _number(stream.get("index"), integer=True),
            "codec": stream.get("codec_name") or "",
            "codecLongName": stream.get("codec_long_name") or "",
            "profile": stream.get("profile") or "",
            "sampleFormat": stream.get("sample_fmt") or "",
            "sampleRate": _number(stream.get("sample_rate"), integer=True),
            "channels": _number(stream.get("channels"), integer=True),
            "channelLayout": stream.get("channel_layout") or "",
            "bitsPerSample": _number(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample"), integer=True),
            "bitRate": _number(stream.get("bit_rate"), integer=True),
            "startTime": _number(stream.get("start_time")),
            "duration": _number(stream.get("duration")),
            "timeBase": stream.get("time_base") or "",
            "frameCount": _number(stream.get("nb_frames"), integer=True),
            "default": bool((stream.get("disposition") or {}).get("default")),
            "tags": stream.get("tags") if isinstance(stream.get("tags"), dict) else {},
        })
    if not audio_streams:
        raise ValueError("The selected file does not contain an audio stream")

    emit("audio_tool_progress", {
        "operation": "inspect", "phase": "probing", "completed": 1,
        "total": 1, "current": input_path.name,
    })
    return {
        "operation": "inspect",
        "inputPath": str(input_path),
        "fileName": input_path.name,
        "fileSize": input_path.stat().st_size,
        "format": {
            "name": format_data.get("format_name") or "",
            "longName": format_data.get("format_long_name") or "",
            "duration": _number(format_data.get("duration")),
            "startTime": _number(format_data.get("start_time")),
            "bitRate": _number(format_data.get("bit_rate"), integer=True),
            "streamCount": _number(format_data.get("nb_streams"), integer=True),
            "probeScore": _number(format_data.get("probe_score"), integer=True),
            "tags": format_data.get("tags") if isinstance(format_data.get("tags"), dict) else {},
        },
        "audioStreams": audio_streams,
        "chapterCount": len(raw.get("chapters") or []),
        "raw": raw,
    }
