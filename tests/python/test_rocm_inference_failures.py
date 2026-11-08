from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap  # noqa: F401

import worker_infer
import worker_rocm as rocm
import worker_workflows as workflows


class RocmFailureTests(unittest.TestCase):
    def setUp(self):
        self.torch = types.SimpleNamespace(
            version=types.SimpleNamespace(hip="7.2.1"),
            cuda=types.SimpleNamespace(is_available=lambda: True, current_device=lambda: 0, synchronize=mock.Mock()),
            backends=types.SimpleNamespace(cudnn=types.SimpleNamespace(enabled=True)),
            device=lambda kind, index: f"{kind}:{index}",
        )
        self.enterContext(mock.patch.dict(sys.modules, {"torch": self.torch}))
        self.enterContext(mock.patch.object(rocm.sys, "platform", "win32"))
        for name, value in [("_native_enabled", False), ("_native_devices", set()), ("_native_checks", set()),
                            ("_operation_failure", None), ("_context_failure", None)]:
            self.enterContext(mock.patch.object(rocm, name, value))
        self.enterContext(mock.patch.dict(rocm.os.environ, {"PYMSS_STUDIO_ROCM_COMPAT": "auto"}))
        self.norm = self.enterContext(mock.patch.object(rocm, "_probe_batch_norm"))
        self.instance = self.enterContext(mock.patch.object(rocm, "_probe_instance_norm"))

    def run_inference(self, inference, **kwargs):
        return rocm.run_rocm_inference(inference, "cuda:0", [0, 1], None, **kwargs)

    def test_fatal_context_precedes_compilation_marker_and_prevents_subsequent_work(self):
        inference = mock.Mock(side_effect=RuntimeError("hipErrorIllegalAddress after MIOpenStatusUnknownError"))
        with self.assertRaises(rocm.RocmWorkerAbort) as aborted:
            self.run_inference(inference)
        self.assertEqual(aborted.exception.error.code, "ROCM_CONTEXT_LOST")
        inference.assert_called_once()
        self.norm.assert_not_called()
        later = mock.Mock()
        with self.assertRaises(rocm.RocmWorkerAbort):
            self.run_inference(later)
        later.assert_not_called()
        with self.assertRaises(rocm.RocmWorkerAbort):
            rocm.begin_rocm_operation()

    def test_context_failure_variants_agree_with_diagnostics_and_poison_the_worker(self):
        from worker_rocm_probe import _error_category

        for message in (
            "HIP error: unspecified launch failure (hipErrorLaunchFailure)",
            "hipErrorPriorLaunchFailure",
            "HIP error: unspecified launch failure in prior launch",
            "HIP context has been destroyed",
            "HIP device lost",
            "Memory access fault by GPU node-1",
        ):
            with self.subTest(message=message), mock.patch.object(rocm, "_context_failure", None):
                self.assertEqual(_error_category(RuntimeError(message))[0], "ROCM_CONTEXT_FAILED")
                inference = mock.Mock(side_effect=RuntimeError(message))
                with self.assertRaises(rocm.RocmWorkerAbort) as aborted:
                    self.run_inference(inference)
                self.assertEqual(aborted.exception.error.code, "ROCM_CONTEXT_LOST")
                self.assertTrue(rocm.rocm_worker_poisoned())
                inference.assert_called_once()
                later = mock.Mock()
                with self.assertRaises(rocm.RocmWorkerAbort):
                    self.run_inference(later)
                later.assert_not_called()
        self.norm.assert_not_called()

    def test_launch_configuration_errors_and_other_backends_do_not_poison_the_worker(self):
        cases = [("cuda", "7.2.1", "hipErrorLaunchOutOfResources"),
                 ("cpu", "7.2.1", "hipErrorLaunchFailure"),
                 ("cuda", None, "hipErrorLaunchFailure")]
        for device, hip, message in cases:
            with self.subTest(device=device, hip=hip, message=message), mock.patch.object(self.torch.version, "hip", hip):
                error = RuntimeError(message)
                with self.assertRaises(RuntimeError) as failure:
                    rocm.run_rocm_inference(mock.Mock(side_effect=error), device, [0], None)
                self.assertIs(failure.exception, error)
                self.assertFalse(rocm.rocm_worker_poisoned())
                self.assertEqual(rocm.run_rocm_inference(lambda: "output", device, [0], None), "output")
        self.norm.assert_not_called()

    def test_oom_target_and_library_errors_are_specific_and_never_retried(self):
        for message, code in [
            ("HIP out of memory; MIOpenStatusUnknownError", "ROCM_OUT_OF_MEMORY"),
            ("no kernel image is available for execution on the device", "ROCM_TARGET_MISSING"),
            ("DLL load failed: undefined symbol", "ROCM_RUNTIME_INCOMPATIBLE"),
        ]:
            with self.subTest(code=code):
                original = RuntimeError(message)
                inference = mock.Mock(side_effect=original)
                with self.assertRaises(rocm.RocmInferenceError) as failure:
                    self.run_inference(inference)
                self.assertEqual(failure.exception.code, code)
                self.assertIs(failure.exception.__cause__, original)
                inference.assert_called_once()
        self.norm.assert_not_called()

    def test_suppressed_failure_latches_until_task_publication(self):
        rocm.begin_rocm_operation()
        try:
            self.run_inference(mock.Mock(side_effect=RuntimeError("HIP out of memory")))
        except Exception:
            pass
        with self.assertRaises(rocm.RocmInferenceError):
            rocm.assert_rocm_operation_succeeded()
        rocm.begin_rocm_operation()
        rocm.assert_rocm_operation_succeeded()

    def test_suppressed_non_runtime_error_is_not_mistaken_for_success(self):
        rocm.begin_rocm_operation()
        original = ValueError("Invalid operator input shape")
        try:
            self.run_inference(mock.Mock(side_effect=original))
        except Exception:
            pass
        with self.assertRaises(ValueError) as failure:
            rocm.assert_rocm_operation_succeeded()
        self.assertIs(failure.exception, original)
        self.norm.assert_not_called()

    def test_fatal_error_bypasses_upstream_exception_handlers(self):
        caught = []
        with self.assertRaises(rocm.RocmWorkerAbort):
            try:
                self.run_inference(mock.Mock(side_effect=RuntimeError("HIP device-side assert triggered")))
            except Exception:
                caught.append(True)
        self.assertEqual(caught, [])

    def test_async_error_is_observed_before_returning_outputs(self):
        self.torch.cuda.synchronize.side_effect = RuntimeError("hipErrorLaunchFailure")
        with self.assertRaises(rocm.RocmWorkerAbort):
            self.run_inference(mock.Mock(return_value={"vocals": "pending"}))

    def test_native_recovery_validates_all_devices_and_precisions(self):
        inference = mock.Mock(side_effect=[RuntimeError("MIOpenStatusUnknownError"), "output"])
        with mock.patch("worker_rocm_probe.run_operator_check") as check:
            self.assertEqual(self.run_inference(inference, required_checks=(("conv1d", "fp32"),)), "output")
            self.assertEqual([call.args[1:] for call in check.call_args_list], [
                ("cuda:0", "conv1d", "fp32"), ("cuda:1", "conv1d", "fp32"),
            ])
            rocm.configure_rocm_runtime("cuda", [0, 1], required_checks=(("conv1d", "fp16"),))
            self.assertEqual([call.args[1:] for call in check.call_args_list][-2:], [
                ("cuda:0", "conv1d", "fp16"), ("cuda:1", "conv1d", "fp16"),
            ])
        self.assertEqual(inference.call_count, 2)
        self.assertEqual(self.torch.cuda.synchronize.call_args_list, [mock.call(0), mock.call(1)])

    def test_one_unverified_device_cannot_commit_global_native_mode(self):
        with mock.patch("worker_rocm_probe.run_operator_check", side_effect=[None, RuntimeError("operator unsupported")]), \
             self.assertRaises(rocm.RocmCompatibilityError):
            self.run_inference(mock.Mock(side_effect=RuntimeError("MIOpenStatusUnknownError")), required_checks=(("conv3d", "fp32"),))
        self.assertFalse(rocm._native_enabled)
        self.assertTrue(self.torch.backends.cudnn.enabled)

    def test_existing_native_preflight_context_failure_aborts_without_inference(self):
        rocm._native_enabled = True
        rocm._native_devices = {0}
        inference = mock.Mock()
        with mock.patch("worker_rocm_probe.run_operator_check", side_effect=RuntimeError("hipErrorIllegalAddress")), \
             self.assertRaises(rocm.RocmWorkerAbort):
            self.run_inference(inference, required_checks=(("conv1d", "fp32"),))
        inference.assert_not_called()
        self.assertTrue(rocm.rocm_worker_poisoned())

    def test_unknown_error_keeps_its_original_traceback(self):
        def original_forward():
            raise RuntimeError("unexpected operator condition")
        try:
            self.run_inference(original_forward)
        except RuntimeError as error:
            self.assertIn("original_forward", [frame.name for frame in rocm.traceback.extract_tb(error.__traceback__)])
        else:
            self.fail("The original inference failure must be raised")

    def test_waiting_thread_rechecks_poison_after_acquiring_the_inference_lock(self):
        waiting = threading.Event()
        errors = []
        inference = mock.Mock()
        check = rocm.assert_rocm_worker_healthy

        def healthy():
            if threading.current_thread().name == "waiting-rocm":
                waiting.set()
            check()

        def run():
            try:
                self.run_inference(inference)
            except rocm.RocmWorkerAbort as error:
                errors.append(error)

        with mock.patch.object(rocm, "assert_rocm_worker_healthy", side_effect=healthy):
            with rocm._inference_lock:
                thread = threading.Thread(target=run, name="waiting-rocm")
                thread.start()
                self.assertTrue(waiting.wait(2))
                with self.assertRaises(rocm.RocmWorkerAbort):
                    self.run_inference(mock.Mock(side_effect=RuntimeError("hipErrorIllegalAddress")))
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        inference.assert_not_called()

    def test_model_loading_fatal_error_preserves_stack_and_skips_existing_cache_cleanup(self):
        def initial_model_transfer():
            raise RuntimeError("hipErrorPriorLaunchFailure")

        class Separator:
            def __init__(self, **_kwargs):
                initial_model_transfer()

        manager = mock.MagicMock()
        pymss = types.ModuleType("pymss")
        pymss.MSSeparator = Separator
        with mock.patch.dict(sys.modules, {"pymss": pymss}):
            with self.assertRaises(rocm.RocmWorkerAbort) as aborted:
                with workflows._separator_cache_scope(manager):
                    worker_infer._studio_separator_type()(device="cuda", device_ids=[0])
        manager.__exit__.assert_not_called()
        self.assertEqual(aborted.exception.error.code, "ROCM_CONTEXT_LOST")
        self.assertIn("initial_model_transfer", "".join(rocm.traceback.format_exception(aborted.exception)))

    def test_native_probe_fatal_error_retains_both_forward_and_probe_stack(self):
        def original_miopen_forward():
            raise RuntimeError("miopenStatusUnknownError")

        def original_gpu_failure(*_args, **_kwargs):
            raise RuntimeError("hipErrorIllegalAddress")

        self.norm.side_effect = original_gpu_failure
        with self.assertRaises(rocm.RocmWorkerAbort) as aborted:
            self.run_inference(original_miopen_forward)
        self.assertIn("original_miopen_forward", aborted.exception.error.original_traceback)
        self.assertIn("original_gpu_failure", "".join(rocm.traceback.format_exception(aborted.exception)))

    def test_native_retry_preserves_specific_oom_classification_and_initial_trace(self):
        def original_miopen_forward():
            raise RuntimeError("miopenStatusUnknownError")

        calls = []
        def inference():
            calls.append(True)
            if len(calls) == 1:
                return original_miopen_forward()
            raise RuntimeError("HIP out of memory")

        try:
            self.run_inference(inference)
        except rocm.RocmInferenceError as error:
            self.assertEqual(error.code, "ROCM_OUT_OF_MEMORY")
            self.assertIn("original_miopen_forward", error.original_traceback)
            try:
                rocm.assert_rocm_operation_succeeded()
            except rocm.RocmInferenceError as latched:
                self.assertIs(latched, error)
                self.assertIsNotNone(latched.__cause__)
        else:
            self.fail("The retry failure must be raised")
        self.assertEqual(len(calls), 2)

    def test_required_checks_match_the_actual_module_dimensions_and_amp(self):
        classes = {name: type(name, (), {}) for name in ["Conv1d", "Conv3d", "GRU", "InstanceNorm3d"]}
        self.torch.nn = types.SimpleNamespace(**classes)
        network = types.SimpleNamespace(modules=lambda: [cls() for cls in classes.values()])
        checks = rocm.model_native_checks(network, use_amp=True)
        self.assertEqual(set(checks), {(name, precision) for name in ["conv1d", "conv3d", "gru", "instanceNorm3d"]
                                     for precision in ["fp32", "fp16"]})
        self.assertEqual(rocm.model_native_checks(network, use_amp=True, device="cpu"), ())

    def test_separator_native_checks_follow_effective_inference_amp(self):
        class Separator:
            def __init__(self, *, training_amp, inference_amp, model_amp=None):
                self.config = types.SimpleNamespace(
                    inference={"use_amp": inference_amp},
                    training=types.SimpleNamespace(instruments=["vocals"], get=lambda _key, _default: training_amp),
                )
                self.model = types.SimpleNamespace(use_amp=model_amp)
                self.device = "cuda"

            def _stems_to_save(self):
                return ["vocals"]

        with mock.patch.dict(sys.modules, {"pymss": types.SimpleNamespace(MSSeparator=Separator)}), \
             mock.patch.object(rocm, "configure_rocm_runtime"), \
             mock.patch.object(worker_infer, "model_native_checks") as checks:
            adapter = worker_infer._studio_separator_type()
            for training, inference, model, expected in [
                (True, False, None, False), (False, True, None, True),
                (False, None, None, False), (True, None, None, True),
                (True, True, False, False),
            ]:
                with self.subTest(training=training, inference=inference, model=model):
                    adapter(training_amp=training, inference_amp=inference, model_amp=model)
                    self.assertIs(checks.call_args.kwargs["use_amp"], expected)

    def test_batch_context_failure_stops_pending_files_without_success_or_gpu_cleanup(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        paths = [root / "first.wav", root / "second.wav"]
        for path in paths:
            path.touch()
        payload = {"taskId": "batch", "model": "model", "output": str(root / "outputs"),
                   "tasks": [{"taskId": str(index), "input": str(path)} for index, path in enumerate(paths)]}
        for message in ("HIP illegal memory access", "hipErrorLaunchFailure", "hipErrorPriorLaunchFailure"):
            with self.subTest(message=message), mock.patch.object(rocm, "_context_failure", None):
                separator = types.SimpleNamespace(close=mock.Mock(), process_folder=mock.Mock(
                    side_effect=lambda *_args: self.run_inference(mock.Mock(side_effect=RuntimeError(message)))),
                )
                with mock.patch.object(worker_infer, "_prepare_separator", return_value=separator), \
                     mock.patch.object(worker_infer, "separation_logging", return_value=contextlib.nullcontext(None)), \
                     mock.patch.object(worker_infer, "emit") as events, mock.patch.object(worker_infer, "emit_error") as errors:
                    self.assertEqual(worker_infer.cmd_infer_batch(payload), 1)
                separator.process_folder.assert_called_once()
                separator.close.assert_not_called()
                self.assertFalse(any(call.args[0] == "task_done" for call in events.call_args_list))
                self.assertEqual([call.args[0] for call in errors.call_args_list], ["ROCM_CONTEXT_LOST"] * 2)
                self.assertTrue(all(call.kwargs["extra"]["workerRestartRequired"] for call in errors.call_args_list))

    def test_batch_swallowed_non_fatal_failure_only_fails_its_own_file(self):
        for error, code in [(RuntimeError("HIP out of memory"), "ROCM_OUT_OF_MEMORY"),
                            (ValueError("Invalid operator input shape"), "INFERENCE_FAILED")]:
            with self.subTest(code=code):
                root = Path(self.enterContext(tempfile.TemporaryDirectory()))
                paths = [root / "first.wav", root / "second.wav"]
                for path in paths:
                    path.touch()
                output = str(root / "outputs" / "second_vocals.wav")

                def process_folder(input_path, *_args, failure=error):
                    if Path(input_path).name == "first.wav":
                        try:  # pymss swallows per-file exceptions and keeps going
                            self.run_inference(mock.Mock(side_effect=failure))
                        except Exception:
                            pass
                        return []
                    return [str(input_path)]

                separator = types.SimpleNamespace(
                    close=mock.Mock(), process_folder=mock.Mock(side_effect=process_folder),
                    studio_outputs=mock.Mock(return_value=[{"stem": "vocals", "path": output}]),
                )
                payload = {"taskId": "batch", "model": "model", "output": str(root / "outputs"),
                           "tasks": [{"taskId": str(index), "input": str(path)} for index, path in enumerate(paths)]}
                with mock.patch.object(worker_infer, "_prepare_separator", return_value=separator), \
                     mock.patch.object(worker_infer, "separation_logging", return_value=contextlib.nullcontext(None)), \
                     mock.patch.object(worker_infer, "emit") as events, \
                     mock.patch.object(worker_infer, "emit_error") as errors:
                    self.assertEqual(worker_infer.cmd_infer_batch(payload), 1)
                self.assertEqual(separator.process_folder.call_count, 2)
                self.assertEqual([(call.args[0], call.kwargs["task_id"]) for call in errors.call_args_list], [(code, "0")])
                done = [call for call in events.call_args_list if call.args[0] == "task_done"]
                self.assertEqual([call.kwargs["task_id"] for call in done], ["1"])
                self.assertEqual(done[0].args[1]["files"], [output])
                self.assertFalse(rocm.rocm_worker_poisoned())
                separator.close.assert_called_once()

    def test_batch_without_a_recorded_hip_failure_keeps_its_existing_exception_boundary(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        paths = [root / "first.wav", root / "second.wav"]
        for path in paths:
            path.touch()
        separator = types.SimpleNamespace(close=mock.Mock(), process_folder=mock.Mock(side_effect=ValueError("Adapter failure")))
        payload = {"taskId": "batch", "model": "model", "output": str(root / "outputs"),
                   "tasks": [{"taskId": str(index), "input": str(path)} for index, path in enumerate(paths)]}
        with mock.patch.object(rocm, "_uses_windows_rocm", return_value=False), \
             mock.patch.object(worker_infer, "_prepare_separator", return_value=separator), \
             mock.patch.object(worker_infer, "separation_logging", return_value=contextlib.nullcontext(None)), \
             mock.patch.object(worker_infer, "emit"), mock.patch.object(worker_infer, "emit_error") as errors:
            self.assertEqual(worker_infer.cmd_infer_batch(payload), 1)
        separator.process_folder.assert_called_once()
        self.assertEqual([call.kwargs["task_id"] for call in errors.call_args_list], ["0", "1"])

    def test_workflow_fatal_failure_stops_batch_and_closes_only_safe_resources(self):
        cleanup = mock.Mock()
        manager = mock.Mock()
        manager.__enter__ = mock.Mock(return_value=mock.Mock())
        manager.__exit__ = cleanup
        with self.assertRaises(rocm.RocmWorkerAbort):
            with workflows._separator_cache_scope(manager):
                self.run_inference(mock.Mock(side_effect=RuntimeError("HIP device-side assert triggered")))
        cleanup.assert_not_called()

    def test_workflow_batch_does_not_run_remaining_nodes_after_context_loss(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        payload = {"taskId": "batch", "output": str(root), "workflow": {},
                   "tasks": [{"taskId": "first"}, {"taskId": "second"}]}
        with mock.patch.object(workflows, "_run_pymss", side_effect=lambda *_args, **_kwargs:
                               self.run_inference(mock.Mock(side_effect=RuntimeError("HIP illegal memory access")))) as run, \
             mock.patch.object(workflows, "emit") as events, mock.patch.object(workflows, "emit_error") as errors:
            self.assertEqual(workflows.cmd_infer_workflow(payload), 1)
        run.assert_called_once()
        self.assertFalse(any(call.args[0] == "task_done" for call in events.call_args_list))
        self.assertEqual([call.kwargs["task_id"] for call in errors.call_args_list], ["first", "second"])

    def test_fatal_cli_flushes_error_and_skips_driver_finalizers(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        marker = root / "finalizer-ran"
        script = Path(worker_infer.__file__).with_name("worker.py")
        code = """
import atexit, runpy, sys, types
from pathlib import Path
import worker_rocm as rocm
rocm._uses_windows_rocm = lambda *_args: True
def failure(): raise RuntimeError('hipErrorPriorLaunchFailure')
module = types.ModuleType('worker_models')
module.cmd_health = lambda: rocm.run_rocm_inference(failure, 'cuda', [0], None)
sys.modules['worker_models'] = module
marker = Path(sys.argv[2])
atexit.register(marker.touch)
sys.argv = [sys.argv[1], 'health']
runpy.run_path(sys.argv[0], run_name='__main__')
"""
        environment = {**os.environ, "PYTHONPATH": str(script.parent), "PYMSS_STUDIO_DATA_ROOT": str(root)}
        process = subprocess.run([sys.executable, "-c", code, str(script), str(marker)], env=environment,
                                 capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 1, process.stderr)
        self.assertIn('"code": "ROCM_CONTEXT_LOST"', process.stdout)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
