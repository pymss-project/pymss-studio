"""Round 3b: graph _resample routes through the audio service (provider swap
changes graph behavior; default selection matches the previous builtin path).
"""

from __future__ import annotations

import numpy as np

from pymss.audio_services import default_audio_service
from pymss.graph.nodes import _resample


class _HalvingResampler:
    name = "halving"

    def resample(self, audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
        return audio[..., : audio.shape[-1] // 2].copy()


def test_graph_resample_uses_selected_provider():
    service = default_audio_service()
    service.register_resampler(_HalvingResampler(), replace=True)
    service.select_resampler("halving")
    try:
        audio = np.ones((2, 48000), dtype=np.float32)
        out = _resample(audio, 48000, 44100)
        assert out.shape[-1] == 24000  # halving provider ran, not PyAV
    finally:
        service.select_resampler("builtin")


def test_graph_resample_default_is_builtin():
    service = default_audio_service()
    assert service.selected_resampler() == "builtin"
    audio = np.ones((2, 1000), dtype=np.float32)
    out = _resample(audio, 48000, 24000)
    # PyAV output length for 1000 samples 48k->24k is exactly 500
    assert out.shape[-1] == 500
