from __future__ import annotations

import contextlib
import io
import json
import tempfile
import sys
import unittest
from pathlib import Path
from functools import partial
from unittest import mock

import worker_bootstrap
from worker_runtime_lock import runtime_lock


COMMON_PACKAGES = ("av", "librosa", "numpy", "pymss", "pymss-core")


def _manifest():
    return {
        "manifestVersion": "2026.09.1",
        "common": {
            "av": "av",
            "librosa": "librosa",
            "numpy": "numpy",
            "pymss": "pymss[proxy]>=2.1.4",
            "pymss-core": "pymss-core>=0.1.6",
        },
        "backends": {
            "cpu": {"platforms": ["win32", "linux", "darwin"], "torch": {"requirement": "torch==2.7.1"}},
            "cuda": {"platforms": ["win32", "linux"], "torch": {"requirement": "torch==2.7.1+cu128"}},
            "rocm": {"platforms": ["win32", "linux"], "torch": {"requirement": "torch==2.7.1+rocm6.3"}},
            "mlx": {"platforms": ["darwin"], "torch": {"requirement": "torch==2.7.1"}, "extras": ["mlx"]},
        },
    }


def _probe_result(backend: str) -> dict[str, object]:
    torch_backend = "cpu" if backend == "mlx" else backend
    package_versions = {name: "2.0.0" for name in COMMON_PACKAGES}
    package_versions.update({"pymss": "2.1.4", "pymss-core": "0.1.6"})
    packages = {name: True for name in COMMON_PACKAGES}
    if backend == "mlx":
        packages["mlx"] = True
        package_versions["mlx"] = "0.21.0"
    return {
        "pythonVersion": "3.12.0",
        "torchVersion": {
            "cpu": "2.7.1",
            "cuda": "2.7.1+cu128",
            "rocm": "2.7.1+rocm6.3",
            "mlx": "2.7.1",
        }[backend],
        "torchBackend": torch_backend,
        "acceleratorAvailable": backend in {"cuda", "rocm"},
        "packages": packages,
        "packageVersions": package_versions,
        "pymssVersion": "2.1.4",
        "pymssCoreVersion": "0.1.6",
        "pymssGraphAvailable": True,
    }


class RuntimeCoreUpdateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.envs_dir = self.root / "runtime-envs"
        self.envs_dir.mkdir()
        self.active_file = self.envs_dir / "active-runtime.json"
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, True))

    def _make_env(self, backend: str, *, state_version: int = worker_bootstrap.ENV_STATE_VERSION, manifest_version: str = "2026.08.1"):
        env_dir = self.envs_dir / backend
        (env_dir / "Scripts").mkdir(parents=True)
        python_path = env_dir / "Scripts" / "python.exe"
        python_path.write_text("stub", encoding="utf-8")
        (env_dir / "pymss-runtime-state.json").write_text(json.dumps({
            "backend": backend,
            "manifestVersion": manifest_version,
            "stateVersion": state_version,
            "torchVersion": _probe_result(backend)["torchVersion"],
            "torchBackend": _probe_result(backend)["torchBackend"],
            "acceleratorAvailable": _probe_result(backend)["acceleratorAvailable"],
            "packages": {name: True for name in COMMON_PACKAGES},
            "packageVersions": {name: "2.0.0" for name in COMMON_PACKAGES},
            "pymssVersion": "2.1.3",
            "pymssCoreVersion": "0.1.6",
            "pymssGraphAvailable": True,
        }), encoding="utf-8")
        self.active_file.write_text(json.dumps({
            "backend": backend,
            "pythonPath": str(python_path),
            "manifestVersion": manifest_version,
            "stateVersion": state_version,
        }), encoding="utf-8")
        return env_dir, python_path

    def _run_update(self, backend: str, *, missing_records: dict[str, str] | None, popen_outputs: list[str] | None = None, manifest_version: str = "2026.08.1", on_pip=None, manifest=None, probe=None):
        env_dir, python_path = self._make_env(backend, manifest_version=manifest_version)
        outputs = popen_outputs or ["metadata repaired\n", "upgrade complete\n"]
        popen_calls: list[list[str]] = []
        missing_probe = iter([missing_records or {}, {}])

        def popen(command, **kwargs):
            popen_calls.append(command)
            if on_pip:
                on_pip()
            del kwargs
            return mock.Mock(
                stdout=iter(outputs.pop(0) for _ in range(1)),
                wait=mock.Mock(return_value=0),
                returncode=0,
                poll=mock.Mock(return_value=0),
            )

        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir), \
             mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", self.active_file), \
             mock.patch.object(worker_bootstrap, "_manifest", return_value=manifest or _manifest()), \
             mock.patch.object(worker_bootstrap, "_latest_pypi_version", side_effect=lambda name: {"pymss": "2.1.4", "pymss-core": "0.1.6"}[name]), \
             mock.patch.object(worker_bootstrap, "_probe_python_runtime", return_value=probe if probe is not None else _probe_result(backend)), \
             mock.patch.object(worker_bootstrap, "_runtime_core_missing_records", side_effect=lambda _path: next(missing_probe, {})), \
             mock.patch.object(worker_bootstrap, "_ensure_runtime_pip"), \
             mock.patch.object(worker_bootstrap.subprocess, "Popen", side_effect=popen), \
             mock.patch.object(sys, "platform", "win32"), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            result = worker_bootstrap.cmd_update_runtime_core({"backend": backend, "mirror": "pypi", "pythonPath": str(python_path), "taskId": "core-update"})

        self.events = [json.loads(line) for line in output.getvalue().splitlines()]
        return result, popen_calls, env_dir, python_path

    def test_update_core_repairs_missing_record_then_runs_normal_upgrade(self):
        result, popen_calls, env_dir, _python_path = self._run_update(
            "cuda",
            missing_records={"pymss": "2.1.3", "pymss-core": "0.1.6"},
        )

        self.assertEqual(result, 0)
        self.assertGreaterEqual(len(popen_calls), 2)
        repair_cmd = popen_calls[0]
        upgrade_cmd = popen_calls[1]
        self.assertIn("--ignore-installed", repair_cmd)
        self.assertIn("--no-deps", repair_cmd)
        self.assertIn("--no-cache-dir", repair_cmd)
        self.assertIn("--only-binary=:all:", repair_cmd)
        self.assertNotIn("--ignore-installed", upgrade_cmd)
        self.assertNotIn("--upgrade", upgrade_cmd)
        self.assertIn("pymss[proxy]==2.1.4", upgrade_cmd)
        self.assertIn("pymss-core==0.1.6", upgrade_cmd)
        self.assertIn("av", upgrade_cmd)
        self.assertIn("numpy", upgrade_cmd)
        self.assertTrue((env_dir / "pymss-core-update.log").is_file())

    def test_update_core_skips_repair_when_records_are_present(self):
        result, popen_calls, _env_dir, _python_path = self._run_update("cpu", missing_records={})

        self.assertEqual(result, 0)
        self.assertEqual(len(popen_calls), 1)
        self.assertNotIn("--ignore-installed", popen_calls[0])
        self.assertNotIn("--upgrade", popen_calls[0])

    def test_incompatible_manifest_is_rejected_before_network_or_install(self):
        env_dir, python_path = self._make_env("cpu")
        state_file = env_dir / "pymss-runtime-state.json"
        for version in ("2026.10.1", None, "", "2026.09.1-invalid", "legacy"):
            with self.subTest(manifest_version=version):
                state = json.loads(state_file.read_text(encoding="utf-8"))
                state["manifestVersion"] = version
                state_file.write_text(json.dumps(state), encoding="utf-8")
                before_state = state_file.read_bytes()
                before_active = self.active_file.read_bytes()
                with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir), \
                     mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", self.active_file), \
                     mock.patch.object(worker_bootstrap, "_manifest", return_value=_manifest()), \
                     mock.patch.object(worker_bootstrap, "_latest_pypi_version", return_value="2.1.4") as latest, \
                     mock.patch.object(worker_bootstrap.shutil, "copytree") as copytree, \
                     mock.patch.object(worker_bootstrap.subprocess, "Popen") as popen, \
                     mock.patch.object(sys, "platform", "win32"), \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    result = worker_bootstrap.cmd_update_runtime_core({"backend": "cpu", "pythonPath": str(python_path)})
                self.assertNotEqual(result, 0)
                self.assertIn("RUNTIME_MANIFEST_INCOMPATIBLE", output.getvalue())
                latest.assert_not_called()
                copytree.assert_not_called()
                popen.assert_not_called()
                self.assertEqual(state_file.read_bytes(), before_state)
                self.assertEqual(self.active_file.read_bytes(), before_active)
                self.assertFalse((env_dir / "pymss-core-update.log").exists())

    def test_current_manifest_allows_core_package_update(self):
        result, commands, env_dir, _python_path = self._run_update(
            "cpu", missing_records={}, manifest_version="2026.09.1",
        )
        self.assertEqual(result, 0)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][0], str(_python_path))
        for requirement in ("av", "librosa", "numpy"):
            self.assertNotIn(requirement, commands[0])
        self.assertNotIn("--no-deps", commands[0])
        self.assertFalse((env_dir / ".pymss-core-update-constraints.txt").exists())
        state = json.loads((env_dir / "pymss-runtime-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["pymssVersion"], "2.1.4")
        self.assertEqual(state["manifestVersion"], "2026.09.1")

    def test_current_manifest_keeps_dependency_bounds_without_installing_extras(self):
        from packaging.requirements import Requirement

        manifest = _manifest()
        manifest["common"]["av"] = "av[codec]>=1,<3"
        captured = []

        def capture_constraints():
            path = self.envs_dir / "mlx" / ".pymss-core-update-constraints.txt"
            captured.extend(Requirement(line) for line in path.read_text(encoding="utf-8").splitlines())

        result, commands, _env_dir, _python_path = self._run_update(
            "mlx", missing_records={}, manifest_version="2026.09.1",
            manifest=manifest, on_pip=capture_constraints,
        )
        self.assertEqual(result, 0)
        self.assertNotIn("mlx", commands[0])
        self.assertNotIn("av[codec]>=1,<3", commands[0])
        av = next(requirement for requirement in captured if requirement.name == "av")
        self.assertIn("2.0.0", av.specifier)
        self.assertNotIn("3.0.0", av.specifier)
        self.assertTrue(all(not requirement.extras for requirement in captured))
        self.assertIn("pymss[proxy]==2.1.4", commands[0])

    def test_progress_reports_preparation_installation_and_verification(self):
        result, _commands, env_dir, _python_path = self._run_update(
            "cpu", missing_records={}, manifest_version="2026.09.1",
        )
        self.assertEqual(result, 0)
        self.assertEqual(
            [event["payload"]["stage"] for event in self.events if event["type"] == "runtime_core_update_stage"],
            ["prepare", "core", "verify"],
        )
        self.assertEqual(self.events[-1]["type"], "runtime_core_update_finished")
        self.assertTrue(all(event["taskId"] == "core-update" for event in self.events))
        self.assertIn("Core package update completed", (env_dir / "pymss-core-update.log").read_text(encoding="utf-8"))

    def test_failed_validation_keeps_previous_metadata_and_environment(self):
        for key, value in (("pymssVersion", "2.1.3"), ("pymssCoreVersion", "0.1.5"),
                           ("pymssGraphAvailable", False), ("torchBackend", "cuda"),
                           ("torchVersion", "2.7.1+cpu"), ("packageVersions", {})):
            with self.subTest(key=key):
                # Each backend directory belongs only to this test; reset the fixture location.
                self.envs_dir = self.root / key
                self.envs_dir.mkdir()
                self.active_file = self.envs_dir / "active-runtime.json"
                before = {}

                def capture_state():
                    before["env"] = (self.envs_dir / "cpu" / "pymss-runtime-state.json").read_bytes()
                    before["active"] = self.active_file.read_bytes()

                probed = {**_probe_result("cpu"), key: value}
                result, _commands, env_dir, python_path = self._run_update(
                    "cpu", missing_records={}, probe=probed, on_pip=capture_state,
                )
                self.assertNotEqual(result, 0)
                self.assertTrue(python_path.is_file())
                self.assertEqual((env_dir / "pymss-runtime-state.json").read_bytes(), before["env"])
                self.assertEqual(self.active_file.read_bytes(), before["active"])
                self.assertFalse((env_dir / ".pymss-core-update-constraints.txt").exists())
                self.assertEqual(self.events[-1]["payload"]["code"], "RUNTIME_CORE_UPDATE_FAILED")
                self.assertNotIn("runtime_core_update_finished", [event["type"] for event in self.events])

    def test_metadata_write_failure_does_not_remove_environment(self):
        before = {}

        def capture_state():
            before["env"] = (self.envs_dir / "cpu" / "pymss-runtime-state.json").read_bytes()
            before["active"] = self.active_file.read_bytes()

        with mock.patch.object(worker_bootstrap, "_atomic_write_json", side_effect=PermissionError("state is read-only")):
            result, _commands, env_dir, python_path = self._run_update("cpu", missing_records={}, on_pip=capture_state)
        self.assertNotEqual(result, 0)
        self.assertTrue(python_path.is_file())
        self.assertEqual((env_dir / "pymss-runtime-state.json").read_bytes(), before["env"])
        self.assertEqual(self.active_file.read_bytes(), before["active"])
        self.assertFalse((env_dir / ".pymss-core-update-constraints.txt").exists())
        self.assertEqual(self.events[-1]["payload"]["code"], "RUNTIME_CORE_UPDATE_FAILED")

    def test_core_update_does_not_copy_scan_or_swap_the_environment(self):
        with mock.patch.object(worker_bootstrap.shutil, "copytree", side_effect=AssertionError("unexpected copy")), \
             mock.patch.object(worker_bootstrap, "_dir_size_bytes", side_effect=AssertionError("unexpected size scan")), \
             mock.patch.object(worker_bootstrap.shutil, "disk_usage", side_effect=AssertionError("unexpected disk scan")), \
             mock.patch.object(Path, "rename", side_effect=AssertionError("unexpected rename")), \
             mock.patch.object(worker_bootstrap.shutil, "rmtree", side_effect=AssertionError("unexpected deletion")):
            result, commands, env_dir, python_path = self._run_update("cuda", missing_records={})
        self.assertEqual(result, 0)
        self.assertEqual(commands[0][0], str(python_path))
        self.assertTrue(env_dir.is_dir())
        self.assertFalse((self.envs_dir / ".cuda.core-updating").exists())
        self.assertFalse((self.envs_dir / ".cuda.core-backup").exists())

    def test_queries_during_in_place_update_are_rejected(self):
        def query_while_pip_runs():
            env_dir = self.envs_dir / "cpu"
            self.assertTrue(env_dir.is_dir())
            self.assertFalse((self.envs_dir / ".cpu.core-updating").exists())
            for query in (worker_bootstrap.cmd_runtime_info, worker_bootstrap.cmd_runtime_env_sizes):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertNotEqual(query({}), 0)
                self.assertIn("RUNTIME_BUSY", output.getvalue())
                self.assertTrue(env_dir.is_dir())

        with mock.patch.object(worker_bootstrap, "runtime_lock", partial(runtime_lock, timeout=0)):
            result, commands, env_dir, _python_path = self._run_update("cpu", missing_records={}, on_pip=query_while_pip_runs)
        self.assertEqual(result, 0)
        self.assertEqual(len(commands), 1)
        state = json.loads((env_dir / "pymss-runtime-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["pymssVersion"], "2.1.4")

    def test_update_core_does_not_continue_when_repair_remains_incomplete(self):
        env_dir, python_path = self._make_env("rocm")
        popen_calls: list[list[str]] = []

        def popen(command, **kwargs):
            popen_calls.append(command)
            del kwargs
            return mock.Mock(stdout=iter(()), wait=mock.Mock(return_value=0), returncode=0, poll=mock.Mock(return_value=0))

        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir), \
             mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", self.active_file), \
             mock.patch.object(worker_bootstrap, "_manifest", return_value=_manifest()), \
             mock.patch.object(worker_bootstrap, "_latest_pypi_version", side_effect=lambda name: {"pymss": "2.1.4", "pymss-core": "0.1.6"}[name]), \
             mock.patch.object(worker_bootstrap, "_probe_python_runtime", return_value=_probe_result("rocm")), \
             mock.patch.object(worker_bootstrap, "_runtime_core_missing_records", side_effect=[{"pymss": "2.1.3"}, {"pymss": "2.1.3"}]), \
             mock.patch.object(worker_bootstrap, "_ensure_runtime_pip"), \
             mock.patch.object(worker_bootstrap.subprocess, "Popen", side_effect=popen), \
             mock.patch.object(sys, "platform", "win32"), \
             contextlib.redirect_stdout(io.StringIO()):
            result = worker_bootstrap.cmd_update_runtime_core({"backend": "rocm", "mirror": "pypi", "pythonPath": str(python_path)})

        self.assertNotEqual(result, 0)
        self.assertEqual(len(popen_calls), 1)
        self.assertIn("--ignore-installed", popen_calls[0])
        self.assertEqual(
            json.loads((env_dir / "pymss-runtime-state.json").read_text(encoding="utf-8"))["pymssVersion"],
            "2.1.3",
        )

    def test_failed_update_preserves_environment_and_state_without_rolling_back_packages(self):
        env_dir, python_path = self._make_env("cpu")
        marker = env_dir / "keep-me.txt"
        marker.write_text("original", encoding="utf-8")
        original_state = (env_dir / "pymss-runtime-state.json").read_text(encoding="utf-8")
        original_active = self.active_file.read_bytes()

        def popen(command, **kwargs):
            del kwargs
            # Pip can fail after writing a package. Retain those files for a retry;
            # never replace or remove the active environment on this error path.
            (Path(command[0]).parent.parent / "partial-package.py").write_text("updated", encoding="utf-8")
            return mock.Mock(
                stdout=iter(["pip failed\n"]),
                wait=mock.Mock(return_value=1),
                returncode=1,
                poll=mock.Mock(return_value=1),
            )

        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir), \
             mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", self.active_file), \
             mock.patch.object(worker_bootstrap, "_manifest", return_value=_manifest()), \
             mock.patch.object(worker_bootstrap, "_latest_pypi_version", side_effect=lambda name: {"pymss": "2.1.4", "pymss-core": "0.1.6"}[name]), \
             mock.patch.object(worker_bootstrap, "_probe_python_runtime", return_value=_probe_result("cpu")), \
             mock.patch.object(worker_bootstrap, "_runtime_core_missing_records", return_value={}), \
             mock.patch.object(worker_bootstrap, "_ensure_runtime_pip"), \
             mock.patch.object(worker_bootstrap.subprocess, "Popen", side_effect=popen), \
             mock.patch.object(sys, "platform", "win32"), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            result = worker_bootstrap.cmd_update_runtime_core({"backend": "cpu", "mirror": "pypi", "pythonPath": str(python_path)})

        self.assertNotEqual(result, 0)
        self.assertTrue(env_dir.is_dir())
        self.assertEqual(marker.read_text(encoding="utf-8"), "original")
        self.assertEqual((env_dir / "pymss-runtime-state.json").read_text(encoding="utf-8"), original_state)
        self.assertEqual(self.active_file.read_bytes(), original_active)
        self.assertEqual((env_dir / "partial-package.py").read_text(encoding="utf-8"), "updated")
        self.assertIn("pip failed", (env_dir / "pymss-core-update.log").read_text(encoding="utf-8"))
        self.assertIn("RUNTIME_CORE_UPDATE_FAILED", output.getvalue())
        self.assertNotIn("runtime_core_update_finished", output.getvalue())
        self.assertFalse((env_dir / ".pymss-core-update-constraints.txt").exists())
        self.assertFalse((self.envs_dir / ".cpu.core-updating").exists())
        self.assertFalse((self.envs_dir / ".cpu.core-backup").exists())

    def test_interrupted_core_swap_restores_backup_when_final_directory_is_missing(self):
        env_dir, _python_path = self._make_env("cpu")
        backup = self.envs_dir / ".cpu.core-backup"
        env_dir.rename(backup)
        staging = self.envs_dir / ".cpu.core-updating"
        staging.mkdir()
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir), \
             mock.patch.object(worker_bootstrap, "_manifest", return_value=_manifest()):
            worker_bootstrap._recover_core_update_transactions()
        self.assertTrue(env_dir.is_dir())
        self.assertTrue((env_dir / "Scripts" / "python.exe").is_file())
        self.assertFalse(backup.exists())
        self.assertFalse(staging.exists())

    def test_interrupted_core_swap_discards_backup_after_valid_final_directory(self):
        env_dir, _python_path = self._make_env("cpu")
        backup = self.envs_dir / ".cpu.core-backup"
        env_dir.rename(backup)
        env_dir.mkdir()
        (env_dir / "Scripts").mkdir()
        (env_dir / "Scripts" / "python.exe").write_text("new", encoding="utf-8")
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs_dir), \
             mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", self.active_file), \
             mock.patch.object(worker_bootstrap, "_manifest", return_value=_manifest()), \
             mock.patch.object(worker_bootstrap, "_probe_python_runtime", return_value=_probe_result("cpu")):
            worker_bootstrap._recover_core_update_transactions()
        self.assertTrue(env_dir.is_dir())
        self.assertEqual((env_dir / "Scripts" / "python.exe").read_text(encoding="utf-8"), "new")
        self.assertFalse(backup.exists())
        recovered_state = json.loads((env_dir / "pymss-runtime-state.json").read_text(encoding="utf-8"))
        active_state = json.loads(self.active_file.read_text(encoding="utf-8"))
        self.assertEqual(recovered_state["manifestVersion"], "2026.09.1")
        self.assertEqual(active_state["manifestVersion"], "2026.09.1")

    def test_update_core_preserves_torch_constraints_for_mlx(self):
        with mock.patch.object(worker_bootstrap.Path, "unlink", autospec=True, side_effect=lambda self, missing_ok=False: None):
            result, popen_calls, env_dir, _python_path = self._run_update("mlx", missing_records=None)

        self.assertEqual(result, 0)
        self.assertEqual(len(popen_calls), 1)
        upgrade_cmd = popen_calls[0]
        self.assertIn("--constraint", upgrade_cmd)
        self.assertIn("torch==2.7.1", (env_dir / ".pymss-core-update-constraints.txt").read_text(encoding="utf-8").splitlines())
        self.assertIn("pymss[proxy]==2.1.4", upgrade_cmd)
        self.assertIn("pymss-core==0.1.6", upgrade_cmd)
        self.assertIn("mlx", upgrade_cmd)

    def test_missing_record_helper_keeps_only_core_packages(self):
        python_path = self.root / "python.exe"
        python_path.write_text("stub", encoding="utf-8")
        payload = json.dumps({"pymss": "2.1.3", "pymss-core": "0.1.6", "torch": "999"})
        with mock.patch.object(worker_bootstrap.subprocess, "check_output", return_value=payload):
            self.assertEqual(worker_bootstrap._runtime_core_missing_records(python_path), {"pymss": "2.1.3", "pymss-core": "0.1.6"})
