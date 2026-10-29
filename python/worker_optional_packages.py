"""Manage optional worker dependencies for the runtime active at task start.

Each active backend gets a writable sidecar under the application data directory. Pip first
resolves against the active environment, then installs only the missing distributions into the
sidecar so platform-specific packages such as PyTorch are neither replaced nor duplicated.
The macOS base runtime remains immutable, including when a component is uninstalled.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from types import ModuleType
from typing import Any

from worker_protocol import emit, emit_error


OPTIONAL_PACKAGES: dict[str, dict[str, str]] = {
    "funasr": {
        "requirement": "funasr==1.3.26",
        "verifyCode": "import funasr, torchaudio",
    },
}

_MLX_FUNASR_REQUIREMENT = "funasr-mlx>=0.2"
_MLX_FUNASR_DISTRIBUTION = "funasr-mlx"

# Apple Silicon uses a dedicated writable funasr-mlx sidecar because the
# immutable MLX base runtime does not include PyTorch.
def _is_apple_silicon() -> bool:
    """Runtime check (tests monkeypatch sys.platform; keep it authoritative)."""
    return sys.platform == "darwin" and platform.machine() == "arm64"


def _apple_silicon_sidecar_status(runtime: RuntimeContext | None = None) -> dict[str, Any] | None:
    """Status for the writable funasr-mlx sidecar on Apple Silicon."""
    if not _is_apple_silicon():
        return None
    runtime = runtime or _runtime_context()
    managed_version = _sidecar_version(_MLX_FUNASR_DISTRIBUTION, _package_dir("funasr", runtime))
    environment_version = None if managed_version else _environment_version(_MLX_FUNASR_DISTRIBUTION, runtime)
    version = managed_version or environment_version
    from pip._vendor.packaging.requirements import Requirement
    def supported(value: str | None) -> bool:
        try:
            return bool(value) and Requirement(_MLX_FUNASR_REQUIREMENT).specifier.contains(value, prereleases=False)
        except ValueError:
            return False
    return {
        "package": "funasr",
        "requirement": _MLX_FUNASR_REQUIREMENT,
        "installed": supported(version),
        "present": bool(version),
        "version": version,
        "source": "managed" if managed_version else "environment" if environment_version else None,
        "issue": None if not version or supported(version) else "version_mismatch",
        "canUninstall": bool(managed_version),
        "runtimeIdentity": runtime.identity,
    }


@dataclass(frozen=True)
class RuntimeContext:
    """Immutable view of the runtime selected when a package operation starts."""

    executable: Path
    backend: str
    identity: str
    packages_dir: Path


def _runtime_envs_dir() -> Path:
    configured = str(os.environ.get("PYMSS_STUDIO_RUNTIME_ENVS_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "darwin":
        data_root = str(os.environ.get("PYMSS_STUDIO_DATA_ROOT") or "").strip()
        return (Path(data_root).expanduser() if data_root else Path.home() / ".pymss-studio") / "runtime-envs"
    executable = Path(sys.executable).resolve()
    env_dir = executable.parent.parent if executable.parent.name.lower() in {"scripts", "bin"} else executable.parent
    return env_dir.parent


def _runtime_identity(*, backend: str | None = None, executable: Path | None = None) -> str:
    backend = _active_runtime_backend() if backend is None else backend
    executable = (executable or Path(sys.executable)).resolve()
    if not backend:
        digest = hashlib.sha256(os.path.normcase(str(executable)).encode("utf-8")).hexdigest()[:12]
        backend = f"runtime-{digest}"
    safe_backend = "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in backend)
    return f"{safe_backend}-py{sys.version_info.major}{sys.version_info.minor}"


def _active_runtime_backend() -> str:
    active_file = str(os.environ.get("PYMSS_STUDIO_ACTIVE_RUNTIME_FILE") or "").strip()
    candidates = [Path(active_file)] if active_file else []
    bundled = str(os.environ.get("PYMSS_STUDIO_BUNDLED_RUNTIME_ENVS_DIR") or "").strip()
    if bundled:
        candidates.append(Path(bundled) / "active-runtime.json")
    for file in candidates:
        try:
            state = json.loads(file.read_text(encoding="utf-8"))
            backend = str(state.get("backend") or "").strip().lower()
            if backend:
                return backend
        except (OSError, ValueError, AttributeError):
            continue
    return ""


def _runtime_context() -> RuntimeContext:
    executable = Path(sys.executable).resolve()
    backend = _active_runtime_backend()
    identity = _runtime_identity(backend=backend, executable=executable)
    packages_root = _runtime_envs_dir() / "optional-packages"
    # Older bundled installs had no user pointer until the first core update,
    # so components used the executable identity instead of the backend name.
    legacy_identity = _runtime_identity(backend="", executable=executable)
    if not (packages_root / identity).exists() and (packages_root / legacy_identity).is_dir():
        identity = legacy_identity
    return RuntimeContext(
        executable=executable,
        backend=backend,
        identity=identity,
        packages_dir=packages_root / identity,
    )


def _package_dir(name: str, runtime: RuntimeContext | None = None) -> Path:
    return (runtime or _runtime_context()).packages_dir / name


def _package_spec(name: Any) -> tuple[str, dict[str, str]]:
    normalized = str(name or "").strip().lower()
    spec = OPTIONAL_PACKAGES.get(normalized)
    if not spec:
        raise ValueError(f"Unsupported optional runtime package: {normalized or 'missing'}")
    if normalized == "funasr" and _is_apple_silicon():
        spec = {"requirement": _MLX_FUNASR_REQUIREMENT, "verifyCode": "from funasr_mlx.api import download_model, transcribe"}
    return normalized, spec


def optional_package_paths(names: Iterable[str], runtime: RuntimeContext | None = None) -> list[Path]:
    """Resolve host-managed component directories using their public package ids."""
    packages = [_package_spec(name)[0] for name in names]
    if not packages:
        return []
    runtime = runtime or _runtime_context()
    return [_package_dir(name, runtime) for name in packages]


def activate_optional_packages(
    runtime: RuntimeContext | None = None, *, names: Iterable[str] = OPTIONAL_PACKAGES,
) -> list[str]:
    added = [str(directory) for directory in optional_package_paths(names, runtime)
             if directory.is_dir() and str(directory) not in sys.path]
    sys.path[:0] = added
    importlib.invalidate_caches()
    return added


@contextmanager
def optional_package_activation(names: Iterable[str]) -> Iterator[None]:
    """Keep component imports only when the plugin's activation succeeds."""
    previous_modules = set(sys.modules)
    added = activate_optional_packages(names=names)
    try:
        yield
    except BaseException:
        if added:
            roots = [Path(os.path.abspath(value)) for value in added]
            # Roll back complete newly imported package trees. A namespace may
            # span component and host directories; deleting only its parent
            # would leave cached children detached from subsequent imports.
            removed: set[str] = set()
            for name in sorted(set(sys.modules) - previous_modules, key=len):
                parent_name = name.rpartition(".")[0]
                while parent_name and parent_name not in removed:
                    parent_name = parent_name.rpartition(".")[0]
                if parent_name:
                    removed.add(name)
                    continue
                module = sys.modules.get(name)
                if not isinstance(module, ModuleType):
                    continue
                attributes = vars(module)
                locations = list(attributes.get("__path__") or ())
                filename = attributes.get("__file__")
                if filename:
                    locations.append(filename)
                if any(Path(os.path.abspath(location)).is_relative_to(root)
                       for location in locations if isinstance(location, (str, os.PathLike)) for root in roots):
                    removed.add(name)
            for name in sorted(removed, key=len, reverse=True):
                module = sys.modules.pop(name, None)
                parent_name, _, attribute = name.rpartition(".")
                parent = sys.modules.get(parent_name)
                if isinstance(parent, ModuleType) and vars(parent).get(attribute) is module:
                    vars(parent).pop(attribute, None)
            sys.path[:] = [value for value in sys.path if value not in added]
            for value in added:
                sys.path_importer_cache.pop(value, None)
            importlib.invalidate_caches()
        raise


