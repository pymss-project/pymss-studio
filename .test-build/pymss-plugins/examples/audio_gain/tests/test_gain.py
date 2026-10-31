from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib
import unittest

import numpy as np
from pymss.graph.core import AudioArtifact
from pymss.operations import OperationContext, OperationError, create_runtime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.gain import OPERATION_ID, apply_gain  # noqa: E402


class AudioGainTests(unittest.TestCase):
    def audio(self, channels=2, frames=32, level=0.1, layout="stereo"):
        return AudioArtifact(np.full((channels, frames), level, dtype=np.float32), 48000, channel_layout=layout)

    def test_gain_preserves_sample_rate_shape_and_input(self):
        original = self.audio()
        result = apply_gain(OperationContext(), {"audio": original}, {"gain_db": 6, "protect_peak": False})
        np.testing.assert_allclose(result["audio"].audio, original.audio * 10 ** (6 / 20), rtol=1e-6)
        self.assertEqual(result["audio"].sample_rate, 48000)
        self.assertEqual(result["audio"].channel_layout, "stereo")
        np.testing.assert_array_equal(original.audio, np.full((2, 32), 0.1, dtype=np.float32))

    def test_short_multichannel_buffers_keep_their_layout(self):
        original = self.audio(channels=6, frames=3, layout="5.1")
        result = apply_gain(OperationContext(), {"audio": original}, {})["audio"]
        self.assertEqual(result.audio.shape, (6, 3))
        self.assertEqual(result.channel_layout, "5.1")
        self.assertEqual(result.source_path, "")

    def test_peak_protection_and_silence_are_finite(self):
        for level, limited in [(1.2, True), (0.0, False)]:
            with self.subTest(level=level):
                result = apply_gain(OperationContext(), {"audio": self.audio(level=level)}, {"gain_db": 12})
                self.assertEqual(result["report"]["peak_limited"], limited)
                self.assertLessEqual(np.max(np.abs(result["audio"].audio)), 0.999001)
                self.assertTrue(np.isfinite(result["audio"].audio).all())

    def test_invalid_gain_and_boolean_parameters_are_rejected(self):
        for gain in [True, "3", float("nan"), float("inf"), -25, 13]:
            with self.subTest(gain=gain), self.assertRaises(ValueError):
                apply_gain(OperationContext(), {"audio": self.audio()}, {"gain_db": gain})
        with self.assertRaises(ValueError):
            apply_gain(OperationContext(), {"audio": self.audio()}, {"protect_peak": "false"})

    def test_invalid_samples_are_rejected(self):
        for level in [float("nan"), float("inf")]:
            with self.subTest(level=level), self.assertRaises(ValueError):
                apply_gain(OperationContext(), {"audio": self.audio(level=level)}, {})
        with self.assertRaises(ValueError):
            apply_gain(OperationContext(), {"audio": self.audio(frames=0)}, {})

    def test_peak_protection_avoids_overflow_and_unprotected_overflow_is_rejected(self):
        audio = self.audio(level=np.finfo(np.float32).max)
        with self.assertRaisesRegex(ValueError, "non-finite output"):
            apply_gain(OperationContext(), {"audio": audio}, {"gain_db": 12, "protect_peak": False})
        result = apply_gain(OperationContext(), {"audio": audio}, {"gain_db": 12})
        self.assertTrue(np.isfinite(result["audio"].audio).all())
        self.assertLessEqual(float(np.max(result["audio"].audio)), 0.999001)

    def test_progress_is_monotonic_and_cancellation_is_cooperative(self):
        updates = []
        context = OperationContext(progress=lambda value, message, event: updates.append(value))
        apply_gain(context, {"audio": self.audio(frames=262145)}, {})
        self.assertEqual(updates, sorted(updates))
        self.assertEqual(updates[-1], 1.0)
        updates.clear()
        context.cancelled = lambda: bool(updates)
        with self.assertRaisesRegex(OperationError, "cancelled"):
            apply_gain(context, {"audio": self.audio(frames=262145)}, {})

    def test_a_copied_package_loads_and_runs_without_the_official_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "audio_gain"
            shutil.copytree(ROOT, destination, ignore=shutil.ignore_patterns("node_modules", "__pycache__", ".git", ".tmp"))
            runtime = create_runtime()
            reports = runtime.load_plugins([temporary])
            self.assertTrue(reports and all(row["loaded"] for row in reports), reports)
            result = runtime.run(OPERATION_ID, inputs={"audio": self.audio()}, params={"gain_db": -6})
            self.assertEqual(result["audio"].audio.shape, (2, 32))

    def test_metadata_and_local_translations_share_the_operation_identity(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        metadata = data["tool"]["pymss"]
        self.assertEqual(metadata["studio"]["views"][0]["operation"], OPERATION_ID)
        self.assertIn(OPERATION_ID, (ROOT / "ui/src/studio.ts").read_text(encoding="utf-8"))
        en = json.loads((ROOT / "ui/src/locales/en.json").read_text(encoding="utf-8"))
        zh = json.loads((ROOT / "ui/src/locales/zh-CN.json").read_text(encoding="utf-8"))

        def keys(value, prefix=""):
            return {path for key, child in value.items() for path in (keys(child, prefix + key + ".") if isinstance(child, dict) else [prefix + key])}

        self.assertEqual(keys(en), keys(zh))
        self.assertEqual(en["title"], metadata["studio"]["locales"]["en"]["title"])
        self.assertEqual(zh["title"], metadata["studio"]["locales"]["zh-CN"]["title"])
        self.assertNotIn("@/", (ROOT / "ui/src/App.vue").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
