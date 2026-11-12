from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]


class RuntimeMirrorPublicationTests(unittest.TestCase):
    def setUp(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
        self.preflight = workflow["jobs"]["preflight"]["steps"]
        self.steps = workflow["jobs"]["github-release"]["steps"]

    def step(self, name):
        return next(step for step in self.steps if step.get("name") == name)

    def run_bash(self, script, directory, **extra_env):
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("Bash is required")
        return subprocess.run([bash, "-c", script], cwd=directory,
                              env={**os.environ, "RELEASE_VERSION": "1.2.3", **extra_env},
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)

    def test_runtime_payloads_are_separate_from_the_github_attachment_directory(self):
        runtime = self.step("Download Windows ROCm runtime assets")
        self.assertEqual(runtime["with"]["path"], "runtime-artifacts")
        publication = next(step for step in self.steps if step.get("id") == "publish_release")
        self.assertEqual(publication["with"]["files"], "artifacts/*")
        for step in self.steps:
            if step.get("uses", "").startswith("actions/download-artifact") and step is not runtime:
                self.assertEqual(step["with"]["path"], "artifacts")

    def test_both_mirrors_publish_before_the_release_and_after_tag_verification(self):
        verify = self.steps.index(self.step("Verify release tag commit"))
        hf = self.steps.index(self.step("Upload release files to HuggingFace dataset"))
        ms = self.steps.index(self.step("Upload release files to ModelScope dataset"))
        publication = next(index for index, step in enumerate(self.steps) if step.get("id") == "publish_release")
        check = self.steps.index(self.step("Verify both runtime mirrors before publication"))
        self.assertLess(verify, hf)
        self.assertLess(verify, ms)
        self.assertLess(hf, publication)
        self.assertLess(ms, publication)
        self.assertLess(check, publication)
        self.assertIn("--verify-only", self.steps[check]["run"])
        setup = next(step for step in self.steps if step.get("id") == "setup_mirror_uploads")
        self.assertIn("steps.verify_release_tag.outcome == 'success'", setup["if"])

    def test_windows_publication_requires_both_tokens_without_logging_them(self):
        guard = next(step for step in self.preflight if step.get("name") == "Require both runtime mirror credentials")
        self.assertIn("steps.plan.outputs.publish == 'true'", guard["if"])
        self.assertIn("steps.plan.outputs.target == 'windows'", guard["if"])
        self.assertIn("steps.plan.outputs.target == 'all'", guard["if"])
        with tempfile.TemporaryDirectory() as directory:
            for hf, ms, expected in (("", "", False), ("private-hf", "", False),
                                     ("", "private-ms", False), ("private-hf", "private-ms", True)):
                with self.subTest(hf=bool(hf), ms=bool(ms)):
                    result = self.run_bash(guard["run"], directory, HF_TOKEN=hf, MODELSCOPE_API_TOKEN=ms)
                    self.assertEqual(result.returncode == 0, expected, result.stderr)
                    self.assertNotIn("private-hf", result.stdout + result.stderr)
                    self.assertNotIn("private-ms", result.stdout + result.stderr)

    def test_mirror_staging_includes_wheels_without_adding_them_to_github_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "artifacts").mkdir()
            (root / "runtime-artifacts").mkdir()
            (root / "artifacts/studio setup.exe").write_bytes(b"installer")
            (root / "runtime-artifacts/runtime.whl").write_bytes(b"runtime")
            result = self.run_bash(self.step("Stage artifacts for mirror uploads")["run"], root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "pms/1.2.3/studio setup.exe").read_bytes(), b"installer")
            self.assertFalse((root / "pms/1.2.3/runtime.whl").exists())
            self.assertEqual([path.name for path in (root / "artifacts").iterdir()], ["studio setup.exe"])

    def test_runtime_locks_are_versioned_separately_from_app_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder, body in (("artifacts", b"first"), ("runtime-artifacts", b"second")):
                (root / folder).mkdir()
                (root / folder / "catalog.json").write_bytes(body)
            result = self.run_bash(self.step("Stage artifacts for mirror uploads")["run"], root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "pms/1.2.3/catalog.json").read_bytes(), b"first")
            self.assertEqual((root / "pms/1.2.3/runtime-locks/catalog.json").read_bytes(), b"second")

    def test_runtime_wheels_in_the_app_attachment_directory_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "artifacts").mkdir()
            (root / "artifacts/runtime.whl").write_bytes(b"runtime")
            result = self.run_bash(self.step("Stage artifacts for mirror uploads")["run"], root)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "pms/1.2.3/runtime.whl").exists())

    def test_legacy_release_is_preserved_but_new_versions_can_publish(self):
        guard = next(step for step in self.preflight if step.get("name") == "Preserve the published legacy runtime links")
        with tempfile.TemporaryDirectory() as directory:
            self.assertNotEqual(self.run_bash(guard["run"], directory, RELEASE_VERSION="0.1.0").returncode, 0)
            self.assertEqual(self.run_bash(guard["run"], directory, RELEASE_VERSION="0.1.1").returncode, 0)

    def test_both_runtime_recipes_use_modelscope_as_the_published_download_root(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/release-windows.yml").read_text(encoding="utf-8"))
        steps = workflow["jobs"]["windows-rocm-runtime-assets"]["steps"]
        resolver = next(step["run"] for step in steps if step.get("name") == "Resolve complete binary closure")
        roots = [line for line in resolver.splitlines() if "'--base-url'" in line]
        self.assertEqual(len(roots), 2)
        for line in roots:
            self.assertIn("https://www.modelscope.cn/datasets/huanlinoto/Pymss-Studio-Dist/resolve/master/wheels", line)
            self.assertNotIn("github.com", line)
