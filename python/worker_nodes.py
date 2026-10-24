"""List plugin-contributed workflow graph node types (advanced editor palette).

Plugins declare nodes as pure data in their manifest
([[contributions.nodes]]); this surfaces those declarations plus the live
registration state so the frontend renders plugin nodes without any
plugin-side frontend code.
"""

from __future__ import annotations

from typing import Any

from worker_protocol import emit, emit_error


def cmd_list_nodes(payload: dict[str, Any]) -> int:
    try:
        from worker_plugins import _manifest_contributions, _root, operation_runtime

        # Ensure plugin node types are registered in the shared graph registry.
        try:
            operation_runtime()
        except Exception as error:  # noqa: BLE001 — palette still lists declared nodes
            import sys
            print(f"[nodes] plugin runtime unavailable: {error}", file=sys.stderr)

        root = _root()
        nodes: list[dict[str, Any]] = []
        installed = root / "installed"
        if installed.is_dir():
            for plugin in sorted(installed.iterdir()):
                if not plugin.is_dir():
                    continue
                contributions = _manifest_contributions(plugin)
                for decl in contributions.get("nodes", []):
                    decl = dict(decl)
                    decl["pluginId"] = _manifest_contributions_plugin_id(plugin)
                    nodes.append(decl)

        # Live registration state: does the executor exist right now?
        registered: set[str] = set()
        try:
            from pymss.graph.core import _PLUGIN_REGISTRY, _load_builtin_nodes

            _load_builtin_nodes()
            registered = set(_PLUGIN_REGISTRY.nodes.keys())
        except Exception:  # noqa: BLE001
            pass
        for decl in nodes:
            decl["registered"] = decl["type"] in registered

        emit("nodes_list", {"nodes": nodes})
        return 0
    except Exception as error:  # noqa: BLE001
        return emit_error("NODES_LIST_FAILED", str(error))


def _manifest_contributions_plugin_id(plugin) -> str:
    try:
        from pymss.plugins.manifest import read_manifest

        manifest = read_manifest(plugin)
        return manifest.plugin_id if manifest else plugin.name
    except Exception:
        return plugin.name
