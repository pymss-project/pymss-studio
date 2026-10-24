"""Plugin-operation bridges for the midi/asr audio tools.

Translates the tools-UI payloads to plugin operation calls and the operation
outputs back to the exact tool result shapes (MidiResult / AsrResult).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from audio_tools.common import _require_directory, _require_file
from worker_protocol import emit


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict) and "kind" in value and set(value.keys()) <= {"kind", "value", "sampleRate", "path"}:
        return value.get("value", value)
    return value


def _resolved_inputs(entries: dict[str, Any]) -> dict[str, Any]:
    """Resolve {kind:file} descriptors the same way cmd_run_operation does.

    runtime.run receives raw values; the plugin-side run() reads
    source_path off the loaded AudioArtifact. Without this, file
    descriptors reach the plugin as plain dicts and fail with
    'requires an audio file input'.
    """
    from worker_operations import _resolve_input

    return {name: _resolve_input(value) for name, value in entries.items()}


def _progress(tool: str, phase: str, completed: int, total: int, current: str) -> None:
    emit("audio_tool_progress", {
        "operation": tool, "phase": phase,
        "completed": completed, "total": total, "current": current,
    })


def run_plugin_operation_as_tool(runtime: Any, tool: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    if tool == "midi":
        return _midi(runtime, operation, payload)
    if tool == "asr":
        return _asr(runtime, operation, payload)
    raise ValueError(f"no plugin-operation bridge for tool {tool!r}")


def _midi(runtime: Any, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    source = _require_file(payload.get("inputPath"), "Input audio")
    model_value = str(payload.get("modelPath") or "").strip()
    if model_value:
        model_path = Path(model_value).expanduser()
        if not model_path.is_file():
            raise ValueError("A GAME model .pt file is required")
        model_param: dict[str, Any] = {"model_path": str(model_path)}
        model_label = model_path.name
    else:
        # Apple Silicon default: let the plugin auto-download the gamemlx
        # alias (medium). Non-Apple platforms fail inside the operation with
        # a clear message asking for an explicit checkpoint.
        alias = str(payload.get("modelAlias") or "medium")
        model_param = {"model_alias": alias}
        model_label = f"gamemlx:{alias}"
    output_dir = _require_directory(payload.get("outputDir"), "Output directory", create=True)
    bpm = float(payload.get("bpm") or 120.0)
    language = str(payload.get("language") or "").strip() or None

    _progress("midi", "loading_model", 0, 2, model_label)
    result = runtime.run(
        operation,
        inputs=_resolved_inputs({"audio": {"kind": "file", "path": str(source)}}),
        params={
            **model_param,
            "tempo": bpm,
            "output_dir": str(output_dir),
            "language": language or "",
        },
    )
    _progress("midi", "transcribing", 1, 2, source.name)
    report = _unwrap(result.get("report")) or {}
    # The plugin reports the produced MIDI path; surface tool-shaped fields.
    midi_path = str(report.get("output_midi") or report.get("result") or "")
    candidates = sorted(Path(str(report.get("output_dir") or output_dir)).glob(f"{source.stem}*.mid"))
    output_path = midi_path if midi_path else (str(candidates[0]) if candidates else "")
    return {
        "operation": "midi",
        "inputPath": str(source),
        "outputDir": str(output_dir),
        "outputPath": output_path,
        "bpm": bpm,
        "language": language or "",
        "noteCount": int(report.get("note_count") or 0),
        "inputDuration": float(report.get("input_duration") or 0.0),
        "firstNoteAt": report.get("first_note_at"),
        "lastNoteAt": report.get("last_note_at"),
        "warnings": list(report.get("warnings") or []),
        "succeeded": 1 if output_path else 0,
    }


def _asr(runtime: Any, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    source = _require_file(payload.get("inputPath"), "Input audio")
    output_dir = _require_directory(payload.get("outputDir"), "Output directory", create=True)
    model_id = str(payload.get("modelPreset") or payload.get("model") or "paraformer-zh")
    language = str(payload.get("language") or "").strip() or None
    text_mode = str(payload.get("textMode") or "plain")

    _progress("asr", "loading_asr_model", 0, 2, model_id)
    result = runtime.run(
        operation,
        inputs=_resolved_inputs({"audio": {"kind": "file", "path": str(source)}}),
        params={"model": model_id, "language": language or "auto"},
    )
    _progress("asr", "recognizing_speech", 1, 2, source.name)
    report = _unwrap(result.get("report")) or {}
    sentences = [
        {"start": float(item.get("start") or 0.0), "end": float(item.get("end") or 0.0), "text": str(item.get("text") or "")}
        for item in (report.get("timestamps") or [])
    ]
    text = str(report.get("text") or "")
    detected = report.get("detected_language")
    # Write transcript outputs the same way the dedicated tool did.
    stem = source.stem
    outputs: list[str] = []
    if text:
        txt = Path(output_dir) / f"{stem}.txt"
        txt.write_text(text + "\n", encoding="utf-8")
        outputs.append(str(txt))
        if sentences:
            srt = Path(output_dir) / f"{stem}.srt"
            srt.write_text(_srt(sentences), encoding="utf-8")
            outputs.append(str(srt))
            lrc = Path(output_dir) / f"{stem}.lrc"
            lrc.write_text(_lrc(sentences), encoding="utf-8")
            outputs.append(str(lrc))
    _progress("asr", "writing_transcript", 2, 2, str(source.name))
    return {
        "operation": "asr",
        "inputPath": str(source),
        "outputDir": str(output_dir),
        "outputPaths": outputs,
        "outputPath": outputs[0] if outputs else "",
        "text": text,
        "sentences": sentences,
        "segmentCount": len(sentences),
        "modelPreset": model_id,
        "requestedLanguage": language or "auto",
        "detectedLanguage": detected,
        "warnings": [] if sentences or text else ["timestamps_unavailable"],
        "succeeded": 1 if text else 0,
    }


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _srt(sentences: list[dict[str, Any]]) -> str:
    blocks = []
    for index, sentence in enumerate(sentences, start=1):
        blocks.append(f"{index}\n{_srt_time(sentence['start'])} --> {_srt_time(sentence['end'])}\n{sentence['text']}\n")
    return "\n".join(blocks)


def _lrc_time(seconds: float) -> str:
    minutes, remainder = divmod(max(0.0, seconds), 60)
    return f"[{int(minutes):02d}:{remainder:05.2f}]"


def _lrc(sentences: list[dict[str, Any]]) -> str:
    return "\n".join(f"{_lrc_time(sentence['start'])}{sentence['text']}" for sentence in sentences)
