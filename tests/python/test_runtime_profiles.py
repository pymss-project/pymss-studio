from __future__ import annotations

import contextlib
import copy
import io
import importlib.machinery
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap  # noqa: F401

import worker
import worker_bootstrap as bootstrap
import worker_runtime_profiles as profiles


MANIFEST_PATH = Path(__file__).resolve().parents[2] / "python/runtime-manifest.json"


class RuntimeProfileTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def plan(self, profile_id=None, targets=None, **kwargs):
        return profiles.runtime_install_plan(
            self.manifest, "rocm", profile_id, targets, platform_name="win32",
            machine_name=kwargs.get("machine_name", "AMD64"),
            python_version=kwargs.get("python_version", "3.12.8"),
        )

    def test_default_is_installable_multiarch_preview_and_legacy_remains_available(self):
        legacy = self.plan("win-rocm721")
        preview = self.plan(targets=["gfx1031"])
        self.assertEqual(legacy["profileId"], "win-rocm721")
        self.assertTrue(legacy["installationEnabled"])
        self.assertTrue(legacy["torch"]["noDeps"])
        self.assertEqual(preview["profileId"], "win-rocm100-multiarch")
        self.assertTrue(preview["installationEnabled"])
        self.assertEqual(preview["releaseState"], "preview")
        self.assertTrue(preview["requiresPreviewAcceptance"])
        with self.assertRaises(ValueError):
            profiles.assert_profile_installable(preview)
        profiles.assert_profile_installable(preview, accept_preview=True)
        self.assertFalse(preview["torch"]["noDeps"])
        self.assertEqual(preview["resolutionStatus"], "unresolved")

    def test_release_states_have_consistent_installation_gates(self):
        self.assertEqual(profiles.profile_release_state({"installationEnabled": True}), "legacy")
        self.assertEqual(profiles.profile_release_state({"installationEnabled": False}), "disabled")
        for state in ("legacy", "disabled", "preview", "stable", "revoked"):
            enabled = state in {"legacy", "preview", "stable"}
            value = {"releaseState": state, "installationEnabled": enabled}
            self.assertEqual(profiles.profile_release_state(value), state)
            with self.subTest(state=state), self.assertRaises(ValueError):
                profiles.profile_release_state({**value, "installationEnabled": not enabled})
        for state in (None, "experimental", [], 1):
            with self.subTest(state=state), self.assertRaises(ValueError):
                profiles.profile_release_state({"releaseState": state})

    def test_multiarch_pair_is_explicit_and_targets_are_deduplicated(self):
        plan = self.plan("win-rocm100-multiarch", ["gfx1101", "gfx1031", "gfx1101"])
        self.assertEqual(plan["targets"], ["gfx1031", "gfx1101"])
        self.assertEqual(plan["validatedTargets"], [])
        self.assertEqual(plan["torch"]["requirements"], [
            "torch[device-gfx1031,device-gfx1101]==2.13.0+rocm10.0.0",
            "torchvision[device-gfx1031,device-gfx1101]==0.28.0+rocm10.0.0",
            "torchaudio==2.11.0.2+rocm10.0.0", "rocm==10.0.0",
        ])
        self.assertIn("torchaudio==2.11.0.2+rocm10.0.0\n", plan["constraints"])
        self.assertEqual(plan["torch"]["indexUrl"], "https://stable.repo.amd.com/rocm/whl-next/")

    def test_invalid_profile_platform_abi_and_targets_are_rejected(self):
        for profile_id, targets in [
            ("unknown", []), ("../rocm", []), ([], []),
            ("win-rocm100-multiarch", []), ("win-rocm100-multiarch", "gfx1031"),
            ("win-rocm100-multiarch", ["gfx9999"]),
            ("win-rocm100-multiarch", ["gfx1031 --extra-index-url=https://example.com"]),
            ("win-rocm100-multiarch", ["all", "gfx1031"]),
            ("win-rocm100-multiarch", [None]), (None, []),
        ]:
            with self.subTest(profile_id=profile_id, targets=targets), self.assertRaises(ValueError):
                self.plan(profile_id, targets)
        for kwargs in [{"machine_name": "ARM64"}, {"python_version": "3.13.0"}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.plan(**kwargs)
        with self.assertRaises(ValueError):
            profiles.resolve_profile(self.manifest, "rocm", "win-rocm721", platform_name="linux")

    def test_linux_keeps_its_own_torch_configuration(self):
        plan = profiles.runtime_install_plan(self.manifest, "rocm", platform_name="linux")
        self.assertIsNone(plan["profileId"])
        self.assertEqual(plan["torch"], {
            "requirement": "torch==2.10.0", "indexUrl": "https://download.pytorch.org/whl/rocm7.1",
        })

    def test_profile_matching_uses_actual_build_and_does_not_assume_legacy(self):
        for version, expected in [("2.9.1+rocm7.2.1", "win-rocm721"),
                                  ("2.13.0+rocm10.0.0", "win-rocm100-multiarch")]:
            profile = profiles.resolve_profile_for_runtime(self.manifest, "rocm", version, platform_name="win32")
            self.assertEqual(profile["id"], expected)
        for version in ["2.9.1", "2.13.0+cpu", "2.13.0+rocm10.1.0", "invalid"]:
            self.assertIsNone(profiles.resolve_profile_for_runtime(self.manifest, "rocm", version, platform_name="win32"))
        with self.assertRaises(ValueError):
            profiles.resolve_profile_for_runtime(
                self.manifest, "rocm", "2.13.0+rocm10.0.0", profile_id="win-rocm721", platform_name="win32",
            )

    def test_read_only_worker_command_does_not_repair_or_download(self):
        events = []
        with mock.patch.object(bootstrap, "_manifest", return_value=self.manifest), \
             mock.patch.object(bootstrap.sys, "platform", "win32"), \
             mock.patch.object(bootstrap, "machine", return_value="AMD64"), \
             mock.patch.object(bootstrap.platform, "python_version", return_value="3.12.8"), \
             mock.patch.object(bootstrap, "_recover_runtime_transactions") as recover, \
             mock.patch.object(bootstrap.subprocess, "Popen") as popen, \
             mock.patch.object(bootstrap, "_emit", side_effect=lambda *args: events.append(args)):
            self.assertEqual(worker.run_command("runtime_install_plan", {
                "backend": "rocm", "profileId": "win-rocm100-multiarch", "targets": ["gfx1032"],
            }), 0)
        recover.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(events[0][0], "runtime_install_plan")
        self.assertEqual(events[0][1]["packages"]["torchaudio"], "2.11.0.2+rocm10.0.0")

    def test_rejected_install_leaves_old_environment_and_pointer_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / "rocm"
            env.mkdir()
            marker = env / "retained.bin"
            marker.write_bytes(b"retained")
            state = env / "pymss-runtime-state.json"
            state.write_text(json.dumps({"torchVersion": "2.13.0+rocm10.0.0"}), encoding="utf-8")
            pointer = root / "active-runtime.json"
            pointer.write_bytes(b"original pointer")
            for payload in [
                {"profileId": "win-rocm100-multiarch", "targets": ["gfx1031"]},
                {"profileId": "unknown"}, {},
            ]:
                with self.subTest(payload=payload), \
                     mock.patch.object(bootstrap, "RUNTIME_ENVS_DIR", root), \
                     mock.patch.object(bootstrap, "ACTIVE_RUNTIME_FILE", pointer), \
                     mock.patch.object(bootstrap, "_manifest", return_value=self.manifest), \
                     mock.patch.object(bootstrap.sys, "platform", "win32"), \
                     mock.patch.object(bootstrap, "machine", return_value="AMD64"), \
                     mock.patch.object(bootstrap.platform, "python_version", return_value="3.12.8"), \
                     mock.patch.object(bootstrap, "_create_runtime_venv") as create, \
                     mock.patch.object(bootstrap.subprocess, "Popen") as popen, \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(bootstrap.cmd_install_runtime({"backend": "rocm", **payload}), 1)
                    create.assert_not_called()
                    popen.assert_not_called()
                    self.assertEqual(marker.read_bytes(), b"retained")
                    self.assertEqual(pointer.read_bytes(), b"original pointer")

    def test_health_validates_recorded_profile_instead_of_default(self):
        profile = profiles.resolve_profile(self.manifest, "rocm", "win-rocm100-multiarch", platform_name="win32")
        probe = {"profileId": profile["id"], "torchVersion": profile["packages"]["torch"],
                 "acceleratorPackages": profile["packages"], "packageVersions": {}}
        manifest = copy.deepcopy(self.manifest)
        manifest["common"] = {}
        with mock.patch.object(bootstrap.sys, "platform", "win32"):
            self.assertTrue(bootstrap._manifest_versions_are_satisfied(probe, manifest, "rocm")[0])
            probe["acceleratorPackages"] = {**profile["packages"], "torchaudio": "2.13.0"}
            valid, failures = bootstrap._manifest_versions_are_satisfied(probe, manifest, "rocm")
        self.assertFalse(valid)
        self.assertTrue(any("torchaudio" in failure for failure in failures))

    def test_missing_or_corrupt_cache_cannot_authorize_overwriting_unknown_rocm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / "rocm"
            python = env / "Scripts/python.exe"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"existing interpreter")
            for cached in [None, "{broken", json.dumps({"torchVersion": "2.9.1+rocm7.2.1"})]:
                for actual in ["2.13.0+rocm10.0.0", "2.14.0+rocm10.1.0", None]:
                    state = env / "pymss-runtime-state.json"
                    state.unlink(missing_ok=True)
                    if cached is not None:
                        state.write_text(cached)
                    with self.subTest(cached=cached, actual=actual), \
                         mock.patch.object(bootstrap, "RUNTIME_ENVS_DIR", root), \
                         mock.patch.object(bootstrap, "_manifest", return_value=self.manifest), \
                         mock.patch.object(bootstrap.sys, "platform", "win32"), \
                         mock.patch.object(bootstrap, "machine", return_value="AMD64"), \
                         mock.patch.object(bootstrap.platform, "python_version", return_value="3.12.8"), \
                         mock.patch.object(bootstrap, "_probe_python_package_versions", return_value={"torch": actual}), \
                         mock.patch.object(bootstrap, "_create_runtime_venv") as create, \
                         mock.patch.object(bootstrap.subprocess, "Popen") as popen, \
                         contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(bootstrap.cmd_install_runtime({"backend": "rocm"}), 1)
                        create.assert_not_called()
                        popen.assert_not_called()
                        self.assertEqual(python.read_bytes(), b"existing interpreter")


