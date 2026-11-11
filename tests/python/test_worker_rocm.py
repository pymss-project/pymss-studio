from __future__ import annotations

import os
import sys
import types
import unittest
import weakref
from unittest import mock

import worker_rocm


class RocmCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.torch = types.SimpleNamespace(
            __version__="2.9.1+rocm7.2.1", version=types.SimpleNamespace(hip="7.2.1"),
            cuda=types.SimpleNamespace(
                is_available=lambda: True, current_device=lambda: 0,
                get_device_name=lambda index: f"GPU {index}", device_count=lambda: 2,
            ),
            backends=types.SimpleNamespace(cudnn=types.SimpleNamespace(enabled=True)),
            device=lambda kind, index: f"{kind}:{index}",
        )
        self.enterContext(mock.patch.dict(sys.modules, {"torch": self.torch}))
        self.enterContext(mock.patch.object(sys, "platform", "win32"))
        self.enterContext(mock.patch.dict(os.environ, {"PYMSS_STUDIO_ROCM_COMPAT": "auto"}))
        self.enterContext(mock.patch.object(worker_rocm, "_native_devices", set()))
        self.enterContext(mock.patch.object(worker_rocm, "_native_enabled", False))
        self.enterContext(mock.patch.object(worker_rocm, "_native_checks", set()))
        self.enterContext(mock.patch.object(worker_rocm, "_operation_failure", None))
        self.enterContext(mock.patch.object(worker_rocm, "_context_failure", None))
        self.probe = self.enterContext(mock.patch.object(worker_rocm, "_probe_batch_norm"))
        self.instance_probe = self.enterContext(mock.patch.object(worker_rocm, "_probe_instance_norm"))
        self.logger = mock.Mock()

    def test_auto_mode_can_construct_a_non_amp_model_without_instance_norm_support(self):
        import worker_infer

        constructed = []

        class Separator:
            def __init__(self, **kwargs):
                constructed.append(kwargs)
                self.config = types.SimpleNamespace(training=types.SimpleNamespace(instruments=["vocals"]))

            def _stems_to_save(self):
                return ["vocals"]

        pymss = types.ModuleType("pymss")
        pymss.MSSeparator = Separator
        self.instance_probe.side_effect = RuntimeError("instance_norm not implemented for Half")
        with mock.patch.dict(sys.modules, {"pymss": pymss}):
            worker_infer._studio_separator_type()(
                device="auto", device_ids=[0], inference_params={"use_amp": False}, logger=self.logger,
            )
        self.assertEqual(len(constructed), 1)
        self.probe.assert_not_called()
        self.instance_probe.assert_not_called()

    def test_native_recovery_does_not_require_unrelated_fp16_instance_norm(self):
        self.instance_probe.side_effect = RuntimeError("instance_norm not implemented for Half")
        inference = mock.Mock(side_effect=[RuntimeError("miopenStatusUnknownError"), "stems"])
        self.assertEqual(worker_rocm.run_rocm_inference(inference, "cuda", [0], self.logger), "stems")
        self.instance_probe.assert_not_called()

    def test_failed_forward_is_released_before_native_reference_check(self):
        class Activation:
            pass

        references = []

        def inference():
            if not references:
                activation = Activation()
                references.append(weakref.ref(activation))
                raise RuntimeError("miopenStatusUnknownError")
            return "stems"

        def native_probe(*_args, **_kwargs):
            self.assertIsNone(references[0](), "failed forward must be released before allocating probe tensors")

        self.probe.side_effect = native_probe
        self.assertEqual(worker_rocm.run_rocm_inference(inference, "cuda", [0], self.logger), "stems")
        self.probe.assert_called_once_with(self.torch, "cuda:0", verify_reference=True)

    def test_auto_mode_does_not_probe_or_disable_miopen(self):
        for _ in range(2):
            self.assertFalse(worker_rocm.configure_rocm_runtime("cuda", [1], self.logger))
        self.probe.assert_not_called()
        self.instance_probe.assert_not_called()
        self.assertTrue(self.torch.backends.cudnn.enabled)

    def test_known_compilation_failure_uses_verified_native_gpu_for_all_selected_devices(self):
        self.assertTrue(worker_rocm.configure_rocm_runtime(
            "auto", [0, 1], self.logger, failure=RuntimeError("miopenStatusUnknownError"),
        ))
        self.assertFalse(self.torch.backends.cudnn.enabled)
        self.assertEqual(self.probe.call_args_list, [
            mock.call(self.torch, "cuda:0", verify_reference=True),
            mock.call(self.torch, "cuda:1", verify_reference=True),
        ])
        self.assertTrue(worker_rocm.configure_rocm_runtime("cuda", [1], self.logger))
        self.assertEqual(self.probe.call_count, 2)
        self.logger.warning.assert_called_once()
        self.assertIn("miopenStatusUnknownError", self.logger.warning.call_args.args[0])

    def test_native_failure_preserves_the_original_error_and_restores_backend(self):
        self.probe.side_effect = RuntimeError("native failed")
        with self.assertRaisesRegex(worker_rocm.RocmCompatibilityError, "HIPRTC_ERROR_COMPILATION.*native failed"):
            worker_rocm.configure_rocm_runtime("cuda", [0], self.logger, failure=RuntimeError("HIPRTC_ERROR_COMPILATION"))
        self.assertTrue(self.torch.backends.cudnn.enabled)
        self.assertFalse(worker_rocm._native_enabled)

    def test_automatic_startup_does_not_run_optional_gpu_checks(self):
        self.probe.side_effect = RuntimeError("HIP out of memory")
        self.assertFalse(worker_rocm.configure_rocm_runtime("cuda", [0], self.logger))
        self.probe.assert_not_called()
        self.assertTrue(self.torch.backends.cudnn.enabled)

    def test_cpu_cuda_and_other_platforms_are_unchanged(self):
        self.assertFalse(worker_rocm.configure_rocm_runtime("cpu", [0], self.logger))
        with mock.patch.object(sys, "platform", "linux"):
            self.assertFalse(worker_rocm.configure_rocm_runtime("cuda", [0], self.logger))
        self.torch.version.hip = None
        self.assertFalse(worker_rocm.configure_rocm_runtime("cuda", [0], self.logger))
        self.probe.assert_not_called()

    def test_control_mode_leaves_original_miopen_behavior_unchanged(self):
        with mock.patch.dict(os.environ, {"PYMSS_STUDIO_ROCM_COMPAT": "off"}):
            self.assertFalse(worker_rocm.configure_rocm_runtime("auto", [0], self.logger))
        self.probe.assert_not_called()
        self.assertTrue(self.torch.backends.cudnn.enabled)

    def test_actual_inference_failure_switches_after_automatic_startup(self):
        self.assertFalse(worker_rocm.configure_rocm_runtime("cuda", [0], self.logger))
        inference = mock.Mock(side_effect=[RuntimeError("miopenStatusUnknownError"), "stems"])
        result = worker_rocm.run_rocm_inference(inference, "cuda", [0], self.logger)
        self.assertEqual(result, "stems")
        self.assertEqual(inference.call_count, 2)
        self.assertFalse(self.torch.backends.cudnn.enabled)
        self.probe.assert_called_with(self.torch, "cuda:0", verify_reference=True)

    def test_actual_inference_recovery_validates_all_data_parallel_devices(self):
        worker_rocm.configure_rocm_runtime("auto", [0, 1], self.logger)
        self.probe.reset_mock()
        inference = mock.Mock(side_effect=[RuntimeError("miopenStatusUnknownError"), "stems"])
        self.assertEqual(worker_rocm.run_rocm_inference(inference, "cuda:0", [0, 1], self.logger), "stems")
        self.assertEqual(self.probe.call_args_list, [
            mock.call(self.torch, "cuda:0", verify_reference=True),
            mock.call(self.torch, "cuda:1", verify_reference=True),
        ])

    def test_actual_inference_retry_is_disabled_in_the_control_group(self):
        inference = mock.Mock(side_effect=RuntimeError("miopenStatusUnknownError"))
        with mock.patch.dict(os.environ, {"PYMSS_STUDIO_ROCM_COMPAT": "off"}), \
             self.assertRaisesRegex(RuntimeError, "miopenStatusUnknownError"):
            worker_rocm.run_rocm_inference(inference, "cuda", [0], self.logger)
        inference.assert_called_once()
        self.probe.assert_not_called()
        self.assertTrue(self.torch.backends.cudnn.enabled)

    def test_actual_inference_unknown_errors_are_not_retried(self):
        inference = mock.Mock(side_effect=RuntimeError("HIP out of memory"))
        with self.assertRaisesRegex(RuntimeError, "out of memory"):
            worker_rocm.run_rocm_inference(inference, "cuda", [0], self.logger)
        inference.assert_called_once()
        self.probe.assert_not_called()

    def test_control_mode_preserves_the_original_failure_traceback(self):
        def failed_forward():
            raise RuntimeError("miopenStatusUnknownError")

        with mock.patch.dict(os.environ, {"PYMSS_STUDIO_ROCM_COMPAT": "off"}):
            try:
                worker_rocm.run_rocm_inference(failed_forward, "cuda", [0], self.logger)
            except RuntimeError as error:
                frames = worker_rocm.traceback.extract_tb(error.__traceback__)
                self.assertIn("failed_forward", [frame.name for frame in frames])
            else:
                self.fail("Control mode must retain the original failure")
        self.probe.assert_not_called()

    def test_non_rocm_failure_preserves_the_original_traceback(self):
        def failed_forward():
            raise RuntimeError("miopenStatusUnknownError")

        self.torch.version.hip = None
        try:
            worker_rocm.run_rocm_inference(failed_forward, "cuda", [0], self.logger)
        except RuntimeError as error:
            frames = worker_rocm.traceback.extract_tb(error.__traceback__)
            self.assertIn("failed_forward", [frame.name for frame in frames])
        else:
            self.fail("Unsupported backends must retain the original failure")
        self.probe.assert_not_called()

    def test_actual_inference_retries_only_once_and_preserves_both_errors(self):
        inference = mock.Mock(side_effect=[RuntimeError("miopenStatusUnknownError"), RuntimeError("native inference failed")])
        with self.assertRaisesRegex(worker_rocm.RocmCompatibilityError, "miopenStatusUnknownError.*native inference failed"):
            worker_rocm.run_rocm_inference(inference, "cuda", [0], self.logger)
        self.assertEqual(inference.call_count, 2)

    def test_failed_forward_frame_releases_activations_before_retry(self):
        class Activation:
            pass

        references = []

        def inference():
            if not references:
                activation = Activation()
                references.append(weakref.ref(activation))
                raise RuntimeError("miopenStatusUnknownError")
            self.assertIsNone(references[0](), "failed forward activations must be released before retry")
            return "stems"

        self.assertEqual(worker_rocm.run_rocm_inference(inference, "cuda", [0], self.logger), "stems")

    def test_forced_native_mode_checks_all_gpus_without_using_miopen(self):
        with mock.patch.dict(os.environ, {"PYMSS_STUDIO_ROCM_COMPAT": "native"}):
            self.assertTrue(worker_rocm.configure_rocm_runtime("auto", [0, 1], self.logger))
        self.assertFalse(self.torch.backends.cudnn.enabled)
        self.assertEqual(self.probe.call_args_list, [
            mock.call(self.torch, "cuda:0", verify_reference=True),
            mock.call(self.torch, "cuda:1", verify_reference=True),
        ])

    def test_diagnostics_compare_both_backends_and_restore_the_setting(self):
        self.probe.side_effect = [RuntimeError("miopenStatusUnknownError"), None, None, None]
        report = worker_rocm.diagnose_rocm()
        self.assertFalse(report["checks"][0]["miopen"]["passed"])
        self.assertTrue(report["checks"][0]["nativeHip"]["passed"])
        self.assertTrue(self.torch.backends.cudnn.enabled)

    def test_optional_instance_norm_half_failure_cannot_block_automatic_startup(self):
        def instance_probe(torch, _device, *, amp, verify_reference):
            if amp and torch.backends.cudnn.enabled:
                raise RuntimeError("HIPRTC_ERROR_COMPILATION")

        self.instance_probe.side_effect = instance_probe
        self.assertFalse(worker_rocm.configure_rocm_runtime("cuda", [0], self.logger))
        self.assertTrue(self.torch.backends.cudnn.enabled)
        self.instance_probe.assert_not_called()

    def test_diagnostics_report_the_failing_instance_norm_case(self):
        def instance_probe(torch, _device, *, amp, verify_reference):
            if amp and torch.backends.cudnn.enabled:
                raise RuntimeError("HIPRTC_ERROR_COMPILATION")

        self.instance_probe.side_effect = instance_probe
        report = worker_rocm.diagnose_rocm()
        self.assertEqual(report["probeVersion"], 2)
        self.assertFalse(report["checks"][0]["miopen"]["passed"])
        self.assertEqual(report["checks"][0]["miopen"]["cases"][-1]["name"], "instanceNormFp16")
        self.assertIn("HIPRTC_ERROR_COMPILATION", report["checks"][0]["miopen"]["cases"][-1]["error"])
        self.assertTrue(report["checks"][0]["nativeHip"]["passed"])
        self.assertTrue(self.torch.backends.cudnn.enabled)


