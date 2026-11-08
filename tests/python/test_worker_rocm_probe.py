from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event
import types
import unittest
from unittest import mock

import worker_rocm_probe as probe


def device(index=0):
    return {"index": index, "name": "AMD GPU", "rawArchitecture": "gfx1031:sramecc-:xnack-",
            "gfx": "gfx1031", "stableId": "unknown", "driverVersion": "unknown"}


class ProbeRequestTests(unittest.TestCase):
    def test_strict_invalid_inputs_never_start_a_subprocess(self):
        invalid = [{}, {"taskId": ""}, {"taskId": 3}, {"taskId": "x", "deviceIds": []},
                   {"taskId": "x", "deviceIds": [True]}, {"taskId": "x", "deviceIds": [-1]},
                   {"taskId": "x", "deviceIds": [0.0]}, {"taskId": "x", "modes": []},
                   {"taskId": "x", "modes": ["nativeHip", "nativeHip"]},
                   {"taskId": "x", "modes": ["cpu"]}, {"taskId": "x", "includeFp16": 1},
                   {"taskId": "x", "timeoutSeconds": True}, {"taskId": "x", "timeoutSeconds": 0},
                   {"taskId": "x", "timeoutSeconds": float("inf")}, {"taskId": "x", "timeoutSeconds": 121}]
        with mock.patch.object(probe, "_run_child") as child, mock.patch.object(probe, "emit_error", return_value=1) as error:
            for payload in invalid:
                with self.subTest(payload=payload):
                    self.assertEqual(probe.cmd_runtime_gpu_probe(payload), 1)
                    self.assertEqual(error.call_args.args[0], "ROCM_PROBE_INVALID_REQUEST")
        child.assert_not_called()

    def test_cpu_or_cuda_torch_cannot_claim_amd_gpu_support(self):
        torch = types.SimpleNamespace(version=types.SimpleNamespace(hip=None), cuda=mock.Mock())
        self.assertFalse(probe._inventory(torch)["available"])
        torch.cuda.is_available.assert_not_called()

    def test_inventory_preserves_raw_architecture_without_inventing_driver(self):
        torch = types.SimpleNamespace(__version__="2.9.1", version=types.SimpleNamespace(hip="7.2.1"),
                                      cuda=types.SimpleNamespace(is_available=lambda: True, device_count=lambda: 1,
                                      get_device_properties=lambda _index: types.SimpleNamespace(name="AMD GPU", gcnArchName="gfx1031:sramecc-:xnack-")))
        result = probe._inventory(torch)
        self.assertEqual(result["devices"][0], device())


