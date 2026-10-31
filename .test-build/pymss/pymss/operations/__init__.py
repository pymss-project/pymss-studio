"""Operation model: serializable specs, execution contract, and runtime.

Design (round 2 of the plugin redesign, docs/plugin-redesign-plan.md):

- ``OperationSpec`` describes one executable processing step: named inputs,
  JSON-Schema params, named outputs. It is plain data — serializable without
  the handler — so hosts (Studio worker, Comfy, CLI) can render UI from it.
- The handler is attached at ``Runtime.register`` time and never travels with
  the spec.
- ``create_runtime()`` builds an *isolated* registry instance. The legacy
  global ``_REGISTRY`` keeps serving old-style plugins; runtimes own their
  operations outright.
- Validation is deliberately small: object-typed params with type/default/
  minimum/maximum/enum support and additionalProperties checking. It is not a
  full JSON-Schema implementation; specs that use unsupported keywords are
  rejected at register time rather than silently mis-validated.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from .._identifiers import OPERATION_ID_RE

if TYPE_CHECKING:
    from ..plugins.lifecycle import PluginState
    from .host_services import HostRequest

__all__ = [
    "OperationError",
    "OperationSpec",
    "OperationContext",
    "Runtime",
    "create_runtime",
    "AUDIO_TYPE",
]

AUDIO_TYPE = "audio"

_SUPPORTED_PARAM_TYPES = {"object", "number", "integer", "string", "boolean"}


class OperationError(Exception):
    """Raised for spec construction, registration, and execution failures."""


@dataclass(frozen=True)
class OperationSpec:
    """Serializable description of one operation.

    ``inputs``/``outputs`` map a port name to ``{"type": "audio"}``-style
    descriptors. ``params`` is a JSON-Schema object schema for the operation's
    parameter set. ``run`` is intentionally not part of the serialized form.
    """

    id: str
    schema_version: int
    title: str
    inputs: Mapping[str, Mapping[str, Any]]
    params: Mapping[str, Any]
    outputs: Mapping[str, Mapping[str, Any]]
    run: Callable[..., Any] | None = None  # attached at Runtime.register
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not OPERATION_ID_RE.fullmatch(self.id):
            raise OperationError(
                f"operation id must be a dot-namespaced lowercase identifier, got {self.id!r}"
            )
        if not isinstance(self.schema_version, int) or self.schema_version < 1:
            raise OperationError("schema_version must be a positive integer")
        _validate_ports("inputs", self.inputs)
        _validate_ports("outputs", self.outputs)
        _validate_params_schema(self.params)

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict form safe for JSON serialization (no handler)."""
        return {
            "id": self.id,
            "schemaVersion": self.schema_version,
            "title": self.title,
            "description": self.description,
            "inputs": {k: dict(v) for k, v in self.inputs.items()},
            "params": dict(self.params),
            "outputs": {k: dict(v) for k, v in self.outputs.items()},
        }


def _validate_ports(kind: str, ports: Mapping[str, Mapping[str, Any]]) -> None:
    if not isinstance(ports, Mapping):
        raise OperationError(f"{kind} must be a mapping of port name -> descriptor")
    for name, descriptor in ports.items():
        if not isinstance(name, str) or not name:
            raise OperationError(f"{kind} port names must be non-empty strings")
        if not isinstance(descriptor, Mapping) or "type" not in descriptor:
            raise OperationError(
                f"{kind}[{name!r}] descriptor must be a mapping with a 'type' key"
            )


