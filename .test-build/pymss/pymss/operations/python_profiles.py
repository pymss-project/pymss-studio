"""Prepared Python environments for existing command and RPC adapters.

Profiles are created at their final location. Only a small binding is published
with the plugin; moving a plugin never relocates a virtual environment.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import sysconfig
import tomllib
import uuid
from typing import Callable

from . import OperationContext, OperationError
from ._command_process import run_command_process
from ._profile_lock import profile_lock

BINDING = ".pymss-python.json"
_MARKER = ".pymss-profile.json"
_PROFILE = re.compile(r"python-[a-f0-9]{32}\Z")
_TARGET = re.compile(r"(?:win32|darwin|linux)-(?:x86_64|arm64)\Z")


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _pairs(items):
    result = {}
    for name, value in items:
        if name in result:
            raise OperationError("Duplicate Python environment metadata field")
        result[name] = value
    return result


def _json(path: Path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
        raise OperationError("Python environment metadata is unavailable or too large")
    try:
        return json.loads(path.read_bytes(), object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
    except (ValueError, UnicodeError) as error:
        raise OperationError("Invalid Python environment metadata") from error


def package_file(root: Path, value: str) -> Path:
    from .command import _relative_file

    relative = _relative_file(value)
    if "\r" in value or "\n" in value:
        raise OperationError("Invalid Python package path")
    path = root.joinpath(*relative.parts)
    current = path
    while current != root:
        if current.is_symlink():
            raise OperationError("Python package files cannot follow symbolic links")
        current = current.parent
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise OperationError("A declared Python package file is missing")
    return path


@dataclass(frozen=True)
class PythonPlan:
    root: Path
    digest: str
    wheels: tuple[tuple[Path, str], ...]
    files: tuple[Path, ...]


def _runtime_files(root: Path):
    ignored = {".git", ".cache", ".studio-deps", "__pycache__", ".venv", "venv", "node_modules",
               ".tmp", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    count = size = 0
    for parent, directories, names in os.walk(root):
        parent = Path(parent)
        directories[:] = sorted(name for name in directories if name not in ignored
                                and (parent != root or name not in {"ui", "views"}))
        if any((parent / name).is_symlink() for name in directories):
            raise OperationError("Python runtime resources cannot follow symbolic links")
        for name in sorted(names):
            path = parent / name
            if (name.startswith((".pymss-", ".env")) or name == ".DS_Store" or (parent == root and name == "operations.json")
                    or path.suffix in {".whl", ".pyc", ".pyo"}):
                continue
            if path.is_symlink() or not path.is_file():
                raise OperationError("Python runtime resources must be regular package files")
            size += path.stat().st_size
            count += 1
            if size > 256 * 1024 * 1024 or count > 10000:
                raise OperationError("Python runtime resources exceed their size or file count limit")
            yield path


def read_python_plan(directory: str | Path) -> PythonPlan | None:
    from .command import current_target

    root = Path(directory).resolve()
    manifest = root / "pyproject.toml"
    if not manifest.is_file() or manifest.stat().st_size > 2 * 1024 * 1024:
        raise OperationError("Python plugin manifest is unavailable or too large")
    project = tomllib.loads(manifest.read_text(encoding="utf-8"))
    config = project.get("tool", {}).get("pymss", {}).get("python")
    if config is None:
        return None
    if not isinstance(config, dict) or set(config) != {"format_version", "lock"} or type(config["format_version"]) is not int or config["format_version"] != 1:
        raise OperationError("Unsupported Python environment declaration")
    lock = _json(package_file(root, config["lock"]))
    if (not isinstance(lock, dict) or set(lock) != {"formatVersion", "requiresPython", "targets"}
            or type(lock["formatVersion"]) is not int or lock["formatVersion"] != 1
            or not isinstance(lock["requiresPython"], str) or len(lock["requiresPython"]) > 200):
        raise OperationError("Unsupported Python dependency lock")
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version
    except ImportError:
        try:
            from pip._vendor.packaging.specifiers import SpecifierSet
            from pip._vendor.packaging.version import Version
        except ImportError as error:
            raise OperationError("Python environment version checks require packaging support in the host") from error
    try:
        compatible = Version(".".join(map(str, sys.version_info[:3]))) in SpecifierSet(lock["requiresPython"])
    except ValueError as error:
        raise OperationError("Invalid Python version constraint") from error
    if not compatible:
        raise OperationError("The active Python version does not satisfy the plugin dependency lock")
    targets = lock["targets"]
    if not isinstance(targets, dict) or not targets or len(targets) > 7 or any(key != "any" and not _TARGET.fullmatch(key) for key in targets):
        raise OperationError("Invalid Python dependency lock targets")
    wheels = targets.get(current_target(), targets.get("any"))
    if not isinstance(wheels, list) or len(wheels) > 256:
        raise OperationError("The Python dependency lock does not support this platform")
    selected, seen = [], set()
    for wheel in wheels:
        if (not isinstance(wheel, dict) or set(wheel) != {"path", "sha256"}
                or not isinstance(wheel["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", wheel["sha256"])):
            raise OperationError("Python dependencies require local wheels with SHA-256 hashes")
        path = package_file(root, wheel["path"])
        if path.suffix != ".whl" or path.name in seen:
            raise OperationError("Python dependencies must be unique wheel files")
        seen.add(path.name)
        selected.append((path, wheel["sha256"]))
    base = Path(getattr(sys, "_base_executable", None) or sys.executable).resolve()
    stamp = base.stat()
    identity = {"config": config, "lock": lock, "target": current_target(), "python": sys.version,
                "cacheTag": sys.implementation.cache_tag, "platform": sysconfig.get_platform(),
                "base": str(base), "size": stamp.st_size, "mtime": stamp.st_mtime_ns}
    files, digest = tuple(_runtime_files(root)), hashlib.sha256(_canonical(identity))
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\0")
    return PythonPlan(root, digest.hexdigest(), tuple(selected), files)


def _interpreter(profile: Path) -> Path:
    return profile / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _write_json(path: Path, value) -> None:
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(_canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_python_environment(directory: str | Path, profiles: str | Path, *,
                               context: OperationContext | None = None,
                               on_created: Callable[[Path], None] | None = None) -> Path:
    """Prepare a fresh offline wheel-only venv, then atomically bind the plugin.

    The caller owns the profile store and its publication transaction. An
    existing binding stays intact until atomic publication. Interruptions after
    publication retain the committed profile. Superseded profiles are retained
    until their owner retires them with ``remove_python_environment``.
    """
    plan = read_python_plan(directory)
    if plan is None:
        raise OperationError("The plugin does not declare a Python environment")
    context = context or OperationContext()
    context.check_cancelled()
    for wheel, expected in plan.wheels:
        if wheel.stat().st_size > 8 * 1024 ** 3:
            raise OperationError("Python dependency wheel exceeds its size limit")
        digest = hashlib.sha256()
        with wheel.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                context.check_cancelled()
                digest.update(block)
        if digest.hexdigest() != expected:
            raise OperationError("Python dependency wheel hash does not match its lock")
    store = Path(profiles).expanduser().resolve()
    if store.is_relative_to(plan.root):
        raise OperationError("Python profiles must be stored outside the plugin package")
    store.mkdir(parents=True, exist_ok=True)
    profile = store / ("python-" + uuid.uuid4().hex)
    creating = store / (".creating-" + profile.name)
    marker = {"formatVersion": 1, "planDigest": plan.digest, "profile": str(profile)}
    try:
        with profile_lock(profile, exclusive=True):
            creating.mkdir()
            _write_json(creating / _MARKER, marker)
            (creating / ".preparing").touch()
            creating.rename(profile)
            if on_created is not None:
                on_created(profile)
            base = getattr(sys, "_base_executable", None) or sys.executable
            context.report_progress(0, "Creating isolated Python environment")
            run_command_process([base, "-I", "-B", "-m", "venv", "--copies", "--without-pip", str(profile)],
                                cwd=store, context=context, timeout=120)
            interpreter = _interpreter(profile)
            # pip is supplied by the host only to provision the empty target;
            # it is never added to the target's persistent import path.
            pip = [sys.executable, "-I", "-B", "-c",
                   "import os,runpy;os.dup2(2,1);runpy.run_module('pip',run_name='__main__')",
                   "--isolated", "--python", str(interpreter)]
            environment = {key: value for key, value in os.environ.items() if not key.startswith(("PIP_", "PYTHON"))}
            environment["PIP_CONFIG_FILE"] = os.devnull
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["PYTHONNOUSERSITE"] = "1"
            environment["PYTHONSAFEPATH"] = "1"
            if plan.wheels:
                requirements = profile / ".locked-wheels.txt"
                requirements.write_text("".join(f"{wheel.as_uri()} --hash=sha256:{digest}\n" for wheel, digest in plan.wheels), encoding="utf-8")
                try:
                    run_command_process([*pip, "install", "--no-index", "--no-deps", "--only-binary=:all:",
                        "--no-compile", "--no-cache-dir", "--require-hashes", "-r", str(requirements)],
                        cwd=store, context=context, timeout=300, environment=environment)
                finally:
                    requirements.unlink(missing_ok=True)
            run_command_process([*pip, "check"], cwd=store, context=context, timeout=30, environment=environment)
            probe = run_command_process([str(interpreter), "-I", "-B", "-c",
                "import json,sys;print(json.dumps({'prefix':sys.prefix,'base':sys.base_prefix,'paths':sys.path}))"],
                cwd=store, context=context, timeout=30)
            facts = json.loads(probe.stdout)
            if Path(facts["prefix"]).resolve() != profile or facts["prefix"] == facts["base"]:
                raise OperationError("The prepared Python interpreter is not isolated")
            host_packages = {Path(path).resolve() for path in sys.path if path and Path(path).name in {"site-packages", "dist-packages"}}
            if any(Path(path).resolve() in host_packages for path in facts["paths"]):
                raise OperationError("The prepared Python interpreter exposes host dependencies")
            package = profile / "package"
            package.mkdir()
            for path in plan.files:
                context.check_cancelled()
                target = package / path.relative_to(plan.root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
            # A prepared binding also pins peer code/resources, so an update
            # cannot change imports underneath an already running process.
            current = read_python_plan(plan.root)
            if current is None or current.digest != plan.digest:
                raise OperationError("Python plugin resources changed during environment preparation")
            context.check_cancelled()
            (profile / ".preparing").unlink()
            _write_json(plan.root / BINDING, marker)
        return profile
    except BaseException:
        try:
            published = _json(plan.root / BINDING) == marker
        except OperationError:
            published = False
        except OSError:
            # An unreadable binding may already point at this environment.
            published = True
        try:
            if not published:
                remove_python_environment(profile)
        except (OperationError, OSError):
            # Keep the marker/journal for owner recovery; cleanup cannot replace
            # the actual preparation failure with an unrelated filesystem error.
            pass
        raise


def bound_python_profile(directory: str | Path) -> Path | None:
    binding = Path(directory) / BINDING
    if not binding.exists():
        return None
    value = _json(binding)
    if (not isinstance(value, dict) or set(value) != {"formatVersion", "planDigest", "profile"}
            or type(value["formatVersion"]) is not int or value["formatVersion"] != 1
            or not isinstance(value["profile"], str) or not Path(value["profile"]).is_absolute()
            or not isinstance(value["planDigest"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["planDigest"])):
        raise OperationError("Invalid Python environment binding")
    profile = Path(value["profile"])
    if not _PROFILE.fullmatch(profile.name) or profile.is_symlink() or not profile.is_dir():
        raise OperationError("The Python environment must be prepared again")
    if _json(profile / _MARKER) != value:
        raise OperationError("The Python environment binding does not match its profile")
    return profile


def python_binding(directory: str | Path, *, expected_digest: str | None = None) -> tuple[Path, str]:
    if expected_digest is None:
        plan = read_python_plan(directory)
        if plan is None:
            raise OperationError("The Python environment has not been prepared")
        expected_digest = plan.digest
    profile = bound_python_profile(directory)
    if profile is None:
        raise OperationError("The Python environment has not been prepared")
    if _json(profile / _MARKER)["planDigest"] != expected_digest:
        raise OperationError("The Python environment must be prepared again")
    return profile, expected_digest


@contextmanager
def python_environment(directory: str | Path, *, binding: tuple[Path, str] | None = None,
                       expected_digest: str | None = None):
    profile, digest = binding or python_binding(directory, expected_digest=expected_digest)
    try:
        with profile_lock(profile):
            marker = _json(profile / _MARKER)
            retired = profile.parent / ("." + profile.name + ".retired.json")
            if marker["planDigest"] != digest or retired.exists() or (profile / ".preparing").exists():
                raise OperationError("The Python environment must be prepared again")
            interpreter = _interpreter(profile)
            if not interpreter.is_file():
                raise OperationError("The Python environment interpreter is unavailable")
            yield interpreter
    except BlockingIOError as error:
        raise OperationError("The Python environment is being prepared or retired; retry after management completes") from error


def remove_python_environment(profile: str | Path) -> bool:
    """Retire an owned profile if idle; return False while executions use it."""
    profile = Path(profile).absolute()
    if not _PROFILE.fullmatch(profile.name) or profile.is_symlink():
        raise OperationError("Refusing to remove an unmanaged Python profile")
    profile = profile.parent.resolve() / profile.name
    tombstone = profile.parent / ("." + profile.name + ".retired.json")
    trash = profile.parent / (".retired-" + profile.name)
    creating = profile.parent / (".creating-" + profile.name)
    if not profile.parent.exists():
        return True
    try:
        with profile_lock(profile, exclusive=True):
            if trash.is_symlink() or creating.is_symlink():
                raise OperationError("Refusing to remove a linked Python profile")
            if profile.exists() or tombstone.exists():
                marker = _json(tombstone if tombstone.exists() else profile / _MARKER)
                if (not isinstance(marker, dict) or set(marker) != {"formatVersion", "planDigest", "profile"}
                        or type(marker["formatVersion"]) is not int or marker["formatVersion"] != 1
                        or not isinstance(marker["planDigest"], str) or not re.fullmatch(r"[a-f0-9]{64}", marker["planDigest"])
                        or not isinstance(marker["profile"], str) or Path(marker["profile"]) != profile):
                    raise OperationError("Refusing to remove an unmanaged Python profile")
                if not tombstone.exists():
                    _write_json(tombstone, marker)
                if profile.exists():
                    if trash.exists():
                        raise OperationError("The retired Python profile directory is already occupied")
                    profile.rename(trash)
                if trash.exists():
                    shutil.rmtree(trash)
                tombstone.unlink()
            elif trash.exists():
                raise OperationError("Refusing to remove an unmanaged Python profile")
            if creating.exists():
                shutil.rmtree(creating)
        return True
    except OSError:
        return False
