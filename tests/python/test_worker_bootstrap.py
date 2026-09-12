import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error as urllib_error
from pathlib import Path
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap

import worker_bootstrap

COMMON_PACKAGES = ("av", "librosa", "numpy", "pymss", "pymss-core")

MANIFEST = {
    "manifestVersion": "test-1",
    "common": {name: name for name in COMMON_PACKAGES},
    "backends": {
        "cpu": {"platforms": ["win32", "linux", "darwin"]},
        "cuda": {"platforms": ["win32", "linux"]},
        "mlx": {"platforms": ["darwin"], "extras": ["mlx"]},
    },
}


def probe_result(torch_backend="cpu", mlx=None, missing=(), graph=True):
    packages = {name: name not in missing for name in COMMON_PACKAGES}
    package_versions = {name: "1.0.0" for name in COMMON_PACKAGES}
    if mlx is not None:
        packages["mlx"] = mlx
        package_versions["mlx"] = "1.0.0" if mlx else None
    return {
        "pythonVersion": "3.12.0",
        "torchVersion": "2.7.1",
        "torchBackend": torch_backend,
        "acceleratorAvailable": torch_backend in {"cuda", "rocm"},
        "packages": packages,
        "packageVersions": package_versions,
        "pymssGraphAvailable": graph,
    }




class RuntimeReadinessTests(unittest.TestCase):
    """`ready` drives whether the app considers itself usable at all, so MLX being probed
    speculatively must never drag it down."""

    def _payload(self, platform_name, probed, backend=None):
        probe = mock.Mock(return_value=probed)
        state = {"pythonPath": sys.executable, "backend": backend}
        with mock.patch.object(worker_bootstrap, "_manifest", return_value=MANIFEST), \
             mock.patch.object(worker_bootstrap, "_read_runtime_state", return_value=state), \
             mock.patch.object(worker_bootstrap, "_installed_envs", return_value=[]), \
             mock.patch.object(worker_bootstrap, "_module_available", return_value=True), \
             mock.patch.object(worker_bootstrap, "_probe_python_runtime", probe), \
             mock.patch.object(sys, "platform", platform_name):
            payload = worker_bootstrap._runtime_info_payload({"backend": backend} if backend else {})
        return payload, probe

    def test_macos_probes_mlx_even_when_no_backend_was_requested(self):
        # Without this the store cannot tell an MLX machine from a plain CPU one.
        payload, probe = self._payload("darwin", probe_result("cpu", mlx=True))
        self.assertIs(payload["packages"]["mlx"], True)
        self.assertIn("mlx", probe.call_args.args[1])
        self.assertTrue(payload["ready"])

    def test_other_platforms_do_not_probe_mlx_without_a_backend(self):
        _, probe = self._payload("win32", probe_result("cpu"))
        self.assertNotIn("mlx", probe.call_args.args[1])

    def test_absent_mlx_does_not_make_a_working_mac_look_unready(self):
        payload, _ = self._payload("darwin", probe_result("cpu", mlx=False))
        self.assertIs(payload["packages"]["mlx"], False)
        self.assertTrue(payload["ready"])

    def test_explicitly_requesting_mlx_requires_mlx(self):
        ready, _ = self._payload("darwin", probe_result("cpu", mlx=False), backend="mlx")
        self.assertFalse(ready["ready"])
        present, _ = self._payload("darwin", probe_result("cpu", mlx=True), backend="mlx")
        self.assertTrue(present["ready"])

    def test_broken_torch_is_never_ready(self):
        payload, _ = self._payload("darwin", probe_result("error:boom", mlx=True))
        self.assertFalse(payload["ready"])

    def test_graph_capability_is_reported_without_affecting_basic_readiness(self):
        payload, _ = self._payload("win32", probe_result("cpu", graph=False))
        self.assertIs(payload["pymssGraphAvailable"], False)
        self.assertTrue(payload["ready"])


class ManifestRequirementTests(unittest.TestCase):
    def test_manifest_status_requires_complete_numeric_markers(self):
        for actual, expected, status in (
            ("2026.09.1", "2026.09.1", "current"),
            (" 2026.9.1.0 ", "2026.09.1", "current"),
            ("2026.08.1", "2026.09.1", "older"),
            ("2026.10.1", "2026.09.1", "newer"),
            (None, "2026.09.1", "unknown"),
            ("2026.09.1", None, "unknown"),
            ("2026.09.1-invalid", "2026.09.1", "unknown"),
            ("legacy", "legacy", "unknown"),
        ):
            with self.subTest(actual=actual, expected=expected):
                self.assertEqual(worker_bootstrap._runtime_manifest_status(actual, expected), status)

    def test_pin_manifest_requirement_preserves_extras(self):
        self.assertEqual(
            worker_bootstrap._pin_manifest_requirement("pymss[proxy]>=2.1.3", "2.2.0"),
            "pymss[proxy]==2.2.0",
        )

    def test_pin_manifest_requirement_handles_plain_requirement(self):
        self.assertEqual(
            worker_bootstrap._pin_manifest_requirement("pymss>=2.0.15", "2.2.0"),
            "pymss==2.2.0",
        )

    def test_release_builders_read_the_runtime_manifest(self):
        root = worker_bootstrap.MANIFEST_PATH.parent.parent
        for name in ("prepare-python-runtime.ps1", "prepare-python-runtime.sh"):
            content = (root / "scripts" / name).read_text(encoding="utf-8")
            self.assertIn("runtime-manifest.json", content)
            self.assertNotIn("pymss>=2.0.15", content)
            self.assertNotIn("pysocks requests pyyaml", content)

    def test_manifest_validation_includes_the_backend_torch_requirement(self):
        manifest = {
            **MANIFEST,
            "backends": {
                "cpu": {
                    "platforms": ["win32"],
                    "torch": {"requirement": "torch==2.7.1"},
                },
            },
        }
        probed = probe_result("cpu")
        probed["torchVersion"] = "2.6.0"

        satisfied, failures = worker_bootstrap._manifest_versions_are_satisfied(probed, manifest, "cpu")

        self.assertFalse(satisfied)
        self.assertTrue(any("torch==2.6.0" in failure for failure in failures))


