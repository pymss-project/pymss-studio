from __future__ import annotations

from pathlib import Path
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
