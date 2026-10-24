"""Studio plugin marketplace: runtime-owned installs and transactional dependency sidecars.

The pymss installer owns source acquisition. Studio owns the environment selection,
dependency constraints, staged verification, and publication of each installation.
Manually installed ~/.pymss/plugins plugins remain discoverable through the legacy loader.
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
import tomllib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from worker_protocol import emit, emit_error, isolate_protocol_stdout
from worker_runtime_lock import RuntimeLockBusy, runtime_lock

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}\Z")
_PROBE_MARKER = "__STUDIO_PLUGIN_PROBE__"


def _name(value: Any) -> str:
    name = str(value or "").strip()
    if not _NAME.fullmatch(name):
        raise ValueError("Invalid plugin name")
    return name


def _root() -> Path:
    from worker_optional_packages import _runtime_context

    runtime = _runtime_context()
    # Preserve the venv path: resolving its python symlink would merge distinct venvs.
    digest = hashlib.sha256(os.path.normcase(os.path.abspath(sys.executable)).encode()).hexdigest()[:12]
    return runtime.packages_dir.parent.parent / "plugins" / f"{runtime.identity}-{digest}"


@contextmanager
def _plugin_dir(path: Path) -> Iterator[None]:
    previous = os.environ.get("PYMSS_PLUGINS_DIR")
    os.environ["PYMSS_PLUGINS_DIR"] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("PYMSS_PLUGINS_DIR", None)
        else:
            os.environ["PYMSS_PLUGINS_DIR"] = previous


def _recover(root: Path) -> None:
    backups = root / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    for backup in backups.iterdir():
        if not backup.is_dir() or not _NAME.fullmatch(backup.name):
            continue
        target = root / "installed" / backup.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(backup)
        else:
            backup.rename(target)
    transactions = root / "transactions"
    if transactions.exists():
        for transaction in transactions.iterdir():
            if transaction.is_dir() and transaction.name.startswith(("install-", "removed-")):
                shutil.rmtree(transaction, ignore_errors=True)


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


def _installed(root: Path) -> list[Path]:
    directory = root / "installed"
    if not directory.exists():
        return []
    return sorted(p for p in directory.iterdir() if p.is_dir() and _NAME.fullmatch(p.name))


def activate_plugin_dependencies(root: Path | None = None) -> None:
    for plugin in _installed(root or _root()):
        deps = plugin / ".studio-deps"
        if deps.is_dir() and str(deps) not in sys.path:
            # The host wins if a later core update adds a formerly missing package.
            sys.path.append(str(deps))
    importlib.invalidate_caches()


def operation_runtime():
    """Load both existing user plugins and runtime-owned marketplace plugins."""
    with isolate_protocol_stdout():
        return _operation_runtime()


def _operation_runtime():
    from pymss.operations import default_runtime
    from pymss.plugins import ensure_plugins_loaded

    root = _root()
    if not root.exists():
        ensure_plugins_loaded()
        return default_runtime()
    _recover_for_read(root)
    # Rust excludes writers for the lifetime of this worker. Do not hold an
    # exclusive file lock during imports: other operation readers may run too.
    activate_plugin_dependencies(root)
    ensure_plugins_loaded()
    runtime = default_runtime()
    if getattr(runtime, "_studio_root", None) != str(root):
        with _plugin_dir(root / "installed"):
            runtime.load_plugins()
        runtime._studio_root = str(root)
    return runtime


def plugin_reports(runtime, installed_dir: Path | None = None) -> list[dict[str, Any]]:
    from pymss.plugins import get_last_report

    report = get_last_report()
    manual = [
        {"name": item.name, "loaded": item.loaded, "error": item.error,
         "source": item.source, "path": item.path, "managed": False}
        for item in (report.results if report else [])
    ]
    managed = []
    for item in runtime.plugin_report():
        item = {**item, "error": _describe_error(item.get("error")), "managed": True}
        if item.get("loaded"):
            try:
                _validate_requirements((installed_dir or _root() / "installed") / item["name"])
            except Exception as error:
                item.update(loaded=False, error=str(error))
        managed.append(item)
    return manual + managed


def _probe(root: Path, dependency_paths: list[Path] | None = None) -> list[dict[str, Any]]:
    """Plugin imports run in a bounded child, keeping query stdout and state clean."""
    code = f"""
