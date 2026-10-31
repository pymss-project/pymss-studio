from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest import mock

from tooling.scripts.generate_operation_metadata import ROOT, main, render_metadata
from tooling.scripts.operation_metadata import build_snapshot, render_snapshot, source_digest, validate_snapshot


class OperationMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.package = Path(self.temporary.name)
        (self.package / "__init__.py").write_bytes(b"answer = 1\n")
        self.project = {"project": {"version": "1.0.0"}, "tool": {"pymss": {"plugin": {"id": "alice.tool", "api_version": 2, "operations": [{"id": "alice.tool.process"}]}}}}
        self.spec = {"id": "alice.tool.process", "schemaVersion": 1, "title": "Process", "description": "", "inputs": {}, "params": {"type": "object"}, "outputs": {}}

    def test_source_digest_has_a_stable_normalized_wire_definition(self):
        expected = hashlib.sha256(b"__init__.py\0answer = 1\n\0").hexdigest()
        self.assertEqual(source_digest(self.package), expected)
        (self.package / "__init__.py").write_bytes(b"answer = 1\r\n")
        self.assertEqual(source_digest(self.package), expected)
        for name in ("ui", "views", ".studio-deps", "__pycache__"):
            directory = self.package / name
            directory.mkdir()
            (directory / "generated.py").write_text("value = 3", encoding="utf-8")
        (self.package / "operations.json").write_text("{}", encoding="utf-8")
        (self.package / ".pymss-install.json").write_text("{}", encoding="utf-8")
        self.assertEqual(source_digest(self.package), expected)
        (self.package / "__init__.py").write_text("answer = 2\n", encoding="utf-8")
        self.assertNotEqual(source_digest(self.package), expected)

    def test_snapshot_requires_exact_declared_operation_set_and_json_values(self):
        snapshot = build_snapshot(self.package, self.project, [self.spec])
        self.assertEqual(snapshot["operations"], [self.spec])
        for descriptions in ([], [self.spec, self.spec], [{**self.spec, "id": "other.operation"}]):
            with self.subTest(descriptions=descriptions), self.assertRaisesRegex(ValueError, "manifest"):
                build_snapshot(self.package, self.project, descriptions)
        with self.assertRaises(ValueError):
            build_snapshot(self.package, self.project, [{**self.spec, "default": float("nan")}])

    def test_line_endings_are_normalized_for_extensionless_and_other_text_files(self):
        paths = [self.package / name for name in ("README", "command.sh", "defaults.JSON")]
        for path in paths:
            path.write_bytes(b"first\nsecond\n")
        binary = self.package / "opaque.bin"
        binary.write_bytes(b"\0\r\n")
        expected = source_digest(self.package)
        for path in paths:
            path.write_bytes(b"first\r\nsecond\r\n")
        self.assertEqual(source_digest(self.package), expected)
        binary.write_bytes(b"\0\n")
        self.assertNotEqual(source_digest(self.package), expected)

    def test_source_edit_invalidates_existing_snapshot(self):
        path = self.package / "operations.json"
        path.write_text(render_snapshot(build_snapshot(self.package, self.project, [self.spec])), encoding="utf-8")
        validate_snapshot(self.package, self.project)
        (self.package / "__init__.py").write_text("answer = 3\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "stale"):
            validate_snapshot(self.package, self.project)

    def test_check_reports_drift_without_overwriting_file(self):
        path = self.package / "operations.json"
        path.write_text("old", encoding="utf-8")
        with mock.patch("tooling.scripts.generate_operation_metadata.render_metadata", return_value={path: "new\n"}), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--check"]), 1)
            self.assertEqual(path.read_text(encoding="utf-8"), "old")
            self.assertEqual(main([]), 0)
            self.assertEqual(path.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(main(["--check"]), 0)

    def test_committed_plugin_snapshots_match_their_sources(self):
        for path in sorted((ROOT / "plugins").glob("*/pyproject.toml")):
            with self.subTest(plugin=path.parent.name):
                project = tomllib.loads(path.read_text(encoding="utf-8"))
                validate_snapshot(path.parent, project)
                value = json.loads((path.parent / "operations.json").read_text(encoding="utf-8"))
                self.assertEqual(value["pluginId"], project["tool"]["pymss"]["plugin"]["id"])

    def test_selected_export_does_not_import_an_unrelated_broken_plugin(self):
        selected = self.package / "selected"
        selected.mkdir()
        (selected / "pyproject.toml").write_text(
            '[project]\nname="selected"\nversion="1.0.0"\n'
            '[tool.pymss.plugin]\nid="metadata.selected"\napi_version=2\n'
            '[[tool.pymss.plugin.operations]]\nid="metadata.selected.run"\n', encoding="utf-8",
        )
        (selected / "__init__.py").write_text(
            'def setup(plugin):\n'
            '    plugin.register_operation(id="metadata.selected.run", params={"type":"object"}, '
            'inputs={}, outputs={}, run=lambda *args: {})\n', encoding="utf-8",
        )
        unrelated = self.package / "unrelated"
        unrelated.mkdir()
        (unrelated / "pyproject.toml").write_text(
            '[project]\nname="unrelated"\nversion="1.0.0"\n'
            '[tool.pymss.plugin]\nid="metadata.unrelated"\napi_version=2\n', encoding="utf-8",
        )
        marker = self.package / "unexpected-import"
        (unrelated / "__init__.py").write_text(
            f'from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError("Unexpected import")\n', encoding="utf-8",
        )
        result = render_metadata(self.package, {"selected"})
        self.assertEqual(list(result), [selected / "operations.json"])
        self.assertFalse(marker.exists())