class MultipleEnvironmentTests(unittest.TestCase):
    """Each backend gets its own venv directory, so several environments coexist and the
    active one is just a pointer. These tests use stub interpreters — nothing is downloaded."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.envs_dir = self.root / "runtime-envs"
        self.envs_dir.mkdir()
        self.active_file = self.envs_dir / "active-runtime.json"
        self.addCleanup(shutil.rmtree, self.root, True)

    def _make_env(self, backend, torch_version):
        env_dir = self.envs_dir / backend
        (env_dir / "Scripts").mkdir(parents=True)
        (env_dir / "Scripts" / "python.exe").write_text("stub", encoding="utf-8")
        (env_dir / "pymss-runtime-state.json").write_text(json.dumps({
            "backend": backend,
            "manifestVersion": "test-1",
            "torchVersion": torch_version,
            "torchBackend": backend,
            "acceleratorAvailable": backend != "cpu",
            "packages": {name: True for name in COMMON_PACKAGES},
        }), encoding="utf-8")

    @contextlib.contextmanager
    def _runtime(self, bootstrap_torch_backend="missing"):
        # The bootstrap-python fallback probes whatever interpreter runs the tests, so pin it:
        # on a dev machine with CUDA torch installed it would otherwise satisfy activation
        # requests for backends that have no managed environment at all.
        probed = probe_result(bootstrap_torch_backend)
        # Worker commands write JSON events to stdout; swallow them so test output stays readable.
        def probe_runtime(python_path, _extras=None):
            path = str(python_path).lower()
            if "cuda" in path:
                return probe_result("cuda")
            if "cpu" in path:
                return probe_result("cpu")
            return probed

        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir), \
             mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", self.active_file), \
             mock.patch.object(worker_bootstrap, "_manifest", return_value=MANIFEST), \
             mock.patch.object(worker_bootstrap, "_probe_python_runtime", side_effect=probe_runtime), \
             mock.patch.object(sys, "platform", "win32"), \
             contextlib.redirect_stdout(io.StringIO()):
            yield

    def _active(self):
        return json.loads(self.active_file.read_text(encoding="utf-8"))

    def test_environments_for_different_backends_coexist(self):
        self._make_env("cpu", "2.7.1")
        self._make_env("cuda", "2.7.1+cu128")
        with self._runtime():
            items = worker_bootstrap._installed_envs(MANIFEST)
        self.assertEqual(
            sorted(item["backend"] for item in items),
            ["cpu", "cuda"],
        )
        # Each keeps its own torch build — installing one must not overwrite the other.
        versions = {item["backend"]: item["torchVersion"] for item in items}
        self.assertEqual(versions, {"cpu": "2.7.1", "cuda": "2.7.1+cu128"})

    def test_environment_is_discovered_when_state_cache_is_missing(self):
        self._make_env("cpu", "2.7.1")
        state_path = self.envs_dir / "cpu" / "pymss-runtime-state.json"
        state_path.unlink()
        with self._runtime():
            items = worker_bootstrap._installed_envs(MANIFEST)
        self.assertEqual([item["backend"] for item in items], ["cpu"])
        self.assertTrue(state_path.is_file())
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["torchBackend"], "cpu")

    def test_environment_without_state_is_not_listed_when_probe_fails(self):
        self._make_env("cpu", "2.7.1")
        (self.envs_dir / "cpu" / "pymss-runtime-state.json").unlink()
        with self._runtime():
            with mock.patch.object(
                worker_bootstrap,
                "_probe_python_runtime",
                return_value=probe_result("cpu", missing=COMMON_PACKAGES),
            ):
                items = worker_bootstrap._installed_envs(MANIFEST)
        self.assertEqual(items, [])

    def test_activation_repoints_at_the_selected_environment(self):
        self._make_env("cpu", "2.7.1")
        self._make_env("cuda", "2.7.1+cu128")
        with self._runtime():
            self.assertEqual(worker_bootstrap.cmd_activate_runtime({"backend": "cuda"}), 0)
            after_cuda = self._active()
            self.assertEqual(worker_bootstrap.cmd_activate_runtime({"backend": "cpu"}), 0)
            after_cpu = self._active()
        self.assertEqual(after_cuda["backend"], "cuda")
        self.assertTrue(after_cuda["pythonPath"].endswith("cuda\\Scripts\\python.exe"))
        self.assertEqual(after_cpu["backend"], "cpu")
        self.assertTrue(after_cpu["pythonPath"].endswith("cpu\\Scripts\\python.exe"))
        # Switching back and forth must not damage either environment.
        with self._runtime():
            self.assertEqual(len(worker_bootstrap._installed_envs(MANIFEST)), 2)

    def test_missing_active_interpreter_is_cleared_before_startup_selection(self):
        self._make_env("cpu", "2.7.1")
        self.active_file.write_text(json.dumps({
            "backend": "cuda",
            "pythonPath": str(self.envs_dir / "cuda" / "Scripts" / "python.exe"),
        }), encoding="utf-8")
        with self._runtime(), \
             mock.patch.object(worker_bootstrap, "_module_available", return_value=False), \
             mock.patch.object(worker_bootstrap, "_detect_gpu_vendors", return_value=[]):
            info = worker_bootstrap._runtime_info_payload({})
            self.assertIsNone(info["installedBackend"])
            self.assertIsNone(info["installState"])
            self.assertFalse(info["ready"])
            candidates = [env for env in info["installedEnvironments"] if env["health"] != "broken"]
            self.assertEqual(len(candidates), 1)
            self.assertEqual(worker_bootstrap.cmd_activate_runtime({
                "backend": candidates[0]["backend"],
                "pythonPath": candidates[0]["pythonPath"],
                "onlyIfNoActive": True,
            }), 0)
        self.assertEqual(self._active()["backend"], "cpu")

    def test_pointer_reader_ignores_invalid_shapes_and_non_file_paths(self):
        self._make_env("cpu", "2.7.1")
        records = (
            [],
            {"backend": "cpu", "pythonPath": ["cpu", "Scripts", "python.exe"]},
            {"backend": "cpu", "pythonPath": "cpu/Scripts"},
            {"backend": "cpu", "pythonPath": "cpu/Scripts/missing.exe"},
            {"backend": "cpu", "pythonPath": str(self.envs_dir / "cpu" / "Scripts")},
        )
        for record in records:
            with self.subTest(record=record):
                self.active_file.write_text(json.dumps(record), encoding="utf-8")
                self.assertIsNone(worker_bootstrap._resolve_runtime_state_from(self.active_file))

    def test_startup_managed_activation_preserves_a_pointer_created_during_probe(self):
        self._make_env("cpu", "2.7.1")
        self._make_env("cuda", "2.7.1+cu128")
        cuda_python = self.envs_dir / "cuda" / "Scripts" / "python.exe"

        def probe_then_user_activation(_python_path, _extras=None):
            self.active_file.write_text(json.dumps({
                "backend": "cuda",
                "pythonPath": str(cuda_python),
            }), encoding="utf-8")
            return probe_result("cpu")

        with self._runtime():
            with mock.patch.object(worker_bootstrap, "_probe_python_runtime", side_effect=probe_then_user_activation):
                result = worker_bootstrap.cmd_activate_runtime({
                    "backend": "cpu",
                    "onlyIfNoActive": True,
                })

        self.assertEqual(result, 0)
        self.assertEqual(self._active()["backend"], "cuda")

    def test_startup_activation_replaces_an_invalid_existing_pointer(self):
        self._make_env("cpu", "2.7.1")
        cpu_python = self.envs_dir / "cpu" / "Scripts" / "python.exe"
        invalid_pointers = (
            "{not-json",
            json.dumps({
                "backend": "cpu",
                "pythonPath": str(self.envs_dir / "missing" / "Scripts" / "python.exe"),
            }),
        )

        for pointer in invalid_pointers:
            self.active_file.write_text(pointer, encoding="utf-8")
            with self._runtime():
                result = worker_bootstrap.cmd_activate_runtime({
                    "backend": "cpu",
                    "pythonPath": str(cpu_python),
                    "onlyIfNoActive": True,
                })

            self.assertEqual(result, 0)
            self.assertEqual(self._active()["backend"], "cpu")
            self.active_file.unlink()

    def test_activation_rejects_a_runtime_that_fails_probe(self):
        self._make_env("cuda", "2.7.1+cu128")
        with self._runtime():
            with mock.patch.object(worker_bootstrap, "_probe_python_runtime", return_value=probe_result("error: [WINERROR 1455]")):
                self.assertNotEqual(worker_bootstrap.cmd_activate_runtime({"backend": "cuda"}), 0)
        self.assertFalse(self.active_file.exists())

    def test_activating_a_missing_backend_is_rejected(self):
        self._make_env("cpu", "2.7.1")
        with self._runtime():
            self.assertNotEqual(worker_bootstrap.cmd_activate_runtime({"backend": "cuda"}), 0)
        self.assertFalse(self.active_file.exists())

    def test_deleting_the_active_environment_is_refused(self):
        self._make_env("cpu", "2.7.1")
        self._make_env("cuda", "2.7.1+cu128")
        with self._runtime():
            worker_bootstrap.cmd_activate_runtime({"backend": "cuda"})
            self.assertNotEqual(worker_bootstrap.cmd_delete_runtime({"backend": "cuda"}), 0)
        self.assertTrue((self.envs_dir / "cuda").is_dir())
        self.assertEqual(self._active()["backend"], "cuda")

    def test_deleting_an_idle_environment_leaves_the_active_one_alone(self):
        self._make_env("cpu", "2.7.1")
        self._make_env("cuda", "2.7.1+cu128")
        with self._runtime():
            worker_bootstrap.cmd_activate_runtime({"backend": "cuda"})
            self.assertEqual(worker_bootstrap.cmd_delete_runtime({"backend": "cpu"}), 0)
            remaining = worker_bootstrap._installed_envs(MANIFEST)
        self.assertEqual([item["backend"] for item in remaining], ["cuda"])
        self.assertFalse((self.envs_dir / "cpu").exists())
        self.assertEqual(self._active()["backend"], "cuda")


class RuntimePathSafetyTests(unittest.TestCase):
    """Runtime state and explicit targets must stay inside the configured runtime directory."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.envs_dir = self.root / "runtime-envs"
        self.envs_dir.mkdir()
        self.addCleanup(shutil.rmtree, self.root, True)

    def _make_env(self, backend):
        env_dir = self.envs_dir / backend
        (env_dir / "Scripts").mkdir(parents=True)
        python_path = env_dir / "Scripts" / "python.exe"
        python_path.write_text("stub", encoding="utf-8")
        (env_dir / "pymss-runtime-state.json").write_text(json.dumps({
            "backend": backend,
            "manifestVersion": "test-1",
            "torchVersion": "2.7.1+cu128" if backend == "cuda" else "2.7.1",
            "torchBackend": backend,
            "acceleratorAvailable": backend != "cpu",
            "packages": {name: True for name in COMMON_PACKAGES},
            "source": "legacy",
        }), encoding="utf-8")
        return python_path

    def test_runtime_venv_path_removes_windows_drive_long_path_prefix(self):
        path = Path(r"\\?\D:\workspace\runtime-envs\cuda")
        self.assertEqual(
            worker_bootstrap._normal_runtime_path(path),
            r"D:\workspace\runtime-envs\cuda",
        )

    def test_runtime_venv_path_converts_windows_unc_long_path_prefix(self):
        path = Path(r"\\?\UNC\server\share\runtime-envs\cuda")
        self.assertEqual(
            worker_bootstrap._normal_runtime_path(path),
            r"\\server\share\runtime-envs\cuda",
        )

    def test_runtime_venv_path_keeps_an_ordinary_path(self):
        path = Path("runtime-envs") / "cpu"
        self.assertEqual(worker_bootstrap._normal_runtime_path(path), os.fspath(path))

    def test_runtime_python_path_drops_extended_prefix_for_short_windows_paths(self):
        # Rust may pass the canonicalized runtime root with ``\\\\?\\``.  The interpreter
        # command must use a regular Win32 path so pip does not derive ``..\\Scripts`` from an
        # extended site-packages path (which raises Errno 22 while installing entry points).
        extended_root = Path(r"\\?\D:\workspace\runtime-envs")
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", extended_root), \
             mock.patch.object(worker_bootstrap.os, "name", "nt"), \
             mock.patch.object(sys, "platform", "win32"):
            path = worker_bootstrap._env_python_path("cuda")

        value = str(path)
        self.assertNotIn("\\\\?\\", value)
        self.assertTrue(value.replace("\\", "/").endswith("cuda/Scripts/python.exe"))

    @unittest.skipUnless(os.name == "nt", "Windows extended paths are not available on POSIX")
    def test_active_state_with_an_extended_python_path_is_recognized(self):
        python_path = self._make_env("cuda")
        active = self.envs_dir / "active-runtime.json"
        extended = "\\\\?\\" + str(python_path.resolve())
        active.write_text(json.dumps({
            "backend": "cuda",
            "pythonPath": extended,
        }), encoding="utf-8")
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir), \
             mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", active), \
             mock.patch.object(sys, "platform", "win32"):
            state = worker_bootstrap._read_runtime_state()
        self.assertIsNotNone(state)
        self.assertEqual(state["backend"], "cuda")
        self.assertNotIn("\\\\?\\", state["pythonPath"])

    @unittest.skipUnless(os.name == "nt", "Windows extended paths are not available on POSIX")
    def test_explicit_target_accepts_normal_path_when_runtime_root_is_extended(self):
        python_path = self._make_env("cuda")
        extended_root = Path("\\\\?\\" + str(self.envs_dir.resolve()))
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", extended_root), \
             mock.patch.object(sys, "platform", "win32"):
            target = worker_bootstrap._target_runtime_from_payload({"pythonPath": str(python_path)}, "cuda")
        self.assertIsNotNone(target)

    def test_active_state_is_read_only_from_the_runtime_directory(self):
        python_path = self._make_env("cuda")
        active = self.envs_dir / "active-runtime.json"
        active.write_text(json.dumps({
            "backend": "cuda",
            "pythonPath": str(python_path),
            "source": "legacy",
        }), encoding="utf-8")
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir), \
             mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", active):
            state = worker_bootstrap._read_runtime_state()
        self.assertEqual(state["backend"], "cuda")
        self.assertNotIn("source", state)

    def test_non_user_runtime_paths_are_rejected(self):
        outside = self.root / "outside" / "Scripts"
        outside.mkdir(parents=True)
        python_path = outside / "python.exe"
        python_path.write_text("stub", encoding="utf-8")
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir), \
             mock.patch.object(sys, "platform", "win32"):
            self.assertIsNone(worker_bootstrap._target_runtime_from_payload({"pythonPath": str(python_path)}, "cuda"))

    def test_explicit_runtime_path_must_match_the_requested_backend(self):
        python_path = self._make_env("cuda")
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir):
            self.assertIsNone(worker_bootstrap._target_runtime_from_payload({"pythonPath": str(python_path)}, "cpu"))

    def test_unknown_backend_is_rejected_before_path_resolution(self):
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir):
            self.assertIsNone(worker_bootstrap._target_runtime_from_payload({"pythonPath": str(self.envs_dir / "../outside" / "bin" / "python")}, "../outside"))

    def test_environment_state_does_not_expose_a_source(self):
        self._make_env("cuda")
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir), \
             mock.patch.object(worker_bootstrap, "_manifest", return_value=MANIFEST), \
             mock.patch.object(worker_bootstrap, "_probe_python_runtime", return_value=probe_result("cuda")), \
             mock.patch.object(sys, "platform", "win32"):
            entries = worker_bootstrap._installed_envs(MANIFEST)
        self.assertEqual(len(entries), 1)
        self.assertNotIn("source", entries[0])

    def test_active_state_is_required_for_runtime_targets_without_a_backend_fallback(self):
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir), \
             mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", self.envs_dir / "active-runtime.json"):
            self.assertIsNone(worker_bootstrap._target_runtime_from_payload({}, "cpu"))

    def test_active_state_outside_the_user_runtime_root_is_ignored(self):
        outside = self.root / "outside" / "Scripts"
        outside.mkdir(parents=True)
        python_path = outside / "python.exe"
        python_path.write_text("stub", encoding="utf-8")
        active = self.envs_dir / "active-runtime.json"
        active.write_text(json.dumps({"backend": "cuda", "pythonPath": str(python_path)}), encoding="utf-8")
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir), \
             mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", active):
            self.assertIsNone(worker_bootstrap._read_runtime_state())

    def test_interrupted_reinstall_backup_restores_when_target_is_incomplete(self):
        backup = self.envs_dir / ".cuda.reinstalling"
        (backup / "Scripts").mkdir(parents=True)
        (backup / "Scripts" / "python.exe").write_text("stub", encoding="utf-8")
        (backup / "pymss-runtime-state.json").write_text(json.dumps({"backend": "cuda"}), encoding="utf-8")
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir), \
             mock.patch.object(sys, "platform", "win32"):
            worker_bootstrap._recover_reinstall_backups()
        self.assertTrue((self.envs_dir / "cuda" / "Scripts" / "python.exe").is_file())
        self.assertFalse(backup.exists())


