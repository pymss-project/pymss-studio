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
            "outputDir": str(self.output.resolve()), "outputRoot": str(self.output.resolve()), "outputFormat": "wav",
        }, task_id="separation-1")

    def test_progress_units_follow_the_model_and_preserve_deduplication_in_normal_mode(self) -> None:
        for model_type, expected_unit in [
            ("vr", "patches"), (" VR ", "patches"), ("mel_band_roformer", "seconds"),
            (None, None), ("   ", None),
        ]:
            with self.subTest(model_type=model_type):
                self.separator.model_type = model_type

                def process(*_args):
                    callback = self.prepare.call_args.kwargs["progress_callback"]
                    callback(float("nan"), 231, "Invalid progress")
                    callback(12, float("inf"), "Invalid progress")
                    callback(10 ** 400, 231, "Invalid progress")
                    callback(12, 10 ** 400, "Invalid progress")
                    callback(12, 231, "Custom progress message")
                    callback(12, 231, "Custom progress message")
                    return [self.source.name]

                self.separator.process_folder.side_effect = process
                self.assertEqual(self.run_command(), 0)
                progress = [call for call in self.emit.call_args_list if call.args[0] == "task_progress"]
                self.assertEqual(len(progress), 1)
                self.assertEqual(progress[0].args[1], {
                    "stage": "separating", "message": "Custom progress message",
                    "done": 12.0, "total": 231.0, "unit": expected_unit,
                })
                self.assertEqual(progress[0].kwargs, {"task_id": "separation-1"})

    def test_batch_progress_units_keep_the_active_file_attribution(self) -> None:
        second = self.root / "second.wav"
        second.touch()
        self.payload["tasks"] = [
            {"taskId": "separation-1", "input": str(self.source)},
            {"taskId": "separation-2", "input": str(second)},
        ]
        self.separator.model_type = "vr"

        def process(path, *_args):
            callback = self.prepare.call_args.kwargs["progress_callback"]
            callback(float("inf"), 231, "Invalid progress")
            callback(12, float("nan"), "Invalid progress")
            callback(12, 231, "Processing audio")
            return [Path(path).name]

        self.separator.process_folder.side_effect = process
        self.assertEqual(self.run_command(), 0)
        progress = [call for call in self.emit.call_args_list if call.args[0] == "task_progress"]
        self.assertEqual([call.kwargs["task_id"] for call in progress], ["separation-1", "separation-2"])
        self.assertTrue(all(call.args[1]["unit"] == "patches" for call in progress))

    def test_stem_batches_accumulate_per_file_and_restore_context_after_failure(self) -> None:
        class SeparatorBase:
            def __init__(self, *, groups, progress_callback, model_type="mel_band_roformer"):
                self.groups = groups
                self.progress_callback = progress_callback
                self.model_type = model_type
                self.config = types.SimpleNamespace(training=types.SimpleNamespace(instruments=["vocals", "drums", "bass"]))
                self.logger = mock.Mock()
                self.reject = False

            def _stems_to_save(self):
                return self.config.training.instruments

            def _stem_batches_to_save(self):
                return self.groups

            def process_folder(self, path):
                for stems in self._stem_batches_to_save():
                    self.separate(None, pbar=False, stems=stems)
                return [Path(path).name]

            def separate(self, _mix, **_kwargs):
                self.progress_callback(0, 231, "Processing audio")
                if self.reject:
                    raise RuntimeError("Inference failed")
                self.progress_callback(231, 231, "Processing audio")
                return {}

        self.pymss.MSSeparator = SeparatorBase
        with mock.patch.dict(sys.modules, {"pymss": self.pymss}), \
                mock.patch("worker_rocm.configure_rocm_runtime"), \
                mock.patch("worker_rocm.run_rocm_inference", side_effect=lambda action, *_args: action()):
            separator_type = worker_infer._studio_separator_type()
            for model_type, expected_unit in [("mel_band_roformer", "seconds"), ("vr", "patches")]:
                separator = None

                def callback(done, total, message):
                    self.prepare.call_args.kwargs["progress_callback"](done, total, message)

                separator = separator_type(groups=[["vocals"], ["drums"], ["bass"]], progress_callback=callback, model_type=model_type)
                self.separator = separator
                for _ in range(2):
                    self.assertEqual(self.run_command(), 0)
                    emitted = [call.args[1] for call in self.emit.call_args_list if call.args[0] == "task_progress"]
                    self.assertEqual([item["done"] for item in emitted], [0, 231, 231, 462, 462, 693])
                    self.assertTrue(all(item["total"] == 693 and item["unit"] == expected_unit for item in emitted))
                    self.assertEqual([item["stemBatchIndex"] for item in emitted], [1, 1, 2, 2, 3, 3])
                    self.assertTrue(all(item["stemBatchCount"] == 3 for item in emitted))
                separator.reject = True
                self.assertEqual(self.run_command(), 17)
                separator.reject = False
                with mock.patch.object(worker_infer, "emit") as emitter:
                    separator.separate(None, stems="vocals")
                emitted = [call.args[1] for call in emitter.call_args_list if call.args[0] == "task_progress"]
                self.assertEqual([item["total"] for item in emitted], [231, 231])
                self.assertNotIn("stemBatchIndex", emitted[0])
                separator.groups = [["vocals", "drums", "bass"]]
                self.assertEqual(self.run_command(), 0)
                emitted = [call.args[1] for call in self.emit.call_args_list if call.args[0] == "task_progress"]
                self.assertEqual([item["done"] for item in emitted], [0, 231])
                self.assertNotIn("stemBatchIndex", emitted[0])

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


