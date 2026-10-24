"""Worker commands for the pymss Operation runtime (round 5).

``list_operations`` / ``describe_operation`` are quick one-shot queries.
``run_operation`` executes one operation with file-referenced inputs and
emits ``operation_result`` as its terminal event (Rust treats it like
``audio_tool_result``: terminal for the task, progress events optional).
"""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

from worker_protocol import emit, emit_error


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

        audio, sr = load_audio(str(value["path"]), sr=None, mono=False)
        import numpy as np

        from pymss.graph.core import AudioArtifact

        arr = np.asarray(audio, dtype=np.float32)
        layout = "stereo" if arr.ndim == 2 and arr.shape[0] == 2 else "mono"
        return AudioArtifact(arr, int(sr), source_path=str(value["path"]), channel_layout=layout)
    return value


def _serialize_output(name: str, value: Any, output_dir: Path) -> Any:
    """Best-effort JSON-safe conversion of one named output."""
    from pymss.graph.core import AudioArtifact

    if isinstance(value, AudioArtifact):
        from pymss.audio_io import save_audio

        path = output_dir / f"operation_output_{name}.wav"
        save_audio(str(path), value.audio.T, value.sample_rate, "wav", {})
        return {"kind": "audio_file", "path": str(path), "sampleRate": value.sample_rate}
    if isinstance(value, (str, int, float, bool)):
        return {"kind": "scalar", "value": value}
    import numpy as np

    if isinstance(value, np.ndarray):
        return {"kind": "array", "shape": list(value.shape), "dtype": str(value.dtype)}
    if isinstance(value, dict):
        return {"kind": "object", "value": {k: _serialize_output(k, v, output_dir)["value"] for k, v in value.items()}}
    if isinstance(value, (list, tuple)):
        return {"kind": "array", "value": [_serialize_output(name, item, output_dir)["value"] for item in value]}
    if isinstance(value, Path):
        return {"kind": "scalar", "value": str(value)}
    return {"kind": "opaque", "value": str(value)}


def cmd_run_operation(payload: dict[str, Any]) -> int:
    operation_id = str(payload.get("operation") or "").strip()
    request_id = str(payload.get("requestId") or "").strip() or None
    output_dir = Path(str(payload.get("outputDir") or "") or ".")
    if not operation_id:
        return emit_error("OPERATION_INVALID", "Missing operation id", request_id=request_id)
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        return emit_error("OPERATION_INVALID", "params must be an object", request_id=request_id)

    try:
        runtime = _runtime()
        raw_inputs = payload.get("inputs") or {}
        inputs = {name: _resolve_input(value) for name, value in raw_inputs.items()}

        from pymss.operations import OperationContext

        def _progress(fraction, message=""):
            emit("operation_progress", {
                "operation": operation_id,
                "fraction": float(fraction),
                "message": str(message or ""),
            }, request_id=request_id)

        output_dir.mkdir(parents=True, exist_ok=True)
        from worker_plugins import validate_operation
        validate_operation(runtime, operation_id)
        result = runtime.run(
            operation_id,
            inputs=inputs,
            params=params,
            context=OperationContext(progress=_progress),
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
        return emit_error(
            "OPERATION_FAILED",
            str(exc),
            traceback.format_exc(),
            request_id=request_id,
            extra={"operation": operation_id},
        )
