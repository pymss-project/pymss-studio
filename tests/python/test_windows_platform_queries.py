import os
import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from . import test_worker_bootstrap as bootstrap_cases

import worker_bootstrap
import worker_models
import worker_plugin_contract
import worker_operation_metadata
import worker_optional_packages
from worker_runtime_lock import runtime_lock


class WindowsPlatformQueryTests(unittest.TestCase):
    def test_plugin_compatibility_does_not_query_wmi(self):
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.dict(os.environ, {"PROCESSOR_ARCHITECTURE": "AMD64", "PROCESSOR_ARCHITEW6432": ""}), \
                mock.patch.object(platform, "machine", side_effect=AssertionError("WMI queried")):
            self.assertEqual(worker_plugin_contract.runtime_profile()["architecture"], "x86_64")

    def test_runtime_info_does_not_query_wmi_for_architecture(self):
        with mock.patch.object(platform, "machine", side_effect=AssertionError("WMI queried")):
            payload, _ = bootstrap_cases.RuntimeReadinessTests()._payload("win32", {
                "packages": {}, "packageVersions": {}, "torchBackend": "cpu",
                "torchVersion": "2.7.1", "acceleratorAvailable": False,
            })
        self.assertEqual(payload["platform"], "win32")

    def test_environment_info_does_not_query_wmi_for_os_description(self):
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(sys, "getwindowsversion", return_value=SimpleNamespace(major=10, minor=0, build=26100), create=True), \
                mock.patch.object(platform, "platform", side_effect=AssertionError("WMI queried")), \
                mock.patch.dict(sys.modules, {"pymss": None, "torch": None}), \
                mock.patch.object(worker_models, "import_available", return_value=False), \
                mock.patch.object(worker_models, "package_version", return_value=None), \
                mock.patch.object(worker_models, "emit") as emit:
            self.assertEqual(worker_models.cmd_env_info(), 0)
        self.assertTrue(emit.call_args.args[1]["platform"].startswith("Windows-"))

    def test_runtime_probe_has_a_deadline(self):
        expired = subprocess.TimeoutExpired("runtime probe", 60)
        with mock.patch.object(worker_bootstrap.subprocess, "check_output", side_effect=expired) as probe:
            with self.assertRaises(subprocess.TimeoutExpired):
                worker_bootstrap._probe_python_runtime(Path(sys.executable))
        self.assertGreater(probe.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(probe.call_args.kwargs["timeout"], 60)

    def test_operation_metadata_fingerprint_does_not_query_wmi(self):
        runtime = SimpleNamespace(packages_dir=Path("missing-packages"), backend="cpu", identity="unchanged")
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(sys, "path", []), \
                mock.patch.object(worker_optional_packages, "_runtime_context", return_value=runtime), \
                mock.patch.object(platform, "machine", side_effect=AssertionError("WMI queried")):
            self.assertTrue(worker_operation_metadata._profile_fingerprint(()))

    def test_probe_timeout_releases_environment_lock_and_allows_retry(self):
        expired = subprocess.TimeoutExpired("runtime probe", 60)
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", Path(temporary)), \
                mock.patch.object(worker_bootstrap, "_manifest", return_value=bootstrap_cases.MANIFEST), \
                mock.patch.object(worker_bootstrap, "_read_runtime_state", return_value={"pythonPath": sys.executable}), \
                mock.patch.object(worker_bootstrap, "_installed_envs", return_value=[]), \
                mock.patch.object(worker_bootstrap, "_recover_runtime_transactions"), \
                mock.patch.object(worker_bootstrap, "_module_available", return_value=True), \
                mock.patch.object(worker_bootstrap, "_probe_python_runtime", side_effect=[expired, bootstrap_cases.probe_result()]), \
                mock.patch.object(worker_bootstrap, "_emit") as emit:
            self.assertEqual(worker_bootstrap.cmd_runtime_info({}), 0)
            failed = emit.call_args.args[1]
            self.assertFalse(failed["ready"])
            self.assertEqual(failed["liveProbeError"]["code"], "PYTHON_PROBE_TIMEOUT")
            with runtime_lock(Path(temporary) / ".runtime-management.lock", timeout=0):
                pass
            self.assertEqual(worker_bootstrap.cmd_runtime_info({}), 0)
            self.assertTrue(emit.call_args.args[1]["ready"])


if __name__ == "__main__":
    unittest.main()
