from types import SimpleNamespace

import numpy as np
import pytest
import torch

from pymss import utils
from pymss.config import AttrDict


@pytest.fixture(params=["cpu", "cuda", "npu_control_flow"])
def overlap_device(request, monkeypatch):
    if request.param == "cuda":
        if not torch.cuda.is_available():
            pytest.skip("CUDA is unavailable")
        return "cuda"
    if request.param == "npu_control_flow":
        # Exercise NPU branching with CPU tensor storage, without claiming NPU kernel coverage.
        proxy = SimpleNamespace(**vars(torch))
        proxy.device = lambda device: SimpleNamespace(type="npu")
        monkeypatch.setattr(utils, "torch", proxy)
    return "cpu"


@pytest.mark.parametrize("counter_value", [1.0, 2.0, 0.0])
def test_finalize_cleans_nonfinite_values_and_preserves_inputs(overlap_device, counter_value):
    samples = torch.tensor([float("nan"), float("inf"), -float("inf"), 0.5, -2.0, 0.0])
    result = samples.repeat(2, 2, 1).to(overlap_device)
    before = result.clone()
    counter = torch.full((1, 1, samples.numel()), counter_value, device=overlap_device)
    with np.errstate(divide="ignore", invalid="ignore"):
        expected = np.nan_to_num(before.cpu().numpy() / counter_value, nan=0.0)

    actual = utils._finalize_overlap(result, counter, samples.numel(), 0)

    assert actual.dtype == np.float32
    assert np.isfinite(actual).all()
    np.testing.assert_array_equal(actual, expected)
    torch.testing.assert_close(result, before, equal_nan=True)


def test_finalize_preserves_border_crop_and_finite_samples(overlap_device):
    result = torch.arange(80, dtype=torch.float32, device=overlap_device).reshape(2, 2, 20)
    counter = torch.ones(1, 1, 20, device=overlap_device)

    actual = utils._finalize_overlap(result, counter, 12, 4)

    np.testing.assert_array_equal(actual, result[..., 4:16].cpu().numpy())


@pytest.mark.parametrize("length", [8, 33])
@pytest.mark.parametrize("overlap", [0, 8])
@pytest.mark.parametrize("invalid", [False, True])
def test_demix_handles_short_and_nonoverlapping_outputs(overlap_device, length, overlap, invalid):
    class Model(torch.nn.Module):
        def forward(self, audio):
            output = audio.clone()
            if invalid:
                output[..., 0] = float("nan")
                output[..., 1] = float("inf")
            return output

    config = AttrDict({
        "audio": {"chunk_size": 16},
        "training": {"instruments": ["vocals"], "target_instrument": None, "use_amp": False},
        "inference": {"batch_size": 1, "overlap_size": overlap},
    })
    mix = torch.from_numpy(np.random.default_rng(12).normal(size=(2, length)).astype(np.float32))

    actual = utils.demix_track(config, Model(), mix, overlap_device, pbar=False)["vocals"]

    assert actual.shape == (2, length)
    assert actual.dtype == np.float32
    assert np.isfinite(actual).all()
    if not invalid:
        np.testing.assert_allclose(actual, mix.numpy(), rtol=1e-6, atol=1e-6)
