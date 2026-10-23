from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import ModuleType, SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap

import worker_workflows
from worker_workflows import (
    _apply_simple_ensembles,
    _apply_simple_output_names,
    _apply_simple_save_sample_rate,
    _prepare_legacy_global_input,
    _prepare_simple_runtime_definition,
    _finalize_simple_output_paths,
    _render_simple_filename,
    _simple_output_names,
    _workflow_output_stem,
)


class WorkflowTemporaryFileTests(unittest.TestCase):
    def test_cleanup_keeps_the_shared_directory_available_for_parallel_writers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(worker_workflows.tempfile, "gettempdir", return_value=temporary):
                payload = {"workflow": {"nodes": []}}
                path, _format = worker_workflows._write_workflow_file(payload, "cleanup-shared-dir")
                worker_workflows._cleanup_workflow_file("cleanup-shared-dir")

                self.assertFalse(path.exists())
                self.assertTrue(path.parent.is_dir())

    def test_single_run_removes_the_transient_definition_on_success_and_failure(self) -> None:
        for task_id, error in (("cleanup-success", None), ("cleanup-failure", RuntimeError("failed"))):
            with self.subTest(task_id=task_id):
                payload = {"taskId": task_id, "workflow": {"nodes": []}, "output": "results"}
                path, _format = worker_workflows._write_workflow_file(payload, task_id)
                self.addCleanup(path.unlink, missing_ok=True)
                run = patch.object(
                    worker_workflows,
                    "_run_pymss",
                    return_value={"files": [], "outputs": [], "outputDir": "results", "outputFormat": "wav"},
                    side_effect=error,
                )
                with run, patch.object(worker_workflows, "emit"), patch.object(worker_workflows, "emit_error", return_value=1):
                    expected = 1 if error else 0
                    self.assertEqual(worker_workflows.cmd_infer_workflow(payload), expected)
                self.assertFalse(path.exists())

    def test_batch_run_removes_each_task_definition(self) -> None:
        task_ids = ["cleanup-batch-1", "cleanup-batch-2"]
        payload = {
            "taskId": "cleanup-batch",
            "workflow": {"nodes": []},
            "output": "results",
            "tasks": [{"taskId": task_id} for task_id in task_ids],
        }
        paths = [worker_workflows._write_workflow_file(payload, task_id)[0] for task_id in task_ids]
        for path in paths:
            self.addCleanup(path.unlink, missing_ok=True)
        result = {"files": [], "outputs": [], "outputDir": "results", "outputFormat": "wav"}
        with patch.object(worker_workflows, "_run_pymss", return_value=result), \
             patch.object(worker_workflows, "emit"), patch.object(worker_workflows, "emit_error", return_value=1):
            self.assertEqual(worker_workflows.cmd_infer_workflow(payload), 0)
        self.assertTrue(all(not path.exists() for path in paths))


class SimpleAudioOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        class DAGLink:
            def __init__(self, **values):
                self.__dict__.update(values)

        class DAGNode:
            def __init__(self, *, id, type, inputs, data, title=""):
                self.id, self.type, self.inputs, self.data, self.title = id, type, inputs, data, title

        self.graph = ModuleType("pymss.graph")
        self.graph.DAGLink, self.graph.DAGNode = DAGLink, DAGNode
        self.graph.AUDIO, self.graph.STRING = "AUDIO", "STRING"
        pymss = ModuleType("pymss")
        pymss.graph = self.graph
        modules = patch.dict("sys.modules", {"pymss": pymss, "pymss.graph": self.graph})
        modules.start()
        self.addCleanup(modules.stop)

    def compile(self, definition):
        nodes = [self.graph.DAGNode(id="input", type="input_audio", inputs=[], data={})]
        nodes.extend(self.graph.DAGNode(
            id=f"step:{step['id']}", type="mss_separate", inputs=[None, None], data={},
        ) for step in definition.get("steps", []))
        dag = SimpleNamespace(nodes=nodes)
        metadata = _apply_simple_ensembles(
            dag, definition, input_path="song.wav", output_format="wav",
        )
        return dag, metadata

    @staticmethod
    def operation(algorithm, sources, *, id="process", stem="Audio", save=False):
        return {
            "id": id, "algorithm": algorithm, "output_stem": stem, "save": save,
            "inputs": [{"source": source, "weight": 1} for source in sources],
        }

    def test_five_stems_sum_chains_native_merge_and_feeds_an_ensemble(self) -> None:
        stems = ["Vocals", "Drums", "Bass", "Guitar", "Other"]
        definition = {
            "steps": [{"id": "split", "stems": stems}],
            "ensembles": [
                self.operation("sum", [f"split.{stem}" for stem in stems]),
                self.operation("avg_wave", ["process.Audio", "input"], id="blend"),
            ],
        }
        dag, metadata = self.compile(definition)
        merges = [node for node in dag.nodes if node.type == "AudioMerge"]
        self.assertEqual(len(merges), 4)
        self.assertTrue(all(node.data["widgets_values"] == ["add", False] for node in merges))
        self.assertEqual(merges[0].inputs[0].source_slot, 0)
        self.assertEqual(merges[0].inputs[1].source_slot, 2)
        for index, node in enumerate(merges[1:], 1):
            self.assertEqual(node.inputs[0].source_node_id, merges[index - 1].id)
            self.assertEqual(node.inputs[1].source_slot, (index + 1) * 2)
        blend = next(node for node in dag.nodes if node.type == "pymss_audio_ensemble")
        self.assertEqual(blend.inputs[0].source_node_id, "studio:ensemble:process")
        self.assertEqual(metadata, [])
        ids = [link.link_id for node in dag.nodes for link in node.inputs if link is not None]
        self.assertEqual(len(ids), len(set(ids)))

    def test_subtraction_preserves_input_order_and_feeds_a_separation(self) -> None:
        definition = {
            "steps": [
                {"id": "split", "stems": ["Vocals", "Other"]},
                {"id": "cleanup", "input": "process.Instrumental", "stems": ["Dry"]},
            ],
            "ensembles": [self.operation("subtract", ["input", "split.Vocals"], stem="Instrumental")],
        }
        dag, _metadata = self.compile(definition)
        operation = next(node for node in dag.nodes if node.type == "AudioMerge")
        self.assertEqual(operation.data["widgets_values"], ["subtract", False])
        self.assertEqual([link.source_node_id for link in operation.inputs], ["input", "step:split"])
        cleanup = next(node for node in dag.nodes if node.id == "step:cleanup")
        self.assertEqual(cleanup.inputs[0].source_node_id, operation.id)

    def test_pure_invert_saves_with_operation_filename_and_keeps_protocol_version(self) -> None:
        definition = {
            "version": 1, "studio": {"editor": "simple"}, "steps": [],
            "ensembles": [self.operation("invert", ["input"], save="Default")],
        }
        dag, metadata = self.compile(definition)
        operation = next(node for node in dag.nodes if node.type == "pymss_audio_invert_phase")
        save = next(node for node in dag.nodes if node.type == "pymss_save_audio")
        self.assertEqual(len(operation.inputs), 1)
        self.assertEqual(save.inputs[0].source_node_id, operation.id)
        self.assertEqual(metadata, [{"node_id": "studio:ensemble-save:process", "stem": "Audio", "filename": "song_Audio_process.wav"}])
        runtime = _prepare_simple_runtime_definition(definition)
        self.assertEqual(runtime["version"], 1)
        self.assertEqual(runtime["steps"], [])
        self.assertNotIn("ensembles", runtime)
        self.assertEqual(len(definition["ensembles"]), 1)

    def test_audio_operation_filename_supports_model_token_and_blank_template_fallback(self) -> None:
        for template, expected in (("%filename%_%model%", "song_AudioInvert.wav"), ("  ", "song_Audio_process.wav")):
            with self.subTest(template=template):
                operation = self.operation("invert", ["input"], save="Default")
                operation["output_name"] = template
                _dag, metadata = self.compile({"steps": [], "ensembles": [operation]})
                self.assertEqual(metadata, [{"node_id": "studio:ensemble-save:process", "stem": "Audio", "filename": expected}])

    def test_audio_operations_reject_wrong_input_counts_and_non_unit_weights(self) -> None:
        cases = [("sum", 1), ("sum", 11), ("subtract", 1), ("subtract", 3), ("invert", 0), ("invert", 2)]
        for algorithm, count in cases:
            with self.subTest(algorithm=algorithm, count=count):
                operation = self.operation(algorithm, ["input"] * count)
                with self.assertRaisesRegex(RuntimeError, "requires"):
                    self.compile({"steps": [], "ensembles": [operation]})
        for algorithm in ("sum", "subtract", "invert"):
            for weight in (0.5, 2, float("nan"), float("inf"), 0, -1, "invalid"):
                with self.subTest(algorithm=algorithm, weight=weight):
                    sources = ["input"] if algorithm == "invert" else ["input", "split.Vocals"]
                    operation = self.operation(algorithm, sources)
                    operation["inputs"][0]["weight"] = weight
                    with self.assertRaisesRegex(RuntimeError, "weight"):
                        self.compile({"steps": [{"id": "split", "stems": ["Vocals"]}], "ensembles": [operation]})

    def test_indirect_cycle_through_separation_is_rejected(self) -> None:
        definition = {
            "steps": [{"id": "cleanup", "input": "process.Audio", "stems": ["Dry"]}],
            "ensembles": [self.operation("invert", ["cleanup.Dry"])],
        }
        with self.assertRaisesRegex(RuntimeError, "cycle"):
            self.compile(definition)

    def test_indirect_forward_reference_through_separation_is_rejected(self) -> None:
        definition = {
            "steps": [{"id": "cleanup", "input": "later.Audio", "stems": ["Dry"]}],
            "ensembles": [
                self.operation("invert", ["cleanup.Dry"]),
                self.operation("invert", ["input"], id="later"),
            ],
        }
        with self.assertRaisesRegex(RuntimeError, "forward reference"):
            self.compile(definition)

    def test_earlier_operation_through_separation_can_feed_a_later_ensemble(self) -> None:
        definition = {
            "steps": [{"id": "cleanup", "input": "process.Audio", "stems": ["Dry"]}],
            "ensembles": [
                self.operation("invert", ["input"]),
                self.operation("avg_wave", ["cleanup.Dry", "process.Audio"], id="blend"),
            ],
        }
        dag, _metadata = self.compile(definition)
        blend = next(node for node in dag.nodes if node.id == "studio:ensemble:blend")
        self.assertEqual([link.source_node_id for link in blend.inputs], ["step:cleanup", "studio:ensemble:process"])

    def test_existing_eight_ensemble_algorithms_keep_native_weight_contract(self) -> None:
        for algorithm in worker_workflows._SIMPLE_ENSEMBLE_ALGORITHMS:
            with self.subTest(algorithm=algorithm):
                operation = self.operation(algorithm, ["input", "split.Vocals"])
                operation["inputs"][1]["weight"] = 0.25
                dag, _metadata = self.compile({
                    "steps": [{"id": "split", "stems": ["Vocals"]}], "ensembles": [operation],
                })
                node = next(node for node in dag.nodes if node.id == "studio:ensemble:process")
                self.assertEqual(node.type, "pymss_audio_ensemble")
                self.assertEqual(node.data["widgets_values"], [2, algorithm, 1.0, 0.25])


