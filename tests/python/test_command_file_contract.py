"""Generic file results survive the Worker and workbench publication boundary."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

if __package__:
    from . import _bootstrap as _worker_test_bootstrap  # noqa: F401
else:
    import _bootstrap as _worker_test_bootstrap  # noqa: F401

import worker_operations
import worker_workflows

try:
    from pymss.operations.files import FileArtifact
except ImportError:
    FileArtifact = None


@unittest.skipIf(FileArtifact is None, "The released pymss 2.2.0 predates generic command files")
class CommandFileTests(unittest.TestCase):
    def test_generic_file_is_serialized_without_audio_decoding_or_json_wrapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "subtitle.srt"
            path.write_text("1\n00:00:00,000 --> 00:00:01,000\nCaption\n", encoding="utf-8")
            artifact = FileArtifact(str(path), "application/x-subrip")
            self.assertEqual(worker_operations._serialize_output("document", artifact, Path(temporary)), {
                "kind": "file", "path": str(path), "mediaType": "application/x-subrip",
            })

    def test_workbench_publishes_generic_files_before_its_private_directory_is_removed(self):
        from pymss.operations import OperationSpec
        from pymss.operations.graph_bridge import register_operation_node
        from pymss.plugins import get_registry

        node_type = "StudioCommandFileOutput"

        def run(context, _inputs, _params):
            path = Path(context.workdir) / "document.txt"
            path.write_text("converted", encoding="utf-8")
            return {"document": FileArtifact(str(path), "text/plain")}

        spec = OperationSpec(id="studio.command.contract", schema_version=1, title="Convert", inputs={},
                             params={"type": "object", "properties": {}}, outputs={"document": {"type": "file"}})
        register_operation_node(spec=spec, run=run, node_type=node_type, source="plugin:studio.command")
        self.addCleanup(get_registry().nodes.pop, node_type, None)
        self.addCleanup(worker_workflows._cleanup_workflow_file, "file-contract")
        with tempfile.TemporaryDirectory() as temporary, patch("worker_nodes.collect_plugin_nodes", return_value=[]), \
                patch.object(worker_workflows, "emit"):
            result = worker_workflows._run_pymss({
                "workflow": {"nodes": [{"id": 1, "type": node_type, "inputs": []}], "links": []},
                "workbench": True, "workbenchOutputs": {}, "outputFormat": "wav",
            }, "file-contract", None, {}, temporary, "flat")
            self.assertEqual(len(result["files"]), 1)
            path = Path(result["files"][0])
            self.assertEqual(path.parent, Path(temporary).resolve())
            self.assertEqual(path.read_text(encoding="utf-8"), "converted")
            self.assertEqual(result["outputs"][0]["kind"], "file")
            self.assertEqual(result["outputs"][0]["mediaType"], "text/plain")
            self.assertFalse(list(Path(temporary).glob(".pymss-workflow-*")))
