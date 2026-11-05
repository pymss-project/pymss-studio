"""Studio adapter for shared pymss plugin management.

The shared backend owns acquisition, installation transactions and publication.
Studio supplies environment selection, dependency constraints, verification and UI metadata.
Every plugin is a pymss v2 manifest plugin installed as ``installed/<plugin id>``;
listing reads manifests only and never imports plugin code. Plugins in the pymss
default directory (~/.pymss/plugins) are shown read-only.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import sys
import tomllib
from contextlib import contextmanager
from contextvars import ContextVar
from threading import RLock
from pathlib import Path
from typing import Any, Iterator

from worker_protocol import emit, emit_error, isolate_protocol_stdout
from worker_plugin_contract import (
    PluginCompatibilityError, catalog_requirements, compatibility, read_requirements,
    require_compatible, runtime_profile,
)

# Match pymss._identifiers without importing the core during inventory reads.
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)*\Z")
_PROBE_MARKER = "__STUDIO_PLUGIN_PROBE__"
_BUNDLED_ROOT = Path(__file__).parent / "preinstalled_plugins"
_PLUGIN_MANIFEST = Path(__file__).parent / "plugin-manifest.json"
_LOCAL_SOURCE: ContextVar[Path | None] = ContextVar("plugin_local_source", default=None)
_REGISTRY_URL: ContextVar[str | None] = ContextVar("plugin_registry_url", default=None)
_CATEGORIES = {"convert", "analyze", "recognize", "edit"}
_RUNTIME_LOCK = RLock()
_DEPENDENCY_HANDOFF = ".studio-deps-pending"
_SOURCE_IGNORED_DIRS = {".studio-deps", ".pymss-python.json", "__pycache__", ".git", "node_modules", ".venv", "venv", ".tmp", ".cache", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


class PluginRuntimeIncompatible(RuntimeError):
    """The active core does not provide the plugin interfaces this worker needs."""


def _manager_engine():
    try:
        from pymss_plugin_manager import API_VERSION, _engine
        if API_VERSION != 1:
            raise ImportError("Unsupported plugin management API")
        return _engine
    except ImportError as error:
        raise PluginRuntimeIncompatible(
            "Update the selected pymss core to use shared plugin management."
        ) from error


def _management_supported() -> bool:
    try:
        from pymss_plugin_manager import API_VERSION
        return API_VERSION == 1
    except ImportError:
        return False


def _readonly_inventory(root: Path) -> dict:
    """Observe an old store without loading code, repairing it, or writing state."""
    warning = "Update the selected pymss core to manage these plugins. The current store is read-only."
    rows = []
    for enabled, directory in ((True, root / "installed"), (False, root / "disabled")):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_dir() or path.is_symlink() or path.name.startswith("."):
                continue
            try:
                data = tomllib.loads((path / "pyproject.toml").read_text(encoding="utf-8"))
            except (OSError, UnicodeError, tomllib.TOMLDecodeError):
                continue
            match data.get("tool"):
                case {"pymss": {"plugin": dict() as manifest}}:
                    pass
                case _:
                    continue
            project = data.get("project", {})
            operations = manifest.get("operations", [])
            if not isinstance(project, dict) or not isinstance(operations, list):
                continue
            name, version = manifest.get("id"), project.get("version")
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                continue
            if version is not None and not isinstance(version, str):
                continue
            if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in operations):
                continue
            rows.append({"name": name, "pluginId": name, "version": version,
                         "source": "recorded", "url": None, "path": str(path), "managed": False,
                         "loaded": None, "enabled": enabled, "prepared": True, "error": None,
                         "operations": [item["id"] for item in operations],
                         "contributions": {"views": []}})
    return {"installed": rows, "pluginsDir": str(root / "installed"), "legacyPlugins": [],
            "inventoryWarnings": [warning], "preinstalled": [], "preinstalledPending": [],
            "preinstalledAvailable": [], "verificationError": None,
            "runtime": {"identity": root.name, "pythonPath": sys.executable,
                        "bindingDir": str(root), "managementSupported": False}}


def _host_constraints(plugin: Path) -> list[str]:
    from pip._vendor.packaging.requirements import Requirement
    from worker_optional_packages import _package_spec
    requirement = _package_spec("funasr")[1]["requirement"]
    name = _canonical(Requirement(requirement).name)
    for text in _plugin_requirements(plugin):
        value = Requirement(text)
        if _canonical(value.name) == name and (not value.marker or value.marker.evaluate({"extra": ""})):
            return [requirement + "\n"]
    return []


def _host_index_args(payload: dict) -> list[str]:
    from worker_bootstrap import _resolve_pypi_mirror
    _, index = _resolve_pypi_mirror(str(payload.get("mirror") or "auto"), str(payload.get("locale") or ""))
    return ["--index-url", index] if index else []


def _core_environment_compatible(plugin: Path) -> None:
    # Legacy platform/backend declarations describe actual backend constraints.
    # GUI SDK and capabilities govern views, not whether the core can load code.
    metadata = _studio_metadata(plugin)
    require_compatible({key: metadata[key] for key in ("platforms", "architectures", "backends") if key in metadata})


def _primary_operation(plugin: Path) -> str | None:
    views = _studio_metadata(plugin).get("views", [])
    if not isinstance(views, list):
        return None
    return next((view.get("operation") for view in views if isinstance(view, dict) and view.get("id") == "tool"), None)


def _component_paths(plugin: Path) -> list[Path]:
    from worker_optional_packages import optional_package_paths
    return optional_package_paths(_runtime_packages(_studio_metadata(plugin)))


def _service(root: Path | None = None):
    _manager_engine()
    from pymss_plugin_manager import PluginContext, PluginPolicy, PluginService
    return PluginService(PluginContext(
        root or _root(), registry_url=_REGISTRY_URL.get(), local_source=_LOCAL_SOURCE.get(),
        bundled_root=_BUNDLED_ROOT,
    ), PluginPolicy(
        event=lambda event, payload, task_id: emit(event, payload, task_id=task_id),
        validate=_core_environment_compatible, probe=_probe,
        constraints=_host_constraints, index_args=_host_index_args,
        deferred_operations=_deferred_dependency_operations, primary_operation=_primary_operation,
        validate_requirements=_validate_requirements, component_paths=_component_paths,
    ))


@contextmanager
def _management(root: Path | None = None):
    with _service(root).bound():
        yield


def _require_plugin_runtime() -> None:
    _manager_engine()
    try:
        import inspect
        from pymss.operations import Runtime
        from pymss.plugins import kit
        from pymss.plugins.manifest import API_VERSION
        from pymss.plugins.sources import resolve_github_commit

        if API_VERSION < 2 or not callable(getattr(kit, "render_output", None)) or not callable(resolve_github_commit):
            raise ValueError("Plugin protocol v2 is unavailable")
        inspect.signature(Runtime.load_plugins).bind(None, [], validate_plugin=None, activation_context=None)
    except (ImportError, AttributeError, TypeError, ValueError):
        try:
            version = importlib.metadata.version("pymss")
        except importlib.metadata.PackageNotFoundError:
            version = "not installed"
        raise PluginRuntimeIncompatible(
            "The selected runtime does not provide the required plugin APIs. "
            "Update its pymss core or select a compatible development runtime in Diagnostics. "
            f"Current pymss: {version}; interpreter: {sys.executable}"
        ) from None


def _name(value: Any):
    with _management(None):
        return _manager_engine()._name(value)


def _root() -> Path:
    from worker_optional_packages import _runtime_context

    runtime = _runtime_context()
    # Preserve the venv path: resolving its python symlink would merge distinct venvs.
    digest = hashlib.sha256(os.path.normcase(os.path.abspath(sys.executable)).encode()).hexdigest()[:12]
    return runtime.packages_dir.parent.parent / "plugins" / f"{runtime.identity}-{digest}"


def register_cli_environment() -> Path | None:
    """Associate a verified Studio interpreter with its CLI defaults."""
    if not any(os.environ.get(name) for name in (
        "PYMSS_STUDIO_RUNTIME_ENVS_DIR", "PYMSS_STUDIO_ACTIVE_RUNTIME_FILE",
        "PYMSS_STUDIO_BUNDLED_RUNTIME_ENVS_DIR",
    )):
        return None
    from worker_bootstrap import _read_runtime_state

    state = _read_runtime_state()
    if not state or not state.get("pythonPath"):
        return None
    try:
        from pymss_plugin_manager.environment import python_identity, register_environment_binding
    except ModuleNotFoundError as error:
        if error.name in {"pymss_plugin_manager", "pymss_plugin_manager.environment"}:
            return None
        raise
    if python_identity(state["pythonPath"]) != python_identity(sys.executable):
        return None
    import pymss_plugin_manager

    core = Path(pymss_plugin_manager.__file__).resolve().parent.parent
    binding = _root()
    from worker_optional_packages import OPTIONAL_PACKAGES, optional_package_paths

    register_environment_binding(binding, core_path=core,
                                 import_paths=tuple(optional_package_paths(OPTIONAL_PACKAGES)))
    return binding


@contextmanager
def _plugin_registry() -> Iterator[None]:
    """Select Studio's configured registry for pymss calls unless the user overrides it."""
    override = os.environ.get("PYMSS_PLUGINS_REGISTRY")
    source_token, url_token = _LOCAL_SOURCE.set(None), _REGISTRY_URL.set(override or None)
    try:
        if not override:
            from worker_plugin_config import load_plugin_config, plugin_registry_url, plugin_source
            source = plugin_source(_PLUGIN_MANIFEST)
            if source["mode"] == "local":
                root = Path(source["localPath"])
                _REGISTRY_URL.set((root / "registry.json").as_uri())
                _LOCAL_SOURCE.set(root)
            else:
                config = load_plugin_config(_PLUGIN_MANIFEST)
                config["repository"] = {"url": source["repositoryUrl"], "ref": source["ref"], "registryRef": source["registryRef"]}
                _REGISTRY_URL.set(plugin_registry_url(config))
        yield
    finally:
        _LOCAL_SOURCE.reset(source_token)
        _REGISTRY_URL.reset(url_token)


