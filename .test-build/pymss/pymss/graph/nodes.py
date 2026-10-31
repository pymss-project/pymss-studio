"""Built-in node executors for the DAG core.

Each node type registers a :class:`~pymss.dag.NodeSignature` factory and an
``execute`` callable. The semantics mirror comfy-mss (``comfy_mss/nodes/*.py``)
so that a comfy-mss JSON graph runs identically here — but implemented directly
on pymss primitives, with no ComfyUI runtime in the loop.

Node coverage (per the goal acceptance criteria):

IO
  - ``pymss_load_audio`` / ``pymss_load_audio_batch``
  - ``pymss_save_audio`` (OUTPUT_NODE)
  - ``input_audio`` (YAML-compiled source; behaves like load_audio)

Separation
  - ``mss_separate`` / ``mss_separate_list``
  - ``custom_mss_separate`` / ``custom_mss_separate_list``
  - ``vr_separate`` / ``vr_separate_list``

Params
  - ``pymss_mss_params`` / ``pymss_vr_params``

Audio tools
  - ``pymss_audio_ensemble`` / ``pymss_audio_invert_phase`` / ``pymss_audio_normalize``

ComfyUI built-ins
  - ``PreviewAudio`` (no-op passthrough; output = its input)
  - ``StringConcatenate`` (string join used to build save filenames)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np
import torch

from .core import (
    AUDIO,
    AudioArtifact,
    DAGError,
    DAGNode,
    DAGOutputRecord,
    MSS_PARAMS,
    NodeContext,
    NodeResult,
    NodeSignature,
    ParamsArtifact,
    PortSpec,
    STRING,
    StringArtifact,
    VR_PARAMS,
    _combined_channel_layout,
    audio_to_numpy,
    get_node_type,
    numpy_to_audio,
    parse_default_int,
    parse_device_ids,
    register_node,
    register_alias,
    safe_filename_part,
    string_value,
    widget,
)


# Max stems comfy-mss exposes on non-list separate nodes. Matches
# ``comfy_mss.constants.MSS_MAX_STEMS`` / ``VR_MAX_STEMS``.
MSS_MAX_STEMS = 8
VR_MAX_STEMS = 2

CUSTOM_MODEL_TYPES = [
    "auto",
    "mel_band_roformer",
    "bs_roformer",
    "bs_roformer_hyperace",
    "bs_conformer",
    "mel_band_conformer",
    "mdx23c",
    "htdemucs",
    "apollo",
    "bandit",
    "bandit_v2",
    "scnet",
]

OUTPUT_NODE_TYPES = {"pymss_save_audio"}


# ---------------------------------------------------------------------------
# Helpers shared across separation nodes
# ---------------------------------------------------------------------------


def _progress_for(ctx: NodeContext, node_id: object, *, audio_index: int = 0, audio_count: int = 1,
                  unit: str = "seconds", passes: int = 1):
    """Adapt pymss' ``callback(done, total, message)`` to per-node progress."""

    pass_index = 0
    last_done = 0

    def _cb(done: int, total: int, message: str | None = None) -> None:
        nonlocal pass_index, last_done
        if ctx.progress_callback is not None:
            ctx.progress_callback(int(done), max(1, int(total)), f"node={node_id} {message or 'separate'}")
        if ctx.progress_event_callback is None and ctx.execution_event_callback is None:
            return
        if passes > 1 and done == 0 and last_done > 0:
            pass_index = min(passes - 1, pass_index + 1)
        last_done = done
        fraction = (pass_index + min(1.0, max(0.0, done / max(1, total)))) / passes
        ctx.report_progress(ctx.nodes_by_id[node_id], (audio_index + fraction) / audio_count,
                            f"node={node_id} {message or 'separate'}", audio_index=audio_index + 1,
                            audio_count=audio_count, done=done, total=total, unit=unit)

    return _cb


def _common_separator_kwargs(
    ctx: NodeContext,
    *,
    device: str | None,
    device_ids_raw: Any,
    params: dict[str, Any] | None,
    use_tta: bool,
    debug: bool,
    stems: list[str],
) -> dict[str, Any]:
    """Build kwargs passed to ``MSSeparator`` that are not model identity."""

    return {
        "device": ctx.device if device in (None, "", "auto") and ctx.device else device or "auto",
        "device_ids": parse_device_ids(device_ids_raw),
        "output_format": "wav",  # we never save through the separator
        "use_tta": bool(use_tta),
        # store_dirs is unused (we pull results from ``separate``), but the
        # MSSeparator constructor requires it; give it an empty mapping so no
        # accidental disk write happens.
        "store_dirs": {stem: "" for stem in stems},
        "debug": bool(debug),
        "progress_callback": None,  # set per-call below
        "inference_params": dict(params or {}),
        "logger": ctx.logger,
    }


def _is_placeholder_stem(name: str) -> bool:
    """Editor placeholder stem names (stem_1, stem_2, ...) — see
    registerNodes.ts DEFAULT_STEMS: ports created before a model is chosen."""
    import re

    return bool(re.fullmatch(r"stem_\d+", str(name).strip()))


