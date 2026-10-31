"""pymss plugin: LUFS loudness normalization.

Registers the `lufs_normalize` capability and a `NormalizeLUFS` workflow node.
Target loudness is set in LUFS (default -14, the streaming-platform standard).
"""

from __future__ import annotations

import numpy as np



def lufs_normalize(audio, sample_rate, target_lufs: float = -14.0, *, channel_layout=None):
    """Normalize audio to a target integrated loudness in LUFS.

    Uses pyloudnorm (declared as this plugin's dependency). Audio is channel-first
    (channels, samples) or 1-D mono, float32. Returns audio at the same layout
    scaled to reach the target loudness. Multichannel input requires its layout.
    """
    import pyloudnorm as pyln
    from .lufs import integrated_loudness

    audio = np.asarray(audio, dtype=np.float32)
    loudness = integrated_loudness(audio, sample_rate, channel_layout=channel_layout)
    if not np.isfinite(loudness):
        return audio
    # Apply one gain to every original channel, including LFE, without remixing.
    return pyln.normalize.loudness(audio.T, loudness, target_lufs).T


# Workflow node consuming the capability.
def _normalize_lufs_signature(node):
    from pymss.graph.core import NodeSignature, PortSpec, AUDIO

    return NodeSignature(
        inputs=[
            PortSpec(name="audio", type=AUDIO),
        ],
        output_names=["audio"],
        output_types=[AUDIO],
    )


def _execute_normalize_lufs(ctx, inputs):
    from pymss.graph.core import DAGError, NodeResult, audio_to_numpy, numpy_to_audio

    audio_in = inputs.get("audio")
    if audio_in is None:
        raise DAGError("NormalizeLUFS requires an AUDIO input")
    waveform, sr = audio_to_numpy(audio_in)
    node = ctx.nodes_by_id[ctx.current_node_id]
    widgets = node.data.get("widgets_values", [])
    target = float(widgets[0]) if widgets and widgets[0] is not None else -14.0
    out = ctx.require("lufs_normalize")(waveform, sr, target_lufs=target, channel_layout=audio_in.channel_layout)
    return NodeResult(outputs={0: numpy_to_audio(np.asarray(out, dtype=np.float32), sr,
        source_path=audio_in.source_path, stem_name=audio_in.stem_name, channel_layout=audio_in.channel_layout)})


def _setup_capabilities(plugin):
    """Register the capability, workflow node and Operation for lufs_normalize."""
    plugin.register_capability(
        "lufs_normalize",
        lufs_normalize,
        description="LUFS-based loudness normalization (broadcast/streaming standard)",
    )
    plugin.register_node("NormalizeLUFS", _execute_normalize_lufs, signature=_normalize_lufs_signature)
    plugin.register_operation(
        id="pymss.loudnorm.normalize",
        title="Normalize Loudness (LUFS)",
        description=lufs_normalize.__doc__.strip().splitlines()[0],
        inputs={"audio": {"type": "audio"}},
        params={"type": "object", "properties": {
            "target_lufs": {"type": "number", "default": -14.0, "minimum": -70.0, "maximum": -5.0},
        }, "additionalProperties": False},
        outputs={"audio": {"type": "audio"}},
        run=lambda ctx, inputs, params: {"audio": _run_loudnorm(inputs["audio"], params)},
    )


def _run_loudnorm(audio_in, params):
    from pymss.graph.core import audio_to_numpy, numpy_to_audio

    waveform, sr = audio_to_numpy(audio_in)
    out = lufs_normalize(waveform, sr, target_lufs=float(params.get("target_lufs", -14.0)),
                         channel_layout=audio_in.channel_layout)
    return numpy_to_audio(np.asarray(out, dtype=np.float32), sr,
        source_path=audio_in.source_path, stem_name=audio_in.stem_name, channel_layout=audio_in.channel_layout)


def _loudness_tool(request):
    from .loudness import _loudness_audio

    return _loudness_audio(request)


def setup(plugin):
    from pymss.plugins.kit import register_tool

    _setup_capabilities(plugin)
    register_tool(plugin, "pymss.loudnorm.tool", _loudness_tool, title="Loudness matching", tool="loudness")
