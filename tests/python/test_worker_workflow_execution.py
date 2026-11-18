from __future__ import annotations

import sys
from contextlib import nullcontext
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

if __package__:
    from . import _bootstrap as _worker_test_bootstrap  # noqa: F401
else:
    import _bootstrap as _worker_test_bootstrap  # noqa: F401

import worker_infer
import worker_workflows


class WorkflowInvocationTests(unittest.TestCase):
    def run_graph(self, run_dag):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = root / "workflow.json"
            definition.write_text('{"nodes": []}', encoding="utf-8")
            graph = types.SimpleNamespace(
                EXECUTION_EVENT_VERSION=1,
                load_comfy_file=lambda _: types.SimpleNamespace(nodes=[]),
                run_dag=run_dag,
            )
            with patch.object(worker_workflows, "_workflow_graph", return_value=graph), \
                 patch.object(worker_workflows, "_write_workflow_file", return_value=(definition, "comfy")), \
                 patch("worker_plugins.operation_runtime"), \
                 patch("worker_workflow_logs.workflow_logging", return_value=nullcontext(types.SimpleNamespace(logger=None))):
                return worker_workflows._run_pymss(
                    {"workflow": {"nodes": []}}, "invocation", None, None,
                    str(root / "out"), "flat", separator_cache=object(),
                )

    def test_legacy_signature_runs_once_without_node_results(self):
        calls = []

        def legacy(dag, *, output_dir, input_path, inputs, execution_event_callback,
                   logger, device, model_dir, download, source, output_format,
                   audio_params, debug, strict, separator_cache):
            calls.append(dag)
            return []

        self.run_graph(legacy)
        self.assertEqual(len(calls), 1)

    def test_native_signature_receives_intermediate_result_collector(self):
        collectors = []

        def native(dag, *, node_results, **kwargs):
            collectors.append(node_results)
            return []

        self.run_graph(native)
        self.assertEqual(collectors, [{}])

    def test_execution_type_error_is_not_retried(self):
        effects = []
        failure = TypeError("node failed after writing output")

        def failing(dag, *, node_results=None, **kwargs):
            effects.append("output written")
            raise failure

        with self.assertRaises(TypeError) as caught:
            self.run_graph(failing)
        self.assertIs(caught.exception, failure)
        self.assertEqual(effects, ["output written"])


class WorkflowProgressTests(unittest.TestCase):
    def test_native_execution_preserves_failure_identity_and_state(self):
        with patch.object(worker_workflows, "emit") as emit:
            worker_workflows._emit_execution("advanced")({
                "event": "node_failed", "version": 1, "node_id": "save", "node_type": "SaveAudio",
                "node_fraction": 0.25, "overall_fraction": 0.6, "error_type": "DAGError", "message": "Missing input",
            })
        self.assertEqual(emit.call_args.args, ("workflow_execution", {
            "event": "node_failed", "version": 1, "nodeId": "save", "nodeType": "SaveAudio",
            "nodeProgress": 0.25, "overallFraction": 0.6, "errorType": "DAGError", "message": "Missing input",
        }))
        self.assertEqual(emit.call_args.kwargs, {"task_id": "advanced"})

    def test_graph_completion_does_not_invent_an_active_node(self):
        with patch.object(worker_workflows, "emit") as emit:
            worker_workflows._emit_execution("advanced")({
                "event": "execution_completed", "version": 1, "node_id": None,
                "node_fraction": None, "overall_fraction": 1,
            })
        self.assertIsNone(emit.call_args.args[1]["nodeId"])
        self.assertIsNone(emit.call_args.args[1]["nodeProgress"])

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


class WorkflowDeviceTests(unittest.TestCase):
    def test_simple_auto_workflow_inherits_the_selected_gpu(self):
        widgets = ["model", "auto", True, "modelscope", "0", False]
        dag = types.SimpleNamespace(nodes=[types.SimpleNamespace(
            type="mss_separate", data={"widgets_values": widgets},
        )])
        worker_workflows._apply_runtime_device(dag, {"device": "auto", "deviceIds": [2]}, simple=True)
        self.assertEqual(widgets[1], "auto")
        self.assertEqual(widgets[4], "2")

    def test_advanced_auto_workflow_preserves_explicit_gpu_ids(self):
        for ids in ("0", "1", "1,2", 0, [1, 2]):
            with self.subTest(ids=ids):
                widgets = ["model", "auto", True, "modelscope", ids, False]
                before = list(widgets)
                dag = types.SimpleNamespace(nodes=[types.SimpleNamespace(
                    type="mss_separate", data={"widgets_values": widgets},
                )])
                with patch.object(worker_infer, "_resolve_separator_device", side_effect=AssertionError("Explicit IDs must not resolve the global GPU")):
                    worker_workflows._apply_runtime_device(dag, {"device": "auto", "deviceIds": [-1]}, simple=False)
                self.assertEqual(widgets, before)

    def test_advanced_auto_workflow_inherits_missing_ids_for_all_separator_layouts(self):
        for kind in ("mss_separate", "vr_separate", "custom_mss_separate"):
            with self.subTest(kind=kind):
                custom = kind == "custom_mss_separate"
                widgets = ["model", "bs_roformer", "auto", ""] if custom else ["model", "auto", False, "modelscope", ""]
                dag = types.SimpleNamespace(nodes=[types.SimpleNamespace(
                    type="pymss_" + kind + "_list", data={"widgets_values": widgets},
                )])
                worker_workflows._apply_runtime_device(dag, {"device": "auto", "deviceIds": [2]}, simple=False)
                self.assertEqual(widgets[3 if custom else 4], "2")


if __name__ == "__main__":
    unittest.main()
