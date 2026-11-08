"""Windows ROCm normalization checks and a native GPU compatibility path."""

from __future__ import annotations

import json
import os
import sys
import traceback
from collections.abc import Callable
from contextlib import nullcontext
from threading import Lock, RLock
from typing import Any


_lock = Lock()
_native_devices: set[int] = set()
_native_enabled = False
_inference_lock = RLock()
_native_checks: set[tuple[int, str, str]] = set()
_operation_failure: Exception | None = None
_context_failure: RuntimeError | None = None


class RocmCompatibilityError(RuntimeError):
    code = "ROCM_NATIVE_CHECK_FAILED"


class RocmInferenceError(RuntimeError):
    def __init__(self, code: str, error: BaseException):
        super().__init__(str(error))
        self.code = code
        self.original_traceback = getattr(error, "original_traceback", None) or getattr(error, "_rocm_original_traceback", None)


class RocmWorkerAbort(BaseException):
    """Cross upstream broad Exception handlers after the HIP context is lost."""
    def __init__(self, error: RuntimeError):
        super().__init__(str(error))
        self.error = error


def classify_rocm_failure(error: BaseException) -> str:
    """Share fatal context handling between inference and isolated diagnostics."""
    message = str(error).lower()
    if any(marker in message for marker in (
        "illegal memory access", "illegal address", "hiperrorillegaladdress", "device-side assert",
        "device side assert", "hiperrorassert", "hiperrorcontextisdestroyed", "context is destroyed",
        "context has been destroyed", "launch failure", "hiperrorlaunchfailure", "hiperrorpriorlaunchfailure",
        "device lost", "memory access fault",
    )):
        return "context_lost"
    if any(marker in message for marker in ("out of memory", "hiperroroutofmemory", "miopenstatusallocfailed")):
        return "out_of_memory"
    if any(marker in message for marker in (
        "no kernel image is available", "invalid device function", "hiperrornobinaryforgpu",
        "hiperrorinvaliddevicefunction", "no binary for gpu",
    )):
        return "missing_kernel"
    if any(marker in message for marker in (
        "dll load failed", "undefined symbol", "symbol not found", "procedure could not be found", "cannot load library",
    )):
        return "library_mismatch"
    return "miopen_compilation" if _known_miopen_failure(error) else "unknown"


def _uses_windows_rocm(device: Any = "cuda") -> bool:
    if sys.platform != "win32" or str(device).lower().startswith("cpu"):
        return False
    torch = sys.modules.get("torch")
    return bool(torch and getattr(getattr(torch, "version", None), "hip", None))


def _record_failure(error: Exception, device: Any) -> Exception:
    global _operation_failure, _context_failure
    if not _uses_windows_rocm(device):
        return error
    category = classify_rocm_failure(error)
    codes = {"context_lost": "ROCM_CONTEXT_LOST", "out_of_memory": "ROCM_OUT_OF_MEMORY",
             "missing_kernel": "ROCM_TARGET_MISSING", "library_mismatch": "ROCM_RUNTIME_INCOMPATIBLE"}
    if isinstance(error, RocmInferenceError) or category not in codes:
        recorded = error
    else:
        recorded = RocmInferenceError(codes[category], error)
    if recorded is not error:
        recorded.__cause__ = error
        recorded.__traceback__ = error.__traceback__
    _operation_failure = recorded
    if category == "context_lost":
        _context_failure = recorded
        raise RocmWorkerAbort(recorded) from error
    return recorded


def rocm_worker_poisoned() -> bool:
    return _context_failure is not None


def run_rocm_initialization(initializer: Callable[[], Any], device: Any) -> Any:
    """Model loading can touch HIP before the first forward; it never retries."""
    assert_rocm_worker_healthy()
    with _inference_lock if _uses_windows_rocm(device) else nullcontext():
        assert_rocm_worker_healthy()
        try:
            return initializer()
        except Exception as error:
            recorded = _record_failure(error, device)
            if recorded is error:
                raise
            raise recorded from error


def begin_rocm_operation() -> None:
    global _operation_failure
    assert_rocm_worker_healthy()
    _operation_failure = None


def assert_rocm_worker_healthy() -> None:
    if _context_failure is not None:
        raise RocmWorkerAbort(_context_failure)


def assert_rocm_operation_succeeded() -> None:
    assert_rocm_worker_healthy()
    if _operation_failure is not None:
        raise _operation_failure


