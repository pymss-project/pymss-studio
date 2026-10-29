from __future__ import annotations

import json
from pathlib import Path
from typing import Any


MODEL_WEIGHT_SUFFIXES = {
    ".bin", ".ckpt", ".onnx", ".pb", ".pt", ".pth", ".safetensors",
}
MODEL_CONFIGURATION_NAMES = {
    "configuration.json", "config.json", "config.yaml", "config.yml",
}
MINIMUM_WEIGHT_BYTES = 1024
_GIT_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"


def unresolved_incomplete_files(model_dir: Path) -> list[Path]:
    if not model_dir.is_dir():
        return []
    unresolved = []
    try:
        for path in model_dir.rglob("*.incomplete"):
            if not path.with_suffix("").is_file():
                unresolved.append(path)
    except OSError:
        return [model_dir]
    return unresolved


def _is_usable_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _is_usable_weight(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < MINIMUM_WEIGHT_BYTES:
            return False
        with path.open("rb") as handle:
            return handle.read(len(_GIT_LFS_POINTER_PREFIX)) != _GIT_LFS_POINTER_PREFIX
    except OSError:
        return False


def _declared_model_files(configuration_path: Path) -> list[Path] | None:
    if configuration_path.name.lower() != "configuration.json":
        return []
    try:
        configuration = json.loads(configuration_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    file_metas = configuration.get("file_path_metas")
    if file_metas is None:
        return []
    if not isinstance(file_metas, dict):
        return None

    values: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, dict):
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(file_metas)
    declared = []
    for value in values:
        relative = Path(value.strip())
        if not value.strip() or relative.is_absolute() or ".." in relative.parts:
            return None
        declared.append(configuration_path.parent / relative)
    return declared


def is_complete_asr_model_cache(model_dir: Path) -> bool:
    """Validate one managed ModelScope cache without importing or loading the model."""

    if not model_dir.is_dir() or unresolved_incomplete_files(model_dir):
        return False
    try:
        configurations = [
            path for path in model_dir.rglob("*")
            if path.is_file() and path.name.lower() in MODEL_CONFIGURATION_NAMES
        ]
    except OSError:
        return False
    if not configurations:
        return False

    snapshot_dirs = {path.parent for path in configurations}
    for snapshot_dir in snapshot_dirs:
        snapshot_configs = [path for path in configurations if path.parent == snapshot_dir]
        if not all(_is_usable_file(path) for path in snapshot_configs):
            continue
        weights = [
            path for path in snapshot_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in MODEL_WEIGHT_SUFFIXES
        ]
        if not weights or not all(_is_usable_weight(path) for path in weights):
            continue

        configuration_json = next(
            (path for path in snapshot_configs if path.name.lower() == "configuration.json"),
            None,
        )
        if configuration_json is not None:
            declared = _declared_model_files(configuration_json)
            if declared is None or any(not _is_usable_file(path) for path in declared):
                continue
        return True
    return False
