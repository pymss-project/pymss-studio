"""Built-in codec capabilities: encode/decode for the audio formats pymss supports.

Each format is registered as a separate capability (wav_encode, mp3_encode,
opus_encode, ...). This keeps the capability pool flat and lets a plugin
register just one format (e.g. a hypothetical wma_encode) without touching the
others.

All encoders share the signature:
    encode(audio: np.ndarray, sample_rate: int, path, **params) -> None

audio is sample-major: (samples,) for mono or (samples, channels) for
multichannel — matching save_audio's public convention.

save_audio() becomes a thin dispatcher: it looks up f"{output_format}_encode"
in the capability pool and calls it. Unknown formats raise CapabilityNotFound
(instead of silently falling through to wav, as the old else-branch did).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .registry import _REGISTRY


def _to_array(audio) -> np.ndarray:
    return np.asarray(audio)


def _layout(audio: np.ndarray) -> str:
    return "stereo" if audio.ndim > 1 and audio.shape[1] == 2 else "mono"


def _resample_opus(audio: np.ndarray, sr: int):
    """Opus needs 8/12/16/24/48 kHz. Resample to nearest legal rate via librosa."""
    _OPUS_RATES = (8000, 12000, 16000, 24000, 48000)
    if int(sr) in _OPUS_RATES:
        return audio, int(sr)
    import librosa

    target_sr = min(_OPUS_RATES, key=lambda r: abs(r - int(sr)))
    if audio.ndim == 2:
        channels = [librosa.resample(audio[:, c].astype(np.float32), orig_sr=int(sr), target_sr=target_sr) for c in range(audio.shape[1])]
        # pad to equal length then stack
        maxlen = max(ch.shape[0] for ch in channels)
        padded = np.stack([np.pad(ch, (0, maxlen - ch.shape[0])) for ch in channels], axis=1)
        return padded, target_sr
    return librosa.resample(audio.astype(np.float32), orig_sr=int(sr), target_sr=target_sr), target_sr


# ---------------------------------------------------------------------------
# soundfile-backed encoders (wav/flac/opus/vorbis/ogg)
# ---------------------------------------------------------------------------


def encode_wav(audio, sample_rate, path, bit_depth: str = "FLOAT", **_):
    import soundfile as sf

    # soundfile handles wav natively; map bit depth to subtype.
    subtypes = {"FLOAT": "FLOAT", "PCM_16": "PCM_16", "PCM_24": "PCM_24", "PCM_32": "PCM_32"}
    audio = _to_array(audio)
    sf.write(str(path), audio, int(sample_rate), format="WAV", subtype=subtypes.get(bit_depth, "FLOAT"))


def encode_flac(audio, sample_rate, path, bit_depth: str = "PCM_24", **_):
    import soundfile as sf

    audio = _to_array(audio)
    sf.write(str(path), audio, int(sample_rate), format="FLAC", subtype="PCM_24" if bit_depth == "PCM_24" else "PCM_16")


def encode_opus(audio, sample_rate, path, **_):
    import soundfile as sf

    audio = _to_array(audio)
    audio, sr = _resample_opus(audio, sample_rate)
    sf.write(str(path), audio, sr, format="OGG", subtype="OPUS")


def encode_vorbis(audio, sample_rate, path, **_):
    import soundfile as sf

    audio = _to_array(audio)
    sf.write(str(path), audio, int(sample_rate), format="OGG", subtype="VORBIS")


# ---------------------------------------------------------------------------
# PyAV-backed encoders (mp3/m4a/aac)
# ---------------------------------------------------------------------------


def _bitrate_to_int(value):
    if value is None:
        return None
    if isinstance(value, int):
        return value
    value = str(value).strip().lower()
    return int(float(value[:-1]) * 1000) if value.endswith("k") else int(value)


def _encode_with_av(audio, sample_rate, path, codec: str, container_format=None, bit_rate=None, extra_options=None):
    """Shared PyAV encode path. audio is sample-major (samples, channels) or (samples,)."""
    import av

    audio = _to_array(audio)
    layout = _layout(audio)
    # PyAV wants channel-major float32 (channels, samples) via from_ndarray with fltp.
    frame_audio = np.ascontiguousarray(audio[:, None] if audio.ndim == 1 else audio)
    frame_audio = np.ascontiguousarray(frame_audio.astype(np.float32).T)

    with av.open(str(path), "w", format=container_format) as container:
        stream = container.add_stream(codec, rate=int(sample_rate))
        stream.layout = layout
        if bit_rate is not None:
            stream.bit_rate = bit_rate
        if extra_options:
            stream.codec_context.options = extra_options

        frame = av.AudioFrame.from_ndarray(frame_audio, format="fltp", layout=layout)
        frame.sample_rate = int(sample_rate)
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def encode_mp3(audio, sample_rate, path, bit_rate="320k", **_):
    _encode_with_av(audio, sample_rate, path, codec="libmp3lame", bit_rate=_bitrate_to_int(bit_rate))


def encode_m4a(audio, sample_rate, path, bit_rate="512k", codec="aac", aac_at_quality=2, **_):
    extra = {"aac_at_quality": str(aac_at_quality)} if codec == "aac_at" else None
    _encode_with_av(
        audio, sample_rate, path, codec=codec, container_format=None, bit_rate=_bitrate_to_int(bit_rate), extra_options=extra
    )


def encode_aac(audio, sample_rate, path, bit_rate="128k", **_):
    _encode_with_av(
        audio, sample_rate, path, codec="aac", container_format="adts", bit_rate=_bitrate_to_int(bit_rate)
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


# Map format name -> (capability func, default params). 'ogg' is a vorbis alias.
_BUILTIN_CODECS = {
    "wav": (encode_wav, {}),
    "flac": (encode_flac, {}),
    "mp3": (encode_mp3, {}),
    "m4a": (encode_m4a, {}),
    "aac": (encode_aac, {}),
    "opus": (encode_opus, {}),
    "vorbis": (encode_vorbis, {}),
    "ogg": (encode_vorbis, {}),  # alias
}


def register_builtin_codecs() -> None:
    """Register all built-in format encoders as capabilities (idempotent).

    Each format F registers a capability named f"{F}_encode". A capability
    that already exists is never overwritten — neither a repeat builtin
    registration nor one over a plugin-provided encoder for the same format.
    """
    with _REGISTRY._builtin_registration():
        for fmt, (func, _default_params) in _BUILTIN_CODECS.items():
            name = f"{fmt}_encode"
            if name in _REGISTRY.capabilities:
                continue
            _REGISTRY.register_capability(
                name, func, source="builtin", description=f"encode audio to {fmt}"
            )


def supported_formats() -> list[str]:
    """Return formats with a registered {fmt}_encode capability (any source).

    Plugin-registered encoders are included so new formats are discoverable.
    """
    return sorted(
        name[:-len("_encode")]
        for name in _REGISTRY.capabilities
        if name.endswith("_encode")
    )


__all__ = [
    "register_builtin_codecs",
    "supported_formats",
    "encode_wav",
    "encode_flac",
    "encode_mp3",
    "encode_m4a",
    "encode_aac",
    "encode_opus",
    "encode_vorbis",
]
