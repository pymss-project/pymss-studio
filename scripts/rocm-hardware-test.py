"""Collect isolated, bounded evidence from a portable Windows ROCm package.

No dependencies, drivers, runtimes or models are installed. Only the package's
relocatable pyvenv.cfg may be repaired by its existing bootstrap helper.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import signal
import struct
import subprocess
import sys
from threading import Thread
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit
import wave


MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 4 * 1024 * 1024
CHECK_IDS = {"allocation", "stftIstft", "conv", "batchNorm", "instanceNorm", "sdpa", "rnn"}
GATE = r'''
import json, runpy, subprocess, sys
request = json.loads(sys.stdin.buffer.readline())
if request['kind'] == 'script':
    sys.argv = [request['path'], *request.get('args', [])]
    sys.path.insert(0, str(__import__('pathlib').Path(request['path']).parent))
    runpy.run_path(request['path'], run_name='__main__')
elif request['kind'] == 'code':
    sys.argv = ['-c', *request.get('args', [])]
    exec(compile(request['code'], '<rocm-test-collector>', 'exec'), {'__name__': '__main__'})
else:
    raise SystemExit(subprocess.call(request['args']))
'''

DEPENDENCIES = r'''
from importlib import metadata
import json, sys
packages = []
for distribution in metadata.distributions():
    entry = {'name': distribution.metadata.get('Name', ''), 'version': distribution.version}
    raw = distribution.read_text('direct_url.json')
    if raw:
        try:
            entry['directUrl'] = json.loads(raw)
        except ValueError:
            entry['directUrl'] = {'error': 'Invalid direct_url.json'}
    packages.append(entry)
print(json.dumps({'pythonVersion': sys.version.split()[0], 'prefix': sys.prefix,
                  'packages': sorted(packages, key=lambda item: item['name'].lower())}))
'''

MACHINE_QUERY = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$result = @{
  os = Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,OSArchitecture
  cpu = @(Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors)
  gpu = @(Get-CimInstance Win32_VideoController | Select-Object Name,AdapterCompatibility,VideoProcessor,DriverVersion,DriverDate,AdapterRAM,Status)
  memoryBytes = [long](Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory
}
$result | ConvertTo-Json -Depth 6 -Compress
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Redactor:
    def __init__(self, package: Path, output: Path) -> None:
        self.paths = [(str(package), "${PACKAGE_ROOT}"), (str(output), "${RESULT_ROOT}"),
                      (str(Path.home()), "${HOME}")]
        self.paths.sort(key=lambda item: len(item[0]), reverse=True)

    def text(self, value: str) -> str:
        for path, replacement in self.paths:
            pattern = r"[\\/]".join(re.escape(part) for part in re.split(r"[\\/]+", path.rstrip("\\/")))
            value = re.sub(pattern + r"(?=[\\/\s\"':,;)]|$)", lambda _match: replacement, value,
                           flags=re.IGNORECASE)
        value = re.sub(r"(?i)([A-Z]:[\\/]Users[\\/])[^\\/\s\"']+", r"\1<redacted-user>", value)
        value = re.sub(r"(?i)(/(?:home|Users)/)[^/\s\"']+", r"\1<redacted-user>", value)
        def source_url(match: re.Match) -> str:
            try:
                parsed = urlsplit(match.group())
                return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "<redacted>" if parsed.query else "", ""))
            except ValueError:
                return "<redacted-url>"
        value = re.sub(r"(?i)https?://[^\s\"'<>]+", source_url, value)
        value = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+@", r"\1<redacted>@", value)
        value = re.sub(r"(?i)([?&](?:token|key|sig|signature|password|secret|credential|authorization|api_key)=)[^&#\s\"']+",
                       r"\1<redacted>", value)
        value = re.sub(r"(?i)(\b(?:password|passwd|access_token|refresh_token|token|secret|credential|authorization|api[_-]?key)\s*[=:]\s*)[^\s,;]+",
                       r"\1<redacted>", value)
        return "".join(character for character in value if character.isprintable() or character in "\n\t")

    def value(self, value: Any, key: str = "") -> Any:
        if re.search(r"(?i)(password|passwd|token|secret|credential|authorization|api[_-]?key)", key):
            return "<redacted>"
        if isinstance(value, dict):
            return {str(name): self.value(item, str(name)) for name, item in value.items()}
        if isinstance(value, list):
            return [self.value(item) for item in value]
        return self.text(value) if isinstance(value, str) else value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_object(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 1024 * 1024:
        raise ValueError(f"JSON file exceeds the size limit: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path.name}")
    return value


def confined_path(root: Path, relative: str, *, directory: bool = True) -> Path:
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or PureWindowsPath(relative).drive or PureWindowsPath(relative).is_absolute()
            or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts):
        raise ValueError("Native runtime paths must be environment-relative")
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise ValueError("Runtime path escapes the selected environment")
    if directory and not path.is_dir():
        raise ValueError("Native runtime directory does not exist")
    return path


def find_package_root(script: Path) -> Path:
    for candidate in (script.parent, script.parent.parent):
        if (candidate / "python/worker.py").is_file() and (candidate / "python-runtime").is_dir():
            return candidate.resolve()
    return script.parent.parent.resolve()


def locate_runtime(package: Path) -> tuple[Path, Path, Path, dict[str, Any]]:
    bootstrap = package / "python-runtime/python.exe"
    roots = [package / "python-runtime/runtime-envs/rocm", package / "runtime-envs/rocm"]
    runtime = next((root for root in roots if (root / "Scripts/python.exe").is_file()), None)
    if not bootstrap.is_file() or runtime is None:
        raise FileNotFoundError("The package must include bootstrap Python and a bundled ROCm environment")
    for path in (bootstrap, runtime):
        if not path.resolve().is_relative_to(package):
            raise ValueError("The bundled runtime resolves outside the package")
    for path in (runtime / "Scripts/python.exe", runtime / "pyvenv.cfg", runtime / "pymss-runtime-state.json"):
        if not path.resolve(strict=True).is_relative_to(package):
            raise ValueError("A bundled runtime file resolves outside the package")
    state = read_object(runtime / "pymss-runtime-state.json")
    if state.get("backend") != "rocm":
        raise ValueError("The selected bundled environment is not recorded as ROCm")
    return bootstrap, runtime / "Scripts/python.exe", runtime, state


def isolated_environment(package: Path, output: Path, bootstrap: Path, runtime: Path,
                         state: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("PYMSS", "HIP_", "HSA_", "MIOPEN_", "ROCM_", "ROCR_", "CUDA_"))
                   and key.upper() not in {"PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "VIRTUAL_ENV", "CONDA_PREFIX", "GPU_DEVICE_ORDINAL"}
                   and key.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}}
    data = output / "data"
    paths = {
        "PYMSS_STUDIO_DATA_ROOT": data,
        "PYMSS_STUDIO_RUNTIME_ENVS_DIR": data / "runtime-envs",
        "PYMSS_STUDIO_ACTIVE_RUNTIME_FILE": data / "runtime-envs/active-runtime.json",
        "PYMSS_STUDIO_BUNDLED_RUNTIME_ENVS_DIR": runtime.parent,
        "PYMSS_STUDIO_BOOTSTRAP_PYTHON": bootstrap,
        "PYMSS_STUDIO_DEFAULT_OUTPUT_DIR": data / "outputs",
        "PYMSS_STUDIO_DEBUG_DIR": data / "debug",
        "PYMSS_USER_MODELS": data / "user-models.json",
        "PYMSS_MODEL_DIR": data / "models",
        "NUMBA_CACHE_DIR": data / "numba-cache",
        "PIP_CACHE_DIR": data / "pip-cache",
        "HF_HOME": data / "huggingface",
        "TORCH_HOME": data / "torch",
        "TORCHINDUCTOR_CACHE_DIR": data / "torchinductor",
        "TRITON_CACHE_DIR": data / "triton",
        "XDG_CACHE_HOME": data / "cache",
        "MIOPEN_USER_DB_PATH": data / "miopen-db",
        "MIOPEN_CUSTOM_CACHE_DIR": data / "miopen-cache",
        "HOME": data / "home", "USERPROFILE": data / "home",
        "APPDATA": data / "appdata", "LOCALAPPDATA": data / "localappdata",
        "TEMP": data / "temp", "TMP": data / "temp",
    }
    for key, path in paths.items():
        environment[key] = str(path)
        if path.is_relative_to(output):
            directory = path.parent if key in {"PYMSS_STUDIO_ACTIVE_RUNTIME_FILE", "PYMSS_USER_MODELS"} else path
            directory.mkdir(parents=True, exist_ok=True)
    native = []
    for field in ("nativeToolDirs", "nativeLibraryDirs"):
        values = state.get(field)
        if not isinstance(values, list) or not values:
            raise ValueError(f"Bundled ROCm state is missing {field}")
        native.extend(str(confined_path(runtime, relative)) for relative in values)
    if os.name == "nt":
        windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
        baseline = [str(windows / "System32"), str(windows), str(windows / "System32/Wbem"),
                    str(windows / "System32/WindowsPowerShell/v1.0")]
    else:
        baseline = [environment.get("PATH", "")]
    environment["PATH"] = os.pathsep.join(dict.fromkeys([*native, str(package / "bin"), *baseline]))
    environment.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
                        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
    write_json(paths["PYMSS_USER_MODELS"], {"models": []})
    overrides = {key: value for key, value in environment.items()
                 if key in paths or key in {"PYTHONUTF8", "PYTHONIOENCODING", "PYTHONUNBUFFERED",
                                            "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE",
                                            "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "PATH"}}
    return environment, overrides


def load_windows_job(package: Path):
    """Reuse this build's tested, stdlib-only Job implementation; no Torch import."""
    worker_path = str(package / "python")
    sys.path.insert(0, worker_path)
    try:
        spec = importlib.util.spec_from_file_location("_collector_probe_support", Path(worker_path) / "worker_rocm_probe.py")
        if spec is None or spec.loader is None:
            raise RuntimeError("The package is missing diagnostic process isolation support")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module._WindowsJob
    finally:
        sys.path.remove(worker_path)


