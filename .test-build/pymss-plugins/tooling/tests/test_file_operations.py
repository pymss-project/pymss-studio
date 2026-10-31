from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import numpy as np


from pymss.plugins import kit as tool_common
from plugins.merge.backend import merge as tool_merge
from plugins.game.backend import midi as tool_midi
from plugins.sdr.backend import sdr as tool_sdr
from plugins.game.backend.engine.infer import MidiInferenceResult
from plugins.game.backend.engine.midi import Note, build_midi_file


class WorkerToolsTests(unittest.TestCase):




    def test_audio_files_in_folder_is_filtered_and_case_insensitive_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            folder = Path(temp_value)
            (folder / "B.wav").write_bytes(b"")
            (folder / "a.FLAC").write_bytes(b"")
            (folder / "notes.txt").write_text("ignore", encoding="utf-8")

            result = tool_merge._audio_files_in_folder(folder)

        self.assertEqual([path.name for path in result], ["a.FLAC", "B.wav"])

    def test_merge_name_order_uses_natural_numbers(self) -> None:
        inputs = [Path("part10.wav"), Path("part2.wav"), Path("part1.wav")]

        ordered = tool_merge._order_audio_files(inputs, "name", "asc")

        self.assertEqual([path.name for path in ordered], ["part1.wav", "part2.wav", "part10.wav"])

    def test_merge_regex_order_uses_first_capture_and_keeps_unmatched_last(self) -> None:
        inputs = [Path("intro.wav"), Path("take-10.wav"), Path("take-2.wav")]

        ordered = tool_merge._order_audio_files(inputs, "regex", "asc", r"take-(?P<order>\d+)")
        descending = tool_merge._order_audio_files(inputs, "regex", "desc", r"take-(\d+)")

        self.assertEqual([path.name for path in ordered], ["take-2.wav", "take-10.wav", "intro.wav"])
        self.assertEqual([path.name for path in descending], ["take-10.wav", "take-2.wav", "intro.wav"])

    def test_merge_regex_order_rejects_invalid_pattern(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid merge regular expression"):
            tool_merge._order_audio_files([Path("part1.wav")], "regex", "asc", "(")

    def test_merge_operation_applies_requested_regex_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            input_dir = root / "inputs"
            output_dir = root / "outputs"
            input_dir.mkdir()
            for name in ("intro.wav", "take-2.wav", "take-10.wav"):
                (input_dir / name).write_bytes(b"")

            with (
                mock.patch.object(tool_merge, "_run_ffmpeg", side_effect=lambda arguments: Path(arguments[-1]).write_bytes(b'audio')) as run_ffmpeg,
                mock.patch.object(tool_merge, "emit"),
            ):
                result = tool_merge._merge_audio({
                    "inputDir": str(input_dir),
                    "outputDir": str(output_dir),
                    "sortBy": "regex",
                    "sortDirection": "desc",
                    "regexPattern": r"take-(\d+)",
                })

        normalized_names = [Path(call.args[0][1]).name for call in run_ffmpeg.call_args_list[:-1]]
        self.assertEqual(normalized_names, ["take-10.wav", "take-2.wav", "intro.wav"])
        self.assertEqual(result["sortBy"], "regex")
        self.assertEqual(result["sortDirection"], "desc")

    def test_calculate_sdr_arrays_returns_per_channel_and_average_values(self) -> None:
        reference = np.array([[1.0, -1.0, 0.5], [0.5, -0.5, 1.0]])
        estimated = reference * 0.9

        sdr, average_sdr, si_sdr, average_si_sdr = tool_sdr._calculate_sdr_arrays(
            reference,
            estimated,
        )

        self.assertEqual(len(sdr), 2)
        self.assertEqual(len(si_sdr), 2)
        self.assertGreater(average_sdr, 10)
        self.assertGreater(average_si_sdr, average_sdr)

    def test_available_path_does_not_overwrite_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            output = Path(temp_value) / "result.wav"
            output.write_bytes(b"existing")

            candidate = tool_common.available_path(output)

        self.assertEqual(candidate.name, "result_2.wav")

    def test_game_midi_writer_outputs_standard_midi_file(self) -> None:
        midi = build_midi_file([Note(onset=0.0, offset=0.5, pitch=60.0)], tempo=120)
        with tempfile.TemporaryDirectory() as temp_value:
            output = Path(temp_value) / "vocal.mid"
            midi.save(output)
            content = output.read_bytes()

        self.assertTrue(content.startswith(b"MThd"))
        self.assertIn(b"MTrk", content)
        self.assertTrue(content.endswith(b"\x00\xff\x2f\x00"))

    def test_game_midi_writer_preserves_note_time_in_ticks(self) -> None:
        midi = build_midi_file([Note(onset=1.0, offset=1.5, pitch=60.0)], tempo=120)
        self.assertIn(
            b"\x00\xff\x51\x03\x07\xa1\x20"
            b"\x87\x40\x90\x3c\x40"
            b"\x83\x60\x80\x3c\x40",
            midi.track,
        )

    def test_game_midi_writer_removes_temporary_file_after_save_failure(self) -> None:
        midi = build_midi_file([Note(onset=0.0, offset=0.5, pitch=60.0)], tempo=120)
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            output = root / "vocal.mid"
            with (
                mock.patch("plugins.game.backend.engine.midi.os.replace", side_effect=OSError("replace failed")),
                self.assertRaisesRegex(OSError, "replace failed"),
            ):
                midi.save(output)

            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_vocal_to_midi_uses_game_torch_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            input_path = root / "vocal.wav"
            model_path = root / "model.pt"
            output_dir = root / "output"
            output_path = output_dir / "vocal.mid"
            input_path.touch()
            model_path.touch()
            output_dir.mkdir()
            output_path.write_bytes(b"MThd")

            def fake_infer(**kwargs: object) -> MidiInferenceResult:
                print("third-party diagnostic")
                progress_callback = kwargs["progress_callback"]
                assert callable(progress_callback)
                progress_callback("transcribing", 1, 2)
                return MidiInferenceResult(
                    output_path=output_path,
                    note_count=12,
                    input_duration=8.0,
                    first_note_at=0.5,
                    last_note_at=7.5,
                    warnings=(),
                )

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch("plugins.game.backend.engine.infer.infer", side_effect=fake_infer) as game_infer,
                mock.patch.object(tool_midi, "emit") as worker_emit,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                result = tool_midi._vocal_to_midi({
                    "inputPath": str(input_path),
                    "modelPath": str(model_path),
                    "outputDir": str(output_dir),
                    "bpm": 128,
                    "language": "zh",
                })

        game_infer.assert_called_once()
        call = game_infer.call_args.kwargs
        self.assertEqual(call["model_path"], model_path.resolve())
        self.assertEqual(call["audio_path"], input_path.resolve())
        self.assertEqual(call["tempo"], 128)
        self.assertEqual(call["language"], "zh")
        self.assertTrue(callable(call["progress_callback"]))
        self.assertEqual(result["outputPath"], str(output_path.resolve()))
        self.assertEqual(result["noteCount"], 12)
        self.assertNotIn("language", result)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("third-party diagnostic", stderr.getvalue())
        worker_emit.assert_any_call("audio_tool_progress", {
            "operation": "midi",
            "phase": "transcribing",
            "completed": 1,
            "total": 2,
            "current": input_path.name,
        })

    def test_vocal_to_midi_rejects_empty_bpm(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            input_path = root / "vocal.wav"
            model_path = root / "model.pt"
            output_dir = root / "output"
            input_path.touch()
            model_path.touch()

            with self.assertRaisesRegex(ValueError, "BPM must be between 30 and 300"):
                tool_midi._vocal_to_midi({
                    "inputPath": str(input_path),
                    "modelPath": str(model_path),
                    "outputDir": str(output_dir),
                    "bpm": None,
                })


if __name__ == "__main__":
    unittest.main()
