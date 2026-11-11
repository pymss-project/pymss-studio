from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PREPARE = ROOT / "scripts/prepare-python-runtime.ps1"


class RocmRuntimePackagingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.powershell = shutil.which("powershell") or shutil.which("pwsh")
        if self.powershell is None:
            self.skipTest("PowerShell is required")

    def _function(self, name):
        script = PREPARE.read_text(encoding="utf-8")
        return re.search(r"^function " + name + r" \{\n.*?^\}", script, re.M | re.S).group()

    def _powershell(self, body, *functions):
        script = self.root / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            + "\n".join(self._function(name) for name in ("Invoke-NativeChecked", *functions))
            + "\n$root = '" + str(ROOT).replace("'", "''") + "'\n" + body,
            encoding="utf-8-sig",
        )
        return subprocess.run([self.powershell, "-NoProfile", "-File", str(script)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")

    def test_preparation_legacy_plan_uses_the_existing_profile_resolver(self):
        result = self._powershell(
            f"$Python = '{sys.executable}'\n"
            "$manifestPath = Join-Path $root 'python/runtime-manifest.json'\n"
            "$RocmProfile = 'win-rocm721'\n$RocmTargets = @()\n"
            "Get-RocmInstallPlan | ConvertTo-Json -Depth 20\n", "Get-RocmInstallPlan",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["profileId"], "win-rocm721")
        self.assertEqual(plan["packages"]["torchaudio"], "2.9.1+rocm7.2.1")
        self.assertEqual(len(plan["torch"]["rocmRequirements"]), 4)
        # The legacy wheel stage disables dependency resolution, so the shipped
        # torchvision build needs its non-Torch image dependency explicitly.
        self.assertTrue(plan["torch"]["noDeps"])
        self.assertIn("Pillow>=10.4,<13", plan["torch"]["requirements"])

    def test_disabled_policy_and_missing_lock_are_rejected_before_touching_runtime(self):
        runtime = self.root / "runtime"
        runtime.mkdir()
        marker = runtime / "preserved.txt"
        marker.write_text("existing runtime", encoding="utf-8")
        fixture = self.root / "source"
        (fixture / "scripts").mkdir(parents=True)
        (fixture / "python").mkdir()
        script = fixture / "scripts/prepare-python-runtime.ps1"
        shutil.copyfile(PREPARE, script)
        shutil.copyfile(ROOT / "python/worker_runtime_profiles.py", fixture / "python/worker_runtime_profiles.py")
        manifest = json.loads((ROOT / "python/runtime-manifest.json").read_text(encoding="utf-8"))
        profile = manifest["backends"]["rocm"]["platformOverrides"]["win32"]["profiles"]["win-rocm100-multiarch"]
        for state, enabled, message in (("disabled", False, "not enabled for installation or packaging"),
                                        ("preview", True, "requires a resolved binary artifact lock")):
            with self.subTest(state=state):
                profile.update(releaseState=state, installationEnabled=enabled)
                (fixture / "python/runtime-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                result = subprocess.run(
                    [self.powershell, "-NoProfile", "-File", str(script), "-Python", sys.executable,
                     "-Variant", "rocm", "-RuntimeDir", str(runtime), "-RocmProfile", "win-rocm100-multiarch",
                     "-RocmTargets", "gfx1031"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertEqual(marker.read_text(encoding="utf-8"), "existing runtime")

    def test_runtime_state_probe_preserves_json_quotes_in_windows_powershell(self):
        source = PREPARE.read_text(encoding="utf-8")
        start = source.index("        $manifestPackageJson =")
        end = source.index("\n    } finally {", start)
        probe = source[start:end].replace(
            "import base64,", "import sys; sys.modules['torch'] = None\nimport base64,",
        )
        result = self._powershell(
            f"$envPython = '{sys.executable}'\n"
            "$manifestPackageNames = @('json', 'pathlib')\n" + probe + "\n$probeOutput\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(result.stdout)
        self.assertTrue(state["packages"]["json"])
        self.assertTrue(state["packages"]["pathlib"])

    def _wheel(self, name, version, requires=()):
        package = name.replace("-", "_")
        info = f"{package}-{version}.dist-info"
        target = self.root / f"{package}-{version}-py3-none-any.whl"
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr(f"{package}/__init__.py", "")
            archive.writestr(f"{info}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                             + "".join(f"Requires-Dist: {requirement}\n" for requirement in requires))
            archive.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            archive.writestr(f"{info}/RECORD", "")
        return target

    def _source_item(self, name, version="1.0"):
        wheel = self.root / f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
        return {"metadata": {"name": name, "version": version}, "download_info": {
            "url": f"https://sample-user:sample-password@repo.radeon.com/{wheel.name}?sample-query=sample-value",
            "archive_info": {"hashes": {"sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}},
        }}

    def _snapshot_environment(self):
        environment = self.root / "runtime with spaces"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        for name in ("torch", "rocm-sdk-core", "rocm-sdk-device-gfx1031"):
            self._wheel(name, "1.0")
        self._wheel("torch", "2.0")
        self._wheel("rocm-sdk-device-gfx1031", "2.0")
        self._wheel("commonpkg", "1.0", ("torch>=2",))
        self._wheel("kerneluser", "1.0", ("rocm-sdk-device-gfx1031>=2",))
        subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--find-links", str(self.root),
                        "torch==1.0", "rocm-sdk-core==1.0", "rocm-sdk-device-gfx1031==1.0"],
                       check=True, capture_output=True)
        profile = {"id": "fixture", "revision": 1, "platforms": [sys.platform], "python": "3.12",
                   "packages": {"torch": "1.0", "rocm-sdk-core": "1.0"},
                   "torch": {"rocmRequirements": ["https://repo.radeon.com/sdk.whl"],
                             "requirements": ["https://repo.radeon.com/torch.whl"]},
                   "installationEnabled": True, "installMode": "legacy"}
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"backends": {"rocm": {"platforms": [sys.platform], "defaultProfile": "fixture",
                                                             "profiles": {"fixture": profile}}}}), encoding="utf-8")
        plan = {"profileId": "fixture", "targets": [], "packages": profile["packages"], "torch": profile["torch"]}
        plan_file = self.root / "plan.json"
        plan_file.write_text(json.dumps(plan), encoding="utf-8")
        for stage, names in (("sdk", ("rocm-sdk-core", "rocm-sdk-device-gfx1031")), ("torch", ("torch",))):
            report = {"version": "1", "install": [self._source_item(name) for name in names]}
            (environment / f"pymss-install-{stage}.json").write_text(json.dumps(report), encoding="utf-8")
        body = (f"$manifestPath = '{manifest}'\n$manifestBackend = 'rocm'\n"
                f"$PythonPath = '{python}'\n$EnvironmentDir = '{environment}'\n"
                f"$rocmPlan = Get-Content -Raw '{plan_file}' | ConvertFrom-Json\n")
        return environment, python, body

    def test_real_stage_snapshot_freezes_transitive_sdk_packages_for_later_pip_installs(self):
        environment, python, body = self._snapshot_environment()
        body += ("Save-AcceleratorSnapshot -PythonPath $PythonPath -EnvironmentDir $EnvironmentDir | Out-Null\n"
                 "Assert-AcceleratorSnapshot -PythonPath $PythonPath -EnvironmentDir $EnvironmentDir\n")
        result = self._powershell(body, "Save-AcceleratorSnapshot", "Assert-AcceleratorSnapshot")
        self.assertEqual(result.returncode, 0, result.stderr)
        constraints = environment / "pymss-accelerator-constraints.txt"
        self.assertIn("rocm-sdk-device-gfx1031==1.0", constraints.read_text(encoding="utf-8"))
        snapshot = json.loads((environment / "pymss-accelerator-state.json").read_text(encoding="utf-8"))
        self.assertEqual(len(snapshot["baseFingerprint"]), 64)
        self.assertEqual(set(snapshot["acceleratorPackageSources"]), {"torch", "rocm-sdk-core", "rocm-sdk-device-gfx1031"})
        self.assertEqual(snapshot["sources"], snapshot["acceleratorPackageSources"])
        for path in (environment / "pymss-accelerator-state.json", environment / "pymss-install-sdk.json", environment / "pymss-install-torch.json"):
            persisted = path.read_text(encoding="utf-8")
            self.assertNotIn("sample-password", persisted)
            self.assertNotIn("sample-query", persisted)
        for requirement in ("commonpkg", "kerneluser"):
            with self.subTest(requirement=requirement):
                installed = subprocess.run(
                    [str(python), "-m", "pip", "install", "--no-index", "--find-links", str(self.root),
                     "--constraint", str(constraints), requirement], capture_output=True, text=True,
                )
                self.assertNotEqual(installed.returncode, 0)
                self.assertIn("ResolutionImpossible", installed.stderr)
        result = self._powershell(body.replace("Save-AcceleratorSnapshot -PythonPath $PythonPath -EnvironmentDir $EnvironmentDir | Out-Null\n", ""),
                                  "Assert-AcceleratorSnapshot")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_base_snapshot_rejects_unofficial_sources_missing_hashes_and_invalid_reports(self):
        environment, _python, body = self._snapshot_environment()
        body += "Save-AcceleratorSnapshot -PythonPath $PythonPath -EnvironmentDir $EnvironmentDir | Out-Null\n"
        report_path = environment / "pymss-install-torch.json"
        valid = json.loads(report_path.read_text(encoding="utf-8"))
        cases = []
        for url in ("https://other.example/torch.whl", "http://repo.radeon.com/torch.whl"):
            report = copy.deepcopy(valid)
            report["install"][0]["download_info"]["url"] = url
            cases.append(report)
        report = copy.deepcopy(valid)
        report["install"][0]["download_info"]["archive_info"] = {"hashes": {}}
        cases.extend([report, {"version": "2", "install": valid["install"]}, {"version": "1", "install": []}])
        for report in cases:
            with self.subTest(report=report):
                report_path.write_text(json.dumps(report), encoding="utf-8")
                result = self._powershell(body, "Save-AcceleratorSnapshot")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((environment / "pymss-accelerator-state.json").exists())
                self.assertTrue("official source and SHA-256" in result.stderr or "invalid installation report" in result.stderr)

    def test_later_stage_report_rejects_same_version_accelerator_reinstallation(self):
        environment, _python, body = self._snapshot_environment()
        body += "Save-AcceleratorSnapshot -PythonPath $PythonPath -EnvironmentDir $EnvironmentDir | Out-Null\n"
        for stage in ("common", "extras", "pymss"):
            with self.subTest(stage=stage):
                invocation = body + (
                    f"Invoke-RuntimeDependencyInstall -PythonPath $PythonPath -EnvironmentDir $EnvironmentDir -Stage '{stage}' "
                    f"-Arguments @('--no-index', '--find-links', '{self.root}', '--force-reinstall', "
                    "'--constraint', (Join-Path $EnvironmentDir 'pymss-accelerator-constraints.txt'), 'torch==1.0')\n"
                )
                result = self._powershell(invocation, "Save-AcceleratorSnapshot", "Assert-AcceleratorSnapshot",
                                          "Assert-DependencyInstallReport", "Invoke-RuntimeDependencyInstall")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("attempted to reinstall base accelerator packages: torch", result.stderr)
                self.assertFalse((environment / f"pymss-install-{stage}.json").exists())

    def test_later_dependency_report_accepts_packages_and_persists_only_sanitized_sources(self):
        environment, _python, body = self._snapshot_environment()
        body += "Save-AcceleratorSnapshot -PythonPath $PythonPath -EnvironmentDir $EnvironmentDir | Out-Null\n"
        report = self.root / "dependency-report.json"
        item = self._source_item("torch")
        item["metadata"] = {"name": "commonpkg", "version": "1.0", "requires_dist": ["dependency>=1"]}
        item["requested"] = True
        original = {"version": "1", "install": [item], "environment": {"python_version": "3.12"}}
        report.write_text(json.dumps(original), encoding="utf-8")
        body += ("Assert-DependencyInstallReport -PythonPath $PythonPath -EnvironmentDir $EnvironmentDir "
                 f"-Stage 'common' -ReportPath '{report}'\n")
        result = self._powershell(body, "Save-AcceleratorSnapshot", "Assert-DependencyInstallReport")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = (environment / "pymss-install-common.json").read_text(encoding="utf-8")
        self.assertNotIn("sample-password", output)
        self.assertNotIn("sample-query", output)
        persisted = json.loads(output)
        expected = copy.deepcopy(original)
        expected["install"][0]["download_info"]["url"] = "https://repo.radeon.com/torch-1.0-py3-none-any.whl"
        self.assertEqual(persisted, expected)


if __name__ == "__main__":
    unittest.main()
