"""Manifest-driven plugin lifecycle: discover → resolve → activate → deactivate.

Implements docs/plugin-protocol.md §4. Every plugin directory needs a v2
manifest; plugins get dependency resolution, ordered activation, a check that
``setup()`` registered exactly the declared operations and nodes, and rollback of their
own registrations on failure.
"""

from __future__ import annotations

import importlib
import importlib.util
from importlib.machinery import ModuleSpec
import re
import sys
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from .manifest import ManifestError, PluginManifest, parse_entry_point, read_manifest


def _remove_plugin_modules(namespace: str) -> None:
    for name in tuple(sys.modules):
        if name == namespace or name.startswith(namespace + "."):
            sys.modules.pop(name, None)


def _import_manifest_entry(directory: Path, module_name: str) -> tuple[ModuleType, str]:
    """Load plugin-local modules beneath a private package without changing sys.path."""
    namespace = f"_pymss_manifest_plugin_{uuid4().hex}"
    entry_file = directory / "__init__.py"
    if module_name != "__init__" and not all(part.isidentifier() for part in module_name.split(".")):
        raise ImportError(f"invalid plugin entry module {module_name!r}")
    try:
        if entry_file.is_file():
            spec = importlib.util.spec_from_file_location(
                namespace, entry_file, submodule_search_locations=[str(directory)],
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load plugin entry {entry_file}")
            root = importlib.util.module_from_spec(spec)
            sys.modules[namespace] = root
            spec.loader.exec_module(root)
        elif module_name == "__init__":
            raise ImportError(f"plugin entry {entry_file} does not exist")
        else:
            root = ModuleType(namespace)
            root.__path__ = [str(directory)]
            root.__package__ = namespace
            root.__spec__ = ModuleSpec(namespace, loader=None, is_package=True)
            root.__spec__.submodule_search_locations = root.__path__
            sys.modules[namespace] = root
        module = root if module_name == "__init__" else importlib.import_module(f"{namespace}.{module_name}")
        return module, namespace
    except BaseException:
        _remove_plugin_modules(namespace)
        raise

_CONSTRAINT_RE = re.compile(r"^(==|>=|<=|>|<|~=)?\s*(.+)$")


def _parse_version(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in re.findall(r"\d+", text.split("+", 1)[0]):
        parts.append(int(chunk))
    while len(parts) < 3:
        parts.append(0)
    while len(parts) > 3 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def satisfies(version: str, constraint: str) -> bool:
    """Small PEP 440 subset comparator (no pre-release algebra). Comma = AND."""
    return all(_satisfies_one(version, part) for part in constraint.split(",") if part.strip())


def _satisfies_one(version: str, constraint: str) -> bool:
    match = _CONSTRAINT_RE.match(constraint.strip())
    if not match:
        return False
    operator, raw = match.group(1) or "==", match.group(2).strip()
    have, want = _parse_version(version), _parse_version(raw)
    if operator == "==":
        # Normalize missing trailing components: 1.2 == 1.2.0
        length = max(len(have), len(want))
        have += (0,) * (length - len(have))
        want += (0,) * (length - len(want))
        return have == want
    if operator == ">=":
        return have >= want
    if operator == "<=":
        return have <= want
    if operator == ">":
        return have > want
    if operator == "<":
        return have < want
    if operator == "~=":
        release = re.match(r"^(\d+(?:\.\d+)+)", raw)
        if release is None:
            return False
        prefix = tuple(int(part) for part in release.group(1).split("."))[:-1]
        ceiling = prefix[:-1] + (prefix[-1] + 1,)
        ceiling += (0,) * max(0, 3 - len(ceiling))
        return want <= have < ceiling
    return False


@dataclass
class PluginState:
    directory: Path
    manifest: PluginManifest | None
    name: str
    status: str = "discovered"          # discovered|resolved|activated|blocked|failed
    error: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    contributions: dict[str, Any] = field(default_factory=dict)


class PluginManager:
    """Owns manifest plugins for one host (runtime/process)."""

    def __init__(
        self, roots: Path | Sequence[Path] | None = None,
        *, active_plugins: Mapping[str, PluginState] | None = None,
    ):
        from .loader import get_plugins_dir

        if roots is None:
            roots = [get_plugins_dir()]
        elif isinstance(roots, (str, Path)):
            roots = [roots]
        self.roots = [Path(root) for root in roots]
        self._states: dict[str, PluginState] = {}
        self._activated: list[str] = []
        self._active_plugins = dict(active_plugins or {})

    # ---- discover ----

    def discover(self) -> list[PluginState]:
        self._states.clear()
        active_directories = {
            str(state.directory.resolve()): state for state in self._active_plugins.values()
        }
        for root in self.roots:
            if not root.exists():
                continue
            for child in sorted(root.iterdir()):
                if not child.is_dir() or child.name.startswith((".", "_")):
                    continue
                key = str(child.resolve())
                if key in active_directories:
                    self._states[key] = active_directories[key]
                    continue
                try:
                    manifest = read_manifest(child)
                except ManifestError as exc:
                    self._states[key] = PluginState(
                        directory=child, manifest=None, name=child.name, status="blocked", error=str(exc),
                    )
                    continue
                self._states[key] = PluginState(directory=child, manifest=manifest, name=child.name)
        return list(self._states.values())

    # ---- resolve ----

    def resolve(self, *, pymss_version: str | None = None) -> list[PluginState]:
        """Resolve dependency graph; mark unsatisfiable plugins blocked."""
        by_id: dict[str, PluginState] = dict(self._active_plugins)
        duplicates: dict[str, list[PluginState]] = {}
        for state in self._states.values():
            if state.manifest is not None:
                plugin_id = state.manifest.plugin_id
                active = self._active_plugins.get(plugin_id)
                if active is not None:
                    if active is not state:
                        state.status = "blocked"
                        state.error = f"duplicate plugin id {plugin_id!r}: already activated from {active.directory}"
                    continue
                if plugin_id in by_id:
                    duplicates.setdefault(plugin_id, [by_id[plugin_id]]).append(state)
                else:
                    by_id[plugin_id] = state
        for plugin_id, states in duplicates.items():
            error = f"duplicate plugin id {plugin_id!r}: {', '.join(state.name for state in states)}"
            for state in states:
                state.status, state.error = "blocked", error

        def check(state: PluginState, seen: tuple[str, ...]) -> None:
            if state.status in {"activated", "resolved", "blocked"}:
                return
            manifest = state.manifest
            assert manifest is not None
            if manifest.plugin_id in seen:
                state.status, state.error = "blocked", f"dependency cycle: {' -> '.join(seen + (manifest.plugin_id,))}"
                return
            if manifest.requires_pymss and pymss_version and not satisfies(pymss_version, manifest.requires_pymss):
                state.status = "blocked"
                state.error = f"requires pymss {manifest.requires_pymss}, installed {pymss_version}"
                return
            for dep_id, constraint in manifest.dependencies.items():
                target = by_id.get(dep_id)
                if target is None or target.manifest is None or target.manifest.version is None:
                    state.blocked_by.append(dep_id)
                    state.status = "blocked"
                    state.error = f"missing dependency {dep_id} {constraint}"
                    continue
                check(target, seen + (manifest.plugin_id,))
                if target.status == "blocked":
                    state.blocked_by.append(dep_id)
                    state.status = "blocked"
                    state.error = f"dependency {dep_id} is not loadable ({target.error})"
                    continue
                if not satisfies(target.manifest.version, constraint):
                    state.blocked_by.append(dep_id)
                    state.status = "blocked"
                    state.error = (
                        f"dependency {dep_id} {constraint} unsatisfied by installed {target.manifest.version}"
                    )
            if state.status != "blocked":
                state.status = "resolved"

        for state in self._states.values():
            if state.manifest is not None:
                check(state, ())
        return list(self._states.values())

    # ---- activate ----

    def activation_order(self) -> list[PluginState]:
        """Resolved manifest plugins ordered dependencies-first (stable)."""
        states = [s for s in self._states.values() if s.manifest is not None and s.status == "resolved"]
        resolved: set[str] = set()
        ordered: list[PluginState] = []

        def visit(state: PluginState, trail: tuple[str, ...]) -> None:
            manifest = state.manifest
            assert manifest is not None
            if manifest.plugin_id in resolved or manifest.plugin_id in trail:
                return
            for dep_id in manifest.dependencies:
                target = next(
                    (s for s in states if s.manifest and s.manifest.plugin_id == dep_id), None
                )
                if target is not None:
                    visit(target, trail + (manifest.plugin_id,))
            resolved.add(manifest.plugin_id)
            ordered.append(state)

        for state in states:
            visit(state, ())
        return ordered

    def activate(
        self, registration_factory,
        *, activation_context: Callable[[Path], AbstractContextManager[None]] | None = None,
    ) -> list[dict[str, Any]]:
        """Activate resolved plugins in dependency order.

        ``registration_factory(source)`` returns the registration handle passed
        to the plugin's ``setup()``. A plugin whose setup fails is rolled back
        (registrations removed by the caller-provided ``rollback`` on the
        handle) and recorded as failed; dependents become blocked.
        The optional host context runs only for resolved plugins and surrounds
        their import and registration. It must propagate errors after cleanup;
        activation is committed only after the context exits successfully.
        """
        report: list[dict[str, Any]] = []
        ordered = self.activation_order()
        for state in ordered:
            manifest = state.manifest
            assert manifest is not None
            if state.status == "blocked":
                continue
            module_name, attr = parse_entry_point(manifest.entry)
            handle = None
            namespace = None
            try:
                context = activation_context(state.directory) if activation_context is not None else nullcontext()
                with context:
                    handle = registration_factory(f"plugin:{manifest.plugin_id}")
                    configure = getattr(handle, "configure_manifest", None)
                    if configure is not None:
                        configure(manifest)
                    module, namespace = _import_manifest_entry(state.directory, module_name)
                    setup = getattr(module, attr, None)
                    if setup is None:
                        raise AttributeError(f"entry {module_name}:{attr} has no {attr!r}")
                    setup(handle)
                    registered = set(getattr(handle, "registered_operations", ()))
                    if registered != manifest.operation_ids:
                        raise ManifestError(
                            "setup() operations do not match the manifest "
                            f"(missing {sorted(manifest.operation_ids - registered)}, "
                            f"undeclared {sorted(registered - manifest.operation_ids)})"
                        )
                    registered_nodes = set(getattr(handle, "registered_nodes", ()))
                    if registered_nodes != manifest.node_types:
                        raise ManifestError(
                            "setup() nodes do not match the manifest "
                            f"(missing {sorted(manifest.node_types - registered_nodes)}, "
                            f"undeclared {sorted(registered_nodes - manifest.node_types)})"
                        )
                    validate_nodes = getattr(handle, "validate_node_registrations", None)
                    if validate_nodes is not None:
                        validate_nodes()
                state.status = "activated"
                state.contributions = manifest.to_dict()["contributions"]
                self._activated.append(manifest.plugin_id)
            except Exception as exc:  # noqa: BLE001 — plugin code must not break the host
                state.status, state.error = "failed", f"{type(exc).__name__}: {exc}"
                # Roll back anything the failing setup() already registered so a
                # half-installed plugin leaves no orphaned operations behind.
                if handle is not None:
                    try:
                        handle.rollback()
                    except Exception:  # noqa: BLE001 — rollback must not mask the failure
                        pass
                if namespace is not None:
                    _remove_plugin_modules(namespace)
                self._block_transitive_dependents(manifest.plugin_id)
        # Rows are built after the whole pass so blocked/failed dependents
        # reflect their final state regardless of activation order.
        report.extend(self._row(state) for state in ordered)
        reported_paths = {row["path"] for row in report}
        for state in self._states.values():
            if str(state.directory) not in reported_paths:
                report.append(self._row(state))
        return report

    def _block_transitive_dependents(self, failed_plugin_id: str) -> None:
        """Mark every plugin that (transitively) depends on a failed plugin blocked."""
        changed = True
        failed_ids = {failed_plugin_id}
        while changed:
            changed = False
            for other in self._states.values():
                other_manifest = other.manifest
                if other_manifest is None or other.status in {"activated", "blocked", "failed"}:
                    continue
                deps = set(other_manifest.dependencies)
                failed_dependencies = deps & failed_ids
                if failed_dependencies:
                    other.status = "blocked"
                    other.error = f"dependency {failed_plugin_id} failed to activate"
                    other.blocked_by.extend(sorted(failed_dependencies))
                    failed_ids.add(other_manifest.plugin_id)
                    changed = True

    # ---- report ----

    def _row(self, state: PluginState) -> dict[str, Any]:
        manifest = state.manifest
        if manifest is None:
            return {"name": state.name, "path": str(state.directory), "loaded": False, "error": state.error}
        row = {
            "name": state.name,
            "path": str(state.directory),
            "loaded": state.status == "activated",
            "error": state.error,
            **manifest.to_dict(),
            "status": state.status,
            "blocked_by": state.blocked_by,
        }
        row.pop("contributions", None) if state.status != "activated" else None
        return row

    def report(self) -> list[dict[str, Any]]:
        return [self._row(state) for state in self._states.values()]


def pymss_package_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("pymss")
    except Exception:  # noqa: BLE001 — version probe must never break loading
        return None
