"""List plugin-contributed workflow graph node types (advanced editor palette).

Plugins declare nodes as pure data in their manifest
([[contributions.nodes]]); this surfaces those declarations plus the live
registration state so the frontend renders plugin nodes without any
plugin-side frontend code.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from worker_protocol import emit, emit_error


def collect_plugin_nodes() -> list[dict[str, Any]]:
    """Collect declarations and live ownership without emitting protocol events."""
    from pymss.plugins.manifest import read_manifest
    from worker_plugins import _root, operation_runtime, plugin_reports

    # Ensure plugin node types are registered in the shared graph registry.
    reports: dict[Path, dict[str, Any]] = {}
    runtime_error = None
    try:
        runtime = operation_runtime()
        reports = {
            Path(row["path"]).resolve(): row
            for row in plugin_reports(runtime)
            if row.get("managed") and row.get("path")
        }
    except Exception as error:  # noqa: BLE001 — palette still lists declared nodes
        import sys
        print(f"[nodes] plugin runtime unavailable: {error}", file=sys.stderr)
        runtime_error = str(error)

    registered: dict[str, Any] = {}
    try:
        from pymss.graph.core import _PLUGIN_REGISTRY, _load_builtin_nodes

        _load_builtin_nodes()
        registered = _PLUGIN_REGISTRY.nodes
    except Exception as error:  # noqa: BLE001
        runtime_error = runtime_error or str(error)

    root = _root()
    nodes: list[dict[str, Any]] = []
    installed = root / "installed"
    if installed.is_dir():
        for plugin in sorted(installed.iterdir()):
            if not plugin.is_dir():
                continue
            try:
                manifest = read_manifest(plugin)
            except Exception:  # noqa: BLE001 — a broken manifest contributes no nodes
                continue
            report = reports.get(plugin.resolve()) or {}
            for decl in manifest.to_dict()["contributions"]["nodes"]:
                registration = registered.get(decl["type"])
                source = registration.source if registration else None
                reason = runtime_error or report.get("error") or report.get("dependencyError")
                status = report.get("status") or "not_loaded"
                if runtime_error:
                    status = "runtime_unavailable"
                elif report.get("dependencyError"):
                    status = "missing_dependencies"
                elif not report.get("loaded"):
                    reason = reason or f"Plugin {manifest.plugin_id!r} is not loaded"
                elif source != f"plugin:{manifest.plugin_id}":
                    status = "source_mismatch" if source else "unregistered"
                    reason = (
                        f"Node {decl['type']!r} is registered by {source!r}, "
                        f"not plugin {manifest.plugin_id!r}"
                        if source else f"Plugin {manifest.plugin_id!r} did not register node {decl['type']!r}"
                    )
                nodes.append({
                    **decl,
                    "pluginId": manifest.plugin_id,
                    "registered": not bool(reason),
                    "registeredSource": source,
                    "status": status,
                    "unavailableReason": reason,
                    "blockedBy": report.get("blocked_by") or [],
                })
    return nodes


def validate_plugin_node_types(node_types: Iterable[str]) -> None:
    """Reject declared plugin nodes that have no available executor.

    Unknown types are left to the graph loader. A failed duplicate does not
    block an active owner or a live builtin with the same name.
    """
    requested = set(node_types)
    if not requested:
        return
    declarations: dict[str, list[dict[str, Any]]] = {}
    for node in collect_plugin_nodes():
        node_type = node["type"]
        if node_type in requested:
            declarations.setdefault(node_type, []).append(node)
    errors = []
    for node_type, rows in declarations.items():
        if any(row["registered"] or row.get("registeredSource") in {"builtin", "pymss.builtin"} for row in rows):
            continue
        reasons = "; ".join(
            f"{row['pluginId']}: {row.get('unavailableReason') or 'plugin executor is unavailable'}"
            for row in rows
        )
        errors.append(f"{node_type}: {reasons}")
    if errors:
        raise RuntimeError("Workflow plugin nodes are unavailable: " + " | ".join(errors))


def cmd_list_nodes(payload: dict[str, Any]) -> int:
    try:
        emit("nodes_list", {"nodes": collect_plugin_nodes()})
        return 0
    except Exception as error:  # noqa: BLE001
        return emit_error("NODES_LIST_FAILED", str(error))
