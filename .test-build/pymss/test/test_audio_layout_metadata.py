import shutil
import subprocess
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from pymss.audio_io import load_audio
from pymss.graph.core import AudioArtifact, DAGError, DAGNode, NodeContext, SeparatorCache
from pymss.graph.nodes import _execute_input_audio, _execute_invert_phase, _run_separation
from pymss.workflow import WorkflowRunner, load_workflow_data
from test.test_audio_downmix import layout_audio, write_layout_wav
from test.test_separator_channels import make_separator


@pytest.fixture
def center_file(tmp_path):
    audio = layout_audio(3, length=192)
    audio[:2] = 0
    path = tmp_path / "center.wav"
    write_layout_wav(path, audio, "3.0")
    return path


def test_loader_returns_layout_without_changing_default_pair(center_file):
    audio, sr = load_audio(center_file)
    with_layout, layout_sr, layout = load_audio(center_file, return_layout=True)
    assert layout == "3.0" and sr == layout_sr
    np.testing.assert_array_equal(audio, with_layout)
    stereo, _, layout = load_audio(center_file, downmix_stereo=True, return_layout=True)
    assert stereo.shape[0] == 2 and layout == "stereo"
    mono, _, layout = load_audio(center_file, mono=True, return_layout=True)
    assert mono.ndim == 1 and layout == "mono"


def test_workflow_preserves_center_channel_layout(center_file, tmp_path):
    source, _ = load_audio(center_file, downmix_stereo=True)
    workflow = load_workflow_data({"version": 1, "steps": [{
        "id": "split", "model": "local-model", "stems": ["vocals"], "save": {"vocals": "vocals"},
    }]})
    runner = WorkflowRunner(workflow, separator_factory=lambda *args, **kwargs: make_separator(2))
    assert runner.run(str(center_file), tmp_path / "out") == [center_file.name]
    saved, _ = load_audio(tmp_path / "out" / "center" / "vocals" / "center_vocals.wav")
    assert np.abs(saved).max() > 0
    np.testing.assert_allclose(saved, source * 0.25, atol=2e-6)


@pytest.mark.parametrize("channels", [1, 2])
@pytest.mark.parametrize("channel_first", [False, True])
def test_workflow_custom_pair_loader_keeps_legacy_array_orientation(tmp_path, channels, channel_first):
    import soundfile as sf

    source = layout_audio(channels, length=192)
    path = tmp_path / "input.wav"
    write_layout_wav(path, source, "mono" if channels == 1 else "stereo")

    def audio_loader(path, **kwargs):
        audio, rate = sf.read(path, dtype="float32", always_2d=True)
        return (audio.T if channel_first else audio), rate

    workflow = load_workflow_data({"version": 1, "steps": [{
        "id": "split", "model": "local-model", "stems": ["vocals"], "save": {"vocals": "vocals"},
    }]})
    runner = WorkflowRunner(workflow, audio_loader=audio_loader,
                            separator_factory=lambda *args, **kwargs: make_separator(2))
    assert runner.run(str(path), tmp_path / "out") == [path.name]
    saved, rate = load_audio(tmp_path / "out" / "input" / "vocals" / "input_vocals.wav")
    assert rate == 8000
    np.testing.assert_allclose(np.atleast_2d(saved), source * 0.25, atol=1e-7)


@pytest.mark.parametrize("layout", [None, "3.0"])
def test_workflow_custom_layout_loader_uses_channel_first_arrays(tmp_path, layout):
    # Two samples must not be mistaken for two channels in the explicit layout form.
    source = layout_audio(3, length=2)
    workflow = load_workflow_data({"version": 1, "steps": [{"id": "split", "model": "local-model"}]})
    runner = WorkflowRunner(workflow, audio_loader=lambda *args, **kwargs: (source, 8000, layout))
    artifact = runner._load_track(str(tmp_path / "input.wav"), "input").artifacts["input"]
    np.testing.assert_array_equal(artifact.audio, source)
    assert artifact.channel_layout == layout


def test_graph_preserves_layout_through_load_transform_and_separate(center_file, tmp_path):
    ctx = NodeContext(tmp_path, None, False, None, SeparatorCache(), input_path=str(center_file))
    artifact = _execute_input_audio(ctx, {}).outputs[0]
    artifact = _execute_invert_phase(ctx, {"a": artifact}).outputs[0]
    node = DAGNode(id=1, type="mss_separate", inputs=[])
    results, _ = _run_separation(ctx, node, artifact, build_separator=lambda: make_separator(2), stems=["vocals"])
    expected, _ = load_audio(center_file, downmix_stereo=True)
    assert np.abs(results["vocals"]).max() > 0
    np.testing.assert_allclose(results["vocals"], -expected.T * 0.25, atol=2e-6)


