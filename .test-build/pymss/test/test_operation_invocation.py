from types import SimpleNamespace

import numpy as np
import pytest

from pymss.graph.core import AudioArtifact, DAGNode
from pymss.operations import OperationError, OperationSpec, Runtime
from pymss.operations.graph_bridge import register_operation_node
from pymss.plugins import get_registry


def test_bridge_uses_the_same_input_and_output_checks_as_runtime(tmp_path):
    spec = OperationSpec(
        id="test.contract.invoke", schema_version=1, title="Invoke",
        inputs={"audio": {"type": "audio"}}, outputs={"audio": {"type": "audio"}},
        params={"type": "object", "properties": {}, "additionalProperties": False},
    )
    runtime = Runtime()
    runtime.register(spec=spec, run=lambda ctx, inputs, params: {})
    node_type = register_operation_node(spec=spec, run=lambda ctx, inputs, params: {}, source="plugin:test.contract")
    registration = get_registry().nodes[node_type]
    try:
        assert registration.source == "plugin:test.contract"
        node = DAGNode("invoke", node_type)
        context = SimpleNamespace(output_dir=tmp_path, logger=None, current_node_id="invoke", nodes_by_id={"invoke": node})
        audio = AudioArtifact(np.zeros((2, 100)), 16000)
        for inputs, error in [({}, "missing required input"), ({"audio": audio}, "declared outputs")]:
            with pytest.raises(OperationError, match=error):
                runtime.run(spec.id, inputs=inputs, params={})
            with pytest.raises(OperationError, match=error):
                registration.func(context, inputs)
    finally:
        get_registry().nodes.pop(node_type, None)