def _read_manifest(plugin: Path):
    with _management(None):
        return _manager_engine()._read_manifest(plugin)


def _local_package(root: Path, name: str | None, subpath: object):
    with _management(root):
        return _manager_engine()._local_package(root, name, subpath)


def _local_catalog(root: Path) -> list[dict]:
    registry = root / "registry.json"
    if registry.is_symlink() or not registry.is_file():
        raise ValueError("Local plugin registry is unavailable or is a symbolic link")
    data = json.loads(registry.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Local plugin registry must be an object")
    rows = []
    for name, entry in data.items():
        if name.startswith("_"):
            continue
        _name(name)
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid local plugin registry entry: {name}")
        if not isinstance(entry.get("tags", []), list):
            raise ValueError(f"Invalid local plugin registry tags: {name}")
        package = _local_package(root, name, entry.get("subpath"))
        rows.append({"name": name, "url": str(root), "subpath": entry["subpath"],
                     "requirements": read_requirements(_studio_metadata(package)),
                     "description": str(entry.get("description") or ""),
                     **_presentation(entry),
                     "tags": [str(tag) for tag in entry.get("tags", [])]})
    return rows


def _uses_default_source(meta: dict):
    with _management(None):
        return _manager_engine()._uses_default_source(meta)


def _migrate_v1(root: Path):
    with _management(root):
        return _manager_engine()._migrate_v1(root)


def _legacy_plugins(root: Path):
    with _management(root):
        return _manager_engine()._legacy_plugins(root)


def _recover(root: Path):
    with _management(root):
        return _manager_engine()._recover(root)


def _complete_recovered_uninstalls(root: Path):
    with _management(root):
        return _manager_engine()._complete_recovered_uninstalls(root)


def _recover_for_read(root: Path):
    with _management(root):
        return _manager_engine()._recover_for_read(root)


def _installed(root: Path, *, disabled: bool=False):
    with _management(root):
        return _manager_engine()._installed(root, disabled=disabled)


def _location(root: Path, name: str):
    with _management(root):
        return _manager_engine()._location(root, name)


def _bundled():
    with _management(None):
        return _manager_engine()._bundled()


def _removed_preinstalled(root: Path):
    with _management(root):
        return _manager_engine()._removed_preinstalled(root)


def _mark_preinstalled_removed(root: Path, name: str, removed: bool):
    with _management(root):
        return _manager_engine()._mark_preinstalled_removed(root, name, removed)


def _intent_definition(manifest: dict):
    with _management(None):
        return _manager_engine()._intent_definition(manifest)


def _application_bindings(root: Path):
    with _management(root):
        return _manager_engine()._application_bindings(root)


def _application_state(root: Path):
    with _management(root):
        return _manager_engine()._application_state(root)


def _execution_intents(root: Path):
    with _management(root):
        return _manager_engine()._execution_intents(root)


def _binding_sources(observations: list[dict], name: str):
    with _management(None):
        return _manager_engine()._binding_sources(observations, name)


def _binding_matches(package: Path, intent: dict):
    with _management(None):
        return _manager_engine()._binding_matches(package, intent)


def _has_current_installation(root: Path, name: str):
    with _management(root):
        return _manager_engine()._has_current_installation(root, name)


def _unprepared_row(root: Path, name: str, entry: dict, observations: list[dict], profile: dict) -> dict:
    definition = entry.get("definition", {})
    metadata = definition.get("studio", {})
    presentation = _presentation(metadata)
    sources = _binding_sources(observations, name)
    versions = {(item.get("version"), item.get("content")) for item in sources}
    selection_required = entry.get("sourceSelectionRequired", False) or (entry.get("revision", 0) == 0 and len(versions) > 1)
    eligible = sources if entry.get("revision", 0) == 0 or selection_required else [
        item for item in sources if (entry.get("version") is None or item.get("version") == entry["version"])
        and (entry.get("content") is None or item.get("content") == entry["content"])
    ]
    core_requirements = read_requirements({key: metadata[key] for key in ("platforms", "architectures", "backends") if key in metadata})
    requirements, view_error = None, None
    views = []
    try:
        requirements = read_requirements(metadata)
        status = compatibility(requirements, profile)
        if not status["compatible"]:
            raise ValueError(" ".join(status["reasons"]))
        for view in _view_declarations(metadata):
            views.append({**view, "title": str(view.get("title") or view["id"]), "permissions": view.get("permissions", []),
                          "sdkVersion": requirements["sdkVersion"], "requiredCapabilities": requirements["requiredCapabilities"],
                          "category": view.get("category") or presentation["category"], "pluginId": name})
    except ValueError as error:
        view_error = str(error)
    preferred = next((item for item in sources if item["binding"] == entry.get("binding")), sources[0] if sources else None)
    meta = preferred["meta"] if preferred else {}
    operations = definition.get("operations", [])
    return {
        "name": name, "pluginId": name, "version": entry.get("version"), "source": meta.get("source", "recorded"),
        "url": meta.get("url"), "path": "", "managed": True, "enabled": entry["state"] == "enabled", "loaded": False,
        "sourceRef": meta.get("ref"), "sourceSubpath": meta.get("subpath"), "contentDigest": entry.get("content"),
        "installedAt": meta.get("installed_at"), "resolvedCommit": meta.get("resolved_commit"),
        "prepared": False, "availability": "not_prepared", "error": None, "dependencyError": None,
        "requirements": requirements, "compatibility": compatibility(core_requirements, profile),
        "description": definition.get("description", ""), "locales": presentation["locales"],
        "toolCategory": presentation["category"], "toolIcon": metadata.get("icon"),
        "operations": [item["id"] for item in operations], "operationTitles": {item["id"]: item["title"] for item in operations},
        "contributions": {"views": views}, "viewError": view_error, "sourceSelectionRequired": selection_required,
        "availableBindings": [{"runtime": item["binding"], "version": item.get("version"), "content": item.get("content")} for item in eligible],
    }


def _prepare_binding(root: Path, payload: dict, task_id: str | None, revision: int):
    with _management(root):
        return _manager_engine()._prepare_binding(root, payload, task_id, revision)


def _record_intent(root: Path, name: str, state: str, *, pending: str | None=None, revision: int | None=None):
    with _management(root):
        return _manager_engine()._record_intent(root, name, state, pending=pending, revision=revision)


def _prepare_preinstalled(root: Path, task_id: str | None, requested: str | None=None, *, intent_revision: int | None=None):
    with _management(root):
        return _manager_engine()._prepare_preinstalled(root, task_id, requested, intent_revision=intent_revision)


def _set_enabled(root: Path, name: str, enabled: bool, *, verify: bool=True):
    with _management(root):
        return _manager_engine()._set_enabled(root, name, enabled, verify=verify)


def _enable_binding(root: Path, name: str, revision: int | None):
    with _management(root):
        return _manager_engine()._enable_binding(root, name, revision)


def activate_plugin_dependencies(root: Path | None = None) -> None:
    root = root or _root()
    intents = _execution_intents(root)
    if intents is None:
        return
    for plugin in _installed(root):
        intent = intents.get(plugin.name, {})
        if intent.get("state", "enabled") != "enabled" or not _binding_matches(plugin, intent):
            continue
        deps = plugin / ".studio-deps"
        if deps.is_dir() and str(deps) not in sys.path:
            # The host wins if a later core update adds a formerly missing package.
            sys.path.append(str(deps))
    importlib.invalidate_caches()


def operation_runtime():
    """Load plugins from the active runtime binding only."""
    # Batch workflows call this from worker threads. stdout redirection is
    # process-global, and plugin activation mutates the shared runtime.
    with _RUNTIME_LOCK, isolate_protocol_stdout():
        return _operation_runtime()


def _load_runtime_plugins(runtime, root: Path) -> list[dict[str, Any]]:
    from worker_optional_packages import optional_package_activation

    def activate(plugin: Path):
        return optional_package_activation(_runtime_packages(_studio_metadata(plugin)))

    return _service(root).load_into(runtime, activation_context=activate)


def _operation_runtime():
    _require_plugin_runtime()
    from pymss.operations import default_runtime
    runtime = default_runtime()
    root = _root()
    if root.exists():
        _recover_for_read(root)
        # Rust excludes writers for the lifetime of this worker. Do not hold an
        # exclusive file lock during imports: other operation readers may run too.
        activate_plugin_dependencies(root)
    previous_root = getattr(runtime, "_studio_root", None)
    if previous_root is not None and previous_root != str(root):
        raise ValueError("The plugin context changed; start a new worker for the selected runtime")
    if previous_root != str(root):
        runtime._studio_reports = _load_runtime_plugins(runtime, root)
        runtime._studio_root = str(root)
    return runtime


def _operation_ids(row: dict[str, Any]) -> list[str]:
    return [operation["id"] for operation in row.get("operations") or [] if isinstance(operation, dict)]


def plugin_reports(runtime, installed_dir: Path | None = None) -> list[dict[str, Any]]:
    from pymss.plugins import get_last_report

    report = get_last_report()
    manual = [
        {"name": item.plugin_id or item.name, "loaded": item.loaded, "error": item.error,
         "source": item.source, "path": item.path, "managed": False}
        for item in (report.results if report else [])
    ]
    managed = []
    manual_names = {item["name"] for item in manual}
    operation_titles = {spec["id"]: spec.get("title") or spec["id"] for spec in runtime.list_operations()}
    for item in getattr(runtime, "_studio_reports", None) or runtime.plugin_report():
        operations = _operation_ids(item)
        item = {**item, "name": item.get("plugin_id") or item["name"], "operations": operations,
                "error": _describe_error(item.get("error"), manual_names), "managed": True,
                "operationTitles": {operation: operation_titles.get(operation, operation) for operation in operations}}
        if item.get("loaded"):
            directory = (installed_dir or _root() / "installed") / item["name"]
            try:
                _validate_requirements(directory)
            except Exception as error:
                if set(item.get("operations", [])) & _deferred_dependency_operations(directory):
                    item["dependencyError"] = str(error)
                else:
                    item.update(loaded=False, error=str(error))
        managed.append(item)
    return manual + managed


def _probe(root: Path, dependency_paths: list[Path] | None = None) -> Any:
    """Validate an explicit candidate through the core's bounded probe."""
    _require_plugin_runtime()
    from pymss_plugin_manager.service import probe
    return probe(root, dependency_paths, component_paths=_component_paths,
                 deferred_operations=_deferred_dependency_operations)


def _manifest(plugin: Path):
    with _management(None):
        return _manager_engine()._manifest(plugin)


def _describe_error(error: str | None, manual_names: set[str] | None = None) -> str | None:
    if not error:
        return None
    return re.sub(
        r"already registered by [\'\"]plugin:([^\'\"]+)[\'\"]",
        lambda match: "already provided by " + (
            "pymss directory plugin " if match[1] in (manual_names or set()) else "marketplace plugin "
        ) + match[1],
        error,
    )


def _studio_metadata(plugin: Path) -> dict[str, Any]:
    path = plugin / "pyproject.toml"
    if not path.is_file():
        return {}
    project = tomllib.loads(path.read_text(encoding="utf-8"))
    value = project.get("tool", {}).get("pymss", {}).get("studio", {})
    return {"description": project.get("project", {}).get("description", ""), **value} if isinstance(value, dict) else {}


def _view_declarations(metadata: dict) -> list[dict]:
    """Validate view metadata for both prepared and recorded installations."""
    declarations = metadata.get("views", [])
    if not isinstance(declarations, list):
        raise ValueError("Studio views must be an array of tables")
    seen = set()
    for view in declarations:
        if not isinstance(view, dict):
            raise ValueError("Each Studio view must be a table")
        view_id = view.get("id")
        if not isinstance(view_id, str) or not _NAME.fullmatch(view_id) or view_id in seen:
            raise ValueError(f"Invalid or duplicate Studio view id: {view_id!r}")
        seen.add(view_id)
        entry = view.get("entry")
        if not isinstance(entry, str) or not entry:
            raise ValueError(f"Studio view {view_id} entry must be a file path")
        placements = view.get("placements", ["tool"])
        permissions = view.get("permissions", ["operations"])
        if not isinstance(placements, list) or not placements or not all(isinstance(item, str) and item in {"tool", "workspace"} for item in placements):
            raise ValueError(f"Studio view {view_id} has unsupported placements")
        if not isinstance(permissions, list) or not all(isinstance(item, str) for item in permissions):
            raise ValueError(f"Studio view {view_id} permissions must be strings")
    return declarations


def _studio_views(plugin: Path) -> list[dict[str, Any]]:
    """Validated [[tool.pymss.studio.views]] with resolved entries inside the plugin."""
    root = plugin.resolve()
    metadata = _studio_metadata(plugin)
    requirements = read_requirements(metadata)
    _runtime_packages(metadata)
    views = []
    for view in _view_declarations(metadata):
        view_id, entry = view["id"], view["entry"]
        relative = Path(entry)
        resolved = (plugin / relative).resolve()
        if relative.is_absolute() or relative.drive or not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError(f"Studio view {view_id} entry must be a file inside the plugin")
        views.append({
            "id": view_id, "title": str(view.get("title") or view_id), "entry": entry,
            "entryPath": str(resolved), "placements": view.get("placements", ["tool"]),
            "operation": view.get("operation") if isinstance(view.get("operation"), str) else None,
            "permissions": view.get("permissions", ["operations"]),
            "sdkVersion": requirements["sdkVersion"],
            "requiredCapabilities": requirements["requiredCapabilities"],
        })
    return views


def _presentation(metadata: dict) -> dict:
    category = metadata.get("category")
    locales = {}
    values = metadata.get("locales", {})
    for language, value in (values.items() if isinstance(values, dict) else []):
        if not isinstance(language, str) or not isinstance(value, dict):
            continue
        row = {key: value[key] for key in ("title", "description") if isinstance(value.get(key), str)}
        if isinstance(value.get("tags"), list):
            row["tags"] = [tag for tag in value["tags"] if isinstance(tag, str)]
        if isinstance(value.get("operations"), dict):
            row["operations"] = {key: label for key, label in value["operations"].items() if isinstance(key, str) and isinstance(label, str)}
        locales[language] = row
    return {"category": category if isinstance(category, str) and category in _CATEGORIES else None, "locales": locales}


def _deferred_dependency_operations(plugin: Path) -> set[str]:
    values = _studio_metadata(plugin).get("deferred_dependency_operations", [])
    return {value for value in values if isinstance(value, str)} if isinstance(values, list) else set()


def _runtime_packages(metadata: dict[str, Any]) -> list[str]:
    from worker_optional_packages import OPTIONAL_PACKAGES

    names = metadata.get("runtime_packages", [])
    if not isinstance(names, list) or any(not isinstance(name, str) or name not in OPTIONAL_PACKAGES for name in names):
        raise ValueError("Studio runtime_packages must list supported runtime component ids")
    if len(names) != len(set(names)):
        raise ValueError("Duplicate Studio runtime_packages entries")
    return names


def _validate_requirements(plugin: Path, *, operation: str | None = None) -> None:
    from pip._vendor.packaging.requirements import Requirement
    from worker_optional_packages import optional_package_paths
    path = plugin / "pyproject.toml"
    if not path.exists():
        return
    project = tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})
    if operation is not None and operation in _deferred_dependency_operations(plugin):
        # These Operations own an explicit optional-backend check/install UI.
        return
    component_paths = optional_package_paths(_runtime_packages(_studio_metadata(plugin)))
    search_paths = list(dict.fromkeys([
        *(str(path) for path in component_paths if path.is_dir()),
        *sys.path, str(plugin / ".studio-deps"),
    ]))
    for text in project.get("dependencies", []):
        requirement = Requirement(text)
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        # Inventory reads the same metadata search order without activating imports.
        distribution = next(importlib.metadata.distributions(name=requirement.name, path=search_paths), None)
        if distribution is None:
            raise ValueError(f"Missing plugin dependency: {requirement.name}")
        version = distribution.version
        if requirement.specifier and version not in requirement.specifier:
            raise ValueError(f"Plugin requires {requirement}; current runtime provides {version}")


