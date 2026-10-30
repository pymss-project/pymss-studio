from __future__ import annotations

import ast
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class PluginSdkBundleTests(unittest.TestCase):
    def test_ci_python_test_patterns_select_existing_test_cases(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        patterns = re.findall(r"python -m unittest discover -s tests/python -t tests -p [\"']([^\"']+)[\"']", workflow)
        self.assertTrue(patterns)
        for pattern in patterns:
            with self.subTest(pattern=pattern):
                files = list((ROOT / "tests/python").glob(pattern))
                self.assertTrue(files, f"CI test pattern does not match a file: {pattern}")
                self.assertTrue(any(
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
                    for path in files for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                ), f"CI test pattern does not contain test cases: {pattern}")

    def test_macos_release_maps_the_sdk_to_the_protocol_resource_directory(self):
        workflow = (ROOT / ".github/workflows/release-macos.yml").read_text(encoding="utf-8")
        step = workflow.split("- name: Build Tauri app bundle", 1)[1].split("- name:", 1)[0]
        generator = step.split("<<'PY'", 1)[1].split("\n", 1)[1].split("\n          PY", 1)[0]
        generator = "\n".join(line[10:] if line.startswith(" " * 10) else line for line in generator.splitlines())
        output = io.StringIO()
        with contextlib.chdir(ROOT), contextlib.redirect_stdout(output):
            exec(compile(generator, "macos-resource-config", "exec"), {})
        resources = json.loads(output.getvalue())["bundle"]["resources"]
        sdk = ROOT / "src-tauri/resources/plugin-sdk"
        self.assertEqual(resources[str(sdk.resolve())], "resources/plugin-sdk")
        for filename in ["pymss-sdk.js", "pymss-sdk.css"]:
            self.assertGreater((sdk / filename).stat().st_size, 0)

    def test_macos_release_checks_both_sdk_files_in_the_finished_bundle(self):
        workflow = (ROOT / ".github/workflows/release-macos.yml").read_text(encoding="utf-8")
        verification = workflow.split("- name: Verify bundled macOS resources", 1)[1].split("- name:", 1)[0]
        for filename in ["pymss-sdk.js", "pymss-sdk.css"]:
            self.assertIn(f'test -s "$RES/resources/plugin-sdk/{filename}"', verification)

    def test_linux_release_maps_the_sdk_and_checks_the_finished_appimage(self):
        workflow = (ROOT / ".github/workflows/release-linux.yml").read_text(encoding="utf-8")
        step = workflow.split("- name: Build AppImage", 1)[1].split("- name:", 1)[0]
        generator = step.split("<<'PY'", 1)[1].split("\n", 1)[1].split("\n          PY", 1)[0]
        generator = "\n".join(line[10:] if line.startswith(" " * 10) else line for line in generator.splitlines())
        with tempfile.TemporaryDirectory() as directory, contextlib.chdir(directory):
            Path("src-tauri").mkdir()
            exec(compile(generator, "linux-resource-config", "exec"), {})
            config = json.loads(Path("src-tauri/tauri.bundle.resources.json").read_text(encoding="utf-8"))
            resources = config["bundle"]["resources"]
            self.assertEqual(resources[str(Path("src-tauri/resources/plugin-sdk").resolve())], "resources/plugin-sdk")
        verification = (ROOT / "scripts/verify-linux-appimage.sh").read_text(encoding="utf-8")
        for filename in ["pymss-sdk.js", "pymss-sdk.css"]:
            self.assertIn(f'test -s "$RESOURCES/resources/plugin-sdk/{filename}"', verification)

    def test_every_windows_portable_variant_stages_the_sdk(self):
        workflow = (ROOT / ".github/workflows/release-windows.yml").read_text(encoding="utf-8")
        stages = re.findall(r"(?ms)^      - name: Stage[^\n]*\n(.*?)(?=^      - |\Z)", workflow)
        self.assertGreaterEqual(len(stages), 4)
        for stage in stages:
            self.assertIn("./scripts/stage-plugin-sdk.ps1 -StagePath $stage", stage)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell staging")
    def test_windows_sdk_staging_copies_the_actual_files(self):
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory) / "Portable app"
            result = subprocess.run([
                powershell, "-NoProfile", "-NonInteractive", "-File", str(ROOT / "scripts/stage-plugin-sdk.ps1"),
                "-StagePath", str(stage),
            ], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            for filename in ["pymss-sdk.js", "pymss-sdk.css"]:
                source = ROOT / "src-tauri/resources/plugin-sdk" / filename
                self.assertEqual((stage / "python/plugin-sdk" / filename).read_bytes(), source.read_bytes())


if __name__ == "__main__":
    unittest.main()
