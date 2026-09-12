import builtins
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
    from .test_worker_bootstrap import MANIFEST, probe_result
else:
    import _bootstrap as _worker_test_bootstrap
    from test_worker_bootstrap import MANIFEST, probe_result

import worker_bootstrap


class RuntimeProbeReuseTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.envs = self.root / "runtime-envs"
        self.paths = {}
        for backend in ("cpu", "cuda"):
            python = self.envs / backend / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"interpreter")
            self.paths[backend] = python
            state = {
                **probe_result(backend), "backend": backend,
                "manifestVersion": "test-1", "stateVersion": worker_bootstrap.ENV_STATE_VERSION,
                "installedAt": "2026-09-01", "customField": {"preserved": True},
            }
            self.cache_path(backend).write_text(json.dumps(state), encoding="utf-8")
        self.active = {"backend": "cuda", "pythonPath": str(self.paths["cuda"])}
        self.enterContext(mock.patch.object(sys, "platform", "win32"))
        self.enterContext(mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs))
        self.enterContext(mock.patch.object(worker_bootstrap, "BUNDLED_RUNTIME_ENVS_DIR", None))
        self.enterContext(mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", self.envs / "active-runtime.json"))
        self.enterContext(mock.patch.object(worker_bootstrap, "_manifest", return_value=MANIFEST))
        self.enterContext(mock.patch.object(worker_bootstrap, "_module_available", return_value=True))
        self.enterContext(mock.patch.object(worker_bootstrap, "_detect_gpu_vendors", return_value=[]))
        self.state = self.enterContext(mock.patch.object(worker_bootstrap, "_read_runtime_state", return_value=self.active))
        self.probe = self.enterContext(mock.patch.object(worker_bootstrap, "_probe_python_runtime", return_value=probe_result("cuda")))
        self.metadata = self.enterContext(mock.patch.object(worker_bootstrap, "_probe_python_package_versions", return_value={"pymss": "9.0.0", "pymss-core": "9.0.1"}))
        self.write = self.enterContext(mock.patch.object(worker_bootstrap, "_atomic_write_json"))

    def cache_path(self, backend):
        return self.envs / backend / "pymss-runtime-state.json"

    def refresh(self, payload=None):
        before = {path: path.read_bytes() for path in self.root.rglob("*.json")}
        result = worker_bootstrap._runtime_info_payload(payload or {}, repair=False)
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*.json")}, before)
        self.write.assert_not_called()
        return result

    def test_active_core_versions_are_reused_while_idle_versions_are_refreshed(self):
        result = self.refresh()
        self.probe.assert_called_once_with(self.paths["cuda"], [])
        self.metadata.assert_called_once_with(self.paths["cpu"], ["pymss", "pymss-core"])
        active = next(item for item in result["installedEnvironments"] if item["backend"] == "cuda")
        idle = next(item for item in result["installedEnvironments"] if item["backend"] == "cpu")
        self.assertEqual(active["pymssVersion"], "1.0.0")
        self.assertEqual(idle["pymssVersion"], "9.0.0")
        self.assertEqual(active["installedAt"], "2026-09-01")
        self.assertEqual(active["customField"], {"preserved": True})

    def test_each_request_gets_a_fresh_probe_instead_of_retaining_a_global_cache(self):
        first = probe_result("cuda")
        second = copy.deepcopy(first)
        second["packageVersions"]["pymss"] = "2.1.3"
        second["pymssGraphAvailable"] = False
        self.probe.side_effect = [first, second]
        before = self.refresh()
        after = self.refresh()
        self.assertEqual(self.probe.call_count, 2)
        self.assertEqual(self.metadata.call_args_list, [mock.call(self.paths["cpu"], ["pymss", "pymss-core"])] * 2)
        self.assertEqual(before["pymssVersion"], "1.0.0")
        self.assertEqual(after["pymssVersion"], "2.1.3")
        self.assertFalse(after["pymssGraphAvailable"])

    def test_missing_cache_reuses_the_matching_full_probe_without_writing_in_read_only_mode(self):
        self.cache_path("cuda").unlink()
        result = self.refresh()
        self.assertEqual(self.probe.call_count, 1)
        self.metadata.assert_called_once_with(self.paths["cpu"], ["pymss", "pymss-core"])
        self.assertEqual(next(item for item in result["installedEnvironments"] if item["backend"] == "cuda")["health"], "ready")
        self.assertFalse(self.cache_path("cuda").exists())

    def test_old_cache_repair_uses_the_active_probe_and_keeps_read_only_storage_unchanged(self):
        path = self.cache_path("cuda")
        original = json.loads(path.read_text(encoding="utf-8"))
        original.update({"stateVersion": 1, "torchBackend": "cpu", "acceleratorAvailable": False})
        path.write_text(json.dumps(original), encoding="utf-8")
        result = self.refresh()
        self.assertEqual(self.probe.call_count, 1)
        active = next(item for item in result["installedEnvironments"] if item["backend"] == "cuda")
        self.assertTrue(active["acceleratorAvailable"])
        self.assertEqual(active["customField"], original["customField"])

    def test_discovery_does_not_persist_extras_requested_for_another_backend(self):
        self.cache_path("cuda").unlink()
        self.probe.return_value = probe_result("cuda", mlx=True)
        original = copy.deepcopy(self.probe.return_value)
        with mock.patch.object(worker_bootstrap, "_repair_runtime_venv_config", return_value=False), \
                mock.patch.object(worker_bootstrap, "_make_posix_venv_relocatable", return_value=False):
            worker_bootstrap._runtime_info_payload({"backend": "mlx"}, repair=True)
        self.probe.assert_called_once_with(self.paths["cuda"], ["mlx"])
        self.write.assert_called_once()
        path, state = self.write.call_args.args
        self.assertEqual(path, self.cache_path("cuda"))
        self.assertEqual(set(state["packages"]), set(MANIFEST["common"]))
        self.assertEqual(set(state["packageVersions"]), set(MANIFEST["common"]))
        self.assertEqual(self.probe.return_value, original)

    def test_configuration_or_link_repairs_invalidate_the_pre_repair_probe(self):
        self.cache_path("cuda").unlink()
        before_repair = probe_result("cuda")
        self.probe.return_value = probe_result("cuda")
        self.probe.return_value["packageVersions"]["pymss"] = "2.1.3"
        for repair_helper in ("_repair_runtime_venv_config", "_make_posix_venv_relocatable"):
            for failed in (False, True):
                with self.subTest(repair_helper=repair_helper, failed=failed):
                    self.probe.reset_mock()
                    self.metadata.reset_mock()
                    self.write.reset_mock()

                    def repair(env_dir):
                        if env_dir.name != "cuda":
                            return False
                        if failed:
                            raise OSError("configuration update failed")
                        return True

                    with mock.patch.object(worker_bootstrap, "_repair_runtime_venv_config", return_value=False), \
                            mock.patch.object(worker_bootstrap, "_make_posix_venv_relocatable", return_value=False), \
                            mock.patch.object(worker_bootstrap, repair_helper, side_effect=repair):
                        worker_bootstrap._installed_envs(MANIFEST, active_python=self.paths["cuda"], active_probe=before_repair)
                    self.probe.assert_called_once_with(self.paths["cuda"], [])
                    self.assertEqual(self.metadata.call_count, 2)
                    self.write.assert_called_once()
                    self.assertEqual(self.write.call_args.args[1]["packageVersions"]["pymss"], "2.1.3")

    def test_venv_config_repair_reports_only_actual_changes(self):
        cfg = self.envs / "cuda" / "pyvenv.cfg"
        cfg.write_text("home = previous-location\n", encoding="utf-8")
        with mock.patch.object(worker_bootstrap, "_bootstrap_python_path", return_value=Path(sys.executable)):
            self.assertIs(worker_bootstrap._repair_runtime_venv_config(cfg.parent), True)
            content = cfg.read_bytes()
            self.assertIs(worker_bootstrap._repair_runtime_venv_config(cfg.parent), False)
            self.assertEqual(cfg.read_bytes(), content)
            self.assertIs(worker_bootstrap._repair_runtime_venv_config(self.root / "absent"), False)

    def test_posix_link_repair_reports_replacements_but_not_matching_links(self):
        env = mock.MagicMock()
        bin_dir = env.__truediv__.return_value
        bin_dir.is_dir.return_value = True
        bootstrap = mock.Mock()
        bootstrap.resolve.return_value = bootstrap
        bootstrap.is_file.return_value = True
        paths = {}

        def child(name):
            if name not in paths:
                paths[name] = mock.Mock()
                paths[name].is_symlink.return_value = False
                paths[name].resolve.return_value = bootstrap
            return paths[name]

        bin_dir.__truediv__.side_effect = child
        with mock.patch.object(worker_bootstrap.os, "name", "posix"), \
                mock.patch.object(worker_bootstrap, "_bootstrap_python_path", return_value=bootstrap), \
                mock.patch.object(worker_bootstrap.os.path, "relpath", return_value="../bootstrap/python"), \
                mock.patch.object(worker_bootstrap.os, "replace") as replace:
            self.assertIs(worker_bootstrap._make_posix_venv_relocatable(env), True)
            self.assertEqual(replace.call_count, 3)
            replace.reset_mock()
            for path in paths.values():
                path.is_symlink.return_value = True
            self.assertIs(worker_bootstrap._make_posix_venv_relocatable(env), False)
            replace.assert_not_called()

    def test_version_reuse_requires_matching_paths_and_both_core_keys(self):
        self.probe.return_value["packageVersions"].pop("pymss-core")
        self.refresh()
        self.assertEqual(self.metadata.call_count, 2)
        self.metadata.reset_mock()
        self.probe.return_value = probe_result("cuda")
        self.state.return_value = {**self.active, "pythonPath": sys.executable}
        self.refresh()
        self.assertEqual(self.metadata.call_count, 2)

    def test_known_missing_core_metadata_is_reused_without_a_second_probe(self):
        self.probe.return_value["packageVersions"]["pymss-core"] = None
        result = self.refresh()
        self.metadata.assert_called_once_with(self.paths["cpu"], ["pymss", "pymss-core"])
        active = next(item for item in result["installedEnvironments"] if item["backend"] == "cuda")
        self.assertIsNone(active["packageVersions"]["pymss-core"])

    def test_bundled_active_environment_reuses_core_versions(self):
        bundled = self.root / "bundled-envs"
        bundled.mkdir()
        with mock.patch.object(worker_bootstrap, "BUNDLED_RUNTIME_ENVS_DIR", self.envs), \
                mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", bundled):
            result = self.refresh()
        self.assertEqual(self.probe.call_count, 1)
        self.metadata.assert_called_once_with(self.paths["cpu"], ["pymss", "pymss-core"])
        active = next(item for item in result["installedEnvironments"] if item["backend"] == "cuda")
        self.assertEqual(active["source"], "bundled")
        self.assertFalse(active["coreUpdateSupported"])

    def test_bundled_bootstrap_probe_is_reused_only_when_it_covers_the_required_extras(self):
        bundled = self.root / "bundled-envs"
        bundled.mkdir()
        pointer = bundled / "active-runtime.json"
        for backend, extras in (("cuda", []), ("mlx", ["mlx"])):
            with self.subTest(backend=backend):
                pointer.write_text(json.dumps({"backend": backend, "pythonPath": sys.executable}), encoding="utf-8")
                self.state.return_value = {"backend": backend, "pythonPath": sys.executable}
                self.probe.reset_mock()
                self.probe.side_effect = [probe_result("cuda")] if backend == "cuda" else [probe_result("cpu"), probe_result("cpu", mlx=True)]
                with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.root / "empty"), \
                        mock.patch.object(worker_bootstrap, "BUNDLED_RUNTIME_ENVS_DIR", bundled), \
                        mock.patch.object(worker_bootstrap, "_bundled_bootstrap_python", return_value=Path(sys.executable)):
                    result = self.refresh({"backend": "cpu"})
                expected = [mock.call(Path(sys.executable), [])]
                if extras:
                    expected.append(mock.call(Path(sys.executable), extras))
                self.assertEqual(self.probe.call_args_list, expected)
                self.assertEqual(len(result["installedEnvironments"]), 1)

    def test_bootstrap_uses_subprocess_torch_facts_without_importing_torch_again(self):
        self.state.return_value = None
        original_import = builtins.__import__

        def import_without_torch(name, *args, **kwargs):
            if name == "torch":
                raise AssertionError("duplicate Torch import")
            return original_import(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", side_effect=import_without_torch):
            result = self.refresh()
        self.probe.assert_called_once_with(Path(sys.executable), [])
        self.assertEqual(result["torchBackend"], "cuda")
        self.assertTrue(result["acceleratorAvailable"])
        self.assertTrue(result["ready"])


if __name__ == "__main__":
    unittest.main()
