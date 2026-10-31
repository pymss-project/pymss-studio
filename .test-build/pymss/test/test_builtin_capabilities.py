"""Tests for built-in capabilities: DSP and channel operations registered
into the plugin system, and verification that separator/ensemble consume them."""

from __future__ import annotations

import numpy as np
import pytest

from pymss.plugins import get_registry, require_capability


@pytest.fixture(autouse=True)
def _ensure_builtins():
    """Built-ins register on `import pymss`; nothing to do per-test."""
    yield


def _mono(samples=10):
    return np.linspace(0.1, 1.0, samples, dtype=np.float32)


def _stereo(samples=10):
    return np.stack([_mono(samples), _mono(samples) * 2], axis=0)


# ---------------------------------------------------------------------------
# Capability presence
# ---------------------------------------------------------------------------


def test_all_builtins_registered():
    reg = get_registry()
    expected = {
        "to_mono", "split_channels", "join_channels",
        "adjust_volume", "invert_phase", "normalize_peak",
        "standardize", "destandardize", "trim", "concat", "mix",
        "ensemble",
    }
    assert expected.issubset(reg.capabilities.keys())
    for name in expected:
        assert reg.capabilities[name].source == "builtin"


# ---------------------------------------------------------------------------
# Channel ops
# ---------------------------------------------------------------------------


def test_to_mono_from_stereo():
    stereo = _stereo()
    out = require_capability("to_mono")(stereo)
    assert out.ndim == 1
    # mean of row0 (x) and row1 (2x) = 1.5x
    assert np.allclose(out, _mono() * 1.5)


def test_to_mono_passthrough_mono():
    a = _mono()
    assert require_capability("to_mono")(a) is a or np.allclose(require_capability("to_mono")(a), a)


def test_split_join_roundtrip():
    stereo = _stereo()
    parts = require_capability("split_channels")(stereo)
    assert len(parts) == 2
    rebuilt = require_capability("join_channels")(parts)
    assert np.allclose(rebuilt, stereo)


def test_join_channels_length_mismatch_raises():
    a = _mono(10)
    b = _mono(11)
    with pytest.raises(ValueError, match="same length"):
        require_capability("join_channels")([a, b])


# ---------------------------------------------------------------------------
# DSP ops
# ---------------------------------------------------------------------------


def test_invert_phase():
    a = _mono()
    assert np.allclose(require_capability("invert_phase")(a), -a)


def test_adjust_volume():
    a = _mono()
    # +6 dB ≈ 2x amplitude
    out = require_capability("adjust_volume")(a, gain_db=6.0206)
    assert np.allclose(out, a * 2.0, atol=1e-4)


def test_normalize_peak():
    a = np.array([0.5, -0.25, 0.1], dtype=np.float32)
    out = require_capability("normalize_peak")(a, target_peak=0.99)
    assert np.isclose(np.abs(out).max(), 0.99, atol=1e-5)


def test_normalize_peak_silence_noop():
    a = np.zeros(10, dtype=np.float32)
    out = require_capability("normalize_peak")(a)
    assert np.allclose(out, 0.0)


def test_standardize_destandardize_roundtrip():
    a = _stereo()
    std_a, stats = require_capability("standardize")(a)
    restored = require_capability("destandardize")(std_a, stats)
    assert np.allclose(restored, a, atol=1e-5)


def test_trim():
    a = _mono(1000)
    sr = 1000
    out = require_capability("trim")(a, sr, start=0.1, duration=0.2)
    assert out.shape[-1] == 200  # 0.2s at 1000Hz


def test_concat():
    a = _mono(5)
    b = _mono(5)
    out = require_capability("concat")([a, b])
    assert out.shape[-1] == 10


def test_mix_add_mean_subtract():
    a = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    b = np.array([0.5, 0.5, 0.5], dtype=np.float32)
    assert np.allclose(require_capability("mix")([a, b], mode="add"), [1.5, 2.5, 3.5])
    assert np.allclose(require_capability("mix")([a, b], mode="mean"), [0.75, 1.25, 1.75])
    assert np.allclose(require_capability("mix")([a, b], mode="subtract"), [0.5, 1.5, 2.5])


# ---------------------------------------------------------------------------
# separator consumes built-in capabilities
# ---------------------------------------------------------------------------


def test_separator_uses_builtin_standardize():
    """The separator's private _standardize_mix must delegate to the builtin."""
    import inspect
    from pymss.separator import _standardize_mix

    src = inspect.getsource(_standardize_mix)
    assert "from .plugins.builtins import standardize" in src


def test_separator_uses_builtin_normalize():
    import inspect
    from pymss.separator import _normalize_outputs

    src = inspect.getsource(_normalize_outputs)
    assert "from .plugins.builtins import normalize_peak" in src


def test_separator_uses_builtin_channels():
    import inspect
    from pymss.separator import _prepare_mix_channels

    src = inspect.getsource(_prepare_mix_channels)
    assert "from .plugins.builtins import to_mono" in src


def test_ensemble_registered_as_capability():
    reg = get_registry()
    assert "ensemble" in reg.capabilities
    assert reg.capabilities["ensemble"].source == "builtin"


# ---------------------------------------------------------------------------
# empty_audio / eq / resample (new builtins)
# ---------------------------------------------------------------------------


def test_empty_audio_stereo():
    out = require_capability("empty_audio")(duration=1.0, sample_rate=44100, channels=2)
    assert out.shape == (2, 44100)
    assert np.all(out == 0)


def test_empty_audio_mono():
    out = require_capability("empty_audio")(duration=0.5, sample_rate=8000, channels=1)
    assert out.shape == (4000,)


def test_resample_changes_length():
    a = np.random.randn(2, 44100).astype(np.float32)
    out = require_capability("resample")(a, 44100, 16000)
    assert out.shape[0] == 2
    assert abs(out.shape[1] - 16000) < 100  # ~1s at 16kHz


def test_resample_same_sr_passthrough():
    a = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    out = require_capability("resample")(a, 44100, 44100)
    assert np.allclose(out, a)


def test_eq_low_boost_increases_low_energy():
    """Boosting low band should increase low-frequency content."""
    sr = 44100
    t = np.linspace(0, 1, sr, False)
    sig = (0.3 * np.sin(2 * np.pi * 100 * t) + 0.3 * np.sin(2 * np.pi * 5000 * t)).astype(np.float32)

    out = require_capability("eq")(sig, sr, low_gain_db=12.0, low_freq=100)

    # Low-band energy ratio should go up after low-shelf boost.
    def lowband_ratio(x):
        spec = np.abs(np.fft.rfft(x))
        return float(spec[: len(spec) // 8].sum() / spec.sum())

    assert lowband_ratio(out) > lowband_ratio(sig)


def test_eq_zero_gain_passthrough():
    """With all gains at 0 dB, output should equal input."""
    sr = 44100
    t = np.linspace(0, 1, sr, False)
    sig = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    out = require_capability("eq")(sig, sr)  # all gains default 0
    assert np.allclose(out, sig, atol=1e-6)


def test_eq_preserves_shape():
    sr = 44100
    stereo = np.random.randn(2, sr).astype(np.float32)
    out = require_capability("eq")(stereo, sr, mid_gain_db=3.0, mid_freq=1000, mid_q=1.0)
    assert out.shape == stereo.shape
