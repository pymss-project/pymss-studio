"""Plugin system: capability registry and node/CLI registration API.

Design reference: docs/plugins.md (when ported to this branch).

Two decoupled concepts:
- Capability: a named function registered into a global pool. Does not know
  who calls it.
- Consumer: a workflow node or CLI command that looks up a capability by name.

Providers and consumers can ship in different packages, coupled only by the
capability name.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from .._builtin_node_types import BUILTIN_NODE_TYPES

class CapabilityNotFound(KeyError):
    """Raised when a required capability is not registered.

    Consumers (nodes / CLI commands) catch this to give a more specific hint,
    e.g. "SaveAudioOpus needs the opus_encode capability; run `pymss install
    opus`".
    """

    def __init__(self, name: str):
        self.name = name
        super().__init__(
            f"capability '{name}' is not registered. Install a plugin that "
            f"provides it (e.g. `pymss install <name>`), or register it via "
            f"pymss.plugins.register_capability."
        )


@dataclass
class Capability:
    """A registered capability: a name + a callable + optional metadata."""

    name: str
    func: Callable[..., Any]
    source: str = "builtin"  # which plugin/package provided it
    description: str = ""


@dataclass
class NodeRegistration:
    """A registered workflow node executor.

    For graph/workflow nodes, `signature` is a factory that takes a DAGNode and
    returns a NodeSignature (inputs/outputs/params). It is optional — simple
    plugin nodes may omit it.
    """

    node_type: str
    func: Callable[..., Any]
    source: str = "builtin"  # which plugin/package provided it
    description: str = ""
    signature: Callable[..., Any] | None = None  # (dag_node) -> NodeSignature


@dataclass
class CLIRegistration:
    """A registered CLI subcommand path, e.g. ("opus", "encode")."""

    path: tuple[str, ...]
    func: Callable[..., Any]
    help: str = ""
    source: str = "builtin"
    add_arguments: Callable[[Any], None] | None = None  # optional argparse hook


@dataclass
class PluginRegistry:
    """Global registry for capabilities, nodes, and CLI commands.

    A single shared instance is exposed as `_REGISTRY`. pymss core registers
    built-in capabilities at startup; plugins add more via the public API.
    """

    capabilities: dict[str, Capability] = field(default_factory=dict)
    nodes: dict[str, NodeRegistration] = field(default_factory=dict)
    cli_commands: list[CLIRegistration] = field(default_factory=list)

    # Names that plugins must never override (pymss / comfy-mss built-ins).
    # All registrations also reject a different owner for an existing name.
    _reserved_node_types: set[str] = field(default_factory=lambda: set(BUILTIN_NODE_TYPES))
    _builtin_registrations: dict[tuple[str, object], object] = field(default_factory=dict, repr=False)
    _builtin_registration_depth: int = field(default=0, repr=False)

    @contextmanager
    def _builtin_registration(self):
        """Record entry identities created by a trusted core initialization path.

        ``source`` labels are public metadata, so lifecycle rollback must not
        treat the string ``builtin`` as proof that an entry belongs to core.
        Partial core imports remain registered when initialization raises.
        """
        before = self._registration_entries()
        self._builtin_registration_depth += 1
        try:
            yield
        finally:
            for key, entry in self._registration_entries().items():
                if entry is not before.get(key) and entry.source in {"builtin", "pymss.builtin"}:
                    self._builtin_registrations[key] = entry
            self._builtin_registration_depth -= 1

    def _registration_entries(self) -> dict[tuple[str, object], object]:
        return {
            **{("capability", name): entry for name, entry in self.capabilities.items()},
            **{("node", name): entry for name, entry in self.nodes.items()},
            **{("cli", id(entry)): entry for entry in self.cli_commands},
        }

    def is_builtin_registration(self, kind: str, name: object, entry: object) -> bool:
        """Check object identity, never an owner label supplied by plugin code."""
        return self._builtin_registrations.get((kind, name)) is entry

    # ---- capability API ----
    def register_capability(
        self,
        name: str,
        func: Callable[..., Any],
        *,
        source: str = "builtin",
        description: str = "",
    ) -> None:
        previous = self.capabilities.get(name)
        if previous is not None and (
            previous.source != source or (
                self.is_builtin_registration("capability", name, previous)
                and not self._builtin_registration_depth
            )
        ):
            raise ValueError(
                f"capability {name!r} is already registered by {previous.source!r}; "
                f"cannot replace it with {source!r}"
            )
        self.capabilities[name] = Capability(
            name=name, func=func, source=source, description=description
        )

    def get_capability(self, name: str) -> Callable[..., Any]:
        try:
            return self.capabilities[name].func
        except KeyError as exc:
            raise CapabilityNotFound(name) from exc

    def has_capability(self, name: str) -> bool:
        return name in self.capabilities

    # ---- node API ----
    def reserve_node_type(self, node_type: str) -> None:
        """Mark a node type as a pymss/comfy-mss built-in that plugins cannot override."""
        self._reserved_node_types.add(node_type)

    def register_node(
        self,
        node_type: str,
        func: Callable[..., Any],
        *,
        source: str = "builtin",
        description: str = "",
        allow_override_builtin: bool = False,
        signature: Callable[..., Any] | None = None,
    ) -> None:
        builtin_source = source in {"builtin", "pymss.builtin"}
        # The legacy override flag remains accepted, but an owner label/flag
        # alone must not grant access to names reserved for core initialization.
        builtin_initialization = builtin_source and self._builtin_registration_depth
        if node_type in self._reserved_node_types and not builtin_initialization:
            raise ValueError(
                f"node type '{node_type}' is a reserved pymss built-in and "
                f"cannot be registered by plugins."
            )
        previous = self.nodes.get(node_type)
        if previous is not None and (
            previous.source != source or (
                self.is_builtin_registration("node", node_type, previous)
                and not self._builtin_registration_depth
            )
        ):
            raise ValueError(
                f"node type {node_type!r} is already registered by {previous.source!r}; "
                f"cannot replace it with {source!r}"
            )
        self.nodes[node_type] = NodeRegistration(
            node_type=node_type, func=func, source=source, description=description,
            signature=signature,
        )

    def get_node(self, node_type: str) -> Callable[..., Any] | None:
        reg = self.nodes.get(node_type)
        return reg.func if reg else None

    # ---- CLI API ----
    def register_cli(
        self,
        path: str | tuple[str, ...],
        func: Callable[..., Any],
        *,
        help: str = "",
        source: str = "builtin",
        add_arguments: Callable[[Any], None] | None = None,
    ) -> None:
        if isinstance(path, str):
            path_tuple = tuple(path.split())
        else:
            path_tuple = tuple(path)
        if not path_tuple:
            raise ValueError("CLI command path must be non-empty")
        self.cli_commands.append(
            CLIRegistration(
                path=path_tuple,
                func=func,
                help=help,
                source=source,
                add_arguments=add_arguments,
            )
        )


# Single shared registry instance.
_REGISTRY: PluginRegistry = PluginRegistry()


@dataclass(frozen=True)
class PluginRegistration:
    """Registration handle passed to a plugin's optional ``setup(plugin)``.

    New-style plugins contribute through this object instead of importing
    registration functions directly, so every registration is tagged with the
    plugin's identity. The methods mirror the module-level API.
    """

    source: str

    def register_operation(
        self,
        *,
        id: str,
        params: dict,
        run: Callable[..., Any],
        inputs: dict | None = None,
        outputs: dict | None = None,
        schema_version: int = 1,
        title: str | None = None,
        description: str = "",
    ) -> None:
        """Register an Operation into the global default runtime.

        Convenience for plugins that only contribute operations; hosts with
        their own Runtime instance call ``Runtime.register`` directly.
        """
        from ..operations import OperationSpec, default_runtime

        default_runtime().register(
            spec=OperationSpec(
                id=id,
                schema_version=schema_version,
                title=title or id,
                description=description,
                inputs=inputs or {},
                params=params,
                outputs=outputs or {},
            ),
            run=run,
            source=self.source,
        )

    def register_capability(
        self,
        name: str,
        func: Callable[..., Any],
        *,
        description: str = "",
    ) -> None:
        _REGISTRY.register_capability(
            name, func, source=self.source, description=description
        )

    def register_node(
        self,
        node_type: str,
        func: Callable[..., Any],
        *,
        description: str = "",
        signature: Callable[..., Any] | None = None,
    ) -> None:
        _REGISTRY.register_node(
            node_type, func, source=self.source, description=description,
            signature=signature,
        )

    def register_cli(
        self,
        path: str | tuple[str, ...],
        func: Callable[..., Any],
        *,
        help: str = "",
        add_arguments: Callable[[Any], None] | None = None,
    ) -> None:
        _REGISTRY.register_cli(
            path, func, help=help, source=self.source, add_arguments=add_arguments
        )


# ---------------------------------------------------------------------------
# Public registration API (used by plugins via `from pymss.plugins import ...`)
# ---------------------------------------------------------------------------


def register_capability(
    name: str,
    func: Callable[..., Any] | None = None,
    *,
    source: str = "plugin",
    description: str = "",
):
    """Register a named capability, or use as a decorator.

    Usage (direct):
        register_capability("opus_encode", opus_encode_fn, source="my-plugin")

    Usage (decorator):
        @register_capability("eq")
        def eq(audio, sample_rate, low_gain=0, mid_gain=0, high_gain=0):
            ...
    """
    if func is None:
        def decorator(f: Callable[..., Any]):
            _REGISTRY.register_capability(name, f, source=source, description=description)
            return f
        return decorator
    _REGISTRY.register_capability(name, func, source=source, description=description)
    return func


def register_node(
    node_type: str,
    func: Callable[..., Any] | None = None,
    *,
    source: str = "plugin",
    description: str = "",
    signature: Callable[..., Any] | None = None,
):
    """Register a workflow node executor. See register_capability for decorator use.

    signature: optional factory (dag_node) -> NodeSignature for graph nodes.
    """
    if func is None:
        def decorator(f: Callable[..., Any]):
            _REGISTRY.register_node(
                node_type, f, source=source, description=description,
                signature=signature,
            )
            return f
        return decorator
    _REGISTRY.register_node(
        node_type, func, source=source, description=description,
        signature=signature,
    )
    return func


def register_cli(
    path: str | tuple[str, ...],
    func: Callable[..., Any] | None = None,
    *,
    help: str = "",
    source: str = "plugin",
    add_arguments: Callable[[Any], None] | None = None,
):
    """Register a CLI subcommand under `pymss <path[0]> <path[1]> ...`.

    path: e.g. "opus encode" or ("opus", "encode"). The first element is the
    plugin's namespace; it must not collide with official pymss commands.
    """
    if func is None:
        def decorator(f: Callable[..., Any]):
            _REGISTRY.register_cli(
                path, f, help=help, source=source, add_arguments=add_arguments
            )
            return f
        return decorator
    _REGISTRY.register_cli(path, func, help=help, source=source, add_arguments=add_arguments)
    return func


def require_capability(name: str) -> Callable[..., Any]:
    """Look up a capability by name; raise CapabilityNotFound if missing.

    Intended for use inside node executors and CLI command handlers."""
    return _REGISTRY.get_capability(name)


def get_registry() -> PluginRegistry:
    """Return the shared plugin registry (for introspection / pymss internals)."""
    return _REGISTRY


__all__ = [
    "CapabilityNotFound",
    "PluginRegistry",
    "register_capability",
    "register_node",
    "register_cli",
    "require_capability",
    "get_registry",
    "_REGISTRY",
]
