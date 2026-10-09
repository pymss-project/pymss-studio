import unittest
import sys
import types
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap  # noqa: F401
else:
    import _bootstrap as _worker_test_bootstrap  # noqa: F401

from worker_inference_constraints import InferenceParameterError, chunk_size_constraint, validate_chunk_size
import worker_infer
import worker_workflows


class MdxChunkConstraintTests(unittest.TestCase):
    def setUp(self):
        self.config = {"audio": {"hop_length": 1024, "n_fft": 8192}, "model": {"num_scales": 5, "scale": [2, 2]}}

    def test_constraint_includes_frame_offset_and_all_five_scales(self):
        self.assertEqual(chunk_size_constraint("mdx23c", self.config), {"step": 32768, "offset": 31744, "min": 31744})
        for value in [261120, 457728, 490496]:
            validate_chunk_size("mdx23c", self.config, value)

    def test_invalid_chunks_report_neighboring_valid_values(self):
        with self.assertRaisesRegex(InferenceParameterError, "457728 or 490496"):
            validate_chunk_size("mdx23c", self.config, 465920)
        for value in [0, -1, 474112, 465920.5, True]:
            with self.subTest(value=value), self.assertRaises(InferenceParameterError):
                validate_chunk_size("mdx23c", self.config, value)

    def test_custom_scale_and_fft_reflection_minimum_are_respected(self):
        config = {"audio": {"hop_length": 512, "n_fft": 8192}, "model": {"num_scales": 1, "scale": [2, 2]}}
        self.assertEqual(chunk_size_constraint("mdx23c", config), {"step": 1024, "offset": 512, "min": 4608})
        validate_chunk_size("mdx23c", config, 4608)
        with self.assertRaises(InferenceParameterError):
            validate_chunk_size("mdx23c", config, 512)

    def test_other_architectures_and_incomplete_metadata_keep_existing_behavior(self):
        self.assertIsNone(chunk_size_constraint("bs_roformer", self.config))
        self.assertIsNone(chunk_size_constraint("mdx23c", {"audio": {"hop_length": 1024}}))
        validate_chunk_size("bs_roformer", self.config, 465920)

    def test_studio_adapter_validates_effective_configuration_after_parent_update(self):
        class BaseSeparator:
            def update_inference_params(self, config, params):
                config["audio"]["chunk_size"] = params["chunk_size"]
                return config

        pymss = types.ModuleType("pymss")
        pymss.MSSeparator = BaseSeparator
        with mock.patch.dict(sys.modules, {"pymss": pymss}):
            adapter = worker_infer._studio_separator_type()
            separator = adapter.__new__(adapter)
            separator.model_type = "mdx23c"
            with self.assertRaises(InferenceParameterError):
                separator.update_inference_params(self.config, {"chunk_size": 465920})
            self.assertIs(separator.update_inference_params(self.config, {"chunk_size": 457728}), self.config)

    def test_workflow_factory_preserves_named_download_and_custom_path_semantics(self):
        separator_type = mock.Mock()
        with mock.patch.object(worker_infer, "_studio_separator_type", return_value=separator_type):
            worker_workflows._workflow_separator_factory(model_name="mdx", model_dir="models", download=True, device="cpu")
            separator_type.from_model_name.assert_called_once_with("mdx", model_dir="models", download=True, device="cpu")
            worker_workflows._workflow_separator_factory(model_path="weights.ckpt", model_type="mdx23c", config_path="custom.yaml", model_dir="ignored", inference_params={"chunk_size": 457728})
            separator_type.assert_called_once_with(model_path="weights.ckpt", model_type="mdx23c", config_path="custom.yaml", inference_params={"chunk_size": 457728})

    def test_invalid_inference_parameters_have_a_distinct_error_code(self):
        with mock.patch.object(worker_infer, "emit_error", return_value=1) as emit_error:
            self.assertEqual(worker_infer._emit_inference_error(InferenceParameterError("Invalid chunk size"), "task"), 1)
            emit_error.assert_called_once_with("INFERENCE_PARAMS_INVALID", "Invalid chunk size", task_id="task")
