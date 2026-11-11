"""Inference parameter resolution metadata and resolved-inference (v4) snapshot checks.

This module never imports Torch or the pymss runtime and never reads model weights:
model files are only ``stat``-ed for the fingerprint. Defaults mirror the pymss SDK
execution path (``MSSeparator.update_inference_params``, ``apply_msst_inference_compat``,
``demix`` / ``demix_track`` / ``demix_track_demucs`` and the built-in VR config).
"""

from __future__ import annotations

import hashlib
import ast
import json
import math
import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from worker_inference_constraints import InferenceParameterError, chunk_size_constraint

RESOLUTION_VERSION = 1
SNAPSHOT_VERSION = 1
SNAPSHOT_PARAMS_VERSION = 4
RESOLVER_VERSION = "1"
ADAPTER_VERSION = "studio-adapter-1"
# pymss decodes MSS input at config.audio.sample_rate, falling back to 44100.
SDK_DEFAULT_SAMPLE_RATE = 44100
VR_SAMPLE_RATE = 44100

ROFORMER_TYPES = frozenset({
    "bs_roformer", "bs_roformer_hyperace", "bs_conformer", "mel_band_roformer", "mel_band_conformer",
})
# Architectures that pymss runs through the generic demix_track chunk/overlap loop.
DEMIX_TRACK_TYPES = ROFORMER_TYPES | {"mdx23c", "scnet", "bandit", "bandit_v2", "apollo"}
LEGACY_DEMUCS_TYPES = frozenset({"demucs", "tasnet", "legacy_demucs", "legacy_tasnet"})

# pymss MSSeparator.load_model() built-in VR inference config (sample_rate fixed at 44100).
VR_BUILTIN_DEFAULTS: dict[str, int | float | bool] = {
    "batch_size": 2,
    "window_size": 512,
    "aggression": 5,
    "enable_post_process": False,
    "post_process_threshold": 0.2,
    "high_end_process": False,
    "normalize": False,
}

BOOL_KEYS = frozenset({"standardize", "normalize", "enable_post_process", "high_end_process"})
INT_KEYS = frozenset({"batch_size", "chunk_size", "overlap_size", "window_size", "aggression"})
FLOAT_KEYS = frozenset({"post_process_threshold"})
MANAGED_KEYS = BOOL_KEYS | INT_KEYS | FLOAT_KEYS
SNAPSHOT_MODES = frozenset({"model", "custom"})
EDITABLE_KINDS = frozenset({"free", "lattice"})


# ---------------------------------------------------------------------------
# Small strict helpers
# ---------------------------------------------------------------------------

