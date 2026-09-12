from __future__ import annotations

import os
import re
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

from worker_audio import _apply_stereo_pan, _equal_power_fade, _read_audio, _resample_audio
from worker_models import (
    _derive_overlap_size_from_num_overlap,
    auxiliary_paths_for,
    config_path_for,
    get_any_model_entry,
    model_path_for,
)
from worker_protocol import _as_bool, _as_float, _as_int, emit, emit_error


class ModelNotFoundError(RuntimeError):
    pass


class ModelDownloadError(RuntimeError):
    pass


class JsonLogHandler:
    def __init__(self, task_id: str):
        import logging
        self.task_id = task_id
        self.level = logging.INFO

    def setLevel(self, level: int) -> None:
        self.level = level

    def handle(self, record: Any) -> bool:
        if record.levelno < self.level:
            return False
        payload = {"level": record.levelname.lower(), "message": record.getMessage()}
        if record.exc_info:
            import logging
            payload["detail"] = logging.Formatter().formatException(record.exc_info)
        elif getattr(record, "stack_info", None):
            payload["detail"] = record.stack_info
        emit("task_log", payload, task_id=self.task_id)
        return True


def _normalize_output_naming(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"enabled": False, "template": "", "stem_order": []}
    template = str(value.get("template") or "").strip()
    stem_order = value.get("stemOrder")
    if not isinstance(stem_order, list):
        stem_order = []
    return {
        "enabled": bool(value.get("enabled")) and bool(template),
        "template": template,
        "stem_order": [str(item or "").strip() for item in stem_order if str(item or "").strip()],
    }


WINDOWS_RESERVED_FILENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
MAX_FILENAME_PART_BYTES = 200


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip(" ._")


def _safe_filename_part(value: str) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" ._")
    text = re.sub(r"\s*_\s*", "_", text).strip(" ._")
    text = _truncate_utf8(text, MAX_FILENAME_PART_BYTES) or "output"
    reserved_key = text.split(".", 1)[0].upper()
    if reserved_key in WINDOWS_RESERVED_FILENAMES or text in {".", ".."}:
        text = f"{text}_"
    return text


def _stem_rank(stem: str, order: list[str]) -> int:
    key = stem.strip().lower()
    for index, item in enumerate(order):
        if item.strip().lower() == key:
            return index
    return len(order) + 1000


def _replace_output_tokens(
    template: str,
    *,
    input_path: str,
    stem: str,
    stem_index: int,
    input_index: int,
    model: str,
    now: datetime,
) -> str:
    values = {
        "%index%": f"{stem_index + 1:02d}",
        "%input_number%": f"{max(1, input_index):02d}",
        "%filename%": Path(input_path).stem,
        "%stem%": stem,
        "%model%": model,
        "%yyyyMMdd%": now.strftime("%Y%m%d"),
        "%hhmmss%": now.strftime("%H%M%S"),
        "%ddmmss%": now.strftime("%d%M%S"),
    }
    name = template
    for token, value in values.items():
        name = name.replace(token, value)
    return _safe_filename_part(name)


