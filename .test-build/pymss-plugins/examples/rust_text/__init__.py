"""Register the package's declarative command operations."""
from pathlib import Path


def setup(plugin):
    try:
        from pymss.plugins.kit import register_commands
    except ImportError as error:
        raise RuntimeError("This plugin requires a pymss runtime with the command adapter") from error
    register_commands(plugin, Path(__file__).parent)
