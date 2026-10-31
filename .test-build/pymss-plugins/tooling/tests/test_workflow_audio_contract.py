from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Lock
from types import SimpleNamespace
import os
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from pymss.graph import AudioArtifact, DAG, DAGLink, DAGNode, get_node_type, run_dag
from pymss.operations import create_runtime


ROOT = Path(__file__).resolve().parents[2]


class WorkflowAudioContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Resolve a public node first so core owns builtin initialization.
        get_node_type("input_audio")
        from pymss.graph import nodes

        cls.graph_nodes = nodes
        cls.runtime = create_runtime()
        reports = cls.runtime.load_plugins([ROOT / "plugins"])
        if not all(row["loaded"] for row in reports):
            raise AssertionError(reports)

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        environment = patch.dict(os.environ, {"PYMSS_SCRATCH_DIR": str(self.scratch)})
        environment.start()
        self.addCleanup(environment.stop)
        self.source = self.root / "input.wav"
        sf.write(self.source, np.full((1600, 2), 0.25, dtype=np.float32), 16000, subtype="FLOAT")

    def graph(self, intermediate="pymss_audio_invert_phase", terminal="AsrTranscribe"):
        processing = DAGNode(2, intermediate, inputs=[DAGLink(1, 1, 0, 2, 0, "AUDIO")])
        if intermediate == "DynamicsLimiter":
            processing.data["widgets_values"] = [-18.0, 100.0]
        if intermediate == "mss_separate":
            processing.data.update(
                widgets_values=["placeholder", "cpu"],
                outputs=[{"name": "vocals", "type": "AUDIO"}],
            )
        return DAG(nodes=[
            DAGNode(1, "pymss_load_audio", data={"widgets_values": [str(self.source)]}),
            processing,
            DAGNode(3, terminal, inputs=[DAGLink(2, 2, 0, 3, 0, "AUDIO")],
                    data={"widgets_values": [120, "en", "small"] if terminal == "GameVocalToMidi" else []}),
        ])

    def asr_backend(self, backend):
        handler = self.runtime._operations["pymss.asr.transcribe"].run
        return patch.dict(handler.__globals__, {
            "_is_apple_silicon": lambda: False,
            "_have_funasr": lambda: True,
            "_transcribe_funasr": backend,
        })

    def test_asr_receives_phase_inverted_samples_instead_of_source_file(self):
        paths = []

        def recognize(path, model, language):
            paths.append(Path(path))
            audio, rate = sf.read(path, always_2d=True)
            self.assertEqual(rate, 16000)
            np.testing.assert_allclose(audio, -0.25, atol=1e-6)
            return {"text": "processed audio", "backend": "funasr"}

        results = {}
        with self.asr_backend(recognize):
            run_dag(self.graph(), output_dir=self.root / "output", node_results=results)
        self.assertEqual(results[3].outputs[0].value, "processed audio")
        self.assertEqual(results[2].outputs[0].source_path, str(self.source))
        self.assertNotEqual(paths[0], self.source)
        self.assertFalse(paths[0].parent.exists())
        self.assertEqual(list(self.scratch.iterdir()), [])
        np.testing.assert_allclose(sf.read(self.source)[0], 0.25, atol=1e-6)

    def test_asr_receives_limiter_output_samples(self):
        captured = []

        def recognize(path, model, language):
            captured.append(sf.read(path, always_2d=True)[0])
            return {"text": "limited audio", "backend": "funasr"}

        results = {}
        with self.asr_backend(recognize):
            run_dag(self.graph("DynamicsLimiter"), output_dir=self.root / "output", node_results=results)
        np.testing.assert_allclose(captured[0], results[2].outputs[0].audio.T, atol=1e-6)
        self.assertLess(float(np.abs(captured[0]).max()), 0.13)
        self.assertEqual(results[3].outputs[0].value, "limited audio")
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_asr_accepts_audio_without_file_provenance(self):
        audio = AudioArtifact(np.full((1, 1600), -0.125, dtype=np.float32), 16000)

        def recognize(path, model, language):
            self.assertTrue(Path(path).is_file())
            np.testing.assert_allclose(sf.read(path)[0], -0.125, atol=1e-6)
            return {"text": "memory audio", "backend": "funasr"}

        with self.asr_backend(recognize):
            result = self.runtime.run("pymss.asr.transcribe", inputs={"audio": audio}, params={})
        self.assertEqual(result["report"]["text"], "memory audio")
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_game_uses_separated_samples_and_registers_persistent_output(self):
        output_dir = self.root / "selected-output"
        output = output_dir / "vocals.mid"
        paths = []

        def separate(ctx, node, audio, **kwargs):
            return {"vocals": audio.audio * -0.5}, audio.sample_rate

        def transcribe(**kwargs):
            path = Path(kwargs["audio_path"])
            paths.append(path)
            samples, rate = sf.read(path, always_2d=True)
            np.testing.assert_allclose(samples, -0.125, atol=1e-6)
            self.assertEqual(rate, 16000)
            self.assertEqual(Path(kwargs["output_dir"]), output_dir)
            self.assertEqual(kwargs["model_path"], "small")
            output.write_bytes(b"MThd")
            kwargs["progress_callback"]("transcribing", 1, 1)
            return SimpleNamespace(output_path=output)

        handler = self.runtime._operations["pymss.game.vocal_to_midi"].run
        results = {}
        with (
            patch.object(self.graph_nodes, "_run_separation", side_effect=separate),
            patch.dict(handler.__globals__, {"vocal_to_midi": transcribe}),
        ):
            saved = run_dag(self.graph("mss_separate", "GameVocalToMidi"),
                            output_dir=output_dir, node_results=results)
        self.assertEqual(list(saved), [str(output)])
        self.assertEqual(saved.records[0].path, str(output))
        self.assertEqual(saved.records[0].format, "mid")
        self.assertEqual(saved.records[0].stem, "vocals")
        self.assertEqual(saved.records[0].source_path, str(self.source))
        self.assertEqual(saved.records[0].node_type, "GameVocalToMidi")
        self.assertEqual(results[3].outputs[0].value, str(output))
        self.assertTrue(output.is_file())
        self.assertFalse(paths[0].parent.exists())
        self.assertEqual(list(self.root.glob("*.mid")), [])
        np.testing.assert_allclose(sf.read(self.source)[0], 0.25, atol=1e-6)

    def test_asr_failure_removes_the_materialized_audio(self):
        for error_type in (RuntimeError, KeyboardInterrupt):
            with self.subTest(error=error_type.__name__):
                paths = []

                def fail(path, model, language):
                    paths.append(Path(path))
                    self.assertTrue(Path(path).is_file())
                    raise error_type("recognizer interrupted")

                with self.asr_backend(fail), self.assertRaisesRegex(error_type, "recognizer interrupted"):
                    run_dag(self.graph(), output_dir=self.root / "output")
                self.assertFalse(paths[0].parent.exists())
                self.assertEqual(list(self.scratch.iterdir()), [])
                self.assertTrue(self.source.is_file())

    def test_game_failure_removes_the_materialized_audio(self):
        paths = []

        def fail(**kwargs):
            path = Path(kwargs["audio_path"])
            paths.append(path)
            self.assertTrue(path.is_file())
            raise RuntimeError("MIDI inference failed")

        handler = self.runtime._operations["pymss.game.vocal_to_midi"].run
        with (
            patch.dict(handler.__globals__, {"vocal_to_midi": fail}),
            self.assertRaisesRegex(RuntimeError, "MIDI inference failed"),
        ):
            run_dag(self.graph(terminal="GameVocalToMidi"), output_dir=self.root / "output")
        self.assertFalse(paths[0].parent.exists())
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertTrue(self.source.is_file())

    def test_concurrent_workflows_use_distinct_live_audio_files(self):
        barrier = Barrier(2)
        lock = Lock()
        paths = []

        def recognize(path, model, language):
            with lock:
                paths.append(Path(path))
            barrier.wait(timeout=10)
            self.assertEqual(len(set(paths)), 2)
            self.assertTrue(all(item.is_file() for item in paths))
            np.testing.assert_allclose(sf.read(path)[0], -0.25, atol=1e-6)
            barrier.wait(timeout=10)
            return {"text": "parallel audio", "backend": "funasr"}

        with self.asr_backend(recognize), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run_dag, self.graph(), output_dir=self.root / str(index))
                       for index in range(2)]
            for future in futures:
                future.result(timeout=30)
        self.assertEqual(len({path.parent for path in paths}), 2)
        self.assertTrue(all(not path.parent.exists() for path in paths))
        self.assertEqual(list(self.scratch.iterdir()), [])
