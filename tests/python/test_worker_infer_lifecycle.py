from __future__ import annotations

import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

try:
    from . import _bootstrap as _worker_test_bootstrap
except ImportError:
    import _bootstrap as _worker_test_bootstrap

import worker_infer


class InferenceLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "input.wav"
        self.source.write_bytes(b"input")
        self.output = self.root / "outputs"
        self.payload = {
            "taskId": "separation-1", "model": "model.pth", "input": str(self.source),
            "output": str(self.output), "outputLayout": "flat", "outputFormat": "wav",
        }
        self.logger = mock.Mock(spec=["addHandler", "removeHandler"])
        self.pymss = types.ModuleType("pymss")
        self.pymss.get_separation_logger = mock.Mock(return_value=self.logger)
        self.separator = types.SimpleNamespace(
            process_folder=mock.Mock(return_value=[self.source.name]),
            studio_outputs=mock.Mock(return_value=[{"path": str(self.output / "vocals.wav"), "stem": "vocals"}]),
            close=mock.Mock(),
            del_cache=mock.Mock(),
        )

    def run_command(self, *, prepare_error=None, device_error=None):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(sys.modules, {"pymss": self.pymss}))
            self.prepare = stack.enter_context(mock.patch.object(
                worker_infer, "_prepare_separator", return_value=self.separator, side_effect=prepare_error,
            ))
            stack.enter_context(mock.patch.object(worker_infer, "_resolve_separator_device", side_effect=device_error))
            self.emit = stack.enter_context(mock.patch.object(worker_infer, "emit"))
            self.emit_error = stack.enter_context(mock.patch.object(worker_infer, "emit_error", return_value=17))
            return worker_infer.cmd_infer(self.payload)

    def assert_handler_released(self) -> None:
        handler = self.logger.addHandler.call_args.args[0]
        self.logger.removeHandler.assert_called_once_with(handler)

    def test_success_preserves_outputs_and_releases_separator_and_handler(self) -> None:
        self.assertEqual(self.run_command(), 0)
        self.separator.process_folder.assert_called_once_with(str(self.source), 1)
        self.separator.close.assert_called_once_with()
        self.separator.del_cache.assert_not_called()
        self.assert_handler_released()
        self.emit_error.assert_not_called()
        self.emit.assert_any_call("task_done", {
            "files": [str(self.output / "vocals.wav")],
            "outputs": self.separator.studio_outputs.return_value,
            "outputDir": str(self.output.resolve()), "outputFormat": "wav",
        }, task_id="separation-1")

    def test_generic_errors_preserve_code_message_traceback_and_task_id(self) -> None:
        for message, code in [
            ("No audio stream found in input", "INPUT_AUDIO_STREAM_MISSING"),
            ("Invalid data found when processing input", "INPUT_MEDIA_UNSUPPORTED"),
            ("Could not open input file", "INPUT_MEDIA_UNSUPPORTED"),
            ("unexpected inference failure", "INFERENCE_FAILED"),
        ]:
            with self.subTest(message=message):
                self.logger.reset_mock()
                self.separator.close.reset_mock()
                self.separator.process_folder.side_effect = RuntimeError(message)
                self.assertEqual(self.run_command(), 17)
                args = self.emit_error.call_args.args
                self.assertEqual(args[:2], (code, message))
                self.assertIn("Traceback", args[2])
                self.assertIn(f"RuntimeError: {message}", args[2])
                self.assertEqual(self.emit_error.call_args.kwargs, {"task_id": "separation-1"})
                self.separator.close.assert_called_once_with()
                self.separator.del_cache.assert_not_called()
                self.assert_handler_released()

    def test_prepare_errors_retain_their_model_specific_codes(self) -> None:
        for error, code in [
            (worker_infer.ModelNotFoundError("missing model"), "MODEL_NOT_FOUND"),
            (worker_infer.ModelDownloadError("download failed"), "MODEL_DOWNLOAD_FAILED"),
        ]:
            with self.subTest(code=code):
                self.logger.reset_mock()
                self.assertEqual(self.run_command(prepare_error=error), 17)
                self.assertEqual(self.emit_error.call_args.args[:2], (code, str(error)))
                self.assertIn(type(error).__name__, self.emit_error.call_args.args[2])
                self.assertEqual(self.emit_error.call_args.kwargs, {"task_id": "separation-1"})
                self.separator.close.assert_not_called()
                self.assert_handler_released()

    def test_device_validation_remains_outside_inference_error_mapping(self) -> None:
        self.assertEqual(self.run_command(device_error=ValueError("invalid device")), 17)
        self.emit_error.assert_called_once_with("DEVICE_CONFIG_INVALID", "invalid device", task_id="separation-1")
        self.prepare.assert_not_called()
        self.logger.addHandler.assert_not_called()

    def test_missing_outputs_still_release_resources(self) -> None:
        self.separator.process_folder.return_value = []
        self.assertEqual(self.run_command(), 17)
        self.emit_error.assert_called_once_with(
            "INFERENCE_FAILED", "Separation did not produce outputs for input.wav", task_id="separation-1",
        )
        self.separator.close.assert_called_once_with()
        self.assert_handler_released()

    def test_legacy_separator_without_close_uses_del_cache(self) -> None:
        del self.separator.close
        self.assertEqual(self.run_command(), 0)
        self.separator.del_cache.assert_called_once_with()
        self.assert_handler_released()

    def test_failed_close_does_not_fall_back_to_del_cache(self) -> None:
        self.separator.close.side_effect = RuntimeError("close failed")
        self.assertEqual(self.run_command(), 0)
        self.separator.close.assert_called_once_with()
        self.separator.del_cache.assert_not_called()
        self.assert_handler_released()

    def test_legacy_cleanup_failure_does_not_replace_success(self) -> None:
        self.separator.close = None
        self.separator.del_cache.side_effect = RuntimeError("cache cleanup failed")
        self.assertEqual(self.run_command(), 0)
        self.separator.del_cache.assert_called_once_with()
        self.assert_handler_released()

    def test_handler_removal_failure_still_closes_separator(self) -> None:
        self.logger.removeHandler.side_effect = RuntimeError("handler removal failed")
        self.assertEqual(self.run_command(), 0)
        self.separator.close.assert_called_once_with()


class AudioParameterNormalizationTests(unittest.TestCase):
    def test_aac_is_forced_without_mutating_other_saved_audio_parameters(self) -> None:
        for codec in ["aac", " AAC ", "alac", "", None]:
            with self.subTest(codec=codec):
                payload = {"m4a_codec": codec, "m4a_bit_rate": "256k", "flac_bit_depth": "PCM_16"}
                original = dict(payload)
                normalized = worker_infer.normalize_audio_params(payload)
                self.assertEqual(normalized["m4a_codec"], "aac")
                self.assertEqual(normalized["m4a_bit_rate"], "256k")
                self.assertEqual(normalized["flac_bit_depth"], "PCM_16")
                self.assertEqual(payload, original)


if __name__ == "__main__":
    unittest.main()
