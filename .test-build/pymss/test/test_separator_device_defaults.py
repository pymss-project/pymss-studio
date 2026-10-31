from __future__ import annotations

from types import SimpleNamespace

import pytest
import torch

from pymss.separator import _prefer_mlx_for_auto, _resolve_public_device, _select_device, _store_torch_model_on_cpu_for_mlx


class DummyLogger:
    def debug(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass


def test_device_rocm_maps_to_cuda(monkeypatch):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.version, "hip", "6.4.47833", raising=False)

    device, params = _resolve_public_device("rocm", {}, DummyLogger())

    assert device == "cuda"
    assert params == {}


def test_device_rocm_requires_rocm_build(monkeypatch):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

    with pytest.raises(RuntimeError, match="rocm"):
        _resolve_public_device("rocm", {}, DummyLogger())


def test_select_device_auto_prefers_hip_as_cuda(monkeypatch):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "device_count", lambda: 1)
    monkeypatch.setattr(torch.version, "hip", "6.4.47833", raising=False)

    assert _select_device("auto", [0], DummyLogger()) == "cuda:0"


def test_select_device_cuda_prefers_hip_as_cuda(monkeypatch):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "device_count", lambda: 1)
    monkeypatch.setattr(torch.version, "hip", "6.4.47833", raising=False)

    assert _select_device("cuda", [0], DummyLogger()) == "cuda:0"


@pytest.mark.parametrize("device", ["cuda", "auto"])
@pytest.mark.parametrize("ids", [[1], [2, 0]])
def test_cuda_primary_device_matches_first_adapter_id(monkeypatch, device, ids):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "device_count", lambda: 3)

    assert _select_device(device, ids, DummyLogger()) == f"cuda:{ids[0]}"


@pytest.mark.parametrize("device", ["cuda", "auto"])
@pytest.mark.parametrize("ids", [None, [], [-1], [3], [0, 3], [True], [1.5], ["1"]])
def test_invalid_cuda_ids_fail_before_model_loading(monkeypatch, device, ids):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "device_count", lambda: 3)

    with pytest.raises(ValueError, match="CUDA"):
        _select_device(device, ids, DummyLogger())


def test_explicit_cuda_requires_cuda_instead_of_using_another_accelerator(monkeypatch):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: True)

    import pymss.separator as separator_module

    monkeypatch.setattr(separator_module, "_npu_runtime_available", lambda: False)

    with pytest.raises(RuntimeError, match="CUDA"):
        _select_device("cuda", [0], DummyLogger())


@pytest.mark.parametrize("runtime", ["cuda", "rocm"])
@pytest.mark.parametrize("node_ids", ["1", "1,0"])
def test_graph_auto_node_uses_the_first_id_with_an_explicit_gpu_runtime(monkeypatch, runtime, node_ids):
    from pymss.graph.nodes import _common_separator_kwargs

    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "device_count", lambda: 2)
    monkeypatch.setattr(torch.version, "hip", "6.4.47833" if runtime == "rocm" else None, raising=False)
    logger = DummyLogger()
    context = SimpleNamespace(device=runtime, logger=logger)
    kwargs = _common_separator_kwargs(
        context, device="auto", device_ids_raw=node_ids, params={}, use_tta=False, debug=False, stems=["vocals"],
    )
    device, _params = _resolve_public_device(kwargs["device"], {}, logger)

    assert device == "cuda"
    assert _select_device(device, kwargs["device_ids"], logger) == "cuda:1"


@pytest.mark.parametrize("device", ["cpu", "mps"])
def test_explicit_non_cuda_devices_keep_their_device_selection(monkeypatch, device):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

    assert _select_device(device, [3], DummyLogger()) == device


@pytest.mark.parametrize(("backend", "device", "keep_cpu"), [
    ("mlx_full", "mps", True),
    ("torch", "mps", False),
    ("mlx_full", "cuda", False),
    ("mlx_full", "cpu", False),
])
def test_model_placement_follows_effective_backend(backend, device, keep_cpu):
    model = SimpleNamespace(mps_model_backend=backend)
    assert _store_torch_model_on_cpu_for_mlx(model, device) is keep_cpu


def test_model_without_mlx_support_is_not_left_on_cpu_for_mps():
    assert not _store_torch_model_on_cpu_for_mlx(torch.nn.Linear(2, 2), "mps")
