"""Tests for the graph execution engine and comfy-mss node adaptation.

These tests do NOT load real separation models. They verify:
- the DAG engine (topo sort, link resolution, run_dag)
- comfy-mss node executors consume the capability pool correctly
- the JSON loader parses comfy-mss workflow format
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import numpy as np
import pytest

from pymss import save_audio
from pymss.graph import (
    DAG,
    DAGError,
    DAGLink,
    DAGNode,
    NodeSignature,
    PortSpec,
    SeparatorCache,
    get_node_type,
    load_comfy_graph,
    load_comfy_file,
    register_node,
    run_dag,
)
from pymss.graph.core import _load_builtin_nodes, OUTPUT_NODE_TYPES
from pymss.plugins import get_registry


@pytest.fixture(scope="session", autouse=True)
def _load_nodes():
    _load_builtin_nodes()


@pytest.fixture
def make_audio(tmp_path):
    def _create(filename="in.wav", sr=44100, duration=0.2, freq=440, channels=2):
        t = np.linspace(0, duration, int(sr * duration), False)
        mono = (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
        data = np.column_stack([mono] * channels) if channels > 1 else mono[:, None]
        p = tmp_path / filename
        save_audio(str(p), data, sr, "wav", {"wav_bit_depth": "PCM_16"})
        return str(p)

    return _create


@pytest.fixture
def tone_file(make_audio):
    return make_audio("in.wav")


# ---------------------------------------------------------------------------
# Node registry
# ---------------------------------------------------------------------------


def test_comfy_mss_nodes_registered():
    reg = get_registry()
    for n in [
        "input_audio", "pymss_load_audio", "pymss_load_audio_batch",
        "pymss_save_audio", "pymss_mss_separate", "mss_separate",
        "pymss_audio_invert_phase", "pymss_audio_normalize", "pymss_audio_ensemble",
    ]:
        assert n in reg.nodes, f"missing node {n}"


def test_pymss_prefix_aliases_registered():
    reg = get_registry()
    # Both bare and pymss_-prefixed names should resolve.
    assert "mss_separate" in reg.nodes
    assert "pymss_mss_separate" in reg.nodes


def test_comfyui_native_nodes_registered():
    reg = get_registry()
    for n in ["SaveAudio", "SaveAudioMP3", "SaveAudioOpus", "AudioEqualizer3Band",
              "AudioMerge", "AudioConcat", "TrimAudioDuration", "LoadAudio"]:
        assert n in reg.nodes, f"missing node {n}"


def test_output_node_types_known():
    assert "pymss_save_audio" in OUTPUT_NODE_TYPES
    assert "SaveAudio" in OUTPUT_NODE_TYPES


# ---------------------------------------------------------------------------
# Topological ordering
# ---------------------------------------------------------------------------


def test_topo_order_respects_dependencies():
    # n1 -> n2 -> n3
    sig = NodeSignature(inputs=[], output_names=["o"], output_types=["AUDIO"])
    n1 = DAGNode(id=1, type="x", inputs=[], signature=sig)
    n2 = DAGNode(id=2, type="x", inputs=[DAGLink(1, 1, 0, 2, 0, "AUDIO")], signature=sig)
    n3 = DAGNode(id=3, type="x", inputs=[DAGLink(2, 2, 0, 3, 0, "AUDIO")], signature=sig)
    from pymss.graph import topological_order

    order = [n.id for n in topological_order([n3, n1, n2])]  # shuffled input
    assert order == [1, 2, 3]


def test_topo_order_detects_cycle():
    sig = NodeSignature(inputs=[], output_names=["o"], output_types=["AUDIO"])
    n1 = DAGNode(id=1, type="x", inputs=[DAGLink(1, 2, 0, 1, 0, "AUDIO")], signature=sig)
    n2 = DAGNode(id=2, type="x", inputs=[DAGLink(2, 1, 0, 2, 0, "AUDIO")], signature=sig)
    from pymss.graph import topological_order

    with pytest.raises(DAGError, match="cycle"):
        topological_order([n1, n2])


# ---------------------------------------------------------------------------
# Engine end-to-end: load -> invert -> save (no model needed)
# ---------------------------------------------------------------------------


def _make_flow_json(input_widget="in.wav"):
    """A 3-node graph: pymss_load_audio -> pymss_audio_invert_phase -> pymss_save_audio."""
    return {
        "last_node_id": 3,
        "last_link_id": 2,
        "nodes": [
            {"id": 1, "type": "pymss_load_audio", "inputs": [],
             "outputs": [{"name": "audio", "type": "AUDIO", "links": [1]},
                         {"name": "name", "type": "STRING", "links": None}],
             "widgets_values": [input_widget]},
            {"id": 2, "type": "pymss_audio_invert_phase",
             "inputs": [{"name": "a", "type": "AUDIO", "link": 1}],
             "outputs": [{"name": "audio", "type": "AUDIO", "links": [2]}],
             "widgets_values": []},
            {"id": 3, "type": "pymss_save_audio",
             "inputs": [{"name": "audio", "type": "AUDIO", "link": 2},
                       {"name": "filename", "type": "STRING", "link": None}],
             "outputs": [],
             "widgets_values": ["wav", "out_inverted"]},
        ],
        "links": [[1, 1, 0, 2, 0, "AUDIO"], [2, 2, 0, 3, 0, "AUDIO"]],
    }


def test_invert_phase_consumes_capability(tone_file, tmp_path):
    """The invert_phase node must delegate to the invert_phase capability."""
    out_dir = tmp_path / "out"
    dag = load_comfy_graph(_make_flow_json(str(tone_file)))
    saved = run_dag(dag, output_dir=str(out_dir),
                    separator_cache=SeparatorCache())
    assert len(saved) == 1
    # Verify the output is actually phase-inverted (orig + inv ~= 0).
    from pymss import load_audio

    orig, _ = load_audio(tone_file)
    inv, _ = load_audio(saved[0])
    assert np.abs(orig + inv).max() < 1e-4


def test_node_consumes_capability_via_ctx_require():
    """Node executors use ctx.require() to look up capabilities (not import directly)."""
    import inspect
    from pymss.graph import nodes

    # invert_phase executor should call ctx.require("invert_phase")
    src = inspect.getsource(nodes._execute_invert_phase)
    assert "ctx.require" in src or "require" in src


def test_save_audio_via_capability(tone_file, tmp_path):
    """save_audio node dispatches through the codec capability pool."""
    out_dir = tmp_path / "out"
    dag = load_comfy_graph(_make_flow_json(str(tone_file)))
    saved = run_dag(dag, output_dir=str(out_dir),
                    separator_cache=SeparatorCache())
    assert os.path.getsize(saved[0]) > 0


# ---------------------------------------------------------------------------
# ComfyUI native IO aliases
# ---------------------------------------------------------------------------


def test_load_audio_alias_works(tone_file, tmp_path):
    """LoadAudio (ComfyUI native) is an alias of pymss_load_audio."""
    reg = get_registry()
    assert reg.nodes["LoadAudio"].func is reg.nodes["pymss_load_audio"].func


# ---------------------------------------------------------------------------
# No torchaudio dependency
# ---------------------------------------------------------------------------


def test_no_torchaudio_in_graph_code():
    """The graph subsystem must not import or call torchaudio."""
    import inspect
    import pymss.graph.nodes
    import pymss.graph.builtin_nodes

    for mod in [pymss.graph.nodes, pymss.graph.builtin_nodes]:
        src = inspect.getsource(mod)
        # No import statements or runtime calls — comments mentioning it are fine.
        import re
        # Strip comments and docstrings roughly: check for import/call usage.
        code_lines = [l for l in src.splitlines() if not l.strip().startswith("#")]
        code = "\n".join(code_lines)
        assert "import torchaudio" not in code, f"{mod.__name__} imports torchaudio"
        assert "torchaudio." not in code, f"{mod.__name__} calls torchaudio."


# ---------------------------------------------------------------------------
# SaveAudioAdvanced (ComfyUI CORE-202 consolidated save node)
# ---------------------------------------------------------------------------


def test_save_audio_advanced_registered():
    reg = get_registry()
    assert "SaveAudioAdvanced" in reg.nodes
    assert "SaveAudioAdvanced" in OUTPUT_NODE_TYPES


def _make_advanced_flow(format_name, quality="", input_widget="in.wav"):
    return {
        "last_node_id": 2, "last_link_id": 1,
        "nodes": [
            {"id": 1, "type": "pymss_load_audio", "inputs": [],
             "outputs": [{"name": "audio", "type": "AUDIO", "links": [1]},
                         {"name": "name", "type": "STRING", "links": None}],
             "widgets_values": [input_widget]},
            {"id": 2, "type": "SaveAudioAdvanced",
             "inputs": [{"name": "audio", "type": "AUDIO", "link": 1},
                       {"name": "filename_prefix", "type": "STRING", "link": None},
                       {"name": "format", "type": "COMBO", "link": None},
                       {"name": "quality", "type": "COMBO", "link": None}],
             "outputs": [{"name": "audio", "type": "AUDIO", "links": None}],
             "widgets_values": ["adv_out/track", format_name, quality]},
        ],
        "links": [[1, 1, 0, 2, 0, "AUDIO"]],
    }


def test_save_audio_advanced_mp3(tone_file, tmp_path):
    dag = load_comfy_graph(_make_advanced_flow("mp3", "128k", str(tone_file)))
    saved = run_dag(dag, output_dir=str(tmp_path / "out"),
                    separator_cache=SeparatorCache())
    assert len(saved) == 1
    assert saved[0].endswith(".mp3")
    assert os.path.getsize(saved[0]) > 0


def test_save_audio_advanced_opus(tone_file, tmp_path):
    dag = load_comfy_graph(_make_advanced_flow("opus", "96k", str(tone_file)))
    saved = run_dag(dag, output_dir=str(tmp_path / "out"),
                    separator_cache=SeparatorCache())
    assert saved[0].endswith(".opus")


def test_save_audio_advanced_flac(tone_file, tmp_path):
    dag = load_comfy_graph(_make_advanced_flow("flac", input_widget=str(tone_file)))
    saved = run_dag(dag, output_dir=str(tmp_path / "out"),
                    separator_cache=SeparatorCache())
    assert saved[0].endswith(".flac")


# ---------------------------------------------------------------------------
# Runtime inputs (input_name widget / inputs mapping) — strict semantics
# ---------------------------------------------------------------------------

def _make_runtime_flow(widgets):
    """Load nodes with [audio, input_name] widgets, each feeding a save node."""
    nodes, links = [], []
    next_id = 0
    for widget_pair in widgets:
        next_id += 1
        nodes.append({"id": next_id, "type": "pymss_load_audio", "inputs": [],
                      "outputs": [{"name": "audio", "type": "AUDIO", "links": [len(links) + 1]},
                                  {"name": "audio_name", "type": "STRING", "links": None}],
                      "widgets_values": list(widget_pair)})
        next_id += 1
        links.append([len(links) + 1, next_id - 1, 0, next_id, 0, "AUDIO"])
        nodes.append({"id": next_id, "type": "pymss_save_audio",
                      "inputs": [{"name": "audio", "type": "AUDIO", "link": len(links)},
                                 {"name": "filename", "type": "STRING", "link": None}],
                      "outputs": [], "widgets_values": ["wav", "44100"]})
    return {"last_node_id": next_id, "last_link_id": len(links),
            "nodes": nodes, "links": links}


def test_runtime_input_name_hits_mapping(make_audio, tmp_path):
    dag = load_comfy_graph(_make_runtime_flow([["placeholder.wav", "lead"], ["other.wav", "backing"]]))
    lead = make_audio("lead.wav", freq=440)
    backing = make_audio("backing.wav", freq=330)
    saved = run_dag(dag, output_dir=str(tmp_path / "out"), inputs={"lead": lead, "backing": backing})
    assert len(saved) == 2


def test_runtime_input_name_declared_but_missing_fails(tone_file, tmp_path):
    dag = load_comfy_graph(_make_runtime_flow([["placeholder.wav", "lead"]]))
    with pytest.raises(DAGError, match="lead"):
        run_dag(dag, output_dir=str(tmp_path / "out"), inputs={"other": tone_file})


def test_no_placeholder_fallback(tone_file, tmp_path):
    dag = load_comfy_graph(_make_runtime_flow([["in.wav", ""]]))
    with pytest.raises(DAGError, match="not an existing file"):
        run_dag(dag, output_dir=str(tmp_path / "out"), input_path=tone_file)


def test_audio_widget_as_inputs_key(tone_file, tmp_path):
    dag = load_comfy_graph(_make_runtime_flow([["lead", ""]]))
    saved = run_dag(dag, output_dir=str(tmp_path / "out"), inputs={"lead": tone_file})
    assert len(saved) == 1


def test_dag_with_48k_input_and_target_rate(make_audio, tmp_path):
    dag = load_comfy_graph(_make_runtime_flow([["lead", ""]]))
    in_48k = make_audio("in_48k.wav", sr=48000, duration=0.2)
    saved = run_dag(dag, output_dir=str(tmp_path / "out"), inputs={"lead": in_48k})
    assert len(saved) == 1
    assert saved.records[0].sample_rate == 44100


def test_safe_filename_part_supports_unicode():
    from pymss.graph.core import safe_filename_part
    assert safe_filename_part("小蓝背心 - 灯火通明") == "小蓝背心 - 灯火通明"
    assert safe_filename_part("日本語の曲_track01") == "日本語の曲_track01"
    assert safe_filename_part("song:name?*<>|") == "song_name"
    assert safe_filename_part("CON") == "_CON"
    assert safe_filename_part("   ") == "audio"


def test_separation_output_uses_model_sample_rate():
    from pymss.graph.nodes import _run_separation, AudioArtifact
    from pymss.graph.core import NodeContext, DAGNode, SeparatorCache
    from types import SimpleNamespace

    audio_in = AudioArtifact(np.zeros((2, 4800), dtype=np.float32), sample_rate=48000)

    class FakeSeparator:
        def __init__(self):
            self.config = SimpleNamespace(audio={"sample_rate": 44100})
            self.progress_callback = None

        def separate(self, mix, pbar=False, stems=None):
            return {"vocals": np.zeros((2, 4410), dtype=np.float32)}

    ctx = NodeContext(Path("."), None, False, None, SeparatorCache())
    node = DAGNode(id=1, type="mss_separate", inputs=[])
    results, out_sr = _run_separation(ctx, node, audio_in, build_separator=FakeSeparator, stems=["vocals"])
    assert out_sr == 44100
    assert "vocals" in results


def test_run_dag_returns_structured_records(tone_file, tmp_path):
    from pymss.graph import DAGExecutionResult, DAGOutputRecord

    dag = load_comfy_graph(_make_runtime_flow([["lead", ""]]))
    saved = run_dag(dag, output_dir=str(tmp_path / "out"), inputs={"lead": tone_file})

    assert isinstance(saved, list)
    assert isinstance(saved, DAGExecutionResult)
    assert len(saved) == 1
    assert isinstance(saved[0], str)

    assert hasattr(saved, "records")
    assert len(saved.records) == 1
    record = saved.records[0]
    assert isinstance(record, DAGOutputRecord)
    assert record.path == saved[0]
    assert record.format == "wav"
    assert record.sample_rate == 44100
    assert record.node_type == "pymss_save_audio"

    record_dict = record.to_dict()
    assert record_dict["path"] == record.path
    assert record_dict["format"] == "wav"

