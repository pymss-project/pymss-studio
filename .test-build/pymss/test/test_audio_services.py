"""Round 3: Audio service with swappable providers (TDD).

The resample path is the first service: providers register explicitly,
selection is by name (default from config), and the *selected* provider is
recorded so callers and tests can assert which implementation ran.
"""

from __future__ import annotations

import numpy as np
import pytest

from pymss.audio_services import (
    AudioService,
    ProviderError,
)


@pytest.fixture
def service() -> AudioService:
    return AudioService()


def _tone(sr: int = 44100, seconds: float = 0.5) -> np.ndarray:
    t = np.arange(int(sr * seconds), dtype=np.float32) / sr
    return np.stack([np.sin(2 * np.pi * 440 * t)] * 2).astype(np.float32)


class _TaggingResampler:
    """Records calls so tests can assert which provider was selected."""

    name = "tagging"

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def resample(self, audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
        self.calls.append((orig_sr, target_sr))
        # naive decimation — correctness of audio not under test here
        factor = target_sr / orig_sr
        out_len = max(1, int(audio.shape[-1] * factor))
        return audio[..., :out_len].copy()


def test_resample_dispatches_to_selected_provider(service):
    provider = _TaggingResampler()
    service.register_resampler(provider)
    service.select_resampler("tagging")
    audio = _tone(44100)
    service.resample(audio, 44100, 22050)
    assert provider.calls == [(44100, 22050)]
    assert service.selected_resampler() == "tagging"


def test_resample_same_rate_short_circuits_without_provider(service):
    """Same-rate resample must not round-trip through a provider."""
    provider = _TaggingResampler()
    service.register_resampler(provider)
    service.select_resampler("tagging")
    audio = _tone(44100)
    out = service.resample(audio, 44100, 44100)
    assert provider.calls == []
    assert out is audio


def test_select_unknown_provider_raises(service):
    with pytest.raises(ProviderError, match="unknown resampler"):
        service.select_resampler("nope")


def test_default_resampler_is_builtin(service):
    service.register_resampler(_TaggingResampler())
    assert service.selected_resampler() == "builtin"
    audio = _tone(44100)
    out = service.resample(audio, 44100, 22050)
    assert out.shape[-1] < audio.shape[-1]  # actually resampled


def test_provider_can_be_swapped_after_selection(service):
    a = _TaggingResampler()
    b = _TaggingResampler()
    b.name = "other"
    service.register_resampler(a)
    service.register_resampler(b)
    service.select_resampler("tagging")
    service.resample(_tone(44100), 44100, 22050)
    service.select_resampler("other")
    service.resample(_tone(44100), 44100, 22050)
    assert len(a.calls) == 1 and len(b.calls) == 1


def test_builtin_provider_matches_audio_io_output(service):
    """The builtin provider must be the exact PyAV implementation graph/CLI use."""
    from pymss.audio_io import resample_audio

    audio = _tone(44100)
    via_service = service.resample(audio, 44100, 32000)
    via_direct = resample_audio(audio, 44100, 32000)
    np.testing.assert_array_equal(via_service, via_direct)