def model_native_checks(model: Any, *, use_amp: bool = False, device: Any = "cuda") -> tuple[tuple[str, str], ...]:
    """Inspect the loaded network, rather than assigning requirements by a model name."""
    if not _uses_windows_rocm(device):
        return ()
    if not callable(getattr(model, "modules", None)):
        model = getattr(model, "model_run", None)
    if not callable(getattr(model, "modules", None)):
        return (("unverifiedModel", "fp32"),)
    import torch
    groups = {
        "conv": ("Conv2d",), "conv1d": ("Conv1d",), "conv3d": ("Conv3d",),
        "convTranspose1d": ("ConvTranspose1d",), "convTranspose2d": ("ConvTranspose2d",), "convTranspose3d": ("ConvTranspose3d",),
        "rnn": ("LSTM",), "gru": ("GRU",), "rnnSimple": ("RNN",),
        "instanceNorm": ("InstanceNorm2d",), "instanceNorm1d": ("InstanceNorm1d",), "instanceNorm3d": ("InstanceNorm3d",),
        "batchNorm": ("BatchNorm2d",), "batchNorm1d": ("BatchNorm1d",), "batchNorm3d": ("BatchNorm3d",),
    }
    modules = list(model.modules())
    result = []
    for check, names in groups.items():
        classes = tuple(cls for name in names if isinstance(cls := getattr(torch.nn, name, None), type))
        if classes and any(isinstance(module, classes) for module in modules):
            result.append((check, "fp32"))
            if use_amp:
                result.append((check, "fp16"))
    return tuple(result)


def _validate_native_checks(torch: Any, index: int, checks: tuple[tuple[str, str], ...]) -> None:
    for check, precision in checks:
        key = (index, check, precision)
        if key in _native_checks:
            continue
        if check == "unverifiedModel":
            raise RocmCompatibilityError("Native HIP recovery requires a model with inspectable operator requirements")
        if check == "instanceNorm":
            _probe_instance_norm(torch, torch.device("cuda", index), amp=precision == "fp16", verify_reference=True)
        else:
            from worker_rocm_probe import run_operator_check
            run_operator_check(torch, torch.device("cuda", index), check, precision)
        _native_checks.add(key)


def _probe_batch_norm(torch: Any, device: Any, *, verify_reference: bool = False) -> None:
    """Exercise the affine training kernel used by MIOpen, without model weights."""
    functional = torch.nn.functional
    with torch.no_grad():
        source = torch.arange(512, dtype=torch.float32, device="cpu").reshape(2, 4, 8, 8) / 64
        weight = torch.tensor([1.0, 1.1, 1.2, 1.3], dtype=torch.float32, device="cpu")
        bias = torch.tensor([0.0, 0.1, 0.2, 0.3], dtype=torch.float32, device="cpu")
        output = functional.batch_norm(
            source.to(device), torch.zeros(4, device=device, dtype=torch.float32), torch.ones(4, device=device, dtype=torch.float32),
            weight.to(device), bias.to(device), training=True,
        )
        if device.type == "cuda":
            torch.cuda.synchronize(device)
        if not bool(torch.isfinite(output).all().item()):
            raise RuntimeError("The GPU normalization check returned non-finite values")
        if verify_reference:
            reference = functional.batch_norm(
                source, torch.zeros(4, dtype=torch.float32, device="cpu"),
                torch.ones(4, dtype=torch.float32, device="cpu"), weight, bias, training=True,
            )
            torch.testing.assert_close(output.cpu(), reference, rtol=1e-3, atol=1e-4)


def _known_miopen_failure(error: BaseException) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in (
        "miopenstatusunknownerror", "hiprtc_error_compilation", "'type_traits' file not found",
    ))


