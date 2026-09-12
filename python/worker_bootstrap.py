from __future__ import annotations

import errno
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from functools import partial, wraps
from pathlib import Path
from typing import Any, Callable

from worker_runtime_lock import RuntimeLockBusy, runtime_lock

MANIFEST_PATH = Path(__file__).with_name("runtime-manifest.json")


def _default_runtime_envs_dir() -> Path:
    executable = Path(sys.executable).resolve()
    if executable.parent.name.lower() in {"scripts", "bin"}:
        env_dir = executable.parent.parent
        if (env_dir / "pyvenv.cfg").is_file():
            return env_dir.parent
        return env_dir / "runtime-envs"
    return executable.parent / "runtime-envs"


RUNTIME_ENVS_DIR = Path(os.environ.get("PYMSS_STUDIO_RUNTIME_ENVS_DIR") or _default_runtime_envs_dir())
ACTIVE_RUNTIME_FILE = Path(os.environ.get("PYMSS_STUDIO_ACTIVE_RUNTIME_FILE") or RUNTIME_ENVS_DIR / "active-runtime.json")
BUNDLED_RUNTIME_ENVS_DIR = Path(os.environ["PYMSS_STUDIO_BUNDLED_RUNTIME_ENVS_DIR"]) if os.environ.get("PYMSS_STUDIO_BUNDLED_RUNTIME_ENVS_DIR") else None

# Distribution name -> import name, for the manifest packages whose two names differ.
# Availability is decided with importlib, so an unmapped dashed name can never be found and the
# environment would read as permanently incomplete. Both detection paths — in-process and the
# probe script below — share this table so they can never disagree.
PACKAGE_IMPORT_NAMES = {
    "pyyaml": "yaml",
    "pymss-core": "pymss_core",
    "typing-extensions": "typing_extensions",
}


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _pin_manifest_requirement(requirement: Any, version: str) -> str:
    """Pin a manifest requirement while retaining any extras it declares."""
    spec = str(requirement or "").strip()
    name_match = re.match(r"([A-Za-z0-9_.-]+)", spec)
    name = name_match.group(1) if name_match else "pymss"
    extras_match = re.search(r"(\[[^\]]+\])", spec)
    extras = extras_match.group(1) if extras_match else ""
    return f"{name}{extras}=={version}"


def _supported_backend(backend: str, manifest: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]] | None:
    normalized = str(backend or "").strip().lower()
    if not normalized or "/" in normalized or "\\" in normalized or normalized in {".", ".."}:
        return None
    manifest = manifest or _manifest()
    spec = manifest.get("backends", {}).get(normalized)
    return (normalized, spec) if isinstance(spec, dict) else None


def _emit(event_type: str, payload: dict[str, Any], task_id: str | None = None) -> None:
    from worker_protocol import emit
    emit(event_type, payload, task_id=task_id)


def _serialized_runtime_command(command: Callable[..., int], *, query: bool = False) -> Callable[[dict[str, Any]], int]:
    @wraps(command)
    def run(payload: dict[str, Any]) -> int:
        try:
            # Repairing queries hold the same lock as writers for their entire operation.
            # Read-only installations may only be observed, never repaired or recovered.
            with runtime_lock(RUNTIME_ENVS_DIR / ".runtime-management.lock", allow_read_only=query) as writable:
                return command(payload, repair=writable) if query else command(payload)
        except RuntimeLockBusy:
            from worker_protocol import emit_error
            return emit_error(
                "RUNTIME_BUSY",
                "Another runtime operation is in progress. Wait for it to finish and retry.",
                task_id=payload.get("taskId"),
                recoverable=True,
            )
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EPERM, errno.EROFS}:
                raise
            from worker_protocol import emit_error
            return emit_error(
                "RUNTIME_PERMISSION_DENIED",
                "The runtime directory does not allow this operation.",
                detail=str(exc),
                task_id=payload.get("taskId"),
                recoverable=True,
            )
    return run


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _resolve_runtime_state_from(file: Path) -> dict[str, Any] | None:
    """Read active-runtime.json and resolve relative pythonPath against its parent dir."""
    try:
        state = json.loads(file.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(state, dict):
        return None
    python_path = state.get("pythonPath")
    if python_path:
        if not isinstance(python_path, str):
            return None
        p = Path(python_path)
        if not p.is_absolute():
            resolved = file.parent / p
            if resolved.is_file():
                state["pythonPath"] = str(resolved.resolve())
            else:
                return None
        elif not p.is_file():
            return None
    state.pop("source", None)
    return state


def _read_runtime_state() -> dict[str, Any] | None:
    state = _resolve_runtime_state_from(ACTIVE_RUNTIME_FILE)
    if state and state.get("pythonPath"):
        supported = _supported_backend(str(state.get("backend") or ""))
        if supported:
            # Rust may persist the active interpreter using the Windows extended-path
            # prefix (``\\?\\``), while the configured runtime root can use a regular
            # drive path.  Normalize the interpreter before deriving its environment
            # directory so an otherwise valid active environment is not discarded.
            state["pythonPath"] = str(_runtime_command_path(Path(str(state["pythonPath"]))))
            try:
                env_dir = _runtime_env_dir_for_python(Path(str(state["pythonPath"])))
            except (OSError, ValueError):
                env_dir = None
            python_path = Path(str(state["pythonPath"]))
            if env_dir and (
                (env_dir.name == supported[0] and (_is_user_runtime_env(env_dir) or _is_bundled_runtime_env(env_dir)))
                or _is_bundled_bootstrap_python(python_path)
            ):
                if _is_bundled_runtime_env(env_dir) or _is_bundled_bootstrap_python(python_path):
                    state["source"] = "bundled"
                return state
    if BUNDLED_RUNTIME_ENVS_DIR:
        bundled = _resolve_runtime_state_from(BUNDLED_RUNTIME_ENVS_DIR / "active-runtime.json")
        if bundled and bundled.get("pythonPath"):
            bundled["source"] = "bundled"
            return bundled
    return None


def _write_runtime_state(state: dict[str, Any]) -> None:
    ACTIVE_RUNTIME_FILE.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(ACTIVE_RUNTIME_FILE, state)


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _atomic_write_text(path: Path, content: str) -> None:
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _env_dir(backend: str) -> Path:
    return RUNTIME_ENVS_DIR / backend


def _env_python_path(backend: str) -> Path:
    env_dir = _env_dir(backend)
    # ``runtime_envs_dir`` can originate from Rust's ``canonicalize`` and therefore carry
    # Windows' ``\\\\?\\`` extended-path prefix.  Passing that path to a venv's Python makes
    # pip build script destinations such as ``Lib\\site-packages\\..\\..\\Scripts``.  The
    # Win32 path parser rejects the ``..`` components under the extended prefix and reports
    # ``[Errno 22] Invalid argument`` while installing entry points (for example ``numba``).
    # Use a normal path for the interpreter whenever it is representable by Win32; keep the
    # extended form for unusually long paths so those installations still retain long-path
    # support.
    candidate = env_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    return _runtime_command_path(candidate)


def _python_path_for_env(env_dir: Path) -> Path:
    """Resolve the interpreter inside an arbitrary environment directory."""
    return _runtime_command_path(env_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python"))


def _env_state_path(backend: str) -> Path:
    return _env_dir(backend) / "pymss-runtime-state.json"


def _env_log_path(backend: str) -> Path:
    return _env_dir(backend) / "pymss-runtime-install.log"


def _recover_reinstall_backups() -> None:
    import shutil

    if not RUNTIME_ENVS_DIR.is_dir():
        return
    for backup in RUNTIME_ENVS_DIR.glob(".*.reinstalling"):
        backend = backup.name.removeprefix(".").removesuffix(".reinstalling")
        if not _supported_backend(backend):
            continue
        target = _env_dir(backend)
        try:
            state = _read_installed_env_state(backend)
            python_path = _env_python_path(backend)
            if state and python_path.is_file():
                shutil.rmtree(backup)
                continue
            if target.exists():
                shutil.rmtree(target)
            backup.rename(target)
        except OSError:
            continue


def _recover_core_update_transactions() -> None:
    """Finish or roll back a core update interrupted during the directory swap.

    The update is deliberately transactional, but a process can still be killed between the
    two renames.  Never leave a hidden backup to be mistaken for an installed environment:
    validate the visible directory first, otherwise restore the original copy.
    """
    if not RUNTIME_ENVS_DIR.is_dir():
        return
    for backup in RUNTIME_ENVS_DIR.glob(".*.core-backup"):
        backend = backup.name.removeprefix(".").removesuffix(".core-backup")
        if not _supported_backend(backend):
            continue
        target = _env_dir(backend)
        staging = RUNTIME_ENVS_DIR / f".{backend}.core-updating"
        try:
            target_is_valid = False
            if target.is_dir():
                target_python = _env_python_path(backend)
                if target_python.is_file():
                    try:
                        probed = _probe_python_runtime(target_python, _backend_extra_names(_manifest(), backend))
                        target_is_valid = _runtime_probe_is_ready(backend, probed, _manifest())
                    except Exception:
                        target_is_valid = False
            if target_is_valid:
                # The process may have stopped after promoting the staged directory but before
                # committing its state files. Rebuild the cache from the promoted interpreter so
                # an otherwise successful transaction does not keep advertising the old manifest.
                recovered_state = _discover_runtime_state(
                    backend,
                    target_python,
                    _manifest(),
                    persist=True,
                )
                active_state = _resolve_runtime_state_from(ACTIVE_RUNTIME_FILE)
                if recovered_state and active_state and _same_path(active_state.get("pythonPath"), target_python):
                    _write_runtime_state({
                        **active_state,
                        **recovered_state,
                        "pythonPath": str(target_python),
                    })
                shutil.rmtree(backup, ignore_errors=True)
            else:
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                backup.rename(target)
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        except OSError:
            # Leave the directories in place for the next startup; deleting either copy here
            # would turn a recoverable interrupted update into data loss.
            continue

    # A staging copy without its backup is never safe to activate: it may have been created
    # before pip completed.  The next update can recreate it from the visible environment.
    for staging in RUNTIME_ENVS_DIR.glob(".*.core-updating"):
        backend = staging.name.removeprefix(".").removesuffix(".core-updating")
        if _supported_backend(backend):
            shutil.rmtree(staging, ignore_errors=True)


def _recover_runtime_transactions() -> None:
    _recover_reinstall_backups()
    _recover_core_update_transactions()


def _bootstrap_python_path() -> Path:
    python_path = Path(os.environ.get("PYMSS_STUDIO_BOOTSTRAP_PYTHON") or sys.executable)
    return python_path if python_path.is_file() else Path(sys.executable)


# Bumped when a fixed install path starts recording trustworthy state. States below this
# version may carry another environment's torch build (see _repaired_env_state).
ENV_STATE_VERSION = 2

PYPI_MIRROR_URLS = {
    "ustc": "https://mirrors.ustc.edu.cn/pypi/web/simple",
    "tsinghua": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "aliyun": "https://mirrors.aliyun.com/pypi/simple",
    "tencent": "https://mirrors.cloud.tencent.com/pypi/simple",
    "pypi": "https://pypi.org/simple",
}


def _resolve_pypi_mirror(mirror: str, locale: str) -> tuple[str, str | None]:
    selected = "ustc" if mirror == "auto" and locale.startswith("zh") else "pypi" if mirror == "auto" else mirror
    return selected, PYPI_MIRROR_URLS.get(selected)


def _backend_extra_names(manifest: dict[str, Any], backend: str | None) -> list[str]:
    return [re.split(r"[<>=!~;\[]", str(extra), maxsplit=1)[0].strip() for extra in manifest["backends"].get(backend or "", {}).get("extras", [])]


def _state_matches_backend(backend: str, state: dict[str, Any]) -> bool:
    """Whether the torch build recorded in an environment's state can belong to that environment.

    MLX ships a CPU torch build, so "cpu" is the correct recording for the mlx backend."""
    recorded = str(state.get("torchBackend") or "")
    if not recorded or recorded.startswith("error:"):
        return True  # Nothing concrete to contradict.
    return recorded == ("cpu" if backend == "mlx" else backend)


def _repaired_env_state(
    backend: str, state: dict[str, Any], *, persist: bool = True, probed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-derive an environment's torch facts from its own interpreter.

    Installs used to record whatever the *active* runtime reported, so a CPU environment
    installed while a CUDA runtime was active kept "2.7.1+cu128" and acceleratorAvailable=true
    forever, and the UI faithfully showed it. Nothing rewrites the file on its own, so the lie
    has to be corrected on read."""
    repaired = dict(state)
    try:
        if probed is None:
            probed = _probe_python_runtime(_env_python_path(backend), _backend_extra_names(_manifest(), backend))
        repaired.update({
            "pythonVersion": probed.get("pythonVersion") or repaired.get("pythonVersion"),
            "torchVersion": probed.get("torchVersion"),
            "torchBackend": probed.get("torchBackend"),
            "acceleratorAvailable": bool(probed.get("acceleratorAvailable")),
            "packages": probed.get("packages") or repaired.get("packages"),
            "packageVersions": probed.get("packageVersions") or repaired.get("packageVersions"),
            "pymssVersion": probed.get("pymssVersion") or repaired.get("pymssVersion"),
            "pymssCoreVersion": probed.get("pymssCoreVersion") or repaired.get("pymssCoreVersion"),
            "stateVersion": ENV_STATE_VERSION,
        })
        if "pymssGraphAvailable" in probed:
            repaired["pymssGraphAvailable"] = bool(probed["pymssGraphAvailable"])
    except Exception:
        # The truth is unavailable (broken or vanished interpreter). Drop the claim rather than
        # repeat a false one, and leave the file untouched so the next read tries again.
        repaired.update({"torchVersion": None, "torchBackend": None, "acceleratorAvailable": False})
        return repaired
    if persist:
        try:
            _atomic_write_json(_env_state_path(backend), repaired)
        except Exception:
            pass
    return repaired


def _read_installed_env_state(
    backend: str, *, repair: bool = True, probed: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    path = _env_state_path(backend)
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except Exception:
        return None
    if not isinstance(state, dict):
        return None
    if int(state.get("stateVersion") or 1) < ENV_STATE_VERSION and not _state_matches_backend(backend, state):
        state = _repaired_env_state(backend, state, persist=repair, probed=probed)
    state.pop("source", None)
    return state


def _fresh_runtime_state_from_probe(
    backend: str, manifest: dict[str, Any], probed: dict[str, Any],
) -> dict[str, Any]:
    """Project a validated probe into a new cache record, without merging or writing state."""
    manifest_version = manifest.get("manifestVersion")
    if not _manifest_versions_are_satisfied(probed, manifest, backend)[0]:
        # A directory can be runnable while still carrying an older or incomplete dependency
        # set. Do not stamp a newly discovered cache as current in that case.
        manifest_version = None
    return {
        "backend": backend,
        "manifestVersion": manifest_version,
        "stateVersion": ENV_STATE_VERSION,
        "pythonVersion": probed.get("pythonVersion"),
        "torchVersion": probed.get("torchVersion"),
        "torchBackend": probed.get("torchBackend"),
        "acceleratorAvailable": bool(probed.get("acceleratorAvailable")),
        "packages": probed.get("packages"),
        "packageVersions": probed.get("packageVersions"),
        "pymssVersion": probed.get("pymssVersion"),
        "pymssCoreVersion": probed.get("pymssCoreVersion"),
        "pymssGraphAvailable": bool(probed.get("pymssGraphAvailable")),
    }


def _discover_runtime_state(
    backend: str,
    python_path: Path,
    manifest: dict[str, Any],
    *,
    persist: bool,
    probed: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Rebuild the per-environment record from the interpreter when its cache is absent.

    The directory and interpreter are the durable installation.  ``pymss-runtime-state.json``
    is a cache written after a successful install, so losing it during an overwrite must not
    make an otherwise runnable environment disappear from the environment list.
    """
    try:
        if probed is None:
            probed = _probe_python_runtime(python_path, _backend_extra_names(manifest, backend))
    except Exception:
        return None
    if not _runtime_probe_is_ready(backend, probed, manifest):
        return None
    state = _fresh_runtime_state_from_probe(backend, manifest, probed)
    if persist:
        try:
            _atomic_write_json(_env_state_path(backend), state)
        except OSError:
            pass
    return state


# PCI vendor IDs. Used to name the GPU vendor without depending on torch: a machine running
# a CPU-only environment reports torch.cuda.is_available() == False even with an NVIDIA card,
# which is exactly the case where a CUDA recommendation matters most.
_PCI_VENDORS = {"10de": "nvidia", "1002": "amd", "1022": "amd", "8086": "intel"}


def _vendor_from_pci_id(value: str) -> str | None:
    text = str(value or "").strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    return _PCI_VENDORS.get(text[-4:]) if len(text) >= 4 else None


def _windows_gpu_vendors() -> list[str]:
    import ctypes
    from ctypes import wintypes

    class DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("DeviceName", wintypes.WCHAR * 32),
            ("DeviceString", wintypes.WCHAR * 128),
            ("StateFlags", wintypes.DWORD),
            ("DeviceID", wintypes.WCHAR * 128),
            ("DeviceKey", wintypes.WCHAR * 128),
        ]

    enum_display_devices = ctypes.windll.user32.EnumDisplayDevicesW
    enum_display_devices.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DISPLAY_DEVICEW), wintypes.DWORD]
    enum_display_devices.restype = wintypes.BOOL

    vendors: list[str] = []
    index = 0
    while index < 32:  # Bounded: mirroring drivers can otherwise enumerate indefinitely.
        device = DISPLAY_DEVICEW()
        device.cb = ctypes.sizeof(DISPLAY_DEVICEW)
        if not enum_display_devices(None, index, ctypes.byref(device), 0):
            break
        index += 1
        match = re.search(r"VEN_([0-9A-Fa-f]{4})", device.DeviceID or "")
        vendor = _vendor_from_pci_id(match.group(1)) if match else None
        if vendor and vendor not in vendors:
            vendors.append(vendor)
    return vendors


