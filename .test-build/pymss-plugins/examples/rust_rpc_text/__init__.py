"""Register the package's task-scoped RPC process operations."""
from pathlib import Path


def setup(plugin):
    try:
        from pymss.plugins.kit import register_rpc_processes
    except ImportError as error:
        raise RuntimeError("This plugin requires a pymss runtime with the RPC process adapter") from error
    register_rpc_processes(plugin, Path(__file__).parent)
