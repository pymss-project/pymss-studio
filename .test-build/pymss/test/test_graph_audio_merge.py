"""Audio arithmetic must preserve gain between progressive separation stages."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from pymss import load_audio, save_audio
from pymss.graph import load_comfy_graph, run_dag
from pymss.graph.builtin_nodes import _execute_audio_merge
from pymss.graph.core import AudioArtifact, StringArtifact


def _merge_context(widgets=()):
    return SimpleNamespace(current_node_id=1, nodes_by_id={
        1: SimpleNamespace(data={"widgets_values": list(widgets)}),
    })


@pytest.mark.parametrize("method,left,right,expected", [
    ("add", [0.8, -0.4], [0.8, 0.2], [1.6, -0.2]),
    ("subtract", [0.8, -0.4], [-0.8, 0.2], [1.6, -0.6]),
    ("multiply", [1.6, -0.4], [0.8, 0.2], [1.28, -0.08]),
    ("mean", [1.6, -0.4], [0.8, 0.2], [1.2, -0.1]),
    ("average", [1.6, -0.4], [0.8, 0.2], [1.2, -0.1]),
])
@pytest.mark.parametrize("normalize", [True, False])
def test_merge_normalization_is_optional(method, left, right, expected, normalize):
    first = AudioArtifact(np.array([left], dtype=np.float32), 8000)
    second = AudioArtifact(np.array([right], dtype=np.float32), 8000)
    result = _execute_audio_merge(_merge_context(), {
        "audio1": first, "audio2": second, "merge_method": StringArtifact(method),
        "normalize": normalize,
    }).outputs[0]
    expected = np.asarray([expected], dtype=np.float32)
    if normalize:
        expected /= np.abs(expected).max()
    np.testing.assert_allclose(result.audio, expected, atol=1e-7)
    np.testing.assert_array_equal(first.audio, np.array([left], dtype=np.float32))
    np.testing.assert_array_equal(second.audio, np.array([right], dtype=np.float32))
    assert result.sample_rate == 8000


def test_linked_merge_method_overrides_saved_widget():
    result = _execute_audio_merge(_merge_context(["subtract", False]), {
        "audio1": AudioArtifact(np.array([[0.8]], dtype=np.float32), 8000),
        "audio2": AudioArtifact(np.array([[0.4]], dtype=np.float32), 8000),
        "merge_method": StringArtifact("add"),
    }).outputs[0]
    np.testing.assert_allclose(result.audio, [[1.2]], atol=1e-7)


@pytest.mark.parametrize("widget_value,linked_value,peak", [(True, False, 1.6), (False, True, 1.0)])
def test_linked_normalize_overrides_saved_widget(widget_value, linked_value, peak):
    audio = AudioArtifact(np.array([[0.8, 0.4]], dtype=np.float32), 8000)
    result = _execute_audio_merge(_merge_context(["add", widget_value]), {
        "audio1": audio, "audio2": audio, "normalize": linked_value,
    }).outputs[0]
    np.testing.assert_allclose(result.audio, [[peak, peak / 2]], atol=1e-7)


@pytest.mark.parametrize("widgets", [[], ["add"], ["add", None], ["add", True]])
def test_legacy_merge_keeps_peak_protection(widgets):
    audio = AudioArtifact(np.array([[0.8, 0.4]], dtype=np.float32), 8000)
    result = _execute_audio_merge(_merge_context(widgets), {"audio1": audio, "audio2": audio}).outputs[0]
    np.testing.assert_allclose(result.audio, [[1.0, 0.5]], atol=1e-7)
    np.testing.assert_array_equal(audio.audio, np.array([[0.8, 0.4]], dtype=np.float32))


@pytest.mark.parametrize("level", [0.0, 0.2, 0.5])
def test_normalize_does_not_amplify_quiet_audio(level):
    audio = AudioArtifact(np.array([[level, -level]], dtype=np.float32), 8000)
    result = _execute_audio_merge(_merge_context(["add"]), {"audio1": audio, "audio2": audio}).outputs[0]
    np.testing.assert_array_equal(result.audio, audio.audio * 2)


def _progressive_graph(tmp_path, arrays, *, inverted_add=False, normalize=False,
                       merge_normalize=False, output_format="wav", bit_depth="FLOAT",
                       merge_method=None):
    nodes, links = [], []

    def node(kind, widgets=(), sources=()):
        node_id = len(nodes) + 1
        inputs = []
        for slot, (name, source) in enumerate(sources):
            link_id = len(links) + 1
            links.append([link_id, source, 0, node_id, slot, "AUDIO"])
            inputs.append({"name": name, "type": "AUDIO", "link": link_id})
        if kind == "AudioMerge":
            inputs.append({"name": "merge_method", "type": "COMBO", "link": None,
                           "widget": {"name": "merge_method"}})
            if merge_normalize is not None:
                inputs.append({"name": "normalize", "type": "BOOLEAN", "link": None,
                               "widget": {"name": "normalize"}})
        nodes.append({"id": node_id, "type": kind, "inputs": inputs, "widgets_values": list(widgets)})
        return node_id

    sources = []
    for index, audio in enumerate(arrays):
        path = tmp_path / f"source-{index}.wav"
        save_audio(str(path), audio.T, 8000, "wav", {"wav_bit_depth": "FLOAT"})
        sources.append(node("pymss_load_audio", [str(path)]))
    current = sources[0]
    for source in sources[1:]:
        if inverted_add:
            source = node("pymss_audio_invert_phase", sources=[("a", source)])
        widgets = [merge_method or ("add" if inverted_add else "subtract")]
        if merge_normalize is not None:
            widgets.append(merge_normalize)
        current = node("AudioMerge", widgets,
                       [("audio1", current), ("audio2", source)])
    if normalize:
        current = node("pymss_audio_normalize", sources=[("audio", current)])
    node("pymss_save_audio", [output_format, "8000", bit_depth, "PCM_24", "320k"], [("audio", current)])
    dag = load_comfy_graph({"nodes": nodes, "links": links})
    paths = run_dag(dag, output_dir=tmp_path / "out")
    assert len(paths) == 1
    output, sample_rate = load_audio(paths[0])
    assert sample_rate == 8000
    return np.atleast_2d(output)


@pytest.mark.parametrize("channels", [1, 2])
@pytest.mark.parametrize("inverted_add", [False, True])
@pytest.mark.parametrize("output_format,bit_depth", [("wav", "FLOAT"), ("wav", "PCM_24"), ("flac", "PCM_24")])
def test_progressive_residual_survives_intermediate_peaks(tmp_path, channels, inverted_add, output_format, bit_depth):
    arrays = [np.tile(values, (channels, 32)).astype(np.float32) for values in (
        [0.8, -0.5], [-0.8, 0.2], [0.7, -0.1],
    )]
    expected = arrays[0] - arrays[1] - arrays[2]
    assert np.abs(arrays[0] - arrays[1]).max() > 1
    assert np.abs(expected).max() < 1
    actual = _progressive_graph(tmp_path, arrays, inverted_add=inverted_add,
                                output_format=output_format, bit_depth=bit_depth)
    np.testing.assert_allclose(actual, expected, atol=2e-7)


@pytest.mark.parametrize("output_format,bit_depth", [("wav", "FLOAT"), ("wav", "PCM_16"), ("flac", "PCM_24")])
def test_normalization_is_explicit_and_applied_after_all_subtractions(tmp_path, output_format, bit_depth):
    arrays = [np.tile(values, (1, 32)).astype(np.float32) for values in (
        [0.8, -0.5], [-0.8, 0.2], [0.1, -0.1],
    )]
    residual = arrays[0] - arrays[1] - arrays[2]
    expected = residual * (0.999 / np.abs(residual).max())
    actual = _progressive_graph(tmp_path, arrays, normalize=True,
                                output_format=output_format, bit_depth=bit_depth)
    np.testing.assert_allclose(actual, expected, atol=4e-5 if bit_depth == "PCM_16" else 2e-7)


@pytest.mark.parametrize("merge_normalize", [None, True])
@pytest.mark.parametrize("output_format,bit_depth", [
    ("wav", "FLOAT"), ("wav", "PCM_16"), ("wav", "PCM_24"), ("flac", "PCM_24"),
])
def test_legacy_merge_saves_scaled_waveform_without_clipping(tmp_path, merge_normalize, output_format, bit_depth):
    audio = np.tile(np.array([[-0.8, -0.4, 0, 0.4, 0.8]], dtype=np.float32), (1, 32))
    actual = _progressive_graph(tmp_path, [audio, audio], merge_method="add",
                                merge_normalize=merge_normalize, output_format=output_format,
                                bit_depth=bit_depth)
    np.testing.assert_allclose(actual, audio / 0.8, atol=4e-5)
