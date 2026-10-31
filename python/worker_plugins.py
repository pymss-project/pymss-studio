"""Studio plugin marketplace: runtime-owned installs and transactional dependency sidecars.

The pymss installer owns source acquisition. Studio owns the environment selection,
dependency constraints, staged verification, and publication of each installation.
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
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from threading import RLock
from pathlib import Path
from typing import Any, Iterator

from worker_protocol import emit, emit_error, isolate_protocol_stdout
from worker_runtime_lock import RuntimeLockBusy, runtime_lock
import worker_plugin_inventory as application_inventory
import worker_plugin_python as python_profiles
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


def _require_plugin_runtime() -> None:
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


def _name(value: Any) -> str:
    """Validate a plugin id; it is also the installation directory name."""
    name = str(value or "").strip()
    if len(name) > 100 or not _NAME.fullmatch(name):
        raise ValueError("Invalid plugin id")
    return name


def _root() -> Path:
    from worker_optional_packages import _runtime_context

    runtime = _runtime_context()
    # Preserve the venv path: resolving its python symlink would merge distinct venvs.
    digest = hashlib.sha256(os.path.normcase(os.path.abspath(sys.executable)).encode()).hexdigest()[:12]
    return runtime.packages_dir.parent.parent / "plugins" / f"{runtime.identity}-{digest}"


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


def _read_manifest(plugin: Path) -> dict[str, Any]:
    """Read the v2 manifest fields Studio lists, without importing pymss or plugin code.

    pymss validates the complete manifest when the plugin is verified and loaded.
    """
    path = plugin / "pyproject.toml"
    if not path.is_file():
        raise ValueError("Plugin has no pyproject.toml with [tool.pymss.plugin]")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    pymss = data.get("tool", {}).get("pymss", {})
    plugin_table = pymss.get("plugin")
    if not isinstance(plugin_table, dict):
        raise ValueError("Plugin has no [tool.pymss.plugin] manifest")
    if plugin_table.get("api_version") != 2:
        raise ValueError(f"Plugin uses api_version {plugin_table.get('api_version')!r}; this Studio requires 2")
    plugin_id = _name(plugin_table.get("id"))
    operations = plugin_table.get("operations", [])
    if not isinstance(operations, list) or not all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in operations):
        raise ValueError("Plugin operations must be an array of tables with ids")
    studio = pymss.get("studio", {})
    project = data.get("project", {})
    return {
        "id": plugin_id,
        "version": project.get("version") if isinstance(project.get("version"), str) else None,
        "description": project.get("description", "") if isinstance(project.get("description"), str) else "",
        "operations": [{"id": item["id"], "title": str(item.get("title") or item["id"])} for item in operations],
        "studio": studio if isinstance(studio, dict) else {},
    }


def _local_package(root: Path, name: str | None, subpath: object) -> Path:
    if not isinstance(subpath, str) or not subpath or "\\" in subpath or ":" in subpath:
        raise ValueError(f"Invalid local plugin registry subpath: {name}")
    relative = Path(subpath)
    path = root / relative
    if relative.is_absolute() or ".." in relative.parts or not path.resolve().is_relative_to(root):
        raise ValueError(f"Local plugin registry entry escapes its source root: {name}")
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError(f"Local plugin registry cannot contain symbolic links: {name}")
        current = current.parent
    if not path.is_dir():
        raise ValueError(f"Local registry entry does not select a plugin package: {name}")
    if name is not None and _read_manifest(path)["id"] != name:
        raise ValueError(f"Local registry entry does not select its matching plugin package: {name}")
    return path


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


def _uses_default_source(meta: dict) -> bool:
    return bool(meta.get("preinstalled") or meta.get("defaultRepository") or meta.get("source") == "registry")


def _migrate_v1(root: Path) -> None:
    """Move installations that are not v2 plugins named by their id out of the load path."""
    legacy = root / "legacy-v1"
    for store in ("installed", "disabled"):
        directory = root / store
        if not directory.is_dir():
            continue
        for plugin in directory.iterdir():
            if not plugin.is_dir() or plugin.name.startswith("."):
                continue
            try:
                current = _read_manifest(plugin)["id"] == plugin.name
            except Exception:
                current = False
            if current:
                continue
            target = legacy / plugin.name
            suffix = 2
            while target.exists() or target.is_symlink():
                target = legacy / f"{plugin.name}-{suffix}"
                suffix += 1
            legacy.mkdir(parents=True, exist_ok=True)
            plugin.rename(target)


def _legacy_plugins(root: Path) -> list[str]:
    legacy = root / "legacy-v1"
    return sorted(path.name for path in legacy.iterdir() if path.is_dir()) if legacy.is_dir() else []


def _recover(root: Path) -> None:
    for store, backup_store in (("installed", "backups"), ("disabled", "disabled-backups")):
        backups = root / backup_store
        backups.mkdir(parents=True, exist_ok=True)
        for backup in backups.iterdir():
            if not backup.is_dir() or not _NAME.fullmatch(backup.name):
                continue
            target = root / store / backup.name
            target.parent.mkdir(parents=True, exist_ok=True)
            opposite = root / ("disabled" if store == "installed" else "installed") / backup.name
            if not target.exists() and opposite.exists():
                previous = _manifest(backup).get("inventory", {}).get("revision", 0)
                replacement = _manifest(opposite).get("inventory", {}).get("revision", 0)
                if type(previous) is int and type(replacement) is int and replacement > previous:
                    _finish_dependency_handoff(opposite, backup)
                    shutil.rmtree(backup)
                    continue
            if target.exists():
                _finish_dependency_handoff(target, backup)
                shutil.rmtree(backup)
            else:
                backup.rename(target)
    transactions = root / "transactions"
    if transactions.exists():
        for transaction in transactions.iterdir():
            if transaction.is_dir() and transaction.name.startswith(("install-", "removed-")):
                try:
                    with runtime_lock(transaction / ".lease", timeout=0):
                        pass
                except RuntimeLockBusy:
                    continue
                shutil.rmtree(transaction, ignore_errors=True)
    _migrate_v1(root)


def _recover_for_read(root: Path) -> None:
    if not root.exists():
        return
    try:
        with runtime_lock(root / ".lock", timeout=0, allow_read_only=True) as writable:
            if writable:
                _recover(root)
    except (RuntimeLockBusy, OSError):
        # A live writer owns its transaction; readers must never clean it up.
        # Read-only installations can still be inspected and executed.
        pass


def _installed(root: Path, *, disabled: bool = False) -> list[Path]:
    directory = root / ("disabled" if disabled else "installed")
    if not directory.exists():
        return []
    return sorted(p for p in directory.iterdir() if p.is_dir() and _NAME.fullmatch(p.name))


def _location(root: Path, name: str) -> Path:
    active, inactive = root / "installed" / name, root / "disabled" / name
    if active.exists() and inactive.exists():
        raise ValueError(f"Plugin has conflicting active and disabled installations: {name}")
    return inactive if inactive.exists() else active


def _bundled() -> dict[str, dict]:
    manifest = _BUNDLED_ROOT / "manifest.json"
    if not manifest.is_file():
        return {}
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if data.get("schemaVersion") != 1 or not isinstance(data.get("plugins"), list):
        raise ValueError("Invalid preinstalled plugin manifest")
    rows = {}
    for row in data["plugins"]:
        name = _name(row["name"])
        if name in rows or not isinstance(row.get("files"), dict):
            raise ValueError("Invalid preinstalled plugin entry")
        for relative in row["files"]:
            path = Path(relative)
            if path.is_absolute() or path.drive or "\\" in relative or ".." in path.parts:
                raise ValueError("Invalid preinstalled plugin file path")
        rows[name] = row
    return rows


def _removed_preinstalled(root: Path) -> set[str]:
    path = root / "preinstalled-removed.json"
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(name, str) for name in data):
        raise ValueError("Invalid preinstalled plugin preferences")
    return {_name(name) for name in data}


def _mark_preinstalled_removed(root: Path, name: str, removed: bool) -> None:
    names = _removed_preinstalled(root)
    if removed:
        names.add(name)
    else:
        names.discard(name)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, prefix=".plugin-state-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(sorted(names), stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / "preinstalled-removed.json")
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def _intent_definition(manifest: dict) -> dict:
    fields = {"sdk_version", "required_capabilities", "platforms", "architectures", "backends", "category", "icon", "locales", "views"}
    definition = {
        "description": manifest.get("description", ""), "operations": manifest.get("operations", []),
        "studio": {key: value for key, value in manifest.get("studio", {}).items() if key in fields},
    }
    return json.loads(json.dumps(definition, ensure_ascii=False, allow_nan=False))


def _application_bindings(root: Path) -> tuple[list[dict], set[str], list[str]]:
    roots = [root]
    warnings = []
    if root.parent.is_dir():
        roots.extend(path for path in sorted(root.parent.iterdir())
                     if path != root and path.is_dir() and not path.is_symlink() and not path.name.startswith(".")
                     and any((path / name).exists() for name in ("installed", "disabled", "preinstalled-removed.json")))
    observations, removed = [], set()
    for binding in roots:
        if not application_inventory.valid_binding(binding.name):
            warnings.append("A plugin runtime directory has an unsupported name and was skipped")
            continue
        try:
            removed.update(_removed_preinstalled(binding))
        except (OSError, ValueError, TypeError):
            warnings.append(f"Plugin removal preferences are unavailable for runtime {binding.name}")
        for enabled, directory in ((True, binding / "installed"), (False, binding / "disabled")):
            try:
                children = sorted(directory.iterdir()) if directory.is_dir() else []
            except OSError:
                warnings.append(f"Plugin records are unavailable for runtime {binding.name}")
                continue
            for package in children:
                if not package.is_dir() or package.is_symlink() or package.name.startswith((".", "_")):
                    continue
                try:
                    manifest, meta = _read_manifest(package), _manifest(package)
                    if manifest["id"] != package.name:
                        continue
                    content = meta.get("inventoryContent")
                    observations.append({
                        "name": manifest["id"], "binding": binding.name, "path": str(package),
                        "enabled": enabled, "version": manifest["version"], "definition": _intent_definition(manifest),
                        "marker": meta.get("inventory", {}), "meta": meta,
                        "content": content if isinstance(content, str) and re.fullmatch(r"[0-9a-f]{64}", content) else None,
                    })
                except (OSError, ValueError, TypeError, KeyError):
                    continue
    return observations, removed, warnings


def _application_state(root: Path) -> tuple[dict, list[dict]]:
    observations, removed, warnings = _application_bindings(root)
    state = application_inventory.merge(root, observations, removed)
    state["migrationWarnings"] = warnings
    return state, observations


def _execution_intents(root: Path) -> dict | None:
    if root != _root():
        return {}
    try:
        return _application_state(root)[0]["plugins"]
    except (OSError, ValueError, TypeError, KeyError):
        # Inventory corruption disables managed code; built-in operations remain usable.
        return None


def _binding_sources(observations: list[dict], name: str) -> list[dict]:
    sources = [item for item in observations if item["name"] == name]
    if len(sources) > 1:
        sources = [{**item, "content": item.get("content") or _plugin_content_digest(Path(item["path"]))}
                   for item in sources]
    return sources


def _binding_matches(package: Path, intent: dict) -> bool:
    if intent.get("revision", 0) == 0:
        return True
    try:
        manifest, meta = _read_manifest(package), _manifest(package)
        return ((intent.get("version") is None or manifest["version"] == intent["version"])
                and (intent.get("content") is None or meta.get("inventoryContent") == intent["content"]))
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _has_current_installation(root: Path, name: str) -> bool:
    intent = _application_state(root)[0]["plugins"].get(name, {})
    package = _location(root, name)
    return package.exists() and intent.get("state") != "removed" and _binding_matches(package, intent)


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
    requirements = read_requirements(metadata)
    views = []
    for view in metadata.get("views", []):
        if not isinstance(view, dict) or not isinstance(view.get("id"), str) or not isinstance(view.get("entry"), str):
            continue
        views.append({**view, "title": str(view.get("title") or view["id"]), "permissions": view.get("permissions", []),
                      "sdkVersion": requirements["sdkVersion"], "requiredCapabilities": requirements["requiredCapabilities"],
                      "category": view.get("category") or presentation["category"], "pluginId": name})
    preferred = next((item for item in sources if item["binding"] == entry.get("binding")), sources[0] if sources else None)
    meta = preferred["meta"] if preferred else {}
    operations = definition.get("operations", [])
    return {
        "name": name, "pluginId": name, "version": entry.get("version"), "source": meta.get("source", "recorded"),
        "url": meta.get("url"), "path": "", "managed": True, "enabled": entry["state"] == "enabled", "loaded": False,
        "prepared": False, "availability": "not_prepared", "error": None, "dependencyError": None,
        "requirements": requirements, "compatibility": compatibility(requirements, profile),
        "description": definition.get("description", ""), "locales": presentation["locales"],
        "toolCategory": presentation["category"], "toolIcon": metadata.get("icon"),
        "operations": [item["id"] for item in operations], "operationTitles": {item["id"]: item["title"] for item in operations},
        "contributions": {"views": views}, "sourceSelectionRequired": selection_required,
        "availableBindings": [{"runtime": item["binding"], "version": item.get("version"), "content": item.get("content")} for item in eligible],
    }


def _prepare_binding(root: Path, payload: dict, task_id: str | None, revision: int) -> None:
    name = _name(payload.get("name"))
    state, observations = _application_state(root)
    entry = state["plugins"].get(name)
    if entry is None:
        raise ValueError("Plugin is not installed")
    if entry["state"] == "removed":
        raise ValueError("Plugin is removed; reinstall it to restore its availability")
    sources = _binding_sources(observations, name)
    requested = payload.get("sourceRuntime")
    if requested is not None and not isinstance(requested, str):
        raise ValueError("Invalid plugin source runtime")
    if requested:
        source = next((item for item in sources if item["binding"] == requested), None)
        if source and "sourceVersion" in payload and payload["sourceVersion"] != source.get("version"):
            raise ValueError("Plugin source changed; refresh its available versions")
    elif entry.get("sourceSelectionRequired"):
        raise ValueError("Select the plugin source version before preparing this runtime")
    elif entry.get("revision", 0) > 0:
        source = next((item for item in sources if item["binding"] == entry.get("binding")
                       and (entry.get("version") is None or item.get("version") == entry["version"])
                       and (entry.get("content") is None or item.get("content") == entry["content"])), None)
        if source is None:
            source = next((item for item in sources if item.get("version") == entry.get("version") and item.get("content") == entry.get("content")), None)
    else:
        if len({(item.get("version"), item.get("content")) for item in sources}) > 1:
            raise ValueError("Select the plugin source version before preparing this runtime")
        source = sources[0] if sources else None
    if source is None:
        raise ValueError("The selected plugin files are unavailable; reinstall the plugin from its original source")
    path = Path(source["path"])
    expected = payload.get("sourceContent") or source.get("content")
    actual = _plugin_content_digest(path)
    if expected is not None and (not isinstance(expected, str) or expected != actual):
        raise ValueError("Plugin source files changed; repair the source installation before preparing another runtime")
    _install(root, {**payload, "action": "install", "name": name}, task_id, intent_revision=revision,
             prepared_binding={**source, "content": actual, "enabled": entry["state"] == "enabled" or payload.get("action") == "enable"})


def _record_intent(root: Path, name: str, state: str, *, pending: str | None = None, revision: int | None = None) -> int:
    observations, removed, _warnings = _application_bindings(root)
    current = application_inventory.merge(root, observations, removed)["plugins"].get(name)
    if current is None:
        raise ValueError("Plugin is not installed")
    if current.get("revision", 0) == 0:
        active = next((item for item in observations if item["name"] == name and item["binding"] == root.name), None)
        if active:
            current = {**current, **active}
        elif len({(item.get("version"), item.get("content")) for item in _binding_sources(observations, name)}) > 1:
            current = {**current, "binding": None, "version": None, "content": None, "sourceSelectionRequired": True}
    if revision is None:
        revision = application_inventory.reserve(root, observations, removed)
    application_inventory.commit(
        root, name, state, revision, version=current.get("version"), content=current.get("content"),
        definition=current.get("definition", {}), binding=current.get("binding"), pending=pending,
        selection_required=current.get("sourceSelectionRequired", False),
    )
    return revision


def _prepare_preinstalled(root: Path, task_id: str | None, requested: str | None = None, *, intent_revision: int | None = None) -> None:
    bundles = _bundled()
    state, _observations = _application_state(root)
    removed = _removed_preinstalled(root) | {name for name, entry in state["plugins"].items() if entry["state"] != "enabled"}
    if requested is not None and requested not in bundles:
        raise ValueError("Unknown preinstalled plugin")
    selected = [requested] if requested is not None else list(bundles)
    for name in selected:
        current = _location(root, name)
        if current.exists():
            if requested:
                _enable_binding(root, name, intent_revision)
            continue
        if requested is None and (name in removed or name in state["plugins"]):
            continue
        row = bundles[name]
        source = _BUNDLED_ROOT / name
        for relative, expected in row["files"].items():
            path = source / relative
            if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()) or not path.is_file():
                raise ValueError(f"Invalid preinstalled plugin file: {name}/{relative}")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f"Preinstalled plugin checksum mismatch: {name}/{relative}")
        transactions = root / "transactions"
        transactions.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=transactions, prefix="install-preinstalled-") as temporary:
            staged = Path(temporary) / name
            staged.mkdir()
            for relative in row["files"]:
                target = staged / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / relative, target)
            if _read_manifest(staged)["id"] != name:
                raise ValueError(f"Preinstalled plugin id does not match its bundle: {name}")
            primary = next((view.get("operation") for view in _studio_views(staged) if view.get("id") == "tool"), None)
            require_compatible(_studio_metadata(staged))
            _validate_requirements(staged, operation=primary)
            (staged / ".pymss-install.json").write_text(json.dumps({
                "name": name, "version": row["version"], "source": "git", "url": row.get("url"),
                "subpath": row.get("subpath"), "ref": None, "preinstalled": True,
            }), encoding="utf-8")
            _stage(task_id, f"Preparing preinstalled plugin: {name}", "preparing", plugin_name=name)
            python_profiles.prepare_python(staged, root, lambda message: _stage(task_id, message, "preparing"))
            _verify_candidate(root, staged, name)
            if requested:
                _mark_preinstalled_removed(root, name, False)
            _publish(staged, root, name, intent_revision=intent_revision)


def _set_enabled(root: Path, name: str, enabled: bool, *, verify: bool = True) -> None:
    source = _location(root, name)
    if not source.is_dir():
        raise ValueError("Plugin is not installed in this runtime")
    target = root / ("installed" if enabled else "disabled") / name
    if enabled and verify:
        require_compatible(_studio_metadata(source))
        _verify_candidate(root, source, name)
    if source == target:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)


def _enable_binding(root: Path, name: str, revision: int | None) -> None:
    source = _location(root, name)
    require_compatible(_studio_metadata(source))
    _validate_dependency_publication(source, root, name)
    _verify_candidate(root, source, name)
    if revision is None:
        observations, removed, _warnings = _application_bindings(root)
        revision = application_inventory.reserve(root, observations, removed)
    with application_inventory.mutation(root, name, revision):
        _set_enabled(root, name, True, verify=False)
        _commit_binding_intent(root, _location(root, name), name, "enabled", revision)


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
    """Load pymss default-directory plugins and runtime-owned marketplace plugins."""
    # Batch workflows call this from worker threads. stdout redirection is
    # process-global, and plugin activation mutates the shared runtime.
    with _RUNTIME_LOCK, isolate_protocol_stdout():
        return _operation_runtime()


def _load_runtime_plugins(runtime, root: Path) -> list[dict[str, Any]]:
    from pymss.plugins import ensure_plugins_loaded
    from worker_optional_packages import optional_package_activation

    profile = runtime_profile()
    components: dict[Path, list[str]] = {}
    intents = _execution_intents(root)

    def validate(plugin: Path) -> None:
        if plugin.parent == root / "installed" and intents is None:
            raise ValueError("Application plugin inventory is unavailable; managed plugins cannot run")
        if plugin.parent == root / "installed" and intents.get(plugin.name, {}).get("state", "enabled") != "enabled":
            raise ValueError("Plugin is disabled by the application configuration")
        if plugin.parent == root / "installed" and not _binding_matches(plugin, intents.get(plugin.name, {})):
            raise ValueError("Plugin version is not prepared for the selected runtime")
        metadata = _studio_metadata(plugin)
        require_compatible(metadata, profile)
        components[plugin] = _runtime_packages(metadata)

    def activate(plugin: Path):
        return optional_package_activation(components[plugin])

    ensure_plugins_loaded(validate_plugin=validate, activation_context=activate)
    return runtime.load_plugins([root / "installed"], validate_plugin=validate, activation_context=activate)


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
    if getattr(runtime, "_studio_root", None) != str(root):
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
    """Plugin imports run in a bounded child, keeping query stdout and state clean."""
    _require_plugin_runtime()
    code = f"""
