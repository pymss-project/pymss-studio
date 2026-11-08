"""Prepare writable bundled-runtime copies with recoverable Python base bindings."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable

_BINDING = "pymss-bootstrap-binding.json"
_BASE_DIRS = {"DLLs", "Lib", "Scripts", "include", "libs", "tcl"}


def _json(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 4 * 1024**2:
        raise ValueError("Runtime seed metadata is too large")
    result = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(result, dict):
        raise ValueError("Runtime seed metadata must be an object")
    return result


def _atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024**2), b""):
            h.update(chunk)
    return h.hexdigest()


def _files(root: Path, *, bootstrap: bool = False) -> list[Path]:
    result = []
    for directory, names, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        names[:] = [name for name in names if name != "__pycache__" and
                    (parent != root or not bootstrap or name in _BASE_DIRS)]
        for name in [*names, *files]:
            child = parent / name
            if child.is_symlink() or not child.resolve().is_relative_to(root):
                raise ValueError("A runtime seed contains a link outside its registered root")
        for name in files:
            child = parent / name
            if name.endswith(".pyc"):
                continue
            if bootstrap and parent == root and child.suffix.lower() not in {".exe", ".dll", ".txt", ".zip", "._pth"}:
                continue
            result.append(child)
            if len(result) > 400000:
                raise ValueError("Runtime seed contains too many files")
    return result


def _copy(source: Path, destination: Path, *, bootstrap: bool = False,
          progress: Callable[[int, int], None] | None = None,
          cancelled: Callable[[], bool] | None = None) -> None:
    files = _files(source, bootstrap=bootstrap)
    total = sum(path.stat().st_size for path in files)
    completed = 0
    previous = 0.0
    for path in files:
        if cancelled and cancelled():
            raise InterruptedError("Runtime seed preparation was cancelled")
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        completed += path.stat().st_size
        now = time.monotonic()
        if progress and (now - previous >= 0.25 or completed == total):
            progress(completed, total)
            previous = now


def _bootstrap_identity(source: Path, generation: Any) -> dict[str, Any]:
    critical = {path.name: _digest(path) for path in source.glob("python*.dll") if path.is_file()}
    executable = source / "python.exe"
    if not executable.is_file() or not critical:
        raise ValueError("The bundled Python base is incomplete")
    critical["python.exe"] = _digest(executable)
    return {"baseGeneration": generation, "criticalFiles": critical}


def ensure_managed_bootstrap(bootstrap_source: Path, managed_root: Path, base_generation: Any,
                             *, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    source = bootstrap_source.resolve(strict=True)
    if source.is_file():
        source = source.parent
    managed = managed_root.resolve()
    identity = _bootstrap_identity(source, base_generation)
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    parent = managed / ".bootstrap"
    parent.mkdir(parents=True, exist_ok=True)
    if not parent.resolve().is_relative_to(managed):
        raise ValueError("The managed bootstrap root escapes its runtime root")
    target = parent / key
    binding = {"schemaVersion": 1, "root": ".bootstrap/" + key, "identity": identity}
    if target.exists():
        _verify_bootstrap(target, identity)
        return binding
    pending = parent / (".pending-" + uuid.uuid4().hex)
    pending.mkdir()
    try:
        _copy(source, pending, bootstrap=True, cancelled=cancelled)
        _atomic(pending / "pymss-bootstrap-state.json", {"schemaVersion": 1, "identity": identity})
        _verify_bootstrap(pending, identity)
        os.replace(pending, target)
    finally:
        if pending.exists():
            shutil.rmtree(pending)
    return binding


def _verify_bootstrap(root: Path, identity: dict[str, Any]) -> None:
    state = _json(root / "pymss-bootstrap-state.json")
    if state.get("identity") != identity or not isinstance(identity.get("criticalFiles"), dict):
        raise ValueError("The managed Python base does not match its generation")
    for name, expected in identity["criticalFiles"].items():
        if (Path(name).name != name or not (root / name).resolve().is_relative_to(root.resolve())
                or _digest(root / name) != expected):
            raise ValueError("The managed Python base failed its integrity check")


def bound_bootstrap_python(env_dir: Path, managed_root: Path, fallback: Path) -> Path:
    path = env_dir / _BINDING
    if not path.is_file():
        return fallback
    binding = _json(path)
    relative = binding.get("root")
    if (binding.get("schemaVersion") != 1 or not isinstance(relative, str) or "\\" in relative
            or len(PurePosixPath(relative).parts) != 2 or not relative.startswith(".bootstrap/")
            or len(PurePosixPath(relative).name) != 64
            or any(ch not in "0123456789abcdef" for ch in PurePosixPath(relative).name)):
        raise ValueError("The runtime has an invalid Python base binding")
    managed = managed_root.resolve()
    root = (managed / relative).resolve(strict=True)
    if not root.is_relative_to(managed):
        raise ValueError("The runtime Python base resolves outside its managed root")
    _verify_bootstrap(root, binding.get("identity") or {})
    return root / "python.exe"


def bind_environment(env_dir: Path, managed_root: Path, binding: dict[str, Any]) -> None:
    _atomic(env_dir / _BINDING, binding)
    python = bound_bootstrap_python(env_dir, managed_root, Path())
    configuration = "\n".join([f"home = {python.parent}", "include-system-site-packages = false",
                                f"executable = {python}", f"command = {python} -m venv {env_dir}", ""])
    temporary = env_dir / ("pyvenv.cfg." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(configuration, encoding="utf-8")
        os.replace(temporary, env_dir / "pyvenv.cfg")
    finally:
        temporary.unlink(missing_ok=True)


def seed_plan(managed_root: Path, bundled_root: Path | None, bootstrap_source: Path,
              manifest: dict[str, Any], *, include_sizes: bool = True) -> dict[str, Any]:
    if bundled_root is None or bundled_root.resolve() == managed_root.resolve():
        return {"available": False}
    source = bundled_root.resolve() / "rocm"
    path = source / "pymss-runtime-state.json"
    if not path.is_file() or not (source / "Scripts/python.exe").is_file():
        return {"available": False}
    state = _json(path)
    if state.get("backend") != "rocm" or not state.get("torchVersion"):
        raise ValueError("The bundled ROCm seed has invalid metadata")
    if not source.resolve().is_relative_to(bundled_root.resolve()):
        raise ValueError("The bundled ROCm seed resolves outside its registered root")
    destination = managed_root / "rocm"
    required = not (destination / "pymss-runtime-state.json").is_file()
    result = {"available": True, "preparationRequired": required, "backend": "rocm", "action": "prepareSeed",
            "profileId": state.get("profileId"), "targets": state.get("installedTargets") or [],
            "downloadBytes": 0, "baseGeneration": manifest.get("baseGeneration", 1),
            "baseFingerprint": state.get("baseFingerprint"), "reason": "bundled_seed_requires_writable_environment"}
    if include_sizes:
        files = _files(source)
        bootstrap = bootstrap_source.parent if bootstrap_source.is_file() else bootstrap_source
        size = sum(path.stat().st_size for path in files)
        base_size = sum(path.stat().st_size for path in _files(bootstrap.resolve(), bootstrap=True))
        result.update({"bytes": size, "bootstrapBytes": base_size,
                       "spaceRequiredBytes": size + base_size + 64 * 1024**2})
    return result


def copy_seed(source_env: Path, destination_env: Path, bootstrap_source: Path, managed_root: Path,
              base_generation: Any, progress: Callable[[int, int], None] | None = None,
              cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    source = source_env.resolve(strict=True)
    destination = destination_env.resolve()
    managed = managed_root.resolve()
    if not destination.is_relative_to(managed) or source == destination or destination.is_relative_to(source):
        raise ValueError("The seed destination must be a distinct managed environment")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("The seed destination must be empty inside its installation transaction")
    destination.mkdir(parents=True, exist_ok=True)
    binding = ensure_managed_bootstrap(bootstrap_source, managed, base_generation, cancelled=cancelled)
    _copy(source, destination, progress=progress, cancelled=cancelled)
    bind_environment(destination, managed, binding)
    state = _json(destination / "pymss-runtime-state.json")
    state["bootstrapBinding"] = binding
    state["seedSource"] = {"profileId": state.get("profileId"), "baseFingerprint": state.get("baseFingerprint")}
    _atomic(destination / "pymss-runtime-state.json", state)
    return state
