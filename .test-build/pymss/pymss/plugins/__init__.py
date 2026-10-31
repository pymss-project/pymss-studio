"""pymss plugin system.

Public API for plugin authors:
    from pymss.plugins import (
        register_capability,  # register a named capability
        register_node,        # register a workflow node
        register_cli,         # register a CLI subcommand
        require_capability,   # look up a capability by name
    )

Internals for pymss core:
    from pymss.plugins import bootstrap, get_plugins_dir, get_registry

See docs/plugins.md for the full design.
"""

from __future__ import annotations

from .loader import (
    DEFAULT_PLUGINS_DIR,
    PluginLoadReport,
    PluginLoadResult,
    bootstrap,
    ensure_plugins_loaded,
    get_last_report,
    get_plugins_dir,
    reset,
)
from .registry import (
    CapabilityNotFound,
    PluginRegistration,
    PluginRegistry,
    get_registry,
    register_capability,
    register_cli,
    register_node,
    require_capability,
)
from .manifest import API_VERSION, ManifestError, OperationDecl, PluginManifest, read_manifest
from .lifecycle import PluginManager, PluginState, satisfies
from . import kit

__all__ = [
    # registration API (plugin authors)
    "register_capability",
    "register_node",
    "register_cli",
    "require_capability",
    "CapabilityNotFound",
    # discovery / loading (pymss core)
    "bootstrap",
    "ensure_plugins_loaded",
    "get_plugins_dir",
    "get_last_report",
    "reset",
    "get_registry",
    "PluginRegistry",
    "PluginLoadReport",
    "PluginLoadResult",
    "DEFAULT_PLUGINS_DIR",
    # plugin protocol v2 (docs/plugin-protocol.md)
    "ManifestError",
    "OperationDecl",
    "PluginManifest",
    "PluginManager",
    "PluginState",
    "read_manifest",
    "satisfies",
    "API_VERSION",
    # plugin author helpers
    "kit",
]