def _section(config: Mapping[str, Any] | None, name: str) -> Mapping[str, Any]:
    value = config.get(name) if isinstance(config, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def _int(value: Any) -> int | None:
    """Integral numbers only (YAML ``882000.0`` is accepted, bools and fractions are not)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _same(left: Any, right: Any) -> bool:
    """Exact JSON-value equality: ``True`` never equals ``1`` and ``0`` never equals ``False``."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return type(left) is type(right) and left == right


def _cap(kind: str, reason: str | None = None, **bounds: int | float | None) -> dict[str, Any]:
    capability: dict[str, Any] = {"kind": kind}
    for key in ("min", "max", "step", "offset"):
        if bounds.get(key) is not None:
            capability[key] = bounds[key]
    if reason:
        capability["reason"] = reason
    return capability


def _cover_default(capability: dict[str, Any], default: Any) -> dict[str, Any]:
    """Widen editable numeric bounds so a model's own configured value is never reported invalid."""
    value = _number(default)
    if value is None or capability.get("kind") not in EDITABLE_KINDS or capability.get("kind") == "lattice":
        return capability
    if "min" in capability and value < capability["min"]:
        capability["min"] = default
    if "max" in capability and value > capability["max"]:
        capability["max"] = default
    return capability


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _sdk_versions() -> tuple[str | None, str | None]:
    from importlib.metadata import PackageNotFoundError, version

    def lookup(name: str) -> str | None:
        try:
            return version(name)
        except (PackageNotFoundError, ValueError):
            return None
        except Exception:
            return None

    return lookup("pymss"), lookup("pymss-core")


def _config_digest(config_path: Path | str | None) -> str | None:
    if not config_path:
        return None
    try:
        return hashlib.sha256(Path(config_path).read_bytes()).hexdigest()
    except OSError:
        return None


def _model_stat(model_path: Path | str | None) -> dict[str, int] | None:
    """Size + mtime only: a change detector, never a content hash (weights are not read)."""
    if not model_path:
        return None
    try:
        stat = os.stat(model_path)
    except OSError:
        return None
    if not os.path.isfile(model_path):
        return None
    return {"size": int(stat.st_size), "mtimeNs": int(stat.st_mtime_ns)}


def compute_inference_fingerprint(
    model_type: str | None,
    config_path: Path | str | None,
    model_path: Path | str | None,
    *,
    ignore_model_stat: bool = False,
) -> str:
    """Shared by model metadata and task execution so both sides hash the same inputs."""
    pymss_version, core_version = _sdk_versions()
    material = {
        "resolver": RESOLVER_VERSION,
        "adapter": ADAPTER_VERSION,
        "pymss": pymss_version,
        "pymssCore": core_version,
        "modelType": str(model_type or "").strip().lower(),
        "config": _config_digest(config_path),
        "model": None if ignore_model_stat else _model_stat(model_path),
        "adapterSources": _adapter_source_digest(),
    }
    if material["modelType"] == "vr":
        material["vrMetadata"] = _vr_model_metadata(model_path)
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"r{RESOLVER_VERSION}:{digest}"


def _adapter_source_digest() -> str:
    """Small worker sources are part of the execution contract, independent of release tags."""
    directory = Path(__file__).parent
    signatures = []
    for name in ("worker_inference_snapshot.py", "worker_inference_constraints.py", "worker_models.py", "worker_infer.py"):
        path = directory / name
        try:
            stat = path.stat()
            signatures.append((str(path), stat.st_size, stat.st_mtime_ns))
        except OSError:
            signatures.append((str(path), -1, -1))
    return _digest_adapter_sources(tuple(signatures))


@lru_cache(maxsize=8)
def _digest_adapter_sources(signatures: tuple[tuple[str, int, int], ...]) -> str:
    digest = hashlib.sha256()
    for name, _, _ in signatures:
        try:
            digest.update(Path(name).read_bytes())
        except OSError:
            digest.update(b"missing")
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Resolution metadata
# ---------------------------------------------------------------------------

def _mss_sample_rate(audio: Mapping[str, Any]) -> tuple[int | None, str]:
    rate = _int(audio.get("sample_rate"))
    if rate is not None and rate > 0:
        return rate, "config:audio.sample_rate"
    if "sample_rate" in audio:
        return None, "invalid-config:audio.sample_rate"
    return SDK_DEFAULT_SAMPLE_RATE, "sdk-default"


def _fft_minimum_chunk(model_type: str, config: Mapping[str, Any]) -> int:
    """Smallest chunk the centered (reflect-padded) STFT accepts: n_fft // 2 + 1."""
    model, audio, kwargs = _section(config, "model"), _section(config, "audio"), _section(config, "kwargs")
    candidates = {
        "scnet": [model.get("nfft")],
        "bandit": [model.get("n_fft")],
        "bandit_v2": [kwargs.get("n_fft")],
        "mdx23c": [audio.get("n_fft")],
    }.get(model_type, [model.get("stft_n_fft"), audio.get("n_fft")] if model_type in ROFORMER_TYPES else [])
    for candidate in candidates:
        n_fft = _int(candidate)
        if n_fft is not None and n_fft > 1:
            return n_fft // 2 + 1
    return 1


def _effective_chunk(inference: Mapping[str, Any], audio: Mapping[str, Any]) -> int | None:
    # apply_msst_inference_compat: inference.chunk_size overrides audio.chunk_size.
    raw = inference.get("chunk_size")
    if raw is None:
        raw = audio.get("chunk_size")
    chunk = _int(raw)
    return chunk if chunk is not None and chunk > 0 else None


def _default_overlap(
    inference: Mapping[str, Any], chunk: int,
) -> tuple[int | None, dict[str, Any], str | None]:
    """Default overlap O: explicit overlap_size, else MSST num_overlap N, else SDK chunk // 2."""
    raw_overlap = inference.get("overlap_size")
    if raw_overlap is not None:
        overlap = _int(raw_overlap)
        if overlap is None or overlap < 0 or overlap >= chunk:
            return None, {"kind": "unknown"}, f"Configured overlap_size {raw_overlap!r} is not in [0, chunk_size)"
        return overlap, {"kind": "samples"}, None
    raw_count = inference.get("num_overlap")
    if raw_count is not None:
        count = _int(raw_count)
        if count is None or count < 1:
            return None, {"kind": "unknown"}, f"Configured num_overlap {raw_count!r} is invalid"
        return chunk - chunk // count, {"kind": "count", "count": count}, None
    return chunk // 2, {"kind": "samples"}, None


def _mss_common(inference: Mapping[str, Any], known_type: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    defaults: dict[str, Any] = {}
    capabilities: dict[str, Any] = {}
    batch = _int(inference.get("batch_size"))
    if batch is not None and batch >= 1:
        defaults["batch_size"] = batch
        capabilities["batch_size"] = _cap("free", min=1, step=1) if known_type else _cap(
            "unknown", "Batch size support is not verified for this architecture")
    else:
        capabilities["batch_size"] = _cap("unknown", "The model config does not define inference.batch_size")
    # The YAML key inference.normalize is the legacy *input standardization* switch;
    # pymss sets it to False when absent. Output normalize is a separate flag, default False.
    standardize = inference.get("normalize", False)
    if isinstance(standardize, bool):
        defaults["standardize"] = standardize
        capabilities["standardize"] = _cap("free")
    else:
        capabilities["standardize"] = _cap("unknown", "Configured inference.normalize is not a boolean")
    defaults["normalize"] = False
    capabilities["normalize"] = _cap("free")
    return defaults, capabilities


def _vr_model_metadata(model_path: Path | str | None) -> Mapping[str, Any] | None:
    """Read the SDK's literal VR catalog without importing its Torch-dependent package."""
    if not model_path:
        return None
    try:
        from importlib.metadata import distribution
        path = Path(distribution("pymss").locate_file("pymss/modules/vocal_remover/vr_models.py"))
        stat = path.stat()
        table = _vr_model_table(str(path), stat.st_size, stat.st_mtime_ns)
        return table.get(Path(model_path).name, {})
    except (OSError, ValueError, TypeError, SyntaxError, ImportError):
        return None
    except Exception:
        return None


@lru_cache(maxsize=4)
def _vr_model_table(path: str, _size: int, _mtime: int) -> dict[str, Any]:
    for node in ast.parse(Path(path).read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "VR_MODEL_METADATA" for target in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, dict):
                return value
    raise ValueError("SDK VR metadata table is unavailable")


def _vr_window_capability(model_path: Path | str | None) -> dict[str, Any]:
    metadata = _vr_model_metadata(model_path)
    if metadata is None:
        return _cap("unknown", "The SDK VR architecture metadata is unavailable")
    if "nout" in metadata and "nout_lstm" in metadata:
        return _cap("lattice", min=144, step=16, offset=0)
    stat = _model_stat(model_path)
    if stat is None:
        return _cap("unknown", "VR window rules require the installed model architecture")
    # Matches VRSeparator.load_model's size-based architecture selection, without reading weights.
    sizes = (31191, 33966, 56817, 123821, 123812, 129605, 218409, 537238, 537227)
    arch = min(sizes, key=lambda size: abs(size - math.ceil(stat["size"] / 1024)))
    if arch in (56817, 218409):
        return _cap("lattice", min=144, step=16, offset=0)
    if arch == 129605:
        return _cap("lattice", min=288, step=32, offset=0)
    return _cap("lattice", min=272, step=16, offset=0)


def _vr_resolution(config: Mapping[str, Any] | None, fingerprint: str, model_path: Path | str | None) -> dict[str, Any]:
    inference = _section(config, "inference")
    defaults: dict[str, Any] = dict(VR_BUILTIN_DEFAULTS)
    # Same precedence as the legacy defaultInferenceParams: config values over built-ins.
    for key, builtin in VR_BUILTIN_DEFAULTS.items():
        value = inference.get(key)
        if isinstance(builtin, bool):
            if isinstance(value, bool):
                defaults[key] = value
        elif key in FLOAT_KEYS:
            number = _number(value)
            if number is not None:
                defaults[key] = number
        else:
            number = _int(value)
            if number is not None:
                defaults[key] = number
    capabilities = {
        "batch_size": _cap("free", min=1, step=1),
        "window_size": _vr_window_capability(model_path),
        "aggression": _cap("free", min=0, max=100, step=1),
        "enable_post_process": _cap("free"),
        "post_process_threshold": _cap("free", min=0, max=1),
        "high_end_process": _cap("free"),
        "normalize": _cap("free"),
        "chunk_size": _cap("unsupported", "VR models process whole spectrogram windows"),
        "overlap_size": _cap("unsupported", "VR models process whole spectrogram windows"),
        "standardize": _cap("unsupported", "VR inference does not apply input standardization"),
    }
    for key in ("batch_size", "window_size", "aggression", "post_process_threshold"):
        _cover_default(capabilities[key], defaults[key])
    return {
        "version": RESOLUTION_VERSION,
        "sampleRate": VR_SAMPLE_RATE,
        "sampleRateSource": "sdk:vr",
        "defaults": defaults,
        "defaultSources": {key: "model-config" if key in inference else "runtime-default" for key in defaults},
        "capabilities": capabilities,
        "overlap": {"kind": "unknown"},
        "fingerprint": fingerprint,
    }


def _legacy_demucs_resolution(config: Mapping[str, Any] | None, fingerprint: str) -> dict[str, Any]:
    inference, audio = _section(config, "inference"), _section(config, "audio")
    reason = "Legacy Demucs/TasNet uses its own split/overlap ratio instead of chunk samples"
    defaults: dict[str, Any] = {"normalize": False}
    capabilities: dict[str, Any] = {
        "chunk_size": _cap("unsupported", reason),
        "overlap_size": _cap("unsupported", reason),
        "batch_size": _cap("unsupported", reason),
        "normalize": _cap("free"),
    }
    standardize = inference.get("normalize")
    if isinstance(standardize, bool):
        defaults["standardize"] = standardize
        capabilities["standardize"] = _cap("free")
    else:
        capabilities["standardize"] = _cap("unknown", "Defined by the model file, not readable without loading it")
    resolution: dict[str, Any] = {
        "version": RESOLUTION_VERSION,
        "defaults": defaults,
        "defaultSources": {key: "runtime-default" if key == "normalize" else "model-config" for key in defaults},
        "capabilities": capabilities,
        "overlap": {"kind": "unknown"},
        "fingerprint": fingerprint,
    }
    rate = _int(audio.get("sample_rate"))
    if rate is not None and rate > 0:
        resolution["sampleRate"] = rate
        resolution["sampleRateSource"] = "config:audio.sample_rate"
    return resolution


def build_parameter_resolution(
    model_type: str | None,
    config: Mapping[str, Any] | None,
    *,
    config_path: Path | str | None,
    model_path: Path | str | None,
) -> dict[str, Any] | None:
    """Return ``inferenceParamMeta.resolution`` or ``None`` when the model cannot be described."""
    normalized = str(model_type or "").strip().lower()
    fingerprint = compute_inference_fingerprint(normalized, config_path, model_path)
    if normalized == "vr":
        return _vr_resolution(config, fingerprint, model_path)
    if normalized in LEGACY_DEMUCS_TYPES:
        return _legacy_demucs_resolution(config, fingerprint)
    if not isinstance(config, Mapping):
        return None

    inference, audio, training = _section(config, "inference"), _section(config, "audio"), _section(config, "training")
    known_type = normalized in DEMIX_TRACK_TYPES or normalized == "htdemucs"
    defaults, capabilities = _mss_common(inference, known_type)
    sample_rate, sample_rate_source = _mss_sample_rate(audio)
    overlap_meta: dict[str, Any] = {"kind": "unknown"}
    audio_chunk = _effective_chunk(inference, audio)

    if normalized == "htdemucs":
        # demix_track_demucs uses training.samplerate * training.segment and ignores audio.chunk_size,
        # while the MLX full path uses audio.chunk_size; only a consistent pair is safe to describe.
        train_rate = _int(training.get("samplerate"))
        segment = _number(training.get("segment"))
        native = train_rate * segment if train_rate and segment and train_rate > 0 and segment > 0 else None
        chunk = int(native) if native is not None and float(native).is_integer() else None
        fixed_reason = "HTDemucs chunk length is training.samplerate × training.segment"
        if chunk is not None and audio_chunk == chunk:
            defaults["chunk_size"] = chunk
            capabilities["chunk_size"] = _cap("fixed", fixed_reason)
            overlap, overlap_meta, problem = _default_overlap(inference, chunk)
            if overlap is None:
                capabilities["overlap_size"] = _cap("unknown", problem)
            else:
                defaults["overlap_size"] = overlap
                capabilities["overlap_size"] = _cap("free", min=0, step=1)
        else:
            capabilities["chunk_size"] = _cap(
                "fixed" if chunk is not None else "unknown",
                "audio.chunk_size does not match training.samplerate × training.segment; "
                "runtime paths would use different chunk lengths",
            )
            capabilities["overlap_size"] = _cap("unknown", "Overlap basis differs between HTDemucs runtime paths")
    elif audio_chunk is None:
        capabilities["chunk_size"] = _cap("unknown", "The model config does not define a chunk size")
        capabilities["overlap_size"] = _cap("unknown", "Overlap requires a known chunk size")
    else:
        defaults["chunk_size"] = audio_chunk
        overlap, overlap_meta, problem = _default_overlap(inference, audio_chunk)
        if overlap is not None:
            defaults["overlap_size"] = overlap
        if not known_type:
            capabilities["chunk_size"] = _cap("unknown", f"Chunk rules for {normalized or 'this model'} are not verified")
            capabilities["overlap_size"] = _cap("unknown", problem or "Overlap rules are not verified")
        else:
            constraint = chunk_size_constraint(normalized, config) if normalized == "mdx23c" else None
            if normalized == "mdx23c" and not constraint:
                capabilities["chunk_size"] = _cap("unknown", "MDX23C chunk lattice could not be derived from the config")
            elif constraint:
                capabilities["chunk_size"] = _cap(
                    "lattice", min=constraint["min"], step=constraint["step"], offset=constraint["offset"],
                )
            else:
                minimum = min(_fft_minimum_chunk(normalized, config), audio_chunk)
                capabilities["chunk_size"] = _cap("free", min=minimum, step=1)
            capabilities["overlap_size"] = (
                _cap("free", min=0, step=1) if overlap is not None else _cap("unknown", problem)
            )

    return {
        "version": RESOLUTION_VERSION,
        "sampleRate": sample_rate,
        "sampleRateSource": sample_rate_source,
        "defaults": defaults,
        "defaultSources": {key: "runtime-default" if key == "normalize" or key == "standardize" and "normalize" not in inference else "model-config" for key in defaults},
        "capabilities": capabilities,
        "overlap": overlap_meta,
        "fingerprint": fingerprint,
    }


# ---------------------------------------------------------------------------
# v4 payload validation
# ---------------------------------------------------------------------------

def parse_inference_params_version(value: Any) -> int | None:
    """Same parsing as the legacy normalizer; versions newer than v4 are refused outright."""
    try:
        version = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    if version is not None and version > SNAPSHOT_PARAMS_VERSION:
        raise InferenceParameterError(
            f"inferenceParamsVersion {version} is newer than this worker supports "
            f"(maximum {SNAPSHOT_PARAMS_VERSION}). Update the application before running this task."
        )
    return version


def _check_value_type(key: str, value: Any) -> None:
    if key in BOOL_KEYS:
        ok = isinstance(value, bool)
    elif key in INT_KEYS:
        ok = _is_int(value)
    else:
        ok = _number(value) is not None
    if not ok:
        raise InferenceParameterError(f"Resolved inference parameter {key}={value!r} has an invalid type")


def validate_snapshot_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Structural v4 checks that need no model files (run before any download or runtime import)."""
    snapshot = payload.get("resolvedInference")
    if snapshot is None:
        raise InferenceParameterError("inferenceParamsVersion 4 requires a resolvedInference snapshot")
    if not isinstance(snapshot, Mapping):
        raise InferenceParameterError("resolvedInference must be an object")
    version = snapshot.get("version")
    if not _is_int(version) or version != SNAPSHOT_VERSION:
        raise InferenceParameterError(
            f"Unsupported resolvedInference version {version!r}; this worker supports version {SNAPSHOT_VERSION}"
        )
    fingerprint = snapshot.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise InferenceParameterError("resolvedInference.fingerprint is missing")
    if not isinstance(snapshot.get("mode"), str) or snapshot.get("mode") not in SNAPSHOT_MODES:
        raise InferenceParameterError(f"resolvedInference.mode {snapshot.get('mode')!r} is invalid")
    for field in ("intent", "sources", "adjustments"):
        if not isinstance(snapshot.get(field), Mapping):
            raise InferenceParameterError(f"resolvedInference.{field} must be an object")

    parameters = snapshot.get("parameters")
    payload_params = payload.get("inferenceParams")
    if not isinstance(parameters, Mapping):
        raise InferenceParameterError("resolvedInference.parameters must be an object")
    if not isinstance(payload_params, Mapping):
        raise InferenceParameterError("inferenceParams must be an object for inferenceParamsVersion 4")
    if not all(isinstance(key, str) for key in parameters) or not all(isinstance(key, str) for key in payload_params):
        raise InferenceParameterError("Inference parameter keys must be strings")
    mismatched = sorted(
        key for key in set(parameters) | set(payload_params)
        if key not in parameters or key not in payload_params or not _same(parameters[key], payload_params[key])
    )
    if mismatched:
        raise InferenceParameterError(
            "inferenceParams do not exactly match resolvedInference.parameters "
            f"(differing keys: {', '.join(mismatched)})"
        )
    for key, value in parameters.items():
        if key not in MANAGED_KEYS:
            raise InferenceParameterError(f"{key!r} is not a managed inference parameter for resolved snapshots")
        _check_value_type(key, value)

    use_tta = snapshot.get("useTta")
    if not isinstance(use_tta, bool):
        raise InferenceParameterError("resolvedInference.useTta must be a boolean")
    payload_tta = payload.get("useTta", False)
    if not isinstance(payload_tta, bool) or payload_tta is not use_tta:
        raise InferenceParameterError(
            f"useTta={payload_tta!r} does not match resolvedInference.useTta={use_tta!r}"
        )
    return dict(snapshot)


def validate_snapshot_against_resolution(
    snapshot: Mapping[str, Any],
    resolution: Mapping[str, Any] | None,
    *,
    accepted_fingerprints: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Check the frozen parameters against the model as it exists now. Never adjusts values."""
    if not resolution:
        raise InferenceParameterError(
            "Inference parameter metadata is unavailable for this model; refresh the model and resubmit"
        )
    fingerprint = snapshot.get("fingerprint")
    if fingerprint != resolution.get("fingerprint") and fingerprint not in accepted_fingerprints:
        raise InferenceParameterError(
            "The model configuration, weights or runtime changed after this task was submitted "
            "(inference fingerprint mismatch). Refresh model metadata and submit a new task."
        )
    defaults: Mapping[str, Any] = resolution.get("defaults") or {}
    capabilities: Mapping[str, Any] = resolution.get("capabilities") or {}
    parameters: dict[str, Any] = dict(snapshot.get("parameters") or {})
    missing = sorted(set(defaults) - set(parameters))
    if missing:
        raise InferenceParameterError(f"Resolved inference snapshot is incomplete (missing: {', '.join(missing)})")

    for key, value in parameters.items():
        if key not in defaults:
            raise InferenceParameterError(f"{key} is not a managed parameter for this model")
        _check_value_type(key, value)
        capability = capabilities.get(key) or {"kind": "unknown"}
        kind = capability.get("kind")
        default = defaults[key]
        # Unknown editability disables new seconds/ratio controls, but must not erase a
        # previously saved native model-library override. The SDK still validates its shape.
        inherited_native = kind == "unknown" and (snapshot.get("sources") or {}).get(key) == "model-library"
        if kind not in EDITABLE_KINDS:
            if not inherited_native and not _same(value, default):
                reason = capability.get("reason")
                raise InferenceParameterError(
                    f"{key} is {kind} for this model and must remain {default!r}, got {value!r}"
                    + (f" ({reason})" if reason else "")
                )
            if not inherited_native:
                continue
        # A model's own configured value is not second-guessed against heuristic bounds,
        # but a lattice is a hard runtime rule and is always enforced.
        if isinstance(value, bool) or (_same(value, default) and kind != "lattice"):
            continue
        number = float(value)
        if "min" in capability and number < capability["min"]:
            raise InferenceParameterError(f"{key}={value!r} is below the minimum {capability['min']!r}")
        if "max" in capability and number > capability["max"]:
            raise InferenceParameterError(f"{key}={value!r} is above the maximum {capability['max']!r}")
        step = capability.get("step")
        if _is_int(value) and _is_int(step) and step > 1 and (value - int(capability.get("offset", 0))) % step:
            offset = int(capability.get("offset", 0))
            raise InferenceParameterError(
                f"{key}={value!r} is not a legal value; it must equal {offset} + k × {step}"
            )

    chunk = parameters.get("chunk_size", defaults.get("chunk_size"))
    for key in ("batch_size", "window_size"):
        if key in parameters and parameters[key] < 1:
            raise InferenceParameterError(f"{key} must be positive")
    if "aggression" in parameters and not 0 <= parameters["aggression"] <= 100:
        raise InferenceParameterError("aggression must be between 0 and 100")
    if "post_process_threshold" in parameters and not 0 <= parameters["post_process_threshold"] <= 1:
        raise InferenceParameterError("post_process_threshold must be between 0 and 1")
    if "chunk_size" in parameters and (not _is_int(chunk) or chunk < 1):
        raise InferenceParameterError(f"chunk_size must be a positive integer, got {chunk!r}")
    overlap = parameters.get("overlap_size")
    if overlap is not None:
        if not _is_int(chunk):
            raise InferenceParameterError("overlap_size requires a known chunk_size")
        if overlap < 0 or overlap >= chunk:
            raise InferenceParameterError(
                f"overlap_size must satisfy 0 <= overlap_size < chunk_size ({chunk}), got {overlap!r}"
            )
    return parameters
