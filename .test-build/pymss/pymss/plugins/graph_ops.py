"""DAG-backed operations: expose a comfy-mss graph file as a pymss Operation.

``build_graph_operation`` returns a ``run(ctx, inputs, params)`` handler that
loads the graph, binds the operation's audio input and params onto graph nodes,
executes via ``run_dag``, and lifts declared node outputs to named operation
outputs. Binding errors surface at build time so broken graphs fail during
activation, not mid-task.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping

from ..graph.core import AudioArtifact, DAGError, audio_to_numpy, numpy_to_audio

def _node_index(dag: Any, node_ref: str) -> int:
    try:
        node_id = int(node_ref)
    except (TypeError, ValueError) as exc:
        raise DAGError(f"graph binding references non-integer node id {node_ref!r}") from exc
    for node in dag.nodes:
        if node.id == node_id:
            return node_id
    raise DAGError(f"graph binding references missing node {node_id}")


def build_graph_operation(
    *,
    graph_file: str | Path,
    audio_input: Mapping[str, Any] | None,
    param_bindings: Mapping[str, Any] | None = None,
    outputs: Mapping[str, Any],
    default_sr: int = 44100,
):
    """Build an Operation run handler from a comfy-mss graph file.

    Args:
        graph_file: path to the graph JSON shipped inside the plugin.
        audio_input: ``{"node": <id>, "widget": <index>, "name": <stem>}`` —
            the load node that receives the operation's audio input. When the
            graph has exactly one load node, ``node`` may be omitted.
        param_bindings: operation param name -> ``{"nodes": [<id>...]}`` —
            each target node receives the param in ``node.data["params"]``.
            Add ``"widget": <index>`` to also replace an existing widget value.
        outputs: output name -> ``{"node": <id>, "output": <slot>}`` —
            the node whose executed output becomes the named operation output.
    """
    path = Path(graph_file)
    data = json.loads(path.read_text(encoding="utf-8"))
    param_bindings = dict(param_bindings or {})
    outputs = dict(outputs)

    def _prepare(dag: Any) -> tuple[int, Any, dict[str, Any], dict[str, Any]]:
        load_nodes = [n for n in dag.nodes if str(n.type) in {"pymss_load_audio", "LoadAudio"}]
        binding = dict(audio_input or {})
        node_ref = binding.get("node")
        if node_ref is None:
            if len(load_nodes) != 1:
                raise DAGError(
                    "graph has multiple/zero load nodes; audio_input.node is required"
                )
            node_ref = load_nodes[0].id
        input_node = _node_index(dag, str(node_ref))
        if input_node not in {node.id for node in load_nodes}:
            raise DAGError("audio_input.node must reference a single-audio load node")
        widget = binding.get("widget", 0)
        if type(widget) is not int or widget not in {0, 1}:
            raise DAGError("audio input widget must be 0 (file path) or 1 (runtime input name)")
        input_name = str(binding.get("name") or "audio")

        bound_params: dict[str, tuple[list[int], int | None]] = {}
        for pname, target in param_bindings.items():
            nodes = target.get("nodes") if isinstance(target, Mapping) else None
            if not isinstance(nodes, list) or not nodes:
                raise DAGError(f"param binding for {pname!r} needs a non-empty 'nodes' list")
            node_ids = [_node_index(dag, str(n)) for n in nodes]
            widget_index = target.get("widget")
            if widget_index is not None:
                if type(widget_index) is not int or widget_index < 0:
                    raise DAGError(f"param binding for {pname!r} needs a non-negative widget index")
                for node_id in node_ids:
                    target_node = next(node for node in dag.nodes if node.id == node_id)
                    if widget_index >= len(target_node.data.get("widgets_values") or []):
                        raise DAGError(f"param binding for {pname!r} references missing widget {widget_index} on node {node_id}")
            bound_params[pname] = (node_ids, widget_index)

        bound_outputs: dict[str, tuple[int, int]] = {}
        for name, target in outputs.items():
            if not isinstance(target, Mapping) or "node" not in target:
                raise DAGError(f"output {name!r} needs a 'node' (and optional 'output' slot)")
            node_id = _node_index(dag, str(target["node"]))
            slot = target.get("output", 0)
            node = next(node for node in dag.nodes if node.id == node_id)
            output_count = max(len(node.signature.output_names), len(node.signature.output_types),
                               len(node.data.get("outputs") or []))
            if type(slot) is not int or not 0 <= slot < output_count:
                raise DAGError(f"output {name!r} references invalid output slot {slot!r} on node {node_id}")
            bound_outputs[name] = (node_id, slot)
        return input_node, widget, input_name, {"params": bound_params, "outputs": bound_outputs}

    from ..graph.comfy_loader import load_comfy_graph

    _prepare(load_comfy_graph(data))

    def _execute(ctx: Any, inputs: Mapping[str, Any], params: Mapping[str, Any], scratch: Path) -> dict[str, Any]:
        import numpy as np

        from ..graph.comfy_loader import load_comfy_graph
        from ..graph.core import run_dag

        audio_in = inputs.get("audio")
        waveform, sr = audio_to_numpy(audio_in) if audio_in is not None else (np.zeros((1, 1), dtype=np.float32), default_sr)

        dag = load_comfy_graph(data)
        input_node, widget, input_name, bindings = _prepare(dag)
        nodes_by_id = {n.id: n for n in dag.nodes}
        node = nodes_by_id[input_node]
        # Feed the load node through run_dag's runtime-input mapping: set its
        # input_name widget to our key and pass the materialized file via
        # inputs={key: path}. This is the same channel pymss-studio uses.
        input_key = f"__pymss_op_input_{input_name}"
        input_path = _audio_ref(waveform, sr, scratch)
        widgets = list((node.data.get("widgets_values") or []))
        while len(widgets) < 2:
            widgets.append("")
        widgets[widget] = input_path if widget == 0 else input_key
        if widget == 0:
            widgets[1] = ""
        node.data = dict(node.data or {})
        node.data["widgets_values"] = widgets

        for pname, (node_ids, widget_index) in bindings["params"].items():
            if pname not in params:
                continue
            for node_id in node_ids:
                node = nodes_by_id[node_id]
                merged = dict(node.data.get("params") or {}) if isinstance(node.data, Mapping) else {}
                merged[pname] = params[pname]
                node.data = dict(node.data or {})
                node.data["params"] = merged
                if widget_index is not None:
                    widgets = list(node.data["widgets_values"])
                    widgets[widget_index] = params[pname]
                    node.data["widgets_values"] = widgets

        node_results: dict[int, Any] = {}
        run_dag(
            dag,
            output_dir=str(scratch),
            inputs={input_key: input_path},
            logger=getattr(ctx, "logger", None),
            node_results=node_results,
        )

        lifted: dict[str, Any] = {}
        for name, (node_id, slot) in bindings["outputs"].items():
            record = node_results.get(node_id)
            outputs_map = getattr(record, "outputs", {}) if record is not None else {}
            if slot not in outputs_map:
                raise DAGError(f"node {node_id} did not produce declared output slot {slot} for {name!r}")
            value = outputs_map[slot]
            if isinstance(value, AudioArtifact):
                lifted[name] = numpy_to_audio(value.audio, value.sample_rate)
            else:
                lifted[name] = value
        return lifted

    def _run(ctx: Any, inputs: Mapping[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
        parent = getattr(ctx, "scratch_dir", None) or getattr(ctx, "workdir", None)
        if parent is not None:
            Path(parent).mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="pymss-graph-op-", dir=parent) as directory:
            return _execute(ctx, inputs, params, Path(directory))

    return _run


def _audio_ref(waveform: Any, sr: int, directory: Path) -> str:
    """Materialize the input waveform into the per-run scratch dir."""
    import soundfile as sf

    path = directory / "input.wav"
    sf.write(path, waveform.T if waveform.ndim > 1 else waveform, sr)
    return str(path)
