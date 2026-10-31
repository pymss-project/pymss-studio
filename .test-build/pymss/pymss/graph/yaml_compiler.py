"""Compile the legacy linear YAML workflow into the unified DAG.

The YAML workflow format (see ``pymss/workflow.py``) is a flat list of steps,
each running one model over an input that is either ``input`` (the original
audio) or ``<step_id>.<stem>`` (an upstream step's stem). Steps also declare
which stems to ``save`` and where.

This compiler turns that flat list into the same DAG representation used by
:mod:`pymss.comfy_loader`, so both formats share the single execution core in
:mod:`pymss.dag`.

Mapping summary
---------------

For each YAML step:

* One ``pymss_mss_params`` (or ``pymss_vr_params``) node carrying the step's
  ``inference_params``.
* One separation node (``mss_separate`` / ``vr_separate`` / ``custom_mss_separate``)
  fed by the params node and by the step's declared ``input`` source.
* For every stem listed under ``save``, one ``pymss_save_audio`` node wired to
  the matching stem output slot. Stems NOT listed in ``save`` are still produced
  (so downstream steps can consume them) but simply not saved.

Folder batching is handled one layer up in :class:`LegacyWorkflowRunner`:
each input file is run as its own DAG execution, with separator caching shared
across files so the same model is loaded once for the whole batch.
"""

from __future__ import annotations

import os
from dataclasses import field
from pathlib import Path
from typing import Any

from .core import (
    DAG,
    DAGError,
    DAGLink,
    DAGNode,
    NodeSignature,
    PortSpec,
    AUDIO,
    STRING,
    MSS_PARAMS,
    VR_PARAMS,
    get_node_type,
)
from ..workflow import Workflow, WorkflowStep, validate_workflow


# Custom model types understood by ``custom_mss_separate``. Mirrors
# ``comfy_mss/nodes/separate.py::_CustomSeparateBase.MODEL_TYPES``.
CUSTOM_MODEL_TYPES = [
    "auto",
    "mel_band_roformer",
    "bs_roformer",
    "bs_roformer_hyperace",
    "bs_conformer",
    "mel_band_conformer",
    "mdx23c",
    "htdemucs",
    "apollo",
    "bandit",
    "bandit_v2",
    "scnet",
]


def compile_workflow_to_dag(workflow: Workflow) -> DAG:
    """Turn a validated :class:`Workflow` into a :class:`DAG`.

    The resulting DAG has stable, human-readable node ids (``step:<id>``,
    ``params:<id>``, ``save:<id>:<stem>``) so error messages stay legible.
    """

    workflow = validate_workflow(workflow)

    dag = DAG()
    dag.meta = {"source": "yaml", "version": workflow.version, "defaults": dict(workflow.defaults)}

    # Single source-of-truth input node. Its audio is supplied at run time via
    # ``NodeContext.input_path`` (set by the runner per file).
    input_node = _build_node(
        node_id="input",
        node_type="input_audio",
        inputs=[],
        data={},
    )
    input_node.signature = _signature_of(input_node)
    dag.nodes.append(input_node)

    # Track output slot layout per step so we can wire ``step.stem`` references.
    # step_id -> (separation_node_id, {stem_name: output_slot})
    produced: dict[str, tuple[str, dict[str, int]]] = {}

    # First pass: build separation + params nodes and record stem slots.
    step_separation_nodes: list[tuple[WorkflowStep, DAGNode]] = []
    for step in workflow.steps:
        kind = _step_kind(step)
        params_node = _build_params_node(step, kind)
        if params_node is not None:
            params_node.signature = _signature_of(params_node)
            dag.nodes.append(params_node)

        sep_node = _build_separation_node(step, kind, input_id="input", params_id=params_node.id if params_node else None)
        sep_node.signature = _signature_of(sep_node)
        dag.nodes.append(sep_node)
        step_separation_nodes.append((step, sep_node))

        stems = _step_stems(step)
        produced[step.id] = (sep_node.id, {stem: i for i, stem in enumerate(stems)})

    # Second pass: wire each step's audio input.
    for step, sep_node in step_separation_nodes:
        link = _resolve_input_link(step, produced, input_node.id)
        if link is not None:
            sep_node.inputs[0] = link

    # Third pass: emit save nodes for declared ``save`` stems.
    save_index = 0
    for step, sep_node in step_separation_nodes:
        sep_id, stem_slots = produced[step.id]
        save_map = _step_save_map(step)
        for stem, save_dir in save_map.items():
            slot = stem_slots.get(stem)
            if slot is None:
                # Be lenient: a save entry for a stem the model doesn't produce
                # is a user error, but validate_workflow_structure should have
                # caught it. Skip defensively rather than crash the whole run.
                continue
            save_node = _build_save_node(step, stem, save_dir, save_index, sep_id, slot)
            save_node.signature = _signature_of(save_node)
            dag.nodes.append(save_node)
            save_index += 1

    return dag


