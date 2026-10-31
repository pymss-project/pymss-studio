"""Independent references for layout-aware loudness and normalization."""

from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pyloudnorm as pyln

from pymss.graph.core import AudioArtifact
from pymss.operations import create_runtime
from pymss.plugins import require_capability
from pymss.plugins.kit import ffmpeg_path


ROOT = Path(__file__).resolve().parents[2]


class LoudnessLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = create_runtime()
        cls.runtime.load_plugins([ROOT / "plugins"])
        cls.sample_rate = 48000
        time = np.arange(cls.sample_rate * 2) / cls.sample_rate
        cls.tone = (0.03 * np.sin(2 * np.pi * 997 * time)).astype(np.float32)

    def ffmpeg_loudness(self, audio, layout):
        process = subprocess.run(
            [str(ffmpeg_path()), "-hide_banner", "-nostats", "-f", "f32le",
             "-ar", str(self.sample_rate), "-ac", str(audio.shape[0]),
             "-channel_layout", layout, "-i", "pipe:0", "-af", "ebur128", "-f", "null", "-"],
            input=np.asarray(audio.T, dtype="<f4").tobytes(), capture_output=True, timeout=30,
        )
        stderr = process.stderr.decode("utf-8", errors="replace")
        self.assertEqual(process.returncode, 0, stderr[-2000:])
        values = re.findall(r"I:\s*([-\d.]+) LUFS", stderr)
        self.assertTrue(values, stderr[-2000:])
        return float(values[-1])

    def test_measurement_and_normalization_match_ffmpeg_with_surround_and_lfe(self):
        for layout, channels in [("5.1", 6), ("5.1(side)", 6), ("7.1", 8)]:
            with self.subTest(layout=layout):
                audio = np.stack([self.tone * (-1 if i % 2 else 1) * (1 + i * 0.1)
                                  for i in range(channels)])
                # A large LFE must not contribute to the measurement.
                audio[3] *= 3
                source = AudioArtifact(audio, self.sample_rate, channel_layout=layout)
                measured = self.runtime.run("pymss.analysis.measure_lufs", inputs={"audio": source}, params={})["report"]["lufs"]
                self.assertAlmostEqual(measured, self.ffmpeg_loudness(audio, layout), delta=0.1)
                normalized = self.runtime.run("pymss.loudnorm.normalize", inputs={"audio": source},
                                              params={"target_lufs": -18.0})["audio"]
                self.assertAlmostEqual(self.ffmpeg_loudness(normalized.audio, layout), -18.0, delta=0.1)
                # Analysis must also forward the layout, rather than guessing.
                measured = self.runtime.run("pymss.analysis.measure_lufs", inputs={"audio": normalized}, params={})["report"]["lufs"]
                self.assertAlmostEqual(measured, -18.0, places=4)

    def test_silent_and_lfe_only_inputs_remain_unchanged(self):
        normalize = require_capability("lufs_normalize")
        measure = require_capability("measure_lufs")
        for lfe in (False, True):
            with self.subTest(lfe=lfe):
                audio = np.zeros((6, len(self.tone)), dtype=np.float32)
                if lfe:
                    audio[3] = self.tone
                self.assertEqual(measure(audio, self.sample_rate, channel_layout="5.1")["lufs"], -np.inf)
                np.testing.assert_array_equal(normalize(audio, self.sample_rate, channel_layout="5.1"), audio)

    def test_processor_operations_preserve_layout_for_loudness_chaining(self):
        audio = AudioArtifact(np.tile(self.tone, (6, 1)), self.sample_rate,
                              source_path="/audio/surround.wav", stem_name="surround", channel_layout="5.1(side)")
        operations = [
            ("pymss.spectral.lowpass", {}),
            ("pymss.dynamics.limiter", {}),
            ("pymss.timing.time_stretch", {"rate": 1.25}),
        ]
        for operation, params in operations:
            with self.subTest(operation=operation):
                audio = self.runtime.run(operation, inputs={"audio": audio}, params=params)["audio"]
                self.assertEqual(audio.channel_layout, "5.1(side)")
                self.assertEqual(audio.source_path, "/audio/surround.wav")
                self.assertEqual(audio.stem_name, "surround")
                self.assertEqual(audio.audio.shape[0], 6)
                self.assertEqual(audio.sample_rate, self.sample_rate)
                normalized = self.runtime.run("pymss.loudnorm.normalize", inputs={"audio": audio},
                                              params={"target_lufs": -18.0})["audio"]
                measured = self.runtime.run("pymss.analysis.measure_lufs", inputs={"audio": normalized}, params={})["report"]["lufs"]
                self.assertAlmostEqual(measured, -18.0, places=4)

    def test_gating_is_shared_by_channels_with_different_active_intervals(self):
        audio = np.zeros((8, len(self.tone) * 3), dtype=np.float32)
        audio[0, :len(self.tone)] = self.tone
        audio[1, :len(self.tone)] = -self.tone
        audio[4, len(self.tone):2 * len(self.tone)] = self.tone * 0.2
        audio[6, 2 * len(self.tone):] = self.tone * 0.005
        level = require_capability("measure_lufs")(audio, self.sample_rate, channel_layout="7.1")["lufs"]
        self.assertAlmostEqual(level, self.ffmpeg_loudness(audio, "7.1"), delta=0.1)

    def test_channel_order_and_phase_do_not_change_energy(self):
        measure = require_capability("measure_lufs")
        front = np.zeros((6, len(self.tone)), dtype=np.float32)
        front[0] = self.tone
        surround = np.zeros_like(front)
        surround[4] = self.tone
        front_level = measure(front, self.sample_rate, channel_layout="5.1")["lufs"]
        surround_level = measure(surround, self.sample_rate, channel_layout="5.1")["lufs"]
        self.assertAlmostEqual(surround_level - front_level, 10 * np.log10(1.41), places=6)
        pair = np.stack([self.tone, -self.tone])
        level = measure(pair, self.sample_rate)["lufs"]
        self.assertAlmostEqual(level - front_level, 10 * np.log10(2), places=6)
        # A 2.1 layout puts LFE in slot 2, not slot 3.
        lfe = np.stack([self.tone * 0, self.tone * 0, self.tone])
        self.assertEqual(measure(lfe, self.sample_rate, channel_layout="2.1")["lufs"], -np.inf)

    def test_mono_stereo_and_gating_match_the_reference_meter(self):
        for channels in (1, 2):
            with self.subTest(channels=channels):
                signal = np.concatenate([self.tone * 0, self.tone, self.tone * 0.005])
                audio = np.stack([signal * (1 + i * 0.2) for i in range(channels)])
                expected = pyln.Meter(self.sample_rate, filter_class="DeMan").integrated_loudness(audio.astype(np.float64).T)
                actual = require_capability("measure_lufs")(audio, self.sample_rate)["lufs"]
                self.assertAlmostEqual(actual, expected, places=6)
                mono_or_stereo = audio[0] if channels == 1 else audio
                result = require_capability("lufs_normalize")(mono_or_stereo, self.sample_rate)
                self.assertEqual(result.shape, mono_or_stereo.shape)

    def test_multichannel_layout_is_required_and_validated(self):
        audio = np.tile(self.tone, (6, 1))
        for capability in ("measure_lufs", "lufs_normalize", "measure_dynamic_range"):
            for layout in (None, "stereo", "5.1.2", "6 channels"):
                with self.subTest(capability=capability, layout=layout), self.assertRaises(ValueError):
                    require_capability(capability)(audio, self.sample_rate, channel_layout=layout)

    def test_count_only_mono_stereo_layouts_match_named_speakers(self):
        for channels, named in ((1, "mono"), (2, "stereo")):
            audio = np.tile(self.tone, (channels, 1))
            for capability in ("measure_lufs", "lufs_normalize", "measure_dynamic_range"):
                with self.subTest(channels=channels, capability=capability):
                    measure = require_capability(capability)
                    expected = measure(audio, self.sample_rate, channel_layout=named)
                    actual = measure(audio, self.sample_rate, channel_layout=f"{channels} channels")
                    if isinstance(actual, dict):
                        self.assertEqual(actual, expected)
                    else:
                        np.testing.assert_array_equal(actual, expected)
                    mismatched = f"{3 - channels} channels"
                    with self.assertRaisesRegex(ValueError, "channel count"):
                        measure(audio, self.sample_rate, channel_layout=mismatched)

    def test_float_wav_decode_can_be_measured_and_normalized(self):
        import soundfile
        from pymss.audio_io import load_audio

        with tempfile.TemporaryDirectory() as temporary:
            for channels in (1, 2):
                with self.subTest(channels=channels):
                    path = Path(temporary) / f"input-{channels}.wav"
                    soundfile.write(path, np.tile(self.tone, (channels, 1)).T,
                                    self.sample_rate, subtype="FLOAT")
                    decoded, sample_rate, layout = load_audio(str(path), sr=None, mono=False, return_layout=True)
                    source = AudioArtifact(decoded, sample_rate, channel_layout=layout)
                    measured = self.runtime.run("pymss.analysis.measure_lufs", inputs={"audio": source}, params={})["report"]["lufs"]
                    self.assertTrue(np.isfinite(measured))
                    normalized = self.runtime.run("pymss.loudnorm.normalize", inputs={"audio": source},
                                                  params={"target_lufs": -18.0})["audio"]
                    actual = self.runtime.run("pymss.analysis.measure_lufs", inputs={"audio": normalized}, params={})["report"]["lufs"]
                    self.assertAlmostEqual(actual, -18.0, places=4)

    def test_dynamic_range_uses_channel_energy_for_each_complete_block(self):
        block = self.tone[:self.sample_rate * 2 // 5]
        wave = np.concatenate([block, block * 0.1])
        audio = np.stack([wave, -wave, wave * 0, wave * 0, wave, -wave])
        result = require_capability("measure_dynamic_range")(audio, self.sample_rate, channel_layout="5.1")
        self.assertAlmostEqual(result["dr_lu"], 20.0, places=5)
        self.assertTrue(np.isfinite(result["lufs_min"]))

    def test_independent_plugins_bundle_identical_meter_logic(self):
        copies = [(ROOT / "plugins" / name / "backend/lufs.py").read_text(encoding="utf-8")
                  for name in ("analysis", "loudnorm")]
        self.assertEqual(copies[0].replace("plugins/loudnorm/", "plugins/analysis/"), copies[1])
