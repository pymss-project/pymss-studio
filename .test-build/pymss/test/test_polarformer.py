from __future__ import annotations

import logging

import pytest
import torch
import yaml

from pymss import MSSeparator
from pymss_core import get_model_from_config
from pymss.separator import _store_torch_model_on_cpu_for_mlx


@pytest.fixture
def polarformer_files(tmp_path):
    config = {
        "audio": {"chunk_size": 64, "sample_rate": 44100, "num_channels": 2},
        "model": {
            "dim": 8, "depth": 1, "heads": 2, "dim_head": 4,
            "stereo": True, "num_stems": 1, "time_transformer_depth": 1,
            "freq_transformer_depth": 1, "freqs_per_bands": [4, 5],
            "stft_n_fft": 16, "stft_hop_length": 4, "stft_win_length": 16,
            "mask_estimator_depth": 1, "use_pope": True,
        },
        "training": {"instruments": ["lead", "back_instrum"], "target_instrument": "lead", "use_amp": False},
        "inference": {"chunk_size": 128, "num_overlap": 2, "batch_size": 1},
    }
    config_path = tmp_path / "polarformer.yaml"
    model_path = tmp_path / "polarformer.ckpt"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    model, _ = get_model_from_config("bs_roformer", config_path)
    torch.save(model.state_dict(), model_path)
    return model_path, config_path


@pytest.mark.parametrize("model_type", ["bs_roformer", "auto"])
def test_separator_loads_pope_weights_and_preserves_stem_configuration(polarformer_files, model_type):
    model_path, config_path = polarformer_files
    with MSSeparator(model_type=model_type, model_path=model_path, config_path=config_path,
                     device="cpu", store_dirs={}, logger=logging.getLogger(__name__)) as separator:
        assert separator.model_type == "bs_roformer"
        assert separator.config.training.instruments == ["lead", "back_instrum"]
        assert separator.config.training.target_instrument == "lead"
        assert separator.config.audio.chunk_size == 128
        assert separator.config.inference.overlap_size == 64
        assert separator.model.use_pope
        with torch.inference_mode():
            output = separator.model(torch.randn(1, 2, 128))
        assert output.shape == (1, 2, 128)
        assert torch.isfinite(output).all()


def test_mlx_request_uses_torch_placement_after_pope_fallback(polarformer_files, caplog):
    model_path, config_path = polarformer_files
    logger = logging.getLogger(__name__)
    with caplog.at_level(logging.WARNING, logger=__name__):
        with MSSeparator(model_type="bs_roformer", model_path=model_path, config_path=config_path,
                         device="cpu", store_dirs={}, logger=logger,
                         inference_params={"mps_model_backend": "mlx_full", "mps_attention_backend": "mlx_transformer"}) as separator:
            assert separator.model.mps_model_backend == "torch"
            assert not _store_torch_model_on_cpu_for_mlx(separator.model, "mps")
            assert not any(getattr(module, "mps_attention_backend", "torch") != "torch" for module in separator.model.modules())
    assert "using 'torch'" in caplog.text
