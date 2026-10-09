"""Verify the lazy audio decoder path used by the worker's librosa operations."""

from __future__ import annotations

import importlib
import json
import math
import os
import struct
import sys
import tempfile
import wave
from pathlib import Path


def verify_audio_runtime() -> dict[str, int]:
    importlib.import_module("audioread")
    import numpy as np
    from librosa import load

    source_rate = 16000
    target_rate = 8000
    frame_count = 320
    frames = bytearray()
    for index in range(frame_count):
        value = int(12000 * math.sin(2 * math.pi * 440 * index / source_rate))
        frames.extend(struct.pack("<hh", value, -value))
    with tempfile.TemporaryDirectory(prefix="pymss-audio-probe-") as directory:
        audio_path = Path(directory) / "audio.wav"
        with wave.open(str(audio_path), "wb") as audio:
            audio.setnchannels(2)
            audio.setsampwidth(2)
            audio.setframerate(source_rate)
            audio.writeframes(frames)
        decoded, rate = load(audio_path, sr=target_rate, mono=False)
    if rate != target_rate or decoded.shape != (2, frame_count // 2):
        raise RuntimeError("Audio decoder returned an unexpected sample rate or channel layout")
    if not np.isfinite(decoded).all() or not np.any(decoded):
        raise RuntimeError("Audio decoder returned invalid samples")
    return {"sampleRate": rate, "channels": decoded.shape[0], "frames": decoded.shape[1]}


if __name__ == "__main__":
    # Verification must not create bytecode or Numba caches in an immutable runtime
    # (including a signed macOS application bundle).
    sys.dont_write_bytecode = True
    with tempfile.TemporaryDirectory(prefix="pymss-audio-cache-") as cache_directory:
        os.environ["NUMBA_CACHE_DIR"] = cache_directory
        print(json.dumps(verify_audio_runtime()))
