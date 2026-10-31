"""Array resampling must match the file loader used for single-model inference."""

from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from pymss.audio_io import _load_audio_av
from pymss.graph import AudioArtifact, load_comfy_graph, run_dag
from pymss.graph.nodes import _run_separation
from pymss.plugins.builtins import resample
from pymss.workflow import _ensure_sample_rate


@pytest.mark.parametrize("channels", [1, 2, 3, 6, 8, 9, 16])
@pytest.mark.parametrize("source_rate,target_rate", [
    (48000, 44100), (44100, 48000), (48000, 32000), (96000, 44100), (16000, 48000),
])
def test_resample_matches_single_model_loader(tmp_path, channels, source_rate, target_rate):
    # Cross two array-processing blocks and include a non-aligned tail.
    frames = 131173
    times = np.arange(frames) / source_rate
    signal = sum(0.08 * np.sin(2 * np.pi * freq * times) for freq in (1000, 20000, 21500, 22000))
    audio = np.stack([signal * (index + 1) / channels for index in range(channels)]).astype(np.float32)
    if channels == 1:
        audio = audio[0]
    path = tmp_path / "input.wav"
    sf.write(path, audio.T, source_rate, subtype="FLOAT")
    expected, rate = _load_audio_av(str(path), sr=target_rate, mono=False)

    actual = resample(audio, source_rate, target_rate)

    assert rate == target_rate
    assert actual.dtype == np.float32
    assert actual.shape == expected.shape
    assert actual.flags.c_contiguous
    # The loader flattens mono, while a channel-first array retains its rank.
    np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-6)
    legacy = _ensure_sample_rate(audio, source_rate, target_rate)
    np.testing.assert_allclose(legacy, expected, rtol=0, atol=1e-6)


@pytest.mark.parametrize("shape", [(0,), (1, 0), (2, 0), (8, 0), (65, 0), (81, 0), (129, 0)])
def test_resample_empty_audio_preserves_shape(shape):
    audio = np.empty(shape, dtype=np.float32)
    actual = resample(audio, 48000, 44100)
    assert actual.shape == shape
    assert actual.dtype == np.float32


@pytest.mark.parametrize("channels", [1, 2, 8])
@pytest.mark.parametrize("frames", [1, 31, 127, 1001])
def test_resample_short_audio_matches_flushed_file_loader(tmp_path, channels, frames):
    audio = np.random.default_rng(7).uniform(-0.5, 0.5, size=(channels, frames)).astype(np.float32)
    path = tmp_path / "short.wav"
    sf.write(path, audio.T, 48000, subtype="FLOAT")
    expected, _ = _load_audio_av(str(path), sr=44100, mono=False)

    actual = resample(audio, 48000, 44100)

    assert actual.shape == (channels, expected.shape[-1])
    np.testing.assert_allclose(actual, expected.reshape(channels, -1), rtol=0, atol=1e-6)


@pytest.mark.parametrize("channels", [2, 65, 81])
def test_resample_same_rate_returns_original_float_array(channels):
    audio = np.zeros((channels, 100), dtype=np.float32)
    assert resample(audio, 48000, 48000) is audio


@pytest.mark.parametrize("channels", [64, 65, 81, 129])
@pytest.mark.parametrize("source_rate,target_rate", [(48000, 44100), (44100, 48000)])
@pytest.mark.parametrize("frames", [31, 1001, 65539])
def test_resample_large_channel_counts_match_independent_channels(channels, source_rate, target_rate, frames):
    audio = np.random.default_rng(7).uniform(-0.5, 0.5, size=(channels, frames)).astype(np.float32)
    expected = np.stack([resample(channel, source_rate, target_rate) for channel in audio])

    actual = resample(audio, source_rate, target_rate)

    assert actual.shape == expected.shape
    assert actual.dtype == np.float32
    assert actual.flags.c_contiguous
    np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-6)


