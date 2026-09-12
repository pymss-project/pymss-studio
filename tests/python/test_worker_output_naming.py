from __future__ import annotations

import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock
from types import SimpleNamespace
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap

import worker_infer
from worker_infer import (
    _claim_output_path,
    _studio_separator_type,
)


class _OutputSeparatorBase:
    def __init__(self, instruments: list[str]) -> None:
        self.config = SimpleNamespace(training=SimpleNamespace(instruments=instruments))
        self.logger = mock.Mock()

    def _stems_to_save(self):
        return self.config.training.instruments

    def _stem_batches_to_save(self):
        return [self.config.training.instruments]

    def process_folder(self, input_folder):
        raise AssertionError("The upstream separation boundary must be stubbed")


class StudioOutputNamingTests(unittest.TestCase):
    def _new_separator(self, *, naming=None, stems=None, model="model-a"):
        with mock.patch.dict(sys.modules, {"pymss": SimpleNamespace(MSSeparator=_OutputSeparatorBase)}):
            separator_type = _studio_separator_type()
        separator = separator_type(
            instruments=stems or ["vocals"], output_naming=naming, output_model=model,
        )
        separator.output_format = "wav"

        def save_audio(audio, _sr, file_name, store_dir):
            Path(store_dir, f"{file_name}.wav").write_bytes(audio)

        separator.save_audio = mock.Mock(side_effect=save_audio)
        return separator

    def test_capture_excludes_stale_files_and_preserves_stem_underscores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale = root / "song_instrument.wav"
            unrelated = root / "unrelated.txt"
            stale.write_bytes(b"stale")
            unrelated.write_bytes(b"keep")
            separator = self._new_separator(stems=["lead_vocals"])
            separator._save_output("lead_vocals", b"vocals", 44100, "song", str(root))
            output = root / "song_lead_vocals.wav"
            self.assertEqual(separator.studio_outputs(), [{"stem": "lead_vocals", "path": str(output)}])
            self.assertEqual(output.read_bytes(), b"vocals")
            self.assertEqual(stale.read_bytes(), b"stale")
            self.assertEqual(unrelated.read_bytes(), b"keep")

    def test_template_numbers_and_reports_stems_in_configured_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            separator = self._new_separator(
                stems=["vocals", "drums"],
                naming={"enabled": True, "template": "%index%_%filename%_%stem%", "stemOrder": ["DRUMS", "vocals"]},
            )
            for stem in ["vocals", "drums"]:
                separator._save_output(stem, stem.encode(), 44100, "song", str(root))
            outputs = separator.studio_outputs()
            self.assertEqual([Path(item["path"]).name for item in outputs], [
                "01_song_drums.wav", "02_song_vocals.wav",
            ])
            self.assertEqual([Path(item["path"]).read_bytes() for item in outputs], [b"drums", b"vocals"])
            self.assertFalse((root / "song_drums.wav").exists())
            self.assertFalse((root / "song_vocals.wav").exists())

    def test_input_number_and_invalid_token_characters_are_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            separator = self._new_separator(
                model="model:bad", stems=["lead/vocal"],
                naming={"enabled": True, "template": "%input_number%_%filename%_%stem%_%model%"},
            )
            with mock.patch.object(separator, "_input_paths_for_context", return_value=[(Path("demo:mix.flac"), 12)]):
                separator._prepare_output_contexts("unused", 12)
            separator._save_output("lead/vocal", b"vocals", 44100, "demo:mix", str(root))
            output = root / "12_demo_mix_lead_vocal_model_bad.wav"
            self.assertEqual(separator.studio_outputs(), [{"stem": "lead/vocal", "path": str(output)}])
            self.assertEqual(output.read_bytes(), b"vocals")

    def test_cross_platform_and_reserved_filename_rules(self) -> None:
        cases = [
            ("bad:name<>|?* %stem% .", "CON", "bad_name_CON.wav"),
            ("CON", "vocals", "CON_.wav"),
            ("CON.txt", "vocals", "CON.txt_.wav"),
            ("nul", "vocals", "nul_.wav"),
            ("LPT9", "vocals", "LPT9_.wav"),
            ("../dir\\name\x01 .", "vocals", "dir_name.wav"),
        ]
        for template, stem, expected in cases:
            with self.subTest(template=template), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                separator = self._new_separator(naming={"enabled": True, "template": template}, stems=[stem])
                separator._save_output(stem, b"audio", 44100, "song", str(root))
                output = root / expected
                self.assertEqual(separator.studio_outputs(), [{"stem": stem, "path": str(output)}])
                self.assertEqual(output.read_bytes(), b"audio")
                self.assertEqual(list(root.iterdir()), [output])

    def test_disabled_or_empty_template_uses_default_safe_names(self) -> None:
        for naming in [None, {}, {"enabled": False, "template": "%index%_%stem%"}, {"enabled": True, "template": "  "}]:
            with self.subTest(naming=naming), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                separator = self._new_separator(naming=naming)
                separator._save_output("vocals", b"audio", 44100, "song", str(root))
                output = root / "song_vocals.wav"
                self.assertEqual(separator.studio_outputs(), [{"stem": "vocals", "path": str(output)}])
                self.assertEqual(output.read_bytes(), b"audio")

    def test_unicode_names_remain_intact_and_long_names_are_utf8_bounded(self) -> None:
        for name in ["音轨🎵_cafe\u0301", "音🎵e\u0301" * 40]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                separator = self._new_separator(naming={"enabled": True, "template": name})
                separator._save_output("vocals", b"audio", 44100, "song", str(root))
                output = Path(separator.studio_outputs()[0]["path"])
                expected = name.encode("utf-8")[:200].decode("utf-8", errors="ignore")
                self.assertEqual(output.name, f"{expected}.wav")
                self.assertLessEqual(len(output.stem.encode("utf-8")), 200)
                self.assertNotIn("\ufffd", output.name)
                self.assertEqual(output.read_bytes(), b"audio")

    def test_default_output_collision_preserves_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "song_vocals.wav"
            existing.write_bytes(b"keep")
            separator = self._new_separator()
            separator._save_output("vocals", b"new", 44100, "song", str(root))
            self.assertEqual(existing.read_bytes(), b"keep")
            output = root / "song_vocals_2.wav"
            self.assertEqual(separator.studio_outputs(), [{"stem": "vocals", "path": str(output)}])
            self.assertEqual(output.read_bytes(), b"new")

    def test_concurrent_output_reservations_do_not_overwrite_audio(self) -> None:
        for shared_separator in [True, False]:
            with self.subTest(shared_separator=shared_separator), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                existing = root / "mix.wav"
                existing.write_bytes(b"keep")
                count = 6
                separators = [self._new_separator(naming={"enabled": True, "template": "mix"})
                              for _ in range(1 if shared_separator else count)]
                barrier = Barrier(count)

                def save(index):
                    separator = separators[0] if shared_separator else separators[index]
                    barrier.wait(timeout=5)
                    separator._save_output("vocals", str(index).encode(), 44100, "song", str(root))

                with ThreadPoolExecutor(max_workers=count) as executor:
                    list(executor.map(save, range(count)))
                outputs = [item for separator in separators for item in separator.studio_outputs()]
                self.assertEqual(len(outputs), count)
                self.assertEqual(len({item["path"] for item in outputs}), count)
                self.assertEqual({Path(item["path"]).read_bytes() for item in outputs}, {str(i).encode() for i in range(count)})
                self.assertEqual(existing.read_bytes(), b"keep")
                self.assertEqual(len(list(root.iterdir())), count + 1)

    def test_encoding_failure_removes_partial_file_and_allows_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "mix.wav"
            existing.write_bytes(b"keep")
            separator = self._new_separator(naming={"enabled": True, "template": "mix"})
            save_audio = separator.save_audio.side_effect

            def fail_save(audio, sr, file_name, store_dir):
                save_audio(b"partial", sr, file_name, store_dir)
                raise RuntimeError("encoder failed")

            separator.save_audio.side_effect = fail_save
            with self.assertRaisesRegex(RuntimeError, "encoder failed"):
                separator._save_output("vocals", b"new", 44100, "song", str(root))
            self.assertEqual(list(root.iterdir()), [existing])
            self.assertEqual(separator.studio_outputs(), [])
            self.assertEqual(separator._studio_claimed_paths, set())
            separator.save_audio.side_effect = save_audio
            separator._save_output("vocals", b"new", 44100, "song", str(root))
            self.assertEqual((root / "mix_2.wav").read_bytes(), b"new")
            self.assertEqual(existing.read_bytes(), b"keep")

    def test_folder_inputs_with_the_same_stem_keep_distinct_indices_and_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "inputs"
            source.mkdir()
            names = ["song.mp3", "song.wav"]
            for name in names:
                (source / name).write_bytes(name.encode())
            output_dir = root / "outputs"
            separator = self._new_separator(
                stems=["vocals", "drums"],
                naming={"enabled": True, "template": "%input_number%_%filename%_%stem%"},
            )

            def process(base, input_folder):
                self.assertEqual(input_folder, str(source))
                for name in names:
                    for stem in ["drums", "vocals"]:
                        base._save_output(stem, f"{name}:{stem}".encode(), 44100, "song", str(output_dir))
                return names

            with mock.patch.object(_OutputSeparatorBase, "process_folder", autospec=True, side_effect=process), \
                    mock.patch.object(worker_infer.os, "listdir", return_value=names):
                self.assertEqual(separator.process_folder(str(source), input_index=7), names)
            outputs = separator.studio_outputs()
            self.assertEqual(len(outputs), 4)
            for index, name in enumerate(names, start=7):
                for stem in ["vocals", "drums"]:
                    output = output_dir / f"{index:02d}_song_{stem}.wav"
                    self.assertEqual(output.read_bytes(), f"{name}:{stem}".encode())
                    self.assertIn({"stem": stem, "path": str(output)}, outputs)
            self.assertEqual(separator._studio_input_contexts, {})
            self.assertIsNone(separator._studio_active_context)

    def test_upstream_failure_discards_only_outputs_created_in_this_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "song.wav"
            source.write_bytes(b"input")
            existing = root / "song_vocals.wav"
            existing.write_bytes(b"keep")
            separator = self._new_separator()

            def process(base, _input_folder):
                base._save_output("vocals", b"new", 44100, "song", str(root))
                raise RuntimeError("separation failed")

            with mock.patch.object(_OutputSeparatorBase, "process_folder", autospec=True, side_effect=process):
                with self.assertRaisesRegex(RuntimeError, "separation failed"):
                    separator.process_folder(str(source))
            self.assertEqual(separator.studio_outputs(), [])
            self.assertEqual(separator._studio_claimed_paths, set())
            self.assertEqual(separator._studio_input_contexts, {})
            self.assertIsNone(separator._studio_active_context)
            self.assertEqual(existing.read_bytes(), b"keep")
            self.assertEqual(source.read_bytes(), b"input")
            self.assertFalse((root / "song_vocals_2.wav").exists())


