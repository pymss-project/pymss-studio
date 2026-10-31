"""Bridge: register Operations as executable graph nodes.

Round 4 of docs/plugin-redesign-plan.md. ``register_operation_node`` derives
a graph node type from an ``OperationSpec``:

- node type name: ``op.<operation id>`` (stable, namespaced, collision-safe
  against builtin node names)
- signature: spec inputs become AUDIO-typed input slots (other port types
  map to STRING for now), spec params become additional unconnected input
  slots so the legacy graph UI can still attach values positionally
- execution: named params are read from ``node.data["params"]`` when the
  executor receives the node (via ``ctx.current_node_id``), falling back to
  values gathered by input-slot name; validation and defaults come from the
  spec itself — no widgets_values positional parsing in plugin code
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from ..graph.core import (
    AUDIO,
    STRING,
    DAGNode,
    NodeContext,
    NodeResult,
    NodeSignature,
    PortSpec,
)
from . import OperationSpec, _invoke_operation
from ..plugins.registry import get_registry

__all__ = ["register_operation_node", "reset_operation_nodes", "operation_node_type"]

_PREFIX = "op."


def operation_node_type(operation_id: str) -> str:
    return _PREFIX + operation_id


def register_operation_node(
    *,
    spec: OperationSpec,
    run: Callable[..., Any],
    source: str = "operation",
    node_type: str | None = None,
    plugin: Any = None,
) -> str:
    """Register one operation as a graph node type; returns the node type."""
    node_type = node_type or operation_node_type(spec.id)

    param_names = list(spec.params.get("properties", {}).keys())
    audio_inputs = list(spec.inputs.keys())
    output_names = list(spec.outputs.keys())

    def _signature(node: DAGNode) -> NodeSignature:
        inputs: list[PortSpec] = [
            PortSpec(name=name, type=_port_type(spec.inputs[name])) for name in audio_inputs
        ]
        # Params ride as optional unconnected slots so gather-by-name works.
        for pname in param_names:
            inputs.append(PortSpec(name=pname, type=_param_port_type(spec, pname)))
        types = [_port_type(spec.outputs[name]) for name in output_names]
        return NodeSignature(inputs=inputs, output_names=output_names, output_types=types)

    def _execute(ctx: NodeContext, inputs: Mapping[str, Any]) -> NodeResult:
        op_inputs = {name: inputs[name] for name in audio_inputs if name in inputs}
        # Params: explicit node.data["params"] wins; gathered slot values
        # (positional widget-style) fill the rest; spec defaults apply after.
        params: dict[str, Any] = {}
        node = ctx.nodes_by_id.get(ctx.current_node_id) if ctx.current_node_id is not None else None
        if node is not None and isinstance(node.data.get("params"), Mapping):
            params.update(node.data["params"])
        for pname in param_names:
            if pname in inputs and pname not in params:
                value = inputs[pname]
                # Graph links deliver Artifacts; parameter validation needs the
                # underlying value (e.g. StringConstant → a string param).
                params[pname] = getattr(value, "value", value)
        from . import OperationContext

        op_ctx = OperationContext(
            workdir=str(ctx.output_dir) if ctx.output_dir is not None else None,
            progress=lambda fraction, message="", event=None: ctx.report_progress(
                node, float(fraction), message or f"operation {spec.id}"
            ) if node is not None else None,
            logger=ctx.logger,
            cancelled=getattr(ctx, "cancelled", None),
            host_request=getattr(ctx, "host_request", None),
        )
        result = _invoke_operation(spec, run, op_inputs, params, op_ctx)
        from .files import FileArtifact
        from ..graph.core import DAGOutputRecord

        outputs: dict[int, Any] = {}
        for slot, name in enumerate(output_names):
            if name in result:
                outputs[slot] = result[name]
        files = [value for value in outputs.values() if isinstance(value, FileArtifact)]
        return NodeResult(outputs=outputs, saved_paths=[value.path for value in files], saved_records=[
            DAGOutputRecord(path=value.path, stem=Path(value.path).stem,
                            node_id=node.id if node is not None else ctx.current_node_id, node_type=node_type,
                            kind="file", media_type=value.media_type)
            for value in files
        ])

    saves_output = any(port.get("type") == "file" for port in spec.outputs.values())
    if plugin is None:
        get_registry().register_node(
            node_type, _execute, signature=_signature, source=source, saves_output=saves_output,
        )
    else:
        plugin.register_node(node_type, _execute, signature=_signature, saves_output=saves_output)
    return node_type


def _port_type(descriptor: Mapping[str, Any]) -> str:
    t = descriptor.get("type", "string")
    return AUDIO if t == "audio" else STRING


def _param_port_type(spec: OperationSpec, pname: str) -> str:
    # Graph port tags are coarse; number/int/bool all ride as STRING slots in
    # the legacy UI. The spec validates real types at execution time.
    return STRING


def reset_operation_nodes() -> None:
    """Remove all op.* node registrations (test isolation)."""
    from ..graph.core import _PLUGIN_REGISTRY

    for node_type in [n for n in list(_PLUGIN_REGISTRY.nodes) if n.startswith(_PREFIX)]:
        del _PLUGIN_REGISTRY.nodes[node_type]