@pytest.mark.parametrize("layout", ["fortran", "reversed", "readonly"])
def test_resample_large_channel_counts_accept_array_layouts(layout):
    audio = np.random.default_rng(7).uniform(-0.5, 0.5, size=(65, 1001)).astype(np.float32)
    if layout == "fortran":
        audio = np.asfortranarray(audio)
    elif layout == "reversed":
        audio = audio[:, ::-1]
    else:
        audio.flags.writeable = False
    expected = np.stack([resample(channel.copy(), 48000, 44100) for channel in audio])

    actual = resample(audio, 48000, 44100)

    assert actual.shape == expected.shape
    assert actual.flags.c_contiguous
    np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-6)


@pytest.mark.parametrize("shape", [(2, 3, 4), (0, 10), ()])
def test_resample_rejects_invalid_audio_shape(shape):
    with pytest.raises(ValueError):
        resample(np.zeros(shape, dtype=np.float32), 48000, 44100)


@pytest.mark.parametrize("source_rate,target_rate", [(0, 44100), (48000, 0), (-1, 44100), (48000, -1)])
def test_resample_rejects_invalid_sample_rate(source_rate, target_rate):
    with pytest.raises(ValueError):
        resample(np.zeros(100, dtype=np.float32), source_rate, target_rate)


@pytest.fixture
def workflow_source(tmp_path):
    def create(source_rate):
        times = np.arange(131173) / source_rate
        signal = sum(0.08 * np.sin(2 * np.pi * frequency * times)
                     for frequency in (1000, 20000, 21500, 22000))
        audio = np.stack((signal, signal * 0.75)).astype(np.float32)
        path = tmp_path / "input.wav"
        sf.write(path, audio.T, source_rate, subtype="FLOAT")
        return path, audio

    return create


@pytest.mark.parametrize("source_rate,target_rate", [
    (48000, 32000), (48000, 44100), (48000, 48000), (96000, 48000),
])
def test_graph_save_resampling_matches_single_model_loader(tmp_path, workflow_source, source_rate, target_rate):
    path, _audio = workflow_source(source_rate)
    dag = load_comfy_graph({
        "nodes": [
            {"id": 1, "type": "pymss_load_audio", "inputs": [],
             "outputs": [{"name": "audio", "type": "AUDIO", "links": [1]}],
             "widgets_values": [str(path), ""]},
            {"id": 2, "type": "pymss_save_audio",
             "inputs": [{"name": "audio", "type": "AUDIO", "link": 1}], "outputs": [],
             "widgets_values": ["wav", str(target_rate), "FLOAT", "PCM_24", "320k"]},
        ],
        "links": [[1, 1, 0, 2, 0, "AUDIO"]],
    })

    saved = run_dag(dag, output_dir=tmp_path / "output", download=False)

    assert len(saved) == 1
    actual, rate = sf.read(saved[0], always_2d=True, dtype="float32")
    expected, _ = _load_audio_av(str(path), sr=target_rate, mono=False)
    assert rate == target_rate
    np.testing.assert_allclose(actual.T, expected, rtol=0, atol=1e-6)


@pytest.mark.parametrize("source_rate,target_rate", [
    (48000, 32000), (48000, 44100), (48000, 48000), (96000, 48000),
])
def test_graph_model_input_resampling_matches_single_model_loader(workflow_source, source_rate, target_rate):
    path, audio = workflow_source(source_rate)
    separator = SimpleNamespace(
        config=SimpleNamespace(audio={"sample_rate": target_rate}),
        separate=lambda samples, **kwargs: {"Audio": samples.copy()},
    )

    result, rate = _run_separation(
        SimpleNamespace(), SimpleNamespace(id="split"), AudioArtifact(audio, source_rate),
        build_separator=lambda: separator, stems=["Audio"],
    )

    expected, _ = _load_audio_av(str(path), sr=target_rate, mono=False)
    assert rate == target_rate
    np.testing.assert_allclose(result["Audio"], expected, rtol=0, atol=1e-6)
