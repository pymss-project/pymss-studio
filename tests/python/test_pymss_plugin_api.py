from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("plugin_api_probe", ROOT / "scripts/verify-pymss-plugin-api.py")
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


class PymssPluginApiTests(unittest.TestCase):
    def setUp(self):
        self.modules = {
            "pymss.plugins.kit": SimpleNamespace(**{name: lambda: None for name in (
                "current_operation_context", "run_native_process", "register_commands", "register_rpc_processes",
            )}),
            "pymss_plugin_manager": SimpleNamespace(PluginContext=type, PluginPolicy=type, PluginService=type),
            "pymss.plugins.marketplace": SimpleNamespace(fetch_official_catalog=lambda: None, source_info=lambda: None),
            "pymss_plugin_manager._engine": SimpleNamespace(UPDATE_REVIEW_VERSION=1),
        }
        self.enterContext(mock.patch.object(PROBE, "import_module", side_effect=self.modules.__getitem__))
        self.enterContext(mock.patch.object(PROBE.metadata, "version", return_value="2.3.4"))

    def test_complete_published_core_passes(self):
        self.assertEqual(PROBE.verify(), {"pymssVersion": "2.3.4", "pluginApisVerified": True})

    def test_unsupported_review_protocol_rejects_the_bundle(self):
        for version in (None, 0, 2, True, "1"):
            with self.subTest(version=version):
                self.modules["pymss_plugin_manager._engine"].UPDATE_REVIEW_VERSION = version
                with self.assertRaisesRegex(RuntimeError, "UPDATE_REVIEW_VERSION"):
                    PROBE.verify()

    def test_missing_or_noncallable_api_rejects_the_bundle(self):
        for module, attributes in self.modules.items():
            for name in tuple(vars(attributes)):
                original = getattr(attributes, name)
                for value in (None, "unavailable"):
                    with self.subTest(module=module, name=name, value=value):
                        setattr(attributes, name, value)
                        with self.assertRaisesRegex(RuntimeError, f"{module}.{name}"):
                            PROBE.verify()
                setattr(attributes, name, original)

    def test_import_failure_is_not_hidden(self):
        with mock.patch.object(PROBE, "import_module", side_effect=ImportError("dependency unavailable")):
            with self.assertRaisesRegex(ImportError, "dependency unavailable"):
                PROBE.verify()

    def test_all_packaging_paths_probe_the_selected_interpreter(self):
        shell = (ROOT / "scripts/prepare-python-runtime.sh").read_text(encoding="utf-8")
        windows = (ROOT / "scripts/prepare-python-runtime.ps1").read_text(encoding="utf-8")
        macos = (ROOT / ".github/workflows/release-macos.yml").read_text(encoding="utf-8")
        self.assertIn('runtime_python "$(dirname "$0")/verify-pymss-plugin-api.py"', shell)
        for interpreter in ("$envPython", "$runtimePython"):
            calls = [line for line in windows.splitlines()
                     if f"Invoke-NativeChecked -FilePath {interpreter} -Arguments @(" in line
                     and '(Join-Path $PSScriptRoot "verify-pymss-plugin-api.py")' in line]
            self.assertEqual(len(calls), 1)
            self.assertIn("'-I'", calls[0])
            self.assertIn("'-B'", calls[0])
        self.assertIn('"$PY" -B scripts/verify-pymss-plugin-api.py', macos)
