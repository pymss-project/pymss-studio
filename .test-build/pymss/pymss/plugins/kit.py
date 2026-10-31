"""Helpers for plugin authors: tool operations, structured progress, files and FFmpeg.

These replace the per-plugin ``protocol.py``/``entry.py``/``common.py`` copies::

    from pymss.plugins.kit import emit, register_tool, require_file

    def setup(plugin):
        register_tool(plugin, "alice.tool.run", _run, title="Run", tool="run")
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager, redirect_stdout
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

SUPPORTED_AUDIO_SUFFIXES = {
    ".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus",
    ".wma", ".aiff", ".aif",
}


class ToolError(RuntimeError):
    """A tool failure carrying host-protocol metadata (code, recoverable, extra)."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        recoverable: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable
        self.extra = extra or {}


# ---- structured progress ----

_context: ContextVar[tuple[Any, Any] | None] = ContextVar("pymss_tool_context", default=None)


@contextmanager
def materialize_audio(audio: Any) -> Iterator[Path]:
    """Expose current AUDIO samples to file-only backends for this call.

    ``source_path`` is provenance, never a cache of the current samples.
    Hosts may own ``PYMSS_SCRATCH_DIR`` to clean up after forcibly stopping a
    worker. Each call gets a separate child directory, including batch threads.
    """
    import av
    import numpy as np
    from pymss.graph.core import AudioArtifact, safe_filename_part

    if not isinstance(audio, AudioArtifact):
        raise TypeError("Expected an AudioArtifact")
    if audio.sample_rate <= 0 or audio.audio.shape[0] == 0 or audio.audio.shape[1] == 0:
        raise ValueError("Audio must have a positive sample rate and non-empty samples")
    if not np.isfinite(audio.audio).all():
        raise ValueError("Audio contains non-finite samples")
    parent = os.environ.get("PYMSS_SCRATCH_DIR") or None
    name = safe_filename_part(audio.stem_name or Path(audio.source_path).stem or "audio")
    layout = audio.channel_layout or f"{audio.audio.shape[0]}c"
    with tempfile.TemporaryDirectory(prefix="pymss-audio-", dir=parent) as temporary:
        path = Path(temporary) / f"{name}.wav"
        # Float WAV preserves processed samples without clipping/quantizing;
        # an explicit layout also preserves multichannel channel positions.
        with av.open(str(path), "w", format="wav") as container:
            stream = container.add_stream("pcm_f32le", rate=audio.sample_rate)
            stream.layout = layout
            for start in range(0, audio.audio.shape[1], 65536):
                packed = np.ascontiguousarray(audio.audio[:, start:start + 65536].T).reshape(1, -1)
                frame = av.AudioFrame.from_ndarray(packed, format="flt", layout=layout)
                frame.sample_rate = audio.sample_rate
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
        yield path


@contextmanager
def operation_context(context) -> Iterator[None]:
    """Bind ``context`` so ``emit`` can report from nested helper code."""
    token = _context.set((context, sys.stdout))
    try:
        yield
    finally:
        _context.reset(token)


def emit(event_type: str, payload: Mapping[str, Any]) -> None:
    """Report a structured tool event through the bound Operation context.

    ``completed``/``total`` in ``payload`` drive the numeric fraction; the event
    itself reaches the host as ``report_progress(..., event=...)``.
    """
    current = _context.get()
    if current is None:
        raise RuntimeError("An Operation context is required")
    context, output = current
    context.check_cancelled()
    completed, total = payload.get("completed", 0), payload.get("total", 0)
    fraction = completed / total if isinstance(total, (int, float)) and total > 0 else 0
    fraction = min(1.0, max(0.0, fraction)) if math.isfinite(fraction) else 0
    message = str(payload.get("current") or "")
    # Hosts may redirect stdout while plugin code runs; progress keeps the
    # stream that was active when the operation started.
    with redirect_stdout(output):
        context.report_progress(fraction, message, event={"type": event_type, "payload": dict(payload)})


@contextmanager
def isolate_stdout() -> Iterator[None]:
    """Send stray prints from third-party libraries to stderr."""
    with redirect_stdout(sys.stderr):
        yield


def register_tool(
    plugin,
    operation_id: str,
    handler: Callable[[dict[str, Any]], Mapping[str, Any]],
    *,
    title: str,
    tool: str | None = None,
    description: str = "File-based audio tool with progress and structured results",
) -> None:
    """Register a request-object operation whose ``handler(request)`` returns a report.

    When ``tool`` is given, a request whose ``operation`` field names another
    tool is rejected.
    """

    def run(context, inputs, params):
        request = inputs.get("request")
        if not isinstance(request, dict):
            raise ValueError("Tool input must contain a request object")
        if tool is not None and request.get("operation", tool) != tool:
            raise ValueError("Tool request does not match its operation")
        with operation_context(context):
            context.check_cancelled()
            result = handler(request)
            context.check_cancelled()
        return {"report": dict(result)}

    plugin.register_operation(
        id=operation_id, title=title, description=description,
        inputs={"request": {"type": "object"}},
        params={"type": "object", "properties": {}, "additionalProperties": False},
        outputs={"report": {"type": "object"}}, run=run,
    )


# ---- paths ----

def require_file(value: Any, label: str) -> Path:
    path = Path(str(value or "").strip()).expanduser()
    if not str(path) or not path.is_file():
        raise ValueError(f"{label} does not exist or is not a file")
    return path.resolve()


