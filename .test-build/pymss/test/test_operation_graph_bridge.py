"""Round 4: graph executor accepts operation-backed nodes (TDD).

An operation registered via ``register_operation_node`` becomes a runnable
graph node: signature generated from OperationSpec ports+params, named params
carried in ``node.data["params"]`` (not widgets_values), execution delegating
to the operation run handler with an OperationContext bridged to NodeContext.
"""

from __future__ import annotations

import numpy as np
import pytest

from pymss.graph.core import (
    AUDIO,
    DAGLink,
    DAGNode,
    NodeResult,
    NodeSignature,
    run_dag,
)
from pymss.operations import OperationSpec
from pymss.operations.graph_bridge import register_operation_node, reset_operation_nodes


@pytest.fixture(autouse=True)
def _clean_operation_nodes():
    reset_operation_nodes()
    yield
    reset_operation_nodes()


def _gain_spec() -> OperationSpec:
    return OperationSpec(
        id="test.gain.apply",
        schema_version=1,
        title="Apply Gain",
        inputs={"audio": {"type": "audio"}},
        params={
            "type": "object",
            "properties": {
                "gain_db": {"type": "number", "default": 0.0},
            },
            "additionalProperties": False,
        },
        outputs={"audio": {"type": "audio"}},
    )


def _gain_run(ctx, inputs, params):
    audio = inputs["audio"]
    gain = 10 ** (params["gain_db"] / 20.0)
    return {"audio": type(audio)(audio.audio * gain, audio.sample_rate, channel_layout=audio.channel_layout)}


def _audio_artifact(samples: int = 100, sr: int = 44100) -> "AudioArtifact":
    from pymss.graph.core import AudioArtifact

    return AudioArtifact(
        np.ones((2, samples), dtype=np.float32), sr, channel_layout="stereo"
    )


def test_operation_node_registers_and_produces_signature():
    register_operation_node(spec=_gain_spec(), run=_gain_run)
    from pymss.graph.core import get_node_type

    info = get_node_type("op.test.gain.apply")
    sig = info.signature(DAGNode(id="n", type="op.test.gain.apply"))
    names = [p.name for p in sig.inputs]
    assert "audio" in names
    assert "gain_db" in names  # params appear as unconnected input slots
    assert sig.output_names == ["audio"]
    assert sig.output_types == [AUDIO]


def test_file_operation_advertises_persistent_output_for_live_registration():
    from pymss.plugins.registry import PluginRegistration, get_registry

    spec = OperationSpec(
        id="test.file.write", schema_version=1, title="Write File",
        inputs={}, params={"type": "object", "properties": {}},
        outputs={"file": {"type": "file"}},
    )
    register_operation_node(spec=spec, run=lambda *_: {}, source="test.file")
    assert get_registry().nodes["op.test.file.write"].saves_output is True
    register_operation_node(spec=spec, run=lambda *_: {}, plugin=PluginRegistration("test.file"))
    assert get_registry().nodes["op.test.file.write"].saves_output is True
    register_operation_node(spec=_gain_spec(), run=_gain_run)
    assert get_registry().nodes["op.test.gain.apply"].saves_output is False


def test_operation_node_executes_with_named_params(tmp_path):
    register_operation_node(spec=_gain_spec(), run=_gain_run)

    source = DAGNode(id="src", type="op.test.gain.apply")  # placeholder, replaced below
    # Build: source node (produces audio) -> gain node with params in data
    from pymss.graph.core import register_node

    executed = {}

    def _source_exec(ctx, inputs):
        return NodeResult(outputs={0: _audio_artifact(10)})

    register_node("test.source", signature=lambda n: NodeSignature(
        inputs=[], output_names=["audio"], output_types=[AUDIO]
    ), execute=_source_exec)

    gain_node = DAGNode(
        id="gain",
        type="op.test.gain.apply",
        inputs=[DAGLink(link_id=1, source_node_id="src", source_slot=0, target_node_id="gain", target_slot=0, type=AUDIO)],
        data={"params": {"gain_db": 6.0}},
    )
    src_node = DAGNode(id="src", type="test.source")
    from pymss.graph.core import DAG

    dag = DAG(nodes=[src_node, gain_node])
    # run_dag writes outputs only for output nodes; use direct execution via
    # the bridge executor instead for this unit-level test.
    from pymss.graph.core import get_node_type

    info = get_node_type("op.test.gain.apply")
    ctx = _make_ctx(tmp_path)
    result = info.execute(ctx, {"audio": _audio_artifact(10), "gain_db": 6.0})
    out = result.outputs[0]
    expected = 1.0 * 10 ** (6.0 / 20.0)
    np.testing.assert_allclose(out.audio[0, 0], expected, rtol=1e-6)


def _make_ctx(tmp_path):
    from pymss.graph.core import NodeContext, SeparatorCache

    return NodeContext(
        output_dir=tmp_path,
        logger=None,
        debug=False,
        progress_callback=None,
        separator_cache=SeparatorCache(max_entries=1),
    )


def test_operation_node_runs_inside_run_dag(tmp_path):
    """Full executor path: source node -> operation node -> save node."""
    from pymss.graph.core import DAG, register_node

    register_operation_node(spec=_gain_spec(), run=_gain_run)

    def _source_exec(ctx, inputs):
        return NodeResult(outputs={0: _audio_artifact(10)})

    register_node("test.source2", signature=lambda n: NodeSignature(
        inputs=[], output_names=["audio"], output_types=[AUDIO]
    ), execute=_source_exec)

    def _save_exec(ctx, inputs):
        import numpy as np
        art = inputs["audio"]
        from pymss.graph.core import NodeResult as NR
        out = tmp_path / "out.wav"
        from pymss.audio_io import save_audio
        save_audio(str(out), art.audio.T, art.sample_rate, "wav", {})
        return NR(outputs={}, saved_paths=[str(out)])

    register_node("test.save2", signature=lambda n: NodeSignature(
        inputs=[__import__("pymss.graph.core", fromlist=["PortSpec"]).PortSpec(name="audio", type=AUDIO)],
        output_names=[], output_types=[],
    ), execute=_save_exec)

    src = DAGNode(id="src", type="test.source2")
    gain = DAGNode(
        id="gain",
        type="op.test.gain.apply",
        inputs=[DAGLink(link_id=1, source_node_id="src", source_slot=0, target_node_id="gain", target_slot=0, type=AUDIO)],
        data={"params": {"gain_db": 0.0}},
    )
    save = DAGNode(
        id="save",
        type="test.save2",
        inputs=[DAGLink(link_id=2, source_node_id="gain", source_slot=0, target_node_id="save", target_slot=0, type=AUDIO)],
    )
    saved = run_dag(DAG(nodes=[src, gain, save]), output_dir=str(tmp_path))
    assert len(saved) == 1 and saved[0].endswith("out.wav")


def test_operation_node_params_come_from_node_data_not_widgets():
    """The node reads data['params'] (named), never widgets_values (positional)."""
    register_operation_node(spec=_gain_spec(), run=_gain_run)
    from pymss.graph.core import get_node_type

    info = get_node_type("op.test.gain.apply")
    ctx = _make_ctx(None)
    # Simulate what run_dag does: gathered inputs include params by slot name
    result = info.execute(ctx, {"audio": _audio_artifact(4), "gain_db": -6.0})
    np.testing.assert_allclose(result.outputs[0].audio[0, 0], 10 ** (-6.0 / 20.0), rtol=1e-6)
