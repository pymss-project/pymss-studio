from __future__ import annotations

from types import SimpleNamespace

from pymss.config import AttrDict
from pymss.separator import MSSeparator
from pymss.utils import _resolve_use_amp


class CaptureLogger:
    def __init__(self):
        self.debug_messages = []
        self.info_messages = []

    def debug(self, message):
        self.debug_messages.append(message)

    def info(self, message):
        self.info_messages.append(message)


def _config(*, training_use_amp=True, inference_use_amp=None, include_inference_use_amp=True):
    inference = {
        "batch_size": 1,
        "overlap_size": 0,
    }
    if include_inference_use_amp:
        inference["use_amp"] = inference_use_amp
    return AttrDict(
        {
            "training": {
                "instruments": ["vocals", "other"],
                "target_instrument": None,
                "use_amp": training_use_amp,
            },
            "audio": {"chunk_size": 1024},
            "inference": inference,
        }
    )


def test_resolve_use_amp_prefers_inference_override():
    assert _resolve_use_amp(_config(training_use_amp=True, inference_use_amp=False)) is False
    assert _resolve_use_amp(_config(training_use_amp=False, inference_use_amp=True)) is True


def test_resolve_use_amp_falls_back_to_training_config():
    assert _resolve_use_amp(_config(training_use_amp=False, include_inference_use_amp=False)) is False
    assert _resolve_use_amp(_config(training_use_amp=True, inference_use_amp=None)) is True


def test_update_inference_params_controls_effective_amp():
    config = _config(training_use_amp=True, inference_use_amp=True)
    separator = SimpleNamespace(logger=CaptureLogger())

    MSSeparator.update_inference_params(separator, config, {"use_amp": False})

    assert config.inference["use_amp"] is False
    assert config.training["use_amp"] is True
    assert _resolve_use_amp(config) is False


def test_mss_debug_log_prints_effective_amp():
    logger = CaptureLogger()
    separator = SimpleNamespace(
        logger=logger,
        model_path="model.ckpt",
        store_dirs={},
        save_as_folder=False,
        output_format="wav",
        audio_params={},
        output_normalize=False,
        use_tta=False,
    )

    MSSeparator._log_model_config(separator, "bs_roformer", _config(training_use_amp=True, inference_use_amp=False))

    assert any("MSS model params: use_amp: False" in message for message in logger.debug_messages)
