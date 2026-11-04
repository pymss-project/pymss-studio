"""Verify the plugin APIs required by the bundled Studio runtime."""

from __future__ import annotations

from importlib import import_module, metadata
import json


def verify() -> dict:
    required = {
        "pymss.plugins.kit": (
            "current_operation_context", "run_native_process",
            "register_commands", "register_rpc_processes",
        ),
        "pymss_plugin_manager": ("PluginContext", "PluginPolicy", "PluginService"),
        "pymss.plugins.marketplace": ("fetch_official_catalog", "source_info"),
    }
    for name, attributes in required.items():
        module = import_module(name)
        for attribute in attributes:
            if not callable(getattr(module, attribute, None)):
                raise RuntimeError(f"Bundled pymss is missing required plugin API: {name}.{attribute}")
    reviewed_update = getattr(import_module("pymss_plugin_manager._engine"), "UPDATE_REVIEW_VERSION", None)
    if type(reviewed_update) is not int or reviewed_update != 1:
        raise RuntimeError("Bundled pymss is missing required plugin API: pymss_plugin_manager._engine.UPDATE_REVIEW_VERSION=1")
    return {"pymssVersion": metadata.version("pymss"), "pluginApisVerified": True}


if __name__ == "__main__":
    print(json.dumps(verify(), sort_keys=True))
