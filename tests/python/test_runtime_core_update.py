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


def current_core_manifest() -> dict:
    shipped = json.loads(worker_bootstrap.MANIFEST_PATH.read_text(encoding="utf-8"))
    value = manifest(shipped["manifestVersion"])
    for name in ("pymss", "pymss-core"):
        value["common"][name] = shipped["common"][name]
    return value


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


class RuntimeCoreVersionTests(unittest.TestCase):
    def test_previous_manifest_sync_is_detected_as_outdated_and_incompatible(self):
        current = current_core_manifest()
        self.assertEqual(worker_bootstrap._runtime_manifest_status("2026.10.3", current["manifestVersion"]), "older")
        self.assertEqual(worker_bootstrap._runtime_health("cpu", probe("cpu", pymss="2.1.8", core="0.1.11"), current), "degraded")
        self.assertEqual(worker_bootstrap._runtime_health("cpu", probe("cpu", pymss="2.2.0", core="0.1.14"), current), "ready")
        self.assertEqual(worker_bootstrap._runtime_health("cpu", probe("cpu", pymss="3.0.0", core="1.0.0"), current), "ready")

    def test_version_queries_use_pypi_even_when_release_versions_are_pinned(self):
        pinned = manifest()
        pinned["common"].update({"pymss": "pymss[proxy]==2.1.4", "pymss-core": "pymss-core==0.1.6"})
        with mock.patch.object(worker_bootstrap, "_manifest", return_value=pinned), \
             mock.patch.object(worker_bootstrap, "_latest_pypi_version", side_effect=["2.2.0", "0.2.0"]) as latest, \
             mock.patch.object(worker_bootstrap, "_emit") as emit:
            self.assertEqual(worker_bootstrap.cmd_runtime_core_versions({}), 0)
        self.assertEqual(latest.call_args_list, [mock.call("pymss", "stable"), mock.call("pymss-core", "stable")])
        self.assertEqual(emit.call_args.args[1]["packages"]["pymss"]["latestVersion"], "2.2.0")

    def test_unavailable_index_is_not_reported_as_the_bundled_latest(self):
        with mock.patch.object(worker_bootstrap, "_latest_pypi_version", side_effect=OSError("unavailable")), \
             mock.patch.object(worker_bootstrap, "_emit") as emit:
            worker_bootstrap.cmd_runtime_core_versions({})
        self.assertIsNone(emit.call_args.args[1]["packages"]["pymss"]["latestVersion"])
        self.assertIn("error", emit.call_args.args[1]["packages"]["pymss"])

    def test_newer_core_is_healthy_but_old_core_and_changed_torch_are_rejected(self):
        pinned = manifest()
        pinned["common"].update({"pymss": "pymss[proxy]==2.1.4", "pymss-core": "pymss-core==0.1.6"})
        current = probe("cpu", pymss="3.0.0", core="1.0.0")
        self.assertEqual(worker_bootstrap._runtime_health("cpu", current, pinned), "ready")
        self.assertFalse(worker_bootstrap._manifest_versions_are_satisfied(probe("cpu", pymss="2.1.3"), pinned, "cpu")[0])
        self.assertFalse(worker_bootstrap._manifest_versions_are_satisfied({**current, "torchVersion": "2.8.0"}, pinned, "cpu")[0])

    def test_resolved_dependencies_extend_overlay_but_cannot_replace_accelerator_packages(self):
        requirements = ["pymss[proxy]==2.2.0", "pymss-core==0.2.0"]
        resolved = worker_bootstrap._resolved_overlay_requirements(requirements, {"version": "1", "install": [
            {"metadata": {"name": "pymss", "version": "2.2.0"}},
            {"metadata": {"name": "new-audio-dependency", "version": "1.0"}},
        ]})
        self.assertIn("pymss[proxy]==2.2.0", resolved)
        self.assertIn("new-audio-dependency==1.0", resolved)
        for name in ("torch", "torchvision", "torchaudio", "triton", "mlx", "mlx-metal", "nvidia-cublas-cu12", "rocm-sdk-core"):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "base accelerator"):
                worker_bootstrap._resolved_overlay_requirements(requirements, {"version": "1", "install": [{"metadata": {"name": name, "version": "9.0"}}]})
        with self.assertRaisesRegex(RuntimeError, "unsupported installation report"):
            worker_bootstrap._resolved_overlay_requirements(requirements, {"version": "2", "install": []})


