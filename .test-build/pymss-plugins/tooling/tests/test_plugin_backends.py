from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock


from plugins.asr.backend import asr
from plugins.inspect.backend import inspect
from plugins.loudnorm.backend import loudness
from plugins.slicer.backend import slicer


class AudioToolModuleTests(unittest.TestCase):

    def test_apple_silicon_asr_uses_mlx_without_importing_torch_and_writes_txt_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "voice.wav"
            source.touch()
            with (
                mock.patch("plugins.asr.backend.asr_mlx.is_apple_silicon", return_value=True),
                mock.patch("plugins.asr.backend.asr_mlx.transcribe", return_value={"text": "hello mlx"}) as mlx,
                mock.patch.object(asr, "emit"),
            ):
                result = asr._transcribe_audio({"inputPath": str(source), "outputDir": str(root / "out"), "modelPreset": "paraformer-zh", "outputFormats": ["txt", "json", "srt"]})
            mlx.assert_called_once()
            self.assertEqual(result["backend"], "funasr-mlx")
            self.assertEqual(result["warnings"], ["timestamps_unavailable"])
            self.assertEqual({Path(path).suffix for path in result["outputPaths"]}, {".txt", ".json"})

    # Captured from FFmpeg 8 on a 2 s 440 Hz sine and a 0.2 s silent clip.
    VOLUMEDETECT_STDERR = (
        "[Parsed_volumedetect_0 @ 000001af07e7b3c0] n_samples: 0\n"
        "[Parsed_volumedetect_0 @ 000001af06206a80] n_samples: 88200\n"
        "[Parsed_volumedetect_0 @ 000001af06206a80] mean_volume: -21.1 dB\n"
        "[Parsed_volumedetect_0 @ 000001af06206a80] max_volume: -18.1 dB\n"
        "[Parsed_volumedetect_0 @ 000001af06206a80] histogram_18db: 25600\n"
    )
    LOUDNORM_SILENT_STDERR = (
        "[Parsed_loudnorm_0 @ 00000178e4bf6ac0] \n{\n"
        '\t"input_i" : "-inf",\n\t"input_tp" : "-inf",\n\t"input_lra" : "0.00",\n'
        '\t"input_thresh" : "-70.00",\n\t"output_i" : "-inf",\n\t"output_tp" : "-inf",\n'
        '\t"output_lra" : "0.00",\n\t"output_thresh" : "-70.00",\n'
        '\t"normalization_type" : "linear",\n\t"target_offset" : "inf"\n}\n'
        "size=N/A time=00:00:00.20 bitrate=N/A speed=24.9x\n"
    )

    def test_loudness_parses_real_volumedetect_output(self) -> None:
        with mock.patch.object(loudness, "_run_measurement", return_value=self.VOLUMEDETECT_STDERR):
            self.assertEqual(loudness._volume_stats(Path("in.wav")), (-21.1, -18.1))
        infinite = "[Parsed_volumedetect_0 @ 0] mean_volume: -inf dB\n[Parsed_volumedetect_0 @ 0] max_volume: -inf dB\n"
        with mock.patch.object(loudness, "_run_measurement", return_value=infinite):
            mean, peak = loudness._volume_stats(Path("in.wav"))
        self.assertEqual((mean, peak), (float("-inf"), float("-inf")))

    def test_loudness_parses_real_loudnorm_output_with_infinite_values(self) -> None:
        # An earlier JSON-looking block must not win over the final summary.
        stderr = '{\n\t"input_i" : "-12.00"\n}\n' + self.LOUDNORM_SILENT_STDERR
        with mock.patch.object(loudness, "_run_measurement", return_value=stderr):
            stats = loudness._loudnorm_stats(Path("in.wav"))
        self.assertEqual(stats["input_i"], float("-inf"))
        self.assertEqual(stats["input_thresh"], -70.0)
        self.assertEqual(stats["target_offset"], float("inf"))

    def test_loudness_limiter_flag_accepts_string_spellings(self) -> None:
        self.assertTrue(loudness._flag(None, "useLimiter", default=True))
        self.assertFalse(loudness._flag("false", "useLimiter", default=True))
        self.assertFalse(loudness._flag(0, "useLimiter", default=True))
        self.assertTrue(loudness._flag("On", "useLimiter", default=False))
        for value in ("maybe", float("nan"), 2):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "useLimiter"):
                loudness._flag(value, "useLimiter", default=True)

    def test_loudness_reports_target_deviation_after_limiting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "source.wav"
            source.touch()
            with (
                mock.patch.object(loudness, "_volume_stats", side_effect=[(-20.0, -17.0), (-12.2, -9.2)]),
                mock.patch.object(loudness, "_loudnorm_stats", return_value={"input_tp": -9.2}),
                self._loudness_format(),
                mock.patch.object(loudness, "_run_ffmpeg", side_effect=lambda arguments: Path(arguments[-1]).write_bytes(b"audio")),
                mock.patch.object(loudness, "emit"),
            ):
                result = loudness._loudness_audio({
                    "inputs": [str(source)], "outputDir": str(root / "output"),
                    "measurement": "rms", "target": -5, "maxTruePeak": -9,
                    "useLimiter": True, "tolerance": 0.5,
                })
            self.assertEqual(result["succeeded"], 1)
            self.assertEqual(result["warnings"], ["loudness_target_not_reached"])
            self.assertEqual(result["measurements"], [{
                "path": result["outputPaths"][0], "inputLevel": -20.0,
                "outputLevel": -12.2, "target": -5.0, "unit": "dB", "withinTolerance": False,
                "outputTruePeak": -9.2, "maxTruePeak": -9, "withinTruePeakLimit": True,
            }])
            self.assertTrue(Path(result["outputPaths"][0]).is_file())

    def test_loudness_limiter_peak_warnings_are_independent_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            sources = [root / "first.wav", root / "second.wav"]
            for source in sources:
                source.touch()
            with (
                mock.patch.object(loudness, "_volume_stats", side_effect=[(-6.5, -3.9), (-6.3, -3.9), (-12, -8), (-10, -4)]),
                mock.patch.object(loudness, "_loudnorm_stats", side_effect=[{"input_tp": -2.66}, {"input_tp": -2}]),
                self._loudness_format(),
                mock.patch.object(loudness, "_run_ffmpeg", side_effect=lambda arguments: Path(arguments[-1]).write_bytes(b"audio")),
                mock.patch.object(loudness, "emit"),
            ):
                result = loudness._loudness_audio({
                    "inputs": [str(source) for source in sources], "outputDir": str(root / "output"),
                    "measurement": "rms", "target": -5, "maxTruePeak": -3, "tolerance": 2, "useLimiter": True,
                })
            self.assertEqual(result["succeeded"], 2)
            first, second = result["measurements"]
            self.assertTrue(first["withinTolerance"])
            self.assertFalse(first["withinTruePeakLimit"])
            self.assertEqual(first["outputTruePeak"], -2.66)
            self.assertEqual(first["maxTruePeak"], -3)
            self.assertFalse(second["withinTolerance"])
            self.assertFalse(second["withinTruePeakLimit"])
            self.assertEqual(result["warnings"], ["loudness_target_not_reached", "loudness_true_peak_exceeded"])

    def test_loudness_limiter_handles_silent_and_invalid_peak_measurements(self) -> None:
        for peak in (float("-inf"), float("inf"), float("nan")):
            with self.subTest(peak=peak), tempfile.TemporaryDirectory() as temp_value:
                root = Path(temp_value)
                source = root / "source.wav"
                source.touch()
                output = root / "output"
                with (
                    mock.patch.object(loudness, "_volume_stats", side_effect=[(-18, -15), (-91, -91)]),
                    mock.patch.object(loudness, "_loudnorm_stats", return_value={"input_tp": peak}),
                    self._loudness_format(),
                    mock.patch.object(loudness, "_run_ffmpeg", side_effect=lambda arguments: Path(arguments[-1]).write_bytes(b"audio")),
                    mock.patch.object(loudness, "emit"),
                ):
                    payload = {"inputs": [str(source)], "outputDir": str(output), "measurement": "rms", "target": -70, "useLimiter": True}
                    if peak == float("-inf"):
                        result = loudness._loudness_audio(payload)
                        self.assertIsNone(result["measurements"][0]["outputTruePeak"])
                        self.assertTrue(result["measurements"][0]["withinTruePeakLimit"])
                        json.dumps(result, allow_nan=False)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "true peak could not be measured"):
                            loudness._loudness_audio(payload)
                        self.assertEqual(list(output.iterdir()), [])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is not available")
    def test_loudness_reports_encoded_mp3_peak_exceeding_limiter_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "square.mp3"
            subprocess.run([
                loudness._ffmpeg_path(), "-nostdin", "-loglevel", "error", "-f", "lavfi",
                "-i", "aevalsrc=0.5*sgn(sin(2*PI*997*t)):s=44100:d=2", "-c:a", "libmp3lame", "-b:a", "128k", str(source),
            ], check=True, capture_output=True)
            with mock.patch.object(loudness, "emit"):
                result = loudness._loudness_audio({
                    "inputs": [str(source)], "outputDir": str(root / "output"),
                    "measurement": "rms", "target": -5, "maxTruePeak": -3, "tolerance": 2, "useLimiter": True,
                })
            measurement = result["measurements"][0]
            actual_peak = loudness._loudnorm_stats(Path(result["outputPaths"][0]))["input_tp"]
            self.assertEqual(measurement["outputTruePeak"], actual_peak)
            self.assertGreater(actual_peak, -3)
            self.assertTrue(measurement["withinTolerance"])
            self.assertFalse(measurement["withinTruePeakLimit"])
            self.assertEqual(result["warnings"], ["loudness_true_peak_exceeded"])

    def test_loudness_reports_measured_output_within_tolerance_without_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "source.wav"
            source.touch()
            with (
                mock.patch.object(loudness, "_volume_stats", side_effect=[(-20.0, -17.0), (-23.2, -20.2)]),
                mock.patch.object(loudness, "_loudnorm_stats", return_value={"input_tp": -20.2}),
                self._loudness_format(),
                mock.patch.object(loudness, "_run_ffmpeg", side_effect=lambda arguments: Path(arguments[-1]).write_bytes(b"audio")),
                mock.patch.object(loudness, "emit"),
            ):
                result = loudness._loudness_audio({
                    "inputs": [str(source)], "outputDir": str(root / "output"),
                    "measurement": "rms", "target": -23, "useLimiter": True,
                })
            self.assertEqual(result["warnings"], [])
            self.assertTrue(result["measurements"][0]["withinTolerance"])
            self.assertEqual(result["measurements"][0]["outputLevel"], -23.2)

    def test_loudness_output_measurement_failure_removes_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "source.wav"
            source.touch()
            output = root / "output"
            with (
                mock.patch.object(loudness, "_volume_stats", side_effect=[(-20.0, -17.0), RuntimeError("output probe failed")]),
                mock.patch.object(loudness, "_loudnorm_stats", return_value={"input_tp": -20}),
                self._loudness_format(),
                mock.patch.object(loudness, "_run_ffmpeg", side_effect=lambda arguments: Path(arguments[-1]).write_bytes(b"audio")),
                mock.patch.object(loudness, "emit"),
                self.assertRaisesRegex(RuntimeError, "output probe failed"),
            ):
                loudness._loudness_audio({
                    "inputs": [str(source)], "outputDir": str(output), "measurement": "rms",
                })
            self.assertEqual(list(output.iterdir()), [])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is not available")
    def test_loudness_real_limited_output_reports_its_actual_level(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "source.wav"
            subprocess.run([
                loudness._ffmpeg_path(), "-nostdin", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2",
                "-c:a", "pcm_s16le", str(source),
            ], check=True, capture_output=True)
            with mock.patch.object(loudness, "emit"):
                result = loudness._loudness_audio({
                    "inputs": [str(source)], "outputDir": str(root / "output"),
                    "measurement": "rms", "target": -5, "maxTruePeak": -9,
                    "useLimiter": True, "tolerance": 0.5,
                })
            measurement = result["measurements"][0]
            self.assertEqual(result["warnings"], ["loudness_target_not_reached"])
            self.assertFalse(measurement["withinTolerance"])
            self.assertGreater(abs(measurement["outputLevel"] - result["target"]), 4)
            actual_level, actual_peak = loudness._volume_stats(Path(result["outputPaths"][0]))
            self.assertEqual(measurement["outputLevel"], actual_level)
            self.assertLessEqual(actual_peak, -9)
            json.dumps(result, allow_nan=False)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is not available")
    def test_loudness_peak_modes_measure_the_encoded_mp3_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "input.mp3"
            subprocess.run([
                loudness._ffmpeg_path(), "-nostdin", "-loglevel", "error", "-f", "lavfi",
                "-i", "sine=frequency=997:sample_rate=44100:duration=3", "-af", "volume=-12dB",
                "-c:a", "libmp3lame", "-b:a", "128k", str(source),
            ], check=True, capture_output=True)
            for mode, unit in (("peak", "dB"), ("true-peak", "dBTP")):
                with self.subTest(mode=mode), mock.patch.object(loudness, "emit"):
                    result = loudness._loudness_audio({
                        "inputs": [str(source)], "outputDir": str(root / mode),
                        "measurement": mode, "target": -1, "tolerance": 0.1, "useLimiter": False,
                    })
                    output = Path(result["outputPaths"][0])
                    actual = loudness._volume_stats(output)[1] if mode == "peak" else loudness._loudnorm_stats(output)["input_tp"]
                    self.assertEqual(result["measurements"][0]["outputLevel"], actual)
                    self.assertEqual(result["measurements"][0]["unit"], unit)
                    self.assertGreater(abs(actual + 1), 0.1)
                    self.assertFalse(result["measurements"][0]["withinTolerance"])
                    self.assertEqual(result["warnings"], ["loudness_target_not_reached"])
                    self.assertNotIn("processed", result)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is not available")
    def test_loudness_selects_first_audio_stream_for_rendering_and_tolerance_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "multi.m4a"
            subprocess.run([
                loudness._ffmpeg_path(), "-nostdin", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=400:sample_rate=44100:duration=2",
                "-f", "lavfi", "-i", "sine=frequency=800:sample_rate=48000:duration=2",
                "-filter_complex", "[1:a]aformat=channel_layouts=stereo[a1]", "-map", "0:a", "-map", "[a1]",
                "-c:a", "aac", "-disposition:a:0", "0", "-disposition:a:1", "default", str(source),
            ], check=True, capture_output=True)
            for name, target in (("render", -30), ("copy", loudness._volume_stats(source)[0])):
                with self.subTest(path=name), mock.patch.object(loudness, "emit"):
                    result = loudness._loudness_audio({
                        "inputs": [str(source)], "outputDir": str(root / name),
                        "measurement": "rms", "target": target, "tolerance": 0.5, "useLimiter": False,
                    })
                    probe = subprocess.run([
                        loudness.ffprobe_path(), "-v", "error", "-select_streams", "a", "-show_entries",
                        "stream=sample_rate,channels", "-of", "json", result["outputPaths"][0],
                    ], check=True, capture_output=True, text=True)
                    self.assertEqual(json.loads(probe.stdout)["streams"], [{"sample_rate": "44100", "channels": 1}])

    def test_loudness_peak_tolerance_can_preserve_an_already_matching_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            source = Path(temp_value) / "source.wav"
            source.write_bytes(b"source")
            with (
                mock.patch.object(loudness, "_volume_stats", return_value=(-4.05, -1.05)) as stats,
                self._loudness_format(),
                mock.patch.object(loudness, "_run_ffmpeg") as render,
                mock.patch.object(loudness, "emit"),
            ):
                result = loudness._loudness_audio({
                    "inputs": [str(source)], "outputDir": str(source.parent / "output"),
                    "measurement": "peak", "target": -1, "tolerance": 0.1, "useLimiter": False,
                })
                self.assertTrue(result["measurements"][0]["withinTolerance"])
                self.assertEqual(result["measurements"][0]["outputLevel"], -1.05)
                self.assertEqual(Path(result["outputPaths"][0]).read_bytes(), b"source")
                self.assertEqual(stats.call_count, 1)
                render.assert_not_called()

    def test_loudness_bitrate_falls_back_to_container_level(self) -> None:
        completed = mock.Mock(stdout=json.dumps({
            "streams": [{"codec_name": "opus", "sample_rate": "48000"}],
            "format": {"bit_rate": "2400000"},
        }))
        with (
            mock.patch.object(loudness, "ffprobe_path", return_value="ffprobe"),
            mock.patch.object(loudness, "run_process", return_value=completed) as run_process,
        ):
            audio_format = loudness._audio_format(Path("in.opus"))
        # Container rate includes cover art, so the fallback is capped.
        self.assertEqual(audio_format["bitRate"], loudness.MAX_FALLBACK_BIT_RATE)
        command = run_process.call_args.args[0]
        self.assertIn("format=bit_rate", command[command.index("-show_entries") + 1])

    def _loudness_format(self):
        return mock.patch.object(loudness, "_audio_format", return_value={"codec": "pcm_s24le", "sampleRate": 48000, "bits": 24, "audioStreamCount": 1})

    def test_loudness_applies_linear_gain_with_oversampled_limiter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "source.wav"
            source.touch()
            output = root / "output"
            output.mkdir()

            def render(arguments):
                Path(arguments[-1]).write_bytes(b"rendered")

            with (
                mock.patch.object(loudness, "_loudnorm_stats", side_effect=[{
                    "input_i": -18.0, "input_tp": -2.0, "input_lra": 4.0,
                    "input_thresh": -28.0, "target_offset": -5.0,
                }, {"input_i": -23.1, "input_tp": -7}]) as stats,
                self._loudness_format(),
                mock.patch.object(loudness, "_run_ffmpeg", side_effect=render) as run_ffmpeg,
                mock.patch.object(loudness, "emit"),
            ):
                result = loudness._loudness_audio({
                    "inputs": [str(source)], "outputDir": str(output),
                    "measurement": "ebu-r128", "target": -23,
                    "maxTruePeak": -1, "useLimiter": True,
                    "lookahead": 12, "release": 200,
                })

            arguments = run_ffmpeg.call_args.args[0]
            filters = arguments[arguments.index("-af") + 1]
            self.assertEqual(result["succeeded"], 1)
            self.assertEqual(result["measurement"], "itu-bs1770-3")
            self.assertEqual(stats.call_args_list[0], mock.call(source.resolve()))
            self.assertEqual(stats.call_count, 2)
            self.assertEqual(result["measurements"][0]["outputLevel"], -23.1)
            self.assertEqual(result["measurements"][0]["unit"], "LUFS")
            self.assertEqual(result["measurements"][0]["outputTruePeak"], -7)
            self.assertTrue(result["measurements"][0]["withinTruePeakLimit"])
            self.assertEqual(result["warnings"], [])
            self.assertIn("volume=-5.00000000dB", filters)
            self.assertNotIn("loudnorm", filters)
            self.assertIn("aresample=192000,alimiter=", filters)
            self.assertIn("level_out=0.870963", filters)
            self.assertTrue(filters.endswith("aresample=48000"))
            self.assertEqual(arguments[arguments.index("-c:a") + 1], "pcm_s24le")
            self.assertEqual(arguments[arguments.index("-ar") + 1], "48000")
            self.assertEqual(Path(result["outputPaths"][0]).read_bytes(), b"rendered")
            self.assertEqual([path.name for path in output.iterdir()], ["source_loudness.wav"])

    def test_loudness_encoder_keeps_source_codec_quality(self) -> None:
        cases = {
            "alac": ["-c:a", "alac", "-sample_fmt", "s32p"],
            "wavpack": ["-c:a", "wavpack"],
            "mp3": ["-b:a", "320000"],
        }
        for codec, expected in cases.items():
            with self.subTest(codec=codec):
                arguments = loudness._encoder_arguments({"codec": codec, "sampleRate": 44100, "bits": 24, "bitRate": 320000})
                self.assertEqual(arguments[:len(expected)], expected)
                self.assertEqual(arguments[-2:], ["-ar", "44100"])

    def test_loudness_rejects_targets_outside_ffmpeg_range(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            source = Path(temp_value) / "source.wav"
            source.touch()
            for overrides in ({"target": -3}, {"target": -80}, {"maxTruePeak": -12}, {"lookahead": 500}):
                with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                    loudness._loudness_audio({
                        "inputs": [str(source)], "outputDir": temp_value,
                        "measurement": "itu-bs1770-3", "target": -23, **overrides,
                    })

    def test_loudness_fails_silent_input_instead_of_boosting_noise_floor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            silent = root / "silent.wav"
            silent.touch()
            output = root / "output"
            with (
                mock.patch.object(loudness, "_volume_stats", return_value=(-91.0, -91.0)),
                mock.patch.object(loudness, "_run_ffmpeg") as run_ffmpeg,
                mock.patch.object(loudness, "emit"),
            ):
                for measurement in ("rms", "peak"):
                    with self.subTest(measurement=measurement), self.assertRaisesRegex(RuntimeError, "silent"):
                        loudness._loudness_audio({
                            "inputs": [str(silent)], "outputDir": str(output),
                            "measurement": measurement, "target": -1 if measurement == "peak" else -23,
                            "useLimiter": False,
                        })
            run_ffmpeg.assert_not_called()

    def test_loudness_removes_partial_output_on_failure_and_stale_partials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "source.wav"
            source.touch()
            output = root / "output"
            output.mkdir()
            (output / ".pymss-loudness-1-1.wav").write_bytes(b"stale")

            def render(arguments):
                Path(arguments[-1]).write_bytes(b"partial")
                raise RuntimeError("encoder failed")

            with (
                mock.patch.object(loudness, "_volume_stats", return_value=(-30.0, -10.0)),
                self._loudness_format(),
                mock.patch.object(loudness, "_run_ffmpeg", side_effect=render),
                mock.patch.object(loudness, "emit"),
                self.assertRaisesRegex(RuntimeError, "encoder failed"),
            ):
                loudness._loudness_audio({
                    "inputs": [str(source)], "outputDir": str(output),
                    "measurement": "rms", "target": -23, "useLimiter": False,
                })

            self.assertEqual(list(output.iterdir()), [])

    def test_loudness_rms_match_uses_reference_level_and_preserves_tolerance_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "source.wav"
            reference = root / "reference.wav"
            source.write_bytes(b"source")
            reference.write_bytes(b"reference")
            output = root / "output"
            with (
                mock.patch.object(loudness, "_volume_stats", side_effect=[(-20.0, -3.0), (-20.0, -3.0)]) as volume_stats,
                self._loudness_format(),
                mock.patch.object(loudness, "_run_ffmpeg") as run_ffmpeg,
                mock.patch.object(loudness, "emit"),
            ):
                result = loudness._loudness_audio({
                    "inputs": [str(source)], "outputDir": str(output),
                    "measurement": "rms-match-file", "tolerance": 0.5,
                    "useLimiter": False, "referencePath": str(reference),
                })

            output_path = Path(result["outputPaths"][0])
            self.assertEqual(result["target"], -20.0)
            self.assertEqual(output_path.read_bytes(), b"source")
            self.assertEqual(volume_stats.call_count, 2)
            self.assertEqual(result["measurements"][0]["outputLevel"], -20.0)
            self.assertTrue(result["measurements"][0]["withinTolerance"])
            run_ffmpeg.assert_not_called()

    def test_loudness_short_gated_clip_falls_back_to_rms_gain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "short.wav"
            source.touch()
            output = root / "output"

            def render(arguments):
                Path(arguments[-1]).write_bytes(b"rendered")

            with (
                mock.patch.object(loudness, "_loudnorm_stats", return_value={
                    "input_i": float("-inf"), "input_tp": -18.0, "input_lra": 0.0,
                    "input_thresh": -70.0, "target_offset": float("inf"),
                }),
                mock.patch.object(loudness, "_volume_stats", side_effect=[(-18.0, -2.0), (-23.0, -7.0)]),
                self._loudness_format(),
                mock.patch.object(loudness, "_run_ffmpeg", side_effect=render) as run_ffmpeg,
                mock.patch.object(loudness, "emit"),
            ):
                result = loudness._loudness_audio({
                    "inputs": [str(source)], "outputDir": str(output),
                    "measurement": "itu-bs1770-3", "target": -23,
                    "useLimiter": False,
                })

        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["measurements"][0]["unit"], "dB")
        self.assertEqual(result["measurements"][0]["outputLevel"], -23.0)
        self.assertEqual(result["warnings"], [])
        self.assertIn("volume=-5.00000000dB", run_ffmpeg.call_args.args[0])

    def test_inspect_normalizes_ffprobe_audio_streams(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            path = Path(temp_value) / "sample.wav"
            path.write_bytes(b"data")
            payload = {
                "format": {"format_name": "wav", "duration": "1.25", "bit_rate": "1536000", "tags": {"title": "Sample"}},
                "streams": [{"index": 0, "codec_type": "audio", "codec_name": "pcm_s24le", "sample_rate": "48000", "channels": 2, "channel_layout": "stereo"}],
                "chapters": [],
            }
            completed = mock.Mock(stdout=json.dumps(payload))
            with (
                mock.patch.object(inspect, "ffprobe_path", return_value="ffprobe"),
                mock.patch.object(inspect, "run_process", return_value=completed),
                mock.patch.object(inspect, "emit"),
            ):
                result = inspect._inspect_audio({"inputPath": str(path)})
        self.assertEqual(result["format"]["duration"], 1.25)
        self.assertEqual(result["audioStreams"][0]["sampleRate"], 48000)
        self.assertEqual(result["format"]["tags"]["title"], "Sample")

    def test_slicer_writes_non_overwriting_segment_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "source.wav"
            source.touch()
            output = root / "output"
            output.mkdir()
            with (
                mock.patch.object(slicer, "_slice_intervals", return_value=[(0.5, 1.5), (2.0, 3.0)]),
                mock.patch.object(slicer, "_render_segment", side_effect=lambda source, target, *args: target.write_bytes(b'audio')) as render,
                mock.patch.object(slicer, "emit"),
            ):
                result = slicer._slice_audio({"inputs": [str(source)], "outputDir": str(output)})
        self.assertEqual(render.call_count, 2)
        self.assertEqual(result["succeeded"], 2)
        self.assertAlmostEqual(result["keptDuration"], 2.0)
        self.assertIn("00000500-00001500", Path(result["segments"][0]["outputPath"]).name)

    def test_slicer_releases_each_analysis_file_before_processing_the_next_input(self) -> None:
        analysis_paths: list[Path] = []

        def analyze(_: Path, raw_path: Path, **__: object) -> list[tuple[float, float]]:
            if analysis_paths:
                self.assertFalse(analysis_paths[-1].exists())
            raw_path.write_bytes(b"decoded-pcm")
            analysis_paths.append(raw_path)
            return [(0.0, 1.0)]

        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            sources = [root / "first.wav", root / "second.wav"]
            for source in sources:
                source.touch()
            output = root / "output"
            output.mkdir()
            with (
                mock.patch.object(slicer, "_slice_intervals", side_effect=analyze),
                mock.patch.object(slicer, "_render_segment", side_effect=lambda source, target, *args: target.write_bytes(b'audio')),
                mock.patch.object(slicer, "emit"),
            ):
                result = slicer._slice_audio({
                    "inputs": [str(source) for source in sources],
                    "outputDir": str(output),
                })

        self.assertEqual(result["sourceCount"], 2)
        self.assertEqual(len(analysis_paths), 2)

    def test_asr_writes_text_json_and_srt(self) -> None:
        class FakeModel:
            def __init__(self, **_: object) -> None:
                pass
            def generate(self, **_: object):
                return [{"text": "测试文本", "sentence_info": [{"start": 0, "end": 1250, "text": "测试文本"}]}]

        module = types.ModuleType("funasr")
        module.AutoModel = FakeModel  # type: ignore[attr-defined]
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "speech.wav"
            source.touch()
            output = root / "output"
            with mock.patch.dict(sys.modules, {"funasr": module}), mock.patch.object(asr, "emit"):
                result = asr._transcribe_audio({
                    "inputPath": str(source), "outputDir": str(output),
                    "outputFormats": ["txt", "json", "srt"], "allowDownload": True,
                })
            paths = [Path(value) for value in result["outputPaths"]]
            self.assertEqual({path.suffix for path in paths}, {".txt", ".json", ".srt"})
            self.assertTrue(all(path.exists() for path in paths))
            self.assertIn("00:00:01,250", next(path for path in paths if path.suffix == ".srt").read_text(encoding="utf-8"))

    def test_asr_allows_local_main_model_with_official_auxiliary_models(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            model = Path(temp_value) / "asr-model"
            model.mkdir()
            resolved = asr._resolve_models({"modelPath": str(model), "allowDownload": True})

        self.assertEqual(resolved[0], str(model.resolve()))
        self.assertEqual(resolved[1:], (asr.DEFAULT_VAD_MODEL, asr.DEFAULT_PUNC_MODEL))

    def test_asr_requires_every_local_model_when_download_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            model = Path(temp_value) / "asr-model"
            model.mkdir()
            with self.assertRaisesRegex(ValueError, "select ASR, VAD, and punctuation"):
                asr._resolve_models({"modelPath": str(model), "allowDownload": False})

    def test_asr_cache_uses_the_configured_model_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            configured_root = Path(temp_value) / "configured-models"
            default_root = Path(temp_value) / "default-models"
            with mock.patch.dict("os.environ", {
                "PYMSS_MODEL_DIR": str(default_root),
                "MODELSCOPE_CACHE": str(Path(temp_value) / "global-cache"),
            }):
                cache_dir = asr._configure_model_cache({"modelDir": str(configured_root)})

                self.assertEqual(cache_dir, configured_root / "_tool_models" / "asr")
                self.assertEqual(os.environ["MODELSCOPE_CACHE"], str(cache_dir))

    def test_asr_cache_falls_back_to_the_worker_default_model_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            default_root = Path(temp_value) / "default-models"
            with mock.patch.dict("os.environ", {"PYMSS_MODEL_DIR": str(default_root)}, clear=True):
                cache_dir = asr._configure_model_cache({})

                self.assertEqual(cache_dir, default_root / "_tool_models" / "asr")
                self.assertEqual(os.environ["MODELSCOPE_CACHE"], str(cache_dir))

    def test_asr_resolves_multilingual_profile_and_validates_language(self) -> None:
        configuration = asr._resolve_model_configuration({
            "modelPreset": "fun-asr-mlt-nano",
            "language": "pt",
        })

        self.assertEqual(configuration["model"], "FunAudioLLM/Fun-ASR-MLT-Nano-2512")
        self.assertEqual(configuration["language"], "pt")
        self.assertIsNone(configuration["punc_model"])
        self.assertTrue(configuration["trust_remote_code"])
        with self.assertRaisesRegex(ValueError, "not supported"):
            asr._resolve_model_configuration({"modelPreset": "paraformer-en", "language": "zh"})

    def test_asr_nano_uses_language_name_and_hotword_list(self) -> None:
        configuration = asr._resolve_model_configuration({
            "modelPreset": "fun-asr-nano",
            "language": "zh",
        })
        options = asr._generate_options(configuration, Path("speech.wav"), "张三，产品名")

        self.assertEqual(options["language"], "中文")
        self.assertEqual(options["hotwords"], ["张三", "产品名"])
        self.assertEqual(options["batch_size"], 1)
        self.assertNotIn("sentence_timestamp", options)

    def test_asr_nano_configures_model_and_returns_profile_metadata(self) -> None:
        model_options: dict[str, object] = {}
        generate_options: dict[str, object] = {}

        class FakeModel:
            def __init__(self, **kwargs: object) -> None:
                model_options.update(kwargs)

            def generate(self, **kwargs: object):
                generate_options.update(kwargs)
                return [{"text": "识别结果", "language": "中文"}]

        module = types.ModuleType("funasr")
        module.AutoModel = FakeModel  # type: ignore[attr-defined]
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "speech.wav"
            source.touch()
            with (
                mock.patch.dict(sys.modules, {"funasr": module}),
                mock.patch.object(asr, "_select_device", return_value="cpu"),
                mock.patch.object(asr, "emit"),
            ):
                result = asr._transcribe_audio({
                    "inputPath": str(source),
                    "outputDir": str(root / "output"),
                    "outputFormats": ["txt"],
                    "modelPreset": "fun-asr-nano",
                    "language": "zh",
                    "hotword": "张三，项目名",
                })

        self.assertEqual(model_options["model"], "FunAudioLLM/Fun-ASR-Nano-2512")
        self.assertEqual(model_options["vad_model"], asr.DEFAULT_VAD_MODEL)
        self.assertNotIn("punc_model", model_options)
        self.assertTrue(model_options["trust_remote_code"])
        self.assertEqual(generate_options["language"], "中文")
        self.assertEqual(generate_options["hotwords"], ["张三", "项目名"])
        self.assertEqual(result["modelPreset"], "fun-asr-nano")
        self.assertEqual(result["detectedLanguage"], "zh")

    def test_asr_sensevoice_normalizes_rich_sentence_text(self) -> None:
        postprocess = types.ModuleType("funasr.utils.postprocess_utils")
        postprocess.rich_transcription_postprocess = lambda value: value.replace("<|zh|>", "")  # type: ignore[attr-defined]
        utils = types.ModuleType("funasr.utils")
        with mock.patch.dict(sys.modules, {
            "funasr.utils": utils,
            "funasr.utils.postprocess_utils": postprocess,
        }):
            entries = asr._sentence_entries([{
                "sentence_info": [{"start": 0, "end": 900, "sentence": "<|zh|>测试"}],
            }], "测试", "rich")

        self.assertEqual(entries, [{"start": 0.0, "end": 0.9, "text": "测试"}])

    def test_asr_model_download_output_emits_deduplicated_progress(self) -> None:
        stderr = io.StringIO()
        output = asr._ModelLoadOutput(stderr)
        first = "\rmodel.pt: 20%|####      | 197M/990M [00:07<00:27, 28.8MB/s]"
        repeated = "\rmodel.pt: 20%|####      | 200M/990M [00:07<00:27, 28.9MB/s]"
        second = "\rmodel.pt: 21%|####      | 203M/990M [00:08<00:27, 28.6MB/s]"

        with mock.patch.object(asr, "emit") as worker_emit:
            output.write(first)
            output.write(repeated)
            output.write(second)
            output.write("model cache warning\n")

        self.assertEqual(worker_emit.call_count, 2)
        worker_emit.assert_any_call("audio_tool_progress", {
            "operation": "asr",
            "phase": "loading_asr_model",
            "completed": 20,
            "total": 100,
            "current": "model.pt",
            "detail": "197M / 990M · 28.8MB/s · ETA 00:27",
        })
        self.assertEqual(stderr.getvalue(), "model cache warning\n")

    def test_asr_rejects_incomplete_download_before_generating_outputs(self) -> None:
        model_id = "iic--punc_ct-transformer_cn-en-common-vocab471067-large"

        class FakeModel:
            def __init__(self, **_: object) -> None:
                print(
                    "Downloading 10 files from "
                    "iic/punc_ct-transformer_cn-en-common-vocab471067-large@master",
                    file=sys.stderr,
                )
                print(
                    "Download failed for model.pt: [Errno 28] No space left on device",
                    file=sys.stderr,
                )

            def generate(self, **_: object):
                raise AssertionError("inference must not start with an incomplete model")

        module = types.ModuleType("funasr")
        module.AutoModel = FakeModel  # type: ignore[attr-defined]
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "speech.wav"
            source.touch()
            model_root = root / "models"
            with (
                mock.patch.dict(sys.modules, {"funasr": module}),
                mock.patch.object(asr, "_select_device", return_value="cpu"),
                mock.patch.object(asr, "emit"),
                redirect_stderr(io.StringIO()),
                self.assertRaises(asr.AudioToolError) as raised,
            ):
                asr._transcribe_audio({
                    "inputPath": str(source),
                    "outputDir": str(root / "output"),
                    "outputFormats": ["txt"],
                    "modelDir": str(model_root),
                    "modelPreset": "paraformer-zh",
                })

            recovery = raised.exception.extra["recovery"]
            self.assertEqual(raised.exception.code, "ASR_MODEL_INCOMPLETE")
            self.assertTrue(raised.exception.recoverable)
            self.assertEqual(recovery["modelIds"], [model_id])
            self.assertEqual(recovery["modelDir"], str(model_root))
            self.assertEqual(recovery["reason"], "disk_full")
            self.assertEqual(list((root / "output").glob("*")), [])

    def test_asr_detects_an_existing_incomplete_selected_model_before_loading(self) -> None:
        class FakeModel:
            def __init__(self, **_: object) -> None:
                raise AssertionError("model loading must not start with an incomplete cache")

        module = types.ModuleType("funasr")
        module.AutoModel = FakeModel  # type: ignore[attr-defined]
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "speech.wav"
            source.touch()
            model_root = root / "models"
            model_id = "iic--punc_ct-transformer_cn-en-common-vocab471067-large"
            incomplete = (
                model_root / "_tool_models" / "asr" / "models" / model_id
                / "snapshots" / "master" / "model.pt.incomplete"
            )
            incomplete.parent.mkdir(parents=True)
            incomplete.touch()
            with (
                mock.patch.dict(sys.modules, {"funasr": module}),
                mock.patch.object(asr, "_select_device", return_value="cpu"),
                mock.patch.object(asr, "emit"),
                self.assertRaises(asr.AudioToolError) as raised,
            ):
                asr._transcribe_audio({
                    "inputPath": str(source),
                    "outputDir": str(root / "output"),
                    "outputFormats": ["txt"],
                    "modelDir": str(model_root),
                    "modelPreset": "paraformer-zh",
                })

        recovery = raised.exception.extra["recovery"]
        self.assertEqual(recovery["modelIds"], [model_id])
        self.assertEqual(recovery["reason"], "incomplete_download")

    def test_asr_detects_a_cache_with_missing_declared_files_before_loading(self) -> None:
        class FakeModel:
            def __init__(self, **_: object) -> None:
                raise AssertionError("model loading must not start with an incomplete cache")

        module = types.ModuleType("funasr")
        module.AutoModel = FakeModel  # type: ignore[attr-defined]
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "speech.wav"
            source.touch()
            model_root = root / "models"
            model_id = "iic--speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
            snapshot = model_root / "_tool_models" / "asr" / "models" / model_id / "snapshots" / "master"
            snapshot.mkdir(parents=True)
            (snapshot / "model.pt").write_bytes(b"weight" * 512)
            (snapshot / "config.yaml").write_bytes(b"config")
            (snapshot / "configuration.json").write_text(json.dumps({
                "file_path_metas": {
                    "init_param": "model.pt",
                    "config": "config.yaml",
                    "tokenizer_conf": {"token_list": "tokens.json"},
                },
            }), encoding="utf-8")
            with (
                mock.patch.dict(sys.modules, {"funasr": module}),
                mock.patch.object(asr, "_select_device", return_value="cpu"),
                mock.patch.object(asr, "emit"),
                self.assertRaises(asr.AudioToolError) as raised,
            ):
                asr._transcribe_audio({
                    "inputPath": str(source),
                    "outputDir": str(root / "output"),
                    "outputFormats": ["txt"],
                    "modelDir": str(model_root),
                    "modelPreset": "paraformer-zh",
                })

        self.assertEqual(raised.exception.extra["recovery"]["modelIds"], [model_id])

    def test_asr_rejects_srt_only_for_a_preset_without_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "speech.wav"
            source.touch()
            with self.assertRaisesRegex(ValueError, "does not provide timestamps"):
                asr._transcribe_audio({
                    "inputPath": str(source),
                    "outputDir": str(root / "output"),
                    "outputFormats": ["srt"],
                    "modelPreset": "fun-asr-nano",
                })
            self.assertEqual(list((root / "output").glob("*")), [])

    def test_asr_does_not_report_success_when_srt_is_the_only_unwritable_output(self) -> None:
        class FakeModel:
            def __init__(self, **_: object) -> None:
                pass

            def generate(self, **_: object):
                return [{"text": "测试文本"}]

        module = types.ModuleType("funasr")
        module.AutoModel = FakeModel  # type: ignore[attr-defined]
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "speech.wav"
            source.touch()
            with (
                mock.patch.dict(sys.modules, {"funasr": module}),
                mock.patch.object(asr, "_select_device", return_value="cpu"),
                mock.patch.object(asr, "emit"),
                self.assertRaisesRegex(RuntimeError, "without a writable output"),
            ):
                asr._transcribe_audio({
                    "inputPath": str(source),
                    "outputDir": str(root / "output"),
                    "outputFormats": ["srt"],
                    "modelPreset": "paraformer-zh",
                })
            self.assertEqual(list((root / "output").glob("*")), [])


if __name__ == "__main__":
    unittest.main()
