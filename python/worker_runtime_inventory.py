"""Bounded, Torch-free system and HIP device inventory for runtime planning."""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from threading import Thread
from pathlib import Path, PurePosixPath
from typing import Any

from worker_protocol import emit

_SYSTEM_QUERY = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
@{devices=@(Get-CimInstance Win32_VideoController | Select-Object -First 64 Name,PNPDeviceID,DriverVersion)} | ConvertTo-Json -Depth 5 -Compress
"""
_GATE = "import json,subprocess,sys; a=json.loads(sys.stdin.buffer.readline()); raise SystemExit(subprocess.call(a))"


def _vendor(name: str, identity: str) -> str:
    value = (name + " " + identity).lower()
    if "ven_1002" in value or re.search(r"\bamd\b|radeon", value):
        return "amd"
    if "ven_10de" in value or "nvidia" in value:
        return "nvidia"
    if "ven_8086" in value or "intel" in value:
        return "intel"
    return "unknown"


def _child(command: list[str], *, timeout: float = 20, directory: Path | None = None,
           environment: dict[str, str] | None = None, last_json: bool = False) -> dict[str, Any]:
    supplied_environment = environment is not None
    environment = dict(environment) if supplied_environment else {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("HIP_", "HSA_", "ROCM_", "ROCR_"))
                   and key.upper() not in {"PYTHONHOME", "PYTHONPATH", "GPU_DEVICE_ORDINAL"}}
    if os.name == "nt" and not supplied_environment:
        windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
        environment["PATH"] = os.pathsep.join(str(p) for p in (windows / "System32", windows))
    process = subprocess.Popen([sys.executable, "-I", "-B", "-c", _GATE], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
                               cwd=directory, start_new_session=os.name != "nt",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    job = None
    outputs = [bytearray(), bytearray()]
    oversized = [False]
    readers = []

    def drain(stream, index: int) -> None:
        while data := stream.read(16384):
            remaining = 128 * 1024 - len(outputs[index])
            outputs[index].extend(data[:max(0, remaining)])
            if len(data) > remaining:
                oversized[0] = True

    try:
        if os.name == "nt":
            from worker_rocm_probe import _WindowsJob
            job = _WindowsJob(process)
        for index, stream in enumerate((process.stdout, process.stderr)):
            reader = Thread(target=drain, args=(stream, index), daemon=True)
            readers.append(reader)
            reader.start()
        process.stdin.write((json.dumps(command) + "\n").encode())
        process.stdin.close()
        process.wait(timeout=timeout)
        for reader in readers:
            reader.join(timeout=1)
        if process.returncode or oversized[0]:
            raise RuntimeError("The hardware inventory process did not return a bounded response")
        decoded = outputs[0].decode("utf-8-sig")
        if not decoded.strip():
            raise ValueError("The hardware inventory response is empty")
        result = json.loads(decoded.strip().splitlines()[-1] if last_json else decoded)
        if not isinstance(result, dict):
            raise ValueError("The hardware inventory response must be an object")
        return result
    finally:
        try:
            if job is not None:
                job.stop()
            elif os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                process.kill()
            process.wait(timeout=10)
        finally:
            if job is not None:
                job.close()
            for reader in readers:
                reader.join(timeout=3)
            if any(reader.is_alive() for reader in readers):
                raise RuntimeError("The hardware inventory process tree did not close its output pipes")
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()


def system_inventory() -> list[dict[str, Any]]:
    if sys.platform != "win32":
        return []
    shell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    raw = _child([str(shell), "-NoProfile", "-NonInteractive", "-Command", _SYSTEM_QUERY])
    values = raw.get("devices", [])
    values = [values] if isinstance(values, dict) else values
    if not isinstance(values, list) or len(values) > 64:
        raise ValueError("Invalid system display inventory")
    devices = []
    for item in values:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or "Unknown GPU")[:160]
        identity = str(item.get("PNPDeviceID") or "")[:512]
        key = hashlib.sha256((identity or name).encode()).hexdigest()[:24]
        devices.append({"id": ("pnp:" if identity else "session:") + key, "vendor": _vendor(name, identity),
                        "name": name, "hardwareId": identity or None,
                        "driverVersion": str(item.get("DriverVersion") or "")[:80] or None,
                        "gfx": None, "architectureSource": "unknown", "architectureConfidence": "unknown",
                        "memoryBytes": None, "isDiscrete": None})
    return devices


def _bundle_path(root: Path, raw: Any) -> Path:
    if not isinstance(raw, str) or not raw or "\\" in raw or ":" in raw:
        raise ValueError("Invalid detector relative path")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise ValueError("Invalid detector relative path")
    result = (root / raw).resolve(strict=True)
    if not result.is_relative_to(root) or not result.is_file():
        raise ValueError("Detector file resolves outside its bundle")
    return result


def verify_detector(root: Path) -> tuple[Path, Path, dict[str, Any]]:
    root = root.resolve(strict=True)
    path = root / "manifest.json"
    if path.stat().st_size > 128 * 1024 or not path.resolve().is_relative_to(root):
        raise ValueError("Invalid detector manifest")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    files = manifest.get("files")
    if manifest.get("schemaVersion") != 1 or not isinstance(files, dict) or not 2 <= len(files) <= 256:
        raise ValueError("Invalid detector bundle schema")
    for raw, expected in files.items():
        file = _bundle_path(root, raw)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected) or file.stat().st_size > 512 * 1024**2:
            raise ValueError("Invalid detector file digest")
        digest = hashlib.sha256()
        with file.open("rb") as source:
            for chunk in iter(lambda: source.read(4 * 1024**2), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("Detector bundle integrity check failed")
    for key in ("executable", "hipLibrary"):
        if manifest.get(key) not in files:
            raise ValueError("The detector entry point is not covered by its manifest")
    return _bundle_path(root, manifest["executable"]), _bundle_path(root, manifest["hipLibrary"]), manifest


def native_inventory(root: Path) -> list[dict[str, Any]]:
    executable, library, _manifest = verify_detector(root)
    raw = _child([str(executable), str(library)], directory=executable.parent)
    if raw.get("schemaVersion") != 1 or type(raw.get("available")) is not bool:
        raise ValueError("Invalid native detector response")
    values = raw.get("devices")
    if not isinstance(values, list) or len(values) > 32:
        raise ValueError("Invalid native detector devices")
    devices = []
    for item in values:
        if not isinstance(item, dict):
            raise ValueError("Invalid native device")
        architecture = re.fullmatch(r"(gfx[0-9a-f]{3,6})(?::[^\s]{0,100})?", str(item.get("gfx", "")).lower())
        if architecture is None:
            continue
        pci = str(item.get("pciBusId") or "").lower()
        stable = re.fullmatch(r"[0-9a-f]{4,8}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", pci) is not None
        name = str(item.get("name") or "AMD GPU")[:160]
        memory = item.get("memoryBytes")
        if type(memory) is not int or not 0 <= memory < 2**60:
            memory = None
        devices.append({"id": "hip-pci:" + pci if stable else "session:" + str(len(devices)),
                        "vendor": "amd", "name": name, "pciBusId": pci if stable else None,
                        "gfx": architecture.group(1), "driverVersion": None,
                        "architectureSource": "native", "architectureConfidence": "confirmed",
                        "memoryBytes": memory, "isDiscrete": item.get("isDiscrete") if type(item.get("isDiscrete")) is bool else None})
    return devices


def hardware_inventory(manifest: dict[str, Any], detector_root: Path | None = None) -> dict[str, Any]:
    from worker_runtime_profiles import backend_spec, profile_release_state
    errors = []
    try:
        system = system_inventory()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        system = []
        errors.append({"code": "SYSTEM_INVENTORY_UNAVAILABLE", "message": "System GPU inventory is unavailable"})
    native = []
    detector_status = "unavailable"
    directory = detector_root or Path(__file__).parent / "rocm-detectors"
    if sys.platform == "win32" and directory.is_dir():
        for candidate in sorted(directory.iterdir())[:8]:
            if (candidate.is_symlink() or not candidate.resolve().is_relative_to(directory.resolve())
                    or not candidate.is_dir() or not (candidate / "manifest.json").is_file()):
                continue
            try:
                native = native_inventory(candidate)
                detector_status = "ready" if native else "device_unavailable"
                if native:
                    break
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                detector_status = "failed"
                errors.append({"code": "NATIVE_INVENTORY_UNAVAILABLE", "message": "The bundled GPU detector could not confirm an architecture"})
    if native:
        amd = [device for device in system if device["vendor"] == "amd"]
        versions = {device["driverVersion"] for device in amd if device["driverVersion"]}
        if len(versions) == 1 and all(device["driverVersion"] for device in amd):
            for device in native:
                device["driverVersion"] = next(iter(versions))
        devices = [*native, *(device for device in system if device["vendor"] != "amd")]
    else:
        devices = system
    profiles = []
    spec = backend_spec(manifest, "rocm", sys.platform)
    for profile in (spec.get("profiles") or {}).values():
        try:
            state = profile_release_state(profile)
            profiles.append({"id": profile["id"], "revision": profile.get("revision", 1),
                             "releaseState": state, "installationEnabled": profile.get("installationEnabled", False),
                             "validationStatus": profile.get("validationStatus", "unknown"),
                             "availableTargets": profile.get("availableTargets", []),
                             "validatedTargets": profile.get("validatedTargets", []),
                             "torchVersion": (profile.get("packages") or {}).get("torch")})
        except (ValueError, KeyError):
            errors.append({"code": "RUNTIME_PROFILE_INVALID", "message": "A ROCm profile has inconsistent release policy"})
    return {"schemaVersion": 1, "platform": sys.platform, "defaultProfileId": spec.get("defaultProfile"),
            "devices": devices, "systemAdapters": system,
            "profiles": profiles, "detectorStatus": detector_status, "errors": errors}


def cmd_runtime_hardware_inventory(_payload: dict[str, Any]) -> int:
    from worker_bootstrap import _manifest
    emit("runtime_hardware_inventory", hardware_inventory(_manifest()))
    return 0
