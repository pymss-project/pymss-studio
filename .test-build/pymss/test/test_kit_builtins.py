"""Tests for pymss.kit.* built-in file operations (convert/inspect/sdr/slice/merge)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from pymss.operations import OperationContext, default_runtime
from pymss.operations.builtins import register_builtin_operations
from pymss.operations.kit_builtins import register_kit_operations


def _silent_context() -> OperationContext:
    return OperationContext()


def _two_segment_tone(path: Path) -> None:
    """4 s stereo: 1 s tone, 1 s silence, 1 s tone, 1 s silence."""
    # Phase must be computed sample-normalized: sin(2*pi*f*t/sr). Feeding the
    # raw product (2*pi*440*n, up to ~1.2e8 rad) into float64 sin destroys
    # phase precision — the "tone" comes out as ~3e-9 noise (silent).
    time_axis = np.arange(44100, dtype=np.float64)
    wave = (0.3 * np.sin(2 * np.pi * 440.0 * time_axis / 44100.0)).astype(np.float32)
    tone = np.zeros((2, 44100 * 4), dtype=np.float32)
    tone[:, :44100] = wave
    tone[:, 44100 * 2:44100 * 3] = wave
    sf.write(path, tone.T, 44100)


class KitBuiltinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        register_builtin_operations()
        cls.runtime = default_runtime()
        cls.work = Path(tempfile.mkdtemp(prefix="pymss-kit-test-"))
        cls.source = cls.work / "in.wav"
        _two_segment_tone(cls.source)
        cls.ctx = _silent_context()

    def test_all_five_kit_operations_registered(self):
        ids = {op["id"] for op in self.runtime.list_operations()}
        for expected in ("pymss.kit.convert", "pymss.kit.inspect", "pymss.kit.sdr", "pymss.kit.slice", "pymss.kit.merge"):
            self.assertIn(expected, ids)

    def test_registration_is_idempotent(self):
        self.assertEqual(register_kit_operations(), 0)

    def test_convert_produces_requested_format(self):
        result = self.runtime.run(
            "pymss.kit.convert",
            inputs={"audio": str(self.source)},
            params={"output_dir": str(self.work), "format": "flac"},
            context=self.ctx,
        )
        output = Path(result["audio_file"])
        self.assertEqual(output.suffix, ".flac")
        self.assertTrue(output.is_file())

    def test_convert_rejects_unknown_format(self):
        with self.assertRaises(Exception):
            self.runtime.run(
                "pymss.kit.convert",
                inputs={"audio": str(self.source)},
                params={"output_dir": str(self.work), "format": "wma"},
                context=self.ctx,
            )

    def test_inspect_reports_stream_metadata(self):
        result = self.runtime.run(
            "pymss.kit.inspect",
            inputs={"audio": str(self.source)},
            params={},
            context=self.ctx,
        )
        report = result["report"]
        self.assertEqual(report["streams"][0]["sample_rate"], 44100)
        self.assertEqual(report["streams"][0]["channels"], 2)
        self.assertGreater(report["duration_seconds"], 3.5)

    def test_sdr_identical_files_score_high(self):
        result = self.runtime.run(
            "pymss.kit.sdr",
            inputs={"reference": str(self.source), "estimated": str(self.source)},
            params={},
            context=self.ctx,
        )
        self.assertGreater(result["report"]["average_sdr"], 60)

    def test_sdr_noise_files_score_low(self):
        noise = self.work / "noise.wav"
        sf.write(noise, (np.random.rand(2, 44100 * 2) * 0.3).astype(np.float32).T, 44100)
        result = self.runtime.run(
            "pymss.kit.sdr",
            inputs={"reference": str(self.source), "estimated": str(noise)},
            params={},
            context=self.ctx,
        )
        self.assertLess(result["report"]["average_sdr"], 10)

    def test_slice_finds_both_loud_segments(self):
        result = self.runtime.run(
            "pymss.kit.slice",
            inputs={"audio": str(self.source)},
            params={
                "output_dir": str(self.work), "threshold_db": -40,
                "min_length_ms": 800, "min_interval_ms": 300,
                "hop_size_ms": 50, "max_silence_kept_ms": 500,
            },
            context=self.ctx,
        )
        self.assertGreaterEqual(result["count"], 2)
        for path in result["files"]:
            self.assertTrue(Path(path).is_file())

    def test_slice_silent_file_is_an_error(self):
        silent = self.work / "silent.wav"
        sf.write(silent, np.zeros((2, 44100), dtype=np.float32).T, 44100)
        with self.assertRaises(Exception):
            self.runtime.run(
                "pymss.kit.slice",
                inputs={"audio": str(silent)},
                params={"output_dir": str(self.work), "min_length_ms": 500,
                        "min_interval_ms": 100, "hop_size_ms": 50, "max_silence_kept_ms": 200},
                context=self.ctx,
            )

    def test_merge_concatenates_segments(self):
        parts = [self.work / "part_a.wav", self.work / "part_b.wav"]
        t_axis = np.arange(44100, dtype=np.float64)
        sf.write(parts[0], (0.2 * np.sin(2 * np.pi * 440.0 * t_axis / 44100.0)).astype(np.float32), 44100)
        sf.write(parts[1], (0.2 * np.sin(2 * np.pi * 880.0 * t_axis / 44100.0)).astype(np.float32), 44100)
        result = self.runtime.run(
            "pymss.kit.merge",
            inputs={"audio": str(parts[0])},
            params={"output_dir": str(self.work), "additional_inputs": str(parts[1])},
            context=self.ctx,
        )
        self.assertEqual(result["count"], 2)
        merged = Path(result["audio_file"])
        self.assertTrue(merged.is_file())
        import soundfile as sf2
        data, sr = sf2.read(str(merged))
        self.assertGreater(len(data), 44100 * 1.8)

    def test_in_memory_only_audio_is_rejected_clearly(self):
        from pymss.graph.core import numpy_to_audio

        artifact = numpy_to_audio(np.zeros((2, 100), dtype=np.float32), 44100)
        with self.assertRaises(ValueError):
            self.runtime.run(
                "pymss.kit.inspect",
                inputs={"audio": artifact},
                params={},
                context=self.ctx,
            )


if __name__ == "__main__":
    unittest.main()


class SdrSampleRateTests(unittest.TestCase):
    """Cross-rate comparison must align on one timeline (PR #33 review)."""

    def test_sdr_cross_rate(self) -> None:
        import numpy as np
        import soundfile as sf

        from pymss.operations import default_runtime

        runtime = default_runtime()
        with tempfile.TemporaryDirectory() as work:
            work_dir = Path(work)
            t = np.arange(44100, dtype=np.float64)
            signal = 0.5 * np.sin(2 * np.pi * 1000.0 * t / 44100.0)
            sf.write(work_dir / "ref44100.wav", signal.astype(np.float32), 44100)
            # Same continuous signal sampled at 48 kHz.
            t48 = np.arange(48000, dtype=np.float64)
            signal48 = 0.5 * np.sin(2 * np.pi * 1000.0 * t48 / 48000.0)
            sf.write(work_dir / "est48000.wav", signal48.astype(np.float32), 48000)
            from test.test_kit_builtins import _silent_context

            result = runtime.run(
                "pymss.kit.sdr",
                inputs={
                    "reference": str(work_dir / "ref44100.wav"),
                    "estimated": str(work_dir / "est48000.wav"),
                },
                params={},
                context=_silent_context(),
            )
            sdr = float(result["report"]["average_sdr"])
            # Identical analog content across rates must score high, not ~-3 dB.
            self.assertGreater(sdr, 40.0, f"cross-rate SDR should be high, got {sdr}")