class SimpleOutputAssociationTests(unittest.TestCase):
    def test_completed_records_are_matched_by_save_node_not_position(self) -> None:
        metadata = [
            {"node_id": "save:cleanup:Dry", "stem": "Dry", "filename": "dry.wav"},
            {"node_id": "studio:ensemble-save:first", "stem": "Audio", "filename": "first.wav"},
        ]
        records = [SimpleNamespace(node_id=item["node_id"]) for item in reversed(metadata)]
        self.assertEqual(worker_workflows._match_simple_output_metadata(records, metadata), list(reversed(metadata)))

    def test_missing_unknown_or_incomplete_save_identities_fail_before_publishing(self) -> None:
        metadata = [{"node_id": "save:split:Vocals", "stem": "Vocals", "filename": "vocals.wav"}]
        for records, message in (
            ([None], "save-node identities"),
            ([SimpleNamespace(node_id="other")], "undeclared save output"),
            ([], "all requested save outputs"),
        ):
            with self.subTest(records=records), self.assertRaisesRegex(RuntimeError, message):
                worker_workflows._match_simple_output_metadata(records, metadata)
        with self.assertRaisesRegex(RuntimeError, "Duplicate"):
            worker_workflows._match_simple_output_metadata([], metadata * 2)

    def test_named_output_outside_task_directory_is_not_moved_or_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "task"
            source_root.mkdir()
            inside = source_root / "inside.wav"
            outside = root / "outside.wav"
            inside.write_bytes(b"inside")
            outside.write_bytes(b"outside")
            output_dir = root / "results"
            with self.assertRaisesRegex(RuntimeError, "outside its task directory"):
                _finalize_simple_output_paths(
                    [str(inside), str(outside)],
                    [{"filename": "one.wav"}, {"filename": "two.wav"}],
                    output_dir, source_root=source_root,
                )
            self.assertEqual(inside.read_bytes(), b"inside")
            self.assertEqual(outside.read_bytes(), b"outside")
            self.assertFalse(output_dir.exists())

    def test_missing_output_does_not_partially_publish_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "inside.wav"
            source.write_bytes(b"audio")
            output_dir = root / "results"
            with self.assertRaisesRegex(RuntimeError, "output is missing"):
                _finalize_simple_output_paths(
                    [str(source), str(root / "missing.wav")],
                    [{"filename": "one.wav"}, {"filename": "two.wav"}],
                    output_dir, source_root=root,
                )
            self.assertEqual(source.read_bytes(), b"audio")
            self.assertFalse(output_dir.exists())

    def test_duplicate_source_paths_are_rejected_before_any_file_is_published(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "inside.wav"
            source.write_bytes(b"audio")
            output_dir = root / "results"
            with self.assertRaisesRegex(RuntimeError, "same output file"):
                _finalize_simple_output_paths(
                    [str(source), str(source)],
                    [{"filename": "one.wav"}, {"filename": "two.wav"}],
                    output_dir, source_root=root,
                )
            self.assertEqual(source.read_bytes(), b"audio")
            self.assertFalse(output_dir.exists())


class SimpleSaveSampleRateTests(unittest.TestCase):
    @staticmethod
    def node(node_type, widgets=None):
        data = {} if widgets is None else {"widgets_values": widgets}
        return SimpleNamespace(id=node_type, type=node_type, data=data)

    def test_compiled_saves_keep_the_incoming_sample_rate(self) -> None:
        saves = [self.node("pymss_save_audio", ["flac", "Default", "44100", "FLOAT", "PCM_24", "320k"]) for _ in range(2)]
        _apply_simple_save_sample_rate(SimpleNamespace(nodes=[self.node("input_audio"), *saves]))
        for save in saves:
            self.assertEqual(save.data["widgets_values"], ["flac", "Default", "0", "FLOAT", "PCM_24", "320k"])

    def test_only_the_compilers_fixed_rate_on_save_nodes_is_replaced(self) -> None:
        explicit = self.node("pymss_save_audio", ["wav", "Default", "48000", "FLOAT", "PCM_24", "320k"])
        other = self.node("pymss_resample", ["wav", "Default", "44100"])
        bare = self.node("pymss_save_audio")
        short = self.node("pymss_save_audio", ["wav"])
        _apply_simple_save_sample_rate(SimpleNamespace(nodes=[explicit, other, bare, short]))
        self.assertEqual(explicit.data["widgets_values"][2], "48000")
        self.assertEqual(other.data["widgets_values"], ["wav", "Default", "44100"])
        self.assertEqual(bare.data, {})
        self.assertEqual(short.data["widgets_values"], ["wav"])


class NativeSimpleAudioOperationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            import numpy as np
            import pymss.graph as graph
            import pymss.graph.core as core
            from pymss.audio_io import load_audio, save_audio
        except (ImportError, ModuleNotFoundError) as exc:
            raise unittest.SkipTest(f"Native pymss graph runtime is unavailable: {exc}") from exc
        cls.np, cls.graph, cls.core = np, graph, core
        cls.load_audio, cls.save_audio = staticmethod(load_audio), staticmethod(save_audio)

    def run_workflow(self, definition, *, factors=None, expected_file_count=1, separation_params=None,
                     source_rate=44100):
        np, graph = self.np, self.graph
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        input_path = root / "song.wav"
        samples = np.array([[0.8, -0.7, 0.2, -0.1] * 64], dtype=np.float32)
        self.save_audio(str(input_path), samples.T, source_rate, "wav", {"wav_bit_depth": "FLOAT"})
        separation_inputs = []
        native_lookup = self.core.get_node_type

        def lookup(node_type):
            native = native_lookup(node_type)
            if node_type not in {"mss_separate", "vr_separate"}:
                return native

            def separate(ctx, inputs):
                audio = inputs["audio"]
                separation_inputs.append(audio.audio.copy())
                if separation_params is not None:
                    separation_params.append(dict(inputs["params"].params))
                return graph.NodeResult(outputs={
                    index * 2: graph.AudioArtifact(audio.audio * factor, audio.sample_rate)
                    for index, factor in enumerate(factors or [0.25])
                })

            return self.core.NodeTypeInfo(type=node_type, signature=native.signature, execute=separate)

        task_id = "native-simple-audio"
        self.addCleanup(worker_workflows._cleanup_workflow_file, task_id)
        with patch.object(self.core, "get_node_type", side_effect=lookup), patch.object(worker_workflows, "emit"):
            result = worker_workflows._run_pymss(
                {"workflow": definition, "outputFormat": "wav"}, task_id,
                input_path=str(input_path), inputs=None, output_dir=str(root / "results"), output_layout="flat",
            )
        actual, sample_rate = self.load_audio(result["files"][0], sr=None, mono=False)
        self.assertEqual(sample_rate, source_rate)
        self.assertEqual(len(result["files"]), expected_file_count)
        return np.asarray(actual).reshape(samples.shape), samples, separation_inputs, result

    def test_native_five_stem_sum_preserves_gain_before_ensemble(self) -> None:
        stems = ["Vocals", "Drums", "Bass", "Guitar", "Other"]
        definition = {
            "version": 1, "studio": {"editor": "simple"},
            "steps": [{"id": "split", "model": "fixture", "stems": stems}],
            "ensembles": [
                SimpleAudioOperationTests.operation("sum", [f"split.{stem}" for stem in stems]),
                SimpleAudioOperationTests.operation("avg_wave", ["process.Audio", "input"], id="blend", save="Default"),
            ],
        }
        actual, samples, _inputs, _result = self.run_workflow(definition, factors=[0.5] * 5)
        self.np.testing.assert_allclose(actual, samples * 1.75, atol=1e-6)
        self.assertGreater(float(actual.max()), 1)

    def test_native_input_minus_vocals_preserves_order_before_ensemble(self) -> None:
        definition = {
            "version": 1, "studio": {"editor": "simple"},
            "steps": [{"id": "split", "model": "fixture", "stems": ["Vocals"]}],
            "ensembles": [
                SimpleAudioOperationTests.operation("subtract", ["input", "split.Vocals"], stem="Instrumental"),
                SimpleAudioOperationTests.operation("avg_wave", ["process.Instrumental", "input"], id="blend", save="Default"),
            ],
        }
        actual, samples, _inputs, _result = self.run_workflow(definition, factors=[-0.5])
        self.np.testing.assert_allclose(actual, samples * 1.25, atol=1e-6)

    def test_native_pure_invert_saves_without_separation(self) -> None:
        definition = {
            "version": 1, "studio": {"editor": "simple"}, "steps": [],
            "ensembles": [SimpleAudioOperationTests.operation("invert", ["input"], save="Default")],
        }
        actual, samples, inputs, result = self.run_workflow(definition)
        self.np.testing.assert_array_equal(actual, -samples)
        self.assertEqual(inputs, [])
        self.assertEqual(result["outputs"][0]["stem"], "Audio")
        self.assertEqual(result["outputs"][0]["name"], "song_Audio_process.wav")

    def test_native_invert_can_feed_a_separation(self) -> None:
        definition = {
            "version": 1, "studio": {"editor": "simple"},
            "steps": [{"id": "cleanup", "model": "fixture", "input": "process.Audio", "stems": ["Dry"], "save": {"Dry": "Default"}}],
            "ensembles": [SimpleAudioOperationTests.operation("invert", ["input"])],
        }
        actual, samples, inputs, result = self.run_workflow(definition)
        self.np.testing.assert_array_equal(inputs[0], -samples)
        self.np.testing.assert_allclose(actual, -samples * 0.25, atol=1e-6)
        self.assertEqual(result["outputs"][0]["stem"], "Dry")

    def test_native_saves_keep_the_source_sample_rate_instead_of_forcing_44100(self) -> None:
        step = {"id": "split", "model": "fixture", "stems": ["Vocals"]}
        for name, definition, gain in (
            ("step save", {
                "version": 1, "studio": {"editor": "simple"},
                "steps": [{**step, "save": {"Vocals": "Default"}}],
            }, 0.25),
            ("ensemble save", {
                "version": 1, "studio": {"editor": "simple"}, "steps": [step],
                "ensembles": [SimpleAudioOperationTests.operation(
                    "avg_wave", ["split.Vocals", "input"], id="blend", save="Default",
                )],
            }, 0.625),
        ):
            with self.subTest(name):
                actual, samples, _inputs, result = self.run_workflow(
                    definition, source_rate=48000, factors=[0.25],
                )
                self.np.testing.assert_allclose(actual, samples * gain, atol=1e-6)
                self.assertEqual(result["outputs"][0]["sampleRate"], 48000)

    def test_native_multi_save_chain_keeps_audio_stems_and_filenames_associated(self) -> None:
        for studio in (True, False):
            with self.subTest(studio=studio):
                definition = {
                    "version": 1,
                    **({"studio": {"editor": "simple"}} if studio else {}),
                    "steps": [{
                        "id": "cleanup", "model": "fixture", "input": "second.Audio",
                        "stems": ["Dry"], "save": {"Dry": "Default"},
                    }],
                    "ensembles": [
                        SimpleAudioOperationTests.operation("invert", ["input"], id="first", save="Default"),
                        SimpleAudioOperationTests.operation("invert", ["first.Audio"], id="second"),
                    ],
                }
                _actual, samples, _inputs, result = self.run_workflow(definition, expected_file_count=2)
                expected = {"Dry": samples * 0.25, "Audio": -samples}
                self.assertEqual({item["stem"] for item in result["outputs"]}, set(expected))
                for item in result["outputs"]:
                    audio, sample_rate = self.load_audio(item["path"], sr=None, mono=False)
                    self.np.testing.assert_allclose(
                        self.np.asarray(audio).reshape(samples.shape), expected[item["stem"]], atol=1e-6,
                    )
                    if studio:
                        self.assertIn(item["stem"], item["name"])
                    self.assertEqual(Path(item["path"]).name, item["name"])
                    self.assertEqual(item["sampleRate"], sample_rate)

    def test_native_vr_zero_values_reach_the_separator_without_changing_other_params(self) -> None:
        params = {
            "batch_size": 3, "window_size": 1024, "aggression": 0,
            "enable_post_process": True, "post_process_threshold": 0,
            "high_end_process": True, "normalize": False, "enable_tta": True,
        }
        captured = []
        definition = {
            "version": 1, "studio": {"editor": "simple"},
            "steps": [{
                "id": "vr", "model": "fixture", "model_type": "vr", "input": "input",
                "stems": ["Vocals"], "save": {"Vocals": "Default"}, "inference_params": params,
            }],
        }
        self.run_workflow(definition, separation_params=captured)
        self.assertEqual(len(captured), 1)
        for key, expected in params.items():
            self.assertEqual(captured[0][key], expected, key)

    def test_native_vr_default_zero_can_be_overridden_by_a_nonzero_step_value(self) -> None:
        captured = []
        definition = {
            "version": 1,
            "defaults": {"inference_params": {"aggression": 0, "post_process_threshold": 0}},
            "steps": [{
                "id": "vr", "model": "fixture", "model_type": "vr", "stems": ["Vocals"],
                "save": {"Vocals": "Default"}, "inference_params": {"aggression": 7},
            }],
        }
        self.run_workflow(definition, separation_params=captured)
        self.assertEqual(captured[0]["aggression"], 7)
        self.assertEqual(captured[0]["post_process_threshold"], 0)

    def test_native_mss_zero_sizes_use_model_defaults_and_default_strings_still_run(self) -> None:
        for sizes in ({"overlap_size": 0, "chunk_size": 0},
                      {"overlap_size": "Default", "chunk_size": "Default"},
                      {"overlap_size": None, "chunk_size": None}):
            with self.subTest(sizes=sizes):
                captured = []
                definition = {
                    "version": 1,
                    "steps": [{"id": "split", "model": "fixture", "stems": ["Vocals"],
                               "save": {"Vocals": "Default"}, "inference_params": sizes}],
                }
                self.run_workflow(definition, separation_params=captured)
                self.assertNotIn("overlap_size", captured[0])
                self.assertNotIn("chunk_size", captured[0])

    def test_native_nullable_vr_numbers_keep_sdk_defaults(self) -> None:
        captured = []
        definition = {
            "version": 1,
            "steps": [{
                "id": "vr", "model": "fixture", "model_type": "vr", "stems": ["Vocals"],
                "save": {"Vocals": "Default"},
                "inference_params": dict.fromkeys([
                    "batch_size", "window_size", "aggression", "post_process_threshold",
                ]),
            }],
        }
        self.run_workflow(definition, separation_params=captured)
        self.assertEqual(captured[0]["batch_size"], 1)
        self.assertEqual(captured[0]["window_size"], 512)
        self.assertEqual(captured[0]["aggression"], 5)
        self.assertEqual(captured[0]["post_process_threshold"], 0.2)

    def test_invalid_simple_numbers_fail_before_the_separator_is_called(self) -> None:
        invalid = [
            {"batch_size": 1.5}, {"window_size": 0}, {"aggression": -1}, {"aggression": 1.5},
            {"chunk_size": 44100.5}, {"overlap_size": -1}, {"batch_size": float("nan")},
            {"chunk_size": float("inf")}, {"post_process_threshold": 2}, {"normalize": "false"},
        ]
        for params in invalid:
            with self.subTest(params=params):
                captured = []
                definition = {"version": 1, "steps": [{
                    "id": "vr", "model": "fixture", "model_type": "vr", "stems": ["Vocals"],
                    "save": {"Vocals": "Default"}, "inference_params": params,
                }]}
                with self.assertRaisesRegex(RuntimeError, "Step vr"):
                    self.run_workflow(definition, separation_params=captured)
                self.assertEqual(captured, [])


class LegacyWorkflowInputTests(unittest.TestCase):
    def test_legacy_placeholder_is_bound_to_global_input_without_mutating_definition(self) -> None:
        definition = {
            "nodes": [
                {"id": 1, "type": "pymss_load_audio", "widgets_values": ["input.wav", ""]},
                {"id": 2, "type": "pymss_load_audio", "widgets_values": ["", None]},
            ],
        }
        payload = {"workflow": definition}

        transient, inputs = _prepare_legacy_global_input(payload, "D:/Audio/song.wav", None)

        self.assertEqual(inputs, {"input.wav": "D:/Audio/song.wav"})
        self.assertEqual(definition["nodes"][1]["widgets_values"], ["", None])
        self.assertEqual(transient["workflow"]["nodes"][0]["widgets_values"], ["D:/Audio/song.wav", ""])
        self.assertEqual(transient["workflow"]["nodes"][1]["widgets_values"], ["D:/Audio/song.wav", None])

    def test_embedded_audio_paths_are_overridden_by_the_global_input(self) -> None:
        payload = {
            "workflow": {
                "nodes": [{"id": 1, "type": "pymss_load_audio", "widgets_values": ["D:/old/song.wav", None]}],
            },
        }

        transient, inputs = _prepare_legacy_global_input(payload, "D:/Audio/new.wav", None)

        self.assertEqual(inputs, {"D:/old/song.wav": "D:/Audio/new.wav"})
        self.assertEqual(
            transient["workflow"]["nodes"][0]["widgets_values"],
            ["D:/Audio/new.wav", None],
        )

    def test_named_slots_are_bound_to_the_global_file_after_ui_rollback(self) -> None:
        payload = {
            "workflow": {
                "nodes": [{"id": 1, "type": "pymss_load_audio", "widgets_values": ["input.wav", "lead"]}],
            },
        }

        _transient, inputs = _prepare_legacy_global_input(payload, "D:/Audio/song.wav", {"lead": "old.wav"})

        self.assertEqual(inputs, {"lead": "D:/Audio/song.wav"})

    def test_legacy_batch_nodes_get_a_transient_global_input_slot(self) -> None:
        payload = {
            "workflow": {
                "nodes": [{"id": 1, "type": "pymss_load_audio_batch", "widgets_values": ["old-folder", False, True]}],
            },
        }

        transient, inputs = _prepare_legacy_global_input(payload, "D:/Audio/song.wav", None)

        self.assertEqual(inputs, {"__pymss_studio_global_input__": "D:/Audio/song.wav"})
        self.assertEqual(
            transient["workflow"]["nodes"][0]["widgets_values"],
            ["old-folder", False, True, "__pymss_studio_global_input__"],
        )

    def test_yaml_workflows_keep_the_original_payload_and_inputs(self) -> None:
        payload = {"workflow": {"steps": [{"id": "one", "input": "input"}]}}

        transient, inputs = _prepare_legacy_global_input(payload, "D:/Audio/song.wav", None)

        self.assertIs(transient, payload)
        self.assertEqual(inputs, {})


class WorkflowOutputMetadataTests(unittest.TestCase):
    def test_simple_filename_metadata_is_detected_and_runtime_copy_is_flat(self) -> None:
        definition = {
            "version": 1,
            "defaults": {"output_format": "flac"},
            "steps": [{
                "id": "split",
                "save": {"vocals": "vocals"},
                "output_names": {"vocals": "lead"},
            }],
        }
        self.assertTrue(_simple_output_names(definition))
        runtime = _prepare_simple_runtime_definition(definition)
        self.assertEqual(runtime["steps"][0]["save"], {"vocals": "Default"})
        self.assertEqual(runtime["steps"][0]["output_format"], "flac")
        self.assertEqual(definition["steps"][0]["save"]["vocals"], "vocals")

    def test_empty_filename_metadata_does_not_change_directory_outputs(self) -> None:
        definition = {
            "version": 1,
            "steps": [{"id": "split", "save": {"vocals": "vocals"}, "output_names": {}}],
        }
        self.assertFalse(_simple_output_names(definition))
        self.assertIs(_prepare_simple_runtime_definition(definition), definition)

    def test_studio_simple_workflow_keeps_default_filename_behavior_when_template_is_empty(self) -> None:
        definition = {
            "version": 1,
            "defaults": {"output_format": "flac"},
            "studio": {"editor": "simple", "viewport": {"x": 0, "y": 0, "zoom": 1}, "nodes": {}},
            "steps": [{
                "id": "split",
                "save": {"Vocals": "Default"},
                "output_names": {},
            }],
        }

        self.assertTrue(_simple_output_names(definition))
        runtime = _prepare_simple_runtime_definition(definition)
        self.assertNotIn("studio", runtime)
        self.assertEqual(runtime["steps"][0]["save"], {"Vocals": "Default"})
        self.assertEqual(runtime["steps"][0]["output_names"], {})
        self.assertEqual(runtime["steps"][0]["output_format"], "flac")

    def test_editor_layout_metadata_is_removed_from_runtime_definition(self) -> None:
        definition = {
            "version": 1,
            "studio": {"editor": "simple", "viewport": {"x": 0, "y": 0, "zoom": 1}},
            "steps": [{"id": "split", "save": {"vocals": "vocals"}}],
        }
        runtime = _prepare_simple_runtime_definition(definition)
        self.assertNotIn("studio", runtime)
        self.assertIn("studio", definition)
        self.assertIsNot(runtime, definition)

    def test_ensemble_metadata_is_removed_before_pymss_yaml_parsing(self) -> None:
        definition = {
            "version": 1,
            "steps": [
                {"id": "split", "input": "input", "save": {}},
                {"id": "cleanup", "input": "blend.Vocals", "save": {}},
            ],
            "ensembles": [{
                "id": "blend",
                "inputs": [{"source": "split.vocals", "weight": 1}, {"source": "split.music", "weight": 1}],
                "algorithm": "avg_wave",
                "output_stem": "Vocals",
                "save": "Default",
            }],
        }
        runtime = _prepare_simple_runtime_definition(definition)
        self.assertNotIn("ensembles", runtime)
        self.assertEqual(runtime["steps"][1]["input"], "input")
        self.assertEqual(definition["steps"][1]["input"], "blend.Vocals")
        self.assertIn("ensembles", definition)
        self.assertIsNot(runtime, definition)

    def test_simple_ensemble_records_compile_to_graph_nodes_and_output_metadata(self) -> None:
        class DAGLink:
            def __init__(self, **values):
                self.__dict__.update(values)

        class DAGNode:
            def __init__(self, *, id, type, inputs, data, title=""):
                self.id = id
                self.type = type
                self.inputs = inputs
                self.data = data
                self.title = title

        graph_module = ModuleType("pymss.graph")
        graph_module.DAGLink = DAGLink
        graph_module.DAGNode = DAGNode
        graph_module.AUDIO = "AUDIO"
        graph_module.STRING = "STRING"
        pymss_module = ModuleType("pymss")
        pymss_module.graph = graph_module
        dag = SimpleNamespace(nodes=[
            DAGNode(id="input", type="input_audio", inputs=[], data={}),
            DAGNode(id="step:modelA", type="mss_separate", inputs=[], data={}),
            DAGNode(id="step:modelB", type="mss_separate", inputs=[], data={}),
            DAGNode(id="step:cleanup", type="mss_separate", inputs=[DAGLink(
                link_id=7,
                source_node_id="input",
                source_slot=0,
                target_node_id="step:cleanup",
                target_slot=0,
                type="AUDIO",
            )], data={}),
            DAGNode(id="save:modelA:Vocals", type="pymss_save_audio", inputs=[None, None], data={}),
        ])
        definition = {
            "steps": [
                {"id": "modelA", "model": "a.ckpt", "stems": ["Vocals"], "save": {"Vocals": "Default"}},
                {"id": "modelB", "stems": ["Drums", "Vocals"]},
                {"id": "cleanup", "input": "blend.Vocals", "stems": ["Voice"]},
            ],
            "ensembles": [{
                "id": "blend",
                "inputs": [
                    {"source": "input", "weight": 1},
                    {"source": "modelB.Vocals", "weight": 0.75},
                ],
                "algorithm": "avg_fft",
                "output_stem": "Vocals",
                "save": "Default",
                "output_name": "%stem%",
            }],
        }

        with tempfile.TemporaryDirectory() as directory:
            reserved_names = set()
            with patch.dict("sys.modules", {"pymss": pymss_module, "pymss.graph": graph_module}):
                step_metadata = _apply_simple_output_names(
                    dag,
                    definition,
                    input_path="D:/Audio/song.wav",
                    output_format="flac",
                    output_dir=Path(directory),
                    reserved_names=reserved_names,
                    apply_names=False,
                )
                ensemble_metadata = _apply_simple_ensembles(
                    dag,
                    definition,
                    input_path="D:/Audio/song.wav",
                    output_format="flac",
                    output_dir=Path(directory),
                    reserved_names=reserved_names,
                    start_index=len(step_metadata),
                )

        ensemble = next(node for node in dag.nodes if node.type == "pymss_audio_ensemble")
        save = next(node for node in dag.nodes if node.id == "studio:ensemble-save:blend")
        cleanup = next(node for node in dag.nodes if node.id == "step:cleanup")
        filename = next(node for node in dag.nodes if node.type == "StringConstant")
        self.assertEqual(ensemble.data["widgets_values"], [2, "avg_fft", 1.0, 0.75])
        self.assertEqual(
            [(link.source_node_id, link.source_slot, link.target_slot) for link in ensemble.inputs],
            [("input", 0, 0), ("step:modelB", 2, 1)],
        )
        self.assertEqual(save.inputs[0].source_node_id, ensemble.id)
        self.assertEqual(save.data["widgets_values"], ["flac", "Default", "0", "FLOAT", "PCM_24", "320k"])
        self.assertEqual(cleanup.inputs[0].source_node_id, ensemble.id)
        self.assertEqual(cleanup.inputs[0].source_slot, 0)
        self.assertEqual(cleanup.inputs[0].target_slot, 0)
        self.assertEqual(save.inputs[1].source_node_id, filename.id)
        self.assertEqual(filename.data["widgets_values"], ["Vocals_2"])
        self.assertEqual(step_metadata, [{"node_id": "save:modelA:Vocals", "stem": "Vocals", "filename": ""}])
        self.assertEqual(ensemble_metadata, [{"node_id": "studio:ensemble-save:blend", "stem": "Vocals", "filename": "Vocals_2.flac"}])

    def test_unsaved_simple_ensemble_can_feed_a_downstream_step(self) -> None:
        class DAGLink:
            def __init__(self, **values):
                self.__dict__.update(values)

        class DAGNode:
            def __init__(self, *, id, type, inputs, data, title=""):
                self.id = id
                self.type = type
                self.inputs = inputs
                self.data = data
                self.title = title

        graph_module = ModuleType("pymss.graph")
        graph_module.DAGLink = DAGLink
        graph_module.DAGNode = DAGNode
        graph_module.AUDIO = "AUDIO"
        graph_module.STRING = "STRING"
        pymss_module = ModuleType("pymss")
        pymss_module.graph = graph_module
        dag = SimpleNamespace(nodes=[
            DAGNode(id="input", type="input_audio", inputs=[], data={}),
            DAGNode(id="step:first", type="mss_separate", inputs=[], data={}),
            DAGNode(id="step:second", type="mss_separate", inputs=[], data={}),
            DAGNode(id="step:cleanup", type="mss_separate", inputs=[DAGLink(
                link_id=3,
                source_node_id="input",
                source_slot=0,
                target_node_id="step:cleanup",
                target_slot=0,
                type="AUDIO",
            )], data={}),
        ])
        definition = {
            "steps": [
                {"id": "first", "stems": ["Vocals"]},
                {"id": "second", "stems": ["Vocals"]},
                {"id": "cleanup", "input": "blend.Vocals", "stems": ["Voice"]},
            ],
            "ensembles": [{
                "id": "blend",
                "inputs": [
                    {"source": "first.Vocals", "weight": 1},
                    {"source": "second.Vocals", "weight": 0.5},
                ],
                "algorithm": "avg_wave",
                "output_stem": "Vocals",
                "save": False,
            }],
        }

        with patch.dict("sys.modules", {"pymss": pymss_module, "pymss.graph": graph_module}):
            metadata = _apply_simple_ensembles(
                dag,
                definition,
                input_path="D:/Audio/song.wav",
                output_format="wav",
            )

        ensemble = next(node for node in dag.nodes if node.id == "studio:ensemble:blend")
        cleanup = next(node for node in dag.nodes if node.id == "step:cleanup")
        self.assertEqual(metadata, [])
        self.assertFalse(any(node.id == "studio:ensemble-save:blend" for node in dag.nodes))
        self.assertEqual(cleanup.inputs[0].source_node_id, ensemble.id)
        link_ids = [
            link.link_id
            for node in dag.nodes
            for link in node.inputs
            if link is not None
        ]
        self.assertEqual(len(link_ids), len(set(link_ids)))

    def test_simple_ensemble_can_feed_a_later_ensemble(self) -> None:
        class DAGLink:
            def __init__(self, **values):
                self.__dict__.update(values)

        class DAGNode:
            def __init__(self, *, id, type, inputs, data, title=""):
                self.id = id
                self.type = type
                self.inputs = inputs
                self.data = data
                self.title = title

        graph_module = ModuleType("pymss.graph")
        graph_module.DAGLink = DAGLink
        graph_module.DAGNode = DAGNode
        graph_module.AUDIO = "AUDIO"
        graph_module.STRING = "STRING"
        pymss_module = ModuleType("pymss")
        pymss_module.graph = graph_module
        dag = SimpleNamespace(nodes=[
            DAGNode(id="input", type="input_audio", inputs=[], data={}),
            DAGNode(id="step:first", type="mss_separate", inputs=[], data={}),
            DAGNode(id="step:second", type="mss_separate", inputs=[], data={}),
        ])
        definition = {
            "steps": [
                {"id": "first", "stems": ["Vocals"]},
                {"id": "second", "stems": ["Vocals"]},
            ],
            "ensembles": [
                {
                    "id": "blend",
                    "inputs": [
                        {"source": "first.Vocals", "weight": 1},
                        {"source": "second.Vocals", "weight": 1},
                    ],
                    "algorithm": "avg_wave",
                    "output_stem": "Vocals",
                    "save": False,
                },
                {
                    "id": "polish",
                    "inputs": [
                        {"source": "blend.Vocals", "weight": 1},
                        {"source": "input", "weight": 0.25},
                    ],
                    "algorithm": "avg_fft",
                    "output_stem": "Final",
                    "save": False,
                },
            ],
        }

        with patch.dict("sys.modules", {"pymss": pymss_module, "pymss.graph": graph_module}):
            metadata = _apply_simple_ensembles(
                dag,
                definition,
                input_path="D:/Audio/song.wav",
                output_format="wav",
            )

        blend = next(node for node in dag.nodes if node.id == "studio:ensemble:blend")
        polish = next(node for node in dag.nodes if node.id == "studio:ensemble:polish")
        self.assertEqual(metadata, [])
        self.assertEqual(polish.inputs[0].source_node_id, blend.id)
        self.assertEqual(polish.inputs[0].source_slot, 0)
        self.assertEqual(polish.inputs[1].source_node_id, "input")

    def test_simple_ensemble_rejects_non_finite_weights_in_worker(self) -> None:
        class DAGLink:
            def __init__(self, **values):
                self.__dict__.update(values)

        class DAGNode:
            def __init__(self, *, id, type, inputs, data, title=""):
                self.id = id
                self.type = type
                self.inputs = inputs
                self.data = data
                self.title = title

        graph_module = ModuleType("pymss.graph")
        graph_module.DAGLink = DAGLink
        graph_module.DAGNode = DAGNode
        graph_module.AUDIO = "AUDIO"
        graph_module.STRING = "STRING"
        pymss_module = ModuleType("pymss")
        pymss_module.graph = graph_module

        for weight in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(weight=weight):
                dag = SimpleNamespace(nodes=[
                    DAGNode(id="input", type="input_audio", inputs=[], data={}),
                    DAGNode(id="step:model", type="mss_separate", inputs=[], data={}),
                ])
                definition = {
                    "steps": [{"id": "model", "stems": ["Vocals"]}],
                    "ensembles": [{
                        "id": "blend",
                        "inputs": [
                            {"source": "input", "weight": 1},
                            {"source": "model.Vocals", "weight": weight},
                        ],
                        "algorithm": "avg_wave",
                        "output_stem": "Vocals",
                        "save": False,
                    }],
                }
                with patch.dict("sys.modules", {"pymss": pymss_module, "pymss.graph": graph_module}):
                    with self.assertRaisesRegex(RuntimeError, "finite and greater than zero"):
                        _apply_simple_ensembles(
                            dag,
                            definition,
                            input_path="D:/Audio/song.wav",
                            output_format="wav",
                        )

    def test_intermediate_outputs_follow_explicit_save_links(self) -> None:
        definition = {
            "version": 1,
            "save_intermediate": False,
            "steps": [
                {"id": "first", "input": "input", "save": {"vocals": "Default", "music": "Default"}},
                {"id": "second", "input": "first.vocals", "save": {"clean": "Default"}},
            ],
        }
        runtime = _prepare_simple_runtime_definition(definition)
        self.assertEqual(runtime["steps"][0]["save"], {"vocals": "Default", "music": "Default"})
        self.assertNotIn("save_intermediate", runtime)
        self.assertEqual(definition["steps"][0]["save"]["vocals"], "Default")

    def test_simple_filename_template_renders_tokens_and_extension(self) -> None:
        self.assertEqual(
            _render_simple_filename(
                "%filename%_%stem%_%model%.wav",
                input_path="D:/Audio/song.mp3",
                stem="vocals",
                model="model.pth",
                step_id="split",
                index=1,
                output_format="flac",
            ),
            "song_vocals_model.flac",
        )
        self.assertEqual(
            _render_simple_filename(
                "%filename%_%stem%_%model%",
                input_path="D:/Audio/小蓝背心 - 灯火通明.mp3",
                stem="Instrumental",
                model="melband_roformer_instvox_duality_v2.ckpt",
                step_id="step1",
                index=1,
                output_format="wav",
            ),
            "小蓝背心 - 灯火通明_Instrumental_melband_roformer_instvox_duality_v2.wav",
        )

    def test_simple_output_paths_restore_unicode_names_after_graph_sanitizing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            generated = output_dir / "pymss_studio_0001.wav"
            generated.write_bytes(b"audio")
            finalized = _finalize_simple_output_paths(
                [str(generated)],
                [{"stem": "Instrumental", "filename": "小蓝背心 - 灯火通明_Instrumental_model.wav"}],
                output_dir,
            )
            self.assertEqual(finalized, [str(output_dir / "小蓝背心 - 灯火通明_Instrumental_model.wav")])
            self.assertTrue(Path(finalized[0]).is_file())
            self.assertFalse(generated.exists())

    def test_simple_output_publish_atomically_avoids_concurrent_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "results"
            first_root = root / "task-a"
            second_root = root / "task-b"
            first_root.mkdir()
            second_root.mkdir()
            first = first_root / "temporary.wav"
            second = second_root / "temporary.wav"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            barrier = Barrier(2)

            def publish(source: Path, source_root: Path) -> str:
                barrier.wait()
                return _finalize_simple_output_paths(
                    [str(source)],
                    [{"stem": "Vocals", "filename": "song_Vocals.wav"}],
                    output_dir,
                    source_root=source_root,
                )[0]

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(publish, first, first_root),
                    executor.submit(publish, second, second_root),
                ]
                published = [Path(future.result()) for future in futures]

            self.assertEqual({path.name for path in published}, {"song_Vocals.wav", "song_Vocals_2.wav"})
            self.assertEqual({path.read_bytes() for path in published}, {b"first", b"second"})
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())

    def test_simple_output_publish_preserves_legacy_save_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "task"
            source = source_root / "stems" / "Vocals.wav"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"audio")
            output_dir = root / "results"

            finalized = _finalize_simple_output_paths(
                [str(source)],
                [],
                output_dir,
                source_root=source_root,
            )

            self.assertEqual(finalized, [str(output_dir / "stems" / "Vocals.wav")])
            self.assertEqual(Path(finalized[0]).read_bytes(), b"audio")
            self.assertFalse(source.exists())

    def test_simple_output_publish_uses_exclusive_copy_without_hard_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "task"
            source_root.mkdir()
            source = source_root / "temporary.wav"
            source.write_bytes(b"audio")
            output_dir = root / "results"
            output_dir.mkdir()
            (output_dir / "song.wav").write_bytes(b"existing")

            with patch.object(worker_workflows.os, "link", side_effect=OSError("unsupported")):
                finalized = _finalize_simple_output_paths(
                    [str(source)],
                    [{"stem": "Vocals", "filename": "song.wav"}],
                    output_dir,
                    source_root=source_root,
                )

            self.assertEqual(finalized, [str(output_dir / "song_2.wav")])
            self.assertEqual((output_dir / "song.wav").read_bytes(), b"existing")
            self.assertEqual((output_dir / "song_2.wav").read_bytes(), b"audio")
            self.assertFalse(source.exists())

    def test_output_stem_matches_single_separation_for_prefixed_filename(self) -> None:
        self.assertEqual(
            _workflow_output_stem("D:/results/song/song_vocals.wav", "D:/Audio/song.wav"),
            "vocals",
        )

    def test_output_stem_keeps_unprefixed_filename(self) -> None:
        self.assertEqual(
            _workflow_output_stem("D:/results/vocals.wav", "D:/Audio/song.wav"),
            "vocals",
        )

    def test_output_stem_handles_windows_separators(self) -> None:
        self.assertEqual(
            _workflow_output_stem(r"D:\\results\\song\\song_vocals.wav", r"D:\\Audio\\song.wav"),
            "vocals",
        )

    def test_output_stem_supports_graphs_without_primary_input(self) -> None:
        self.assertEqual(_workflow_output_stem("D:/results/custom_mix.wav"), "custom_mix")


if __name__ == "__main__":
    unittest.main()
