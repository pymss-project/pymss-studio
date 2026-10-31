from __future__ import annotations

import json
import os
import re
import sys
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any, TextIO

from pymss.plugins.kit import emit, isolate_stdout as isolate_protocol_stdout
from .asr_cache import is_complete_asr_model_cache, unresolved_incomplete_files
from pymss.plugins.kit import (
    ToolError as AudioToolError,
    render_output,
    require_directory as _require_directory,
    require_file as _require_file,
)

DEFAULT_MODEL = "paraformer-zh"
DEFAULT_VAD_MODEL = "fsmn-vad"
DEFAULT_PUNC_MODEL = "ct-punc"
DEFAULT_PROFILE = "paraformer-zh"
OUTPUT_FORMATS = {"txt", "json", "srt"}
ASR_MODEL_CACHE_RELATIVE_PATH = Path("_tool_models") / "asr"
_MODELSCOPE_MODEL_ALIASES = {
    "paraformer-zh": "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    "paraformer-en": "iic/speech_paraformer-large-vad-punc_asr_nat-en-16k-common-vocab10020",
    "fsmn-vad": "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
    "ct-punc": "iic/punc_ct-transformer_cn-en-common-vocab471067-large",
}

ASR_LANGUAGE_NAMES = {
    "zh": "中文",
    "en": "英文",
    "yue": "粤语",
    "ja": "日文",
    "ko": "韩文",
    "vi": "越南语",
    "id": "印尼语",
    "th": "泰语",
    "ms": "马来语",
    "fil": "菲律宾语",
    "ar": "阿拉伯语",
    "hi": "印地语",
    "bg": "保加利亚语",
    "hr": "克罗地亚语",
    "cs": "捷克语",
    "da": "丹麦语",
    "nl": "荷兰语",
    "et": "爱沙尼亚语",
    "fi": "芬兰语",
    "el": "希腊语",
    "hu": "匈牙利语",
    "ga": "爱尔兰语",
    "lv": "拉脱维亚语",
    "lt": "立陶宛语",
    "mt": "马耳他语",
    "pl": "波兰语",
    "pt": "葡萄牙语",
    "ro": "罗马尼亚语",
    "sk": "斯洛伐克语",
    "sl": "斯洛文尼亚语",
    "sv": "瑞典语",
}
ASR_LANGUAGE_CODES_BY_NAME = {name: code for code, name in ASR_LANGUAGE_NAMES.items()}

_MLT_LANGUAGES = frozenset({"auto", *ASR_LANGUAGE_NAMES})
ASR_MODEL_PROFILES: dict[str, dict[str, Any]] = {
    "paraformer-zh": {
        "model": DEFAULT_MODEL,
        "vad_model": DEFAULT_VAD_MODEL,
        "punc_model": DEFAULT_PUNC_MODEL,
        "languages": frozenset({"zh"}),
        "default_language": "zh",
        "language_mode": "fixed",
        "hotword_mode": "string",
        "text_mode": "plain",
        "timestamps": True,
    },
    "paraformer-en": {
        "model": "paraformer-en",
        "vad_model": DEFAULT_VAD_MODEL,
        "punc_model": DEFAULT_PUNC_MODEL,
        "languages": frozenset({"en"}),
        "default_language": "en",
        "language_mode": "fixed",
        "hotword_mode": "string",
        "text_mode": "plain",
        "timestamps": True,
    },
    "sensevoice-small": {
        "model": "iic/SenseVoiceSmall",
        "vad_model": DEFAULT_VAD_MODEL,
        "punc_model": None,
        "languages": frozenset({"auto", "zh", "en", "yue", "ja", "ko"}),
        "default_language": "auto",
        "language_mode": "code",
        "hotword_mode": "none",
        "text_mode": "rich",
        "timestamps": True,
    },
    "fun-asr-nano": {
        "model": "FunAudioLLM/Fun-ASR-Nano-2512",
        "vad_model": DEFAULT_VAD_MODEL,
        "punc_model": None,
        "vad_kwargs": {"max_single_segment_time": 30_000},
        "languages": frozenset({"auto", "zh", "en", "ja"}),
        "default_language": "auto",
        "language_mode": "name",
        "hotword_mode": "list",
        "text_mode": "plain",
        "timestamps": False,
        "trust_remote_code": True,
    },
    "fun-asr-mlt-nano": {
        "model": "FunAudioLLM/Fun-ASR-MLT-Nano-2512",
        "vad_model": DEFAULT_VAD_MODEL,
        "punc_model": None,
        "vad_kwargs": {"max_single_segment_time": 30_000},
        "languages": _MLT_LANGUAGES,
        "default_language": "auto",
        "language_mode": "name",
        "hotword_mode": "list",
        "text_mode": "plain",
        "timestamps": False,
        "trust_remote_code": True,
    },
}

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DOWNLOAD_PROGRESS_RE = re.compile(
    r"(?P<name>[^\r\n]{1,240}?):\s*(?P<percent>\d{1,3})%\|"
)
_DOWNLOAD_TRANSFER_RE = re.compile(
    r"(?P<completed>\d+(?:\.\d+)?\s*[KMGT]?B?)\s*/\s*"
    r"(?P<total>\d+(?:\.\d+)?\s*[KMGT]?B?)",
    re.IGNORECASE,
)
_DOWNLOAD_TIMING_RE = re.compile(
    r"\[(?P<elapsed>[^<\],]+)<(?P<remaining>[^,\]]+),\s*(?P<rate>[^\]]+)\]"
)
_MODEL_DOWNLOAD_RE = re.compile(
    r"\bDownloading\s+\d+\s+files\s+from\s+(?P<model>[^@\s]+)@",
    re.IGNORECASE,
)
_MODEL_LOAD_PATH_RE = re.compile(
    r"(?:Loading pretrained params from|ckpt:)\s+(?P<path>.+?\.(?:pt|pth|bin|onnx|safetensors))\s*$",
    re.IGNORECASE,
)
_MODEL_DOWNLOAD_FAILURE_MARKERS = (
    "download failed",
    "failed to download",
    "file(s) failed to download",
    "no space left on device",
)


