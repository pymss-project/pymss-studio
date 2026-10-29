from __future__ import annotations

import json
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


class PluginUpdateTests(unittest.TestCase):
    setUp = marketplace.PluginMarketplaceTests.setUp
    source = marketplace.PluginMarketplaceTests.source
    manage = marketplace.PluginMarketplaceTests.manage
    install = marketplace.PluginMarketplaceTests.install
    reset_runtime = staticmethod(marketplace.PluginMarketplaceTests.reset_runtime)

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

        with mock.patch.object(plugins, "_install_dependencies", side_effect=AssertionError("unexpected dependency install")), \
                mock.patch.object(plugins.shutil, "copytree", side_effect=copy_source):
            code, events = self.manage("update", name="hello")
        self.assertEqual(code, 0, events)
        self.assertEqual(events[-1]["payload"]["installed"][0]["version"], "2.0.0")
        self.assertEqual((deps / "dependency.py").read_text(), "value = 42")
        self.assertEqual(deps.stat().st_ino, original_inode)
        self.assertIn("preserving", [event["payload"].get("phase") for event in events if event["type"] == "plugin_stage"])

    def test_dependency_installation_requires_explicit_update_opt_in(self):
        self.install(self.source())
        self.source(version="2.0.0")
        with mock.patch.object(plugins, "_install_dependencies") as install:
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
        with mock.patch.object(plugins, "_install_dependencies", side_effect=AssertionError("unexpected pip")):
            result, events = self.manage("update", name="hello")
        self.assertEqual(result, 0, events)
        self.assertTrue(events[-1]["payload"]["installed"][0]["loaded"])
        self.assertEqual(plugins._manifest(self.root / "installed/hello")["version"], "2.0.0")

    def test_optional_tool_backend_is_not_downloaded_by_default_installation(self):
        with mock.patch.object(plugins, "_run_process", side_effect=AssertionError("unexpected pip")):
            code, events = self.manage("install", target=str(self.deferred_source()))
        self.assertEqual(code, 0, events)
        row = events[-1]["payload"]["installed"][0]
        self.assertTrue(row["loaded"])
        self.assertIn("missing-tool-backend", row["dependencyError"])

    def test_deferral_does_not_apply_to_a_different_operation(self):
        with mock.patch.object(plugins, "_run_process", side_effect=RuntimeError("dependency installation required")) as pip:
            code, events = self.manage("install", target=str(self.deferred_source(matches=False)))
        self.assertEqual(code, 1)
        self.assertTrue(pip.called)
        self.assertIn("dependency installation required", events[-1]["payload"]["message"])

    def test_code_update_with_new_missing_requirement_keeps_the_old_version(self):
        self.install(self.source())
        source = self.source(version="2.0.0")
        project = source / "pyproject.toml"
        project.write_text(project.read_text().replace("dependencies=[]", 'dependencies=["missing-tool-backend>=1"]'))
        with mock.patch.object(plugins, "_run_process", side_effect=AssertionError("unexpected pip")):
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
            with self.subTest(value=value), mock.patch.object(plugins, "_install") as install:
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

        with mock.patch.object(plugins, "_run_process", side_effect=resolve):
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