def validate_operation(runtime, operation: str) -> None:
    for item in getattr(runtime, "_studio_reports", None) or []:
        if operation in _operation_ids(item):
            intents = _execution_intents(_root())
            if intents is None:
                raise ValueError("Application plugin inventory is unavailable; managed plugins cannot run")
            intent = intents.get(item["plugin_id"], {})
            if (_root() / "disabled" / item["plugin_id"]).is_dir() or intent.get("state", "enabled") != "enabled":
                raise ValueError("Plugin is disabled")
            if not _binding_matches(_root() / "installed" / item["plugin_id"], intent):
                raise ValueError("Plugin version is not prepared for the selected runtime")
            _validate_requirements(_root() / "installed" / item["plugin_id"], operation=operation)


def _project_version(directory: Path):
    with _management(None):
        return _manager_engine()._project_version(directory)


def _directory_plugins() -> list[dict]:
    """Read-only rows for plugins in the pymss default directory (pymss CLI installs)."""
    directory = Path(os.environ.get("PYMSS_PLUGINS_DIR") or Path.home() / ".pymss" / "plugins")
    rows = []
    try:
        children = sorted(directory.iterdir()) if directory.is_dir() else []
    except OSError:
        children = []
    for child in children:
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        try:
            manifest, error = _read_manifest(child), None
        except Exception as exc:
            manifest, error = {"id": child.name, "version": None}, str(exc)
        rows.append({
            "name": manifest["id"], "version": manifest["version"], "managed": False,
            "source": "pymss", "url": None, "path": str(child), "loaded": None if error is None else False,
            "error": error, "operations": None,
        })
    return rows