def _parse_model_download_progress(value: str) -> dict[str, Any] | None:
    line = _ANSI_ESCAPE_RE.sub("", value).strip()
    match = _DOWNLOAD_PROGRESS_RE.search(line)
    if not match:
        return None

    percent = min(100, max(0, int(match.group("percent"))))
    name = match.group("name").strip().rsplit(":", 1)[-1].strip()
    details: list[str] = []
    transfer = _DOWNLOAD_TRANSFER_RE.search(line, match.end())
    if transfer:
        details.append(f"{transfer.group('completed').strip()} / {transfer.group('total').strip()}")
    timing = _DOWNLOAD_TIMING_RE.search(line, match.end())
    if timing:
        details.append(timing.group("rate").strip())
        details.append(f"ETA {timing.group('remaining').strip()}")
    return {
        "operation": "asr",
        "phase": "loading_asr_model",
        "completed": percent,
        "total": 100,
        "current": name,
        "detail": " · ".join(details),
    }


class _ModelLoadOutput:
    """Convert third-party download bars into worker progress without terminal spam."""

    def __init__(self, fallback: TextIO) -> None:
        self._fallback = fallback
        self._last_progress: tuple[str, int] | None = None
        self.observed_model_ids: set[str] = set()
        self.failed_model_ids: set[str] = set()
        self.loaded_model_paths: set[Path] = set()
        self.disk_full = False
        self._current_model_id = ""

    def _observe(self, value: str) -> None:
        line = _ANSI_ESCAPE_RE.sub("", value).strip()
        if not line:
            return
        download = _MODEL_DOWNLOAD_RE.search(line)
        if download:
            self._current_model_id = download.group("model").strip()
            self.observed_model_ids.add(self._current_model_id)
        loaded = _MODEL_LOAD_PATH_RE.search(line)
        if loaded:
            self.loaded_model_paths.add(Path(loaded.group("path").strip().strip('"')))
        lowered = line.lower()
        if any(marker in lowered for marker in _MODEL_DOWNLOAD_FAILURE_MARKERS):
            if self._current_model_id:
                self.failed_model_ids.add(self._current_model_id)
            self.disk_full = self.disk_full or "no space left on device" in lowered

    def write(self, value: str) -> int:
        handled = False
        for line in re.split(r"[\r\n]+", value):
            self._observe(line)
            progress = _parse_model_download_progress(line)
            if not progress:
                continue
            handled = True
            marker = (str(progress["current"]), int(progress["completed"]))
            if marker != self._last_progress:
                self._last_progress = marker
                emit("audio_tool_progress", progress)
        if not handled:
            self._fallback.write(value)
        return len(value)

    def flush(self) -> None:
        self._fallback.flush()