class BundledRuntimeFallbackTests(unittest.TestCase):
    """Packaged runtimes remain usable when user-managed runtime state is absent."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.user_envs = self.root / "user" / "runtime-envs"
        self.bundled_envs = self.root / "package" / "python-runtime" / "runtime-envs"
        self.bootstrap_python = self.root / "package" / "python-runtime" / "bin" / "python3"
        self.user_envs.mkdir(parents=True)
        self.bundled_envs.mkdir(parents=True)
        self.bootstrap_python.parent.mkdir(parents=True)
        self.bootstrap_python.write_text("stub", encoding="utf-8")
        (self.bundled_envs / "active-runtime.json").write_text(json.dumps({
            "backend": "mlx",
            "pythonPath": "../bin/python3",
        }), encoding="utf-8")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_packaged_mlx_is_used_when_user_active_state_is_missing(self):
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.user_envs), \
             mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", self.user_envs / "active-runtime.json"), \
             mock.patch.object(worker_bootstrap, "BUNDLED_RUNTIME_ENVS_DIR", self.bundled_envs), \
             mock.patch.object(sys, "platform", "darwin"):
            state = worker_bootstrap._read_runtime_state()
            target = worker_bootstrap._target_runtime_from_payload({}, "mlx")

        self.assertEqual(state["backend"], "mlx")
        self.assertEqual(state["source"], "bundled")
        self.assertEqual(Path(state["pythonPath"]).resolve(), self.bootstrap_python.resolve())
        self.assertIsNotNone(target)
        # Windows may spell the same temporary directory through its 8.3 alias
        # after resolving the relative bundled-runtime path.
        self.assertEqual(target[3].resolve(), self.bootstrap_python.resolve())

    def test_packaged_mlx_remains_listed_when_user_cpu_runtime_is_active(self):
        cpu_python = self.user_envs / "cpu" / "bin" / "python"
        cpu_python.parent.mkdir(parents=True)
        cpu_python.write_text("stub", encoding="utf-8")
        (self.user_envs / "cpu" / "pymss-runtime-state.json").write_text(json.dumps({
            "backend": "cpu",
            "packages": {name: True for name in COMMON_PACKAGES},
        }), encoding="utf-8")
        (self.user_envs / "active-runtime.json").write_text(json.dumps({
            "backend": "cpu",
            "pythonPath": str(cpu_python),
        }), encoding="utf-8")
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.user_envs), \
             mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", self.user_envs / "active-runtime.json"), \
             mock.patch.object(worker_bootstrap, "BUNDLED_RUNTIME_ENVS_DIR", self.bundled_envs), \
             mock.patch.object(worker_bootstrap, "_probe_python_runtime", return_value=probe_result("cpu", mlx=True)), \
             mock.patch.object(sys, "platform", "darwin"):
            items = worker_bootstrap._installed_envs(MANIFEST)
        self.assertEqual(sorted(item["backend"] for item in items), ["cpu", "mlx"])

    def test_packaged_environment_is_discovered_without_state_cache(self):
        cpu_python = self.bundled_envs / "cpu" / "Scripts" / "python.exe"
        cpu_python.parent.mkdir(parents=True)
        cpu_python.write_text("stub", encoding="utf-8")
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.user_envs), \
             mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", self.user_envs / "active-runtime.json"), \
             mock.patch.object(worker_bootstrap, "BUNDLED_RUNTIME_ENVS_DIR", self.bundled_envs), \
             mock.patch.object(worker_bootstrap, "_probe_python_runtime", return_value=probe_result("cpu")), \
             mock.patch.object(sys, "platform", "win32"):
            items = worker_bootstrap._installed_envs(MANIFEST)
        self.assertEqual([item["backend"] for item in items], ["cpu"])
        self.assertEqual(items[0]["source"], "bundled")

    def test_activating_bundled_mlx_clears_user_pointer(self):
        cpu_python = self.user_envs / "cpu" / "bin" / "python"
        cpu_python.parent.mkdir(parents=True)
        cpu_python.write_text("stub", encoding="utf-8")
        (self.user_envs / "active-runtime.json").write_text(json.dumps({
            "backend": "cpu",
            "pythonPath": str(cpu_python),
        }), encoding="utf-8")
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.user_envs), \
             mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", self.user_envs / "active-runtime.json"), \
             mock.patch.object(worker_bootstrap, "BUNDLED_RUNTIME_ENVS_DIR", self.bundled_envs), \
             mock.patch.object(worker_bootstrap, "_manifest", return_value=MANIFEST), \
             mock.patch.object(worker_bootstrap, "_probe_python_runtime", return_value=probe_result("cpu", mlx=True)), \
             mock.patch.object(sys, "platform", "darwin"), \
             contextlib.redirect_stdout(io.StringIO()):
            result = worker_bootstrap.cmd_activate_runtime({
                "backend": "mlx",
                "pythonPath": str(self.bootstrap_python),
            })
        self.assertEqual(result, 0)
        self.assertFalse((self.user_envs / "active-runtime.json").exists())

    def test_startup_bundled_activation_preserves_a_pointer_created_after_probe(self):
        cpu_python = self.user_envs / "cpu" / "bin" / "python"
        cpu_python.parent.mkdir(parents=True)
        cpu_python.write_text("stub", encoding="utf-8")
        active = self.user_envs / "active-runtime.json"
        original_target = worker_bootstrap._target_runtime_from_payload

        def target_then_user_activation(payload, backend):
            target = original_target(payload, backend)
            active.write_text(json.dumps({
                "backend": "cpu",
                "pythonPath": str(cpu_python),
            }), encoding="utf-8")
            return target

        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.user_envs), \
             mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", active), \
             mock.patch.object(worker_bootstrap, "BUNDLED_RUNTIME_ENVS_DIR", self.bundled_envs), \
             mock.patch.object(worker_bootstrap, "_manifest", return_value=MANIFEST), \
             mock.patch.object(worker_bootstrap, "_probe_python_runtime", return_value=probe_result("cpu", mlx=True)), \
             mock.patch.object(worker_bootstrap, "_target_runtime_from_payload", side_effect=target_then_user_activation), \
             mock.patch.object(sys, "platform", "darwin"), \
             contextlib.redirect_stdout(io.StringIO()):
            result = worker_bootstrap.cmd_activate_runtime({
                "backend": "mlx",
                "pythonPath": str(self.bootstrap_python),
                "onlyIfNoActive": True,
            })
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(active.read_text(encoding="utf-8"))["backend"], "cpu")


class RuntimeVenvRepairTests(unittest.TestCase):
    def test_posix_venv_home_uses_the_bootstrap_bin_directory(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        bootstrap = root / "python-runtime" / "bin" / "python3"
        env_dir = root / "runtime-envs" / "cpu"
        bootstrap.parent.mkdir(parents=True)
        bootstrap.write_text("stub", encoding="utf-8")
        env_dir.mkdir(parents=True)
        (env_dir / "pyvenv.cfg").write_text("home = old\n", encoding="utf-8")

        with mock.patch.object(worker_bootstrap, "_bootstrap_python_path", return_value=bootstrap):
            worker_bootstrap._repair_runtime_venv_config(env_dir)

        content = (env_dir / "pyvenv.cfg").read_text(encoding="utf-8")
        self.assertIn(f"home = {bootstrap.parent}", content)
        self.assertIn(f"executable = {bootstrap}", content)


class PackageImportNameTests(unittest.TestCase):
    """Package availability is decided with importlib, so every manifest package needs an
    importable name. A dashed distribution name that nobody mapped can never be found, and the
    environment would read as permanently incomplete with no hint as to why."""

    def _manifest_packages(self):
        # The real manifest, not the test stub: this is a guard on the shipped dependency list.
        manifest = json.loads(worker_bootstrap.MANIFEST_PATH.read_text(encoding="utf-8"))
        extras = [name for spec in manifest["backends"].values() for name in spec.get("extras", [])]
        return [*manifest["common"], *extras]

    def test_every_manifest_package_resolves_to_an_importable_name(self):
        unimportable = [
            name for name in self._manifest_packages()
            if not worker_bootstrap.PACKAGE_IMPORT_NAMES.get(name, name).isidentifier()
        ]
        self.assertEqual(unimportable, [], f"add these to PACKAGE_IMPORT_NAMES: {unimportable}")

    def test_the_mapping_carries_no_entries_for_packages_that_are_gone(self):
        # A stale entry is harmless but hides that the dependency was dropped.
        packages = set(self._manifest_packages())
        stale = sorted(set(worker_bootstrap.PACKAGE_IMPORT_NAMES) - packages)
        self.assertEqual(stale, [], f"PACKAGE_IMPORT_NAMES maps packages no longer in the manifest: {stale}")

    def test_the_probe_script_shares_the_same_mapping(self):
        # Two detection paths exist (in-process and the probe subprocess). They must agree, or an
        # environment's package list changes depending on which one answered.
        captured = {}

        def fake_check_output(command, **kwargs):
            del kwargs
            captured["script"] = command[2]
            return json.dumps(probe_result())

        with mock.patch.object(worker_bootstrap, "_manifest", return_value=MANIFEST), \
             mock.patch.object(worker_bootstrap.subprocess, "check_output", fake_check_output):
            worker_bootstrap._probe_python_runtime(Path("python"))
        self.assertIn(json.dumps(worker_bootstrap.PACKAGE_IMPORT_NAMES), captured["script"])


class EnvironmentStateTrustTests(unittest.TestCase):
    """An environment's recorded torch build must describe that environment. Installs used to
    record whatever the *active* runtime reported, so a CPU env installed while CUDA was active
    claimed "2.7.1+cu128" and an available accelerator — and the UI showed exactly that."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.envs_dir = self.root / "runtime-envs"
        self.envs_dir.mkdir()
        self.addCleanup(shutil.rmtree, self.root, True)

    def _write_state(self, backend, **overrides):
        env_dir = self.envs_dir / backend
        (env_dir / "Scripts").mkdir(parents=True)
        (env_dir / "Scripts" / "python.exe").write_text("stub", encoding="utf-8")
        state = {
            "backend": backend,
            "manifestVersion": "test-1",
            "torchVersion": "2.7.1",
            "torchBackend": backend,
            "acceleratorAvailable": False,
            "packages": {name: True for name in COMMON_PACKAGES},
            **overrides,
        }
        (env_dir / "pymss-runtime-state.json").write_text(json.dumps(state), encoding="utf-8")

    def _stored(self, backend):
        return json.loads((self.envs_dir / backend / "pymss-runtime-state.json").read_text(encoding="utf-8"))

    @contextlib.contextmanager
    def _runtime(self, probe):
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir), \
             mock.patch.object(worker_bootstrap, "_manifest", return_value=MANIFEST), \
             mock.patch.object(worker_bootstrap, "_probe_python_runtime", probe), \
             mock.patch.object(sys, "platform", "win32"):
            yield

    def test_a_foreign_torch_build_is_reprobed_and_the_file_corrected(self):
        self._write_state("cpu", torchVersion="2.7.1+cu128", torchBackend="cuda", acceleratorAvailable=True)
        probe = mock.Mock(return_value=probe_result("cpu"))
        with self._runtime(probe):
            state = worker_bootstrap._read_installed_env_state("cpu")
        self.assertEqual(state["torchBackend"], "cpu")
        self.assertIs(state["acceleratorAvailable"], False)
        # Probed the environment's own interpreter, not the active runtime's.
        self.assertTrue(str(probe.call_args.args[0]).endswith("cpu\\Scripts\\python.exe"))
        # Corrected on disk, so the cost is paid once.
        self.assertEqual(self._stored("cpu")["torchBackend"], "cpu")

    def test_a_corrected_state_is_not_reprobed(self):
        self._write_state("cpu", torchVersion="2.7.1+cu128", torchBackend="cuda", acceleratorAvailable=True)
        probe = mock.Mock(return_value=probe_result("cpu"))
        with self._runtime(probe):
            worker_bootstrap._read_installed_env_state("cpu")
            worker_bootstrap._read_installed_env_state("cpu")
        self.assertEqual(probe.call_count, 1)

    def test_a_consistent_state_is_never_reprobed(self):
        self._write_state("cuda", torchVersion="2.7.1+cu128", torchBackend="cuda", acceleratorAvailable=True)
        probe = mock.Mock(side_effect=AssertionError("must not probe"))
        with self._runtime(probe):
            state = worker_bootstrap._read_installed_env_state("cuda")
        self.assertEqual(state["torchVersion"], "2.7.1+cu128")

    def test_mlx_recording_a_cpu_torch_build_is_not_treated_as_corrupt(self):
        # MLX environments legitimately ship a CPU torch build alongside mlx.
        self._write_state("mlx", torchBackend="cpu")
        probe = mock.Mock(side_effect=AssertionError("must not probe"))
        with self._runtime(probe):
            self.assertEqual(worker_bootstrap._read_installed_env_state("mlx")["torchBackend"], "cpu")

    def test_an_unprobeable_environment_drops_the_claim_instead_of_repeating_it(self):
        self._write_state("cpu", torchVersion="2.7.1+cu128", torchBackend="cuda", acceleratorAvailable=True)
        probe = mock.Mock(side_effect=OSError("interpreter is gone"))
        with self._runtime(probe):
            state = worker_bootstrap._read_installed_env_state("cpu")
        self.assertIsNone(state["torchVersion"])
        self.assertIs(state["acceleratorAvailable"], False)
        # The file keeps its old contents so a transient failure is retried, not made permanent.
        self.assertEqual(self._stored("cpu")["torchBackend"], "cuda")

    def test_installed_environments_report_the_corrected_facts(self):
        self._write_state("cpu", torchVersion="2.7.1+cu128", torchBackend="cuda", acceleratorAvailable=True)
        with self._runtime(mock.Mock(return_value=probe_result("cpu"))), \
             mock.patch.object(worker_bootstrap, "_env_python_path", wraps=worker_bootstrap._env_python_path):
            items = worker_bootstrap._installed_envs(MANIFEST)
        self.assertEqual([(i["backend"], i["torchVersion"], i["acceleratorAvailable"]) for i in items],
                         [("cpu", "2.7.1", False)])


