"""Keep historical model imports bound to their canonical pymss_core modules."""

from importlib import import_module
from importlib.abc import Loader, MetaPathFinder
from importlib.util import find_spec, spec_from_loader
import sys

_LOCAL_MODULE_PREFIX = "pymss.modules."
_CORE_MODULE_PREFIX = "pymss_core.modules."


class _CoreAliasLoader(Loader):
    def __init__(self, core_name):
        self.core_name = core_name

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        # Replace the placeholder so importlib never rewrites the canonical
        # module's __spec__, __loader__, or __package__ with alias metadata.
        sys.modules[module.__name__] = import_module(self.core_name)


class _CoreAliasFinder(MetaPathFinder):
    def __init__(self):
        self.packages = {}

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(_LOCAL_MODULE_PREFIX):
            return None
        for local_name in sorted(self.packages, key=len, reverse=True):
            if fullname.startswith(local_name + "."):
                core_name = self.packages[local_name] + fullname[len(local_name):]
                core_spec = find_spec(core_name)
                if core_spec is None:
                    return None
                return spec_from_loader(
                    fullname,
                    _CoreAliasLoader(core_name),
                    is_package=core_spec.submodule_search_locations is not None,
                )
        return None


_alias_finder = _CoreAliasFinder()


def alias_module(local_name, core_name):
    if not local_name.startswith(_LOCAL_MODULE_PREFIX):
        raise ValueError(f"invalid local module alias: {local_name}")
    if not core_name.startswith(_CORE_MODULE_PREFIX):
        raise ValueError(f"invalid core module alias: {core_name}")
    module = import_module(core_name)
    sys.modules[local_name] = module
    parent_name, _, child_name = local_name.rpartition(".")
    parent = sys.modules.get(parent_name)
    if parent is not None:
        setattr(parent, child_name, module)
    if hasattr(module, "__path__"):
        # Future children must use the same mapping, or Python would execute
        # their source again under the legacy package's shared __path__.
        _alias_finder.packages[local_name] = core_name
        if _alias_finder not in sys.meta_path:
            sys.meta_path.insert(0, _alias_finder)
    return module


def alias_submodules(local_package, core_package, names):
    for name in names:
        alias_module(f"{local_package}.{name}", f"{core_package}.{name}")
