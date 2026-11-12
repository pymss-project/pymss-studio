from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap  # noqa: F401

import worker_bootstrap as bootstrap
import worker_runtime_artifacts as artifacts
import worker_runtime_profiles as profiles
import worker_runtime_transactions as transactions

VERIFY_ROCM_ACTIVATION = bootstrap._verify_rocm_activation


class RuntimeOperationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.pointer = self.root / "active-runtime.json"
        cpu = self.root / "cpu/Scripts/python.exe"
        cpu.parent.mkdir(parents=True)
        cpu.touch()
        self.pointer.write_text(json.dumps({"backend": "cpu", "pythonPath": str(cpu)}))
        self.manifest = json.loads(bootstrap.MANIFEST_PATH.read_text(encoding="utf-8"))
        self.manifest["common"] = {"pymss": "pymss>=2.3.4", "pymss-core": "pymss-core>=0.1.14"}
        self.profile = self.manifest["backends"]["rocm"]["platformOverrides"]["win32"]["profiles"]["win-rocm100-multiarch"]
        self.profile.update(installationEnabled=True, releaseState="preview", availableTargets=["gfx1031", "gfx1101", "all"])
        self.fixed = dict(self.profile["packages"])
        self.host = {**self.fixed, "pymss": "2.3.4", "pymss-core": "0.1.14"}
        self.device_names = {target: "amd-torch-device-" + target for target in ("gfx1031", "gfx1101")}
        self.lock = self.make_lock()
        self.lock_path = self.root / "lock.json"
        self.lock_path.write_text(json.dumps(self.lock))
        self.installed = {}
        self.installed_sources = {}
        self.commands = []
        self.requirements = []
        self.downloads = []
        self.download_directories = []
        self.events = []
        for patch in [
            mock.patch.object(bootstrap, "RUNTIME_ENVS_DIR", self.root),
            mock.patch.object(bootstrap, "ACTIVE_RUNTIME_FILE", self.pointer),
            mock.patch.object(bootstrap, "BUNDLED_RUNTIME_ENVS_DIR", None),
            mock.patch.object(bootstrap, "_manifest", side_effect=lambda: copy.deepcopy(self.manifest)),
            mock.patch.object(bootstrap.sys, "platform", "win32"),
            mock.patch.object(bootstrap, "machine", return_value="AMD64"),
            mock.patch.object(bootstrap.platform, "python_version", return_value="3.12.8"),
            mock.patch.object(artifacts, "artifact_lock_path", return_value=self.lock_path),
            mock.patch.object(artifacts, "download_locked_artifacts", side_effect=self.download),
            mock.patch.object(bootstrap, "_create_runtime_venv", side_effect=self.create),
            mock.patch.object(bootstrap, "_runtime_python_works", return_value=True),
            mock.patch.object(bootstrap, "_runtime_pip_works", return_value=True),
            mock.patch.object(bootstrap, "_ensure_runtime_pip"),
            mock.patch.object(bootstrap, "_verify_runtime_pip_check"),
            mock.patch.object(bootstrap, "_verify_runtime_audio"),
            mock.patch.object(bootstrap, "_verify_rocm_activation", return_value={"status": "passed"}),
            mock.patch.object(bootstrap, "_probe_python_runtime", side_effect=self.probe),
            mock.patch.object(bootstrap, "_probe_python_package_versions", side_effect=lambda _python, names, *_args: {n: self.installed.get(n) for n in names}),
            mock.patch.object(bootstrap, "_probe_python_distribution_sources", side_effect=self.sources),
            mock.patch.object(bootstrap.subprocess, "Popen", side_effect=self.pip),
            mock.patch.object(bootstrap, "_emit", side_effect=lambda *event: self.events.append(event)),
            mock.patch("worker_runtime_paths.discover_rocm_native_paths", return_value={"nativeLibraryDirs": [], "nativeToolDirs": []}),
        ]:
            self.enterContext(patch)

    def make_lock(self):
        wheel_artifacts = []
        for name, version in {**self.host, **{name: "1.0" for name in self.device_names.values()}}.items():
            target = next((t for t, n in self.device_names.items() if n == name), None)
            filename = name.replace("-", "_") + "-" + version + "-py3-none-any.whl"
            body = f"locked:{name}:{version}".encode()
            item = {"name": name, "version": version, "url": "https://example.com/" + filename,
                    "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "filename": filename,
                    "tags": ["py3-none-any"], "role": "device" if target else "host",
                    "targets": [target] if target else [], "dependencies": []}
            item["provenance"] = {"kind": "upstream-wheel", "source": {k: item[k] for k in ("url", "sha256", "bytes")}}
            wheel_artifacts.append(item)
        value = {"schemaVersion": 1, "backend": "rocm", "profileId": self.profile["id"], "profileRevision": self.profile["revision"],
                 "platform": "win32", "machine": "amd64", "python": "3.12", "pythonAbi": "cp312",
                 "platformTag": "win_amd64", "channel": "preview", "targets": sorted(self.device_names),
                 "hostArtifacts": sorted(self.host), "targetArtifacts": {t: [n] for t, n in self.device_names.items()},
                 "artifacts": wheel_artifacts}
        value["lockId"] = artifacts.artifact_lock_id(value)
        return value

    def download(self, plan, wheelhouse, *, artifacts, notify, source, locale):
        self.downloads.append([item["name"] for item in artifacts])
        self.download_directories.append(Path(wheelhouse))
        for item in artifacts:
            (Path(wheelhouse) / item["filename"]).write_bytes(f"locked:{item['name']}:{item['version']}".encode())
        return Path(wheelhouse)

    def create(self, directory):
        python = directory / "Scripts/python.exe"
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"independent interpreter")

    def probe(self, *_args):
        return {"pythonVersion": "3.12.8", "torchVersion": self.installed.get("torch"), "torchBackend": "rocm",
                "acceleratorAvailable": True, "packages": {"pymss": True, "pymss-core": True},
                "acceleratorPackages": profiles.accelerator_versions(self.installed), "packageVersions": dict(self.installed),
                "pymssVersion": "2.3.4", "pymssCoreVersion": "0.1.14", "pymssGraphAvailable": True}

    def sources(self, _python, names):
        return {name: copy.deepcopy(self.installed_sources.get(name, {})) for name in names}

    def pip(self, command, **_kwargs):
        self.commands.append(command)
        requirements = Path(command[command.index("-r") + 1]).read_text()
        self.requirements.append(requirements)
        urls = {line.split(" @ ", 1)[0]: line.split(" @ ", 1)[1].split(" --hash", 1)[0]
                for line in requirements.splitlines()}
        selected = [item for item in self.lock["artifacts"] if item["name"] + " @ " in requirements]
        self.installed.update({item["name"]: item["version"] for item in selected})
        self.installed_sources.update({item["name"]: {**{key: item[key] for key in ("version", "sha256")}, "url": urls[item["name"]]}
                                       for item in selected})
        report = {"version": "1", "install": [{"metadata": {"name": item["name"], "version": item["version"]},
                   "download_info": {"url": urls[item["name"]], "archive_info": {"hashes": {"sha256": item["sha256"]}}}}
                  for item in selected]}
        Path(command[command.index("--report") + 1]).write_text(json.dumps(report))
        return mock.Mock(stdout=io.StringIO(), returncode=0, wait=mock.Mock(return_value=0))

    def install(self, **options):
        with contextlib.redirect_stdout(io.StringIO()):
            return bootstrap.cmd_install_runtime({"backend": "rocm", "profileId": self.profile["id"],
                "targets": ["gfx1031"], "acceptPreview": True, **options})

    def state(self):
        return json.loads((self.root / "rocm/pymss-runtime-state.json").read_text())

    def expected(self):
        state = self.state()
        return {"expected" + key[0].upper() + key[1:]: state[key]
                for key in ("profileId", "profileRevision", "baseFingerprint", "targetFingerprint")}

    def test_locked_install_prefetches_selected_wheels_and_keeps_pip_offline(self):
        self.assertEqual(self.install(source="huggingface", locale="zh-CN"), 0)
        self.assertEqual(artifacts.download_locked_artifacts.call_args.kwargs["source"], "huggingface")
        self.assertEqual(artifacts.download_locked_artifacts.call_args.kwargs["locale"], "zh-cn")
        self.assertEqual(len(self.downloads), 1)
        self.assertIn(self.device_names["gfx1031"], self.downloads[0])
        self.assertNotIn(self.device_names["gfx1101"], self.downloads[0])
        self.assertTrue(self.commands)
        self.assertTrue(all("--no-index" in command and "--require-hashes" in command for command in self.commands))
        self.assertTrue(all("file:///" in text and "https://" not in text for text in self.requirements))
        self.assertEqual(list(self.root.rglob(".wheels-*")), [])
        self.assertEqual(self.download_directories[0].parent, self.root / "rocm")

    def test_space_estimate_includes_only_selected_downloads(self):
        with mock.patch.object(transactions.RocmTransaction, "preflight", return_value={}) as preflight:
            self.assertEqual(self.install(), 0)
            selected = set(self.host) | {self.device_names["gfx1031"]}
            expected = sum(item["bytes"] for item in self.lock["artifacts"] if item["name"] in selected)
            self.assertEqual(preflight.call_args.kwargs, {"estimated_bytes": expected, "download_bytes": expected})
            self.assertEqual(self.install(action="addTargets", targets=["gfx1101"], **self.expected()), 0)
            expected = next(item["bytes"] for item in self.lock["artifacts"] if item["name"] == self.device_names["gfx1101"])
            self.assertEqual(preflight.call_args.kwargs, {"estimated_bytes": expected, "download_bytes": expected})

    def test_insufficient_wheelhouse_space_fails_before_creating_a_transaction(self):
        with mock.patch.object(transactions.RocmTransaction, "preflight", side_effect=transactions.RuntimeSpaceError("insufficient")):
            self.assertEqual(self.install(), 1)
        self.assertFalse(self.commands)
        self.assertFalse(self.downloads)
        self.assertFalse((self.root / ".rocm-transaction.json").exists())

    def prepare_legacy(self):
        self.create(self.root / "rocm")
        self.installed = dict(self.manifest["backends"]["rocm"]["platformOverrides"]["win32"]["profiles"]["win-rocm721"]["packages"])
        self.installed.update({"pymss": "2.3.4", "pymss-core": "0.1.14"})
        old = {"backend": "rocm", "stateVersion": 2, "pythonVersion": "3.12.8",
               "torchVersion": self.installed["torch"], "manifestVersion": "2026.11.2"}
        (self.root / "rocm/pymss-runtime-state.json").write_text(json.dumps(old))
        return bootstrap._runtime_operation_plan({"backend": "rocm", "profileId": self.profile["id"],
            "targets": ["gfx1031"], "action": "upgrade"}, self.manifest)

    def test_legacy_upgrade_uses_live_identity_and_preserves_the_original_rollback_generation(self):
        plan = self.prepare_legacy()
        expected = {key: value for key, value in plan.items() if key.startswith("expected")}
        self.assertTrue(all(expected.values()))
        old = (self.root / "rocm/pymss-runtime-state.json").read_bytes()
        marker = self.root / "rocm/original.bin"
        marker.write_bytes(b"original legacy payload")
        pointer = self.pointer.read_bytes()
        legacy_probe = self.probe()
        self.assertEqual(self.commands, [])

        def fresh(directory):
            self.create(directory)
            self.installed.clear()

        with mock.patch.object(bootstrap, "_create_runtime_venv", side_effect=fresh):
            self.assertEqual(self.install(action="upgrade", **expected), 0)
        self.assertEqual(self.state()["profileId"], self.profile["id"])
        self.assertEqual((self.root / ".rocm-previous/environment/pymss-runtime-state.json").read_bytes(), old)
        self.assertEqual((self.root / ".rocm-previous/environment/original.bin").read_bytes(), b"original legacy payload")
        self.assertEqual(self.pointer.read_bytes(), pointer)
        with mock.patch.object(bootstrap, "_probe_python_runtime", return_value=legacy_probe), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.cmd_rollback_runtime({"backend": "rocm", "activate": False}), 0)
        self.assertEqual(self.state()["torchVersion"], legacy_probe["torchVersion"])
        self.assertEqual(marker.read_bytes(), b"original legacy payload")

    def test_legacy_plan_hides_probe_details_and_rejects_sdk_changes_before_replacement(self):
        plan = self.prepare_legacy()
        old = (self.root / "rocm/pymss-runtime-state.json").read_bytes()
        pointer = self.pointer.read_bytes()
        expected = {key: value for key, value in plan.items() if key.startswith("expected")}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.cmd_runtime_install_plan({"backend": "rocm", "profileId": self.profile["id"],
                "targets": ["gfx1031"], "action": "upgrade"}), 0)
        public_plan = self.events[-1][1]
        self.assertNotIn("_legacySourceState", public_plan)
        self.assertNotIn("_legacySourceProbe", public_plan)
        self.installed["rocm-sdk-core"] = "7.2.0"
        self.assertEqual(self.install(action="upgrade", **expected), 1)
        self.assertEqual((self.root / "rocm/pymss-runtime-state.json").read_bytes(), old)
        self.assertEqual(self.pointer.read_bytes(), pointer)
        self.assertEqual(self.commands, [])

    def test_legacy_identity_cannot_authorize_a_different_or_unknown_installed_build(self):
        plan = self.prepare_legacy()
        old = (self.root / "rocm/pymss-runtime-state.json").read_bytes()
        expected = {key: value for key, value in plan.items() if key.startswith("expected")}
        for version in ("2.13.0+rocm10.0.0", "2.14.0+rocm10.1.0"):
            with self.subTest(version=version):
                self.installed["torch"] = version
                self.assertEqual(self.install(action="upgrade", **expected), 1)
                self.assertEqual((self.root / "rocm/pymss-runtime-state.json").read_bytes(), old)
                self.assertEqual(self.commands, [])

    def test_preview_requires_explicit_acceptance_and_cannot_override_disabled(self):
        self.assertEqual(self.install(acceptPreview=False), 1)
        self.profile.update(installationEnabled=False, releaseState="disabled")
        self.assertEqual(self.install(), 1)
        self.assertEqual(self.commands, [])

    def test_multiarch_install_persists_real_coverage_roles_and_lock(self):
        self.assertEqual(self.install(), 0)
        state = self.state()
        self.assertEqual(state["requestedTargets"], ["gfx1031"])
        self.assertEqual(state["installedTargets"], ["gfx1031"])
        self.assertEqual(state["devicePackageNames"], [self.device_names["gfx1031"]])
        self.assertEqual(state["packageRoles"]["torch"], "host")
        self.assertEqual(state["fingerprintSchemaVersion"], 2)
        self.assertEqual(state["artifactLockId"], self.lock["lockId"])
        self.assertEqual(state["activationValidation"], {"status": "notRun"})
        self.assertIn("--require-hashes", self.commands[0])
        self.assertIn("--no-deps", self.commands[0])

    def test_target_addition_preserves_base_and_previous_bytes(self):
        self.assertEqual(self.install(), 0)
        before = self.state()
        original = self.root / "rocm/payload.bin"
        original.write_bytes(b"original content")
        expected = self.expected()
        self.commands.clear()
        self.assertEqual(self.install(action="addTargets", targets=["gfx1101"], **expected), 0)
        after = self.state()
        self.assertEqual(after["baseFingerprint"], before["baseFingerprint"])
        self.assertNotEqual(after["targetFingerprint"], before["targetFingerprint"])
        self.assertEqual(after["requestedTargets"], ["gfx1101"])
        self.assertEqual(after["installedTargets"], ["gfx1031", "gfx1101"])
        original.write_bytes(b"candidate change")
        self.assertEqual((self.root / ".rocm-previous/environment/payload.bin").read_bytes(), b"original content")
        self.assertEqual(len(self.commands), 1)
        # The only install operation is the added device closure; host wheels are frozen.
        report = json.loads((self.root / "rocm/pymss-install-locked.json").read_text())
        self.assertEqual([i["metadata"]["name"] for i in report["install"]], [self.device_names["gfx1101"]])

    def test_stale_upgrade_and_unconfirmed_profile_change_are_rejected(self):
        self.assertEqual(self.install(), 0)
        before = (self.root / "rocm/pymss-runtime-state.json").read_bytes()
        self.assertEqual(self.install(action="upgrade", expectedBaseFingerprint="stale"), 1)
        self.profile["revision"] = 2
        self.assertEqual(self.install(), 1)
        self.assertEqual((self.root / "rocm/pymss-runtime-state.json").read_bytes(), before)

    def test_confirmed_revision_upgrade_uses_transaction_and_keeps_old_recipe(self):
        self.assertEqual(self.install(), 0)
        before = self.state()
        expected = self.expected()
        self.profile["revision"] = 2
        self.lock["profileRevision"] = 2
        self.lock["lockId"] = artifacts.artifact_lock_id(self.lock)
        self.lock_path.write_text(json.dumps(self.lock))
        self.assertEqual(self.install(action="upgrade", **expected), 0)
        self.assertEqual(self.state()["profileRevision"], 2)
        self.assertNotEqual(self.state()["baseFingerprint"], before["baseFingerprint"])
        old = json.loads((self.root / ".rocm-previous/environment/pymss-runtime-state.json").read_text())
        self.assertEqual(old["baseFingerprint"], before["baseFingerprint"])
        self.assertEqual(old["profileRevision"], 1)
        previous = json.loads((self.root / ".rocm-previous/manifest.json").read_text())
        self.assertEqual(previous["profile"]["revision"], 1)

    def test_pip_report_does_not_substitute_for_installed_device_metadata(self):
        pip = self.pip

        def incomplete(command, **kwargs):
            result = pip(command, **kwargs)
            self.installed.pop(self.device_names["gfx1031"], None)
            return result

        with mock.patch.object(bootstrap.subprocess, "Popen", side_effect=incomplete):
            self.assertEqual(self.install(), 1)
        self.assertFalse((self.root / "rocm/pymss-runtime-state.json").exists())
        self.assertFalse((self.root / ".rocm-transaction.json").exists())

    def test_target_addition_rejects_host_hash_drift_even_when_version_is_unchanged(self):
        self.assertEqual(self.install(), 0)
        expected = self.expected()
        before = (self.root / "rocm/pymss-runtime-state.json").read_bytes()
        host = next(item for item in self.lock["artifacts"] if item["name"] == "torch")
        host["sha256"] = host["provenance"]["source"]["sha256"] = "b" * 64
        self.lock["lockId"] = artifacts.artifact_lock_id(self.lock)
        self.lock_path.write_text(json.dumps(self.lock))
        count = len(self.commands)
        self.assertEqual(self.install(action="addTargets", targets=["gfx1101"], **expected), 1)
        self.assertEqual(len(self.commands), count)
        self.assertEqual((self.root / "rocm/pymss-runtime-state.json").read_bytes(), before)

    def test_target_addition_rejects_live_same_version_host_replacements_before_transaction(self):
        self.host["rocm-sdk-core"] = "10.0.0"
        self.lock = self.make_lock()
        self.lock_path.write_text(json.dumps(self.lock))
        self.assertEqual(self.install(), 0)
        expected, state = self.expected(), self.state()
        marker = self.root / "rocm/retained.bin"
        marker.write_bytes(b"original environment")
        self.pointer.write_text(json.dumps({**state, "backend": "rocm", "pythonPath": str(self.root / "rocm/Scripts/python.exe")}))
        selection = self.root / ".overlays/rocm/active-overlay.json"
        selection.parent.mkdir(parents=True)
        selection.write_text(json.dumps({"baseFingerprint": state["baseFingerprint"], "overlayPath": "retained-overlay"}))
        state_path = self.root / "rocm/pymss-runtime-state.json"
        original = {path: path.read_bytes() for path in (marker, self.pointer, selection, state_path)}
        sdk = next(name for name in self.installed_sources if name.startswith("rocm") and name != "rocm")
        for name in ("torch", sdk, "pymss"):
            with self.subTest(distribution=name):
                digest = self.installed_sources[name]["sha256"]
                self.installed_sources[name]["sha256"] = "b" * 64
                count = len(self.commands)
                self.assertEqual(self.install(action="addTargets", targets=["gfx1101"], **expected), 1)
                self.assertEqual(len(self.commands), count)
                for path, content in original.items():
                    self.assertEqual(path.read_bytes(), content)
                self.assertFalse((self.root / ".rocm-transaction.json").exists())
                self.assertFalse(list(self.root.glob(".rocm-backup-*")))
                self.installed_sources[name]["sha256"] = digest

    def change_torch_recipe(self):
        self.profile["revision"] = 2
        self.profile["packages"]["torch"] = "2.14.0+rocm10.0.0"
        self.fixed["torch"] = self.profile["packages"]["torch"]
        self.host["torch"] = self.fixed["torch"]
        self.lock = self.make_lock()
        self.lock_path.write_text(json.dumps(self.lock))

    def prepare_changed_torch_upgrade(self):
        self.assertEqual(self.install(), 0)
        before, sources = self.state(), copy.deepcopy(self.installed_sources)
        expected = self.expected()
        self.change_torch_recipe()
        self.assertEqual(self.install(action="upgrade", **expected), 0)
        self.assertEqual(self.state()["torchVersion"], "2.14.0+rocm10.0.0")
        self.assertEqual(self.state()["profileRevision"], 2)
        return before, sources

    def saved_probe(self, state):
        return {**self.probe(), "torchVersion": state["torchVersion"],
                "acceleratorPackages": state["resolvedAcceleratorPackages"],
                "packageVersions": state["packageVersions"]}

    def test_changed_torch_recipe_can_upgrade_then_restore_its_original_revision(self):
        before, sources = self.prepare_changed_torch_upgrade()
        with mock.patch.object(bootstrap, "_probe_python_runtime", return_value=self.saved_probe(before)), \
             mock.patch.object(bootstrap, "_probe_python_distribution_sources", side_effect=lambda _python, names: {n: sources[n] for n in names}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.cmd_rollback_runtime({"backend": "rocm", "activate": False}), 0)
        self.assertEqual(self.state()["torchVersion"], before["torchVersion"])
        self.assertEqual(self.state()["profileRevision"], 1)
        self.assertEqual(self.state()["baseFingerprint"], before["baseFingerprint"])

    def test_changed_torch_upgrade_rejects_live_source_hash_drift(self):
        self.assertEqual(self.install(), 0)
        expected, before = self.expected(), self.state()
        self.installed_sources["torch"]["sha256"] = "b" * 64
        self.change_torch_recipe()
        count = len(self.commands)
        self.assertEqual(self.install(action="upgrade", **expected), 1)
        self.assertEqual(len(self.commands), count)
        self.assertEqual(self.state(), before)

    def test_locked_source_can_move_download_location_without_changing_artifact_identity(self):
        self.assertEqual(self.install(), 0)
        expected = self.expected()
        self.installed_sources["torch"]["url"] = "file:///C:/release-cache/torch.whl"
        self.change_torch_recipe()
        self.assertEqual(self.install(action="upgrade", **expected), 0)

    def test_changed_torch_rollback_still_enforces_current_app_common_minimum(self):
        before, sources = self.prepare_changed_torch_upgrade()
        current = self.state()
        self.manifest["common"]["pymss"] = "pymss>=2.5.0"
        with mock.patch.object(bootstrap, "_probe_python_runtime", return_value=self.saved_probe(before)), \
             mock.patch.object(bootstrap, "_probe_python_distribution_sources", side_effect=lambda _python, names: {n: sources[n] for n in names}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.cmd_rollback_runtime({"backend": "rocm", "activate": False}), 1)
        self.assertEqual(self.state(), current)

    def test_device_failure_restores_original_directory_pointer_and_overlay(self):
        self.assertEqual(self.install(), 0)
        expected = self.expected()
        marker = self.root / "rocm/retained.bin"
        marker.write_bytes(b"old environment")
        selection = self.root / ".overlays/rocm/active-overlay.json"
        selection.parent.mkdir(parents=True)
        selection.write_text('{"baseFingerprint":"retained"}')
        before, pointer = self.state(), self.pointer.read_bytes()
        with mock.patch.object(bootstrap, "_verify_runtime_audio", side_effect=RuntimeError("decoder failure")):
            self.assertEqual(self.install(action="addTargets", targets=["gfx1101"], **expected), 1)
        self.assertEqual(self.state(), before)
        self.assertEqual(marker.read_bytes(), b"old environment")
        self.assertEqual(self.pointer.read_bytes(), pointer)
        self.assertEqual(selection.read_text(), '{"baseFingerprint":"retained"}')
        self.assertFalse((self.root / ".rocm-transaction.json").exists())

    def test_device_all_is_materialized_as_explicit_coverage(self):
        self.assertEqual(self.install(targets=["all"]), 0)
        state = self.state()
        self.assertEqual(state["requestedTargets"], ["all"])
        self.assertEqual(state["resolvedTargets"], ["gfx1031", "gfx1101"])
        self.assertEqual(state["installedTargets"], ["gfx1031", "gfx1101"])

    def test_failed_gpu_activation_restores_original_and_does_not_claim_retained_candidate(self):
        self.assertEqual(self.install(), 0)
        before = (self.root / "rocm/pymss-runtime-state.json").read_bytes()
        pointer = self.pointer.read_bytes()
        with mock.patch.object(bootstrap, "_verify_rocm_activation", return_value={"status": "failed", "reason": "targetMissing"}):
            self.assertEqual(self.install(activate=True), 0)
        self.assertEqual(self.pointer.read_bytes(), pointer)
        self.assertEqual((self.root / "rocm/pymss-runtime-state.json").read_bytes(), before)
        finished = [event[1] for event in self.events if event[0] == "runtime_install_finished"][-1]
        self.assertFalse(finished["activated"])
        self.assertFalse(finished["preparedState"]["retained"])
        self.assertFalse((self.root / ".rocm-transaction.json").exists())

    def test_first_gpu_failure_keeps_dependencies_pointer_and_overlays_for_activation_retry(self):
        pointer = self.pointer.read_bytes()
        selection = self.root / ".overlays/rocm/active-overlay.json"
        selection.parent.mkdir(parents=True)
        selection.write_text('{"pythonPath":"old-bundled-python","overlayPath":"retained-overlay"}')
        overlay = selection.read_bytes()
        with mock.patch.object(bootstrap, "_verify_rocm_activation", return_value={"status": "failed", "reason": "timeout"}):
            self.assertEqual(self.install(activate=True), 0)
        self.assertEqual(self.pointer.read_bytes(), pointer)
        self.assertEqual(selection.read_bytes(), overlay)
        self.assertTrue(self.state()["dependencyReady"])
        self.assertEqual(self.state()["activationValidation"]["status"], "failed")
        self.assertTrue((self.root / "rocm/.pymss-runtime-commit.json").is_file())
        self.assertFalse((self.root / ".rocm-transaction.json").exists())
        finished = [event[1] for event in self.events if event[0] == "runtime_install_finished"][-1]
        self.assertTrue(finished["preparedState"]["retained"])
        self.assertEqual(finished["state"]["pythonPath"], str(self.root / "rocm/Scripts/python.exe"))
        self.assertFalse(finished["activated"])
        count = len(self.commands)
        with mock.patch.object(bootstrap.subprocess, "Popen", side_effect=AssertionError("Activation must not reinstall dependencies")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.cmd_activate_runtime({"backend": "rocm"}), 0)
        self.assertEqual(len(self.commands), count)
        self.assertEqual(json.loads(self.pointer.read_text())["backend"], "rocm")

    def test_first_gpu_failure_does_not_revive_a_dangling_canonical_pointer(self):
        self.pointer.write_text(json.dumps({"backend": "rocm", "pythonPath": str(self.root / "rocm/Scripts/python.exe")}))
        original = self.pointer.read_bytes()
        with mock.patch.object(bootstrap, "_verify_rocm_activation", return_value={"status": "failed"}):
            self.assertEqual(self.install(activate=True), 0)
        self.assertEqual(self.pointer.read_bytes(), original)
        self.assertFalse((self.root / "rocm").exists())
        finished = [event[1] for event in self.events if event[0] == "runtime_install_finished"][-1]
        self.assertFalse(finished["preparedState"]["retained"])

    def test_prepared_commit_recovery_uses_the_persisted_nonactivation_contract(self):
        original_write = transactions._write
        original_pointer = self.pointer.read_bytes()
        for phase in ("before-marker", "after-marker"):
            with self.subTest(phase=phase):
                transaction = transactions.RocmTransaction(self.root, self.pointer)
                record = transaction.begin("install", activate=True)
                self.create(self.root / "rocm")
                (self.root / "rocm/pymss-runtime-state.json").write_text(json.dumps({"environmentTransactionId": record["id"]}))

                def interrupted(path, value):
                    if (phase == "before-marker" and path.name == ".pymss-runtime-commit.json") or (
                            phase == "after-marker" and path == transaction.journal and value.get("phase") == "committed"):
                        raise OSError("interrupted prepared commit")
                    original_write(path, value)

                with mock.patch.object(transactions, "_write", side_effect=interrupted), self.assertRaises(OSError):
                    transaction.commit_prepared()
                self.assertFalse(json.loads(transaction.journal.read_text())["activate"])
                self.assertTrue(transactions.RocmTransaction(self.root, self.pointer).recover())
                self.assertEqual(self.pointer.read_bytes(), original_pointer)
                self.assertEqual((self.root / "rocm").is_dir(), phase == "after-marker")
                self.assertFalse(transaction.journal.exists())

    def test_first_failed_gpu_seed_keeps_candidate_while_bundled_pointer_remains_active(self):
        self.assertEqual(self.install(), 0)
        bundled = self.root / "app/python-runtime/runtime-envs"
        bundled.mkdir(parents=True)
        source = bundled / "rocm"
        shutil.copytree(self.root / "rocm", source)
        shutil.rmtree(self.root / "rocm")
        base = bundled.parent
        (base / "python.exe").write_bytes(b"embedded Python")
        (base / "python312.dll").write_bytes(b"Python ABI")
        self.pointer.write_text(json.dumps({"backend": "rocm", "pythonPath": str(source / "Scripts/python.exe")}))
        pointer = self.pointer.read_bytes()
        self.commands.clear()
        with mock.patch.object(bootstrap, "BUNDLED_RUNTIME_ENVS_DIR", bundled), \
             mock.patch.object(bootstrap, "_bootstrap_python_path", return_value=base / "python.exe"), \
             mock.patch.object(bootstrap, "_verify_rocm_activation", return_value={"status": "failed"}):
            self.assertEqual(self.install(action="prepareSeed", activate=True), 0)
            self.assertEqual(self.pointer.read_bytes(), pointer)
            self.assertEqual(bootstrap._read_runtime_state()["pythonPath"], str(source / "Scripts/python.exe"))
            self.assertTrue(self.state()["dependencyReady"])
            self.assertEqual(self.state()["activationValidation"]["status"], "failed")
        self.assertEqual(self.commands, [])

    def test_bundled_seed_preparation_copies_without_network_and_binds_recoverable_python(self):
        self.assertEqual(self.install(), 0)
        bundled = self.root / "app/python-runtime/runtime-envs"
        bundled.mkdir(parents=True)
        source = bundled / "rocm"
        shutil.copytree(self.root / "rocm", source)
        shutil.rmtree(self.root / "rocm")
        base = bundled.parent
        (base / "python.exe").write_bytes(b"embedded Python")
        (base / "python312.dll").write_bytes(b"Python ABI")
        source_state = (source / "pymss-runtime-state.json").read_bytes()
        self.commands.clear()
        with mock.patch.object(bootstrap, "BUNDLED_RUNTIME_ENVS_DIR", bundled), \
             mock.patch.object(bootstrap, "_bootstrap_python_path", return_value=base / "python.exe"), \
             mock.patch.object(bootstrap, "_ensure_runtime_pip") as pip_bootstrap:
            plan = bootstrap._runtime_operation_plan({"backend": "rocm", "action": "prepareSeed"}, self.manifest)
            self.assertEqual(plan["downloadBytes"], 0)
            self.assertTrue(plan["seedPreparationRequired"])
            self.assertEqual(self.install(action="prepareSeed"), 0)
            pip_bootstrap.assert_not_called()
            state = self.state()
            binding = state["bootstrapBinding"]
            bound = self.root / binding["root"] / "python.exe"
            self.assertTrue(bound.is_file())
            self.assertIn(str(bound.resolve()), (self.root / "rocm/pyvenv.cfg").read_text())
            self.assertEqual((source / "pymss-runtime-state.json").read_bytes(), source_state)
        self.assertEqual(self.commands, [])

    def test_manual_activation_cannot_bypass_failed_minimum_gpu_check(self):
        self.assertEqual(self.install(), 0)
        pointer = self.pointer.read_bytes()
        with mock.patch.object(bootstrap, "_verify_rocm_activation", return_value={"status": "failed", "reason": "timeout"}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.cmd_activate_runtime({"backend": "rocm"}), 1)
        self.assertEqual(self.pointer.read_bytes(), pointer)

    def test_rollback_uses_the_restored_device_choice_and_allows_an_explicit_replacement(self):
        previous_ids = ["hip-pci:0000:03:00.0"]
        current_ids = ["hip-pci:0000:04:00.0"]
        self.assertEqual(self.install(selectedDeviceIds=previous_ids), 0)
        self.assertEqual(self.install(selectedDeviceIds=current_ids), 0)
        before, pointer = self.state(), self.pointer.read_bytes()
        with mock.patch.object(bootstrap, "_verify_rocm_activation", return_value={"status": "failed"}) as gate, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.cmd_rollback_runtime({"backend": "rocm", "activate": True}), 1)
        self.assertEqual(gate.call_args.kwargs["selected_device_ids"], previous_ids)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.pointer.read_bytes(), pointer)
        with mock.patch.object(bootstrap, "_verify_rocm_activation", return_value={"status": "passed"}) as gate, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.cmd_rollback_runtime({"backend": "rocm", "activate": True, "selectedDeviceIds": current_ids}), 0)
        self.assertEqual(gate.call_args.kwargs["selected_device_ids"], current_ids)
        self.assertEqual(json.loads(self.pointer.read_text())["selectedDeviceIds"], current_ids)

    def test_plan_echoes_confirmed_selection_and_install_passes_it_to_gpu_gate(self):
        ids = ["hip-pci:0000:03:00.0"]
        plan = bootstrap._runtime_operation_plan({"backend": "rocm", "profileId": self.profile["id"],
                   "targets": ["gfx1031"], "selectedDeviceIds": ids}, self.manifest)
        self.assertEqual(plan["selectedDeviceIds"], ids)
        with mock.patch.object(bootstrap, "_verify_rocm_activation", return_value={"status": "passed"}) as gate:
            self.assertEqual(self.install(activate=True, selectedDeviceIds=ids), 0)
        self.assertEqual(gate.call_args.kwargs["selected_device_ids"], ids)
        self.assertEqual(self.state()["selectedDeviceIds"], ids)
        count = len(self.commands)
        self.assertEqual(self.install(selectedDeviceIds=["session:unconfirmed"]), 1)
        self.assertEqual(len(self.commands), count)

    def test_gpu_gate_delegates_with_only_the_selected_runtime_environment(self):
        python = self.root / "rocm/Scripts/python.exe"
        environment = {"PATH": "selected-sdk"}
        ids = ["hip-pci:0000:03:00.0"]
        with mock.patch("worker_runtime_activation.verify_activation", return_value={"status": "passed"}) as verify, \
             mock.patch.object(bootstrap, "_probe_environment", return_value=environment):
            self.assertEqual(VERIFY_ROCM_ACTIVATION(python, ["gfx1031"], selected_device_ids=ids), {"status": "passed"})
        verify.assert_called_once_with(python, ["gfx1031"], None, selected_device_ids=ids, environment=environment)

    def test_moved_managed_root_rollback_rebinds_the_saved_python_generation(self):
        self.assertEqual(self.install(), 0)
        bundled = self.root / "app/python-runtime/runtime-envs"
        bundled.mkdir(parents=True)
        shutil.copytree(self.root / "rocm", bundled / "rocm")
        shutil.rmtree(self.root / "rocm")
        base = bundled.parent
        (base / "python.exe").write_bytes(b"embedded Python")
        dll = base / "python312.dll"
        dll.write_bytes(b"base generation one")
        with mock.patch.object(bootstrap, "BUNDLED_RUNTIME_ENVS_DIR", bundled), \
             mock.patch.object(bootstrap, "_bootstrap_python_path", return_value=base / "python.exe"):
            self.assertEqual(self.install(action="prepareSeed"), 0)
            first_binding = self.state()["bootstrapBinding"]
            expected = self.expected()
            self.manifest["baseGeneration"] = 2
            dll.write_bytes(b"base generation two")
            self.assertEqual(self.install(action="upgrade", **expected), 0)
            self.assertNotEqual(self.state()["bootstrapBinding"], first_binding)
            old_root = self.root
            moved = Path(self.enterContext(tempfile.TemporaryDirectory())) / "moved-managed"
            old_root.rename(moved)
            self.root, self.pointer = moved, moved / "active-runtime.json"
            expected_base = moved / first_binding["root"] / "python.exe"

            def probe_restored(*args):
                cfg = (moved / "rocm/pyvenv.cfg").read_text()
                self.assertIn(str(expected_base.resolve()), cfg)
                self.assertNotIn(str(old_root.resolve()), cfg)
                return self.probe(*args)

            with mock.patch.object(bootstrap, "RUNTIME_ENVS_DIR", moved), \
                 mock.patch.object(bootstrap, "ACTIVE_RUNTIME_FILE", self.pointer), \
                 mock.patch.object(bootstrap, "_probe_python_runtime", side_effect=probe_restored), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(bootstrap.cmd_rollback_runtime({"backend": "rocm", "activate": False}), 0)
            self.assertEqual(self.state()["bootstrapBinding"], first_binding)


if __name__ == "__main__":
    unittest.main()
