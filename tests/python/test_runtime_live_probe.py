import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
    from .test_worker_bootstrap import MANIFEST, probe_result
else:
    import _bootstrap as _worker_test_bootstrap
    from test_worker_bootstrap import MANIFEST, probe_result

import worker_bootstrap


class RuntimeLiveProbeTests(unittest.TestCase):
    def setUp(self):
        self.python = Path(sys.executable)
        self.state = {
            **probe_result("cuda"),
            "backend": "cuda", "pythonPath": str(self.python),
            "pymssVersion": "2.1.3", "pymssCoreVersion": "1.0.0",
            "manifestVersion": MANIFEST["manifestVersion"], "installedAt": "2026-09-01",
        }
        self.original = copy.deepcopy(self.state)
        self.probe = self.enterContext(mock.patch.object(worker_bootstrap, "_probe_python_runtime"))
        self.enterContext(mock.patch.object(worker_bootstrap, "_manifest", return_value=MANIFEST))
        self.enterContext(mock.patch.object(worker_bootstrap, "_read_runtime_state", return_value=self.state))
        self.enterContext(mock.patch.object(worker_bootstrap, "_installed_envs", side_effect=lambda *a, **k: [copy.deepcopy(self.state)]))
        self.enterContext(mock.patch.object(worker_bootstrap, "_module_available", return_value=True))
        self.enterContext(mock.patch.object(worker_bootstrap, "_detect_gpu_vendors", return_value=[]))
        self.write = self.enterContext(mock.patch.object(worker_bootstrap, "_atomic_write_json"))

    def assert_failed(self, result, code):
        self.assertFalse(result["ready"])
        self.assertFalse(result["acceleratorAvailable"])
        self.assertNotEqual(result["pymssGraphAvailable"], True)
        self.assertTrue(result["torchBackend"].startswith("error:"))
        self.assertEqual(result["liveProbeError"]["code"], code)
        self.assertTrue(result["liveProbeError"]["message"])
        active = result["installedEnvironments"][0]
        self.assertEqual(active["health"], "broken")
        self.assertFalse(active["acceleratorAvailable"])
        self.assertNotEqual(active["pymssGraphAvailable"], True)
        self.assertEqual(result["installedBackend"], "cuda")
        self.assertEqual(result["installState"], self.original)
        self.assertEqual(self.state, self.original)
        self.assertEqual(result["pymssVersion"], "2.1.3")
        self.assertEqual(result["torchVersion"], self.state["torchVersion"])
        self.write.assert_not_called()

    def test_failed_active_probe_does_not_promote_cached_capabilities_to_readiness(self):
        failures = [
            (PermissionError(13, "secret-value", "secret-path"), "PYTHON_START_FAILED"),
            (subprocess.CalledProcessError(3, ["python", "secret-command"], output="secret-output"), "PYTHON_PROBE_FAILED"),
            (json.JSONDecodeError("invalid", "secret-response", 0), "PYTHON_PROBE_INVALID_RESPONSE"),
            (RuntimeError("secret-value"), "PYTHON_PROBE_FAILED"),
        ]
        for failure, code in failures:
            with self.subTest(code=code):
                self.probe.side_effect = failure
                result = worker_bootstrap._runtime_info_payload({})
                self.assert_failed(result, code)
                self.assertNotIn("secret-", json.dumps(result))
                self.assertLessEqual(len(result["liveProbeError"]["message"]), 240)

    def test_torch_import_failure_has_a_safe_diagnostic_and_retains_the_windows_error_code(self):
        self.probe.return_value = {
            **probe_result("error:OSError: [WinError 1455] secret-path https://user:password@host/?token=secret-value"),
            "pymssVersion": "2.1.3", "pymssCoreVersion": "1.0.0",
            "acceleratorAvailable": True,
        }
        result = worker_bootstrap._runtime_info_payload({})
        self.assert_failed(result, "TORCH_IMPORT_FAILED")
        self.assertIn("WinError 1455", result["liveProbeError"]["message"])
        for secret in ("secret-", "password", "https://", "token="):
            self.assertNotIn(secret, json.dumps(result))

    def test_successful_refresh_clears_failure_and_restores_live_capabilities(self):
        self.probe.side_effect = OSError("first probe failed")
        self.assert_failed(worker_bootstrap._runtime_info_payload({}), "PYTHON_START_FAILED")
        self.probe.side_effect = None
        self.probe.return_value = probe_result("cuda")
        result = worker_bootstrap._runtime_info_payload({})
        self.assertIsNone(result["liveProbeError"])
        self.assertTrue(result["ready"])
        self.assertTrue(result["acceleratorAvailable"])
        self.assertEqual(result["installedEnvironments"][0]["health"], "ready")
        self.assertEqual(self.state, self.original)
        self.write.assert_not_called()

    def test_bootstrap_failures_remain_unready_without_creating_an_active_pointer(self):
        failures = (
            OSError("secret-path"),
            json.JSONDecodeError("invalid", "secret-response", 0),
            probe_result("error:OSError: [WinError 1455] secret-path"),
        )
        with mock.patch.object(worker_bootstrap, "_read_runtime_state", return_value=None), \
                mock.patch.object(worker_bootstrap, "_installed_envs", return_value=[]):
            for failure in failures:
                with self.subTest(failure=type(failure).__name__):
                    self.probe.side_effect = failure if isinstance(failure, Exception) else None
                    self.probe.return_value = failure
                    result = worker_bootstrap._runtime_info_payload({})
                    self.assertFalse(result["ready"])
                    self.assertFalse(result["acceleratorAvailable"])
                    self.assertIsNone(result["pymssGraphAvailable"])
                    self.assertIsNone(result["installedBackend"])
                    self.assertIsNone(result["installState"])
                    self.assertTrue(result["liveProbeError"])
                    self.assertNotIn("secret-", json.dumps(result))
        self.write.assert_not_called()


class RuntimeProbeProtocolTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(worker_bootstrap, "_manifest", return_value=MANIFEST))

    def test_empty_non_object_and_incomplete_responses_are_rejected(self):
        for output in ("", "not json", "null", "[]", "{}", '{"packages":{},"torchBackend":"cpu"}'):
            with self.subTest(output=output), \
                    mock.patch.object(worker_bootstrap.subprocess, "check_output", return_value=output):
                with self.assertRaises(ValueError):
                    worker_bootstrap._probe_python_runtime(Path(sys.executable))

    def test_valid_probe_response_is_preserved_and_stderr_is_captured(self):
        result = probe_result("cpu")
        with mock.patch.object(worker_bootstrap.subprocess, "check_output", return_value=json.dumps(result)) as run:
            self.assertEqual(worker_bootstrap._probe_python_runtime(Path(sys.executable)), result)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.PIPE)

    def test_missing_and_non_boolean_capability_fields_are_rejected(self):
        for field in ("pymssGraphAvailable", "acceleratorAvailable"):
            for value in ("absent", None, "true", 1):
                with self.subTest(field=field, value=value):
                    result = probe_result("cpu")
                    result[field] = value
                    if value == "absent":
                        del result[field]
                    with mock.patch.object(worker_bootstrap.subprocess, "check_output", return_value=json.dumps(result)):
                        with self.assertRaises(ValueError):
                            worker_bootstrap._probe_python_runtime(Path(sys.executable))

    def test_requested_package_versions_must_be_present_and_nullable_strings(self):
        for value in ("absent", 123, {}, []):
            with self.subTest(value=value):
                result = probe_result("cpu")
                result["packageVersions"]["pymss"] = value
                if value == "absent":
                    del result["packageVersions"]["pymss"]
                with mock.patch.object(worker_bootstrap.subprocess, "check_output", return_value=json.dumps(result)):
                    with self.assertRaises(ValueError):
                        worker_bootstrap._probe_python_runtime(Path(sys.executable))


if __name__ == "__main__":
    unittest.main()