def require_directory(value: Any, label: str, *, create: bool = False) -> Path:
    raw_value = str(value or "").strip()
    if not raw_value:
        raise ValueError(f"{label} is required")
    path = Path(raw_value).expanduser()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError(f"{label} does not exist or is not a directory")
    return path.resolve()


def available_path(path: Path) -> Path:
    """Return ``path`` or the first free ``<stem>_<n><suffix>`` sibling."""
    if not path.exists():
        return path
    for index in range(2, 10000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Unable to allocate an output filename for {path.name}")


def _rename_export_path(temporary: Path, path: Path) -> None:
    """Move a completed file without replacing a concurrent output."""
    if os.name == "nt":
        os.rename(temporary, path)
        return
    import ctypes
    import errno

    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = getattr(library, "renamex_np", None)
        arguments = (os.fsencode(temporary), os.fsencode(path), 0x4)  # RENAME_EXCL
        signature = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    elif sys.platform.startswith("linux"):
        rename = getattr(library, "renameat2", None)
        arguments = (-100, os.fsencode(temporary), -100, os.fsencode(path), 1)  # AT_FDCWD, RENAME_NOREPLACE
        signature = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    else:
        rename = None
    if rename is None:
        raise OSError(errno.ENOTSUP, "Atomic output publishing is unavailable", str(path))
    rename.argtypes = signature
    rename.restype = ctypes.c_int
    if rename(*arguments) != 0:
        code = ctypes.get_errno() or errno.EIO
        raise OSError(code, os.strerror(code), str(path))


def publish_export_path(temporary: Path, path: Path) -> Path:
    """Atomically publish a completed export without overwriting an earlier file."""
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
            # The final hard link is already complete. Host cleanup can remove
            # a transiently locked task temp after the process exits.
            pass
        return candidate
    raise FileExistsError(f"Failed to publish a unique export filename: {path}")


def render_output(path: Path, render: Callable[[Path], None], *, allow_empty: bool = False) -> Path:
    """Render into a private partial next to ``path``, then publish it atomically.

    Hosts that sweep orphaned partials set ``PYMSS_STUDIO_AUDIO_TEMP_TOKEN`` and
    ``PYMSS_STUDIO_AUDIO_TEMP_STATE``; the partial name then carries the token
    and launcher pid so the host can attribute it.
    """
    # Preserve the extension for FFmpeg's muxer. A killed process can leave only
    # this private partial, never an incomplete file with the final output name.
    token = os.environ.get("PYMSS_STUDIO_AUDIO_TEMP_TOKEN", "")
    state = os.environ.get("PYMSS_STUDIO_AUDIO_TEMP_STATE", "")
    prefix = ".pymss-audio-"
    if token or state:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,96}", token) or not state:
            raise RuntimeError("Audio temporary-file state is invalid")
        for attempt in range(10):
            try:
                scope = json.loads(Path(state).read_text(encoding="utf-8"))
                launcher_pid = scope["pid"]
                if (
                    scope["token"] == token
                    and type(launcher_pid) is int
                    and launcher_pid > 0
                    and launcher_pid in {os.getpid(), os.getppid()}
                    and Path(scope["directory"]).resolve() == path.parent.resolve()
                ):
                    prefix = f".pymss-audio-{token}-{launcher_pid}-"
                    break
            except (OSError, ValueError, KeyError, TypeError):
                pass
            if attempt == 9:
                raise RuntimeError("Audio temporary-file state is unavailable")
            time.sleep(.02)
    fd, value = tempfile.mkstemp(prefix=prefix, suffix=path.suffix, dir=path.parent)
    os.close(fd)
    temporary = Path(value)
    try:
        render(temporary)
        if not allow_empty and not temporary.stat().st_size:
            raise RuntimeError("No audio data was written")
        return publish_export_path(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # Publishing may already have succeeded while an antivirus scanner
            # briefly retains the temporary hard link.
            pass


# ---- external processes ----

def ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("FFmpeg was not found in the application tools or PATH")
    return path


def ffprobe_path() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise RuntimeError("FFprobe was not found in the application tools or PATH")
    return path


def run_process(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def run_ffmpeg(arguments: list[str]) -> None:
    run_process([ffmpeg_path(), "-nostdin", "-hide_banner", "-loglevel", "error", *arguments])


def subprocess_detail(error: subprocess.CalledProcessError) -> str:
    detail = (error.stderr or error.stdout or "").strip()
    return detail[-2000:] if detail else f"FFmpeg exited with status {error.returncode}"


def register_commands(plugin, directory: str | Path) -> None:
    """Register declarative native commands without importing the runner at startup."""
    from ..operations.command import register_commands as register

    register(plugin, directory)


def register_rpc_processes(plugin, directory: str | Path) -> None:
    """Register task-scoped native JSON-RPC operations through the v2 handle."""
    from ..operations.rpc_process import register_rpc_processes as register

    register(plugin, directory)


__all__ = [
    "SUPPORTED_AUDIO_SUFFIXES",
    "ToolError",
    "available_path",
    "emit",
    "ffmpeg_path",
    "ffprobe_path",
    "isolate_stdout",
    "operation_context",
    "publish_export_path",
    "register_tool",
    "register_commands",
    "register_rpc_processes",
    "render_output",
    "require_directory",
    "require_file",
    "run_ffmpeg",
    "run_process",
    "subprocess_detail",
]
