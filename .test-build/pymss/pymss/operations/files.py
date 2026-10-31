"""Non-audio file references shared by command operations and their hosts."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import OperationError


@dataclass(frozen=True)
class FileArtifact:
    path: str
    media_type: str = "application/octet-stream"

    @property
    def value(self) -> str:
        """Legacy STRING graph consumers read a file reference as its path."""
        return self.path

    def __str__(self) -> str:
        return self.path

    def __fspath__(self) -> str:
        return self.path

    def to_dict(self) -> dict[str, str]:
        return {"kind": "file", "path": self.path, "mediaType": self.media_type}


def input_file(value: Any) -> Path:
    if isinstance(value, FileArtifact):
        value = value.path
    elif isinstance(value, Mapping) and value.get("kind") == "file":
        value = value.get("path")
    else:
        # StringArtifact is how legacy graph constants carry filesystem paths.
        value = getattr(value, "value", value)
    if not isinstance(value, (str, Path)) or not str(value).strip() or "\0" in str(value):
        raise OperationError("A non-empty input file path is required")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise OperationError("The selected input file is unavailable")
    return path
