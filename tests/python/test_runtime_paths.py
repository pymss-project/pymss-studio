from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
from pathlib import Path
from unittest import mock

from . import _bootstrap  # noqa: F401
import worker_runtime_paths as paths


class RocmRuntimePathsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.environment = self.root / "runtime with spaces"
        venv.EnvBuilder(with_pip=False).create(self.environment)
        self.python = self.environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.site = self.environment / ("Lib/site-packages" if os.name == "nt" else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")

    def _sdk_package(self, name):
        package = self.site / name
        (package / "bin").mkdir(parents=True)
        (package / "lib/llvm/bin").mkdir(parents=True)
        (package / "lib/rocm_sysdeps/bin").mkdir(parents=True)
        (package / "__init__.py").touch()
        return package

    def _official_sdk(self):
        sdk = self.site / "rocm_sdk"
        sdk.mkdir()
        (sdk / "__init__.py").touch()
        (sdk / "_dist_info.py").write_text("""
class Package:
    is_target_specific = False
    def get_py_package_name(self, family=None):
        return '_rocm_sdk_core_7_2_1'
ALL_PACKAGES = {'core': Package()}
AVAILABLE_TARGET_FAMILIES = []
""", encoding="utf-8")
        return self._sdk_package("_rocm_sdk_core_7_2_1")

    def test_query_uses_selected_interpreter_and_official_versioned_package_table(self):
        selected = self._official_sdk()
        other = self._sdk_package("_rocm_sdk_core_10_0_0")
        foreign = self.root / "foreign"
        foreign.mkdir()
        (foreign / "rocm_sdk.py").write_text("raise RuntimeError('foreign PYTHONPATH was used')", encoding="utf-8")
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(foreign)}):
            report = paths.discover_rocm_native_paths(self.environment, self.python)
        self.assertEqual(report["nativePathsSource"], "rocm-sdk")
        self.assertEqual(report["nativePathsModules"], [selected.name])
        self.assertTrue(report["nativeToolDirs"])
        self.assertTrue(all(selected.name in value and other.name not in value for value in report["nativeToolDirs"]))
        self.assertTrue(all(not Path(value).is_absolute() and "\\" not in value for value in report["nativeLibraryDirs"]))
        absolute = paths.resolve_rocm_native_paths(self.environment, report)
        self.assertIn(str((selected / "lib/llvm/bin").resolve()), absolute["nativeToolDirs"])

    def test_discovery_and_persisted_relative_paths_survive_relocation(self):
        self._official_sdk()
        report = paths.discover_rocm_native_paths(self.environment, self.python)
        moved = self.root / "relocated environment"
        shutil.move(self.environment, moved)
        relocated_python = moved / self.python.relative_to(self.environment)
        after = paths.discover_rocm_native_paths(moved, relocated_python)
        self.assertEqual(after, report)
        for value in paths.resolve_rocm_native_paths(moved, report)["nativeToolDirs"]:
            self.assertTrue(Path(value).is_relative_to(moved.resolve()))

    def test_missing_sdk_uses_only_local_legacy_payloads(self):
        self._sdk_package("_rocm_sdk_core_legacy")
        report = paths.discover_rocm_native_paths(self.environment, self.python)
        self.assertEqual(report["nativePathsSource"], "legacy")
        self.assertEqual(report["nativePathsQueryStatus"], "sdk-not-installed")
        self.assertEqual(report["nativePathsModules"], ["_rocm_sdk_core_legacy"])

    def test_ambiguous_legacy_sdk_versions_are_rejected(self):
        self._sdk_package("_rocm_sdk_core_7_2_1")
        self._sdk_package("_rocm_sdk_core_10_0_0")
        with self.assertRaisesRegex(ValueError, "Multiple legacy"):
            paths.discover_rocm_native_paths(self.environment, self.python)

    def test_no_sdk_reports_empty_paths(self):
        report = paths.discover_rocm_native_paths(self.environment, self.python)
        self.assertEqual(report["nativePathsSource"], "none")
        self.assertEqual(report["nativeLibraryDirs"], [])
        self.assertEqual(report["nativeToolDirs"], [])

    def test_foreign_interpreter_is_rejected_even_when_legacy_payload_exists(self):
        self._sdk_package("_rocm_sdk_core_legacy")
        with self.assertRaisesRegex(RuntimeError, "outside"):
            paths.discover_rocm_native_paths(self.environment, Path(sys.executable))

    def test_persisted_absolute_and_traversal_paths_are_rejected(self):
        for value in ("../outside", "/outside", "C:/outside", "C:outside", "\\\\server\\share", "Lib\\site-packages", ".", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                paths.resolve_rocm_native_paths(self.environment, {"nativeToolDirs": [value]})
        with self.assertRaises(ValueError):
            paths.resolve_rocm_native_paths(self.environment, {"nativeToolDirs": "not-a-list"})

    def test_foreign_result_and_query_timeout_fail_closed(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({"modules": [{"name": "foreign", "root": str(self.root)}]}), "")
        with mock.patch.object(paths.subprocess, "run", return_value=result):
            with self.assertRaises(ValueError):
                paths.discover_rocm_native_paths(self.environment, self.python)
        with mock.patch.object(paths.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 0.1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                paths.discover_rocm_native_paths(self.environment, self.python, timeout=0.1)

    def test_symlink_directory_escape_is_rejected(self):
        link = self.environment / "outside"
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest("Directory symlinks are unavailable")
        with self.assertRaises(ValueError):
            paths.resolve_rocm_native_paths(self.environment, {"nativeToolDirs": ["outside"]})


if __name__ == "__main__":
    unittest.main()
