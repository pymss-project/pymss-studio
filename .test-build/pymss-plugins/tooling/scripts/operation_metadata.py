"""Portable, advisory operation descriptions for pymss plugin API v2."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any

MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_FILES = 5000
MAX_METADATA_BYTES = 2 * 1024 * 1024
_IGNORED_DIRS = {".git", ".studio-deps", "__pycache__", "node_modules", ".venv", "venv", ".tmp", ".cache", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def source_digest(directory: Path) -> str:
    """Match Studio's backend source fingerprint, including normalized text."""
    digest = hashlib.sha256()
    size = count = 0
    if directory.is_symlink():
        raise ValueError("Plugin metadata cannot follow a symbolic link")
    for parent, subdirs, files in os.walk(directory):
        parent = Path(parent)
        subdirs[:] = sorted(name for name in subdirs if name not in _IGNORED_DIRS and (parent != directory or name not in {"ui", "views"}))
        if any((parent / name).is_symlink() for name in subdirs):
            raise ValueError("Plugin metadata cannot follow a symbolic link")
        for name in sorted(files):
            path = parent / name
            if (parent == directory and name == "operations.json") or name in {".pymss-install.json", ".pymss-install-intent.tmp", ".pymss-python.json", ".studio-deps-pending", ".DS_Store", ".env"} or name.startswith(".env.") or path.suffix in {".pyc", ".pyo"}:
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Plugin metadata requires regular source files")
            count += 1
            if count > MAX_SOURCE_FILES or size + info.st_size > MAX_SOURCE_BYTES:
                raise ValueError("Plugin sources exceed metadata limits")
            with path.open("rb") as stream:
                content = stream.read(MAX_SOURCE_BYTES - size + 1)
            size += len(content)
            if size > MAX_SOURCE_BYTES:
                raise ValueError("Plugin sources exceed metadata limits")
            if b"\0" not in content:
                content = content.replace(b"\r\n", b"\n")
            digest.update(path.relative_to(directory).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(content)
            digest.update(b"\0")
    return digest.hexdigest()


def build_snapshot(directory: Path, project: dict[str, Any], descriptions: list[dict[str, Any]]) -> dict[str, Any]:
    plugin = project["tool"]["pymss"]["plugin"]
    if type(plugin.get("api_version")) is not int or plugin["api_version"] != 2:
        raise ValueError("Operation snapshots require plugin API v2")
    declared = [operation["id"] for operation in plugin.get("operations", [])]
    actual = [operation["id"] for operation in descriptions]
    if len(actual) != len(set(actual)) or len(declared) != len(set(declared)) or set(actual) != set(declared):
        raise ValueError(f"Operation descriptions do not match the manifest: {plugin['id']}")
    value = {
        "formatVersion": 1, "pluginApiVersion": 2, "pluginId": plugin["id"],
        "pluginVersion": project["project"]["version"], "sourceDigest": source_digest(directory),
        "operations": sorted(descriptions, key=lambda operation: operation["id"]),
    }
    if len(render_snapshot(value).encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError("Operation descriptions exceed metadata limits")
    return value


def render_snapshot(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def validate_snapshot(directory: Path, project: dict[str, Any]) -> None:
    path = directory / "operations.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_METADATA_BYTES:
        raise ValueError(f"Operation snapshot is missing or invalid: {directory.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or type(value.get("formatVersion")) is not int or value["formatVersion"] != 1:
        raise ValueError(f"Invalid operation snapshot format: {directory.name}")
    descriptions = value.get("operations")
    if not isinstance(descriptions, list):
        raise ValueError(f"Invalid operation snapshot descriptions: {directory.name}")
    expected = build_snapshot(directory, project, descriptions)
    if value != expected:
        raise ValueError(f"Operation snapshot is stale: {directory.name}; regenerate operation metadata")
