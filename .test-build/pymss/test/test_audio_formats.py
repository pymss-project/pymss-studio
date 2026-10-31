"""Round-trip encode/decode tests for save_audio across all supported formats."""

from __future__ import annotations

import os
import tempfile

import numpy as np
import pytest

from pymss.audio_io import load_audio, save_audio


@pytest.fixture
def tone():
    """2s stereo 440Hz tone at 44100 Hz, peak ~0.3."""
    sr = 44100
    t = np.linspace(0, 2, sr * 2, False)
    mono = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    return np.stack([mono, mono], axis=1), sr


@pytest.mark.parametrize(
    "fmt,ext,params",
    [
        ("wav", ".wav", {"wav_bit_depth": "FLOAT"}),
        ("wav", ".wav", {"wav_bit_depth": "PCM_16"}),
        ("flac", ".flac", {}),
        ("mp3", ".mp3", {"mp3_bit_rate": "320k"}),
        ("m4a", ".m4a", {}),
        ("aac", ".aac", {"aac_bit_rate": "128k"}),
        ("vorbis", ".ogg", {}),
        ("ogg", ".ogg", {}),  # alias of vorbis
        ("opus", ".opus", {}),
    ],
)
def test_save_audio_roundtrip(tone, fmt, ext, params):
    audio, sr = tone
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, f"out{ext}")
        save_audio(path, audio, sr, fmt, params)
        assert os.path.getsize(path) > 0
        loaded, loaded_sr = load_audio(path)
        # Sample rate: opus is resampled to 48000, others keep 44100.
        if fmt == "opus":
            assert loaded_sr == 48000
        else:
            assert loaded_sr == sr
        # Channel count preserved. load_audio returns channel-first: (channels, samples).
        assert loaded.shape[0] == 2
        # Lossless formats preserve peak exactly; lossy ones roughly.
        peak = float(np.abs(loaded).max())
        if fmt in {"wav", "flac"}:
            assert 0.25 < peak < 0.35
        else:
            assert 0.2 < peak < 0.4


def test_opus_resamples_non_native_sr(tone):
    """opus only supports 8/12/16/24/48 kHz; 44100 must be resampled (to 48000)."""
    audio, sr = tone
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "out.opus")
        save_audio(path, audio, sr, "opus", {})
        _, loaded_sr = load_audio(path)
        assert loaded_sr == 48000


def test_mono_save(tone):
    """Mono audio saves and loads back with one channel."""
    audio, sr = tone
    mono = audio[:, 0]
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "mono.wav")
        save_audio(path, mono, sr, "wav", {"wav_bit_depth": "PCM_16"})
        loaded, _ = load_audio(path)
        # mono loads back as 1-D (samples,)
        assert loaded.ndim == 1
