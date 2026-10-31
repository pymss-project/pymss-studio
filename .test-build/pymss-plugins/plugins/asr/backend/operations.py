"""pymss plugin: speech-to-text transcription.

Protocol v2. Two backends behind one Operation:

* Apple Silicon (macOS arm64) — ``funasr-mlx`` native MLX ports
  (sensevoice / paraformer offline, numerically verified, no PyTorch).
* Everything else (CUDA or CPU) — upstream ``funasr`` AutoModel with the same
  model zoo ids (Paraformer / SenseVoice).

The backend is chosen at runtime by availability, not by platform sniffing
alone: funasr-mlx wins if importable, else funasr. Both return the same
result shape (``text`` plus optional timestamps), so hosts and workflow nodes
see one contract regardless of machine.

Legacy capability registration (``asr_transcribe``) is kept for the DAG /
capability pool; the v1 Operation adds Studio form-driven usage.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any



def _is_apple_silicon() -> bool:
    return sys.platform == "darwin" and sys.platform == "darwin" and _arm64()


def _arm64() -> bool:
    import platform

    return platform.machine() == "arm64"


def _have_funasr_mlx() -> bool:
    try:
        import funasr_mlx  # noqa: F401

        return True
    except ImportError:
        return False


def _have_funasr() -> bool:
    try:
        import funasr  # noqa: F401

        return True
    except ImportError:
        return False


def _ensure_audio_on_disk(audio_in: Any) -> Path:
    """Validate explicit file inputs; AUDIO provenance is never readable content."""
    if isinstance(audio_in, (str, Path)) and Path(audio_in).is_file():
        return Path(audio_in)
    raise ValueError(
        "ASR requires an existing audio file or an AudioArtifact"
    )


# ---------------------------------------------------------------------------
# MLX backend
# ---------------------------------------------------------------------------


def _transcribe_mlx(audio_path: Path, model: str, language: str | None, model_dir: str | None) -> dict[str, Any]:
    from .asr_mlx import transcribe

    return transcribe(audio_path, model, model_dir, language=language)


# ---------------------------------------------------------------------------
# funasr (CUDA/CPU) backend
# ---------------------------------------------------------------------------

_MLX_TO_FUNASR = {
    "sensevoice-small": "iic/SenseVoiceSmall",
    "paraformer-zh": "paraformer-zh",
    "paraformer-en": "paraformer-en",
}

_model_cache: dict[str, Any] = {}


def _transcribe_funasr(audio_path: Path, model: str, language: str | None) -> dict[str, Any]:
    from funasr import AutoModel

    model_id = _MLX_TO_FUNASR.get(model, model)
    key = f"{model_id}:{language or 'auto'}"
    asr = _model_cache.get(key)
    if asr is None:
        asr = AutoModel(model=model_id, disable_update=True)
        _model_cache[key] = asr
    result = asr.generate(input=str(audio_path))
    first = result[0] if result else {}
    text = str(first.get("text", "")).strip()
    timestamps: list[dict[str, float]] = []
    sentence_info = first.get("sentence_info") or []
    for item in sentence_info:
        start, end = item.get("start"), item.get("end")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            timestamps.append({"start": float(start) / 1000.0, "end": float(end) / 1000.0, "text": str(item.get("text", ""))})
    out: dict[str, Any] = {"text": text, "backend": "funasr", "model": model}
    if timestamps:
        out["timestamps"] = timestamps
    return out


def asr_transcribe(audio_path, sample_rate=None, model: str = "sensevoice-small",
                   language: str | None = None, model_dir: str | None = None):
    """Transcribe current AUDIO content or an explicit file to text."""
    from pymss.graph.core import AudioArtifact
    from pymss.plugins.kit import materialize_audio

    if isinstance(audio_path, AudioArtifact):
        with materialize_audio(audio_path) as path:
            return asr_transcribe(path, model=model, language=language, model_dir=model_dir)
    path = _ensure_audio_on_disk(audio_path)
    if _is_apple_silicon() and _have_funasr_mlx():
        return _transcribe_mlx(path, model, language, model_dir)
    if _have_funasr():
        return _transcribe_funasr(path, model, language)
    raise ImportError(
        "no ASR backend available: install funasr-mlx (Apple Silicon) or funasr (CUDA/CPU)"
    )


def _setup_capabilities(plugin):
    """Register the asr_transcribe capability and its Operation."""
    plugin.register_capability(
        "asr_transcribe",
        asr_transcribe,
        description="Speech-to-text via funasr-mlx (Apple Silicon) or funasr (CUDA/CPU)",
    )
    plugin.register_operation(
        id="pymss.asr.transcribe",
        title="Speech to Text",
        description="Transcribe audio to text (funasr-mlx on Apple Silicon, funasr elsewhere)",
        inputs={"audio": {"type": "audio"}},
        params={"type": "object", "properties": {
            "model": {"type": "string", "enum": ["sensevoice-small", "paraformer-zh", "paraformer-en"],
                       "default": "sensevoice-small"},
            "language": {"type": "string", "enum": ["auto", "zh", "en", "yue", "ja", "ko"],
                          "default": "auto"},
            "model_dir": {"type": "string"},
        }, "additionalProperties": False},
        outputs={"report": {"type": "object"}},
        run=lambda ctx, inputs, params: {"report": asr_transcribe(
            inputs["audio"],
            model=str(params.get("model", "sensevoice-small")),
            language=params.get("language") or None,
            model_dir=params.get("model_dir") or None,
        )},
    )


def _asr_tool(request):
    from .asr import _transcribe_audio

    return _transcribe_audio(request)


def setup(plugin):
    from pymss.plugins.kit import register_tool

    _setup_capabilities(plugin)
    register_tool(plugin, "pymss.asr.tool", _asr_tool, title="Speech recognition", tool="asr")

    _register_graph_node(plugin)


def _register_graph_node(plugin):
    from pymss.graph.core import AUDIO, STRING, NodeSignature, PortSpec

    def signature(node):
        return NodeSignature(
            inputs=[PortSpec(name="audio", type=AUDIO)],
            output_names=["text", "backend"],
            output_types=[STRING, STRING],
        )

    def execute(ctx, inputs):
        from pymss.graph.core import NodeResult, StringArtifact
        from pymss.operations import OperationContext

        node = ctx.nodes_by_id[ctx.current_node_id]
        widgets = node.data.get("widgets_values", [])
        # widgets: [model, language, model_dir]
        model = str(widgets[0]) if len(widgets) > 0 and widgets[0] else "sensevoice-small"
        language = str(widgets[1]) if len(widgets) > 1 and widgets[1] else None
        model_dir = str(widgets[2]) if len(widgets) > 2 and widgets[2] else None
        result = plugin.run_operation(
            "pymss.asr.transcribe", inputs=inputs,
            params={"model": model, "language": language or "auto", "model_dir": model_dir or ""},
            context=OperationContext(workdir=str(ctx.output_dir), logger=ctx.logger,
                progress=lambda fraction, message="", event=None: ctx.report_progress(node, fraction, message)),
        )["report"]
        return NodeResult(outputs={
            0: StringArtifact(str(result.get("text", ""))),
            1: StringArtifact(str(result.get("backend", ""))),
        })

    plugin.register_node("AsrTranscribe", execute, signature=signature)
