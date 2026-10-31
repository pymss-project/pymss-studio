import logging
from unittest.mock import patch

import pytest
import torch
import yaml

import pymss
from pymss_core import ModelTypeDetectionError, detect_model_type, get_model_from_config


def test_public_detection_uses_core_implementation():
    assert pymss.detect_model_type is detect_model_type
    assert pymss.ModelTypeDetectionError is ModelTypeDetectionError


@pytest.mark.parametrize("config", [
    {}, {"model": {"dim": 8}},
    {"model": {"freqs_per_bands": [4, 5], "num_bands": 60}},
    {"model_type": "unknown"},
    {"model_type": "scnet", "model": {"type": "bs_roformer"}},
])
def test_auto_fails_before_loading_weights_or_changing_runtime_state(tmp_path, config):
    config_path = tmp_path / "model.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    benchmark = torch.backends.cudnn.benchmark
    with patch("pymss.separator._load_state_dict") as load_weights, patch("pymss.separator._resolve_public_device") as device:
        with pytest.raises(ModelTypeDetectionError, match="Set model_type explicitly"):
            pymss.MSSeparator("auto", tmp_path / "missing.ckpt", config_path, device="cpu", store_dirs={})
        load_weights.assert_not_called()
        device.assert_not_called()
    assert torch.backends.cudnn.benchmark == benchmark


@pytest.mark.parametrize("contents", [None, b"model: [", b"\xff\xfe"])
def test_auto_requires_readable_yaml(tmp_path, contents):
    config_path = tmp_path / "model.yaml"
    if contents is not None:
        config_path.write_bytes(contents)
    with pytest.raises(ModelTypeDetectionError, match="readable YAML") as error:
        pymss.MSSeparator("auto", tmp_path / "missing.ckpt", config_path, device="cpu", store_dirs={})
    assert error.value.__cause__ is not None


@pytest.mark.parametrize("manual", [False, True])
def test_separator_auto_and_manual_override_with_apollo(tmp_path, manual):
    config = {
        "audio": {"sample_rate": 16000, "chunk_size": 640},
        "model": {"sr": 16000, "win": 20, "feature_dim": 16, "layer": 1},
        "training": {"instruments": ["audio"], "target_instrument": None, "use_amp": False},
        "inference": {"batch_size": 1, "overlap_size": 320},
    }
    model_path = tmp_path / "apollo.pt"
    config_path = tmp_path / "apollo.pt.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    model, _ = get_model_from_config("apollo", config_path)
    torch.save(model.state_dict(), model_path)
    if manual:
        config["model_type"] = "unknown"
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    # Covers the default config path when model_path is a pathlib.Path.
    with pymss.MSSeparator("apollo" if manual else "auto", model_path, device="cpu", store_dirs={},
                           logger=logging.getLogger(__name__)) as separator:
        assert separator.model_type == "apollo"
        with torch.inference_mode():
            output = separator.model(torch.randn(1, 1, 640))
        assert output.shape == (1, 1, 640)
        assert torch.isfinite(output).all()
