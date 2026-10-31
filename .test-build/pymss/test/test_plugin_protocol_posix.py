"""Native POSIX filesystem contracts used by the macOS CI job."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from pymss.operations import OperationContext, Runtime
from pymss.plugins import ManifestError, read_manifest
from pymss.plugins.graph_ops import build_graph_operation


@unittest.skipUnless(os.name == "posix", "Native POSIX symbolic links are required")
class PosixPluginProtocolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pymss-posix-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "插件 空间"
        self.root.mkdir()

    def plugin(self, name="plugin", *, entry=None, operations=(), extra=""):
        directory = self.root / name
        directory.mkdir()
        (directory / "pyproject.toml").write_text(
            '[project]\nname="pymss-plugin-posix"\nversion="1.0.0"\n'
            '[tool.pymss.plugin]\nid="author.posix"\napi_version=2\n'
            + (f'entry="{entry}"\n' if entry else "")
            + "".join(f'[[tool.pymss.plugin.operations]]\nid="{operation}"\n' for operation in operations)
            + extra, encoding="utf-8",
        )
        (directory / "__init__.py").write_text("def setup(plugin): pass\n", encoding="utf-8")
        return directory

    def test_package_entry_under_a_unicode_path_supports_relative_imports(self):
        directory = self.plugin("音频 插件", entry="pkg.handlers:setup", operations=["author.posix.run"])
        package = directory / "pkg"
        package.mkdir()
        (package / "__init__.py").touch()
        (package / "helper.py").write_text("VALUE = 42\n", encoding="utf-8")
        (package / "handlers.py").write_text(
            "from .helper import VALUE\n"
            "def setup(plugin):\n"
            "    plugin.register_operation(id='author.posix.run', inputs={}, params={'type':'object'}, "
            "outputs={'value':{'type':'number'}}, run=lambda c,i,p: {'value':VALUE})\n", encoding="utf-8",
        )
        before = list(sys.path)
        runtime = Runtime()
        report = runtime.load_plugins([self.root])
        self.assertTrue(report[0]["loaded"], report)
        self.assertEqual(runtime.run("author.posix.run", inputs={}, params={}), {"value": 42})
        self.assertEqual(sys.path, before)

    def test_a_symlink_alias_of_the_same_root_is_the_same_plugin_identity(self):
        self.plugin()
        alias = self.root.parent / "插件 alias"
        alias.symlink_to(self.root, target_is_directory=True)
        runtime = Runtime()
        for root in [self.root, alias]:
            report = runtime.load_plugins([root])
            self.assertTrue(report[0]["loaded"], report)

    def test_manifest_graph_symlinks_cannot_escape_the_plugin(self):
        directory = self.plugin(extra=(
            '[[tool.pymss.plugin.contributions.graphs]]\nid="g"\nfile="graph.json"\noperation="author.posix.run"\n'
        ))
        outside = self.root / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        (directory / "graph.json").symlink_to(outside)
        with self.assertRaisesRegex(ManifestError, "escapes the plugin directory"):
            read_manifest(directory)

    def test_graph_execution_cleans_a_symlinked_unicode_scratch_parent(self):
        import numpy as np
        from pymss.graph.core import numpy_to_audio

        scratch = self.root / "临时 音频"
        scratch.mkdir()
        alias = self.root / "scratch-alias"
        alias.symlink_to(scratch, target_is_directory=True)
        graph = self.root / "graph.json"
        graph.write_text(json.dumps({
            "nodes": [{"id": 1, "type": "pymss_load_audio", "widgets_values": ["", "audio"],
                       "inputs": [], "outputs": [{"name": "audio"}]}], "links": [],
        }), encoding="utf-8")
        run = build_graph_operation(graph_file=graph, audio_input={"node": "1"},
                                    outputs={"audio": {"node": "1"}})
        output = run(OperationContext(workdir=str(alias)),
                     {"audio": numpy_to_audio(np.full((2, 1000), 0.1, dtype=np.float32), 44100)}, {})
        self.assertAlmostEqual(float(output["audio"].audio.mean()), 0.1, places=4)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(list(scratch.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