def _model_cache_id(model_id: str) -> str:
    parts = [part for part in model_id.strip().strip("/\\").replace("\\", "/").split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        return ""
    return "--".join(parts)


def _model_cache_recovery(
    output: _ModelLoadOutput,
    cache_dir: Path | None,
) -> dict[str, Any] | None:
    if cache_dir is None:
        return None
    models_dir = cache_dir / "models"
    broken_ids = {_model_cache_id(value) for value in output.failed_model_ids}
    broken_ids.discard("")

    for model_id in output.observed_model_ids:
        cache_id = _model_cache_id(model_id)
        if cache_id and unresolved_incomplete_files(models_dir / cache_id):
            broken_ids.add(cache_id)

    for loaded_path in output.loaded_model_paths:
        try:
            relative = loaded_path.resolve(strict=False).relative_to(models_dir.resolve(strict=False))
        except ValueError:
            continue
        if not loaded_path.is_file() and relative.parts:
            broken_ids.add(relative.parts[0])

    if not broken_ids:
        return None
    return {
        "action": "redownload_asr_models",
        "tool": "asr",
        "modelIds": sorted(broken_ids),
        "modelDir": str(cache_dir.parent.parent),
        "reason": "disk_full" if output.disk_full else "incomplete_download",
    }


def _managed_model_cache_id(value: Any) -> str:
    model = str(value or "").strip()
    if not model or Path(model).is_dir():
        return ""
    resolved = _MODELSCOPE_MODEL_ALIASES.get(model, model)
    return _model_cache_id(resolved) if "/" in resolved else ""


def _existing_model_cache_recovery(
    configuration: dict[str, Any],
    cache_dir: Path | None,
) -> dict[str, Any] | None:
    if cache_dir is None:
        return None
    models_dir = cache_dir / "models"
    configured_ids = {
        _managed_model_cache_id(configuration.get(key))
        for key in ("model", "vad_model", "punc_model")
    }
    configured_ids.discard("")
    broken_ids = sorted(
        model_id for model_id in configured_ids
        if (models_dir / model_id).is_dir()
        and not is_complete_asr_model_cache(models_dir / model_id)
    )
    if not broken_ids:
        return None
    return {
        "action": "redownload_asr_models",
        "tool": "asr",
        "modelIds": broken_ids,
        "modelDir": str(cache_dir.parent.parent),
        "reason": "incomplete_download",
    }


def _model_cache_error(recovery: dict[str, Any]) -> AudioToolError:
    return AudioToolError(
        "ASR_MODEL_INCOMPLETE",
        "The ASR model cache is incomplete or failed to download",
        recoverable=True,
        extra={"recovery": recovery},
    )


def _json_default(value: Any) -> Any:
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "item"):
        return value.item()
    return str(value)


def _timestamp_seconds(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, number / 1000.0)


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _normalize_text(value: Any, mode: str) -> str:
    text = str(value or "").strip()
    if not text or mode != "rich":
        return text
    from funasr.utils.postprocess_utils import rich_transcription_postprocess  # type: ignore

    return str(rich_transcription_postprocess(text)).strip()


