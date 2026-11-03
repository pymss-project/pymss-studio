"""Local plugin observations, with loading performed only on explicit request."""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.metadata
import os
from pathlib import Path
import sys
from typing import Any

from worker_protocol import emit, emit_error


def _python_identity(value: str) -> str:
    path = Path(value).absolute()
    # Keep the executable link so separate venvs sharing a base stay distinct.
    normalized = str(path.parent.resolve() / path.name)
    if os.name == "nt":
        normalized = normalized.removeprefix("\\\\?\\")
    return os.path.normcase(normalized)


def _apply_committed_intents(plugins, root: Path, rows: list[dict]) -> list[dict]:
    """Read the core's committed intents without inventory reconciliation."""
    from pymss_plugin_manager import inventory

    with plugins._management(root):
        entries = inventory._load(root)["plugins"]
        engine = plugins._manager_engine()
        observed = {row["name"]: row for row in rows}
        for name, intent in entries.items():
            if intent["state"] == "removed":
                observed.pop(name, None)
                continue
            row = observed.get(name)
            if row is None:
                row = {"name": name, "version": intent.get("version"), "path": "",
                       "loaded": None, "error": "Plugin is not prepared for this runtime"}
                observed[name] = row
            elif not engine._binding_matches(Path(row["path"]), intent):
                row["error"] = "Plugin does not match the committed installation"
            row["enabled"] = intent["state"] == "enabled"
        return list(observed.values())


def plugin_diagnostics(*, verify: bool = False) -> dict[str, Any]:
    from worker_bootstrap import _read_runtime_state
    import worker_plugins as plugins

    root = plugins._root()
    state = _read_runtime_state() or {}
    configured = state.get("pythonPath")
    interpreter_matches = _python_identity(configured) == _python_identity(sys.executable) if configured else None
    try:
        core_version = importlib.metadata.version("pymss")
    except importlib.metadata.PackageNotFoundError:
        core_version = None
    api_error = None
    binding_error = None
    management_supported = plugins._management_supported()
    try:
        plugins._require_plugin_runtime()
    except plugins.PluginRuntimeIncompatible as error:
        api_error = str(error)
        reason = error.__cause__ or error.__context__
        if reason:
            api_error += f" ({type(reason).__name__}: {reason})"

    observed = plugins._readonly_inventory(root)
    rows = [{key: row.get(key) for key in ("name", "version", "enabled", "path", "loaded", "error")}
            for row in observed["installed"]]
    known_paths = {row["path"] for row in rows}
    for enabled, directory in ((True, root / "installed"), (False, root / "disabled")):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.name.startswith(".") or not path.is_dir() or str(path) in known_paths:
                continue
            rows.append({"name": path.name, "version": None, "enabled": enabled, "path": str(path),
                         "loaded": None, "error": "Plugin manifest is invalid or unavailable"})

    if management_supported:
        rows = _apply_committed_intents(plugins, root, rows)
        try:
            plugins._service(root)._identity()
        except (OSError, ValueError, TypeError) as error:
            binding_error = str(error)

    verification_error = None
    if verify:
        if binding_error:
            verification_error = binding_error
        elif interpreter_matches is False:
            verification_error = "The worker interpreter differs from the configured runtime"
        elif api_error:
            verification_error = api_error
        else:
            try:
                reports = {row.get("name"): row for row in plugins._probe(root) if row.get("managed")}
                for row in rows:
                    if not row["enabled"] or row["error"]:
                        continue
                    report = reports.get(row["name"])
                    if report is not None:
                        row["loaded"] = report.get("loaded") is True
                        row["error"] = report.get("error") or report.get("dependencyError")
                    else:
                        row["error"] = "Plugin loading did not return a verification result"
            except Exception as error:
                verification_error = str(error)

    return {
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "runtime": {
            "pythonPath": sys.executable, "configuredPythonPath": configured,
            "interpreterMatches": interpreter_matches,
            "identity": root.name, "bindingDir": str(root), "pymssVersion": core_version,
            "managementSupported": management_supported, "bindingError": binding_error,
            "pluginApisSupported": api_error is None, "apiError": api_error,
        },
        "plugins": rows,
        "verification": {"performed": verify, "error": verification_error},
    }


def cmd_plugin_diagnostics(payload: dict[str, Any]) -> int:
    try:
        verify = payload.get("verify", False)
        if type(verify) is not bool:
            raise ValueError("verify must be a boolean")
        emit("plugin_diagnostics", plugin_diagnostics(verify=verify))
        return 0
    except Exception as error:
        return emit_error("PLUGIN_DIAGNOSTICS_FAILED", str(error), recoverable=True)
