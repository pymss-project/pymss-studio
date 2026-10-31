from copy import deepcopy
from asyncio import CancelledError
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from pymss.graph import (
    AUDIO, STRING, EXECUTION_EVENT_VERSION, PROGRESS_EVENT_VERSION,
    AudioArtifact, DAG, DAGError, DAGLink, DAGNode,
    LegacyWorkflowRunner, NodeResult, NodeSignature, PortSpec, SeparatorCache,
    StringArtifact, load_comfy_graph, register_node, run_dag,
)
from pymss.workflow import load_workflow_data


def make_graph(execute=None):
    register_node("events_source", signature=lambda node: NodeSignature([], ["value"], [STRING]),
                  execute=lambda ctx, inputs: NodeResult(outputs={0: StringArtifact("value")}))
    register_node("events_work", signature=lambda node: NodeSignature([PortSpec("value", STRING)], [], []),
                  execute=execute or (lambda ctx, inputs: NodeResult()))
    return DAG(nodes=[DAGNode("source", "events_source"), DAGNode(
        "work", "events_work", inputs=[DAGLink(1, "source", 0, "work", 0, STRING)],
    )])


def event_names(events):
    return [event["event"] for event in events]


def test_lifecycle_is_explicit_and_legacy_progress_is_unchanged(tmp_path):
    def execute(ctx, inputs):
        ctx.report_progress(ctx.nodes_by_id["work"], 1, "Audio completed", audio_index=1, audio_count=1)
        return NodeResult()

    baseline, baseline_legacy = [], []
    run_dag(make_graph(execute), output_dir=tmp_path, progress_event_callback=baseline.append,
            progress_callback=lambda *args: baseline_legacy.append(args))
    events, progress, legacy = [], [], []
    run_dag(make_graph(execute), output_dir=tmp_path, execution_event_callback=events.append,
            progress_event_callback=progress.append, progress_callback=lambda *args: legacy.append(args))
    assert PROGRESS_EVENT_VERSION == 1
    assert progress == baseline and legacy == baseline_legacy
    assert all("event" not in event and "version" not in event for event in progress)
    assert event_names(events) == ["execution_started", "node_started", "node_completed", "node_started",
                                   "node_progress", "node_completed", "execution_completed"]
    assert all(event["version"] == EXECUTION_EVENT_VERSION for event in events)
    assert events[4]["node_id"] == "work" and events[4]["node_fraction"] == 1
    assert events[4]["audio_index"] == events[4]["audio_count"] == 1
    assert events[-1]["node_id"] is None and events[-1]["overall_fraction"] == 1


def test_failure_after_full_audio_progress_is_not_a_node_completion(tmp_path, monkeypatch):
    failure = RuntimeError("Separation failed")
    separator = SimpleNamespace(close=Mock())
    monkeypatch.setattr(SeparatorCache, "_default_factory", Mock(return_value=separator))

    def execute(ctx, inputs):
        ctx.separator_cache.get(model_name="model")
        ctx.report_progress(ctx.nodes_by_id["work"], 1, "Audio completed", audio_index=1, audio_count=1)
        raise failure

    events = []
    with pytest.raises(RuntimeError) as caught:
        run_dag(make_graph(execute), output_dir=tmp_path, execution_event_callback=events.append)
    assert caught.value is failure
    assert event_names(events)[-2:] == ["node_failed", "execution_failed"]
    assert events[-1]["node_id"] == "work" and events[-1]["error_type"] == "RuntimeError"
    assert not any(event["event"] == "node_completed" and event["node_id"] == "work" for event in events)
    separator.close.assert_called_once()


def test_input_resolution_failure_is_attributed_to_the_consuming_node(tmp_path):
    dag = make_graph()
    dag.nodes[1].inputs[0].source_slot = 12
    events = []
    with pytest.raises(DAGError, match="output slot"):
        run_dag(dag, output_dir=tmp_path, execution_event_callback=events.append)
    assert events[-1]["node_id"] == "work"
    assert event_names(events)[-2:] == ["node_failed", "execution_failed"]