def _run_separation(
    ctx: NodeContext,
    node: DAGNode,
    audio: AudioArtifact,
    *,
    build_separator: Any,
    stems: list[str],
    audio_index: int = 0,
    audio_count: int = 1,
) -> tuple[dict[str, np.ndarray], int]:
    """Drive one ``MSSeparator.separate`` call under inference_mode and progress."""

    mix, sample_rate = audio_to_numpy(audio)
    # pymss separators expect channel-first ``[channels, samples]``, which is
    # exactly how AudioArtifact stores it. No transpose needed (the comfy-mss
    # ``audio_to_numpy`` helper does the same — it returns channel-first and
    # passes it straight to ``separator.separate``).
    model_audio = np.asarray(mix, dtype=np.float32)

    with torch.inference_mode(False):
        # NOTE: do not use a ``with`` block here. The separator may be a shared,
        # cached instance (SeparatorCache) whose lifetime spans the whole run;
        # MSSeparator.__exit__ calls close(), which would destroy the config
        # and break every subsequent node that reuses the cached instance.
        separator = build_separator()
        target_sr = int(getattr(separator, "config", None).audio.get("sample_rate", sample_rate)) if _has_config(separator) else sample_rate
        if target_sr != sample_rate:
            model_audio = _resample(model_audio, sample_rate, target_sr)
            sample_rate = target_sr
        try:
            vr = getattr(separator, "model_type", None) == "vr"
            passes = 2 if vr and getattr(getattr(separator, "model", None), "enable_tta", False) else 1
            separator.progress_callback = _progress_for(ctx, node.id, audio_index=audio_index,
                                                       audio_count=audio_count, unit="batches" if vr else "seconds",
                                                       passes=passes)
        except Exception:  # pragma: no cover - attribute is settable in practice
            pass
        layout_kwargs = {"channel_layout": audio.channel_layout} if audio.channel_layout and model_audio.shape[0] > 2 else {}
        results = separator.separate(model_audio, pbar=False, stems=None, **layout_kwargs)

    return {stem: np.asarray(arr, dtype=np.float32) for stem, arr in results.items()}, sample_rate


def _to_samples_first(channel_first: np.ndarray) -> np.ndarray:
    if channel_first.ndim != 2:
        return np.asarray(channel_first, dtype=np.float32)
    # Stored channel-first; pymss wants samples-first.
    return np.asfortranarray(channel_first.T)


def _has_config(separator: Any) -> bool:
    config = getattr(separator, "config", None)
    return config is not None and hasattr(config, "audio")


def _resample(audio: np.ndarray, source_sr: int, target_sr: int) -> np.ndarray:
    if source_sr == target_sr or audio.size == 0:
        return audio
    # Route through the audio service so a selected provider affects every
    # graph resample path (model prep, ensemble alignment, save conversion).
    # Default selection is the builtin PyAV implementation — byte-identical
    # to the previous direct plugins.builtins.resample call.
    from ..audio_services import default_audio_service

    return default_audio_service().resample(audio, source_sr, target_sr)


