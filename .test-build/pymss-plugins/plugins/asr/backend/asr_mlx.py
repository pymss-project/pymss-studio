"""Apple Silicon FunASR MLX adapter kept separate from the Torch backend."""

from __future__ import annotations

import platform
import sys
import json
from pathlib import Path
from typing import Any


_MLX_CTC_ARTIFACTS = {
    "sensevoice-small": ("sensevoice-mlx-fp16", "sensevoice"),
    "paraformer-zh": ("paraformer-mlx-fp16", "paraformer"),
    "paraformer-en": (None, "paraformer"),
}

_BPE_SOURCES = {
    "sensevoice": "https://www.modelscope.cn/models/iic/SenseVoiceSmall/resolve/master/chn_jpn_yue_eng_ko_spectok.bpe.model",
    "paraformer": "https://www.modelscope.cn/models/iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch/resolve/master/tokens.json",
}
_ENGLISH_MODEL_URL = "https://www.modelscope.cn/models/iic/speech_paraformer-large-vad-punc_asr_nat-en-16k-common-vocab10020/resolve/master/"


def is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def _download_to(url: str, destination: Path) -> Path:
    import os
    import tempfile
    import urllib.request

    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.is_file():
        temporary = None
        try:
            with urllib.request.urlopen(url, timeout=60) as response, tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as out:
                temporary = Path(out.name)
                while chunk := response.read(1 << 20):
                    out.write(chunk)
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return destination


def _detok_pieces(family: str, checkpoint: Path, token_ids: list[int], cache: Path, preset: str | None = None) -> str:
    """Decode CTC token ids to text with the family's vocabulary file."""
    ids = [int(i) for i in token_ids if int(i) >= 0]
    if preset == "paraformer-en":
        import sentencepiece as spm

        resources = {}
        for name in ("tokens.json", "bpe.model"):
            local = checkpoint / name
            resources[name] = local if local.is_file() else _download_to(_ENGLISH_MODEL_URL + name, cache / "paraformer-en" / name)
        tokens = json.loads(resources["tokens.json"].read_text(encoding="utf-8"))
        if not isinstance(tokens, list) or not all(isinstance(token, str) for token in tokens):
            raise ValueError("The English Paraformer token list must be a JSON array of strings")
        pieces = [tokens[i] for i in ids if i < len(tokens) and tokens[i] not in {"<blank>", "<unk>", "<s>", "</s>", "<sos>", "<eos>", "<sos/eos>"}]
        return str(spm.SentencePieceProcessor(model_file=str(resources["bpe.model"])).decode(pieces)).strip()
    if family == "sensevoice":
        import sentencepiece as spm

        local = checkpoint / "chn_jpn_yue_eng_ko_spectok.bpe.model"
        if not local.is_file():
            local = _download_to(_BPE_SOURCES[family], cache / "chn_jpn_yue_eng_ko_spectok.bpe.model")
        sp = spm.SentencePieceProcessor(model_file=str(local))
        import re

        decoded = str(sp.decode(ids))
        # SenseVoice emits metadata tags (<|zh|><|NEUTRAL|><|Speech|><|woitn|> ...)
        # before the transcript; strip them from the text output.
        decoded = re.sub(r"<\|[^|<>]*\|>", "", decoded)
        return decoded.strip()
    local = checkpoint / "tokens.json"
    if local.is_file():
        pieces = json.loads(local.read_text(encoding="utf-8"))
    elif (checkpoint / "tokens.txt").is_file():
        pieces = (checkpoint / "tokens.txt").read_text(encoding="utf-8").splitlines()
    else:
        local = _download_to(_BPE_SOURCES[family], cache / "paraformer-zh" / "tokens.json")
        pieces = json.loads(local.read_text(encoding="utf-8"))
    if not isinstance(pieces, list) or not all(isinstance(piece, str) for piece in pieces):
        raise ValueError("The Paraformer token list must contain strings")
    return "".join(
        pieces[i] if 0 <= i < len(pieces) and pieces[i] not in {"<blank>", "<s>", "</s>", "<unk>", "<sos>", "<eos>"} else ""
        for i in ids
    ).replace("@@", "").strip()


def _transcribe_mlx_ctc(audio_path: Path, checkpoint: Path, family: str, language: str | None, cache: Path, preset: str | None = None) -> str:
    """sensevoice: load_model(family='sensevoice') + greedy_decode + sp detok.

    funasr_mlx.api.transcribe only covers LLM-ASR families (qwen3/glm/moss/
    fun-asr-nano); passing a sensevoice checkpoint makes it fail with
    'Cannot infer family from checkpoint name; pass --family'.
    """
    from funasr_mlx.frontend import FbankConfig, WavFrontend, read_audio
    from funasr_mlx.pipeline import load_model

    model, config = load_model(checkpoint, family=family)
    options = dict(config.get("frontend_conf") or {})
    cmvn = options.pop("cmvn_file", None)
    options.setdefault("lfr_m", 7)
    options.setdefault("lfr_n", 6)
    if cmvn and not Path(cmvn).is_absolute():
        base = checkpoint if checkpoint.is_dir() else checkpoint.parent
        cmvn = str(base / cmvn)
    import mlx.core as mx
    import soundfile

    frontend_config = FbankConfig.from_dict(options)
    duration = soundfile.info(str(audio_path)).duration
    samples = read_audio(str(audio_path), sample_rate=frontend_config.fs, max_seconds=max(1.0, duration + 1.0))
    frontend = WavFrontend(frontend_config, cmvn=cmvn)
    chunk_size = max(1, int(frontend_config.fs * min(30.0, frontend_config.max_seconds)))
    transcripts = []
    for start in range(0, len(samples), chunk_size):
        features = frontend(samples[start:start + chunk_size])
        if features.shape[0] == 0:
            continue
        lengths = mx.array([features.shape[0]], dtype=mx.int32)
        decode_options = {"language": language or "auto"} if family == "sensevoice" else {}
        token_ids = model.greedy_decode(features[None], lengths, **decode_options)
        ids = token_ids[0] if token_ids and isinstance(token_ids[0], (list, tuple)) else token_ids
        text = _detok_pieces(family, checkpoint, list(ids), cache, preset)
        if text:
            transcripts.append(text)
    return "\n".join(transcripts)


def transcribe(audio_path: Path, model: str, local_model_dir: str | None, model_root: str | None = None,
               language: str | None = None) -> dict[str, Any]:
    from funasr_mlx.api import download_model

    cache = Path(model_root).expanduser() / "_tool_models" / "asr" / "funasr-mlx" if model_root else Path.home() / ".pymss" / "models" / "funasr-mlx"
    selected = _MLX_CTC_ARTIFACTS.get(model)
    if local_model_dir:
        checkpoint = Path(local_model_dir).expanduser()
        if not checkpoint.is_dir():
            raise ValueError("Local MLX ASR model directory does not exist")
    else:
        if selected is None:
            raise ValueError(f"ASR preset {model!r} is unavailable on Apple Silicon")
        if selected[0] is None:
            raise ValueError("English Paraformer requires a local MLX model directory; use SenseVoice for automatic model downloads")
        cache.mkdir(parents=True, exist_ok=True)
        checkpoint = Path(download_model(selected[0], dest=str(cache)))
    if selected is not None:
        text = _transcribe_mlx_ctc(audio_path, checkpoint, selected[1], language, cache, preset=model)
        return {"text": text, "backend": "funasr-mlx", "model": model}
    from funasr_mlx.api import transcribe as mlx_transcribe

    result = mlx_transcribe(str(audio_path), model=str(checkpoint))
    return result if isinstance(result, dict) else {"text": str(result or "")}