LINUX_DRM_ROOT = Path("/sys/class/drm")


def _linux_gpu_vendors(drm_root: Path | None = None) -> list[str]:
    vendors: list[str] = []
    for path in sorted((drm_root or LINUX_DRM_ROOT).glob("card*/device/vendor")):
        try:
            vendor = _vendor_from_pci_id(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if vendor and vendor not in vendors:
            vendors.append(vendor)
    return vendors


def _detect_gpu_vendors() -> list[str]:
    """Best-effort GPU vendor list, never raising. macOS is deliberately absent: the backend
    there follows from the CPU architecture, which platform.machine() already answers."""
    try:
        if sys.platform == "win32":
            return _windows_gpu_vendors()
        if sys.platform.startswith("linux"):
            return _linux_gpu_vendors()
    except Exception:
        return []
    return []


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(str(path), onerror=lambda _exc: None):
        for name in files:
            try:
                entry = Path(root) / name
                # Symlinks (common in POSIX venvs) point outside the env; don't double count.
                if entry.is_symlink():
                    continue
                total += entry.stat().st_size
            except OSError:
                continue
    return total


def _incomplete_env_backends(manifest: dict[str, Any], *, repair: bool = True) -> list[str]:
    """Backends whose venv exists but never recorded an install state.

    An interrupted or failed install leaves the venv behind (state is only written on
    success), so these directories can hold gigabytes while not counting as installed.
    Reporting them is what lets the UI offer to reclaim the space."""
    incomplete: list[str] = []
    for backend in manifest.get("backends", {}):
        python_path = _env_python_path(backend)
        if not python_path.is_file():
            continue
        state = _read_installed_env_state(backend, repair=repair)
        if state and str(state.get("backend") or "").strip().lower() in {"", backend}:
            continue
        if _discover_runtime_state(backend, python_path, manifest, persist=repair) is None:
            incomplete.append(backend)
    return incomplete


def _env_size_targets(manifest: dict[str, Any]) -> dict[str, Path]:
    """Backends that own a dedicated directory on disk, in the same priority order as
    _installed_envs(). The bootstrap interpreter is deliberately absent: it is the app's own
    runtime, not a removable environment, so reporting a size for it would be misleading."""
    targets: dict[str, Path] = {}
    for backend in manifest.get("backends", {}):
        if _env_python_path(backend).is_file():
            targets[backend] = _env_dir(backend)
    if BUNDLED_RUNTIME_ENVS_DIR and BUNDLED_RUNTIME_ENVS_DIR.is_dir():
        for backend in manifest.get("backends", {}):
            if backend in targets:
                continue
            bundled_env = BUNDLED_RUNTIME_ENVS_DIR / backend
            bundled_python = bundled_env / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            if bundled_python.is_file():
                targets[backend] = bundled_env
    return targets


def _probe_python_runtime(python_path: Path, extras: list[str] | None = None) -> dict[str, Any]:
    extras = extras or []
    package_names = list(_manifest()["common"].keys()) + extras
    script = """
import importlib.util, json, platform
from importlib import metadata
packages = json.loads(%PACKAGES%)
mapping = json.loads(%MAPPING%)
result = {'pythonVersion': platform.python_version(), 'torchVersion': None, 'torchBackend': 'missing', 'acceleratorAvailable': False, 'packages': {}, 'packageVersions': {}, 'pymssVersion': None, 'pymssCoreVersion': None, 'pymssGraphAvailable': False}
for name in packages:
    result['packages'][name] = importlib.util.find_spec(mapping.get(name, name)) is not None
    try:
        result['packageVersions'][name] = metadata.version(name)
    except metadata.PackageNotFoundError:
        result['packageVersions'][name] = None
result['pymssVersion'] = result['packageVersions'].get('pymss')
result['pymssCoreVersion'] = result['packageVersions'].get('pymss-core')
try:
    result['pymssGraphAvailable'] = importlib.util.find_spec('pymss.graph') is not None
except Exception:
    result['pymssGraphAvailable'] = False
if importlib.util.find_spec('torch') is not None:
    try:
        import torch
        result['torchVersion'] = torch.__version__
        result['torchBackend'] = 'rocm' if getattr(torch.version, 'hip', None) else 'cuda' if getattr(torch.version, 'cuda', None) else 'cpu'
        result['acceleratorAvailable'] = bool(torch.cuda.is_available())
    except Exception as exc:
        result['torchBackend'] = f'error:{exc}'
print(json.dumps(result, ensure_ascii=False))
""".replace("%PACKAGES%", repr(json.dumps(package_names))) \
   .replace("%MAPPING%", repr(json.dumps(PACKAGE_IMPORT_NAMES)))
    output = subprocess.check_output(
        [str(python_path), "-c", script], text=True, encoding="utf-8", errors="replace", stderr=subprocess.PIPE,
    )
    result = json.loads(output)
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("pythonVersion"), str)
        or not isinstance(result.get("packages"), dict)
        or not isinstance(result.get("packageVersions"), dict)
        or not isinstance(result.get("torchBackend"), str)
        or not isinstance(result.get("acceleratorAvailable"), bool)
        or not isinstance(result.get("pymssGraphAvailable"), bool)
        or any(not isinstance(result["packages"].get(name), bool) for name in package_names)
        or any(
            name not in result["packageVersions"]
            or not isinstance(result["packageVersions"][name], (str, type(None)))
            for name in package_names
        )
    ):
        raise ValueError("Python runtime probe returned an incomplete response")
    return result