class ActiveEnvironmentReportsLiveFactsTests(unittest.TestCase):
    """A shipped environment records its state on the build machine, which has no GPU, so a
    packaged CUDA environment claims acceleratorAvailable=false forever. The active
    environment is probed live on every runtime_info, so that answer must win."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.envs_dir = self.root / "runtime-envs"
        self.envs_dir.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.env_python = self.envs_dir / "cuda" / "Scripts" / "python.exe"
        self.env_python.parent.mkdir(parents=True)
        self.env_python.write_text("stub", encoding="utf-8")
        (self.envs_dir / "cuda" / "pymss-runtime-state.json").write_text(json.dumps({
            "backend": "cuda",
            "manifestVersion": "test-1",
            "torchVersion": "2.7.1+cu128",
            "torchBackend": "cuda",
            # What a GPU-less CI runner records.
            "acceleratorAvailable": False,
            "packages": {name: True for name in COMMON_PACKAGES},
        }), encoding="utf-8")

    def _payload(self, active_python):
        active = self.envs_dir / "active-runtime.json"
        active.write_text(json.dumps({"backend": "cuda", "pythonPath": str(active_python)}), encoding="utf-8")
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir), \
             mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", active), \
             mock.patch.object(worker_bootstrap, "_manifest", return_value=MANIFEST), \
             mock.patch.object(worker_bootstrap, "_module_available", return_value=True), \
             mock.patch.object(worker_bootstrap, "_probe_python_runtime", return_value=probe_result("cuda")), \
             mock.patch.object(sys, "platform", "win32"):
            return worker_bootstrap._runtime_info_payload({})

    def test_the_active_environment_reports_the_probed_accelerator(self):
        entry = next(e for e in self._payload(self.env_python)["installedEnvironments"] if e["backend"] == "cuda")
        self.assertIs(entry["acceleratorAvailable"], True)
        self.assertEqual(entry["torchVersion"], "2.7.1")

    def test_an_idle_environment_keeps_its_recorded_state(self):
        # The active runtime is some other interpreter, so the cuda card must not borrow its facts.
        other = self.root / "other" / "python.exe"
        other.parent.mkdir(parents=True)
        other.write_text("stub", encoding="utf-8")
        entry = next(e for e in self._payload(other)["installedEnvironments"] if e["backend"] == "cuda")
        self.assertIs(entry["acceleratorAvailable"], False)
        self.assertEqual(entry["torchVersion"], "2.7.1+cu128")

    def test_paths_differing_only_in_separators_still_match(self):
        entry = next(e for e in self._payload(str(self.env_python).replace("\\", "/"))["installedEnvironments"]
                     if e["backend"] == "cuda")
        self.assertIs(entry["acceleratorAvailable"], True)

    def test_an_unprobeable_active_runtime_leaves_the_recording_alone(self):
        active = self.envs_dir / "active-runtime.json"
        active.write_text(json.dumps({"backend": "cuda", "pythonPath": str(self.env_python)}), encoding="utf-8")
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir), \
             mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", active), \
             mock.patch.object(worker_bootstrap, "_manifest", return_value=MANIFEST), \
             mock.patch.object(worker_bootstrap, "_module_available", return_value=True), \
             mock.patch.object(worker_bootstrap, "_probe_python_runtime", side_effect=OSError("boom")), \
             mock.patch.object(sys, "platform", "win32"):
            payload = worker_bootstrap._runtime_info_payload({})
        entry = next(e for e in payload["installedEnvironments"] if e["backend"] == "cuda")
        self.assertEqual(entry["torchVersion"], "2.7.1+cu128")


class InstallRecordsTheNewEnvironmentTests(unittest.TestCase):
    """Regression guard for the root cause: the install used _runtime_info_payload(), which
    probes the *active* runtime — still the previous environment at that point."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.envs_dir = self.root / "runtime-envs"
        self.active_file = self.envs_dir / "active-runtime.json"
        self.addCleanup(shutil.rmtree, self.root, True)
        # A CUDA runtime is active while a CPU environment gets installed.
        self.active_python = self.root / "system" / "python.exe"
        self.active_python.parent.mkdir(parents=True)
        self.active_python.write_text("stub", encoding="utf-8")
        self.envs_dir.mkdir(parents=True)
        self.active_file.write_text(json.dumps({
            "backend": "cuda", "pythonPath": str(self.active_python),
        }), encoding="utf-8")
        # Pre-create the target venv so the install skips venv.EnvBuilder.
        (self.envs_dir / "cpu" / "Scripts").mkdir(parents=True)
        (self.envs_dir / "cpu" / "Scripts" / "python.exe").write_text("stub", encoding="utf-8")

    def _probe(self, python_path, extras=None):
        del extras
        if Path(python_path) == self.active_python:
            return {**probe_result("cuda"), "torchVersion": "2.7.1+cu128"}
        probed = {**probe_result("cpu"), "torchVersion": "2.7.1+cpu"}
        probed["packageVersions"] = {
            **(probed.get("packageVersions") or {}),
            "pymss-core": "0.1.6",
        }
        return probed

    @staticmethod
    def _create_stub_venv(env_dir):
        python_path = Path(env_dir) / "Scripts" / "python.exe"
        python_path.parent.mkdir(parents=True, exist_ok=True)
        python_path.write_text("new", encoding="utf-8")

    def test_the_recorded_state_describes_the_installed_environment(self):
        manifest = {
            **MANIFEST,
            "common": {**MANIFEST["common"], "pymss-core": "pymss-core==0.1.6"},
            "backends": {"cpu": {"platforms": ["win32"], "torch": {"requirement": "torch==2.7.1"}}},
        }
        pip = mock.Mock(return_value=mock.Mock(stdout=iter(()), wait=mock.Mock(return_value=0), returncode=0))
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir), \
             mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", self.active_file), \
             mock.patch.object(worker_bootstrap, "_manifest", return_value=manifest), \
             mock.patch.object(worker_bootstrap, "_probe_python_runtime", side_effect=self._probe), \
             mock.patch.object(worker_bootstrap, "_create_runtime_venv", side_effect=self._create_stub_venv), \
             mock.patch.object(worker_bootstrap.subprocess, "run", return_value=mock.Mock(returncode=0)), \
             mock.patch.object(worker_bootstrap.subprocess, "Popen", pip), \
             mock.patch.object(sys, "platform", "win32"), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(worker_bootstrap.cmd_install_runtime({"backend": "cpu", "mirror": "pypi"}), 0)
        state = json.loads((self.envs_dir / "cpu" / "pymss-runtime-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["torchVersion"], "2.7.1+cpu")
        self.assertEqual(state["torchBackend"], "cpu")
        self.assertIs(state["acceleratorAvailable"], False)
        self.assertEqual(state["stateVersion"], worker_bootstrap.ENV_STATE_VERSION)

    def test_missing_pip_is_bootstrapped_before_installing(self):
        calls = []
        logs = []
        responses = [
            mock.Mock(returncode=1, stdout=""),
            mock.Mock(returncode=0, stdout="pip bootstrapped\n"),
            mock.Mock(returncode=0, stdout=""),
        ]

        def run(command, **kwargs):
            calls.append(command)
            del kwargs
            return responses.pop(0)

        with mock.patch.object(worker_bootstrap.subprocess, "run", side_effect=run), \
             mock.patch.object(worker_bootstrap, "_emit"):
            worker_bootstrap._ensure_runtime_pip(Path("python"), "task-1", lambda stage, message: logs.append((stage, message)))

        self.assertEqual(calls[1][1:], ["-m", "ensurepip", "--upgrade", "--default-pip"])
        self.assertIn(("bootstrap", "pip is unavailable; bootstrapping it with ensurepip"), logs)
        self.assertIn(("bootstrap", "pip bootstrapped"), logs)

    def test_missing_pip_is_detected_as_unusable(self):
        with mock.patch.object(worker_bootstrap.subprocess, "run", return_value=mock.Mock(returncode=1)):
            self.assertFalse(worker_bootstrap._runtime_pip_works(Path("python")))

    def test_reinstall_without_state_uses_a_fresh_directory(self):
        manifest = {
            **MANIFEST,
            "common": {**MANIFEST["common"], "pymss-core": "pymss-core==0.1.6"},
            "backends": {"cpu": {"platforms": ["win32"], "torch": {"requirement": "torch==2.7.1"}}},
        }
        env_dir = self.envs_dir / "cpu"
        env_python = env_dir / "Scripts" / "python.exe"
        env_python.parent.mkdir(parents=True, exist_ok=True)
        env_python.write_text("stub", encoding="utf-8")
        marker = env_dir / "old-environment.txt"
        marker.write_text("old", encoding="utf-8")
        pip = mock.Mock(return_value=mock.Mock(stdout=iter(()), wait=mock.Mock(return_value=0), returncode=0))

        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir), \
             mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", self.active_file), \
             mock.patch.object(worker_bootstrap, "_manifest", return_value=manifest), \
             mock.patch.object(worker_bootstrap, "_probe_python_runtime", return_value={
                 **probe_result("cpu"),
                 "packageVersions": {**probe_result("cpu")["packageVersions"], "pymss-core": "0.1.6"},
             }), \
             mock.patch.object(worker_bootstrap, "_runtime_pip_works", return_value=True), \
             mock.patch.object(worker_bootstrap, "_create_runtime_venv", side_effect=self._create_stub_venv), \
             mock.patch.object(worker_bootstrap.subprocess, "Popen", pip), \
             mock.patch.object(sys, "platform", "win32"), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(worker_bootstrap.cmd_install_runtime({"backend": "cpu", "mirror": "pypi"}), 0)

        log = (env_dir / "pymss-runtime-install.log").read_text(encoding="utf-8")
        self.assertIn("install backend=cpu", log)
        self.assertFalse(marker.exists())
        self.assertFalse((self.envs_dir / ".cpu.reinstalling").exists())

    def test_failed_reinstall_without_state_restores_the_previous_directory(self):
        manifest = {
            **MANIFEST,
            "common": {**MANIFEST["common"], "pymss-core": "pymss-core==0.1.6"},
            "backends": {"cpu": {"platforms": ["win32"], "torch": {"requirement": "torch==2.7.1"}}},
        }
        env_dir = self.envs_dir / "cpu"
        marker = env_dir / "old-environment.txt"
        marker.write_text("old", encoding="utf-8")
        failed_pip = mock.Mock(return_value=mock.Mock(
            stdout=iter(("failed\n",)),
            wait=mock.Mock(return_value=1),
            returncode=1,
        ))

        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir), \
             mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", self.active_file), \
             mock.patch.object(worker_bootstrap, "_manifest", return_value=manifest), \
             mock.patch.object(worker_bootstrap, "_create_runtime_venv", side_effect=self._create_stub_venv), \
             mock.patch.object(worker_bootstrap, "_runtime_pip_works", return_value=True), \
             mock.patch.object(worker_bootstrap.subprocess, "Popen", failed_pip), \
             mock.patch.object(sys, "platform", "win32"), \
             contextlib.redirect_stdout(io.StringIO()):
            result = worker_bootstrap.cmd_install_runtime({"backend": "cpu", "mirror": "pypi"})

        self.assertNotEqual(result, 0)
        self.assertEqual(marker.read_text(encoding="utf-8"), "old")
        self.assertFalse((self.envs_dir / ".cpu.reinstalling").exists())