def _validate_params_schema(schema: Mapping[str, Any]) -> None:
    if not isinstance(schema, Mapping):
        raise OperationError("params must be a mapping (JSON-Schema object)")
    t = schema.get("type")
    if t != "object":
        raise OperationError("params schema must have type 'object' at the top level")
    props = schema.get("properties", {})
    if not isinstance(props, Mapping):
        raise OperationError("params.properties must be a mapping")
    for pname, pschema in props.items():
        if not isinstance(pschema, Mapping):
            raise OperationError(f"params.properties[{pname!r}] must be a mapping")
        ptype = pschema.get("type")
        if ptype not in _SUPPORTED_PARAM_TYPES - {"object"}:
            raise OperationError(
                f"param {pname!r} has unsupported type {ptype!r} "
                f"(supported: number, integer, string, boolean)"
            )
        for keyword in ("minimum", "maximum", "default", "enum"):
            if keyword in pschema and not _check_keyword_value(ptype, keyword, pschema[keyword]):
                raise OperationError(
                    f"param {pname!r} has a {keyword!r} value inconsistent with type {ptype!r}"
                )


def _check_keyword_value(ptype: str, keyword: str, value: Any) -> bool:
    if keyword == "enum":
        return isinstance(value, list) and value
    if keyword == "default":
        return _matches_type(ptype, value)
    if ptype == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if ptype == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if ptype == "string":
        return isinstance(value, str)
    if ptype == "boolean":
        return isinstance(value, bool)
    return False


def _matches_type(ptype: str, value: Any) -> bool:
    if ptype == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if ptype == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if ptype == "string":
        return isinstance(value, str)
    if ptype == "boolean":
        return isinstance(value, bool)
    return False


@dataclass
class OperationContext:
    """Execution context handed to every operation run.

    ``cancelled`` is checked cooperatively via ``ctx.check_cancelled()``.
    An optional ``host_request`` supplies task-scoped services to RPC peers;
    absence means services are unavailable, never implicit user approval.
    """

    workdir: str | None = None
    progress: Callable[[float, str, dict[str, Any] | None], None] | None = None
    cancelled: Callable[[], bool] | None = None
    logger: Any = None
    host_request: Callable[[HostRequest], Any] | None = None

    def report_progress(self, fraction: float, message: str = "", *, event: Mapping[str, Any] | None = None) -> None:
        """Report progress; ``event`` carries optional structured host data (``{"type", "payload"}``)."""
        if self.progress is not None:
            self.progress(fraction, message, dict(event) if event is not None else None)

    def check_cancelled(self) -> None:
        if self.cancelled is not None and self.cancelled():
            raise OperationError("operation cancelled")


@dataclass
class _OperationEntry:
    spec: OperationSpec
    run: Callable[..., Any]
    source: str


