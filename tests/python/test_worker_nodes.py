from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap  # noqa: F401
else:
    import _bootstrap as _worker_test_bootstrap  # noqa: F401

import worker_nodes
import worker_plugins


class WorkerNodesTests(unittest.TestCase):
    def list_node(self, *, source="plugin:example.nodes", status="activated", error=None,
                  dependency_error=None, blocked_by=None, runtime_error=None, saves_output=False):
        from pymss.graph import core
        from pymss.plugins.registry import NodeRegistration

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = root / "installed" / "example.nodes"
            plugin.mkdir(parents=True)
            plugin.joinpath("pyproject.toml").write_text(
                '[project]\nname="example-nodes"\nversion="1.0.0"\n'
                '[tool.pymss.plugin]\nid="example.nodes"\napi_version=2\n'
                '[[tool.pymss.plugin.operations]]\nid="example.nodes.tool"\n'
                '[[tool.pymss.plugin.contributions.nodes]]\ntype="SharedNode"\n'
                'inputs=[]\noutputs=[]\nwidgets=[]\n', encoding="utf-8",
            )
            report = {
                "name": "example.nodes", "plugin_id": "example.nodes", "path": str(plugin),
                "loaded": status == "activated", "status": status, "error": error,
                "blocked_by": blocked_by or [], "operations": [{"id": "example.nodes.tool"}],
            }
            runtime = SimpleNamespace(_studio_reports=[report], list_operations=lambda: [])
            registrations = {} if source is None else {
                "SharedNode": NodeRegistration("SharedNode", lambda *_: None, source=source),
            }
            if source is not None:
                registrations["SharedNode"].saves_output = saves_output
            with mock.patch.object(worker_plugins, "_root", return_value=root), \
                 mock.patch.object(worker_plugins, "operation_runtime", return_value=runtime, side_effect=runtime_error), \
                 mock.patch("pymss.plugins.get_last_report", return_value=None), \
                 mock.patch.object(worker_plugins, "_validate_requirements", side_effect=dependency_error), \
                 mock.patch.object(worker_plugins, "_deferred_dependency_operations", return_value={"example.nodes.tool"}), \
                 mock.patch.object(core, "_load_builtin_nodes"), \
                 mock.patch.object(core._PLUGIN_REGISTRY, "nodes", registrations), \
                 mock.patch.object(worker_nodes, "emit") as emit:
                collected = worker_nodes.collect_plugin_nodes()
                emit.assert_not_called()
                self.assertEqual(worker_nodes.cmd_list_nodes({}), 0)
                event, payload = emit.call_args.args
                self.assertEqual(event, "nodes_list")
                self.assertEqual(payload["nodes"], collected)
                return payload["nodes"][0]

    def test_matching_live_owner_is_registered(self):
        node = self.list_node()
        self.assertTrue(node["registered"])
        self.assertIsNone(node["unavailableReason"])
        self.assertEqual(node["pluginId"], "example.nodes")

    def test_another_plugin_with_the_same_type_is_not_our_executor(self):
        node = self.list_node(source="plugin:example.other")
        self.assertFalse(node["registered"])
        self.assertEqual(node["status"], "source_mismatch")
        self.assertIn("example.other", node["unavailableReason"])

    def test_only_available_owner_advertises_persistent_outputs(self):
        self.assertTrue(self.list_node(saves_output=True)["savesOutput"])
        self.assertFalse(self.list_node()["savesOutput"])
        for kwargs in [
            {"source": "plugin:example.other"},
            {"status": "failed", "error": "setup failed"},
            {"dependency_error": RuntimeError("missing dependency")},
            {"runtime_error": RuntimeError("runtime unavailable")},
        ]:
            with self.subTest(kwargs=kwargs):
                self.assertFalse(self.list_node(saves_output=True, **kwargs)["savesOutput"])

    def test_blocked_and_failed_plugins_expose_the_loading_error(self):
        for status in ("blocked", "failed"):
            with self.subTest(status=status):
                node = self.list_node(status=status, error="dependency example.required is unavailable",
                                      blocked_by=["example.required"])
                self.assertFalse(node["registered"])
                self.assertEqual(node["status"], status)
                self.assertIn("example.required", node["unavailableReason"])
                self.assertEqual(node["blockedBy"], ["example.required"])

    def test_deferred_tool_dependencies_do_not_make_workflow_node_available(self):
        node = self.list_node(dependency_error=RuntimeError("Missing plugin dependency: funasr"))
        self.assertFalse(node["registered"])
        self.assertEqual(node["status"], "missing_dependencies")
        self.assertIn("funasr", node["unavailableReason"])

    def test_missing_executor_and_runtime_failure_are_diagnostic(self):
        node = self.list_node(source=None)
        self.assertFalse(node["registered"])
        self.assertEqual(node["status"], "unregistered")
        self.assertIn("SharedNode", node["unavailableReason"])
        node = self.list_node(runtime_error=RuntimeError("pymss runtime unavailable"))
        self.assertFalse(node["registered"])
        self.assertEqual(node["status"], "runtime_unavailable")
        self.assertIn("pymss runtime unavailable", node["unavailableReason"])

    def test_preflight_rejects_declared_unavailable_nodes_with_plugin_and_reason(self):
        unavailable = self.list_node(status="blocked", error="dependency example.required is unavailable")
        with mock.patch.object(worker_nodes, "collect_plugin_nodes", return_value=[unavailable]):
            with self.assertRaisesRegex(RuntimeError, r"SharedNode: example.nodes:.*example.required"):
                worker_nodes.validate_plugin_node_types(iter(["SharedNode", "SharedNode"]))

    def test_preflight_allows_active_owner_and_leaves_other_types_to_the_loader(self):
        active = self.list_node()
        rejected = {**active, "registered": False, "pluginId": "example.other", "unavailableReason": "node type already registered"}
        unrelated = {**rejected, "type": "UnusedNode"}
        for rows in ([active, rejected, unrelated], [rejected, active, unrelated]):
            with mock.patch.object(worker_nodes, "collect_plugin_nodes", return_value=rows):
                worker_nodes.validate_plugin_node_types(["SharedNode", "pymss_load_audio", "UnknownNode"])

    def test_preflight_empty_graph_does_not_initialize_plugins(self):
        with mock.patch.object(worker_nodes, "collect_plugin_nodes") as collect:
            worker_nodes.validate_plugin_node_types([])
            collect.assert_not_called()

    def test_preflight_bad_declaration_cannot_disable_a_live_builtin(self):
        for source in ("builtin", "pymss.builtin", "plugin:example.other", None):
            with self.subTest(source=source):
                declaration = self.list_node(source=source, status="failed", error="reserved node type")
                self.assertFalse(declaration["registered"])
                with mock.patch.object(worker_nodes, "collect_plugin_nodes", return_value=[declaration]):
                    if source in {"builtin", "pymss.builtin"}:
                        worker_nodes.validate_plugin_node_types(["SharedNode"])
                    else:
                        with self.assertRaisesRegex(RuntimeError, "reserved node type"):
                            worker_nodes.validate_plugin_node_types(["SharedNode"])


if __name__ == "__main__":
    unittest.main()
