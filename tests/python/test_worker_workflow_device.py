from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock


PYTHON_DIR = Path(__file__).resolve().parents[2] / "python"
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

import worker_infer
import worker_workflows


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
                with mock.patch.object(worker_infer, "_resolve_separator_device", side_effect=AssertionError("Explicit IDs must not resolve the global GPU")):
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