class OutputNamingTests(unittest.TestCase):
    def test_separator_writes_the_template_name_without_touching_the_default_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            renamed_result = root / "song_vocals.wav"
            renamed_result.write_bytes(b"keep")

            separator = object.__new__(_studio_separator_type())
            separator.output_format = "wav"
            separator._studio_naming = {
                "enabled": True,
                "template": "%index%_%filename%_%stem%",
                "stem_order": ["vocals"],
            }
            separator._studio_output_model = "model-a"
            separator._studio_output_lock = Lock()
            separator._studio_claimed_paths = set()
            separator._studio_last_outputs = []
            separator._studio_input_path = str(root / "song.mp3")
            separator._studio_input_index = 1
            separator._studio_now = worker_infer.datetime(2026, 8, 18, 2, 49, 0)
            separator._studio_stem_indices = {"vocals": 0}

            def save_audio(_audio, _sr, file_name, store_dir):
                Path(store_dir, f"{file_name}.wav").write_bytes(b"new")

            separator.save_audio = save_audio
            separator._save_output("vocals", object(), 44100, "song", str(root))

            self.assertEqual(renamed_result.read_bytes(), b"keep")
            target = root / "01_song_vocals.wav"
            self.assertEqual(target.read_bytes(), b"new")
            self.assertEqual(separator.studio_outputs(), [{"stem": "vocals", "path": str(target)}])

    def test_separator_adds_a_suffix_before_writing_over_a_template_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "01_song_vocals.wav"
            existing.write_bytes(b"keep")

            separator = object.__new__(_studio_separator_type())
            separator.output_format = "wav"
            separator._studio_naming = {
                "enabled": True,
                "template": "%index%_%filename%_%stem%",
                "stem_order": ["vocals"],
            }
            separator._studio_output_model = "model-a"
            separator._studio_output_lock = Lock()
            separator._studio_claimed_paths = set()
            separator._studio_last_outputs = []
            separator._studio_input_path = str(root / "song.mp3")
            separator._studio_input_index = 1
            separator._studio_now = worker_infer.datetime(2026, 8, 18, 2, 49, 0)
            separator._studio_stem_indices = {"vocals": 0}
            separator.save_audio = lambda _audio, _sr, file_name, store_dir: Path(store_dir, f"{file_name}.wav").write_bytes(b"new")

            separator._save_output("vocals", object(), 44100, "song", str(root))

            self.assertEqual(existing.read_bytes(), b"keep")
            self.assertEqual((root / "01_song_vocals_2.wav").read_bytes(), b"new")

    def test_claimed_path_skips_an_existing_output_without_racing_to_overwrite_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "song_vocals.wav"
            original.write_bytes(b"keep")

            claimed = _claim_output_path(original)

            self.assertEqual(claimed.name, "song_vocals_2.wav")
            self.assertEqual(original.read_bytes(), b"keep")
            self.assertTrue(claimed.is_file())

    def test_claimed_path_remains_available_after_numbered_candidates_are_reserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "song_vocals.wav"
            reserved = {original, *(original.with_name(f"song_vocals_{index}.wav") for index in range(2, 1000))}

            claimed = _claim_output_path(original, reserved)

            self.assertNotIn(claimed, reserved)
            self.assertEqual(claimed.parent, original.parent)
            self.assertEqual(claimed.suffix, ".wav")
            self.assertTrue(claimed.is_file())

    def test_fallback_retries_existing_and_reserved_names_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "song_vocals.wav"
            reserved = {original, *(original.with_name(f"song_vocals_{index}.wav") for index in range(2, 1000))}
            occupied = original.with_name(f"song_vocals_{'a' * 32}.wav")
            occupied.write_bytes(b"keep")
            reserved.add(original.with_name(f"song_vocals_{'b' * 32}.wav"))

            with mock.patch.object(worker_infer, "uuid4", side_effect=[
                SimpleNamespace(hex=char * 32) for char in "abc"
            ]) as unique_id:
                claimed = _claim_output_path(original, reserved)

            self.assertEqual(unique_id.call_count, 3)
            self.assertEqual(claimed.name, f"song_vocals_{'c' * 32}.wav")
            self.assertTrue(claimed.is_file())
            self.assertEqual(occupied.read_bytes(), b"keep")

    def test_fallback_collisions_stop_after_a_bounded_number_of_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "song_vocals.wav"
            reserved = {original, *(original.with_name(f"song_vocals_{index}.wav") for index in range(2, 1000))}
            occupied = original.with_name(f"song_vocals_{'a' * 32}.wav")
            occupied.write_bytes(b"keep")

            with mock.patch.object(worker_infer, "uuid4", return_value=SimpleNamespace(hex="a" * 32)) as unique_id:
                with self.assertRaisesRegex(FileExistsError, "unique output filename"):
                    _claim_output_path(original, reserved)

            self.assertEqual(unique_id.call_count, 10)
            self.assertEqual(occupied.read_bytes(), b"keep")
            self.assertEqual(list(original.parent.iterdir()), [occupied])

    def test_separator_releases_its_placeholder_when_audio_encoding_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            separator = object.__new__(_studio_separator_type())
            separator.output_format = "wav"
            separator._studio_naming = {
                "enabled": True,
                "template": "%index%_%filename%_%stem%",
                "stem_order": ["vocals"],
            }
            separator._studio_output_model = "model-a"
            separator._studio_output_lock = Lock()
            separator._studio_claimed_paths = set()
            separator._studio_last_outputs = []
            separator._studio_input_path = str(root / "song.mp3")
            separator._studio_input_index = 1
            separator._studio_now = worker_infer.datetime(2026, 8, 18, 2, 49, 0)
            separator._studio_stem_indices = {"vocals": 0}

            def fail_save(*_args):
                raise RuntimeError("encoder failed")

            separator.save_audio = fail_save

            with self.assertRaisesRegex(RuntimeError, "encoder failed"):
                separator._save_output("vocals", object(), 44100, "song", str(root))

            self.assertFalse((root / "01_song_vocals.wav").exists())
            self.assertEqual(separator.studio_outputs(), [])

    def _new_separator_for_upstream_process(self, *, stem_count: int = 1):
        separator = object.__new__(_studio_separator_type())
        separator.output_format = "wav"
        separator._studio_naming = {
            "enabled": True,
            "template": "%input_number%_%filename%_%stem%",
            "stem_order": ["vocals", "instrumental"],
        }
        separator._studio_output_model = "model-a"
        separator._studio_output_lock = Lock()
        separator._studio_claimed_paths = set()
        separator._studio_last_outputs = []
        separator._studio_input_path = ""
        separator._studio_input_index = 1
        separator._studio_now = worker_infer.datetime(2026, 8, 18, 2, 49, 0)
        separator._studio_input_contexts = {}
        separator._studio_active_context = None
        separator._studio_active_output_count = 0
        separator._studio_expected_output_count = stem_count
        separator._studio_output_sources = {}
        separator._studio_stem_indices = {"vocals": 0, "instrumental": 1}
        separator.logger = mock.Mock()

        def save_audio(_audio, _sr, file_name, store_dir):
            Path(store_dir, f"{file_name}.wav").write_bytes(b"new")

        separator.save_audio = save_audio
        return separator

    def test_process_folder_delegates_to_pymss_and_preserves_input_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "song.wav"
            source.write_bytes(b"input")
            separator = self._new_separator_for_upstream_process()

            def upstream_process(base, input_folder):
                self.assertEqual(input_folder, str(source))
                base._save_output("vocals", object(), 44100, "song", str(root))
                return ["song.wav"]

            with mock.patch("pymss.MSSeparator.process_folder", autospec=True, side_effect=upstream_process) as process:
                success_files = separator.process_folder(str(source), input_index=7)

            process.assert_called_once_with(separator, str(source))
            self.assertEqual(success_files, ["song.wav"])
            self.assertEqual(
                separator.studio_outputs(),
                [{"stem": "vocals", "path": str(root / "07_song_vocals.wav")}],
            )

    def test_process_folder_cleans_failed_outputs_after_upstream_delegate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.wav"
            second = root / "second.wav"
            first.write_bytes(b"input")
            second.write_bytes(b"input")
            separator = self._new_separator_for_upstream_process()

            def upstream_process(base, input_folder):
                self.assertEqual(input_folder, str(root))
                base._save_output("vocals", object(), 44100, "first", str(root))
                base._save_output("vocals", object(), 44100, "second", str(root))
                return ["first.wav"]

            with mock.patch("pymss.MSSeparator.process_folder", autospec=True, side_effect=upstream_process) as process:
                success_files = separator.process_folder(str(root))

            process.assert_called_once_with(separator, str(root))
            self.assertEqual(success_files, ["first.wav"])
            self.assertTrue((root / "01_first_vocals.wav").is_file())
            self.assertFalse((root / "02_second_vocals.wav").exists())
            self.assertEqual(
                separator.studio_outputs(),
                [{"stem": "vocals", "path": str(root / "01_first_vocals.wav")}],
            )


if __name__ == "__main__":
    unittest.main()