def _manifest_contributions(plugin: Path) -> dict[str, Any]:
    """Studio views for the plugin, with presentation defaults applied."""
    plugin_id = _read_manifest(plugin)["id"]
    metadata = _studio_metadata(plugin)
    category = _presentation(metadata)["category"]
    views = _studio_views(plugin)
    for entry in views:
        entry["pluginId"] = plugin_id
        if category:
            entry["category"] = category
        if isinstance(metadata.get("icon"), str):
            entry["icon"] = metadata["icon"]
    return {"views": views}


def _inventory(root: Path, *, verify: bool = False, default_policy: str | None = None) -> dict[str, Any]:
    """List installations from their manifests; ``verify`` also imports them in a child process."""
    if not _management_supported():
        return _readonly_inventory(root)
    _recover_for_read(root)
    installed = [*_installed(root), *_installed(root, disabled=True)]
    bundles = _bundled()
    state, observations = _application_state(root)
    legacy_preferences = bool(observations) or any(entry["revision"] == 0 for entry in state["plugins"].values()) or bool(_legacy_plugins(root))
    policy = state["defaultPolicy"] or ("review" if legacy_preferences else default_policy or "auto")
    reports, verification_error = {}, None
    profile = runtime_profile()
    if verify and installed:
        try:
            reports = {row["name"]: row for row in _probe(root) if row.get("managed")}
        except Exception as error:
            verification_error = str(error)
    rows = []
    for plugin in installed:
        intent = state["plugins"].get(plugin.name, {})
        pending = intent.get("pending", {})
        if intent.get("state") == "removed" and not pending:
            continue
        if intent.get("state") == "enabled" and intent.get("revision", 0) > 0 and plugin.parent.name == "disabled":
            continue
        if intent.get("state") != "removed" and not _binding_matches(plugin, intent):
            continue
        enabled = plugin.parent.name == "installed" and intent.get("state", "enabled") == "enabled"
        meta, manifest, contributions, metadata = {}, None, {}, {}
        row_error = dependency_error = view_error = None
        error_kind = view_error_kind = None
        requirements = None
        compatible = {"compatible": True, "reasons": []}
        try:
            meta = _manifest(plugin)
            manifest = _read_manifest(plugin)
            metadata = manifest["studio"]
            core_requirements = read_requirements({key: metadata[key] for key in ("platforms", "architectures", "backends") if key in metadata})
            compatible = compatibility(core_requirements, profile)
            if not compatible["compatible"]:
                row_error = " ".join(compatible["reasons"])
                error_kind = "compatibility"
            try:
                requirements = read_requirements(metadata)
                view_compatibility = compatibility(requirements, profile)
                if view_compatibility["compatible"]:
                    contributions = _manifest_contributions(plugin)
                else:
                    view_error = " ".join(view_compatibility["reasons"])
                    view_error_kind = "compatibility"
            except (ValueError, OSError) as error:
                view_error = str(error)
                view_error_kind = "load"
        except Exception as error:
            row_error = str(error)
            error_kind = "load"
        if manifest is not None and row_error is None and enabled:
            try:
                _validate_requirements(plugin)
            except Exception as error:
                # Operations with their own backend manager stay usable and report the gap.
                if set(_operation_ids(manifest)) & _deferred_dependency_operations(plugin):
                    dependency_error = str(error)
                else:
                    row_error = str(error)
                    error_kind = "dependency"
        presentation = _presentation(metadata)
        for view in contributions.get("views", []):
            try:
                stat = Path(view["entryPath"]).stat()
                view["revision"] = f"{meta.get('installed_at', '')}:{meta.get('version', '')}:{stat.st_mtime_ns}:{stat.st_size}"
            except OSError:
                view["revision"] = str(meta.get("installed_at") or meta.get("version") or "")
        report = reports.get(plugin.name, {})
        loaded = enabled and row_error is None and report.get("loaded", True) is not False
        reported_error = _describe_error(report.get("error")) if enabled else None
        if error_kind is None and enabled and (reported_error or report.get("loaded") is False):
            error_kind = "load"
        operations = _operation_ids(manifest or {}) if enabled else []
        titles = {item["id"]: item["title"] for item in (manifest or {}).get("operations", [])}
        rows.append({
            "name": plugin.name, "pluginId": plugin.name, "version": (manifest or {}).get("version") or meta.get("version"),
            "managed": True, "source": meta.get("source", "unknown"), "url": meta.get("url"),
            "resolvedCommit": meta.get("resolved_commit"),
            "contentDigest": intent.get("content"), "installedAt": meta.get("installed_at"),
            "sourceRef": meta.get("ref"), "sourceSubpath": meta.get("subpath"),
            "requirements": requirements, "compatibility": compatible,
            "path": str(plugin), "loaded": loaded, "enabled": enabled,
            "prepared": True, "availability": "ready" if loaded else "unavailable",
            "removalPending": intent.get("state") == "removed" and bool(pending),
            "cleanupRuntime": pending.get("binding"),
            "preinstalled": plugin.name in bundles and meta.get("preinstalled", False),
            "toolCategory": presentation["category"],
            "toolIcon": metadata.get("icon") if isinstance(metadata.get("icon"), str) else None,
            "locales": presentation["locales"], "description": (manifest or {}).get("description", ""),
            "dependencyError": (report.get("dependencyError") or dependency_error) if enabled else None,
            "error": row_error or reported_error, "errorKind": error_kind,
            "operations": operations,
            "operationTitles": {operation: titles.get(operation, operation) for operation in operations},
            "contributions": contributions, "viewError": view_error, "viewErrorKind": view_error_kind,
        })
    represented = {row["name"] for row in rows}
    for name, entry in state["plugins"].items():
        if name in represented or (entry["state"] == "removed" and not entry.get("pending")):
            continue
        row = _unprepared_row(root, name, entry, observations, profile)
        row["preinstalled"] = name in bundles
        row["removalPending"] = entry["state"] == "removed" and bool(entry.get("pending"))
        row["cleanupRuntime"] = entry.get("pending", {}).get("binding")
        rows.append(row)
    rows.extend(_directory_plugins())
    available = [name for name in bundles if name not in state["plugins"] and not _location(root, name).exists()]
    try:
        with _plugin_registry():
            update_source = [str(_LOCAL_SOURCE.get()) if _LOCAL_SOURCE.get() else None, _REGISTRY_URL.get()]
    except (OSError, ValueError):
        # A broken catalog configuration must not hide installed plugins.
        update_source = None
    return {
        "installed": rows, "pluginsDir": str(root / "installed"),
        "verificationError": verification_error,
        "legacyPlugins": _legacy_plugins(root),
        "runtime": {"identity": root.name, "pythonPath": sys.executable, "bindingDir": str(root),
                    "updateSourceIdentity": hashlib.sha256(json.dumps(update_source).encode()).hexdigest(),
                    "managementSupported": True, **profile},
        "preinstalled": [{"name": name, "version": row["version"], "installed": name in state["plugins"] and state["plugins"][name]["state"] != "removed"}
                         for name, row in bundles.items()],
        "preinstalledPending": available if policy == "auto" and not state["migrationWarnings"] else [],
        "preinstalledAvailable": available,
        "inventoryWarnings": state["migrationWarnings"],
    }


