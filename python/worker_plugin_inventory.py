"""Compatibility import for the shared pymss plugin management backend."""
from importlib import import_module


def __getattr__(name):
    return getattr(import_module("pymss_plugin_manager.inventory"), name)
