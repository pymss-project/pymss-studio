"""pymss.graph: unified DAG execution for comfy-mss JSON and YAML workflows.

Public entry points:
    load_comfy_file(path) -> DAG       parse a comfy-mss JSON workflow
    compile_workflow_to_dag(wf) -> DAG compile a YAML workflow
    run_dag(dag, output_dir=..., ...)  execute and return saved file paths
    SeparatorCache                     reuse loaded separators across nodes

Node executors consume the capability pool from pymss.plugins rather than
reimplementing DSP/codec logic.
"""

from __future__ import annotations

from .core import (
    AUDIO,
    Artifact,
    AudioArtifact,
    DAG,
    DAGError,
    EXECUTION_EVENT_VERSION,
    DAGLink,
    DAGNode,
    DAGOutputRecord,
    DAGExecutionResult,
    MSS_PARAMS,
    NodeContext,
    NodeResult,
    NodeSignature,
    NodeTypeInfo,
    OUTPUT_NODE_TYPES,
    ParamsArtifact,
    PortSpec,
    PROGRESS_EVENT_VERSION,
    SeparatorCache,
    STRING,
    StringArtifact,
    UnknownNodeError,
    VR_PARAMS,
    audio_to_numpy,
    ctx_node_of,
    get_node_type,
    numpy_to_audio,
    parse_default_int,
    parse_device_ids,
    register_alias,
    register_node,
    run_dag,
    safe_filename_part,
    string_value,
    topological_order,
    widget,
)
from .comfy_loader import load_comfy_file, load_comfy_graph
from .yaml_compiler import compile_workflow_to_dag
from .runner import LegacyWorkflowRunner

__all__ = [
    "PROGRESS_EVENT_VERSION",
    "EXECUTION_EVENT_VERSION",
    # artifacts
    "AudioArtifact",
    "StringArtifact",
    "ParamsArtifact",
    "Artifact",
    "AUDIO",
    "STRING",
    "MSS_PARAMS",
    "VR_PARAMS",
    # graph
    "DAG",
    "DAGNode",
    "DAGLink",
    "DAGOutputRecord",
    "DAGExecutionResult",
    "NodeSignature",
    "PortSpec",
    "NodeResult",
    "NodeContext",
    "NodeTypeInfo",
    "DAGError",
    "UnknownNodeError",
    "OUTPUT_NODE_TYPES",
    # registry
    "register_node",
    "register_alias",
    "get_node_type",
    # execution
    "run_dag",
    "SeparatorCache",
    "topological_order",
    # loaders
    "load_comfy_file",
    "load_comfy_graph",
    "compile_workflow_to_dag",
    "LegacyWorkflowRunner",
    # helpers
    "audio_to_numpy",
    "numpy_to_audio",
    "string_value",
    "safe_filename_part",
    "widget",
    "parse_default_int",
    "parse_device_ids",
    "ctx_node_of",
]
