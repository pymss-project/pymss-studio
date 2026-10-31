"""Built-in ComfyUI node executors reimplemented on pymss primitives.

These mirror the audio and string nodes shipped with ComfyUI
(``comfy_extras/nodes_audio.py``, ``comfy_extras/nodes_string.py``) so that
graphs authored in ComfyUI run unchanged here. Only nodes useful to MSS
workflows are included — VAE/latent/generation nodes are intentionally omitted.

Audio nodes operate on :class:`~pymss.graph.core.AudioArtifact` (channel-first
``[channels, samples]`` float32), matching the storage convention used by the
comfy-mss nodes in :mod:`pymss.graph.nodes`.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .core import (
    AUDIO,
    AudioArtifact,
    DAGError,
    DAGNode,
    DAGOutputRecord,
    NodeContext,
    NodeResult,
    NodeSignature,
    PortSpec,
    STRING,
    StringArtifact,
    _combined_channel_layout,
    audio_to_numpy,
    numpy_to_audio,
    register_node,
    safe_filename_part,
    string_value,
    widget,
)


OUTPUT_NODE_TYPES: set[str] = set()


# ---------------------------------------------------------------------------
# ComfyUI native IO nodes (LoadAudio / SaveAudio / SaveAudioMP3 / SaveAudioOpus)
# ---------------------------------------------------------------------------
#
# These are deprecated in ComfyUI in favor of SaveAudioAdvanced, but real-world
# graphs still use them. We make them behave like the pymss equivalents:
# ``LoadAudio`` reads ``ctx.input_path`` (same as pymss_load_audio), and the
# ``SaveAudio*`` nodes write via pymss' save_audio with a format-appropriate
# bitrate. They are registered as aliases where the widget layout matches; the
# only real divergence is that ComfyUI ``SaveAudio*`` take ``filename_prefix``
# rather than a per-file ``output_folder``, which we map onto the output dir.


# LoadAudio behaves identically to pymss_load_audio (both load one file from
# ctx.input_path and emit AUDIO + name). Register it as an alias.
def _register_native_io_aliases() -> None:
    """Alias ComfyUI native IO node types to the pymss equivalents."""

    from .core import register_alias
    from ..plugins.registry import _REGISTRY as _PLUGIN_REGISTRY

    if "pymss_load_audio" in _PLUGIN_REGISTRY.nodes and "LoadAudio" not in _PLUGIN_REGISTRY.nodes:
        register_alias("LoadAudio", "pymss_load_audio")


def _save_audio_signature(node: DAGNode) -> NodeSignature:
    return NodeSignature(
        inputs=[PortSpec(name="audio", type=AUDIO), PortSpec(name="filename_prefix", type=STRING)],
        output_names=[], output_types=[], is_output_node=True,
    )


def _save_audio_mp3_signature(node: DAGNode) -> NodeSignature:
    return NodeSignature(
        inputs=[PortSpec(name="audio", type=AUDIO), PortSpec(name="filename_prefix", type=STRING), PortSpec(name="quality", type="COMBO")],
        output_names=[], output_types=[], is_output_node=True,
    )


def _make_save_executor(fmt: str):
    def _execute(ctx: NodeContext, inputs: dict[str, Any]) -> NodeResult:
        return _save_with_prefix(ctx, inputs, fmt)
    return _execute


register_node("SaveAudio", signature=_save_audio_signature, execute=_make_save_executor("flac"))
register_node("SaveAudioMP3", signature=_save_audio_mp3_signature, execute=_make_save_executor("mp3"))
register_node("SaveAudioOpus", signature=_save_audio_mp3_signature, execute=_make_save_executor("opus"))


def _save_audio_advanced_signature(node: DAGNode) -> NodeSignature:
    return NodeSignature(
        inputs=[
            PortSpec(name="audio", type=AUDIO),
            PortSpec(name="filename_prefix", type=STRING),
            PortSpec(name="format", type="COMBO"),
            PortSpec(name="quality", type="COMBO"),
        ],
        output_names=["audio"],
        output_types=[AUDIO],
        is_output_node=True,
    )


# Quality presets -> codec keyword value mapping. V0 is LAME's variable
# bitrate; pymss's mp3 encoder takes a fixed bit_rate, so V0 maps to 320k as a
# high-quality approximation. flac has no quality knob.
_MP3_QUALITY = {"V0": "320k", "128k": "128k", "320k": "320k"}
_OPUS_QUALITY = {"64k": "64k", "96k": "96k", "128k": "128k", "192k": "192k", "320k": "320k"}


def _execute_save_audio_advanced(ctx: NodeContext, inputs: dict[str, Any]) -> NodeResult:
    """ComfyUI ``SaveAudioAdvanced`` — one node for flac/mp3/opus with quality.

    This is ComfyUI's consolidated save node (CORE-202) that is deprecating the
    per-format SaveAudio/SaveAudioMP3/SaveAudioOpus nodes. The format is chosen
    via a widget rather than a separate node type. We dispatch through the
    codec capability pool.
    """

    from ..audio_io import save_audio

    audio_input = inputs.get("audio")
    if audio_input is None:
        raise DAGError("SaveAudioAdvanced requires an AUDIO input")
    node = ctx.nodes_by_id[ctx.current_node_id]
    widgets = node.data.get("widgets_values", [])
    prefix = (
        string_value(inputs.get("filename_prefix", StringArtifact("")))
        or str(widget(widgets, 0, "audio/ComfyUI") or "audio/ComfyUI")
    )
    # The format widget is a DynamicCombo; exported as [format_name, quality] in
    # widgets_values (index 1 = format, index 2 = quality), but some front-ends
    # pack it as a nested dict/list. Handle both.
    raw_format = widget(widgets, 1, "flac")
    raw_quality = widget(widgets, 2, "")
    if isinstance(raw_format, (list, tuple)) and raw_format:
        # Nested form: [format_name, quality]
        fmt_name = str(raw_format[0]).lower()
        quality = str(raw_format[1]) if len(raw_format) > 1 else ""
    elif isinstance(raw_format, dict):
        fmt_name = str(raw_format.get("format", "flac")).lower()
        quality = str(raw_format.get("quality", "") or raw_quality or "")
    else:
        fmt_name = str(raw_format).lower() or "flac"
        quality = str(raw_quality or "")

    # Build codec params from the quality preset.
    audio_params = dict(ctx.audio_params)
    if fmt_name == "mp3":
        audio_params["mp3_bit_rate"] = _MP3_QUALITY.get(quality, "320k")
    elif fmt_name == "opus":
        # pymss's opus encoder (soundfile OGG/OPUS) uses libsndfile's default
        # bitrate; we honor the chosen value via the m4a-style bit_rate key only
        # if a path uses it. soundfile ignores unknown keys, so this is safe.
        audio_params["opus_bit_rate"] = _OPUS_QUALITY.get(quality, "128k")

    # Resolve output path from the filename_prefix.
    parts = prefix.replace("\\", "/").split("/")
    folder = safe_filename_part("/".join(parts[:-1])) if len(parts) > 1 else ""
    stem = safe_filename_part(parts[-1]) if parts[-1] else "audio"
    target_dir = ctx.output_dir / folder if folder else ctx.output_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    audios = audio_input if isinstance(audio_input, list) else [audio_input]
    saved: list[str] = []
    records: list[DAGOutputRecord] = []
    for i, art in enumerate(audios):
        if not isinstance(art, AudioArtifact):
            raise DAGError("SaveAudioAdvanced received non-AUDIO value")
        suffix = f"_{i + 1}" if len(audios) > 1 else ""
        path = target_dir / f"{stem}{suffix}.{fmt_name}"
        arr = art.audio
        save_arr = arr.T if arr.ndim == 2 else arr[:, None]
        save_audio(str(path), np.asfortranarray(save_arr), art.sample_rate, fmt_name, audio_params)
        saved_path = str(path)
        saved.append(saved_path)
        records.append(
            DAGOutputRecord(
                path=saved_path,
                stem=art.stem_name or "",
                node_id=ctx.current_node_id,
                node_type="SaveAudioAdvanced",
                sample_rate=art.sample_rate,
                format=fmt_name,
                source_path=str(art.source_path or ""),
            )
        )
    # Pass the audio through (output slot 0) so downstream nodes can use it.
    return NodeResult(outputs={0: audio_input}, saved_paths=saved, saved_records=records)


register_node("SaveAudioAdvanced", signature=_save_audio_advanced_signature, execute=_execute_save_audio_advanced)
OUTPUT_NODE_TYPES.update({"SaveAudio", "SaveAudioMP3", "SaveAudioOpus", "SaveAudioAdvanced"})


def _save_with_prefix(ctx: NodeContext, inputs: dict[str, Any], fmt: str) -> NodeResult:
    """Shared executor for ComfyUI ``SaveAudio`` family.

    ComfyUI stores a ``filename_prefix`` widget (e.g. ``"audio/ComfyUI"``) and
    appends a counter. We honor the prefix as a subfolder + filename stem.
    All formats (wav/flac/mp3/m4a/aac/opus/vorbis/ogg) are supported natively via
    the codec capability pool; opus is no longer silently remapped.
    """

    from ..audio_io import save_audio

    audio_input = inputs.get("audio")
    if audio_input is None:
        raise DAGError("SaveAudio requires an AUDIO input")
    node = ctx.nodes_by_id[ctx.current_node_id]
    widgets = node.data.get("widgets_values", [])
    prefix = string_value(inputs.get("filename_prefix", StringArtifact(""))) or str(widget(widgets, 0, "audio") or "audio")
    quality = str(widget(widgets, 1, "") or "")

    target_fmt = fmt
    audio_params = dict(ctx.audio_params)
    if fmt == "mp3" and quality in {"128k", "320k"}:
        audio_params["mp3_bit_rate"] = quality

    parts = prefix.replace("\\", "/").split("/")
    folder = safe_filename_part("/".join(parts[:-1])) if len(parts) > 1 else ""
    stem = safe_filename_part(parts[-1]) if parts[-1] else "audio"

    target_dir = ctx.output_dir / folder if folder else ctx.output_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    audios = audio_input if isinstance(audio_input, list) else [audio_input]
    saved: list[str] = []
    records: list[DAGOutputRecord] = []
    for i, art in enumerate(audios):
        if not isinstance(art, AudioArtifact):
            raise DAGError("SaveAudio received non-AUDIO value")
        suffix = f"_{i + 1}" if len(audios) > 1 else ""
        path = target_dir / f"{stem}{suffix}.{target_fmt}"
        arr = art.audio
        save_arr = arr.T if arr.ndim == 2 else arr[:, None]
        save_audio(str(path), np.asfortranarray(save_arr), art.sample_rate, target_fmt, audio_params)
        saved_path = str(path)
        saved.append(saved_path)
        records.append(
            DAGOutputRecord(
                path=saved_path,
                stem=art.stem_name or "",
                node_id=ctx.current_node_id,
                node_type="SaveAudio",
                sample_rate=art.sample_rate,
                format=target_fmt,
                source_path=str(art.source_path or ""),
            )
        )
    return NodeResult(outputs={}, saved_paths=saved, saved_records=records)


# ---------------------------------------------------------------------------
# Audio helpers shared by concat/merge/join
# ---------------------------------------------------------------------------


def _audio_waveform(audio: Any) -> tuple[np.ndarray, int]:
    arr, sr = audio_to_numpy(audio)
    return np.asarray(arr, dtype=np.float32), int(sr)


# Reuse the resample helper from nodes (avoids duplicating the PyAV-backed
# implementation). AudioMerge needs it to align inputs at different sample rates.
from .nodes import _resample


def _match_sample_rates(a: np.ndarray, sr_a: int, b: np.ndarray, sr_b: int) -> tuple[np.ndarray, np.ndarray, int]:
    if sr_a == sr_b:
        return a, b, sr_a
    if sr_a > sr_b:
        return a, _resample(b, sr_b, sr_a), sr_a
    return _resample(a, sr_a, sr_b), b, sr_b


# ---------------------------------------------------------------------------
# TrimAudioDuration
# ---------------------------------------------------------------------------


def _trim_audio_signature(node: DAGNode) -> NodeSignature:
    return NodeSignature(
        inputs=[PortSpec(name="audio", type=AUDIO), PortSpec(name="start_index", type="FLOAT"), PortSpec(name="duration", type="FLOAT")],
        output_names=["audio"],
        output_types=[AUDIO],
    )


def _execute_trim_audio(ctx: NodeContext, inputs: dict[str, Any]) -> NodeResult:
    waveform, sr = _audio_waveform(inputs["audio"])
    widgets = ctx.nodes_by_id[ctx.current_node_id].data.get("widgets_values", [])
    start = float(widget(widgets, 0, 0.0) or 0.0)
    duration = float(widget(widgets, 1, 60.0) or 60.0)

    total = waveform.shape[-1]
    if total == 0:
        return NodeResult(outputs={0: inputs["audio"]})
    start_frame = total + int(round(start * sr)) if start < 0 else int(round(start * sr))
    start_frame = max(0, min(start_frame, total))
    end_frame = max(0, min(start_frame + int(round(duration * sr)), total))
    if start_frame >= end_frame:
        raise DAGError("TrimAudioDuration: start time must be before end time and within audio length")
    return NodeResult(outputs={0: AudioArtifact(
        waveform[..., start_frame:end_frame], sr, channel_layout=inputs["audio"].channel_layout
    )})


register_node("TrimAudioDuration", signature=_trim_audio_signature, execute=_execute_trim_audio)


# ---------------------------------------------------------------------------
# SplitAudioChannels / JoinAudioChannels
# ---------------------------------------------------------------------------


def _split_channels_signature(node: DAGNode) -> NodeSignature:
    return NodeSignature(
        inputs=[PortSpec(name="audio", type=AUDIO)],
        output_names=["left", "right"],
        output_types=[AUDIO, AUDIO],
    )


def _execute_split_channels(ctx: NodeContext, inputs: dict[str, Any]) -> NodeResult:
    waveform, sr = _audio_waveform(inputs["audio"])
    if waveform.ndim != 2 or waveform.shape[0] != 2:
        raise DAGError("SplitAudioChannels requires stereo input (2 channels)")
    return NodeResult(outputs={
        0: AudioArtifact(waveform[0:1, :], sr),
        1: AudioArtifact(waveform[1:2, :], sr),
    })


register_node("SplitAudioChannels", signature=_split_channels_signature, execute=_execute_split_channels)


def _join_channels_signature(node: DAGNode) -> NodeSignature:
    return NodeSignature(
        inputs=[PortSpec(name="audio_left", type=AUDIO), PortSpec(name="audio_right", type=AUDIO)],
        output_names=["audio"],
        output_types=[AUDIO],
    )


def _execute_join_channels(ctx: NodeContext, inputs: dict[str, Any]) -> NodeResult:
    left = inputs.get("audio_left")
    right = inputs.get("audio_right")
    if left is None:
        return NodeResult(outputs={0: right}) if right is not None else NodeResult()
    if right is None:
        return NodeResult(outputs={0: left})
    lw, lsr = _audio_waveform(left)
    rw, rsr = _audio_waveform(right)
    if lw.shape[0] != 1 or rw.shape[0] != 1:
        raise DAGError("JoinAudioChannels: both inputs must be mono")
    lw, rw, out_sr = _match_sample_rates(lw, lsr, rw, rsr)
    min_len = min(lw.shape[-1], rw.shape[-1])
    stereo = np.stack([lw[0, :min_len], rw[0, :min_len]], axis=0)
    return NodeResult(outputs={0: AudioArtifact(stereo, out_sr)})


register_node("JoinAudioChannels", signature=_join_channels_signature, execute=_execute_join_channels)


# ---------------------------------------------------------------------------
# AudioConcat / AudioMerge
# ---------------------------------------------------------------------------


def _audio_concat_signature(node: DAGNode) -> NodeSignature:
    return NodeSignature(
        inputs=[PortSpec(name="audio1", type=AUDIO), PortSpec(name="audio2", type=AUDIO), PortSpec(name="direction", type="COMBO")],
        output_names=["audio"],
        output_types=[AUDIO],
    )


def _execute_audio_concat(ctx: NodeContext, inputs: dict[str, Any]) -> NodeResult:
    a1 = inputs.get("audio1")
    a2 = inputs.get("audio2")
    if a1 is None:
        return NodeResult(outputs={0: a2}) if a2 is not None else NodeResult()
    if a2 is None:
        return NodeResult(outputs={0: a1})
    w1, sr1 = _audio_waveform(a1)
    w2, sr2 = _audio_waveform(a2)
    # ComfyUI upmixes mono to stereo for concat; replicate.
    if w1.shape[0] == 1:
        w1 = np.repeat(w1, 2, axis=0)
    if w2.shape[0] == 1:
        w2 = np.repeat(w2, 2, axis=0)
    if w1.shape[0] != w2.shape[0]:
        raise DAGError("AudioConcat requires matching channel counts after mono-to-stereo conversion.")
    layout = _combined_channel_layout([a1, a2], w1.shape[0])
    w1, w2, out_sr = _match_sample_rates(w1, sr1, w2, sr2)
    direction = string_value(inputs.get("direction", StringArtifact("after")))
    joined = (w1, w2) if direction == "after" else (w2, w1)
    return NodeResult(outputs={0: AudioArtifact(np.concatenate(joined, axis=-1), out_sr, channel_layout=layout)})


register_node("AudioConcat", signature=_audio_concat_signature, execute=_execute_audio_concat)


def _audio_merge_signature(node: DAGNode) -> NodeSignature:
    return NodeSignature(
        inputs=[
            PortSpec(name="audio1", type=AUDIO),
            PortSpec(name="audio2", type=AUDIO),
            PortSpec(name="merge_method", type="COMBO"),
            PortSpec(name="normalize", type="BOOLEAN", shape=7),
        ],
        output_names=["audio"],
        output_types=[AUDIO],
    )


def _execute_audio_merge(ctx: NodeContext, inputs: dict[str, Any]) -> NodeResult:
    """Merge audio with ComfyUI's peak protection enabled by default.

    The optional pymss ``normalize`` input (widget index 1) can disable peak
    scaling for intermediate arithmetic. Normalize the final result before
    integer PCM export if it exceeds full scale.
    """
    from .nodes import _coerce_bool

    a1 = inputs.get("audio1")
    a2 = inputs.get("audio2")
    if a1 is None:
        return NodeResult(outputs={0: a2}) if a2 is not None else NodeResult()
    if a2 is None:
        return NodeResult(outputs={0: a1})
    w1, sr1 = _audio_waveform(a1)
    w2, sr2 = _audio_waveform(a2)
    layout = _combined_channel_layout([a1, a2], max(w1.shape[0], w2.shape[0]))
    w1, w2, out_sr = _match_sample_rates(w1, sr1, w2, sr2)
    len1, len2 = w1.shape[-1], w2.shape[-1]
    if len2 > len1:
        w2 = w2[..., :len1]
    elif len2 < len1:
        pad = np.zeros((w2.shape[0], len1 - len2), dtype=np.float32)
        w2 = np.concatenate([w2, pad], axis=-1)
    widgets = ctx.nodes_by_id[ctx.current_node_id].data.get("widgets_values", [])
    method_value = inputs.get("merge_method")
    if method_value is None:
        method_value = widget(widgets, 0, "add")
    method = string_value(method_value)
    normalize = _coerce_bool(inputs.get("normalize"), widget(widgets, 1, True))
    if method == "add":
        out = w1 + w2
    elif method == "subtract":
        out = w1 - w2
    elif method == "multiply":
        out = w1 * w2
    else:
        out = (w1 + w2) / 2
    if normalize:
        peak = float(np.abs(out).max()) if out.size else 0.0
        if peak > 1.0:
            out = out / peak
    return NodeResult(outputs={0: AudioArtifact(out, out_sr, channel_layout=layout)})


register_node("AudioMerge", signature=_audio_merge_signature, execute=_execute_audio_merge)


# ---------------------------------------------------------------------------
# AudioAdjustVolume
# ---------------------------------------------------------------------------


def _adjust_volume_signature(node: DAGNode) -> NodeSignature:
    return NodeSignature(
        inputs=[PortSpec(name="audio", type=AUDIO), PortSpec(name="volume", type="INT")],
        output_names=["audio"],
        output_types=[AUDIO],
    )


def _execute_adjust_volume(ctx: NodeContext, inputs: dict[str, Any]) -> NodeResult:
    waveform, sr = _audio_waveform(inputs["audio"])
    widgets = ctx.nodes_by_id[ctx.current_node_id].data.get("widgets_values", [])
    volume_db = int(widget(widgets, 0, 0) or 0)
    if volume_db == 0:
        return NodeResult(outputs={0: inputs["audio"]})
    gain = 10 ** (volume_db / 20)
    return NodeResult(outputs={0: AudioArtifact(waveform * gain, sr, channel_layout=inputs["audio"].channel_layout)})


register_node("AudioAdjustVolume", signature=_adjust_volume_signature, execute=_execute_adjust_volume)


# ---------------------------------------------------------------------------
# EmptyAudio
# ---------------------------------------------------------------------------


def _empty_audio_signature(node: DAGNode) -> NodeSignature:
    return NodeSignature(
        inputs=[PortSpec(name="duration", type="FLOAT"), PortSpec(name="sample_rate", type="INT"), PortSpec(name="channels", type="INT")],
        output_names=["audio"],
        output_types=[AUDIO],
    )


def _execute_empty_audio(ctx: NodeContext, inputs: dict[str, Any]) -> NodeResult:
    widgets = ctx.nodes_by_id[ctx.current_node_id].data.get("widgets_values", [])
    duration = float(widget(widgets, 0, 60.0) or 60.0)
    sr = int(widget(widgets, 1, 44100) or 44100)
    channels = int(widget(widgets, 2, 2) or 2)
    n = int(round(duration * sr))
    return NodeResult(outputs={0: AudioArtifact(np.zeros((channels, n), dtype=np.float32), sr)})


register_node("EmptyAudio", signature=_empty_audio_signature, execute=_execute_empty_audio)


# ---------------------------------------------------------------------------
# AudioEqualizer3Band (experimental in ComfyUI; needs torchaudio)
# ---------------------------------------------------------------------------


def _eq_signature(node: DAGNode) -> NodeSignature:
    return NodeSignature(
        inputs=[
            PortSpec(name="audio", type=AUDIO),
            PortSpec(name="low_gain_dB", type="FLOAT"),
            PortSpec(name="low_freq", type="INT"),
            PortSpec(name="mid_gain_dB", type="FLOAT"),
            PortSpec(name="mid_freq", type="INT"),
            PortSpec(name="mid_q", type="FLOAT"),
            PortSpec(name="high_gain_dB", type="FLOAT"),
            PortSpec(name="high_freq", type="INT"),
        ],
        output_names=["audio"],
        output_types=[AUDIO],
    )


def _execute_eq(ctx: NodeContext, inputs: dict[str, Any]) -> NodeResult:
    waveform, sr = _audio_waveform(inputs["audio"])
    if waveform.shape[-1] == 0:
        return NodeResult(outputs={0: inputs["audio"]})
    widgets = ctx.nodes_by_id[ctx.current_node_id].data.get("widgets_values", [])
    low_gain = float(widget(widgets, 0, 0.0) or 0.0)
    low_freq = int(widget(widgets, 1, 100) or 100)
    mid_gain = float(widget(widgets, 2, 0.0) or 0.0)
    mid_freq = int(widget(widgets, 3, 1000) or 1000)
    mid_q = float(widget(widgets, 4, 0.707) or 0.707)
    high_gain = float(widget(widgets, 5, 0.0) or 0.0)
    high_freq = int(widget(widgets, 6, 5000) or 5000)

    # Consume the built-in eq capability (scipy biquad); no torchaudio needed.
    out = ctx.require("eq")(
        waveform, sr,
        low_gain_db=low_gain, low_freq=low_freq,
        mid_gain_db=mid_gain, mid_freq=mid_freq, mid_q=mid_q,
        high_gain_db=high_gain, high_freq=high_freq,
    )
    return NodeResult(outputs={0: AudioArtifact(
        np.asarray(out, dtype=np.float32), sr, channel_layout=inputs["audio"].channel_layout
    )})


register_node("AudioEqualizer3Band", signature=_eq_signature, execute=_execute_eq)


# ===========================================================================
# String nodes (comfy_extras/nodes_string.py)
# ===========================================================================


def _str_in(*names: str) -> list[PortSpec]:
    return [PortSpec(name=n, type=STRING) for n in names]


def _one_str_out() -> NodeSignature:
    return NodeSignature(inputs=[], output_names=["STRING"], output_types=[STRING])


def _w(ctx: NodeContext, idx: int, default: Any = "") -> Any:
    return widget(ctx.nodes_by_id[ctx.current_node_id].data.get("widgets_values", []), idx, default)


# StringConcatenate lives in nodes.py already (comfy-mss depends on it). The
# rest below are pure-string and trivial.


def _sig_concat(node: DAGNode) -> NodeSignature:
    return NodeSignature(inputs=_str_in("string_a", "string_b", "delimiter"), output_names=["STRING"], output_types=[STRING])


def _exec_concat(ctx, inputs):
    a = string_value(inputs.get("string_a")) or str(_w(ctx, 0, ""))
    b = string_value(inputs.get("string_b")) or str(_w(ctx, 1, ""))
    delim = string_value(inputs.get("delimiter")) or str(_w(ctx, 2, ""))
    return NodeResult(outputs={0: StringArtifact(delim.join((a, b)))})


# ComfyUI registers StringConcatenate under this id; nodes.py registers a
# comfy-mss-compatible version. We skip re-registering here to avoid a
# collision — the existing one already handles the filename use case.


def _sig_substring(node):
    return NodeSignature(inputs=_str_in("string") + [PortSpec(name="start", type="INT"), PortSpec(name="end", type="INT")], output_names=["STRING"], output_types=[STRING])


def _exec_substring(ctx, inputs):
    s = string_value(inputs.get("string")) or str(_w(ctx, 0, ""))
    start = int(inputs.get("start", _w(ctx, 1, 0)) if not isinstance(inputs.get("start"), str) else _w(ctx, 1, 0))
    end = int(inputs.get("end", _w(ctx, 2, len(s))) if not isinstance(inputs.get("end"), str) else _w(ctx, 2, len(s)))
    return NodeResult(outputs={0: StringArtifact(s[start:end])})


register_node("StringSubstring", signature=_sig_substring, execute=_exec_substring)


def _sig_replace(node):
    return NodeSignature(inputs=_str_in("string", "find", "replace"), output_names=["STRING"], output_types=[STRING])


def _exec_replace(ctx, inputs):
    s = string_value(inputs.get("string")) or str(_w(ctx, 0, ""))
    find = string_value(inputs.get("find")) or str(_w(ctx, 1, ""))
    rep = string_value(inputs.get("replace")) or str(_w(ctx, 2, ""))
    return NodeResult(outputs={0: StringArtifact(s.replace(find, rep))})


register_node("StringReplace", signature=_sig_replace, execute=_exec_replace)


def _sig_trim(node):
    return NodeSignature(inputs=_str_in("string") + [PortSpec(name="mode", type="COMBO")], output_names=["STRING"], output_types=[STRING])


def _exec_trim(ctx, inputs):
    s = string_value(inputs.get("string")) or str(_w(ctx, 0, ""))
    mode = string_value(inputs.get("mode", StringArtifact("Both"))) or str(_w(ctx, 1, "Both")) or "Both"
    if mode == "Left":
        out = s.lstrip()
    elif mode == "Right":
        out = s.rstrip()
    else:
        out = s.strip()
    return NodeResult(outputs={0: StringArtifact(out)})


register_node("StringTrim", signature=_sig_trim, execute=_exec_trim)


def _sig_case(node):
    return NodeSignature(inputs=_str_in("string") + [PortSpec(name="mode", type="COMBO")], output_names=["STRING"], output_types=[STRING])


def _exec_case(ctx, inputs):
    s = string_value(inputs.get("string")) or str(_w(ctx, 0, ""))
    mode = string_value(inputs.get("mode", StringArtifact("UPPERCASE"))) or str(_w(ctx, 1, "UPPERCASE")) or "UPPERCASE"
    table = {"UPPERCASE": s.upper, "lowercase": s.lower, "Capitalize": s.capitalize, "Title Case": s.title}
    out = table.get(mode, lambda: s)()
    return NodeResult(outputs={0: StringArtifact(out)})


register_node("CaseConverter", signature=_sig_case, execute=_exec_case)


def _sig_format(node):
    # Autogrow inputs are complex; accept a single 'value' plus f_string widget.
    return NodeSignature(inputs=[PortSpec(name="value", type=STRING), PortSpec(name="f_string", type=STRING)], output_names=["STRING"], output_types=[STRING])


def _exec_format(ctx, inputs):
    import string as _string

    fmt = string_value(inputs.get("f_string")) or str(_w(ctx, 0, "{a}"))
    # Gather named value inputs (a, b, c, ...) that were wired in.
    values = {}
    for key in list(inputs.keys()):
        if key in _string.ascii_lowercase or key == "value":
            values[key] = string_value(inputs.get(key))
    # ComfyUI's StringFormat uses {a}/{b}/...; if only 'value' wired, map to {a}.
    if "value" in values and "a" not in values:
        values.setdefault("a", values["value"])
    try:
        return NodeResult(outputs={0: StringArtifact(fmt.format(**{k: v for k, v in values.items() if k in _string.ascii_lowercase}))})
    except (KeyError, IndexError, ValueError):
        return NodeResult(outputs={0: StringArtifact(fmt)})


register_node("StringFormat", signature=_sig_format, execute=_exec_format)


def _sig_regex_replace(node):
    return NodeSignature(inputs=_str_in("string", "regex_pattern", "replace"), output_names=["STRING"], output_types=[STRING])


def _exec_regex_replace(ctx, inputs):
    import re

    s = string_value(inputs.get("string")) or str(_w(ctx, 0, ""))
    pattern = string_value(inputs.get("regex_pattern")) or str(_w(ctx, 1, ""))
    rep = string_value(inputs.get("replace")) or str(_w(ctx, 2, ""))
    try:
        out = re.sub(pattern, rep, s)
    except re.error:
        out = s
    return NodeResult(outputs={0: StringArtifact(out)})


register_node("RegexReplace", signature=_sig_regex_replace, execute=_exec_regex_replace)


def _sig_regex_extract(node):
    return NodeSignature(inputs=_str_in("string", "regex_pattern") + [PortSpec(name="mode", type="COMBO"), PortSpec(name="group_index", type="INT")], output_names=["STRING"], output_types=[STRING])


def _exec_regex_extract(ctx, inputs):
    import re

    s = string_value(inputs.get("string")) or str(_w(ctx, 0, ""))
    pattern = string_value(inputs.get("regex_pattern")) or str(_w(ctx, 1, ""))
    mode = string_value(inputs.get("mode", StringArtifact("First Match"))) or str(_w(ctx, 2, "First Match")) or "First Match"
    group_index = int(_w(ctx, 3, 1) or 1)
    try:
        if mode == "First Match":
            m = re.search(pattern, s)
            out = m.group(0) if m else ""
        elif mode == "All Matches":
            ms = re.findall(pattern, s)
            if ms and isinstance(ms[0], tuple):
                out = "\n".join(m[0] for m in ms)
            else:
                out = "\n".join(ms)
        elif mode == "First Group":
            m = re.search(pattern, s)
            out = m.group(group_index) if m and len(m.groups()) >= group_index else ""
        else:  # All Groups
            out = "\n".join(m.group(group_index) for m in re.finditer(pattern, s) if m.groups() and len(m.groups()) >= group_index)
    except re.error:
        out = ""
    return NodeResult(outputs={0: StringArtifact(out)})


register_node("RegexExtract", signature=_sig_regex_extract, execute=_exec_regex_extract)


def _sig_json_extract(node):
    return NodeSignature(inputs=_str_in("json_string", "key"), output_names=["STRING"], output_types=[STRING])


def _exec_json_extract(ctx, inputs):
    import json

    raw = string_value(inputs.get("json_string")) or str(_w(ctx, 0, ""))
    key = string_value(inputs.get("key")) or str(_w(ctx, 1, ""))
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and key in data:
            v = data[key]
            out = "" if v is None else str(v)
        else:
            out = ""
    except (json.JSONDecodeError, TypeError):
        out = ""
    return NodeResult(outputs={0: StringArtifact(out)})


register_node("JsonExtractString", signature=_sig_json_extract, execute=_exec_json_extract)


def _sig_string_constant(node):
    return NodeSignature(inputs=[], output_names=["STRING"], output_types=[STRING])


def _exec_string_constant(ctx, inputs):
    """Constant string source — feeds StringConcatenate suffixes and any
    STRING input that needs a literal (ComfyUI covers this with Primitive
    String nodes; graphs imported without them rely on this node)."""
    return NodeResult(outputs={0: StringArtifact(str(_w(ctx, 0, "")))})


register_node("StringConstant", signature=_sig_string_constant, execute=_exec_string_constant)


# Register ComfyUI native IO aliases after all comfy-mss nodes are guaranteed
# to exist (nodes.py is imported by core._load_builtin_nodes before this module).
_register_native_io_aliases()


__all__ = ["OUTPUT_NODE_TYPES"]