def _catalog_source_info(*, failed: bool = False) -> dict:
    try:
        import pymss.plugins.marketplace as marketplace
    except ModuleNotFoundError as error:
        if error.name == "pymss.plugins.marketplace":
            return {"supported": False}
        raise
    capabilities = getattr(marketplace, "source_capabilities", lambda: {})()
    return {"supported": True, **capabilities, **(marketplace.source_info() or ({} if failed else {"provider": "custom"}))}


def cmd_plugin_marketplace(payload: dict[str, Any]) -> int:
    try:
        root = _root()
        snapshot = ({"runtime": {"identity": root.name, "pythonPath": sys.executable}}
                    if payload.get("catalogOnly") else _inventory(root, verify=payload.get("forceVerify") is True, default_policy=payload.get("defaultPolicy")))
        try:
            if payload.get("includeCatalog", True) and _management_supported():
                with _plugin_registry():
                    if _LOCAL_SOURCE.get():
                        items = _local_catalog(_LOCAL_SOURCE.get())
                    else:
                        from pymss.plugins.install import list_available
                        items = list_available(_REGISTRY_URL.get())
                    profile = runtime_profile()
                    snapshot["catalog"] = []
                    for item in items:
                        requirements = catalog_requirements(item.get("requirements"))
                        snapshot["catalog"].append({
                            "name": _name(item["name"]), "url": str(item.get("url") or ""),
                            "requirements": requirements, "compatibility": compatibility({**requirements, "sdkVersion": None, "requiredCapabilities": []}, profile),
                            **_presentation(item),
                            "subpath": item.get("subpath"), "description": str(item.get("description") or ""),
                            "tags": [str(tag) for tag in item.get("tags", [])],
                        })
            else:
                snapshot["catalog"] = []
            snapshot["catalogError"] = None
            if payload.get("includeCatalog", True):
                snapshot["catalogSource"] = _catalog_source_info()
        except Exception as error:
            snapshot["catalog"], snapshot["catalogError"] = [], str(error)
            snapshot["catalogSource"] = _catalog_source_info(failed=True)
        emit("plugin_marketplace", snapshot)
        return 0
    except Exception as error:
        return emit_error("PLUGIN_MARKETPLACE_FAILED", str(error), recoverable=True)


