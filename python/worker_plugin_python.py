"""Compatibility import for the shared pymss plugin management backend."""
from importlib import import_module


def prepare_python(plugin, root, progress):
    from worker_plugins import _management
    with _management(root):
        return import_module("pymss_plugin_manager.profiles").prepare_python(plugin, root, progress)


def collect_python_profiles(root):
    from worker_plugins import _management
    with _management(root):
        return import_module("pymss_plugin_manager.profiles").collect_python_profiles(root)


def __getattr__(name):
    return getattr(import_module("pymss_plugin_manager.profiles"), name)
