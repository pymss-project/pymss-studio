"""Manifest-only plugin inventory: listing never imports plugin code or keeps a cache."""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from unittest import mock

if __package__:
    from . import test_worker_plugins as marketplace
else:
    import test_worker_plugins as marketplace
import worker_plugins as plugins


class PluginInventoryTests(unittest.TestCase):
    setUp = marketplace.PluginMarketplaceTests.setUp
    reset_runtime = staticmethod(marketplace.PluginMarketplaceTests.reset_runtime)
    source = marketplace.PluginMarketplaceTests.source
    manifest = staticmethod(marketplace.PluginMarketplaceTests.manifest)

    def test_listing_reads_manifests_without_probing_or_caching(self):
        shutil.copytree(self.source(), self.root / "installed/hello")
        with mock.patch.object(plugins, "_probe", side_effect=AssertionError("unexpected verification")):
            snapshot = plugins._inventory(self.root)
        row = snapshot["installed"][0]
        self.assertEqual((row["name"], row["loaded"], row["operations"]), ("hello", True, ["hello.hello"]))
        self.assertFalse((self.root / "inventory-cache.json").exists())

    def test_listing_does_not_import_pymss(self):
        shutil.copytree(self.source(), self.root / "installed/hello")
        code = (
            "import sys, worker_plugins as p\n"
            "from pathlib import Path\n"
            "p._inventory(Path(sys.argv[1]))\n"
            "print('pymss' in sys.modules)\n"
        )
        environment = {**plugins.os.environ, "PYTHONPATH": plugins.os.pathsep.join([str(plugins.Path(plugins.__file__).parent), plugins.os.environ.get("PYTHONPATH", "")])}
        result = subprocess.run([sys.executable, "-c", code, str(self.root)], capture_output=True, text=True, env=environment, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "False")

    def test_inventory_lists_managed_disabled_and_pymss_directory_plugins(self):
        shutil.copytree(self.source(), self.root / "installed/hello")
        shutil.copytree(self.source(name="author.manual"), self.work / "manual/author.manual")
        shutil.copytree(self.source(name="dormant"), self.root / "disabled/dormant")
        rows = {row["name"]: row for row in plugins._inventory(self.root)["installed"]}
        self.assertTrue(rows["hello"]["loaded"])
        self.assertFalse(rows["dormant"]["enabled"])
        self.assertFalse(rows["dormant"]["loaded"])
        self.assertEqual(rows["dormant"]["operations"], [])
        self.assertFalse(rows["author.manual"]["managed"])

    def test_missing_dependency_is_reported_without_importing(self):
        source = self.source()
        source.joinpath("pyproject.toml").write_text(self.manifest("hello", dependencies='["studio-missing-test-dependency"]'))
        shutil.copytree(source, self.root / "installed/hello")
        row = plugins._inventory(self.root)["installed"][0]
        self.assertFalse(row["loaded"])
        self.assertIn("Missing plugin dependency", row["error"])

    def test_forced_verification_merges_probe_reports(self):
        shutil.copytree(self.source(), self.root / "installed/hello")
        report = [{"name": "hello", "managed": True, "loaded": False, "error": "import failed"}]
        with mock.patch.object(plugins, "_probe", return_value=report) as probe:
            snapshot = plugins._inventory(self.root, verify=True)
        probe.assert_called_once_with(self.root)
        self.assertFalse(snapshot["installed"][0]["loaded"])
        self.assertEqual(snapshot["installed"][0]["error"], "import failed")

    def test_marketplace_query_verifies_only_when_forced(self):
        from pymss.plugins import install as installer
        shutil.copytree(self.source(), self.root / "installed/hello")
        with mock.patch.object(installer, "list_available", return_value=[]), \
             mock.patch.object(plugins, "_probe", return_value=[]) as probe, \
             mock.patch.object(plugins, "emit"):
            plugins.cmd_plugin_marketplace({})
            probe.assert_not_called()
            plugins.cmd_plugin_marketplace({"forceVerify": True})
            probe.assert_called_once()

    def test_catalog_only_query_does_not_list_or_verify_plugins(self):
        from pymss.plugins import install as installer
        with mock.patch.object(plugins, "_inventory", side_effect=AssertionError("unexpected listing")), \
             mock.patch.object(installer, "list_available", return_value=[]), \
             mock.patch.object(plugins, "emit") as emit:
            self.assertEqual(plugins.cmd_plugin_marketplace({"catalogOnly": True}), 0)
        self.assertEqual(emit.call_args.args[0], "plugin_marketplace")
        self.assertEqual(emit.call_args.args[1]["catalog"], [])


if __name__ == "__main__":
    unittest.main()
