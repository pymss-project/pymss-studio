from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

if __package__:
    from . import _bootstrap as _worker_test_bootstrap  # noqa: F401
else:
    import _bootstrap as _worker_test_bootstrap  # noqa: F401

import worker_workflows


class WorkflowProgressTests(unittest.TestCase):
    def test_native_overall_fraction_is_forwarded_and_audio_seconds_are_preserved(self):
        with patch.object(worker_workflows, "emit") as emit:
            callback = worker_workflows._emit_progress("workflow")
            callback({"overall_fraction": 0.5, "done": 50, "total": 100, "unit": "seconds", "message": "Processing audio"})
        payload = emit.call_args.args[1]
        self.assertEqual(payload["progress"], 62)
        self.assertEqual((payload["done"], payload["total"]), (50, 100))
        self.assertEqual(emit.call_args.kwargs, {"task_id": "workflow"})

    def test_node_completion_and_vr_batches_are_not_displayed_as_audio_seconds(self):
        with patch.object(worker_workflows, "emit") as emit:
            callback = worker_workflows._emit_progress("workflow")
            callback({"overall_fraction": 0.5, "done": 20, "total": 40, "unit": "batches"})
            callback({"overall_fraction": 0.75, "message": "Audio processing completed"})
        for call in emit.call_args_list:
            self.assertNotIn("done", call.args[1])
            self.assertNotIn("total", call.args[1])
        self.assertEqual([call.args[1]["progress"] for call in emit.call_args_list], [62, 76])

    def test_old_runtime_is_rejected_with_an_actionable_update_message(self):
        pymss, graph = types.ModuleType("pymss"), types.ModuleType("pymss.graph")
        pymss.graph = graph
        with patch.dict(sys.modules, {"pymss": pymss, "pymss.graph": graph}):
            with self.assertRaisesRegex(RuntimeError, "Update the runtime core") as caught:
                worker_workflows._run_pymss({}, "old-runtime", None, None, "outputs", "flat")
            self.assertNotIn("2.1.9", str(caught.exception))