class PyPiMirrorSelectionTests(unittest.TestCase):
    def test_auto_uses_ustc_for_chinese_locale(self):
        mirror, url = worker_bootstrap._resolve_pypi_mirror("auto", "zh-CN")
        self.assertEqual(mirror, "ustc")
        self.assertIn("ustc.edu.cn", url)

    def test_auto_uses_pypi_for_non_chinese_locale(self):
        mirror, url = worker_bootstrap._resolve_pypi_mirror("auto", "en")
        self.assertEqual(mirror, "pypi")
        self.assertEqual(url, "https://pypi.org/simple")

    def test_domestic_mirrors_have_expected_urls(self):
        for mirror, host in {
            "tsinghua": "pypi.tuna.tsinghua.edu.cn",
            "aliyun": "mirrors.aliyun.com",
            "tencent": "mirrors.cloud.tencent.com",
        }.items():
            selected, url = worker_bootstrap._resolve_pypi_mirror(mirror, "en")
            self.assertEqual(selected, mirror)
            self.assertIn(host, url)


class GpuVendorDetectionTests(unittest.TestCase):
    """Vendor detection must not depend on torch: a CPU-only environment on an NVIDIA machine
    reports no CUDA, which is precisely when recommending CUDA matters."""

    def test_pci_ids_map_to_vendors_in_every_notation(self):
        self.assertEqual(worker_bootstrap._vendor_from_pci_id("0x10de"), "nvidia")
        self.assertEqual(worker_bootstrap._vendor_from_pci_id("10DE"), "nvidia")
        self.assertEqual(worker_bootstrap._vendor_from_pci_id("0x1002\n"), "amd")
        self.assertEqual(worker_bootstrap._vendor_from_pci_id("8086"), "intel")

    def test_unknown_or_malformed_ids_yield_no_vendor(self):
        for value in ("0xffff", "", "  ", "12", None):
            self.assertIsNone(worker_bootstrap._vendor_from_pci_id(value))

    def _fake_drm_root(self, cards):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        for card, vendor_id in cards:
            device = root / card / "device"
            device.mkdir(parents=True)
            (device / "vendor").write_text(vendor_id + "\n", encoding="utf-8")
        return root

    def test_linux_reads_vendors_from_sysfs_and_deduplicates(self):
        root = self._fake_drm_root([("card0", "0x1002"), ("card1", "0x10de"), ("card2", "0x10de")])
        self.assertEqual(worker_bootstrap._linux_gpu_vendors(root), ["amd", "nvidia"])

    def test_unreadable_sysfs_entries_are_skipped(self):
        root = self._fake_drm_root([("card0", "0x10de")])
        with mock.patch.object(Path, "read_text", side_effect=OSError("denied")):
            self.assertEqual(worker_bootstrap._linux_gpu_vendors(root), [])

    def test_a_missing_drm_tree_yields_no_vendors(self):
        self.assertEqual(worker_bootstrap._linux_gpu_vendors(Path(tempfile.mkdtemp()) / "absent"), [])

    def test_detection_never_raises(self):
        with mock.patch.object(worker_bootstrap, "_windows_gpu_vendors", side_effect=RuntimeError("boom")), \
             mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(worker_bootstrap._detect_gpu_vendors(), [])

    def test_macos_reports_no_vendors(self):
        # The macOS backend follows from platform.machine(), not from a GPU vendor.
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(worker_bootstrap._detect_gpu_vendors(), [])


