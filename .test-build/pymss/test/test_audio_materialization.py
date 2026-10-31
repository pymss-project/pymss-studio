from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import numpy as np
import pytest

from pymss.audio_io import load_audio
from pymss.graph.core import AudioArtifact
from pymss.plugins.kit import materialize_audio


@pytest.mark.parametrize("channels,layout", [(1, "mono"), (2, "stereo"), (6, "5.1(side)")])
def test_materialization_preserves_current_samples_rate_and_layout(tmp_path, monkeypatch, channels, layout):
    monkeypatch.setenv("PYMSS_SCRATCH_DIR", str(tmp_path))
    samples = np.random.default_rng(42).uniform(-1.5, 1.5, (channels, 1000)).astype(np.float32)
    audio = AudioArtifact(samples, 32000, source_path="/original/mix.wav", stem_name="vocals", channel_layout=layout)
    with materialize_audio(audio) as path:
        assert path.name == "vocals.wav"
        loaded, rate, actual_layout = load_audio(path, return_layout=True)
        np.testing.assert_array_equal(np.atleast_2d(loaded), samples)
        assert rate == 32000
        # WAV's mono/stereo headers may omit the redundant channel mask.
        assert actual_layout in {layout, f"{channels} channels"} if channels <= 2 else actual_layout == layout
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_materialization_cleans_up_on_interruption(tmp_path, monkeypatch):
    monkeypatch.setenv("PYMSS_SCRATCH_DIR", str(tmp_path))
    with pytest.raises(KeyboardInterrupt):
        with materialize_audio(AudioArtifact(np.ones(100), 16000)) as path:
            assert path.exists()
            raise KeyboardInterrupt
    assert list(tmp_path.iterdir()) == []


def test_concurrent_materialization_is_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PYMSS_SCRATCH_DIR", str(tmp_path))
    barrier = Barrier(2)

    def worker(value):
        with materialize_audio(AudioArtifact(np.full(100, value), 16000)) as path:
            barrier.wait(timeout=10)
            loaded, _ = load_audio(path)
            np.testing.assert_allclose(loaded, value)
            return path

    with ThreadPoolExecutor(2) as pool:
        paths = list(pool.map(worker, [0.1, 0.9]))
    assert paths[0] != paths[1]
    assert list(tmp_path.iterdir()) == []
