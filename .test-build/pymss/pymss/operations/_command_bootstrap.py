"""Private process gate. Run as a script, without importing pymss or site packages."""
from __future__ import annotations

import json
from collections import deque
import os
import select
import signal
import subprocess
import sys
import threading


def _relay_posix(source, sink, finished) -> None:
    """Watch parent EOF even when the native stdin pipe is backpressured."""
    pending = deque()
    offset = buffered = 0
    forward = True
    lost_parent = False
    parent_fd, child_fd = source.fileno(), sink.fileno()
    try:
        os.set_blocking(parent_fd, False)
        os.set_blocking(child_fd, False)
        # The control-line reader can have prefetched protocol bytes. Drain
        # that buffer before selecting the underlying file descriptor.
        try:
            first = source.read1(65536)
        except BlockingIOError:
            first = None
        if first:
            pending.append(first)
            buffered = len(first)
        while not finished.is_set():
            readable, writable, _ = select.select([parent_fd], [child_fd] if pending and forward else [], [], .1)
            if parent_fd in readable:
                try:
                    chunk = os.read(parent_fd, 65536)
                except BlockingIOError:
                    chunk = None
                if chunk == b"":
                    lost_parent = True
                    break
                if chunk and forward:
                    pending.append(chunk)
                    buffered += len(chunk)
                    # Match the protocol's bounded reliable frame window.
                    if buffered > 8 * 1024 * 1024:
                        lost_parent = True
                        break
            if child_fd in writable and pending:
                try:
                    count = os.write(child_fd, memoryview(pending[0])[offset:])
                except BlockingIOError:
                    continue
                except OSError:
                    # Still drain/watch the parent after the peer closes stdin.
                    forward = False
                    pending.clear()
                    buffered = offset = 0
                    continue
                buffered -= count
                offset += count
                if offset == len(pending[0]):
                    pending.popleft()
                    offset = 0
    except OSError:
        lost_parent = True
    finally:
        if lost_parent and not finished.is_set():
            os.killpg(os.getpgrp(), signal.SIGKILL)
        try:
            sink.close()
        except OSError:
            pass


def main() -> int:
    # The parent assigns this gate to its Windows Job before releasing argv.
    # On POSIX this process owns a session; EOF also reaps descendants if the
    # operation worker is forcibly stopped before its finally block can run.
    request = sys.stdin.buffer.readline(256 * 1024)
    if not request.endswith(b"\n"):
        return 125
    request = json.loads(request)
    arguments = request["arguments"]
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        api.OpenJobObjectW.restype = wintypes.HANDLE
        api.GetCurrentProcess.restype = wintypes.HANDLE
        api.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        api.IsProcessInJob.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        job = api.OpenJobObjectW(4, False, request["jobName"])  # JOB_OBJECT_QUERY
        contained = wintypes.BOOL()
        try:
            if not job or not api.IsProcessInJob(api.GetCurrentProcess(), job, ctypes.byref(contained)) or not contained.value:
                print("The command process ownership scope is unavailable", file=sys.stderr)
                return 125
        finally:
            if job:
                api.CloseHandle(job)
    finished = threading.Event()
    forward_stdin = request.get("forwardStdin") is True

    def parent_lifetime() -> None:
        os.read(sys.stdin.fileno(), 1)
        if not finished.is_set() and os.name != "nt":
            os.killpg(os.getpgrp(), signal.SIGKILL)

    def relay_input(child) -> None:
        forward = True
        parent_closed = False
        try:
            while chunk := sys.stdin.buffer.read1(65536):
                if forward:
                    try:
                        child.stdin.write(chunk)
                        child.stdin.flush()
                    except OSError:
                        # Keep watching parent EOF after the peer closes stdin;
                        # a child write failure is not itself parent death.
                        forward = False
            parent_closed = True
        except OSError:
            pass
        finally:
            try:
                child.stdin.close()
            except OSError:
                pass
            if parent_closed and not finished.is_set() and os.name != "nt":
                os.killpg(os.getpgrp(), signal.SIGKILL)

    if os.name != "nt" and not forward_stdin:
        watcher = threading.Thread(target=parent_lifetime, daemon=True)
        watcher.start()
    try:
        child = subprocess.Popen(arguments, stdin=subprocess.PIPE if forward_stdin else subprocess.DEVNULL, close_fds=True)
        if forward_stdin:
            target, args = (relay_input, (child,)) if os.name == "nt" else (_relay_posix, (sys.stdin.buffer, child.stdin, finished))
            threading.Thread(target=target, args=args, daemon=True).start()
            # The native peer owns its protocol/diagnostic pipes. Keeping the
            # gate's inherited write handles open would hide peer stdout EOF.
            os.close(sys.stdout.fileno())
            os.close(sys.stderr.fileno())
        code = child.wait()
        if forward_stdin:
            finished.set()
            # The relay can still be waiting for parent input. Avoid buffered
            # stdin finalization racing that thread after the native peer exits.
            status = code if code >= 0 else 128 - code
            os._exit(status if -(2 ** 31) <= status < 2 ** 31 else 1)
        return code if code >= 0 else 128 - code
    except OSError:
        # Do not echo argv: paths and parameter values may be sensitive.
        print("The plugin executable could not be started", file=sys.stderr)
        return 126
    finally:
        finished.set()


if __name__ == "__main__":
    raise SystemExit(main())