class AcceleratorIdentityTests(unittest.TestCase):
    def test_install_report_sources_do_not_persist_url_credentials_or_queries(self):
        report = {"version": "1", "install": [{"metadata": {"name": "ROCM_SDK_CORE", "version": "10.0.0"},
                   "download_info": {"url": "https://user:credential@stable.repo.amd.com/sdk.whl?signature=private#fragment",
                                     "archive_info": {"hashes": {"sha256": "a" * 64}}}}]}
        records = profiles.accelerator_source_records(report)
        self.assertEqual(records["rocm-sdk-core"]["url"], "https://stable.repo.amd.com/sdk.whl")
        self.assertEqual(records["rocm-sdk-core"]["hashes"]["sha256"], "a" * 64)
        with self.assertRaises(RuntimeError):
            profiles.accelerator_source_records({"install": []})

    def test_profile_source_verification_rejects_unofficial_hosts_and_missing_hashes(self):
        plan = {"packages": {"rocm": "10.0.0"}, "torch": {"indexUrl": "https://stable.repo.amd.com/rocm/whl-next/"}}
        good = {"version": "10.0.0", "url": "https://stable.repo.amd.com/rocm.whl", "hashes": {"sha256": "a" * 64}}
        profiles.verify_profile_accelerator_sources(plan, {"rocm": good})
        for changed in [{"url": "https://example.com/rocm.whl"}, {"url": "http://stable.repo.amd.com/rocm.whl"},
                        {"version": "10.1.0"}, {"hashes": {}}]:
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                profiles.verify_profile_accelerator_sources(plan, {"rocm": {**good, **changed}})

    def test_protects_canonical_names_without_blocking_all_amd_packages(self):
        for name in ["rocm", "ROCM_SDK_CORE", "amd_torch_device_gfx1031", "amd-torch-device-gfx110x",
                     "amd-torchvision-device-gfx1031", "torch", "nvidia-cublas-cu12", "mlx-metal"]:
            with self.subTest(name=name):
                self.assertTrue(profiles.is_base_accelerator_package(name))
        self.assertFalse(profiles.is_base_accelerator_package("amd-ordinary-tool"))
        self.assertTrue(profiles.is_base_accelerator_package("custom-kernel", ["Custom_Kernel"]))

    def test_constraints_preserve_exact_local_versions_and_reject_injection(self):
        self.assertEqual(profiles.accelerator_constraints({"torch": "2.13.0+rocm10.0.0", "rocm": "10.0.0"}),
                         "rocm==10.0.0\ntorch==2.13.0+rocm10.0.0\n")
        for value in ["10.0\n--extra-index-url=https://example.com", "10.0; python_version > '3'", "10.0.*"]:
            with self.subTest(value=value), self.assertRaises(Exception):
                profiles.accelerator_constraints({"rocm": value})

    def test_version_verification_detects_changes_removal_and_new_base_packages(self):
        expected = {"torch": "2.13.0+rocm10.0.0", "rocm": "10.0.0"}
        profiles.verify_accelerator_versions(expected, {**expected, "numpy": "2.0"})
        for actual in [{"torch": "2.13.0"}, {**expected, "rocm": "10.1.0"},
                       {**expected, "amd-torch-device-gfx1031": "2.13.0+rocm10.0.0"}]:
            with self.subTest(actual=actual), self.assertRaisesRegex(RuntimeError, "Base accelerator packages changed"):
                profiles.verify_accelerator_versions(expected, actual)

    def test_target_addition_preserves_base_abi_but_changes_capability_identity(self):
        profile = {"id": "win-rocm100-multiarch", "revision": 1}
        host = {"torch": "2.13.0+rocm10.0.0", "rocm-sdk-core": "10.0.0"}
        before = profiles.runtime_fingerprints(profile, "3.12.8", host, ["gfx1031"])
        after = profiles.runtime_fingerprints(profile, "3.12.9", {
            **host, "amd-torch-device-gfx1101": "2.13.0+rocm10.0.0",
        }, ["gfx1031", "gfx1101"], device_packages=["amd-torch-device-gfx1101"])
        self.assertEqual(before["baseFingerprint"], after["baseFingerprint"])
        self.assertNotEqual(before["targetFingerprint"], after["targetFingerprint"])
        changed = profiles.runtime_fingerprints({**profile, "revision": 2}, "3.12.8", host)
        self.assertNotEqual(before["baseFingerprint"], changed["baseFingerprint"])

    def test_overlay_rejects_metadata_and_unregistered_shadow_modules(self):
        for entry in ["torch", "rocm.py", "torch" + importlib.machinery.EXTENSION_SUFFIXES[0],
                      "_rocm_sdk_core", "amd_torch_device_gfx1031", "custom_kernel"]:
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / entry).mkdir()
                with self.assertRaisesRegex(RuntimeError, "shadows"):
                    profiles.assert_no_accelerator_overlay(root, ["custom-kernel"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            info = root / "hidden-1.0.dist-info"
            info.mkdir()
            (info / "METADATA").write_text("Metadata-Version: 2.1\nName: amd-torch-device-gfx1031\nVersion: 1.0\n")
            with self.assertRaisesRegex(RuntimeError, "contains"):
                profiles.assert_no_accelerator_overlay(root)

    def test_old_overlay_is_not_reused_after_base_profile_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / "rocm"
            env.mkdir()
            python = env / "Scripts/python.exe"
            python.parent.mkdir()
            python.touch()
            (env / "pymss-runtime-state.json").write_text(json.dumps({"baseFingerprint": "new"}))
            overlay = root / ".overlays/rocm/old/site-packages"
            overlay.mkdir(parents=True)
            selection = root / "selected.json"
            selection.write_text(json.dumps({"pythonPath": str(python), "baseFingerprint": "old", "overlayPath": str(overlay)}))
            with mock.patch.object(bootstrap, "RUNTIME_ENVS_DIR", root), \
                 mock.patch.object(bootstrap, "_runtime_overlay_selection_path", return_value=selection):
                self.assertIsNone(bootstrap._registered_runtime_overlay("rocm", python))

    def test_core_resolver_protects_packages_from_frozen_report(self):
        with self.assertRaisesRegex(RuntimeError, "base accelerator package"):
            bootstrap._resolved_overlay_requirements(["pymss==2.3.4"], {
                "version": "1", "install": [{"metadata": {"name": "custom-kernel", "version": "2.0"}}],
            }, {"custom-kernel": "1.0"})


class InstallationFreezeTests(unittest.TestCase):
    def install(self, *, backend="cpu", drift=False):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        pointer = root / "active-runtime.json"
        pointer.write_text(json.dumps({"backend": "cuda", "pythonPath": "previous-python"}))
        original_pointer = pointer.read_bytes()
        env = root / backend
        env.mkdir()
        (env / "retained.bin").write_bytes(b"previous environment")
        manifest = {
            "manifestVersion": "2026.11.2", "common": {"pymss": "pymss>=2.3.4", "pymss-core": "pymss-core>=0.1.14"},
            "backends": {backend: {"platforms": ["win32", "darwin"], "torch": {"requirement": "torch==2.7.1"},
                                   "extras": ["mlx"] if backend == "mlx" else []}},
        }
        platform_name = "darwin" if backend == "mlx" else "win32"
        phase = {"mlx": False}
        commands = []

        def create(directory):
            python = directory / ("bin/python" if backend == "mlx" else "Scripts/python.exe")
            python.parent.mkdir(parents=True)
            python.touch()

        def probe(*_args):
            packages = {"pymss": True, "pymss-core": True}
            fixed = {"torch": "2.7.1"}
            if backend == "mlx":
                packages["mlx"] = phase["mlx"]
                if phase["mlx"]:
                    fixed.update({"mlx": "0.31.0", "mlx-metal": "0.31.0"})
            if drift and any("pymss>=2.3.4" in command for command, _ in commands):
                fixed["rocm"] = "10.0.0"
            return {"pythonVersion": "3.12.8", "torchVersion": "2.7.1", "torchBackend": "cpu",
                    "acceleratorAvailable": False, "acceleratorPackages": fixed, "packages": packages,
                    "packageVersions": {"pymss": "2.3.4", "pymss-core": "0.1.14", **fixed},
                    "pymssVersion": "2.3.4", "pymssCoreVersion": "0.1.14", "pymssGraphAvailable": True}

        def popen(command, **_kwargs):
            constraints = None
            if "--constraint" in command:
                constraints = Path(command[command.index("--constraint") + 1]).read_text()
            commands.append((command, constraints))
            if "mlx" in command:
                phase["mlx"] = True
            return mock.Mock(stdout=io.StringIO(), wait=mock.Mock(return_value=0), returncode=0)

        with mock.patch.object(bootstrap, "RUNTIME_ENVS_DIR", root), \
             mock.patch.object(bootstrap, "ACTIVE_RUNTIME_FILE", pointer), \
             mock.patch.object(bootstrap, "_manifest", return_value=manifest), \
             mock.patch.object(bootstrap.sys, "platform", platform_name), \
             mock.patch.object(bootstrap, "_create_runtime_venv", side_effect=create), \
             mock.patch.object(bootstrap, "_probe_python_runtime", side_effect=probe), \
             mock.patch.object(bootstrap, "_ensure_runtime_pip"), \
             mock.patch.object(bootstrap, "_verify_runtime_audio"), \
             mock.patch.object(bootstrap.subprocess, "Popen", side_effect=popen), \
             contextlib.redirect_stdout(io.StringIO()):
            result = bootstrap.cmd_install_runtime({"backend": backend, "mirror": "pypi"})
        return result, root, pointer, original_pointer, commands

    def test_changed_accelerator_set_prevents_activation_and_restores_original(self):
        result, root, pointer, original, commands = self.install(drift=True)
        self.assertEqual(result, 1)
        self.assertEqual(pointer.read_bytes(), original)
        self.assertEqual((root / "cpu/retained.bin").read_bytes(), b"previous environment")
        constrained = [constraint for command, constraint in commands if "pymss>=2.3.4" in command]
        self.assertEqual(constrained, ["torch==2.7.1\n"])
        self.assertFalse(list(root.glob("*.txt")))

    def test_fresh_mlx_packages_join_base_before_common_and_core_installation(self):
        result, root, pointer, _original, commands = self.install(backend="mlx")
        self.assertEqual(result, 0)
        state = json.loads(pointer.read_text())
        self.assertEqual(state["resolvedAcceleratorPackages"], {
            "torch": "2.7.1", "mlx": "0.31.0", "mlx-metal": "0.31.0",
        })
        core_constraints = [constraint for command, constraint in commands if "pymss>=2.3.4" in command]
        self.assertEqual(core_constraints, ["mlx==0.31.0\nmlx-metal==0.31.0\ntorch==2.7.1\n"])
        self.assertFalse(list(root.glob("*.txt")))


if __name__ == "__main__":
    unittest.main()
