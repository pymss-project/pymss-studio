from __future__ import annotations

from pathlib import Path
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
VARIANTS = ("cuda", "cpu", "online", "rocm")


class WindowsReleaseTests(unittest.TestCase):
    def setUp(self):
        self.windows = yaml.safe_load(
            (ROOT / ".github/workflows/release-windows.yml").read_text(encoding="utf-8")
        )["jobs"]

    def test_release_preflight_keeps_all_platforms_and_distributes_one_core_snapshot(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
        triggers = workflow.get("on", workflow.get(True))
        self.assertEqual(set(triggers["workflow_dispatch"]["inputs"]),
                         {"version", "target", "release_type", "allow_auto_update"})
        self.assertNotIn("default", triggers["workflow_dispatch"]["inputs"]["version"])
        jobs = workflow["jobs"]
        preflight = jobs["preflight"]
        upload = next(step for step in preflight["steps"]
                      if step.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertEqual(upload["with"]["path"], "python/runtime-manifest.json")
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        plan = next(step for step in preflight["steps"] if step.get("id") == "plan")
        self.assertEqual(plan["env"]["RELEASE_COMMIT_INPUT"], "${{ github.sha }}")
        for platform in ("windows", "macos", "linux"):
            with self.subTest(platform=platform):
                job = jobs[platform]
                self.assertEqual(job["needs"], "preflight")
                self.assertIn("needs.preflight.outputs.target == 'all'", job["if"])
                self.assertIn(f"needs.preflight.outputs.target == '{platform}'", job["if"])
                self.assertEqual(job["with"]["core_manifest_artifact"], upload["with"]["name"])
                child = yaml.safe_load((ROOT / ".github/workflows" / f"release-{platform}.yml").read_text(encoding="utf-8"))
                child_job = (child["jobs"]["windows-rocm-runtime-assets"] if platform == "windows"
                             else next(iter(child["jobs"].values())))
                download = next(i for i, step in enumerate(child_job["steps"])
                                if step.get("with", {}).get("name") == "${{ inputs.core_manifest_artifact }}")
                build = next(i for i, step in enumerate(child_job["steps"])
                             if any(marker in step.get("run", "") for marker in
                                    ("prepare-python-runtime", "runtime-artifacts.py', 'snapshot'")))
                self.assertLess(download, build)
        for variant in ("cpu", "cuda", "rocm"):
            verify = next(step for step in self.windows[f"windows-{variant}-prepare"]["steps"]
                          if step.get("name", "").startswith("Verify prepared"))
            self.assertIn(f"runtime-envs/{variant}/Scripts/python.exe", verify["run"])
            self.assertIn("scripts/resolve-release-core.py python/runtime-manifest.json --verify", verify["run"])
        publisher = jobs["github-release"]
        self.assertIn("preflight", publisher["needs"])
        self.assertIn("needs.preflight.outputs.publish == 'true'", publisher["if"])
        self.assertIn("needs.preflight.result == 'success'", publisher["if"])
        publish = next(step for step in publisher["steps"] if step.get("id") == "publish_release")
        self.assertEqual(publish["with"]["target_commitish"], "${{ github.sha }}")
        verify = next(step for step in publisher["steps"] if step.get("name") == "Verify release tag commit")
        self.assertIn('--verify-tag "$RELEASE_TAG" "$GITHUB_SHA"', verify["run"])
        self.assertLess(publisher["steps"].index(verify), publisher["steps"].index(publish))

    def test_rocm_candidate_uses_a_separate_prerelease_entry(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/release-rocm-preview.yml").read_text(encoding="utf-8"))
        job = workflow["jobs"]["release"]
        self.assertEqual(job["uses"], "./.github/workflows/release.yml")
        self.assertEqual(job["with"]["release_type"], "prerelease")
        self.assertEqual(job["with"]["target"], "windows")
        self.assertTrue(job["with"]["rocm_preview_candidate"])
        self.assertNotIn("allow_auto_update", job["with"])

    @unittest.skipUnless(shutil.which("bash"), "Bash is required for the release metadata check")
    def test_release_policy_reaches_application_update_metadata_for_every_platform(self):
        modules = {}
        for name, filename in (("plan", "resolve-release-plan.py"), ("metadata", "generate-update-metadata.py")):
            spec = importlib.util.spec_from_file_location(f"release_update_{name}", ROOT / "scripts" / filename)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            modules[name] = module
        manifest = json.loads((ROOT / "python/runtime-manifest.json").read_text(encoding="utf-8"))
        publisher = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))["jobs"]["github-release"]
        prepare = next(step for step in publisher["steps"] if step.get("id") == "release_meta")["run"]
        for version, mode, automatic in (("0.1.0", "stable", True), ("0.1.0", "stable", False),
                                          ("0.1.0-rc.1", "prerelease", True)):
            with self.subTest(mode=mode, automatic=automatic), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                plan = modules["plan"].release_plan(manifest, version, release_type=mode,
                                                    allow_auto_update=automatic)
                environment = {}
                for name, template in publisher["env"].items():
                    if not name.startswith("RELEASE_"):
                        continue
                    output = re.fullmatch(r"\$\{\{ needs\.preflight\.outputs\.(\w+) \}\}", template)
                    self.assertIsNotNone(output, name)
                    value = plan[output[1]]
                    environment[name] = value if isinstance(value, str) else json.dumps(value)
                env_file, output_file = folder / "environment.txt", folder / "outputs.txt"
                result = subprocess.run([shutil.which("bash"), "-e", "-c", prepare],
                                        env={**os.environ, **environment, "GITHUB_ENV": env_file.as_posix(),
                                             "GITHUB_OUTPUT": output_file.as_posix()},
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                environment.update(line.split("=", 1) for line in env_file.read_text(encoding="utf-8").splitlines())
                for slug, suffix in (("windows-x64-update", "zip"), ("macos-arm64-update", "app.tar.gz"),
                                     ("linux-x64", "AppImage")):
                    asset = folder / f"Pymss-Studio-{version}-{slug}.{suffix}"
                    asset.write_bytes(b"archive")
                    asset.with_name(asset.name + ".sig").write_text("signature", encoding="utf-8")
                    if slug.startswith("macos"):
                        asset.with_name(asset.name + ".json").write_text(json.dumps({
                            "version": version, "pymss_unpacked_size": 1,
                            "pymss_runtime_contract": {"schemaVersion": 1},
                        }), encoding="utf-8")
                files = modules["metadata"].generate(folder, environment)
                self.assertEqual(len(files), 3 if mode == "prerelease" else 6)
                for name in files:
                    metadata = json.loads((folder / name).read_text(encoding="utf-8"))
                    self.assertEqual(metadata["version"], version)
                    self.assertEqual(metadata["pymss_update_supported"], automatic)
                    self.assertEqual(metadata["pymss_requires_manual_install"], not automatic)
                    if mode == "prerelease":
                        self.assertEqual(metadata["channel"], "prerelease")

    def test_every_package_downloads_the_same_tools_before_staging(self):
        producer = self.windows["windows-bundled-tools"]
        self.assertNotIn("needs", producer)
        upload = next(step for step in producer["steps"] if step.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertEqual(upload["with"]["path"], "release/bin")
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        for variant in VARIANTS:
            with self.subTest(variant=variant):
                job = self.windows[f"windows-{variant}-prepare"]
                self.assertEqual(set(job["needs"]), {"windows-rocm-runtime-assets", "windows-bundled-tools"})
                downloads = [i for i, step in enumerate(job["steps"])
                             if step.get("uses", "").startswith("actions/download-artifact@")
                             and step.get("with", {}).get("name") == upload["with"]["name"]]
                self.assertEqual(len(downloads), 1)
                self.assertEqual(job["steps"][downloads[0]]["with"]["path"], "release/bin")
                stage = next(i for i, step in enumerate(job["steps"]) if 'Copy-Item "release/bin" "$stage/bin"' in step.get("run", ""))
                self.assertLess(downloads[0], stage)
                runs = "\n".join(step.get("run", "") for step in job["steps"])
                self.assertNotIn("ffmpeg-release-essentials.zip", runs)
                self.assertNotIn("releases/download/release-1.37.0", runs)
                self.assertNotIn("aka.ms/vs/17", runs)
                self.assertIn("scripts/verify-native-plugins.py", runs)
                for suffix in ("7z", "exe"):
                    self.assertEqual(self.windows[f"windows-{variant}-{suffix}"]["needs"], f"windows-{variant}-prepare")

    def test_windows_and_macos_release_jobs_have_bounded_timeouts(self):
        macos = yaml.safe_load(
            (ROOT / ".github/workflows/release-macos.yml").read_text(encoding="utf-8")
        )["jobs"]
        for name, job in {**self.windows, **macos}.items():
            with self.subTest(job=name):
                self.assertIsInstance(job.get("timeout-minutes"), int)
                self.assertGreater(job["timeout-minutes"], 0)
                self.assertLess(job["timeout-minutes"], 360)

    def test_tool_download_retries_fit_within_the_job_deadline(self):
        job = self.windows["windows-bundled-tools"]
        source = job["steps"][0]["run"]
        retries = int(re.search(r"--retry (\d+)", source)[1])
        seconds = int(re.search(r"--max-time (\d+)", source)[1])
        delay = int(re.search(r"--retry-delay (\d+)", source)[1])
        downloads = len(re.findall(r"^Get-BundledFile ", source, re.M))
        self.assertEqual(downloads, 3)
        budget = downloads * ((retries + 1) * seconds + retries * delay)
        self.assertLess(budget + 5 * 60, job["timeout-minutes"] * 60)

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell is required for the bundled download guard check")
    def test_download_guard_rejects_failed_or_empty_downloads(self):
        source = self.windows["windows-bundled-tools"]["steps"][0]["run"]
        harness = r'''
param([string]$Source, [string]$Mode)
$ErrorActionPreference = "Stop"
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
$download = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-BundledFile'
}, $true)
if (-not $download) { throw 'Download guard not found' }
Invoke-Expression $download.Extent.Text
function curl.exe {
    $destination = $args[[Array]::IndexOf($args, '--output') + 1]
    if ($Mode -eq 'failed') {
        [IO.File]::WriteAllText($destination, 'partial download')
        $global:LASTEXITCODE = 22
    } else {
        [IO.File]::WriteAllText($destination, $(if ($Mode -eq 'empty') { '' } else { 'downloaded bytes' }))
        $global:LASTEXITCODE = 0
    }
}
Get-BundledFile 'https://example.invalid/archive' (Join-Path $PSScriptRoot 'tool archive.zip')
'''
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            production = folder / "tools.ps1"
            production.write_text(source, encoding="utf-8")
            runner = folder / "guard.ps1"
            runner.write_text(harness, encoding="utf-8")
            for mode, expected in [("valid", None), ("failed", "Bundled tool download failed"), ("empty", "Bundled tool download is empty")]:
                with self.subTest(mode=mode):
                    result = subprocess.run(
                        [shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-File", str(runner), str(production), mode],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                    )
                    if expected is None:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual((folder / "tool archive.zip").read_text(), "downloaded bytes")
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(expected, result.stderr)