def _plugin_source_info(draft: object | None = None) -> dict:
    from worker_plugin_config import load_plugin_config, plugin_registry_url, plugin_source
    info = {"defaults": {}, "effective": {}, "available": False, "error": None,
            "catalogCount": None, "registryUrl": None, "registryPath": None,
            "configPath": str(_PLUGIN_MANIFEST)}
    try:
        config = load_plugin_config(_PLUGIN_MANIFEST)
        local_default = os.environ.get("PYMSS_STUDIO_PLUGINS_SOURCE")
        empty = {"localPath": None, "repositoryUrl": None, "ref": None, "registryRef": None}
        info["defaults"] = {**empty, "repositoryUrl": config["repository"]["url"],
                            "ref": config["repository"]["ref"],
                            "registryRef": config["repository"].get("registryRef", config["repository"]["ref"]),
                            "mode": "local" if local_default else "repository", "localPath": local_default}
        source = plugin_source(_PLUGIN_MANIFEST, draft)
        registry_override = os.environ.get("PYMSS_PLUGINS_REGISTRY") if draft is None else None
        origin = "debug" if draft is not None or os.environ.get("PYMSS_STUDIO_PLUGIN_SOURCE_OVERRIDE") else "environment" if local_default else "manifest"
        info["effective"] = {**empty, **source, "origin": "registry-environment" if registry_override else origin}
        info["available"] = None
        if registry_override:
            info["registryUrl"] = registry_override
        elif source["mode"] == "local":
            root = Path(source["localPath"])
            info["registryPath"] = str(root / "registry.json")
            info["registryUrl"] = (root / "registry.json").as_uri()
            info["catalogCount"] = len(_local_catalog(root))
            info["available"] = True
        else:
            info["registryUrl"] = plugin_registry_url({"repository": {"url": source["repositoryUrl"], "ref": source["ref"], "registryRef": source["registryRef"]}})
    except Exception as error:
        info.update(available=False, error=str(error))
    return info