def _sentence_entries(
    results: list[dict[str, Any]],
    transcript: str,
    text_mode: str = "plain",
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for result in results:
        sentence_info = result.get("sentence_info")
        if isinstance(sentence_info, list):
            for item in sentence_info:
                if not isinstance(item, dict):
                    continue
                start = _timestamp_seconds(item.get("start"))
                end = _timestamp_seconds(item.get("end"))
                text = _normalize_text(item.get("text") or item.get("sentence"), text_mode)
                if text and start is not None and end is not None and end >= start:
                    entries.append({"start": start, "end": end, "text": text})
    if entries:
        return entries

    timestamps: list[Any] = []
    for result in results:
        value = result.get("timestamp")
        if isinstance(value, list):
            timestamps.extend(value)
    pairs = [item for item in timestamps if isinstance(item, (list, tuple)) and len(item) >= 2]
    if transcript and pairs:
        start = _timestamp_seconds(pairs[0][0])
        end = _timestamp_seconds(pairs[-1][1])
        if start is not None and end is not None and end >= start:
            return [{"start": start, "end": end, "text": transcript}]
    return []


def _write_srt(path: Path, sentences: list[dict[str, Any]]) -> None:
    blocks = []
    for index, sentence in enumerate(sentences, start=1):
        blocks.append(
            f"{index}\n{_srt_time(sentence['start'])} --> {_srt_time(sentence['end'])}\n{sentence['text']}\n"
        )
    path.write_text("\n".join(blocks), encoding="utf-8")


def _resolve_local_models(payload: dict[str, Any]) -> tuple[str, str, str]:
    local_values = [str(payload.get(key) or "").strip() for key in ("modelPath", "vadModelPath", "puncModelPath")]
    allow_download = bool(payload.get("allowDownload"))
    if not allow_download and not all(local_values):
        raise ValueError("Automatic model download is disabled; select ASR, VAD, and punctuation model directories")
    resolved = []
    defaults = (DEFAULT_MODEL, DEFAULT_VAD_MODEL, DEFAULT_PUNC_MODEL)
    for value, label, default in zip(
        local_values,
        ("ASR model", "VAD model", "Punctuation model"),
        defaults,
    ):
        if value:
            path = Path(value).expanduser()
            if not path.is_dir():
                raise ValueError(f"{label} does not exist or is not a directory")
            resolved.append(str(path.resolve()))
        else:
            resolved.append(default)
    return resolved[0], resolved[1], resolved[2]


def _resolve_model_configuration(payload: dict[str, Any]) -> dict[str, Any]:
    if str(payload.get("modelPath") or "").strip():
        model, vad_model, punc_model = _resolve_local_models(payload)
        return {
            "id": "local",
            "model": model,
            "vad_model": vad_model,
            "punc_model": punc_model,
            "language": "auto",
            "language_mode": "fixed",
            "hotword_mode": "string",
            "text_mode": "plain",
            "timestamps": True,
        }

    profile_id = str(payload.get("modelPreset") or DEFAULT_PROFILE).strip().lower()
    if profile_id == "local":
        raise ValueError("Local ASR model directory is required")
    profile = ASR_MODEL_PROFILES.get(profile_id)
    if not profile:
        raise ValueError(f"Unsupported ASR model preset: {profile_id or 'missing'}")
    language = str(payload.get("language") or profile["default_language"]).strip().lower()
    if language not in profile["languages"]:
        raise ValueError(f"Language '{language}' is not supported by ASR model preset '{profile_id}'")
    return {"id": profile_id, **profile, "language": language}


def _resolve_models(payload: dict[str, Any]) -> tuple[str, str | None, str | None]:
    configuration = _resolve_model_configuration(payload)
    return configuration["model"], configuration["vad_model"], configuration["punc_model"]


def _split_hotwords(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,，;；\n]+", value) if item.strip()]


def _generate_options(
    configuration: dict[str, Any],
    input_path: Path,
    hotword: str,
) -> dict[str, Any]:
    is_nano = configuration["id"] in {"fun-asr-nano", "fun-asr-mlt-nano"}
    options: dict[str, Any] = {
        "input": str(input_path),
        "batch_size": 1,
    } if is_nano else {
        "input": str(input_path),
        "batch_size_s": 300,
        "sentence_timestamp": True,
    }

    language = str(configuration["language"])
    language_mode = str(configuration["language_mode"])
    if language_mode == "code":
        options["language"] = language
        options["use_itn"] = True
    elif language_mode == "name":
        options["itn"] = True
        if language != "auto":
            options["language"] = ASR_LANGUAGE_NAMES[language]

    hotword_mode = str(configuration["hotword_mode"])
    if hotword and hotword_mode == "string":
        options["hotword"] = hotword
    elif hotword and hotword_mode == "list":
        options["hotwords"] = _split_hotwords(hotword)
    return options


def _detected_language(results: list[dict[str, Any]]) -> str:
    for result in results:
        language = str(result.get("language") or result.get("lang") or "").strip()
        if language:
            return ASR_LANGUAGE_CODES_BY_NAME.get(language, language)
        match = re.match(r"<\|([^|]+)\|>", str(result.get("text") or ""))
        if match:
            return match.group(1)
    return ""


def _select_device() -> str:
    import torch  # type: ignore

    if torch.cuda.is_available():
        return "cuda:0"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


def _configure_model_cache(payload: dict[str, Any]) -> Path | None:
    model_root = str(payload.get("modelDir") or "").strip()
    if not model_root:
        model_root = os.environ.get("PYMSS_MODEL_DIR", "").strip()
    if not model_root:
        return None

    cache_dir = Path(model_root).expanduser() / ASR_MODEL_CACHE_RELATIVE_PATH
    # Each audio-tool invocation runs in a dedicated Worker process. Assigning the cache path
    # here keeps ModelScope downloads aligned with the model directory selected in Settings,
    # even when the parent process has a global ModelScope cache configured.
    os.environ["MODELSCOPE_CACHE"] = str(cache_dir)
    return cache_dir


def _transcribe_audio(payload: dict[str, Any]) -> dict[str, Any]:
    input_path = _require_file(payload.get("inputPath"), "Input audio")
    output_dir = _require_directory(payload.get("outputDir"), "Output directory", create=True)
    requested_formats = payload.get("outputFormats") or ["txt", "json", "srt"]
    if not isinstance(requested_formats, list) or not requested_formats:
        raise ValueError("At least one ASR output format is required")
    output_formats = {str(value).lower() for value in requested_formats}
    if not output_formats <= OUTPUT_FORMATS:
        raise ValueError("Unsupported ASR output format")
    configuration = _resolve_model_configuration(payload)
    from .asr_mlx import is_apple_silicon, transcribe as transcribe_mlx
    if is_apple_silicon():
        return _transcribe_audio_mlx(payload, input_path, output_dir, output_formats, configuration, transcribe_mlx)
    if output_formats == {"srt"} and not configuration.get("timestamps"):
        raise ValueError("The selected ASR model does not provide timestamps required for SRT output")
    model_path = str(configuration["model"])
    vad_model_path = configuration.get("vad_model")
    punc_model_path = configuration.get("punc_model")
    hotword = str(payload.get("hotword") or "").strip()

    cache_dir = _configure_model_cache(payload)
    existing_recovery = _existing_model_cache_recovery(configuration, cache_dir)
    if existing_recovery:
        raise _model_cache_error(existing_recovery)

    emit("audio_tool_progress", {
        "operation": "asr", "phase": "loading_asr_model", "completed": 0,
        "total": 0, "current": Path(model_path).name,
    })
    try:
        with isolate_protocol_stdout():
            from funasr import AutoModel  # type: ignore
    except Exception as error:
        raise RuntimeError(
            "The FunASR runtime component could not be loaded. Uninstall and reinstall it from "
            "Tools > ASR transcription."
        ) from error

    model_options: dict[str, Any] = {
        "model": model_path,
        "device": _select_device(),
        "disable_update": True,
        "disable_pbar": True,
    }
    if vad_model_path:
        model_options["vad_model"] = vad_model_path
    if punc_model_path:
        model_options["punc_model"] = punc_model_path
    if configuration.get("vad_kwargs"):
        model_options["vad_kwargs"] = configuration["vad_kwargs"]
    if configuration.get("trust_remote_code"):
        model_options["trust_remote_code"] = True

    model_output = _ModelLoadOutput(sys.stderr)
    try:
        with redirect_stderr(model_output), isolate_protocol_stdout():
            model = AutoModel(**model_options)
    except Exception as error:
        recovery = (
            _model_cache_recovery(model_output, cache_dir)
            or _existing_model_cache_recovery(configuration, cache_dir)
        )
        if recovery:
            raise _model_cache_error(recovery) from error
        raise
    recovery = _model_cache_recovery(model_output, cache_dir)
    if recovery:
        raise _model_cache_error(recovery)

    emit("audio_tool_progress", {
        "operation": "asr", "phase": "loading_asr_model", "completed": 1,
        "total": 1, "current": Path(model_path).name, "detail": "",
    })

    emit("audio_tool_progress", {
        "operation": "asr", "phase": "recognizing_speech", "completed": 0,
        "total": 1, "current": input_path.name,
    })
    generate_options = _generate_options(configuration, input_path, hotword)
    with isolate_protocol_stdout():
        generated = model.generate(**generate_options)
    if isinstance(generated, dict):
        results = [generated]
    elif isinstance(generated, list):
        results = [item for item in generated if isinstance(item, dict)]
    else:
        results = []
    text_mode = str(configuration["text_mode"])
    transcript = "\n".join(_normalize_text(item.get("text"), text_mode) for item in results).strip()
    if not transcript:
        raise RuntimeError("ASR completed without returning recognized text")
    sentences = _sentence_entries(results, transcript, text_mode)
    detected_language = _detected_language(results)
    warnings: list[str] = []

    emit("audio_tool_progress", {
        "operation": "asr", "phase": "writing_transcript", "completed": 0,
        "total": len(output_formats), "current": input_path.name,
    })
    output_paths: list[str] = []
    completed = 0
    base = output_dir / f"{input_path.stem}_asr"
    for output_format in sorted(output_formats):
        if output_format == "srt" and not sentences:
            warnings.append("timestamps_unavailable")
            continue
        output_path = base.with_suffix(f".{output_format}")
        if output_format == "txt":
            output_path = render_output(output_path, lambda target: target.write_text(transcript + "\n", encoding="utf-8"))
        elif output_format == "json":
            output_path = render_output(output_path, lambda target: target.write_text(json.dumps({
                "inputPath": str(input_path), "text": transcript,
                "modelPreset": configuration["id"],
                "requestedLanguage": configuration["language"],
                "detectedLanguage": detected_language,
                "sentences": sentences, "raw": results,
            }, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8"))
        else:
            output_path = render_output(output_path, lambda target: _write_srt(target, sentences))
        output_paths.append(str(output_path))
        completed += 1
        emit("audio_tool_progress", {
            "operation": "asr", "phase": "writing_transcript", "completed": completed,
            "total": len(output_formats), "current": output_path.name,
        })
    if not output_paths:
        raise RuntimeError("ASR completed without a writable output for the selected formats")
    return {
        "operation": "asr", "outputDir": str(output_dir), "outputPaths": output_paths,
        "succeeded": len(output_paths), "text": transcript, "sentences": sentences,
        "segmentCount": len(sentences), "warnings": warnings,
        "modelPreset": configuration["id"],
        "requestedLanguage": configuration["language"],
        "detectedLanguage": detected_language,
    }


def _transcribe_audio_mlx(payload: dict[str, Any], input_path: Path, output_dir: Path, output_formats: set[str], configuration: dict[str, Any], transcribe_mlx: Any) -> dict[str, Any]:
    """MLX path preserves the Studio output contract without importing Torch."""
    emit("audio_tool_progress", {"operation": "asr", "phase": "loading_asr_model", "completed": 0, "total": 1, "current": str(configuration["id"])})
    try:
        with isolate_protocol_stdout():
            raw = transcribe_mlx(input_path, str(configuration["id"]), str(payload.get("modelPath") or "") or None, str(payload.get("modelDir") or "") or None, language=configuration["language"])
    except ImportError as error:
        raise RuntimeError("The FunASR MLX runtime component could not be loaded. Install it from Tools > ASR transcription.") from error
    transcript = str(raw.get("text") or "").strip()
    if not transcript:
        raise RuntimeError("ASR completed without returning recognized text")
    emit("audio_tool_progress", {"operation": "asr", "phase": "recognizing_speech", "completed": 1, "total": 1, "current": input_path.name})
    sentences: list[dict[str, Any]] = []
    warnings: list[str] = []
    paths: list[str] = []
    base = output_dir / f"{input_path.stem}_asr"
    for output_format in sorted(output_formats):
        if output_format == "srt":
            warnings.append("timestamps_unavailable")
            continue
        output_path = base.with_suffix(f".{output_format}")
        if output_format == "txt":
            output_path = render_output(output_path, lambda target: target.write_text(transcript + "\n", encoding="utf-8"))
        else:
            output_path = render_output(output_path, lambda target: target.write_text(json.dumps({"inputPath": str(input_path), "text": transcript, "modelPreset": configuration["id"], "requestedLanguage": configuration["language"], "detectedLanguage": "", "sentences": [], "raw": [raw]}, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8"))
        paths.append(str(output_path))
    if not paths:
        raise ValueError("The MLX ASR backend does not provide timestamps required for SRT output")
    return {"operation": "asr", "outputDir": str(output_dir), "outputPaths": paths, "succeeded": len(paths), "text": transcript, "sentences": sentences, "segmentCount": 0, "warnings": warnings, "modelPreset": configuration["id"], "requestedLanguage": configuration["language"], "detectedLanguage": "", "backend": "funasr-mlx"}
