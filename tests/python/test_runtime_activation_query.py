from __future__ import annotations

import copy
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap  # noqa: F401

import worker_bootstrap as bootstrap


class RuntimeActivationQueryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.python = self.root / "rocm/Scripts/python.exe"
        self.python.parent.mkdir(parents=True)
        self.python.touch()
        self.manifest = {"manifestVersion": "2026.11.8", "common": {
            "pymss": "pymss>=2.3.4", "pymss-core": "pymss-core>=0.1.14"},
            "backends": {"rocm": {"platforms": ["win32"], "torch": {"requirement": "torch==2.9.1"}},
                         "cpu": {"platforms": ["win32"], "torch": {"requirement": "torch==2.9.1"}}}}
        self.probe = {"pythonVersion": "3.12.8", "torchVersion": "2.9.1+rocm7.2.1", "torchBackend": "rocm",
                      "acceleratorAvailable": True, "packages": {"pymss": True, "pymss-core": True},
                      "packageVersions": {"pymss": "2.3.4", "pymss-core": "0.1.14"},
                      "pymssVersion": "2.3.4", "pymssCoreVersion": "0.1.14", "pymssGraphAvailable": True}
        self.state = {**self.probe, "backend": "rocm", "pythonPath": str(self.python),
                      "installedTargets": ["gfx1031"], "selectedDeviceIds": ["hip-pci:0000:03:00.0"],
                      "deviceIds": [7], "activationValidation": {"status": "passed"}}
        self.pointer = self.root / "active-runtime.json"
        self.pointer.write_text(json.dumps(self.state))
        self.state_file = self.root / "rocm/pymss-runtime-state.json"
        self.state_file.write_text(json.dumps(self.state))
        for patch in [
            mock.patch.object(bootstrap, "RUNTIME_ENVS_DIR", self.root),
            mock.patch.object(bootstrap, "ACTIVE_RUNTIME_FILE", self.pointer),
            mock.patch.object(bootstrap, "BUNDLED_RUNTIME_ENVS_DIR", None),
            mock.patch.object(bootstrap, "_manifest", return_value=self.manifest),
            mock.patch.object(bootstrap.sys, "platform", "win32"),
            mock.patch.object(bootstrap, "_read_runtime_state", side_effect=lambda: copy.deepcopy(self.state)),
            mock.patch.object(bootstrap, "_probe_python_runtime", side_effect=lambda *_args: copy.deepcopy(self.probe)),
            mock.patch.object(bootstrap, "_installed_envs", side_effect=lambda *_args, **_kwargs: [copy.deepcopy(self.state)]),
            mock.patch.object(bootstrap, "_module_available", return_value=False),
            mock.patch.object(bootstrap, "_detect_gpu_vendors", return_value=["amd"]),
        ]:
            self.enterContext(patch)

    def query(self, result):
        with mock.patch.object(bootstrap, "_verify_rocm_activation", return_value=result) as gate:
            value = bootstrap._runtime_info_payload({}, repair=False)
        return value, gate

    def test_active_packaged_seed_requires_live_basic_gpu_execution(self):
        result = {"status": "passed", "devices": [{"index": 0, "gfx": "gfx1031", "status": "passed"}]}
        value, gate = self.query(result)
        self.assertTrue(value["ready"])
        self.assertTrue(value["acceleratorAvailable"])
        self.assertEqual(value["activationValidation"], result)
        self.assertEqual(value["installedEnvironments"][0]["activationValidation"], result)
        gate.assert_called_once_with(self.python, ["gfx1031"], None,
                                     selected_device_ids=["hip-pci:0000:03:00.0"])

    def test_failed_basic_gpu_check_preserves_dependency_health_and_does_not_persist(self):
        original = {path: path.read_bytes() for path in (self.pointer, self.state_file)}
        result = {"status": "failed", "reason": "selectedDeviceMappingUnavailable", "devices": []}
        with mock.patch.object(bootstrap, "_atomic_write_json") as write:
            value, _gate = self.query(result)
        self.assertFalse(value["ready"])
        self.assertFalse(value["acceleratorAvailable"])
        self.assertTrue(all(value["packages"].values()))
        self.assertEqual(value["torchBackend"], "rocm")
        self.assertIsNone(value["liveProbeError"])
        self.assertEqual(value["installedEnvironments"][0]["health"], "ready")
        self.assertFalse(value["installedEnvironments"][0]["acceleratorAvailable"])
        self.assertEqual(value["installedEnvironments"][0]["activationValidation"], result)
        write.assert_not_called()
        for path, content in original.items():
            self.assertEqual(path.read_bytes(), content)

    def test_queries_do_not_reuse_a_cached_success_after_device_changes(self):
        with mock.patch.object(bootstrap, "_verify_rocm_activation", side_effect=[{"status": "passed"}, {"status": "failed", "reason": "timeout"}]) as gate:
            first = bootstrap._runtime_info_payload({}, repair=False)
            second = bootstrap._runtime_info_payload({}, repair=False)
        self.assertTrue(first["ready"])
        self.assertFalse(second["ready"])
        self.assertEqual(gate.call_count, 2)

    def test_failed_import_does_not_launch_another_gpu_check(self):
        with mock.patch.object(bootstrap, "_probe_python_runtime", side_effect=RuntimeError("import failed")), \
             mock.patch.object(bootstrap, "_verify_rocm_activation") as gate:
            value = bootstrap._runtime_info_payload({}, repair=False)
        self.assertFalse(value["ready"])
        self.assertIsNotNone(value["liveProbeError"])
        self.assertEqual(value["activationValidation"]["status"], "notRun")
        gate.assert_not_called()

    def test_unactivated_rocm_candidate_does_not_replace_an_active_cpu_environment(self):
        cpu = self.root / "cpu/Scripts/python.exe"
        cpu.parent.mkdir(parents=True)
        cpu.touch()
        active = {"backend": "cpu", "pythonPath": str(cpu)}
        probe = {**self.probe, "torchBackend": "cpu", "torchVersion": "2.9.1", "acceleratorAvailable": False}
        with mock.patch.object(bootstrap, "_read_runtime_state", return_value=active), \
             mock.patch.object(bootstrap, "_probe_python_runtime", return_value=probe), \
             mock.patch.object(bootstrap, "_verify_rocm_activation") as gate:
            value = bootstrap._runtime_info_payload({}, repair=False)
        self.assertEqual(value["installState"]["pythonPath"], str(cpu))
        self.assertEqual(value["installedBackend"], "cpu")
        self.assertTrue(value["ready"])
        self.assertEqual(value["activationValidation"]["status"], "notRun")
        gate.assert_not_called()

    def test_unactivated_candidate_without_pointer_is_not_reported_as_active(self):
        with mock.patch.object(bootstrap, "_read_runtime_state", return_value=None), \
             mock.patch.object(bootstrap, "_verify_rocm_activation") as gate:
            value = bootstrap._runtime_info_payload({}, repair=False)
        self.assertIsNone(value["installState"])
        self.assertIsNone(value["installedBackend"])
        self.assertFalse(value["ready"])
        self.assertEqual(value["activationValidation"]["status"], "notRun")
        gate.assert_not_called()

    def test_custom_rocm_interpreter_without_managed_pointer_is_checked_and_remains_usable(self):
        with mock.patch.object(bootstrap, "_read_runtime_state", return_value=None), \
             mock.patch.object(bootstrap, "_module_available", return_value=True):
            value, gate = self.query({"status": "passed", "devices": []})
        gate.assert_called_once_with(Path(sys.executable), [], None)
        self.assertTrue(value["ready"])
        self.assertIsNone(value["installState"])
        self.assertIsNone(value["installedBackend"])

    def test_missing_selected_interpreter_cannot_validate_a_different_bootstrap(self):
        self.python.unlink()
        with mock.patch.object(bootstrap, "_module_available", return_value=True), \
             mock.patch.object(bootstrap, "_verify_rocm_activation") as gate:
            value = bootstrap._runtime_info_payload({}, repair=False)
        self.assertFalse(value["ready"])
        gate.assert_not_called()

    def test_manual_activation_checks_the_saved_card_before_replacing_another_backend(self):
        cpu = self.root / "cpu/Scripts/python.exe"
        cpu.parent.mkdir(parents=True)
        cpu.touch()
        active = {"backend": "cpu", "pythonPath": str(cpu)}
        self.pointer.write_text(json.dumps(active))
        before = self.pointer.read_bytes()
        target = (copy.deepcopy(self.state), self.python.parent.parent, self.state_file, self.python)
        with mock.patch.object(bootstrap, "_target_runtime_from_payload", return_value=target), \
             mock.patch.object(bootstrap, "_read_runtime_state", return_value=active), \
             mock.patch.object(bootstrap, "_registered_runtime_overlay", return_value=None), \
             mock.patch.object(bootstrap, "_verify_rocm_activation", return_value={"status": "failed"}) as gate, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.cmd_activate_runtime({"backend": "rocm"}), 1)
        gate.assert_called_once_with(self.python, ["gfx1031"], None, selected_device_ids=self.state["selectedDeviceIds"])
        self.assertEqual(self.pointer.read_bytes(), before)

    def test_an_explicit_activation_choice_is_persisted_and_used_by_the_next_live_check(self):
        selected = ["hip-pci:0000:04:00.0"]
        target = (copy.deepcopy(self.state), self.python.parent.parent, self.state_file, self.python)
        with mock.patch.object(bootstrap, "_target_runtime_from_payload", return_value=target), \
             mock.patch.object(bootstrap, "_read_runtime_state", side_effect=lambda: json.loads(self.pointer.read_text())), \
             mock.patch.object(bootstrap, "_registered_runtime_overlay", return_value=None), \
             mock.patch.object(bootstrap, "_verify_rocm_activation", return_value={"status": "passed"}) as gate, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.cmd_activate_runtime({"backend": "rocm", "selectedDeviceIds": selected}), 0)
            overview = bootstrap._runtime_info_payload({}, repair=False)
        self.assertTrue(overview["ready"])
        self.assertEqual(json.loads(self.pointer.read_text())["selectedDeviceIds"], selected)
        self.assertEqual(json.loads(self.state_file.read_text())["selectedDeviceIds"], selected)
        self.assertEqual([call.kwargs["selected_device_ids"] for call in gate.call_args_list], [selected, selected])

    def test_bundled_activation_reuses_the_matching_pointer_choice_without_rewriting_the_seed(self):
        selected = ["hip-pci:0000:04:00.0"]
        original = self.state_file.read_bytes()
        target = (copy.deepcopy(self.state), self.python.parent.parent, self.state_file, self.python)
        with mock.patch.object(bootstrap, "_target_runtime_from_payload", return_value=target), \
             mock.patch.object(bootstrap, "_read_runtime_state", side_effect=lambda: json.loads(self.pointer.read_text())), \
             mock.patch.object(bootstrap, "_is_bundled_runtime_env", return_value=True), \
             mock.patch.object(bootstrap, "_registered_runtime_overlay", return_value=None), \
             mock.patch.object(bootstrap, "_verify_rocm_activation", return_value={"status": "passed"}) as gate, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.cmd_activate_runtime({"backend": "rocm", "selectedDeviceIds": selected}), 0)
            self.assertEqual(bootstrap.cmd_activate_runtime({"backend": "rocm"}), 0)
        self.assertEqual(self.state_file.read_bytes(), original)
        self.assertEqual([call.kwargs["selected_device_ids"] for call in gate.call_args_list], [selected, selected])


if __name__ == "__main__":
    unittest.main()
