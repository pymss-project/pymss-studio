from __future__ import annotations

import shutil
import tempfile
import unittest
import wave
from pathlib import Path

from pymss.operations import OperationContext, OperationError, create_runtime


ROOT = Path(__file__).resolve().parents[2]


class ConvertTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = create_runtime()
        with tempfile.TemporaryDirectory() as temporary:
            shutil.copytree(ROOT / "plugins/convert", Path(temporary) / "convert")
            reports = cls.runtime.load_plugins([temporary])
        assert reports and all(row["loaded"] for row in reports), reports

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)

    def source(self, name="source.wav"):
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(44100)
            stream.writeframes(b"\x00\x01" * 4410)
        return str(path)

    def run_batch(self, paths, **kwargs):
        params = {"output_dir": str(self.work / "output"), "wav_bit_depth": "PCM-16", **kwargs.pop("params", {})}
        return self.runtime.run("pymss.convert.batch", inputs={"files": {"paths": paths}}, params=params, **kwargs)["report"]

    def test_batch_deduplicates_and_preserves_same_named_files(self):
        first, second = self.source("a/source.wav"), self.source("b/source.wav")
        updates = []
        report = self.run_batch([first, first, second], context=OperationContext(progress=lambda fraction, message, event: updates.append(fraction)))
        self.assertEqual(report["succeeded"], 2)
        self.assertEqual(len(set(report["outputPaths"])), 2)
        for path in report["outputPaths"]:
            with wave.open(path, "rb") as stream:
                self.assertEqual(stream.getnchannels(), 2)
                self.assertEqual(stream.getframerate(), 44100)
        self.assertEqual(updates, sorted(updates))
        self.assertEqual(updates[-1], 1)

    def test_partial_failure_preserves_success_and_reports_failed_file(self):
        good = self.source()
        bad = self.work / "invalid.wav"
        bad.write_bytes(b"invalid audio")
        report = self.run_batch([good, str(bad)])
        self.assertEqual(report["succeeded"], 1)
        self.assertEqual(report["failed"][0]["path"], str(bad))
        self.assertTrue(report["failed"][0]["message"])

    def test_cancelled_context_stops_before_the_next_file(self):
        progress = []
        context = OperationContext(progress=lambda fraction, message, event: progress.append(fraction), cancelled=lambda: bool(progress and progress[-1] >= 0.5))
        with self.assertRaisesRegex(OperationError, "cancelled"):
            self.run_batch([self.source("a.wav"), self.source("b.wav")], context=context)
        self.assertEqual(len(list((self.work / "output").glob("*.wav"))), 1)

    def test_invalid_input_is_rejected_before_output_is_written(self):
        with self.assertRaises(ValueError):
            self.run_batch([self.source(), str(self.work / "missing.wav")])
        self.assertFalse((self.work / "output").exists())


if __name__ == "__main__":
    unittest.main()
