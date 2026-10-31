"""Audio services: swappable provider pools behind stable interfaces.

Round 3 of docs/plugin-redesign-plan.md. The resample service is first:

- ``AudioService`` owns named provider slots (``register_resampler`` /
  ``select_resampler``). Selection is explicit; last selection wins and is
  recorded (``selected_resampler()``) so callers can assert which
  implementation handled a call — fixing the "replaced the capability but the
  graph called the builtin directly" class of bug.
- The builtin provider wraps ``pymss.audio_io.resample_audio`` (PyAV) exactly,
  so pointing general paths at the service changes no audio behavior.
- Same-rate inputs short-circuit before any provider runs, matching
  ``resample_audio`` semantics.
"""

from __future__ import annotations

from typing import Callable, Protocol

import numpy as np

__all__ = ["AudioService", "ProviderError", "Resampler"]


class ProviderError(Exception):
    """Raised for unknown providers or invalid registration."""


class Resampler(Protocol):
    """Resample channel-first float32 audio between sample rates."""

    name: str

    def resample(self, audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray: ...


def default_audio_service() -> AudioService:
    """Process-lazy shared AudioService.

    Hosts wanting isolation construct their own ``AudioService`` and thread it
    through; module-level helpers (graph _resample, separator) use this one so
    provider selection is process-wide and observable.
    """
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = AudioService()
    return _DEFAULT_SERVICE


_DEFAULT_SERVICE: AudioService | None = None


class _BuiltinResampler:
    """Default provider: the PyAV implementation all general paths use today."""

    name = "builtin"

    def resample(self, audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
        from .audio_io import resample_audio

        return resample_audio(audio, orig_sr, target_sr)


class AudioService:
    """Provider-routed audio operations for one host/process.

    Providers register explicitly; nothing is inferred from import order.
    """

    def __init__(self) -> None:
        self._resamplers: dict[str, Resampler] = {
            _BuiltinResampler.name: _BuiltinResampler()
        }
        self._selected_resampler: str = _BuiltinResampler.name

    # ---- resample ----

    def register_resampler(self, provider: Resampler, *, replace: bool = False) -> None:
        name = getattr(provider, "name", None)
        if not isinstance(name, str) or not name:
            raise ProviderError("resampler provider must have a non-empty 'name'")
        if not callable(getattr(provider, "resample", None)):
            raise ProviderError(f"resampler provider {name!r} must be callable at .resample()")
        if name in self._resamplers and not replace and name != "builtin":
            raise ProviderError(
                f"resampler {name!r} already registered (pass replace=True to override)"
            )
        self._resamplers[name] = provider

    def select_resampler(self, name: str) -> None:
        if name not in self._resamplers:
            raise ProviderError(
                f"unknown resampler {name!r}; registered: {sorted(self._resamplers)}"
            )
        self._selected_resampler = name

    def selected_resampler(self) -> str:
        return self._selected_resampler

    def list_resamplers(self) -> list[str]:
        return sorted(self._resamplers)

    def resample(self, audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
        if int(orig_sr) == int(target_sr):
            return audio
        provider = self._resamplers[self._selected_resampler]
        return provider.resample(audio, int(orig_sr), int(target_sr))
