from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/resolve-release-plan.py"
SPEC = importlib.util.spec_from_file_location("release_plan", SCRIPT)
PLAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLAN)
MANIFEST = json.loads((ROOT / "python/runtime-manifest.json").read_text(encoding="utf-8"))


class ReleasePlanTests(unittest.TestCase):
    def test_existing_lightweight_and_annotated_tags_must_match_the_build_commit(self):
        commit = "1" * 40
        other = "2" * 40
        for output, valid in (("", True), (f"{commit}\trefs/tags/v0.1.0\n", True),
                              (f"{other}\trefs/tags/v0.1.0\n", False),
                              (f"{other}\trefs/tags/v0.1.0\n{commit}\trefs/tags/v0.1.0^{{}}\n", True),
                              (f"{commit}\trefs/tags/v0.1.0\n{other}\trefs/tags/v0.1.0^{{}}\n", False)):
            with self.subTest(output=output), mock.patch.object(
                PLAN.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output),
            ):
                if valid:
                    PLAN.verify_release_tag("v0.1.0", commit)
                else:
                    with self.assertRaisesRegex(ValueError, "different commit"):
                        PLAN.verify_release_tag("v0.1.0", commit)
        with mock.patch.object(PLAN.subprocess, "run", side_effect=subprocess.CalledProcessError(128, ["git"])):
            with self.assertRaises(subprocess.CalledProcessError):
                PLAN.verify_release_tag("v0.1.0", commit)

    def test_stable_release_uses_manifest_recipe_and_normalizes_the_tag(self):
        plan = PLAN.release_plan(MANIFEST, "v0.1.0", release_type="stable")
        self.assertEqual((plan["version"], plan["tag"]), ("0.1.0", "v0.1.0"))
        self.assertTrue(plan["publish"])
        self.assertFalse(plan["prerelease"])
        self.assertFalse(plan["allow_auto_update"])
        profile = PLAN.resolve_profile(MANIFEST, "rocm", platform_name="win32")
        self.assertEqual(plan["rocm_profile"], profile["id"])
        self.assertEqual(plan["rocm_revision"], profile["revision"])
        self.assertEqual(plan["rocm_channel"], PLAN.profile_release_state(profile))
        self.assertEqual(plan["rocm_targets"], "all")

    def test_legacy_default_derives_empty_targets_and_the_current_revision(self):
        manifest = copy.deepcopy(MANIFEST)
        windows = manifest["backends"]["rocm"]["platformOverrides"]["win32"]
        windows["defaultProfile"] = "win-rocm721"
        windows["profiles"]["win-rocm721"]["revision"] = 7
        plan = PLAN.release_plan(manifest, "0.1.0", release_type="build-only")
        self.assertEqual(plan["rocm_profile"], "win-rocm721")
        self.assertEqual(plan["rocm_revision"], 7)
        self.assertEqual(plan["rocm_channel"], "legacy")
        self.assertEqual(plan["rocm_targets"], "")
        self.assertFalse(plan["publish"])

    def test_tag_push_infers_release_type_without_enabling_automatic_updates(self):
        for version, prerelease in (("v0.1.0", False), ("v0.1.0-rc.1+build.2", True)):
            with self.subTest(version=version):
                plan = PLAN.release_plan(MANIFEST, version)
                self.assertTrue(plan["publish"])
                self.assertEqual(plan["prerelease"], prerelease)
                self.assertFalse(plan["allow_auto_update"])

    def test_prerelease_automatic_updates_remain_an_explicit_policy(self):
        plan = PLAN.release_plan(MANIFEST, "0.1.0-rc.1", release_type="prerelease",
                                 allow_auto_update=True)
        self.assertTrue(plan["prerelease"])
        self.assertTrue(plan["allow_auto_update"])

    def test_invalid_versions_and_configuration_fail_before_building(self):
        for version in ("", "0.1", "v", "0.01.0", "0.1.0-01", "0.1.0-", "0.1.0+x..y", "0.1.0\nx=1"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                PLAN.release_plan(MANIFEST, version)
        for options in ({"target": "other"}, {"release_type": "other"},
                        {"release_type": "stable"},
                        {"release_type": "build-only", "allow_auto_update": True},
                        {"rocm_profile": "unknown"},
                        {"target": "linux", "rocm_preview_candidate": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                PLAN.release_plan(MANIFEST, "0.1.0-rc.1", **options)

    def test_disabled_rocm_candidate_requires_explicit_prerelease_without_mutating_policy(self):
        manifest = copy.deepcopy(MANIFEST)
        profile = manifest["backends"]["rocm"]["platformOverrides"]["win32"]["profiles"]["win-rocm100-multiarch"]
        profile.update(installationEnabled=False, releaseState="disabled")
        original = copy.deepcopy(manifest)
        with self.assertRaisesRegex(ValueError, "not enabled"):
            PLAN.release_plan(manifest, "0.1.0")
        for version, release_type in (("0.1.0", "stable"), ("0.1.0", "prerelease"),
                                      ("0.1.0-rc.1", "build-only")):
            with self.subTest(version=version, release_type=release_type), self.assertRaises(ValueError):
                PLAN.release_plan(manifest, version, release_type=release_type, rocm_preview_candidate=True)
        plan = PLAN.release_plan(manifest, "0.1.0-rocm-preview.1", target="windows",
                                 release_type="prerelease", rocm_preview_candidate=True)
        self.assertEqual(plan["rocm_channel"], "preview")
        self.assertTrue(plan["rocm_preview_candidate"])
        self.assertEqual(manifest, original)
        self.assertEqual(PLAN.release_plan(manifest, "0.1.0", target="macos")["target"], "macos")

    def test_cli_emits_typed_workflow_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "outputs.txt"
            result = subprocess.run(
                [sys.executable, str(SCRIPT)], cwd=ROOT,
                env={**os.environ, "GITHUB_OUTPUT": str(output), "RELEASE_VERSION_INPUT": "v0.1.0",
                     "RELEASE_TARGET_INPUT": "all", "RELEASE_TYPE_INPUT": "stable",
                     "RELEASE_AUTO_UPDATE_INPUT": "false", "RELEASE_ROCM_PROFILE_INPUT": "",
                     "RELEASE_ROCM_PREVIEW_INPUT": "false", "RELEASE_COMMIT_INPUT": ""},
                capture_output=True, text=True, encoding="utf-8", timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
            self.assertEqual(values["publish"], "true")
            self.assertEqual(values["prerelease"], "false")
            self.assertEqual(values["version"], "0.1.0")
            self.assertEqual(values["rocm_targets"], "all")