def cmd_plugin_source_info(payload: dict[str, Any]) -> int:
    emit("plugin_source_info", _plugin_source_info(payload.get("source")))
    return 0


def _stage(task_id: str | None, message: str, phase: str='working', *, plugin_name: str | None=None):
    with _management(None):
        return _manager_engine()._stage(task_id, message, phase, plugin_name=plugin_name)


def _run_process(command: list[str], task_id: str | None, *, env: dict[str, str] | None=None):
    with _management(None):
        return _manager_engine()._run_process(command, task_id, env=env)


def _canonical(name: str):
    with _management(None):
        return _manager_engine()._canonical(name)


def _plugin_requirements(plugin: Path):
    with _management(None):
        return _manager_engine()._plugin_requirements(plugin)


def _dependency_versions(root: Path, exclude: str):
    with _management(root):
        return _manager_engine()._dependency_versions(root, exclude)


def _validate_dependency_publication(plugin: Path, root: Path, name: str):
    with _management(root):
        return _manager_engine()._validate_dependency_publication(plugin, root, name)


def _install_dependencies(plugin: Path, root: Path, task_id: str | None, payload: dict):
    with _management(root):
        return _manager_engine()._install_dependencies(plugin, root, task_id, payload)


def _finish_dependency_handoff(target: Path, backup: Path):
    with _management(None):
        return _manager_engine()._finish_dependency_handoff(target, backup)


