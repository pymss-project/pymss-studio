from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import worker_workflows


class EnsembleOutputTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workflow_file = self.root / "workflow.json"
        self.workflow_file.write_text("{}", encoding="utf-8")
        self.payload = {
            "workflow": {"nodes": [], "extra": {"studioEnsemble": {"outputStem": "Vocals"}}},
            "outputFormat": "wav",
            "outputNaming": {"enabled": True, "template": "%index%*%filename%*%stem%"},
        }
        self.graph = types.ModuleType("pymss.graph")
        self.graph.load_comfy_file = mock.Mock(return_value=object())
        self.graph.run_dag = mock.Mock(side_effect=self.save_audio)
        package = types.ModuleType("pymss")
        package.graph = self.graph
        self.enterContext(mock.patch.dict(sys.modules, {"pymss": package, "pymss.graph": self.graph}))
        self.enterContext(mock.patch.object(worker_workflows, "_write_workflow_file", return_value=(self.workflow_file, "comfy")))

    def save_audio(self, _dag, **kwargs):
        path = Path(kwargs["output_dir"]) / f"audio.{kwargs['output_format']}"
        path.write_bytes(b"encoded")

        class Saved(list):
            pass

        saved = Saved([str(path)])
        saved.records = [types.SimpleNamespace(path=str(path), stem="", sample_rate=48000)]
        return saved

    def run_workflow(self, *, input_name="歌曲A.mp3", layout="flat"):
        return worker_workflows._run_pymss(
            self.payload, "ensemble", input_path=str(self.root / input_name), inputs=None,
            output_dir=str(self.root / "results"), output_layout=layout,
        )

    def test_template_and_logical_stem_are_used_for_all_formats(self):
        for fmt in ("wav", "flac", "mp3", "m4a"):
            with self.subTest(format=fmt):
                self.payload["outputFormat"] = fmt
                result = self.run_workflow()
                target = self.root / "results" / f"01_歌曲A_Vocals.{fmt}"
                self.assertEqual(result["files"], [str(target)])
                self.assertEqual(result["outputs"], [{"stem": "Vocals", "path": str(target), "name": target.name, "sampleRate": 48000}])
                self.assertEqual(target.read_bytes(), b"encoded")
                self.assertFalse((target.parent / f"audio.{fmt}").exists())

    def test_default_naming_and_folder_layout(self):
        self.payload["outputNaming"] = {"enabled": False, "template": "%model%"}
        result = self.run_workflow(layout="folders")
        target = self.root / "results" / "歌曲A" / "歌曲A_Vocals.wav"
        self.assertEqual(result["files"], [str(target)])
        self.assertEqual(result["outputDir"], str(target.parent.resolve()))
        self.assertFalse(list(target.parent.glob(".pymss-ensemble-*")))

    def test_repeated_and_flat_batch_outputs_preserve_existing_files(self):
        output = self.root / "results"
        output.mkdir()
        original = output / "01_歌曲A_Vocals.wav"
        original.write_bytes(b"original")
        legacy = output / "audio.wav"
        legacy.write_bytes(b"legacy")
        first = self.run_workflow()
        second = self.run_workflow()
        third = self.run_workflow(input_name="歌曲B.mp3")
        self.assertEqual(Path(first["files"][0]).name, "01_歌曲A_Vocals_2.wav")
        self.assertEqual(Path(second["files"][0]).name, "01_歌曲A_Vocals_3.wav")
        self.assertEqual(Path(third["files"][0]).name, "01_歌曲B_Vocals.wav")
        self.assertEqual(original.read_bytes(), b"original")
        self.assertEqual(legacy.read_bytes(), b"legacy")

    def test_batch_forwards_input_number_and_keeps_single_output_index(self):
        self.payload["outputNaming"]["template"] = "%input_number%_%index%_%filename%_%model%_%stem%"
        payload = {**self.payload, "output": str(self.root / "results"), "outputLayout": "flat", "taskId": "batch"}
        tasks = [{"taskId": f"task-{index}", "input": str(self.root / f"song{index}.mp3"), "inputIndex": index}
                 for index in (1, 2)]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = worker_workflows.cmd_infer_workflow({**payload, "tasks": tasks})
        self.assertEqual(code, 0)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        done = [event for event in events if event["type"] == "task_done"]
        self.assertEqual([Path(event["payload"]["files"][0]).name for event in done],
                         ["01_01_song1_Ensemble_Vocals.wav", "02_01_song2_Ensemble_Vocals.wav"])

    def test_publication_after_999_existing_names_preserves_every_previous_output(self):
        output = self.root / "results"
        output.mkdir()
        existing = [output / ("01_歌曲A_Vocals.wav" if index == 1 else f"01_歌曲A_Vocals_{index}.wav")
                    for index in range(1, 1000)]
        for path in existing:
            path.write_bytes(b"original")

        result = self.run_workflow()

        target = Path(result["files"][0])
        self.assertNotIn(target, existing)
        self.assertEqual(target.parent, output)
        self.assertEqual(target.suffix, ".wav")
        self.assertEqual(target.read_bytes(), b"encoded")
        self.assertEqual(result["outputs"][0]["stem"], "Vocals")
        self.assertTrue(all(path.read_bytes() == b"original" for path in existing))
        self.assertFalse(list(output.glob(".pymss-ensemble-*")))

    def test_concurrent_runs_claim_distinct_final_paths(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.run_workflow(), range(4)))
        paths = [Path(result["files"][0]) for result in results]
        self.assertEqual(len(set(paths)), 4)
        self.assertTrue(all(path.read_bytes() == b"encoded" for path in paths))
        self.assertFalse(list((self.root / "results").glob(".pymss-ensemble-*")))

    def test_invalid_stem_is_rejected_before_running_the_graph(self):
        self.payload["workflow"]["extra"]["studioEnsemble"]["outputStem"] = " "
        with self.assertRaisesRegex(RuntimeError, "stem"):
            self.run_workflow()
        self.graph.run_dag.assert_not_called()

    def test_failure_cleans_only_temporary_outputs(self):
        output = self.root / "results"
        output.mkdir()
        original = output / "audio.wav"
        original.write_bytes(b"keep")

        def fail(_dag, **kwargs):
            (Path(kwargs["output_dir"]) / "partial.wav").write_bytes(b"partial")
            raise RuntimeError("encoder failed")

        self.graph.run_dag.side_effect = fail
        with self.assertRaisesRegex(RuntimeError, "encoder failed"):
            self.run_workflow()
        self.assertEqual(list(output.iterdir()), [original])
        self.assertEqual(original.read_bytes(), b"keep")

    def test_publication_failure_releases_reserved_filename(self):
        with mock.patch.object(Path, "replace", side_effect=PermissionError("file locked")):
            with self.assertRaises(PermissionError):
                self.run_workflow()
        self.assertEqual(list((self.root / "results").iterdir()), [])

    def test_missing_output_is_not_reported_as_success(self):
        self.graph.run_dag.side_effect = lambda _dag, **kwargs: [str(Path(kwargs["output_dir"]) / "missing.wav")]
        with self.assertRaisesRegex(RuntimeError, "output"):
            self.run_workflow()
        self.assertEqual(list((self.root / "results").iterdir()), [])

    def test_external_output_is_never_moved_or_deleted(self):
        external = self.root / "keep.wav"
        external.write_bytes(b"keep")
        self.graph.run_dag.side_effect = None
        self.graph.run_dag.return_value = [str(external)]
        with self.assertRaisesRegex(RuntimeError, "outside"):
            self.run_workflow()
        self.assertEqual(external.read_bytes(), b"keep")
        self.assertEqual(list((self.root / "results").iterdir()), [])

    def test_plain_graph_keeps_its_own_naming_even_with_a_studio_template(self):
        self.payload["workflow"] = {"nodes": []}
        result = self.run_workflow()
        target = self.root / "results" / "audio.wav"
        self.assertEqual(result["files"], [str(target)])
        self.assertEqual(self.graph.run_dag.call_args.kwargs["output_dir"], target.parent)

    def test_reserved_and_path_characters_remain_inside_the_output_directory(self):
        self.payload["workflow"]["extra"]["studioEnsemble"]["outputStem"] = "CON"
        self.payload["outputNaming"]["template"] = "%stem%"
        result = self.run_workflow()
        self.assertEqual(Path(result["files"][0]).name, "CON_.wav")
        self.payload["outputNaming"]["template"] = "../../%stem%"
        result = self.run_workflow()
        self.assertEqual(Path(result["files"][0]).parent, self.root / "results")


if __name__ == "__main__":
    unittest.main()
