from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from pymss.graph import (
    AUDIO, AudioArtifact, DAG, DAGLink, DAGNode, NodeResult, NodeSignature,
    PortSpec, SeparatorCache, register_node, run_dag,
)


def test_cache_evicts_before_loading_and_reuses_consecutive_requests():
    active = []
    order = []

    def factory(**kwargs):
        assert not any(not entry.closed for entry in active)
        entry = SimpleNamespace(closed=False)
        def close():
            entry.closed = True
            order.append("close")
        entry.close = Mock(side_effect=close)
        active.append(entry)
        order.append("load")
        return entry

    with SeparatorCache(factory=factory) as cache:
        first = cache.get(model_name="first")
        assert cache.get(model_name="first") is first
        second = cache.get(model_name="second")
        assert first.closed and not second.closed
        assert order == ["load", "close", "load"]
    assert all(entry.closed for entry in active)
    assert all(entry.close.call_count == 1 for entry in active)


def test_cache_capacity_is_explicit_and_uses_lru():
    factory = Mock(side_effect=lambda **kwargs: SimpleNamespace(close=Mock()))
    with SeparatorCache(factory=factory, max_entries=2) as cache:
        first = cache.get(model_name="a")
        second = cache.get(model_name="b")
        assert cache.get(model_name="a") is first
        cache.get(model_name="c")
        second.close.assert_called_once()
        first.close.assert_not_called()
    with SeparatorCache(factory=factory, max_entries=None) as cache:
        first = cache.get(model_name="a")
        cache.get(model_name="b")
        first.close.assert_not_called()
        assert cache.get(model_name="a") is first


@pytest.mark.parametrize("capacity", [0, -1, True, 1.5])
def test_invalid_cache_capacity_is_rejected(capacity):
    with pytest.raises(ValueError):
        SeparatorCache(max_entries=capacity)


def test_failed_load_does_not_retain_the_previous_model():
    first = SimpleNamespace(close=Mock())
    factory = Mock(side_effect=[first, RuntimeError("Model load failed"), SimpleNamespace(close=Mock())])
    with SeparatorCache(factory=factory) as cache:
        cache.get(model_name="a")
        with pytest.raises(RuntimeError):
            cache.get(model_name="b")
        first.close.assert_called_once()
        assert cache.get(model_name="b") is not first


def test_failed_eviction_aborts_loading_and_retains_the_separator_for_cleanup():
    failure = RuntimeError("Model cleanup failed")
    first = SimpleNamespace(close=Mock(side_effect=[failure, None]))
    factory = Mock(return_value=first)
    with SeparatorCache(factory=factory) as cache:
        cache.get(model_name="a")
        with pytest.raises(RuntimeError) as caught:
            cache.get(model_name="b")
        assert caught.value is failure
        factory.assert_called_once()
        first.close.assert_called_once()
    assert first.close.call_count == 2


def test_eviction_can_retry_after_a_failed_close():
    first = SimpleNamespace(close=Mock(side_effect=[RuntimeError("Model cleanup failed"), None]))
    second = SimpleNamespace(close=Mock())
    factory = Mock(side_effect=[first, second])
    with SeparatorCache(factory=factory) as cache:
        cache.get(model_name="a")
        with pytest.raises(RuntimeError, match="Model cleanup failed"):
            cache.get(model_name="b")
        assert cache.get(model_name="b") is second
        assert first.close.call_count == 2
        assert factory.call_count == 2
    second.close.assert_called_once()


def test_failed_close_does_not_reuse_a_partially_closed_separator():
    first = SimpleNamespace(closed=False)
    second = SimpleNamespace(close=Mock())

    def close():
        first.closed = True
        if first.close.call_count == 1:
            raise RuntimeError("Model cleanup failed")

    first.close = Mock(side_effect=close)
    factory = Mock(side_effect=[first, second])
    with SeparatorCache(factory=factory) as cache:
        assert cache.get(model_name="a") is first
        with pytest.raises(RuntimeError, match="Model cleanup failed"):
            cache.get(model_name="b")
        assert first.closed
        assert cache.get(model_name="a") is second
        assert first.close.call_count == 2
    second.close.assert_called_once()


def test_final_cache_close_continues_after_a_separator_error():
    first = SimpleNamespace(close=Mock(side_effect=RuntimeError("Model cleanup failed")))
    second = SimpleNamespace(close=Mock())
    factory = Mock(side_effect=[first, second])
    cache = SeparatorCache(factory=factory, max_entries=None)
    cache.get(model_name="a")
    cache.get(model_name="b")
    cache.close()
    cache.close()
    first.close.assert_called_once()
    second.close.assert_called_once()


