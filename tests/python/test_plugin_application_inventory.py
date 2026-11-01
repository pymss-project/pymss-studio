from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap  # noqa: F401
else:
    import _bootstrap as _worker_test_bootstrap  # noqa: F401

import worker_plugin_inventory as inventory


class ApplicationInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name) / "plugins"
        self.cpu = self.parent / "cpu-py312-abc"
        self.cuda = self.parent / "cuda-py312-def"
        self.cpu.mkdir(parents=True)
        self.cuda.mkdir()

    def observation(self, root=None, *, enabled=True, version="1.0.0", revision=None):
        value = {"name": "alice.tool", "binding": (root or self.cpu).name, "version": version,
                 "enabled": enabled, "definition": {"description": "Audio processing"}}
        if revision is not None:
            value["marker"] = {"revision": revision, "state": "enabled" if enabled else "disabled"}
        return value

    def commit(self, root, state, revision, **kwargs):
        inventory.commit(root, "alice.tool", state, revision, definition={"description": "Audio processing"}, version="1.0.0", **kwargs)

    def test_reading_legacy_observations_does_not_create_global_files(self):
        state = inventory.merge(self.cpu, [self.observation()], set())
        self.assertEqual(state["plugins"]["alice.tool"]["state"], "enabled")
        self.assertFalse(inventory.state_path(self.cpu).exists())

    def test_unrelated_mutation_does_not_freeze_legacy_runtime_preferences(self):
        observations = [self.observation(self.cpu), self.observation(self.cuda, enabled=False)]
        inventory.reserve(self.cuda, observations, set())
        inventory.set_default_policy(self.cuda, "auto", observations, set())
        self.assertEqual(json.loads(inventory.state_path(self.cpu).read_text())["plugins"], {})
        self.assertEqual(inventory.merge(self.cpu, observations, set())["plugins"]["alice.tool"]["state"], "enabled")
        self.assertEqual(inventory.merge(self.cuda, observations, set())["plugins"]["alice.tool"]["state"], "disabled")

    def test_superseded_file_mutation_is_rejected_before_entering(self):
        first = inventory.reserve(self.cpu, [], set())
        latest = inventory.reserve(self.cuda, [], set())
        self.commit(self.cuda, "enabled", latest)
        with self.assertRaisesRegex(ValueError, "superseded"):
            with inventory.mutation(self.cpu, "alice.tool", first):
                self.fail("A superseded action must not touch files")

    def test_inventory_replace_retries_a_transient_windows_reader(self):
        replace = inventory.os.replace
        attempts = []

        def busy_once(source, target):
            attempts.append(target)
            if len(attempts) == 1:
                raise PermissionError("Inventory reader is still closing")
            return replace(source, target)

        with mock.patch.object(inventory.os, "replace", side_effect=busy_once):
            revision = inventory.reserve(self.cpu, [], set())
        self.assertEqual(revision, 1)
        self.assertEqual(len(attempts), 2)

    def test_current_runtime_is_preferred_without_selecting_a_higher_legacy_version(self):
        observations = [self.observation(self.cpu, version="9.0.0"), self.observation(self.cuda, version="1.0.0")]
        record = inventory.merge(self.cuda, observations, set())["plugins"]["alice.tool"]
        self.assertEqual(record["version"], "1.0.0")
        self.assertEqual(record["binding"], self.cuda.name)

    def test_legacy_removal_prevents_default_restoration_in_a_different_runtime(self):
        state = inventory.merge(self.cuda, [self.observation(self.cpu)], {"alice.tool"})
        self.assertEqual(state["plugins"]["alice.tool"]["state"], "removed")

    def test_legacy_removal_elsewhere_does_not_disable_the_current_legacy_install(self):
        observations = [self.observation(self.cpu)]
        state = inventory.merge(self.cpu, observations, {"alice.tool"})
        self.assertEqual(state["plugins"]["alice.tool"]["state"], "enabled")
        state = inventory.merge(self.cuda, observations, {"alice.tool"})
        self.assertEqual(state["plugins"]["alice.tool"]["state"], "removed")

    def test_explicit_restore_wins_over_old_removal_preferences(self):
        revision = inventory.reserve(self.cpu, [self.observation()], {"alice.tool"})
        self.commit(self.cpu, "enabled", revision)
        record = inventory.merge(self.cuda, [self.observation()], {"alice.tool"})["plugins"]["alice.tool"]
        self.assertEqual(record["state"], "enabled")

    def test_revision_is_shared_across_runtime_roots(self):
        first = inventory.reserve(self.cpu, [], set())
        second = inventory.reserve(self.cuda, [], set())
        self.assertGreater(second, first)
        self.assertEqual(inventory.state_path(self.cpu), inventory.state_path(self.cuda))

    def test_late_publication_cannot_undo_a_newer_remove_action(self):
        install = inventory.reserve(self.cpu, [], set())
        remove = inventory.reserve(self.cuda, [], set())
        self.commit(self.cuda, "removed", remove)
        self.commit(self.cpu, "enabled", install)
        record = inventory.merge(self.cpu, [self.observation(self.cpu, revision=install)], set())["plugins"]["alice.tool"]
        self.assertEqual(record["state"], "removed")

    def test_committed_binding_recovers_after_intent_file_commit_is_interrupted(self):
        revision = inventory.reserve(self.cpu, [], set())
        record = inventory.merge(self.cuda, [self.observation(self.cpu, revision=revision)], set())["plugins"]["alice.tool"]
        self.assertEqual(record["revision"], revision)
        self.assertEqual(record["binding"], self.cpu.name)

    def test_marker_does_not_activate_a_physically_disabled_plugin(self):
        observation = self.observation(enabled=False)
        observation["marker"] = {"revision": 4, "state": "enabled"}
        record = inventory.merge(self.cpu, [observation], set())["plugins"]["alice.tool"]
        self.assertEqual(record["state"], "disabled")
        self.assertEqual(record["revision"], 0)

    def test_pending_cleanup_remains_visible_until_the_matching_revision_completes(self):
        revision = inventory.reserve(self.cpu, [], set())
        self.commit(self.cpu, "removed", revision, pending="uninstall")
        self.assertEqual(inventory.merge(self.cuda, [], set())["plugins"]["alice.tool"]["pending"]["binding"], self.cpu.name)
        inventory.complete(self.cpu, "alice.tool", revision)
        self.assertNotIn("pending", inventory.merge(self.cuda, [], set())["plugins"]["alice.tool"])

    def test_old_cleanup_cannot_clear_a_newer_user_action(self):
        first = inventory.reserve(self.cpu, [], set())
        self.commit(self.cpu, "removed", first, pending="uninstall")
        second = inventory.reserve(self.cuda, [], set())
        self.commit(self.cuda, "disabled", second, pending="disable")
        inventory.complete(self.cpu, "alice.tool", first)
        record = inventory.merge(self.cuda, [], set())["plugins"]["alice.tool"]
        self.assertIn("pending", record)
        self.assertEqual(record["revision"], second)

    def test_default_policy_does_not_change_plugin_decisions(self):
        revision = inventory.reserve(self.cpu, [], set())
        self.commit(self.cpu, "removed", revision)
        inventory.set_default_policy(self.cuda, "auto", [], set())
        state = inventory.merge(self.cuda, [], set())
        self.assertEqual(state["defaultPolicy"], "auto")
        self.assertEqual(state["plugins"]["alice.tool"]["state"], "removed")

    def test_invalid_global_state_is_reported_instead_of_resetting_user_choices(self):
        inventory.state_path(self.cpu).write_text('{"schemaVersion":2}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "format"):
            inventory.merge(self.cpu, [], set())
        self.assertEqual(json.loads(inventory.state_path(self.cpu).read_text())["schemaVersion"], 2)
