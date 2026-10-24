"""Studio audio-tools → pymss Operation adapter.

Replaces the per-tool dedicated implementations (audio_tools/*.py) with the
matching pymss Operations while keeping the exact worker event contract:

* ``audio_tool_progress`` — phase/completed/total/current payloads the tools UI
  already renders (phases are translated from operation progress messages)
* ``audio_tool_result``   — the tool-shaped result dicts the result panes render

Tool → Operation mapping:

* convert → pymss.kit.convert     (batch: one run per file)
* inspect → pymss.kit.inspect
* sdr     → pymss.kit.sdr
* slicer  → pymss.kit.slice       (batch: one run per file)
* merge   → pymss.kit.merge       (directory scan + ordering here, one run)
* midi    → pymss.game.vocal_to_midi plugin operation when installed,
            else the legacy dedicated implementation (out-of-box fallback)
* asr     → pymss.asr.transcribe plugin operation when installed,
            else the legacy dedicated implementation

Keeping midi/asr on a plugin-optional fallback preserves the out-of-box
experience: the heavy model stacks stay opt-in via the marketplace, while the
five kit tools are always available (ffmpeg only).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from worker_protocol import emit

# tools whose dedicated implementation is fully replaced by a kit operation
_TOOL_TO_OPERATION = {
    "convert": "pymss.kit.convert",
    "inspect": "pymss.kit.inspect",
    "sdr": "pymss.kit.sdr",
    "slicer": "pymss.kit.slice",
    "merge": "pymss.kit.merge",
}

# tools served by plugin operations when available
_PLUGIN_TOOL_OPERATIONS = {
    "midi": "pymss.game.vocal_to_midi",
    "asr": "pymss.asr.transcribe",
}

# Tools without a plugin operation route straight to their legacy handler.
_LEGACY_ONLY_TOOLS = {"loudness"}


def _runtime():
    from worker_operations import _runtime as operations_runtime

    return operations_runtime()


def _operation_available(runtime: Any, operation_id: str) -> bool:
    try:
        runtime.describe_operation(operation_id)
        return True
    except Exception:
        return False


def _file_input(path: str) -> dict[str, Any]:
    return {"kind": "file", "path": str(path)}


def _emit_progress(operation: str, phase: str, completed: int, total: int, current: str) -> None:
    emit("audio_tool_progress", {
        "operation": operation, "phase": phase,
        "completed": completed, "total": total, "current": current,
    })


def _unwrap(value: Any) -> Any:
    """Unwrap the {kind, value} serialization worker_operations produces."""
    if isinstance(value, dict) and "kind" in value and set(value.keys()) <= {"kind", "value", "sampleRate", "path"}:
        return value.get("value", value)
    return value


def _run_tool_convert(payload: dict[str, Any]) -> dict[str, Any]:
    from audio_tools.common import _require_directory, _require_file

    raw_inputs = payload.get("inputs")
    if not isinstance(raw_inputs, list) or not raw_inputs:
        raise ValueError("At least one input audio file is required")
    inputs = [_require_file(value, "Input audio") for value in raw_inputs]
    output_dir = _require_directory(payload.get("outputDir"), "Output directory", create=True)
    params = {
        "output_dir": str(output_dir),
        "format": str(payload.get("outputFormat") or "wav").strip().lower(),
        "sample_rate": int(payload.get("sampleRate") or 44100),
        "channels": int(payload.get("channels") or 2),
        "wav_bit_depth": str(payload.get("wavBitDepth") or "PCM-24").upper(),
        "flac_bit_depth": str(payload.get("flacBitDepth") or "16-bit"),
        "mp3_bit_rate": str(payload.get("mp3BitRate") or "320k").lower(),
        "ogg_bit_rate": str(payload.get("oggBitRate") or "320k").lower(),
    }
    runtime = _runtime()
    operation = _TOOL_TO_OPERATION["convert"]
    output_paths: list[str] = []
    failures: list[dict[str, str]] = []
    total = len(inputs)
    for index, source in enumerate(inputs, start=1):
        _emit_progress("convert", "converting", index - 1, total, source.name)
        try:
            result = runtime.run(
                operation, inputs={"audio": str(source)}, params=params,
            )
            path = _unwrap(result.get("audio_file"))
            if isinstance(path, str):
                output_paths.append(path)
        except Exception as error:  # noqa: BLE001 — batch tools report per-file failures
            failures.append({"path": str(source), "message": str(error)})
        _emit_progress("convert", "converting", index, total, source.name)
    if not output_paths:
        raise ValueError(failures[0]["message"] if failures else "No files were converted")
    return {
        "operation": "convert",
        "outputDir": str(output_dir),
        "outputPaths": output_paths,
        "succeeded": len(output_paths),
        "failed": failures,
    }


def _run_tool_inspect(payload: dict[str, Any]) -> dict[str, Any]:
    from audio_tools.common import _require_file

    source = _require_file(payload.get("inputPath"), "Input audio")
    _emit_progress("inspect", "probing", 0, 1, source.name)
    runtime = _runtime()
    result = runtime.run(
        _TOOL_TO_OPERATION["inspect"], inputs={"audio": str(source)}, params={},
    )
    _emit_progress("inspect", "probing", 1, 1, source.name)
    report = _unwrap(result.get("report")) or {}
    streams = report.get("streams") or []
    return {
        "operation": "inspect",
        "inputPath": str(source),
        "fileName": report.get("file_name", source.name),
        "fileSize": report.get("file_size", source.stat().st_size),
        "format": {
            "name": report.get("container", ""),
            "longName": "",
            "duration": report.get("duration_seconds"),
            "startTime": None,
            "bitRate": report.get("bit_rate"),
            "streamCount": len(streams) or None,
            "probeScore": None,
            "tags": {},
        },
        "audioStreams": [
            {
                "index": None,
                "codec": stream.get("codec", ""),
                "codecLongName": "",
                "profile": "",
                "sampleFormat": stream.get("sample_format", ""),
                "sampleRate": stream.get("sample_rate"),
                "channels": stream.get("channels"),
                "channelLayout": stream.get("channel_layout", ""),
                "bitsPerSample": None,
                "bitRate": stream.get("bit_rate"),
                "startTime": None,
                "duration": stream.get("duration_seconds"),
                "timeBase": "",
                "frameCount": None,
                "default": False,
                "tags": {},
            }
            for stream in streams
        ],
        "chapterCount": 0,
        "raw": {},
    }


def _run_tool_sdr(payload: dict[str, Any]) -> dict[str, Any]:
    from audio_tools.common import _require_file

    reference = _require_file(payload.get("referencePath"), "Reference audio")
    estimated = _require_file(payload.get("estimatedPath"), "Estimated audio")
    _emit_progress("sdr", "loading_reference", 0, 2, reference.name)
    _emit_progress("sdr", "loading_estimated", 1, 2, estimated.name)
    _emit_progress("sdr", "calculating", 0, 1, "")
    runtime = _runtime()
    result = runtime.run(
        _TOOL_TO_OPERATION["sdr"],
        inputs={"reference": str(reference), "estimated": str(estimated)},
        params={},
    )
    report = _unwrap(result.get("report")) or {}
    return {
        "operation": "sdr",
        "referencePath": str(reference),
        "estimatedPath": str(estimated),
        "sampleRate": report.get("sample_rate", 44100),
        "sdr": report.get("sdr", []),
        "averageSdr": report.get("average_sdr", 0.0),
        "siSdr": report.get("si_sdr", []),
        "averageSiSdr": report.get("average_si_sdr", 0.0),
    }


def _run_tool_slicer(payload: dict[str, Any]) -> dict[str, Any]:
    from audio_tools.common import _require_directory, _require_file

    raw_inputs = payload.get("inputs")
    if not isinstance(raw_inputs, list) or not raw_inputs:
        raise ValueError("At least one input audio file is required")
    inputs = [_require_file(value, "Input audio") for value in raw_inputs]
    output_dir = _require_directory(payload.get("outputDir"), "Output directory", create=True)
    runtime = _runtime()
    params = {
        "output_dir": str(output_dir),
        "format": str(payload.get("outputFormat") or "wav").lower(),
        "threshold_db": float(payload.get("threshold", -40)),
        "min_length_ms": int(payload.get("minLength", 5000)),
        "min_interval_ms": int(payload.get("minInterval", 300)),
        "hop_size_ms": int(payload.get("hopSize", 10)),
        "max_silence_kept_ms": int(payload.get("maxSilKept", 500)),
    }
    operation = _TOOL_TO_OPERATION["slicer"]
    segments: list[dict[str, Any]] = []
    output_paths: list[str] = []
    kept = 0.0
    total = len(inputs)
    for index, source in enumerate(inputs, start=1):
        _emit_progress("slicer", "analyzing_silence", index - 1, total, source.name)
        result = runtime.run(operation, inputs={"audio": str(source)}, params=params)
        files = _unwrap(result.get("files")) or []
        report = _unwrap(result.get("report")) or {}
        intervals = report.get("intervals") or []
        for order, path in enumerate(files):
            start = float(intervals[order]["start"]) if order < len(intervals) else 0.0
            end = float(intervals[order]["end"]) if order < len(intervals) else 0.0
            segments.append({
                "sourcePath": str(source), "outputPath": str(path),
                "start": start, "end": end, "duration": end - start,
            })
            output_paths.append(str(path))
            kept += end - start
        _emit_progress("slicer", "writing_segments", index, total, source.name)
    if not segments:
        raise ValueError("No non-silent audio segments were detected")
    return {
        "operation": "slicer",
        "outputDir": str(output_dir),
        "outputPaths": output_paths,
        "sourceCount": total,
        "segments": segments,
        "keptDuration": kept,
        "succeeded": len(output_paths),
        "failed": [],
    }


def _run_tool_merge(payload: dict[str, Any]) -> dict[str, Any]:
    from audio_tools.common import _require_directory
    from audio_tools.merge import _audio_files_in_folder, _order_audio_files

    input_dir = _require_directory(payload.get("inputDir"), "Input directory")
    output_dir = _require_directory(payload.get("outputDir"), "Output directory", create=True)
    sort_by = str(payload.get("sortBy") or "name").strip().lower()
    sort_direction = str(payload.get("sortDirection") or "asc").strip().lower()
    regex_pattern = str(payload.get("regexPattern") or "")
    inputs = _audio_files_in_folder(input_dir)
    if not inputs:
        raise ValueError("The input directory does not contain supported audio files")
    inputs = _order_audio_files(inputs, sort_by, sort_direction, regex_pattern)
    runtime = _runtime()
    total = len(inputs)
    _emit_progress("merge", "normalizing", 0, total, inputs[0].name if inputs else "")
    extras = ";".join(str(path) for path in inputs[1:])
    _emit_progress("merge", "merging", 0, 1, "merged")
    result = runtime.run(
        _TOOL_TO_OPERATION["merge"],
        inputs={"audio": str(inputs[0])},
        params={"output_dir": str(output_dir), "additional_inputs": extras},
    )
    merged = _unwrap(result.get("audio_file"))
    return {
        "operation": "merge",
        "outputDir": str(output_dir),
        "outputPath": str(merged),
        "merged": total,
        "sortBy": sort_by if sort_by in ("name", "modified", "regex") else "name",
        "sortDirection": sort_direction if sort_direction in ("asc", "desc") else "asc",
        "succeeded": 1,
        "skipped": [],
    }


def _run_plugin_tool(tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run midi/asr through their plugin operations; fallback to legacy on absence."""
    if tool in _LEGACY_ONLY_TOOLS:
        operation = None
    else:
        runtime = _runtime()
        operation = _PLUGIN_TOOL_OPERATIONS[tool]
        if not _operation_available(runtime, operation):
            operation = None
    if operation is None:
        # Legacy dedicated implementation keeps the tool working out of the box.
        # Route through worker_tools' module attribute so tests (and any future
        # overrides) that patch it there are honored on this path too.
        from worker_tools import get_audio_tool_handler

        handler = get_audio_tool_handler(tool)
        if handler is None:
            from audio_tools.registry import get_audio_tool_handler as registry_handler

            handler = registry_handler(tool)
        return handler(payload)  # type: ignore[return-value]
    from audio_tools_op_bridge import run_plugin_operation_as_tool

    return run_plugin_operation_as_tool(runtime, tool, operation, payload)
    if _operation_available(runtime, operation):
        from audio_tools_op_bridge import run_plugin_operation_as_tool

        return run_plugin_operation_as_tool(runtime, tool, operation, payload)
    # Legacy dedicated implementation keeps the tool working out of the box.
    # Route through worker_tools' module attribute so tests (and any future
    # overrides) that patch it there are honored on this path too.
    from worker_tools import get_audio_tool_handler

    handler = get_audio_tool_handler(tool)
    if handler is None:
        from audio_tools.registry import get_audio_tool_handler as registry_handler

        handler = registry_handler(tool)
    return handler(payload)  # type: ignore[return-value]


_ADAPTERS = {
    "convert": _run_tool_convert,
    "inspect": _run_tool_inspect,
    "sdr": _run_tool_sdr,
    "slicer": _run_tool_slicer,
    "merge": _run_tool_merge,
    "midi": lambda payload: _run_plugin_tool("midi", payload),
    "asr": lambda payload: _run_plugin_tool("asr", payload),
    "loudness": lambda payload: _run_plugin_tool("loudness", payload),
}


def run_audio_tool_via_operations(payload: dict[str, Any]) -> dict[str, Any]:
    """Entry point used by worker_tools.cmd_audio_tools."""
    operation = str(payload.get("operation") or "").strip().lower()
    adapter = _ADAPTERS.get(operation)
    if adapter is None:
        raise ValueError(f"audio tool {operation!r} has no operation adapter")
    return adapter(payload)