# ---------------------------------------------------------------------------
# Node builders
# ---------------------------------------------------------------------------


_node_counter = [0]


def _next_id(prefix: str) -> str:
    _node_counter[0] += 1
    return f"{prefix}"


def _build_node(node_id: str, node_type: str, inputs: list[DAGLink | None], data: dict[str, Any]) -> DAGNode:
    node = DAGNode(id=node_id, type=node_type, inputs=list(inputs), data=dict(data), title=node_id)
    return node


def _build_params_node(step: WorkflowStep, kind: str) -> DAGNode | None:
    if kind == "vr":
        node_type = "pymss_vr_params"
        ip = step.inference_params or {}
        widgets = [
            int(ip.get("batch_size", 1) or 1),
            int(ip.get("window_size", 512) or 512),
            int(ip.get("aggression", 5) or 5),
            bool(ip.get("enable_tta", False)),
            bool(ip.get("high_end_process", False)),
            bool(ip.get("enable_post_process", False)),
            float(ip.get("post_process_threshold", 0.2) or 0.2),
            bool(ip.get("normalize", False)),
        ]
    else:
        node_type = "pymss_mss_params"
        ip = step.inference_params or {}
        overlap = ip.get("overlap_size")
        chunk = ip.get("chunk_size")
        widgets = [
            int(ip.get("batch_size", 1) or 1),
            "Default" if overlap in (None, "Default") else str(overlap),
            "Default" if chunk in (None, "Default") else str(chunk),
            bool(ip.get("normalize", False)),
            bool(ip.get("enable_tta", step.use_tta or False)),
            bool(ip.get("standardize", False)),
        ]
    node = _build_node(
        node_id=f"params:{step.id}",
        node_type=node_type,
        inputs=[],
        data={"widgets_values": widgets},
    )
    return node


def _build_separation_node(step: WorkflowStep, kind: str, *, input_id: str, params_id: str | None) -> DAGNode:
    if kind == "custom":
        node_type = "custom_mss_separate"
    elif kind == "vr":
        node_type = "vr_separate"
    else:
        node_type = "mss_separate"
    # ``model_name`` widget; device is taken from defaults at run time via the
    # context, but we also bake the step's own device override in if present.
    device = step.device or ""
    model_name = step.model or ""
    widgets: list[Any]
    if kind == "custom":
        model_type = step.model_type or "mel_band_roformer"
        widgets = [model_name, model_type, device or "auto", "0", False]
    else:
        widgets = [model_name, device or "auto", True, "modelscope", "0", False]

    inputs: list[DAGLink | None] = [None, None]  # [audio, params]
    if params_id is not None:
        inputs[1] = DAGLink(
            link_id=_link_id(),
            source_node_id=params_id,
            source_slot=0,
            target_node_id=f"step:{step.id}",
            target_slot=1,
            type=VR_PARAMS if kind == "vr" else MSS_PARAMS,
        )
    node = _build_node(
        node_id=f"step:{step.id}",
        node_type=node_type,
        inputs=inputs,
        data={
            "widgets_values": widgets,
            # Declare the stems the user asked for so the executor emits exactly
            # those output slots (and so save-node wiring can find them).
            "outputs": [
                {"name": f"{stem} (Audio)", "type": AUDIO}
                for stem in _step_stems(step)
            ],
        },
    )
    if kind == "custom":
        node.data["model_path"] = step.model_path
        node.data["config_path"] = step.config_path
    return node


