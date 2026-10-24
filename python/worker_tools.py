from __future__ import annotations

import traceback
from typing import Any

from audio_tools.common import AudioToolError
from audio_tools.registry import get_audio_tool_handler
from worker_protocol import bind_request_id, emit, emit_error

_USE_OPERATIONS = True


def cmd_audio_tools(payload: dict[str, Any]) -> int:
    operation = str(payload.get("operation") or "").strip().lower()
    request_id = str(payload.get("requestId") or "").strip() or None
    if _USE_OPERATIONS:
        try:
            # pymss Operation path: kit builtins for convert/inspect/sdr/
            # slicer/merge, plugin operations for midi/asr (with a legacy
            # fallback inside the adapter when the plugin is not installed).
            from audio_tools_ops import run_audio_tool_via_operations

            # Bind the request id so progress events emitted by legacy handlers
            # (plugin-tool fallback) inherit it, mirroring the direct path below.
            with bind_request_id(request_id):
                result = run_audio_tool_via_operations(payload)
            emit("audio_tool_result", result, request_id=request_id)
            return 0
        except ImportError:
            pass  # adapter module unavailable → legacy handlers below
        except AudioToolError as error:
            return emit_error(
                error.code,
                str(error),
                traceback.format_exc(),
                request_id=request_id,
                recoverable=error.recoverable,
                extra={"operation": operation, **error.extra},
            )
        except Exception as error:
            return emit_error(
                "AUDIO_TOOL_FAILED",
                str(error),
                traceback.format_exc(),
                request_id=request_id,
                extra={"operation": operation},
            )
    handler = get_audio_tool_handler(operation)
    if handler is None:
        return emit_error("AUDIO_TOOL_INVALID", "Unknown audio tool operation", request_id=request_id)
    try:
        with bind_request_id(request_id):
            result = handler(payload)
        emit("audio_tool_result", result, request_id=request_id)
        return 0
    except AudioToolError as error:
        return emit_error(
            error.code,
            str(error),
            traceback.format_exc(),
            request_id=request_id,
            recoverable=error.recoverable,
            extra={"operation": operation, **error.extra},
        )
    except Exception as error:
        return emit_error(
            "AUDIO_TOOL_FAILED",
            str(error),
            traceback.format_exc(),
            request_id=request_id,
            extra={"operation": operation},
        )
