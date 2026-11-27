"""Cross-platform inference acceleration and Windows safety guards for Studio workers."""

from __future__ import annotations

import os
import sys
from typing import Any

_ROFORMER_MODEL_TYPES = frozenset({
    "bs_roformer",
    "mel_band_roformer",
    "bs_conformer",
    "mel_band_conformer",
})

_AUTO_BATCH_MODEL_TYPES = _ROFORMER_MODEL_TYPES | frozenset({
    "mdx23c",
    "scnet",
})

_GIB = 1024 ** 3


def _env_flag(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _resolve_device_info(device: Any, torch_mod: Any) -> tuple[str, int]:
    try:
        parsed = torch_mod.device(device)
        return str(parsed.type).lower(), int(parsed.index or 0)
    except Exception:
        text = str(device or "cpu").strip().lower()
        parts = text.split(":", 1)
        device_type = parts[0] or "cpu"
        try:
            index = int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            index = 0
        return device_type, index


def _is_hip_build(torch_mod: Any) -> bool:
    return bool(getattr(getattr(torch_mod, "version", None), "hip", None))


def _device_total_vram_bytes(device_type: str, device_index: int, torch_mod: Any) -> int | None:
    try:
        if device_type == "xpu":
            props = torch_mod.xpu.get_device_properties(device_index)
            return int(getattr(props, "total_memory", 0) or 0) or None
        if device_type == "cuda" and not _is_hip_build(torch_mod):
            props = torch_mod.cuda.get_device_properties(device_index)
            return int(getattr(props, "total_memory", 0) or 0) or None
    except Exception:
        return None
    return None


def _configure_cuda_tf32(device_type: str, torch_mod: Any) -> bool:
    if device_type != "cuda" or _is_hip_build(torch_mod):
        return False
    if _env_flag("PYMSS_STUDIO_TF32") is False:
        return False
    applied = False
    try:
        cuda_backend = getattr(getattr(torch_mod, "backends", None), "cuda", None)
        matmul = getattr(cuda_backend, "matmul", None)
        if matmul is not None and hasattr(matmul, "allow_tf32"):
            matmul.allow_tf32 = True
            applied = True
        cudnn_backend = getattr(getattr(torch_mod, "backends", None), "cudnn", None)
        if cudnn_backend is not None and hasattr(cudnn_backend, "allow_tf32"):
            cudnn_backend.allow_tf32 = True
            applied = True
    except Exception:
        return False
    return applied


def _set_inference_option(config: Any, key: str, value: Any) -> bool:
    inference = getattr(config, "inference", None)
    if inference is None:
        return False
    try:
        inference[key] = value
        return True
    except Exception:
        try:
            setattr(inference, key, value)
            return True
        except Exception:
            return False


def _get_inference_option(config: Any, key: str, default: Any = None) -> Any:
    inference = getattr(config, "inference", None)
    if inference is None:
        return default
    if hasattr(inference, "get"):
        return inference.get(key, default)
    return getattr(inference, key, default)


def _install_windows_sdpa_contiguous_guard(
    model: Any,
    *,
    device_type: str,
    torch_mod: Any,
    platform: str,
) -> int:
    """Ensure Q/K/V tensors are contiguous before SDPA on Windows XPU/ROCm.

    Non-contiguous transpose views passed into fused SDPA kernels are a known
    trigger for native 0xC0000005 access violations on Windows GPU drivers.
    """
    if platform != "win32" or _env_flag("PYMSS_STUDIO_WIN_SDPA_GUARD") is False:
        return 0
    if not (device_type == "xpu" or (device_type == "cuda" and _is_hip_build(torch_mod))):
        return 0
    if model is None or not hasattr(model, "modules"):
        return 0
    wrapped = 0
    for module in model.modules():
        if getattr(module, "_studio_contiguous_sdpa_guarded", False):
            continue
        attn_fn = getattr(module, "_attention", None)
        if not callable(attn_fn) or not hasattr(module, "flash"):
            continue

        def _make_guarded(original):
            def _guarded_attention(q, k, v):
                if hasattr(q, "is_contiguous") and not q.is_contiguous():
                    q = q.contiguous()
                if hasattr(k, "is_contiguous") and not k.is_contiguous():
                    k = k.contiguous()
                if hasattr(v, "is_contiguous") and not v.is_contiguous():
                    v = v.contiguous()
                return original(q, k, v)
            return _guarded_attention

        module._attention = _make_guarded(attn_fn)
        module._studio_contiguous_sdpa_guarded = True
        wrapped += 1
    return wrapped


def _choose_auto_batch_size(total_vram_bytes: int | None, *, platform: str) -> int:
    if not total_vram_bytes:
        return 1
    if platform == "win32":
        # Stay conservative on Windows to avoid WDDM 2s TDR resets and shared-memory paging.
        return 2 if total_vram_bytes >= int(7.0 * _GIB) else 1
    if total_vram_bytes >= int(10.0 * _GIB):
        return 4
    if total_vram_bytes >= int(6.0 * _GIB):
        return 2
    return 1


def _maybe_tune_batch_size(
    *,
    model_type: str,
    config: Any,
    explicit_params: dict[str, Any],
    device_type: str,
    device_index: int,
    low_precision_active: bool,
    torch_mod: Any,
    platform: str,
) -> int | None:
    if _env_flag("PYMSS_STUDIO_AUTO_BATCH") is False or not low_precision_active:
        return None
    if model_type not in _AUTO_BATCH_MODEL_TYPES:
        return None
    if explicit_params.get("batch_size") is not None:
        return None
    current = _get_inference_option(config, "batch_size", 1)
    try:
        current_int = int(current)
    except (TypeError, ValueError):
        return None
    if current_int != 1:
        return None
    if device_type not in {"xpu", "cuda"} or _is_hip_build(torch_mod):
        return None
    total_vram = _device_total_vram_bytes(device_type, device_index, torch_mod)
    target = _choose_auto_batch_size(total_vram, platform=platform)
    if target <= current_int:
        return None
    if _set_inference_option(config, "batch_size", target):
        return target
    return None


def optimize_separator_inference(
    separator: Any,
    *,
    explicit_params: dict[str, Any] | None = None,
    amp_enabled: bool = True,
    logger: Any = None,
    torch_mod: Any = None,
    platform: str | None = None,
) -> dict[str, Any]:
    """Apply safe, platform-aware inference optimizations to a loaded MSSeparator."""
    result: dict[str, Any] = {
        "tf32": False,
        "autoBatchSize": None,
        "winSdpaGuardCount": 0,
    }
    if torch_mod is None:
        torch_mod = sys.modules.get("torch")
        if torch_mod is None:
            try:
                import torch as torch_mod  # type: ignore
            except Exception:
                return result
    active_platform = platform or sys.platform
    model = getattr(separator, "model", None)
    config = getattr(separator, "config", None)
    model_type = str(getattr(separator, "model_type", "") or "").strip().lower()
    device_type, device_index = _resolve_device_info(getattr(separator, "device", "cpu"), torch_mod)

    result["tf32"] = _configure_cuda_tf32(device_type, torch_mod)
    result["winSdpaGuardCount"] = _install_windows_sdpa_contiguous_guard(
        model,
        device_type=device_type,
        torch_mod=torch_mod,
        platform=active_platform,
    )
    result["autoBatchSize"] = _maybe_tune_batch_size(
        model_type=model_type,
        config=config,
        explicit_params=explicit_params or {},
        device_type=device_type,
        device_index=device_index,
        low_precision_active=amp_enabled,
        torch_mod=torch_mod,
        platform=active_platform,
    )

    if logger is not None and any((
        result["tf32"],
        result["autoBatchSize"],
        result["winSdpaGuardCount"],
    )):
        applied = []
        if result["autoBatchSize"]:
            applied.append(f"auto_batch={result['autoBatchSize']}")
        if result["tf32"]:
            applied.append("cuda_tf32")
        if result["winSdpaGuardCount"]:
            applied.append(f"win_sdpa_contiguous_guard={result['winSdpaGuardCount']}")
        try:
            logger.info("Studio inference optimizations (%s): %s", device_type, ", ".join(applied))
        except Exception:
            pass
    return result