class EnvironmentSizeTests(unittest.TestCase):
    """Disk usage per environment — the number users need before stacking up several
    multi-GB backends."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.envs_dir = self.root / "runtime-envs"
        self.envs_dir.mkdir()
        self.addCleanup(shutil.rmtree, self.root, True)

    def _make_env(self, backend, payload_bytes):
        env_dir = self.envs_dir / backend
        (env_dir / "Scripts").mkdir(parents=True)
        (env_dir / "Scripts" / "python.exe").write_bytes(b"")
        (env_dir / "Lib").mkdir()
        (env_dir / "Lib" / "torch.bin").write_bytes(b"x" * payload_bytes)

    @contextlib.contextmanager
    def _runtime(self):
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir), \
             mock.patch.object(worker_bootstrap, "_manifest", return_value=MANIFEST), \
             mock.patch.object(sys, "platform", "win32"), \
             contextlib.redirect_stdout(self.stdout):
            yield

    stdout = None

    def _emitted(self):
        return json.loads(self.stdout.getvalue().strip().splitlines()[-1])

    def test_reports_a_size_per_installed_environment(self):
        self._make_env("cpu", 4096)
        self._make_env("cuda", 16384)
        self.stdout = io.StringIO()
        with self._runtime():
            self.assertEqual(worker_bootstrap.cmd_runtime_env_sizes({}), 0)
        payload = self._emitted()["payload"]
        self.assertEqual(payload["sizes"], {"cpu": 4096, "cuda": 16384})
        self.assertEqual(payload["totalBytes"], 20480)

    def test_an_interrupted_install_is_reported_as_leftover(self):
        # Install state is only written on success, so a cancelled install leaves a venv that
        # holds gigabytes while not counting as installed. Without this the space is stranded:
        # no card, no size, no way to delete it.
        self._make_env("cpu", 1024)
        (self.envs_dir / "cpu" / "pymss-runtime-state.json").write_text('{"backend": "cpu"}', encoding="utf-8")
        self._make_env("cuda", 8192)  # no state file — interrupted
        self.stdout = io.StringIO()
        with self._runtime():
            worker_bootstrap.cmd_runtime_env_sizes({})
        payload = self._emitted()["payload"]
        self.assertEqual(payload["incompleteBackends"], ["cuda"])
        # Its bytes are still reported, so the total stays honest about disk in use.
        self.assertEqual(payload["sizes"]["cuda"], 8192)

    def test_a_complete_environment_is_never_called_incomplete(self):
        self._make_env("cpu", 1024)
        (self.envs_dir / "cpu" / "pymss-runtime-state.json").write_text('{"backend": "cpu"}', encoding="utf-8")
        self.stdout = io.StringIO()
        with self._runtime():
            worker_bootstrap.cmd_runtime_env_sizes({})
        self.assertEqual(self._emitted()["payload"]["incompleteBackends"], [])

    def test_backends_without_an_environment_are_absent(self):
        self._make_env("cpu", 1024)
        self.stdout = io.StringIO()
        with self._runtime():
            worker_bootstrap.cmd_runtime_env_sizes({})
        # rocm is in the manifest but not installed — reporting 0 would read as "installed,
        # empty" in the UI, so it must be missing entirely.
        self.assertEqual(list(self._emitted()["payload"]["sizes"]), ["cpu"])

    def test_size_targets_skip_the_bootstrap_interpreter(self):
        # The app's own runtime is not a removable environment and must not be measured.
        self.stdout = io.StringIO()
        with self._runtime():
            targets = worker_bootstrap._env_size_targets(MANIFEST)
        self.assertEqual(targets, {})

    def test_unstattable_files_are_skipped_rather_than_raising(self):
        self._make_env("cpu", 2048)
        env_dir = self.envs_dir / "cpu"
        real_walk = os.walk

        def walk_with_ghost(path, **kwargs):
            for root, dirs, files in real_walk(path, **kwargs):
                # A file that disappears between listing and stat() — routine on a live venv.
                if root == str(env_dir / "Lib"):
                    files = [*files, "ghost.bin"]
                yield root, dirs, files

        with mock.patch.object(worker_bootstrap.os, "walk", walk_with_ghost):
            measured = worker_bootstrap._dir_size_bytes(env_dir)
        self.assertEqual(measured, 2048)

    def test_a_failing_measurement_does_not_fail_the_command(self):
        self._make_env("cpu", 2048)
        self.stdout = io.StringIO()
        with mock.patch.object(worker_bootstrap, "_dir_size_bytes", side_effect=OSError("denied")), \
             self._runtime():
            self.assertEqual(worker_bootstrap.cmd_runtime_env_sizes({}), 0)
        # Reported as unknown rather than as a broken command.
        self.assertEqual(self._emitted()["payload"]["sizes"], {})

    def test_undiscoverable_targets_do_not_fail_the_command(self):
        self.stdout = io.StringIO()
        with mock.patch.object(worker_bootstrap, "_env_size_targets", side_effect=OSError("denied")), \
             self._runtime():
            self.assertEqual(worker_bootstrap.cmd_runtime_env_sizes({}), 0)
        self.assertEqual(self._emitted()["payload"],
                         {"sizes": {}, "totalBytes": 0, "incompleteBackends": []})


if __name__ == "__main__":
    unittest.main()
