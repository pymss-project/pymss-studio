"""Cooperative cancellation of inference between processing chunks.

Studio requests cancellation by creating the file named in the request's ``cancelFile``. The
worker checks it at each progress report, stops after the chunk that is running, waits for
queued accelerator work and exits normally. Ending the process while GPU work is in flight makes
the display driver reset the engine, which also drops the device used to draw the app window.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable


class TaskCancelled(BaseException):
    """Raised when Studio asks the running task to stop.

    Derives from ``BaseException`` so that model and pipeline code catching ``Exception`` lets it
    reach the worker entry point.
    """


_request_path: str | None = None


def watch(payload: dict[str, Any]) -> None:
    """Use the cancellation request file named by this command's payload."""
    global _request_path
    value = payload.get("cancelFile")
    _request_path = value if isinstance(value, str) and value else None


def cancellation_requested() -> bool:
    return _request_path is not None and os.path.exists(_request_path)


def raise_if_cancelled() -> None:
    if cancellation_requested():
        raise TaskCancelled()


def cancellable(callback: Callable[..., Any] | None) -> Callable[..., Any] | None:
    """Check for cancellation before each progress report passed to ``callback``."""
    if callback is None:
        return None

    def checked(*args: Any, **kwargs: Any) -> Any:
        raise_if_cancelled()
        return callback(*args, **kwargs)

    return checked


def finish_device_work() -> None:
    """Wait for queued accelerator work so the process exits with nothing in flight."""
    torch = sys.modules.get("torch")
    if torch is None:
        return
    for name in ("cuda", "xpu"):
        backend = getattr(torch, name, None)
        is_initialized = getattr(backend, "is_initialized", None)
        if callable(is_initialized) and is_initialized():
            backend.synchronize()
