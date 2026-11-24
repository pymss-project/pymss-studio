from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if __package__:
    from . import test_worker_plugins as marketplace
else:
    import test_worker_plugins as marketplace

import worker_plugins as plugins
from pymss_plugin_manager import _engine as management


class PluginUpdateTests(unittest.TestCase):
    setUp = marketplace.PluginMarketplaceTests.setUp
    source = marketplace.PluginMarketplaceTests.source
    manage = marketplace.PluginMarketplaceTests.manage
    install = marketplace.PluginMarketplaceTests.install
    reset_runtime = staticmethod(marketplace.PluginMarketplaceTests.reset_runtime)

    def test_reviewed_update_flows_through_worker_and_keeps_inventory_content_identity(self):
        self.install(self.source())
        self.source(version="2.0.0")
        code, events = self.manage("check_updates", name="hello")
        self.assertEqual(code, 0, events)
        checked = events[-1]["payload"]["updates"]["hello"]
        self.assertEqual(checked["status"], "update")
        self.assertTrue(events[-1]["payload"]["installed"][0]["contentDigest"])
        code, events = self.manage("update", name="hello", expectedUpdate=checked)
        self.assertEqual(code, 0, events)
        self.assertEqual(events[-1]["payload"]["installed"][0]["version"], "2.0.0")

    def test_same_version_bundled_content_can_be_updated_after_the_catalog_advances(self):
        from pymss.plugins import install as installer, marketplace

        self.install(self.source())
        receipt = self.root / "installed/hello/.pymss-install.json"
        metadata = json.loads(receipt.read_text())
        metadata.update(source="git", url=marketplace.GITHUB_REPOSITORY, subpath="plugins/hello", preinstalled=True)
        receipt.write_text(json.dumps(metadata))
        snapshot = self.work / "bundle"
        bundled = snapshot / "hello"
        shutil.copytree(self.source(), bundled)
        (bundled / "ui.html").write_text("File drop support", encoding="utf-8")
        commit = "b" * 40
        row = {"name": "hello", "subpath": "plugins/hello", "url": marketplace.GITHUB_REPOSITORY,
               "version": "1.0.0", "files": {}}
        (snapshot / "manifest.json").write_text(json.dumps({"schemaVersion": 1, "plugins": [row],
            "source": {"local": False, "commit": commit, "repository": marketplace.GITHUB_REPOSITORY}}))

        def acquire(url, destination, *, subpath, ref, temporary_root):
            self.assertEqual(url, marketplace.GITCODE_REPOSITORY)
            self.assertEqual(ref, commit)
            shutil.copytree(bundled, destination)
            return {"resolved_commit": ref}

        with mock.patch.object(plugins, "_BUNDLED_ROOT", snapshot), \
             mock.patch.dict(os.environ, {"PYMSS_PLUGINS_REGISTRY": marketplace.GITCODE_REGISTRY, "PYMSS_PLUGINS_SOURCE": "gitcode"}), \
             mock.patch.object(installer, "list_available",
                               return_value=[{**row, "ref": commit, "url": marketplace.GITCODE_REPOSITORY}]) as catalog, \
             mock.patch.object(installer, "_clone", side_effect=acquire) as clone, \
             mock.patch.object(marketplace, "_json") as compare:
            code, events = self.manage("check_updates", name="hello")
            self.assertEqual(code, 0, events)
            self.assertEqual(events[-1]["payload"]["updates"]["hello"]["status"], "content")
            compare.assert_not_called()
            older_commit = "a" * 40
            marketplace_receipt = {**metadata, "url": marketplace.GITCODE_REPOSITORY,
                                   "preinstalled": False, "defaultRepository": True,
                                   "resolved_commit": older_commit}
            compare.return_value = {"merge_base_commit": {"sha": older_commit}}
            receipt.write_text(json.dumps(marketplace_receipt))
            code, events = self.manage("check_updates", name="hello")
            self.assertEqual(code, 0, events)
            update = events[-1]["payload"]["updates"]["hello"]
            self.assertEqual((update["status"], update["remote_commit"]), ("content", commit))
            compare.assert_called_once_with(
                "https://api.gitcode.com/api/v5/repos/pymss-project/pymss-plugins/"
                f"compare/{older_commit}...{commit}?suffix=.toml",
            )

            latest_commit = "c" * 40
            catalog.return_value = [{**row, "ref": commit, "url": marketplace.GITCODE_REPOSITORY}]
            compare.return_value = {"status": "behind", "merge_base_commit": {"sha": commit}}
            for preinstalled in (False, True):
                with self.subTest(preinstalled=preinstalled):
                    receipt.write_text(json.dumps({**marketplace_receipt, "preinstalled": preinstalled,
                                                   "resolved_commit": latest_commit}))
                    code, events = self.manage("check_updates", name="hello")
                    self.assertEqual(code, 0, events)
                    update = events[-1]["payload"]["updates"]["hello"]
                    self.assertEqual(update["status"], "source_changed")
                    self.assertNotIn("target", update)
                    code, events = self.manage("update", name="hello", expectedUpdate=update)
                    self.assertEqual(code, 1, events)
                    clone.assert_not_called()
                    self.assertEqual(json.loads(receipt.read_text())["resolved_commit"], latest_commit)

            compare.side_effect = OSError("Comparison unavailable")
            receipt.write_text(json.dumps(marketplace_receipt))
            code, events = self.manage("check_updates", name="hello")
            self.assertEqual(code, 0, events)
            update = events[-1]["payload"]["updates"]["hello"]
            self.assertEqual(update["status"], "source_changed")
            self.assertNotIn("target", update)

            receipt.write_text(json.dumps({**marketplace_receipt, "url": "https://github.com/example/plugins"}))
            code, events = self.manage("check_updates", name="hello")
            self.assertEqual(code, 0, events)
            self.assertEqual(events[-1]["payload"]["updates"]["hello"]["status"], "source_changed")

            catalog.return_value = [{**row, "ref": latest_commit, "url": marketplace.GITCODE_REPOSITORY}]
            comparison_base = {
                f"{older_commit}...{commit}": older_commit,
                f"{commit}...{latest_commit}": commit,
            }

            def gitcode_compare(url):
                self.assertTrue(url.startswith("https://api.gitcode.com/"), url)
                pair = url.split("/compare/")[1].split("?")[0]
                return {"merge_base_commit": {"sha": comparison_base.get(pair, commit)}}

            compare.side_effect = gitcode_compare
            comparison_base[f"{commit}...{latest_commit}"] = older_commit
            receipt.write_text(json.dumps(marketplace_receipt))
            code, events = self.manage("check_updates", name="hello")
            self.assertEqual(code, 0, events)
            self.assertEqual(events[-1]["payload"]["updates"]["hello"]["status"], "source_changed")
            comparison_base[f"{commit}...{latest_commit}"] = commit
            receipt.write_text(json.dumps({**metadata, "resolved_commit": "d" * 40}))
            code, events = self.manage("check_updates", name="hello")
            self.assertEqual(code, 0, events)
            update = events[-1]["payload"]["updates"]["hello"]
            self.assertEqual(update["status"], "source_changed")
            self.assertNotIn("target", update)

            for initial, saved in ((marketplace_receipt, False), (metadata, True)):
                with self.subTest(preinstalled=saved):
                    receipt.write_text(json.dumps(initial))
                    self.assertFalse(receipt.with_name("ui.html").exists())
                    code, events = self.manage("check_updates", name="hello")
                    self.assertEqual(code, 0, events)
                    update = events[-1]["payload"]["updates"]["hello"]
                    self.assertEqual((update["status"], update["content_changed"]), ("content", True))
                    self.assertEqual(update["remote_commit"], commit)
                    self.assertEqual(update["target"]["source"]["ref"], commit)
                    code, events = self.manage("update", name="hello", expectedUpdate=update)
                    self.assertEqual(code, 0, events)
                    self.assertEqual(json.loads(receipt.read_text())["resolved_commit"], commit)
                    self.assertTrue(receipt.with_name("ui.html").is_file())
                    if not saved:
                        receipt.with_name("ui.html").unlink()
            code, events = self.manage("check_updates", name="hello")
            self.assertEqual(code, 0, events)
            update = events[-1]["payload"]["updates"]["hello"]
            self.assertEqual((update["status"], update["remote_commit"]), ("source_changed", latest_commit))
            catalog.return_value = [{**row, "ref": commit, "url": marketplace.GITCODE_REPOSITORY}]
            code, events = self.manage("check_updates", name="hello")
            self.assertEqual(code, 0, events)
            self.assertEqual(events[-1]["payload"]["updates"]["hello"]["status"], "latest")

    def test_an_old_core_cannot_silently_ignore_a_reviewed_update(self):
        with mock.patch.object(management, "UPDATE_REVIEW_VERSION", 0), mock.patch.object(plugins, "_service") as service:
            code, events = self.manage("update", name="hello", expectedUpdate={"target": {"schemaVersion": 1}})
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["payload"]["code"], "PLUGIN_RUNTIME_INCOMPATIBLE")
        service.assert_not_called()

    def test_reviewed_official_update_keeps_the_checked_snapshot_through_the_worker(self):
        from pymss.plugins import install as installer, marketplace

        self.install(self.source())
        receipt = self.root / "installed/hello/.pymss-install.json"
        metadata = json.loads(receipt.read_text())
        metadata.update(source="url", defaultRepository=True, url=marketplace.GITCODE_REPOSITORY,
                        subpath="plugins/hello", resolved_commit="a" * 40)
        receipt.write_text(json.dumps(metadata))
        source = self.source(version="2.0.0")
        entry = {"name": "hello", "url": marketplace.GITCODE_REPOSITORY, "subpath": "plugins/hello",
                 "version": "2.0.0", "ref": "b" * 40}

        def acquire(url, destination, *, subpath, ref, temporary_root):
            self.assertEqual(url, marketplace.GITCODE_REPOSITORY)
            self.assertEqual(subpath, "plugins/hello")
            self.assertEqual(ref, "b" * 40)
            shutil.copytree(source, destination)
            return {"resolved_commit": ref}

        with mock.patch.dict(os.environ, {"PYMSS_PLUGINS_REGISTRY": marketplace.GITHUB_REGISTRY, "PYMSS_PLUGINS_SOURCE": "auto"}), \
             mock.patch.object(installer, "list_available", return_value=[entry]) as catalog, \
             mock.patch.object(installer, "_clone", side_effect=acquire) as clone:
            code, events = self.manage("check_updates", name="hello")
            self.assertEqual(code, 0, events)
            checked = json.loads(json.dumps(events[-1]["payload"]["updates"]["hello"]))
            catalog.return_value = [{**entry, "url": marketplace.GITHUB_REPOSITORY, "version": "3.0.0", "ref": "c" * 40}]
            code, events = self.manage("update", name="hello", expectedUpdate=checked)
        self.assertEqual(code, 0, events)
        catalog.assert_called_once()
        clone.assert_called_once()
        row = next(item for item in events[-1]["payload"]["installed"] if item["name"] == "hello" and item["managed"])
        self.assertEqual(row["version"], "2.0.0")
        self.assertEqual(row["resolvedCommit"], "b" * 40)

    def deferred_source(self, *, matches=True):
        source = self.source()
        project = source / "pyproject.toml"
        operation = "hello.hello" if matches else "hello.other"
        project.write_text(project.read_text().replace("dependencies=[]", 'dependencies=["missing-tool-backend>=1"]') + f'''
[tool.pymss.studio]
sdk_version = 1
deferred_dependency_operations = ["{operation}"]
[[tool.pymss.studio.views]]
id = "tool"
entry = "ui/index.html"
placements = ["tool"]
operation = "hello.hello"
''')
        (source / "ui").mkdir(exist_ok=True)
        (source / "ui/index.html").write_text("<html></html>")
        return source

    def test_code_update_preserves_existing_dependencies_without_running_pip(self):
        self.install(self.source())
        deps = self.root / "installed/hello/.studio-deps"
        deps.mkdir()
        (deps / "dependency.py").write_text("value = 42")
        original_inode = deps.stat().st_ino
        self.source(version="2.0.0")
        copytree = shutil.copytree

        def copy_source(source, *args, **kwargs):
            if Path(source).name == ".studio-deps":
                raise AssertionError("code-only updates must not copy the dependency tree")
            return copytree(source, *args, **kwargs)

        with mock.patch.object(management, "_install_dependencies", side_effect=AssertionError("unexpected dependency install")), \
                mock.patch.object(management.shutil, "copytree", side_effect=copy_source):
            code, events = self.manage("update", name="hello")
        self.assertEqual(code, 0, events)
        self.assertEqual(events[-1]["payload"]["installed"][0]["version"], "2.0.0")
        self.assertEqual((deps / "dependency.py").read_text(), "value = 42")
        self.assertEqual(deps.stat().st_ino, original_inode)
        self.assertIn("preserving", [event["payload"].get("phase") for event in events if event["type"] == "plugin_stage"])

    def test_dependency_installation_requires_explicit_update_opt_in(self):
        self.install(self.source())
        self.source(version="2.0.0")
        with mock.patch.object(management, "_install_dependencies") as install:
            code, events = self.manage("update", name="hello", installDependencies=True)
        self.assertEqual(code, 0, events)
        self.assertEqual(install.call_count, 1)

    def test_candidate_verification_can_import_reused_dependency_packages(self):
        self.install(self.source())
        deps = self.root / "installed/hello/.studio-deps"
        package = deps / "kept_dependency"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("value = 42")
        metadata = deps / "kept_dependency-1.0.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text("Name: kept-dependency\nVersion: 1.0\n")
        source = self.source(version="2.0.0")
        project = source / "pyproject.toml"
        project.write_text(project.read_text().replace("dependencies=[]", 'dependencies=["kept-dependency==1.0"]'))
        code = source / "__init__.py"
        code.write_text("from kept_dependency import value\n" + code.read_text())
        with mock.patch.object(management, "_install_dependencies", side_effect=AssertionError("unexpected pip")):
            result, events = self.manage("update", name="hello")
        self.assertEqual(result, 0, events)
        self.assertTrue(events[-1]["payload"]["installed"][0]["loaded"])
        self.assertEqual(plugins._manifest(self.root / "installed/hello")["version"], "2.0.0")

    def test_optional_tool_backend_is_not_downloaded_by_default_installation(self):
        with mock.patch.object(management, "_run_process", side_effect=AssertionError("unexpected pip")):
            code, events = self.manage("install", target=str(self.deferred_source()))
        self.assertEqual(code, 0, events)
        row = events[-1]["payload"]["installed"][0]
        self.assertTrue(row["loaded"])
        self.assertIn("missing-tool-backend", row["dependencyError"])

    def test_deferral_does_not_apply_to_a_different_operation(self):
        with mock.patch.object(management, "_run_process", side_effect=RuntimeError("dependency installation required")) as pip:
            code, events = self.manage("install", target=str(self.deferred_source(matches=False)))
        self.assertEqual(code, 1)
        self.assertTrue(pip.called)
        self.assertIn("dependency installation required", events[-1]["payload"]["message"])

    def test_code_update_with_new_missing_requirement_keeps_the_old_version(self):
        self.install(self.source())
        source = self.source(version="2.0.0")
        project = source / "pyproject.toml"
        project.write_text(project.read_text().replace("dependencies=[]", 'dependencies=["missing-tool-backend>=1"]'))
        with mock.patch.object(management, "_run_process", side_effect=AssertionError("unexpected pip")):
            code, events = self.manage("update", name="hello")
        self.assertEqual(code, 1)
        self.assertIn("Missing plugin dependency", events[-1]["payload"]["message"])
        self.assertEqual(plugins._manifest(self.root / "installed/hello")["version"], "1.0.0")

    def test_code_updates_still_enforce_the_python_version_requirement(self):
        self.install(self.source())
        source = self.source(version="2.0.0")
        project = source / "pyproject.toml"
        project.write_text(project.read_text().replace('requires-python=">=3.10"', 'requires-python=">=999"'))
        code, events = self.manage("update", name="hello")
        self.assertEqual(code, 1)
        self.assertIn("Plugin requires Python", events[-1]["payload"]["message"])
        self.assertEqual(plugins._manifest(self.root / "installed/hello")["version"], "1.0.0")

    def test_non_boolean_dependency_opt_in_is_rejected_before_installation(self):
        for value in ("false", 1, None):
            with self.subTest(value=value), mock.patch.object(management, "_install") as install:
                code, _events = self.manage("update", name="hello", installDependencies=value)
            self.assertEqual(code, 1)
            install.assert_not_called()

    def test_explicit_backend_installation_uses_the_runtime_package_version_policy(self):
        source = self.source()
        project = source / "pyproject.toml"
        project.write_text(project.read_text().replace("dependencies=[]", 'dependencies=["funasr>=1.2"]'))

        def resolve(_command, _task_id):
            self.assertIn("funasr==1.3.26", (source.parent / "constraints.txt").read_text())
            (source.parent / "pip-report.json").write_text(json.dumps({"install": []}))

        with mock.patch.object(management, "_run_process", side_effect=resolve):
            plugins._install_dependencies(source, self.root, None, {"action": "repair", "mirror": "pypi"})


class DependencyHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def package(self, path, value):
        path.mkdir(parents=True)
        (path / "__init__.py").write_text(value)
        (path / "pyproject.toml").write_text(f'[tool.pymss.plugin]\nid="{path.name}"\napi_version=2\n')
        return path

    def test_interrupted_handoffs_recover_dependencies_in_both_stores(self):
        for store, backup_store in (("installed", "backups"), ("disabled", "disabled-backups")):
            for transferred in (False, True):
                with self.subTest(store=store, transferred=transferred):
                    name = f"hello-{str(transferred).lower()}"
                    backup = self.package(self.root / backup_store / name, "old")
                    target = self.package(self.root / store / name, "new")
                    (target / ".studio-deps-pending").write_text("1")
                    deps = (target if transferred else backup) / ".studio-deps"
                    deps.mkdir()
                    (deps / "dependency.py").write_text("value = 42")
                    original_inode = deps.stat().st_ino
                    plugins._recover(self.root)
                    self.assertEqual((target / "__init__.py").read_text(), "new")
                    self.assertEqual((target / ".studio-deps/dependency.py").read_text(), "value = 42")
                    self.assertEqual((target / ".studio-deps").stat().st_ino, original_inode)
                    self.assertFalse(backup.exists())
                    self.assertFalse((target / ".studio-deps-pending").exists())

    def test_failed_dependency_move_rolls_back_the_complete_old_plugin(self):
        target = self.package(self.root / "installed/hello", "old")
        deps = target / ".studio-deps"
        deps.mkdir()
        (deps / "dependency.py").write_text("value = 42")
        candidate = self.package(self.root / "transactions/install-test/hello", "new")
        rename = Path.rename

        def fail_handoff(path, destination):
            if path.name == ".studio-deps":
                raise PermissionError("dependency directory in use")
            return rename(path, destination)

        with mock.patch.object(Path, "rename", fail_handoff), self.assertRaises(PermissionError):
            plugins._publish(candidate, self.root, "hello", preserve_dependencies=True)
        self.assertEqual((target / "__init__.py").read_text(), "old")
        self.assertEqual((deps / "dependency.py").read_text(), "value = 42")
        self.assertEqual((candidate / "__init__.py").read_text(), "new")
        self.assertFalse((self.root / "backups/hello").exists())

    def test_failed_marker_cleanup_returns_transferred_dependencies_to_old_plugin(self):
        target = self.package(self.root / "installed/hello", "old")
        deps = target / ".studio-deps"
        deps.mkdir()
        (deps / "dependency.py").write_text("value = 42")
        original_inode = deps.stat().st_ino
        candidate = self.package(self.root / "transactions/install-test/hello", "new")
        unlink = Path.unlink

        def fail_cleanup(path, *args, **kwargs):
            if path == target / ".studio-deps-pending":
                raise PermissionError("handoff marker in use")
            return unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", fail_cleanup), self.assertRaises(PermissionError):
            plugins._publish(candidate, self.root, "hello", preserve_dependencies=True)
        self.assertEqual((target / "__init__.py").read_text(), "old")
        self.assertEqual((deps / "dependency.py").read_text(), "value = 42")
        self.assertEqual(deps.stat().st_ino, original_inode)
        self.assertFalse((self.root / "backups/hello").exists())