class RuntimeOverlayUpdateTests(unittest.TestCase):
    def setUp(self):
        # Temporary roots may use Windows short names or macOS /var symlinks.
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.envs = self.root / "runtime-envs"
        self.envs.mkdir()
        self.active = self.envs / "active-runtime.json"
        self.addCleanup(shutil.rmtree, self.root, True)

    def make_env(self, backend="cpu", *, manifest_version="2026.09.2", bundled=False, platform="win32", bootstrap=False):
        runtime_root = self.root / "bundled-envs" if bundled else self.envs
        env = runtime_root / backend
        python = runtime_root.parent / "bin/python3" if bootstrap else env / ("Scripts/python.exe" if platform == "win32" else "bin/python")
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("stub", encoding="utf-8")
        env.mkdir(parents=True, exist_ok=True)
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
        active_state = {
            **state,
            "pythonPath": str(python),
            "source": "bundled" if bundled else "managed",
        }
        self.active.write_text(json.dumps(active_state), encoding="utf-8")
        if bootstrap:
            (runtime_root / "active-runtime.json").write_text(json.dumps(active_state), encoding="utf-8")
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
        platform="win32",
        bootstrap=False,
        latest_versions=None,
        installed_probe=None,
        resolved_packages=None,
        prepare_only=False,
        after_plan=None,
        mode=None,
        channel="stable",
        rollback_after=False,
        state_overrides=None,
        expected_resolution_cores=None,
    ):
        bundled_root, env, python = self.make_env(backend, manifest_version=manifest_version, bundled=bundled, platform=platform, bootstrap=bootstrap)
        if state_overrides:
            for path in (self.active, env / "pymss-runtime-state.json"):
                state = json.loads(path.read_text(encoding="utf-8"))
                path.write_text(json.dumps({**state, **state_overrides}), encoding="utf-8")
        base = probe(backend, pymss="2.1.3")
        final = overlay_probe or probe(backend)
        probe_calls = [base, base, final]
        commands = []

        def live_versions(_python, _names, overlay=None):
            if overlay:
                active = json.loads(self.active.read_text(encoding="utf-8"))
                if active.get("overlayPath"):
                    return active["packageVersions"]
            return (installed_probe or base)["packageVersions"]

        def popen(command, **kwargs):
            command_status = 0 if "--dry-run" in command else pip_status
            if "--dry-run" in command:
                for requirement in expected_resolution_cores or ():
                    self.assertIn(requirement, command)
                self.assertNotIn("--target", command)
                self.assertNotIn("--no-deps", command)
                constraints = Path(command[command.index("--constraint") + 1]).read_text(encoding="utf-8")
                self.assertIn(f"torch=={base['torchVersion']}", constraints)
                selected = {name: version for name, version in final["packageVersions"].items() if base["packageVersions"].get(name) != version}
                selected.update(resolved_packages or {})
                Path(command[command.index("--report") + 1]).write_text(json.dumps({"version": "1", "install": [
                    {"metadata": {"name": name, "version": version}} for name, version in selected.items()
                ]}), encoding="utf-8")
            else:
                commands.append(command)
            if platform == "darwin" and bootstrap:
                self.assertEqual(kwargs["env"]["PYTHONHOME"], str(bundled_root.parent))
            return mock.Mock(
                stdout=iter(["pip output\n"]),
                wait=mock.Mock(return_value=command_status),
                returncode=command_status,
                poll=mock.Mock(return_value=command_status),
            )

        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.envs), \
             mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", self.active), \
             mock.patch.object(worker_bootstrap, "BUNDLED_RUNTIME_ENVS_DIR", bundled_root if bundled else None), \
             mock.patch.object(worker_bootstrap, "_manifest", return_value=manifest_value or manifest()), \
             mock.patch.object(worker_bootstrap, "_latest_pypi_version", side_effect=lambda name, channel="stable": (latest_versions or {"pymss": "2.1.4", "pymss-core": "0.1.6"})[name]) as latest, \
             mock.patch.object(worker_bootstrap, "_probe_python_package_versions", side_effect=live_versions), \
             mock.patch.object(worker_bootstrap, "_probe_python_runtime", side_effect=lambda *_args, **_kwargs: probe_calls.pop(0) if probe_calls else (base if rollback_after else final)), \
             mock.patch.object(worker_bootstrap, "_verify_runtime_audio", side_effect=audio_error) as audio_probe, \
             mock.patch.object(worker_bootstrap, "_runtime_pip_works", return_value=True), \
             mock.patch.object(worker_bootstrap.subprocess, "Popen", side_effect=popen), \
             mock.patch.object(sys, "platform", platform), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            payload = {
                "backend": backend,
                "pythonPath": str(python),
                "mirror": "pypi",
                "taskId": "overlay-update",
                "repairDependencies": repair,
                "mode": mode,
                "channel": channel,
            }
            result = worker_bootstrap.cmd_update_runtime_core({**payload, "prepareOnly": True})
            if result == 0 and not prepare_only:
                plan = next(json.loads(line)["payload"] for line in output.getvalue().splitlines() if json.loads(line)["type"] == "runtime_core_update_plan")
                if after_plan:
                    after_plan(plan, self.active)
                result = worker_bootstrap.cmd_update_runtime_core({**payload, "planId": plan["id"]})
                if result == 0 and rollback_after:
                    probe_calls.extend([base, base, base])
                    restore = {**payload, "mode": "rollback", "prepareOnly": True}
                    result = worker_bootstrap.cmd_update_runtime_core(restore)
                    self.assertEqual(result, 0)
                    plans = [json.loads(line)["payload"] for line in output.getvalue().splitlines() if json.loads(line)["type"] == "runtime_core_update_plan"]
                    result = worker_bootstrap.cmd_update_runtime_core({**payload, "mode": "rollback", "planId": plans[-1]["id"]})
                    with mock.patch.object(worker_bootstrap, "_detect_gpu_vendors", return_value=[]):
                        self.assertTrue(worker_bootstrap._runtime_info_payload({})["rollbackAvailable"])
            self.assertLessEqual(latest.call_count, 2)
            if result == 0 and not prepare_only:
                self.assertEqual(audio_probe.call_count, 2 if rollback_after else 1)
                self.assertEqual(audio_probe.call_args.args[0], python)
                if rollback_after:
                    self.assertIsNone(audio_probe.call_args.args[1])
                else:
                    self.assertEqual(audio_probe.call_args.args[1].name, "site-packages")
        return result, commands, env, python, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_update_can_exceed_release_pins_and_install_new_dependencies(self):
        pinned = manifest()
        pinned["common"].update({"pymss": "pymss[proxy]==2.1.4", "pymss-core": "pymss-core==0.1.6"})
        result, commands, _env, _python, _events = self.run_update(
            manifest_value=pinned, latest_versions={"pymss": "2.2.0", "pymss-core": "0.2.0"},
            overlay_probe=probe("cpu", pymss="2.2.0", core="0.2.0"),
            resolved_packages={"new-audio-dependency": "1.0"},
        )
        self.assertEqual(result, 0)
        self.assertIn("pymss[proxy]==2.2.0", commands[0])
        self.assertIn("pymss-core==0.2.0", commands[0])
        self.assertIn("new-audio-dependency==1.0", commands[0])
        self.assertEqual(json.loads(self.active.read_text(encoding="utf-8"))["pymssVersion"], "2.2.0")

    def test_repair_keeps_live_newer_core_instead_of_stale_state_or_release_pins(self):
        pinned = manifest()
        pinned["common"].update({"pymss": "pymss[proxy]==2.1.4", "pymss-core": "pymss-core==0.1.6"})
        current = probe("cpu", pymss="2.2.0", core="0.2.0")
        result, commands, *_ = self.run_update(
            manifest_value=pinned, repair=True, installed_probe=current, overlay_probe=current,
            latest_versions={"pymss": None, "pymss-core": None},
        )
        self.assertEqual(result, 0)
        self.assertIn("pymss[proxy]==2.2.0", commands[0])
        self.assertIn("pymss-core==0.2.0", commands[0])

    def test_synchronization_never_downgrades_a_newer_installed_core(self):
        current = probe("cpu", pymss="2.2.0", core="0.2.0")
        result, commands, *_ = self.run_update(mode="sync", installed_probe=current, overlay_probe=current)
        self.assertEqual(result, 0)
        self.assertIn("pymss[proxy]==2.2.0", commands[0])

    def test_current_manifest_sync_upgrades_an_old_v1_core_to_the_v2_baseline(self):
        old = probe("cpu", pymss="2.1.8", core="0.1.11")
        current = current_core_manifest()
        result, commands, _env, _python, _events = self.run_update(
            mode="sync", manifest_value=current, manifest_version="2026.10.3",
            installed_probe=old, state_overrides=old,
            overlay_probe=probe("cpu", pymss="2.2.0", core="0.1.14"),
            latest_versions={"pymss": None, "pymss-core": None},
            expected_resolution_cores=("pymss[proxy]==2.2.0", "pymss-core==0.1.14"),
        )
        self.assertEqual(result, 0)
        self.assertIn("pymss[proxy]==2.2.0", commands[0])
        self.assertIn("pymss-core==0.1.14", commands[0])
        active = json.loads(self.active.read_text(encoding="utf-8"))
        self.assertEqual(active["manifestVersion"], current["manifestVersion"])
        self.assertEqual(active["pymssVersion"], "2.2.0")

    def test_current_manifest_sync_preserves_a_newer_installed_core(self):
        installed = probe("cpu", pymss="3.0.0", core="1.0.0")
        result, commands, *_ = self.run_update(
            mode="sync", manifest_value=current_core_manifest(), installed_probe=installed,
            overlay_probe=installed, latest_versions={"pymss": None, "pymss-core": None},
            expected_resolution_cores=("pymss[proxy]==3.0.0", "pymss-core==1.0.0"),
        )
        self.assertEqual(result, 0)
        self.assertIn("pymss[proxy]==3.0.0", commands[0])
        self.assertIn("pymss-core==1.0.0", commands[0])

    def test_current_manifest_updates_beyond_the_minimum_version(self):
        result, commands, *_ = self.run_update(
            mode="update", manifest_value=current_core_manifest(),
            latest_versions={"pymss": "2.3.0", "pymss-core": "0.2.0"},
            overlay_probe=probe("cpu", pymss="2.3.0", core="0.2.0"),
        )
        self.assertEqual(result, 0)
        self.assertIn("pymss[proxy]==2.3.0", commands[0])
        self.assertIn("pymss-core==0.2.0", commands[0])

    def test_accelerator_dependency_resolution_failure_preserves_active_environment(self):
        result, commands, *_ = self.run_update(resolved_packages={"torch": "2.8.0"})
        self.assertNotEqual(result, 0)
        self.assertEqual(commands, [])
        self.assertNotIn("overlayPath", json.loads(self.active.read_text(encoding="utf-8")))

    def test_missing_latest_version_does_not_install_or_modify_the_active_environment(self):
        result, commands, *_ = self.run_update(latest_versions={"pymss": None, "pymss-core": None})
        self.assertNotEqual(result, 0)
        self.assertEqual(commands, [])
        self.assertNotIn("overlayPath", json.loads(self.active.read_text(encoding="utf-8")))

    def test_changed_torch_is_not_activated(self):
        result, _commands, *_ = self.run_update(overlay_probe={**probe("cpu"), "torchVersion": "2.8.0"})
        self.assertNotEqual(result, 0)
        self.assertNotIn("overlayPath", json.loads(self.active.read_text(encoding="utf-8")))

    def check_bundled_platform_update(self, platform, backend, *, bootstrap=False):
        result, commands, env, python, _events = self.run_update(
            backend, platform=platform, bundled=True, bootstrap=bootstrap,
        )
        self.assertEqual(result, 0)
        self.assertEqual(commands[0][0], str(python))
        self.assertEqual(json.loads((env / "pymss-runtime-state.json").read_text(encoding="utf-8"))["manifestVersion"], "2026.09.2")
        self.assertTrue(Path(json.loads(self.active.read_text(encoding="utf-8"))["overlayPath"]).is_relative_to(self.envs))

    def test_linux_bundled_environment_uses_a_user_overlay(self):
        self.check_bundled_platform_update("linux", "cpu")

    def test_macos_bundled_bootstrap_uses_a_user_overlay(self):
        self.check_bundled_platform_update("darwin", "mlx", bootstrap=True)

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
            ["prepare", "resolve", "prepare", "update", "verify", "activate"],
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
        self.assertIn("new-package==1.2.0", command)
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
                result, commands, _env, _python, events = self.run_update(manifest_version=value)
                self.assertNotEqual(result, 0)
                self.assertEqual(commands, [])
                error = events[-1]["payload"]
                self.assertEqual(error["code"], "RUNTIME_MANIFEST_INCOMPATIBLE")
                self.assertTrue(error["recoverable"])
                self.assertEqual(error["manifestStatus"], "newer" if value == "2026.10.1" else "unknown")
                self.assertEqual(error["runtimeManifestVersion"], value)
                self.assertEqual(error["appManifestVersion"], "2026.09.3")
                self.assertIn("Update Pymss Studio first", error["message"])
                self.assertEqual(json.loads(self.active.read_text(encoding="utf-8"))["manifestVersion"], value)

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
        self.assertEqual(len(generations), 1)
        self.assertFalse(stale_staging.exists())


if __name__ == "__main__":
    unittest.main()
