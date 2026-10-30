"""Round 5: worker_operations commands (list/describe/run) against the real
pymss runtime, using the same stdout-capture pattern as test_worker_tools.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap

import worker_operations

def _loudnorm_fixture(root: Path) -> Path:
    """Self-contained audio operation for worker I/O and parameter transport."""
    fixture = root / "source" / "loudnorm"
    fixture.mkdir(parents=True)
    (fixture / "pyproject.toml").write_text(
        '[project]\n'
        'name="pymss-plugin-loudnorm"\n'
        'version="0.2.0"\n'
        'requires-python=">=3.10"\n'
        'dependencies=[]\n\n'
        '[tool.pymss.plugin]\n'
        'id="pymss.loudnorm"\n'
        'api_version=2\n\n'
        '[[tool.pymss.plugin.operations]]\n'
        'id="pymss.loudnorm.normalize"\n'
    )
    (fixture / "__init__.py").write_text(
        'import numpy as np\n\n'
        'def _peak_normalize(audio_in, params):\n'
        '    from pymss.graph.core import audio_to_numpy, numpy_to_audio\n'
        '    waveform, sr = audio_to_numpy(audio_in)\n'
        '    target = float(params.get("target_lufs", -14.0))\n'
        '    # A deterministic peak target isolates the worker transport.\n'
        '    peak = float(np.max(np.abs(waveform))) or 1.0\n'
        '    scale = (10 ** (target / 20.0)) / max(peak, 1e-9)\n'
        '    out = np.asarray(waveform * scale, dtype=np.float32)\n'
        '    return {"audio": numpy_to_audio(out, sr)}\n'
        '\n'
        'def setup(plugin):\n'
        '    plugin.register_operation(\n'
        '        id="pymss.loudnorm.normalize", title="Normalize Loudness (LUFS)",\n'
        '        description="LUFS loudness normalization",\n'
        '        inputs={"audio": {"type": "audio"}},\n'
        '        params={"type": "object", "properties": {\n'
        '            "target_lufs": {"type": "number", "default": -14.0, "minimum": -70.0, "maximum": -5.0},\n'
        '        }, "additionalProperties": False},\n'
        '        outputs={"audio": {"type": "audio"}},\n'
        '        run=lambda ctx, inputs, params: _peak_normalize(inputs["audio"], params),\n'
        '    )\n'
    )
    return fixture


_LOUDNORM_SRC = None  # resolved lazily via _loudnorm_fixture()


def _read_event(output: io.StringIO) -> dict:
    return json.loads(output.getvalue())


class ListOperationsTests(unittest.TestCase):
    def test_lists_operations_from_installed_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            plugins = Path(d) / "plugins"
            plugins.mkdir()
            shutil.copytree(_loudnorm_fixture(Path(d)), plugins / "loudnorm")
            old = os.environ.get("PYMSS_PLUGINS_DIR")
            os.environ["PYMSS_PLUGINS_DIR"] = str(plugins)
            try:
                # Fresh runtime per call: reset module-level default runtime
                import pymss.operations as ops
                ops._DEFAULT_RUNTIME = None
                import pymss.plugins as plugs
                plugs.reset()

                output = io.StringIO()
                with redirect_stdout(output):
                    code = worker_operations.cmd_list_operations({})
                event = _read_event(output)
            finally:
                if old is None:
                    os.environ.pop("PYMSS_PLUGINS_DIR", None)
                else:
                    os.environ["PYMSS_PLUGINS_DIR"] = old
                import pymss.operations as ops
                ops._DEFAULT_RUNTIME = None
                import pymss.plugins as plugs
                plugs.reset()
        self.assertEqual(code, 0)
        self.assertTrue(event["payload"]["pymssOperationsAvailable"])
        ids = [op["id"] for op in event["payload"]["operations"]]
        self.assertIn("pymss.loudnorm.normalize", ids)

    def test_reports_incompatible_when_required_plugin_apis_are_missing(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.dict("sys.modules", {"pymss": None, "pymss.plugins": None}),
            redirect_stdout(output),
        ):
            code = worker_operations.cmd_list_operations({})
        event = _read_event(output)
        self.assertEqual(code, 1)
        self.assertEqual(event["type"], "error")
        self.assertEqual(event["payload"]["code"], "OPERATIONS_LIST_FAILED")
        self.assertIn("required plugin APIs", event["payload"]["message"])


class RunOperationTests(unittest.TestCase):
    def _install_plugin(self, d: Path) -> None:
        plugins = d / "plugins"
        plugins.mkdir()
        shutil.copytree(_loudnorm_fixture(d), plugins / "loudnorm")
        os.environ["PYMSS_PLUGINS_DIR"] = str(plugins)

    def _reset_runtime(self) -> None:
        import pymss.operations as ops
        import pymss.plugins as plugs

        ops._DEFAULT_RUNTIME = None
        plugs.reset()

    def test_run_operation_normalizes_file_input(self) -> None:
        import numpy as np
        import soundfile as sf

        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            self._install_plugin(work)
            self._reset_runtime()
            try:
                sr = 44100
                t = np.arange(sr * 2, dtype=np.float32) / sr
                tone = (0.3 * np.stack([np.sin(2 * np.pi * 440 * t)] * 2)).astype(np.float32)
                wav = work / "tone.wav"
                sf.write(str(wav), tone.T, sr)

                out_dir = work / "configured-output"
                output = io.StringIO()
                with (
                    mock.patch.dict(os.environ, {"PYMSS_STUDIO_DEFAULT_OUTPUT_DIR": str(out_dir)}),
                    redirect_stdout(output),
                ):
                    code = worker_operations.cmd_run_operation({
                        "operation": "pymss.loudnorm.normalize",
                        "inputs": {"audio": {"kind": "file", "path": str(wav)}},
                        "params": {"target_lufs": -18.0},
                        "requestId": "req-1",
                    })
                events = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
                self.assertEqual(code, 0, events)
                result_event = next(e for e in events if e["type"] == "operation_result")
                audio_out = result_event["payload"]["outputs"]["audio"]
                self.assertEqual(audio_out["kind"], "audio_file")
                self.assertTrue(Path(audio_out["path"]).parent.samefile(out_dir))

                data, srr = sf.read(audio_out["path"], dtype="float32")
                self.assertEqual(srr, sr)
                self.assertEqual(data.shape, (sr * 2, 2))
                self.assertTrue(np.isfinite(data).all())
                self.assertAlmostEqual(float(abs(data).max()), 10 ** (-18.0 / 20.0), places=4)
            finally:
                self._reset_runtime()
                os.environ.pop("PYMSS_PLUGINS_DIR", None)

    def test_run_operation_missing_input_fails_with_code(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            self._install_plugin(work)
            self._reset_runtime()
            try:
                output = io.StringIO()
                with redirect_stdout(output):
                    code = worker_operations.cmd_run_operation({
                        "operation": "pymss.loudnorm.normalize",
                        "inputs": {},
                        "params": {},
                        "outputDir": str(work / "out"),
                    })
                event = _read_event(output)
                self.assertEqual(code, 1)
                self.assertEqual(event["payload"]["code"], "OPERATION_FAILED")
                self.assertIn("audio", event["payload"]["message"])
            finally:
                self._reset_runtime()
                os.environ.pop("PYMSS_PLUGINS_DIR", None)


class OperationInputTests(unittest.TestCase):
    def test_plugin_recovery_metadata_survives_the_operation_protocol(self):
        from pymss.operations import OperationSpec, Runtime
        class RecoveryError(RuntimeError):
            code = "ASR_MODEL_INCOMPLETE"
            recoverable = True
            extra = {"recovery": {"tool": "asr", "modelIds": ["selected-model"]}}
        def fail(*_):
            raise RecoveryError("The selected model cache is incomplete")
        runtime = Runtime()
        runtime.register(spec=OperationSpec(id="test.recovery", schema_version=1, title="Recovery", inputs={}, params={"type": "object"}, outputs={}), run=fail)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(worker_operations, "_runtime", return_value=runtime):
            output = io.StringIO()
            with redirect_stdout(output):
                code = worker_operations.cmd_run_operation({"operation": "test.recovery", "requestId": "recovery-1", "outputDir": directory})
        event = _read_event(output)
        self.assertEqual(code, 1)
        self.assertEqual(event["requestId"], "recovery-1")
        self.assertEqual(event["payload"]["code"], "ASR_MODEL_INCOMPLETE")
        self.assertTrue(event["payload"]["recoverable"])
        self.assertEqual(event["payload"]["recovery"]["modelIds"], ["selected-model"])

    def test_non_audio_file_descriptors_remain_objects(self):
        from pymss.operations import OperationSpec, Runtime

        runtime = Runtime()
        runtime.register(
            spec=OperationSpec(id="test.metadata", schema_version=1, title="Metadata", inputs={"metadata": {"type": "object"}},
                               params={"type": "object"}, outputs={"value": {"type": "string"}}),
            run=lambda c, inputs, p: {"value": inputs["metadata"]["path"]},
        )
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with mock.patch.object(worker_operations, "_runtime", return_value=runtime), redirect_stdout(output):
                code = worker_operations.cmd_run_operation({
                    "operation": "test.metadata", "inputs": {"metadata": {"kind": "file", "path": "metadata.json"}},
                    "outputDir": directory,
                })
            event = _read_event(output)
            self.assertEqual(code, 0, event)
            self.assertEqual(event["payload"]["outputs"]["value"]["value"], "metadata.json")

    def test_audio_file_inputs_keep_the_decoded_multichannel_layout(self):
        import numpy as np
        import soundfile as sf
        from pymss.audio_io import load_audio
        from pymss.operations import OperationSpec, Runtime

        runtime = Runtime()
        runtime.register(
            spec=OperationSpec(id="test.layout", schema_version=1, title="Layout", inputs={"audio": {"type": "audio"}},
                               params={"type": "object"}, outputs={"layout": {"type": "string"}, "channels": {"type": "integer"}}),
            run=lambda c, inputs, p: {"layout": inputs["audio"].channel_layout, "channels": inputs["audio"].audio.shape[0]},
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "surround.wav"
            sf.write(source, np.zeros((1000, 6), dtype=np.float32), 44100)
            _, _, expected = load_audio(str(source), sr=None, mono=False, return_layout=True)
            output = io.StringIO()
            with mock.patch.object(worker_operations, "_runtime", return_value=runtime), redirect_stdout(output):
                code = worker_operations.cmd_run_operation({
                    "operation": "test.layout", "inputs": {"audio": {"kind": "file", "path": str(source)}},
                    "outputDir": directory,
                })
            event = _read_event(output)
            self.assertEqual(code, 0, event)
            self.assertEqual(event["payload"]["outputs"]["layout"]["value"], expected)
            self.assertEqual(event["payload"]["outputs"]["channels"]["value"], 6)

    def test_invalid_input_and_parameter_containers_are_rejected_before_execution(self):
        for field in ["inputs", "params"]:
            for value in [[], False, 0, ""]:
                with self.subTest(field=field, value=value):
                    output = io.StringIO()
                    with mock.patch.object(worker_operations, "_runtime") as runtime, redirect_stdout(output):
                        code = worker_operations.cmd_run_operation({"operation": "test.invalid", field: value})
                    runtime.assert_not_called()
                    self.assertEqual(code, 1)
                    self.assertEqual(_read_event(output)["payload"]["code"], "OPERATION_INVALID")


class OperationOutputTests(unittest.TestCase):
    @staticmethod
    def audio(level):
        import numpy as np
        from pymss.graph.core import AudioArtifact

        return AudioArtifact(np.full((2, 1000), level, dtype=np.float32), 44100)

    def test_nested_audio_and_array_outputs_survive_the_worker_protocol(self):
        import numpy as np
        import soundfile as sf
        from pymss.operations import OperationSpec, Runtime

        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime()
            runtime.register(
                spec=OperationSpec(id="test.nested", schema_version=1, title="Nested", inputs={},
                                   params={"type": "object"}, outputs={"result": {"type": "object"}}),
                run=lambda ctx, inputs, params: {"result": {
                    "stems": [self.audio(0.1), self.audio(0.5)],
                    "scores": np.ones(3, dtype=np.float32),
                    "metadata": {"empty": None, "level": np.float32(0.5), "file": Path("audio.wav")},
                }},
            )
            output = io.StringIO()
            with mock.patch.object(worker_operations, "_runtime", return_value=runtime), redirect_stdout(output):
                code = worker_operations.cmd_run_operation({"operation": "test.nested", "outputDir": directory})
            event = _read_event(output)
            self.assertEqual(code, 0, event)
            result = event["payload"]["outputs"]["result"]["value"]
            paths = [entry["path"] for entry in result["stems"]]
            self.assertEqual(len(set(paths)), 2)
            for path, level in zip(paths, [0.1, 0.5]):
                waveform, rate = sf.read(path)
                self.assertEqual(rate, 44100)
                self.assertEqual(waveform.shape, (1000, 2))
                self.assertAlmostEqual(float(waveform.max()), level, places=4)
            self.assertEqual(result["scores"], {"kind": "array", "shape": [3], "dtype": "float32"})
            self.assertEqual(result["metadata"], {"empty": None, "level": 0.5, "file": "audio.wav"})

    def test_nonfinite_measurements_remain_valid_json_with_their_meaning_preserved(self):
        import numpy as np

        result = worker_operations._serialize_output("report", {
            "integrated_lufs": float("-inf"),
            "levels": [np.float32(float("inf")), float("nan"), -27.46],
        }, Path("unused"))
        wire = json.dumps(result, allow_nan=False)
        self.assertEqual(json.loads(wire)["value"], {
            "integrated_lufs": "-Infinity", "levels": ["Infinity", "NaN", -27.46],
        })

    def test_repeated_and_concurrent_outputs_preserve_earlier_audio(self):
        import soundfile as sf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = worker_operations._serialize_output("audio", self.audio(0.1), root)
            levels = [0.2, 0.3, 0.4, 0.5]
            with ThreadPoolExecutor(max_workers=4) as executor:
                outputs = list(executor.map(lambda level: worker_operations._serialize_output("audio", self.audio(level), root), levels))
            outputs.insert(0, first)
            self.assertEqual(len({entry["path"] for entry in outputs}), 5)
            for entry, level in zip(outputs, [0.1, *levels]):
                waveform, _ = sf.read(entry["path"])
                self.assertAlmostEqual(float(waveform.max()), level, places=4)
            self.assertEqual(len(list(root.iterdir())), 5)

    def test_output_port_names_cannot_escape_the_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = worker_operations._serialize_output("../lead\\voice", self.audio(0.1), root)
            self.assertTrue(Path(output["path"]).parent.samefile(root))

    def test_multichannel_output_roundtrip_preserves_layout_and_current_float_samples(self):
        import numpy as np
        from pymss.graph.core import AudioArtifact

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scratch = root / "scratch"
            scratch.mkdir()
            for layout, channels in [("5.1(side)", 6), ("5.1", 6), ("7.1", 8), ("quad", 4)]:
                with self.subTest(layout=layout), mock.patch.dict(os.environ, {"PYMSS_SCRATCH_DIR": str(scratch)}):
                    wave = np.linspace(-1.25, 1.25, 1000, dtype=np.float32)
                    samples = np.stack([wave * (1 - channel * 0.05) for channel in range(channels)])
                    audio = AudioArtifact(samples, 48000, source_path="missing-original.wav", channel_layout=layout)
                    exported = worker_operations._serialize_output("surround", audio, root)
                    loaded = worker_operations._resolve_input({"kind": "file", "path": exported["path"]})
                    self.assertEqual(loaded.channel_layout, layout)
                    self.assertEqual(loaded.sample_rate, 48000)
                    np.testing.assert_allclose(loaded.audio, samples, rtol=0, atol=1e-7)
                    self.assertEqual(list(scratch.iterdir()), [])

    def test_failed_audio_write_does_not_publish_or_leave_a_partial(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as scratch:
            root = Path(directory)
            previous = root / "operation_output_audio.wav"
            previous.write_bytes(b"previous export")

            def fail(source, target):
                Path(target).write_bytes(b"partial")
                raise OSError("disk failure")

            with (
                mock.patch.dict(os.environ, {"PYMSS_SCRATCH_DIR": scratch}),
                mock.patch.object(worker_operations.shutil, "copyfile", side_effect=fail),
                self.assertRaisesRegex(OSError, "disk failure"),
            ):
                worker_operations._serialize_output("audio", self.audio(0.1), root)
            self.assertEqual(list(root.iterdir()), [previous])
            self.assertEqual(previous.read_bytes(), b"previous export")
            self.assertEqual(list(Path(scratch).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
