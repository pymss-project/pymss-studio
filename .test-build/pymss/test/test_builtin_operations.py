"""Round 7: built-in operations ship through the same runtime path as plugins."""

from __future__ import annotations

import numpy as np

from pymss.operations import default_runtime
from pymss.operations.builtins import register_builtin_operations


def _artifact(samples: int = 1000, sr: int = 44100):
    from pymss.graph.core import AudioArtifact

    return AudioArtifact(np.ones((2, samples), dtype=np.float32) * 0.5, sr, channel_layout="stereo")


def test_builtin_operations_register_into_default_runtime():
    rt = default_runtime()
    ids = [op["id"] for op in rt.list_operations()]
    assert "pymss.audio.adjust_volume" in ids
    assert "pymss.audio.invert_phase" in ids
    assert "pymss.audio.normalize_peak" in ids
    assert "pymss.audio.trim" in ids


def test_registration_is_idempotent():
    assert register_builtin_operations() == 0  # default_runtime already has them


def test_adjust_volume_operation_runs():
    rt = default_runtime()
    result = rt.run(
        "pymss.audio.adjust_volume",
        inputs={"audio": _artifact()},
        params={"gain_db": 6.0},
    )
    expected = 0.5 * 10 ** (6.0 / 20.0)
    np.testing.assert_allclose(result["audio"].audio[0, 0], expected, rtol=1e-6)


def test_invert_phase_operation_runs():
    rt = default_runtime()
    result = rt.run(
        "pymss.audio.invert_phase",
        inputs={"audio": _artifact(10)},
        params={},
    )
    np.testing.assert_allclose(result["audio"].audio[0, 0], -0.5, rtol=1e-6)


def test_normalize_peak_operation_runs():
    rt = default_runtime()
    result = rt.run(
        "pymss.audio.normalize_peak",
        inputs={"audio": _artifact(10)},
        params={"target_peak": 0.8},
    )
    np.testing.assert_allclose(float(np.max(np.abs(result["audio"].audio))), 0.8, rtol=1e-6)


def test_builtin_ops_visible_to_plugin_ops_cli_path():
    """The same runtime the CLI/Studio/Comfy use carries the built-ins."""
    rt = default_runtime()
    entry = rt._operations["pymss.audio.normalize_peak"]
    assert entry.source == "pymss.builtin"