@pytest.mark.parametrize("target_sr", [8000, 16000])
def test_graph_passes_explicit_layout_after_resampling(tmp_path, target_sr):
    artifact = AudioArtifact(layout_audio(3), 8000, channel_layout="3.0")

    class Separator:
        config = SimpleNamespace(audio={"sample_rate": target_sr})

        def separate(self, mix, pbar=False, stems=None, *, channel_layout=None):
            assert channel_layout == "3.0"
            assert mix.shape[0] == 3
            return {"vocals": np.zeros((mix.shape[1], 2), dtype=np.float32)}

    ctx = NodeContext(tmp_path, None, False, None, SeparatorCache())
    _, output_sr = _run_separation(ctx, DAGNode(id=1, type="mss_separate", inputs=[]), artifact,
                                   build_separator=Separator, stems=["vocals"])
    assert output_sr == target_sr


@pytest.mark.parametrize("fallback", ["librosa", "ffmpeg"])
def test_decoder_fallback_preserves_file_layout(center_file, monkeypatch, fallback):
    if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg tools are required for fallback layout metadata")
    from pymss import audio_io

    expected, _ = load_audio(center_file)

    def fail(*args, **kwargs):
        raise RuntimeError("Decoder unavailable")

    monkeypatch.setattr(audio_io, "_load_audio_av", fail)
    if fallback == "ffmpeg":
        monkeypatch.setattr(audio_io, "_load_audio_librosa", fail)
    audio, _, layout = load_audio(center_file, return_layout=True)
    assert layout == "3.0"
    np.testing.assert_allclose(audio, expected, atol=1e-7)


@pytest.fixture
def decoded_without_ffprobe(center_file, monkeypatch):
    from pymss import audio_io

    source, _ = load_audio(center_file)

    def fail_decoder(*args, **kwargs):
        raise RuntimeError("Decoder unavailable")

    def missing_probe(*args, **kwargs):
        raise FileNotFoundError("ffprobe is unavailable")

    monkeypatch.setattr(audio_io, "_load_audio_av", fail_decoder)
    monkeypatch.setattr(audio_io, "_ffmpeg_audio_stream_info", missing_probe)
    # A successful decode must not be discarded for another decoder attempt.
    monkeypatch.setattr(audio_io, "_load_audio_ffmpeg", lambda *args, **kwargs: pytest.fail("Unexpected decoder retry"))
    return center_file, source


def test_librosa_without_ffprobe_keeps_samples_and_unknown_layout(decoded_without_ffprobe):
    path, source = decoded_without_ffprobe
    audio, rate, layout = load_audio(path, return_layout=True)
    assert rate == 8000 and layout == "3 channels"
    np.testing.assert_array_equal(audio, source)


@pytest.mark.parametrize("engine", ["workflow", "graph"])
@pytest.mark.parametrize("model_channels", [1, 2])
def test_missing_ffprobe_only_blocks_stereo_downmix(decoded_without_ffprobe, tmp_path, engine, model_channels):
    path, source = decoded_without_ffprobe
    separator = make_separator(model_channels)
    model_calls = separator.model.calls

    def separate():
        if engine == "workflow":
            workflow = load_workflow_data({"version": 1, "steps": [{
                "id": "split", "model": "local-model", "stems": ["vocals"], "save": {"vocals": "vocals"},
            }]})
            runner = WorkflowRunner(workflow, separator_factory=lambda *args, **kwargs: separator)
            assert runner.run(str(path), tmp_path / "out") == [path.name]
            return load_audio(tmp_path / "out" / "center" / "vocals" / "center_vocals.wav")[0]
        ctx = NodeContext(tmp_path, None, False, None, SeparatorCache(), input_path=str(path))
        artifact = _execute_input_audio(ctx, {}).outputs[0]
        node = DAGNode(id=1, type="mss_separate", inputs=[])
        results, _ = _run_separation(ctx, node, artifact, build_separator=lambda: separator, stems=["vocals"])
        return results["vocals"][:, 0]

    if model_channels == 2:
        with pytest.raises(ValueError, match="named channel positions"):
            separate()
        assert not model_calls
        assert not list((tmp_path / "out").rglob("*.wav"))
    else:
        np.testing.assert_allclose(separate(), source.mean(axis=0) * 0.25, atol=1e-7)


