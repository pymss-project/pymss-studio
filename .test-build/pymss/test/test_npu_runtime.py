from types import SimpleNamespace

import numpy as np
import pytest
import torch

from pymss import separator as separator_module
from pymss.modules.vocal_remover.uvr_lib_v5 import spec_utils
from test.test_separator_device_defaults import DummyLogger


def _available_npu(monkeypatch):
    monkeypatch.setattr(separator_module, "_npu_runtime_available", lambda: True)
    monkeypatch.setattr(torch, "npu", SimpleNamespace(device_count=lambda: 3), raising=False)


@pytest.mark.parametrize("device", ["npu", "auto"])
@pytest.mark.parametrize("ids", [[0], [1], [2, 0]])
def test_npu_selection_uses_the_requested_first_device(monkeypatch, device, ids):
    _available_npu(monkeypatch)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    assert separator_module._select_device(device, ids, DummyLogger()) == f"npu:{ids[0]}"


@pytest.mark.parametrize("device", ["npu", "auto"])
@pytest.mark.parametrize("ids", [None, [], [-1], [3], [0, 3], [True], [1.5], ["1"]])
def test_invalid_npu_ids_fail_before_loading_a_model(monkeypatch, device, ids):
    _available_npu(monkeypatch)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    with pytest.raises(ValueError, match="NPU"):
        separator_module._select_device(device, ids, DummyLogger())


@pytest.mark.parametrize("device", ["cpu", "mps", "cuda"])
def test_explicit_existing_device_does_not_probe_npu(monkeypatch, device):
    def forbidden_probe():
        raise AssertionError("Explicit devices must not initialize another backend")

    monkeypatch.setattr(separator_module, "_npu_runtime_available", forbidden_probe)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    if device == "cuda":
        with pytest.raises(RuntimeError, match="CUDA"):
            separator_module._select_device(device, [0], DummyLogger())
    else:
        assert separator_module._select_device(device, [0], DummyLogger()) == device


def test_auto_prefers_cuda_without_initializing_npu(monkeypatch):
    def forbidden_probe():
        raise AssertionError("CUDA selection must not initialize NPU")

    monkeypatch.setattr(separator_module, "_npu_runtime_available", forbidden_probe)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "device_count", lambda: 1)
    assert separator_module._select_device("auto", [0], DummyLogger()) == "cuda:0"


def test_explicit_npu_fails_when_runtime_is_unavailable(monkeypatch):
    monkeypatch.setattr(separator_module, "_npu_runtime_available", lambda: False)
    with pytest.raises(RuntimeError, match="torch_npu"):
        separator_module._resolve_public_device("npu", {}, DummyLogger())


def test_vr_npu_hann_window_is_created_on_cpu(monkeypatch):
    created = []
    result = object()

    def factory(*args, **kwargs):
        tensor = torch.hann_window(*args, **kwargs)
        created.append(tensor.device.type)
        return SimpleNamespace(to=lambda device: result)

    proxy = SimpleNamespace(device=lambda device: SimpleNamespace(type="npu", index=0), hann_window=factory)
    monkeypatch.setattr(spec_utils, "torch", proxy)
    monkeypatch.setattr(spec_utils, "_HANN_WINDOW_CACHE", {})
    with torch.device("meta"):
        assert spec_utils._hann_window(16, torch.float32, "npu:0") is result
        assert spec_utils._hann_window(16, torch.float32, "npu:0") is result
    assert created == ["cpu"]


@pytest.mark.skipif(not torch.cuda.is_available(), reason="CUDA is unavailable")
def test_vr_cuda_stft_keeps_double_precision():
    wave = np.random.default_rng(24).normal(size=(2, 97)).astype(np.float64)
    actual = spec_utils._torch_stft(wave, 16, 4, "cuda")
    expected = torch.stft(torch.from_numpy(wave).cuda(), 16, 4,
                          window=torch.hann_window(16, dtype=torch.float64, device="cuda"),
                          center=True, pad_mode="constant", return_complex=True).cpu().numpy()
    assert actual.dtype == np.complex128
    np.testing.assert_allclose(actual, expected, atol=1e-11, rtol=1e-11)
