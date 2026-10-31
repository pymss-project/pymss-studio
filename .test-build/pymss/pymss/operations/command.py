"""Experimental command adapter using the existing v2 Operation contract.

Plugin setup calls ``register_commands(plugin, Path(__file__).parent)``. The
language-independent command declarations live in ``[tool.pymss.commands]``;
their format version is independent of plugin API and Studio SDK versions.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import contextmanager
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import tempfile
import tomllib
from typing import Any, Mapping

from . import OperationContext, OperationError, OperationSpec
from ._command_process import run_command_process
from .files import FileArtifact, input_file

_TARGET = re.compile(r"(?:win32|darwin|linux)-(?:x86_64|arm64)\Z")
_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"{name}{i}" for name in ("COM", "LPT") for i in range(1, 10))}


def _relative_file(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\0" in value:
        raise OperationError("Command executable must be a relative package path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise OperationError("Command executable must stay inside its package")
    return path


def _executable(root: Path, value: str) -> Path:
    path = root.joinpath(*_relative_file(value).parts)
    current = path
    while current != root:
        if current.is_symlink():
            raise OperationError("Command executable cannot follow a symbolic link")
        current = current.parent
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise OperationError("The command executable for this platform is not installed")
    if os.name == "nt" and path.suffix.lower() != ".exe":
        raise OperationError("Windows command targets must be native .exe programs")
    if os.name != "nt" and not os.access(path, os.X_OK):
        raise OperationError("The command executable is not executable on this platform")
    return path


def current_target() -> str:
    import sys

    machine = platform.machine().lower()
    architecture = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    return f"{sys.platform}-{architecture}"


@dataclass(frozen=True)
class CommandOperation:
    root: Path
    spec: OperationSpec
    targets: Mapping[str, str]
    arguments: tuple[str | dict[str, str], ...]
    timeout: float
    max_output_bytes: int
    node_type: str | None = None
    plugin_id: str = ""
    python_entry: str | None = None
    python_binding: tuple[Path, str] | None = None
    python_plan_digest: str | None = None

    @contextmanager
    def program(self):
        if self.python_entry is not None:
            from .python_profiles import package_file, python_environment

            with python_environment(self.root, binding=self.python_binding, expected_digest=self.python_plan_digest) as interpreter:
                root = interpreter.parent.parent / "package"
                script = package_file(root, self.python_entry)
                yield (str(interpreter), "-I", "-B", "-u", "-X", "utf8",
                       str(Path(__file__).with_name("_python_entry.py")), str(root), str(script))
        else:
            target = self.targets.get(current_target())
            if target is None:
                raise OperationError("This command does not provide an executable for the current platform and architecture")
            yield _executable(self.root, target)

    def invoke(self, context: OperationContext, program: Path | tuple[str, ...], files: Mapping[str, Path],
               params: Mapping[str, Any], output: Path, stage: Path) -> None:
        arguments = list(program) if isinstance(program, tuple) else [str(program)]
        for token in self.arguments:
            if isinstance(token, str):
                value = token
            elif "input" in token:
                value = str(files[token["input"]])
            elif "output" in token:
                value = str(output)
            else:
                value = params[token["param"]]
                if isinstance(value, str) and (value.startswith("-") or (os.name == "nt" and value.startswith("/"))):
                    raise OperationError("String parameter values cannot be command options; declare options as literal arguments")
                if isinstance(value, float) and not math.isfinite(value):
                    raise OperationError("Command numeric arguments must be finite")
                value = str(value).lower() if isinstance(value, bool) else str(value)
            if "\0" in value:
                raise OperationError("Command arguments cannot contain null characters")
            arguments.append(value)
        run_command_process(arguments, cwd=stage, context=context, timeout=self.timeout)

    def run(self, context: OperationContext, inputs: Mapping[str, Any], params: Mapping[str, Any]) -> dict:
        context.check_cancelled()
        with self.program() as program:
            return self._run(context, program, inputs, params)

    def _run(self, context: OperationContext, program, inputs: Mapping[str, Any], params: Mapping[str, Any]) -> dict:
        files = {name: input_file(inputs[name]) for name in self.spec.inputs}
        for name, path in files.items():
            extensions = self.spec.inputs[name].get("extensions", [])
            if extensions and path.suffix.lower().lstrip(".") not in extensions:
                raise OperationError(f"Input {name!r} has an unsupported file extension")
        if not context.workdir:
            raise OperationError("A command operation requires an explicit output working directory")
        destination = Path(context.workdir).expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        name, descriptor = next(iter(self.spec.outputs.items()))
        from ..plugins.kit import render_output

        scratch = os.environ.get("PYMSS_SCRATCH_DIR") or None
        with tempfile.TemporaryDirectory(prefix="pymss-command-", dir=scratch, ignore_cleanup_errors=True) as temporary:
            stage = Path(temporary).resolve(strict=True)
            output = stage / descriptor["filename"]
            context.report_progress(0, "Starting plugin command", event={"type": "command_stage", "payload": {"phase": "running"}})
            self.invoke(context, program, files, params, output, stage)
            info = output.lstat() if output.exists() or output.is_symlink() else None
            if info is None or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or output.resolve().parent != stage:
                raise OperationError("The command did not produce its declared regular output file")
            if info.st_size > self.max_output_bytes:
                raise OperationError("The command output file exceeds its declared size limit")
            context.report_progress(0.95, "Publishing command output", event={"type": "command_stage", "payload": {"phase": "publishing"}})
            context.check_cancelled()
            # The existing exclusive publisher preserves earlier user files;
            # hosts already own its temporary-file and forced-exit cleanup.
            published = render_output(destination / descriptor["filename"], lambda partial: shutil.copyfile(output, partial), allow_empty=True)
        return {name: FileArtifact(str(published), descriptor.get("media_type", "application/octet-stream"))}


def read_commands(directory: str | Path, *, adapter: str = "command") -> list[CommandOperation]:
    if adapter not in {"command", "rpc-process"}:
        raise OperationError("Unsupported process adapter")
    rpc = adapter == "rpc-process"
    root = Path(directory).resolve()
    manifest = root / "pyproject.toml"
    if not manifest.is_file() or manifest.stat().st_size > 2 * 1024 * 1024:
        raise OperationError("Command plugin manifest is unavailable or too large")
    project = tomllib.loads(manifest.read_text(encoding="utf-8"))
    config = project.get("tool", {}).get("pymss", {}).get("rpc_process" if rpc else "commands", {})
    if type(config.get("format_version")) is not int or config["format_version"] != 1 or set(config) != {"format_version", "operations"}:
        raise OperationError("Unsupported command declaration format")
    values = config["operations"]
    if not isinstance(values, list) or not 1 <= len(values) <= 32:
        raise OperationError("Declare between 1 and 32 command operations")
    result, seen = [], set()
    python_plan = None
    for value in values:
        allowed = {"id", "schema_version", "title", "description", "inputs", "params", "outputs", "targets", "python", "arguments", "timeout", "max_output_bytes", "node_type"}
        if rpc:
            allowed |= {"protocol_version", "permissions", "host_timeout"}
        if not isinstance(value, dict) or set(value) - allowed:
            raise OperationError("Unsupported command operation fields")
        spec = OperationSpec(id=value.get("id"), schema_version=value.get("schema_version", 1),
                             title=value.get("title", value.get("id")), description=value.get("description", ""),
                             inputs=value.get("inputs", {}), params=value.get("params", {"type": "object", "properties": {}, "additionalProperties": False}),
                             outputs=value.get("outputs", {}))
        if spec.id in seen or not spec.id.startswith(project.get("tool", {}).get("pymss", {}).get("plugin", {}).get("id", "") + "."):
            raise OperationError("Command operation ids must be unique and belong to their plugin")
        seen.add(spec.id)
        if type(spec.schema_version) is not int:
            raise OperationError("Command operation schema_version must be a positive integer")
        if not isinstance(spec.title, str) or not isinstance(spec.description, str):
            raise OperationError("Command titles and descriptions must be strings")
        for name, port in spec.inputs.items():
            if not _NAME.fullmatch(name) or port.get("type") != "file" or set(port) - {"type", "title", "extensions"}:
                raise OperationError("Command inputs must be named file ports")
            extensions = port.get("extensions", [])
            if not isinstance(extensions, list) or len(extensions) > 32 or any(not isinstance(item, str) or not re.fullmatch(r"[a-z0-9]{1,12}", item) for item in extensions):
                raise OperationError("Command file extensions must be lowercase names without a dot")
        if len(spec.outputs) != 1:
            raise OperationError("A command operation must declare exactly one output file")
        for name, port in spec.outputs.items():
            filename = port.get("filename")
            if (not _NAME.fullmatch(name) or port.get("type") != "file" or set(port) - {"type", "title", "filename", "media_type"}
                    or not isinstance(filename, str) or not _FILENAME.fullmatch(filename) or filename.endswith(".")
                    or filename.split(".")[0].upper() in _RESERVED):
                raise OperationError("Command output must declare a portable, plain filename")
            if not isinstance(port.get("media_type", ""), str) or len(port.get("media_type", "")) > 200:
                raise OperationError("Invalid command output media type")
        python_entry = value.get("python")
        targets = value.get("targets", {})
        if python_entry is not None:
            from .python_profiles import package_file, read_python_plan

            if python_plan is None:
                python_plan = read_python_plan(root)
            if "targets" in value or python_plan is None:
                raise OperationError("Python commands require a Python environment declaration instead of native targets")
            if package_file(root, python_entry).suffix != ".py":
                raise OperationError("Python command entry points must be .py files")
        elif not isinstance(targets, dict) or not targets or len(targets) > 6:
            raise OperationError("Command operations require platform executable targets")
        for target, executable in targets.items():
            if not _TARGET.fullmatch(target):
                raise OperationError("Unsupported command platform target")
            _relative_file(executable)
        arguments = value.get("arguments")
        if not isinstance(arguments, list) or len(arguments) > 256:
            raise OperationError("Command arguments must be a bounded argv array")
        referenced_outputs = set()
        for token in arguments:
            if isinstance(token, str):
                if len(token) > 8192 or "\0" in token:
                    raise OperationError("Invalid command argument literal")
                continue
            if rpc:
                raise OperationError("RPC process arguments must be literals; inputs and parameters use the protocol")
            if not isinstance(token, dict) or len(token) != 1:
                raise OperationError("Command argument references must have exactly one source")
            kind, name = next(iter(token.items()))
            available = {"input": spec.inputs, "param": spec.params.get("properties", {}), "output": spec.outputs}
            if kind not in available or not isinstance(name, str) or name not in available[kind]:
                raise OperationError("Command argument references an undeclared port or parameter")
            if kind == "param" and "default" not in available[kind][name] and name not in spec.params.get("required", []):
                raise OperationError("Referenced command parameters must be required or have a default")
            if kind == "param" and available[kind][name].get("type") not in {"string", "integer", "number", "boolean"}:
                raise OperationError("Command argument parameters must have scalar types")
            if kind == "output":
                referenced_outputs.add(name)
        if not rpc and referenced_outputs != set(spec.outputs):
            raise OperationError("Command arguments must include the declared output file")
        timeout, maximum = value.get("timeout", 300), value.get("max_output_bytes", 256 * 1024 * 1024)
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not 0 < timeout <= 86400:
            raise OperationError("Command timeout must be finite and between 0 and 86400 seconds")
        if type(maximum) is not int or not 0 <= maximum <= 16 * 1024 * 1024 * 1024:
            raise OperationError("Invalid command output size limit")
        node_type = value.get("node_type")
        if node_type is not None:
            nodes = project.get("tool", {}).get("pymss", {}).get("plugin", {}).get("contributions", {}).get("nodes", [])
            declaration = next((node for node in nodes if node.get("type") == node_type), None)
            if declaration is None:
                raise OperationError("Command node_type must also be declared as a v2 plugin node")
            expected_inputs = [{"name": name, "type": "STRING"} for name in [*spec.inputs, *spec.params.get("properties", {})]]
            expected_outputs = [{"name": name, "type": "STRING"} for name in spec.outputs]
            if declaration.get("inputs", []) != expected_inputs or declaration.get("outputs", []) != expected_outputs:
                raise OperationError("Command node ports must match its operation's files and named parameters")
        options = dict(root=root, spec=spec, targets=targets, arguments=tuple(arguments), timeout=float(timeout),
                       max_output_bytes=maximum, node_type=node_type, plugin_id=project["tool"]["pymss"]["plugin"]["id"], python_entry=python_entry,
                       python_plan_digest=python_plan.digest if python_entry is not None else None)
        if rpc:
            from .rpc_process import RpcOperation
            from .host_services import HOST_METHODS

            if type(value.get("protocol_version")) is not int or value["protocol_version"] != 1:
                raise OperationError("RPC process declarations require protocol_version = 1")
            permissions, host_timeout = value.get("permissions", []), value.get("host_timeout", 5)
            if (not isinstance(permissions, list) or not all(isinstance(item, str) for item in permissions)
                    or len(permissions) != len(set(permissions)) or any(item not in HOST_METHODS for item in permissions)):
                raise OperationError("Unsupported or duplicate RPC host permissions")
            if isinstance(host_timeout, bool) or not isinstance(host_timeout, (float, int)) or not 0 < host_timeout <= 30:
                raise OperationError("RPC host timeout must be finite and between 0 and 30 seconds")
            result.append(RpcOperation(**options, permissions=frozenset(permissions), host_timeout=float(host_timeout)))
        else:
            result.append(CommandOperation(**options))
    return result


def register_commands(plugin, directory: str | Path) -> None:
    """Register all declarations through the existing v2 handle and rollback."""
    register_process_operations(plugin, read_commands(directory))


def register_process_operations(plugin, commands: list[CommandOperation]) -> None:
    for command in commands:
        if getattr(plugin, "source", None) != "plugin:" + command.plugin_id:
            raise OperationError("Process declarations do not belong to the registering plugin")
    bound, python_bindings = [], {}
    for command in commands:
        if command.python_entry is not None:
            from .python_profiles import python_binding

            if command.root not in python_bindings:
                python_bindings[command.root] = python_binding(command.root, expected_digest=command.python_plan_digest)
            command = replace(command, python_binding=python_bindings[command.root])
        with command.program():
            pass
        bound.append(command)
    for command in bound:
        spec = command.spec
        plugin.register_operation(id=spec.id, title=spec.title, description=spec.description,
                                  schema_version=spec.schema_version, inputs=spec.inputs, params=spec.params,
                                  outputs=spec.outputs, run=command.run)
        if command.node_type is not None:
            from .graph_bridge import register_operation_node

            register_operation_node(spec=spec, run=command.run, node_type=command.node_type, plugin=plugin)