def _claim_output_path(path: Path, reserved: set[Path] | None = None) -> Path:
    """Reserve an unused output path so concurrent workers cannot overwrite it."""
    reserved = reserved or set()
    path.parent.mkdir(parents=True, exist_ok=True)
    for index in range(1, 1000):
        candidate = path if index == 1 else path.with_name(f"{path.stem}_{index}{path.suffix}")
        if candidate in reserved:
            continue
        try:
            candidate.touch(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    fallback = path.with_name(f"{path.stem}_{int(datetime.now().timestamp_ns())}{path.suffix}")
    fallback.touch(exist_ok=False)
    return fallback


def _studio_separator_type() -> type[Any]:
    """Build the pymss adapter lazily so non-inference worker commands do not import Torch."""
    from pymss import MSSeparator  # type: ignore

    class StudioMSSeparator(MSSeparator):
        def __init__(self, *args: Any, output_naming: Any = None, output_model: str = "", **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._studio_naming = _normalize_output_naming(output_naming)
            self._studio_output_model = output_model
            self._studio_output_lock = Lock()
            self._studio_claimed_paths: set[Path] = set()
            self._studio_last_outputs: list[dict[str, str]] = []
            self._studio_input_path = ""
            self._studio_input_index = 1
            self._studio_now = datetime.now()
            self._studio_input_contexts: dict[str, deque[dict[str, Any]]] = {}
            self._studio_active_context: dict[str, Any] | None = None
            self._studio_active_output_count = 0
            self._studio_expected_output_count = self._calculate_expected_output_count()
            self._studio_output_sources: dict[Path, str] = {}
            stems = self._stems_to_save() or list(self.config.training.instruments)
            ordered_stems = [stem for _, stem in sorted(
                enumerate(stems),
                key=lambda pair: (_stem_rank(str(pair[1]), self._studio_naming["stem_order"]), pair[0]),
            )]
            self._studio_stem_indices = {
                str(stem).strip().lower(): index
                for index, stem in enumerate(ordered_stems)
            }

        def _calculate_expected_output_count(self) -> int:
            try:
                instruments = list(self.config.training.instruments)
                batches = self._stem_batches_to_save()
                count = sum(len(instruments if batch is None else batch) for batch in batches)
                return max(1, count)
            except Exception:
                return 1

        def _input_paths_for_context(self, input_folder: str, input_index: int) -> list[tuple[Path, int]]:
            source = Path(input_folder)
            if source.is_file():
                return [(source, max(1, input_index))]
            if source.is_dir():
                # Match pymss.MSSeparator.process_folder() ordering so output
                # indices stay aligned with the upstream processing loop.
                return [
                    (source / name, max(1, input_index + offset))
                    for offset, name in enumerate(os.listdir(source))
                ]
            raise ValueError(f"Input path '{input_folder}' does not exist.")

        def _prepare_output_contexts(self, input_folder: str, input_index: int) -> None:
            contexts: dict[str, deque[dict[str, Any]]] = {}
            for path, current_index in self._input_paths_for_context(input_folder, input_index):
                contexts.setdefault(path.stem, deque()).append({
                    "file_name": path.stem,
                    "source_name": path.name,
                    "input_path": str(path),
                    "input_index": current_index,
                    "now": datetime.now(),
                })
            self._studio_input_contexts = contexts
            self._studio_active_context = None
            self._studio_active_output_count = 0

        def _output_context_for(self, file_name: str) -> dict[str, Any]:
            active = getattr(self, "_studio_active_context", None)
            expected_output_count = max(1, int(getattr(self, "_studio_expected_output_count", 1)))
            active_output_count = int(getattr(self, "_studio_active_output_count", 0))
            input_contexts = getattr(self, "_studio_input_contexts", {})
            if (
                active is None
                or active.get("file_name") != file_name
                or active_output_count >= expected_output_count
            ):
                pending = input_contexts.get(file_name)
                active = pending.popleft() if pending else {
                    "file_name": file_name,
                    "source_name": file_name,
                    "input_path": file_name,
                    "input_index": 1,
                    "now": datetime.now(),
                }
                self._studio_active_context = active
                active_output_count = 0
            self._studio_active_output_count = active_output_count + 1
            return active

        def _discard_outputs_for_failed_inputs(self, success_files: set[str]) -> None:
            kept_outputs: list[dict[str, str]] = []
            output_sources = getattr(self, "_studio_output_sources", {})
            for output in self._studio_last_outputs:
                path = Path(str(output.get("path") or ""))
                source_name = output_sources.get(path)
                if source_name is None or source_name in success_files:
                    kept_outputs.append(output)
                    continue
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    self.logger.warning(f"Cannot remove incomplete output: {path}, error: {exc}")
                self._studio_claimed_paths.discard(path)
                output_sources.pop(path, None)
            self._studio_last_outputs = kept_outputs

        def _save_output(self, instr: str, audio: Any, sr: int, file_name: str, save_dir: str) -> None:
            # pymss invokes _save_output from its own save executor. Keep the
            # naming and reservation adapter under one lock, while leaving the
            # separation, prefetch, and save scheduling to pymss itself.
            with self._studio_output_lock:
                context = self._output_context_for(file_name)
                stem = str(instr or "").strip() or file_name
                if self._studio_naming["enabled"]:
                    target_name = _replace_output_tokens(
                        self._studio_naming["template"],
                        input_path=str(context.get("input_path") or self._studio_input_path),
                        stem=stem,
                        stem_index=self._studio_stem_indices.get(stem.lower(), len(self._studio_stem_indices)),
                        input_index=int(context.get("input_index") or self._studio_input_index),
                        model=self._studio_output_model,
                        now=context.get("now") or self._studio_now,
                    )
                else:
                    target_name = _safe_filename_part(f"{file_name}_{stem}")

                target = _claim_output_path(
                    Path(save_dir) / f"{target_name}.{self.output_format.lower()}",
                    self._studio_claimed_paths,
                )
                try:
                    self.save_audio(audio, sr, target.stem, str(target.parent))
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
                self._studio_claimed_paths.add(target)
                self._studio_last_outputs.append({"stem": stem, "path": str(target)})
                output_sources = getattr(self, "_studio_output_sources", None)
                if output_sources is None:
                    output_sources = {}
                    self._studio_output_sources = output_sources
                output_sources[target] = str(context.get("source_name") or file_name)

        def process_folder(self, input_folder: str, input_index: int = 1) -> list[str]:
            """Delegate separation to pymss and retain Studio output metadata."""
            self._studio_last_outputs = []
            self._studio_output_sources = {}
            self._prepare_output_contexts(input_folder, input_index)
            try:
                success_files = super().process_folder(input_folder)
            except Exception:
                self._discard_outputs_for_failed_inputs(set())
                raise
            finally:
                self._studio_input_contexts = {}
                self._studio_active_context = None
                self._studio_active_output_count = 0
            self._discard_outputs_for_failed_inputs(set(success_files))
            return success_files

        def studio_outputs(self) -> list[dict[str, str]]:
            return sorted(
                self._studio_last_outputs,
                key=lambda output: (
                    self._studio_stem_indices.get(str(output.get("stem") or "").lower(), len(self._studio_stem_indices)),
                    str(output.get("path") or "").lower(),
                ),
            )

    return StudioMSSeparator


def resolve_pymss_output_dir(output_dir: str, success_files: list[str], fallback_input: str, save_as_folder: bool) -> str:
    if not save_as_folder:
        return str(Path(output_dir))
    file_name = Path(success_files[0]).stem if success_files else Path(fallback_input).stem
    return str(Path(output_dir) / file_name)

def _emit_inference_error(exc: Exception, task_id: str) -> int:
    message = str(exc)
    lowered = message.lower()
    if "no audio stream found" in lowered:
        return emit_error(
            "INPUT_AUDIO_STREAM_MISSING",
            message,
            traceback.format_exc(),
            task_id=task_id,
        )
    if "invalid data found" in lowered or "could not open input" in lowered:
        return emit_error(
            "INPUT_MEDIA_UNSUPPORTED",
            message,
            traceback.format_exc(),
            task_id=task_id,
        )
    return emit_error("INFERENCE_FAILED", message, traceback.format_exc(), task_id=task_id)

def _close_separator(separator: Any) -> None:
    if separator is None:
        return
    close = getattr(separator, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass
    else:
        try:
            separator.del_cache()
        except Exception:
            pass


def _normalize_output_dir(value: Any) -> str:
    default_output_dir = os.environ.get("PYMSS_STUDIO_DEFAULT_OUTPUT_DIR")
    output_dir = value or default_output_dir or "results"
    output_path = Path(str(output_dir))
    if not output_path.is_absolute() and default_output_dir:
        return str(Path(default_output_dir).parent / output_path)
    return str(output_dir)

def _normalize_selected_stems(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    stems: list[str] = []
    seen: set[str] = set()
    for item in value:
        stem = str(item or "").strip()
        if not stem or stem.lower() in seen:
            continue
        stems.append(stem)
        seen.add(stem.lower())
    return stems

def _store_dirs_for_selected_stems(output_dir: str, selected_stems: list[str]) -> Any:
    if not selected_stems:
        return output_dir
    return {stem: output_dir for stem in selected_stems}

def _normalize_output_layout(value: Any) -> str:
    return "flat" if str(value or "").strip().lower() == "flat" else "folders"


def _normalize_device_ids(value: Any) -> list[int]:
    raw_ids = value if isinstance(value, list) else [value]
    device_ids: list[int] = []
    for raw_id in raw_ids:
        try:
            device_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if device_id >= 0 and device_id not in device_ids:
            device_ids.append(device_id)
    return device_ids or [0]


def _resolve_separator_device(device: Any, device_ids: Any) -> tuple[str, list[int], str]:
    requested_device = str(device or "auto").strip().lower() or "auto"
    normalized_ids = _normalize_device_ids(device_ids)
    if requested_device != "cuda":
        return requested_device, normalized_ids, requested_device

    # pymss currently leaves an explicit `device="cuda"` unchanged. PyTorch
    # interprets that bare value as the process default CUDA device (normally
    # cuda:0), so the selected device id would otherwise be ignored. Let
    # pymss's auto path resolve the indexed CUDA device from device_ids.
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError("CUDA was selected, but PyTorch is not installed") from exc
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA was selected, but CUDA is not available")
    device_count = int(torch.cuda.device_count())
    invalid_ids = [device_id for device_id in normalized_ids if device_id >= device_count]
    if invalid_ids:
        raise RuntimeError(
            f"CUDA device id(s) {invalid_ids} are unavailable; detected {device_count} CUDA device(s)"
        )
    return "auto", normalized_ids, f"cuda:{normalized_ids[0]}"


def _resolve_studio_model(
    model_name: str,
    model_dir: str | None,
    *,
    require_supported: bool,
    require_exists: bool,
) -> dict[str, Any]:
    entry = get_any_model_entry(model_name)
    if require_supported and not bool(getattr(entry, "supported", True)):
        reason = str(getattr(entry, "unsupported_reason", "") or "unsupported")
        raise RuntimeError(f"Model {model_name!r} is not supported: {reason}")
    model_path = model_path_for(entry, model_dir)
    config_path = config_path_for(entry, model_dir)
    auxiliary_paths = auxiliary_paths_for(entry, model_dir)
    required_paths = [model_path]
    if config_path is not None:
        required_paths.append(config_path)
    required_paths.extend(auxiliary_paths)
    missing = [path for path in required_paths if not path.is_file()]
    if require_exists and missing:
        raise FileNotFoundError(
            f"Model {model_name!r} is missing required file(s): {', '.join(str(path) for path in missing)}"
        )
    return {
        "entry": entry,
        "model_type": getattr(entry, "model_type", None),
        "model_path": str(model_path),
        "config_path": str(config_path) if config_path else None,
        "auxiliary_paths": [str(path) for path in auxiliary_paths],
    }


def _prepare_separator(
    *,
    payload: dict[str, Any],
    task_id: str,
    progress_callback: Any,
    logger: Any,
) -> Any:
    model_name = payload.get("model")
    if not model_name:
        raise ValueError("Missing model name")
    model_dir = payload.get("modelDir") or None
    download = bool(payload.get("download", True))
    source = payload.get("source") or "modelscope"
    download_method = payload.get("downloadMethod") or "aria2c"
    endpoint = payload.get("endpoint") or None
    device, device_ids, resolved_device_label = _resolve_separator_device(
        payload.get("device"), payload.get("deviceIds")
    )
    output_format = payload.get("outputFormat") or "wav"
    selected_stems = _normalize_selected_stems(payload.get("selectedStems"))
    use_tta = bool(payload.get("useTta", False))
    debug = bool(payload.get("debug", False))
    inference_params = normalize_inference_params(
        payload.get("inferenceParams"),
        payload.get("inferenceParamsVersion"),
    )
    audio_params = normalize_audio_params(payload.get("audioParams"))

    emit("task_log", {
        "level": "info",
        "message": f"Runtime device: {resolved_device_label} (device_ids={device_ids})",
    }, task_id=task_id)

    if download:
        emit("task_stage", {"stage": "downloading_model", "message": "Checking model files"}, task_id=task_id)
    else:
        emit("task_stage", {"stage": "ensuring_model", "message": "Checking model files"}, task_id=task_id)
    StudioMSSeparator = _studio_separator_type()
    emit("task_stage", {"stage": "loading_model", "message": "Loading model"}, task_id=task_id)
    try:
        resolved = _resolve_studio_model(model_name, model_dir, require_supported=True, require_exists=True)
    except Exception as resolve_exc:
        if not download:
            raise ModelNotFoundError(str(resolve_exc)) from resolve_exc
        from pymss import model_download as pymss_model_download  # type: ignore
        from pymss.model_download import download_model  # type: ignore
        from worker_download import download_studio_model, effective_proxy_url, files_for_studio_model, prepare_pymss_download
        emit("task_stage", {"stage": "downloading_model", "message": "Downloading model files"}, task_id=task_id)
        try:
            prepare_pymss_download(pymss_model_download, task_id, download_model, download_method)
            _entry, files = files_for_studio_model(model_name, model_dir)
            download_studio_model(
                pymss_model_download,
                model_name,
                files,
                source=source,
                endpoint=endpoint,
                proxy=effective_proxy_url(),
                task_id=task_id,
            )
            resolved = _resolve_studio_model(model_name, model_dir, require_supported=True, require_exists=True)
        except Exception as download_exc:
            raise ModelDownloadError(str(download_exc)) from download_exc
    if not isinstance(resolved, dict):
        raise RuntimeError(f"resolve_model returned unexpected result for {model_name!r}: {type(resolved).__name__}")
    resolved_model_type = resolved.get('model_type')
    resolved_model_path = resolved.get('model_path')
    if not resolved_model_type or not resolved_model_path:
        missing = [key for key in ('model_type', 'model_path') if not resolved.get(key)]
        raise RuntimeError(f"resolve_model result for {model_name!r} is missing required field(s): {', '.join(missing)}")
    runtime_inference_params = _enrich_inference_params_for_model(
        model_type=resolved_model_type,
        config_path=resolved.get('config_path'),
        inference_params=inference_params,
    )
    return StudioMSSeparator(
        model_type=resolved_model_type,
        model_path=resolved_model_path,
        config_path=resolved.get('config_path'),
        device=device,
        device_ids=device_ids,
        output_format=output_format,
        use_tta=use_tta,
        store_dirs=_store_dirs_for_selected_stems(_normalize_output_dir(payload.get("output")), selected_stems),
        save_as_folder=bool(payload.get("saveAsFolder", False)),
        audio_params=audio_params,
        logger=logger,
        debug=debug,
        progress_callback=progress_callback,
        inference_params=runtime_inference_params,
        output_naming=payload.get("outputNaming"),
        output_model=str(model_name or ""),
    )


def normalize_inference_params(payload_params: Any, version: Any = None) -> dict[str, Any]:
    if not isinstance(payload_params, dict):
        return {}

    params = dict(payload_params)
    try:
        version_value = int(version) if version is not None else None
    except (TypeError, ValueError):
        version_value = None

    if version_value is not None and version_value >= 2:
        if params.get("standardize") in {"", "default"}:
            params.pop("standardize", None)
        if params.get("normalize") in {"", "default"}:
            params.pop("normalize", None)
        return params

    # Legacy desktop tasks used `normalize` for input standardization and did not
    # send the new output-normalize flag separately. If `standardize` is absent,
    # treat the historical `normalize` field as the old input standardization
    # switch and default the new output normalize to False.
    if "standardize" not in params and "normalize" in params:
        legacy_standardize = params.pop("normalize")
        params["standardize"] = legacy_standardize
        params["normalize"] = False
        return params

    if "standardize" in params and "normalize" not in params:
        params["normalize"] = False
    elif "standardize" not in params and "normalize" not in params:
        params["standardize"] = True
        params["normalize"] = False
    return params


def _sanitize_runtime_inference_params(params: dict[str, Any]) -> dict[str, Any]:
    next_params = dict(params or {})

    def _drop_non_positive_int(key: str) -> None:
        if key not in next_params:
            return
        value = _as_int(next_params.get(key))
        if value is None or value <= 0:
            next_params.pop(key, None)
            return
        next_params[key] = value

    for numeric_key in ("batch_size", "overlap_size", "num_overlap", "chunk_size", "window_size"):
        _drop_non_positive_int(numeric_key)

    if "aggression" in next_params:
        aggression_value = _as_int(next_params.get("aggression"))
        if aggression_value is None or aggression_value < 0:
            next_params.pop("aggression", None)
        else:
            next_params["aggression"] = aggression_value

    if "post_process_threshold" in next_params:
        threshold_value = _as_float(next_params.get("post_process_threshold"))
        if threshold_value is None or threshold_value < 0:
            next_params.pop("post_process_threshold", None)
        else:
            next_params["post_process_threshold"] = threshold_value

    for bool_key in ("enable_post_process", "high_end_process", "standardize", "normalize"):
        if bool_key not in next_params:
            continue
        bool_value = _as_bool(next_params.get(bool_key))
        if bool_value is None:
            next_params.pop(bool_key, None)
        else:
            next_params[bool_key] = bool_value

    return next_params



def _enrich_inference_params_for_model(
    *,
    model_type: str | None,
    config_path: str | None,
    inference_params: dict[str, Any],
) -> dict[str, Any]:
    params = _sanitize_runtime_inference_params(inference_params)
    normalized_model_type = str(model_type or '').strip().lower()
    if normalized_model_type == 'vr':
        params.pop('num_overlap', None)
        return params
    if normalized_model_type == 'apollo':
        params.pop('num_overlap', None)
        return params
    if not config_path or not Path(config_path).is_file():
        params.pop('num_overlap', None)
        return params

    try:
        from pymss.config import load_config, to_plain  # type: ignore

        config = to_plain(load_config(str(config_path)))
    except Exception:
        params.pop('num_overlap', None)
        return params

    inference = config.get('inference') if isinstance(config, dict) else None
    audio = config.get('audio') if isinstance(config, dict) else None
    inference = inference if isinstance(inference, dict) else {}
    audio = audio if isinstance(audio, dict) else {}

    explicit_overlap_size = _as_int(params.get('overlap_size'))
    explicit_num_overlap = _as_int(params.get('num_overlap'))
    config_overlap_size = _as_int(inference.get('overlap_size'))
    config_num_overlap = _as_int(inference.get('num_overlap'))
    chunk_size = _as_int(params.get('chunk_size'))
    if chunk_size is None:
        chunk_size = _as_int(audio.get('chunk_size'))
    if chunk_size is None:
        chunk_size = _as_int(inference.get('chunk_size'))

    if explicit_overlap_size is None:
        derived_overlap_size: int | None = None
        if explicit_num_overlap is not None:
            derived_overlap_size = _derive_overlap_size_from_num_overlap(chunk_size, explicit_num_overlap)
        elif config_overlap_size is None and config_num_overlap is not None:
            derived_overlap_size = _derive_overlap_size_from_num_overlap(chunk_size, config_num_overlap)
        if derived_overlap_size is not None:
            params['overlap_size'] = derived_overlap_size

    params.pop('num_overlap', None)
    return params



def normalize_audio_params(payload_audio_params: Any) -> dict[str, Any]:
    defaults = {
        "wav_bit_depth": "FLOAT",
        "flac_bit_depth": "PCM_24",
        "mp3_bit_rate": "320k",
        "m4a_bit_rate": "512k",
        "m4a_codec": "aac",
        "m4a_aac_at_quality": 2,
    }
    if not isinstance(payload_audio_params, dict):
        return defaults
    normalized = {
        **defaults,
        **payload_audio_params,
    }
    normalized["m4a_codec"] = "aac"
    return normalized


def cmd_infer_batch(payload: dict[str, Any]) -> int:
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return emit_error("INPUT_NOT_FOUND", "Missing batch tasks", task_id=payload.get("taskId") or None)

    root_task_id = str(payload.get("taskId") or raw_tasks[0].get("taskId") or f"sep_{int(datetime.now().timestamp())}")
    output_root = _normalize_output_dir(payload.get("output"))
    output_format = payload.get("outputFormat") or "wav"
    output_layout = _normalize_output_layout(payload.get("outputLayout"))
    save_as_folder = output_layout == "folders"
    batch_tasks: list[dict[str, Any]] = []

    for index, item in enumerate(raw_tasks):
        if not isinstance(item, dict):
            return emit_error("INPUT_NOT_FOUND", f"Invalid batch task at index {index}", task_id=root_task_id)
        task_id = str(item.get("taskId") or "").strip()
        input_path = str(item.get("input") or "").strip()
        if not task_id:
            return emit_error("INPUT_NOT_FOUND", f"Missing taskId for batch task {index + 1}", task_id=root_task_id)
        if not input_path:
            return emit_error("INPUT_NOT_FOUND", f"Missing input path for batch task {task_id}", task_id=task_id)
        source_path = Path(input_path)
        if not source_path.exists():
            return emit_error("INPUT_NOT_FOUND", f"Input path does not exist: {input_path}", task_id=task_id)
        batch_tasks.append({
            "taskId": task_id,
            "input": str(source_path),
            "inputIndex": int(item.get("inputIndex") or index + 1),
        })

    logger = None
    log_handler = None
    separator = None
    active_task_id: str | None = None
    last_reported_done: float | None = None
    last_reported_total: float | None = None
    last_progress_message = ""

    def emit_batch_progress(done: Any, total: Any, message: str | None = None) -> None:
        nonlocal last_reported_done, last_reported_total, last_progress_message
        try:
            total_value = float(total)
            done_value = float(done)
        except (TypeError, ValueError):
            return
        safe_message = message or "Separating"
        if (
            done_value == last_reported_done
            and total_value == last_reported_total
            and safe_message == last_progress_message
        ):
            return
        last_reported_done = done_value
        last_reported_total = total_value
        last_progress_message = safe_message
        targets = [active_task_id] if active_task_id else [item["taskId"] for item in batch_tasks]
        for task_id in targets:
            if not task_id:
                continue
            emit("task_progress", {
                "stage": "separating",
                "message": safe_message,
                "done": done_value,
                "total": total_value,
            }, task_id=task_id)

    try:
        Path(output_root).mkdir(parents=True, exist_ok=True)
        for item in batch_tasks:
            task_output = resolve_pymss_output_dir(output_root, [], item["input"], save_as_folder)
            emit("task_started", {"model": payload.get("model"), "input": item["input"], "output": task_output}, task_id=item["taskId"])
            emit("task_stage", {"stage": "validating_input", "message": "Validating input"}, task_id=item["taskId"])
        try:
            from pymss import get_separation_logger  # type: ignore
            logger = get_separation_logger()
            log_handler = JsonLogHandler(root_task_id)
            logger.addHandler(log_handler)
        except Exception:
            logger = None
        separator = _prepare_separator(
            payload={**payload, "output": output_root, "saveAsFolder": save_as_folder},
            task_id=root_task_id,
            progress_callback=emit_batch_progress,
            logger=logger,
        )
        for item in batch_tasks:
            task_id = item["taskId"]
            active_task_id = task_id
            emit("task_stage", {"stage": "separating", "message": "Separating"}, task_id=task_id)
            success_files = separator.process_folder(item["input"], int(item.get("inputIndex") or 1))
            if Path(item["input"]).name not in {Path(name).name for name in success_files}:
                emit_error("INFERENCE_FAILED", f"Batch separation did not produce outputs for {Path(item['input']).name}", task_id=task_id)
                continue
            task_output = resolve_pymss_output_dir(output_root, success_files, item["input"], save_as_folder)
            emit("task_stage", {"stage": "writing_output", "message": "Collecting outputs"}, task_id=task_id)
            outputs = separator.studio_outputs()
            emit("task_done", {
                "files": [output["path"] for output in outputs],
                "outputs": outputs,
                "outputDir": str(Path(task_output).resolve()),
                "outputFormat": output_format,
            }, task_id=task_id)
        active_task_id = None
        return 0
    except Exception as exc:
        for item in batch_tasks:
            _emit_inference_error(exc, item["taskId"])
        return 1
    finally:
        if logger is not None and log_handler is not None:
            try:
                logger.removeHandler(log_handler)
            except Exception:
                pass
        _close_separator(separator)


def cmd_infer(payload: dict[str, Any]) -> int:
    if isinstance(payload.get("tasks"), list):
        return cmd_infer_batch(payload)

    task_id = payload.get("taskId") or f"sep_{int(datetime.now().timestamp())}"
    model_name = payload.get("model")
    input_path = payload.get("input")
    default_output_dir = os.environ.get("PYMSS_STUDIO_DEFAULT_OUTPUT_DIR")
    output_dir = payload.get("output") or default_output_dir or "results"
    output_path = Path(output_dir)
    if not output_path.is_absolute() and default_output_dir:
        output_dir = str(Path(default_output_dir).parent / output_path)
    if not model_name:
        return emit_error("MODEL_NOT_FOUND", "Missing model name", task_id=task_id)
    if not input_path:
        return emit_error("INPUT_NOT_FOUND", "Missing input path", task_id=task_id)
    if not Path(input_path).exists():
        return emit_error("INPUT_NOT_FOUND", f"Input path does not exist: {input_path}", task_id=task_id)
    try:
        _resolve_separator_device(payload.get("device"), payload.get("deviceIds"))
    except Exception as exc:
        return emit_error("DEVICE_CONFIG_INVALID", str(exc), task_id=task_id)

    output_format = payload.get("outputFormat") or "wav"
    output_layout = _normalize_output_layout(payload.get("outputLayout"))
    save_as_folder = output_layout == "folders"
    task_output = resolve_pymss_output_dir(output_dir, [], input_path, save_as_folder)

    last_reported_done: float | None = None
    last_reported_total: float | None = None
    last_progress_message = ""

    def emit_separation_progress(done: Any, total: Any, message: str | None = None) -> None:
        nonlocal last_reported_done, last_reported_total, last_progress_message
        try:
            total_value = float(total)
            done_value = float(done)
        except (TypeError, ValueError):
            return
        safe_message = message or "Separating"
        if (
            done_value == last_reported_done
            and total_value == last_reported_total
            and safe_message == last_progress_message
        ):
            return
        last_reported_done = done_value
        last_reported_total = total_value
        last_progress_message = safe_message
        emit("task_progress", {
            "stage": "separating",
            "message": safe_message,
            "done": done_value,
            "total": total_value,
        }, task_id=task_id)

    separator = None
    logger = None
    log_handler = None
    try:
        emit("task_started", {"model": model_name, "input": input_path, "output": task_output}, task_id=task_id)
        emit("task_stage", {"stage": "validating_input", "message": "Validating input"}, task_id=task_id)
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        try:
            from pymss import get_separation_logger  # type: ignore
            logger = get_separation_logger()
            log_handler = JsonLogHandler(task_id)
            logger.addHandler(log_handler)
        except Exception:
            logger = None

        separator = _prepare_separator(
            payload={
                **payload,
                "output": output_dir,
                "saveAsFolder": save_as_folder,
            },
            task_id=task_id,
            progress_callback=emit_separation_progress,
            logger=logger,
        )
        emit("task_stage", {"stage": "separating", "message": "Separating"}, task_id=task_id)
        success_files = separator.process_folder(input_path, int(payload.get("inputIndex") or 1))
        if Path(input_path).name not in {Path(name).name for name in success_files}:
            return emit_error("INFERENCE_FAILED", f"Separation did not produce outputs for {Path(input_path).name}", task_id=task_id)
        emit("task_stage", {"stage": "writing_output", "message": "Collecting outputs"}, task_id=task_id)
        task_output = resolve_pymss_output_dir(output_dir, success_files, input_path, save_as_folder)
        outputs = separator.studio_outputs()
        emit("task_done", {"files": [output["path"] for output in outputs], "outputs": outputs, "outputDir": str(Path(task_output).resolve()), "outputFormat": output_format}, task_id=task_id)
        return 0
    except ModelNotFoundError as exc:
        return emit_error("MODEL_NOT_FOUND", str(exc), traceback.format_exc(), task_id=task_id)
    except ModelDownloadError as exc:
        return emit_error("MODEL_DOWNLOAD_FAILED", str(exc), traceback.format_exc(), task_id=task_id)
    except Exception as exc:
        return _emit_inference_error(exc, task_id)
    finally:
        if logger is not None and log_handler is not None:
            try:
                logger.removeHandler(log_handler)
            except Exception:
                pass
        _close_separator(separator)