import json, sys
from pathlib import Path
import worker_plugins as p
from worker_protocol import isolate_protocol_stdout
with isolate_protocol_stdout():
    p.activate_plugin_dependencies(Path(sys.argv[1]))
    sys.path.extend(json.loads(sys.argv[2]))
    from pymss.operations import default_runtime
    runtime = default_runtime()
    runtime._studio_reports = p._load_runtime_plugins(runtime, Path(sys.argv[1]))
    reports = p.plugin_reports(runtime, Path(sys.argv[1]) / "installed")
print({_PROBE_MARKER!r} + json.dumps(reports))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [
        str(Path(__file__).parent), env.get("PYTHONPATH", ""),
    ]))
    result = subprocess.run(
        [sys.executable, "-c", code, str(root), json.dumps(list(map(str, dependency_paths or [])))], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=45, env=env,
    )
    for line in reversed(result.stdout.splitlines()):
        if line.startswith(_PROBE_MARKER):
            return json.loads(line[len(_PROBE_MARKER):])
    raise RuntimeError((result.stderr or "Plugin verification produced no report")[-3000:])


def _manifest(plugin: Path) -> dict[str, Any]:
    path = plugin / ".pymss-install.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


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


def _studio_views(plugin: Path) -> list[dict[str, Any]]:
    """Validated [[tool.pymss.studio.views]] with resolved entries inside the plugin."""
    root = plugin.resolve()
    metadata = _studio_metadata(plugin)
    requirements = read_requirements(metadata)
    _runtime_packages(metadata)
    views, seen = [], set()
    for view in metadata.get("views", []):
        if not isinstance(view, dict):
            raise ValueError("Each Studio view must be a table")
        view_id = str(view.get("id") or "")
        if not _NAME.fullmatch(view_id) or view_id in seen:
            raise ValueError(f"Invalid or duplicate Studio view id: {view_id!r}")
        seen.add(view_id)
        entry = view.get("entry")
        relative = Path(entry) if isinstance(entry, str) and entry else None
        resolved = (plugin / relative).resolve() if relative else None
        if relative is None or relative.is_absolute() or relative.drive or resolved is None or not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError(f"Studio view {view_id} entry must be a file inside the plugin")
        placements = view.get("placements", ["tool"])
        permissions = view.get("permissions", ["operations"])
        if not isinstance(placements, list) or not placements or not all(item in {"tool", "workspace"} for item in placements):
            raise ValueError(f"Studio view {view_id} has unsupported placements")
        if not isinstance(permissions, list) or not all(isinstance(item, str) for item in permissions):
            raise ValueError(f"Studio view {view_id} permissions must be strings")
        views.append({
            "id": view_id, "title": str(view.get("title") or view_id), "entry": entry,
            "entryPath": str(resolved), "placements": placements,
            "operation": view.get("operation") if isinstance(view.get("operation"), str) else None,
            "permissions": permissions,
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


def _project_version(directory: Path) -> str | None:
    project = directory / "pyproject.toml"
    if not project.exists():
        return None
    return tomllib.loads(project.read_text(encoding="utf-8")).get("project", {}).get("version")


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
        row_error = dependency_error = None
        requirements = None
        compatible = {"compatible": True, "reasons": []}
        try:
            meta = _manifest(plugin)
            manifest = _read_manifest(plugin)
            metadata = manifest["studio"]
            requirements = read_requirements(metadata)
            compatible = compatibility(requirements, profile)
            if not compatible["compatible"]:
                row_error = " ".join(compatible["reasons"])
            contributions = _manifest_contributions(plugin)
        except Exception as error:
            row_error = str(error)
        if manifest is not None and row_error is None and enabled:
            try:
                _validate_requirements(plugin)
            except Exception as error:
                # Operations with their own backend manager stay usable and report the gap.
                if set(_operation_ids(manifest)) & _deferred_dependency_operations(plugin):
                    dependency_error = str(error)
                else:
                    row_error = str(error)
        presentation = _presentation(metadata)
        for view in contributions.get("views", []):
            try:
                stat = Path(view["entryPath"]).stat()
                view["revision"] = f"{meta.get('installed_at', '')}:{meta.get('version', '')}:{stat.st_mtime_ns}:{stat.st_size}"
            except OSError:
                view["revision"] = str(meta.get("installed_at") or meta.get("version") or "")
        report = reports.get(plugin.name, {})
        loaded = enabled and row_error is None and report.get("loaded", True) is not False
        operations = _operation_ids(manifest or {}) if enabled else []
        titles = {item["id"]: item["title"] for item in (manifest or {}).get("operations", [])}
        rows.append({
            "name": plugin.name, "pluginId": plugin.name, "version": (manifest or {}).get("version") or meta.get("version"),
            "managed": True, "source": meta.get("source", "unknown"), "url": meta.get("url"),
            "resolvedCommit": meta.get("resolved_commit"),
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
            "error": row_error or (_describe_error(report.get("error")) if enabled else None),
            "operations": operations,
            "operationTitles": {operation: titles.get(operation, operation) for operation in operations},
            "contributions": contributions,
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
    return {
        "installed": rows, "pluginsDir": str(root / "installed"),
        "verificationError": verification_error,
        "legacyPlugins": _legacy_plugins(root),
        "runtime": {"identity": root.name, "pythonPath": sys.executable, **profile},
        "preinstalled": [{"name": name, "version": row["version"], "installed": name in state["plugins"] and state["plugins"][name]["state"] != "removed"}
                         for name, row in bundles.items()],
        "preinstalledPending": available if policy == "auto" and not state["migrationWarnings"] else [],
        "preinstalledAvailable": available,
        "inventoryWarnings": state["migrationWarnings"],
    }


def cmd_plugin_marketplace(payload: dict[str, Any]) -> int:
    try:
        root = _root()
        snapshot = ({"runtime": {"identity": root.name, "pythonPath": sys.executable}}
                    if payload.get("catalogOnly") else _inventory(root, verify=payload.get("forceVerify") is True, default_policy=payload.get("defaultPolicy")))
        try:
            if payload.get("includeCatalog", True):
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
                            "requirements": requirements, "compatibility": compatibility(requirements, profile),
                            **_presentation(item),
                            "subpath": item.get("subpath"), "description": str(item.get("description") or ""),
                            "tags": [str(tag) for tag in item.get("tags", [])],
                        })
            else:
                snapshot["catalog"] = []
            snapshot["catalogError"] = None
        except Exception as error:
            snapshot["catalog"], snapshot["catalogError"] = [], str(error)
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


def _stage(task_id: str | None, message: str, phase: str = "working", *, plugin_name: str | None = None) -> None:
    emit("plugin_stage", {"message": message, "phase": phase, **({"pluginName": plugin_name} if plugin_name else {})}, task_id=task_id)


def _run_process(command: list[str], task_id: str | None, *, env: dict[str, str] | None = None) -> None:
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env,
    )
    recent: list[str] = []
    try:
        assert process.stdout is not None
        for line in process.stdout:
            message = line.strip()[-2000:]
            if message:
                recent = (recent + [message])[-8:]
                emit("plugin_log", {"message": message}, task_id=task_id)
        code = process.wait()
        if code:
            raise RuntimeError("\n".join(recent) or f"Dependency installer exited with code {code}")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if process.stdout:
            process.stdout.close()


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _plugin_requirements(plugin: Path) -> list[str]:
    from pip._vendor.packaging.specifiers import SpecifierSet
    from pip._vendor.packaging.version import Version

    project_file = plugin / "pyproject.toml"
    project = tomllib.loads(project_file.read_text(encoding="utf-8")).get("project", {}) if project_file.exists() else {}
    python_constraint = project.get("requires-python")
    if python_constraint and Version(".".join(map(str, sys.version_info[:3]))) not in SpecifierSet(python_constraint):
        raise ValueError(f"Plugin requires Python {python_constraint}")
    requirements = project.get("dependencies", [])
    if not isinstance(requirements, list) or not all(isinstance(item, str) for item in requirements):
        raise ValueError("Plugin dependencies must be a list of Python requirements")
    return requirements


def _dependency_versions(root: Path, exclude: str) -> dict[str, str]:
    versions: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            versions[_canonical(name)] = distribution.version
    intents = _application_state(root)[0]["plugins"]
    for other in _installed(root):
        if other.name == exclude:
            continue
        intent = intents.get(other.name, {})
        if intent.get("state", "enabled") != "enabled" or not _binding_matches(other, intent):
            continue
        for distribution in importlib.metadata.distributions(path=[str(other / ".studio-deps")]):
            name = _canonical(distribution.metadata["Name"])
            if name in versions and versions[name] != distribution.version:
                raise ValueError(f"Installed plugin dependency conflict: {name}")
            versions[name] = distribution.version
    return versions


def _validate_dependency_publication(plugin: Path, root: Path, name: str) -> None:
    dependencies = plugin / ".studio-deps"
    if not dependencies.is_dir():
        return
    versions = _dependency_versions(root, name)
    for distribution in importlib.metadata.distributions(path=[str(dependencies)]):
        dependency = _canonical(distribution.metadata["Name"])
        if dependency in versions and versions[dependency] != distribution.version:
            raise ValueError(f"Installed plugin dependency conflict: {dependency}")


def _install_dependencies(plugin: Path, root: Path, task_id: str | None, payload: dict) -> None:
    from pip._vendor.packaging.requirements import Requirement

    python_profiles.prepare_python(plugin, root, lambda message: _stage(task_id, message, "preparing"))
    requirements = _plugin_requirements(plugin)
    project_file = plugin / "pyproject.toml"
    if not requirements:
        return
    # Freeze host and peer sidecars, without borrowing another plugin's files.
    versions = _dependency_versions(root, plugin.name)
    constraints = plugin.parent / "constraints.txt"
    constraint_lines = [f"{name}=={version}\n" for name, version in sorted(versions.items())]
    # The tool's backend manager and an explicit marketplace repair must agree
    # on supported ASR versions. Frozen host constraints still take precedence.
    from worker_optional_packages import _package_spec
    backend_requirement = _package_spec("funasr")[1]["requirement"]
    backend_name = _canonical(Requirement(backend_requirement).name)
    for text in requirements:
        requirement = Requirement(text)
        if _canonical(requirement.name) == backend_name and (not requirement.marker or requirement.marker.evaluate({"extra": ""})):
            constraint_lines.append(backend_requirement + "\n")
            break
    constraints.write_text("".join(constraint_lines), encoding="utf-8")
    report = plugin.parent / "pip-report.json"
    from worker_bootstrap import _resolve_pypi_mirror
    _, index = _resolve_pypi_mirror(str(payload.get("mirror") or "auto"), str(payload.get("locale") or ""))
    index_args = ["--index-url", index] if index else []
    base = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--prefer-binary"]
    # dependency_mode = "declared-only" (in [tool.pymss.plugin]): transitive
    # metadata is ignored — the plugin's dependency list IS the full closure.
    # Used when an upstream wheel pins a host-incompatible version of a package
    # that is actually compatible (e.g. gamemlx's conservative numpy<2 pin).
    plugin_meta = tomllib.loads(project_file.read_text(encoding="utf-8")).get("tool", {}).get("pymss", {}).get("plugin", {}) if project_file.exists() else {}
    declared_only = str(plugin_meta.get("dependency_mode") or "").strip().lower() == "declared-only"
    _stage(task_id, "Resolving plugin dependencies against the active runtime", "resolving")
    resolve_cmd = base + ["--dry-run", "--report", str(report), "--constraint", str(constraints)]
    if declared_only:
        resolve_cmd.append("--no-deps")
    _run_process(resolve_cmd + [*index_args, *requirements], task_id)
    resolved = json.loads(report.read_text(encoding="utf-8")).get("install", [])
    selected = [
        item["download_info"]["url"] if item.get("is_direct") else
        f"{item['metadata']['name']}=={item['metadata']['version']}"
        for item in resolved
        if not declared_only or _canonical(str(item["metadata"]["name"])) not in versions
    ]
    if selected:
        _stage(task_id, "Installing additional dependencies", "installing")
        _run_process(base + ["--no-deps", "--target", str(plugin / ".studio-deps"), *index_args, *selected], task_id)


def _finish_dependency_handoff(target: Path, backup: Path) -> None:
    marker = target / _DEPENDENCY_HANDOFF
    if marker.exists():
        dependencies = target / ".studio-deps"
        if not dependencies.exists():
            (backup / ".studio-deps").rename(dependencies)
        marker.unlink()


def _commit_binding_intent(root: Path, package: Path, name: str, state: str, revision: int | None) -> None:
    manifest, meta = _read_manifest(package), _manifest(package)
    if revision is None:
        observations, removed, _warnings = _application_bindings(root)
        revision = application_inventory.reserve(root, observations, removed)
    content = _plugin_content_digest(package)
    meta.update({"inventory": {"revision": revision, "state": state}, "inventoryContent": content})
    path = package / ".pymss-install.json"
    temporary = package / ".pymss-install-intent.tmp"
    try:
        temporary.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    application_inventory.commit(
        root, name, state, revision, version=manifest["version"], definition=_intent_definition(manifest), content=content,
    )


def _publish(plugin: Path, root: Path, name: str, *, disabled: bool = False, preserve_dependencies: bool = False,
             intent_revision: int | None = None) -> None:
    if intent_revision is None:
        observations, removed, _warnings = _application_bindings(root)
        intent_revision = application_inventory.reserve(root, observations, removed)
    with application_inventory.mutation(root, name, intent_revision):
        backup = _publish_locked(plugin, root, name, disabled=disabled, preserve_dependencies=preserve_dependencies, intent_revision=intent_revision)
    if backup.exists():
        # Deleting large sidecars must not block unrelated runtime publications.
        shutil.rmtree(backup, ignore_errors=True)


def _rename_plugin(source: Path, target: Path) -> None:
    deadline = time.monotonic() + 2
    while True:
        try:
            source.rename(target)
            return
        except OSError as error:
            if getattr(error, "winerror", None) not in {5, 32} or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _publish_locked(plugin: Path, root: Path, name: str, *, disabled: bool, preserve_dependencies: bool,
                    intent_revision: int) -> Path:
    target = root / ("disabled" if disabled else "installed") / name
    previous = _location(root, name)
    backup = root / ("disabled-backups" if previous.parent.name == "disabled" else "backups") / name
    target.parent.mkdir(parents=True, exist_ok=True)
    backup.parent.mkdir(parents=True, exist_ok=True)
    marker = plugin / _DEPENDENCY_HANDOFF
    marker.unlink(missing_ok=True)
    (plugin / ".pymss-install-intent.tmp").unlink(missing_ok=True)
    if preserve_dependencies:
        if not (previous / ".studio-deps").is_dir() or (plugin / ".studio-deps").exists():
            raise ValueError("Cannot reuse plugin dependencies during publication")
        # Recovery completes this handoff if the worker stops between the two renames.
        marker.write_text("1", encoding="utf-8")
    # The ordering marker travels with the atomically published package. A lost
    # application-record write can be reconstructed from this committed binding.
    meta = _manifest(plugin)
    content = _plugin_content_digest(plugin)
    meta.update({"inventory": {"revision": intent_revision, "state": "disabled" if disabled else "enabled"}, "inventoryContent": content})
    (plugin / ".pymss-install.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    if previous.exists():
        _rename_plugin(previous, backup)
    published = False
    try:
        _rename_plugin(plugin, target)
        published = True
        _finish_dependency_handoff(target, backup)
    except BaseException:
        if published:
            if preserve_dependencies and (target / ".studio-deps").exists() and not (backup / ".studio-deps").exists():
                (target / ".studio-deps").rename(backup / ".studio-deps")
            _rename_plugin(target, plugin)
        if backup.exists():
            _rename_plugin(backup, previous)
        raise
    manifest = _read_manifest(target)
    application_inventory.commit(
        root, name, "disabled" if disabled else "enabled", intent_revision,
        version=manifest["version"], definition=_intent_definition(manifest), content=content,
    )
    return backup


def _verify_candidate(root: Path, candidate: Path, name: str, *, candidate_dependencies: Path | None = None) -> None:
    """Validate activation without replacing a current plugin or breaking healthy peers."""
    transactions = root / "transactions"
    transactions.mkdir(parents=True, exist_ok=True)
    state = _application_state(root)[0]
    peers = [peer for peer in _installed(root)
             if state["plugins"].get(peer.name, {}).get("state", "enabled") == "enabled"
             and _binding_matches(peer, state["plugins"].get(peer.name, {}))]
    previous_failures = {row["name"] for row in _probe(root) if row.get("managed") and not row.get("loaded")} if peers else set()
    with tempfile.TemporaryDirectory(dir=transactions, prefix="install-verify-") as temporary:
        probe = Path(temporary)
        directory = probe / "installed"
        directory.mkdir()
        dependencies = [candidate_dependencies if candidate_dependencies is not None else candidate / ".studio-deps"]
        for peer in peers:
            if peer.name != name:
                shutil.copytree(peer, directory / peer.name, ignore=shutil.ignore_patterns(".studio-deps"))
                dependencies.append(peer / ".studio-deps")
        shutil.copytree(candidate, directory / name, ignore=shutil.ignore_patterns(".studio-deps"))
        reports = _probe(probe, dependencies)
        failed = [row for row in reports if row.get("managed") and not row.get("loaded") and (row["name"] == name or row["name"] not in previous_failures)]
        if not any(row.get("managed") and row["name"] == name for row in reports):
            raise ValueError(f"Plugin produced no registration report: {name}")
        if failed:
            raise ValueError("; ".join(f"{row['name']}: {row.get('error')}" for row in failed))


def _acquire_source(target: str, directory: Path, subpath: str | None = None):
    from datetime import datetime, timezone
    from pymss.plugins.install import InstallResult, install, list_available

    local_root = _LOCAL_SOURCE.get()
    from_default = bool(local_root and _NAME.fullmatch(target))
    if from_default:
        entry = next((row for row in _local_catalog(local_root) if row["name"] == target), None)
        if entry is None:
            raise ValueError(f"Local plugin registry has no entry for: {target}")
        target, subpath = str(local_root), entry["subpath"]

    local = Path(target).expanduser()
    if not local.is_dir():
        registry_name = target if _NAME.fullmatch(target) else None
        remote = target.startswith(("http://", "https://", "git@", "ssh://"))
        if not remote and (local.is_absolute() or target.startswith((".", "~")) or "/" in target or "\\" in target):
            raise ValueError(f"Local plugin source folder is unavailable: {local}")
        kwargs = {}
        expected_version = None
        if registry_name:
            entry = next((row for row in list_available(_REGISTRY_URL.get()) if row["name"] == registry_name), None)
            if entry is None or not entry.get("url"):
                raise ValueError(f"Plugin source catalog has no entry for: {registry_name}")
            target = entry["url"]
            subpath = entry.get("subpath") or subpath
            expected_version = entry.get("version")
            if entry.get("ref") is not None:
                from worker_plugin_config import _validate_ref
                _validate_ref(entry["ref"])
                kwargs["ref"] = entry["ref"]
        result = install(target, plugins_dir=directory, subpath=subpath, no_deps=True,
                         registry_url=_REGISTRY_URL.get(), **kwargs)
        expected_commit = kwargs.get("ref", "")
        if re.fullmatch(r"[0-9a-f]{40,64}", expected_commit) and _manifest(result.path).get("resolved_commit") != expected_commit:
            raise ValueError("Plugin source commit does not match the catalog")
        if registry_name:
            if expected_version is not None and _project_version(result.path) != expected_version:
                raise ValueError(f"Plugin source version does not match the catalog: {registry_name}")
            if result.name != registry_name:
                raise ValueError(f"Catalog entry {registry_name} installed plugin {result.name}")
            metadata = _manifest(result.path)
            metadata["name"] = registry_name
            metadata["defaultRepository"] = True
            (result.path / ".pymss-install.json").write_text(json.dumps(metadata), encoding="utf-8")
        return result
    source = local / subpath if subpath else local
    if not source.is_dir():
        raise ValueError(f"Local plugin source folder is unavailable: {source}")
    name = _read_manifest(source)["id"]
    destination = directory / name
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(*_SOURCE_IGNORED_DIRS, ".env", ".env.*", ".DS_Store", "*.pyc", "*.pyo"))
    version = _project_version(destination)
    # Preserve the pymss manifest format without its CLI name@ref parsing of local paths.
    (destination / ".pymss-install.json").write_text(json.dumps({
        "name": name, "version": version, "source": "path", "url": str(local.resolve()),
        "subpath": subpath, "ref": None, "installed_at": datetime.now(timezone.utc).isoformat(),
        "defaultRepository": from_default,
    }), encoding="utf-8")
    return InstallResult(name=name, path=destination, source="path", version=version)


@contextmanager
def _installation_transaction(root: Path, *, concurrent: bool) -> Iterator[Path]:
    # Keep a lease journal until source cleanup; profiles stay at stable paths.
    try:
        with ExitStack() as resources:
            with runtime_lock(root / ".lock", timeout=180) if concurrent else nullcontext():
                if concurrent:
                    _recover(root)
                transactions = root / "transactions"
                transactions.mkdir(parents=True, exist_ok=True)
                directory = Path(resources.enter_context(tempfile.TemporaryDirectory(dir=transactions, prefix="install-")))
                resources.enter_context(runtime_lock(directory / ".lease", timeout=0))
            yield directory
    finally:
        with runtime_lock(root / ".lock", timeout=180) if concurrent else nullcontext():
            python_profiles.collect_python_profiles(root)


def _install(root: Path, payload: dict, task_id: str | None, *, intent_revision: int | None = None,
             prepared_binding: dict | None = None) -> str:

    action = payload["action"]
    requested = _name(payload["name"]) if payload.get("name") else None
    current = _location(root, requested) if requested else None
    prepared_source = Path(prepared_binding["path"]) if prepared_binding is not None else None
    state = _application_state(root)[0]
    desired = state["plugins"].get(requested, {})
    disabled = (not prepared_binding["enabled"]) if prepared_binding is not None else action in {"update", "repair"} and (
        desired.get("state") == "disabled" if desired.get("revision", 0) > 0 else current is not None and current.parent.name == "disabled"
    )
    if prepared_source is not None:
        target = str(prepared_source)
    elif action in {"update", "repair"}:
        if desired.get("state") == "removed":
            raise ValueError("Plugin is not installed")
        if current is None or not current.is_dir():
            raise ValueError("Plugin is not installed in this runtime")
        meta = _manifest(current)
        if action == "repair":
            target = str(current)
        else:
            target = requested if _uses_default_source(meta) else str(meta.get("url") or "")
            if not target:
                raise ValueError("Plugin has no update source; install it again from its source")
            if meta.get("source") == "path" and target != requested and not Path(target).is_dir():
                raise ValueError(f"Local plugin source folder is unavailable: {target}")
    else:
        target = str(payload.get("target") or requested or "").strip()
        if not target:
            raise ValueError("Missing plugin source")
    if action == "install" and prepared_source is None:
        candidate = requested or (_read_manifest(Path(target))["id"] if Path(target).is_dir() else None)
        if candidate and _has_current_installation(root, _name(candidate)):
            raise ValueError("Plugin is already installed; use update or repair")
    # pymss's clone helper also uses TemporaryDirectory; keep it inside this transaction.
    previous_tempdir = tempfile.tempdir
    with _installation_transaction(root, concurrent=action == "install" and prepared_source is None) as work:
        tempfile.tempdir = str(work)
        try:
            _stage(task_id, "Acquiring plugin source", "acquiring")
            subpath = None
            if action == "update" and current and not _uses_default_source(_manifest(current)):
                subpath = _manifest(current).get("subpath")
            with isolate_protocol_stdout():
                with _plugin_registry():
                    result = _acquire_source(target, work / "sources", subpath)
            name = _name(result.name)
            if prepared_source is not None:
                if _read_manifest(result.path)["version"] != prepared_binding.get("version") or _plugin_content_digest(result.path) != prepared_binding["content"]:
                    raise ValueError("Plugin source changed while preparing the runtime; refresh and try again")
                original = prepared_binding["meta"]
                acquired = _manifest(result.path)
                acquired.update({key: value for key, value in original.items() if key not in {"inventory", "inventoryContent", "installed_at"}})
                if not original.get("source"):
                    acquired.update({"source": "recorded", "url": None})
                (result.path / ".pymss-install.json").write_text(json.dumps(acquired), encoding="utf-8")
            require_compatible(_studio_metadata(result.path))
            if requested and (action in {"update", "repair"} or prepared_source is not None) and name != requested:
                raise ValueError(f"The update source provides plugin {name}, not {requested}")
            if action == "install" and prepared_source is None and _has_current_installation(root, name):
                raise ValueError("Plugin is already installed; use update or repair")
            # A repair keeps the original update source rather than the staging-copy path.
            if action == "repair" and current:
                shutil.copy2(current / ".pymss-install.json", result.path / ".pymss-install.json")
            elif current and _manifest(current).get("preinstalled"):
                meta = _manifest(result.path)
                meta["preinstalled"] = True
                (result.path / ".pymss-install.json").write_text(json.dumps(meta), encoding="utf-8")
            shutil.rmtree(result.path / ".studio-deps", ignore_errors=True)
            (result.path / python_profiles.BINDING).unlink(missing_ok=True)
            primary = next((view.get("operation") for view in _studio_views(result.path) if view.get("id") == "tool"), None)
            deferred = primary is not None and primary in _deferred_dependency_operations(result.path)
            isolated_python = python_profiles.declares_python(result.path)
            code_update = action == "update" and not isolated_python and not payload.get("installDependencies", False)
            deferred_install = action == "install" and not isolated_python and deferred and not payload.get("installDependencies", False)
            preserved_dependencies = None
            if code_update:
                _plugin_requirements(result.path)
                _stage(task_id, "Preserving installed plugin dependencies", "preserving")
                if current is not None and (current / ".studio-deps").is_dir():
                    preserved_dependencies = current / ".studio-deps"
            elif deferred_install:
                _plugin_requirements(result.path)
                _stage(task_id, "Optional backend dependencies are managed separately from the plugin files", "preserving")
            else:
                _install_dependencies(result.path, root, task_id, payload)
            with runtime_lock(root / ".lock", timeout=180) if action == "install" and prepared_source is None else nullcontext():
                # A competing installer may have committed this id during acquisition.
                if action == "install" and prepared_source is None and _has_current_installation(root, name):
                    raise ValueError("Plugin is already installed; use update or repair")
                if action == "install":
                    _validate_dependency_publication(result.path, root, name)
                _stage(task_id, "Verifying plugin registration", "verifying")
                if preserved_dependencies is not None:
                    _verify_candidate(root, result.path, name, candidate_dependencies=preserved_dependencies)
                else:
                    _verify_candidate(root, result.path, name)
                _stage(task_id, "Activating plugin", "activating")
                _publish(result.path, root, name, disabled=disabled, preserve_dependencies=preserved_dependencies is not None, intent_revision=intent_revision)
            return name
        finally:
            tempfile.tempdir = previous_tempdir


def _plugin_content_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for parent, subdirs, files in os.walk(directory):
        subdirs[:] = sorted(name for name in subdirs if name not in _SOURCE_IGNORED_DIRS)
        if any((Path(parent) / name).is_symlink() for name in subdirs):
            raise ValueError("Plugin sources with symbolic links cannot be compared")
        for name in sorted(files):
            if name in {".pymss-install.json", ".pymss-install-intent.tmp", ".pymss-python.json", _DEPENDENCY_HANDOFF, ".DS_Store", ".env"} or name.startswith(".env.") or name.endswith((".pyc", ".pyo")):
                continue
            path = Path(parent) / name
            digest.update(path.relative_to(directory).as_posix().encode())
            digest.update(b"\0")
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            digest.update(b"\0")
    return digest.hexdigest()


def _check_updates(root: Path, task_id: str | None) -> dict:
    # Snapshot the manifests under the lock; network work must not hold a writer lease.
    with runtime_lock(root / ".lock"):
        _recover(root)
        manifests = [(plugin.name, _manifest(plugin)) for plugin in [*_installed(root), *_installed(root, disabled=True)]]
    updates = {}
    checks = root / "checks"
    checks.mkdir(parents=True, exist_ok=True)
    previous_tempdir = tempfile.tempdir
    catalog_cache: dict | Exception | None = None
    commit_cache: dict[tuple[str, str | None], str] = {}

    def default_entry(name: str) -> dict:
        nonlocal catalog_cache
        if catalog_cache is None:
            try:
                if _LOCAL_SOURCE.get():
                    items = _local_catalog(_LOCAL_SOURCE.get())
                else:
                    from pymss.plugins.install import list_available
                    items = list_available(_REGISTRY_URL.get())
                catalog_cache = {item["name"]: item for item in items}
            except Exception as error:
                catalog_cache = error
        if isinstance(catalog_cache, Exception):
            raise ValueError(f"Plugin source catalog is unavailable: {catalog_cache}")
        if name not in catalog_cache:
            raise ValueError(f"Plugin source catalog has no entry for: {name}")
        return catalog_cache[name]

    with tempfile.TemporaryDirectory(dir=checks) as temporary:
        tempfile.tempdir = temporary
        try:
            view = Path(temporary) / "installed"
            for name, meta in manifests:
                _stage(task_id, f"Checking {name}", "checking", plugin_name=name)
                try:
                    with _plugin_registry():
                        source = None
                        catalog_version = None
                        catalog_ref = None
                        if _uses_default_source(meta):
                            entry = default_entry(name)
                            if _LOCAL_SOURCE.get():
                                source = _local_package(_LOCAL_SOURCE.get(), name, entry.get("subpath"))
                            else:
                                meta = {**meta, "url": entry["url"], "subpath": entry.get("subpath")}
                                catalog_ref = entry.get("ref")
                                if isinstance(entry.get("version"), str) and entry["version"].strip():
                                    catalog_version = entry["version"]
                        elif meta.get("source") == "path":
                            source = Path(meta["url"])
                            if meta.get("subpath"):
                                source = source / meta["subpath"]
                        if source is not None:
                            if not source.is_dir():
                                raise ValueError(f"Local plugin source folder is unavailable: {source}")
                            remote = _project_version(source)
                            if remote is None:
                                raise ValueError("Local plugin source does not declare a version")
                            changed = _plugin_content_digest(source) != _plugin_content_digest(_location(root, name))
                            update = {"local_version": meta.get("version"), "remote_version": remote, "content_changed": changed,
                                      "up_to_date": remote == meta.get("version") and not changed}
                        elif catalog_version is not None:
                            from pymss.plugins.sources import github_repository, resolve_github_commit
                            remote_commit = None
                            if github_repository(meta["url"]):
                                key = (meta["url"], catalog_ref)
                                if key not in commit_cache:
                                    commit_cache[key] = resolve_github_commit(*key)
                                remote_commit = commit_cache[key]
                            changed = remote_commit is not None and remote_commit != meta.get("resolved_commit")
                            update = {"local_version": meta.get("version"), "remote_version": catalog_version,
                                      "local_commit": meta.get("resolved_commit"), "remote_commit": remote_commit,
                                      "content_changed": changed,
                                      "up_to_date": catalog_version == meta.get("version") and not changed}
                        else:
                            from pymss.plugins.install import check_update
                            directory = view / name
                            directory.mkdir(parents=True)
                            (directory / ".pymss-install.json").write_text(json.dumps(meta), encoding="utf-8")
                            update = check_update(name, plugins_dir=view)
                    if update.get("remote_version") is None:
                        raise ValueError("The update source does not declare a version")
                    updates[name] = update
                except Exception as error:
                    updates[name] = {"error": str(error)}
        finally:
            tempfile.tempdir = previous_tempdir
    return updates


def cmd_manage_plugin(payload: dict[str, Any]) -> int:
    task_id = str(payload.get("taskId") or "").strip() or None
    action = str(payload.get("action") or "").strip()
    try:
        if action not in {"install", "update", "repair", "uninstall", "check_updates", "enable", "disable", "prepare", "prepare_preinstalled", "install_preinstalled"}:
            raise ValueError("Unsupported plugin action")
        if "installDependencies" in payload and type(payload["installDependencies"]) is not bool:
            raise ValueError("installDependencies must be a boolean")
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
        if payload.get("expectedRuntime") is not None and payload["expectedRuntime"] != root.name:
            raise ValueError("Active runtime changed; read the catalog again before acquiring plugins")
        if action in {"install", "update", "repair", "enable", "prepare", "prepare_preinstalled", "install_preinstalled"}:
            _require_plugin_runtime()
        root.mkdir(parents=True, exist_ok=True)
        revision = None
        if action != "check_updates":
            observations, removed, _warnings = _application_bindings(root)
            revision = application_inventory.reserve(root, observations, removed)
        updates = {}
        if action == "check_updates":
            updates = _check_updates(root, task_id)
        elif action == "install":
            _install(root, {**payload, "action": action}, task_id, intent_revision=revision)
        else:
            with runtime_lock(root / ".lock"):
                _recover(root)
                if action == "uninstall":
                    name = _name(payload.get("name"))
                    target = _location(root, name)
                    state = _application_state(root)[0]
                    entry = state["plugins"].get(name)
                    if entry is None:
                        raise ValueError("Plugin is not installed")
                    if entry.get("pending", {}).get("binding", root.name) != root.name:
                        raise ValueError("Switch to the source runtime to finish removing its plugin files")
                    with application_inventory.mutation(root, name, revision):
                        trash = root / "transactions" / f"removed-{name}"
                        _record_intent(root, name, "removed", pending="uninstall" if target.is_dir() or trash.exists() else None, revision=revision)
                        _stage(task_id, "Removing plugin and its additional dependencies", "removing")
                        if name in _bundled():
                            _mark_preinstalled_removed(root, name, True)
                        # Rename under the intent lock; large sidecars are deleted after it.
                        trash.parent.mkdir(parents=True, exist_ok=True)
                        if target.exists() and not trash.exists():
                            target.rename(trash)
                    if trash.exists():
                        shutil.rmtree(trash)
                    if target.exists():
                        # A previous failed removal occupied the trash directory.
                        with application_inventory.mutation(root, name, revision):
                            target.rename(trash)
                        shutil.rmtree(trash)
                    application_inventory.complete(root, name, revision)
                elif action in {"enable", "disable"}:
                    _stage(task_id, "Changing plugin availability", "activating")
                    name = _name(payload.get("name"))
                    target = _location(root, name)
                    entry = _application_state(root)[0]["plugins"].get(name)
                    if entry is None:
                        raise ValueError("Plugin is not installed")
                    if action == "enable":
                        if not target.is_dir() or not _binding_matches(target, entry):
                            _prepare_binding(root, {**payload, "name": name}, task_id, revision)
                        else:
                            _enable_binding(root, name, revision)
                    else:
                        with application_inventory.mutation(root, name, revision):
                            _record_intent(root, name, "disabled", pending="disable" if target.is_dir() else None, revision=revision)
                            if target.is_dir():
                                _set_enabled(root, name, False)
                            application_inventory.complete(root, name, revision)
                elif action == "prepare":
                    _stage(task_id, "Preparing plugin for the selected runtime", "preparing")
                    _prepare_binding(root, payload, task_id, revision)
                elif action in {"prepare_preinstalled", "install_preinstalled"}:
                    name = _name(payload.get("name")) if action == "install_preinstalled" else None
                    if name is None:
                        state, _observations = _application_state(root)
                        if state["migrationWarnings"]:
                            raise ValueError("Plugin removal preferences must be readable before preparing default tools")
                        observations, removed, _warnings = _application_bindings(root)
                        application_inventory.set_default_policy(root, "auto", observations, removed)
                    _prepare_preinstalled(root, task_id, name, intent_revision=revision)
                else:
                    _install(root, {**payload, "action": action}, task_id, intent_revision=revision)
        if action != "check_updates":
            with runtime_lock(root / ".lock"):
                python_profiles.collect_python_profiles(root)
        _stage(task_id, "Refreshing plugin status", "refreshing")
        snapshot = _inventory(root, default_policy=payload.get("defaultPolicy"))
        snapshot["updates"] = updates
        emit("plugin_manage_result", snapshot, task_id=task_id)
        return 0
    except PluginRuntimeIncompatible as error:
        return emit_error("PLUGIN_RUNTIME_INCOMPATIBLE", str(error), task_id=task_id, recoverable=True)
    except PluginCompatibilityError as error:
        return emit_error(error.code, str(error), task_id=task_id, recoverable=True)
    except Exception as error:
        return emit_error("PLUGIN_MANAGE_FAILED", str(error), task_id=task_id, recoverable=True)