def _clean_model_display_name(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    # Strip comfy-mss' ``[category] filename`` annotation prefix.
    import re

    cleaned = re.sub(r"^\[[^\]]+\]\s*", "", text)
    return cleaned.strip()


# ---------------------------------------------------------------------------
# Stems discovery
# ---------------------------------------------------------------------------


def _stems_from_separator(separator: Any) -> list[str]:
    """Return the model's stem names, tolerating config layout differences."""

    config = getattr(separator, "config", None)
    if config is None:
        return []
    training = getattr(config, "training", None)
    target = getattr(training, "target_instrument", None) if training is not None else None
    if target:
        return [target]
    instruments = getattr(training, "instruments", None) if training is not None else None
    if instruments:
        return list(instruments)
    instruments = getattr(config, "instruments", None)
    if instruments:
        return list(instruments)
    audio = getattr(config, "audio", None)
    if audio is not None:
        stems = getattr(audio, "get", lambda *_: None)("instruments")
        if stems:
            return list(stems)
    return []


# ---------------------------------------------------------------------------
# IO nodes
# ---------------------------------------------------------------------------


def _input_audio_signature(node: DAGNode) -> NodeSignature:
    return NodeSignature(
        inputs=[],
        output_names=["audio"],
        output_types=[AUDIO],
    )


def _execute_input_audio(ctx: NodeContext, inputs: dict[str, Any]) -> NodeResult:
    from ..audio_io import load_audio

    if ctx.input_path is None:
        raise DAGError("input_audio node requires an input file (pass input_path to run_dag)")
    mix, sr, layout = load_audio(ctx.input_path, sr=None, mono=False, return_layout=True)
    artifact = AudioArtifact(mix, int(sr), source_path=ctx.input_path, channel_layout=layout)
    return NodeResult(outputs={0: artifact})


register_node("input_audio", signature=_input_audio_signature, execute=_execute_input_audio)


def _load_audio_signature(node: DAGNode) -> NodeSignature:
    return NodeSignature(
        inputs=[PortSpec(name="audio", type="COMBO")],
        output_names=["audio", "audio_name"],
        output_types=[AUDIO, STRING],
    )


def _resolve_load_audio_path(ctx: NodeContext, widget_name: str) -> str:
    """Resolve the audio widget of a load node — no guessing.

    Exactly two ways a load node gets its file:
    1. The widget names an existing path on disk — the graph carries its own
       input.
    2. The host provided it via the ``inputs`` mapping (see ``run_dag``):
       either the node's ``input_name`` widget matched a key (checked by the
       caller before getting here), or — legacy one-slot graphs — the audio
       widget itself is the key.
    Anything else is an error. There is deliberately no placeholder/positional
    fallback: silently substituting a mistyped name or reordering files across
    nodes produces wrong-but-plausible output, which is far worse than a clear
    failure.
    """
    if widget_name and Path(widget_name).is_file():
        return widget_name
    if widget_name and widget_name in ctx.inputs:
        return ctx.inputs[widget_name]
    detail = (
        f"widget {widget_name!r} is not an existing file"
        if widget_name
        else "audio widget is empty"
    )
    raise DAGError(
        f"pymss_load_audio {detail}; set the node's audio widget to a file path, "
        f"or give it an input_name and provide it via run_dag(inputs=...) "
        f"(available: {', '.join(sorted(ctx.inputs)) or 'none'})"
    )


def _execute_load_audio(ctx: NodeContext, inputs: dict[str, Any]) -> NodeResult:
    from ..audio_io import load_audio

    node = ctx_node_of(ctx, "pymss_load_audio")
    widgets_values = node_data(node).get("widgets_values", [])
    widget_name = str(widget(widgets_values, 0, "") or "").strip()
    # comfy-mss >= 1.0.3 appends an optional input_name widget (slot 1): the
    # runtime-input key hosts like pymss-studio use to feed this node.
    input_name = str(widget(widgets_values, 1, "") or "").strip()
    if input_name:
        if input_name in ctx.inputs:
            path = ctx.inputs[input_name]
            mix, sr, layout = load_audio(path, sr=None, mono=False, return_layout=True)
            name = Path(path).stem
            artifact = AudioArtifact(mix, int(sr), source_path=path, channel_layout=layout)
            return NodeResult(outputs={0: artifact, 1: StringArtifact(name)})
        # A named slot that the host did not provide is an explicit error —
        # falling through to positional slots would silently feed the wrong file.
        raise DAGError(
            f"pymss_load_audio declares runtime input {input_name!r} but the host did not provide it "
            f"(available: {', '.join(sorted(ctx.inputs)) or 'none'})"
        )
    path = _resolve_load_audio_path(ctx, widget_name)
    mix, sr, layout = load_audio(path, sr=None, mono=False, return_layout=True)
    name = Path(path).stem
    artifact = AudioArtifact(mix, int(sr), source_path=path, channel_layout=layout)
    return NodeResult(outputs={0: artifact, 1: StringArtifact(name)})


register_node("pymss_load_audio", signature=_load_audio_signature, execute=_execute_load_audio)


def _load_audio_batch_signature(node: DAGNode) -> NodeSignature:
    return NodeSignature(
        inputs=[
            PortSpec(name="folder", type=STRING),
            PortSpec(name="recursive", type="BOOLEAN"),
            PortSpec(name="sort_files", type="BOOLEAN"),
        ],
        output_names=["audio", "audio_name"],
        output_types=[AUDIO, STRING],
        is_list=True,
    )


def _execute_load_audio_batch(ctx: NodeContext, inputs: dict[str, Any]) -> NodeResult:
    from ..audio_io import load_audio

    node = ctx_node_of(ctx, "pymss_load_audio_batch")
    widgets_values = node_data(node).get("widgets_values", [])
    folder = string_value(inputs.get("folder", StringArtifact(""))) or widget(widgets_values, 0, "")
    recursive = _coerce_bool(inputs.get("recursive"), widget(widgets_values, 1, False))
    sort_files = _coerce_bool(inputs.get("sort_files"), widget(widgets_values, 2, True))
    input_name = str(widget(widgets_values, 3, "") or "").strip()

    paths = _scan_audio_folder(str(folder or ""), recursive=recursive, sort_files=sort_files)
    # A named slot (input_name widget, comfy-mss >= 1.0.3) receives the
    # host-provided file keyed by that name; it takes precedence over folder
    # scanning. No positional fallbacks — see _resolve_load_audio_path.
    if input_name:
        if input_name in ctx.inputs:
            paths = [ctx.inputs[input_name]]
        else:
            raise DAGError(
                f"pymss_load_audio_batch declares runtime input {input_name!r} but the host did not provide it "
                f"(available: {', '.join(sorted(ctx.inputs)) or 'none'})"
            )
    if not paths:
        raise DAGError(
            f"pymss_load_audio_batch found no audio files in {folder!r}; set the folder widget "
            "or provide the declared runtime input via run_dag(inputs=...)"
        )

    artifacts: list[AudioArtifact] = []
    names: list[StringArtifact] = []
    for path in paths:
        mix, sr, layout = load_audio(path, sr=None, mono=False, return_layout=True)
        artifacts.append(AudioArtifact(mix, int(sr), source_path=path, channel_layout=layout))
        names.append(StringArtifact(Path(path).stem))
    return NodeResult(outputs={0: artifacts, 1: names})


register_node("pymss_load_audio_batch", signature=_load_audio_batch_signature, execute=_execute_load_audio_batch)


def _save_audio_signature(node: DAGNode) -> NodeSignature:
    return NodeSignature(
        inputs=[
            PortSpec(name="audio", type=AUDIO),
            PortSpec(name="filename", type=STRING, shape=7),
            PortSpec(name="output_format", type="COMBO"),
            PortSpec(name="output_folder", type=STRING),
            PortSpec(name="sample_rate", type="COMBO"),
            PortSpec(name="wav_bit_depth", type="COMBO"),
            PortSpec(name="flac_bit_depth", type="COMBO"),
            PortSpec(name="mp3_bit_rate", type="COMBO"),
        ],
        output_names=[],
        output_types=[],
        is_output_node=True,
    )


def _execute_save_audio(ctx: NodeContext, inputs: dict[str, Any]) -> NodeResult:
    from ..audio_io import save_audio

    node = ctx_node_of(ctx, "pymss_save_audio")
    widgets_values = node_data(node).get("widgets_values", [])

    audio_input = inputs.get("audio")
    if audio_input is None:
        raise DAGError("pymss_save_audio requires an AUDIO input")

    output_format = str(widget(widgets_values, 0, "wav") or "wav").lower()
    # Two widget layouts exist in the wild:
    #   comfy-mss upstream: [format, output_folder, sample_rate, wav_bd, flac_bd, mp3_br]
    #   pymss-studio export: [format, sample_rate, wav_bd, flac_bd, mp3_br]  (no folder)
    # Detect by checking whether widget[1] looks like a sample rate (pure int).
    w1 = str(widget(widgets_values, 1, "") or "")
    folder_from_input = string_value(inputs.get("output_folder", StringArtifact("")))
    if w1.isdigit():
        # pymss-studio layout: no output_folder widget.
        output_folder = folder_from_input
        sample_rate_widget = w1
        wav_bit_depth = str(widget(widgets_values, 2, "FLOAT") or "FLOAT")
        flac_bit_depth = str(widget(widgets_values, 3, "PCM_24") or "PCM_24")
        mp3_bit_rate = str(widget(widgets_values, 4, "320k") or "320k")
    else:
        output_folder = folder_from_input or w1
        sample_rate_widget = str(widget(widgets_values, 2, "") or "")
        wav_bit_depth = str(widget(widgets_values, 3, "FLOAT") or "FLOAT")
        flac_bit_depth = str(widget(widgets_values, 4, "PCM_24") or "PCM_24")
        mp3_bit_rate = str(widget(widgets_values, 5, "320k") or "320k")

    audio_params = {
        **ctx.audio_params,
        "wav_bit_depth": wav_bit_depth,
        "flac_bit_depth": flac_bit_depth,
        "mp3_bit_rate": mp3_bit_rate,
    }

    target_dir = ctx.output_dir
    if output_folder and output_folder.lower() != "default":
        target_dir = ctx.output_dir / safe_filename_part(output_folder)
    target_dir.mkdir(parents=True, exist_ok=True)

    filename_hint = string_value(inputs.get("filename", StringArtifact("")))

    audios = audio_input if isinstance(audio_input, list) else [audio_input]
    saved: list[str] = []
    records: list[DAGOutputRecord] = []
    for index, artifact in enumerate(audios):
        if not isinstance(artifact, AudioArtifact):
            raise DAGError("pymss_save_audio received a non-AUDIO value in its list input")
        sr = int(artifact.sample_rate)
        target_sr = _parse_int(sample_rate_widget, default=sr)
        audio = artifact.audio
        if target_sr and target_sr != sr:
            audio = _resample(audio, sr, target_sr)
            sr = target_sr
        # save_audio expects samples-first [samples, channels] (librosa style).
        save_audio_array = _to_samples_first(audio) if audio.shape[0] <= audio.shape[1] else audio
        if save_audio_array.ndim == 1:
            save_audio_array = save_audio_array[:, None]
        name = _build_save_filename(filename_hint, artifact, index, len(audios), output_format, prefix=ctx.name_prefix)
        target = target_dir / name
        save_audio(str(target), np.asfortranarray(save_audio_array), sr, output_format, audio_params)
        saved_path = str(target)
        saved.append(saved_path)
        records.append(
            DAGOutputRecord(
                path=saved_path,
                stem=artifact.stem_name or "",
                node_id=ctx.current_node_id,
                node_type="pymss_save_audio",
                sample_rate=sr,
                format=output_format,
                source_path=str(artifact.source_path or ""),
            )
        )
    return NodeResult(outputs={}, saved_paths=saved, saved_records=records)


def _build_save_filename(hint: str, artifact: AudioArtifact, index: int, total: int, ext: str, *, prefix: str = "") -> str:
    if hint:
        base = safe_filename_part(hint)
    elif artifact.stem_name:
        base = safe_filename_part(artifact.stem_name)
    elif artifact.source_path:
        base = safe_filename_part(Path(artifact.source_path).stem)
    else:
        base = "audio"
    if prefix:
        base = f"{safe_filename_part(prefix)}_{base}"
    if total > 1:
        base = f"{base}_{index + 1}"
    return f"{base}.{ext}"


def _parse_int(value: Any, *, default: int) -> int:
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        return default


register_node("pymss_save_audio", signature=_save_audio_signature, execute=_execute_save_audio)


# ---------------------------------------------------------------------------
# Params nodes
# ---------------------------------------------------------------------------


def _mss_params_signature(node: DAGNode) -> NodeSignature:
    return NodeSignature(
        inputs=[],
        output_names=["mss_params"],
        output_types=[MSS_PARAMS],
    )


def _execute_mss_params(ctx: NodeContext, inputs: dict[str, Any]) -> NodeResult:
    widgets_values = node_data(ctx_node_of(ctx, "pymss_mss_params")).get("widgets_values", [])
    batch_size = int(widget(widgets_values, 0, 1) or 1)
    overlap = parse_default_int(widget(widgets_values, 1, "Default"), "overlap_size")
    chunk = parse_default_int(widget(widgets_values, 2, "Default"), "chunk_size")
    normalize = _coerce_bool(None, widget(widgets_values, 3, False))
    enable_tta = _coerce_bool(None, widget(widgets_values, 4, False))
    standardize = _coerce_bool(None, widget(widgets_values, 5, False))

    params: dict[str, Any] = {"batch_size": max(1, batch_size)}
    if overlap is not None:
        params["overlap_size"] = overlap
    if chunk is not None:
        params["chunk_size"] = chunk
    params["normalize"] = bool(normalize)
    params["standardize"] = bool(standardize)
    # enable_tta is consumed by the separator directly; keep it on params so
    # separate nodes can pop it (mirrors comfy-mss ``params_and_tta``).
    params["enable_tta"] = bool(enable_tta)
    return NodeResult(outputs={0: ParamsArtifact(params=params, params_type="mss")})


register_node("pymss_mss_params", signature=_mss_params_signature, execute=_execute_mss_params)


def _vr_params_signature(node: DAGNode) -> NodeSignature:
    return NodeSignature(
        inputs=[],
        output_names=["vr_params"],
        output_types=[VR_PARAMS],
    )


def _execute_vr_params(ctx: NodeContext, inputs: dict[str, Any]) -> NodeResult:
    widgets_values = node_data(ctx_node_of(ctx, "pymss_vr_params")).get("widgets_values", [])
    params = {
        "batch_size": int(widget(widgets_values, 0, 1) or 1),
        "window_size": int(widget(widgets_values, 1, 512) or 512),
        "aggression": int(widget(widgets_values, 2, 5) or 5),
        "enable_tta": _coerce_bool(None, widget(widgets_values, 3, False)),
        "high_end_process": _coerce_bool(None, widget(widgets_values, 4, False)),
        "enable_post_process": _coerce_bool(None, widget(widgets_values, 5, False)),
        "post_process_threshold": float(widget(widgets_values, 6, 0.2) or 0.2),
        "normalize": _coerce_bool(None, widget(widgets_values, 7, False)),
        "use_amp": True,
    }
    return NodeResult(outputs={0: ParamsArtifact(params=params, params_type="vr")})


register_node("pymss_vr_params", signature=_vr_params_signature, execute=_execute_vr_params)


# ---------------------------------------------------------------------------
# Separation nodes
# ---------------------------------------------------------------------------


def _separate_signature_factory(max_stems: int, *, is_list: bool):
    def _make(node: DAGNode) -> NodeSignature:
        return NodeSignature(
            inputs=[
                PortSpec(name="audio", type=AUDIO),
                PortSpec(name="params", type=MSS_PARAMS, shape=7),
            ],
            output_names=None if is_list else None,  # dynamic; resolved at execute time
            output_types=None,
            is_list=is_list,
        )

    return _make


def _resolve_stems_for_node(node: DAGNode, ctx: NodeContext, *, kind: str) -> list[str]:
    """Stem names declared on the node (from comfy output port labels)."""

    if node.type.removeprefix("pymss_").endswith("_list"):
        return []
    raw = node_data(node).get("outputs", [])
    stems: list[str] = []
    for output in raw:
        name = str(output.get("name") or output.get("label") or output.get("localized_name") or "").strip()
        otype = str(output.get("type") or "").upper()
        if otype != AUDIO or not name:
            continue
        stem = _strip_stem_suffix(name)
        if stem:
            stems.append(stem)
    return stems


def _strip_stem_suffix(name: str) -> str:
    import re

    text = name.strip()
    if not text:
        return ""
    return re.sub(r"\s*\((audio|string)\)\s*$", "", text, flags=re.IGNORECASE).strip()


def _pop_tta(params_artifact: ParamsArtifact | None) -> tuple[dict[str, Any], bool]:
    if params_artifact is None:
        return {}, False
    params = dict(params_artifact.params)
    use_tta = bool(params.pop("enable_tta", False))
    return params, use_tta


def _validate_params(params_artifact: Any, expected_type: str, node_id: object) -> ParamsArtifact:
    if params_artifact is None:
        return ParamsArtifact(params={}, params_type=expected_type)
    if not isinstance(params_artifact, ParamsArtifact):
        raise DAGError(f"node {node_id!r} params input is not a params object")
    if params_artifact.params_type != expected_type:
        raise DAGError(
            f"node {node_id!r} expects {expected_type} params but got {params_artifact.params_type}"
        )
    return params_artifact


def _make_model_separator_factory(ctx: NodeContext, node: DAGNode, *, kind: str, params: dict[str, Any], use_tta: bool, device: str | None, device_ids_raw: Any, debug: bool, stems: list[str]):
    widgets_values = node_data(node).get("widgets_values", [])
    model_name = _clean_model_display_name(str(widget(widgets_values, 0, "") or ""))
    download_missing = _coerce_bool(None, widget(widgets_values, 3 if kind != "custom" else None, True)) if kind != "custom" else True
    source = str(widget(widgets_values, 4 if kind == "mss" or kind == "vr" else None, "") or "")
    if not source:
        source = ctx.source or "modelscope"
    if kind == "vr":
        # vr widgets: [model, device, download_missing, source, device_ids, debug, ...]
        download_missing = _coerce_bool(None, widget(widgets_values, 3, True))
        widget_source = str(widget(widgets_values, 4, "") or "")
        source = widget_source or ctx.source or "modelscope"

    def _factory():
        separator_kwargs = _common_separator_kwargs(
            ctx,
            device=device,
            device_ids_raw=device_ids_raw,
            params=params,
            use_tta=use_tta,
            debug=debug,
            stems=stems,
        )
        key_kwargs = dict(separator_kwargs)
        key_kwargs.update(
            {
                "model_name": model_name,
                "model_dir": str(ctx.model_dir) if ctx.model_dir is not None else None,
                "download": bool(ctx.download and download_missing),
                "source": ctx.source,
            }
        )
        separator = ctx.separator_cache.get(
            model_name=model_name,
            model_dir=str(ctx.model_dir) if ctx.model_dir is not None else None,
            download=bool(ctx.download and download_missing),
            source=ctx.source,
            endpoint=ctx.endpoint,
            **separator_kwargs,
        )
        return separator

    return _factory


def _make_custom_separator_factory(ctx: NodeContext, node: DAGNode, *, model_type: str, params: dict[str, Any], use_tta: bool, device: str | None, device_ids_raw: Any, debug: bool, stems: list[str]):
    data = node_data(node)
    if data.get("model_path"):
        entry = {"model_path": data["model_path"], "config_path": data.get("config_path")}
    else:
        widgets_values = data.get("widgets_values", [])
        model_name = str(widget(widgets_values, 0, "") or "").strip()
        if not model_name:
            raise DAGError("custom_mss_separate requires a model_name or model_path")

        entry = _resolve_user_model(model_name)
        if entry is None:
            raise DAGError(f"custom model not found or missing yaml: {model_name}")

    def _factory():
        separator_kwargs = _common_separator_kwargs(
            ctx,
            device=device,
            device_ids_raw=device_ids_raw,
            params=params,
            use_tta=use_tta,
            debug=debug,
            stems=stems,
        )
        return ctx.separator_cache.get(
            model_type=model_type,
            model_path=entry["model_path"],
            config_path=entry["config_path"],
            **separator_kwargs,
        )

    return _factory


def _execute_separate(kind: str, *, is_list: bool):
    expected_params_type = "vr" if kind == "vr" else "mss"

    def _execute(ctx: NodeContext, inputs: dict[str, Any]) -> NodeResult:
        node = ctx_node_of_type(ctx, kind, is_list=is_list)
        widgets_values = node_data(node).get("widgets_values", [])
        is_custom = kind == "custom"
        device_idx = 2 if is_custom else 1
        device = str(widget(widgets_values, device_idx, "auto") or "auto")
        device_ids_raw = widget(widgets_values, device_idx + 1 if is_custom else 4, "0")
        debug = _coerce_bool(None, widget(widgets_values, -1, False))

        params_artifact = _validate_params(inputs.get("params"), expected_params_type, node.id)
        params, use_tta = _pop_tta(params_artifact)

        audio_input = inputs.get("audio")
        if audio_input is None:
            raise DAGError(f"node {node.id!r} requires an audio input")
        audios = audio_input if isinstance(audio_input, list) else [audio_input]

        declared_stems = _resolve_stems_for_node(node, ctx, kind=kind)
        outputs: dict[int, Any] = {}
        saved: list[str] = []

        if is_custom:
            model_type = str(widget(widgets_values, 1, "mel_band_roformer") or "mel_band_roformer")
            factory = _make_custom_separator_factory(
                ctx, node, model_type=model_type, params=params, use_tta=use_tta, device=device, device_ids_raw=device_ids_raw, debug=debug, stems=declared_stems,
            )
        else:
            factory = _make_model_separator_factory(
                ctx, node, kind=kind, params=params, use_tta=use_tta, device=device, device_ids_raw=device_ids_raw, debug=debug, stems=declared_stems,
            )

        # We need stems before building; resolve once from a throwaway load if
        # the comfy port labels were empty (e.g. *_list nodes, or unconfigured
        # custom nodes).
        stems_for_run = list(declared_stems)
        if not stems_for_run:
            separator = factory()
            stems_for_run = _stems_from_separator(separator) or ["output"]

        all_audio_results: list[AudioArtifact] = []
        all_stem_names: list[StringArtifact] = []
        for audio_index, audio in enumerate(audios):
            ctx.report_progress(node, audio_index / len(audios), f"node={node.id} Processing audio",
                                audio_index=audio_index + 1, audio_count=len(audios))
            results, output_sample_rate = _run_separation(ctx, node, audio, build_separator=factory, stems=stems_for_run,
                                                        audio_index=audio_index, audio_count=len(audios))
            claimed: set[str] = set()
            for stem in stems_for_run:
                value = results.get(stem)
                if value is None:
                    # Case-insensitive fallback, mirroring comfy-mss
                    # ``collect_stem_outputs``: the port label a user draws in
                    # ComfyUI may not match the model's exact stem casing
                    # (e.g. "vocals" vs "Vocals"). Match on lowercased names.
                    value = next((v for k, v in results.items() if k.lower() == stem.lower()), None)
                if value is None and _is_placeholder_stem(stem):
                    # Editor placeholder ports (stem_1/stem_2/...) mean "the
                    # next unclaimed output"; assign remaining produced stems
                    # in production order.
                    value = next((v for k, v in results.items() if k not in claimed), None)
                if value is None:
                    raise DAGError(
                        f"node {node.id!r} declared stem {stem!r} but model produced "
                        f"only {sorted(results.keys())}"
                    )
                if value is not None:
                    claimed.add(next((k for k, v in results.items() if v is value), stem))
                artifact = numpy_to_audio(np.asarray(value, dtype=np.float32), output_sample_rate, stem_name=stem, source_path=audio.source_path)
                all_audio_results.append(artifact)
                all_stem_names.append(StringArtifact(stem))
            ctx.report_progress(node, (audio_index + 1) / len(audios), f"node={node.id} Audio processing completed",
                                audio_index=audio_index + 1, audio_count=len(audios))

        if is_list:
            outputs[0] = all_audio_results
            outputs[1] = all_stem_names
        else:
            for slot, artifact in enumerate(all_audio_results):
                outputs[slot * 2] = artifact
            for slot, name in enumerate(all_stem_names):
                outputs[slot * 2 + 1] = name
        return NodeResult(outputs=outputs, saved_paths=saved)

    return _execute


for _kind, _is_list in [
    ("mss", False),
    ("mss", True),
    ("vr", False),
    ("vr", True),
    ("custom", False),
    ("custom", True),
]:
    _type = {
        ("mss", False): "mss_separate",
        ("mss", True): "mss_separate_list",
        ("vr", False): "vr_separate",
        ("vr", True): "vr_separate_list",
        ("custom", False): "custom_mss_separate",
        ("custom", True): "custom_mss_separate_list",
    }[(_kind, _is_list)]
    register_node(
        _type,
        signature=_separate_signature_factory(MSS_MAX_STEMS if _kind != "vr" else VR_MAX_STEMS, is_list=_is_list),
        execute=_execute_separate(_kind, is_list=_is_list),
    )
    # comfy-mss upstream uses bare names (mss_separate, vr_separate, ...); some
    # exported graphs carry a ``pymss_`` prefix. Register both so either runs.
    register_alias(f"pymss_{_type}", _type)


# ---------------------------------------------------------------------------
# Audio tool nodes
# ---------------------------------------------------------------------------


def _invert_phase_signature(node: DAGNode) -> NodeSignature:
    return NodeSignature(inputs=[PortSpec(name="a", type=AUDIO)], output_names=["-a"], output_types=[AUDIO])


def _execute_invert_phase(ctx: NodeContext, inputs: dict[str, Any]) -> NodeResult:
    audio = inputs.get("a")
    if not isinstance(audio, AudioArtifact):
        raise DAGError("pymss_audio_invert_phase requires an AUDIO input")
    inverted = ctx.require("invert_phase")(audio.audio)
    artifact = AudioArtifact(inverted, audio.sample_rate, source_path=audio.source_path,
                             stem_name=audio.stem_name, channel_layout=audio.channel_layout)
    return NodeResult(outputs={0: artifact})


register_node("pymss_audio_invert_phase", signature=_invert_phase_signature, execute=_execute_invert_phase)


def _normalize_signature(node: DAGNode) -> NodeSignature:
    return NodeSignature(inputs=[PortSpec(name="audio", type=AUDIO)], output_names=["audio"], output_types=[AUDIO])


def _execute_normalize(ctx: NodeContext, inputs: dict[str, Any]) -> NodeResult:
    audio = inputs.get("audio")
    if not isinstance(audio, AudioArtifact):
        raise DAGError("pymss_audio_normalize requires an AUDIO input")
    # Consume the built-in normalize_peak capability. comfy-mss "normalize" only
    # acts when peak > 1.0 (anti-clip), so we gate it here to preserve semantics.
    waveform = audio.audio
    peak = float(np.abs(waveform).max()) if waveform.size else 0.0
    if peak > 1.0:
        waveform = ctx.require("normalize_peak")(waveform, target_peak=0.999)
    artifact = AudioArtifact(waveform, audio.sample_rate, source_path=audio.source_path,
                             stem_name=audio.stem_name, channel_layout=audio.channel_layout)
    return NodeResult(outputs={0: artifact})


register_node("pymss_audio_normalize", signature=_normalize_signature, execute=_execute_normalize)


def _ensemble_signature(node: DAGNode) -> NodeSignature:
    # Input count is widget-driven; declare enough slots so gather maps links to
    # the right ``audio_N`` names. We cap at the comfy-mss max of 10.
    widgets_values = node.data.get("widgets_values") or []
    try:
        count = max(1, min(10, int(widgets_values[0] if widgets_values else 2) or 2))
    except (TypeError, ValueError):
        count = 2
    return NodeSignature(
        inputs=[PortSpec(name=f"audio_{i + 1}", type=AUDIO) for i in range(count)],
        output_names=["audio"],
        output_types=[AUDIO],
    )


def _execute_ensemble(ctx: NodeContext, inputs: dict[str, Any]) -> NodeResult:
    # Consume the built-in ensemble capability (registered from ensemble.average_waveforms).
    average_waveforms = ctx.require("ensemble")

    node = ctx_node_of(ctx, "pymss_audio_ensemble")
    widgets_values = node_data(node).get("widgets_values", [])
    input_count = max(2, min(10, int(widget(widgets_values, 0, 2) or 2)))
    ensemble_type = str(widget(widgets_values, 1, "avg_wave") or "avg_wave")
    raw_weights = [widget(widgets_values, 2 + i, 1) for i in range(input_count)]
    weights = np.asarray([float(w) for w in raw_weights], dtype=np.float32)

    audios: list[AudioArtifact] = []
    for i in range(1, input_count + 1):
        audio = inputs.get(f"audio_{i}")
        if not isinstance(audio, AudioArtifact):
            raise DAGError(f"pymss_audio_ensemble missing audio_{i}")
        audios.append(audio)

    sample_rate = audios[0].sample_rate
    aligned: list[np.ndarray] = []
    for audio in audios:
        arr = audio.audio
        if audio.sample_rate != sample_rate:
            arr = _resample(arr, audio.sample_rate, sample_rate)
        aligned.append(arr)
    channel_counts = {a.shape[0] for a in aligned}
    if max(channel_counts) > 2 and len(channel_counts) > 1:
        raise DAGError("Ensemble requires matching multichannel audio channel counts.")
    min_channels = min(channel_counts)
    layout = _combined_channel_layout(audios, min_channels)
    min_samples = min(a.shape[1] for a in aligned)
    aligned = [a[:min_channels, :min_samples] for a in aligned]
    stacked = np.stack(aligned, axis=0)
    result = average_waveforms(stacked, weights=weights, algorithm=ensemble_type)
    artifact = AudioArtifact(np.ascontiguousarray(result), sample_rate, channel_layout=layout)
    return NodeResult(outputs={0: artifact})


register_node("pymss_audio_ensemble", signature=_ensemble_signature, execute=_execute_ensemble)


# ---------------------------------------------------------------------------
# ComfyUI built-in passthrough nodes
# ---------------------------------------------------------------------------


def _preview_audio_signature(node: DAGNode) -> NodeSignature:
    return NodeSignature(inputs=[PortSpec(name="audio", type=AUDIO)], output_names=[], output_types=[])


def _execute_preview_audio(ctx: NodeContext, inputs: dict[str, Any]) -> NodeResult:
    # No-op: comfy-mss uses PreviewAudio purely for in-UI playback. We surface
    # the artifact on the context so a future host can collect previews, but we
    # do not write anything to disk.
    return NodeResult(outputs={})


register_node("PreviewAudio", signature=_preview_audio_signature, execute=_execute_preview_audio)


def _string_concat_signature(node: DAGNode) -> NodeSignature:
    return NodeSignature(
        inputs=[
            PortSpec(name="string_a", type=STRING),
            PortSpec(name="string_b", type=STRING),
            PortSpec(name="delimiter", type=STRING),
        ],
        output_names=["STRING"],
        output_types=[STRING],
    )


def _execute_string_concat(ctx: NodeContext, inputs: dict[str, Any]) -> NodeResult:
    node = ctx_node_of(ctx, "StringConcatenate")
    widgets_values = node_data(node).get("widgets_values", [])
    a = string_value(inputs.get("string_a")) or str(widget(widgets_values, 0, "") or "")
    b = string_value(inputs.get("string_b")) or str(widget(widgets_values, 1, "") or "")
    delimiter = string_value(inputs.get("delimiter")) or str(widget(widgets_values, 2, "") or "")
    if a and b:
        joined = f"{a}{delimiter}{b}"
    else:
        joined = f"{a}{b}"
    return NodeResult(outputs={0: StringArtifact(joined)})


register_node("StringConcatenate", signature=_string_concat_signature, execute=_execute_string_concat)


# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------


def node_data(node: DAGNode) -> dict[str, Any]:
    return node.data


def ctx_node_of(ctx: NodeContext, node_type: str) -> DAGNode:
    """Find the currently-executing node of ``node_type``.

    The executor passes the node's own data via ``ctx``-less design; we instead
    stash the current node id on the context before each execute call. See
    ``run_dag`` — it sets ``ctx.current_node``.
    """

    node_id = getattr(ctx, "current_node_id", None)
    if node_id is None:
        raise DAGError(f"cannot resolve {node_type} node: no current node on context")
    node = ctx.nodes_by_id.get(node_id)
    if node is None:
        raise DAGError(f"cannot resolve {node_type} node: id {node_id!r} not in graph")
    return node


def ctx_node_of_type(ctx: NodeContext, kind: str, *, is_list: bool) -> DAGNode:
    node_id = getattr(ctx, "current_node_id", None)
    if node_id is None:
        raise DAGError("cannot resolve current separation node")
    return ctx.nodes_by_id[node_id]


def _coerce_bool(explicit: Any, fallback: bool) -> bool:
    if explicit is None:
        return bool(fallback)
    if isinstance(explicit, (bool,)):
        return explicit
    if isinstance(explicit, StringArtifact):
        return explicit.value.lower() in {"true", "1", "yes"}
    return bool(explicit)


def _scan_audio_folder(folder: str, *, recursive: bool, sort_files: bool) -> list[str]:
    root = Path(folder).expanduser()
    if not root.is_dir():
        return []
    extensions = _AUDIO_EXTENSIONS
    paths: list[str] = []
    if recursive:
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in extensions:
                paths.append(str(path))
    else:
        for path in sorted(root.iterdir()):
            if path.is_file() and path.suffix.lower() in extensions:
                paths.append(str(path))
    if sort_files:
        paths.sort()
    return paths


_AUDIO_EXTENSIONS = frozenset(
    {".wav", ".flac", ".mp3", ".m4a", ".ogg", ".aac", ".aiff", ".aif", ".wma", ".opus"}
)


def _resolve_user_model(name: str):
    """Find a user-registered model entry by name or alias.

    Returns a dict with ``model_path`` / ``config_path`` / ``model_type``, or
    ``None`` when no user model matches.
    """

    from ..user_models import list_user_models

    needle = name.strip().lower()
    for entry in list_user_models():
        candidates = [entry.name, *(entry.aliases or ())]
        if any(str(c).strip().lower() == needle for c in candidates):
            return {
                "model_path": entry.model_path,
                "config_path": entry.config_path,
                "model_type": entry.model_type or entry.architecture,
            }
    return None


__all__ = [
    "CUSTOM_MODEL_TYPES",
    "MSS_MAX_STEMS",
    "OUTPUT_NODE_TYPES",
    "VR_MAX_STEMS",
]
