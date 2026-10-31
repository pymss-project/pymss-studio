"""Bounded, task-owned subprocess execution for declarative commands."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import select
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Sequence

from . import OperationContext, OperationError


class CommandProcessError(OperationError):
    def __init__(self, code: str, message: str, *, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.extra = {"stderr": detail} if detail else {}
        self.recoverable = True


class _WindowsJob:
    def __init__(self, process: subprocess.Popen, name: str):
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD),
            ]

        class Limits(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [("times", ctypes.c_int64 * 4), ("page_faults", wintypes.DWORD),
                        ("total_processes", wintypes.DWORD), ("active_processes", wintypes.DWORD),
                        ("terminated_processes", wintypes.DWORD)]

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        api.SetInformationJobObject.restype = wintypes.BOOL
        api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        api.AssignProcessToJobObject.restype = wintypes.BOOL
        api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        api.TerminateJobObject.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        api.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        api.QueryInformationJobObject.restype = wintypes.BOOL
        self.accounting = Accounting
        self.api, self.handle = api, api.CreateJobObjectW(None, name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Limits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not api.AssignProcessToJobObject(self.handle, int(process._handle))):
            error = ctypes.WinError(ctypes.get_last_error())
            api.CloseHandle(self.handle)
            self.handle = None
            raise error

    def close(self):
        if self.handle:
            import ctypes

            try:
                if not self.api.TerminateJobObject(self.handle, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
                deadline = time.monotonic() + 5
                while True:
                    accounting = self.accounting()
                    if not self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                        raise ctypes.WinError(ctypes.get_last_error())
                    if accounting.active_processes == 0:
                        break
                    if time.monotonic() >= deadline:
                        raise CommandProcessError("COMMAND_CLEANUP_FAILED", "Plugin command descendants did not stop")
                    time.sleep(0.02)
            finally:
                self.api.CloseHandle(self.handle)
                self.handle = None


def _posix_group_alive(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    # Orphan zombies have no runnable code or open files. Some headless Linux
    # init processes defer reaping them, so they do not block publication.
    if sys.platform.startswith("linux"):
        if not Path("/proc/self/stat").is_file():
            return True
        for status in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = status.read_text().rsplit(") ", 1)[1].split()
                if int(fields[2]) == group and fields[0] != "Z":
                    return True
            except (OSError, ValueError, IndexError):
                continue
        return False
    return True


class _ExitObserver:
    """Observe native child exit without releasing a POSIX process identity."""
    def __init__(self, process: subprocess.Popen):
        self.process, self.queue = process, None
        self.waitid = os.name != "nt" and all(hasattr(os, name) for name in ("waitid", "P_PID", "WNOWAIT"))
        if os.name != "nt" and not self.waitid:
            self.queue = select.kqueue()
            try:
                self.queue.control([select.kevent(process.pid, filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT, fflags=select.KQ_NOTE_EXIT)], 0, 0)
            except BaseException:
                self.queue.close()
                self.queue = None
                raise

    def exited(self) -> bool:
        if os.name == "nt":
            return self.process.poll() is not None
        if self.waitid:
            return os.waitid(os.P_PID, self.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        return bool(self.queue.control(None, 1, 0))

    def close(self):
        if self.queue is not None:
            self.queue.close()
            self.queue = None


def run_command_process(
    arguments: Sequence[str], *, cwd: Path, context: OperationContext,
    timeout: float = 300, output_limit: int = 1024 * 1024,
    protocol: Any = None, environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run argv without a shell; drain bounded pipes and reap normal descendants.

    stdin is a private lifetime channel, never the command's application input.
    A native command is trusted code with user permissions, not a sandbox.
    """
    if type(output_limit) is not int or not 1 <= output_limit <= 16 * 1024 * 1024:
        raise CommandProcessError("COMMAND_ARGUMENTS_INVALID", "Invalid command diagnostic buffer limit")
    if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not 0 < timeout <= 86400:
        raise CommandProcessError("COMMAND_ARGUMENTS_INVALID", "Invalid command execution timeout")
    if getattr(sys, "frozen", False):
        raise CommandProcessError("COMMAND_HOST_UNSUPPORTED", "Command operations require a Python interpreter host")
    waitid = all(hasattr(os, name) for name in ("waitid", "P_PID", "WNOWAIT"))
    if os.name != "nt" and not waitid and not hasattr(select, "kqueue"):
        raise CommandProcessError("COMMAND_HOST_UNSUPPORTED", "This host cannot supervise command process groups")
    job_name = "Local\\pymss-command-" + uuid.uuid4().hex if os.name == "nt" else None
    request = json.dumps({"arguments": list(arguments), "jobName": job_name, "forwardStdin": protocol is not None}, ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n"
    if len(request) >= 256 * 1024:
        raise CommandProcessError("COMMAND_ARGUMENTS_INVALID", "Plugin command arguments exceed the size limit")
    context.check_cancelled()
    process = subprocess.Popen(
        [getattr(sys, "_base_executable", None) or sys.executable, "-I", "-S", str(Path(__file__).with_name("_command_bootstrap.py"))],
        cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=os.name != "nt", close_fds=True, env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    job = None
    observer = None
    readers: list[threading.Thread] = []
    buffers = [bytearray(), bytearray()]
    failed_read = threading.Event()

    def drain(stream, buffer):
        try:
            while chunk := stream.read1(65536):
                buffer.extend(chunk)
                if len(buffer) > output_limit:
                    del buffer[:-output_limit]
        except OSError:
            failed_read.set()
        finally:
            stream.close()

    try:
        if os.name == "nt":
            job = _WindowsJob(process, job_name)
        observer = _ExitObserver(process)
        for stream, buffer in zip((process.stdout, process.stderr), buffers):
            reader = threading.Thread(target=protocol.read_stdout if protocol is not None and stream is process.stdout else drain,
                                      args=(stream,) if protocol is not None and stream is process.stdout else (stream, buffer), daemon=True)
            reader.start()
            readers.append(reader)
        try:
            process.stdin.write(request)
            process.stdin.flush()
            write_error = None
        except OSError as error:
            # Windows broken pipes may be EINVAL; preserve the exited gate's
            # status and diagnostic tail while still running full cleanup.
            write_error = error
        if protocol is not None and write_error is None:
            protocol.start(process.stdin)
        deadline = time.monotonic() + timeout
        while True:
            context.check_cancelled()
            if failed_read.is_set():
                raise CommandProcessError("COMMAND_IO_FAILED", "Plugin command output could not be read")
            if protocol is not None:
                protocol.update()
            if observer.exited():
                break
            if write_error is not None:
                raise CommandProcessError("COMMAND_IO_FAILED", "Plugin command startup request could not be sent") from write_error
            if time.monotonic() >= deadline:
                raise CommandProcessError("COMMAND_TIMEOUT", "Plugin command exceeded its execution timeout")
            time.sleep(0.02)
    finally:
        # Kill the whole ownership scope even on success: grandchildren must
        # not keep pipe handles or continue modifying a completed task's files.
        cleanup_error = None
        if protocol is not None:
            protocol.cancel_waiters()
        try:
            if job is not None:
                job.close()
            elif os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                process.kill()
        except OSError as error:
            cleanup_error = error
        except CommandProcessError as error:
            cleanup_error = error
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as error:
                raise CommandProcessError("COMMAND_CLEANUP_FAILED", "Plugin command could not be stopped") from error
        finally:
            if observer is not None:
                observer.close()
        if os.name != "nt" and cleanup_error is None:
            deadline = time.monotonic() + 5
            try:
                while _posix_group_alive(process.pid):
                    if time.monotonic() >= deadline:
                        raise CommandProcessError("COMMAND_CLEANUP_FAILED", "Plugin command descendants did not stop")
                    time.sleep(0.02)
            except (OSError, CommandProcessError) as error:
                cleanup_error = error
        for reader in readers:
            reader.join(timeout=5)
        if any(reader.is_alive() for reader in readers):
            raise CommandProcessError("COMMAND_CLEANUP_FAILED", "Plugin command output handles did not close")
        if protocol is not None:
            protocol.close()
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        if cleanup_error is not None:
            raise CommandProcessError("COMMAND_CLEANUP_FAILED", "Plugin command scope could not be closed") from cleanup_error
    context.check_cancelled()
    if failed_read.is_set():
        raise CommandProcessError("COMMAND_IO_FAILED", "Plugin command output could not be read")
    stdout, stderr = (bytes(value).decode("utf-8", errors="replace") for value in buffers)
    if protocol is not None:
        try:
            protocol.finish(process_failed=bool(process.returncode))
        except OperationError as error:
            if process.returncode:
                extra = getattr(error, "extra", {})
                error.extra = {**(extra if isinstance(extra, dict) else {}), "exitCode": process.returncode,
                               **({"stderr": stderr[-4000:]} if stderr else {})}
            raise
    if process.returncode:
        raise CommandProcessError("COMMAND_FAILED", f"Plugin command exited with status {process.returncode}", detail=stderr[-4000:])
    return subprocess.CompletedProcess(list(arguments), 0, stdout, stderr)
