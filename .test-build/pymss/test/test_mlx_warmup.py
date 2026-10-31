from __future__ import annotations

from types import SimpleNamespace

import pytest

from pymss.config import AttrDict
from pymss.separator import _prefer_mlx_for_auto, _resolve_public_device
from pymss.utils import (
    _mlx_warmup_audio_length,
    _mlx_warmup_channel_candidates,
    _mlx_warmup_length_candidates,
    warmup_mlx_full,
)

class DummyLogger:
    def debug(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

def _config(*, chunk_size=960000, batch_size=2):
    return AttrDict(
        {
            "audio": {"chunk_size": chunk_size},
            "inference": {"batch_size": batch_size},
        }
    )

class FakeModel:
    """Minimal stand-in recording the shapes the warmup pass requests."""

    mps_model_backend = "mlx_full"
    mps_model_compute_dtype = None

    def modules(self):
        return [self]

    def parameters(self):
        import torch

        if not hasattr(self, "_param"):
            self._param = torch.zeros(1)  # stays on CPU: never actually moved, `to` is recorded only
        return iter([self._param])

    def to(self, device):
        self.moved_to = str(device)  # mock the move; real tensor.to("mps") is unavailable on non-Apple platforms
        return self

    def __init__(self, *, audio_channels=2, stft_kwargs=None, error=None, allowed_channels=None, allowed_lengths=None):
        import torch

        self.audio_channels = audio_channels
        self.stft_kwargs = stft_kwargs if stft_kwargs is not None else {"n_fft": 2048, "hop_length": 512}
        self.mps_model_compute_dtype = torch.float16
        self.error = error
        self.allowed_channels = allowed_channels
        self.allowed_lengths = allowed_lengths
        self.calls = []

    def mlx_forward_mx(self, raw_audio):
        self.calls.append(tuple(raw_audio.shape))
        if self.error is not None:
            raise self.error
        if self.allowed_channels is not None and raw_audio.shape[1] not in self.allowed_channels:
            raise ValueError("raw_audio channel count does not match RoFormer stereo setting")
        if self.allowed_lengths is not None and raw_audio.shape[-1] not in self.allowed_lengths:
            raise ValueError("incompatible STFT frame count")
        return raw_audio

@pytest.fixture()
def fake_mlx(monkeypatch):
    """Provide a fake ``mlx.core`` so warmup logic is testable without MLX."""

    class FakeArray:
        def __init__(self, shape, dtype):
            self.shape = shape
            self.dtype = dtype

    class FakeMx:
        float16 = "float16"
        float32 = "float32"

        def __init__(self):
            self.zero_shapes = []
            self.eval_calls = 0

        def zeros(self, shape, dtype=None):
            self.zero_shapes.append((tuple(shape), dtype))
            return FakeArray(tuple(shape), dtype)

        def eval(self, *_args):
            self.eval_calls += 1

    fake = FakeMx()
    monkeypatch.setitem(__import__("sys").modules, "mlx.core", fake)
    monkeypatch.setitem(__import__("sys").modules, "mlx", SimpleNamespace(core=fake))
    cleared = []
    import pymss.utils as utils

    monkeypatch.setattr(utils, "clear_mlx_cache", lambda: cleared.append(True))
    return fake, cleared

def test_device_mlx_enables_warmup_by_default(monkeypatch):
    import torch

    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: True)

    device, params = _resolve_public_device("mlx", {}, DummyLogger())

    assert device == "mps"

def test_warmup_audio_length_follows_stft_settings():
    assert _mlx_warmup_audio_length(FakeModel(stft_kwargs={"n_fft": 2048, "hop_length": 512})) == 32768
    assert _mlx_warmup_audio_length(FakeModel(stft_kwargs={"n_fft": 4096, "hop_length": 1024})) == 65536


def test_warmup_audio_length_reads_subband_stft_object():
    class SubbandSTFTStub:
        n_fft, hop_length = 2048, 512

    model = FakeModel()
    del model.stft_kwargs
    model.stft = SubbandSTFTStub()

    assert _mlx_warmup_audio_length(model) == 32768