def _probe_instance_norm(torch: Any, device: Any, *, amp: bool = False, verify_reference: bool = False) -> None:
    """Exercise HyperACE's affine InstanceNorm, including half-precision input."""
    functional = torch.nn.functional
    with torch.no_grad():
        source = (torch.arange(16384, dtype=torch.float32, device="cpu") % 97 - 48).reshape(1, 64, 16, 16) / 16
        weight = torch.linspace(0.7, 1.3, 64, dtype=torch.float32, device="cpu")
        bias = torch.linspace(-0.2, 0.2, 64, dtype=torch.float32, device="cpu")
        inputs = source.to(device=device, dtype=torch.float16 if amp else torch.float32)
        with torch.autocast(device_type=device.type, dtype=torch.float16, enabled=amp and device.type == "cuda"):
            output = functional.instance_norm(inputs, weight=weight.to(device), bias=bias.to(device), eps=1e-8)
        if device.type == "cuda":
            torch.cuda.synchronize(device)
        if not bool(torch.isfinite(output).all().item()):
            raise RuntimeError("The GPU instance normalization check returned non-finite values")
        if verify_reference:
            reference = functional.instance_norm(inputs.float().cpu(), weight=weight, bias=bias, eps=1e-8)
            torch.testing.assert_close(
                output.float().cpu(), reference,
                rtol=1e-2 if amp else 1e-3, atol=3e-3 if amp else 1e-4,
            )


def _enable_native(torch: Any, ids: list[int], logger: Any, cause: BaseException | None,
                   checks: tuple[tuple[str, str], ...] = ()) -> bool:
    """Called with the compatibility lock held, before committing the global flag."""
    global _native_enabled
    previous = torch.backends.cudnn.enabled
    torch.backends.cudnn.enabled = False
    try:
        for index in ids:
            # Optional diagnostic operators must not become model requirements.
            _probe_batch_norm(torch, torch.device("cuda", index), verify_reference=True)
            _validate_native_checks(torch, index, checks)
    except Exception as fallback_error:
        torch.backends.cudnn.enabled = previous
        failure = RocmCompatibilityError(
            f"ROCm MIOpen failure ({cause}); the native GPU self-check also failed: {fallback_error}"
        )
        failure.__cause__ = fallback_error
        failure.original_traceback = getattr(cause, "_rocm_original_traceback", None)
        recorded = _record_failure(failure, "cuda")
        raise recorded from fallback_error
    _native_enabled = True
    _native_devices.update(ids)
    if logger:
        if cause is not None:
            logger.warning(
                f"ROCm MIOpen compilation is unavailable ({str(cause)[:300]}). "
                "Native HIP passed the FP32 reference check; miopenEnabled=False "
                "for this worker. Performance may differ."
            )
        else:
            logger.info("ROCm forced native HIP mode validated; miopenEnabled=False for this worker")
    return True


def configure_rocm_runtime(
    device: Any = "auto", device_ids: Any = None, logger: Any = None, *, failure: BaseException | None = None,
    required_checks: tuple[tuple[str, str], ...] = (),
) -> bool:
    """Keep a verified native backend after an actual failure or explicit request.

    Automatic startup leaves the selected model's capabilities untouched. The
    full normalization matrix is available only through diagnose_rocm().
    """
    requested = str(device or "auto").strip().lower()
    assert_rocm_worker_healthy()
    if sys.platform != "win32" or requested.startswith("cpu") or requested not in {"auto", "cuda"} and not requested.startswith("cuda:"):
        return False
    with _inference_lock:
        assert_rocm_worker_healthy()
        import torch
        if not getattr(torch.version, "hip", None) or not torch.cuda.is_available():
            return False
        mode = os.environ.get("PYMSS_STUDIO_ROCM_COMPAT", "auto").strip().lower()
        if mode == "off":
            if logger:
                logger.info("ROCm compatibility handling is disabled for this worker")
            return False
        selected_ids = isinstance(device_ids, (list, tuple)) and bool(device_ids)
        ids = list(device_ids) if selected_ids else [torch.cuda.current_device()]
        if requested.startswith("cuda:"):
            primary = int(requested.split(":", 1)[1])
            ids = [primary, *ids] if selected_ids else [primary]
        ids = list(dict.fromkeys(int(value) for value in ids))
        with _lock:
            if not _native_enabled and mode != "native" and failure is None:
                return False
            if not _native_enabled and not torch.backends.cudnn.enabled:
                return False
            if failure is not None:
                # Release failed forward activations before native validation allocates.
                failure._rocm_original_traceback = "".join(traceback.format_exception(failure))
                traceback.clear_frames(failure.__traceback__)
                failure.__traceback__ = None
            if _native_enabled:
                torch.backends.cudnn.enabled = False
                try:
                    for index in ids:
                        if index not in _native_devices:
                            _probe_batch_norm(torch, torch.device("cuda", index), verify_reference=True)
                            _native_devices.add(index)
                        _validate_native_checks(torch, index, required_checks)
                except Exception as error:
                    failure = RocmCompatibilityError(f"Native HIP required operator validation failed: {error}")
                    failure.__cause__ = error
                    recorded = _record_failure(failure, device)
                    raise recorded from error
                return True
            return _enable_native(torch, ids, logger, failure, required_checks)


