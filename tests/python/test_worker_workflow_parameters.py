from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from . import _bootstrap  # noqa: F401

import worker_workflow_parameters as policy
import worker_models
import worker_workflows
from worker_inference_constraints import InferenceParameterError
from worker_inference_snapshot import compute_inference_fingerprint


def entry(node_id, parameters, *, fingerprint=None, use_tta=False, sources=None, **fields):
    return {
        "id": str(node_id), "model": "[Category] Display alias",
        "parameters": parameters, "useTta": use_tta,
        "sources": {"enable_tta": "runtime-default", **(sources or {})},
        **({"fingerprint": fingerprint} if fingerprint is not None else {}),
        **fields,
    }


class WorkflowParameterValidationTests(unittest.TestCase):
    def test_invalid_payload_rejected_before_installing_a_consumer(self):
        node = SimpleNamespace(id=12, type="mss_separate", inputs=[], data={})
        for snapshot, error in (
            ({"version": True, "nodes": {}}, "version 1"),
            ({"version": 1, "nodes": {}}, "missing: 12"),
            ({"version": 1, "nodes": {"12": entry(13, {})}}, "node id"),
            ({"version": 1, "nodes": {"12": entry(12, {"chunk_size": 0})}}, "chunk_size"),
            ({"version": 1, "nodes": {"12": entry(12, {"standardize": 0})}}, "standardize"),
            ({"version": 1, "nodes": {"12": entry(12, {"batch_size": float("inf")})}}, "finite numbers"),
        ):
            with self.subTest(snapshot=snapshot), self.assertRaisesRegex(InferenceParameterError, error):
                policy.install_workflow_parameters(None, SimpleNamespace(nodes=[node]), snapshot, report=Mock())
        self.assertEqual(node.type, "mss_separate")

    def test_downloaded_file_receipt_only_accepts_the_file_observed_by_this_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "weights"
            kwargs = {"model_name": "catalog-alias", "download": True}
            identity = ("vr", None, str(model))
            fingerprint = compute_inference_fingerprint(*identity)
            downloads = {}
            report = Mock()

            def get(**_):
                if not model.exists():
                    model.write_bytes(b"downloaded")

            cache = SimpleNamespace(get=Mock(side_effect=get))
            consumer = policy._ConsumerPolicy(
                "node", {}, False, {}, {}, fingerprint, False, False, {}, None, report, downloads,
            )
            with patch.object(policy, "_resolve_execution_model", return_value=(identity, True)):
                options = {**kwargs, "inference_params": {}, "use_tta": False}
                policy._ConsumerCache(cache, consumer, {}).get(**options)
                policy._ConsumerCache(cache, consumer, {}).get(**options)
                self.assertEqual(report.call_args.args[0]["fingerprint"], compute_inference_fingerprint(*identity))
                model.write_bytes(b"changed")
                with self.assertRaisesRegex(InferenceParameterError, "fingerprint mismatch"):
                    policy._ConsumerCache(cache, consumer, {}).get(**options)
            self.assertEqual(cache.get.call_count, 2)
            self.assertEqual(report.call_count, 2)

    def test_first_download_without_metadata_records_fingerprint_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model.ckpt"
            config = Path(directory) / "model.yaml"
            identity = ("mel_band_roformer", str(config), str(model))
            report = Mock()

            def download(**_):
                if not model.exists():
                    model.write_bytes(b"downloaded")
                    config.write_text("audio: {chunk_size: 8192}", encoding="utf-8")

            cache = SimpleNamespace(get=Mock(side_effect=download))
            consumer = policy._ConsumerPolicy(
                "node", {}, False, {}, {}, None, False, False, {}, None, report, {},
            )
            options = {"model_name": "catalog-alias", "download": True, "inference_params": {}, "use_tta": False}
            with patch.object(policy, "_resolve_execution_model", return_value=(identity, True)):
                policy._ConsumerCache(cache, consumer, {}).get(**options)
                fingerprint = report.call_args.args[0]["fingerprint"]
                self.assertEqual(fingerprint, compute_inference_fingerprint(*identity))
                policy._ConsumerCache(cache, consumer, {}).get(**options)
                config.write_text("audio: {chunk_size: 16384}", encoding="utf-8")
                with self.assertRaisesRegex(InferenceParameterError, "fingerprint mismatch"):
                    policy._ConsumerCache(cache, consumer, {}).get(**options)
                consumer.fingerprint = fingerprint
                consumer.downloads.clear()
                with self.assertRaisesRegex(InferenceParameterError, "fingerprint mismatch"):
                    policy._ConsumerCache(cache, consumer, {}).get(**options)
            self.assertEqual(cache.get.call_count, 2)

    def test_separately_queued_catalog_tasks_accept_installed_weights_with_unchanged_config(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "weights.ckpt"
            config = Path(directory) / "config.yaml"
            config.write_text("audio: {chunk_size: 8192}", encoding="utf-8")
            identity = ("mel_band_roformer", str(config), str(model))
            submitted = compute_inference_fingerprint(*identity)
            options = {"model_name": "catalog-alias", "download": True, "inference_params": {}, "use_tta": False}

            def download(**_):
                if not model.is_file():
                    model.write_bytes(b"downloaded")

            cache = SimpleNamespace(get=Mock(side_effect=download))

            def run(fingerprint, *, download=True, catalog=True):
                report = Mock()
                consumer = policy._ConsumerPolicy(
                    "node", {}, False, {}, {}, fingerprint, False, False, {}, None, report, {},
                )
                with patch.object(policy, "_resolve_execution_model", return_value=(identity, catalog)):
                    policy._ConsumerCache(cache, consumer, {}).get(**{**options, "download": download})
                return report.call_args.args[0]["fingerprint"]

            installed = run(submitted)
            self.assertEqual(run(submitted), installed)
            self.assertEqual(run(submitted, download=False), installed)
            self.assertEqual(run(submitted, download=False, catalog=False), installed)
            self.assertIs(cache.get.call_args.kwargs["download"], False)
            model.write_bytes(b"new weights")
            with self.assertRaisesRegex(InferenceParameterError, "fingerprint mismatch"):
                run(installed)
            config.write_text("audio: {chunk_size: 16384}", encoding="utf-8")
            with self.assertRaisesRegex(InferenceParameterError, "fingerprint mismatch"):
                run(submitted)

    def test_vr_fallback_snapshot_records_installed_yaml_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "weights.pth"
            config = Path(directory) / "config.yaml"
            identity = ("vr", str(config), str(model))
            submitted = compute_inference_fingerprint(*identity)
            report = Mock()
            consumer = policy._ConsumerPolicy(
                "node", {}, False, {}, {}, submitted, False, False, {}, None, report, {},
            )

            def download(**_):
                if not model.is_file():
                    model.write_bytes(b"downloaded")
                    config.write_text("inference: {normalize: true}", encoding="utf-8")

            cache = SimpleNamespace(get=Mock(side_effect=download))
            options = {"model_name": "vr-alias", "download": True, "inference_params": {}, "use_tta": False}
            with patch.object(policy, "_resolve_execution_model", return_value=(identity, True)):
                policy._ConsumerCache(cache, consumer, {}).get(**options)
                consumer.fingerprint = report.call_args.args[0]["fingerprint"]
                self.assertNotEqual(consumer.fingerprint, submitted)
                self.assertEqual(consumer.fingerprint, compute_inference_fingerprint(*identity))
                consumer.downloads.clear()
                policy._ConsumerCache(cache, consumer, {}).get(**options)
                config.write_text("inference: {normalize: false}", encoding="utf-8")
                with self.assertRaisesRegex(InferenceParameterError, "fingerprint mismatch"):
                    policy._ConsumerCache(cache, consumer, {}).get(**options)


class NativeWorkflowParameterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import numpy as np
            import pymss.graph as graph
        except (ImportError, ModuleNotFoundError) as exc:
            raise unittest.SkipTest(f"Native pymss graph runtime is unavailable: {exc}") from exc
        cls.np = np
        cls.graph = graph
        cls.dynamic_type = "studio_test_workflow_dynamic_params"
        graph.register_node(
            cls.dynamic_type,
            signature=lambda _: graph.NodeSignature([], ["params"], [graph.MSS_PARAMS]),
            execute=lambda ctx, _: graph.NodeResult(outputs={
                0: graph.ParamsArtifact(ctx.nodes_by_id[ctx.current_node_id].data["widgets_values"][0], "mss"),
            }),
        )

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.model_path = self.root / "model.ckpt"
        self.model_path.write_bytes(b"weights fixture")
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text("audio: {chunk_size: 8192}\ninference: {overlap_size: 2048}\n", encoding="utf-8")
        self.created = []
        self.processed = []
        self.events = self.enterContext(patch.object(worker_workflows, "emit"))
        self.enterContext(patch("worker_plugins.operation_runtime"))
        self.enterContext(patch("worker_nodes.validate_plugin_node_types"))
        self.enterContext(patch("pymss.audio_io.load_audio", return_value=(
            self.np.ones((2, 16), dtype=self.np.float32), 44100, None,
        )))
        self.resolver = self.enterContext(patch("pymss.model_registry.resolve_model", side_effect=self.resolve))
        self.enterContext(patch.object(worker_workflows, "_workflow_separator_factory", side_effect=self.create))

    def resolve(self, name, *, model_dir, require_exists):
        self.assertIn(name, {"actual-alias", "vr-alias"})
        self.assertFalse(require_exists)
        return {
            "model_type": "vr" if name == "vr-alias" else "mel_band_roformer",
            "model_path": str(self.model_path),
            "config_path": None if name == "vr-alias" else str(self.config_path),
        }

    def create(self, **kwargs):
        self.created.append(kwargs)

        def separate(audio, **_):
            self.processed.append(kwargs)
            return {"Vocals": audio}

        return SimpleNamespace(
            model_type=kwargs.get("model_type", "mss"),
            config=SimpleNamespace(audio={"sample_rate": 44100}, instruments=["Vocals"]),
            separate=separate,
        )

    def fingerprint(self, model_type="mel_band_roformer"):
        config_path = None if model_type == "vr" else self.config_path
        return compute_inference_fingerprint(model_type, config_path, self.model_path)

    def run_workflow(self, definition, nodes, *, task_id="workflow-params", context=None, download_method=None, **kwargs):
        try:
            return worker_workflows._run_pymss(
                {"workflow": definition, "workflowParameters": {"version": 1, "nodes": nodes, "context": context or {}},
                 "modelDir": str(self.root), "device": "cpu", "downloadMethod": download_method},
                task_id, str(self.root / "input.wav"), None, str(self.root / "output"), "flat", **kwargs,
            )
        finally:
            worker_workflows._cleanup_workflow_file(task_id)

    def reports(self):
        return [(call.kwargs["task_id"], call.args[1]) for call in self.events.call_args_list
                if call.args[0] == "workflow_node_parameters"]

    @staticmethod
    def separation(node_id, producer_id, *, model="[Category] actual-alias", node_type="pymss_mss_separate", **raw):
        ports = [{"name": "audio", "link": node_id * 10}]
        links = [[node_id * 10, 1, 0, node_id, 0, "AUDIO"]]
        if producer_id is not None:
            ports.append({"name": "params", "link": node_id * 10 + 1})
            links.append([node_id * 10 + 1, producer_id, 0, node_id, 1, "PYMSS_MSS_PARAMS"])
        return {
            "id": node_id, "type": node_type, "widgets_values": [model, "cpu"], "inputs": ports,
            "outputs": [{"name": "Vocals (Audio)", "type": "AUDIO"}], **raw,
        }, links

    def test_same_model_consumers_merge_shared_dynamic_and_static_links_before_native_cache(self):
        linked = {"batch_size": 3, "overlap_size": 0, "enable_tta": True, "use_amp": False, "mask_mode": "direct"}
        definition = {"nodes": [
            {"id": 1, "type": "input_audio"},
            {"id": 2, "type": self.dynamic_type, "widgets_values": [linked]},
            {"id": 3, "type": "pymss_mss_params", "widgets_values": [5, "0", "0", False, False, False]},
        ], "links": []}
        fingerprint = self.fingerprint()
        nodes = {}
        for node_id, producer in [(4, 2), (5, 2), (6, 2), (7, 3)]:
            node, links = self.separation(node_id, producer, node_type="mss_separate_list" if node_id == 7 else "pymss_mss_separate")
            definition["nodes"].append(node)
            definition["links"].extend(links)
            nodes[str(node_id)] = entry(
                node_id, {"batch_size": 5 if node_id == 7 else 1, "chunk_size": 8192,
                          "overlap_size": 0 if node_id == 7 else 1024},
                fingerprint=fingerprint, linked=True, dynamic=node_id != 7,
                overrides={"batch_size": 4, "enable_tta": False} if node_id in {4, 6} else {},
                sources={"chunk_size": "model-config", "overlap_size": "link" if node_id == 7 else "custom",
                         "batch_size": "link" if node_id == 7 else "model-library"},
            )
        before = copy.deepcopy((definition, nodes))
        with (
            self.graph.SeparatorCache(factory=self.create, max_entries=None) as cache,
            patch.object(self.graph, "run_dag", wraps=self.graph.run_dag) as run,
        ):
            result = self.run_workflow(definition, nodes, separator_cache=cache)
            self.assertEqual(run.call_args.kwargs["node_results"][2].outputs[0].params, linked)
            self.run_workflow(definition, nodes, task_id="retry-params", separator_cache=cache)
            self.assertEqual(len(self.created), 3)
            self.assertEqual([item["inference_params"]["batch_size"] for item in self.processed], [4, 3, 4, 5] * 2)
            self.assertEqual([item["use_tta"] for item in self.processed], [False, True, False, False] * 2)
            self.assertEqual([item["inference_params"]["overlap_size"] for item in self.processed], [0] * 8)
            self.assertEqual(self.created[0]["inference_params"]["mask_mode"], "direct")
            self.assertIs(self.created[0]["inference_params"]["use_amp"], False)
            self.model_path.write_bytes(b"replaced weights")
            with self.assertRaisesRegex(InferenceParameterError, r"Workflow node 4: .*fingerprint mismatch"):
                self.run_workflow(definition, nodes, task_id="stale-params", separator_cache=cache)

        self.assertEqual((definition, nodes), before)
        self.assertEqual(self.reports()[0], ("workflow-params", {
            "nodeId": "4", "parameters": {"batch_size": 4, "chunk_size": 8192, "overlap_size": 0, "use_amp": False},
            "nativeExtras": {"mask_mode": "direct"},
            "useTta": False, "sources": {"batch_size": "node", "chunk_size": "model-config",
                                        "overlap_size": "link", "use_amp": "link", "enable_tta": "node"},
        }))
        self.assertEqual(self.reports()[1][1]["sources"]["enable_tta"], "link")
        self.assertEqual(self.reports()[3][1]["sources"]["overlap_size"], "link")
        self.assertEqual([task for task, _ in self.reports()], ["workflow-params"] * 4 + ["retry-params"] * 4)
        self.assertEqual(self.resolver.call_args_list[0].args, ("actual-alias",))
        self.assertEqual(self.resolver.call_args_list[0].kwargs["model_dir"], str(self.root))
        self.assertEqual({item["nodeType"] for item in result["nodeValues"]}, {"pymss_mss_separate"})
        execution = [call.args[1] for call in self.events.call_args_list
                     if call.args[0] == "workflow_execution" and call.args[1].get("nodeId") == "7"]
        self.assertTrue(execution)
        self.assertEqual({event["nodeType"] for event in execution}, {"mss_separate_list"})

    def test_dynamic_constraints_are_checked_after_link_and_override_merge(self):
        node, links = self.separation(3, 2)
        definition = {"nodes": [
            {"id": 1, "type": "input_audio"},
            {"id": 2, "type": self.dynamic_type, "widgets_values": [{"chunk_size": 1024}]},
            node,
        ], "links": links}
        nodes = {"3": entry(3, {"chunk_size": 8192, "overlap_size": 2048}, fingerprint=self.fingerprint())}
        with self.assertRaisesRegex(InferenceParameterError, r"Workflow node 3: overlap_size .* chunk_size"):
            self.run_workflow(definition, nodes)
        self.assertEqual(self.created, [])
        self.assertEqual(self.reports(), [])
        nodes["3"]["overrides"] = {"overlap_size": 0}
        self.run_workflow(definition, nodes)
        self.assertEqual(self.created[0]["inference_params"], {"chunk_size": 1024, "overlap_size": 0})

    def test_dynamic_chunk_recalculates_only_inherited_overlap(self):
        consumer, links = self.separation(3, 2)
        graph = {"nodes": [
            {"id": 1, "type": "input_audio"},
            {"id": 2, "type": self.dynamic_type, "widgets_values": [{"chunk_size": 1024}]},
            consumer,
        ], "links": links}
        context = {"mode": "custom", "intent": {"overlapBasisPoints": 5000}}
        for source, fields, expected in [
            ("custom", {}, 512), ("model-config", {"overlapCount": 4}, 768),
            ("model-config", {"scaleModelOverlap": True}, 256), ("node", {"overrides": {"overlap_size": 0}}, 0),
        ]:
            with self.subTest(source=source, fields=fields):
                value = entry(3, {"chunk_size": 8192, "overlap_size": 2048}, dynamic=True,
                              sources={"overlap_size": source}, fingerprint=self.fingerprint(), **fields)
                self.run_workflow(graph, {"3": value}, context=context)
                self.assertEqual(self.processed[-1]["inference_params"]["overlap_size"], expected)

    def test_historical_retry_keeps_observed_dynamic_values_even_when_the_producer_changes(self):
        consumer, links = self.separation(3, 2)
        generated = {"batch_size": 3, "mask_mode": "original", "enable_tta": True}
        definition = {"nodes": [
            {"id": 1, "type": "input_audio"},
            {"id": 2, "type": self.dynamic_type, "widgets_values": [generated]},
            consumer,
        ], "links": links}
        nodes = {"3": entry(3, {"batch_size": 1}, dynamic=True, fingerprint=self.fingerprint())}
        self.run_workflow(definition, nodes)
        actual = self.reports()[0][1]
        nodes["3"].update(parameters=actual["parameters"], nativeExtras=actual["nativeExtras"],
                          useTta=actual["useTta"], sources=actual["sources"], replay=True, dynamic=False)
        generated.update(batch_size=8, mask_mode="changed", enable_tta=False)
        self.run_workflow(definition, nodes, task_id="retry")
        self.assertEqual(self.processed[-1]["inference_params"], {"batch_size": 3, "mask_mode": "original"})
        self.assertTrue(self.processed[-1]["use_tta"])
        self.assertEqual(self.reports()[-1][1]["sources"]["batch_size"], "link")

    def test_simple_custom_path_legacy_sentinels_and_vr_zero_values(self):
        model_dir = self.root / "step-models"
        definition = {"version": 1, "defaults": {"inference_params": {"use_amp": False}}, "steps": [
            {"id": "custom", "model_path": str(self.model_path), "config_path": str(self.config_path),
             "model_type": "mel_band_roformer", "stems": ["Vocals"],
             "inference_params": {"overlap_size": 0, "chunk_size": "Default"}},
            {"id": "vr", "model": "vr-alias", "model_type": "vr", "model_dir": str(model_dir),
             "stems": ["Vocals"], "inference_params": {"aggression": 0, "post_process_threshold": 0}},
        ]}
        nodes = {
            "custom": entry("custom", {"chunk_size": 8192, "overlap_size": 2048}, fingerprint=self.fingerprint(),
                            sources={"overlap_size": "node"}, overrides={"overlap_size": 0}),
            "vr": entry("vr", {"aggression": 0, "post_process_threshold": 0, "normalize": False},
                        fingerprint=self.fingerprint("vr"), sources={"enable_tta": "node", "aggression": "node"}),
        }
        self.run_workflow(definition, nodes)
        self.assertEqual(self.created[0]["model_path"], str(self.model_path))
        self.assertEqual(self.created[0]["inference_params"], {"chunk_size": 8192, "overlap_size": 2048, "use_amp": False})
        self.assertEqual(self.created[1]["inference_params"], {
            "use_amp": False, "aggression": 0, "post_process_threshold": 0, "normalize": False,
        })
        self.assertIs(self.created[1]["use_tta"], False)
        self.assertEqual(self.resolver.call_args.args, ("vr-alias",))
        self.assertEqual(self.resolver.call_args.kwargs["model_dir"], str(model_dir))
        self.assertEqual([event["nodeId"] for _, event in self.reports()], ["custom", "vr"])

    def test_advanced_custom_list_verifies_raw_model_paths(self):
        node, links = self.separation(
            2, None, node_type="pymss_custom_mss_separate_list",
            widgets_values=["Display alias", "mel_band_roformer", "cpu"],
            model_path=str(self.model_path), config_path=str(self.config_path),
        )
        definition = {"nodes": [{"id": 1, "type": "input_audio"}, node], "links": links}
        self.run_workflow(definition, {"2": entry(2, {"overlap_size": 0}, fingerprint=self.fingerprint())})
        self.resolver.assert_not_called()
        self.assertEqual(self.created[0]["model_path"], str(self.model_path))
        self.assertEqual(self.created[0]["config_path"], str(self.config_path))
        self.assertEqual(len(self.reports()), 1)

    def test_native_cache_uses_frozen_download_permission_and_host_policy(self):
        for kind, name in [("mss", "actual-alias"), ("vr", "vr-alias")]:
            for method, widget_value, frozen, allowed in [
                ("urllib", True, False, False),
                ("urllib", False, True, True),
                ("never", True, True, False),
            ]:
                with self.subTest(kind=kind, frozen=frozen, method=method):
                    widgets = [name, "cpu", widget_value, "modelscope", "0", False]
                    node, links = self.separation(2, None, node_type=f"{kind}_separate", widgets_values=widgets)
                    definition = {"nodes": [{"id": 1, "type": "input_audio"}, node], "links": links}
                    self.run_workflow(definition, {"2": entry(2, {}, downloadMissing=frozen, downloadSource="huggingface")}, download_method=method)
                    self.assertIs(self.created[-1]["download"], allowed)
                    self.assertEqual(self.created[-1]["source"], "huggingface")

    def test_named_custom_auto_uses_detected_architecture_for_fingerprint(self):
        self.config_path.write_text(
            "model: {num_bands: 60}\naudio: {chunk_size: 8192}\ninference: {overlap_size: 2048}\n", encoding="utf-8",
        )
        node, links = self.separation(
            2, None, node_type="pymss_custom_mss_separate",
            widgets_values=["User model", "auto", "cpu"],
        )
        definition = {"nodes": [{"id": 1, "type": "input_audio"}, node], "links": links}
        local = {"model_path": str(self.model_path), "config_path": str(self.config_path)}
        user_entry = SimpleNamespace(**local, source="user", model_type="mel_band_roformer")
        with patch("pymss.user_models.get_user_model_entry", return_value=user_entry) as lookup, patch.object(worker_models, "emit") as emit:
            worker_models.cmd_model_info({"inferenceTarget": {"modelName": "User model", "modelType": "auto"}})
        lookup.assert_called_once_with("User model")
        fingerprint = emit.call_args.args[1]["resolution"]["fingerprint"]
        self.assertEqual(fingerprint, self.fingerprint())
        with patch("pymss.graph.nodes._resolve_user_model", return_value=local):
            self.run_workflow(definition, {"2": entry(2, {"batch_size": 4}, fingerprint=fingerprint)})
        self.assertEqual(self.created[-1]["model_type"], "auto")
        self.assertEqual(self.created[-1]["inference_params"]["batch_size"], 4)


if __name__ == "__main__":
    unittest.main()