def _commit_binding_intent(root: Path, package: Path, name: str, state: str, revision: int | None):
    with _management(root):
        return _manager_engine()._commit_binding_intent(root, package, name, state, revision)


def _publish(plugin: Path, root: Path, name: str, *, disabled: bool=False, preserve_dependencies: bool=False, intent_revision: int | None=None):
    with _management(root):
        return _manager_engine()._publish(plugin, root, name, disabled=disabled, preserve_dependencies=preserve_dependencies, intent_revision=intent_revision)


def _rename_plugin(source: Path, target: Path):
    with _management(None):
        return _manager_engine()._rename_plugin(source, target)


def _publish_locked(plugin: Path, root: Path, name: str, *, disabled: bool, preserve_dependencies: bool, intent_revision: int):
    with _management(root):
        return _manager_engine()._publish_locked(plugin, root, name, disabled=disabled, preserve_dependencies=preserve_dependencies, intent_revision=intent_revision)


def _verify_candidate(root: Path, candidate: Path, name: str, *, candidate_dependencies: Path | None=None):
    with _management(root):
        return _manager_engine()._verify_candidate(root, candidate, name, candidate_dependencies=candidate_dependencies)


def _acquire_source(target: str, directory: Path, subpath: str | None=None):
    with _plugin_registry():
        with _management(None):
            return _manager_engine()._acquire_source(target, directory, subpath)


@contextmanager
def _installation_transaction(root: Path, *, concurrent: bool):
    with _management(root):
        with _manager_engine()._installation_transaction(root, concurrent=concurrent) as result:
            yield result


def _install(root: Path, payload: dict, task_id: str | None, *, intent_revision: int | None=None, prepared_binding: dict | None=None):
    with _plugin_registry():
        with _management(root):
            return _manager_engine()._install(root, payload, task_id, intent_revision=intent_revision, prepared_binding=prepared_binding)


def _plugin_content_digest(directory: Path):
    with _management(None):
        return _manager_engine()._plugin_content_digest(directory)


def _check_updates(root: Path, task_id: str | None):
    with _plugin_registry():
        with _management(root):
            return _manager_engine()._check_updates(root, task_id)


def cmd_manage_plugin(payload: dict[str, Any]) -> int:
    task_id = str(payload.get("taskId") or "").strip() or None
    action = str(payload.get("action") or "").strip()
    try:
        if action not in {"install", "update", "repair", "uninstall", "check_updates", "enable", "disable", "prepare", "prepare_preinstalled", "install_preinstalled"}:
            raise ValueError("Unsupported plugin action")
        if "installDependencies" in payload and type(payload["installDependencies"]) is not bool:
            raise ValueError("installDependencies must be a boolean")
        if "expectedUpdate" in payload and getattr(_manager_engine(), "UPDATE_REVIEW_VERSION", None) != 1:
            raise PluginRuntimeIncompatible("Update this runtime's pymss core before applying a reviewed plugin update.")
        if payload.get("defaultPolicy") not in {None, "auto", "review"}:
            raise ValueError("Invalid default plugin policy")
        if payload.get("expectedSource") is not None:
            expected = payload["expectedSource"]
            if not isinstance(expected, str) or len(expected) > 8192:
                raise ValueError("Invalid expected plugin source")
            info = _plugin_source_info()
            actual = {key: value for key, value in info["effective"].items() if key != "origin"}
            actual["registryUrl"] = info["registryUrl"]
            if info["error"] or json.loads(expected) != actual:
                raise ValueError("Plugin source changed; read the catalog again before acquiring plugins")
        root = _root()
        if action in {"install", "update", "repair", "enable", "prepare", "prepare_preinstalled", "install_preinstalled"}:
            _require_plugin_runtime()
        with _plugin_registry():
            outcome = _service(root).manage(action, **{key: value for key, value in payload.items() if key != "action"})
        _stage(task_id, "Refreshing plugin status", "refreshing")
        snapshot = _inventory(root, default_policy=payload.get("defaultPolicy"))
        snapshot["updates"] = outcome["updates"]
        emit("plugin_manage_result", snapshot, task_id=task_id)
        return 0
    except PluginRuntimeIncompatible as error:
        return emit_error("PLUGIN_RUNTIME_INCOMPATIBLE", str(error), task_id=task_id, recoverable=True)
    except PluginCompatibilityError as error:
        return emit_error(error.code, str(error), task_id=task_id, recoverable=True)
    except Exception as error:
        return emit_error("PLUGIN_MANAGE_FAILED", str(error), task_id=task_id, recoverable=True)