def make_dag(node_type, audios, received):
    register_node("lifecycle_source", signature=lambda node: NodeSignature([], ["audio"], [AUDIO]),
                  execute=lambda ctx, inputs: NodeResult(outputs={0: audios}))
    def sink(ctx, inputs):
        value = inputs["audio"]
        received.extend(value if isinstance(value, list) else [value])
        return NodeResult()
    register_node("lifecycle_sink", signature=lambda node: NodeSignature([PortSpec("audio", AUDIO)], [], []), execute=sink)
    data = {"widgets_values": ["model", "cpu", False, "modelscope", "0", False],
            "outputs": [{"name": "audios" if node_type.endswith("_list") else "Vocals (Audio)", "type": AUDIO}]}
    if "custom" in node_type:
        data.update(model_path="model.ckpt", config_path="model.yaml", widgets_values=["", "bs_roformer", "cpu", "0", False])
    return DAG(nodes=[
        DAGNode("source", "lifecycle_source"),
        DAGNode("split", node_type, inputs=[DAGLink(1, "source", 0, "split", 0, AUDIO)], data=data),
        DAGNode("sink", "lifecycle_sink", inputs=[DAGLink(2, "split", 0, "sink", 0, AUDIO)]),
    ])


@pytest.mark.parametrize("kind", ["mss_separate", "vr_separate", "custom_mss_separate"])
@pytest.mark.parametrize("suffix", ["", "_list"])
@pytest.mark.parametrize("prefix", ["", "pymss_"])
@pytest.mark.parametrize("quiet", [False, True])
def test_native_graph_lists_report_each_completion_and_keep_metadata_model_open(tmp_path, kind, suffix, prefix, quiet):
    audios = [AudioArtifact(np.ones((2, 256), dtype=np.float32), 1000) for _ in range(3)]
    received, events, legacy = [], [], []
    separator = SimpleNamespace(
        config=SimpleNamespace(audio={"sample_rate": 1000}, training=SimpleNamespace(
            instruments=["Vocals", "Instrumental"], target_instrument=None)), close=Mock(),
        model_type="vr" if kind == "vr_separate" else "bs_roformer",
    )
    def separate(mix, **kwargs):
        separator.close.assert_not_called()
        if not quiet:
            for done in (0, 50, 100):
                separator.progress_callback(done, 100, "Processing audio")
        return {"Vocals": mix * 0.6, "Instrumental": mix * 0.4}
    separator.separate = Mock(side_effect=separate)
    factory = Mock(return_value=separator)
    with SeparatorCache(factory=factory) as cache:
        run_dag(make_dag(prefix + kind + suffix, audios, received), output_dir=tmp_path,
                separator_cache=cache, progress_event_callback=events.append,
                progress_callback=lambda *args: legacy.append(args))
    factory.assert_called_once()
    separator.close.assert_called_once()
    assert separator.separate.call_count == 3
    assert len(received) == (6 if suffix else 1)
    complete = [event for event in events if event["message"].endswith("Audio processing completed")]
    assert [event["audio_index"] for event in complete] == [1, 2, 3]
    assert all(event["audio_count"] == 3 and "done" not in event for event in complete)
    assert complete[0]["overall_fraction"] < complete[1]["overall_fraction"] < complete[2]["overall_fraction"]
    assert [event["overall_fraction"] for event in events] == sorted(event["overall_fraction"] for event in events)
    assert events[-1]["overall_fraction"] == 1
    assert all(len(args) == 3 for args in legacy)


def test_failed_audio_is_not_reported_completed_and_owned_cache_is_closed(tmp_path, monkeypatch):
    separator = SimpleNamespace(config=SimpleNamespace(audio={"sample_rate": 1000}), close=Mock())
    separator.separate = Mock(side_effect=[{"Vocals": np.ones((2, 256), np.float32)}, RuntimeError("Separation failed")])
    monkeypatch.setattr(SeparatorCache, "_default_factory", Mock(return_value=separator))
    events = []
    audios = [AudioArtifact(np.ones((2, 256), np.float32), 1000) for _ in range(3)]
    with pytest.raises(RuntimeError, match="Separation failed"):
        run_dag(make_dag("mss_separate", audios, []), output_dir=tmp_path, progress_event_callback=events.append)
    separator.close.assert_called_once()
    assert [event["audio_index"] for event in events if event["message"].endswith("Audio processing completed")] == [1]


def test_vr_tta_structured_progress_combines_passes_without_changing_legacy_numbers(tmp_path):
    from pymss.graph import NodeContext
    from pymss.graph.nodes import _progress_for
    events, legacy = [], []
    node = DAGNode("vr", "vr_separate")
    ctx = NodeContext(tmp_path, None, False, lambda *args: legacy.append(args), SeparatorCache(),
                      nodes_by_id={"vr": node}, node_count=1, progress_event_callback=events.append)
    callback = _progress_for(ctx, "vr", unit="batches", passes=2)
    for done in (0, 50, 100, 0, 50, 100):
        callback(done, 100, "Processing VR batches")
    assert [event["overall_fraction"] for event in events] == [0, 0.25, 0.5, 0.5, 0.75, 1]
    assert [done for done, _total, _message in legacy] == [0, 50, 100, 0, 50, 100]
