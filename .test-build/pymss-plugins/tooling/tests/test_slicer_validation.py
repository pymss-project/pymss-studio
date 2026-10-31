from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from plugins.slicer.backend import slicer


class SlicerValidationTests(unittest.TestCase):
    def test_invalid_numeric_parameters_fail_before_filesystem_or_audio_work(self):
        fields = ("threshold", "minLength", "minInterval", "hopSize", "maxSilKept")
        for field in fields:
            for value in (None, True, False, float("nan"), float("inf"), -float("inf"), "10", [], 10 ** 1000):
                with self.subTest(field=field, value=repr(value)[:60]), \
                        mock.patch.object(slicer, "_require_file") as require_file, \
                        mock.patch.object(slicer, "_require_directory") as require_directory:
                    with self.assertRaisesRegex(ValueError, field):
                        slicer._slice_audio({"inputs": ["input.wav"], "outputDir": "output", field: value})
                    require_file.assert_not_called()
                    require_directory.assert_not_called()

    def test_timing_parameters_do_not_silently_truncate_fractional_values(self):
        for field in ("minLength", "minInterval", "hopSize", "maxSilKept"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "integer"):
                slicer._slice_audio({"inputs": ["input.wav"], field: 20.5})

    def test_missing_parameters_keep_defaults_and_fractional_threshold_is_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.wav"
            source.write_bytes(b"audio")
            for extra, threshold in (({}, -40.0), ({"threshold": -40.5}, -40.5)):
                with self.subTest(extra=extra), \
                        mock.patch.object(slicer, "_slice_intervals", return_value=[(0, 1)]) as analyze, \
                        mock.patch.object(slicer, "_render_segment", side_effect=lambda _, path, *_args: path.write_bytes(b"audio")), \
                        mock.patch.object(slicer, "emit"):
                    result = slicer._slice_audio({"inputs": [str(source)], "outputDir": str(root / "output"), **extra})
                    self.assertEqual(result["succeeded"], 1)
                    self.assertEqual(analyze.call_args.kwargs, {
                        "threshold": threshold, "min_length": 5000, "min_interval": 300,
                        "hop_size": 10, "max_sil_kept": 500,
                    })


if __name__ == "__main__":
    unittest.main()