def test_librosa_keeps_probe_execution_errors(center_file, monkeypatch):
    from pymss import audio_io

    error = subprocess.CalledProcessError(1, ["ffprobe"])

    def fail_probe(*args, **kwargs):
        raise error

    monkeypatch.setattr(audio_io, "_ffmpeg_audio_stream_info", fail_probe)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        audio_io._load_audio_librosa(center_file, return_layout=True)
    assert caught.value is error


def test_librosa_rejects_probe_channel_mismatch(center_file, monkeypatch):
    from pymss import audio_io

    monkeypatch.setattr(audio_io, "_ffmpeg_audio_stream_info", lambda path: (8000, 2, "stereo"))
    with pytest.raises(ValueError, match="channel count"):
        audio_io._load_audio_librosa(center_file, return_layout=True)


@pytest.fixture(params=["librosa", "ffmpeg"])
def unknown_layout_file(tmp_path, monkeypatch, request):
    if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg tools are required for fallback layout metadata")
    from pymss import audio_io

    pcm = (layout_audio(3, length=192) * 32767).astype("<i2")
    path = tmp_path / "unlabelled.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(3)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(np.ascontiguousarray(pcm.T).tobytes())
    assert audio_io._ffmpeg_audio_stream_info(path) == (8000, 3, None)

    def fail(*args, **kwargs):
        raise RuntimeError("Decoder unavailable")

    monkeypatch.setattr(audio_io, "_load_audio_av", fail)
    # Keep each fallback isolated so the other decoder cannot hide a failure.
    other_loader = "_load_audio_ffmpeg" if request.param == "librosa" else "_load_audio_librosa"
    monkeypatch.setattr(audio_io, other_loader, fail)
    return path, pcm.astype(np.float32) / 32768


def test_unknown_layout_preserves_samples_without_assuming_positions(unknown_layout_file):
    import av

    path, expected = unknown_layout_file
    audio, rate, layout = load_audio(path, return_layout=True)
    assert rate == 8000
    positions = av.AudioLayout(layout).channels
    assert len(positions) == 3 and all(channel.name == "NONE" for channel in positions)
    np.testing.assert_array_equal(audio, expected)
    ordinary_audio, ordinary_rate = load_audio(path)
    assert ordinary_rate == rate
    np.testing.assert_array_equal(ordinary_audio, audio)


@pytest.mark.parametrize("model_channels", [1, 2])
def test_workflow_unknown_layout_only_requires_positions_for_stereo(unknown_layout_file, tmp_path, model_channels):
    path, source = unknown_layout_file
    separator = make_separator(model_channels)
    model_calls = separator.model.calls
    workflow = load_workflow_data({"version": 1, "steps": [{
        "id": "split", "model": "local-model", "stems": ["vocals"], "save": {"vocals": "vocals"},
    }]})
    runner = WorkflowRunner(workflow, separator_factory=lambda *args, **kwargs: separator)
    if model_channels == 2:
        with pytest.raises(ValueError, match="named channel positions"):
            runner.run(str(path), tmp_path / "out")
        assert not model_calls
        assert not list((tmp_path / "out").rglob("*.wav"))
        return

    assert runner.run(str(path), tmp_path / "out") == [path.name]
    saved, _ = load_audio(tmp_path / "out" / "unlabelled" / "vocals" / "unlabelled_vocals.wav")
    assert saved.ndim == 1
    np.testing.assert_allclose(saved, source.mean(axis=0) * 0.25, atol=1e-7)


@pytest.mark.parametrize("model_channels", [1, 2])
def test_graph_unknown_layout_only_requires_positions_for_stereo(unknown_layout_file, tmp_path, model_channels):
    path, source = unknown_layout_file
    ctx = NodeContext(tmp_path, None, False, None, SeparatorCache(), input_path=str(path))
    artifact = _execute_input_audio(ctx, {}).outputs[0]
    separator = make_separator(model_channels)
    node = DAGNode(id=1, type="mss_separate", inputs=[])
    if model_channels == 2:
        with pytest.raises(ValueError, match="named channel positions"):
            _run_separation(ctx, node, artifact, build_separator=lambda: separator, stems=["vocals"])
        assert not separator.model.calls
        return

    results, rate = _run_separation(ctx, node, artifact, build_separator=lambda: separator, stems=["vocals"])
    assert rate == 8000 and results["vocals"].shape == (source.shape[1], 1)
    np.testing.assert_allclose(results["vocals"][:, 0], source.mean(axis=0) * 0.25, atol=1e-7)


