import shutil
import subprocess
import sys

import av
import numpy as np
import pytest

from pymss.audio_io import _load_audio_av, downmix_to_stereo, load_audio
from test.test_separator_channels import make_separator


def write_layout_wav(path, audio, layout, sample_rate=8000):
    with av.open(str(path), "w") as container:
        stream = container.add_stream("pcm_f32le", rate=sample_rate)
        stream.layout = layout
        frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(audio.T).reshape(1, -1), format="flt", layout=layout)
        frame.sample_rate = sample_rate
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)


def layout_audio(channels, length=8000):
    phase = np.arange(length, dtype=np.float32) / 8000
    return np.stack([0.1 * np.sin(2 * np.pi * (100 + 70 * index) * phase) for index in range(channels)])


@pytest.mark.parametrize("layout", ["3.0", "5.1", "5.1(side)", "7.1"])
def test_pyav_stereo_downmix_matches_ffmpeg_layout(tmp_path, layout):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required for the reference downmix")
    audio = layout_audio(len(av.AudioLayout(layout).channels))
    path = tmp_path / "input.wav"
    write_layout_wav(path, audio, layout)
    with av.open(str(path)) as container:
        assert next(container.decode(audio=0)).layout.name == layout

    result, sample_rate = load_audio(path, downmix_stereo=True)
    reference = subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
        "-ac", "2", "-f", "f32le", "-acodec", "pcm_f32le", "-",
    ], check=True, capture_output=True)
    expected = np.frombuffer(reference.stdout, dtype="<f4").reshape(-1, 2).T
    assert sample_rate == 8000
    assert result.shape == (2, audio.shape[1])
    np.testing.assert_allclose(result, expected, atol=2e-6)
    np.testing.assert_allclose(downmix_to_stereo(audio, 8000, layout), expected, atol=2e-6)
    assert not np.allclose(result[0], result[1])


@pytest.mark.parametrize("layout", ["mono", "stereo", "5.1"])
def test_default_loading_preserves_source_channels(tmp_path, layout):
    channels = len(av.AudioLayout(layout).channels)
    audio = layout_audio(channels)
    path = tmp_path / "input.wav"
    write_layout_wav(path, audio, layout)
    result, _ = load_audio(path)
    expected = audio[0] if channels == 1 else audio
    np.testing.assert_allclose(result, expected, atol=1e-7)
    if channels <= 2:
        result, _ = load_audio(path, downmix_stereo=True)
        np.testing.assert_allclose(result, expected, atol=1e-7)


@pytest.mark.parametrize("layout", ["7.1", "FL+FR+FC+LFE+BL+BR+BC+SL+SR"])
def test_pyav_many_channel_decode_preserves_samples(tmp_path, layout):
    channels = len(av.AudioLayout(layout).channels)
    audio = layout_audio(channels)
    path = tmp_path / "input.wav"
    output = tmp_path / "decoded.npz"
    write_layout_wav(path, audio, layout)
    # Isolate the native access violation seen with PyAV 14 planar frames.
    result = subprocess.run([
        sys.executable, "-c",
        (
            "import sys, numpy as np; from pymss.audio_io import _load_audio_av; "
            "audio, rate, layout = _load_audio_av(sys.argv[1], return_layout=True); "
            "mono, _ = _load_audio_av(sys.argv[1], mono=True); "
            "np.savez(sys.argv[2], audio=audio, mono=mono, rate=rate, layout=layout)"
        ),
        str(path), str(output),
    ], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    with np.load(output) as decoded:
        assert decoded["rate"] == 8000
        assert decoded["layout"] == av.AudioLayout(layout).name
        np.testing.assert_allclose(decoded["audio"], audio, atol=1e-7)
        np.testing.assert_allclose(decoded["mono"], audio.mean(axis=0), atol=1e-7)


def test_stereo_downmix_ffmpeg_fallback_keeps_layout(tmp_path, monkeypatch):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required for fallback decoding")
    from pymss import audio_io

    path = tmp_path / "input.wav"
    write_layout_wav(path, layout_audio(3), "3.0")
    expected, _ = _load_audio_av(path, downmix_stereo=True, sr=16000, offset=0.2, duration=0.3)

    def fail_decoder(*args, **kwargs):
        raise RuntimeError("Decoder failure")

    monkeypatch.setattr(audio_io, "_load_audio_av", fail_decoder)
    result, rate = load_audio(path, downmix_stereo=True, sr=16000, offset=0.2, duration=0.3)
    assert rate == 16000
    assert result.shape == expected.shape == (2, 4800)
    # Seeking and resampling may differ at the clip boundaries.
    np.testing.assert_allclose(result[:, 128:-128], expected[:, 128:-128], atol=3e-5)


@pytest.mark.parametrize("model_channels", [1, 2])
@pytest.mark.parametrize("layout", ["mono", "stereo", "3.0"])
def test_process_folder_saves_expected_channel_count(tmp_path, model_channels, layout):
    channels = len(av.AudioLayout(layout).channels)
    source = layout_audio(channels, length=192)
    path = tmp_path / "input.wav"
    write_layout_wav(path, source, layout)
    separator = make_separator(model_channels)
    separator.store_dirs = {"vocals": str(tmp_path / "out")}
    separator.output_format = "wav"
    separator.audio_params = {"wav_bit_depth": "FLOAT"}
    separator.save_as_folder = False
    separator.debug = True

    assert separator.process_folder(str(path)) == ["input.wav"]
    saved_path = tmp_path / "out" / "input_vocals.wav"
    result, _ = load_audio(saved_path)
    if channels == 1 or channels > 2 and model_channels == 1:
        assert result.ndim == 1
        expected = source.mean(axis=0) * 0.25
    else:
        assert result.shape == (2, source.shape[1])
        expected = (source if channels == 2 else downmix_to_stereo(source, 8000, layout)) * 0.25
    np.testing.assert_allclose(result, expected, atol=2e-6)


def test_downmix_rejects_mismatched_layout():
    with pytest.raises(ValueError, match="does not match"):
        downmix_to_stereo(layout_audio(6), 8000, "stereo")


def test_nonstandard_channel_count_requires_explicit_layout():
    audio = layout_audio(9)
    with pytest.raises(ValueError, match="provide channel_layout"):
        downmix_to_stereo(audio, 8000)
    result = downmix_to_stereo(audio, 8000, "FL+FR+FC+LFE+BL+BR+BC+SL+SR")
    assert result.shape == (2, audio.shape[1])


@pytest.mark.parametrize("channels", [1, 2])
def test_workflow_accepts_multichannel_audio(tmp_path, channels):
    from pymss.workflow import WorkflowRunner, load_workflow_data

    path = tmp_path / "input.wav"
    write_layout_wav(path, layout_audio(6, length=192), "5.1")
    separator = make_separator(channels)
    workflow = load_workflow_data({"version": 1, "steps": [{
        "id": "split", "model": "local-model", "stems": ["vocals"], "save": {"vocals": "vocals"},
    }]})
    runner = WorkflowRunner(workflow, separator_factory=lambda *args, **kwargs: separator)
    assert runner.run(str(path), tmp_path / "out") == ["input.wav"]
    saved = tmp_path / "out" / "input" / "vocals" / "input_vocals.wav"
    result, _ = load_audio(saved)
    assert result.ndim == (1 if channels == 1 else 2)
