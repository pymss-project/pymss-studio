from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock

if __package__:
    from . import test_worker_plugins as marketplace
    from . import test_preinstalled_plugins as preinstalled
else:
    import test_worker_plugins as marketplace
    import test_preinstalled_plugins as preinstalled

import worker_plugin_inventory as inventory
import worker_plugins as plugins


class RuntimeBindingTests(unittest.TestCase):
    source = marketplace.PluginMarketplaceTests.source
    manage = marketplace.PluginMarketplaceTests.manage
    install = marketplace.PluginMarketplaceTests.install
    reset_runtime = staticmethod(marketplace.PluginMarketplaceTests.reset_runtime)
    bundle = preinstalled.PreinstalledPluginTests.bundle

    def setUp(self):
        marketplace.PluginMarketplaceTests.setUp(self)
        self.paths = list(sys.path)
        self.bundles = self.work / "bundled"
        self.bundles.mkdir()
        patch = mock.patch.object(plugins, "_BUNDLED_ROOT", self.bundles)
        patch.start()
        self.addCleanup(patch.stop)

    def switch(self, backend):
        self.reset_runtime()
        sys.path[:] = self.paths
        (self.work / "active.json").write_text(json.dumps({"backend": backend}), encoding="utf-8")
        self.root = plugins._root()
        return self.root

    def row(self, name="hello"):
        return next(row for row in plugins._inventory(self.root)["installed"] if row["managed"] and row["name"] == name)

    def test_switching_runtime_keeps_the_installation_record_without_loading_its_view(self):
        self.install(self.source())
        cpu = self.root
        before = (cpu / "installed/hello/.pymss-install.json").read_bytes()
        self.switch("cuda")
        row = self.row()
        self.assertEqual(row["version"], "1.0.0")
        self.assertEqual(row["availability"], "not_prepared")
        self.assertFalse(row["loaded"])
        self.assertFalse(row["prepared"])
        self.assertEqual(row["availableBindings"][0]["runtime"], cpu.name)
        self.assertEqual((cpu / "installed/hello/.pymss-install.json").read_bytes(), before)
        self.assertFalse((self.root / "installed/hello").exists())

    def test_preparation_preserves_origin_and_does_not_copy_old_sidecar(self):
        source = self.source()
        self.install(source)
        cpu = self.root
        dependency = cpu / "installed/hello/.studio-deps/previous_runtime.py"
        dependency.parent.mkdir()
        dependency.write_text("value = 7\n", encoding="utf-8")
        self.switch("cuda")
        resolver = plugins._install_dependencies
        with mock.patch.object(plugins, "_install_dependencies", wraps=resolver) as resolved:
            code, events = self.manage("prepare", name="hello", expectedRuntime=self.root.name)
        self.assertEqual(code, 0, events)
        resolved.assert_called_once()
        self.assertTrue(self.row()["prepared"])
        self.assertTrue(self.row()["loaded"])
        self.assertFalse((self.root / "installed/hello/.studio-deps/previous_runtime.py").exists())
        self.assertTrue(dependency.is_file())
        receipt = plugins._manifest(self.root / "installed/hello")
        self.assertEqual(receipt["source"], "path")
        self.assertEqual(receipt["url"], str(source.resolve()))

    def test_remove_in_one_runtime_prevents_loading_a_retained_copy_in_another(self):
        self.install(self.source())
        cpu = self.root
        self.switch("cuda")
        self.assertEqual(self.manage("prepare", name="hello")[0], 0)
        self.assertEqual(self.manage("uninstall", name="hello")[0], 0)
        self.switch("cpu")
        self.assertTrue((cpu / "installed/hello").is_dir())
        self.assertFalse(any(row["name"] == "hello" for row in plugins._inventory(self.root)["installed"]))
        self.assertNotIn("hello.hello", {spec["id"] for spec in plugins.operation_runtime().list_operations()})

    def test_disable_is_global_and_reenable_prepares_the_other_binding_explicitly(self):
        self.install(self.source())
        cpu = self.root
        self.assertEqual(self.manage("disable", name="hello")[0], 0)
        self.switch("cuda")
        self.assertFalse(self.row()["enabled"])
        self.assertEqual(self.manage("enable", name="hello")[0], 0)
        self.switch("cpu")
        self.assertTrue(self.row()["enabled"])
        self.assertFalse(self.row()["prepared"])
        code, events = self.manage("prepare", name="hello")
        self.assertEqual(code, 0, events)
        self.assertTrue(self.row()["loaded"])
        self.assertTrue((cpu / "installed/hello").is_dir())
        self.assertFalse((cpu / "disabled/hello").exists())

    def test_removed_default_stays_removed_and_explicit_restore_wins_old_tombstones(self):
        self.bundle()
        cpu = self.root
        self.assertEqual(self.manage("prepare_preinstalled")[0], 0)
        self.assertEqual(self.manage("uninstall", name="example.convert")[0], 0)
        tombstone = (cpu / "preinstalled-removed.json").read_bytes()
        self.switch("cuda")
        self.assertEqual(plugins._inventory(self.root)["preinstalledPending"], [])
        self.assertEqual(self.manage("prepare_preinstalled")[0], 0)
        self.assertFalse((self.root / "installed/example.convert").exists())
        self.assertEqual(self.manage("install_preinstalled", name="example.convert")[0], 0)
        self.switch("cpu")
        self.assertEqual((cpu / "preinstalled-removed.json").read_bytes(), tombstone)
        self.assertTrue(self.row("example.convert")["enabled"])
        self.assertFalse(self.row("example.convert")["prepared"])
        self.assertEqual(self.manage("prepare", name="example.convert")[0], 0)
        self.assertTrue(self.row("example.convert")["loaded"])

    def test_legacy_version_conflict_requires_an_explicit_source_choice(self):
        source = self.source(version="1.0.0")
        cpu = self.root
        destination = cpu / "installed/hello"
        destination.parent.mkdir(parents=True)
        shutil.copytree(source, destination)
        self.switch("cuda")
        cuda = self.root
        self.source(version="2.0.0")
        destination = cuda / "installed/hello"
        destination.parent.mkdir(parents=True)
        shutil.copytree(source, destination)
        self.switch("rocm")
        self.assertTrue(self.row()["sourceSelectionRequired"])
        code, _events = self.manage("prepare", name="hello")
        self.assertEqual(code, 1)
        code, events = self.manage("prepare", name="hello", sourceRuntime=cuda.name, sourceVersion="2.0.0")
        self.assertEqual(code, 0, events)
        self.assertEqual(self.row()["version"], "2.0.0")
        self.assertEqual(plugins._read_manifest(cpu / "installed/hello")["version"], "1.0.0")

    def test_published_marker_recovers_an_interrupted_global_record_commit(self):
        commit = inventory.commit
        with mock.patch.object(inventory, "commit", side_effect=OSError("Inventory write interrupted")):
            code, _events = self.manage("install", target=str(self.source()))
        self.assertEqual(code, 1)
        self.assertTrue((self.root / "installed/hello").exists())
        self.assertTrue(self.row()["loaded"])
        self.assertGreater(plugins._application_state(self.root)[0]["plugins"]["hello"]["revision"], 0)
        self.assertIs(inventory.commit, commit)

    def test_failed_cleanup_remains_disabled_and_can_be_retried(self):
        self.install(self.source())
        target = self.root / "installed/hello"
        rename = Path.rename

        def fail_current(path, destination):
            if path == target:
                raise PermissionError("Plugin files are in use")
            return rename(path, destination)

        with mock.patch.object(Path, "rename", fail_current):
            code, _events = self.manage("uninstall", name="hello")
        self.assertEqual(code, 1)
        self.assertFalse(self.row()["enabled"])
        self.assertTrue(self.row()["removalPending"])
        self.assertEqual(self.manage("uninstall", name="hello")[0], 0)
        self.assertFalse(any(row["name"] == "hello" for row in plugins._inventory(self.root)["installed"]))

    def test_existing_profile_reviews_defaults_and_explicit_acceptance_is_shared(self):
        self.bundle()
        review = plugins._inventory(self.root, default_policy="review")
        self.assertEqual(review["preinstalledPending"], [])
        self.assertEqual(review["preinstalledAvailable"], ["example.convert"])
        self.assertFalse((self.root / "installed/example.convert").exists())
        self.assertEqual(self.manage("prepare_preinstalled", defaultPolicy="review")[0], 0)
        self.assertEqual(plugins._application_state(self.root)[0]["defaultPolicy"], "auto")
        self.switch("cuda")
        self.assertEqual(plugins._inventory(self.root, default_policy="review")["preinstalledPending"], [])
        self.assertEqual(self.row("example.convert")["availability"], "not_prepared")

    def test_legacy_installations_require_review_even_if_the_settings_file_is_missing(self):
        self.bundle()
        shutil.copytree(self.source(), self.root / "installed/hello")
        snapshot = plugins._inventory(self.root, default_policy="auto")
        self.assertEqual(snapshot["preinstalledPending"], [])
        self.assertEqual(snapshot["preinstalledAvailable"], ["example.convert"])

    def test_damaged_legacy_preferences_prevent_default_preparation(self):
        self.bundle()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "preinstalled-removed.json").write_text("{", encoding="utf-8")
        self.switch("cuda")
        snapshot = plugins._inventory(self.root, default_policy="auto")
        self.assertEqual(snapshot["preinstalledPending"], [])
        self.assertTrue(snapshot["inventoryWarnings"])
        code, events = self.manage("prepare_preinstalled")
        self.assertEqual(code, 1, events)
        self.assertFalse((self.root / "installed/example.convert").exists())

    def test_explicit_reinstall_can_replace_a_retained_removed_copy(self):
        source = self.source()
        self.install(source)
        cpu = self.root
        self.switch("cuda")
        self.assertEqual(self.manage("uninstall", name="hello")[0], 0)
        self.switch("cpu")
        self.assertTrue((cpu / "installed/hello").exists())
        code, events = self.manage("install", target=str(source))
        self.assertEqual(code, 0, events)
        self.assertTrue(self.row()["loaded"])

    def test_same_version_legacy_content_conflict_survives_global_disable(self):
        source = self.source()
        shutil.copytree(source, self.root / "installed/hello")
        self.switch("cuda")
        source.joinpath("__init__.py").write_text(source.joinpath("__init__.py").read_text() + "\nCUSTOM_VERSION = True\n")
        shutil.copytree(source, self.root / "installed/hello")
        self.switch("rocm")
        self.assertTrue(self.row()["sourceSelectionRequired"])
        self.assertEqual(self.manage("disable", name="hello")[0], 0)
        self.assertTrue(self.row()["sourceSelectionRequired"])
        self.assertEqual(self.manage("prepare", name="hello")[0], 1)
        choice = self.row()["availableBindings"][0]
        code, events = self.manage("prepare", name="hello", sourceRuntime=choice["runtime"],
                                   sourceVersion=choice["version"], sourceContent=choice["content"])
        self.assertEqual(code, 0, events)
        self.assertFalse(self.row()["enabled"])
        self.assertEqual(self.manage("enable", name="hello")[0], 0)
        self.assertTrue(self.row()["loaded"])

    def test_superseded_uninstall_does_not_delete_a_newer_enabled_binding(self):
        self.install(self.source())
        reserve = inventory.reserve

        def superseded(root, observations, removed):
            old = reserve(root, observations, removed)
            new = reserve(root, observations, removed)
            current = inventory.merge(root, observations, removed)["plugins"]["hello"]
            inventory.commit(root, "hello", "enabled", new, definition=current["definition"],
                             version=current["version"], content=current.get("content"))
            return old

        with mock.patch.object(inventory, "reserve", side_effect=superseded):
            code, events = self.manage("uninstall", name="hello")
        self.assertEqual(code, 1, events)
        self.assertIn("superseded", events[-1]["payload"]["message"])
        self.assertTrue((self.root / "installed/hello").is_dir())
        self.assertTrue(self.row()["loaded"])

    def test_corrupt_inventory_disables_managed_code_but_keeps_builtin_operations(self):
        self.install(self.source())
        inventory.state_path(self.root).write_text('{"schemaVersion":2}', encoding="utf-8")
        self.reset_runtime()
        runtime = plugins.operation_runtime()
        ids = {operation["id"] for operation in runtime.list_operations()}
        self.assertIn("pymss.kit.convert", ids)
        self.assertNotIn("hello.hello", ids)
        self.assertEqual(json.loads(inventory.state_path(self.root).read_text())["schemaVersion"], 2)

    def test_non_runtime_sibling_cannot_poison_the_global_inventory(self):
        source = self.source()
        sibling = self.root.parent / "cpu (backup)" / "installed/hello"
        shutil.copytree(source, sibling)
        code, events = self.manage("install", target=str(source))
        self.assertEqual(code, 0, events)
        self.assertTrue(self.row()["loaded"])
        self.assertTrue(plugins._inventory(self.root)["inventoryWarnings"])

    def test_cross_store_recovery_finishes_dependency_handoff(self):
        self.install(self.source())
        target = self.root / "installed/hello"
        deps = target / ".studio-deps"
        deps.mkdir()
        (deps / "version.py").write_text("VALUE = 1\n", encoding="utf-8")
        replacement = self.root / "disabled/hello"
        shutil.copytree(target, replacement, ignore=shutil.ignore_patterns(".studio-deps"))
        meta = plugins._manifest(replacement)
        meta["inventory"]["revision"] += 1
        meta["inventory"]["state"] = "disabled"
        replacement.joinpath(".pymss-install.json").write_text(json.dumps(meta), encoding="utf-8")
        replacement.joinpath(plugins._DEPENDENCY_HANDOFF).write_text("1", encoding="utf-8")
        backup = self.root / "backups/hello"
        backup.parent.mkdir(exist_ok=True)
        target.rename(backup)
        plugins._recover(self.root)
        self.assertTrue((replacement / ".studio-deps/version.py").is_file())
        self.assertFalse((replacement / plugins._DEPENDENCY_HANDOFF).exists())
        self.assertFalse(backup.exists())

    def test_failed_trash_cleanup_stays_pending_until_its_runtime_finishes(self):
        self.install(self.source())
        cpu = self.root
        trash = cpu / "transactions/removed-hello"
        remove = shutil.rmtree

        def in_use(path, *args, **kwargs):
            if Path(path) == trash and trash.exists():
                if kwargs.get("ignore_errors"):
                    return
                raise PermissionError("Plugin files are still in use")
            return remove(path, *args, **kwargs)

        with mock.patch.object(shutil, "rmtree", side_effect=in_use):
            code, events = self.manage("uninstall", name="hello")
            self.assertEqual(code, 1, events)
            self.assertTrue(self.row()["removalPending"])
            code, events = self.manage("uninstall", name="hello")
            self.assertEqual(code, 1, events)
            self.assertTrue(self.row()["removalPending"])
            self.switch("cuda")
            code, events = self.manage("uninstall", name="hello")
            self.assertEqual(code, 1, events)
            self.assertIn("source runtime", events[-1]["payload"]["message"])
            self.switch("cpu")
        code, events = self.manage("uninstall", name="hello")
        self.assertEqual(code, 0, events)
        self.assertFalse(trash.exists())

    def test_preparation_keeps_a_disabled_plugin_disabled_in_every_runtime(self):
        self.install(self.source())
        self.assertEqual(self.manage("disable", name="hello")[0], 0)
        self.switch("cuda")
        code, events = self.manage("prepare", name="hello")
        self.assertEqual(code, 0, events)
        self.assertTrue(self.row()["prepared"])
        self.assertFalse(self.row()["enabled"])
        self.assertTrue((self.root / "disabled/hello").exists())
        self.switch("cpu")
        self.assertFalse(self.row()["enabled"])
        self.assertEqual(self.manage("uninstall", name="hello")[0], 0)
        self.switch("cuda")
        code, events = self.manage("prepare", name="hello")
        self.assertEqual(code, 1, events)
        self.assertIn("removed", events[-1]["payload"]["message"])

    def test_reenabling_a_conflicting_sidecar_is_rejected_before_moving_files(self):
        def sidecar(plugin, _root, _task, _payload):
            version = "1.0" if plugin.name == "hello" else "2.0"
            distribution = plugin / ".studio-deps" / f"studio_conflict_fixture-{version}.dist-info"
            distribution.mkdir(parents=True)
            (distribution / "METADATA").write_text(
                f"Metadata-Version: 2.1\nName: studio-conflict-fixture\nVersion: {version}\n", encoding="utf-8")

        with mock.patch.object(plugins, "_install_dependencies", side_effect=sidecar):
            self.install(self.source())
            self.assertEqual(self.manage("disable", name="hello")[0], 0)
            self.install(self.source(name="other"))
        code, events = self.manage("enable", name="hello")
        self.assertEqual(code, 1, events)
        self.assertIn("dependency conflict", events[-1]["payload"]["message"])
        self.assertTrue((self.root / "disabled/hello").exists())
        self.assertFalse(self.row()["enabled"])

    def test_large_cleanup_runs_outside_the_shared_intent_lock(self):
        source = self.source()
        self.install(source)
        self.source(version="2.0.0")
        remove = shutil.rmtree
        checked = []
        cleanup_paths = {self.root / "backups/hello", self.root / "transactions/removed-hello"}

        def observe(path, *args, **kwargs):
            if Path(path) in cleanup_paths and Path(path).exists():
                self.assertIsNone(inventory._HELD_LOCK.get())
                checked.append(Path(path).name)
            return remove(path, *args, **kwargs)

        with mock.patch.object(shutil, "rmtree", side_effect=observe):
            code, events = self.manage("update", name="hello")
            self.assertEqual(code, 0, events)
            code, events = self.manage("uninstall", name="hello")
            self.assertEqual(code, 0, events)
        self.assertEqual(checked, ["hello", "removed-hello"])

    def test_missing_preferred_files_can_be_reinstalled_over_a_stale_binding(self):
        source = self.source()
        self.install(source)
        self.switch("cuda")
        self.assertEqual(self.manage("prepare", name="hello")[0], 0)
        self.source(version="2.0.0")
        self.assertEqual(self.manage("update", name="hello")[0], 0)
        shutil.rmtree(self.root / "installed/hello")
        self.switch("cpu")
        self.assertEqual(self.row()["availability"], "not_prepared")
        self.assertEqual(self.row()["availableBindings"], [])
        code, events = self.manage("install", target=str(source))
        self.assertEqual(code, 0, events)
        self.assertTrue(self.row()["loaded"])
        self.assertEqual(self.row()["version"], "2.0.0")