class BatchInferenceLifecycleTests(unittest.TestCase):
    def test_missing_output_marks_the_batch_failed_without_stopping_later_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.wav"
            second = root / "second.wav"
            first.touch()
            second.touch()

            class Separator:
                def process_folder(self, path: str, _input_index: int = 1) -> list[str]:
                    return [] if Path(path) == first else [path]

                def studio_outputs(self) -> list[dict[str, str]]:
                    return [{"path": str(root / "vocals.wav"), "stem": "vocals"}]

                def close(self) -> None:
                    pass

            payload = {
                "taskId": "batch",
                "model": "model.pth",
                "output": str(root / "outputs"),
                "outputLayout": "flat",
                "outputFormat": "wav",
                "tasks": [
                    {"taskId": "first-task", "input": str(first)},
                    {"taskId": "second-task", "input": str(second)},
                ],
            }
            emitted: list[tuple[str | None, str]] = []

            def emit_error(*_args, **kwargs):
                emitted.append((kwargs.get("task_id"), "error"))
                return 1

            with mock.patch.object(worker_infer, "_prepare_separator", return_value=Separator()), \
                 mock.patch.object(worker_infer, "emit", side_effect=lambda kind, _value, task_id=None: emitted.append((task_id, kind))), \
                 mock.patch.object(worker_infer, "emit_error", side_effect=emit_error):
                self.assertEqual(worker_infer.cmd_infer_batch(payload), 1)

            self.assertIn(("first-task", "error"), emitted)
            self.assertIn(("second-task", "task_done"), emitted)

    def test_later_failure_does_not_send_an_error_after_an_earlier_task_done(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.wav"
            second = root / "second.wav"
            first.touch()
            second.touch()

            class Separator:
                def process_folder(self, path: str, _input_index: int = 1) -> list[str]:
                    if Path(path) == second:
                        raise RuntimeError("second failed")
                    return [path]

                def studio_outputs(self) -> list[dict[str, str]]:
                    return [{"path": str(root / "vocals.wav"), "stem": "vocals"}]

                def close(self) -> None:
                    pass

            payload = {
                "taskId": "batch",
                "model": "model.pth",
                "output": str(root / "outputs"),
                "outputLayout": "flat",
                "outputFormat": "wav",
                "tasks": [
                    {"taskId": "first-task", "input": str(first)},
                    {"taskId": "second-task", "input": str(second)},
                ],
            }
            emitted: list[tuple[str | None, str]] = []

            def emit_error(*_args, **kwargs):
                emitted.append((kwargs.get("task_id"), "error"))
                return 1

            with mock.patch.object(worker_infer, "_prepare_separator", return_value=Separator()), \
                 mock.patch.object(worker_infer, "emit", side_effect=lambda kind, _value, task_id=None: emitted.append((task_id, kind))), \
                 mock.patch.object(worker_infer, "emit_error", side_effect=emit_error):
                self.assertEqual(worker_infer.cmd_infer_batch(payload), 1)

            first_events = [kind for task_id, kind in emitted if task_id == "first-task"]
            second_events = [kind for task_id, kind in emitted if task_id == "second-task"]
            self.assertIn("task_done", first_events)
            self.assertNotIn("error", first_events)
            self.assertIn("error", second_events)


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