def test_validation_failure_has_no_fabricated_active_node(tmp_path):
    dag = make_graph()
    dag.nodes[0].inputs = [DAGLink(2, "work", 0, "source", 0, STRING)]
    events = []
    with pytest.raises(DAGError, match="cycle"):
        run_dag(dag, output_dir=tmp_path, execution_event_callback=events.append)
    assert event_names(events) == ["execution_started", "execution_failed"]
    assert events[-1]["node_id"] is None


def test_explicit_non_strict_skipping_is_distinct_from_completion(tmp_path):
    events = []
    run_dag(DAG(nodes=[DAGNode("unknown", "events_unknown")]), output_dir=tmp_path,
            strict=False, execution_event_callback=events.append)
    assert event_names(events) == ["execution_started", "node_skipped", "execution_completed"]


def test_empty_graph_completes_without_node_events(tmp_path):
    events = []
    assert run_dag(DAG(), output_dir=tmp_path, execution_event_callback=events.append) == []
    assert event_names(events) == ["execution_started", "execution_completed"]
    assert events[-1]["overall_fraction"] == 1


@pytest.mark.parametrize("owned_cache", [False, True])
def test_native_separator_progress_supports_events_without_legacy_callback(
    tmp_path, monkeypatch, owned_cache,
):
    events = []
    register_node("events_audio", signature=lambda node: NodeSignature([], ["audio"], [AUDIO]),
                  execute=lambda ctx, inputs: NodeResult(outputs={0: AudioArtifact(np.zeros((2, 64)), 1000)}))
    separator = SimpleNamespace(config=SimpleNamespace(audio={"sample_rate": 1000}), close=Mock(), model_type="bs_roformer")

    def separate(mix, **kwargs):
        separator.progress_callback(1, 4, "Separating")
        separator.progress_callback(4, 4, "Separating")
        return {"Vocals": np.zeros((64, 2), dtype=np.float32)}

    separator.separate = separate
    dag = DAG(nodes=[DAGNode("input", "events_audio"), DAGNode("split", "mss_separate",
        inputs=[DAGLink(1, "input", 0, "split", 0, AUDIO)],
        data={"widgets_values": ["model", "cpu", False, "modelscope", "0", False],
              "outputs": [{"name": "Vocals (Audio)", "type": AUDIO}]})])
    monkeypatch.setattr(SeparatorCache, "_default_factory", Mock(return_value=separator))
    with (nullcontext() if owned_cache else SeparatorCache()) as cache:
        run_dag(dag, output_dir=tmp_path, separator_cache=cache, execution_event_callback=events.append)
        if not owned_cache:
            separator.close.assert_not_called()
    separator.close.assert_called_once()
    progress = next(event for event in events if event["event"] == "node_progress" and "done" in event)
    assert progress["done"] == 1 and progress["total"] == 4 and progress["unit"] == "seconds"
    assert event_names(events)[-2:] == ["node_completed", "execution_completed"]
    assert events[-2]["node_id"] == "split"


@pytest.mark.parametrize("exception", [KeyboardInterrupt, CancelledError])
def test_node_interruptions_are_not_failures_and_propagate(tmp_path, exception):
    failure, events = exception("Interrupted"), []

    def execute(ctx, inputs):
        raise failure

    with pytest.raises(exception) as caught:
        run_dag(make_graph(execute), output_dir=tmp_path, execution_event_callback=events.append)
    assert caught.value is failure
    assert event_names(events)[-2:] == ["node_cancelled", "execution_cancelled"]


def test_observer_failure_during_execution_error_does_not_mask_the_original(tmp_path):
    failure, observer_failure = ValueError("Node failed"), RuntimeError("Observer failed")

    def observe(event):
        if event["event"] == "node_failed":
            raise observer_failure

    with pytest.raises(ValueError) as caught:
        run_dag(make_graph(Mock(side_effect=failure)), output_dir=tmp_path, execution_event_callback=observe)
    assert caught.value is failure and caught.value.__context__ is observer_failure