import json, sys
from pathlib import Path
import worker_plugins as p
from worker_protocol import isolate_protocol_stdout
with isolate_protocol_stdout():
    p.activate_plugin_dependencies(Path(sys.argv[1]))
    sys.path.extend(json.loads(sys.argv[2]))
    from pymss.operations import default_runtime
    from pymss.plugins import ensure_plugins_loaded
    ensure_plugins_loaded()
    runtime = default_runtime()
    with p._plugin_dir(Path(sys.argv[1]) / "installed"):
        reports = runtime.load_plugins()
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


def _describe_error(error: str | None) -> str | None:
    if not error:
        return None
    return re.sub(
        r"already registered by [\'\"]_pymss_(runtime_)?plugin_([^\'\"]+)[\'\"]",
        lambda match: "already provided by " + ("marketplace plugin " if match[1] else "manually installed plugin ") + match[2],
        error,
    )


def _validate_requirements(plugin: Path) -> None:
    from pip._vendor.packaging.requirements import Requirement
    path = plugin / "pyproject.toml"
    if not path.exists():
        return
    project = tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})
    for text in project.get("dependencies", []):
        requirement = Requirement(text)
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        try:
            version = importlib.metadata.version(requirement.name)
        except importlib.metadata.PackageNotFoundError:
            raise ValueError(f"Missing plugin dependency: {requirement.name}") from None
        if requirement.specifier and version not in requirement.specifier:
            raise ValueError(f"Plugin requires {requirement}; current runtime provides {version}")


def validate_operation(runtime, operation: str) -> None:
    for item in runtime.plugin_report():
        if operation in item.get("operations", []):
            _validate_requirements(_root() / "installed" / item["name"])


def _project_version(directory: Path) -> str | None:
    project = directory / "pyproject.toml"
    if not project.exists():
        return None
    return tomllib.loads(project.read_text(encoding="utf-8")).get("project", {}).get("version")


def _manual_plugin_fallback() -> list[dict]:
    from pymss.plugins.loader import get_plugins_dir
    rows = []
    directory = get_plugins_dir()
    try:
        for child in sorted(directory.iterdir()) if directory.exists() else []:
            if child.is_dir() and not child.name.startswith((".", "_")):
                entry = next((child / name for name in ("__init__.py", "plugin.py") if (child / name).exists()), child / "__init__.py")
                rows.append({"name": child.name, "managed": False, "loaded": None, "path": str(entry)})
    except OSError:
        pass
    for entry in importlib.metadata.entry_points(group="pymss.plugins"):
        rows.append({"name": entry.name, "managed": False, "loaded": None, "source": "entrypoint"})
    return rows


def _manifest_contributions(plugin: Path) -> dict[str, Any]:
    """Views/declared contributions from the pymss protocol manifest, if any.

    Studio consumes these to offer plugin-provided pages; graphs and player
    entries are surfaced for diagnostics even before their Studio UI lands.
    """
    try:
        from pymss.plugins.manifest import read_manifest
    except ImportError:
        return {}
    try:
        manifest = read_manifest(plugin)
    except Exception:
        return {}
    if manifest is None:
        return {}
    contributions = manifest.to_dict()["contributions"]
    for entry in contributions.get("views", []):
        entry["pluginId"] = manifest.plugin_id
        entry["entryPath"] = str((plugin / entry["entry"]).resolve())
    return contributions


