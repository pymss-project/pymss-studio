"""Plugin protocol v2 tests: manifests, lifecycle, dependency resolution."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
import io
from pathlib import Path
from unittest.mock import patch

from pymss.plugins import ManifestError, PluginManager, read_manifest, satisfies
from pymss.plugins.lifecycle import pymss_package_version
from pymss.plugins.graph_ops import build_graph_operation
import pymss.operations as ops


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name: str, body: str, extra: dict | None = None) -> Path:
        plugin = self.root / name
        plugin.mkdir(parents=True, exist_ok=True)
        plugin.joinpath("pyproject.toml").write_text(body, encoding="utf-8")
        plugin.joinpath("__init__.py").write_text("", encoding="utf-8")
        for rel, content in (extra or {}).items():
            target = plugin / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return plugin

    def test_plugin_without_manifest_is_rejected(self):
        plugin = self.write("plain", "")
        with self.assertRaisesRegex(ManifestError, "missing"):
            read_manifest(plugin)
        plugin.joinpath("pyproject.toml").unlink()
        with self.assertRaisesRegex(ManifestError, "missing"):
            read_manifest(plugin)

    def test_full_manifest_parses_and_resolves_paths(self):
        plugin = self.write(
            "chain",
            """
[project]
name = "pymss-plugin-chain"
version = "1.2.0"

[tool.pymss.plugin]
id = "author.chain"
api_version = 2
requires_pymss = ">=2.1"

[tool.pymss.plugin.dependencies]
"author.denoise" = ">=1,<2"

[[tool.pymss.plugin.operations]]
id = "author.chain.process"
title = "Process"

[[tool.pymss.plugin.contributions.graphs]]
id = "chain"
file = "graphs/chain.json"
operation = "author.chain.process"

