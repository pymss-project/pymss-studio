"""Task-scoped, read-only host callbacks for external operation processes."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import stat
import time
from typing import Any, Callable, Mapping

HOST_METHODS = frozenset({"host.taskInfo", "host.fileInfo"})


class HostServiceError(Exception):
    def __init__(self, code: str, message: str, rpc_code: int = -32000):
        super().__init__(message)
        self.code, self.rpc_code = code, rpc_code


@dataclass(frozen=True)
class HostRequest:
    id: str
    plugin_id: str
    operation_id: str
    task_id: str
    method: str
    params: Mapping[str, Any]
    permissions: frozenset[str]
    inputs: Mapping[str, Path]
    deadline: float
    cancelled: Callable[[], bool]

    def check_active(self) -> None:
        if self.cancelled():
            raise HostServiceError("HOST_CANCELLED", "The task no longer accepts host requests", -32800)
        if time.monotonic() >= self.deadline:
            raise HostServiceError("HOST_TIMEOUT", "The host request deadline expired", -32001)


@dataclass(frozen=True)
class ReadonlyHost:
    """Explicit headless/Worker services; no GUI, environment or arbitrary paths."""
    name: str
    operation_id: str | None = None

    def __call__(self, request: HostRequest) -> dict[str, Any]:
        request.check_active()
        if self.operation_id is not None and request.operation_id != self.operation_id:
            raise HostServiceError("HOST_PERMISSION_DENIED", "The request belongs to another operation", -32003)
        if request.method not in HOST_METHODS:
            raise HostServiceError("HOST_METHOD_NOT_FOUND", "This host service is unavailable", -32601)
        if request.method not in request.permissions:
            raise HostServiceError("HOST_PERMISSION_DENIED", "The plugin did not declare this host service", -32003)
        if request.method == "host.taskInfo":
            if request.params:
                raise HostServiceError("HOST_INVALID_PARAMS", "Task information does not accept parameters", -32602)
            return {"host": self.name, "interactive": False, "pluginId": request.plugin_id,
                    "operationId": request.operation_id, "taskId": request.task_id,
                    "capabilities": sorted(HOST_METHODS & request.permissions)}
        if set(request.params) != {"input"} or not isinstance(request.params["input"], str):
            raise HostServiceError("HOST_INVALID_PARAMS", "A declared input reference is required", -32602)
        reference = request.params["input"]
        if reference not in request.inputs:
            raise HostServiceError("HOST_PERMISSION_DENIED", "The file is not an input of this task", -32003)
        path = request.inputs[reference]
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise OSError("Input is not a file")
        except OSError as error:
            raise HostServiceError("HOST_FILE_UNAVAILABLE", "The selected input file is unavailable", -32004) from error
        request.check_active()
        return {"input": reference, "name": path.name, "size": info.st_size, "extension": path.suffix.lower().lstrip(".")}