def _inventory(root: Path, *, verify: bool = True) -> dict[str, Any]:
    _recover_for_read(root)
    installed = _installed(root)
    # Queries can overlap with other operation readers. Only recovery needs the lock.
    report_list: list[dict] = []
    verification_error = None
    if verify:
        try:
            report_list = _probe(root)
        except Exception as error:
            verification_error = str(error)
    reports = {item["name"]: item for item in report_list if item.get("managed")}
    rows = []
    for plugin in installed:
        try:
            meta, row_error = _manifest(plugin), None
        except Exception as error:
            meta, row_error = {}, str(error)
        report = reports.get(plugin.name, {})
        rows.append({
            "name": plugin.name, "version": meta.get("version"), "managed": True,
            "source": meta.get("source", "unknown"), "url": meta.get("url"),
            "path": str(plugin), "loaded": report.get("loaded"),
            "error": _describe_error(report.get("error")) or row_error,
            "operations": report.get("operations", []),
            "contributions": _manifest_contributions(plugin) if row_error is None else {},
        })
    if verification_error:
        report_list.extend(_manual_plugin_fallback())
    for report in report_list:
        if report.get("managed"):
            continue
        path = report.get("path")
        try:
            version = _project_version(Path(path).parent) if path else None
        except Exception:
            version = None
        rows.append({
            "name": report["name"], "version": version, "managed": False,
            "source": "manual", "url": None, "path": path or report.get("source", ""),
            "loaded": report.get("loaded"), "error": _describe_error(report.get("error")),
            "operations": None,
        })
    return {
        "installed": rows, "pluginsDir": str(root / "installed"),
        "verificationError": verification_error,
        "runtime": {"identity": root.name, "pythonPath": sys.executable},
    }


def cmd_plugin_marketplace(payload: dict[str, Any]) -> int:
    try:
        from pymss.plugins.install import list_available

        snapshot = _inventory(_root())
        try:
            snapshot["catalog"] = [
                {"name": _name(item["name"]), "url": str(item.get("url") or ""),
                 "subpath": item.get("subpath"), "description": str(item.get("description") or ""),
                 "tags": [str(tag) for tag in item.get("tags", [])]}
                for item in list_available()
            ]
            snapshot["catalogError"] = None
        except Exception as error:
            snapshot["catalog"], snapshot["catalogError"] = [], str(error)
        emit("plugin_marketplace", snapshot)
        return 0
    except Exception as error:
        return emit_error("PLUGIN_MARKETPLACE_FAILED", str(error), recoverable=True)


def _stage(task_id: str | None, message: str) -> None:
    emit("plugin_stage", {"message": message}, task_id=task_id)


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


def _install_dependencies(plugin: Path, root: Path, task_id: str | None, payload: dict) -> None:
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
    if not requirements:
        return
    # Resolve against the selected interpreter, freezing host packages. Other installed
    # sidecars constrain versions but are not borrowed: uninstalling one cannot break another.
    versions: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            versions[_canonical(name)] = distribution.version
    for other in _installed(root):
        if other.name == plugin.name:
            continue
        for distribution in importlib.metadata.distributions(path=[str(other / ".studio-deps")]):
            name = _canonical(distribution.metadata["Name"])
            if name in versions and versions[name] != distribution.version:
                raise ValueError(f"Installed plugin dependency conflict: {name}")
            versions[name] = distribution.version
    constraints = plugin.parent / "constraints.txt"
    constraints.write_text("".join(f"{name}=={version}\n" for name, version in sorted(versions.items())), encoding="utf-8")
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
    _stage(task_id, "Resolving plugin dependencies against the active runtime")
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
        _stage(task_id, "Installing additional dependencies")
        _run_process(base + ["--no-deps", "--target", str(plugin / ".studio-deps"), *index_args, *selected], task_id)


def _publish(plugin: Path, root: Path, name: str) -> None:
    target = root / "installed" / name
    backup = root / "backups" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    backup.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.rename(backup)
    try:
        plugin.rename(target)
    except BaseException:
        if backup.exists():
            backup.rename(target)
        raise
    if backup.exists():
        # Publication has committed. A cleanup failure is retried on the next query.
        shutil.rmtree(backup, ignore_errors=True)


