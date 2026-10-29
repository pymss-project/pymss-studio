"""Studio-side plugin protocol tests: contributions surfaced to the UI host."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap

import worker_plugins as plugins


class ContributionTests(unittest.TestCase):
    def setUp(self):
        work = Path(__file__).resolve().parents[2] / ".tmp" / "plugin-protocol-tests"
        work.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.environment = mock.patch.dict(os.environ, {
            "PYMSS_STUDIO_RUNTIME_ENVS_DIR": str(self.work / "runtimes"),
            "PYMSS_STUDIO_ACTIVE_RUNTIME_FILE": str(self.work / "active.json"),
            "PYMSS_PLUGINS_DIR": str(self.work / "manual"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        (self.work / "active.json").write_text('{"backend":"cpu"}')
        self.root = plugins._root()

    def managed(self, name: str, *, views: str = "", graphs: str = "") -> Path:
        source = self.work / "sources" / name
        source.mkdir(parents=True)
        (source / "ui").mkdir()
        (source / "ui/index.html").write_text("<html>plugin view</html>")
        (source / "graphs").mkdir()
        (source / "graphs/chain.json").write_text("{}")
        toml = f"""[project]
name = "pymss-plugin-{name}"
version = "1.0.0"

[tool.pymss.plugin]
id = "author.{name}"
api_version = 2

[[tool.pymss.plugin.operations]]
id = "author.{name}.process"

[[tool.pymss.plugin.contributions.graphs]]
id = "chain"
file = "graphs/chain.json"
operation = "author.{name}.process"

[tool.pymss.studio]
sdk_version = 1
[[tool.pymss.studio.views]]
id = "editor"
title = "Editor"
entry = "ui/index.html"
placements = ["tool", "workspace"]
operation = "author.{name}.process"
permissions = ["operations", "files.pickAudio"]
"""
        (source / "pyproject.toml").write_text(toml)
        (source / "__init__.py").write_text(
            f"def setup(plugin):\n"
            f"    plugin.register_operation(id='author.{name}.process', title='P', inputs={{}},\n"
            f"        params={{'type':'object','properties':{{}}}}, outputs={{}}, run=lambda c,i,p: {{}})\n"
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = plugins.cmd_manage_plugin({"action": "install", "target": str(source), "taskId": "plugin_protocol_test", "mirror": "pypi"})
        events = [json.loads(line) for line in output.getvalue().splitlines() if line]
        assert code == 0, events
        return source

    def test_studio_views_are_surfaced_from_the_studio_table(self):
        self.managed("chain")
        row = next(item for item in plugins._inventory(self.root)["installed"] if item["name"] == "author.chain")
        contributions = row["contributions"]
        self.assertEqual(list(contributions), ["views"])
        self.assertEqual(contributions["views"][0]["pluginId"], "author.chain")
        entry_path = contributions["views"][0]["entryPath"].replace("\\", "/")
        self.assertTrue(entry_path.endswith("ui/index.html"))
        self.assertEqual(contributions["views"][0]["placements"], ["tool", "workspace"])
        self.assertEqual(contributions["views"][0]["permissions"], ["operations", "files.pickAudio"])

    def test_view_entry_path_resolves_inside_the_plugin_directory(self):
        self.managed("chain")
        row = next(item for item in plugins._inventory(self.root)["installed"] if item["name"] == "author.chain")
        entry = Path(row["contributions"]["views"][0]["entryPath"])
        plugin_dir = self.root / "installed" / "author.chain"
        self.assertTrue(entry.is_file())
        self.assertTrue(plugin_dir in entry.parents)

    def test_manifestless_directory_plugin_has_no_contributions(self):
        manual = self.work / "manual/plain"
        manual.mkdir(parents=True)
        manual.joinpath("__init__.py").write_text("")
        rows = plugins._inventory(self.root)["installed"]
        manual_row = next(item for item in rows if item["name"] == "plain")
        self.assertFalse(manual_row["managed"])
        self.assertNotIn("contributions", manual_row)


if __name__ == "__main__":
    unittest.main()