class ProbeControllerTests(unittest.TestCase):
    def setUp(self):
        self.state = {"profileId": "legacy", "baseFingerprint": "base", "targetFingerprint": "target"}
        self.enterContext(mock.patch.object(probe, "_runtime_context", return_value=(self.state, {})))
        self.events = self.enterContext(mock.patch.object(probe, "emit"))

    def test_controller_has_no_torch_import_and_unavailable_is_not_install_failure(self):
        with mock.patch.dict(sys.modules, {"torch": None}), mock.patch.object(probe, "_run_child", return_value={
                "available": False, "errorCode": "ROCM_RUNTIME_UNAVAILABLE", "message": "ROCm is unavailable"}), \
                mock.patch.object(probe, "_write_report") as save:
            self.assertEqual(probe.cmd_runtime_gpu_probe({"taskId": "diagnostic"}), 0)
        report = save.call_args.args[0]
        self.assertEqual(report["status"], "unavailable")
        self.assertFalse(report["cacheReusable"])
        self.assertEqual(self.events.call_args.args[0], "runtime_gpu_probe_finished")

    def test_optional_failure_does_not_hide_basic_gpu_availability_and_modes_are_isolated(self):
        allocation = probe._check_result("allocation", "fp32", "passed", "OK")
        optional = probe._check_result("instanceNorm", "fp32", "failed", "Unavailable")
        mode_result = {"status": "completed", "checks": [allocation, optional]}
        with mock.patch.object(probe, "_run_child", side_effect=[
                {"available": True, "devices": [device()]}, copy.deepcopy(mode_result), copy.deepcopy(mode_result)]) as child:
            report = probe.run_gpu_probe({"taskId": "diagnostic", "deviceIds": [0]})
        self.assertTrue(report["deviceCapabilities"][0]["basicAvailable"])
        self.assertFalse(report["cacheReusable"], "Unknown driver must prevent automatic cache reuse")
        self.assertEqual([call.args[0].get("mode") for call in child.call_args_list], [None, "miopen", "nativeHip"])
        self.assertFalse(child.call_args_list[1].args[0]["includeFp16"])

    def test_mode_timeout_preserves_partial_checks_and_continues_in_fresh_child(self):
        allocation = probe._check_result("allocation", "fp32", "passed", "OK")
        with mock.patch.object(probe, "_run_child", side_effect=[
                {"available": True, "devices": [device()]},
                {"status": "timeout", "checks": [allocation], "errorCode": "ROCM_PROBE_TIMEOUT"},
                {"status": "completed", "checks": [allocation]}]):
            report = probe.run_gpu_probe({"taskId": "diagnostic"})
        self.assertEqual(report["status"], "partial")
        modes = report["deviceCapabilities"][0]["modes"]
        self.assertEqual(modes[0]["checks"][0]["status"], "passed")
        self.assertEqual(modes[0]["checks"][1]["status"], "timeout")
        self.assertTrue(modes[1]["basicAvailable"])
        self.assertFalse(report["cacheReusable"])

    def test_cleanup_failure_stops_further_children_and_cannot_overwrite_last_report(self):
        with mock.patch.object(probe, "_run_child", side_effect=[
                {"available": True, "devices": [device()]}, probe.ProbeCleanupError("unsafe")]) as child, \
                mock.patch.object(probe, "_write_report") as save, \
                mock.patch.object(probe, "emit_error", return_value=1) as error:
            self.assertEqual(probe.cmd_runtime_gpu_probe({"taskId": "diagnostic"}), 1)
        self.assertEqual(child.call_count, 2)
        save.assert_not_called()
        self.assertEqual(error.call_args.args[0], "ROCM_PROBE_CLEANUP_FAILED")

    def test_start_failure_keeps_stage_type_and_safe_os_cause(self):
        with mock.patch.object(probe.subprocess, "Popen", side_effect=OSError(126, "The specified ROCm module could not be found token=startup-secret")), \
                mock.patch.object(probe, "emit_error", return_value=1) as error:
            self.assertEqual(probe.cmd_runtime_gpu_probe({"taskId": "diagnostic"}), 1)
        self.assertEqual(error.call_args.args[0], "ROCM_PROBE_FAILED")
        self.assertEqual(error.call_args.kwargs["extra"]["failureStage"], "inventory_process_start")
        self.assertEqual(error.call_args.kwargs["extra"]["exceptionType"], "OSError")
        self.assertIn("specified ROCm module could not be found", error.call_args.kwargs["detail"])
        self.assertNotIn("startup-secret", error.call_args.kwargs["detail"])

    def test_cancelled_mode_stops_matrix_and_is_not_saved(self):
        with mock.patch.object(probe, "_run_child", side_effect=[
                {"available": True, "devices": [device(), device(1)]}, {"status": "cancelled", "checks": []}]) as child, \
                mock.patch.object(probe, "_write_report") as save:
            self.assertEqual(probe.cmd_runtime_gpu_probe({"taskId": "diagnostic"}), 0)
        self.assertEqual(child.call_count, 2)
        save.assert_not_called()

    def test_requested_missing_index_is_structured_unavailable_not_cpu_fallback(self):
        with mock.patch.object(probe, "_run_child", return_value={"available": True, "devices": [device()]}) as child:
            report = probe.run_gpu_probe({"taskId": "diagnostic", "deviceIds": [1]})
        self.assertEqual(child.call_count, 1)
        self.assertEqual(report["status"], "partial")
        self.assertFalse(report["deviceCapabilities"][0]["basicAvailable"])

    def test_read_report_detects_stale_runtime_and_never_reuses_old_hardware_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            report = probe._base_report(self.state, "old-task")
            report["baseFingerprint"] = "previous-base"
            report["cacheReusable"] = True
            path.write_text(json.dumps(report), encoding="utf-8")
            with mock.patch.object(probe, "_report_path", return_value=path):
                self.assertEqual(probe.cmd_runtime_gpu_probe_report({}), 0)
        result = self.events.call_args.args[1]["report"]
        self.assertFalse(result["matchesCurrentRuntime"])
        self.assertFalse(result["cacheReusable"])

    def _partial_cached_report(self):
        report = probe._base_report(self.state, "diagnostic")
        report["status"] = "partial"
        report["deviceCapabilities"] = [{**device(), "basicAvailable": True, "modes": [
            {"mode": "miopen", "status": "timeout", "basicAvailable": True, "checks": [
                probe._check_result("allocation", "fp32", "passed", "Output matched the CPU reference"),
                probe._check_result("stftIstft", "fp32", "timeout", "The isolated mode timed out", "ROCM_PROBE_TIMEOUT"),
            ]}]}]
        return report

    def test_malformed_nested_cache_returns_null_without_changing_the_file(self):
        mutations = [
            lambda report: report.update(deviceCapabilities=[None]),
            lambda report: report["deviceCapabilities"][0].pop("modes"),
            lambda report: report["deviceCapabilities"][0].update(modes=None),
            lambda report: report["deviceCapabilities"][0]["modes"][0].update(checks=None),
            lambda report: report["deviceCapabilities"][0]["modes"][0].update(checks=[None]),
            lambda report: report["deviceCapabilities"][0].update(index=True),
            lambda report: report["deviceCapabilities"][0].update(basicAvailable="true"),
            lambda report: report["deviceCapabilities"][0]["modes"][0].update(basicAvailable=None),
            lambda report: report["deviceCapabilities"][0]["modes"][0].update(mode="other"),
            lambda report: report["deviceCapabilities"][0]["modes"][0].update(status=[]),
            lambda report: report["deviceCapabilities"][0]["modes"][0]["checks"][0].update(id="unknown"),
            lambda report: report["deviceCapabilities"][0]["modes"][0]["checks"][0].update(precision="fp64"),
            lambda report: report["deviceCapabilities"][0]["modes"][0]["checks"][0].update(status="other"),
            lambda report: report["deviceCapabilities"][0]["modes"][0]["checks"][0].pop("status"),
            lambda report: report["deviceCapabilities"][0]["modes"][0]["checks"][0].update(requiredForBasic=None),
            lambda report: report["deviceCapabilities"][0]["modes"][0]["checks"][0].update(message=5),
            lambda report: report["deviceCapabilities"][0]["modes"][0]["checks"][0].update(detail="x" * 501),
            lambda report: report.update(deviceCapabilities=report["deviceCapabilities"] * 9),
            lambda report: report["deviceCapabilities"][0].update(modes=report["deviceCapabilities"][0]["modes"] * 3),
            lambda report: report["deviceCapabilities"][0]["modes"][0].update(checks=report["deviceCapabilities"][0]["modes"][0]["checks"] * 8),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            for index, mutate in enumerate(mutations):
                with self.subTest(mutation=index):
                    report = self._partial_cached_report()
                    mutate(report)
                    original = json.dumps(report)
                    path.write_text(original, encoding="utf-8")
                    with mock.patch.object(probe, "_report_path", return_value=path):
                        self.assertEqual(probe.cmd_runtime_gpu_probe_report({}), 0)
                    self.assertIsNone(self.events.call_args.args[1]["report"])
                    self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_oversized_cache_is_bounded_and_returned_as_null(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            report = self._partial_cached_report()
            report["extra"] = "x" * probe.MAX_CACHED_REPORT_BYTES
            path.write_text(json.dumps(report), encoding="utf-8")
            original_size = path.stat().st_size
            with mock.patch.object(probe, "_report_path", return_value=path):
                self.assertEqual(probe.cmd_runtime_gpu_probe_report({}), 0)
            self.assertIsNone(self.events.call_args.args[1]["report"])
            self.assertEqual(path.stat().st_size, original_size)

    def test_valid_partial_cache_keeps_observations_without_promoting_timeouts_to_passed(self):
        report = self._partial_cached_report()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            with mock.patch.object(probe, "_report_path", return_value=path):
                self.assertEqual(probe.cmd_runtime_gpu_probe_report({}), 0)
        observed = self.events.call_args.args[1]["report"]
        self.assertEqual(observed["status"], "partial")
        self.assertTrue(observed["fromCache"])
        self.assertTrue(observed["matchesCurrentRuntime"])
        self.assertFalse(observed["cacheReusable"])
        self.assertEqual(observed["deviceCapabilities"][0]["modes"][0]["checks"][1]["status"], "timeout")

    def test_old_version_or_null_profile_metadata_is_shown_as_stale(self):
        report = self._partial_cached_report()
        report.update(probeVersion=probe.PROBE_VERSION + 1, profileId=None, baseFingerprint=None, targetFingerprint=None)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            with mock.patch.object(probe, "_report_path", return_value=path):
                self.assertEqual(probe.cmd_runtime_gpu_probe_report({}), 0)
        observed = self.events.call_args.args[1]["report"]
        self.assertIsNotNone(observed)
        self.assertTrue(observed["fromCache"])
        self.assertFalse(observed["matchesCurrentRuntime"])
        self.assertFalse(observed["cacheReusable"])

    def test_atomic_persistence_uses_managed_data_root(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {"PYMSS_STUDIO_DATA_ROOT": temporary}):
            report = probe._base_report(self.state, "diagnostic")
            probe._write_report(report)
            path = Path(temporary) / "runtime-diagnostics" / "rocm-gpu-probe.json"
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), report)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_cache_identity_includes_target_driver_device_and_precision_coverage(self):
        report = probe._base_report(self.state, "diagnostic")
        report["deviceCapabilities"] = [{**device(), "stableId": "device-unique-id", "driverVersion": "driver-1",
                                          "modes": [{"mode": "miopen", "checks": [probe._check_result("allocation", "fp32", "passed", "OK")]}]}]
        identity = probe._cache_identity(report)
        self.assertTrue(identity)
        for field, value in [("targetFingerprint", "other-target"), ("probeVersion", 99)]:
            other = copy.deepcopy(report)
            other[field] = value
            self.assertNotEqual(probe._cache_identity(other), identity)
        for field in ("stableId", "driverVersion"):
            other = copy.deepcopy(report)
            other["deviceCapabilities"][0][field] += "-different"
            self.assertNotEqual(probe._cache_identity(other), identity)
        report["deviceCapabilities"][0]["modes"][0]["checks"].append(probe._check_result("allocation", "fp16", "passed", "OK"))
        self.assertNotEqual(probe._cache_identity(report), identity)


