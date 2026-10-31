"""Workflow registrations obey the same manifest/rollback boundary as operations."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pymss.graph.core import DAGError
from pymss.operations import OperationContext, OperationError, _RuntimePluginHandle, create_runtime
from pymss.plugins import ManifestError, get_registry, read_manifest
from pymss.plugins.registry import PluginRegistry


class PluginNodeContractTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        registry = get_registry()
        capabilities = dict(registry.capabilities)
        nodes = dict(registry.nodes)
        commands = list(registry.cli_commands)
        trusted = dict(registry._builtin_registrations)

        def restore():
            registry.capabilities.clear()
            registry.capabilities.update(capabilities)
            registry.nodes.clear()
            registry.nodes.update(nodes)
            registry.cli_commands[:] = commands
            registry._builtin_registrations.clear()
            registry._builtin_registrations.update(trusted)

        self.addCleanup(restore)

    def plugin(self, name, declared=(), body="pass", widgets="[]"):
        directory = self.root / name
        directory.mkdir(exist_ok=True)
        manifest = (
            f'[project]\nname = "node-contract-{name}"\nversion = "1.0.0"\n'
            f'[tool.pymss.plugin]\nid = "contract.{name}"\napi_version = 2\n'
        )
        for node_type in declared:
            manifest += (
                f'[[tool.pymss.plugin.contributions.nodes]]\ntype = "{node_type}"\n'
                f'widgets = {widgets}\n'
            )
        (directory / "pyproject.toml").write_text(manifest, encoding="utf-8")
        (directory / "__init__.py").write_text(
            "def setup(plugin):\n" + "\n".join("    " + line for line in body.splitlines()) + "\n",
            encoding="utf-8",
        )
        return directory

    def test_missing_and_undeclared_nodes_fail_and_roll_back(self):
        self.plugin("mismatch", ["DeclaredContractNode"],
                    "plugin.register_node('ActualContractNode', lambda ctx, inputs: None)")
        row = create_runtime().load_plugins([self.root])[0]
        self.assertFalse(row["loaded"])
        self.assertIn("missing ['DeclaredContractNode']", row["error"])
        self.assertIn("undeclared ['ActualContractNode']", row["error"])
        self.assertNotIn("ActualContractNode", get_registry().nodes)

    def test_direct_node_registration_cannot_bypass_manifest_tracking(self):
        self.plugin("direct", body=(
            "from pymss.plugins import register_node\n"
            "register_node('DirectContractNode', lambda ctx, inputs: None, source='contract.direct')"
        ))
        row = create_runtime().load_plugins([self.root])[0]
        self.assertFalse(row["loaded"])
        self.assertIn("must be registered through setup(plugin)", row["error"])
        self.assertNotIn("DirectContractNode", get_registry().nodes)

    def test_graph_api_cannot_label_undeclared_plugin_nodes_as_builtin(self):
        for name, fail in (("graph_direct", False), ("graph_failure", True)):
            with self.subTest(fail=fail):
                body = (
                    "from pymss.graph.core import register_node, NodeSignature\n"
                    "register_node('FakeBuiltinContractNode', signature=lambda node: NodeSignature([], [], []), execute=lambda ctx, inputs: None)\n"
                )
                if fail:
                    body += "raise RuntimeError('setup failed')"
                self.plugin(name, body=body)
                rows = create_runtime().load_plugins([self.root])
                row = next(row for row in rows if row["name"] == name)
                self.assertFalse(row["loaded"])
                self.assertIn("setup failed" if fail else "must be registered through setup(plugin)", row["error"])
                self.assertNotIn("FakeBuiltinContractNode", get_registry().nodes)

    def test_failed_setup_removes_capabilities_and_commands_with_fake_builtin_label(self):
        self.plugin("fake_labels", body=(
            "from pymss.plugins import register_capability, register_cli\n"
            "register_capability('fake_builtin_contract', lambda: None, source='builtin')\n"
            "register_cli('fake-contract', lambda args: None, source='builtin')\n"
            "raise RuntimeError('setup failed')"
        ))
        self.assertFalse(create_runtime().load_plugins([self.root])[0]["loaded"])
        self.assertNotIn("fake_builtin_contract", get_registry().capabilities)
        self.assertFalse(any(entry.path == ("fake-contract",) for entry in get_registry().cli_commands))

    def test_lazy_builtin_nodes_survive_a_subsequent_plugin_failure(self):
        self.plugin("lazy", body=(
            "from pymss.graph.core import get_node_type\n"
            "assert get_node_type('SaveAudio') is not None\n"
            "raise RuntimeError('setup failed after builtin loading')"
        ))
        code = (
            "import json, sys\n"
            "from pymss.operations import create_runtime\n"
            "from pymss.graph.core import get_node_type\n"
            "from pymss.plugins import get_registry\n"
            "row = create_runtime().load_plugins([sys.argv[1]])[0]\n"
            "entry = get_registry().nodes['SaveAudio']\n"
            "print(json.dumps({'loaded': row['loaded'], 'error': row['error'], "
            "'trusted': get_registry().is_builtin_registration('node', 'SaveAudio', entry), "
            "'available': get_node_type('SaveAudio') is not None}))\n"
        )
        process = subprocess.run([sys.executable, "-c", code, str(self.root)], check=True, capture_output=True, text=True)
        result = json.loads(process.stdout)
        self.assertFalse(result["loaded"])
        self.assertIn("setup failed after builtin loading", result["error"])
        self.assertTrue(result["trusted"])
        self.assertTrue(result["available"])

    def test_reserved_builtin_names_fail_before_executor_import_without_poisoning_graphs(self):
        for index, node_type in enumerate(("SaveAudio", "LoadAudio", "mss_separate", "pymss_mss_separate")):
            self.plugin(f"reserved_{index}", [node_type],
                        f"plugin.register_node({node_type!r}, lambda ctx, inputs: None)")
        self.plugin("tool", ["LightweightContractNode"],
                    "plugin.register_node('LightweightContractNode', lambda ctx, inputs: None)")
        code = (
            "import json, sys\n"
            "class BlockExecutors:\n"
            "    def find_spec(self, fullname, path=None, target=None):\n"
            "        if fullname in {'pymss.graph.nodes', 'pymss.graph.builtin_nodes'}:\n"
            "            raise AssertionError('graph executors must remain lazy')\n"
            "guard = BlockExecutors()\n"
            "sys.meta_path.insert(0, guard)\n"
            "from pymss.operations import create_runtime\n"
            "rows = create_runtime().load_plugins([sys.argv[1]])\n"
            "assert 'pymss.graph.nodes' not in sys.modules\n"
            "assert 'pymss.graph.builtin_nodes' not in sys.modules\n"
            "sys.meta_path.remove(guard)\n"
            "from pymss.graph.core import get_node_type\n"
            "from pymss.plugins import get_registry\n"
            "from pymss._builtin_node_types import BUILTIN_NODE_TYPES\n"
            "assert get_node_type('input_audio') is not None\n"
            "assert get_node_type('StringConstant') is not None\n"
            "registry = get_registry()\n"
            "actual = {name for name, entry in registry.nodes.items() "
            "if registry.is_builtin_registration('node', name, entry)}\n"
            "assert actual == BUILTIN_NODE_TYPES, (actual - BUILTIN_NODE_TYPES, BUILTIN_NODE_TYPES - actual)\n"
            "print(json.dumps({'rows': rows, 'builtin_count': len(actual)}))\n"
        )
        process = subprocess.run([sys.executable, "-c", code, str(self.root)], check=True, capture_output=True, text=True)
        result = json.loads(process.stdout)
        rows = {row["name"]: row for row in result["rows"]}
        self.assertTrue(rows["tool"]["loaded"])
        for index in range(4):
            self.assertFalse(rows[f"reserved_{index}"]["loaded"])
            self.assertIn("reserved pymss built-in", rows[f"reserved_{index}"]["error"])
        self.assertGreater(result["builtin_count"], 0)

    def test_builtin_identity_is_recorded_even_when_core_initialization_raises(self):
        registry = PluginRegistry()
        with self.assertRaisesRegex(RuntimeError, "partial initialization"):
            with registry._builtin_registration():
                registry.register_node("PartialBuiltin", lambda *args: None)
                raise RuntimeError("partial initialization")
        previous = registry.nodes["PartialBuiltin"]
        self.assertTrue(registry.is_builtin_registration("node", "PartialBuiltin", previous))
        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register_node("PartialBuiltin", lambda *args: None, source="builtin")
        registry.nodes.pop("PartialBuiltin")
        registry.register_node("PartialBuiltin", lambda *args: None, source="builtin")
        self.assertFalse(registry.is_builtin_registration("node", "PartialBuiltin", registry.nodes["PartialBuiltin"]))

    def test_builtin_alias_cannot_overwrite_a_plugin_node(self):
        from pymss.graph.core import register_alias

        registry = get_registry()
        with registry._builtin_registration():
            registry.register_node("CanonicalBuiltinContract", lambda *args: "builtin")
        registry.register_node("PluginAliasContract", lambda *args: "plugin", source="plugin:contract.owner")
        previous = registry.nodes["PluginAliasContract"]
        with self.assertRaisesRegex(ValueError, "already registered"):
            register_alias("PluginAliasContract", "CanonicalBuiltinContract")
        self.assertIs(registry.nodes["PluginAliasContract"], previous)

    def test_node_conflict_preserves_successful_provider(self):
        for name in ("first", "second"):
            self.plugin(name, ["SharedContractNode"],
                        f"plugin.register_node('SharedContractNode', lambda ctx, inputs: '{name}')")
        rows = {row["name"]: row for row in create_runtime().load_plugins([self.root])}
        self.assertTrue(rows["first"]["loaded"])
        self.assertFalse(rows["second"]["loaded"])
        self.assertIn("already registered by 'plugin:contract.first'", rows["second"]["error"])
        registration = get_registry().nodes["SharedContractNode"]
        self.assertEqual(registration.source, "plugin:contract.first")
        self.assertEqual(registration.func(SimpleNamespace(), {}), "first")

    def test_capability_conflict_rolls_back_prior_nodes(self):
        registry = get_registry()
        registry.register_capability("contract_cap", lambda: "original", source="plugin:contract.owner")
        original = registry.capabilities["contract_cap"]
        self.plugin("capability", ["ContractCapNode"],
                    "plugin.register_node('ContractCapNode', lambda ctx, inputs: None)\n"
                    "plugin.register_capability('contract_cap', lambda: 'replacement')")
        row = create_runtime().load_plugins([self.root])[0]
        self.assertFalse(row["loaded"])
        self.assertIs(registry.capabilities["contract_cap"], original)
        self.assertNotIn("ContractCapNode", registry.nodes)

    def test_same_source_can_load_in_multiple_runtimes_and_failure_restores_it(self):
        directory = self.plugin("repeat", ["RepeatContractNode"],
                                "plugin.register_node('RepeatContractNode', lambda ctx, inputs: 'original')")
        self.assertTrue(create_runtime().load_plugins([self.root])[0]["loaded"])
        self.assertTrue(create_runtime().load_plugins([self.root])[0]["loaded"])
        previous = get_registry().nodes["RepeatContractNode"]
        (directory / "__init__.py").write_text(
            "def setup(plugin):\n"
            "    plugin.register_node('RepeatContractNode', lambda ctx, inputs: 'replacement')\n"
            "    raise RuntimeError('registration failed')\n", encoding="utf-8",
        )
        row = create_runtime().load_plugins([self.root])[0]
        self.assertFalse(row["loaded"])
        self.assertIs(get_registry().nodes["RepeatContractNode"], previous)

    def test_late_builtin_registration_reports_conflict_without_overwriting(self):
        registry = PluginRegistry()

        def plugin(*args):
            return "plugin"

        registry.register_node("FutureBuiltinContractNode", plugin, source="plugin:contract.custom")
        registry.register_capability("trim", plugin, source="plugin:contract.custom")
        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register_node("FutureBuiltinContractNode", lambda *args: "builtin", source="builtin")
        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register_capability("trim", lambda *args: "builtin", source="builtin")
        self.assertIs(registry.nodes["FutureBuiltinContractNode"].func, plugin)
        self.assertIs(registry.capabilities["trim"].func, plugin)

    def test_builtin_label_and_override_flag_do_not_bypass_reserved_names(self):
        registry = PluginRegistry()
        with self.assertRaisesRegex(ValueError, "reserved pymss built-in"):
            registry.register_node("SaveAudio", lambda *args: None, source="builtin", allow_override_builtin=True)
        self.assertNotIn("SaveAudio", registry.nodes)

    def test_node_operation_calls_keep_runtime_validation_and_context(self):
        handle = _RuntimePluginHandle(create_runtime(), "plugin:contract.operation")
        contexts = []

        def run(ctx, inputs, params):
            contexts.append(ctx)
            return {"gain": params["gain"]}

        handle.register_operation(
            id="contract.operation.apply",
            params={"type": "object", "properties": {"gain": {"type": "number", "minimum": 0, "default": 2}}},
            outputs={"gain": {"type": "number"}}, run=run,
        )
        context = OperationContext()
        self.assertEqual(handle.run_operation("contract.operation.apply", inputs={}, params={}, context=context), {"gain": 2})
        self.assertEqual(contexts, [context])
        with self.assertRaisesRegex(OperationError, "below minimum"):
            handle.run_operation("contract.operation.apply", inputs={}, params={"gain": -1})
        with self.assertRaisesRegex(OperationError, "not registered by"):
            handle.run_operation("another.plugin.apply", inputs={}, params={})
        self.assertEqual(contexts, [context])

    def test_widget_constraints_are_checked_before_executor(self):
        widgets = '[{name="gain", type="number", default=0.0, min=-6.0, max=6.0}, {name="mode", type="combo", default="auto", options=["auto", "manual"]}, {name="enabled", type="toggle", default=true}]'
        self.plugin("widgets", ["WidgetContractNode"],
                    "plugin.register_node('WidgetContractNode', lambda ctx, inputs: 'executed')", widgets)
        self.assertTrue(create_runtime().load_plugins([self.root])[0]["loaded"])
        execute = get_registry().nodes["WidgetContractNode"].func

        def context(values):
            return SimpleNamespace(current_node_id=1, nodes_by_id={1: SimpleNamespace(data={"widgets_values": values})})

        for values in ([], [None], [0, "manual", False], [1.5, "auto", True, "legacy-extra-slot"]):
            with self.subTest(values=values):
                self.assertEqual(execute(context(values), {}), "executed")
        for values in ([7], [-7], ["1"], [True], [float("nan")], [float("inf")], [0, "unknown"], [0, "auto", "false"], {}):
            with self.subTest(values=values), self.assertRaises(DAGError):
                execute(context(values), {})

    def test_invalid_widget_declarations_are_rejected_at_discovery(self):
        for spec in (
            '{name="gain", type="number", min=5, max=1}',
            '{name="gain", type="number", step=0}',
            '{name="gain", type="number", min="zero"}',
            '{name="gain", type="number", default=99, max=1}',
            '{name="mode", type="combo", default="missing", options=["auto"]}',
        ):
            with self.subTest(spec=spec):
                directory = self.plugin("invalid", ["InvalidContractNode"], widgets=f"[{spec}]")
                with self.assertRaises(ManifestError):
                    read_manifest(directory)


if __name__ == "__main__":
    unittest.main()
