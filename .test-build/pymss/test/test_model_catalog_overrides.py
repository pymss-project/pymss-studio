import logging
from unittest.mock import patch

import numpy as np
import pytest
import torch
import yaml

from pymss.config import AttrDict
from pymss.model_registry import create_separator, get_model_entry, resolve_model
from pymss.separator import (
    MSSeparator,
    _apply_target_instrument_override,
    _build_results,
)


MODEL_NAME = "model_mel_band_roformer_ep_0_sdr_11.4805.ckpt"


def test_issue_64_catalog_entry_corrects_the_reported_stem_order():
    entry = get_model_entry(MODEL_NAME)
    assert entry.category_path == "vocal/vocal_extraction"
    assert entry.target_stem == "Vocals"
    assert entry.target_instrument_override == "Vocals"

    resolved = resolve_model(MODEL_NAME, require_exists=False)
    assert resolved["target_instrument_override"] == "Vocals"


def test_target_instrument_override_uses_the_configured_stem_casing():
    config = AttrDict(
        {
            "training": {
                "instruments": ["Vocals", "Instrumental"],
                "target_instrument": "Instrumental",
            },
        }
    )

    _apply_target_instrument_override(config, "vocals")

    assert config.training.target_instrument == "Vocals"


def test_target_instrument_override_rejects_unknown_stems():
    config = AttrDict(
        {
            "training": {
                "instruments": ["Vocals", "Instrumental"],
                "target_instrument": "Instrumental",
            },
        }
    )

    with pytest.raises(ValueError, match="is not present in configured instruments"):
        _apply_target_instrument_override(config, "Other")


def test_corrected_target_labels_the_prediction_as_vocals_and_residual_as_instrumental():
    config = AttrDict(
        {
            "training": {
                "instruments": ["Vocals", "Instrumental"],
                "target_instrument": "Instrumental",
            },
        }
    )
    mix = np.array([[0.8, -0.4]], dtype=np.float32)
    predicted = np.array([[0.3, -0.1]], dtype=np.float32)

    _apply_target_instrument_override(config, "Vocals")
    results = _build_results(
        {"Vocals": predicted},
        config.training.instruments,
        mix,
        config,
        None,
        logging.getLogger(__name__),
    )

    np.testing.assert_allclose(results["Vocals"], predicted.T)
    np.testing.assert_allclose(results["Instrumental"], (mix - predicted).T)


def test_create_separator_forwards_catalog_target_override():
    resolved = {
        "model_type": "mel_band_roformer",
        "model_path": "model.ckpt",
        "config_path": "model.yaml",
        "source": "catalog",
        "inference_params": {},
        "target_instrument_override": "Vocals",
    }
    sentinel = object()
    with (
        patch("pymss.model_registry.resolve_model", return_value=resolved),
        patch("pymss.separator.MSSeparator", return_value=sentinel) as separator,
    ):
        assert create_separator(MODEL_NAME) is sentinel

    assert separator.call_args.kwargs["target_instrument_override"] == "Vocals"


def test_from_model_name_forwards_catalog_target_override():
    resolved = {
        "model_type": "mel_band_roformer",
        "model_path": "model.ckpt",
        "config_path": "model.yaml",
        "source": "catalog",
        "inference_params": {},
        "target_instrument_override": "Vocals",
    }
    with (
        patch("pymss.model_registry.resolve_model", return_value=resolved),
        patch.object(MSSeparator, "__init__", return_value=None) as initialize,
    ):
        MSSeparator.from_model_name(MODEL_NAME)

    assert initialize.call_args.kwargs["target_instrument_override"] == "Vocals"


@pytest.fixture
def model_files(tmp_path, monkeypatch):
    from pymss.config import load_config

    weights = tmp_path / MODEL_NAME
    weights.touch()
    config_path = tmp_path / "custom.yaml"
    config_path.write_text(yaml.safe_dump({
        "audio": {"chunk_size": 1024, "sample_rate": 44100},
        "model": {},
        "inference": {"batch_size": 1, "num_overlap": 1},
        "training": {"instruments": ["Drums", "Bass"], "target_instrument": "Drums"},
    }), encoding="utf-8")
    monkeypatch.setattr("pymss.separator._load_state_dict", lambda *_args: {})
    monkeypatch.setattr("pymss.separator.get_model_from_config", lambda _type, path, **_kwargs: (
        torch.nn.Identity(), load_config(path),
    ))
    monkeypatch.setattr(MSSeparator, "log_system_info", lambda _self: None)
    monkeypatch.setattr(MSSeparator, "check_ffmpeg_installed", lambda _self: None)
    return weights, config_path


def test_explicit_files_preserve_target_despite_catalog_filename(model_files):
    weights, config_path = model_files
    with MSSeparator("bs_roformer", weights, config_path, device="cpu") as separator:
        assert separator.target_instrument_override is None
        assert separator.config.training.target_instrument == "Drums"


def test_explicit_target_override_still_applies_to_direct_files(model_files):
    weights, config_path = model_files
    with MSSeparator("bs_roformer", weights, config_path, device="cpu", target_instrument_override="bass") as separator:
        assert separator.config.training.target_instrument == "Bass"


@pytest.mark.parametrize("factory", [create_separator, MSSeparator.from_model_name])
@pytest.mark.parametrize("override_kwargs", [{}, {"target_instrument_override": None}], ids=["omitted", "none"])
def test_registered_model_filename_does_not_apply_catalog_override(
    model_files, tmp_path, monkeypatch, factory, override_kwargs
):
    from pymss import user_models

    weights, config_path = model_files
    registry = tmp_path / "user_models.json"
    monkeypatch.setattr(user_models, "DEFAULT_USER_MODELS_PATH", registry)
    user_models.register_user_model("custom-model", "bs_roformer", weights, config_path, path=registry)
    assert resolve_model("custom-model")["source"] == "user"

    with factory("custom-model", device="cpu", **override_kwargs) as separator:
        assert separator.target_instrument_override is None
        assert separator.config.training.target_instrument == "Drums"


@pytest.mark.parametrize("factory", [create_separator, MSSeparator.from_model_name])
@pytest.mark.parametrize("override_kwargs,expected_override,expected_target", [
    ({}, "Vocals", "Vocals"),
    ({"target_instrument_override": None}, "Vocals", "Vocals"),
    ({"target_instrument_override": "instrumental"}, "instrumental", "Instrumental"),
    ({"target_instrument_override": ""}, "", "Instrumental"),
], ids=["omitted", "none", "explicit", "disabled"])
def test_catalog_name_applies_override_during_model_loading(
    model_files, tmp_path, factory, override_kwargs, expected_override, expected_target
):
    from pymss.model_registry import config_path_for, model_path_for

    _, original_config = model_files
    entry = get_model_entry(MODEL_NAME)
    weights = model_path_for(entry, tmp_path)
    config_path = config_path_for(entry, tmp_path)
    weights.parent.mkdir(parents=True, exist_ok=True)
    weights.touch()
    config = yaml.safe_load(original_config.read_text(encoding="utf-8"))
    config["training"] = {"instruments": ["Vocals", "Instrumental"], "target_instrument": "Instrumental"}
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    with factory(MODEL_NAME, model_dir=tmp_path, device="cpu", **override_kwargs) as separator:
        assert separator.target_instrument_override == expected_override
        assert separator.config.training.target_instrument == expected_target