def _acquire_source(target: str, directory: Path, subpath: str | None = None):
    from datetime import datetime, timezone
    from pymss.plugins.install import InstallResult, install

    local = Path(target).expanduser()
    if not local.is_dir():
        remote = target.startswith(("http://", "https://", "git@", "ssh://"))
        if not remote and (local.is_absolute() or target.startswith((".", "~")) or "/" in target or "\\" in target):
            raise ValueError(f"Local plugin source folder is unavailable: {local}")
        kwargs = {}
        if target.startswith(("git@", "ssh://")):
            from urllib.parse import urlsplit
            body, separator, fragment = target.partition("#")
            has_ref = body.count("@") > 1 if body.startswith("git@") else "@" in urlsplit(body).path
            if has_ref:
                body, ref = body.rsplit("@", 1)
                target = body + (separator + fragment if separator else "")
                kwargs["ref"] = ref
            else:
                kwargs["ref"] = ""
        return install(target, plugins_dir=directory, subpath=subpath, no_deps=True, **kwargs)
    source = local / subpath if subpath else local
    if not source.is_dir():
        raise ValueError(f"Local plugin source folder is unavailable: {source}")
    name = _name(source.name)
    destination = directory / name
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".studio-deps", "__pycache__", ".git"))
    version = _project_version(destination)
    # Preserve the pymss manifest format without its CLI name@ref parsing of local paths.
    (destination / ".pymss-install.json").write_text(json.dumps({
        "name": name, "version": version, "source": "path", "url": str(local.resolve()),
        "subpath": subpath, "ref": None, "installed_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    return InstallResult(name=name, path=destination, source="path", version=version)


def _install(root: Path, payload: dict, task_id: str | None) -> str:

    action = payload["action"]
    requested = _name(payload["name"]) if payload.get("name") else None
    current = root / "installed" / requested if requested else None
    if action in {"update", "repair"}:
        if current is None or not current.is_dir():
            raise ValueError("Plugin is not installed in this runtime")
        meta = _manifest(current)
        if action == "repair":
            target = str(current)
        else:
            target = str(meta.get("url") or "")
            if not target:
                raise ValueError("Plugin has no update source; install it again from its source")
            if meta.get("source") == "path" and not Path(target).is_dir():
                raise ValueError(f"Local plugin source folder is unavailable: {target}")
    else:
        target = str(payload.get("target") or requested or "").strip()
        if not target:
            raise ValueError("Missing plugin source")
    if action == "install":
        candidate = requested or (Path(target).name if Path(target).is_dir() else None)
        if candidate and (root / "installed" / _name(candidate)).exists():
            raise ValueError("Plugin is already installed; use update or repair")
    transactions = root / "transactions"
    transactions.mkdir(parents=True, exist_ok=True)
    # pymss's clone helper also uses TemporaryDirectory; keep it inside this transaction.
    previous_tempdir = tempfile.tempdir
    with tempfile.TemporaryDirectory(dir=transactions, prefix="install-") as transaction:
        work = Path(transaction)
        tempfile.tempdir = transaction
        try:
            _stage(task_id, "Acquiring plugin source")
            subpath = _manifest(current).get("subpath") if action == "update" and current else None
            with isolate_protocol_stdout():
                result = _acquire_source(target, work / "sources", subpath)
            name = _name(result.name)
            if requested and action in {"update", "repair"} and name != requested:
                # A local source folder may have been renamed after its first installation.
                destination = result.path.with_name(requested)
                result.path.rename(destination)
                result.path = destination
                name = requested
            installed = root / "installed" / name
            if action == "install" and installed.exists():
                raise ValueError("Plugin is already installed; use update or repair")
            # A repair keeps the original update source rather than the staging-copy path.
            if action == "repair" and current:
                shutil.copy2(current / ".pymss-install.json", result.path / ".pymss-install.json")
            shutil.rmtree(result.path / ".studio-deps", ignore_errors=True)
            _install_dependencies(result.path, root, task_id, payload)
            _stage(task_id, "Verifying plugin registration")
            # Probe the staged plugin with all existing peers, excluding its old version.
            probe_root = work / "probe"
            probe_dir = probe_root / "installed"
            probe_dir.mkdir(parents=True)
            dependencies = [result.path / ".studio-deps"]
            peers = _installed(root)
            previous_failures = {item["name"] for item in _probe(root) if item.get("managed") and not item.get("loaded")} if peers else set()
            for peer in peers:
                if peer.name != name:
                    shutil.copytree(peer, probe_dir / peer.name, ignore=shutil.ignore_patterns(".studio-deps"))
                    dependencies.append(peer / ".studio-deps")
            shutil.copytree(result.path, probe_dir / name, ignore=shutil.ignore_patterns(".studio-deps"))
            reports = _probe(probe_root, dependencies)
            failed = [item for item in reports if item.get("managed") and not item.get("loaded") and (item["name"] == name or item["name"] not in previous_failures)]
            if failed:
                raise RuntimeError("; ".join(f"{item['name']}: {item.get('error')}" for item in failed))
            _stage(task_id, "Activating plugin")
            _publish(result.path, root, name)
            return name
        finally:
            tempfile.tempdir = previous_tempdir


def _check_updates(root: Path, task_id: str | None) -> dict:
    from pymss.plugins.install import check_update

    # Snapshot the manifests under the lock; network work must not hold a writer lease.
    with runtime_lock(root / ".lock"):
        _recover(root)
        manifests = [(plugin.name, _manifest(plugin)) for plugin in _installed(root)]
    updates = {}
    checks = root / "checks"
    checks.mkdir(parents=True, exist_ok=True)
    previous_tempdir = tempfile.tempdir
    with tempfile.TemporaryDirectory(dir=checks) as temporary:
        tempfile.tempdir = temporary
        try:
            view = Path(temporary) / "installed"
            for name, meta in manifests:
                _stage(task_id, f"Checking {name}")
                try:
                    if meta.get("source") == "path":
                        source = Path(meta["url"])
                        if meta.get("subpath"):
                            source = source / meta["subpath"]
                        if not source.is_dir():
                            raise ValueError(f"Local plugin source folder is unavailable: {source}")
                        remote = _project_version(source)
                        if remote is None:
                            raise ValueError("Local plugin source does not declare a version")
                        update = {"local_version": meta.get("version"), "remote_version": remote,
                                  "up_to_date": remote == meta.get("version")}
                    else:
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
        if action not in {"install", "update", "repair", "uninstall", "check_updates"}:
            raise ValueError("Unsupported plugin action")
        root = _root()
        root.mkdir(parents=True, exist_ok=True)
        updates = {}
        if action == "check_updates":
            updates = _check_updates(root, task_id)
        else:
            with runtime_lock(root / ".lock"):
                _recover(root)
                if action == "uninstall":
                    name = _name(payload.get("name"))
                    target = root / "installed" / name
                    if not target.is_dir():
                        raise ValueError("Plugin is not installed in this runtime")
                    _stage(task_id, "Removing plugin and its additional dependencies")
                    # Rename first so interrupted cleanup never leaves a half-loaded plugin.
                    trash = root / "transactions" / f"removed-{name}"
                    trash.parent.mkdir(parents=True, exist_ok=True)
                    shutil.rmtree(trash, ignore_errors=True)
                    target.rename(trash)
                    shutil.rmtree(trash)
                else:
                    _install(root, {**payload, "action": action}, task_id)
        snapshot = _inventory(root)
        snapshot["updates"] = updates
        emit("plugin_manage_result", snapshot, task_id=task_id)
        return 0
    except Exception as error:
        return emit_error("PLUGIN_MANAGE_FAILED", str(error), task_id=task_id, recoverable=True)