def _probe_python_package_versions(python_path: Path, package_names: list[str]) -> dict[str, str | None]:
    if not python_path.is_file() or not package_names:
        return {}
    script = """
import json
from importlib import metadata
names = json.loads(%NAMES%)
result = {}
for name in names:
    try:
        result[name] = metadata.version(name)
    except metadata.PackageNotFoundError:
        result[name] = None
print(json.dumps(result, ensure_ascii=False))
""".replace("%NAMES%", repr(json.dumps(package_names)))
    try:
        output = subprocess.check_output([str(python_path), "-c", script], text=True, encoding="utf-8", errors="replace")
        data = json.loads(output.strip() or "{}")
        return {name: (str(data.get(name)) if data.get(name) else None) for name in package_names}
    except Exception:
        return {}


def _runtime_core_missing_records(python_path: Path) -> dict[str, str]:
    """Return core package versions whose wheel RECORD files were pruned or lost.

    Older bundled runtimes intentionally removed most ``*.dist-info`` files to reduce
    package size.  That makes pip unable to uninstall the old ``pymss`` distribution
    during an in-place core update.  Inspect only the two packages managed by the core
    update flow; never infer that a large accelerator package needs a reinstall.
    """
    if not python_path.is_file():
        return {}
    script = """
import json
from importlib import metadata

result = {}
for name in ("pymss", "pymss-core"):
    try:
        distribution = metadata.distribution(name)
        record = distribution.read_text("RECORD")
        if not record:
            result[name] = distribution.version
    except metadata.PackageNotFoundError:
        pass
print(json.dumps(result, ensure_ascii=False))
"""
    try:
        output = subprocess.check_output(
            [str(python_path), "-c", script],
            text=True,
            encoding="utf-8",
            errors="replace",
            stderr=subprocess.PIPE,
            timeout=30,
        )
        data = json.loads(output.strip() or "{}")
        if not isinstance(data, dict):
            return {}
        return {
            name: str(version)
            for name, version in data.items()
            if name in {"pymss", "pymss-core"} and version
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        # Metadata inspection is a repair aid.  If an old interpreter cannot answer,
        # let the normal pip command produce its usual diagnostic instead.
        return {}


def _installed_envs(
    manifest: dict[str, Any], *, repair: bool = True,
    active_python: Path | None = None, active_probe: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    def live_probe_for(python_path: Path, names: list[str]) -> dict[str, Any] | None:
        # A request may ask about a different backend than the active one. Reuse only
        # facts from this interpreter that cover every package required by the caller.
        if active_python and active_probe and _same_path(python_path, active_python):
            packages = active_probe.get("packages") or {}
            versions = active_probe.get("packageVersions") or {}
            if all(name in packages and name in versions for name in names):
                # Match the caller's own probe shape, so unrelated extras are not
                # added to a discovered or repaired environment's persisted cache.
                return {
                    **active_probe,
                    "packages": {name: packages[name] for name in names},
                    "packageVersions": {name: versions[name] for name in names},
                }
        return None

    def core_versions(python_path: Path) -> dict[str, str | None]:
        names = ["pymss", "pymss-core"]
        probed = live_probe_for(python_path, names)
        if probed is not None:
            versions = probed["packageVersions"]
            return {name: str(versions[name]) if versions[name] else None for name in names}
        return _probe_python_package_versions(python_path, names)

    items: list[dict[str, Any]] = []
    seen_backends: set[str] = set()
    # User-managed environments (highest priority)
    for backend in manifest.get("backends", {}):
        env_dir = _env_dir(backend)
        if repair and env_dir.is_dir() and not _is_bundled_runtime_env(env_dir):
            try:
                configuration_changed = _repair_runtime_venv_config(env_dir)
                configuration_changed = _make_posix_venv_relocatable(env_dir) or configuration_changed
            except OSError:
                configuration_changed = True
            if configuration_changed and active_python and _same_path(_env_python_path(backend), active_python):
                # Repair may replace the base interpreter or its configuration. A
                # probe from before that change is not valid for subsequent reads.
                active_probe = None
        python_path = _env_python_path(backend)
        probed = live_probe_for(python_path, _manifest_requirement_names(manifest, backend))
        state = _read_installed_env_state(backend, repair=repair, probed=probed)
        if state and str(state.get("backend") or "").strip().lower() not in {"", backend}:
            state = None
        if python_path.is_file() and state is None:
            state = _discover_runtime_state(backend, python_path, manifest, persist=repair, probed=probed)
        if state and python_path.is_file():
            package_versions = core_versions(python_path)
            items.append({
                **state,
                "backend": backend,
                "pythonPath": str(python_path),
                "logPath": str(_env_log_path(backend)),
                "health": _runtime_health(backend, state, manifest),
                "coreUpdateSupported": repair,
                "packageVersions": {**(state.get("packageVersions") or {}), **package_versions},
                "pymssVersion": (package_versions.get("pymss") or state.get("pymssVersion")),
                "pymssCoreVersion": (package_versions.get("pymss-core") or state.get("pymssCoreVersion")),
            })
            seen_backends.add(backend)
    bundled_state = _resolve_runtime_state_from(BUNDLED_RUNTIME_ENVS_DIR / "active-runtime.json") if BUNDLED_RUNTIME_ENVS_DIR else None
    if bundled_state:
        bundled_state["source"] = "bundled"
    bundled_python = _bundled_bootstrap_python()
    bundled_backend = str(bundled_state.get("backend") or "") if bundled_state else ""
    if (
        bundled_state
        and bundled_state.get("source") == "bundled"
        and bundled_python
        and bundled_backend in manifest.get("backends", {})
        and bundled_backend not in seen_backends
    ):
        try:
            probed = live_probe_for(bundled_python, _manifest_requirement_names(manifest, bundled_backend))
            if probed is None:
                probed = _probe_python_runtime(bundled_python, _backend_extra_names(manifest, bundled_backend))
            if _runtime_probe_is_ready(bundled_backend, probed, manifest):
                bundled_live_state = {
                    **bundled_state,
                    "packages": probed.get("packages"),
                    "packageVersions": probed.get("packageVersions"),
                    "torchBackend": probed.get("torchBackend"),
                }
                items.append({
                    **bundled_state,
                    "backend": bundled_backend,
                    "pythonPath": str(bundled_python),
                    "source": "bundled",
                    "health": _runtime_health(bundled_backend, bundled_live_state, manifest),
                    "coreUpdateSupported": False,
                    "packages": probed.get("packages"),
                    "packageVersions": probed.get("packageVersions"),
                    "pymssVersion": probed.get("pymssVersion"),
                    "pymssCoreVersion": probed.get("pymssCoreVersion"),
                    "pymssGraphAvailable": bool(probed.get("pymssGraphAvailable")),
                })
        except Exception:
            pass
    if BUNDLED_RUNTIME_ENVS_DIR and BUNDLED_RUNTIME_ENVS_DIR.is_dir():
        for backend in manifest.get("backends", {}):
            if backend in seen_backends:
                continue
            bundled_env = BUNDLED_RUNTIME_ENVS_DIR / backend
            bundled_state_path = bundled_env / "pymss-runtime-state.json"
            bundled_python = bundled_env / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            if not bundled_python.is_file():
                continue
            try:
                state = json.loads(bundled_state_path.read_text(encoding="utf-8")) if bundled_state_path.is_file() else None
            except Exception:
                state = None
            if isinstance(state, dict) and str(state.get("backend") or "").strip().lower() not in {"", backend}:
                state = None
            if not isinstance(state, dict):
                probed = live_probe_for(bundled_python, _manifest_requirement_names(manifest, backend))
                state = _discover_runtime_state(backend, bundled_python, manifest, persist=False, probed=probed)
            if not state:
                continue
            package_versions = core_versions(bundled_python)
            items.append({
                **state,
                "backend": backend,
                "pythonPath": str(bundled_python),
                "source": "bundled",
                "health": _runtime_health(backend, state, manifest),
                "coreUpdateSupported": False,
                "packageVersions": {**(state.get("packageVersions") or {}), **package_versions},
                "pymssVersion": package_versions.get("pymss") or state.get("pymssVersion"),
                "pymssCoreVersion": package_versions.get("pymss-core") or state.get("pymssCoreVersion"),
                "pymssGraphAvailable": state.get("pymssGraphAvailable"),
            })
            seen_backends.add(backend)
    return items


def _same_path(left: Any, right: Any) -> bool:
    try:
        left_path = _normal_runtime_path(Path(str(left))) if os.name == "nt" else str(left)
        right_path = _normal_runtime_path(Path(str(right))) if os.name == "nt" else str(right)
        return os.path.normcase(os.path.normpath(left_path)) == os.path.normcase(os.path.normpath(right_path))
    except Exception:
        return False


def _envs_with_live_active(
    manifest: dict[str, Any],
    active_python: Path | None,
    active_probe: dict[str, Any] | None,
    *,
    repair: bool = True,
) -> list[dict[str, Any]]:
    """Installed environments, with the active one's torch facts taken from the live probe.

    A packaged environment records its state on the build machine, which has no GPU, so a
    CUDA environment may claim acceleratorAvailable=false forever. The active
    environment was just probed for real, so prefer that answer over the recording. Idle
    environments keep their recorded state — probing each one would spawn an interpreter per
    environment on every refresh."""
    environments = _installed_envs(manifest, repair=repair, active_python=active_python, active_probe=active_probe)
    if not active_probe or not active_python:
        return environments
    for entry in environments:
        if not _same_path(entry.get("pythonPath"), active_python):
            continue
        active_state = {
            **entry,
            "packages": {**(entry.get("packages") or {}), **(active_probe.get("packages") or {})},
            "packageVersions": {**(entry.get("packageVersions") or {}), **(active_probe.get("packageVersions") or {})},
            "torchBackend": active_probe.get("torchBackend"),
        }
        entry.update({
            "torchVersion": active_probe.get("torchVersion"),
            "torchBackend": active_probe.get("torchBackend"),
            "acceleratorAvailable": bool(active_probe.get("acceleratorAvailable")),
            "health": _runtime_health(str(entry.get("backend") or ""), active_state, manifest),
            # Merged, not replaced: the probe's extras follow the requested backend, so a
            # replace could drop a key (mlx) the recording legitimately carries.
            "packages": {**(entry.get("packages") or {}), **(active_probe.get("packages") or {})},
            "packageVersions": {**(entry.get("packageVersions") or {}), **(active_probe.get("packageVersions") or {})},
            "pymssVersion": active_probe.get("pymssVersion") or entry.get("pymssVersion"),
            "pymssCoreVersion": active_probe.get("pymssCoreVersion") or entry.get("pymssCoreVersion"),
        })
        if "pymssGraphAvailable" in active_probe:
            entry["pymssGraphAvailable"] = bool(active_probe["pymssGraphAvailable"])
        break
    return environments


def _latest_pypi_version(distribution: str) -> str | None:
    url = f"https://pypi.org/pypi/{distribution}/json"
    with urllib.request.urlopen(url, timeout=12) as response:
        payload = json.loads(response.read().decode("utf-8"))
    version = payload.get("info", {}).get("version")
    return str(version) if version else None


def _runtime_env_dir_for_python(python_path: Path) -> Path:
    return python_path.parent.parent if python_path.parent.name.lower() in {"scripts", "bin"} else python_path.parent


def _path_is_within(path: Path, root: Path) -> bool:
    """Check containment while accepting mixed regular/extended Windows path spellings."""
    candidates = [(path, root)]
    normalized_path = Path(_normal_runtime_path(path))
    normalized_root = Path(_normal_runtime_path(root))
    if normalized_path != path or normalized_root != root:
        candidates.append((normalized_path, normalized_root))
    for child, parent in candidates:
        try:
            if child.resolve().is_relative_to(parent.resolve()):
                return True
        except (OSError, ValueError):
            continue
    return False


def _is_user_runtime_env(env_dir: Path) -> bool:
    # ``canonicalize`` on Windows can add ``\\?\\`` to one side only.  Compare both
    # spellings so a short-path command path still belongs to the configured runtime root
    # and is not rejected as an outside interpreter.  The original spelling is retained as
    # the first candidate so genuinely long paths keep their extended-path support.
    return _path_is_within(env_dir, RUNTIME_ENVS_DIR)


def _is_bundled_runtime_env(env_dir: Path) -> bool:
    if not BUNDLED_RUNTIME_ENVS_DIR:
        return False
    return _path_is_within(env_dir, BUNDLED_RUNTIME_ENVS_DIR)


def _bundled_bootstrap_python() -> Path | None:
    if not BUNDLED_RUNTIME_ENVS_DIR:
        return None
    runtime_root = BUNDLED_RUNTIME_ENVS_DIR.parent
    candidates = [
        runtime_root / "python.exe",
        runtime_root / "bin" / "python3",
        runtime_root / "bin" / "python",
    ]
    return next((path for path in candidates if path.is_file()), None)


def _is_bundled_bootstrap_python(path: Path) -> bool:
    bundled = _bundled_bootstrap_python()
    try:
        return bool(bundled and _same_path(path.resolve(), bundled.resolve()))
    except OSError:
        return False


def _target_runtime_from_payload(payload: dict[str, Any], backend: str) -> tuple[dict[str, Any] | None, Path, Path, Path] | None:
    if not _supported_backend(backend):
        return None
    target_python = _runtime_command_path(Path(str(payload.get("pythonPath")))) if payload.get("pythonPath") else None

    if target_python:
        if not target_python.is_file():
            return None
        if _is_bundled_bootstrap_python(target_python):
            state = _resolve_runtime_state_from(BUNDLED_RUNTIME_ENVS_DIR / "active-runtime.json") if BUNDLED_RUNTIME_ENVS_DIR else None
            if state and str(state.get("backend") or "").strip().lower() == backend:
                state["source"] = "bundled"
                return state, target_python.parent, Path(), target_python
            return None
        env_dir = _runtime_env_dir_for_python(target_python)
        if (not _is_user_runtime_env(env_dir) and not _is_bundled_runtime_env(env_dir)) or env_dir.name != backend:
            return None
        env_state_path = env_dir / "pymss-runtime-state.json"
        state = None
        if env_state_path.is_file():
            try:
                state = json.loads(env_state_path.read_text(encoding="utf-8"))
            except Exception:
                state = None
            if state and str(state.get("backend") or "").strip().lower() not in {"", backend}:
                return None
        if state is None:
            state = _discover_runtime_state(
                backend,
                target_python,
                _manifest(),
                persist=not _is_bundled_runtime_env(env_dir),
            )
        if state is not None and _is_bundled_runtime_env(env_dir):
            state["source"] = "bundled"
        return state, env_dir, env_state_path, target_python

    active = _read_runtime_state()
    active_python = _runtime_command_path(Path(str(active.get("pythonPath")))) if active and active.get("pythonPath") else None
    if active and str(active.get("backend") or "").strip().lower() == backend and active_python and active_python.is_file():
        env_dir = _runtime_env_dir_for_python(active_python)
        env_state_path = env_dir / "pymss-runtime-state.json"
        state = dict(active)
        if env_state_path.is_file():
            try:
                state = json.loads(env_state_path.read_text(encoding="utf-8"))
            except Exception:
                state = dict(active)
        if _is_bundled_runtime_env(env_dir):
            state["source"] = "bundled"
        return state, env_dir, env_state_path, active_python

    state = _read_installed_env_state(backend)
    env_dir = _env_dir(backend)
    env_state_path = _env_state_path(backend)
    python_path = _env_python_path(backend)
    if state and python_path.is_file():
        return state, env_dir, env_state_path, python_path

    if python_path.is_file():
        return (
            _discover_runtime_state(backend, python_path, _manifest(), persist=True),
            env_dir,
            env_state_path,
            python_path,
        )

    if BUNDLED_RUNTIME_ENVS_DIR:
        bundled_env = BUNDLED_RUNTIME_ENVS_DIR / backend
        bundled_python = bundled_env / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        bundled_state_path = bundled_env / "pymss-runtime-state.json"
        if bundled_python.is_file():
            bundled_state = None
            if bundled_state_path.is_file():
                try:
                    bundled_state = json.loads(bundled_state_path.read_text(encoding="utf-8"))
                except Exception:
                    bundled_state = None
            if bundled_state is not None:
                bundled_state["source"] = "bundled"
            return bundled_state, bundled_env, bundled_state_path, bundled_python

    bundled_state = _resolve_runtime_state_from(BUNDLED_RUNTIME_ENVS_DIR / "active-runtime.json") if BUNDLED_RUNTIME_ENVS_DIR else None
    bundled_python = _bundled_bootstrap_python()
    if (
        bundled_state
        and str(bundled_state.get("backend") or "") == backend
        and bundled_python
    ):
        bundled_state["source"] = "bundled"
        return bundled_state, bundled_python.parent, Path(), bundled_python

    return None


def _runtime_probe_is_ready(backend: str, probed: dict[str, Any], manifest: dict[str, Any]) -> bool:
    packages = probed.get("packages") or {}
    required = _manifest_requirement_names(manifest, backend)
    if not all(packages.get(name) is True for name in required):
        return False
    expected_torch_backend = "cpu" if backend == "mlx" else backend
    if probed.get("torchBackend") != expected_torch_backend:
        return False
    return True


def _runtime_health(backend: str, state: dict[str, Any] | None, manifest: dict[str, Any]) -> str:
    """Classify a cached environment without spawning another interpreter."""
    if not isinstance(state, dict):
        return "unknown"
    packages = state.get("packages")
    if not isinstance(packages, dict):
        return "unknown"
    required = _manifest_requirement_names(manifest, backend)
    if any(packages.get(name) is False for name in required):
        return "broken"
    expected_torch_backend = "cpu" if backend == "mlx" else backend
    recorded_torch_backend = str(state.get("torchBackend") or "")
    if recorded_torch_backend and recorded_torch_backend != expected_torch_backend:
        return "broken"
    if all(name in packages for name in required) and all(packages.get(name) is True for name in required):
        versions = state.get("packageVersions")
        if isinstance(versions, dict) and versions:
            manifest_ok, _failures = _manifest_versions_are_satisfied(state, manifest, backend)
            if not manifest_ok:
                return "degraded"
        return "ready"
    return "unknown"


def _manifest_requirement_names(manifest: dict[str, Any], backend: str) -> list[str]:
    """Return distribution names that the selected backend must provide."""
    return [*manifest.get("common", {}), *_backend_extra_names(manifest, backend)]


def _runtime_manifest_status(actual: Any, expected: Any) -> str:
    """Compare numeric manifest markers without treating malformed prefixes as versions."""
    actual_text = str(actual or "").strip()
    expected_text = str(expected or "").strip()
    if not all(re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value) for value in (actual_text, expected_text)):
        return "unknown"
    actual_parts = [int(part) for part in actual_text.split(".")]
    expected_parts = [int(part) for part in expected_text.split(".")]
    length = max(len(actual_parts), len(expected_parts))
    actual_parts.extend([0] * (length - len(actual_parts)))
    expected_parts.extend([0] * (length - len(expected_parts)))
    if actual_parts == expected_parts:
        return "current"
    return "older" if actual_parts < expected_parts else "newer"


def _manifest_versions_are_satisfied(
    probed: dict[str, Any],
    manifest: dict[str, Any],
    backend: str,
) -> tuple[bool, list[str]]:
    """Validate installed distribution versions against the shipped manifest.

    Pip resolves the requirements during installation, but a successful command alone is not
    enough to justify recording the new manifest version: an old, partially-installed runtime
    can still make pip exit successfully when only one package was updated.  Use the same
    requirement parser as pip when it is available in the runtime, and report missing or
    incompatible distributions to the caller before state is committed.
    """
    try:
        from packaging.requirements import Requirement
        from packaging.version import Version
    except Exception as exc:
        return False, [f"manifest version validation is unavailable: {exc}"]

    versions = probed.get("packageVersions") or {}
    requirements: dict[str, Any] = dict(manifest.get("common", {}))
    for extra in manifest.get("backends", {}).get(backend, {}).get("extras", []) or []:
        name = str(extra).split("[", 1)[0].split("=", 1)[0].strip()
        if name:
            requirements.setdefault(name, extra)
    torch_requirement = manifest.get("backends", {}).get(backend, {}).get("torch", {}).get("requirement")
    if torch_requirement:
        requirements["torch"] = torch_requirement
        versions = {**versions, "torch": probed.get("torchVersion")}
    failures: list[str] = []
    for name, requirement in requirements.items():
        actual = versions.get(name)
        if not actual:
            failures.append(f"{name} is not installed")
            continue
        try:
            parsed = Requirement(str(requirement))
            if parsed.specifier and Version(str(actual)) not in parsed.specifier:
                failures.append(f"{name}=={actual} does not satisfy {parsed.specifier}")
        except Exception as exc:
            failures.append(f"{name}: invalid requirement {requirement!r}: {exc}")
    return not failures, failures


def _runtime_probe_failure(error: Exception) -> dict[str, str]:
    # Exception strings can contain the entire probe command, subprocess output
    # or credential-bearing paths. Report only the failure category and codes.
    if isinstance(error, ValueError):
        code, reason = "PYTHON_PROBE_INVALID_RESPONSE", "Python runtime probe returned an invalid response"
    elif isinstance(error, OSError):
        code, reason = "PYTHON_START_FAILED", "Python interpreter could not be started"
    else:
        code, reason = "PYTHON_PROBE_FAILED", "Python runtime probe did not complete"
    details = type(error).__name__
    if isinstance(error, subprocess.CalledProcessError):
        details += f", exit code {error.returncode}"
    elif isinstance(error, OSError):
        winerror = getattr(error, "winerror", None)
        if isinstance(winerror, int):
            details += f", WinError {winerror}"
        elif isinstance(error.errno, int):
            details += f", errno {error.errno}"
    return {"code": code, "message": f"{reason} ({details})."[:240]}


def _torch_probe_failure(torch_backend: str) -> dict[str, str]:
    details = re.search(r"\b(?:ImportError|ModuleNotFoundError|OSError|RuntimeError)\b", torch_backend)
    winerror = re.search(r"\bwinerror\s+(\d{1,8})\b", torch_backend, re.IGNORECASE)
    parts = [details.group(0)] if details else []
    if winerror:
        parts.append(f"WinError {winerror.group(1)}")
    suffix = f" ({', '.join(parts)})" if parts else ""
    return {"code": "TORCH_IMPORT_FAILED", "message": f"Torch import failed{suffix}."}


def _runtime_info_payload(payload: dict[str, Any], *, repair: bool = True) -> dict[str, Any]:
    manifest = _manifest()
    backend = str(payload.get("backend") or "").strip() or None
    install_state = _read_runtime_state()
    packages = {name: _module_available(PACKAGE_IMPORT_NAMES.get(name, name)) for name in manifest["common"]}
    package_versions = None
    pymss_version = None
    pymss_core_version = None
    pymss_graph_available = None
    extra_names = _backend_extra_names(manifest, backend)
    # With no explicit backend the caller is asking "what am I running on"; on macOS that answer
    # depends on whether MLX is present, so probe for it even though no backend was requested.
    if not backend and sys.platform == "darwin" and "mlx" not in extra_names:
        extra_names = [*extra_names, "mlx"]
    for name in extra_names:
        packages[name] = _module_available(name)
    torch_version = None
    torch_backend = "missing"
    accelerator_available = False
    live_probe_error: dict[str, str] | None = None
    active_probe: dict[str, Any] | None = None
    active_python = _runtime_command_path(Path(str(install_state.get("pythonPath")))) if install_state and install_state.get("pythonPath") else None
    if active_python and active_python.is_file():
        try:
            probed = _probe_python_runtime(active_python, extra_names)
            active_probe = probed
            packages = dict(probed.get("packages") or packages)
            package_versions = probed.get("packageVersions")
            pymss_version = probed.get("pymssVersion")
            pymss_core_version = probed.get("pymssCoreVersion")
            pymss_graph_available = probed.get("pymssGraphAvailable")
            torch_version = probed.get("torchVersion")
            torch_backend = str(probed.get("torchBackend") or torch_backend)
            accelerator_available = bool(probed.get("acceleratorAvailable"))
        except Exception as exc:
            live_probe_error = _runtime_probe_failure(exc)
            package_versions = install_state.get("packageVersions")
            pymss_version = install_state.get("pymssVersion")
            pymss_core_version = install_state.get("pymssCoreVersion")
            torch_version = install_state.get("torchVersion")
    elif _module_available("torch"):
        try:
            probed = _probe_python_runtime(Path(sys.executable), extra_names)
            active_probe = probed
            packages = dict(probed.get("packages") or packages)
            package_versions = probed.get("packageVersions")
            pymss_version = probed.get("pymssVersion")
            pymss_core_version = probed.get("pymssCoreVersion")
            pymss_graph_available = probed.get("pymssGraphAvailable")
            torch_version = probed.get("torchVersion")
            torch_backend = str(probed.get("torchBackend") or torch_backend)
            accelerator_available = bool(probed.get("acceleratorAvailable"))
        except Exception as exc:
            live_probe_error = _runtime_probe_failure(exc)
    if live_probe_error is None and torch_backend.startswith("error:"):
        live_probe_error = _torch_probe_failure(torch_backend)
    if live_probe_error:
        # Cached versions remain useful for display, but not as evidence that
        # dependencies, devices or graph execution are currently available.
        packages = {name: False for name in packages}
        torch_backend = f"error:{live_probe_error['code']}"
        accelerator_available = False
        pymss_graph_available = None
        active_probe = None
    if not pymss_version and package_versions:
        pymss_version = package_versions.get("pymss")
    if not pymss_core_version and package_versions:
        pymss_core_version = package_versions.get("pymss-core")
    environments = _envs_with_live_active(manifest, active_python, active_probe, repair=repair)
    if live_probe_error and active_python:
        for entry in environments:
            if _same_path(entry.get("pythonPath"), active_python):
                entry.update({
                    "health": "broken", "torchBackend": torch_backend,
                    "acceleratorAvailable": False, "pymssGraphAvailable": None,
                    "packages": {name: False for name in (entry.get("packages") or {})},
                })
    return {
        "manifestVersion": manifest["manifestVersion"],
        "pythonVersion": platform.python_version(),
        "platform": sys.platform,
        "machine": platform.machine(),
        "bootstrapPython": str(_bootstrap_python_path()),
        "runtimeEnvsDir": str(RUNTIME_ENVS_DIR),
        "activeRuntimeFile": str(ACTIVE_RUNTIME_FILE),
        "backend": backend,
        "installedBackend": install_state.get("backend") if install_state else None,
        "installState": install_state,
        "statePath": str(ACTIVE_RUNTIME_FILE),
        "logPath": str(install_state.get("logPath")) if install_state and install_state.get("logPath") else None,
        "installedEnvironments": environments,
        "gpuVendors": _detect_gpu_vendors(),
        "torchVersion": torch_version,
        "torchBackend": torch_backend,
        "acceleratorAvailable": accelerator_available,
        "packages": packages,
        "packageVersions": package_versions,
        "pymssVersion": pymss_version,
        "pymssCoreVersion": pymss_core_version,
        "pymssGraphAvailable": pymss_graph_available,
        "liveProbeError": live_probe_error,
        # mlx is an optional extra: it must not drag readiness down when it was probed
        # speculatively (no backend requested). An explicit mlx backend still requires it below.
        "ready": live_probe_error is None and all(v for k, v in packages.items() if k != "mlx" or backend == "mlx") and torch_backend != "missing" and not torch_backend.startswith("error:") and (
            not backend or backend == "cpu" and torch_backend == "cpu"
            or backend == "cuda" and torch_backend == "cuda" and accelerator_available
            or backend == "rocm" and torch_backend == "rocm" and accelerator_available
            or backend == "mlx" and packages.get("mlx", False)
        ),
    }


@partial(_serialized_runtime_command, query=True)
def cmd_runtime_info(payload: dict[str, Any], *, repair: bool = True) -> int:
    if repair:
        _recover_runtime_transactions()
    _emit("runtime_info", _runtime_info_payload(payload, repair=repair))
    return 0


def cmd_runtime_core_versions(payload: dict[str, Any]) -> int:
    del payload
    packages: dict[str, dict[str, str | None]] = {}
    for name in ("pymss", "pymss-core"):
        try:
            packages[name] = {"latestVersion": _latest_pypi_version(name)}
        except Exception as exc:
            packages[name] = {"latestVersion": None, "error": str(exc)}
    _emit("runtime_core_versions", {"packages": packages})
    return 0


@partial(_serialized_runtime_command, query=True)
def cmd_runtime_env_sizes(payload: dict[str, Any], *, repair: bool = True) -> int:
    """Disk usage per installed environment. Split out of runtime_info on purpose: walking a
    multi-GB venv takes long enough that it would slow down every startup and every refresh."""
    del payload
    sizes: dict[str, int] = {}
    incomplete: list[str] = []
    if repair:
        _recover_runtime_transactions()
    manifest = _manifest()
    try:
        targets = _env_size_targets(manifest)
        incomplete = _incomplete_env_backends(manifest, repair=repair)
    except Exception:
        # Probing the directories can fail outright (permissions, a vanished data root).
        # Sizes are supplementary, so degrade to "unknown" instead of failing the command.
        targets = {}
    for backend, env_dir in targets.items():
        try:
            sizes[backend] = _dir_size_bytes(env_dir)
        except Exception:
            continue
    _emit("runtime_env_sizes", {
        "sizes": sizes,
        "totalBytes": sum(sizes.values()),
        "incompleteBackends": incomplete,
    })
    return 0


@_serialized_runtime_command
def cmd_activate_runtime(payload: dict[str, Any]) -> int:
    backend = str(payload.get("backend") or "").strip().lower()
    supported = _supported_backend(backend)
    if not supported:
        from worker_protocol import emit_error
        return emit_error("RUNTIME_BACKEND_UNSUPPORTED", f"Unsupported runtime backend: {backend or 'missing'}")
    backend, _spec = supported
    target = _target_runtime_from_payload(payload, backend)
    if not target:
        from worker_protocol import emit_error
        return emit_error("RUNTIME_NOT_INSTALLED", f"Backend {backend} is not installed")
    state, env_dir, env_state_path, python_path = target
    manifest = _manifest()

    def preserve_existing_pointer() -> bool:
        if not payload.get("onlyIfNoActive"):
            return False
        try:
            # The startup caller uses this guard to avoid replacing a pointer that a user
            # created while the probe was in flight.  A file that cannot be parsed or whose
            # interpreter disappeared is not an active environment, though, and must not block
            # recovery of the only usable runtime.
            state = _resolve_runtime_state_from(ACTIVE_RUNTIME_FILE)
            if not state or not state.get("pythonPath"):
                return False
            pointer_backend = str(state.get("backend") or "").strip().lower()
            if not _supported_backend(pointer_backend):
                return False
            pointer_python = _runtime_command_path(Path(str(state["pythonPath"])))
            if not pointer_python.is_file():
                return False
            if _is_bundled_bootstrap_python(pointer_python):
                return True
            pointer_env = _runtime_env_dir_for_python(pointer_python)
            return (
                pointer_env.name == pointer_backend
                and (_is_user_runtime_env(pointer_env) or _is_bundled_runtime_env(pointer_env))
            )
        except OSError:
            # Startup recovery must not replace a pointer it cannot safely inspect.
            return True

    # The startup probe and this command are separate operations. A user action can create an
    # active pointer between them, so honour the guard for both bundled and managed runtimes.
    if preserve_existing_pointer():
        return 0
    if _is_bundled_runtime_env(env_dir) or _is_bundled_bootstrap_python(python_path):
        try:
            probed = _probe_python_runtime(python_path, _backend_extra_names(manifest, backend))
        except Exception as exc:
            from worker_protocol import emit_error
            return emit_error("RUNTIME_ACTIVATION_FAILED", f"Bundled runtime {backend} failed validation: {exc}", recoverable=True)
        if not _runtime_probe_is_ready(backend, probed, manifest):
            from worker_protocol import emit_error
            return emit_error(
                "RUNTIME_ACTIVATION_FAILED",
                f"Bundled runtime {backend} failed validation: torchBackend={probed.get('torchBackend')}, "
                f"acceleratorAvailable={probed.get('acceleratorAvailable')}",
                recoverable=True,
            )
        # Startup recovery is intentionally non-destructive.  The frontend may have inspected
        # an empty pointer just before a user switched to a managed runtime.  In that mode leave
        # the pointer untouched; the packaged active-runtime.json is the fallback for a genuinely
        # empty user state, so no write or unlink is needed.
        if not payload.get("onlyIfNoActive"):
            try:
                ACTIVE_RUNTIME_FILE.unlink()
            except FileNotFoundError:
                pass
        active = {
            **(state or {}),
            "backend": backend,
            "pythonPath": str(python_path),
            "source": "bundled",
            "activatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _emit("runtime_activated", active)
        return 0
    try:
        probed = _probe_python_runtime(python_path, _backend_extra_names(manifest, backend))
    except Exception as exc:
        from worker_protocol import emit_error
        return emit_error(
            "RUNTIME_ACTIVATION_FAILED",
            f"Runtime {backend} failed validation: {exc}",
            recoverable=True,
        )
    if not _runtime_probe_is_ready(backend, probed, manifest):
        from worker_protocol import emit_error
        return emit_error(
            "RUNTIME_ACTIVATION_FAILED",
            f"Runtime {backend} failed validation: torchBackend={probed.get('torchBackend')}, "
            f"acceleratorAvailable={probed.get('acceleratorAvailable')}",
            recoverable=True,
        )
    if not state:
        state = _fresh_runtime_state_from_probe(backend, manifest, probed)
    else:
        state = {
            **state,
            "stateVersion": max(int(state.get("stateVersion") or 1), ENV_STATE_VERSION),
            "pythonVersion": probed.get("pythonVersion") or state.get("pythonVersion"),
            "torchVersion": probed.get("torchVersion"),
            "torchBackend": probed.get("torchBackend"),
            "acceleratorAvailable": bool(probed.get("acceleratorAvailable")),
            "packages": probed.get("packages") or state.get("packages"),
            "packageVersions": probed.get("packageVersions") or state.get("packageVersions"),
            "pymssVersion": probed.get("pymssVersion") or state.get("pymssVersion"),
            "pymssCoreVersion": probed.get("pymssCoreVersion") or state.get("pymssCoreVersion"),
            "pymssGraphAvailable": bool(probed.get("pymssGraphAvailable")),
        }
    if env_state_path:
        try:
            _atomic_write_json(env_state_path, state)
        except OSError:
            # Activation remains valid when state persistence is unavailable; the next probe can
            # rebuild the cache again from the interpreter.
            pass
    active = {
        **state,
        "backend": backend,
        "pythonPath": str(python_path),
        "logPath": str(env_dir / "pymss-runtime-install.log"),
        "activatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    # Close the remaining race window while the live probe was running. Per-environment cache
    # refreshes above are harmless, but an active pointer created meanwhile belongs to the user.
    if preserve_existing_pointer():
        return 0
    _write_runtime_state(active)
    _emit("runtime_activated", active)
    return 0


@_serialized_runtime_command
def cmd_delete_runtime(payload: dict[str, Any]) -> int:
    backend = str(payload.get("backend") or "").strip().lower()
    supported = _supported_backend(backend)
    if not supported:
        from worker_protocol import emit_error
        return emit_error("RUNTIME_BACKEND_UNSUPPORTED", f"Unsupported runtime backend: {backend or 'missing'}")
    backend, _spec = supported
    target = _target_runtime_from_payload(payload, backend)
    if not target:
        from worker_protocol import emit_error
        return emit_error("RUNTIME_NOT_INSTALLED", f"Backend {backend} is not installed")
    _state, env_dir, _env_state_path_value, python_path = target
    if _is_bundled_runtime_env(env_dir) or _is_bundled_bootstrap_python(python_path):
        from worker_protocol import emit_error
        return emit_error("RUNTIME_BUNDLED_READ_ONLY", f"Bundled runtime {backend} cannot be deleted.")
    active = _read_runtime_state()
    active_python = _runtime_command_path(Path(str(active.get("pythonPath")))) if active and active.get("pythonPath") else None
    if active and active_python and _same_path(active_python, python_path):
        from worker_protocol import emit_error
        return emit_error("RUNTIME_DELETE_ACTIVE", f"Cannot delete the currently active runtime ({backend}). Switch to another environment first.")
    import shutil
    try:
        if env_dir.is_dir():
            shutil.rmtree(str(env_dir))
        if active_python and _same_path(active_python, python_path):
            try:
                ACTIVE_RUNTIME_FILE.unlink()
            except Exception:
                pass
        _emit("runtime_deleted", {"backend": backend})
        return 0
    except Exception as exc:
        from worker_protocol import emit_error
        return emit_error(
            "RUNTIME_PERMISSION_DENIED" if isinstance(exc, PermissionError) else "RUNTIME_DELETE_FAILED",
            str(exc),
        )


def _normal_runtime_path(path: Path) -> str:
    """Return a normal Win32 path before handing a runtime directory to ``venv``.

    Rust's ``canonicalize`` returns a ``\\\\?\\`` long-path prefix on Windows.  The prefix is
    valid for ordinary file I/O, but Python 3.12's venv/ensurepip bootstrap intermittently fails
    when its destination (and the child interpreter it launches) keeps that prefix, especially
    for repository paths containing non-ASCII characters.
    """
    value = os.fspath(path)
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[len("\\\\?\\UNC\\"):]
    if value.startswith("\\\\?\\"):
        return value[len("\\\\?\\"):]
    return value


def _runtime_command_path(path: Path) -> Path:
    """Use a regular Win32 path for short runtime commands, retaining long-path support."""
    normal = _normal_runtime_path(path)
    if os.name == "nt" and len(normal) < 240:
        return Path(normal)
    return path


def _create_runtime_venv(env_dir: Path) -> None:
    import venv

    venv.EnvBuilder(with_pip=True, clear=True, symlinks=(os.name != "nt")).create(_normal_runtime_path(env_dir))


def _runtime_python_works(python_path: Path) -> bool:
    try:
        return subprocess.run(
            [str(python_path), "-c", "import sys; print(sys.prefix)"],
            capture_output=True,
            text=True,
            timeout=20,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _runtime_pip_works(python_path: Path) -> bool:
    try:
        return subprocess.run(
            [str(python_path), "-m", "pip", "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _ensure_runtime_pip(
    python_path: Path,
    task_id: str,
    append_log: Any,
) -> None:
    """Repair an interrupted venv before the first pip operation.

    A killed install can leave Scripts/python.exe in place while ensurepip never ran. Treat that
    directory as recoverable instead of reusing it until every later pip command fails.
    """
    if _runtime_pip_works(python_path):
        return

    message = "pip is unavailable; bootstrapping it with ensurepip"
    append_log("bootstrap", message)
    _emit("runtime_install_log", {"stage": "bootstrap", "message": message}, task_id)
    result = subprocess.run(
        [str(python_path), "-m", "ensurepip", "--upgrade", "--default-pip"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    for line in result.stdout.splitlines():
        message = line.rstrip()
        append_log("bootstrap", message)
        _emit("runtime_install_log", {"stage": "bootstrap", "message": message}, task_id)
    if result.returncode != 0:
        raise RuntimeError(f"pip bootstrap failed with exit code {result.returncode}")

    if not _runtime_pip_works(python_path):
        raise RuntimeError("pip bootstrap completed but pip is still unavailable")


def _repair_runtime_venv_config(env_dir: Path) -> bool:
    cfg = env_dir / "pyvenv.cfg"
    bootstrap = _bootstrap_python_path()
    if not cfg.is_file() or not bootstrap.is_file():
        return False
    content = "\n".join([
        f"home = {_normal_runtime_path(bootstrap.parent)}",
        "include-system-site-packages = false",
        f"executable = {_normal_runtime_path(bootstrap)}",
        f"command = {_normal_runtime_path(bootstrap)} -m venv {_normal_runtime_path(env_dir)}",
        "",
    ])
    if cfg.read_text(encoding="utf-8", errors="replace") != content:
        _atomic_write_text(cfg, content)
        return True
    return False


def _make_posix_venv_relocatable(env_dir: Path) -> bool:
    if os.name == "nt":
        return False
    bin_dir = env_dir / "bin"
    bootstrap = _bootstrap_python_path().resolve()
    if not bin_dir.is_dir() or not bootstrap.is_file():
        return False
    relative = os.path.relpath(bootstrap, bin_dir)
    names = {"python", "python3", f"python{sys.version_info.major}.{sys.version_info.minor}"}
    changed = False
    for name in names:
        path = bin_dir / name
        try:
            if path.is_symlink() and path.resolve() == bootstrap:
                continue
        except OSError:
            pass
        temporary = bin_dir / f".{name}.{os.getpid()}.pymss-tmp"
        try:
            temporary.unlink(missing_ok=True)
            temporary.symlink_to(relative)
            os.replace(temporary, path)
            changed = True
        finally:
            temporary.unlink(missing_ok=True)
    return changed


@_serialized_runtime_command
def cmd_install_runtime(payload: dict[str, Any]) -> int:
    """Build and activate an environment for `backend`.

    Cancellation is not handled here: the desktop shell kills this process and its whole tree
    (see cancel_task in app_cmd.rs), which takes pip down with it. The interrupted venv is left
    on disk on purpose — _incomplete_env_backends() reports it so the space can be reclaimed."""
    task_id = str(payload.get("taskId") or f"runtime_install_{int(time.time() * 1000)}")
    backend = str(payload.get("backend") or "cpu").strip().lower()
    mirror = str(payload.get("mirror") or "auto").strip().lower()
    locale = str(payload.get("locale") or "").strip().lower()
    manifest = _manifest()
    _recover_runtime_transactions()
    supported = _supported_backend(backend, manifest)
    if not supported:
        from worker_protocol import emit_error
        return emit_error("RUNTIME_BACKEND_UNSUPPORTED", f"Unsupported runtime backend: {backend}", task_id=task_id)
    backend, spec = supported
    if sys.platform not in spec.get("platforms", []):
        from worker_protocol import emit_error
        return emit_error("RUNTIME_PLATFORM_UNSUPPORTED", f"Backend {backend} is not supported on {sys.platform}", task_id=task_id)
    index_url = None
    mirror, index_url = _resolve_pypi_mirror(mirror, locale)
    env_dir = _env_dir(backend)
    try:
        had_existing_environment = env_dir.is_dir() and any(env_dir.iterdir())
    except OSError:
        had_existing_environment = env_dir.exists()
    env_dir.mkdir(parents=True, exist_ok=True)
    env_python = _env_python_path(backend)
    install_log_path = _env_log_path(backend)
    reinstall_backup: Path | None = None
    if had_existing_environment:
        reinstall_backup = RUNTIME_ENVS_DIR / f".{backend}.reinstalling"
        if reinstall_backup.exists():
            shutil.rmtree(reinstall_backup)
        env_dir.rename(reinstall_backup)
        # The install log is written before venv creation, so recreate the destination root after
        # moving the previous environment aside. Without this, every real reinstall fails before
        # the first pip command because the log's parent directory no longer exists.
        env_dir.mkdir(parents=True, exist_ok=True)
    def append_log(stage: str, message: str) -> None:
        with install_log_path.open("a", encoding="utf-8", errors="replace") as file:
            file.write(f"[{stage}] {message}\n")

    def run_pip(args: list[str], stage: str, package_index: str | None = None) -> None:
        command = [str(env_python), "-m", "pip", "install", "--no-cache-dir"]
        if stage == "pymss":
            command.append("--upgrade")
        if stage in {"common", "pymss"}:
            command.extend(["--only-binary=:all:", "--prefer-binary"])
        if package_index or (index_url and stage != "torch"):
            command.extend(["--index-url", package_index or index_url])
        command.extend(args)
        append_log(stage, "pip install " + " ".join(args))
        _emit("runtime_install_stage", {"stage": stage, "command": "pip install " + " ".join(args)}, task_id)
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=os.environ.copy())
        assert process.stdout is not None
        try:
            for line in process.stdout:
                message = line.rstrip()
                append_log(stage, message)
                _emit("runtime_install_log", {"stage": stage, "message": message}, task_id)
        except Exception:
            # If the protocol pipe is gone, do not leave pip orphaned while the worker reports
            # a failure.  An orphaned install can keep files locked and make the next click look
            # like a successful retry only because the first attempt continued in the background.
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
        if process.wait() != 0:
            raise RuntimeError(f"pip failed during {stage} with exit code {process.returncode}")

    def run_pip_with_pypi_fallback(args: list[str], stage: str) -> None:
        try:
            run_pip(args, stage)
        except RuntimeError:
            if mirror == "pypi":
                raise
            append_log(stage, "selected mirror failed, retrying with PyPI")
            _emit("runtime_install_stage", {"stage": stage, "message": "Selected mirror failed, retrying with PyPI"}, task_id)
            run_pip(args, stage, "https://pypi.org/simple")

    try:
        install_log_path.write_text(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] install backend={backend} mirror={mirror} manifest={manifest['manifestVersion']}\n",
            encoding="utf-8",
        )
        rebuild_message = ""
        _repair_runtime_venv_config(env_dir)
        if not env_python.is_file():
            _create_runtime_venv(env_dir)
            # A newly created venv writes pyvenv.cfg from the bootstrap interpreter.  When
            # that interpreter was launched through a Windows extended path, venv copies the
            # ``\\\\?\\`` prefix into the config.  Normalize it before the first pip process
            # starts so fresh installs and repaired installs follow the same path rules.
            _repair_runtime_venv_config(env_dir)
        elif not _runtime_python_works(env_python):
            rebuild_message = "existing environment interpreter is unusable; rebuilding"
            _emit("runtime_install_log", {"stage": "venv", "message": rebuild_message}, task_id)
            import shutil
            shutil.rmtree(env_dir, ignore_errors=True)
            env_dir.mkdir(parents=True, exist_ok=True)
            _create_runtime_venv(env_dir)
            _repair_runtime_venv_config(env_dir)
        elif not _runtime_pip_works(env_python):
            # A cancelled or older portable install can leave a runnable venv without pip. The
            # venv's ensurepip module may have been pruned already, so recreate it from the
            # bootstrap runtime instead of failing the first package install.
            rebuild_message = "existing environment has no pip; rebuilding the virtual environment"
            _emit("runtime_install_log", {
                "stage": "bootstrap",
                "message": rebuild_message,
            }, task_id)
            import shutil
            shutil.rmtree(env_dir, ignore_errors=True)
            env_dir.mkdir(parents=True, exist_ok=True)
            _create_runtime_venv(env_dir)
            _repair_runtime_venv_config(env_dir)
        if rebuild_message:
            append_log("bootstrap", rebuild_message)
        _make_posix_venv_relocatable(env_dir)
        _emit("runtime_install_started", {"backend": backend, "manifestVersion": manifest["manifestVersion"], "logPath": str(install_log_path)}, task_id)
        _ensure_runtime_pip(env_python, task_id, append_log)
        torch = spec.get("torch", {})
        if torch.get("rocmRequirements"):
            run_pip(list(torch["rocmRequirements"]), "rocm-sdk", None)
        torch_args = list(torch.get("requirements", [])) if torch.get("requirements") else [torch["requirement"]]
        run_pip((["--no-deps"] if torch.get("noDeps") else []) + torch_args, "torch", torch.get("indexUrl"))
        common = [value for name, value in manifest["common"].items() if name not in {"pymss", "pymss-core"}]
        pymss_requirement = manifest["common"]["pymss"]
        pymss_core_requirement = manifest["common"]["pymss-core"]
        run_pip_with_pypi_fallback(common, "common")
        # Install the declared pymss extras with dependency resolution enabled.  The previous
        # --no-deps path silently skipped PySocks required by pymss[proxy], so a fresh runtime
        # passed the basic import probe but failed when a SOCKS proxy was used.
        run_pip_with_pypi_fallback([pymss_requirement, pymss_core_requirement], "pymss")
        if spec.get("extras"):
            run_pip_with_pypi_fallback(list(spec["extras"]), "extras")
        # Probe the interpreter that was just built, not _runtime_info_payload(): that one reads
        # the *active* runtime, which at this point is still the previously activated environment
        # — recording its torch build here is what made a CPU env report cu128.
        probed = _probe_python_runtime(env_python, _backend_extra_names(manifest, backend))
        if not _runtime_probe_is_ready(backend, probed, manifest):
            missing = [
                name for name in [*manifest.get("common", {}), *_backend_extra_names(manifest, backend)]
                if (probed.get("packages") or {}).get(name) is not True
            ]
            raise RuntimeError(
                f"Runtime probe failed for {backend}: missing packages={missing}, "
                f"torchBackend={probed.get('torchBackend')}, "
                f"acceleratorAvailable={probed.get('acceleratorAvailable')}"
            )
        manifest_ok, manifest_failures = _manifest_versions_are_satisfied(probed, manifest, backend)
        if not manifest_ok:
            raise RuntimeError(
                "Runtime manifest verification failed: " + "; ".join(manifest_failures)
            )
        state = {
            "backend": backend,
            "manifestVersion": manifest["manifestVersion"],
            "stateVersion": ENV_STATE_VERSION,
            "installedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "pythonVersion": probed.get("pythonVersion") or platform.python_version(),
            "torchVersion": probed.get("torchVersion"),
            "torchBackend": probed.get("torchBackend"),
            "acceleratorAvailable": bool(probed.get("acceleratorAvailable")),
            "packages": probed.get("packages"),
            "packageVersions": probed.get("packageVersions"),
            "pymssVersion": probed.get("pymssVersion"),
            "pymssCoreVersion": probed.get("pymssCoreVersion"),
            "pymssGraphAvailable": bool(probed.get("pymssGraphAvailable")),
        }
        _atomic_write_json(_env_state_path(backend), state)
        active = {
            **state,
            "pythonPath": str(env_python),
            "logPath": str(install_log_path),
            "activatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _write_runtime_state(active)
        if reinstall_backup and reinstall_backup.exists():
            import shutil
            try:
                shutil.rmtree(reinstall_backup)
            except OSError as backup_error:
                append_log("warning", f"failed to remove reinstall backup: {backup_error}")
        _emit("runtime_install_finished", {"backend": backend, "state": active, "logPath": str(install_log_path)}, task_id)
        return 0
    except Exception as exc:
        from worker_protocol import emit_error
        if reinstall_backup and reinstall_backup.exists():
            import shutil
            if env_dir.exists():
                shutil.rmtree(env_dir, ignore_errors=True)
            reinstall_backup.rename(env_dir)
        append_log("error", str(exc))
        return emit_error(
            "RUNTIME_PERMISSION_DENIED" if isinstance(exc, PermissionError) else "RUNTIME_INSTALL_FAILED",
            str(exc),
            detail=f"详细安装日志：{install_log_path}",
            task_id=task_id,
            recoverable=True,
            extra={"backend": backend, "logPath": str(install_log_path)},
        )


@_serialized_runtime_command
def cmd_update_runtime_core(payload: dict[str, Any]) -> int:
    task_id = str(payload.get("taskId") or f"runtime_core_update_{int(time.time() * 1000)}")
    backend = str(payload.get("backend") or "").strip().lower()
    mirror = str(payload.get("mirror") or "auto").strip().lower()
    locale = str(payload.get("locale") or "").strip().lower()
    manifest = _manifest()
    _recover_runtime_transactions()
    supported = _supported_backend(backend)
    if not supported:
        from worker_protocol import emit_error
        return emit_error("RUNTIME_BACKEND_UNSUPPORTED", f"Unsupported runtime backend: {backend or 'missing'}", task_id=task_id)
    backend, _spec = supported

    target = _target_runtime_from_payload(payload, backend)
    if not target:
        from worker_protocol import emit_error
        return emit_error("RUNTIME_NOT_INSTALLED", f"Backend {backend} is not installed", task_id=task_id)
    state, env_dir, env_state_path, python_path = target
    if not state:
        from worker_protocol import emit_error
        return emit_error(
            "RUNTIME_NOT_INSTALLED",
            f"Backend {backend} has no completed installation state",
            task_id=task_id,
            recoverable=True,
        )
    if _is_bundled_runtime_env(env_dir) or _is_bundled_bootstrap_python(python_path):
        from worker_protocol import emit_error
        return emit_error("RUNTIME_CORE_UPDATE_UNSUPPORTED", "Bundled runtime environments cannot be updated in place; install a user-managed environment first.", task_id=task_id, recoverable=True)
    if not python_path.is_file():
        from worker_protocol import emit_error
        return emit_error("RUNTIME_NOT_INSTALLED", f"Active runtime Python not found: {python_path}", task_id=task_id)
    if _same_path(python_path, _bootstrap_python_path()):
        from worker_protocol import emit_error
        return emit_error("RUNTIME_CORE_UPDATE_UNSUPPORTED", "The app bootstrap Python runtime cannot be updated in place", task_id=task_id)
    # Core update is only available for the currently active runtime
    active = _read_runtime_state()
    active_python = _runtime_command_path(Path(str(active.get("pythonPath")))) if active and active.get("pythonPath") else None
    if not active_python or not _same_path(active_python, python_path):
        from worker_protocol import emit_error
        return emit_error("RUNTIME_CORE_UPDATE_INACTIVE", "Core update is only available for the currently active runtime. Please switch to this environment first.", task_id=task_id, recoverable=True)
    manifest_status = _runtime_manifest_status(state.get("manifestVersion"), manifest.get("manifestVersion"))
    if manifest_status not in {"current", "older"}:
        from worker_protocol import emit_error
        return emit_error(
            "RUNTIME_MANIFEST_INCOMPATIBLE",
            "The runtime manifest is newer than this application or has an unknown version. "
            "Use a compatible application version or reinstall the runtime before updating core packages.",
            task_id=task_id,
            recoverable=True,
        )
    mirror, index_url = ("pypi", PYPI_MIRROR_URLS["pypi"]) if mirror == "auto" else _resolve_pypi_mirror(mirror, locale)
    try:
        target_pymss_version = _latest_pypi_version("pymss")
    except Exception as exc:
        from worker_protocol import emit_error
        return emit_error("RUNTIME_CORE_UPDATE_FAILED", f"Failed to resolve latest pymss version from PyPI: {exc}", task_id=task_id, recoverable=True)
    try:
        target_pymss_core_version = _latest_pypi_version("pymss-core")
    except Exception as exc:
        from worker_protocol import emit_error
        return emit_error("RUNTIME_CORE_UPDATE_FAILED", f"Failed to resolve latest pymss-core version from PyPI: {exc}", task_id=task_id, recoverable=True)
    log_path = env_dir / "pymss-core-update.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] update pymss core mirror={mirror}\n", encoding="utf-8")
    _emit("runtime_core_update_started", {"backend": backend, "logPath": str(log_path)}, task_id)
    previous_active_state = _read_runtime_state()

    staging_dir: Path | None = None
    swap_backup_dir: Path | None = None

    def append_log(stage: str, message: str) -> None:
        with log_path.open("a", encoding="utf-8", errors="replace") as file:
            file.write(f"[{stage}] {message}\n")
        # The active directory is copied before pip runs.  Mirror subsequent log lines into the
        # staged copy so the final environment retains the complete update log after the swap.
        if staging_dir and staging_dir.exists():
            try:
                staged_log = staging_dir / log_path.name
                with staged_log.open("a", encoding="utf-8", errors="replace") as file:
                    file.write(f"[{stage}] {message}\n")
            except OSError:
                pass

    # Keep dependency resolution enabled so new dependencies introduced by pymss are installed,
    # but constrain Torch to the build already installed in this backend.
    torch_version = str((state or {}).get("torchVersion") or "").strip()
    if not torch_version:
        try:
            torch_version = str(_probe_python_runtime(python_path).get("torchVersion") or "").strip()
        except Exception:
            torch_version = ""
    if not torch_version:
        from worker_protocol import emit_error
        return emit_error("RUNTIME_CORE_UPDATE_FAILED", "Unable to determine the installed Torch version; refusing to update the runtime core.", task_id=task_id, recoverable=True)
    constraints_path: Path | None = None
    try:
        # Work on a sibling copy.  The active environment remains runnable while pip resolves
        # packages, and a failed update can discard the copy without touching the user's working
        # runtime.  The final directory swap is performed only after the live probe succeeds.
        environment_bytes = _dir_size_bytes(env_dir)
        free_bytes = shutil.disk_usage(RUNTIME_ENVS_DIR).free
        required_bytes = environment_bytes + 256 * 1024 * 1024
        if free_bytes < required_bytes:
            raise RuntimeError(
                "Insufficient disk space for a transactional runtime update: "
                f"need {required_bytes} bytes, available {free_bytes} bytes"
            )
        staging_dir = RUNTIME_ENVS_DIR / f".{backend}.core-updating"
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        shutil.copytree(env_dir, staging_dir, symlinks=True)
        _repair_runtime_venv_config(staging_dir)
        work_python_path = _python_path_for_env(staging_dir)
        if not work_python_path.is_file():
            raise RuntimeError(f"Staged runtime Python not found: {work_python_path}")

        constraints_path = staging_dir / ".pymss-core-update-constraints.txt"
        _atomic_write_text(constraints_path, f"torch=={torch_version}\n")
        command = [str(work_python_path), "-m", "pip", "install", "--upgrade", "--no-cache-dir", "--only-binary=:all:", "--prefer-binary"]
        command.extend(["--constraint", str(constraints_path)])
        if index_url:
            command.extend(["--index-url", index_url])
        # Keep extras declared by the shipped manifest (currently ``[proxy]``) when
        # upgrading an environment created by an older manifest.  Installing only
        # the bare distribution would leave newly declared optional dependencies
        # absent even though the core package itself was updated successfully.
        pymss_requirement = _pin_manifest_requirement(
            manifest.get("common", {}).get("pymss"),
            target_pymss_version,
        )
        pymss_core_requirement = f"pymss-core=={target_pymss_core_version}"
        common_requirements = [
            str(requirement)
            for name, requirement in manifest.get("common", {}).items()
            if name not in {"pymss", "pymss-core"}
        ]
        backend_extras = [str(requirement) for requirement in _spec.get("extras", []) or []]
        command.extend([*common_requirements, pymss_requirement, pymss_core_requirement, *backend_extras])

        def run_pip(command: list[str], stage: str) -> None:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=os.environ.copy(),
            )
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    message = line.rstrip()
                    append_log(stage, message)
                    _emit("runtime_core_update_log", {"stage": stage, "message": message}, task_id)
            except Exception:
                # Do not leave pip running after a broken event pipe.  Otherwise a failed update can
                # continue changing the environment and make the next click appear to repair it.
                if process.poll() is None:
                    process.kill()
                process.wait()
                raise
            finally:
                close_stdout = getattr(process.stdout, "close", None)
                if close_stdout:
                    close_stdout()
            if process.wait() != 0:
                raise RuntimeError(f"pip failed during {stage} with exit code {process.returncode}")

        _ensure_runtime_pip(work_python_path, task_id, append_log)

        # Releases before the metadata-preserving prune fix may have a working package but no
        # RECORD file.  Pip refuses to uninstall such a distribution, so repair only these two
        # small core packages from their currently installed versions before the real upgrade.
        # ``--ignore-installed --no-deps`` is deliberately limited to this repair command: the
        # normal update below keeps dependency resolution and the Torch constraint unchanged.
        missing_records = _runtime_core_missing_records(work_python_path)
        if missing_records:
            repair_requirements = [
                f"{name}=={missing_records[name]}"
                for name in ("pymss", "pymss-core")
                if name in missing_records
            ]
            repair_command = [
                str(work_python_path), "-m", "pip", "install", "--ignore-installed", "--no-deps",
                "--no-cache-dir", "--only-binary=:all:", "--prefer-binary",
            ]
            if index_url:
                repair_command.extend(["--index-url", index_url])
            repair_command.extend(repair_requirements)
            repair_message = (
                "Restoring pip installation metadata for "
                + ", ".join(repair_requirements)
            )
            append_log("metadata", repair_message)
            _emit("runtime_core_update_stage", {"stage": "metadata", "message": repair_message}, task_id)
            run_pip(repair_command, "metadata")
            remaining_records = _runtime_core_missing_records(work_python_path)
            if remaining_records:
                raise RuntimeError(
                    "pip metadata repair did not restore RECORD for: "
                    + ", ".join(sorted(remaining_records))
                )

        _emit(
            "runtime_core_update_stage",
            {
                "stage": "manifest",
                "command": "pip install --upgrade " + " ".join(
                    [*common_requirements, pymss_requirement, pymss_core_requirement, *backend_extras]
                ),
            },
            task_id,
        )
        run_pip(command, "manifest")
        probed = _probe_python_runtime(work_python_path, _backend_extra_names(_manifest(), backend))
        manifest_ok, manifest_failures = _manifest_versions_are_satisfied(probed, manifest, backend)
        if not manifest_ok:
            raise RuntimeError(
                "Runtime manifest verification failed: " + "; ".join(manifest_failures)
            )
        if probed.get("pymssVersion") != target_pymss_version:
            raise RuntimeError(f"pymss stayed at {probed.get('pymssVersion') or 'unknown'} after update; expected {target_pymss_version}")
        if probed.get("pymssCoreVersion") != target_pymss_core_version:
            raise RuntimeError(f"pymss-core stayed at {probed.get('pymssCoreVersion') or 'unknown'} after update; expected {target_pymss_core_version}")
        if probed.get("pymssGraphAvailable") is False:
            raise RuntimeError("pymss.graph is unavailable after the core update; retry the update or reinstall the runtime")
        expected_torch_backend = "cpu" if backend == "mlx" else backend
        if probed.get("torchBackend") != expected_torch_backend:
            raise RuntimeError(
                f"Torch backend changed to {probed.get('torchBackend') or 'unknown'} during update; expected {expected_torch_backend}"
            )
        if probed.get("torchVersion") != torch_version:
            raise RuntimeError(
                f"Torch version changed to {probed.get('torchVersion') or 'unknown'} during update; expected {torch_version}"
            )
        updated = {
            **(state or {}),
            "backend": backend,
            "manifestVersion": manifest.get("manifestVersion"),
            "pythonPath": str(python_path),
            "logPath": str(log_path),
            "packages": probed.get("packages") or (state.get("packages") if state else None),
            "packageVersions": probed.get("packageVersions") or (state.get("packageVersions") if state else None),
            "pymssVersion": probed.get("pymssVersion") or (state.get("pymssVersion") if state else None),
            "pymssCoreVersion": probed.get("pymssCoreVersion") or (state.get("pymssCoreVersion") if state else None),
            "pymssGraphAvailable": bool(probed.get("pymssGraphAvailable")),
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        swap_backup_dir = RUNTIME_ENVS_DIR / f".{backend}.core-backup"
        if swap_backup_dir.exists():
            shutil.rmtree(swap_backup_dir, ignore_errors=True)
        env_dir.rename(swap_backup_dir)
        try:
            staging_dir.rename(env_dir)
            staging_dir = None
            if env_state_path:
                env_state = dict(updated)
                env_state.pop("pythonPath", None)
                env_state.pop("logPath", None)
                env_state.pop("activatedAt", None)
                env_state.pop("source", None)
                _atomic_write_json(env_state_path, env_state)
            _write_runtime_state(updated)
        except Exception:
            if env_dir.exists():
                shutil.rmtree(env_dir, ignore_errors=True)
            if swap_backup_dir.exists():
                swap_backup_dir.rename(env_dir)
            swap_backup_dir = None
            if previous_active_state:
                try:
                    _write_runtime_state(previous_active_state)
                except OSError:
                    pass
            raise
        shutil.rmtree(swap_backup_dir, ignore_errors=True)
        swap_backup_dir = None
        _emit("runtime_core_update_finished", {"backend": backend, "state": updated, "logPath": str(log_path)}, task_id)
        return 0
    except Exception as exc:
        from worker_protocol import emit_error
        append_log("error", str(exc))
        return emit_error(
            "RUNTIME_CORE_UPDATE_FAILED",
            str(exc),
            detail=f"详细更新日志：{log_path}",
            task_id=task_id,
            recoverable=True,
            extra={"backend": backend, "logPath": str(log_path)},
        )
    finally:
        for path in {constraints_path, env_dir / ".pymss-core-update-constraints.txt"}:
            if not path:
                continue
            try:
                path.unlink()
            except OSError:
                pass
        if staging_dir and staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        if swap_backup_dir and swap_backup_dir.exists():
            if not env_dir.exists():
                try:
                    swap_backup_dir.rename(env_dir)
                except OSError:
                    pass
            else:
                shutil.rmtree(swap_backup_dir, ignore_errors=True)
