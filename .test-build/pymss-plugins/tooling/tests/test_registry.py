from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tooling.scripts.generate_registry import ROOT, render_registry


class PluginRegistryTests(unittest.TestCase):
    def test_catalog_is_generated_from_package_metadata(self):
        self.assertEqual(render_registry(), json.loads((ROOT / "registry.json").read_text(encoding="utf-8")))
        self.assertNotIn("audio_gain", render_registry())

    def test_release_catalog_requires_an_immutable_source(self):
        with self.assertRaisesRegex(ValueError, "immutable commit"):
            render_registry(release=True)
        commit = "a" * 40
        catalog = render_registry(ref=commit, release=True)
        self.assertTrue(all(row["ref"] == commit for name, row in catalog.items() if not name.startswith("_")))

    def test_each_plugin_owns_its_backend_and_ui_sources(self):
        self.assertFalse((ROOT / "tool_runtime").exists())
        for project in (ROOT / "plugins").glob("*/pyproject.toml"):
            with self.subTest(plugin=project.parent.name):
                self.assertFalse((project.parent / "_tool").exists())
                self.assertTrue((project.parent / "ui/src").is_dir())
                self.assertTrue((project.parent / "ui/dist/index.html").is_file())

    def test_invalid_metadata_does_not_silently_disappear_from_catalog(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tooling").mkdir()
            (root / "tooling/catalog.toml").write_text((ROOT / "tooling/catalog.toml").read_text())
            plugin = root / "plugins/example"
            plugin.mkdir(parents=True)
            (plugin / "pyproject.toml").write_text('''
[project]
version = "1.0.0"
[tool.pymss.plugin]
id = "example.plugin"
[tool.pymss.studio]
category = "invalid"
''')
            with self.assertRaisesRegex(ValueError, "category"):
                render_registry(root)