class ProbeModeTests(unittest.TestCase):
    def setUp(self):
        self.torch = types.SimpleNamespace(version=types.SimpleNamespace(hip="7.2.1"),
                                           cuda=types.SimpleNamespace(is_available=lambda: True, set_device=mock.Mock()),
                                           backends=types.SimpleNamespace(cudnn=types.SimpleNamespace(enabled=True)),
                                           device=lambda _kind, index: f"cuda:{index}")
        self.events = []

    def sink(self, kind, value):
        self.events.append((kind, value))

    def test_fatal_context_error_stops_remaining_operators_in_that_process(self):
        with mock.patch.object(probe, "run_operator_check", side_effect=[None, RuntimeError("hipErrorPriorLaunchFailure token=private C:/Users/private/audio.wav")]) as operator, \
                mock.patch.object(Path, "home", return_value=Path("C:/Users/private")):
            probe._run_mode(self.torch, 0, "nativeHip", True, self.sink)
        self.assertEqual(operator.call_count, 2)
        self.assertFalse(self.torch.backends.cudnn.enabled)
        report = self.events[-1][1]
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["checks"][1]["errorCode"], "ROCM_CONTEXT_FAILED")
        self.assertEqual(report["checks"][-1]["status"], "skipped")
        self.assertNotIn("private", json.dumps(self.events))

    def test_failure_keeps_specific_root_cause_with_redacted_credentials_and_home(self):
        error = RuntimeError(
            "HIPRTC_ERROR_COMPILATION: 'type_traits' file not found token=private-token "
            "https://account:password@example.test/build?access_token=private-url-token\n"
            "c:/uSeRs/Private/rocm/include/file.hpp:19:8: fatal error\n"
            "3 | #include <type_traits>"
        )
        with mock.patch.object(Path, "home", return_value=Path("C:/Users/private")):
            context = probe._safe_exception_context(error)
        self.assertEqual(context["exceptionType"], "RuntimeError")
        self.assertIn("'type_traits' file not found", context["detail"])
        self.assertIn("<home>/rocm/include/file.hpp", context["detail"])
        self.assertNotIn("private-token", context["detail"])
        self.assertNotIn("account", context["detail"])
        self.assertNotIn("password", context["detail"])
        self.assertNotIn("private-url-token", context["detail"])
        self.assertNotIn("#include", context["detail"])
        self.assertNotIn("uSeRs", context["detail"])

    def test_windows_home_matches_backslashes_and_failure_detail_is_bounded(self):
        with mock.patch.object(Path, "home", return_value=Path("C:/Users/Private")):
            context = probe._safe_exception_context(RuntimeError(
                "DLL dependency missing in C:\\users\\PRIVATE\\rocm\\bin; " + "x" * 800))
        self.assertIn("DLL dependency missing", context["detail"])
        self.assertIn("<home>\\rocm\\bin", context["detail"])
        self.assertLessEqual(len(context["detail"]), 500)

    def test_source_and_invocation_dumps_are_excluded_without_hiding_root_cause(self):
        for unsafe in ["  3 | #include <secret-source>", "payload={'audio': 'secret-audio.wav'}",
                       "argv=['python.exe', 'secret-arg']", "env={'SECRET': 'secret-value'}"]:
            with self.subTest(unsafe=unsafe):
                context = probe._safe_exception_context(RuntimeError("ROCm DLL loader: missing dependency\n" + unsafe))
                self.assertEqual(context["detail"], "ROCm DLL loader: missing dependency")
        context = probe._safe_exception_context(subprocess.CalledProcessError(126, ["private-command"], stderr="private-source"))
        self.assertEqual(context["detail"], "The diagnostic subprocess exited with status 126")

    def test_operator_failure_exposes_safe_detail_beside_friendly_summary(self):
        def execute(_torch, _device, check, _precision):
            if check == "instanceNorm":
                raise RuntimeError("HIPRTC_ERROR_COMPILATION: 'type_traits' file not found")
        with mock.patch.object(probe, "run_operator_check", side_effect=execute), \
                mock.patch("worker_rocm.classify_rocm_failure", return_value="miopen_compilation", create=True):
            probe._run_mode(self.torch, 0, "miopen", False, self.sink)
        failed = self.events[-1][1]["checks"][4]
        self.assertEqual(failed["exceptionType"], "RuntimeError")
        self.assertIn("type_traits", failed["detail"])
        self.assertEqual(failed["message"], "The ROCm operator or runtime compiler failed")

    def test_fp16_is_executed_independently_and_optional_failure_does_not_stop_fp32(self):
        def execute(_torch, _device, check, precision):
            if check == "instanceNorm" and precision == "fp16":
                raise NotImplementedError("InstanceNorm Half unavailable")
        with mock.patch.object(probe, "run_operator_check", side_effect=execute) as operator, \
                mock.patch("worker_rocm.classify_rocm_failure", return_value="unknown", create=True):
            probe._run_mode(self.torch, 0, "miopen", True, self.sink)
        self.assertEqual(operator.call_count, 14)
        report = self.events[-1][1]
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["checks"][11]["precision"], "fp16")
        self.assertEqual(report["checks"][11]["status"], "unavailable")
        self.assertEqual(report["checks"][4]["status"], "passed")

    def test_real_cpu_numeric_checks_validate_implementation_only(self):
        import torch

        previous = torch.get_num_threads()
        try:
            torch.set_num_threads(1)
            variants = ("conv1d", "conv3d", "convTranspose1d", "convTranspose2d", "convTranspose3d",
                        "gru", "rnnSimple", "batchNorm1d", "batchNorm3d", "instanceNorm1d", "instanceNorm3d")
            for check in (*probe._CHECKS, *variants):
                with self.subTest(check=check):
                    probe.run_operator_check(torch, torch.device("cpu"), check)
        finally:
            torch.set_num_threads(previous)
        self.assertFalse(probe._inventory(types.SimpleNamespace(version=types.SimpleNamespace(hip=None)))["available"])


