from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import worker_bootstrap as worker
from . import test_runtime_core_update as fixture

manifest = fixture.manifest
probe = fixture.probe


class CorePlanTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.RuntimeOverlayUpdateTests("test_success_builds_overlay_without_torch_or_base_mutation")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_online_update_uses_the_selected_index_despite_inherited_offline_settings(self):
        environment = {"PIP_NO_INDEX": "1", "HTTPS_PROXY": "http://127.0.0.1:9", "PIP_CERT": "runtime-ca.pem"}
        with mock.patch.dict(os.environ, environment):
            status, commands, *_ = self.case.run_update(
                backend="mlx", bundled=True, platform="darwin", bootstrap=True,
                expected_pip_environment={**environment, "PIP_NO_INDEX": "0"},
            )
        self.assertEqual(status, 0)
        self.assertIn("--index-url", commands[0])
        self.assertEqual(commands[0][commands[0].index("--index-url") + 1], worker.PYPI_MIRROR_URLS["pypi"])

    def test_dependency_sync_preserves_offline_verification_settings(self):
        with mock.patch.dict(os.environ, {"PIP_NO_INDEX": "1"}):
            status, *_ = self.case.run_update(mode="sync", expected_pip_environment={"PIP_NO_INDEX": "1"})
        self.assertEqual(status, 0)

    def test_preparation_never_installs_or_activates_packages(self):
        status, commands, _env, _python, events = self.case.run_update(prepare_only=True)
        self.assertEqual(status, 0)
        self.assertEqual(commands, [])
        self.assertNotIn("overlayPath", json.loads(self.case.active.read_text(encoding="utf-8")))
        plan = next(event["payload"] for event in events if event["type"] == "runtime_core_update_plan")
        self.assertEqual(plan["targetVersions"], {"pymss": "2.1.4", "pymss-core": "0.1.6"})

    def test_preparation_preserves_other_staging_and_never_recovers_transactions(self):
        foreign = self.case.envs / ".overlays/cpu/.other.staging"
        foreign.mkdir(parents=True)
        (foreign / "keep").write_bytes(b"owned by another operation")
        with mock.patch.object(worker, "_recover_runtime_transactions", wraps=worker._recover_runtime_transactions) as recover:
            status, *_ = self.case.run_update(prepare_only=True)
        self.assertEqual(status, 0)
        recover.assert_not_called()
        self.assertEqual((foreign / "keep").read_bytes(), b"owned by another operation")

    def test_preparation_requires_explicit_recovery_before_using_an_interrupted_runtime(self):
        for name in (".rocm-transaction.json", ".overlay-activation.json", ".cpu.reinstalling", ".cpu.core-backup", ".cpu.core-updating"):
            with self.subTest(marker=name):
                marker = self.case.envs / name
                marker.write_text("interrupted", encoding="utf-8")
                with mock.patch.object(worker, "_recover_runtime_transactions") as recover:
                    status, commands, *_rest, events = self.case.run_update(prepare_only=True)
                self.assertNotEqual(status, 0)
                self.assertEqual(commands, [])
                self.assertEqual(events[-1]["payload"]["code"], "RUNTIME_RECOVERY_REQUIRED")
                self.assertEqual(marker.read_text(encoding="utf-8"), "interrupted")
                recover.assert_not_called()
                marker.unlink()

    def test_preparation_preserves_the_previous_update_log_even_when_resolution_fails(self):
        root = self.case.envs / ".overlays/cpu"
        root.mkdir(parents=True)
        log = root / "pymss-core-update.log"
        log.write_text("original update failure", encoding="utf-8")
        status, *_rest, events = self.case.run_update(prepare_only=True)
        self.assertEqual(status, 0)
        self.assertEqual(log.read_text(encoding="utf-8"), "original update failure")
        started = next(event["payload"] for event in events if event["type"] == "runtime_core_update_started")
        self.assertNotIn("logPath", started)

        def fail(_command):
            raise RuntimeError("resolution failed")

        status, *_rest, events = self.case.run_update(prepare_only=True, on_resolve=fail)
        self.assertNotEqual(status, 0)
        self.assertEqual(log.read_text(encoding="utf-8"), "original update failure")
        self.assertNotIn("logPath", events[-1]["payload"])
        self.assertTrue(Path(events[-1]["payload"]["planLogPath"]).is_file())

    def test_preparation_does_not_require_user_recovery_for_separate_bundle_markers(self):
        bundle = self.case.root / "bundled-envs"
        bundle.mkdir()
        marker = bundle / ".overlay-activation.json"
        marker.write_bytes(b"immutable bundle marker")
        status, *_ = self.case.run_update(prepare_only=True, bundled=True)
        self.assertEqual(status, 0)
        self.assertEqual(marker.read_bytes(), b"immutable bundle marker")

    def test_preparation_owns_and_cleans_only_its_scratch_directory(self):
        scratch = self.case.root / "scratch"
        scratch.mkdir()
        reports = []

        def inspect(command):
            report = Path(command[command.index("--report") + 1])
            self.assertEqual(report.parent.parent, scratch)
            reports.append(report)

        with mock.patch.dict(os.environ, {"PYMSS_SCRATCH_DIR": str(scratch)}):
            for _ in range(2):
                self.assertEqual(self.case.run_update(prepare_only=True, on_resolve=inspect)[0], 0)
        self.assertNotEqual(reports[0].parent, reports[1].parent)
        self.assertEqual(list(scratch.iterdir()), [])

    def test_cancelled_preparation_leaves_the_active_runtime_and_foreign_files_untouched(self):
        scratch = self.case.root / "scratch"
        scratch.mkdir()
        foreign = self.case.envs / ".overlays/cpu/.other.staging"
        foreign.mkdir(parents=True)
        (foreign / "keep").write_bytes(b"preserved")

        def cancel(_command):
            raise KeyboardInterrupt

        with mock.patch.dict(os.environ, {"PYMSS_SCRATCH_DIR": str(scratch)}), self.assertRaises(KeyboardInterrupt):
            self.case.run_update(prepare_only=True, on_resolve=cancel)
        self.assertNotIn("overlayPath", json.loads(self.case.active.read_text(encoding="utf-8")))
        self.assertEqual((foreign / "keep").read_bytes(), b"preserved")
        self.assertEqual(list(scratch.iterdir()), [])

    def test_read_only_target_discovery_does_not_create_an_environment_cache(self):
        _root, env, python = self.case.make_env()
        state = env / "pymss-runtime-state.json"
        state.unlink()
        with mock.patch.object(worker, "RUNTIME_ENVS_DIR", self.case.envs), \
             mock.patch.object(worker, "ACTIVE_RUNTIME_FILE", self.case.active), \
             mock.patch.object(worker, "BUNDLED_RUNTIME_ENVS_DIR", None), \
             mock.patch.object(worker, "_manifest", return_value=manifest()), \
             mock.patch.object(worker, "_probe_python_runtime", return_value=probe("cpu")):
            target = worker._target_runtime_from_payload({"pythonPath": str(python)}, "cpu", repair=False)
        self.assertIsNotNone(target[0])
        self.assertFalse(state.exists())

    def test_apply_uses_the_confirmed_versions_after_upstream_changes(self):
        latest = {"pymss": "2.1.4", "pymss-core": "0.1.6"}
        def change_upstream(_plan, _active):
            latest["pymss"] = "9.0.0"
        status, commands, *_ = self.case.run_update(latest_versions=latest, after_plan=change_upstream)
        self.assertEqual(status, 0)
        self.assertIn("pymss[proxy]==2.1.4", commands[0])

    def test_dependency_sync_preserves_newer_core_without_querying_upstream(self):
        current = probe("cpu", pymss="2.2.0", core="0.2.0")
        def verify_no_query(plan, _active):
            worker._latest_pypi_version.assert_not_called()
            self.assertEqual(plan["mode"], "sync")
        status, commands, *_ = self.case.run_update(mode="sync", installed_probe=current, overlay_probe=current, after_plan=verify_no_query)
        self.assertEqual(status, 0)
        self.assertIn("pymss[proxy]==2.2.0", commands[0])

    def test_resolver_can_select_a_compatible_core_instead_of_both_global_latest_versions(self):
        final = probe("cpu", pymss="2.2.0", core="0.1.7")
        status, commands, *_ = self.case.run_update(
            latest_versions={"pymss": "2.2.0", "pymss-core": "0.2.0"}, overlay_probe=final,
        )
        self.assertEqual(status, 0)
        self.assertIn("pymss-core==0.1.7", commands[0])

    def test_pymss_update_resolves_its_core_without_querying_core_releases(self):
        final = probe("cpu", pymss="2.2.0", core="0.1.7")
        status, commands, *_ = self.case.run_update(
            latest_versions={"pymss": "2.2.0"}, overlay_probe=final,
        )
        self.assertEqual(status, 0)
        self.assertIn("pymss[proxy]==2.2.0", commands[0])
        self.assertIn("pymss-core==0.1.7", commands[0])

    def test_core_only_resolution_cannot_be_confirmed_as_a_pymss_update(self):
        compatible = manifest()
        compatible["common"]["pymss"] = "pymss[proxy]>=2.1.3"
        status, commands, *_rest, events = self.case.run_update(
            manifest_value=compatible,
            latest_versions={"pymss": "2.1.3", "pymss-core": "0.1.7"},
            overlay_probe=probe("cpu", pymss="2.1.3", core="0.1.7"),
        )
        self.assertNotEqual(status, 0)
        self.assertEqual(commands, [])
        self.assertIn("No newer pymss", events[-1]["payload"]["message"])

    def test_uninstallable_newer_release_cannot_turn_into_a_successful_no_op_update(self):
        status, commands, *_rest, events = self.case.run_update(overlay_probe=probe("cpu", pymss="2.1.3"))
        self.assertNotEqual(status, 0)
        self.assertEqual(commands, [])
        self.assertIn("No newer pymss version could be resolved", events[-1]["payload"]["message"])

    def test_expired_plan_or_changed_active_environment_is_rejected_before_installation(self):
        def expire(plan, _active):
            path = self.case.envs / ".overlays/.plans" / f"{plan['id']}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["createdAt"] = 0
            path.write_text(json.dumps(data), encoding="utf-8")
        status, commands, *_ = self.case.run_update(after_plan=expire)
        self.assertNotEqual(status, 0)
        self.assertEqual(commands, [])

    def test_an_environment_switch_invalidates_a_confirmed_plan(self):
        def switch(_plan, active):
            data = json.loads(active.read_text(encoding="utf-8"))
            overlay = self.case.envs / ".overlays/cpu/another/site-packages"
            overlay.mkdir(parents=True)
            data["overlayPath"] = str(overlay)
            active.write_text(json.dumps(data), encoding="utf-8")
        status, commands, *_ = self.case.run_update(after_plan=switch)
        self.assertNotEqual(status, 0)
        self.assertEqual(commands, [])

    def test_channel_controls_prerelease_targets(self):
        final = probe("cpu", pymss="2.2.0rc1")
        status, commands, *_ = self.case.run_update(
            channel="prerelease", latest_versions={"pymss": "2.2.0rc1", "pymss-core": "0.1.6"}, overlay_probe=final,
        )
        self.assertEqual(status, 0)
        self.assertIn("--pre", commands[0])
        self.assertIn("pymss[proxy]==2.2.0rc1", commands[0])

    def test_plan_ids_cannot_reference_arbitrary_files(self):
        with self.assertRaises(ValueError):
            worker._load_core_update_plan("../../settings/app.json")

    def test_unwritable_logs_cannot_turn_a_committed_update_into_a_failure(self):
        real_open = Path.open
        def unavailable_log(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            if path.name == "pymss-core-update.log" and mode == "a":
                raise OSError("log storage unavailable")
            return real_open(path, *args, **kwargs)
        with mock.patch.object(Path, "open", unavailable_log):
            status, _commands, _env, _python, events = self.case.run_update()
        self.assertEqual(status, 0)
        self.assertEqual(events[-1]["type"], "runtime_core_update_finished")

    def test_rollback_updates_active_state_and_managed_cache_and_survives_a_fresh_query(self):
        compatible = manifest()
        compatible["common"]["pymss"] = "pymss[proxy]>=2.1.3"
        status, commands, env, _python, _events = self.case.run_update(manifest_value=compatible, rollback_after=True)
        self.assertEqual(status, 0)
        self.assertEqual(len(commands), 1)
        active = json.loads(self.case.active.read_text(encoding="utf-8"))
        cached = json.loads((env / "pymss-runtime-state.json").read_text(encoding="utf-8"))
        self.assertEqual(active["pymssVersion"], "2.1.3")
        self.assertEqual(cached["pymssVersion"], active["pymssVersion"])
        self.assertEqual(cached.get("overlayPath"), active.get("overlayPath"))
        self.assertEqual(active["previousRuntime"]["pymssVersion"], "2.1.4")

    def test_missing_cached_torch_version_does_not_disable_verified_core_rollback(self):
        compatible = manifest()
        compatible["common"]["pymss"] = "pymss[proxy]>=2.1.3"
        status, _commands, _env, _python, events = self.case.run_update(
            manifest_value=compatible, state_overrides={"torchVersion": None}, rollback_after=True,
        )
        self.assertEqual(status, 0)
        finished = [event["payload"] for event in events if event["type"] == "runtime_core_update_finished"]
        self.assertTrue(finished[0]["rollbackAvailable"])
        active = json.loads(self.case.active.read_text(encoding="utf-8"))
        self.assertEqual(active["pymssVersion"], "2.1.3")
        self.assertEqual(active["torchVersion"], "2.7.1")
        self.assertEqual(active["previousRuntime"]["torchVersion"], "2.7.1")


class ChannelTests(unittest.TestCase):
    def test_discovery_excludes_yanked_and_empty_releases_and_follows_channel(self):
        releases = {"2.1.9": [{}], "2.2.0rc1": [{}], "9.0.0": [{"yanked": True}], "10.0.0": [], "invalid": [{}]}
        data = json.dumps({"releases": releases}).encode()
        with mock.patch.object(worker.urllib.request, "urlopen", side_effect=lambda *_args, **_kwargs: io.BytesIO(data)):
            self.assertEqual(worker._latest_pypi_version("pymss", "stable"), "2.1.9")
            self.assertEqual(worker._latest_pypi_version("pymss", "prerelease"), "2.2.0rc1")

    def test_pep440_comparison_detects_rc_to_final_and_avoids_channel_downgrades(self):
        with mock.patch.object(worker, "_latest_pypi_version", return_value="2.2.0"), mock.patch.object(worker, "_emit") as emit:
            worker.cmd_runtime_core_versions({"channel": "stable", "versions": {"pymss": "2.2.0rc1"}})
            self.assertTrue(emit.call_args.args[1]["packages"]["pymss"]["updateAvailable"])
            worker.cmd_runtime_core_versions({"channel": "stable", "versions": {"pymss": "2.3.0rc1"}})
            self.assertFalse(emit.call_args.args[1]["packages"]["pymss"]["updateAvailable"])


class RuntimeOverlayDeletionTests(unittest.TestCase):
    def test_deleting_an_idle_source_preserves_the_active_source_and_rollback(self):
        for indexed_source in ("managed", "bundled"):
            with self.subTest(indexed_source=indexed_source):
                case = fixture.RuntimeOverlayUpdateTests("test_success_builds_overlay_without_torch_or_base_mutation")
                case.setUp()
                self.addCleanup(case.doCleanups)
                _root, managed_env, managed_python = case.make_env()
                bundled_root, _env, bundled_python = case.make_env(bundled=True)
                with mock.patch.object(worker, "RUNTIME_ENVS_DIR", case.envs), \
                     mock.patch.object(worker, "ACTIVE_RUNTIME_FILE", case.active), \
                     mock.patch.object(worker, "BUNDLED_RUNTIME_ENVS_DIR", bundled_root), \
                     mock.patch.object(worker, "_manifest", return_value=manifest()), \
                     mock.patch.object(worker.sys, "platform", "win32"), \
                     mock.patch.object(worker, "_emit"):
                    states = {}
                    for source, python in (("managed", managed_python), ("bundled", bundled_python)):
                        overlays = []
                        for generation in ("current", "previous"):
                            overlay = worker._runtime_overlay_root() / "cpu" / f"{source}-{generation}" / "site-packages"
                            overlay.mkdir(parents=True)
                            overlays.append(str(overlay))
                        states[source] = {
                            "backend": "cpu", "pythonPath": str(python), "overlayPath": overlays[0],
                            "previousRuntime": {"pythonPath": str(python), "overlayPath": overlays[1]},
                        }
                    worker._atomic_write_json(case.active, states["bundled"])
                    selection = worker._runtime_overlay_selection_path("cpu")
                    worker._atomic_write_json(selection, states[indexed_source])
                    managed_selection = worker._runtime_overlay_selection_path("cpu", managed_python)
                    worker._atomic_write_json(managed_selection, states["managed"])
                    # Also exercise an active pointer without a matching selection index.
                    if indexed_source == "bundled":
                        bundled_selection = worker._runtime_overlay_selection_path("cpu", bundled_python)
                        worker._atomic_write_json(bundled_selection, states["bundled"])
                    result = worker.cmd_delete_runtime({"backend": "cpu", "pythonPath": str(managed_python)})
                    self.assertEqual(result, 0)
                    self.assertFalse(managed_env.exists())
                    self.assertFalse(managed_selection.exists())
                    self.assertEqual(json.loads(case.active.read_text(encoding="utf-8")), states["bundled"])
                    for state in (states["bundled"], states["bundled"]["previousRuntime"]):
                        self.assertTrue(Path(state["overlayPath"]).is_dir())
                    for state in (states["managed"], states["managed"]["previousRuntime"]):
                        self.assertFalse(Path(state["overlayPath"]).exists())
                    if indexed_source == "bundled":
                        self.assertEqual(json.loads(selection.read_text(encoding="utf-8")), states["bundled"])
                        self.assertEqual(worker._registered_runtime_overlay("cpu", bundled_python), states["bundled"])
                    else:
                        self.assertFalse(selection.exists())


class LegacyRuntimeOverlayTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.user = self.root / "user/runtime-envs"
        self.bundled = self.root / "package/python-runtime/runtime-envs"
        self.python = self.bundled / "cpu/bin/python"
        self.python.parent.mkdir(parents=True)
        self.python.write_text("stub", encoding="utf-8")
        self.user.mkdir(parents=True)
        self.state = {"backend": "cpu", "pythonPath": "cpu/bin/python", **probe("cpu")}
        for key, generation in (("overlayPath", "current"), ("previousRuntime", "previous")):
            overlay = self.bundled / ".overlays/cpu" / generation / "site-packages"
            overlay.mkdir(parents=True)
            if key == "overlayPath":
                self.state[key] = str(overlay)
            else:
                self.state[key] = {**probe("cpu"), "pythonPath": str(self.python), "overlayPath": str(overlay)}
        self.pointer = self.bundled / "active-runtime.json"
        self.pointer.write_text(json.dumps(self.state), encoding="utf-8")
        self.enterContext(mock.patch.object(worker, "RUNTIME_ENVS_DIR", self.user))
        self.enterContext(mock.patch.object(worker, "ACTIVE_RUNTIME_FILE", self.user / "active-runtime.json"))
        self.enterContext(mock.patch.object(worker, "BUNDLED_RUNTIME_ENVS_DIR", self.bundled))
        linux_manifest = manifest()
        linux_manifest["backends"]["cpu"]["platforms"] = ["linux"]
        self.enterContext(mock.patch.object(worker, "_manifest", return_value=linux_manifest))
        self.enterContext(mock.patch.object(worker.sys, "platform", "linux"))

    def test_old_overlay_is_used_by_version_probes_and_remains_available_for_rollback(self):
        with mock.patch.object(worker, "_probe_python_runtime", return_value=probe("cpu")) as live, \
             mock.patch.object(worker, "_module_available", return_value=False), \
             mock.patch.object(worker, "_envs_with_live_active", return_value=[]), \
             mock.patch.object(worker, "_detect_gpu_vendors", return_value=[]):
            info = worker._runtime_info_payload({}, repair=False)
        self.assertEqual(info["installState"]["overlayPath"], self.state["overlayPath"])
        self.assertEqual(live.call_args.args, (self.python, [], Path(self.state["overlayPath"])))
        self.assertTrue(info["rollbackAvailable"])
        self.assertFalse((self.user / "active-runtime.json").exists())

    def test_user_pointer_can_keep_an_old_overlay_and_relative_paths_resolve_from_the_record(self):
        for relative in (False, True):
            with self.subTest(relative=relative):
                state = {**self.state, "pythonPath": str(self.python)}
                if relative:
                    state["overlayPath"] = ".overlays/cpu/current/site-packages"
                    path = self.pointer
                else:
                    path = self.user / "active-runtime.json"
                worker._atomic_write_json(path, state)
                selected = worker._resolve_runtime_state_from(path)
                self.assertEqual(selected["overlayPath"], self.state["overlayPath"])
                self.assertEqual(worker._overlay_path_from_state(selected), Path(self.state["overlayPath"]))

    def test_missing_or_outside_overlays_are_rejected_without_modifying_the_record(self):
        outside = self.root / "outside/site-packages"
        outside.mkdir(parents=True)
        for overlay in (outside, self.bundled / ".overlays/missing", self.user / ".overlays/../../../outside/site-packages"):
            with self.subTest(overlay=overlay):
                state = {**self.state, "overlayPath": str(overlay)}
                worker._atomic_write_json(self.pointer, state)
                self.assertNotIn("overlayPath", worker._read_runtime_state())
                self.assertIsNone(worker._overlay_path_from_state(state))
                self.assertEqual(json.loads(self.pointer.read_text(encoding="utf-8")), state)


class ActivationRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory).resolve()
        self.active = self.root / "active-runtime.json"
        self.enterContext(mock.patch.object(worker, "RUNTIME_ENVS_DIR", self.root))
        self.enterContext(mock.patch.object(worker, "ACTIVE_RUNTIME_FILE", self.active))
        self.enterContext(mock.patch.object(worker, "_manifest", return_value=manifest()))
        self.selection = worker._runtime_overlay_selection_path("cpu")
        self.selection.parent.mkdir(parents=True)
        self.previous = {"backend": "cpu", "activationId": "previous", "overlayPath": "previous-overlay"}
        self.next = {"backend": "cpu", "overlayPath": "next-overlay"}
        self.active.write_text(json.dumps(self.previous), encoding="utf-8")
        self.selection.write_text(json.dumps(self.previous), encoding="utf-8")

    def test_failed_authoritative_write_restores_the_previous_index(self):
        with mock.patch.object(worker, "_write_runtime_state", side_effect=OSError("injected write failure")):
            with self.assertRaises(OSError):
                worker._commit_runtime_overlay(self.next)
        self.assertEqual(json.loads(self.active.read_text(encoding="utf-8")), self.previous)
        self.assertEqual(json.loads(self.selection.read_text(encoding="utf-8")), self.previous)
        self.assertFalse((self.root / ".overlay-activation.json").exists())

    def test_interruption_before_commit_is_rolled_back_on_recovery(self):
        with mock.patch.object(worker, "_write_runtime_state", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                worker._commit_runtime_overlay(self.next)
        worker._recover_runtime_transactions()
        self.assertEqual(json.loads(self.selection.read_text(encoding="utf-8")), self.previous)
        self.assertEqual(json.loads(self.active.read_text(encoding="utf-8")), self.previous)

    def test_interruption_after_commit_completes_the_index_on_recovery(self):
        with mock.patch.object(worker, "_recover_overlay_activation", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                worker._commit_runtime_overlay(self.next)
        self.selection.write_text(json.dumps(self.previous), encoding="utf-8")
        worker._recover_runtime_transactions()
        self.assertEqual(json.loads(self.active.read_text(encoding="utf-8")), self.next)
        self.assertEqual(json.loads(self.selection.read_text(encoding="utf-8")), self.next)
        self.assertFalse((self.root / ".overlay-activation.json").exists())

    def test_post_commit_index_failure_keeps_the_committed_environment_and_journal(self):
        with mock.patch.object(worker, "_recover_overlay_activation", side_effect=OSError("temporary index failure")):
            worker._commit_runtime_overlay(self.next)
        self.assertEqual(json.loads(self.active.read_text(encoding="utf-8")), self.next)
        self.assertTrue((self.root / ".overlay-activation.json").exists())
        worker._recover_runtime_transactions()
        self.assertFalse((self.root / ".overlay-activation.json").exists())

    def test_source_selection_and_cleanup_preserve_other_environments_of_the_same_backend(self):
        interpreters = []
        for source in ("managed", "bundled"):
            python = self.root / source / "Scripts/python.exe"
            python.parent.mkdir(parents=True)
            python.write_text("stub", encoding="utf-8")
            interpreters.append(python)
        generations = {}
        for name in ("current", "previous", "other-source", "unreferenced"):
            generation = self.selection.parent / name
            (generation / "site-packages").mkdir(parents=True)
            generations[name] = generation
        other = {"backend": "cpu", "pythonPath": str(interpreters[1]), "overlayPath": str(generations["other-source"] / "site-packages")}
        self.selection.write_text(json.dumps(other), encoding="utf-8")
        current = {
            "backend": "cpu", "pythonPath": str(interpreters[0]),
            "overlayPath": str(generations["current"] / "site-packages"),
            "previousRuntime": {"pythonPath": str(interpreters[0]), "overlayPath": str(generations["previous"] / "site-packages")},
        }
        worker._commit_runtime_overlay(current)
        worker._cleanup_runtime_overlays("cpu", {generations["current"]})
        self.assertEqual(worker._registered_runtime_overlay("cpu", interpreters[1])["overlayPath"], other["overlayPath"])
        for name in ("current", "previous", "other-source"):
            self.assertTrue(generations[name].is_dir(), name)
        self.assertFalse(generations["unreferenced"].exists())
        worker._atomic_write_json(worker._runtime_overlay_selection_path("cpu", interpreters[0]), current)
        worker._atomic_write_json(self.selection, {"backend": "cpu", "pythonPath": str(interpreters[0])})
        self.assertIsNone(worker._registered_runtime_overlay("cpu", interpreters[0]))