def node_context(tmp_path, node_type, widgets):
    node = DAGNode(id=1, type=node_type, inputs=[], data={"widgets_values": widgets})
    return NodeContext(tmp_path, None, False, None, SeparatorCache(), nodes_by_id={1: node}, current_node_id=1)


@pytest.mark.parametrize("operation", ["trim", "volume", "eq", "normalize", "invert"])
def test_channel_preserving_nodes_keep_layout(tmp_path, operation):
    from pymss.graph import builtin_nodes, nodes

    audio = AudioArtifact(layout_audio(3) * 20, 8000, channel_layout="3.0")
    functions = {
        "trim": (builtin_nodes._execute_trim_audio, "TrimAudioDuration", [0.0, 0.01]),
        "volume": (builtin_nodes._execute_adjust_volume, "AudioAdjustVolume", [6]),
        "eq": (builtin_nodes._execute_eq, "AudioEqualizer3Band", [3, 100, 0, 1000, 0.707, 0, 3000]),
        "normalize": (nodes._execute_normalize, "pymss_audio_normalize", []),
        "invert": (nodes._execute_invert_phase, "pymss_audio_invert_phase", []),
    }
    execute, node_type, widgets = functions[operation]
    result = execute(node_context(tmp_path, node_type, widgets), {"audio": audio, "a": audio}).outputs[0]
    assert result.channel_layout == "3.0"
    assert result.audio.shape[0] == 3


@pytest.mark.parametrize("operation", ["concat", "merge", "ensemble"])
@pytest.mark.parametrize("second_layout", ["3.0", "2.1", None])
def test_combining_multichannel_audio_requires_compatible_layouts(tmp_path, operation, second_layout):
    from pymss.graph import builtin_nodes, nodes

    first = AudioArtifact(layout_audio(3), 8000, channel_layout="3.0")
    second = AudioArtifact(layout_audio(3), 8000, channel_layout=second_layout)
    functions = {
        "concat": (builtin_nodes._execute_audio_concat, "AudioConcat", []),
        "merge": (builtin_nodes._execute_audio_merge, "AudioMerge", []),
        "ensemble": (nodes._execute_ensemble, "pymss_audio_ensemble", [2, "avg_wave", 1, 1]),
    }
    execute, node_type, widgets = functions[operation]
    inputs = {"audio1": first, "audio2": second, "audio_1": first, "audio_2": second}
    ctx = node_context(tmp_path, node_type, widgets)
    if second_layout != "3.0":
        with pytest.raises(DAGError, match="channel layouts"):
            execute(ctx, inputs)
    else:
        result = execute(ctx, inputs).outputs[0]
        assert result.channel_layout == "3.0" and result.audio.shape[0] == 3


def test_split_and_join_replace_layout(tmp_path):
    from pymss.graph.builtin_nodes import _execute_join_channels, _execute_split_channels

    ctx = node_context(tmp_path, "SplitAudioChannels", [])
    stereo = AudioArtifact(layout_audio(2), 8000)
    channels = _execute_split_channels(ctx, {"audio": stereo}).outputs
    assert channels[0].channel_layout == channels[1].channel_layout == "mono"
    result = _execute_join_channels(ctx, {"audio_left": channels[0], "audio_right": channels[1]}).outputs[0]
    assert result.channel_layout == "stereo"
    np.testing.assert_array_equal(result.audio, stereo.audio)


def test_artifact_rejects_layout_with_wrong_channel_count():
    with pytest.raises(ValueError, match="channel count"):
        AudioArtifact(layout_audio(3), 8000, channel_layout="5.1")


@pytest.mark.parametrize("channels,layout", [(1, "mono"), (2, "stereo"), (6, "5.1")])
def test_ensemble_does_not_truncate_multichannel_audio(tmp_path, channels, layout):
    from pymss.graph.nodes import _execute_ensemble

    first = AudioArtifact(layout_audio(3), 8000, channel_layout="3.0")
    second = AudioArtifact(layout_audio(channels), 8000, channel_layout=layout)
    ctx = node_context(tmp_path, "pymss_audio_ensemble", [2, "avg_wave", 1, 1])
    with pytest.raises(DAGError, match="channel counts"):
        _execute_ensemble(ctx, {"audio_1": first, "audio_2": second})
