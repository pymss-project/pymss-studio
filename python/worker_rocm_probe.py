"""Bounded ROCm operator diagnostics; only disposable children import Torch.

The checks use PyTorch's documented HIP ``cuda`` device interface, public STFT,
SDPA and LSTM APIs (2.9 documentation). Reports describe observed operators,
never a promise that every separation model can run on the device.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from queue import Empty, Queue
import re
import signal
import subprocess
import sys
from threading import Event, Thread
import time
from typing import Any, Callable

from worker_protocol import _redact_sensitive_text, emit, emit_error

PROBE_VERSION = 1
MAX_CACHED_REPORT_BYTES = 1024 * 1024
_PREFIX = "__PYMSS_ROCM_PROBE__"
_MODES = ("miopen", "nativeHip")
_CHECKS = ("allocation", "stftIstft", "conv", "batchNorm", "instanceNorm", "sdpa", "rnn")


class ProbeCleanupError(RuntimeError):
    pass


class ProbeStageError(RuntimeError):
    def __init__(self, stage: str, error: BaseException) -> None:
        super().__init__("An isolated diagnostic stage failed")
        self.stage = stage
        self.original_error = error


def _safe_exception_context(error: BaseException) -> dict[str, str]:
    """Retain a bounded cause, excluding native source and invocation dumps."""
    if isinstance(error, subprocess.TimeoutExpired):
        detail = f"The diagnostic subprocess exceeded {error.timeout} seconds"
    elif isinstance(error, subprocess.CalledProcessError):
        detail = f"The diagnostic subprocess exited with status {error.returncode}"
    else:
        lines = []
        for line in str(error).splitlines()[:2]:
            if re.search(
                r"(?i)(?:\b(?:argv|environ(?:ment)?|env|payload|command)\s*[:=]|"
                r"\b(?:kernel\s+source|source\s+code|source)\s*[:=]|"
                r"^\s*(?:command\b|traceback\b|file\s+[\"']|#\s*(?:include|define|pragma)|\d+\s*\|)|"
                r"\b(?:__global__|__device__|__host__|template\s*<)|"
                r"\b(?:extern\s+[\"']C[\"']|(?:int|void|float|double|half|auto)\s+\w+\s*\(|std::|at::)|"
                r"[\[{]\s*[\"']|"
                r"(?:^|[\\/\s])(?:clang(?:\+\+)?|hipcc|python(?:\d(?:\.\d+)?)?|cmd|powershell)(?:\.exe)?\s+-)",
                line,
            ):
                continue
            lines.append(line)
        # Redact before truncation so a token/URL cut in the middle cannot evade
        # the existing assignment and URL-credential redaction rules.
        detail = _redact_sensitive_text("\n".join(lines))
        home = str(Path.home()).rstrip("/\\")
        if home:
            home_pattern = r"[\\/]".join(re.escape(part) for part in re.split(r"[\\/]+", home))
            detail = re.sub(home_pattern + r"(?=[\\/\s\"':,;)]|$)", "<home>", detail, flags=re.IGNORECASE)
        # Diagnostic messages can include non-printing native bytes; preserve
        # only line breaks and printable characters in the bounded cause.
        detail = "".join(character for character in detail if character.isprintable() or character == "\n")[:500]
    result = {"exceptionType": type(error).__name__[:80]}
    if detail:
        result["detail"] = detail
    return result


def _validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    task_id = payload.get("taskId")
    if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 128:
        raise ValueError("taskId must be a non-empty string of at most 128 characters")
    devices = payload.get("deviceIds")
    if devices is not None and (not isinstance(devices, list) or not 1 <= len(devices) <= 8
                                or any(type(value) is not int or value < 0 or value > 255 for value in devices)):
        raise ValueError("deviceIds must contain between 1 and 8 non-negative device indices")
    modes = payload.get("modes", list(_MODES))
    if (not isinstance(modes, list) or not modes or len(modes) > 2
            or any(not isinstance(value, str) or value not in _MODES for value in modes)
            or len(set(modes)) != len(modes)):
        raise ValueError("modes must contain unique miopen or nativeHip values")
    fp16 = payload.get("includeFp16", False)
    if type(fp16) is not bool:
        raise ValueError("includeFp16 must be a boolean")
    timeout = payload.get("timeoutSeconds", 45)
    if type(timeout) not in (float, int) or not math.isfinite(timeout) or not 5 <= timeout <= 120:
        raise ValueError("timeoutSeconds must be a number between 5 and 120")
    return {"taskId": task_id, "deviceIds": list(dict.fromkeys(devices)) if devices else None,
            "modes": modes, "includeFp16": fp16, "timeoutSeconds": float(timeout)}


def _runtime_context() -> tuple[dict[str, Any], dict[str, str]]:
    """Read the running interpreter's state, not a possibly different active env."""
    from worker_runtime_paths import resolve_rocm_native_paths

    root = Path(sys.prefix).resolve()
    try:
        state = json.loads((root / "pymss-runtime-state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    # Rust has already selected and resolved the runtime. Revalidate persisted
    # SDK dirs, and prepend only directories within this exact interpreter env.
    paths = resolve_rocm_native_paths(root, state)
    additions = [*paths["nativeLibraryDirs"], *paths["nativeToolDirs"]]
    if additions:
        environment["PATH"] = os.pathsep.join([*dict.fromkeys(additions), environment.get("PATH", "")])
        if os.name != "nt":
            environment["LD_LIBRARY_PATH"] = os.pathsep.join(
                [*paths["nativeLibraryDirs"], environment.get("LD_LIBRARY_PATH", "")])
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUNBUFFERED"] = "1"
    return state, environment


def _report_path() -> Path:
    configured = os.environ.get("PYMSS_STUDIO_DATA_ROOT")
    runtime_root = os.environ.get("PYMSS_STUDIO_RUNTIME_ENVS_DIR")
    root = (Path(configured).expanduser() if configured else Path(runtime_root).expanduser().parent
            if runtime_root else Path.home() / ".pymss-studio")
    return root / "runtime-diagnostics" / "rocm-gpu-probe.json"


def _write_report(report: dict[str, Any]) -> None:
    from worker_bootstrap import _atomic_write_json

    _atomic_write_json(_report_path(), report)


def _base_report(state: dict[str, Any], task_id: str) -> dict[str, Any]:
    return {"taskId": task_id, "probeVersion": PROBE_VERSION, "status": "unavailable",
            "profileId": state.get("profileId"), "baseFingerprint": state.get("baseFingerprint"),
            "targetFingerprint": state.get("targetFingerprint"),
            "checkedAt": datetime.now(timezone.utc).isoformat(), "cacheReusable": False,
            "matchesCurrentRuntime": True, "fromCache": False, "deviceCapabilities": []}


def _check_result(check: str, precision: str, status: str, message: str, code: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"id": check, "precision": precision, "status": status, "message": message,
                              "requiredForBasic": check == "allocation" and precision == "fp32"}
    if code:
        result["errorCode"] = code
    return result


def _error_category(error: BaseException) -> tuple[str, str, bool]:
    # Native messages are classified here; only _safe_exception_context may
    # include a bounded, redacted cause in the resulting diagnostic record.
    text = str(error).lower()
    from worker_rocm import classify_rocm_failure

    category = classify_rocm_failure(error)
    if category == "context_lost":
        return "ROCM_CONTEXT_FAILED", "The GPU context failed; the isolated mode was stopped", True
    if category == "out_of_memory" or "out of memory" in text or "hiperroroutofmemory" in text:
        return "ROCM_PROBE_OUT_OF_MEMORY", "The diagnostic allocation ran out of device memory", True
    if category == "missing_kernel":
        return "ROCM_KERNEL_UNAVAILABLE", "The runtime has no compatible GPU kernel for this operator", False
    if category == "library_mismatch":
        return "ROCM_LIBRARY_MISMATCH", "The ROCm operator could not load compatible runtime libraries", False
    if "miopen" in text or "hiprtc" in text:
        return "ROCM_OPERATOR_FAILED", "The ROCm operator or runtime compiler failed", False
    if isinstance(error, (NotImplementedError, AttributeError)) or "not implemented" in text or "not supported" in text:
        return "ROCM_OPERATOR_UNAVAILABLE", "The runtime does not provide this operator and precision", False
    if isinstance(error, AssertionError):
        return "ROCM_REFERENCE_MISMATCH", "The operator output differs from the CPU reference", False
    return "ROCM_OPERATOR_FAILED", "The isolated GPU operator check failed", False


def _internal_emit(kind: str, value: dict[str, Any]) -> None:
    print(_PREFIX + json.dumps({"kind": kind, "value": value}, ensure_ascii=True), flush=True)


def _inventory(torch: Any) -> dict[str, Any]:
    if not getattr(torch.version, "hip", None):
        return {"available": False, "errorCode": "ROCM_RUNTIME_UNAVAILABLE",
                "message": "The selected interpreter does not provide a ROCm Torch build", "devices": []}
    if not torch.cuda.is_available():
        return {"available": False, "errorCode": "ROCM_DEVICE_UNAVAILABLE",
                "message": "The selected ROCm runtime has no available AMD GPU", "devices": []}
    devices = []
    for index in range(min(torch.cuda.device_count(), 256)):
        properties = torch.cuda.get_device_properties(index)
        raw = str(getattr(properties, "gcnArchName", "unknown"))[:160]
        architecture = re.match(r"^(gfx[0-9a-f]+)(?::|$)", raw, re.IGNORECASE)
        devices.append({"index": index, "name": str(properties.name)[:160], "rawArchitecture": raw,
                        "gfx": architecture.group(1).lower() if architecture else "unknown",
                        # Torch's compiled HIP version is not the installed driver.
                        "stableId": "unknown", "driverVersion": "unknown"})
    return {"available": bool(devices), "torchVersion": str(torch.__version__),
            "hipVersion": str(torch.version.hip), "devices": devices}


def _synchronize(torch: Any, device: Any) -> None:
    if device.type == "cuda":
        torch.cuda.synchronize(device)


def _assert_output(torch: Any, device: Any, output: Any, reference: Any, *, fp16: bool = False) -> None:
    _synchronize(torch, device)
    if not bool(torch.isfinite(output).all().item()):
        raise AssertionError("Non-finite output")
    torch.testing.assert_close(output.cpu().to(reference.dtype), reference,
                               rtol=2e-2 if fp16 else 2e-3, atol=5e-3 if fp16 else 2e-4)


def run_operator_check(torch: Any, device: Any, check_id: str, precision: str = "fp32") -> None:
    """Small deterministic reference comparisons, also testable on CPU offline."""
    import copy

    if precision not in {"fp32", "fp16"}:
        raise ValueError("Unknown diagnostic precision")
    check = check_id
    fp16 = precision == "fp16"
    dtype = torch.float16 if fp16 else torch.float32
    functional = torch.nn.functional
    with torch.no_grad():
        if check == "allocation":
            source = torch.arange(64, dtype=torch.float32, device="cpu").reshape(8, 8) / 32
            actual_input = source.to(device=device, dtype=dtype)
            output = actual_input @ actual_input.T + 1
            rounded = source.to(dtype).float()
            _assert_output(torch, device, output, rounded @ rounded.T + 1, fp16=fp16)
        elif check == "stftIstft":
            # FP16 FFT is genuinely executed when requested. Unsupported complex
            # half paths are recorded separately, without an FP32 substitution.
            source = torch.sin(torch.arange(2048, dtype=torch.float32, device="cpu") * 0.17).to(dtype)
            window = torch.hann_window(256, dtype=torch.float32, device="cpu").to(dtype)
            inputs, gpu_window = source.to(device), window.to(device)
            spectrum = torch.stft(inputs, n_fft=256, hop_length=64, window=gpu_window, return_complex=True)
            reference = torch.stft(source.float(), n_fft=256, hop_length=64,
                                   window=window.float(), return_complex=True)
            _assert_output(torch, device, spectrum, reference, fp16=fp16)
            restored = torch.istft(spectrum, n_fft=256, hop_length=64, window=gpu_window, length=2048)
            _assert_output(torch, device, restored, source.float(), fp16=fp16)
        elif check in {"conv", "conv1d", "conv3d", "convTranspose1d", "convTranspose2d", "convTranspose3d"}:
            dimensions = 2 if check == "conv" else int(check[-2])
            transpose = "Transpose" in check
            spatial = (8,) * dimensions if dimensions < 3 else (4,) * dimensions
            source_shape = (1, 2, *spatial)
            channels = (2, 3) if transpose else (3, 2)
            weight_shape = (*channels, *((3,) * dimensions))
            source = torch.linspace(-0.7, 0.7, math.prod(source_shape), dtype=torch.float32,
                                    device="cpu").reshape(source_shape).to(dtype)
            weights = torch.linspace(-0.2, 0.2, math.prod(weight_shape), dtype=torch.float32,
                                     device="cpu").reshape(weight_shape).to(dtype)
            operation = getattr(functional, f"conv{'_transpose' if transpose else ''}{dimensions}d")
            output = operation(source.to(device), weights.to(device), padding=1)
            reference = operation(source.float(), weights.float(), padding=1)
            _assert_output(torch, device, output, reference, fp16=fp16)
        elif check in {"batchNorm1d", "batchNorm3d", "instanceNorm1d", "instanceNorm3d"}:
            dimensions = int(check[-2])
            shape = (2, 4, *((8,) if dimensions == 1 else (4, 4, 4)))
            source = torch.linspace(-0.7, 0.7, math.prod(shape), dtype=torch.float32,
                                    device="cpu").reshape(shape).to(dtype)
            weights = torch.linspace(0.7, 1.3, 4, dtype=torch.float32, device="cpu").to(dtype)
            bias = torch.linspace(-0.2, 0.2, 4, dtype=torch.float32, device="cpu").to(dtype)
            if check.startswith("batch"):
                output = functional.batch_norm(source.to(device), None, None, weight=weights.to(device),
                                               bias=bias.to(device), training=True)
                reference = functional.batch_norm(source.float(), None, None, weight=weights.float(),
                                                  bias=bias.float(), training=True)
            else:
                output = functional.instance_norm(source.to(device), weight=weights.to(device), bias=bias.to(device))
                reference = functional.instance_norm(source.float(), weight=weights.float(), bias=bias.float())
            _assert_output(torch, device, output, reference, fp16=fp16)
        elif check == "batchNorm":
            if not fp16:
                from worker_rocm import _probe_batch_norm
                _probe_batch_norm(torch, device, verify_reference=True)
            else:
                source = (torch.arange(256, dtype=torch.float32, device="cpu").reshape(2, 4, 4, 8) / 64).to(dtype)
                output = functional.batch_norm(source.to(device), None, None, training=True)
                reference = functional.batch_norm(source.float(), None, None, training=True)
                _assert_output(torch, device, output, reference, fp16=True)
        elif check == "instanceNorm":
            from worker_rocm import _probe_instance_norm
            _probe_instance_norm(torch, device, amp=fp16, verify_reference=True)
        elif check == "sdpa":
            source = torch.linspace(-0.5, 0.5, 256, dtype=torch.float32, device="cpu").reshape(1, 2, 8, 16).to(dtype)
            inputs = source.to(device)
            output = functional.scaled_dot_product_attention(inputs, inputs, inputs, dropout_p=0.0)
            rounded = source.float()
            reference = torch.softmax(rounded @ rounded.transpose(-2, -1) / 4, dim=-1) @ rounded
            _assert_output(torch, device, output, reference, fp16=fp16)
        elif check in {"rnn", "gru", "rnnSimple"}:
            source = torch.linspace(-0.5, 0.5, 64, dtype=torch.float32, device="cpu").reshape(8, 1, 8).to(dtype)
            model_type = {"rnn": torch.nn.LSTM, "gru": torch.nn.GRU, "rnnSimple": torch.nn.RNN}[check]
            reference_model = model_type(8, 4, device="cpu", dtype=torch.float32).eval()
            for parameter in reference_model.parameters():
                parameter.copy_(torch.linspace(-0.1, 0.1, parameter.numel(), dtype=torch.float32,
                                                device="cpu").reshape(parameter.shape))
            model = copy.deepcopy(reference_model).to(device=device, dtype=dtype)
            reference_model.load_state_dict({name: value.float().cpu() for name, value in model.state_dict().items()})
            output, _ = model(source.to(device))
            reference, _ = reference_model(source.float())
            _assert_output(torch, device, output, reference, fp16=fp16)
        else:
            raise ValueError("Unknown diagnostic operator")


def _run_mode(torch: Any, index: int, mode: str, fp16: bool, sink: Callable = _internal_emit) -> None:
    if not getattr(torch.version, "hip", None) or not torch.cuda.is_available():
        sink("result", {"status": "unavailable", "errorCode": "ROCM_DEVICE_UNAVAILABLE"})
        return
    torch.cuda.set_device(index)
    device = torch.device("cuda", index)
    torch.backends.cudnn.enabled = mode == "miopen"
    # Prevent host preferences from silently changing the FP32 reference path.
    if hasattr(torch.backends.cudnn, "allow_tf32"):
        torch.backends.cudnn.allow_tf32 = False
    checks = []
    stopped = False
    for precision in (["fp32", "fp16"] if fp16 else ["fp32"]):
        for check in _CHECKS:
            if stopped:
                result = _check_result(check, precision, "skipped", "The isolated mode stopped after a GPU failure")
            else:
                try:
                    run_operator_check(torch, device, check, precision)
                    result = _check_result(check, precision, "passed", "GPU output matched the CPU reference")
                except Exception as error:
                    code, message, fatal = _error_category(error)
                    result = _check_result(check, precision, "unavailable" if code == "ROCM_OPERATOR_UNAVAILABLE" else "failed",
                                           message, code)
                    result.update(_safe_exception_context(error))
                    stopped = fatal or (check == "allocation" and precision == "fp32")
            checks.append(result)
            sink("check", result)
    sink("result", {"status": "partial" if stopped else "completed", "checks": checks})


def _child_main(payload: dict[str, Any]) -> int:
    dll_handles = []
    try:
        if os.name == "nt":
            from worker_runtime_paths import resolve_rocm_native_paths

            try:
                state = json.loads((Path(sys.prefix) / "pymss-runtime-state.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
            paths = resolve_rocm_native_paths(Path(sys.prefix), state if isinstance(state, dict) else {})
            dll_handles = [os.add_dll_directory(path) for path in paths["nativeLibraryDirs"]]
        import torch
    except Exception as error:
        for handle in dll_handles:
            handle.close()
        _internal_emit("result", {"available": False, "status": "unavailable", "devices": [],
                                  "errorCode": "ROCM_RUNTIME_UNAVAILABLE",
                                  "message": "Torch could not be imported in the selected runtime",
                                  "failureStage": "torch_import", **_safe_exception_context(error)})
        return 0
    try:
        if payload["phase"] == "inventory":
            _internal_emit("result", _inventory(torch))
        else:
            _run_mode(torch, payload["index"], payload["mode"], payload["includeFp16"])
        return 0
    except Exception as error:
        code, message, _ = _error_category(error)
        _internal_emit("result", {"available": False, "status": "unavailable", "errorCode": code,
                                  "message": message, "devices": [], "failureStage": payload["phase"],
                                  **_safe_exception_context(error)})
        return 0
    finally:
        for handle in dll_handles:
            handle.close()


def _posix_descendants(pid: int) -> list[int]:
    snapshot = subprocess.run(["ps", "-e", "-o", "pid=,ppid="], capture_output=True,
                              encoding="ascii", errors="replace", check=True, timeout=5)
    edges: dict[int, list[int]] = {}
    for line in snapshot.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and all(value.isdecimal() for value in fields):
            child, parent = map(int, fields)
            edges.setdefault(parent, []).append(child)
    result, pending = [], list(edges.get(pid, []))
    while pending:
        child = pending.pop()
        if child not in result:
            result.append(child)
            pending.extend(edges.get(child, []))
    return result


class _WindowsJob:
    """Nested, kill-on-close job; the child waits on stdin until assignment."""

    def __init__(self, child: subprocess.Popen) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("perProcess", ctypes.c_longlong), ("perJob", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("minWorkingSet", ctypes.c_size_t),
                        ("maxWorkingSet", ctypes.c_size_t), ("activeLimit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", ctypes.c_ulonglong * 6),
                        ("processMemory", ctypes.c_size_t), ("jobMemory", ctypes.c_size_t),
                        ("peakProcessMemory", ctypes.c_size_t), ("peakJobMemory", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [("times", ctypes.c_longlong * 4), ("pageFaults", wintypes.DWORD),
                        ("total", wintypes.DWORD), ("active", wintypes.DWORD), ("terminated", wintypes.DWORD)]

        self.ctypes, self.accounting = ctypes, Accounting
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        process = None
        try:
            limits = ExtendedLimits()
            limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            process = self.kernel.OpenProcess(0x0100 | 0x0001, False, child.pid)
            if not process or not self.kernel.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise
        finally:
            if process:
                self.kernel.CloseHandle(process)

    def stop(self) -> None:
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise ProbeCleanupError("The diagnostic job could not be terminated")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            accounting = self.accounting()
            if not self.kernel.QueryInformationJobObject(self.handle, 1, self.ctypes.byref(accounting),
                                                         self.ctypes.sizeof(accounting), None):
                break
            if accounting.active == 0:
                return
            time.sleep(0.01)
        raise ProbeCleanupError("The diagnostic job did not become empty")

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _stop_child_tree(child: subprocess.Popen, job: _WindowsJob | None = None) -> None:
    """Confirm a timed-out mode's subtree stopped before starting another mode."""
    if os.name == "nt":
        if job is None:
            raise ProbeCleanupError("The diagnostic job is unavailable")
        job.stop()
        child.wait(timeout=5)
        return
    try:
        # Keep the mode in Rust's outer worker group. Stop it before traversing so
        # it cannot fork while the subtree is enumerated, then stop descendants.
        if child.poll() is not None:
            raise ProbeCleanupError("The diagnostic parent exited before its remaining subtree could be identified")
        os.kill(child.pid, signal.SIGSTOP)
        descendants: list[int] = []
        for _ in range(16):
            observed = _posix_descendants(child.pid)
            for pid in observed:
                try:
                    os.kill(pid, signal.SIGSTOP)
                except ProcessLookupError:
                    pass
            if set(observed) == set(descendants):
                break
            descendants = observed
        else:
            raise ProbeCleanupError("The diagnostic subprocess tree did not become stable")
        for pid in reversed(descendants):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.kill()
        child.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            # Dead orphan zombies have released GPU, handles and inherited pipes.
            snapshot = subprocess.run(["ps", "-o", "stat=", "-p", ",".join(map(str, descendants))],
                                      capture_output=True, text=True, timeout=2, check=False) if descendants else None
            if snapshot is None or not any(value and not value.startswith("Z") for value in snapshot.stdout.split()):
                return
            time.sleep(0.02)
    except (OSError, subprocess.SubprocessError):
        try:
            child.kill()
            child.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
    raise ProbeCleanupError("The isolated diagnostic process tree exit could not be confirmed")


def _run_child(payload: dict[str, Any], environment: dict[str, str], timeout: float,
               on_check: Callable[[dict[str, Any]], None], cancelled: Event | None = None) -> dict[str, Any]:
    # A handshake lets Windows assign a nested job before any native code can
    # spawn descendants. The child inherits Rust's outer job/process group too.
    runner = ("import runpy,sys;sys.stdin.readline();sys.path.insert(0,sys.argv[1]);"
              "runpy.run_module('worker_rocm_probe',run_name='__main__')")
    command = [sys.executable, "-I", "-B", "-u", "-c", runner, str(Path(__file__).resolve().parent),
               json.dumps(payload, separators=(",", ":"))]
    messages: Queue = Queue(maxsize=128)
    reading_done = Event()
    job = None
    try:
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as error:
        raise ProbeStageError(f"{payload['phase']}_process_start", error) from error
    try:
        if os.name == "nt":
            job = _WindowsJob(child)
        assert child.stdin is not None
        child.stdin.write(b"\n")
        child.stdin.close()
    except BaseException as error:
        # The child has not passed the handshake and cannot have descendants.
        child.kill()
        child.wait(timeout=5)
        if child.stdin is not None:
            child.stdin.close()
        if child.stdout is not None:
            child.stdout.close()
        if job is not None:
            job.close()
        if isinstance(error, Exception):
            raise ProbeStageError("process_isolation", error) from error
        raise

    try:

        def reader() -> None:
            try:
                assert child.stdout is not None
                while chunk := child.stdout.readline(16384):
                    if not chunk.startswith(_PREFIX.encode()) or len(chunk) >= 16384:
                        continue
                    try:
                        value = json.loads(chunk[len(_PREFIX):])
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if isinstance(value, dict):
                        try:
                            messages.put(value, timeout=0.1)
                        except Exception:
                            break
            finally:
                reading_done.set()

        reader_thread = Thread(target=reader, daemon=True)
        reader_thread.start()
        deadline, result, checks = time.monotonic() + timeout, None, []
        interrupted = None
        try:
            while True:
                if cancelled is not None and cancelled.is_set():
                    interrupted = "cancelled"
                    break
                if time.monotonic() >= deadline:
                    interrupted = "timeout"
                    break
                try:
                    message = messages.get(timeout=0.025)
                except Empty:
                    if child.poll() is not None and reading_done.is_set():
                        break
                    continue
                value = message.get("value")
                if not isinstance(value, dict):
                    continue
                if message.get("kind") == "check" and value.get("id") in _CHECKS:
                    checks.append(value)
                    on_check(value)
                elif message.get("kind") == "result":
                    result = value
            if interrupted:
                _stop_child_tree(child, job)
                return {"status": interrupted, "checks": checks, "available": False,
                        "errorCode": "ROCM_PROBE_TIMEOUT" if interrupted == "timeout" else "ROCM_PROBE_CANCELLED",
                        "message": "The isolated GPU diagnostic timed out" if interrupted == "timeout" else "The GPU diagnostic was cancelled"}
            child.wait(timeout=5)
            if job is not None:
                # Even a normally completed Python process may leave a native
                # compiler running. Empty the job and confirm before returning.
                job.stop()
            if child.returncode or result is None:
                return {"status": "partial", "available": False, "checks": checks,
                        "errorCode": "ROCM_PROBE_CHILD_FAILED", "message": "The isolated GPU diagnostic exited without a complete result",
                        "exitCode": child.returncode, "failureStage": payload["phase"]}
            return result
        finally:
            if child.poll() is None:
                _stop_child_tree(child, job)
            reader_thread.join(timeout=1)
            if reader_thread.is_alive():
                # Closing a buffered pipe being read by another thread can wait
                # forever. Let the owning worker fail and Rust stop its group.
                raise ProbeCleanupError("The diagnostic output pipe remains owned by a subprocess")
            if child.stdout is not None:
                child.stdout.close()
    finally:
        if job is not None:
            job.close()


def _cache_identity(report: dict[str, Any]) -> str | None:
    devices = report.get("deviceCapabilities") or []
    if not devices or not report.get("baseFingerprint") or not report.get("targetFingerprint"):
        return None
    if any(not device.get("stableId") or device["stableId"] == "unknown"
           or not device.get("driverVersion") or device["driverVersion"] == "unknown" for device in devices):
        return None
    identity = {key: report.get(key) for key in ("probeVersion", "baseFingerprint", "targetFingerprint")}
    identity["devices"] = [{key: device.get(key) for key in ("index", "stableId", "gfx", "driverVersion")}
                           for device in devices]
    identity["coverage"] = [[(mode["mode"], [(check["id"], check["precision"]) for check in mode["checks"]])
                             for mode in device["modes"]] for device in devices]
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def run_gpu_probe(payload: dict[str, Any], *, cancelled: Event | None = None) -> dict[str, Any]:
    request = _validate_payload(payload)
    try:
        state, environment = _runtime_context()
    except Exception as error:
        raise ProbeStageError("runtime_context", error) from error
    task_id = request["taskId"]
    report = _base_report(state, task_id)
    emit("runtime_gpu_probe_started", {**request, "probeVersion": PROBE_VERSION}, task_id=task_id)
    emit("runtime_gpu_probe_stage", {"taskId": task_id, "phase": "inventory"}, task_id=task_id)
    inventory = _run_child({"phase": "inventory"}, environment, min(request["timeoutSeconds"], 20), lambda _check: None, cancelled)
    if not inventory.get("available"):
        report.update({key: inventory[key] for key in ("errorCode", "message", "exceptionType", "detail", "failureStage", "exitCode")
                       if key in inventory})
        report["status"] = "cancelled" if inventory.get("status") == "cancelled" else "unavailable"
    else:
        available = {device["index"]: device for device in inventory["devices"]}
        indices = request["deviceIds"] if request["deviceIds"] is not None else list(available)[:8]
        report.update({"status": "completed", "torchVersion": inventory.get("torchVersion"),
                       "hipVersion": inventory.get("hipVersion")})
        for index in indices:
            device = {**available.get(index, {"index": index, "name": "unknown", "rawArchitecture": "unknown",
                                             "gfx": "unknown", "stableId": "unknown", "driverVersion": "unknown"}),
                      "basicAvailable": False, "modes": []}
            report["deviceCapabilities"].append(device)
            for mode in request["modes"]:
                emit("runtime_gpu_probe_stage", {"taskId": task_id, "phase": "mode", "deviceIndex": index, "mode": mode}, task_id=task_id)
                if index not in available:
                    result = {"status": "unavailable", "checks": [], "errorCode": "ROCM_DEVICE_UNAVAILABLE",
                              "message": "The requested GPU index is unavailable in this runtime"}
                else:
                    result = _run_child({"phase": "mode", "index": index, "mode": mode,
                                         "includeFp16": request["includeFp16"]}, environment, request["timeoutSeconds"],
                                        lambda check: emit("runtime_gpu_probe_stage", {"taskId": task_id, "phase": "check",
                                                          "deviceIndex": index, "mode": mode, "check": check}, task_id=task_id), cancelled)
                checks = result.get("checks", [])
                if result.get("status") in {"timeout", "cancelled", "partial"}:
                    observed = {(check["id"], check["precision"]) for check in checks}
                    for precision in (["fp32", "fp16"] if request["includeFp16"] else ["fp32"]):
                        for check in _CHECKS:
                            if (check, precision) not in observed:
                                checks.append(_check_result(check, precision, "timeout" if result["status"] == "timeout" else "skipped",
                                                           result.get("message", "The isolated mode did not complete"), result.get("errorCode")))
                entry = {"mode": mode, "status": result.get("status", "unavailable"), "checks": checks,
                         "basicAvailable": any(check["id"] == "allocation" and check["precision"] == "fp32"
                                               and check["status"] == "passed" for check in checks)}
                for key in ("errorCode", "message", "exceptionType", "detail", "failureStage", "exitCode"):
                    if key in result:
                        entry[key] = result[key]
                device["modes"].append(entry)
                device["basicAvailable"] |= entry["basicAvailable"]
                if entry["status"] != "completed":
                    report["status"] = "cancelled" if entry["status"] == "cancelled" else "partial"
                if report["status"] == "cancelled":
                    return report
    identity = _cache_identity(report)
    report["cacheReusable"] = bool(identity and report["status"] == "completed")
    if identity:
        report["cacheKey"] = identity
    return report


def cmd_runtime_gpu_probe(payload: dict[str, Any]) -> int:
    try:
        _validate_payload(payload)
    except ValueError as error:
        return emit_error("ROCM_PROBE_INVALID_REQUEST", str(error), task_id=payload.get("taskId"), recoverable=True)
    try:
        report = run_gpu_probe(payload)
        if report["status"] != "cancelled":
            try:
                _write_report(report)
            except OSError as error:
                report["persistenceError"] = "The diagnostic report could not be saved"
                report["persistenceFailure"] = {"failureStage": "report_write", **_safe_exception_context(error)}
        emit("runtime_gpu_probe_finished", report, task_id=payload.get("taskId"))
        return 0
    except ProbeCleanupError as error:
        context = _safe_exception_context(error)
        return emit_error("ROCM_PROBE_CLEANUP_FAILED", "The diagnostic subprocess tree exit could not be confirmed",
                          detail=context.pop("detail", None), task_id=payload.get("taskId"), recoverable=True,
                          extra={"failureStage": "process_cleanup", **context})
    except Exception as error:
        stage = error.stage if isinstance(error, ProbeStageError) else "gpu_probe"
        context = _safe_exception_context(error.original_error if isinstance(error, ProbeStageError) else error)
        return emit_error("ROCM_PROBE_FAILED", "The isolated GPU diagnostic could not be started",
                          detail=context.pop("detail", None), task_id=payload.get("taskId"), recoverable=True,
                          extra={"failureStage": stage, **context})


def _cache_text(value: Any, maximum: int, *, nullable: bool = False) -> bool:
    return nullable and value is None or isinstance(value, str) and len(value) <= maximum


def _cache_context_valid(value: dict[str, Any]) -> bool:
    for field, maximum in (("message", 500), ("detail", 500), ("exceptionType", 80),
                           ("errorCode", 128), ("failureStage", 128)):
        if field in value and not _cache_text(value[field], maximum, nullable=True):
            return False
    exit_code = value.get("exitCode")
    return exit_code is None or type(exit_code) is int and -(2 ** 31) <= exit_code < 2 ** 32


def _valid_cached_report(report: Any) -> bool:
    """Check nested data before exposing a user-editable last report to the UI."""
    if (not isinstance(report, dict) or type(report.get("probeVersion")) is not int
            or not 1 <= report["probeVersion"] < 2 ** 31
            or report.get("status") not in ("completed", "partial", "unavailable", "cancelled")
            or type(report.get("cacheReusable")) is not bool
            or not _cache_text(report.get("checkedAt"), 80) or not _cache_context_valid(report)):
        return False
    try:
        datetime.fromisoformat(report["checkedAt"])
    except ValueError:
        return False
    for field in ("profileId", "baseFingerprint", "targetFingerprint", "cacheKey", "torchVersion", "hipVersion"):
        if field in report and not _cache_text(report[field], 256, nullable=True):
            return False
    if "taskId" in report and not _cache_text(report["taskId"], 128, nullable=True):
        return False
    for field in ("fromCache", "matchesCurrentRuntime"):
        if field in report and type(report[field]) is not bool:
            return False
    devices = report.get("deviceCapabilities")
    if not isinstance(devices, list) or len(devices) > 8:
        return False
    indices = set()
    for device in devices:
        if (not isinstance(device, dict) or type(device.get("index")) is not int
                or not 0 <= device["index"] <= 255 or device["index"] in indices
                or type(device.get("basicAvailable")) is not bool
                or any(not _cache_text(device.get(field), maximum)
                       for field, maximum in (("name", 160), ("rawArchitecture", 160), ("gfx", 80)))):
            return False
        indices.add(device["index"])
        if any(field in device and not _cache_text(device[field], 256, nullable=True)
               for field in ("stableId", "driverVersion")):
            return False
        modes = device.get("modes")
        if not isinstance(modes, list) or not 1 <= len(modes) <= 2:
            return False
        mode_ids = set()
        for mode in modes:
            if (not isinstance(mode, dict) or mode.get("mode") not in _MODES
                    or mode["mode"] in mode_ids or type(mode.get("basicAvailable")) is not bool
                    or mode.get("status") not in ("completed", "partial", "unavailable", "timeout", "cancelled")
                    or not _cache_context_valid(mode)):
                return False
            mode_ids.add(mode["mode"])
            checks = mode.get("checks")
            if not isinstance(checks, list) or len(checks) > len(_CHECKS) * 2:
                return False
            check_ids = set()
            basic_passed = False
            for check in checks:
                if (not isinstance(check, dict) or check.get("id") not in _CHECKS
                        or check.get("precision") not in ("fp32", "fp16")
                        or check.get("status") not in ("passed", "failed", "unavailable", "timeout", "skipped")
                        or type(check.get("requiredForBasic")) is not bool
                        or not _cache_text(check.get("message"), 500) or not _cache_context_valid(check)):
                    return False
                identity = (check["id"], check["precision"])
                if identity in check_ids or check["requiredForBasic"] != (identity == ("allocation", "fp32")):
                    return False
                check_ids.add(identity)
                basic_passed |= identity == ("allocation", "fp32") and check["status"] == "passed"
            if mode["basicAvailable"] != basic_passed:
                return False
        if device["basicAvailable"] != any(mode["basicAvailable"] for mode in modes):
            return False
    return True


def cmd_runtime_gpu_probe_report(payload: dict[str, Any]) -> int:
    try:
        # Bound the read itself, rather than trusting a racy stat size check.
        with _report_path().open("rb") as file:
            data = file.read(MAX_CACHED_REPORT_BYTES + 1)
        report = json.loads(data) if len(data) <= MAX_CACHED_REPORT_BYTES else None
        if not _valid_cached_report(report):
            report = None
    except (OSError, ValueError, RecursionError):
        report = None
    if report is not None:
        try:
            state, _environment = _runtime_context()
            matches = (report.get("probeVersion") == PROBE_VERSION
                       and all(report.get(key) and report.get(key) == state.get(key)
                               for key in ("profileId", "baseFingerprint", "targetFingerprint")))
        except Exception:
            matches = False
        report["matchesCurrentRuntime"] = bool(matches)
        report["fromCache"] = True
        # An offline report query cannot establish the actual current device and
        # installed driver, even if a previous inventory supplied an identity.
        report["cacheReusable"] = False
    emit("runtime_gpu_probe_report", {"report": report}, task_id=payload.get("taskId"))
    return 0


if __name__ == "__main__":
    raise SystemExit(_child_main(json.loads(sys.argv[-1])))
