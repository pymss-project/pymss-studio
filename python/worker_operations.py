"""Worker commands for the pymss Operation runtime (round 5).

``list_operations`` / ``describe_operation`` are quick one-shot queries.
``run_operation`` executes one operation with file-referenced inputs and
emits ``operation_result`` as its terminal event (Rust treats it like
``audio_tool_result``: terminal for the task, progress events optional).
"""

from __future__ import annotations

import re
import traceback
from pathlib import Path
from typing import Any

from worker_protocol import emit, emit_error
from worker_paths import normalize_output_dir


def _runtime():
    from worker_plugins import operation_runtime

    return operation_runtime()


def cmd_list_operations(payload: dict[str, Any]) -> int:
    try:
        runtime = _runtime()
        ops = runtime.list_operations()
        from worker_plugins import plugin_reports
        report = plugin_reports(runtime)
        emit(
            "operations_list",
            {
                "operations": ops,
                "plugins": report,
                "pymssOperationsAvailable": True,
            },
        )
        return 0
    except ImportError as exc:
        emit(
            "operations_list",
            {"operations": [], "plugins": [], "pymssOperationsAvailable": False, "error": str(exc)},
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        return emit_error("OPERATIONS_LIST_FAILED", str(exc), traceback.format_exc())


def cmd_describe_operation(payload: dict[str, Any]) -> int:
    operation_id = str(payload.get("operation") or "").strip()
    if not operation_id:
        return emit_error("OPERATION_INVALID", "Missing operation id")
    try:
        spec = _runtime().describe_operation(operation_id)
        emit("operation_spec", spec)
        return 0
    except ImportError as exc:
        return emit_error("OPERATIONS_UNAVAILABLE", str(exc))
    except Exception as exc:  # noqa: BLE001
        return emit_error("OPERATION_DESCRIBE_FAILED", str(exc), traceback.format_exc())


def _resolve_input(value: Any) -> Any:
    """Convert a JSON input descriptor into a Python-side value.

    ``{"kind": "file", "path": ...}`` loads through pymss.audio_io so the
    operation receives a real AudioArtifact-shaped input (channel-first
    float32 + sample rate). Numbers/strings pass through unchanged.
    """
    if isinstance(value, dict) and value.get("kind") == "file":
        from pymss.audio_io import load_audio

        audio, sr, layout = load_audio(str(value["path"]), sr=None, mono=False, return_layout=True)
        import numpy as np

        from pymss.graph.core import AudioArtifact

        arr = np.asarray(audio, dtype=np.float32)
        return AudioArtifact(arr, int(sr), source_path=str(value["path"]), channel_layout=layout)
    return value


def _serialize_output(name: str, value: Any, output_dir: Path) -> Any:
    """Best-effort JSON-safe conversion of one named output."""
    import numpy as np

    from pymss.graph.core import AudioArtifact

    if isinstance(value, AudioArtifact):
        from pymss.audio_io import save_audio
        from pymss.plugins.kit import render_output

        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name))[:48].rstrip(" .") or "output"
        path = render_output(
            output_dir / f"operation_output_{filename}.wav",
            lambda target: save_audio(str(target), value.audio.T, value.sample_rate, "wav", {}),
        )
        return {"kind": "audio_file", "path": str(path), "sampleRate": value.sample_rate}
    if isinstance(value, np.generic):
        value = value.item()
    if value is None or isinstance(value, (str, int, float, bool)):
        return {"kind": "scalar", "value": value}
    if isinstance(value, np.ndarray):
        return {"kind": "array", "shape": list(value.shape), "dtype": str(value.dtype)}
    if isinstance(value, dict):
        children = {k: _serialize_output(f"{name}_{k}", v, output_dir) for k, v in value.items()}
        return {"kind": "object", "value": {k: child.get("value", child) for k, child in children.items()}}
    if isinstance(value, (list, tuple)):
        children = [_serialize_output(f"{name}_{index}", item, output_dir) for index, item in enumerate(value)]
        return {"kind": "array", "value": [child.get("value", child) for child in children]}
    if isinstance(value, Path):
        return {"kind": "scalar", "value": str(value)}
    return {"kind": "opaque", "value": str(value)}


def cmd_run_operation(payload: dict[str, Any]) -> int:
    operation_id = str(payload.get("operation") or "").strip()
    request_id = str(payload.get("requestId") or "").strip() or None
    if not operation_id:
        return emit_error("OPERATION_INVALID", "Missing operation id", request_id=request_id)
    params = payload.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return emit_error("OPERATION_INVALID", "params must be an object", request_id=request_id)
    raw_inputs = payload.get("inputs")
    if raw_inputs is None:
        raw_inputs = {}
    if not isinstance(raw_inputs, dict):
        return emit_error("OPERATION_INVALID", "inputs must be an object", request_id=request_id)

    try:
        output_dir = Path(normalize_output_dir(payload.get("outputDir")))
        runtime = _runtime()
        input_specs = runtime.describe_operation(operation_id).get("inputs", {})
        inputs = {
            name: _resolve_input(value) if str(input_specs.get(name, {}).get("type", "")).lower() == "audio" else value
            for name, value in raw_inputs.items()
        }

        from pymss.operations import OperationContext

        def _progress(fraction, message="", event=None):
            emit("operation_progress", {
                "operation": operation_id,
                "fraction": float(fraction),
                "message": str(message or ""),
                **({"event": event} if event is not None else {}),
            }, request_id=request_id)

        output_dir.mkdir(parents=True, exist_ok=True)
        from worker_plugins import validate_operation
        validate_operation(runtime, operation_id)
        result = runtime.run(
            operation_id,
            inputs=inputs,
            params=params,
            context=OperationContext(workdir=str(output_dir), progress=_progress),
        )
        serialized = {
            name: _serialize_output(name, value, output_dir)
            for name, value in result.items()
        }
        emit("operation_result", {
            "operation": operation_id,
            "outputs": serialized,
            "outputDir": str(output_dir),
        }, request_id=request_id)
        return 0
    except ImportError as exc:
        return emit_error("OPERATIONS_UNAVAILABLE", str(exc), request_id=request_id)
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", None)
        metadata = getattr(exc, "extra", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        return emit_error(
            code if isinstance(code, str) else "OPERATION_FAILED",
            str(exc),
            traceback.format_exc(),
            request_id=request_id,
            recoverable=getattr(exc, "recoverable", False) is True,
            extra={"operation": operation_id, **metadata},
        )