def run_rocm_inference(inference: Callable[[], Any], device: Any, device_ids: Any, logger: Any,
                       *, required_checks: tuple[tuple[str, str], ...] = ()) -> Any:
    """Recover a real MIOpen failure before pymss catches it and saves outputs."""
    assert_rocm_worker_healthy()
    def execute() -> Any:
        result = inference()
        if _uses_windows_rocm(device):
            torch = sys.modules["torch"]
            synchronize = getattr(torch.cuda, "synchronize", None)
            if callable(synchronize):
                ids = list(device_ids) if isinstance(device_ids, (list, tuple)) and device_ids else [torch.cuda.current_device()]
                if str(device).startswith("cuda:"):
                    ids.insert(0, int(str(device).split(":", 1)[1]))
                for index in dict.fromkeys(ids):
                    synchronize(index)
        return result
    with _inference_lock if _uses_windows_rocm(device) else nullcontext():
        assert_rocm_worker_healthy()
        if _native_enabled and _uses_windows_rocm(device):
            configure_rocm_runtime(device, device_ids, logger, required_checks=required_checks)
        try:
            return execute()
        except RuntimeError as error:
            category = classify_rocm_failure(error)
            if category != "miopen_compilation":
                recorded = _record_failure(error, device)
                if recorded is error:
                    raise
                raise recorded from error
            options = {"required_checks": required_checks} if required_checks else {}
            if not configure_rocm_runtime(device, device_ids, logger, failure=error, **options):
                _record_failure(error, device)
                raise
            if logger:
                logger.warning("Retrying separation once with native HIP after an actual MIOpen failure")
            try:
                return execute()
            except Exception as retry_error:
                recorded = _record_failure(retry_error, device)
                if isinstance(recorded, RocmInferenceError):
                    recorded.original_traceback = getattr(error, "_rocm_original_traceback", None)
                    raise recorded from retry_error
                failure = RocmCompatibilityError(
                    f"ROCm MIOpen inference failed ({error}); the native HIP inference retry failed: {retry_error}"
                )
                failure.__cause__ = retry_error
                failure.original_traceback = getattr(error, "_rocm_original_traceback", None)
                recorded = _record_failure(failure, device)
                raise recorded from retry_error
        except Exception as error:
            recorded = _record_failure(error, device)
            if recorded is error:
                raise
            raise recorded from error


def diagnose_rocm() -> dict[str, Any]:
    import torch
    result: dict[str, Any] = {
        "platform": sys.platform, "torchVersion": torch.__version__,
        "hipVersion": getattr(torch.version, "hip", None),
        "gpuAvailable": bool(torch.cuda.is_available()), "probeVersion": 2, "checks": [],
    }
    if sys.platform != "win32" or not result["hipVersion"] or not result["gpuAvailable"]:
        result["message"] = "An available Windows ROCm GPU runtime is required"
        return result
    previous = torch.backends.cudnn.enabled
    try:
        for index in range(torch.cuda.device_count()):
            target = torch.device("cuda", index)
            entry: dict[str, Any] = {"device": str(target), "name": torch.cuda.get_device_name(index)}
            for mode, enabled in (("miopen", True), ("nativeHip", False)):
                torch.backends.cudnn.enabled = enabled
                cases = []
                for name in ("batchNormFp32", "instanceNormFp32", "instanceNormFp16"):
                    try:
                        if name == "batchNormFp32":
                            _probe_batch_norm(torch, target, verify_reference=True)
                        else:
                            _probe_instance_norm(torch, target, amp=name == "instanceNormFp16", verify_reference=True)
                        cases.append({"name": name, "passed": True})
                    except Exception as error:
                        cases.append({"name": name, "passed": False, "error": str(error)})
                entry[mode] = {"passed": all(case["passed"] for case in cases), "cases": cases}
            result["checks"].append(entry)
    finally:
        torch.backends.cudnn.enabled = previous
    return result


if __name__ == "__main__":
    print(json.dumps(diagnose_rocm(), ensure_ascii=False, indent=2))