class Runner:
    def __init__(self, package: Path, output: Path, environment: dict[str, str], redactor: Redactor) -> None:
        self.package, self.output = package, output
        self.environment, self.redactor = environment, redactor
        self.records: list[dict[str, Any]] = []
        self.job_factory = load_windows_job(package) if os.name == "nt" else None
        self.cleanup_failed = False

    def run(self, name: str, python: Path, request: dict[str, Any], timeout: float) -> dict[str, Any]:
        if self.cleanup_failed:
            raise RuntimeError("A prior process tree did not exit; further diagnostics are blocked")
        print(json.dumps({"type": "collector_stage", "stage": name}, ensure_ascii=False), flush=True)
        started = time.monotonic()
        record: dict[str, Any] = {"id": name, "status": "fail", "startedAt": utc_now(),
                                  "timeoutSeconds": timeout, "stdout": f"commands/{name}.stdout.jsonl",
                                  "stderr": f"commands/{name}.stderr.log"}
        self.records.append(record)
        events: list[Any] = []
        counts = {"stdout": 0, "stderr": 0}
        dropped = {"stdout": 0, "stderr": 0}
        errors: list[str] = []

        def drain(stream, kind: str) -> None:
            try:
                with (self.output / record[kind]).open("w", encoding="utf-8") as destination:
                    while line := stream.readline(MAX_LINE_BYTES + 1):
                        if len(line) > MAX_LINE_BYTES or counts[kind] + len(line) > MAX_OUTPUT_BYTES:
                            dropped[kind] += len(line)
                            if len(line) > MAX_LINE_BYTES:
                                while line and not line.endswith(b"\n"):
                                    line = stream.readline(65536)
                                    dropped[kind] += len(line)
                            continue
                        counts[kind] += len(line)
                        decoded = line.decode("utf-8-sig", errors="replace").rstrip("\r\n")
                        if kind == "stdout":
                            try:
                                event = json.loads(decoded)
                            except ValueError:
                                event = {"type": "collector_output", "text": decoded}
                            event = self.redactor.value(event)
                            events.append(event)
                            destination.write(json.dumps(event, ensure_ascii=False) + "\n")
                        else:
                            destination.write(self.redactor.text(decoded) + "\n")
            except Exception as error:
                errors.append(self.redactor.text(str(error)))

        child = job = None
        readers: list[Thread] = []
        try:
            child = subprocess.Popen([str(python), "-B", "-u", "-c", GATE], cwd=self.output,
                                     env=self.environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, start_new_session=os.name != "nt",
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if self.job_factory:
                job = self.job_factory(child)
            for stream, kind in ((child.stdout, "stdout"), (child.stderr, "stderr")):
                reader = Thread(target=drain, args=(stream, kind), daemon=True)
                readers.append(reader)
                reader.start()
            child.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
            child.stdin.close()
            try:
                child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                record.update({"timedOut": True, "message": "The diagnostic exceeded its outer time limit"})
            record["exitCode"] = child.poll()
        except KeyboardInterrupt:
            record.update({"cancelled": True, "message": "Collection was interrupted"})
            raise
        except Exception as error:
            record["message"] = self.redactor.text(str(error))
        finally:
            try:
                if job is not None:
                    job.stop()
                elif child is not None and child.poll() is None:
                    if os.name == "nt":
                        # Assignment failed before the stdin handshake; this child cannot
                        # have started native libraries or created descendants.
                        child.kill()
                    else:
                        os.killpg(child.pid, signal.SIGKILL)
                elif child is not None and os.name != "nt":
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if child is not None:
                    child.wait(timeout=10)
                record["treeCleanupConfirmed"] = True
            except Exception as error:
                self.cleanup_failed = True
                record.update({"treeCleanupConfirmed": False, "cleanupError": self.redactor.text(str(error))})
            finally:
                if job is not None:
                    job.close()
            for reader in readers:
                reader.join(timeout=3)
            if any(reader.is_alive() for reader in readers):
                self.cleanup_failed = True
                record.update({"treeCleanupConfirmed": False, "cleanupError": "A descendant still owns an output pipe"})
            if child is not None:
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None and not stream.closed and not any(reader.is_alive() for reader in readers):
                        stream.close()
            record.update({"durationSeconds": round(time.monotonic() - started, 3),
                           "capturedBytes": counts, "droppedBytes": dropped})
            if errors:
                record["captureErrors"] = errors
        record["events"] = events
        record["status"] = "pass" if (record.get("exitCode") == 0 and not record.get("timedOut")
                                       and record.get("treeCleanupConfirmed") and not errors
                                       and not any(dropped.values())) else "fail"
        return record


def worker_request(worker: Path, command: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "script", "path": str(worker), "args": [command, "--payload", json.dumps(payload)]}


def payload_of(record: dict[str, Any], event_type: str) -> dict[str, Any] | None:
    matching = [event.get("payload") for event in record.get("events", [])
                if isinstance(event, dict) and event.get("type") == event_type and isinstance(event.get("payload"), dict)]
    return matching[-1] if matching else None


def assess_inventory(record: dict[str, Any]) -> dict[str, Any]:
    report = payload_of(record, "runtime_hardware_inventory")
    if record["status"] != "pass" or report is None:
        return {"status": "fail", "message": "No complete hardware inventory protocol event was received"}
    devices, profiles = report.get("devices"), report.get("profiles")
    if not isinstance(devices, list) or not isinstance(profiles, list):
        return {"status": "fail", "message": "The hardware inventory has an invalid device or profile list"}
    profile_states = [{key: profile.get(key) for key in ("id", "releaseState", "installationEnabled")}
                      for profile in profiles if isinstance(profile, dict)]
    rocm10 = next((profile for profile in profile_states if profile["id"] == "win-rocm100-multiarch"), {})
    evidence = {"detectorStatus": report.get("detectorStatus"), "profiles": profile_states,
                "stableDeviceIds": [device["id"] for device in devices if isinstance(device, dict)
                                    and isinstance(device.get("id"), str)
                                    and device["id"].startswith(("hip-pci:", "pnp:"))]}
    if not valid_profile_policy(rocm10):
        return {**evidence, "status": "fail", "message": "The ROCm 10 profile has an inconsistent installation policy"}
    if report.get("errors") or report.get("detectorStatus") == "failed":
        return {**evidence, "status": "fail", "message": "The hardware inventory reported a system, native detector or profile error"}
    confirmed = [device for device in devices if isinstance(device, dict) and device.get("vendor") == "amd"
                 and device.get("architectureConfidence") == "confirmed" and device.get("gfx")]
    if report.get("detectorStatus") != "ready" or not confirmed:
        return {**evidence, "status": "blocked", "message": "The native detector could not confirm an AMD GPU architecture"}
    if any(not isinstance(device.get("id"), str) or not device["id"].startswith("hip-pci:") for device in confirmed):
        return {**evidence, "status": "blocked", "message": "A confirmed AMD GPU has no stable PCI identity"}
    return {**evidence, "status": "pass", "message": "The native detector confirmed AMD architectures and stable PCI identities"}


def assess_runtime_info(record: dict[str, Any]) -> dict[str, Any]:
    report = payload_of(record, "runtime_info")
    if record["status"] != "pass" or report is None:
        return {"status": "fail", "message": "No complete runtime information protocol event was received"}
    packages = report.get("packages")
    required_packages = {name: value for name, value in packages.items() if name != "mlx"} if isinstance(packages, dict) else {}
    if (report.get("liveProbeError") or report.get("torchBackend") != "rocm"
            or not required_packages or not all(value is True for value in required_packages.values())):
        return {"status": "fail", "message": "The bundled ROCm inference dependencies failed the live import check"}
    validation = report.get("activationValidation")
    evidence = {"activationValidation": validation, "seedPreparationRequired": report.get("seedPreparationRequired", False)}
    if not isinstance(validation, dict) or validation.get("status") != "passed":
        return {**evidence, "status": "blocked", "message": "Dependencies imported, but the live minimum GPU execution gate did not pass"}
    if report.get("acceleratorAvailable") is not True or report.get("ready") is not True:
        return {**evidence, "status": "fail", "message": "The runtime readiness flags contradict its successful minimum GPU execution gate"}
    return {**evidence, "status": "pass", "message": "Dependency imports and the live minimum GPU execution gate passed"}


def valid_profile_policy(profile: dict[str, Any]) -> bool:
    state = profile.get("releaseState")
    return ((state in ("preview", "stable", "legacy") and profile.get("installationEnabled") is True)
            or (state in ("disabled", "revoked") and profile.get("installationEnabled") is False))


def assess_profile_plan(record: dict[str, Any]) -> dict[str, Any]:
    report = payload_of(record, "runtime_install_plan")
    if (record["status"] != "pass" or report is None or report.get("backend") != "rocm"
            or report.get("profileId") != "win-rocm100-multiarch"
            or not valid_profile_policy(report)
            or (report.get("releaseState") == "preview" and report.get("requiresPreviewAcceptance") is not True)
            or (report.get("releaseState") != "preview" and report.get("requiresPreviewAcceptance") is True)):
        return {"status": "fail", "message": "The read-only ROCm 10 plan has an inconsistent installation policy"}
    return {"status": "pass", "releaseState": report["releaseState"],
            "message": "The read-only ROCm 10 plan preserves its release policy; no installation was requested"}


def assess_probe(record: dict[str, Any], include_fp16: bool) -> dict[str, Any]:
    if record["status"] != "pass":
        return {"status": "fail", "message": "The diagnostic command or process cleanup failed"}
    report = payload_of(record, "runtime_gpu_probe_finished")
    if report is None:
        return {"status": "fail", "message": "No complete GPU diagnostic protocol event was received"}
    devices = report.get("deviceCapabilities")
    if report.get("status") == "unavailable" or not isinstance(devices, list) or not devices:
        return {"status": "blocked", "message": "ROCm did not expose an available GPU; consult the report and driver evidence"}
    required = {(check, precision) for check in CHECK_IDS
                for precision in (["fp32", "fp16"] if include_fp16 else ["fp32"])}
    rows = []
    for device in devices:
        modes = device.get("modes", [])
        for mode_id in ("miopen", "nativeHip"):
            mode = next((value for value in modes if value.get("mode") == mode_id), {})
            checks = mode.get("checks", [])
            passed = {(check.get("id"), check.get("precision")) for check in checks
                      if check.get("status") == "passed"}
            success = (mode.get("status") == "completed" and mode.get("basicAvailable") is True
                       and required.issubset(passed))
            rows.append({"deviceIndex": device.get("index"), "mode": mode_id,
                         "status": "pass" if success else "fail",
                         "passedChecks": len(passed & required), "requiredChecks": len(required)})
    success = report.get("status") == "completed" and rows and all(row["status"] == "pass" for row in rows)
    return {"status": "pass" if success else "fail", "modes": rows,
            "message": "All requested operator checks passed" if success else "One or more requested operator checks did not pass"}


def synthetic_audio(path: Path) -> dict[str, Any]:
    rate, seconds = 44100, 8
    with wave.open(str(path), "wb") as file:
        file.setparams((2, 2, rate, 0, "NONE", "not compressed"))
        for offset in range(0, rate * seconds, 4096):
            frames = bytearray()
            for index in range(offset, min(offset + 4096, rate * seconds)):
                t = index / rate
                envelope = min(1.0, t * 5, (seconds - t) * 5)
                left = envelope * (0.18 * math.sin(2 * math.pi * 220 * t) + 0.08 * math.sin(2 * math.pi * 733 * t))
                right = envelope * (0.16 * math.sin(2 * math.pi * 330 * t) + 0.06 * math.sin(2 * math.pi * 991 * t))
                frames.extend(struct.pack("<hh", round(left * 32767), round(right * 32767)))
            file.writeframesraw(frames)
    return {"path": path.name, "sampleRate": rate, "channels": 2, "seconds": seconds,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "purpose": "Deterministic numerical smoke input; not an audio quality reference"}


def write_summary(output: Path, summary: dict[str, Any]) -> None:
    write_json(output / "summary.json", summary)
    lines = ["# ROCm 实机自动取证结果", "", f"- 状态：`{summary['status']}`", f"- 时间：{summary['startedAt']}",
             "- 本报告只覆盖环境和算子；模型输出、音质、界面及真实回退需要按测试指南继续验证。", "",
             "| 项目 | 结果 | 说明 |", "|---|---|---|"]
    for check in summary["checks"]:
        message = str(check.get("message", "")).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {check['id']} | {check['status']} | {message} |")
    lines.extend(["", "模型与音质结果：`skipped`（自动采集器不下载或运行模型）。", "",
                  "详见 `summary.json`、`session.json`、`machine.json`、`hardware-inventory.json`、`runtime-info.json`、`rocm100-profile-plan.json`、`dependencies.json`、`probe-fp32.json`、`probe-fp16.json` 和 `commands/`。", ""])
    (output / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def collect(package: Path, output: Path, probe_timeout: float, command_timeout: float) -> int:
    output.mkdir(parents=True, exist_ok=False)
    (output / "commands").mkdir()
    redactor = Redactor(package, output)
    summary: dict[str, Any] = {"schemaVersion": 1, "startedAt": utc_now(), "status": "blocked",
                               "checks": [], "commands": [], "scope": "environment_and_operators_only",
                               "hardwareValidationComplete": False, "releaseReadiness": "unverified"}
    runner = None
    try:
        bootstrap, python, runtime, state = locate_runtime(package)
        worker = package / "python/worker.py"
        if not worker.is_file():
            raise FileNotFoundError("The package worker was not found")
        if not worker.resolve().is_relative_to(package):
            raise ValueError("The worker resolves outside the package")
        environment, overrides = isolated_environment(package, output, bootstrap, runtime, state)
        runner = Runner(package, output, environment, redactor)
        write_json(output / "session.json", {"schemaVersion": 1,
                   "pythonPath": python.relative_to(package).as_posix(),
                   "bootstrapPythonPath": bootstrap.relative_to(package).as_posix(),
                   "workerPath": worker.relative_to(package).as_posix(),
                   "runtimeRoot": runtime.relative_to(package).as_posix(),
                   "nativeLibraryDirs": state["nativeLibraryDirs"], "nativeToolDirs": state["nativeToolDirs"],
                   "environment": redactor.value(overrides), "runtimeState": redactor.value(state)})
        summary["audio"] = synthetic_audio(output / "synthetic-stereo.wav")
        files = {}
        for relative in ("build-manifest.json", "build-info.json", "package.json", "python/runtime-manifest.json",
                         "python-runtime/build-manifest.json", "python-runtime/runtime-build-manifest.json",
                         runtime.relative_to(package).as_posix() + "/pymss-runtime-state.json"):
            path = package / relative
            if path.is_file():
                if not path.resolve().is_relative_to(package):
                    raise ValueError("A package manifest resolves outside the package")
                files[relative] = {"sizeBytes": path.stat().st_size,
                                   "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                   "content": redactor.value(read_object(path))}
        write_json(output / "package-evidence.json", files)
        if os.name != "nt":
            raise RuntimeError("This test package requires a Windows host")
        machine = runner.run("machine", bootstrap,
                             {"kind": "exec", "args": [str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
                                                        "-NoProfile", "-NonInteractive", "-Command", MACHINE_QUERY]}, min(command_timeout, 60))
        values = [event for event in machine["events"] if isinstance(event, dict) and "gpu" in event and "os" in event]
        write_json(output / "machine.json", values[-1] if values else {"status": "blocked", "message": "CIM collection failed"})
        summary["checks"].append({"id": "machine", "status": "pass" if values and machine["status"] == "pass" else "blocked"})

        inventory = runner.run("hardware-inventory", bootstrap,
                               worker_request(worker, "runtime_hardware_inventory", {}), command_timeout)
        write_json(output / "hardware-inventory.json", payload_of(inventory, "runtime_hardware_inventory")
                   or {"status": "fail", "message": "No hardware inventory event"})
        summary["checks"].append({"id": "hardware-inventory", **assess_inventory(inventory)})
        profile_plan = runner.run("rocm100-profile-plan", bootstrap,
                                   worker_request(worker, "runtime_install_plan", {"backend": "rocm", "profileId": "win-rocm100-multiarch", "targets": ["gfx1031"]}),
                                   command_timeout)
        write_json(output / "rocm100-profile-plan.json", payload_of(profile_plan, "runtime_install_plan")
                   or {"status": "fail", "message": "No ROCm 10 plan event"})
        summary["checks"].append({"id": "rocm100-profile-plan", **assess_profile_plan(profile_plan)})

        cfg = runtime / "pyvenv.cfg"
        if cfg.is_file():
            (output / "original-pyvenv.cfg.redacted.txt").write_text(redactor.text(cfg.read_text(encoding="utf-8")), encoding="utf-8")
        repair = runner.run("portable-path-repair", bootstrap, {"kind": "code", "code":
            "import json, sys\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\n"
            "from worker_bootstrap import _repair_runtime_venv_config\n"
            "print(json.dumps({'repaired': _repair_runtime_venv_config(Path(sys.argv[2]))}))",
            "args": [str(package / "python"), str(runtime)]}, command_timeout)
        summary["checks"].append({"id": "portable-path-repair", "status": repair["status"],
                                  "message": "Only the bundled pyvenv.cfg may be relocated; the original is recorded with paths redacted"})
        dependencies = runner.run("dependencies", python, {"kind": "code", "code": DEPENDENCIES}, command_timeout)
        values = [event for event in dependencies["events"] if isinstance(event, dict) and "packages" in event]
        write_json(output / "dependencies.json", values[-1] if values else {"status": "fail"})
        summary["checks"].append({"id": "dependencies", "status": dependencies["status"] if values else "fail"})
        runtime_info = runner.run("runtime-info", python, worker_request(worker, "runtime_info", {"backend": "rocm"}), command_timeout)
        runtime_report = payload_of(runtime_info, "runtime_info")
        write_json(output / "runtime-info.json", runtime_report or {"status": "fail", "message": "No runtime information event"})
        summary["checks"].append({"id": "runtime-info", **assess_runtime_info(runtime_info)})
        summary["checks"].append({"id": "managed-seed-preparation", "status": "skipped",
                                  "seedPreparationRequired": bool((runtime_report or {}).get("seedPreparationRequired")),
                                  "message": "The collector does not copy or activate the bundled seed; follow the separate managed-seed test in the guide"})
        for name, command, payload, event_type in (
            ("env-info", "env_info", {}, "env_info"),
            ("list-models", "list_models", {"includeCustom": False, "includeLocalState": False}, "models"),
            ("runtime-install-plan", "runtime_install_plan", {"backend": "rocm", "profileId": state.get("profileId")}, "runtime_install_plan"),
        ):
            record = runner.run(name, python, worker_request(worker, command, payload), command_timeout)
            payload = payload_of(record, event_type)
            status = record["status"] if payload is not None else "fail"
            message = ""
            if command == "env_info" and payload is not None:
                if not payload.get("pymssAvailable") or not payload.get("torchAvailable") or payload.get("torchBackend") != "rocm":
                    status, message = "fail", "The bundled inference dependencies did not initialize as ROCm"
                elif not payload.get("cudaAvailable") or not payload.get("cudaDeviceCount"):
                    status, message = "blocked", "No usable GPU was exposed; exit code zero is not a GPU pass"
            elif command == "list_models" and payload is not None and not payload.get("count"):
                status, message = "fail", "The bundled supported-model catalog is empty"
            elif command == "runtime_install_plan" and payload is not None:
                if payload.get("backend") != "rocm" or state.get("profileId") and payload.get("profileId") != state["profileId"]:
                    status, message = "fail", "The read-only installation plan does not match the bundled ROCm profile"
            summary["checks"].append({"id": name, "status": status, "message": message})
            if payload is not None:
                write_json(output / (name + ".json"), payload)
        # Bound the complete controller as well as its per-mode child timeout.
        # Eight devices * two modes is the worker's maximum diagnostic coverage.
        for precision, fp16 in (("fp32", False), ("fp16", True)):
            request = {"taskId": "hardware-test-" + precision, "includeFp16": fp16,
                       "modes": ["miopen", "nativeHip"], "timeoutSeconds": probe_timeout}
            record = runner.run("probe-" + precision, python, worker_request(worker, "runtime_gpu_probe", request),
                                min(probe_timeout, 20) + 16 * probe_timeout + 90)
            report = payload_of(record, "runtime_gpu_probe_finished")
            write_json(output / ("probe-" + precision + ".json"), report or {"status": "fail", "message": "No finished event"})
            summary["checks"].append({"id": "probe-" + precision, **assess_probe(record, fp16)})
        summary["checks"].append({"id": "model-inference-and-ui", "status": "skipped",
                                  "message": "Continue with the accompanying hardware test guide"})
    except KeyboardInterrupt:
        summary["checks"].append({"id": "collection", "status": "blocked", "message": "Collection was cancelled"})
    except Exception as error:
        summary["checks"].append({"id": "collection", "status": "blocked", "message": redactor.text(str(error)),
                                  "exceptionType": type(error).__name__})
    finally:
        if runner is not None:
            summary["commands"] = [{key: value for key, value in record.items() if key != "events"}
                                   for record in runner.records]
        statuses = {check["status"] for check in summary["checks"]}
        summary["status"] = "fail" if "fail" in statuses else "blocked" if "blocked" in statuses else "pass"
        summary["finishedAt"] = utc_now()
        write_summary(output, summary)
    print(json.dumps({"type": "collector_finished", "status": summary["status"],
                      "scope": summary["scope"], "hardwareValidationComplete": False,
                      "summary": redactor.text(str(output / "summary.json"))}, ensure_ascii=False), flush=True)
    return {"pass": 0, "fail": 1, "blocked": 2}[summary["status"]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path, default=find_package_root(Path(__file__).resolve()))
    parser.add_argument("--output", type=Path, help="A new result directory; default: results-<timestamp>-<pid> inside the package")
    parser.add_argument("--probe-timeout", type=float, default=45, help="Per inventory/mode timeout, 5 to 120 seconds")
    parser.add_argument("--command-timeout", type=float, default=180, help="Other command timeout, 10 to 600 seconds")
    arguments = parser.parse_args(argv)
    if not math.isfinite(arguments.probe_timeout) or not 5 <= arguments.probe_timeout <= 120:
        parser.error("--probe-timeout must be between 5 and 120")
    if not math.isfinite(arguments.command_timeout) or not 10 <= arguments.command_timeout <= 600:
        parser.error("--command-timeout must be between 10 and 600")
    try:
        package = arguments.package_root.resolve(strict=True)
    except OSError:
        parser.error("--package-root does not exist")
    output = (arguments.output or package / ("results-" + datetime.now().strftime("%Y%m%d-%H%M%S")
                                            + "-" + str(os.getpid()))).resolve()
    if output.exists() or package == output or package.is_relative_to(output):
        parser.error("--output must be a new directory and must not contain the package")
    try:
        return collect(package, output, arguments.probe_timeout, arguments.command_timeout)
    except Exception as error:
        print(json.dumps({"type": "collector_error", "message": Redactor(package, output).text(str(error))},
                         ensure_ascii=False), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
