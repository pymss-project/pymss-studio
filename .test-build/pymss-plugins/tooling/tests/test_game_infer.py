from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import torch


from plugins.game.backend.engine.config import ConfigNode, load_model_bundle
from plugins.game.backend.engine.infer import (
    WARNING_NO_NOTES_DETECTED,
    WARNING_STEREO_DOWNMIX_FALLBACK,
    _normalize_notes,
    _owned_notes,
    _prepare_inference_chunks,
    _prepare_mono_waveform,
    infer,
)
from plugins.game.backend.engine.midi import Note


class GameInferenceTests(unittest.TestCase):
    def test_infer_rejects_legacy_some_checkpoint_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            model_path = root / "legacy.ckpt"
            model_path.touch()

            with self.assertRaisesRegex(ValueError, r"\.pt extension"):
                infer(model_path, root / "vocal.wav", root, tempo=120)

    def test_model_bundle_requires_release_config_beside_weights(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            model_path = Path(temp_value) / "model.pt"
            model_path.touch()

            with self.assertRaisesRegex(FileNotFoundError, "config.yaml"):
                load_model_bundle(model_path)

    def test_model_bundle_loads_release_layout_and_timestep(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            model_path = root / "model.pt"
            model_path.touch()
            (root / "config.yaml").write_text(
                """
model:
  use_languages: true
inference:
  features:
    audio_sample_rate: 44100
    hop_size: 441
""".strip(),
                encoding="utf-8",
            )
            (root / "lang_map.json").write_text(
                json.dumps({"en": 1, "zh": 4}),
                encoding="utf-8",
            )

            model, inference, languages = load_model_bundle(model_path)

        self.assertTrue(model.use_languages)
        self.assertEqual(inference.features.timestep, 0.01)
        self.assertEqual(languages, {"en": 1, "zh": 4})

    def test_notes_are_sorted_and_overlaps_are_trimmed(self) -> None:
        notes = [
            Note(onset=0.4, offset=0.8, pitch=64.0),
            Note(onset=0.0, offset=0.5, pitch=60.0),
        ]

        normalized = _normalize_notes(notes)

        self.assertEqual(normalized[0], Note(onset=0.0, offset=0.5, pitch=60.0))
        self.assertEqual(normalized[1], Note(onset=0.5, offset=0.8, pitch=64.0))

    def test_antiphase_stereo_uses_strongest_channel(self) -> None:
        left = np.sin(np.linspace(0, np.pi * 8, 4000, dtype=np.float32))

        waveform, warnings = _prepare_mono_waveform(np.stack([left, -left]))

        np.testing.assert_allclose(waveform, left)
        self.assertEqual(warnings, (WARNING_STEREO_DOWNMIX_FALLBACK,))

    def test_in_phase_stereo_is_downmixed_without_warning(self) -> None:
        left = np.linspace(-0.5, 0.5, 1000, dtype=np.float32)
        right = left * 0.5

        waveform, warnings = _prepare_mono_waveform(np.stack([left, right]))

        np.testing.assert_allclose(waveform, (left + right) / 2)
        self.assertEqual(warnings, ())

    def test_non_finite_audio_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-finite"):
            _prepare_mono_waveform(np.array([0.0, np.nan], dtype=np.float32))

    def test_long_chunks_keep_absolute_timeline_with_context(self) -> None:
        chunks = [{"offset": 10.0, "waveform": np.zeros(150_000, dtype=np.float32)}]

        prepared = _prepare_inference_chunks(
            chunks,
            sample_rate=1000,
            max_duration=60,
            context_duration=1,
        )

        self.assertEqual(len(prepared), 3)
        self.assertEqual(
            [(chunk.ownership_start, chunk.ownership_end) for chunk in prepared],
            [(10.0, 70.0), (70.0, 130.0), (130.0, 160.0)],
        )
        self.assertEqual([chunk.offset for chunk in prepared], [10.0, 69.0, 129.0])
        boundary_note = Note(onset=69.8, offset=70.4, pitch=60.0)
        self.assertEqual(_owned_notes([boundary_note], prepared[0]), [])
        self.assertEqual(_owned_notes([boundary_note], prepared[1]), [boundary_note])

    def test_repeated_inference_uses_stable_d3pm_random_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            model_path = root / "model.pt"
            audio_path = root / "vocal.wav"
            model_path.touch()
            audio_path.touch()
            model_config = ConfigNode({"use_languages": False})
            inference_config = ConfigNode({
                "features": ConfigNode({
                    "audio_sample_rate": 1000,
                    "hop_size": 10,
                }),
            })
            inference_model = mock.Mock()

            def stochastic_decode(*_args, **_kwargs) -> list[Note]:
                onset = float(torch.rand(()).item()) * 0.2
                return [Note(onset=onset, offset=onset + 0.1, pitch=60.0)]

            with (
                mock.patch(
                    "plugins.game.backend.engine.infer.load_model_bundle",
                    return_value=(model_config, inference_config, None),
                ),
                mock.patch("plugins.game.backend.engine.infer._load_state_dict", return_value={"weight": object()}),
                mock.patch(
                    "plugins.game.backend.engine.infer.SegmentationEstimationInferenceModel",
                    return_value=inference_model,
                ),
                mock.patch("plugins.game.backend.engine.infer._select_device", return_value=torch.device("cpu")),
                mock.patch(
                    "plugins.game.backend.engine.infer.librosa.load",
                    return_value=(np.ones(3000, dtype=np.float32), 1000),
                ),
                mock.patch("plugins.game.backend.engine.infer._decode_chunk", side_effect=stochastic_decode),
            ):
                first = infer(model_path, audio_path, root / "first", tempo=120)
                second = infer(model_path, audio_path, root / "second", tempo=120)

            self.assertEqual(first.first_note_at, second.first_note_at)
            self.assertEqual(first.last_note_at, second.last_note_at)

    def test_silent_audio_returns_explicit_empty_note_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            model_path = root / "model.pt"
            audio_path = root / "silent.wav"
            model_path.touch()
            audio_path.touch()
            model_config = ConfigNode({"use_languages": False})
            inference_config = ConfigNode({
                "features": ConfigNode({
                    "audio_sample_rate": 1000,
                    "hop_size": 10,
                }),
            })
            inference_model = mock.Mock()
            with (
                mock.patch(
                    "plugins.game.backend.engine.infer.load_model_bundle",
                    return_value=(model_config, inference_config, None),
                ),
                mock.patch("plugins.game.backend.engine.infer._load_state_dict", return_value={"weight": object()}),
                mock.patch(
                    "plugins.game.backend.engine.infer.SegmentationEstimationInferenceModel",
                    return_value=inference_model,
                ),
                mock.patch("plugins.game.backend.engine.infer._select_device", return_value=torch.device("cpu")),
                mock.patch(
                    "plugins.game.backend.engine.infer.librosa.load",
                    return_value=(np.zeros(3000, dtype=np.float32), 1000),
                ),
            ):
                result = infer(
                    model_path,
                    audio_path,
                    root / "output",
                    tempo=120,
                    language="zh",
                )

            self.assertEqual(result.note_count, 0)
            self.assertIn(WARNING_NO_NOTES_DETECTED, result.warnings)
            self.assertIsNone(result.language)
            self.assertTrue(result.output_path.is_file())
            self.assertEqual(list(result.output_path.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
