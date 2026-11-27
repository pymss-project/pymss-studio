from __future__ import annotations

import types
import unittest
from unittest import mock

import worker_infer_accel


class _RoformerModel:
    def __init__(self) -> None:
        self.half_calls = 0
        self.band_split = object()

    def half(self) -> "_RoformerModel":
        self.half_calls += 1
        return self

    def modules(self) -> list[object]:
        return [self]


def _cuda_torch(total_memory: int) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        version=types.SimpleNamespace(hip=None),
        device=lambda value: types.SimpleNamespace(type="cuda", index=0),
        cuda=types.SimpleNamespace(
            get_device_properties=lambda index: types.SimpleNamespace(total_memory=total_memory),
        ),
        backends=types.SimpleNamespace(
            cuda=types.SimpleNamespace(matmul=types.SimpleNamespace(allow_tf32=False)),
            cudnn=types.SimpleNamespace(allow_tf32=False),
        ),
    )


class OptimizeSeparatorInferenceTests(unittest.TestCase):
    def test_roformer_weights_stay_in_full_precision_under_amp(self):
        # Pure FP16 RoFormer weights turn chunks containing digital silence into NaN,
        # which the overlap-add replaces with zeros (silent song intros and endings).
        model = _RoformerModel()
        separator = types.SimpleNamespace(
            model=model,
            config=types.SimpleNamespace(inference={"batch_size": 1, "use_amp": True}),
            model_type="bs_roformer",
            device="cuda:0",
        )
        with mock.patch.dict(worker_infer_accel.os.environ, {}, clear=True):
            result = worker_infer_accel.optimize_separator_inference(
                separator,
                explicit_params={},
                amp_enabled=True,
                torch_mod=_cuda_torch(8 * 1024 ** 3),
                platform="win32",
            )

        self.assertEqual(model.half_calls, 0)
        self.assertTrue(separator.config.inference["use_amp"])
        self.assertNotIn("fp16Weights", result)
        self.assertTrue(result["tf32"])
        self.assertEqual(result["autoBatchSize"], 2)
        self.assertEqual(separator.config.inference["batch_size"], 2)


if __name__ == "__main__":
    unittest.main()