@dataclass
class Runtime:
    """Isolated operation registry + executor.

    Not shared across processes or hosts; each host (CLI, Studio worker,
    Comfy) creates its own instance. Capabilities/nodes/CLI commands that a
    plugin registers from ``setup()`` still land in the global registry.
    """

    _operations: dict[str, _OperationEntry] = field(default_factory=dict)
    _load_report: list[dict[str, Any]] = field(default_factory=list)
    _active_plugins: dict[str, PluginState] = field(default_factory=dict)

    # ---- registration ----

    def register(
        self,
        *,
        spec: OperationSpec,
        run: Callable[..., Any],
        source: str = "builtin",
    ) -> None:
        if spec.id in self._operations:
            existing = self._operations[spec.id]
            raise OperationError(
                f"operation {spec.id!r} already registered by {existing.source!r}"
            )
        if not callable(run):
            raise OperationError(f"run for {spec.id!r} must be callable")
        self._operations[spec.id] = _OperationEntry(spec=spec, run=run, source=source)

    # ---- discovery ----

    def list_operations(self) -> list[dict[str, Any]]:
        return [entry.spec.to_dict() for entry in self._operations.values()]

    def describe_operation(self, operation_id: str) -> dict[str, Any]:
        entry = self._require_entry(operation_id)
        return entry.spec.to_dict()

    def _require_entry(self, operation_id: str) -> _OperationEntry:
        entry = self._operations.get(operation_id)
        if entry is None:
            raise OperationError(f"unknown operation {operation_id!r}")
        return entry

    # ---- execution ----

    def run(
        self,
        operation_id: str,
        *,
        inputs: Mapping[str, Any],
        params: Mapping[str, Any],
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        entry = self._require_entry(operation_id)
        return _invoke_operation(entry.spec, entry.run, inputs, params, context or OperationContext())

    # ---- plugin loading (setup protocol) ----

    def load_plugins(
        self, roots: Sequence[str | Path] | None = None,
        *, validate_plugin: Callable[[Path], None] | None = None,
        activation_context: Callable[[Path], AbstractContextManager[None]] | None = None,
    ) -> list[dict[str, Any]]:
        """Load v2 manifest plugins from ``roots``; each setup() registers into *this* runtime.

        ``roots`` defaults to ``get_plugins_dir()``. Plugins go through the
        PluginManager (dependency resolution, ordered activation, blocked
        diagnostics). Registrations made by a plugin whose setup() raises, or
        whose operations differ from its manifest, are rolled back and the
        failure is recorded in the report (docs/plugin-protocol.md). Already
        activated plugins satisfy dependencies across calls and are not
        initialized again; replacing their code requires a new Runtime.
        A host may validate each newly discovered package before resolution;
        a validation error blocks the package and its dependent plugins before
        any of their Python code is imported.
        ``activation_context`` surrounds import, setup and registration checks
        only after resolution succeeds. It must propagate activation errors so
        failed registrations are rolled back and dependents remain blocked.
        """
        from ..plugins.lifecycle import PluginManager, pymss_package_version
        from ..plugins.loader import get_plugins_dir

        directories = [Path(root) for root in roots] if roots is not None else [get_plugins_dir()]
        manager = PluginManager(directories, active_plugins=self._active_plugins)
        states = manager.discover()
        if validate_plugin is not None:
            for state in states:
                if state.status == "discovered" and state.manifest is not None:
                    try:
                        validate_plugin(state.directory)
                    except Exception as error:
                        state.status, state.error = "blocked", str(error)
        manager.resolve(pymss_version=pymss_package_version())
        self._load_report = manager.activate(
            lambda source: _RuntimePluginHandle(self, source=source),
            activation_context=activation_context,
        )
        for state in states:
            if state.status == "activated" and state.manifest is not None:
                self._active_plugins[state.manifest.plugin_id] = state
        return self._load_report

    def plugin_report(self) -> list[dict[str, Any]]:
        return list(self._load_report)


@dataclass
class _RuntimePluginHandle:
    """setup(plugin) handle whose registrations target one runtime."""

    runtime: Runtime
    source: str

    def __init__(self, runtime: "Runtime", source: str):
        from ..plugins.registry import get_registry

        self.runtime = runtime
        self.source = source
        registry = get_registry()
        self._operations_before = dict(runtime._operations)
        self._capabilities_before = dict(registry.capabilities)
        self._nodes_before = dict(registry.nodes)
        self._cli_before = list(registry.cli_commands)
        self.registered_operations: list[str] = []
        self.registered_nodes: list[str] = []
        self._node_declarations = {}

    def configure_manifest(self, manifest) -> None:
        """Bind declarations before import/setup so graph handlers share validation."""
        self._node_declarations = {node.type: node for node in manifest.nodes}

    def run_operation(self, operation_id, *, inputs, params, context=None):
        """Run this plugin's Operation from a registered node using its Runtime.

        Nodes retain this handle in their executor closure so standalone and
        workflow calls share input/parameter validation and execution context.
        """
        if operation_id not in self.registered_operations:
            raise OperationError(f"operation {operation_id!r} is not registered by {self.source!r}")
        return self.runtime.run(operation_id, inputs=inputs, params=params, context=context)

    def validate_node_registrations(self) -> None:
        """Reject registrations outside the handle or whose owner was changed."""
        from ..plugins.manifest import ManifestError
        from ..plugins.registry import get_registry

        registry = get_registry()
        current = registry.nodes
        tracked = set(self.registered_nodes)
        for name, registration in current.items():
            if registration is self._nodes_before.get(name):
                continue
            # A plugin may lazily import core graph modules during setup.
            if registry.is_builtin_registration("node", name, registration) and name not in tracked:
                continue
            if name not in tracked or registration.source != self.source:
                raise ManifestError(f"node {name!r} must be registered through setup(plugin) by {self.source!r}")
        for name in tracked:
            if name not in current or current[name].source != self.source:
                raise ManifestError(f"node {name!r} is not registered by {self.source!r}")

    def register_operation(self, *, id, params, inputs=None, outputs=None, run=None, **kwargs):
        spec = OperationSpec(
            id=id,
            schema_version=int(kwargs.pop("schema_version", 1)),
            title=kwargs.pop("title", id),
            description=kwargs.pop("description", ""),
            inputs=inputs or {},
            params=params,
            outputs=outputs or {},
        )
        if run is None:
            raise OperationError(f"operation {id!r} requires a run handler")
        self.runtime.register(spec=spec, run=run, source=self.source)
        self.registered_operations.append(id)

    def rollback(self) -> None:
        """Restore registrations made or replaced during this plugin's activation."""
        from ..plugins.registry import get_registry

        self.runtime._operations.clear()
        self.runtime._operations.update(self._operations_before)
        registry = get_registry()
        for kind, current, before in (
            ("capability", registry.capabilities, self._capabilities_before),
            ("node", registry.nodes, self._nodes_before),
        ):
            # Importing graph modules may initialize builtins. Preserve those
            # registrations because Python retains the imported modules too.
            builtins = {
                name: entry for name, entry in current.items()
                if name not in before and registry.is_builtin_registration(kind, name, entry)
            }
            current.clear()
            current.update(builtins)
            current.update(before)
        builtin_commands = [
            entry for entry in registry.cli_commands
            if registry.is_builtin_registration("cli", id(entry), entry)
            and not any(entry is previous for previous in self._cli_before)
        ]
        registry.cli_commands[:] = [*self._cli_before, *builtin_commands]

    # Legacy compatibility surface: a runtime plugin may still contribute
    # capabilities/nodes/cli via the old global registry if it wants to.

    def register_capability(self, name, func, *, description=""):
        from pymss.plugins import register_capability

        register_capability(name, func, source=self.source, description=description)

    def register_node(self, node_type, func, *, description="", signature=None):
        from functools import wraps

        from pymss.plugins import register_node

        declaration = self._node_declarations.get(node_type)
        if declaration is not None and declaration.widgets:
            @wraps(func)
            def execute(ctx, inputs):
                from ..graph.core import DAGError, ctx_node_of

                node = ctx_node_of(ctx, node_type)
                values = node.data.get("widgets_values") if node is not None else None
                try:
                    declaration.validate_widgets(values)
                except ValueError as exc:
                    raise DAGError(str(exc)) from exc
                return func(ctx, inputs)
        else:
            execute = func
        register_node(node_type, execute, source=self.source, description=description, signature=signature)
        self.registered_nodes.append(node_type)

    def register_cli(self, path, func, *, help="", add_arguments=None):
        from pymss.plugins import register_cli

        register_cli(path, func, help=help, source=self.source, add_arguments=add_arguments)


def _invoke_operation(spec, run, inputs, params, context):
    """Shared execution boundary for standalone and graph-backed Operations."""
    context.check_cancelled()
    _check_inputs(spec, inputs)
    resolved = _resolve_input_values(spec, inputs)
    merged = _merge_and_validate_params(spec.id, spec.params, params)
    result = run(context, resolved, merged)
    if not isinstance(result, Mapping):
        raise OperationError(f"operation {spec.id!r} must return a mapping of named outputs")
    missing = set(spec.outputs) - set(result)
    if missing:
        raise OperationError(f"operation {spec.id!r} did not return declared outputs: {sorted(missing)}")
    return dict(result)


def _resolve_input_values(spec: OperationSpec, inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Coerce file descriptors into typed artifacts per the declared port type.

    The CLI passes ``{"kind": "file", "path": ...}`` descriptors; operations
    declared with an ``audio`` port expect an ``AudioArtifact``. Load at the
    boundary so every caller (CLI, bridge, tests) shares one conversion path.
    """
    resolved: dict[str, Any] = dict(inputs)
    for port_name, port_spec in spec.inputs.items():
        value = resolved.get(port_name)
        if not isinstance(value, Mapping) or value.get("kind") != "file":
            continue
        if not str(port_spec.get("type", "")).lower() == "audio":
            continue
        from ..graph.core import AudioArtifact
        from ..audio_io import load_audio

        path = str(value.get("path", ""))
        mix, sr, layout = load_audio(path, sr=None, mono=False, return_layout=True)
        resolved[port_name] = AudioArtifact(mix, int(sr), source_path=path, channel_layout=layout)
    return resolved


def _check_inputs(spec: OperationSpec, inputs: Mapping[str, Any]) -> None:
    for port_name in spec.inputs:
        if port_name not in inputs:
            raise OperationError(f"operation {spec.id!r} missing required input {port_name!r}")


def _merge_and_validate_params(
    operation_id: str,
    schema: Mapping[str, Any],
    params: Mapping[str, Any],
) -> dict[str, Any]:
    props = schema.get("properties", {})
    allow_additional = schema.get("additionalProperties", True)
    merged: dict[str, Any] = {}
    for pname, pschema in props.items():
        if pname in params:
            merged[pname] = params[pname]
        elif "default" in pschema:
            merged[pname] = pschema["default"]
        elif pname in schema.get("required", []):
            raise OperationError(f"operation {operation_id!r} missing required param {pname!r}")
    for pname in params:
        if pname not in props and not allow_additional:
            raise OperationError(
                f"operation {operation_id!r} received unknown param {pname!r}"
            )
    for pname, value in merged.items():
        pschema = props[pname]
        ptype = pschema["type"]
        if not _matches_type(ptype, value):
            raise OperationError(
                f"operation {operation_id!r} param {pname!r} must be {ptype}, got {type(value).__name__}"
            )
        if "minimum" in pschema and value < pschema["minimum"]:
            raise OperationError(
                f"operation {operation_id!r} param {pname!r} below minimum {pschema['minimum']}: {value}"
            )
        if "maximum" in pschema and value > pschema["maximum"]:
            raise OperationError(
                f"operation {operation_id!r} param {pname!r} above maximum {pschema['maximum']}: {value}"
            )
        if "enum" in pschema and value not in pschema["enum"]:
            raise OperationError(
                f"operation {operation_id!r} param {pname!r} must be one of {pschema['enum']}"
            )
    return merged


def create_runtime(*, load_plugins: bool = False) -> Runtime:
    """Create an isolated runtime; optionally load plugins immediately."""
    runtime = Runtime()
    if load_plugins:
        runtime.load_plugins()
    return runtime


_DEFAULT_RUNTIME: Runtime | None = None


def default_runtime() -> Runtime:
    """Process-lazy default runtime used by the legacy setup() bridge.

    Distinct from ``create_runtime()`` instances: hosts that want isolation
    create their own; ``PluginRegistration.register_operation`` (the global
    loader's setup handle) lands here. The first call also registers the
    built-in operations so every host gets them without extra wiring.
    """
    global _DEFAULT_RUNTIME
    if _DEFAULT_RUNTIME is None:
        _DEFAULT_RUNTIME = Runtime()
        from .builtins import register_builtin_operations

        register_builtin_operations()
    return _DEFAULT_RUNTIME
