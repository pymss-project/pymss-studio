from __future__ import annotations

import json
import io
import os
import subprocess
import sys
import unittest
from unittest import mock
from contextlib import redirect_stdout

from . import test_worker_plugins as marketplace
import worker_plugins
import worker_operations
from pymss_plugin_manager import PluginContext, PluginPolicy, PluginService
from pymss_plugin_manager.leases import binding_lease, release_runtime
from pymss_plugin_manager.locks import RuntimeLockBusy


class SharedPluginManagementTests(unittest.TestCase):
    setUp = marketplace.PluginMarketplaceTests.setUp
    reset_runtime = staticmethod(marketplace.PluginMarketplaceTests.reset_runtime)
    manage = marketplace.PluginMarketplaceTests.manage

    def provider(self):
        source = self.work / "provider-source"
        source.mkdir()
        (source / "pyproject.toml").write_text(
            '[project]\nname="shared-provider"\nversion="1.0.0"\ndependencies=[]\n'
            '[tool.pymss.plugin]\nid="example.provider"\napi_version=2\n', encoding="utf-8")
        (source / "__init__.py").write_text(
            'def run(args):\n'
            '    from pymss.plugins import require_capability\n'
            '    print(require_capability("shared_provider")())\n'
            '    return 0\n'
            'def setup(plugin):\n'
            '    plugin.register_capability("shared_provider", lambda: 42)\n'
            '    plugin.register_cli("provider-probe", run, help="Inspect the provider")\n', encoding="utf-8")
        return source

    def cli(self, *arguments):
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        return subprocess.run([sys.executable, "-X", "utf8", "-m", "pymss", "--plugin-store", str(self.root), *arguments],
                              capture_output=True, text=True, encoding="utf-8", timeout=45, env=env)

    def test_cli_install_studio_disable_and_cli_uninstall_share_one_store(self):
        installed = self.cli("install", str(self.provider()))
        self.assertEqual(installed.returncode, 0, installed.stderr)
        row = next(row for row in worker_plugins._inventory(self.root)["installed"] if row["managed"])
        self.assertEqual(row["operations"], [])
        self.assertEqual(row["contributions"]["views"], [])
        self.assertTrue(row["loaded"])
        executed = self.cli("provider-probe")
        self.assertEqual(executed.returncode, 0, executed.stderr)
        self.assertEqual(executed.stdout.strip(), "42")
        self.assertEqual(self.manage("disable", name="example.provider")[0], 0)
        disabled = self.cli("provider-probe")
        self.assertNotEqual(disabled.returncode, 0)
        listing = self.cli("plugins", "list", "--json")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertEqual(json.loads(listing.stdout)["installed"][0]["state"], "disabled")
        removed = self.cli("plugins", "uninstall", "example.provider", "--json")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(any(row["managed"] for row in worker_plugins._inventory(self.root)["installed"]))

    def test_store_queries_do_not_execute_installed_setup(self):
        source = self.provider()
        self.assertEqual(self.cli("install", str(source)).returncode, 0)
        installed = self.root / "installed/example.provider/__init__.py"
        installed.write_text('raise AssertionError("Listing executed plugin code")', encoding="utf-8")
        listing = self.cli("plugins", "list", "--json")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertEqual(json.loads(listing.stdout)["installed"][0]["name"], "example.provider")

    def test_an_old_core_is_read_only_without_a_second_installer(self):
        source = self.provider()
        import shutil
        shutil.copytree(source, self.root / "installed/example.provider")
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        with mock.patch.dict(sys.modules, {"pymss_plugin_manager": None}):
            snapshot = worker_plugins._inventory(self.root)
            self.assertFalse(snapshot["runtime"]["managementSupported"])
            self.assertEqual(snapshot["installed"][0]["name"], "example.provider")
            code, events = self.manage("disable", name="example.provider")
            self.assertEqual(code, 1)
            self.assertEqual(events[-1]["payload"]["code"], "PLUGIN_RUNTIME_INCOMPATIBLE")
        after = {str(path.relative_to(self.root)): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_old_core_inventory_isolates_malformed_manifest_fields_without_writing(self):
        import shutil

        source = self.provider()
        shutil.copytree(source, self.root / "installed/example.provider")
        project = '[project]\nname="bad"\nversion="1.0"\n'
        plugin = '[tool.pymss.plugin]\nid="bad.plugin"\napi_version=2\n'
        cases = {
            "syntax": b"[invalid",
            "encoding": b"\xff",
            "tool": ("tool=42\n" + project).encode(),
            "pymss": (project + '[tool]\npymss="invalid"\n').encode(),
            "plugin": (project + '[tool.pymss]\nplugin=42\n').encode(),
            "project": ("project=42\n" + plugin).encode(),
            "version": (project.replace('version="1.0"', "version=42") + plugin).encode(),
            "operations": (project + plugin + 'operations="invalid"\n').encode(),
            "operation_entry": (project + plugin + 'operations=[42]\n').encode(),
            "operation_id": (project + plugin + 'operations=[{id=42}]\n').encode(),
        }
        for store in ("installed", "disabled"):
            broken = self.root / store / "bad.plugin"
            broken.mkdir(parents=True)
            for field, content in cases.items():
                with self.subTest(store=store, field=field):
                    (broken / "pyproject.toml").write_bytes(content)
                    before = {str(path.relative_to(self.root)): path.read_bytes()
                              for path in self.root.rglob("*") if path.is_file()}
                    with mock.patch.dict(sys.modules, {"pymss_plugin_manager": None}):
                        snapshot = worker_plugins._inventory(self.root)
                    self.assertFalse(snapshot["runtime"]["managementSupported"])
                    self.assertEqual([row["name"] for row in snapshot["installed"]], ["example.provider"])
                    after = {str(path.relative_to(self.root)): path.read_bytes()
                             for path in self.root.rglob("*") if path.is_file()}
                    self.assertEqual(after, before)

    def test_old_core_inventory_accepts_minimal_and_disabled_plugin_metadata(self):
        provider = self.root / "installed/minimal.provider"
        provider.mkdir(parents=True)
        (provider / "pyproject.toml").write_text(
            '[tool.pymss.plugin]\nid="minimal.provider"\napi_version=2\n', encoding="utf-8")
        disabled = self.root / "disabled/disabled.provider"
        disabled.mkdir(parents=True)
        (disabled / "pyproject.toml").write_text(
            '[project]\nversion="1.0"\n[tool.pymss.plugin]\nid="disabled.provider"\napi_version=2\n'
            '[[tool.pymss.plugin.operations]]\nid="disabled.provider.run"\n', encoding="utf-8")
        for package in (provider, disabled):
            (package / "__init__.py").write_text('raise AssertionError("Inventory executed plugin code")\n', encoding="utf-8")
        with mock.patch.dict(sys.modules, {"pymss_plugin_manager": None}):
            snapshot = worker_plugins._inventory(self.root)
        rows = {row["name"]: row for row in snapshot["installed"]}
        self.assertEqual(set(rows), {"minimal.provider", "disabled.provider"})
        self.assertIsNone(rows["minimal.provider"]["version"])
        self.assertEqual(rows["minimal.provider"]["operations"], [])
        self.assertTrue(rows["minimal.provider"]["enabled"])
        self.assertEqual(rows["disabled.provider"]["operations"], ["disabled.provider.run"])
        self.assertFalse(rows["disabled.provider"]["enabled"])

    def test_managed_cli_preserves_an_explicit_repository_subpath(self):
        source = self.provider()
        installed = self.cli("install", str(self.work), "--subpath", "provider-source")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        row = worker_plugins._inventory(self.root)["installed"][0]
        self.assertEqual(row["name"], "example.provider")
        project = source / "pyproject.toml"
        project.write_text(project.read_text(encoding="utf-8").replace('version="1.0.0"', 'version="1.1.0"'), encoding="utf-8")
        updated = self.cli("plugins", "update", "example.provider", "--json")
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertEqual(json.loads(updated.stdout)["installed"][0]["version"], "1.1.0")

    def test_optional_view_errors_do_not_disable_the_operation(self):
        source = marketplace.PluginMarketplaceTests.source(self)
        project = source / "pyproject.toml"
        original = project.read_text(encoding="utf-8")
        (source / "ui").mkdir()
        (source / "ui/index.html").write_text("<html></html>", encoding="utf-8")
        view = '[[tool.pymss.studio.views]]\nid="tool"\nentry="ui/index.html"\npermissions=[]\n'
        cases = (
            ("999", "sdk_version=999\n" + view),
            ("views", "sdk_version=1\nviews=42\n"),
            ("placements", "sdk_version=1\n" + view + "placements=[[]]\n"),
        )
        for index, (error, metadata) in enumerate(cases):
            with self.subTest(view_error=error):
                project.write_text(original + "\n[tool.pymss.studio]\n" + metadata, encoding="utf-8")
                code, events = (self.manage("update", name="hello") if index else
                                self.manage("install", target=str(source)))
                self.assertEqual(code, 0, events)
                row = worker_plugins._inventory(self.root)["installed"][0]
                self.assertTrue(row["loaded"])
                self.assertIsNone(row["error"])
                self.assertTrue(row["compatibility"]["compatible"])
                self.assertIn(error, row["viewError"])
                self.assertEqual(row["contributions"].get("views", []), [])
                unprepared = worker_plugins._inventory(self.root.parent / "other-runtime")["installed"][0]
                self.assertFalse(unprepared["prepared"])
                self.assertTrue(unprepared["compatibility"]["compatible"])
                self.assertIsNone(unprepared["error"])
                self.assertIn(error, unprepared["viewError"])
                self.assertEqual(unprepared["contributions"]["views"], [])
                output = io.StringIO()
                with redirect_stdout(output):
                    result = worker_operations.cmd_run_operation({
                        "operation": "hello.hello", "inputs": {}, "params": {},
                        "outputDir": str(self.work / "output"), "requestId": "headless-backend",
                    })
                self.assertEqual(result, 0, output.getvalue())
                event = json.loads(output.getvalue().splitlines()[-1])
                self.assertEqual(event["payload"]["outputs"]["value"]["value"], 42)

    def test_active_lease_blocks_mutation_and_does_not_upgrade_in_process(self):
        service = PluginService(PluginContext(self.root))
        service.install(str(self.provider()))
        with binding_lease(self.root):
            with self.assertRaises(RuntimeLockBusy):
                service.disable("example.provider")
        self.assertTrue(service.list()["installed"][0]["enabled"])
        service.disable("example.provider")
        self.assertFalse(service.list()["installed"][0]["enabled"])

    def test_wrong_interpreter_identity_is_rejected_without_rewriting_it(self):
        service = PluginService(PluginContext(self.root))
        service.install(str(self.provider()))
        identity = self.root / ".pymss-binding.json"
        content = identity.read_bytes()
        with mock.patch.object(sys, "prefix", str(self.work / "another-venv")):
            with self.assertRaisesRegex(ValueError, "PLUGIN_ENV_MISMATCH"):
                service.disable("example.provider")
        self.assertEqual(identity.read_bytes(), content)

    def test_cancelled_install_keeps_the_previous_installation(self):
        source = self.provider()
        service = PluginService(PluginContext(self.root))
        service.install(str(source))
        previous = (self.root / "installed/example.provider/.pymss-install.json").read_bytes()
        cancelled = False

        def event(kind, payload, task_id):
            nonlocal cancelled
            if kind == "plugin_stage" and payload.get("phase") == "acquiring":
                cancelled = True

        service = PluginService(PluginContext(self.root), PluginPolicy(event=event, cancelled=lambda: cancelled))
        with self.assertRaises((InterruptedError, RuntimeError)):
            service.update("example.provider")
        self.assertEqual((self.root / "installed/example.provider/.pymss-install.json").read_bytes(), previous)
        release_runtime()
