"""One-task JSON-RPC session, independent of the process supervision layer."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import queue
import re
import threading
import time
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4

from . import OperationContext, OperationError
from .host_services import HOST_METHODS, HostRequest, HostServiceError

MAX_FRAME = 256 * 1024
_PLUGIN_ID = re.compile(r"plugin:[A-Za-z0-9._-]{1,80}\Z")
_HOST_SLOTS = threading.BoundedSemaphore(8)


class RpcProcessError(OperationError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.recoverable = code, True


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON member")
        value[key] = item
    return value


def _constant(_value):
    raise ValueError("Non-finite JSON number")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


def _frame(value: Mapping) -> bytes:
    try:
        raw = json.dumps(dict(value), ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii") + b"\n"
    except (TypeError, ValueError, RecursionError) as error:
        raise RpcProcessError("RPC_INVALID_MESSAGE", "The process message is not a finite JSON object") from error
    if len(raw) > MAX_FRAME:
        raise RpcProcessError("RPC_FRAME_LIMIT", "The process message exceeds the frame limit")
    return raw


@dataclass
class _Callback:
    deadline: float
    cancelled: threading.Event


class RpcSession:
    def __init__(self, *, context: OperationContext, plugin_id: str, operation_id: str,
                 inputs: Mapping[str, Path], params: Mapping[str, Any], output_name: str, output: Path,
                 permissions: frozenset[str], host_timeout: float = 5, handshake_timeout: float = 10):
        self.context, self.plugin_id, self.operation_id = context, plugin_id, operation_id
        self.host = getattr(context, "host_request", None)
        self.task_id = "task:" + uuid4().hex
        self.inputs = MappingProxyType(dict(inputs))
        self.params = dict(params)
        self.output_name, self.output = output_name, output
        self.permissions, self.host_timeout, self.handshake_timeout = permissions, host_timeout, handshake_timeout
        self.state = "new"
        self.deadline = 0.0
        self.incoming: queue.Queue = queue.Queue(maxsize=32)
        self.outgoing: queue.Queue = queue.Queue(maxsize=32)
        self.completed: queue.Queue = queue.Queue(maxsize=8)
        self.stop = threading.Event()
        self.eof = threading.Event()
        self.reader_error: Exception | None = None
        self.writer_error: Exception | None = None
        self.callback_error: RpcProcessError | None = None
        self.eof_deadline = 0.0
        self.lock = threading.Lock()
        self.latest_progress: dict | None = None
        self.fraction = 0.0
        self.active: dict[str, _Callback] = {}
        self.seen_requests: set[str] = set()
        self.workers: set[threading.Thread] = set()
        self.writer: threading.Thread | None = None
        self.abandoned_requests = False

    def start(self, stream) -> None:
        self.state, self.deadline = "initializing", time.monotonic() + self.handshake_timeout

        def write():
            try:
                while not self.stop.is_set():
                    try:
                        data = self.outgoing.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    stream.write(data)
                    stream.flush()
            except (OSError, ValueError) as error:
                if not self.stop.is_set():
                    self.writer_error = error

        self.writer = threading.Thread(target=write, daemon=True)
        self.writer.start()
        self._send({"jsonrpc": "2.0", "id": "host:1", "method": "rpc.initialize", "params": {
            "protocolVersion": 1, "pluginId": self.plugin_id, "operationId": self.operation_id,
            "taskId": self.task_id, "permissions": sorted(self.permissions),
        }})

    def _send(self, value):
        if self.stop.is_set():
            return
        try:
            self.outgoing.put_nowait(_frame(value))
        except queue.Full as error:
            raise RpcProcessError("RPC_BACKPRESSURE", "The plugin is not reading its bounded response channel") from error

    def read_stdout(self, stream) -> None:
        try:
            while True:
                raw = stream.readline(MAX_FRAME + 1)
                if not raw:
                    break
                if len(raw) > MAX_FRAME or not raw.endswith(b"\n"):
                    raise RpcProcessError("RPC_FRAME_LIMIT", "The plugin sent an oversized or incomplete protocol frame")
                try:
                    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant, parse_float=_float)
                except (UnicodeError, ValueError, RecursionError) as error:
                    raise RpcProcessError("RPC_INVALID_MESSAGE", "The plugin sent invalid protocol JSON") from error
                if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                    raise RpcProcessError("RPC_INVALID_MESSAGE", "The plugin sent an invalid JSON-RPC object")
                if value.get("method") == "task.progress" and "id" not in value:
                    params = value.get("params")
                    if (set(value) != {"jsonrpc", "method", "params"} or not isinstance(params, dict)
                            or set(params) - {"fraction", "message"} or isinstance(params.get("fraction"), bool)
                            or not isinstance(params.get("fraction"), (float, int))
                            or not 0 <= params["fraction"] <= 1 or not math.isfinite(params["fraction"])
                            or not isinstance(params.get("message", ""), str) or len(params.get("message", "")) > 1024):
                        raise RpcProcessError("RPC_INVALID_MESSAGE", "The plugin sent invalid task progress")
                    with self.lock:
                        message = params.get("message", "").replace("\r", " ").replace("\n", " ")
                        message = re.sub(r"[\x00-\x1f\x7f]", "", message).encode("utf-8", errors="replace").decode("utf-8")
                        self.latest_progress = {"fraction": params["fraction"], "message": message}
                    continue
                try:
                    self.incoming.put_nowait(value)
                except queue.Full as error:
                    raise RpcProcessError("RPC_BACKPRESSURE", "The plugin exceeded the reliable message window") from error
        except (OSError, ValueError, RpcProcessError) as error:
            self.reader_error = error
        finally:
            self.eof.set()
            stream.close()

    def _reply_error(self, request_id, error: HostServiceError):
        self._send({"jsonrpc": "2.0", "id": request_id, "error": {
            "code": error.rpc_code, "message": str(error), "data": {"code": error.code},
        }})

    def _host_request(self, frame):
        request_id, method = frame.get("id"), frame.get("method")
        if (set(frame) != {"jsonrpc", "id", "method", "params"} or not isinstance(request_id, str)
                or not _PLUGIN_ID.fullmatch(request_id) or not isinstance(method, str) or not isinstance(frame.get("params"), dict)):
            raise RpcProcessError("RPC_INVALID_MESSAGE", "The plugin sent an invalid host request")
        if request_id in self.seen_requests:
            raise RpcProcessError("RPC_DUPLICATE_REQUEST", "The plugin reused a host request id")
        self.seen_requests.add(request_id)
        if len(self.seen_requests) > 128:
            raise RpcProcessError("RPC_REQUEST_LIMIT", "The plugin exceeded its task host request budget")
        if self.state != "running" or self.stop.is_set():
            self._reply_error(request_id, HostServiceError("HOST_TASK_FINISHED", "The task does not accept host requests", -32004))
            return
        if method not in HOST_METHODS:
            self._reply_error(request_id, HostServiceError("HOST_METHOD_NOT_FOUND", "This host service is unavailable", -32601))
            return
        if method not in self.permissions:
            self._reply_error(request_id, HostServiceError("HOST_PERMISSION_DENIED", "The plugin did not declare this host service", -32003))
            return
        if not callable(self.host):
            self._reply_error(request_id, HostServiceError("HOST_UNAVAILABLE", "This execution host does not provide callbacks", -32004))
            return
        self.workers = {worker for worker in self.workers if worker.is_alive()}
        if len(self.workers) >= 4 or len(self.active) >= 4:
            self._reply_error(request_id, HostServiceError("HOST_BUSY", "The task host service window is full", -32005))
            return
        if not _HOST_SLOTS.acquire(blocking=False):
            self._reply_error(request_id, HostServiceError("HOST_BUSY", "The execution host service window is full", -32005))
            return
        callback = _Callback(time.monotonic() + self.host_timeout, threading.Event())
        self.active[request_id] = callback
        request = HostRequest(request_id, self.plugin_id, self.operation_id, self.task_id, method,
            MappingProxyType(dict(frame["params"])), self.permissions, self.inputs, callback.deadline,
            lambda: self.stop.is_set() or callback.cancelled.is_set())

        def invoke_service():
            try:
                request.check_active()
                value = self.host(request)
                request.check_active()
                response = {"jsonrpc": "2.0", "id": request_id, "result": value}
                _frame(response)
            except HostServiceError as error:
                response = {"jsonrpc": "2.0", "id": request_id, "error": {
                    "code": error.rpc_code, "message": str(error), "data": {"code": error.code}}}
            except Exception:
                response = {"jsonrpc": "2.0", "id": request_id, "error": {
                    "code": -32603, "message": "The host service failed", "data": {"code": "HOST_SERVICE_FAILED"}}}
            if not self.stop.is_set() and not callback.cancelled.is_set():
                try:
                    self.completed.put_nowait((request_id, callback, response))
                except queue.Full:
                    self.callback_error = RpcProcessError("RPC_BACKPRESSURE", "Host callback results exceeded the response window")

        def invoke():
            try:
                invoke_service()
            finally:
                _HOST_SLOTS.release()

        worker = threading.Thread(target=invoke, daemon=True)
        self.workers.add(worker)
        try:
            worker.start()
        except BaseException:
            self.workers.discard(worker)
            self.active.pop(request_id, None)
            _HOST_SLOTS.release()
            raise

    def _response(self, frame):
        request_id = frame.get("id")
        if set(frame) not in ({"jsonrpc", "id", "result"}, {"jsonrpc", "id", "error"}):
            raise RpcProcessError("RPC_INVALID_MESSAGE", "The plugin sent an invalid response")
        expected = {"initializing": "host:1", "running": "host:2", "shutting_down": "host:3"}.get(self.state)
        if expected is None or not isinstance(request_id, str) or request_id != expected:
            raise RpcProcessError("RPC_UNEXPECTED_RESPONSE", "The plugin replied to an unknown or completed request")
        if self.state == "running":
            self._report_progress()
        if "error" in frame:
            error = frame["error"]
            if not isinstance(error, dict) or type(error.get("code")) is not int or not isinstance(error.get("message"), str):
                raise RpcProcessError("RPC_INVALID_MESSAGE", "The plugin sent an invalid error response")
            raise RpcProcessError("RPC_REMOTE_ERROR", error["message"][:4000])
        result = frame["result"]
        if self.state == "initializing":
            if not isinstance(result, dict) or type(result.get("protocolVersion")) is not int or result != {"protocolVersion": 1}:
                raise RpcProcessError("RPC_VERSION_MISMATCH", "The plugin did not accept process protocol version 1")
            self.state, self.deadline = "running", 0.0
            self._send({"jsonrpc": "2.0", "id": "host:2", "method": "operations.run", "params": {
                "operationId": self.operation_id, "taskId": self.task_id,
                "inputs": {name: {"kind": "file", "ref": name, "path": str(path)} for name, path in self.inputs.items()},
                "params": self.params, "outputs": {self.output_name: {"kind": "file", "path": str(self.output)}},
            }})
        elif self.state == "running":
            if self.active:
                raise RpcProcessError("RPC_PENDING_REQUESTS", "The plugin completed with unresolved host requests")
            expected_result = {"outputs": {self.output_name: {"kind": "file", "path": str(self.output)}}}
            if result != expected_result:
                raise RpcProcessError("RPC_INVALID_RESULT", "The plugin returned a file outside its declared output reference")
            self.state, self.deadline = "shutting_down", time.monotonic() + self.handshake_timeout
            self._send({"jsonrpc": "2.0", "id": "host:3", "method": "rpc.shutdown", "params": {}})
        else:
            if result != {}:
                raise RpcProcessError("RPC_INVALID_RESULT", "The plugin returned an invalid shutdown response")
            self.state, self.deadline = "done", time.monotonic() + self.handshake_timeout

    def _report_progress(self):
        with self.lock:
            progress, self.latest_progress = self.latest_progress, None
        if progress is not None and self.state == "running" and not self.stop.is_set():
            self.fraction = max(self.fraction, float(progress["fraction"]))
            self.context.report_progress(min(.94, self.fraction * .94), progress.get("message", ""),
                event={"type": "rpc_progress", "payload": {"pluginId": self.plugin_id, "taskId": self.task_id}})

    def update(self):
        if self.reader_error is not None:
            if isinstance(self.reader_error, RpcProcessError):
                raise self.reader_error
            raise RpcProcessError("RPC_IO_FAILED", "The plugin protocol channel could not be read")
        if self.writer_error is not None:
            raise RpcProcessError("RPC_IO_FAILED", "The plugin protocol channel could not be written")
        if self.callback_error is not None:
            raise self.callback_error
        for _ in range(32):
            try:
                frame = self.incoming.get_nowait()
            except queue.Empty:
                break
            if "method" in frame:
                self._host_request(frame)
            else:
                self._response(frame)
        self._report_progress()
        for _ in range(4):
            try:
                request_id, callback, response = self.completed.get_nowait()
            except queue.Empty:
                break
            if self.active.get(request_id) is callback:
                del self.active[request_id]
                if time.monotonic() >= callback.deadline:
                    callback.cancelled.set()
                    self._reply_error(request_id, HostServiceError("HOST_TIMEOUT", "The host request deadline expired", -32001))
                else:
                    self._send(response)
        for request_id, callback in list(self.active.items()):
            if time.monotonic() >= callback.deadline:
                callback.cancelled.set()
                del self.active[request_id]
                self._reply_error(request_id, HostServiceError("HOST_TIMEOUT", "The host request deadline expired", -32001))
        if not self.stop.is_set():
            if self.eof.is_set() and self.incoming.empty() and self.state != "done":
                self.eof_deadline = self.eof_deadline or time.monotonic() + min(1, self.handshake_timeout)
                if time.monotonic() >= self.eof_deadline:
                    raise RpcProcessError("RPC_PEER_EXITED", "The plugin closed its protocol channel before completing the task")
            if self.deadline and time.monotonic() >= self.deadline:
                if self.state == "done":
                    raise RpcProcessError("RPC_EXIT_TIMEOUT", "The plugin acknowledged shutdown but did not exit")
                raise RpcProcessError("RPC_HANDSHAKE_TIMEOUT", "The plugin did not complete protocol negotiation or shutdown")

    def cancel_waiters(self):
        self.stop.set()
        self.abandoned_requests = self.abandoned_requests or bool(self.active)
        for callback in self.active.values():
            callback.cancelled.set()
        self.active.clear()

    def close(self):
        self.cancel_waiters()
        if self.writer is not None:
            self.writer.join(timeout=2)
            if self.writer.is_alive():
                raise RpcProcessError("RPC_CLEANUP_FAILED", "The protocol writer did not stop")
        deadline = time.monotonic() + .5
        for worker in self.workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))

    def finish(self, *, process_failed: bool = False):
        self.update()
        if not process_failed and (self.state != "done" or self.active or self.abandoned_requests):
            raise RpcProcessError("RPC_PEER_EXITED", "The plugin exited without completing its task protocol")
