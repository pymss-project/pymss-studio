"""Register isolated Python RPC operations through the v2 compatibility entry."""
from pathlib import Path


def setup(plugin):
    try:
        from pymss.operations.python_profiles import read_python_plan
        from pymss.plugins.kit import register_rpc_processes
    except ImportError as error:
        raise RuntimeError("This plugin requires a pymss runtime with isolated Python process support") from error
    read_python_plan(Path(__file__).parent)
    register_rpc_processes(plugin, Path(__file__).parent)