class NormalizationProbeTests(unittest.TestCase):
    def test_adapter_recovers_before_the_upstream_process_folder_swallows_errors(self):
        import worker_infer

        calls = []

        class Separator:
            def separate(self, mix, **kwargs):
                calls.append((mix, kwargs))
                if len(calls) == 1:
                    raise RuntimeError("miopenStatusUnknownError")
                return {"vocals": "audio"}

            def process_folder(self, _path):
                try:
                    return self.separate("mix", stems=["vocals"])
                except RuntimeError:
                    return {}

        pymss = types.ModuleType("pymss")
        pymss.MSSeparator = Separator
        with mock.patch.dict(sys.modules, {"pymss": pymss}), \
             mock.patch.object(worker_rocm, "configure_rocm_runtime", return_value=True) as configure:
            adapter = worker_infer._studio_separator_type()
            separator = adapter.__new__(adapter)
            separator.device = "cuda:0"
            separator.device_ids = [0, 1]
            separator.logger = mock.Mock()
            result = Separator.process_folder(separator, "input.flac")
        self.assertEqual(result, {"vocals": "audio"})
        self.assertEqual(calls, [("mix", {"stems": ["vocals"]})] * 2)
        self.assertEqual(configure.call_args.args, ("cuda:0", [0, 1], separator.logger))
        self.assertIn("miopenStatusUnknownError", str(configure.call_args.kwargs["failure"]))

    def test_separator_checks_the_selected_rocm_gpu_before_constructing_the_model(self):
        import worker_infer

        logger = mock.Mock()
        configured = []

        class Separator:
            def __init__(self, **kwargs):
                self.config = types.SimpleNamespace(training=types.SimpleNamespace(instruments=["vocals"]))
                self.assert_configured = list(configured)

            def _stems_to_save(self):
                return ["vocals"]

        pymss = types.ModuleType("pymss")
        pymss.MSSeparator = Separator
        with mock.patch.dict(sys.modules, {"pymss": pymss}), \
             mock.patch.object(worker_rocm, "configure_rocm_runtime", side_effect=lambda *args: configured.append(args)) as configure, \
             mock.patch.object(worker_infer, "_resolve_separator_device", return_value=("auto", [1], "cuda:1")), \
             mock.patch.object(worker_infer, "_resolve_studio_model", return_value={"model_type": "bs_roformer", "model_path": "model.ckpt"}), \
             mock.patch.object(worker_infer, "_enrich_inference_params_for_model", return_value={}), \
             mock.patch.object(worker_infer, "emit"):
            separator = worker_infer._prepare_separator(
                payload={"model": "model", "device": "cuda", "deviceIds": [1], "output": "outputs", "download": False},
                task_id="rocm-test", progress_callback=None, logger=logger,
            )
        configure.assert_called_once_with("auto", [1], logger)
        self.assertEqual(separator.assert_configured, [("auto", [1], logger)])

    def test_reference_probe_executes_with_real_torch_on_cpu(self):
        import torch
        previous = torch.get_default_dtype()
        try:
            torch.set_default_dtype(torch.float64)
            worker_rocm._probe_batch_norm(torch, torch.device("cpu"), verify_reference=True)
            worker_rocm._probe_instance_norm(torch, torch.device("cpu"), verify_reference=True)
        finally:
            torch.set_default_dtype(previous)


if __name__ == "__main__":
    unittest.main()
