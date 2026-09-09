from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class RuntimePruningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, True))

    def _write_sample_runtime(self, *, windows: bool) -> tuple[Path, Path]:
        runtime = self.root / "runtime"
        site_packages = (
            runtime / "Lib" / "site-packages"
            if windows
            else runtime / "lib" / "python3.12" / "site-packages"
        )
        meta_dir = site_packages / "samplepkg-1.0.0.dist-info"
        meta_dir.mkdir(parents=True)
        (meta_dir / "METADATA").write_text("Name: samplepkg\nVersion: 1.0.0\n", encoding="utf-8")
        (meta_dir / "RECORD").write_text("samplepkg/__init__.py,,\n", encoding="utf-8")
        (meta_dir / "WHEEL").write_text("Wheel-Version: 1.0\n", encoding="utf-8")
        (meta_dir / "entry_points.txt").write_text("[console_scripts]\n", encoding="utf-8")
        (meta_dir / "INSTALLER").write_text("pip\n", encoding="utf-8")
        (meta_dir / "REQUESTED").write_text("", encoding="utf-8")
        (meta_dir / "LICENSE").write_text("sample license\n", encoding="utf-8")
        (meta_dir / "licenses").mkdir()
        cache_dir = site_packages / "cachepkg"
        (cache_dir / "__pycache__").mkdir(parents=True)
        (cache_dir / "__pycache__" / "x.pyc").write_bytes(b"pyc")
        (cache_dir / "tests").mkdir(parents=True)
        (cache_dir / "tests" / "test_x.py").write_text("pass", encoding="utf-8")

        core_meta_dir = site_packages / "pymss_core-0.1.6.dist-info"
        core_meta_dir.mkdir(parents=True)
        (core_meta_dir / "METADATA").write_text("Name: pymss-core\nVersion: 0.1.6\n", encoding="utf-8")
        (core_meta_dir / "RECORD").write_text("pymss_core/__init__.py,,\n", encoding="utf-8")
        (core_meta_dir / "WHEEL").write_text("Wheel-Version: 1.0\n", encoding="utf-8")
        return runtime, site_packages

    def test_windows_prune_keeps_runtime_versions_and_records(self) -> None:
        runtime, site_packages = self._write_sample_runtime(windows=True)
        script = Path(__file__).resolve().parents[2] / "scripts" / "prune-python-runtime.ps1"
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell is None:
            self.skipTest("PowerShell is required for the Windows pruning script test")
        command = [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-RuntimeDir",
            str(runtime),
            "-KeepScripts",
            "-KeepVenv",
        ]
        subprocess.run(command, check=True, cwd=runtime, env={**os.environ, "OS": "Windows_NT"})

        meta_dir = site_packages / "samplepkg-1.0.0.dist-info"
        self.assertTrue((meta_dir / "METADATA").exists())
        self.assertTrue((meta_dir / "RECORD").exists())
        self.assertFalse((meta_dir / "WHEEL").exists())
        self.assertTrue((meta_dir / "entry_points.txt").exists())
        self.assertTrue((meta_dir / "INSTALLER").exists())
        self.assertTrue((meta_dir / "REQUESTED").exists())
        self.assertTrue((meta_dir / "LICENSE").exists())
        self.assertTrue((meta_dir / "licenses").exists())
        core_meta_dir = site_packages / "pymss_core-0.1.6.dist-info"
        self.assertTrue((core_meta_dir / "METADATA").exists())
        self.assertTrue((core_meta_dir / "RECORD").exists())
        self.assertFalse((core_meta_dir / "WHEEL").exists())
        self.assertFalse((site_packages / "cachepkg" / "__pycache__").exists())
        self.assertFalse((site_packages / "cachepkg" / "tests").exists())

    def test_posix_prune_keeps_runtime_versions_and_records(self) -> None:
        runtime, site_packages = self._write_sample_runtime(windows=False)
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is required for the POSIX pruning script test")
        script = Path(__file__).resolve().parents[2] / "scripts" / "prune-python-runtime.sh"
        subprocess.run(
            [bash, str(script), str(runtime), "--keep-venv"],
            check=True,
            cwd=runtime,
            env=os.environ.copy(),
        )

        meta_dir = site_packages / "samplepkg-1.0.0.dist-info"
        self.assertTrue((meta_dir / "METADATA").exists())
        self.assertTrue((meta_dir / "RECORD").exists())
        self.assertFalse((meta_dir / "WHEEL").exists())
        self.assertTrue((meta_dir / "entry_points.txt").exists())
        self.assertTrue((meta_dir / "INSTALLER").exists())
        self.assertTrue((meta_dir / "REQUESTED").exists())
        self.assertTrue((meta_dir / "LICENSE").exists())
        self.assertTrue((meta_dir / "licenses").exists())
        core_meta_dir = site_packages / "pymss_core-0.1.6.dist-info"
        self.assertTrue((core_meta_dir / "METADATA").exists())
        self.assertTrue((core_meta_dir / "RECORD").exists())
        self.assertFalse((core_meta_dir / "WHEEL").exists())
        self.assertFalse((site_packages / "cachepkg" / "__pycache__").exists())
        self.assertFalse((site_packages / "cachepkg" / "tests").exists())