def _sidecar_version(name: str, directory: Path) -> str | None:
    if not directory.is_dir():
        return None
    try:
        for distribution in metadata.distributions(path=[str(directory)]):
            distribution_name = str(distribution.metadata.get("Name") or "").strip().lower()
            if distribution_name == name:
                return str(distribution.version)
    except Exception:
        return None
    return None


def _environment_version(name: str, runtime: RuntimeContext | None = None) -> str | None:
    runtime = runtime or _runtime_context()
    script = (
        "from importlib import metadata\n"
        f"try:\n print(metadata.version({name!r}))\n"
        "except metadata.PackageNotFoundError:\n print('')\n"
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    try:
        output = subprocess.check_output(
            [str(runtime.executable), "-c", script],
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
    except Exception:
        return None
    return output.strip() or None


def _required_version(requirement: str) -> str | None:
    name, separator, version = requirement.partition("==")
    return version.strip() if separator and name.strip() and version.strip() else None


def optional_package_status(name: Any, runtime: RuntimeContext | None = None) -> dict[str, Any]:
    """Return a lightweight availability snapshot for the selected runtime.

    Managed components are assembled in a staging directory, deeply imported there, and only
    then activated. Consequently, matching installed metadata is sufficient for the common
    status path; importing FunASR here would load Torch on every page activation and make a
    simple availability check take tens of seconds.
    """

    runtime = runtime or _runtime_context()
    normalized, spec = _package_spec(name)
    if normalized == "funasr":
        mlx_status = _apple_silicon_sidecar_status(runtime)
        if mlx_status is not None:
            return mlx_status
    directory = _package_dir(normalized, runtime)
    managed_version = _sidecar_version(normalized, directory)
    environment_version = None if managed_version else _environment_version(normalized, runtime)
    version = managed_version or environment_version
    source = "managed" if managed_version else "environment" if environment_version else None
    expected_version = _required_version(spec["requirement"])
    issue = None
    if version and expected_version and version != expected_version:
        issue = "version_mismatch"
    return {
        "package": normalized,
        "requirement": spec["requirement"],
        "installed": bool(version) and issue is None,
        "present": bool(version),
        "version": version,
        "source": source,
        "canUninstall": source == "managed" or (source == "environment" and sys.platform != "darwin"),
        "issue": issue,
        "runtimeIdentity": runtime.identity,
    }


def _emit_stage(name: str, action: str, message: str, task_id: str | None = None) -> None:
    emit("optional_package_stage", {"package": name, "action": action, "message": message}, task_id=task_id)


def _run_process(
    command: list[str],
    name: str,
    action: str,
    environment: dict[str, str] | None = None,
    task_id: str | None = None,
) -> None:
    environment = dict(os.environ if environment is None else environment)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
    )
    assert process.stdout is not None
    recent: list[str] = []
    try:
        for line in process.stdout:
            message = line.rstrip()
            if not message:
                continue
            recent.append(message)
            recent = recent[-8:]
            emit("optional_package_log", {"package": name, "action": action, "message": message}, task_id=task_id)
    except Exception:
        # A closed protocol pipe must also stop pip. Otherwise the failed worker leaves an
        # installer running, and a second click can appear to fix the problem only because the
        # orphaned first process finished writing the package in the background.
        if process.poll() is None:
            process.kill()
        process.wait()
        raise
    finally:
        process.stdout.close()
    if process.wait() != 0:
        detail = recent[-1] if recent else f"pip exited with code {process.returncode}"
        raise RuntimeError(detail)


def _resolve_install_requirements(
    requirement: str,
    index_url: str,
    report_path: Path,
    name: str,
    task_id: str | None = None,
    runtime: RuntimeContext | None = None,
) -> list[str]:
    runtime = runtime or _runtime_context()
    mlx_only = name == "funasr" and _is_apple_silicon()
    fixed = {}
    audio_version = None
    if mlx_only:
        for package in ("mlx", "mlx-metal", "pymss", "pymss-core"):
            version = _environment_version(package, runtime)
            if version:
                fixed[package] = version
    else:
        torch_version = _environment_version("torch", runtime)
        if not torch_version:
            raise RuntimeError("The active runtime does not include PyTorch")
        audio_version = torch_version.split("+", 1)[0]
        fixed = {"torch": torch_version, "torchaudio": audio_version}
    constraints = report_path.with_suffix(".constraints.txt")
    constraints.write_text("".join(f"{package}=={version}\n" for package, version in fixed.items()), encoding="utf-8")
    try:
        _run_process(
            [
                str(runtime.executable),
                "-m",
                "pip",
                "install",
                "--dry-run",
                "--disable-pip-version-check",
                "--prefer-binary",
                "--constraint",
                str(constraints),
                "--report",
                str(report_path),
                "--index-url",
                index_url,
                requirement,
            ],
            name,
            "install",
            task_id=task_id,
        )
    finally:
        constraints.unlink(missing_ok=True)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    from pip._vendor.packaging.utils import canonicalize_name
    from worker_bootstrap import _is_base_accelerator_package

    resolved: list[str] = []
    for item in report.get("install") or []:
        package_metadata = item.get("metadata") or {}
        package_name = str(package_metadata.get("name") or "").strip()
        version = str(package_metadata.get("version") or "").strip()
        if not package_name or not version:
            raise RuntimeError("pip returned an incomplete optional package resolution report")
        if not all(character.isalnum() or character in {"-", "_", "."} for character in package_name):
            raise RuntimeError(f"pip returned an invalid package name: {package_name}")
        normalized_name = canonicalize_name(package_name)
        if normalized_name == "torchaudio" and not mlx_only:
            if version.split("+", 1)[0] != audio_version:
                raise RuntimeError("The component requires a torchaudio version incompatible with the fixed runtime package torch.")
            # The dedicated audio installer selects the wheel matching the
            # captured Torch backend, rather than taking the PyPI build here.
            continue
        if _is_base_accelerator_package(normalized_name) or normalized_name in {"pymss", "pymss-core"}:
            raise RuntimeError(f"The component requires changing the runtime package {package_name}. The base runtime was preserved.")
        resolved.append(f"{package_name}=={version}")
    from pip._vendor.packaging.requirements import Requirement
    required_name = canonicalize_name(Requirement(requirement).name)
    resolved_names = {
        canonicalize_name(value.partition("==")[0].strip())
        for value in resolved
    }
    if required_name not in resolved_names:
        # A broken copy may still satisfy pip's metadata check in the active environment. Install
        # the requested distribution into the sidecar so it shadows that unusable copy.
        resolved.append(requirement)
    return resolved


def _torchaudio_requirement(runtime: RuntimeContext | None = None) -> tuple[str, str | None] | None:
    runtime = runtime or _runtime_context()
    torch_version = _environment_version("torch", runtime)
    if not torch_version:
        raise RuntimeError("The active runtime does not include PyTorch")
    version = torch_version.split("+", 1)[0]
    torchaudio_version = _environment_version("torchaudio", runtime)
    if torchaudio_version == torch_version:
        return None
    from worker_bootstrap import _manifest
    backend = runtime.backend
    torch_spec = ((_manifest().get("backends") or {}).get(backend) or {}).get("torch") or {}
    if backend == "rocm":
        for requirement in torch_spec.get("requirements") or []:
            if "/torchaudio-" in str(requirement).lower():
                return str(requirement), None
        if not torch_spec.get("indexUrl"):
            raise RuntimeError("The active ROCm runtime does not define a compatible torchaudio wheel")
    return f"torchaudio=={version}", str(torch_spec.get("indexUrl") or "").strip() or None


def _install_torchaudio(
    staging: Path,
    name: str,
    task_id: str | None,
    runtime: RuntimeContext | None = None,
) -> None:
    runtime = runtime or _runtime_context()
    requirement = _torchaudio_requirement(runtime)
    if requirement is None:
        return
    package, index_url = requirement
    command = [
        str(runtime.executable), "-m", "pip", "install", "--disable-pip-version-check",
        "--prefer-binary", "--no-deps", "--target", str(staging),
    ]
    if index_url:
        command.extend(["--index-url", index_url])
    command.append(package)
    _emit_stage(name, "install", f"Installing the compatible audio runtime: {package}", task_id)
    _run_process(command, name, "install", task_id=task_id)


def _install_package(
    name: str,
    spec: dict[str, str],
    mirror: str,
    locale: str,
    task_id: str | None = None,
    runtime: RuntimeContext | None = None,
) -> None:
    from worker_bootstrap import PYPI_MIRROR_URLS, _resolve_pypi_mirror

    runtime = runtime or _runtime_context()

    selected_mirror, index_url = _resolve_pypi_mirror(mirror, locale)
    official = PYPI_MIRROR_URLS["pypi"]
    indexes = [index_url or official]
    if selected_mirror != "pypi" and official not in indexes:
        indexes.append(official)

    target = _package_dir(name, runtime)
    staging = target.with_name(f".{name}.installing")
    report_path = target.with_name(f".{name}.pip-report.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    try:
        for attempt, index in enumerate(indexes):
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)
            report_path.unlink(missing_ok=True)
            if attempt:
                _emit_stage(name, "install", "Selected package source is unavailable; retrying with PyPI", task_id)
            try:
                resolved = _resolve_install_requirements(
                    spec["requirement"],
                    index,
                    report_path,
                    name,
                    task_id,
                    runtime,
                )
                command = [
                    str(runtime.executable), "-m", "pip", "install", "--disable-pip-version-check",
                    "--prefer-binary", "--no-deps",
                    "--target", str(staging), "--index-url", index, *resolved,
                ]
                _run_process(command, name, "install", task_id=task_id)
                last_error = None
                break
            except Exception as error:
                last_error = error
        if last_error:
            raise last_error
        if not (name == "funasr" and _is_apple_silicon()):
            _install_torchaudio(staging, name, task_id, runtime)
        _emit_stage(name, "install", "Verifying the ASR runtime component", task_id)
        _run_process(
            [
                str(runtime.executable),
                "-c",
                f"import sys; sys.path.insert(0, sys.argv[1]); {spec['verifyCode']}",
                str(staging),
            ],
            name,
            "install",
            task_id=task_id,
        )
        shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
        activate_optional_packages(runtime)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        report_path.unlink(missing_ok=True)


def _uninstall_package(
    name: str,
    task_id: str | None = None,
    runtime: RuntimeContext | None = None,
) -> None:
    runtime = runtime or _runtime_context()
    target = _package_dir(name, runtime)
    if target.is_dir():
        shutil.rmtree(target)
    elif sys.platform == "darwin" and _environment_version(name, runtime):
        raise RuntimeError("This component is supplied by the read-only base runtime and cannot be uninstalled. The runtime was preserved.")
    if sys.platform != "darwin" and _environment_version(name, runtime):
        _run_process(
            [str(runtime.executable), "-m", "pip", "uninstall", "--yes", name],
            name,
            "uninstall",
            task_id=task_id,
        )
    importlib.invalidate_caches()


def cmd_optional_package_status(payload: dict[str, Any]) -> int:
    try:
        status = optional_package_status(payload.get("package"), _runtime_context())
    except Exception as error:
        return emit_error("OPTIONAL_PACKAGE_INVALID", str(error), recoverable=True)
    emit("optional_package_status", status)
    return 0


def cmd_manage_optional_package(payload: dict[str, Any]) -> int:
    action = str(payload.get("action") or "").strip().lower()
    task_id = str(payload.get("taskId") or "").strip() or None
    try:
        runtime = _runtime_context()
        name, spec = _package_spec(payload.get("package"))
        if action not in {"install", "uninstall"}:
            raise ValueError(f"Unsupported optional package action: {action or 'missing'}")
        current = optional_package_status(name, runtime)
        if action == "install" and not current["installed"]:
            _emit_stage(name, action, f"Installing {spec['requirement']}", task_id)
            _install_package(
                name, spec, str(payload.get("mirror") or "auto").strip().lower(),
                str(payload.get("locale") or "").strip().lower(), task_id, runtime,
            )
        elif action == "uninstall" and current["present"]:
            _emit_stage(name, action, f"Uninstalling {name}", task_id)
            if name == "funasr" and _is_apple_silicon():
                shutil.rmtree(_package_dir(name, runtime), ignore_errors=True)
                importlib.invalidate_caches()
            else:
                _uninstall_package(name, task_id, runtime)
        status = optional_package_status(name, runtime)
        if action == "install" and not status["installed"]:
            raise RuntimeError(f"{name} is still unavailable after installation")
        if action == "uninstall" and status["present"] and status["canUninstall"]:
            raise RuntimeError(f"{name} is still installed after uninstallation")
        emit("optional_package_status", status, task_id=task_id)
        return 0
    except Exception as error:
        return emit_error(
            "OPTIONAL_PACKAGE_MANAGE_FAILED",
            str(error),
            task_id=task_id,
            recoverable=True,
        )
