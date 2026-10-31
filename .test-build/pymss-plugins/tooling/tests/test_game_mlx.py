from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
import soundfile

from plugins.game.backend import mlx_backend, operations


class GameMlxTests(unittest.TestCase):
    def test_apple_backend_forwards_model_and_precision_without_importing_torch(self):
        with patch.object(mlx_backend, "is_apple_silicon", return_value=True), \
             patch.object(mlx_backend, "have_gamemlx", return_value=True), \
             patch.object(mlx_backend, "infer_mlx", return_value="result") as infer:
            result = operations.vocal_to_midi("voice.wav", "small", "outputs", language="zh", precision="fp16")
        self.assertEqual(result, "result")
        self.assertEqual(infer.call_args.kwargs["model_path"], "small")
        self.assertEqual(infer.call_args.kwargs["precision"], "fp16")
        self.assertEqual(infer.call_args.kwargs["language"], "zh")

    def test_mlx_output_preserves_existing_files_and_reports_seconds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio = root / "voice.wav"
            soundfile.write(audio, np.zeros(16000, dtype=np.float32), 16000)
            target = root / "voice.mid"
            target.write_bytes(b"existing")

            def extract(model, path, **kwargs):
                self.assertEqual(path, audio)
                self.assertEqual(kwargs["language"], "zh")
                output = kwargs["output_dir"] / "voice.mid"
                output.write_bytes(b"generated")
                return [output]

            gamemlx = SimpleNamespace(load_model=Mock(return_value=(object(), {"zh": 0})), extract=extract)
            messages = [
                SimpleNamespace(type="note_on", velocity=90, time=1.0),
                SimpleNamespace(type="note_off", velocity=0, time=0.5),
            ]
            with patch.dict(sys.modules, {"gamemlx": gamemlx, "mido": SimpleNamespace(MidiFile=Mock(return_value=messages))}):
                result = mlx_backend.infer_mlx(audio, "small", root, language="zh")
            self.assertEqual(target.read_bytes(), b"existing")
            self.assertEqual(result.output_path.read_bytes(), b"generated")
            self.assertNotEqual(result.output_path, target)
            self.assertEqual(result.note_count, 1)
            self.assertEqual(result.first_note_at, 1.0)
            self.assertEqual(result.last_note_at, 1.5)
            self.assertEqual(result.input_duration, 1.0)
            self.assertEqual(list(root.glob(".pymss-midi-*")), [])

    def test_failed_mlx_export_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def extract(model, path, **kwargs):
                (kwargs["output_dir"] / "voice.mid").write_bytes(b"partial")
                raise OSError("export interrupted")

            gamemlx = SimpleNamespace(load_model=Mock(return_value=(object(), {})), extract=extract)
            with patch.dict(sys.modules, {"gamemlx": gamemlx, "mido": SimpleNamespace()}), \
                 self.assertRaisesRegex(OSError, "export interrupted"):
                mlx_backend.infer_mlx(root / "voice.wav", "small", root)
            self.assertEqual(list(root.iterdir()), [])
