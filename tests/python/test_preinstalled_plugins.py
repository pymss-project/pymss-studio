from __future__ import annotations

import hashlib
import json
import shutil
import unittest
from unittest import mock

if __package__:
    from . import test_worker_plugins as marketplace_tests
else:
    import test_worker_plugins as marketplace_tests

import worker_plugins as plugins


class PreinstalledPluginTests(unittest.TestCase):
    source = marketplace_tests.PluginMarketplaceTests.source
    manage = marketplace_tests.PluginMarketplaceTests.manage
    install = marketplace_tests.PluginMarketplaceTests.install
    reset_runtime = staticmethod(marketplace_tests.PluginMarketplaceTests.reset_runtime)

    def setUp(self):
        marketplace_tests.PluginMarketplaceTests.setUp(self)
        self.bundles = self.work / "bundled"
        self.bundles.mkdir()
        self.bundle_patch = mock.patch.object(plugins, "_BUNDLED_ROOT", self.bundles, create=True)
        self.bundle_patch.start()
        self.addCleanup(self.bundle_patch.stop)

    def bundle(self, name="example.convert"):
        source = self.source(name=name)
        destination = self.bundles / name
        shutil.copytree(source, destination)
        files = {str(path.relative_to(destination)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in destination.rglob("*") if path.is_file()}
        manifest = {"schemaVersion": 1, "plugins": [{
            "name": name, "version": "1.0.0",
            "url": "https://example.com/plugins", "subpath": "plugins/convert", "files": files,
        }]}
        (self.bundles / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_optional_backend_deferral_is_limited_to_declared_operation(self):
        source = self.source()
        project = source / "pyproject.toml"
        text = project.read_text(encoding="utf-8").replace("dependencies=[]", 'dependencies=["missing-pymss-tool-backend>=1"]')
        project.write_text(text + '\n[tool.pymss.studio]\ndeferred_dependency_operations=["hello.tool"]\n', encoding="utf-8")
        plugins._validate_requirements(source, operation="hello.tool")
        for operation in (None, "hello.other"):
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, "Missing plugin dependency"):
                plugins._validate_requirements(source, operation=operation)

    def test_read_only_inventory_does_not_seed_preinstalled_files(self):
        self.bundle()
        snapshot = plugins._inventory(self.root)
        self.assertEqual(snapshot["preinstalledPending"], ["example.convert"])
        self.assertFalse((self.root / "installed/example.convert").exists())

    def test_declared_tool_metadata_works_without_preinstalled_resources(self):
        source = self.source()
        (source / "ui").mkdir()
        (source / "ui/index.html").write_text("<html>tool</html>")
        project = source / "pyproject.toml"
        project.write_text(project.read_text() + '''
[tool.pymss.studio]
sdk_version = 1
category = "analyze"
icon = "AnalyticsOutline"
[[tool.pymss.studio.views]]
id = "tool"
entry = "ui/index.html"
placements = ["tool"]
operation = "hello.hello"
''')
        with mock.patch.object(plugins, "_bundled", return_value={}):
            self.install(source)
            row = plugins._inventory(self.root, verify=False)["installed"][0]
            self.assertNotIn("toolId", row)
            self.assertEqual(row["toolCategory"], "analyze")
            self.assertEqual(row["toolIcon"], "AnalyticsOutline")
            self.assertEqual(row["contributions"]["views"][0]["pluginId"], "hello")
            self.assertEqual(row["contributions"]["views"][0]["category"], "analyze")
            self.assertEqual(plugins._inventory(self.root, verify=False)["preinstalledPending"], [])
            revision = row["contributions"]["views"][0]["revision"]
            target = self.root / "installed/hello/ui/index.html"
            target.write_text("<html>updated tool</html>")
            updated = plugins._inventory(self.root, verify=False)["installed"][0]
            self.assertNotEqual(updated["contributions"]["views"][0]["revision"], revision)

    def test_prepare_is_offline_idempotent_and_does_not_replace_user_version(self):
        self.bundle()
        with mock.patch.object(plugins, "_install_dependencies", side_effect=AssertionError("No pip for preinstalled plugins")):
            code, events = self.manage("prepare_preinstalled")
        self.assertEqual(code, 0, events)
        self.assertTrue(events[-1]["payload"]["installed"][0]["loaded"])
        target = self.root / "installed/example.convert/__init__.py"
        target.write_text(target.read_text() + "\nUSER_VERSION = True\n")
        code, events = self.manage("prepare_preinstalled")
        self.assertEqual(code, 0, events)
        self.assertIn("USER_VERSION", target.read_text())

    def test_bundle_whose_package_has_another_plugin_id_is_rejected(self):
        self.bundle()
        package = self.bundles / "example.convert"
        package.joinpath("pyproject.toml").write_text(marketplace_tests.PluginMarketplaceTests.manifest("other.convert"))
        manifest = json.loads((self.bundles / "manifest.json").read_text(encoding="utf-8"))
        manifest["plugins"][0]["files"]["pyproject.toml"] = hashlib.sha256(package.joinpath("pyproject.toml").read_bytes()).hexdigest()
        (self.bundles / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        code, events = self.manage("prepare_preinstalled")
        self.assertEqual(code, 1, events)
        self.assertIn("does not match its bundle", events[-1]["payload"]["message"])
        self.assertFalse((self.root / "installed/example.convert").exists())

    def test_disabled_plugins_do_not_load_and_can_be_enabled_after_restart(self):
        self.install(self.source())
        code, events = self.manage("disable", name="hello")
        self.assertEqual(code, 0, events)
        row = events[-1]["payload"]["installed"][0]
        self.assertFalse(row["enabled"])
        self.assertFalse(row["loaded"])
        self.reset_runtime()
        self.assertNotIn("hello.hello", {item["id"] for item in plugins.operation_runtime().list_operations()})
        code, events = self.manage("enable", name="hello")
        self.assertEqual(code, 0, events)
        self.assertTrue(events[-1]["payload"]["installed"][0]["enabled"])
        self.assertTrue(events[-1]["payload"]["installed"][0]["loaded"])

    def test_uninstalling_preinstalled_plugin_does_not_resurrect_it(self):
        self.bundle()
        self.assertEqual(self.manage("prepare_preinstalled")[0], 0)
        self.assertEqual(self.manage("uninstall", name="example.convert")[0], 0)
        self.assertEqual(plugins._inventory(self.root)["preinstalledPending"], [])
        self.assertEqual(self.manage("prepare_preinstalled")[0], 0)
        self.assertFalse((self.root / "installed/example.convert").exists())
        self.assertEqual(self.manage("install_preinstalled", name="example.convert")[0], 0)
        self.assertTrue((self.root / "installed/example.convert").is_dir())

    def test_update_of_disabled_plugin_preserves_disabled_state(self):
        self.install(self.source())
        self.assertEqual(self.manage("disable", name="hello")[0], 0)
        self.source(version="2.0.0")
        code, events = self.manage("update", name="hello")
        self.assertEqual(code, 0, events)
        row = events[-1]["payload"]["installed"][0]
        self.assertEqual(row["version"], "2.0.0")
        self.assertFalse(row["enabled"])
        self.assertFalse((self.root / "installed/hello").exists())

    def test_corrupted_bundle_is_rejected_before_publication(self):
        self.bundle()
        (self.bundles / "example.convert/__init__.py").write_text("raise RuntimeError('corrupted')")
        code, events = self.manage("prepare_preinstalled")
        self.assertEqual(code, 1, events)
        self.assertFalse((self.root / "installed/example.convert").exists())

    def test_user_installation_of_a_bundled_id_keeps_its_origin(self):
        self.bundle()
        source = self.source(name="example.convert")
        target = self.root / "installed/example.convert"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
        (target / ".pymss-install.json").write_text('{"source":"path","version":"1.0.0"}')
        row = plugins._inventory(self.root, verify=False)["installed"][0]
        self.assertEqual(row["name"], "example.convert")
        self.assertFalse(row["preinstalled"])

    def test_state_recovery_preserves_disabled_backup_location(self):
        source = self.source()
        backup = self.root / "disabled-backups/hello"
        backup.parent.mkdir(parents=True)
        shutil.copytree(source, backup)
        plugins._recover(self.root)
        self.assertTrue((self.root / "disabled/hello/__init__.py").is_file())
        self.assertFalse((self.root / "installed/hello").exists())

    def test_failed_enable_keeps_the_disabled_installation(self):
        source = self.source(broken=True)
        target = self.root / "disabled/hello"
        target.parent.mkdir(parents=True)
        shutil.copytree(source, target)
        code, events = self.manage("enable", name="hello")
        self.assertEqual(code, 1, events)
        self.assertTrue(target.is_dir())
        self.assertFalse((self.root / "installed/hello").exists())