def test_observer_failure_preserves_the_original_explicit_cause(tmp_path):
    root_cause = LookupError("Model unavailable")
    failure, observer_failure = ValueError("Node failed"), RuntimeError("Observer failed")

    def execute(ctx, inputs):
        raise failure from root_cause

    def observe(event):
        if event["event"] == "node_failed":
            raise observer_failure

    with pytest.raises(ValueError) as caught:
        run_dag(make_graph(execute), output_dir=tmp_path, execution_event_callback=observe)
    assert caught.value is failure
    assert failure.__cause__ is root_cause and failure.__context__ is observer_failure


def test_without_an_execution_observer_failures_are_not_formatted(tmp_path):
    class Failure(ValueError):
        def __str__(self):
            pytest.fail("An unobserved failure must not be formatted by the event layer")

    failure = Failure()
    with pytest.raises(Failure) as caught:
        run_dag(make_graph(Mock(side_effect=failure)), output_dir=tmp_path)
    assert caught.value is failure


def test_observer_error_is_not_reported_as_a_node_failure(tmp_path, monkeypatch):
    failure, events = RuntimeError("Observer failed"), []
    separator = SimpleNamespace(close=Mock())
    monkeypatch.setattr(SeparatorCache, "_default_factory", Mock(return_value=separator))

    def execute(ctx, inputs):
        ctx.separator_cache.get(model_name="model")
        ctx.report_progress(ctx.nodes_by_id["work"], 0.5, "Separating")

    def observe(event):
        events.append(event)
        if event["event"] == "node_progress":
            raise failure

    with pytest.raises(RuntimeError) as caught:
        run_dag(make_graph(execute), output_dir=tmp_path, execution_event_callback=observe)
    assert caught.value is failure
    assert event_names(events)[-1] == "node_progress"
    assert "node_failed" not in event_names(events)
    separator.close.assert_called_once()


def test_comfy_json_and_output_semantics_are_unchanged(tmp_path):
    definition = {"nodes": [
        {"id": 1, "type": "EmptyAudio", "widgets_values": [0.01, 44100, 2]},
        {"id": 2, "type": "SaveAudio", "widgets_values": ["track"],
         "inputs": [{"name": "audio", "type": AUDIO, "link": 1}]},
    ], "links": [[1, 1, 0, 2, 0, AUDIO]], "extra": {"editor": "ComfyUI"}}
    original = deepcopy(definition)
    saved = run_dag(load_comfy_graph(definition), output_dir=tmp_path, execution_event_callback=lambda event: None)
    assert definition == original and len(saved) == 1
    assert saved.records[0].node_id == 2


@pytest.mark.parametrize("continue_on_error", [False, True])
@pytest.mark.parametrize("exception", [KeyboardInterrupt, CancelledError])
def test_folder_runner_propagates_interruptions_and_stops_the_batch(tmp_path, monkeypatch, continue_on_error, exception):
    import pymss.graph.runner as module
    for name in ("a.wav", "b.wav"):
        (tmp_path / name).touch()
    calls, events = [], []

    def execute(dag, **kwargs):
        calls.append(kwargs)
        kwargs["execution_event_callback"]({"event": "execution_cancelled"})
        raise exception("Interrupted")

    monkeypatch.setattr(module, "run_dag", execute)
    workflow = load_workflow_data({"version": 1, "steps": [{"id": "split", "model": "model", "stems": ["Vocals"]}]})
    runner = LegacyWorkflowRunner(workflow, continue_on_error=continue_on_error,
                                  execution_event_callback=events.append)
    with pytest.raises(exception):
        runner.run(tmp_path, tmp_path / "outputs")
    assert len(calls) == 1 and events == [{"event": "execution_cancelled", "track": "a"}]