def _build_save_node(step: WorkflowStep, stem: str, save_dir: Any, index: int, sep_id: str, stem_slot: int) -> DAGNode:
    save_id = f"save:{step.id}:{stem}"
    output_format = step.output_format or "wav"
    widgets = [output_format, str(save_dir or "Default"), "44100", "FLOAT", "PCM_24", "320k"]
    link = DAGLink(
        link_id=_link_id(),
        source_node_id=sep_id,
        source_slot=stem_slot * 2,  # audio slots are even (audio/string interleaved)
        target_node_id=save_id,
        target_slot=0,
        type=AUDIO,
    )
    node = _build_node(
        node_id=save_id,
        node_type="pymss_save_audio",
        inputs=[link, None, None, None, None, None, None, None],
        data={"widgets_values": widgets},
    )
    return node


# ---------------------------------------------------------------------------
# Input reference resolution
# ---------------------------------------------------------------------------


def _resolve_input_link(step: WorkflowStep, produced: dict[str, tuple[str, dict[str, int]]], input_node_id: str) -> DAGLink | None:
    ref = step.input or "input"
    if ref == "input":
        return DAGLink(
            link_id=_link_id(),
            source_node_id=input_node_id,
            source_slot=0,
            target_node_id=f"step:{step.id}",
            target_slot=0,
            type=AUDIO,
        )
    if "." not in ref:
        raise DAGError(f"step {step.id!r} input {ref!r} is neither 'input' nor a '<step>.<stem>' reference")
    source_id, stem = ref.split(".", 1)
    source_id = source_id.strip()
    stem = stem.strip()
    entry = produced.get(source_id)
    if entry is None:
        raise DAGError(f"step {step.id!r} references unknown upstream step {source_id!r}")
    sep_node_id, stem_slots = entry
    slot = stem_slots.get(stem)
    if slot is None:
        raise DAGError(f"step {step.id!r} references stem {stem!r} which step {source_id!r} does not produce")
    return DAGLink(
        link_id=_link_id(),
        source_node_id=sep_node_id,
        source_slot=slot * 2,
        target_node_id=f"step:{step.id}",
        target_slot=0,
        type=AUDIO,
    )


# ---------------------------------------------------------------------------
# Step classification + stem/save helpers
# ---------------------------------------------------------------------------


def _step_kind(step: WorkflowStep) -> str:
    """Decide which separation node type a step compiles to.

    Mirrors comfy-mss' classification: explicit ``model_type`` for catalog
    models, ``vr`` for legacy VR models (heuristic via model_type string),
    ``custom`` when the step carries explicit model_path/model_type.
    """

    if step.model_path:
        return "custom"
    model_type = (step.model_type or "").lower()
    if model_type == "vr":
        return "vr"
    return "mss"


def _step_stems(step: WorkflowStep) -> list[str]:
    if step.stems:
        return list(step.stems)
    # Without an explicit stem list we cannot name output slots at compile time.
    # The executor will still run, but save-node wiring needs names; require the
    # user to declare stems in the YAML when using the DAG path. ``validate``
    # does not enforce this for the legacy runner, so surface a clear error here.
    if step.save:
        raise DAGError(
            f"step {step.id!r} lists stems under 'save' but declares no 'stems'; "
            "the DAG runner needs explicit stem names to wire outputs"
        )
    return ["output"]


def _step_save_map(step: WorkflowStep) -> dict[str, Any]:
    if not step.save:
        return {}
    # ``save`` is {stem: subdir}; values may be string or dict (legacy tolerated).
    normalized: dict[str, Any] = {}
    for stem, value in step.save.items():
        if isinstance(value, dict):
            normalized[stem] = value.get("dir") or value.get("output_dir") or stem
        else:
            normalized[stem] = value
    return normalized


# ---------------------------------------------------------------------------
# Signature lookup (so compiled nodes match the registered executors)
# ---------------------------------------------------------------------------


def _signature_of(node: DAGNode) -> NodeSignature:
    info = get_node_type(node.type)
    return info.signature(node)


_link_counter = [0]


def _link_id() -> int:
    _link_counter[0] += 1
    return _link_counter[0]


__all__ = ["compile_workflow_to_dag", "CUSTOM_MODEL_TYPES"]
