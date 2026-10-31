from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest

import numpy as np

from pymss.graph.core import AudioArtifact
from pymss.operations import create_runtime
from pymss.plugins import get_registry, require_capability


ROOT = Path(__file__).resolve().parents[2]
FILTERS = [
    ("SpectralHighpass", "highpass", "cutoff_hz", 2000),
    ("SpectralLowpass", "lowpass", "cutoff_hz", 32000),
    ("SpectralPeakEQ", "peak_eq", "center_hz", 32000),
]


class SpectralValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = create_runtime()
        reports = cls.runtime.load_plugins([ROOT / "plugins"])
        if not all(row["loaded"] for row in reports):
            raise AssertionError(reports)

    def audio(self, sample_rate):
        tone = 0.1 * np.sin(2 * np.pi * 220 * np.arange(4096) / sample_rate)
        return AudioArtifact(np.stack([tone, -tone]).astype(np.float32), sample_rate)

    def run_node(self, node_type, capability, frequency, audio):
        widgets = [frequency, 6.0, 0.707] if capability == "peak_eq" else [frequency, 0.707]
        context = SimpleNamespace(
            current_node_id="filter",
            nodes_by_id={"filter": SimpleNamespace(data={"widgets_values": widgets})},
            require=require_capability,
        )
        return get_registry().nodes[node_type].func(context, {"audio": audio}).outputs[0]

    def test_nodes_and_operations_reject_frequency_at_or_above_nyquist(self):
        for node_type, capability, _, sample_rate in FILTERS:
            audio = self.audio(sample_rate)
            for frequency in (sample_rate / 2, sample_rate * 0.625):
                with self.subTest(node=node_type, frequency=frequency):
                    with self.assertRaisesRegex(ValueError, "Nyquist"):
                        self.run_node(node_type, capability, frequency, audio)
                    with self.assertRaisesRegex(ValueError, "Nyquist"):
                        self.runtime.run(f"pymss.spectral.{capability}", inputs={"audio": audio},
                                         params={"f0": frequency})

    def test_valid_node_and_operation_filters_match(self):
        for node_type, capability, _, sample_rate in FILTERS:
            audio = self.audio(sample_rate)
            original = audio.audio.copy()
            frequency = sample_rate / 4
            params = {"f0": frequency, "Q": 0.707}
            if capability == "peak_eq":
                params["gain_db"] = 6.0
            with self.subTest(node=node_type):
                node_output = self.run_node(node_type, capability, frequency, audio)
                operation_output = self.runtime.run(
                    f"pymss.spectral.{capability}", inputs={"audio": audio}, params=params,
                )["audio"]
                self.assertEqual(node_output.audio.shape, original.shape)
                self.assertTrue(np.isfinite(node_output.audio).all())
                np.testing.assert_allclose(node_output.audio, operation_output.audio)
                np.testing.assert_array_equal(audio.audio, original)

    def test_capabilities_reject_invalid_frequency_sample_rate_and_quality(self):
        audio = self.audio(32000).audio
        for _, capability, parameter, _ in FILTERS:
            fn = require_capability(capability)
            base = {parameter: 1000.0, "Q": 0.707}
            if capability == "peak_eq":
                base["gain_db"] = 6.0
            for frequency in (0.0, -1.0, 16000.0, 20000.0, float("nan"), float("inf")):
                with self.subTest(capability=capability, frequency=frequency):
                    with self.assertRaisesRegex(ValueError, "Nyquist"):
                        fn(audio, 32000, **{**base, parameter: frequency})
            for sample_rate in (0.0, -1.0, float("nan"), float("inf")):
                with self.subTest(capability=capability, sample_rate=sample_rate):
                    with self.assertRaisesRegex(ValueError, "sample rate"):
                        fn(audio, sample_rate, **base)
            for quality in (0.0, -1.0, float("nan"), float("inf")):
                with self.subTest(capability=capability, quality=quality):
                    with self.assertRaisesRegex(ValueError, "Filter Q"):
                        fn(audio, 32000, **{**base, "Q": quality})

    def test_peak_capability_rejects_nonfinite_gain(self):
        peak_eq = require_capability("peak_eq")
        for gain in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(gain=gain):
                with self.assertRaisesRegex(ValueError, "gain"):
                    peak_eq(self.audio(32000).audio, 32000, center_hz=1000, gain_db=gain)


if __name__ == "__main__":
    unittest.main()
