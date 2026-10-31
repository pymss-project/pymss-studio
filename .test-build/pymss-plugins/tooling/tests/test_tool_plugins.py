"""Check independent plugin discovery and the published Operation contract."""

from __future__ import annotations

import json
import tomllib
import unittest
from pathlib import Path

import numpy as np

from pymss.graph.core import audio_to_numpy, numpy_to_audio
from pymss.operations import OperationError, create_runtime
from pymss.plugins.manifest import read_manifest


ROOT = Path(__file__).resolve().parents[2]


class ToolPluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = create_runtime()
        cls.reports = cls.runtime.load_plugins([ROOT / "plugins"])

    def audio(self, sample_rate=44100):
        t = np.arange(sample_rate, dtype=np.float32) / sample_rate
        wave = (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        return numpy_to_audio(np.stack([wave, wave * 0.5]), sample_rate)

    def test_all_catalog_plugins_load_without_model_backends(self):
        registry = json.loads((ROOT / "registry.json").read_text(encoding="utf-8"))
        names = {name for name in registry if not name.startswith("_")}
        self.assertEqual({row["plugin_id"] for row in self.reports}, names)
        self.assertTrue(all(row["loaded"] for row in self.reports), self.reports)
        for row in self.reports:
            with self.subTest(plugin=row["plugin_id"]):
                self.assertEqual(registry[row["plugin_id"]]["operations"], [op["id"] for op in row["operations"]])

    def test_each_plugin_contributes_a_self_contained_tool_view(self):
        for directory in sorted((ROOT / "plugins").iterdir()):
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            with self.subTest(plugin=directory.name):
                read_manifest(directory)
                data = tomllib.loads((directory / "pyproject.toml").read_text(encoding="utf-8"))
                views = data["tool"]["pymss"]["studio"]["views"]
                self.assertEqual({view['id'] for view in views}, {'tool', 'spectrum'} if directory.name == 'analysis' else {'tool'})
                view = next(view for view in views if view['id'] == 'tool')
                self.assertEqual(view["placements"], ["tool"])
                self.assertIn("audio.analyze" if directory.name == "tempo_key" else "operations", view["permissions"])
                self.assertTrue(set(view["permissions"]) <= {"ui.overlay", "operations", "operations.cancel", "files", "files.pickAudio", "settings", "app.info", "clipboard.writeText", "audio.analyze", "runtime", "models.deleteTool", "shell.openExternal"})
                entry = directory / view["entry"]
                self.assertTrue(entry.is_file())
                html = entry.read_text(encoding="utf-8")
                self.assertNotIn('<script src=', html)
                self.assertNotIn('<link ', html)

    def test_tool_entries_reference_exactly_the_plugins_own_operations(self):
        tools = {name: json.loads((ROOT / "plugins" / name / "ui/src/tool.json").read_text(encoding="utf-8")) for name in ["analysis", "dynamics", "spectral", "timing"]}
        specs = self.runtime.list_operations()
        for name, tool in tools.items():
            with self.subTest(plugin=name):
                manifest = read_manifest(ROOT / "plugins" / name)
                expected = {spec["id"] for spec in specs if spec["id"].startswith(manifest.plugin_id + ".")}
                self.assertEqual({entry["id"] for entry in tool["operations"]}, expected)
                self.assertTrue(expected)

    def test_spectral_default_peak_eq_preserves_stereo_audio(self):
        source = self.audio()
        result = self.runtime.run("pymss.spectral.peak_eq", inputs={"audio": source}, params={})
        actual, sample_rate = audio_to_numpy(result["audio"])
        expected, _ = audio_to_numpy(source)
        self.assertEqual(sample_rate, 44100)
        np.testing.assert_allclose(actual, expected, atol=1e-6)

    def test_filter_operations_accept_their_existing_frequency_parameter(self):
        for name in ["highpass", "lowpass", "peak_eq"]:
            with self.subTest(operation=name):
                result = self.runtime.run(f"pymss.spectral.{name}", inputs={"audio": self.audio()}, params={"f0": 1000.0})
                audio, _ = audio_to_numpy(result["audio"])
                self.assertEqual(audio.shape, (2, 44100))
                self.assertTrue(np.isfinite(audio).all())

    def test_filter_rejects_frequency_at_or_above_input_nyquist(self):
        with self.assertRaisesRegex(ValueError, "Nyquist"):
            self.runtime.run("pymss.spectral.lowpass", inputs={"audio": self.audio(8000)}, params={"f0": 8000})

    def test_true_peak_of_stereo_matches_largest_channel_peak(self):
        source = self.audio()
        wave, sr = audio_to_numpy(source)
        operation = "pymss.analysis.measure_true_peak"
        stereo = self.runtime.run(operation, inputs={"audio": source}, params={})["report"]
        channels = [
            self.runtime.run(operation, inputs={"audio": numpy_to_audio(channel, sr)}, params={})["report"]
            for channel in wave
        ]
        self.assertAlmostEqual(stereo["true_peak_dbtp"], max(row["true_peak_dbtp"] for row in channels), places=5)

    def test_limiter_operation_preserves_layout_and_caps_peak(self):
        source = numpy_to_audio(np.array([[0.0, 2.0, -2.0], [0.0, -1.0, 1.0]], dtype=np.float32), 44100)
        result = self.runtime.run("pymss.dynamics.limiter", inputs={"audio": source}, params={"threshold_db": -6.0})
        audio, sample_rate = audio_to_numpy(result["audio"])
        self.assertEqual(audio.shape, (2, 3))
        self.assertEqual(sample_rate, 44100)
        self.assertLessEqual(float(np.abs(audio).max()), 10 ** (-6 / 20) + 1e-6)

    def test_parameter_validation_rejects_invalid_values(self):
        for operation, params in [
            ("pymss.timing.time_stretch", {"rate": 0}),
            ("pymss.spectral.peak_eq", {"gain_db": 99}),
            ("pymss.analysis.spectrum", {"n_fft": 2.5}),
            ("pymss.game.vocal_to_midi", {"tempo": 0}),
        ]:
            with self.subTest(operation=operation), self.assertRaises(OperationError):
                self.runtime.run(operation, inputs={"audio": self.audio()}, params=params)

    def test_analysis_rms_and_spectrum_return_reports(self):
        source = self.audio()
        measured = self.runtime.run("pymss.analysis.measure_rms", inputs={"audio": source}, params={})
        self.assertGreater(measured["report"]["rms_linear"], 0)
        spectrum = self.runtime.run("pymss.analysis.spectrum", inputs={"audio": source}, params={"n_fft": 1024})
        self.assertEqual(len(spectrum["report"]["freqs"]), 513)

    def test_loudness_normalization_reaches_target(self):
        source = self.audio()
        normalized = self.runtime.run("pymss.loudnorm.normalize", inputs={"audio": source}, params={"target_lufs": -18.0})
        measured = self.runtime.run("pymss.analysis.measure_lufs", inputs={"audio": normalized["audio"]}, params={})
        self.assertAlmostEqual(measured["report"]["lufs"], -18.0, places=3)

    def test_timing_stretch_preserves_channels_and_changes_duration(self):
        result = self.runtime.run("pymss.timing.time_stretch", inputs={"audio": self.audio()}, params={"rate": 2.0})
        wave, sr = audio_to_numpy(result["audio"])
        self.assertEqual(wave.shape, (2, 22050))
        self.assertEqual(sr, 44100)

    def test_multichannel_peak_measurement_does_not_downmix_opposite_channels(self):
        wave = np.tile(np.array([[0.8, -0.8], [-0.8, 0.8]], dtype=np.float32), (3, 1))
        result = self.runtime.run("pymss.analysis.measure_true_peak", inputs={"audio": numpy_to_audio(wave, 44100)}, params={})
        self.assertAlmostEqual(result["report"]["sample_peak_dbfs"], 20 * np.log10(0.8), places=5)


class ViewBuildTests(unittest.TestCase):
    def test_each_plugin_declares_its_own_component_source_and_packaged_page(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("view_components", ROOT / "tooling/scripts/view_components.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        entries = module.component_entries()
        self.assertEqual(len(entries), 13)
        for entry in entries:
            with self.subTest(plugin=entry["plugin"]):
                self.assertTrue(Path(entry["component"]).is_relative_to(ROOT / "plugins" / entry["plugin"]))
                self.assertIn("Generated by tooling/scripts/build_tool_views.mjs", Path(entry["entry"]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
