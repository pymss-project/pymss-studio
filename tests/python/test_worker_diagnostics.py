from pathlib import Path
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import worker
import worker_bootstrap
import worker_diagnostics
import worker_plugins


class PluginDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "plugins"
        for patch in (
            mock.patch.object(worker_plugins, "_root", return_value=self.root),
            mock.patch.object(worker_plugins, "_require_plugin_runtime"),
            mock.patch.object(worker_plugins, "_management_supported", return_value=True),
            mock.patch.object(worker_bootstrap, "_read_runtime_state", return_value={"pythonPath": sys.executable}),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def package(self, name="example.provider", *, enabled=True):
        package = self.root / ("installed" if enabled else "disabled") / name
        package.mkdir(parents=True)
        (package / "pyproject.toml").write_text(
            f'[project]\nname="{name}"\nversion="1.0"\n[tool.pymss.plugin]\nid="{name}"\napi_version=2\n', encoding="utf-8")
        (package / "__init__.py").write_text('raise AssertionError("diagnostics loaded plugin code")', encoding="utf-8")
        return package

    def test_reading_does_not_load_recover_or_change_plugins(self):
        self.package()
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with mock.patch.object(worker_plugins, "_probe") as probe, \
             mock.patch.object(worker_plugins, "_inventory", side_effect=AssertionError("inventory repairs state")), \
             mock.patch.object(worker_plugins, "_recover_for_read", side_effect=AssertionError("recovery writes state")):
            result = worker_diagnostics.plugin_diagnostics()
        probe.assert_not_called()
        self.assertTrue(result["runtime"]["interpreterMatches"])
        self.assertIsNone(result["plugins"][0]["loaded"])
        self.assertFalse(result["verification"]["performed"])
        after = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_absent_store_is_not_created(self):
        result = worker_diagnostics.plugin_diagnostics()
        self.assertEqual(result["plugins"], [])
        self.assertFalse(self.root.exists())

    def test_old_core_remains_observable_and_cannot_be_verified(self):
        self.package()
        with mock.patch.object(worker_plugins, "_require_plugin_runtime", side_effect=worker_plugins.PluginRuntimeIncompatible("Missing plugin APIs")), \
             mock.patch.object(worker_plugins, "_management_supported", return_value=False), \
             mock.patch.object(worker_plugins, "_probe") as probe:
            result = worker_diagnostics.plugin_diagnostics(verify=True)
        probe.assert_not_called()
        self.assertFalse(result["runtime"]["pluginApisSupported"])
        self.assertFalse(result["runtime"]["managementSupported"])
        self.assertIn("Missing plugin APIs", result["verification"]["error"])
        self.assertEqual(len(result["plugins"]), 1)

    def test_actual_interpreter_is_compared_with_the_configured_one(self):
        with mock.patch.object(worker_bootstrap, "_read_runtime_state", return_value={"pythonPath": str(self.root / "other/Scripts/python.exe")}):
            self.assertFalse(worker_diagnostics.plugin_diagnostics()["runtime"]["interpreterMatches"])
            with mock.patch.object(worker_plugins, "_probe") as probe:
                result = worker_diagnostics.plugin_diagnostics(verify=True)
            probe.assert_not_called()
            self.assertIn("differs", result["verification"]["error"])
        with mock.patch.object(worker_bootstrap, "_read_runtime_state", return_value=None):
            self.assertIsNone(worker_diagnostics.plugin_diagnostics()["runtime"]["interpreterMatches"])

    def test_api_import_failures_preserve_the_underlying_dependency_error(self):
        def unavailable():
            raise worker_plugins.PluginRuntimeIncompatible("Plugin APIs unavailable") from ModuleNotFoundError("No module named 'librosa'")
        with mock.patch.object(worker_plugins, "_require_plugin_runtime", side_effect=unavailable):
            result = worker_diagnostics.plugin_diagnostics()
        self.assertIn("librosa", result["runtime"]["apiError"])

    @unittest.skipUnless(os.name == "nt", "Windows interpreter aliases")
    def test_windows_extended_and_case_variant_paths_identify_the_same_interpreter(self):
        expected = worker_diagnostics._python_identity(sys.executable)
        self.assertEqual(worker_diagnostics._python_identity(sys.executable.upper()), expected)
        self.assertEqual(worker_diagnostics._python_identity("\\\\?\\" + sys.executable), expected)

    @unittest.skipIf(os.name == "nt", "POSIX venv executable links")
    def test_venvs_sharing_a_base_executable_still_have_different_identities(self):
        base = Path(sys.executable).resolve()
        paths = [self.root / name / "bin/python" for name in ("first", "second")]
        for path in paths:
            path.parent.mkdir(parents=True)
            path.symlink_to(base)
        self.assertNotEqual(*(worker_diagnostics._python_identity(str(path)) for path in paths))

    def test_loading_is_explicit_and_disabled_records_are_not_marked_loaded(self):
        self.package()
        self.package("example.disabled", enabled=False)
        reports = [{"name": "example.provider", "managed": True, "loaded": True}]
        with mock.patch.object(worker_plugins, "_probe", return_value=reports) as probe:
            result = worker_diagnostics.plugin_diagnostics(verify=True)
        probe.assert_called_once_with(self.root)
        rows = {row["name"]: row for row in result["plugins"]}
        self.assertTrue(rows["example.provider"]["loaded"])
        self.assertIsNone(rows["example.disabled"]["loaded"])

    def test_committed_disable_and_uninstall_override_retained_runtime_files(self):
        self.package()
        inventory = self.root.parent / "inventory.json"
        for state in ["disabled", "removed"]:
            with self.subTest(state=state):
                inventory.write_text(json.dumps({"schemaVersion": 1, "revision": 1, "defaultPolicy": None,
                    "plugins": {"example.provider": {"state": state, "revision": 0}}}), encoding="utf-8")
                before = inventory.read_bytes()
                with mock.patch("pymss_plugin_manager.inventory.merge", side_effect=AssertionError("must not reconcile")), \
                     mock.patch("pymss_plugin_manager.inventory._write", side_effect=AssertionError("must not write")):
                    result = worker_diagnostics.plugin_diagnostics()
                self.assertEqual(inventory.read_bytes(), before)
                if state == "disabled":
                    self.assertFalse(result["plugins"][0]["enabled"])
                else:
                    self.assertEqual(result["plugins"], [])

    def test_a_binding_for_a_different_interpreter_is_not_verified(self):
        self.package()
        (self.root / ".pymss-binding.json").write_text(json.dumps({"schemaVersion": 1, "environment": {"python": "/different/python"}}), encoding="utf-8")
        with mock.patch.object(worker_plugins, "_probe") as probe:
            result = worker_diagnostics.plugin_diagnostics(verify=True)
        probe.assert_not_called()
        self.assertIn("PLUGIN_ENV_MISMATCH", result["runtime"]["bindingError"])
        self.assertEqual(result["runtime"]["bindingError"], result["verification"]["error"])

    def test_verification_and_manifest_errors_remain_visible(self):
        package = self.package()
        (package / "pyproject.toml").write_text("[broken", encoding="utf-8")
        with mock.patch.object(worker_plugins, "_probe", side_effect=RuntimeError("probe timed out")):
            result = worker_diagnostics.plugin_diagnostics(verify=True)
        self.assertIn("manifest", result["plugins"][0]["error"])
        self.assertEqual(result["verification"]["error"], "probe timed out")

    def test_verify_requires_a_boolean(self):
        for value in [None, "true", 1, 0]:
            with self.subTest(value=value), mock.patch.object(worker_diagnostics, "emit_error", return_value=1) as error:
                self.assertEqual(worker_diagnostics.cmd_plugin_diagnostics({"verify": value}), 1)
                self.assertEqual(error.call_args.args[0], "PLUGIN_DIAGNOSTICS_FAILED")

    def test_dispatch_skips_network_and_proxy_setup(self):
        with mock.patch.object(worker_diagnostics, "emit") as emit, \
             mock.patch.object(worker, "configure_process_proxy", side_effect=AssertionError("proxy setup is unnecessary")), \
             mock.patch.object(worker, "load_proxy_config", side_effect=AssertionError("proxy lookup is unnecessary")):
            self.assertEqual(worker.run_command("plugin_diagnostics", {}), 0)
        self.assertEqual(emit.call_args.args[0], "plugin_diagnostics")
        json.dumps(emit.call_args.args[1])
