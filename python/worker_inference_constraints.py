"""Model-specific inference constraints without importing the inference runtime."""

from collections.abc import Mapping
from typing import Any


class InferenceParameterError(ValueError):
    pass


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
        return number if float(value) == number else None
    except (TypeError, ValueError, OverflowError):
        return None


def chunk_size_constraint(model_type: str | None, config: Mapping[str, Any]) -> dict[str, int] | None:
    if str(model_type or "").strip().lower() != "mdx23c":
        return None
    audio = config.get("audio") or {}
    model = config.get("model") or {}
    if not isinstance(audio, Mapping) or not isinstance(model, Mapping):
        return None
    hop = _integer(audio.get("hop_length"))
    scales = _integer(model.get("num_scales"))
    scale = model.get("scale")
    stride = _integer(scale[0]) if isinstance(scale, (list, tuple)) and len(scale) == 2 else None
    n_fft = _integer(audio.get("n_fft"))
    if hop is None or hop < 1 or scales is None or scales < 0 or stride is None or stride < 1:
        return None
    step = hop * stride ** scales
    offset = step - hop
    minimum = max(1, (n_fft or 0) // 2 + 1)
    minimum = offset + max(0, (minimum - offset + step - 1) // step) * step
    return {"step": step, "offset": offset, "min": minimum}


def validate_chunk_size(model_type: str | None, config: Mapping[str, Any], chunk_size: Any) -> None:
    constraint = chunk_size_constraint(model_type, config)
    if not constraint:
        return
    chunk = _integer(chunk_size)
    step, offset, minimum = constraint["step"], constraint["offset"], constraint["min"]
    if chunk is not None and chunk >= minimum and (chunk - offset) % step == 0:
        return
    candidate = chunk if chunk is not None else minimum
    lower = max(minimum, offset + ((candidate - offset) // step) * step)
    upper = max(minimum, offset + ((candidate - offset + step - 1) // step) * step)
    choices = " or ".join(str(value) for value in dict.fromkeys([lower, upper]))
    raise InferenceParameterError(
        f"Invalid MDX23C chunk_size {chunk_size!r}. Use {choices} samples; "
        f"the chunk size must be at least {minimum} and satisfy (chunk_size + {step - offset}) % {step} == 0."
    )
