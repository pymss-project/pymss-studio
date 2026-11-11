from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sysconfig
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
        for name in ("setuptools", "wheel"):
            package = site_packages / name
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            info = site_packages / f"{name}-78.1.0.dist-info"
            info.mkdir()
            (info / "METADATA").write_text(f"Name: {name}\nVersion: 78.1.0\n", encoding="utf-8")
        return runtime, site_packages

    def _check_packaging_dependencies(self, site_packages: Path) -> None:
        self.assertTrue((site_packages / "setuptools/__init__.py").is_file())
        self.assertTrue((site_packages / "setuptools-78.1.0.dist-info/METADATA").is_file())
        self.assertFalse((site_packages / "wheel").exists())
        self.assertFalse((site_packages / "wheel-78.1.0.dist-info").exists())

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
        self._check_packaging_dependencies(site_packages)

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
        self._check_packaging_dependencies(site_packages)

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

    def _check_rocm_resources(self, *, windows: bool) -> None:
        runtime, site = self._write_sample_runtime(windows=windows)
        protected = [
            "_rocm_sdk_core_7_2_1/include/hip/hip_runtime.h",
            "_rocm_sdk_core_7_2_1/lib/llvm/lib/clang/20/include/stddef.h",
            "_rocm_sdk_core_7_2_1/lib/llvm/bin/offload-arch.exe",
            "_rocm_sdk_core_7_2_1/bin/hiprtc-builtins.bc",
            "_rocm_sdk_devel_7_2_1/docs/runtime-data.json",
            "rocm_sdk/tests/base_test.py",
            "rocm_sdk_device_gfx1031/lib/library.kpack",
            "amd_torch_device_gfx1031/kernels/attention.hsaco",
            "rocm_sdk_core-7.2.1.dist-info/WHEEL",
            "rocm_sdk_core-7.2.1.dist-info/METADATA",
            "rocm_sdk_core-7.2.1.dist-info/entry_points.txt",
            "amd_torch_device_gfx1031-2.13.0.dist-info/WHEEL",
            "torch/include/ATen/native/hip/Loops.cuh",
            "torch/include/ATen/native/hip/Kernel.h",
        ]
        for name in protected:
            path = site / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"runtime resource")
        unrelated = site / "cachepkg/include/unneeded.h"
        unrelated.parent.mkdir(parents=True)
        unrelated.touch()
        launcher = runtime / ("Scripts/rocm-sdk.exe" if windows else "bin/rocm-sdk")
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(b"SDK entry point")
        script_dir = Path(__file__).resolve().parents[2] / "scripts"
        if windows:
            shell = shutil.which("pwsh") or shutil.which("powershell")
            if shell is None:
                self.skipTest("PowerShell is required")
            command = [shell, "-NoProfile", "-File", str(script_dir / "prune-python-runtime.ps1"),
                       "-RuntimeDir", str(runtime), "-Backend", "rocm"]
        else:
            shell = shutil.which("bash")
            if shell is None:
                self.skipTest("Bash is required")
            command = [shell, str(script_dir / "prune-python-runtime.sh"), str(runtime), "--backend=rocm"]
        result = subprocess.run(command, capture_output=True, text=True, env={**os.environ, "OS": "Windows_NT"})
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in protected:
            self.assertEqual((site / name).read_bytes(), b"runtime resource", name)
        self.assertFalse(unrelated.exists())
        self.assertFalse((site / "cachepkg/tests").exists())
        self.assertEqual(launcher.read_bytes(), b"SDK entry point")
        self._check_packaging_dependencies(site)

    def test_windows_rocm_prune_retains_hiprtc_kernels_sdk_metadata_and_tools(self):
        self._check_rocm_resources(windows=True)

    def test_posix_rocm_prune_retains_hiprtc_kernels_sdk_metadata_and_tools(self):
        self._check_rocm_resources(windows=False)

    def _check_posix_xmlrpc_survives_pruning(self, *, keep_venv: bool) -> None:
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is required for the POSIX pruning script test")
        runtime = (self.root / "xmlrpc-runtime").resolve()
        stdlib = runtime / "lib" / "python3.12"
        shutil.copytree(
            Path(sysconfig.get_path("stdlib")) / "xmlrpc", stdlib / "xmlrpc",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        for name in ("idlelib", "tkinter", "venv", "ensurepip"):
            package = stdlib / name
            package.mkdir()
            (package / "__init__.py").touch()
        script = Path(__file__).resolve().parents[2] / "scripts/prune-python-runtime.sh"
        result = subprocess.run(
            [bash, str(script), str(runtime), *(["--keep-venv"] if keep_venv else [])],
            capture_output=True, text=True, cwd=runtime,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((stdlib / "xmlrpc/client.py").is_file(), "pip install requires the retained xmlrpc package")
        probe = subprocess.run(
            [sys.executable, "-I", "-B", "-S", "-c", """
import json, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import xmlrpc, xmlrpc.client
assert Path(xmlrpc.__file__).resolve() == root / 'xmlrpc/__init__.py'
assert Path(xmlrpc.client.__file__).resolve() == root / 'xmlrpc/client.py'
value = {'status': 'ready'}
assert xmlrpc.client.loads(xmlrpc.client.dumps((value,)))[0] == (value,)
print(json.dumps({'origin': xmlrpc.client.__file__}))
""", str(stdlib)], capture_output=True, text=True,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(Path(json.loads(probe.stdout)["origin"]).resolve(), stdlib / "xmlrpc/client.py")
        self.assertFalse((stdlib / "idlelib").exists())
        self.assertFalse((stdlib / "tkinter").exists())
        self.assertEqual((stdlib / "venv").exists(), keep_venv)
        self.assertEqual((stdlib / "ensurepip").exists(), keep_venv)

    def test_posix_prune_preserves_xmlrpc_for_the_package_installer(self) -> None:
        self._check_posix_xmlrpc_survives_pruning(keep_venv=False)

    def test_posix_prune_with_venv_preserves_xmlrpc_for_the_package_installer(self) -> None:
        self._check_posix_xmlrpc_survives_pruning(keep_venv=True)

    def _check_posix_native_tk_pruning(self, *, keep_venv: bool) -> None:
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is required for the POSIX pruning script test")
        runtime, site_packages = self._write_sample_runtime(windows=False)
        library = runtime / "lib"
        stdlib = library / "python3.12"
        removed_files = [
            stdlib / "lib-dynload/_tkinter.cpython-312-x86_64-linux-gnu.so",
            stdlib / "lib-dynload/_tkinter.abi3.so",
            library / "libtcl9.0.so",
            library / "libtcl9tk9.0.so",
            library / "libtk8.6.so.0",
            library / "libtcl9.0.dylib",
            library / "libtk8.6.dylib",
        ]
        removed_directories = [
            stdlib / "tkinter", library / "tcl9", library / "tcl9.0", library / "tk9.0",
            library / "itcl4.3.5", library / "thread3.0.4",
        ]
        for directory in removed_directories:
            removed_files.append(directory / "libtcl-extension.so")
        retained_files = [
            library / "libpython3.12.so.1.0", library / "libssl.so.3",
            stdlib / "lib-dynload/_ssl.cpython-312-x86_64-linux-gnu.so",
            stdlib / "lib-dynload/_ctypes.cpython-312-x86_64-linux-gnu.so",
            stdlib / "xmlrpc/client.py", stdlib / "venv/__init__.py",
            stdlib / "ensurepip/__init__.py", library / "threadpool/data",
            site_packages / "tkinter_helpers/__init__.py",
        ]
        for path in [*removed_files, *retained_files]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"runtime fixture")
        script = Path(__file__).resolve().parents[2] / "scripts/prune-python-runtime.sh"
        result = subprocess.run(
            [bash, str(script), str(runtime), *(["--keep-venv"] if keep_venv else [])],
            capture_output=True, text=True, cwd=runtime,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in [*removed_files, *removed_directories]:
            self.assertFalse(path.exists(), str(path.relative_to(runtime)))
        for path in retained_files:
            if path.parent.name in {"venv", "ensurepip"}:
                self.assertEqual(path.exists(), keep_venv)
            else:
                self.assertEqual(path.read_bytes(), b"runtime fixture")
        self._check_packaging_dependencies(site_packages)

    def test_posix_prune_removes_native_tk_dependencies(self) -> None:
        self._check_posix_native_tk_pruning(keep_venv=False)

    def test_posix_prune_with_venv_removes_native_tk_dependencies(self) -> None:
        self._check_posix_native_tk_pruning(keep_venv=True)
