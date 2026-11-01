"""Operation descriptions remain readable without loading executable plugins."""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap  # noqa: F401
else:
    import _bootstrap as _worker_test_bootstrap  # noqa: F401

import worker_operation_metadata as metadata
import worker_operations
import worker_plugins
import worker_plugin_inventory


def description(default=1):
    return {
        "id": "alice.tool.process", "schemaVersion": 1, "title": "Process", "description": "Process a value",
        "inputs": {}, "params": {"type": "object", "properties": {"amount": {"type": "integer", "default": default}}},
        "outputs": {"value": {"type": "number"}},
    }


class OperationMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.root = self.work / "managed"
        self.package = self.root / "installed" / "alice.tool"
        self.package.mkdir(parents=True)
        (self.package / "pyproject.toml").write_text(
            '[project]\nname="alice-tool"\nversion="1.0.0"\n'
            '[tool.pymss.plugin]\nid="alice.tool"\napi_version=2\n'
            '[[tool.pymss.plugin.operations]]\nid="alice.tool.process"\ntitle="Process"\n', encoding="utf-8",
        )
        (self.package / "__init__.py").write_text('raise AssertionError("Unexpected plugin import")\n', encoding="utf-8")
        self.profile = "cpu"
        self.original_fingerprint = metadata._profile_fingerprint
        for patch in (
            mock.patch.object(worker_plugins, "_root", return_value=self.root),
            mock.patch.object(worker_plugins, "_recover_for_read"),
            mock.patch.dict(os.environ, {"PYMSS_PLUGINS_DIR": str(self.work / "manual")}),
            mock.patch.object(metadata, "_profile_fingerprint", side_effect=lambda packages: hashlib.sha256(
                json.dumps([self.profile, [(str(package.directory), package.digest) for package in packages]]).encode()
            ).hexdigest()),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def snapshot(self, spec=None):
        value = {
            "formatVersion": 1, "pluginApiVersion": 2, "pluginId": "alice.tool", "pluginVersion": "1.0.0",
            "sourceDigest": metadata.source_digest(self.package), "operations": [spec or description()],
        }
        (self.package / "operations.json").write_text(json.dumps(value), encoding="utf-8")
        return value

    def query(self):
        context = metadata.capture_context()
        self.assertIsNotNone(context)
        return metadata.cached_description(context, "alice.tool.process")

    def test_published_snapshot_never_loads_pymss_torch_or_plugin(self):
        self.snapshot()
        output = io.StringIO()
        with (
            mock.patch.dict("sys.modules", {"pymss": None, "torch": None}),
            mock.patch.object(worker_operations, "_runtime", side_effect=AssertionError("Runtime query")),
            redirect_stdout(output),
        ):
            code = worker_operations.cmd_describe_operation({"operation": "alice.tool.process"})
        event = json.loads(output.getvalue())
        self.assertEqual(code, 0, event)
        self.assertEqual(event["type"], "operation_spec")
        self.assertEqual(event["payload"], metadata.with_digest(description()))

    def test_default_and_parameter_bounds_are_part_of_the_digest(self):
        first = description()
        self.assertNotEqual(metadata.spec_digest(first), metadata.spec_digest(description(2)))
        other = description()
        other["params"]["properties"]["amount"]["maximum"] = 4
        self.assertNotEqual(metadata.spec_digest(first), metadata.spec_digest(other))
        self.assertEqual(metadata.spec_digest(first), metadata.spec_digest(metadata.with_digest(first)))

    def test_changed_backend_source_invalidates_snapshot(self):
        self.snapshot()
        (self.package / "__init__.py").write_text("value = 2\n", encoding="utf-8")
        self.assertIsNone(self.query())

    def test_ui_changes_do_not_invalidate_backend_snapshot(self):
        self.snapshot()
        (self.package / "ui").mkdir()
        (self.package / "ui" / "index.html").write_text("<main>Tools</main>", encoding="utf-8")
        self.assertIsNotNone(self.query())

    def test_source_digest_normalizes_line_endings(self):
        path = self.package / "__init__.py"
        path.write_bytes(b"first = 1\r\nsecond = 2\r\n")
        expected = metadata.source_digest(self.package)
        path.write_bytes(b"first = 1\nsecond = 2\n")
        self.assertEqual(metadata.source_digest(self.package), expected)

    def test_corrupt_duplicate_or_non_finite_json_is_a_cache_miss(self):
        for content in ('{', '{"formatVersion":1,"formatVersion":1}', '{"value":NaN}'):
            with self.subTest(content=content):
                (self.package / "operations.json").write_text(content, encoding="utf-8")
                self.assertIsNone(self.query())

    def test_incorrect_identity_version_or_operation_set_is_not_used(self):
        for field, replacement in (("pluginId", "other.plugin"), ("pluginVersion", "2.0.0"), ("formatVersion", True), ("operations", [])):
            with self.subTest(field=field):
                value = self.snapshot()
                value[field] = replacement
                (self.package / "operations.json").write_text(json.dumps(value), encoding="utf-8")
                self.assertIsNone(self.query())

    def test_duplicate_operation_owners_fall_back_to_the_runtime(self):
        self.snapshot()
        manual = self.work / "manual" / "duplicate"
        manual.mkdir(parents=True)
        for name in ("pyproject.toml", "__init__.py"):
            (manual / name).write_bytes((self.package / name).read_bytes())
        self.assertIsNone(self.query())

    def test_unrelated_invalid_plugin_does_not_disable_static_descriptions(self):
        self.snapshot()
        unrelated = self.root / "installed" / "broken"
        unrelated.mkdir()
        (unrelated / "pyproject.toml").write_text("invalid = [", encoding="utf-8")
        self.assertIsNotNone(self.query())

    def test_disabled_packages_are_not_served_from_snapshot(self):
        self.snapshot()
        disabled = self.root / "disabled"
        disabled.mkdir()
        self.package.rename(disabled / self.package.name)
        self.assertIsNone(self.query())

    def test_global_disable_and_unprepared_versions_are_not_served_from_snapshot(self):
        self.snapshot()
        for revision, state, version in [(1, "disabled", "1.0.0"), (2, "enabled", "2.0.0")]:
            with self.subTest(state=state, version=version):
                worker_plugin_inventory.commit(self.root, "alice.tool", state, revision, definition={}, version=version)
                self.assertTrue(self.package.is_dir())
                self.assertIsNone(self.query())

    def test_runtime_fallback_caches_the_requested_operation_for_later_queries(self):
        runtime = SimpleNamespace(describe_operation=lambda _: description(), list_operations=lambda: [description()])
        output = io.StringIO()
        with mock.patch.object(worker_operations, "_runtime", return_value=runtime), redirect_stdout(output):
            self.assertEqual(worker_operations.cmd_describe_operation({"operation": "alice.tool.process"}), 0)
        with mock.patch.object(worker_operations, "_runtime", side_effect=AssertionError("Unexpected runtime")), redirect_stdout(io.StringIO()):
            self.assertEqual(worker_operations.cmd_describe_operation({"operation": "alice.tool.process"}), 0)
        self.assertEqual(self.query(), metadata.with_digest(description()))

    def test_live_description_is_used_even_if_list_operations_would_differ(self):
        runtime = SimpleNamespace(
            describe_operation=lambda _: description(2),
            list_operations=mock.Mock(side_effect=AssertionError("Listing is not a description")),
        )
        with mock.patch.object(worker_operations, "_runtime", return_value=runtime), redirect_stdout(io.StringIO()):
            self.assertEqual(worker_operations.cmd_describe_operation({"operation": "alice.tool.process"}), 0)
        self.assertEqual(self.query(), metadata.with_digest(description(2)))
        runtime.list_operations.assert_not_called()

    def test_runtime_specific_cache_overrides_advisory_published_snapshot(self):
        self.snapshot()
        runtime = SimpleNamespace(list_operations=lambda: [description(2)])
        metadata.remember_description(metadata.capture_context(), runtime.list_operations()[0])
        self.assertEqual(self.query(), metadata.with_digest(description(2)))

    def test_profile_change_invalidates_runtime_cache(self):
        runtime = SimpleNamespace(list_operations=lambda: [description()])
        metadata.remember_description(metadata.capture_context(), runtime.list_operations()[0])
        self.profile = "mlx"
        self.assertIsNone(self.query())

    def test_profile_fingerprint_tracks_installed_distribution_changes_without_importing_them(self):
        site = self.work / "site-packages"
        distribution = site / "numpy-1.0.dist-info"
        distribution.mkdir(parents=True)
        info = distribution / "METADATA"
        info.write_text("Name: numpy\nVersion: 1.0\n", encoding="utf-8")
        runtime = SimpleNamespace(backend="cpu", identity="cpu-py312", packages_dir=self.work / "components")
        with mock.patch("worker_optional_packages._runtime_context", return_value=runtime), mock.patch.object(sys, "path", [str(site)]):
            first = self.original_fingerprint(())
            info.write_text("Name: numpy\nVersion: 2.0.1\n", encoding="utf-8")
            second = self.original_fingerprint(())
        self.assertNotEqual(first, second)

    def test_import_search_path_aliases_do_not_invalidate_the_same_environment(self):
        site = self.work / "site-packages"
        distribution = site / "sample-1.0.dist-info"
        distribution.mkdir(parents=True)
        (distribution / "METADATA").write_text("Name: sample\nVersion: 1.0\n", encoding="utf-8")
        empty = self.work / "plugin-module"
        empty.mkdir()
        runtime = SimpleNamespace(backend="cpu", identity="cpu-py312", packages_dir=self.work / "components")
        with mock.patch("worker_optional_packages._runtime_context", return_value=runtime), mock.patch.object(sys, "path", [str(site)]):
            first = self.original_fingerprint(())
            sys.path.extend([str(site) + os.sep + ".", str(empty)])
            self.assertEqual(self.original_fingerprint(()), first)

    def test_explicit_refresh_uses_live_description_even_if_cache_cannot_be_written(self):
        self.snapshot()
        runtime = SimpleNamespace(describe_operation=lambda _: description(2), list_operations=lambda: [description(2)])
        output = io.StringIO()
        with (
            mock.patch.object(worker_operations, "_runtime", return_value=runtime),
            mock.patch.object(metadata, "remember_description"),
            redirect_stdout(output),
        ):
            self.assertEqual(worker_operations.cmd_describe_operation({"operation": "alice.tool.process", "refresh": True}), 0)
        self.assertEqual(json.loads(output.getvalue())["payload"], metadata.with_digest(description(2)))

    def test_source_change_during_fallback_is_not_cached(self):
        before = metadata.capture_context()
        (self.package / "__init__.py").write_text("value = 3\n", encoding="utf-8")
        metadata.remember_description(before, description())
        self.assertFalse((self.root / "operation-metadata").exists())

    def test_expected_digest_change_does_not_invoke_handler_or_create_output(self):
        called = mock.Mock()
        runtime = SimpleNamespace(describe_operation=lambda _: description(2), list_operations=lambda: [description(2)], run=called)
        output = io.StringIO()
        destination = self.work / "outputs"
        with mock.patch.object(worker_operations, "_runtime", return_value=runtime), redirect_stdout(output):
            code = worker_operations.cmd_run_operation({
                "operation": "alice.tool.process", "expectedSpecDigest": metadata.spec_digest(description()),
                "outputDir": str(destination), "requestId": "definition-change", "inputs": {}, "params": {},
            })
        event = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(event["payload"]["code"], "OPERATION_SPEC_CHANGED")
        self.assertTrue(event["payload"]["recoverable"])
        self.assertEqual(event["requestId"], "definition-change")
        called.assert_not_called()
        self.assertFalse(destination.exists())
        self.assertIsNone(self.query())

    def test_invalid_expected_digest_is_rejected_before_runtime_start(self):
        with mock.patch.object(worker_operations, "_runtime", side_effect=AssertionError("Unexpected runtime")), redirect_stdout(io.StringIO()):
            self.assertEqual(worker_operations.cmd_run_operation({"operation": "alice.tool.process", "expectedSpecDigest": "invalid"}), 1)


if __name__ == "__main__":
    unittest.main()