class ProbeChildProcessTests(unittest.TestCase):
    def test_native_exit_without_json_keeps_exit_code_and_failure_stage(self):
        original = subprocess.Popen

        def start(_command, **kwargs):
            return original([sys.executable, "-I", "-c", "import sys,os;sys.stdin.readline();os._exit(17)"], **kwargs)

        with mock.patch.object(probe.subprocess, "Popen", side_effect=start):
            result = probe._run_child({"phase": "inventory"}, dict(os.environ), 5, lambda _check: None)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["exitCode"], 17)
        self.assertEqual(result["failureStage"], "inventory")

    @unittest.skipUnless(os.name == "nt", "Nested job verification is Windows-specific")
    def test_probe_jobs_work_inside_an_existing_worker_job(self):
        original = subprocess.Popen
        worker_directory = str(Path(probe.__file__).resolve().parent)
        script = ("import sys,os,json;sys.stdin.readline();"
                  f"sys.path.insert(0,{worker_directory!r});import worker_rocm_probe as p;"
                  "result=p._run_child({'phase':'inventory'},dict(os.environ),20,lambda _:None);"
                  "p._internal_emit('result',result)")

        def start(command, **kwargs):
            return original([sys.executable, "-I", "-u", "-c", script], **kwargs)

        with mock.patch.object(probe.subprocess, "Popen", side_effect=start):
            result = probe._run_child({"phase": "inventory"}, dict(os.environ), 25, lambda _check: None)
        self.assertIn("available", result)
        self.assertNotEqual(result.get("errorCode"), "ROCM_PROBE_CHILD_FAILED")

    def test_real_non_rocm_interpreter_reports_unavailable(self):
        import torch

        if getattr(torch.version, "hip", None):
            self.skipTest("This check requires a non-ROCm Torch runtime")
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        result = probe._run_child({"phase": "inventory"}, environment, 20, lambda _check: None)
        self.assertFalse(result["available"])
        self.assertEqual(result["errorCode"], "ROCM_RUNTIME_UNAVAILABLE")

    def test_timeout_kills_spawned_descendant_and_returns_partial_checks(self):
        original = subprocess.Popen
        seen = []
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "descendant.pid"
            script = ("import pathlib,subprocess,sys,time;sys.stdin.readline();"
                      "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                      f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid));"
                      f"print({(probe._PREFIX + json.dumps({'kind':'check','value':probe._check_result('allocation','fp32','passed','OK')}))!r},flush=True);"
                      "time.sleep(60)")

            def start(command, **kwargs):
                if len(command) > 5 and command[1] == "-I" and "runpy.run_module" in command[5]:
                    return original([sys.executable, "-I", "-u", "-c", script], **kwargs)
                return original(command, **kwargs)

            with mock.patch.object(probe.subprocess, "Popen", side_effect=start):
                result = probe._run_child({"phase": "mode"}, dict(os.environ), 0.8, seen.append)
            self.assertEqual(result["status"], "timeout")
            self.assertEqual(len(result["checks"]), 1)
            self.assertEqual(len(seen), 1)
            self.assertTrue(pid_file.exists())
            descendant = int(pid_file.read_text())
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                kernel.OpenProcess.restype = wintypes.HANDLE
                handle = kernel.OpenProcess(0x1000, False, descendant)
                if handle:
                    code = wintypes.DWORD()
                    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
                    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                    try:
                        self.assertTrue(kernel.GetExitCodeProcess(handle, ctypes.byref(code)))
                        self.assertNotEqual(code.value, 259)
                    finally:
                        kernel.CloseHandle(handle)
            else:
                result = subprocess.run(["ps", "-o", "stat=", "-p", str(descendant)],
                                        capture_output=True, text=True, check=False)
                self.assertTrue(not result.stdout.strip() or result.stdout.strip().startswith("Z"))

    def test_cancel_event_uses_confirmed_subtree_cleanup(self):
        cancelled = Event()
        cancelled.set()
        result = probe._run_child({"phase": "inventory"}, dict(os.environ), 20, lambda _check: None, cancelled)
        self.assertEqual(result["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
