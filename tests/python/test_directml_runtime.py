import contextlib
import io
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from . import _bootstrap as _worker_test_bootstrap  # noqa: F401

import worker_bootstrap
import worker_infer
import worker_models
import worker_workflows


class DirectMLRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.manifest = worker_bootstrap._manifest()

    def run_probe(self, sdk, *, cuda=None):
        torch = types.SimpleNamespace(
            __version__="2.4.1+cpu", version=types.SimpleNamespace(hip=None, cuda=cuda),
            cuda=types.SimpleNamespace(is_available=lambda: bool(cuda)),
        )
        modules = {"torch": torch, "torch_directml": sdk}

        def run(command, **kwargs):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), mock.patch.dict(sys.modules, modules), \
                    mock.patch("importlib.util.find_spec", return_value=object()), \
                    mock.patch("importlib.metadata.version", return_value="1.0.0"):
                exec(compile(command[2], "runtime-probe", "exec"), {})
            return output.getvalue()

        with mock.patch.object(worker_bootstrap.subprocess, "check_output", side_effect=run):
            return worker_bootstrap._probe_python_runtime(
                Path(sys.executable), worker_bootstrap._backend_extra_names(self.manifest, "dml"),
            )

    def test_cpu_torch_with_directml_is_a_distinct_backend(self):
        probe = self.run_probe(types.SimpleNamespace(is_available=lambda: True))
        self.assertEqual(probe["torchBackend"], "dml")
        self.assertTrue(probe["acceleratorAvailable"])
        self.assertTrue(probe["packages"]["torch-directml"])
        self.assertTrue(worker_bootstrap._runtime_probe_is_ready("dml", probe, self.manifest))
        self.assertFalse(worker_bootstrap._runtime_probe_is_ready("cpu", probe, self.manifest))

    def test_no_adapter_is_reported_without_relabeling_the_environment_cpu(self):
        probe = self.run_probe(types.SimpleNamespace(is_available=lambda: False))
        self.assertEqual(probe["torchBackend"], "dml")
        self.assertFalse(probe["acceleratorAvailable"])

    def test_directml_initialization_errors_do_not_claim_cpu_readiness(self):
        def fail():
            raise OSError("DirectML driver initialization failed")
        probe = self.run_probe(types.SimpleNamespace(is_available=fail))
        self.assertTrue(probe["torchBackend"].startswith("error:"))
        self.assertFalse(worker_bootstrap._runtime_probe_is_ready("dml", probe, self.manifest))

    def test_cuda_environment_is_not_reclassified_by_an_optional_package(self):
        probe = self.run_probe(types.SimpleNamespace(is_available=lambda: True), cuda="12.8")
        self.assertEqual(probe["torchBackend"], "cuda")

    def test_worker_preserves_adapter_ids_for_directml_validation(self):
        devices = types.ModuleType("pymss.devices")
        devices.directml_device = mock.Mock(return_value="privateuseone:2")
        with mock.patch.dict(sys.modules, {"pymss.devices": devices}), \
                mock.patch.object(worker_infer, "_normalize_device_ids", side_effect=AssertionError("DML IDs must not be coerced")):
            self.assertEqual(worker_infer._resolve_separator_device("dml", [2]), ("dml", [2], "dml:2"))
            devices.directml_device.assert_called_once_with([2])
            devices.directml_device.side_effect = ValueError("invalid DirectML adapter")
            with self.assertRaisesRegex(ValueError, "DirectML"):
                worker_infer._resolve_separator_device("dml", [-1])
            devices.directml_device.assert_called_with([-1])

    def test_env_info_reports_adapters_separately_from_cuda(self):
        torch = types.SimpleNamespace(
            __version__="2.4.1+cpu", version=types.SimpleNamespace(hip=None, cuda=None),
            cuda=types.SimpleNamespace(is_available=lambda: False, device_count=lambda: 0),
            backends=types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: False)),
        )
        devices = types.ModuleType("pymss.devices")
        devices.directml_available = lambda: True
        devices.directml_devices = lambda: [{"index": 2, "name": "NVIDIA GPU"}]
        with mock.patch.dict(sys.modules, {"torch": torch, "pymss": types.ModuleType("pymss"), "pymss.devices": devices}), \
                mock.patch.object(worker_models, "import_available", side_effect=lambda name: name == "torch_directml"), \
                mock.patch.object(worker_models, "package_version", return_value=None), \
                mock.patch.object(worker_models, "emit") as emit:
            self.assertEqual(worker_models.cmd_env_info(), 0)
        payload = emit.call_args.args[1]
        self.assertEqual(payload["torchBackend"], "dml")
        self.assertTrue(payload["dmlAvailable"])
        self.assertEqual(payload["dmlDevices"], [{"id": 2, "name": "NVIDIA GPU"}])
        self.assertFalse(payload["cudaAvailable"])
        self.assertEqual(payload["cudaDevices"], [])

    def test_workflows_inherit_the_selected_directml_adapter_and_preserve_node_overrides(self):
        def node(kind, device, ids="0"):
            widgets = ["model", "bs_roformer", device, ids, False] if kind == "custom_mss_separate" else ["model", device, True, "modelscope", ids, False]
            return types.SimpleNamespace(type=kind, data={"widgets_values": widgets})
        automatic = node("mss_separate", "auto", "")
        abbreviated = types.SimpleNamespace(type="mss_separate", data={"widgets_values": ["model"]})
        explicit = node("vr_separate", "dml", "1")
        cpu = node("custom_mss_separate", "cpu")
        dag = types.SimpleNamespace(nodes=[automatic, explicit, cpu, abbreviated])
        with mock.patch.object(worker_infer, "_resolve_separator_device", return_value=("dml", [2], "dml:2")) as resolve:
            worker_workflows._apply_runtime_device(dag, {"device": "dml", "deviceIds": [2]}, simple=False)
            resolve.assert_called_once_with("dml", [2])
            self.assertEqual(automatic.data["widgets_values"][1:2], ["dml"])
            self.assertEqual(automatic.data["widgets_values"][4], "2")
            self.assertEqual(abbreviated.data["widgets_values"], ["model", "dml", True, "modelscope", "2"])
            self.assertEqual(explicit.data["widgets_values"][4], "1")
            self.assertEqual(cpu.data["widgets_values"][2:4], ["cpu", "0"])
            worker_workflows._apply_runtime_device(dag, {"device": "dml", "deviceIds": [2]}, simple=True)
            self.assertEqual(explicit.data["widgets_values"][4], "2")

    def test_audio_only_workflow_does_not_require_a_directml_adapter(self):
        dag = types.SimpleNamespace(nodes=[types.SimpleNamespace(type="pymss_audio_invert")])
        with mock.patch.object(worker_infer, "_resolve_separator_device", side_effect=AssertionError("DSP needs no GPU")):
            worker_workflows._apply_runtime_device(dag, {"device": "dml"}, simple=True)

    def test_auto_workflow_preserves_the_selected_adapter_id(self):
        widgets = ["model", "auto", True, "modelscope", "0", False]
        dag = types.SimpleNamespace(nodes=[types.SimpleNamespace(type="mss_separate", data={"widgets_values": widgets})])
        worker_workflows._apply_runtime_device(dag, {"device": "auto", "deviceIds": [2]}, simple=True)
        self.assertEqual(widgets[1], "auto")
        self.assertEqual(widgets[4], "2")

    def test_list_nodes_and_aliases_inherit_adapters_with_their_own_widget_layout(self):
        for prefix in ("", "pymss_"):
            for kind in ("mss_separate", "vr_separate", "custom_mss_separate"):
                for suffix in ("", "_list"):
                    with self.subTest(node_type=prefix + kind + suffix):
                        custom = kind.startswith("custom_")
                        widgets = ["model", "bs_roformer", "auto", "0", False] if custom else ["model", "auto", False, "modelscope", "0", False]
                        dag = types.SimpleNamespace(nodes=[types.SimpleNamespace(type=prefix + kind + suffix, data={"widgets_values": widgets})])
                        with mock.patch.object(worker_infer, "_resolve_separator_device", return_value=("dml", [1], "dml:1")):
                            worker_workflows._apply_runtime_device(dag, {"device": "dml", "deviceIds": [1]}, simple=True)
                        self.assertEqual(widgets[2 if custom else 1], "dml")
                        self.assertEqual(widgets[3 if custom else 4], "1")
                        self.assertEqual(widgets[1 if custom else 0], "bs_roformer" if custom else "model")

    def test_graph_nodes_preserve_explicit_adapter_ids_without_validating_global_ids(self):
        for prefix in ("", "pymss_"):
            for kind in ("mss_separate", "vr_separate", "custom_mss_separate"):
                for suffix in ("", "_list"):
                    for node_ids in ("0", "1", "1,2", 0, [1, 2]):
                        for runtime in ("auto", "cpu", "cuda", "dml"):
                            with self.subTest(node_type=prefix + kind + suffix, node_ids=node_ids, runtime=runtime):
                                custom = kind.startswith("custom_")
                                widgets = ["model", "bs_roformer", "auto", node_ids, False] if custom else ["model", "auto", False, "modelscope", node_ids, False]
                                before = list(widgets)
                                dag = types.SimpleNamespace(nodes=[types.SimpleNamespace(type=prefix + kind + suffix, data={"widgets_values": widgets})])
                                with mock.patch.object(worker_infer, "_resolve_separator_device", side_effect=AssertionError("Explicit graph IDs must not depend on global IDs")) as resolve:
                                    worker_workflows._apply_runtime_device(dag, {"device": runtime, "deviceIds": [-1]}, simple=False)
                                resolve.assert_not_called()
                                self.assertEqual(widgets, before)

    def test_graph_nodes_inherit_only_missing_or_empty_adapter_ids(self):
        for kind in ("mss_separate", "vr_separate", "custom_mss_separate"):
            for node_ids in (None, "", "  ", []):
                with self.subTest(kind=kind, node_ids=node_ids):
                    custom = kind.startswith("custom_")
                    widgets = ["model", "bs_roformer", "auto", node_ids, False] if custom else ["model", "auto", False, "modelscope", node_ids, False]
                    dag = types.SimpleNamespace(nodes=[types.SimpleNamespace(type=kind, data={"widgets_values": widgets})])
                    with mock.patch.object(worker_infer, "_resolve_separator_device", return_value=("dml", [2], "dml:2")) as resolve:
                        worker_workflows._apply_runtime_device(dag, {"device": "dml", "deviceIds": [2]}, simple=False)
                    resolve.assert_called_once_with("dml", [2])
                    self.assertEqual(widgets[2 if custom else 1], "dml")
                    self.assertEqual(widgets[3 if custom else 4], "2")

    def test_graph_explicit_provider_overrides_do_not_validate_unrelated_runtime_ids(self):
        for kind in ("mss_separate", "vr_separate", "custom_mss_separate"):
            for device in ("cpu", "cuda", "mps", "dml"):
                with self.subTest(kind=kind, device=device):
                    custom = kind.startswith("custom_")
                    widgets = ["model", "bs_roformer", device, "0", False] if custom else ["model", device, False, "modelscope", "0", False]
                    before = list(widgets)
                    dag = types.SimpleNamespace(nodes=[types.SimpleNamespace(type=kind, data={"widgets_values": widgets})])
                    with mock.patch.object(worker_infer, "_resolve_separator_device", side_effect=AssertionError("Explicit graph provider must not depend on global IDs")) as resolve:
                        worker_workflows._apply_runtime_device(dag, {"device": "dml", "deviceIds": [-1]}, simple=False)
                    resolve.assert_not_called()
                    self.assertEqual(widgets, before)

    def test_manifest_keeps_dml_pins_separate_from_cpu_and_cuda(self):
        spec = self.manifest["backends"]["dml"]
        self.assertEqual(spec["platforms"], ["win32"])
        self.assertEqual(spec["python"], "3.12")
        self.assertEqual(spec["torch"]["requirements"], ["torch==2.4.1", "torchvision==0.19.1"])
        self.assertEqual(spec["torch"]["indexUrl"], "https://download.pytorch.org/whl/cpu")
        for backend in ("cpu", "cuda"):
            self.assertEqual(self.manifest["backends"][backend]["torch"]["requirement"], "torch==2.7.1")
        probe = {"torchVersion": "2.4.1+cpu", "packageVersions": {
            "torch-directml": "0.2.5.dev240914", "torchvision": "0.19.1+cpu", "numpy": "2.0.0",
        }}
        narrow_manifest = {**self.manifest, "common": {"numpy": "numpy>=1.26,<3"}}
        valid, failures = worker_bootstrap._manifest_versions_are_satisfied(probe, narrow_manifest, "dml")
        self.assertFalse(valid)
        self.assertTrue(any("numpy" in error for error in failures))
        probe["packageVersions"]["numpy"] = "1.26.4"
        self.assertEqual(worker_bootstrap._manifest_versions_are_satisfied(probe, narrow_manifest, "dml"), (True, []))


if __name__ == "__main__":
    unittest.main()
