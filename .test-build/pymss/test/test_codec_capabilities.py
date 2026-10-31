"""Tests for codec capabilities: save_audio dispatches via the capability pool,
and each format encoder is independently callable."""

from __future__ import annotations

import os
import tempfile

import numpy as np
import pytest

from pymss import load_audio, save_audio
from pymss.plugins import CapabilityNotFound, get_registry, require_capability
from pymss.plugins.codecs import supported_formats


@pytest.fixture
def tone():
    sr = 44100
    t = np.linspace(0, 1, sr, False)
    mono = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    return np.stack([mono, mono], axis=1), sr


def test_all_builtin_codecs_registered():
    reg = get_registry()
    for fmt in ("wav", "flac", "mp3", "m4a", "aac", "opus", "vorbis", "ogg"):
        cap = f"{fmt}_encode"
        assert cap in reg.capabilities, f"missing {cap}"
        assert reg.capabilities[cap].source == "builtin"


def test_supported_formats_lists_all():
    fmts = supported_formats()
    for fmt in ("wav", "flac", "mp3", "m4a", "aac", "opus", "vorbis", "ogg"):
        assert fmt in fmts


def test_save_audio_unknown_format_raises(tone):
    """Unknown formats must raise CapabilityNotFound, not silently fall back to wav."""
    audio, sr = tone
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "x.wma")
        with pytest.raises(CapabilityNotFound):
            save_audio(path, audio, sr, "wma", {})


def test_encode_capability_callable_directly(tone):
    """require_capability('mp3_encode') works without going through save_audio."""
    audio, sr = tone
    encode = require_capability("mp3_encode")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "direct.mp3")
        encode(audio, sr, path, bit_rate="128k")
        assert os.path.getsize(path) > 0
        loaded, loaded_sr = load_audio(path)
        assert loaded_sr == sr


def test_save_audio_dispatches_to_capability(tone):
    """save_audio and the direct capability produce the same format output."""
    audio, sr = tone
    with tempfile.TemporaryDirectory() as d:
        p1 = os.path.join(d, "via_save.flac")
        save_audio(p1, audio, sr, "flac", {})
        p2 = os.path.join(d, "via_cap.flac")
        require_capability("flac_encode")(audio, sr, p2)
        # Both are valid FLAC; sizes should be in the same ballpark.
        assert abs(os.path.getsize(p1) - os.path.getsize(p2)) < 1000


def test_legacy_audio_params_still_work(tone):
    """Legacy audio_params keys (wav_bit_depth, mp3_bit_rate) map through."""
    audio, sr = tone
    with tempfile.TemporaryDirectory() as d:
        # wav with PCM_16
        p16 = os.path.join(d, "16.wav")
        save_audio(p16, audio, sr, "wav", {"wav_bit_depth": "PCM_16"})
        # wav with FLOAT
        pf = os.path.join(d, "f.wav")
        save_audio(pf, audio, sr, "wav", {"wav_bit_depth": "FLOAT"})
        # PCM_16 file is smaller than FLOAT (16-bit vs 32-bit)
        assert os.path.getsize(p16) < os.path.getsize(pf)

        # mp3 with different bitrates
        p128 = os.path.join(d, "128.mp3")
        save_audio(p128, audio, sr, "mp3", {"mp3_bit_rate": "128k"})
        p320 = os.path.join(d, "320.mp3")
        save_audio(p320, audio, sr, "mp3", {"mp3_bit_rate": "320k"})
        assert os.path.getsize(p128) < os.path.getsize(p320)


def test_opus_capability_resamples(tone):
    audio, sr = tone
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "out.opus")
        require_capability("opus_encode")(audio, sr, path)
        _, loaded_sr = load_audio(path)
        assert loaded_sr == 48000  # opus resamples 44100 -> 48000
