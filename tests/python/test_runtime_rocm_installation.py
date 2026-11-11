from __future__ import annotations

import contextlib
import copy
import io
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
from worker_runtime_transactions import RocmTransaction


class RocmWorkerTransactionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.envs = self.root / "runtime-envs"
        self.envs.mkdir()
        self.active = self.envs / "active-runtime.json"
        self.python = self.envs / "rocm/Scripts/python.exe"
        self.create(self.python.parent.parent)
        (self.envs / "rocm/generation.txt").write_text("original")
        cpu = self.envs / "cpu/Scripts/python.exe"
        cpu.parent.mkdir(parents=True)
        cpu.touch()
        self.original_pointer = json.dumps({"backend": "cpu", "pythonPath": str(cpu)}).encode()
        self.active.write_bytes(self.original_pointer)
        self.manifest = json.loads(Path(bootstrap.MANIFEST_PATH).read_text())
        self.manifest["backends"]["rocm"]["platformOverrides"]["win32"]["defaultProfile"] = "win-rocm721"
        self.manifest["common"] = {"pymss": "pymss>=2.3.4", "pymss-core": "pymss-core>=0.1.14"}
        self.fixed = self.manifest["backends"]["rocm"]["platformOverrides"]["win32"]["profiles"]["win-rocm721"]["packages"]
        self.probed = {
            "pythonVersion": "3.12.8", "torchVersion": self.fixed["torch"], "torchBackend": "rocm", "acceleratorAvailable": True,
            "packages": {"pymss": True, "pymss-core": True}, "acceleratorPackages": self.fixed,
            "packageVersions": {"pymss": "2.3.4", "pymss-core": "0.1.14", **self.fixed},
            "pymssVersion": "2.3.4", "pymssCoreVersion": "0.1.14", "pymssGraphAvailable": True,
        }
        (self.envs / "rocm/pymss-runtime-state.json").write_text(json.dumps({
            **self.probed, "backend": "rocm", "profileId": "win-rocm721", "profileRevision": 1,
            "nativeLibraryDirs": ["sdk/lib"], "nativeToolDirs": ["sdk/bin"],
        }))
        self.native = {"nativeLibraryDirs": ["sdk/lib"], "nativeToolDirs": ["sdk/bin"], "nativePathsSource": "rocm-sdk"}
        for patch in [
            mock.patch.object(bootstrap, "RUNTIME_ENVS_DIR", self.envs),
            mock.patch.object(bootstrap, "ACTIVE_RUNTIME_FILE", self.active),
            mock.patch.object(bootstrap, "BUNDLED_RUNTIME_ENVS_DIR", None),
            mock.patch.object(bootstrap, "_manifest", side_effect=lambda: copy.deepcopy(self.manifest)),
            mock.patch.object(bootstrap.sys, "platform", "win32"),
            mock.patch.object(bootstrap, "machine", return_value="AMD64"),
            mock.patch.object(bootstrap.platform, "python_version", return_value="3.12.8"),
            mock.patch.object(bootstrap, "_probe_python_package_versions", return_value={"torch": self.fixed["torch"]}),
            mock.patch.object(bootstrap, "_probe_python_runtime", side_effect=lambda *_args: copy.deepcopy(self.probed)),
            mock.patch.object(bootstrap, "_create_runtime_venv", side_effect=self.create),
            mock.patch.object(bootstrap, "_ensure_runtime_pip"),
            mock.patch.object(bootstrap, "_verify_runtime_audio"),
            mock.patch.object(bootstrap, "_verify_rocm_activation", return_value={"status": "passed"}),
            mock.patch("worker_runtime_paths.discover_rocm_native_paths", side_effect=lambda *_args: dict(self.native)),
        ]:
            self.enterContext(patch)

    @staticmethod
    def create(env):
        for suffix in ["Scripts", "sdk/lib", "sdk/bin"]:
            (env / suffix).mkdir(parents=True, exist_ok=True)
        (env / "Scripts/python.exe").write_bytes(b"interpreter")

    def install(self, **payload):
        report_mutator = payload.pop("_report_mutator", lambda _path, items: items)
        def pip(command, **_kwargs):
            report = Path(command[command.index("--report") + 1])
            items = []
            if report.name in {"pymss-install-rocm-sdk.json", "pymss-install-torch.json"}:
                items = [{"metadata": {"name": name, "version": version},
                          "download_info": {"url": f"https://repo.radeon.com/{name}.whl", "archive_info": {"hashes": {"sha256": "a" * 64}}}}
                         for name, version in self.fixed.items()]
            report.write_text(json.dumps({"version": "1", "install": report_mutator(report, items)}))
            return mock.Mock(stdout=io.StringIO(), wait=mock.Mock(return_value=0), returncode=0)

        with mock.patch.object(bootstrap.subprocess, "Popen", side_effect=pip), \
             contextlib.redirect_stdout(io.StringIO()):
            return worker.run_command("install_runtime", {"backend": "rocm", "mirror": "pypi", **payload})

    def test_install_prepare_then_rollback_preserves_another_active_backend(self):
        self.assertEqual(self.install(), 0)
        self.assertEqual(self.active.read_bytes(), self.original_pointer)
        self.assertTrue((self.python.parent.parent / ".pymss-runtime-commit.json").is_file())
        state = json.loads((self.python.parent.parent / "pymss-runtime-state.json").read_text())
        self.assertEqual(state["nativeToolDirs"], ["sdk/bin"])
        self.assertEqual(state["profileId"], "win-rocm721")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(worker.run_command("rollback_runtime", {"backend": "rocm"}), 0)
        self.assertEqual((self.python.parent.parent / "generation.txt").read_text(), "original")
        self.assertEqual(self.active.read_bytes(), self.original_pointer)
        self.assertIsNotNone(RocmTransaction(self.envs, self.active).previous_manifest())

    def test_explicit_activation_commits_matching_transaction_id(self):
        self.assertEqual(self.install(activate=True), 0)
        pointer = json.loads(self.active.read_text())
        state = json.loads((self.python.parent.parent / "pymss-runtime-state.json").read_text())
        self.assertEqual(pointer["environmentTransactionId"], state["environmentTransactionId"])
        self.assertEqual(pointer["backend"], "rocm")
        self.assertFalse((self.envs / ".rocm-transaction.json").exists())

    def test_unverified_accelerator_sources_or_missing_hashes_prevent_commit(self):
        for field in ["url", "hashes"]:
            def mutate(_path, items):
                for item in items:
                    if field == "url":
                        item["download_info"]["url"] = "https://example.com/torch.whl"
                    else:
                        item["download_info"]["archive_info"]["hashes"] = {}
                return items
            with self.subTest(field=field):
                self.assertEqual(self.install(activate=True, _report_mutator=mutate), 1)
                self.assertEqual((self.python.parent.parent / "generation.txt").read_text(), "original")
                self.assertEqual(self.active.read_bytes(), self.original_pointer)

    def test_common_dependencies_cannot_replace_even_the_same_accelerator_version(self):
        def mutate(path, items):
            if path.name == "pymss-install-common.json":
                return [{"metadata": {"name": "torch", "version": self.fixed["torch"]}}]
            return items
        self.assertEqual(self.install(activate=True, _report_mutator=mutate), 1)
        self.assertEqual((self.python.parent.parent / "generation.txt").read_text(), "original")
        self.assertEqual(self.active.read_bytes(), self.original_pointer)

    def test_persisted_sources_and_full_reports_do_not_contain_url_credentials(self):
        def mutate(_path, items):
            for item in items:
                item["download_info"]["url"] = "https://user:credential@repo.radeon.com/pkg.whl?signature=private#fragment"
            return items
        self.assertEqual(self.install(activate=True, _report_mutator=mutate), 0)
        for path in [*(self.python.parent.parent).glob("pymss-install-*.json"), self.python.parent.parent / "pymss-runtime-state.json"]:
            content = path.read_text()
            self.assertNotIn("credential", content)
            self.assertNotIn("signature", content)
            self.assertNotIn("fragment", content)

    def test_active_runtime_cannot_be_replaced_while_preserving_its_old_pointer(self):
        self.active.write_text(json.dumps({"backend": "rocm", "pythonPath": str(self.python)}))
        original = self.active.read_bytes()
        self.assertEqual(self.install(activate=False), 1)
        self.assertEqual(self.active.read_bytes(), original)
        self.assertEqual((self.python.parent.parent / "generation.txt").read_text(), "original")

    def test_active_runtime_rollback_requires_committing_its_pointer(self):
        self.assertEqual(self.install(activate=True), 0)
        pointer = self.active.read_bytes()
        state = (self.python.parent.parent / "pymss-runtime-state.json").read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(worker.run_command("rollback_runtime", {"backend": "rocm", "activate": False}), 1)
        self.assertEqual(self.active.read_bytes(), pointer)
        self.assertEqual((self.python.parent.parent / "pymss-runtime-state.json").read_bytes(), state)
        self.assertFalse((self.envs / ".rocm-transaction.json").exists())

    def test_rollback_restores_authoritative_overlay_when_environment_cache_is_stale(self):
        overlay = self.envs / ".overlays/rocm/old-core/site-packages"
        overlay.mkdir(parents=True)
        old_state = json.loads((self.python.parent.parent / "pymss-runtime-state.json").read_text())
        selected = {**old_state, "pythonPath": str(self.python), "overlayPath": str(overlay),
                    "pymssVersion": "2.4.0", "packageVersions": {**old_state["packageVersions"], "pymss": "2.4.0"}}
        self.active.write_text(json.dumps(selected))
        (overlay.parent.parent / "active-overlay.json").write_text(json.dumps(selected))
        self.assertEqual(self.install(activate=True), 0)

        def probe(_python, _extras, selected_overlay=None):
            result = copy.deepcopy(self.probed)
            if selected_overlay:
                self.assertEqual(selected_overlay, overlay.resolve())
                result["pymssVersion"] = "2.4.0"
                result["packageVersions"]["pymss"] = "2.4.0"
            return result

        with mock.patch.object(bootstrap, "_probe_python_runtime", side_effect=probe), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(worker.run_command("rollback_runtime", {"backend": "rocm"}), 0)
        restored = json.loads(self.active.read_text())
        self.assertEqual(restored["overlayPath"], str(overlay))
        self.assertEqual(restored["pymssVersion"], "2.4.0")

    def test_install_failure_restores_original_environment_and_pointer(self):
        with mock.patch.object(bootstrap, "_verify_runtime_audio", side_effect=RuntimeError("decoder failed")):
            self.assertEqual(self.install(activate=True), 1)
        self.assertEqual((self.python.parent.parent / "generation.txt").read_text(), "original")
        self.assertEqual(self.active.read_bytes(), self.original_pointer)
        self.assertFalse((self.envs / ".rocm-transaction.json").exists())

    def test_install_and_recovery_failure_preserve_both_errors_and_backup(self):
        with mock.patch.object(bootstrap, "_verify_runtime_audio", side_effect=RuntimeError("decoder failed")), \
             mock.patch.object(RocmTransaction, "abort", side_effect=PermissionError("locked files")), \
             mock.patch("worker_protocol.emit_error", return_value=1) as error:
            self.assertEqual(self.install(activate=True), 1)
        self.assertEqual(error.call_args.args[0], "RUNTIME_RECOVERY_FAILED")
        self.assertIn("decoder failed", error.call_args.args[1])
        self.assertIn("locked files", error.call_args.args[1])
        self.assertTrue((self.envs / ".rocm-transaction.json").is_file())
        self.assertTrue(list(self.envs.glob(".rocm-backup-*/environment/generation.txt")))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(worker.run_command("recover_runtime", {}), 0)
        self.assertEqual(self.active.read_bytes(), self.original_pointer)

    def test_gpu_probe_environment_keeps_only_selected_sdk_directories(self):
        other = self.envs / ".rocm-previous/environment/sdk/bin"
        other.mkdir(parents=True)
        regular = self.root / "bin"
        regular.mkdir()
        import os
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join([str(other), str(regular)])}):
            environment = bootstrap._probe_environment(python_path=self.python)
        self.assertEqual(environment["PATH"].split(os.pathsep), [
            str((self.python.parent.parent / "sdk/bin").resolve()), str((self.python.parent.parent / "sdk/lib").resolve()), str(regular),
        ])


if __name__ == "__main__":
    unittest.main()
