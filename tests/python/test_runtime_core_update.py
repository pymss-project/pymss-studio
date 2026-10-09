from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import worker_bootstrap


COMMON = {
    "av": "av>=14,<15",
    "numpy": "numpy>=1.26,<3",
    "pysocks": "PySocks>=1.7.1,<2",
    "pymss": "pymss[proxy]>=2.1.4",
    "pymss-core": "pymss-core>=0.1.6",
}


def manifest(version: str = "2026.09.3") -> dict:
    return {
        "schemaVersion": 1,
        "baseGeneration": 1,
        "manifestVersion": version,
        "python": "3.12",
        "common": dict(COMMON),
        "backends": {
            "cpu": {"platforms": ["win32"], "torch": {"requirement": "torch==2.7.1"}},
            "cuda": {"platforms": ["win32"], "torch": {"requirement": "torch==2.7.1+cu128"}},
            "mlx": {"platforms": ["darwin"], "torch": {"requirement": "torch==2.7.1"}, "extras": ["mlx"]},
        },
    }


def probe(backend: str, *, pymss: str = "2.1.4", core: str = "0.1.6", graph: bool = True) -> dict:
    torch_backend = "cpu" if backend == "mlx" else backend
    torch_version = {"cpu": "2.7.1", "cuda": "2.7.1+cu128", "mlx": "2.7.1"}[backend]
    versions = {
        "av": "14.2.0",
        "numpy": "2.1.0",
        "pysocks": "1.7.1",
        "pymss": pymss,
        "pymss-core": core,
    }
    packages = {name: True for name in versions}
    if backend == "mlx":
        packages["mlx"] = True
        versions["mlx"] = "0.32.0"
    return {
        "pythonVersion": "3.12.0",
        "torchVersion": torch_version,
        "torchBackend": torch_backend,
        "acceleratorAvailable": backend == "cuda",
        "packages": packages,
        "packageVersions": versions,
        "pymssVersion": pymss,
        "pymssCoreVersion": core,
        "pymssGraphAvailable": graph,
    }


class RuntimeOverlayUpdateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.envs = self.root / "runtime-envs"
        self.envs.mkdir()
        self.active = self.envs / "active-runtime.json"
        self.addCleanup(shutil.rmtree, self.root, True)

    def make_env(self, backend="cpu", *, manifest_version="2026.09.2", bundled=False):
        runtime_root = self.root / "bundled-envs" if bundled else self.envs
        env = runtime_root / backend
        python = env / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("stub", encoding="utf-8")
        state = {
            "backend": backend,
            "manifestVersion": manifest_version,
            "stateVersion": worker_bootstrap.ENV_STATE_VERSION,
            "pythonVersion": "3.12.0",
            "torchVersion": probe(backend)["torchVersion"],
            "torchBackend": probe(backend)["torchBackend"],
            "packages": probe(backend)["packages"],
            "packageVersions": {**probe(backend)["packageVersions"], "pymss": "2.1.3"},
            "pymssVersion": "2.1.3",
            "pymssCoreVersion": "0.1.6",
            "pymssGraphAvailable": True,
        }
        (env / "pymss-runtime-state.json").write_text(json.dumps(state), encoding="utf-8")
        self.active.write_text(json.dumps({
            **state,
            "pythonPath": str(python),
            "source": "bundled" if bundled else "managed",
        }), encoding="utf-8")
        return runtime_root, env, python

    def run_update(
        self,
        backend="cpu",
        *,
        bundled=False,
        manifest_version="2026.09.2",
        pip_status=0,
        overlay_probe=None,
        manifest_value=None,
        repair=False,
        audio_error=None,
    ):
        bundled_root, env, python = self.make_env(backend, manifest_version=manifest_version, bundled=bundled)
        base = probe(backend, pymss="2.1.3")
        final = overlay_probe or probe(backend)
        probe_calls = [base, final]
        commands = []

        def popen(command, **kwargs):
            commands.append(command)
            del kwargs
            return mock.Mock(
                stdout=iter(["pip output\n"]),
                wait=mock.Mock(return_value=pip_status),
                returncode=pip_status,
                poll=mock.Mock(return_value=pip_status),
            )

        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs), \
             mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", self.active), \
             mock.patch.object(worker_bootstrap, "BUNDLED_RUNTIME_ENVS_DIR", bundled_root if bundled else None), \
             mock.patch.object(worker_bootstrap, "_manifest", return_value=manifest_value or manifest()), \
             mock.patch.object(worker_bootstrap, "_latest_pypi_version", side_effect=lambda name: {"pymss": "2.1.4", "pymss-core": "0.1.6"}[name]), \
             mock.patch.object(worker_bootstrap, "_probe_python_runtime", side_effect=lambda *_args, **_kwargs: probe_calls.pop(0) if probe_calls else final), \
             mock.patch.object(worker_bootstrap, "_verify_runtime_audio", side_effect=audio_error) as audio_probe, \
             mock.patch.object(worker_bootstrap, "_ensure_runtime_pip"), \
             mock.patch.object(worker_bootstrap.subprocess, "Popen", side_effect=popen), \
             mock.patch.object(sys, "platform", "win32"), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            result = worker_bootstrap.cmd_update_runtime_core({
                "backend": backend,
                "pythonPath": str(python),
                "mirror": "pypi",
                "taskId": "overlay-update",
                "repairDependencies": repair,
            })
            if result == 0:
                audio_probe.assert_called_once()
                self.assertEqual(audio_probe.call_args.args[0], python)
                self.assertEqual(audio_probe.call_args.args[1].name, "site-packages")
        return result, commands, env, python, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_success_builds_overlay_without_torch_or_base_mutation(self):
        result, commands, _env, python, events = self.run_update("cuda")
        self.assertEqual(result, 0)
        self.assertEqual(len(commands), 1)
        command = commands[0]
        self.assertEqual(command[0], str(python))
        self.assertIn("--target", command)
        self.assertIn("--no-deps", command)
        self.assertIn("--cache-dir", command)
        self.assertIn("pymss[proxy]==2.1.4", command)
        self.assertIn("pymss-core==0.1.6", command)
        self.assertFalse(any(value.lower().startswith("torch") for value in command))
        active = json.loads(self.active.read_text(encoding="utf-8"))
        overlay = Path(active["overlayPath"])
        self.assertTrue(overlay.is_dir())
        self.assertTrue(overlay.is_relative_to(self.envs / ".overlays"))
        self.assertTrue(python.is_file())
        self.assertEqual(active["manifestVersion"], "2026.09.3")
        self.assertEqual(
            [event["payload"]["stage"] for event in events if event["type"] == "runtime_core_update_stage"],
            ["prepare", "manifest", "verify", "activate"],
        )

    def test_only_base_unsatisfied_requirements_are_added_to_overlay(self):
        current = manifest()
        current["common"]["new-package"] = "new-package>=1,<2"
        final = probe("cpu")
        final["packages"]["new-package"] = True
        final["packageVersions"]["new-package"] = "1.2.0"
        result, commands, *_ = self.run_update(manifest_value=current, overlay_probe=final)
        self.assertEqual(result, 0)
        command = commands[0]
        self.assertIn("new-package>=1,<2", command)
        self.assertNotIn("av>=14,<15", command)
        self.assertNotIn("numpy>=1.26,<3", command)

    def test_bundled_environment_uses_writable_overlay(self):
        result, _commands, env, python, _events = self.run_update(bundled=True)
        self.assertEqual(result, 0)
        active = json.loads(self.active.read_text(encoding="utf-8"))
        self.assertEqual(active["source"], "bundled")
        self.assertTrue(Path(active["overlayPath"]).is_relative_to(self.envs / ".overlays"))
        self.assertTrue(python.is_file())
        self.assertEqual(json.loads((env / "pymss-runtime-state.json").read_text(encoding="utf-8"))["manifestVersion"], "2026.09.2")

    def test_failed_install_does_not_change_active_pointer(self):
        result, _commands, env, _python, events = self.run_update(pip_status=1)
        self.assertNotEqual(result, 0)
        self.assertNotIn("overlayPath", json.loads(self.active.read_text(encoding="utf-8")))
        self.assertTrue((self.envs / ".overlays" / "cpu" / "pymss-core-update.log").is_file())
        self.assertEqual(events[-1]["payload"]["code"], "RUNTIME_CORE_UPDATE_FAILED")
        self.assertTrue(env.is_dir())

    def test_failed_validation_does_not_activate_overlay(self):
        result, _commands, env, _python, _events = self.run_update(overlay_probe=probe("cpu", graph=False))
        self.assertNotEqual(result, 0)
        self.assertNotIn("overlayPath", json.loads(self.active.read_text(encoding="utf-8")))
        self.assertTrue(env.is_dir())

    def test_failed_audio_decode_does_not_activate_overlay(self):
        result, _commands, env, _python, events = self.run_update(
            audio_error=RuntimeError("No module named 'audioread'"),
        )
        self.assertNotEqual(result, 0)
        self.assertNotIn("overlayPath", json.loads(self.active.read_text(encoding="utf-8")))
        self.assertTrue(env.is_dir())
        self.assertIn("audioread", events[-1]["payload"]["message"])

    def test_newer_or_unknown_manifest_is_rejected_without_repair(self):
        for value in ("2026.10.1", "legacy", ""):
            with self.subTest(value=value):
                result, commands, *_ = self.run_update(manifest_version=value)
                self.assertNotEqual(result, 0)
                self.assertEqual(commands, [])

    def test_repair_accepts_legacy_manifest_and_rebuilds_overlay(self):
        result, commands, *_ = self.run_update(manifest_version="", repair=True)
        self.assertEqual(result, 0)
        self.assertEqual(len(commands), 1)

    def test_overlay_cleanup_keeps_current_and_one_previous_generation(self):
        root = self.envs / ".overlays" / "cpu"
        for name in ("old-a", "old-b"):
            (root / name / "site-packages").mkdir(parents=True)
        stale_staging = root / ".aborted.staging"
        (stale_staging / "site-packages").mkdir(parents=True)
        result, _commands, *_ = self.run_update()
        self.assertEqual(result, 0)
        generations = [path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")]
        self.assertEqual(len(generations), 2)
        self.assertFalse(stale_staging.exists())


if __name__ == "__main__":
    unittest.main()