def test_warmup_audio_length_survives_missing_stft_kwargs():
    model = FakeModel()
    del model.stft_kwargs

    cands = _mlx_warmup_length_candidates(model)
    assert cands and all((L // 512 + 1) % 16 == 0 for L in cands)

@pytest.mark.parametrize("scale,num_scales", [((3, 2), 2), ((3, 2), 5), ((2, 2), 4), ((4, 2), 2)])
def test_warmup_aligns_to_full_mdx_encoder(fake_mlx, monkeypatch, scale, num_scales):
    import torch
    from pymss_core.modules.mdx23c_tfc_tdf_v3 import TFC_TDF_net

    config = AttrDict({
        "audio": {"n_fft": 2048, "hop_length": 512, "dim_f": 2 * scale[1] ** num_scales, "num_channels": 2},
        "model": {"num_subbands": 1, "num_scales": num_scales, "scale": scale, "num_blocks_per_scale": 1,
                  "num_channels": 4, "growth": 4, "bottleneck_factor": 2, "norm": "none", "act": "relu"},
        "training": {"instruments": ["vocals"], "target_instrument": "vocals"},
    })
    model = TFC_TDF_net(config).eval()
    lengths = _mlx_warmup_length_candidates(model)
    frames = [length // model.stft.hop_length + 1 for length in lengths]
    assert frames and all(count % (scale[0] ** num_scales) == 0 for count in frames)
    if scale == (3, 2) and num_scales == 5:
        assert frames[0] == 243
    calls = []

    def forward(raw_audio):
        calls.append(tuple(raw_audio.shape))
        count = raw_audio.shape[-1] // model.stft.hop_length + 1
        x = torch.zeros(raw_audio.shape[0], config.model.num_channels, count, model.stft.dim_f)
        with torch.inference_mode():
            y = model._forward_core(x)
        assert y.shape == x.shape
        return raw_audio

    monkeypatch.setattr(model, "mlx_forward_mx", forward)
    assert warmup_mlx_full(model, _config(chunk_size=None)) is True
    assert len(calls) == 1
    assert calls[0][-1] == lengths[0]


def test_warmup_alignment_uses_each_downscale_stride():
    import torch.nn as nn

    model = FakeModel()
    model.encoder_blocks = [
        SimpleNamespace(downscale=nn.Sequential(nn.Identity(), nn.Conv2d(1, 1, (stride, 2), stride=(stride, 2))))
        for stride in (2, 3, 5)
    ]
    model.mdx_scale = [3, 2]
    lengths = _mlx_warmup_length_candidates(model)
    assert lengths and all((length // 512 + 1) % 30 == 0 for length in lengths)


def test_warmup_channel_candidates_prefers_declared_channels():
    assert _mlx_warmup_channel_candidates(FakeModel(audio_channels=1)) == (1,)
    assert _mlx_warmup_channel_candidates(FakeModel(audio_channels=2)) == (2,)

    model = FakeModel()
    del model.audio_channels

    assert _mlx_warmup_channel_candidates(model) == (2, 1)

def test_warmup_uses_real_shape_directly_when_channels_known(fake_mlx):
    fake, cleared = fake_mlx
    model = FakeModel()

    assert warmup_mlx_full(model, _config(chunk_size=960000, batch_size=2)) is True

    assert model.calls == [(2, 2, 960000)]
    assert fake.zero_shapes == [((2, 2, 960000), "float16")]
    assert fake.eval_calls == 1
    assert len(cleared) == 1
    assert not hasattr(model, "_pymss_mlx_full_backend_error")


def test_warmup_keeps_channel_probe_before_unknown_real_input(fake_mlx):
    fake, cleared = fake_mlx
    model = FakeModel(audio_channels=None, allowed_channels=(1,))
    assert warmup_mlx_full(model, _config(batch_size=2)) is True
    short = _mlx_warmup_length_candidates(model)[0]
    assert model.calls == [(1, 2, short), (1, 1, short), (2, 1, 960000)]
    assert fake.eval_calls == 2 and len(cleared) == 2


def test_known_channels_still_validate_real_shape_and_report_failure(fake_mlx):
    _, cleared = fake_mlx
    model = FakeModel(allowed_lengths=(32768,))
    assert warmup_mlx_full(model, _config()) is False
    assert model.calls == [(2, 2, 960000)]
    assert "STFT frame count" in model._pymss_mlx_full_backend_error
    assert len(cleared) == 1

def test_warmup_skips_real_shape_phase_without_chunk_size(fake_mlx):
    fake, cleared = fake_mlx
    model = FakeModel()

    assert warmup_mlx_full(model, _config(chunk_size=None, batch_size=2)) is True

    assert len(model.calls) == 1  # first successful tiny length stops the probe
    assert len(cleared) == 1

def test_warmup_probes_channel_count_on_value_error(fake_mlx):
    model = FakeModel(audio_channels=None, allowed_channels=(1,))
    del model.audio_channels

    assert warmup_mlx_full(model, _config(chunk_size=None)) is True

    assert [shape[1] for shape in model.calls] == [2, 1]  # channel probe order on the first candidate

@pytest.mark.parametrize("batch_size", [1, 2])
def test_warmup_runs_real_shape_once_after_short_probe_exhaustion(fake_mlx, batch_size):
    fake, cleared = fake_mlx
    model = FakeModel(audio_channels=None, allowed_lengths=(960000,))
    lengths = _mlx_warmup_length_candidates(model)

    assert warmup_mlx_full(model, _config(batch_size=batch_size)) is True
    assert model.calls == [(1, channels, length) for length in lengths for channels in (2, 1)] + [(batch_size, 2, 960000)]
    assert fake.eval_calls == 1
    assert len(cleared) == 1
    assert model.mps_model_backend == "mlx_full"
    assert not hasattr(model, "_pymss_mlx_full_backend_error")


def test_warmup_short_probe_exhaustion_without_real_shape_fails(fake_mlx):
    _, cleared = fake_mlx
    model = FakeModel(allowed_lengths=())

    assert warmup_mlx_full(model, _config(chunk_size=None)) is False
    assert "STFT frame count" in model._pymss_mlx_full_backend_error
    assert len(cleared) == 1


def test_warmup_real_shape_error_is_not_retried(fake_mlx, monkeypatch):
    _, cleared = fake_mlx
    model = FakeModel(allowed_lengths=())
    original = model.mlx_forward_mx

    def forward(raw_audio):
        if raw_audio.shape[-1] == 960000:
            model.calls.append(tuple(raw_audio.shape))
            raise RuntimeError("metal exploded")
        return original(raw_audio)

    monkeypatch.setattr(model, "mlx_forward_mx", forward)
    assert warmup_mlx_full(model, _config()) is False
    assert [shape for shape in model.calls if shape[-1] == 960000] == [(2, 2, 960000)]
    assert "metal exploded" in model._pymss_mlx_full_backend_error
    assert len(cleared) == 1


def test_warmup_records_error_and_returns_false(fake_mlx):
    model = FakeModel(error=RuntimeError("metal exploded"))

    assert warmup_mlx_full(model, _config()) is False
    assert "metal exploded" in model._pymss_mlx_full_backend_error

def test_warmup_returns_false_for_not_implemented_backend(fake_mlx):
    model = FakeModel(error=NotImplementedError("PoPE models require the PyTorch backend"))

    assert warmup_mlx_full(model, _config()) is False
    assert not hasattr(model, "_pymss_mlx_full_backend_error")

def test_warmup_reports_channel_probe_exhaustion(fake_mlx):
    model = FakeModel(audio_channels=None, allowed_channels=())
    del model.audio_channels

    assert warmup_mlx_full(model, _config(chunk_size=None)) is False
    assert "channel count" in model._pymss_mlx_full_backend_error

def _separator_stub(device="mps"):
    from pymss.separator import MSSeparator

    stub = SimpleNamespace(device=device, logger=DummyLogger())
    stub._warmup_mlx_full_backend = MSSeparator._warmup_mlx_full_backend.__get__(stub)
    return stub

def test_load_model_warmup_skips_non_mps_device(monkeypatch):
    import pymss.utils as utils

    called = []
    monkeypatch.setattr(utils, "warmup_mlx_full", lambda *args: called.append(args) or True)
    stub = _separator_stub(device="cpu")
    model = FakeModel()

    stub._warmup_mlx_full_backend(model, _config())

    assert not called

def test_load_model_warmup_skips_torch_backend(monkeypatch):
    import pymss.utils as utils

    called = []
    monkeypatch.setattr(utils, "warmup_mlx_full", lambda *args: called.append(args) or True)
    stub = _separator_stub()
    model = FakeModel()
    model.mps_model_backend = "torch"

    stub._warmup_mlx_full_backend(model, _config())

    assert not called

def test_load_model_warmup_failure_downgrades_to_torch(monkeypatch):
    import pymss.utils as utils

    def failing(_model, _config):
        _model._pymss_mlx_full_backend_error = "repr: boom"
        return False

    monkeypatch.setattr(utils, "warmup_mlx_full", failing)
    stub = _separator_stub()
    model = FakeModel()

    stub._warmup_mlx_full_backend(model, _config())

    assert model.mps_model_backend == "torch"

def test_load_model_warmup_success_keeps_mlx_backend(monkeypatch):
    import pymss.utils as utils

    monkeypatch.setattr(utils, "warmup_mlx_full", lambda _model, _config: True)
    stub = _separator_stub()
    model = FakeModel()

    stub._warmup_mlx_full_backend(model, _config())

    assert model.mps_model_backend == "mlx_full"