[[tool.pymss.studio.views]]
id = "editor"
entry = "ui/missing.html"
""",
            {"graphs/chain.json": "{}"},
        )
        manifest = read_manifest(plugin)
        self.assertEqual(manifest.plugin_id, "author.chain")
        self.assertEqual(manifest.version, "1.2.0")
        self.assertEqual(manifest.dependencies, {"author.denoise": ">=1,<2"})
        self.assertEqual(manifest.operation_ids, {"author.chain.process"})
        self.assertEqual(manifest.to_dict()["operations"], [{"id": "author.chain.process", "title": "Process"}])
        self.assertTrue(manifest.graphs[0].file.samefile(plugin / "graphs" / "chain.json"))
        self.assertNotIn("views", manifest.to_dict()["contributions"])

    def test_invalid_fields_are_rejected(self):
        head = '[tool.pymss.plugin]\nid = "a.b"\napi_version = 2\n\n'
        cases = {
            "tool-array": 'tool = []\n',
            "tool-string": 'tool = "invalid"\n',
            "pymss-array": '[tool]\npymss = []\n',
            "pymss-string": '[tool]\npymss = "invalid"\n',
            "bad-id": '[tool.pymss.plugin]\nid = "Not Valid"\napi_version = 2\n',
            "api-v1": '[tool.pymss.plugin]\nid = "a.b"\napi_version = 1\n',
            "api-string": '[tool.pymss.plugin]\nid = "a.b"\napi_version = "2"\n',
            "api-missing": '[tool.pymss.plugin]\nid = "a.b"\n',
            "escape": head + '[[tool.pymss.plugin.contributions.graphs]]\nid = "g"\nfile = "../../x.json"\noperation = "a.b.run"\n',
            "foreign-op": head + '[[tool.pymss.plugin.operations]]\nid = "other.run"\n',
            "duplicate-op": head + '[[tool.pymss.plugin.operations]]\nid = "a.b.run"\n\n[[tool.pymss.plugin.operations]]\nid = "a.b.run"\n',
            "bad-constraint": head + '[tool.pymss.plugin.dependencies]\n"other" = "latest"\n',
        }
        for name, body in cases.items():
            with self.subTest(case=name), self.assertRaises(ManifestError):
                read_manifest(self.write(name, body))

    def test_invalid_utf8_is_reported_as_a_manifest_error(self):
        plugin = self.write("bad-encoding", "")
        (plugin / "pyproject.toml").write_bytes(b"\xff\xfe")
        with self.assertRaisesRegex(ManifestError, "cannot read pyproject.toml"):
            read_manifest(plugin)

    def test_version_constraints(self):
        self.assertTrue(satisfies("1.2.0", ">=1,<2"))
        self.assertTrue(satisfies("1.2", "==1.2.0"))
        self.assertTrue(satisfies("1.2.3", "~=1.2"))
        self.assertTrue(satisfies("1.3.0", "~=1.2"))
        self.assertFalse(satisfies("2.0.0", "~=1.2"))
        self.assertFalse(satisfies("2.0.0", "<2"))
        self.assertFalse(satisfies("1.0.0", ">=1.5"))

    def test_compatible_release_preserves_the_full_lower_bound_and_prefix(self):
        for version, constraint, expected in [
            ("1.4.0", "~=1.4.5", False), ("1.4.5", "~=1.4.5", True),
            ("1.4.9", "~=1.4.5", True), ("1.5.0", "~=1.4.5", False),
            ("1.9.0", "~=1.4", True), ("2.0.0", "~=1.4", False),
            ("1.4.5.0", "~=1.4.5.1", False), ("1.4.5.1", "~=1.4.5.1", True),
            ("1.4.6", "~=1.4.5.1", False), ("1.4.5.0", "==1.4.5", True),
        ]:
            with self.subTest(version=version, constraint=constraint):
                self.assertEqual(satisfies(version, constraint), expected)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        environment = patch.dict(os.environ, {"PYMSS_PLUGINS_DIR": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        from pymss.plugins import get_registry

        registry = get_registry()
        capabilities, nodes, commands = dict(registry.capabilities), dict(registry.nodes), list(registry.cli_commands)
        self.addCleanup(lambda: registry.capabilities.update(capabilities))
        self.addCleanup(registry.capabilities.clear)
        self.addCleanup(lambda: registry.nodes.update(nodes))
        self.addCleanup(registry.nodes.clear)
        self.addCleanup(lambda: registry.cli_commands.__setitem__(slice(None), commands))
        self.addCleanup(setattr, ops, "_DEFAULT_RUNTIME", ops._DEFAULT_RUNTIME)

    def plugin(self, name: str, plugin_id: str, version: str, deps: dict | None = None,
               body: str = "", broken: bool = False, entry: str | None = None,
               operations: list[str] | None = None, root: Path | None = None) -> Path:
        directory = (root or self.root) / name
        directory.mkdir(parents=True, exist_ok=True)
        lines = [
            "[project]", f'name = "pymss-plugin-{name}"', f'version = "{version}"', "",
            "[tool.pymss.plugin]", f'id = "{plugin_id}"', "api_version = 2",
        ]
        if entry is not None:
            lines.append(f'entry = "{entry}"')
        lines.append("")
        for operation in [f"{plugin_id}.run"] if operations is None else operations:
            lines += ["[[tool.pymss.plugin.operations]]", f'id = "{operation}"', ""]
        if deps:
            lines.append("[tool.pymss.plugin.dependencies]")
            lines += [f'"{k}" = "{v}"' for k, v in deps.items()]
            lines.append("")
        directory.joinpath("pyproject.toml").write_text("\n".join(lines), encoding="utf-8")
        code = body or (
            "def setup(plugin):\n"
            f'    plugin.register_operation(id="{plugin_id}.run", title="{plugin_id}", inputs={{}},\n'
            '        params={"type":"object","properties":{}}, outputs={}, run=lambda c,i,p: {})\n'
        )
        if broken:
            code += "\nraise RuntimeError('setup exploded')\n"
        directory.joinpath("__init__.py").write_text(code, encoding="utf-8")
        return directory

    def states(self, manager: PluginManager):
        return {s.manifest.plugin_id if s.manifest else s.name: s for s in manager.discover()}

    def test_dependency_order_and_registration(self):
        self.plugin("master", "author.master", "1.0.0", deps={"author.util": ">=1"})
        self.plugin("util", "author.util", "1.4.0")
        manager = PluginManager(self.root)
        manager.discover()
        manager.resolve(pymss_version=pymss_package_version())
        order = [s.manifest.plugin_id for s in manager.activation_order()]
        self.assertEqual(order, ["author.util", "author.master"])

        ops._DEFAULT_RUNTIME = None
        runtime = ops.Runtime()
        rows = manager.activate(lambda source: ops._RuntimePluginHandle(runtime, source=source))
        by_id = {row["plugin_id"]: row for row in rows}
        self.assertEqual(by_id["author.master"]["status"], "activated")
        self.assertIn("author.util.run", runtime.list_operations()[0]["id"].join([]) or
                      [op["id"] for op in runtime.list_operations()])
        self.assertTrue(by_id["author.util"]["loaded"])

    def test_malformed_manifests_do_not_block_healthy_neighbors(self):
        malformed = {
            "bad-encoding": b"\xff\xfe",
            "bad-tool": b"tool = []\n",
            "bad-pymss": b"[tool]\npymss = []\n",
        }
        for name, content in malformed.items():
            directory = self.plugin(name, f"author.{name}", "1.0.0")
            (directory / "pyproject.toml").write_bytes(content)
        self.plugin("healthy", "author.healthy", "1.0.0")
        manager = PluginManager(self.root)
        states = {state.name: state for state in manager.discover()}
        for name in malformed:
            self.assertEqual(states[name].status, "blocked")
            self.assertTrue(states[name].error)
        manager.resolve()
        runtime = ops.Runtime()
        rows = manager.activate(lambda source: ops._RuntimePluginHandle(runtime, source=source))
        healthy = next(row for row in rows if row["plugin_id"] == "author.healthy")
        self.assertEqual(healthy["status"], "activated")
        self.assertIn("author.healthy.run", [operation["id"] for operation in runtime.list_operations()])

    def test_missing_and_version_unsatisfied_dependencies_block(self):
        self.plugin("needs", "author.needs", "1.0.0", deps={"author.gone": ">=1"})
        self.plugin("util", "author.util", "1.0.0")
        self.plugin("picky", "author.picky", "1.0.0", deps={"author.util": ">=2"})
        manager = PluginManager(self.root)
        states = {s.manifest.plugin_id: s for s in manager.discover() if s.manifest}
        manager.resolve(pymss_version="2.1.8")
        self.assertEqual(states["author.needs"].status, "blocked")
        self.assertIn("missing dependency", states["author.needs"].error)
        self.assertEqual(states["author.picky"].status, "blocked")
        self.assertIn("unsatisfied", states["author.picky"].error)
        self.assertEqual(states["author.util"].status, "resolved")

    def test_cycle_is_blocked(self):
        self.plugin("a", "author.a", "1.0.0", deps={"author.b": ">=1"})
        self.plugin("b", "author.b", "1.0.0", deps={"author.a": ">=1"})
        manager = PluginManager(self.root)
        states = {s.manifest.plugin_id: s for s in manager.discover() if s.manifest}
        manager.resolve()
        self.assertEqual(states["author.a"].status, "blocked")
        self.assertEqual(states["author.b"].status, "blocked")
        self.assertIn("cycle", (states["author.a"].error or "") + (states["author.b"].error or ""))

    def test_failed_activation_blocks_dependents_and_reports(self):
        self.plugin("broken", "author.broken", "1.0.0", broken=True)
        self.plugin("user", "author.user", "1.0.0", deps={"author.broken": ">=1"})
        self.plugin("grandchild", "author.grandchild", "1.0.0", deps={"author.user": ">=1"})
        manager = PluginManager(self.root)
        manager.discover()
        manager.resolve()
        runtime = ops.Runtime()
        rows = manager.activate(lambda source: ops._RuntimePluginHandle(runtime, source=source))
        by_id = {row["plugin_id"]: row for row in rows}
        self.assertEqual(by_id["author.broken"]["status"], "failed")
        self.assertIn("setup exploded", by_id["author.broken"]["error"])
        self.assertEqual(by_id["author.user"]["status"], "blocked")
        self.assertEqual(by_id["author.grandchild"]["status"], "blocked")
        self.assertIn("failed to activate", by_id["author.user"]["error"])
        self.assertEqual([op["id"] for op in runtime.list_operations()], [])

    def test_duplicate_plugin_ids_block_all_providers_and_dependents(self):
        for first, second in [("1.0.0", "9.0.0"), ("9.0.0", "1.0.0"), ("9.0.0", "9.0.0")]:
            with self.subTest(versions=(first, second)):
                self.plugin("first", "author.provider", first, broken=True)
                self.plugin("second", "author.provider", second, broken=True)
                self.plugin("consumer", "author.consumer", "1.0.0", deps={"author.provider": ">=9"})
                manager = PluginManager(self.root)
                manager.discover()
                manager.resolve()
                with patch("pymss.operations._RuntimePluginHandle") as factory:
                    report = manager.activate(factory)
                factory.assert_not_called()
                self.assertEqual({row["status"] for row in report}, {"blocked"})
                for row in report:
                    self.assertIn("duplicate plugin id", row["error"])
                    self.assertIn("first, second", row["error"])

    def test_duplicate_plugin_id_from_a_second_root_preserves_the_active_provider(self):
        self.plugin("original", "author.shared", "1.0.0", body=(
            "def setup(plugin):\n"
            "    plugin.register_operation(id='author.shared.run', inputs={}, params={'type':'object'}, "
            "outputs={}, run=lambda c,i,p: {'value': 1})\n"
        ))
        runtime = ops.Runtime()
        self.assertTrue(runtime.load_plugins()[0]["loaded"])
        second_root = self.root.parent / f"{self.root.name}-managed"
        self.addCleanup(shutil.rmtree, second_root, True)
        self.plugin("duplicate", "author.shared", "1.0.0", root=second_root, body=(
            "def setup(plugin):\n"
            "    plugin.register_operation(id='author.shared.run', inputs={}, params={'type':'object'}, "
            "outputs={}, run=lambda c,i,p: {'value': 2})\n"
        ))
        report = runtime.load_plugins([second_root])
        self.assertEqual(report[0]["status"], "blocked")
        self.assertIn("already activated from", report[0]["error"])
        self.assertEqual([entry["id"] for entry in runtime.list_operations()], ["author.shared.run"])
        self.assertEqual(runtime.run("author.shared.run", inputs={}, params={}), {"value": 1})

    def test_explicit_roots_load_plugins_from_every_root(self):
        second_root = self.root.parent / f"{self.root.name}-second"
        self.addCleanup(shutil.rmtree, second_root, True)
        self.plugin("one", "author.one", "1.0.0")
        self.plugin("two", "author.two", "1.0.0", root=second_root)
        runtime = ops.Runtime()
        rows = runtime.load_plugins([self.root, second_root])
        self.assertEqual({row["plugin_id"] for row in rows if row["loaded"]}, {"author.one", "author.two"})
        self.assertEqual({row["path"] for row in rows}, {str(self.root / "one"), str(second_root / "two")})

    def test_setup_must_register_exactly_the_declared_operations(self):
        from pymss.plugins import get_registry

        capabilities_before = dict(get_registry().capabilities)
        self.plugin("missing", "author.missing", "1.0.0", operations=["author.missing.run", "author.missing.other"])
        self.plugin("extra", "author.extra", "1.0.0", operations=[], body=(
            "def setup(plugin):\n"
            "    plugin.register_capability('extra_cap', lambda: 1)\n"
            "    plugin.register_operation(id='author.extra.run', inputs={}, params={'type':'object'}, "
            "outputs={}, run=lambda c,i,p: {})\n"
        ))
        runtime = ops.Runtime()
        rows = {row["plugin_id"]: row for row in runtime.load_plugins()}
        self.assertEqual(rows["author.missing"]["status"], "failed")
        self.assertIn("missing ['author.missing.other']", rows["author.missing"]["error"])
        self.assertEqual(rows["author.extra"]["status"], "failed")
        self.assertIn("undeclared ['author.extra.run']", rows["author.extra"]["error"])
        self.assertEqual(runtime.list_operations(), [])
        self.assertEqual(dict(get_registry().capabilities), capabilities_before)

    def test_same_directory_names_across_roots_keep_distinct_plugin_ids(self):
        for dependent in (False, True):
            with self.subTest(dependent=dependent):
                roots = [self.root / str(dependent) / name for name in ("first", "second")]
                self.plugin("pkg", "author.one", "1.0.0", root=roots[0],
                            deps={"author.two": ">=1"} if dependent else None)
                self.plugin("pkg", "author.two", "1.0.0", root=roots[1])
                runtime = ops.Runtime()
                rows = runtime.load_plugins(roots)
                self.assertEqual({row["plugin_id"] for row in rows}, {"author.one", "author.two"})
                self.assertTrue(all(row["loaded"] for row in rows), rows)
                self.assertEqual({operation["id"] for operation in runtime.list_operations()},
                                 {"author.one.run", "author.two.run"})

    def test_same_named_duplicate_ids_report_every_rejected_path(self):
        roots = [self.root / name for name in ("first", "second")]
        for root in roots:
            self.plugin("pkg", "author.shared", "1.0.0", root=root)
        rows = ops.Runtime().load_plugins(roots)
        self.assertEqual({row["path"] for row in rows}, {str(root / "pkg") for root in roots})
        self.assertTrue(all(row["status"] == "blocked" for row in rows), rows)

    def test_progress_events_reach_the_host_callback(self):
        calls = []
        context = ops.OperationContext(progress=lambda *args: calls.append(args))
        context.report_progress(0.5, "half")
        context.report_progress(1.0, "done", event={"type": "tool_progress", "payload": {"completed": 1}})
        self.assertEqual(calls, [
            (0.5, "half", None),
            (1.0, "done", {"type": "tool_progress", "payload": {"completed": 1}}),
        ])

    def test_other_api_versions_are_blocked_without_importing(self):
        for version in (1, 99):
            with self.subTest(api_version=version):
                directory = self.root / f"api{version}"
                directory.mkdir(parents=True)
                directory.joinpath("pyproject.toml").write_text(
                    f'[tool.pymss.plugin]\nid = "author.api{version}"\napi_version = {version}\n', encoding="utf-8")
                directory.joinpath("__init__.py").write_text("raise RuntimeError('must not import')", encoding="utf-8")
        runtime = ops.Runtime()
        rows = {row["name"]: row for row in runtime.load_plugins()}
        for version in (1, 99):
            self.assertFalse(rows[f"api{version}"]["loaded"])
            self.assertIn(f"api_version must be 2, got {version}", rows[f"api{version}"]["error"])

    def test_runtime_load_plugins_reports_manifest_rows_and_rejects_manifestless_plugins(self):
        self.plugin("util", "author.util", "1.0.0")
        legacy = self.root / "legacy-cap"
        legacy.mkdir()
        legacy.joinpath("__init__.py").write_text(
            "from pymss.plugins import register_capability\n"
            "register_capability('legacy_shout', lambda x: x.upper(), source='legacy-cap')\n",
            encoding="utf-8")
        ops._DEFAULT_RUNTIME = None
        import pymss.plugins as plugs
        plugs.reset()
        runtime = ops.Runtime()
        report = runtime.load_plugins()
        by_name = {row["name"]: row for row in report}
        self.assertTrue(by_name["util"]["loaded"])
        self.assertEqual(by_name["util"]["plugin_id"], "author.util")
        self.assertEqual(by_name["util"]["operations"], [{"id": "author.util.run", "title": "author.util.run"}])
        self.assertFalse(by_name["legacy-cap"]["loaded"])
        self.assertIn("missing pyproject.toml", by_name["legacy-cap"]["error"])
        self.assertNotIn("legacy_shout", plugs.get_registry().capabilities)

    def test_broken_manifest_is_blocked_with_reason(self):
        directory = self.root / "badm"
        directory.mkdir(parents=True)
        directory.joinpath("pyproject.toml").write_text(
            '[tool.pymss.plugin]\nid = "Bad Id"\napi_version = 2\n', encoding="utf-8")
        directory.joinpath("__init__.py").write_text("", encoding="utf-8")
        manager = PluginManager(self.root)
        states = {s.name: s for s in manager.discover()}
        self.assertEqual(states["badm"].status, "blocked")
        self.assertIn("plugin id", states["badm"].error)

    def test_entry_modules_and_packages_support_relative_imports(self):
        cases = [
            ("flat", "entry:setup", "entry.py", "helper.py", ".helper"),
            ("package", "pkg:setup", "pkg/__init__.py", "pkg/helper.py", ".helper"),
            ("nested", "pkg.handlers:setup", "pkg/handlers.py", "pkg/helper.py", ".helper"),
            ("default", None, "__init__.py", "helper.py", ".helper"),
        ]
        original_path = list(sys.path)
        for name, entry, filename, helper, relative in cases:
            with self.subTest(entry=entry):
                plugin_id = f"author.{name}"
                directory = self.plugin(name, plugin_id, "1.0.0", entry=entry)
                for path, contents in {
                    helper: "VALUE = 42\n",
                    filename: (
                        f"from {relative} import VALUE\n"
                        "def setup(plugin):\n"
                        f"    plugin.register_operation(id='{plugin_id}.run', inputs={{}}, "
                        "params={'type':'object','properties':{}}, outputs={'value':{'type':'number'}}, "
                        "run=lambda c,i,p: {'value': VALUE})\n"
                    ),
                }.items():
                    target = directory / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(contents, encoding="utf-8")
                if filename.startswith("pkg/"):
                    (directory / "pkg/__init__.py").touch(exist_ok=True)
                runtime = ops.Runtime()
                rows = runtime.load_plugins()
                row = next(row for row in rows if row["name"] == name)
                self.assertTrue(row["loaded"], row)
                self.assertEqual(runtime.run(f"{plugin_id}.run", inputs={}, params={}), {"value": 42})
        self.assertEqual(sys.path, original_path)

    def test_failed_setup_restores_all_registration_types_and_modules(self):
        from pymss.plugins import get_registry

        registry = get_registry()
        registry.register_capability("shared_cap", lambda: "original", source="existing")
        registry.register_node("SharedNode", lambda *args: None, source="existing")
        registry.register_cli(("existing", "run"), lambda args: 0, source="existing")
        original_cap = registry.capabilities["shared_cap"]
        original_node = registry.nodes["SharedNode"]
        original_commands = list(registry.cli_commands)
        modules_before = {name for name in sys.modules if name.startswith("_pymss_manifest_plugin_")}
        self.plugin("broken", "author.broken", "1.0.0", body=(
            "from pymss.plugins import register_capability\n"
            "register_capability('import_side_effect', lambda: 1)\n"
            "def setup(plugin):\n"
            "    plugin.register_operation(id='author.broken.run', inputs={}, "
            "params={'type':'object','properties':{}}, outputs={}, run=lambda c,i,p: {})\n"
            "    plugin.register_capability('shared_cap', lambda: 'replacement')\n"
            "    plugin.register_node('SharedNode', lambda *args: None)\n"
            "    plugin.register_cli(('broken','run'), lambda args: 0)\n"
            "    raise RuntimeError('failed after registration')\n"
        ))
        runtime = ops.Runtime()
        row = runtime.load_plugins()[0]
        self.assertFalse(row["loaded"])
        self.assertEqual(runtime.list_operations(), [])
        self.assertIs(registry.capabilities["shared_cap"], original_cap)
        self.assertIs(registry.nodes["SharedNode"], original_node)
        self.assertNotIn("import_side_effect", registry.capabilities)
        self.assertEqual(registry.cli_commands, original_commands)
        self.assertEqual(
            {name for name in sys.modules if name.startswith("_pymss_manifest_plugin_")}, modules_before,
        )

    def test_cli_uses_manifest_lifecycle_and_activates_once(self):
        import pymss.cli as cli
        import pymss.plugins as plugins

        directory = self.plugin("custom", "author.custom", "1.0.0", entry="entry:setup")
        (directory / "entry.py").write_text(
            "from pathlib import Path\n"
            "def setup(plugin):\n"
            "    counter = Path(__file__).parent / 'calls.txt'\n"
            "    counter.write_text((counter.read_text() if counter.exists() else '') + '1')\n"
            "    plugin.register_operation(id='author.custom.run', inputs={}, "
            "params={'type':'object','properties':{}}, outputs={}, run=lambda c,i,p: {})\n",
            encoding="utf-8",
        )
        future = self.plugin("future", "author.future", "1.0.0", body="raise RuntimeError('must not execute')\n")
        manifest = future / "pyproject.toml"
        manifest.write_text(manifest.read_text(encoding="utf-8").replace("api_version = 2", "api_version = 999"), encoding="utf-8")
        plugins.reset()
        self.addCleanup(plugins.reset)
        ops._DEFAULT_RUNTIME = None
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.main(["ops", "list"]), 0)
            self.assertEqual(cli.main(["ops", "list"]), 0)
        self.assertIn("author.custom.run", output.getvalue())
        self.assertEqual((directory / "calls.txt").read_text(), "1")
        failed = next(row for row in plugins.get_last_report().failed if row.name == "future")
        self.assertIn("got 999", failed.error)

    def test_cli_validate_reads_the_manifest_without_running_code(self):
        import pymss.cli as cli
        import pymss.plugins as plugins

        directories = []
        for name in ("checked", "neighbor"):
            directories.append(self.plugin(name, f"author.{name}", "1.0.0", body=(
                "from pathlib import Path\n"
                "Path(__file__).with_name('imported.txt').write_text('imported')\n"
                "def setup(plugin):\n"
                "    Path(__file__).with_name('setup.txt').write_text('setup')\n"
                f"    plugin.register_operation(id='author.{name}.run', inputs={{}}, "
                "params={'type':'object','properties':{}}, outputs={}, run=lambda c,i,p: {})\n"
            )))
        directory = directories[0]
        self.addCleanup(plugins.reset)
        output = io.StringIO()
        arguments = ["plugins", "validate", str(directory)]
        for explicit_arguments in (True, False):
            with self.subTest(explicit_arguments=explicit_arguments):
                plugins.reset()
                ops._DEFAULT_RUNTIME = None
                with patch.object(sys, "argv", ["pymss", *arguments]), redirect_stdout(output):
                    self.assertEqual(cli.main(arguments if explicit_arguments else None), 0)
                for plugin_dir in directories:
                    self.assertFalse((plugin_dir / "imported.txt").exists())
                    self.assertFalse((plugin_dir / "setup.txt").exists())
        self.assertIn("author.checked 1.0.0 (api_version 2)", output.getvalue())
        self.assertIn("author.checked.run", output.getvalue())
        (directory / "pyproject.toml").write_text('[tool.pymss.plugin]\nid = "author.checked"\napi_version = 1\n', encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["plugins", "validate", str(directory)]), 1)
        for plugin_dir in directories:
            self.assertFalse((plugin_dir / "imported.txt").exists())
            self.assertFalse((plugin_dir / "setup.txt").exists())

    def test_cli_still_loads_plugin_commands(self):
        import pymss.cli as cli
        import pymss.plugins as plugins

        self.plugin("commands", "author.commands", "1.0.0", operations=[], body=(
            "def setup(plugin):\n"
            "    plugin.register_cli(('plugin-command',), lambda args: 17)\n"
        ))
        plugins.reset()
        self.addCleanup(plugins.reset)
        ops._DEFAULT_RUNTIME = None
        self.assertEqual(cli.main(["plugin-command"]), 17)


class GraphOperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def graph(self) -> Path:
        graph = {
            "nodes": [
                {"id": 1, "type": "pymss_load_audio", "widgets_values": [None, "input"],
                 "inputs": [], "outputs": [{"name": "audio"}], "pos": [0, 0]},
                {"id": 2, "type": "AudioAdjustVolume", "widgets_values": [0],
                 "inputs": [{"name": "audio", "type": "AUDIO", "link": 1}, {"name": "volume", "type": "INT", "link": 2}],
                 "outputs": [{"name": "audio"}], "pos": [0, 0]},
            ],
            "links": [[1, 1, 0, 2, 0, "AUDIO"], [2, 1, 1, 2, 1, "INT"]],
        }
        path = self.root / "graph.json"
        path.write_text(json.dumps(graph), encoding="utf-8")
        return path

    def test_graph_operation_executes_end_to_end(self):
        import numpy as np

        run = build_graph_operation(
            graph_file=self.graph(),
            audio_input={"node": "1", "widget": 0, "name": "audio"},
            param_bindings={"gain": {"nodes": ["2"], "widget": 0}},
            outputs={"audio": {"node": "2", "output": 0}},
        )
        waveform = np.ones((2, 4410), dtype=np.float32) * 0.1
        from pymss.graph.core import numpy_to_audio
        result = run(None, {"audio": numpy_to_audio(waveform, 44100)}, {"gain": 6.0})
        out = result["audio"]
        data, sr = out.audio, out.sample_rate
        self.assertEqual(sr, 44100)
        # The volume widget is dB: +6 dB ≈ ×1.995
        self.assertAlmostEqual(float(np.abs(data).max()), 0.1 * 10 ** (6 / 20), places=4)
        second = run(None, {"audio": numpy_to_audio(waveform, 44100)}, {"gain": -6.0})
        self.assertAlmostEqual(float(np.abs(second["audio"].audio).max()), 0.1 * 10 ** (-6 / 20), places=4)

    def test_binding_errors_are_rejected_at_build_time(self):
        from pymss.graph.core import DAGError

        path = self.graph()
        for overrides in [
            {"audio_input": {"node": "999"}},
            {"audio_input": {"node": "1", "widget": -1}},
            {"audio_input": {"node": "1", "widget": 99}},
            {"audio_input": {"node": "2", "widget": 0}},
            {"param_bindings": {"gain": {"nodes": ["999"]}}},
            *({"param_bindings": {"gain": {"nodes": ["2"], "widget": index}}} for index in [-1, 99, True]),
            *({"outputs": {"audio": {"node": "2", "output": slot}}} for slot in [-1, 1, 99, "0", True]),
        ]:
            with self.subTest(overrides=overrides), self.assertRaises(DAGError):
                build_graph_operation(**{
                    "graph_file": path, "audio_input": {"node": "1"},
                    "outputs": {"audio": {"node": "2"}}, **overrides,
                })

    def test_file_and_runtime_name_input_widgets_both_bind_the_supplied_audio(self):
        import numpy as np
        from pymss.graph.core import numpy_to_audio

        for widget in [0, 1]:
            with self.subTest(widget=widget):
                run = build_graph_operation(graph_file=self.graph(),
                                            audio_input={"node": "1", "widget": widget},
                                            outputs={"audio": {"node": "2"}})
                result = run(None, {"audio": numpy_to_audio(np.full((2, 1000), 0.25, dtype=np.float32), 44100)}, {})
                self.assertAlmostEqual(float(result["audio"].audio.mean()), 0.25, places=4)

    def test_distinct_output_slots_are_lifted_without_leaking_scratch_files(self):
        import numpy as np
        from pymss.graph.core import numpy_to_audio

        path = self.graph()
        graph = json.loads(path.read_text(encoding="utf-8"))
        graph["nodes"][1] = {
            "id": 2, "type": "SplitAudioChannels", "widgets_values": [],
            "inputs": [{"name": "audio", "link": 1}],
            "outputs": [{"name": "left"}, {"name": "right"}],
        }
        graph["links"] = graph["links"][:1]
        path.write_text(json.dumps(graph), encoding="utf-8")
        run = build_graph_operation(graph_file=path, audio_input={"node": "1"}, outputs={
            "left": {"node": "2", "output": 0}, "right": {"node": "2", "output": 1},
        })
        scratch = self.root / "scratch"
        result = run(ops.OperationContext(workdir=str(scratch)), {"audio": numpy_to_audio(
            np.stack([np.full(1000, 0.1), np.full(1000, 0.7)]).astype(np.float32), 44100,
        )}, {})
        self.assertAlmostEqual(float(result["left"].audio.mean()), 0.1, places=4)
        self.assertAlmostEqual(float(result["right"].audio.mean()), 0.7, places=4)
        self.assertEqual(list(scratch.iterdir()), [])

    def test_concurrent_graph_operations_keep_inputs_isolated_and_clean_up(self):
        import numpy as np
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from pymss.graph.core import numpy_to_audio
        from pymss.plugins import graph_ops

        run = build_graph_operation(graph_file=self.graph(), audio_input={"node": "1"},
                                    outputs={"audio": {"node": "2"}})
        materialize = graph_ops._audio_ref
        barrier = Barrier(2)

        def synchronized(*args):
            path = materialize(*args)
            barrier.wait(timeout=5)
            return path

        scratch = self.root / "scratch"
        context = ops.OperationContext(workdir=str(scratch))
        with patch.object(graph_ops, "_audio_ref", side_effect=synchronized), ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda level: run(context, {"audio": numpy_to_audio(
                np.full((2, 1000), level, dtype=np.float32), 44100,
            )}, {}), [0.1, 0.7]))
        for result, level in zip(results, [0.1, 0.7]):
            self.assertAlmostEqual(float(result["audio"].audio.mean()), level, places=4)
        self.assertEqual(list(scratch.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
