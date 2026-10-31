from __future__ import annotations

import importlib
import inspect
from typing import Any


def _game_module_path(path: str) -> str:
    package = __package__.rsplit(".", 1)[0]
    if path.startswith("modules."):
        return f"{package}.{path}"
    if path.startswith("game."):
        return package + path[len("game"): ]
    return path


def build_object_from_class_name(
    class_name: str,
    required_parent_class: type | None = None,
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    module_name, object_name = _game_module_path(class_name).rsplit(".", 1)
    module = importlib.import_module(module_name)
    object_type = getattr(module, object_name)
    if required_parent_class is not None and not issubclass(object_type, required_parent_class):
        raise TypeError(f"{class_name} is not a {required_parent_class.__name__}")
    parameters = inspect.signature(object_type).parameters
    if not any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values()):
        kwargs = {
            key: value
            for key, value in kwargs.items()
            if key in parameters
        }
    return object_type(*args, **kwargs)
