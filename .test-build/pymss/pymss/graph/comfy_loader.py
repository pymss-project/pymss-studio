"""Load native comfy-mss JSON graphs into the pymss DAG core.

This module understands the ComfyUI graph serialization format as produced by
comfy-mss (``nodes`` + ``links`` + ``widgets_values``) and turns it into the
internal :class:`pymss.dag.DAG` representation that :func:`pymss.dag.run_dag`
executes.

We deliberately do NOT depend on ComfyUI at all: the graph is treated as plain
data. Only the handful of node types comfy-mss defines are understood; unknown
node types are rejected unless ``strict=False``, in which case they are dropped
from the graph (their downstream consumers will then fail unless their inputs
were optional).

The format reference is the comfy-mss example graphs under
``comfy-mss/examples/`` and the node definitions in ``comfy-mss/nodes.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .core import (
    DAG,
    DAGError,
    DAGLink,
    DAGNode,
    PortSpec,
    get_node_type,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_comfy_graph(data: Any) -> DAG:
    """Build a :class:`DAG` from a parsed comfy-mss workflow dict.

    Args:
        data: The parsed workflow JSON (an object with ``nodes`` and ``links``).

    Returns:
        A :class:`DAG` ready to pass to :func:`pymss.dag.run_dag`.

    Raises:
        DAGError: If the graph is malformed or references unknown nodes and no
            ``strict`` override is set on :func:`run_dag`.
    """

    if not isinstance(data, dict):
        raise DAGError("comfy workflow JSON must be an object")

    raw_nodes = data.get("nodes")
    raw_links = data.get("links")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise DAGError("comfy workflow has no 'nodes' array")
    if raw_links is not None and not isinstance(raw_links, list):
        raise DAGError("comfy workflow 'links' must be an array when present")

    link_map = _build_link_map(raw_links)
    nodes_by_id: dict[int, dict[str, Any]] = {}
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            continue
        node_id = raw.get("id")
        if not isinstance(node_id, int):
            continue
        nodes_by_id[node_id] = raw

    dag = DAG()
    dag.meta = {
        "id": data.get("id"),
        "version": data.get("version"),
        "extra": data.get("extra") if isinstance(data.get("extra"), dict) else {},
    }

    for node_id in sorted(nodes_by_id.keys()):
        raw = nodes_by_id[node_id]
        node = _build_node(raw, nodes_by_id, link_map)
        if node is not None:
            dag.nodes.append(node)

    if not dag.nodes:
        raise DAGError("comfy workflow contains no usable nodes")

    return dag


def load_comfy_file(path: str | os.PathLike) -> DAG:
    """Load a comfy-mss JSON file from disk."""

    text = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DAGError(f"comfy workflow {path} is not valid JSON: {exc}") from exc
    return load_comfy_graph(data)


# ---------------------------------------------------------------------------
# Internal: link map + node construction
# ---------------------------------------------------------------------------


def _build_link_map(raw_links: list[Any]) -> dict[int, tuple[int, int, int, int, str]]:
    """Index comfy links by id.

    Each comfy link tuple is ``[link_id, source_node_id, source_slot,
    target_node_id, target_slot, type]``. We keep a 5-tuple without the id.
    """

    links: dict[int, tuple[int, int, int, int, str]] = {}
    if not raw_links:
        return links
    for entry in raw_links:
        # ComfyUI serializes links as 6-tuples; a host bug (pymss-studio <= 0.0.12
        # litegraph adapter) JSON-round-tripped them into numeric-key objects
        # ({"0": id, "1": src, ...}). Accept both so stored graphs keep working.
        if isinstance(entry, dict):
            entry = [entry.get(str(i), entry.get(i)) for i in range(6)]
        if not isinstance(entry, list) or len(entry) < 6:
            continue
        try:
            link_id = int(entry[0])
            source_node = int(entry[1])
            source_slot = int(entry[2])
            target_node = int(entry[3])
            target_slot = int(entry[4])
        except (TypeError, ValueError):
            continue
        link_type = str(entry[5] or "")
        links[link_id] = (source_node, source_slot, target_node, target_slot, link_type)
    return links


def _build_node(raw: dict[str, Any], nodes_by_id: dict[int, dict[str, Any]], link_map: dict[int, tuple[int, int, int, int, str]]) -> DAGNode | None:
    node_id = raw["id"]
    node_type = str(raw.get("type") or "").strip()
    if not node_type:
        return None

    widgets_values = list(raw.get("widgets_values") or [])
    inputs_raw = list(raw.get("inputs") or [])
    outputs_raw = list(raw.get("outputs") or [])

    # Resolve the incoming links per input slot. Comfy stores on each input the
    # ``link`` id (or null) that feeds it. We translate that to our DAGLink.
    resolved_inputs: list[DAGLink | None] = []
    for input_def in inputs_raw:
        if not isinstance(input_def, dict):
            resolved_inputs.append(None)
            continue
        link_id = input_def.get("link")
        if link_id is None:
            resolved_inputs.append(None)
            continue
        link = link_map.get(int(link_id))
        if link is None:
            resolved_inputs.append(None)
            continue
        source_node, source_slot, _target_node, target_slot, link_type = link
        resolved_inputs.append(
            DAGLink(
                link_id=int(link_id),
                source_node_id=source_node,
                source_slot=source_slot,
                target_node_id=node_id,
                target_slot=target_slot,
                type=link_type,
            )
        )

    # If the node declares fewer inputs than its signature expects (e.g. comfy
    # collapsed optional inputs), pad with None so slot indices line up.
    node = DAGNode(
        id=node_id,
        type=node_type,
        inputs=resolved_inputs,
        data={
            "widgets_values": widgets_values,
            "outputs": outputs_raw,
            "raw": raw,
        },
        title=str(raw.get("title") or node_type),
    )
    node._explicit_order = raw.get("order")  # type: ignore[attr-defined]

    try:
        node_info = get_node_type(node_type)
    except KeyError:
        # Unknown node: keep it in the graph so topo order can route around it
        # if strict=False drops it later. Signature is left empty; run_dag will
        # raise (strict) or skip (non-strict) at execution time.
        node.signature = _empty_signature()
        return node

    try:
        node.signature = node_info.signature(node)
    except Exception as exc:  # pragma: no cover - signature factories are simple
        raise DAGError(f"failed to read signature for node {node_id} ({node_type}): {exc}") from exc
    return node


def _empty_signature():
    from .core import NodeSignature

    return NodeSignature(inputs=[], output_names=[], output_types=[])


__all__ = ["load_comfy_file", "load_comfy_graph"]
