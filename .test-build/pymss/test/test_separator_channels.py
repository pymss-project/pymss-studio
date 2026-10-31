import logging

import numpy as np
import pytest
import torch
import yaml

from pymss.config import AttrDict
from pymss.separator import OUTPUT_NORMALIZE_PEAK, MSSeparator, _model_input_channels


class ChannelModel(torch.nn.Module):
    def __init__(self, channels):
        super().__init__()
        self.audio_channels = channels
        self.calls = []

    def forward(self, audio):
        assert audio.shape[1] == self.audio_channels
        self.calls.append(audio.detach().cpu().numpy().copy())
        return torch.stack((audio * 0.25, audio * 0.75), dim=1)


def make_separator(channels, *, tta=False, standardize=False, normalize=False, callback=None):
    separator = MSSeparator.__new__(MSSeparator)
    separator.model_type = "bs_roformer"
    separator.model = ChannelModel(channels)
    separator.config = AttrDict({
        "model": {"stereo": channels == 2},
        "audio": {"chunk_size": 64, "sample_rate": 8000},
        "inference": {"batch_size": 2, "overlap_size": 8, "normalize": standardize},
        "training": {"instruments": ["vocals", "other"], "target_instrument": None, "use_amp": False},
    })
    separator.device = "cpu"
    separator.use_tta = tta
    separator.output_normalize = normalize
    separator.progress_callback = callback
    separator.logger = logging.getLogger("pymss.channels")
    return separator


def channel_audio(channels):
    wave = np.sin(np.linspace(0, 4 * np.pi, 192, endpoint=False)).astype(np.float32)
    return np.stack([wave * (0.2 * (index + 1)) for index in range(channels)])


@pytest.mark.parametrize("model_channels", [1, 2])
@pytest.mark.parametrize("input_channels", [1, 2, 6])
def test_channel_matrix_through_real_demix(input_channels, model_channels):
    separator = make_separator(model_channels)
    audio = channel_audio(input_channels)
    original = audio.copy()
    results = separator.separate(audio, pbar=False)
    expected_channels = min(input_channels, 2) if model_channels == 2 else (2 if input_channels == 2 else 1)

    for stem, gain in (("vocals", 0.25), ("other", 0.75)):
        assert results[stem].shape == (audio.shape[1], expected_channels)
        assert np.isfinite(results[stem]).all()
        if input_channels <= 2:
            np.testing.assert_allclose(results[stem], audio.T * gain, atol=1e-6)
        elif model_channels == 1:
            np.testing.assert_allclose(results[stem], audio.mean(axis=0)[:, None] * gain, atol=1e-6)
        else:
            assert not np.allclose(results[stem][:, 0], results[stem][:, 1])
    np.testing.assert_array_equal(audio, original)


@pytest.mark.parametrize("model_channels", [1, 2])
def test_one_dimensional_mono_stays_mono(model_channels):
    audio = channel_audio(1)[0]
    result = make_separator(model_channels).separate(audio, pbar=False)["vocals"]
    assert result.shape == (len(audio), 1)
    np.testing.assert_allclose(result[:, 0], audio * 0.25, atol=1e-6)


def test_stereo_mono_model_preserves_phase_and_progress_with_tta():
    events = []
    separator = make_separator(1, tta=True, standardize=True, callback=lambda *args: events.append(args))
    left = channel_audio(1)[0]
    audio = np.stack((left, -left * 0.3))
    result = separator.separate(audio, pbar=False, stems="vocals")["vocals"]
    np.testing.assert_allclose(result, audio.T * 0.25, atol=1e-6)
    fractions = [done / total for done, total, _ in events]
    assert fractions == sorted(fractions)
    assert fractions[0] == 0 and fractions[-1] == 1
    assert any(0 < value < 1 for value in fractions)


def test_normalize_after_recombining_left_and_right():
    separator = make_separator(1, normalize=True)
    audio = channel_audio(2)
    result = separator.separate(audio, pbar=False, stems="vocals")["vocals"]
    np.testing.assert_allclose(result[:, 1], result[:, 0] * 2, atol=1e-6)


def test_normalize_uses_one_gain_across_returned_stems_and_channels():
    separator = make_separator(1, normalize=True)
    audio = channel_audio(2)
    results = separator.separate(audio, pbar=False)
    np.testing.assert_allclose(results["other"], results["vocals"] * 3, atol=1e-6)
    np.testing.assert_allclose(results["other"][:, 1], results["other"][:, 0] * 2, atol=1e-6)
    assert float(np.abs(results["other"]).max()) == pytest.approx(OUTPUT_NORMALIZE_PEAK)
    selected = separator.separate(audio, pbar=False, stems="vocals")
    assert float(np.abs(selected["vocals"]).max()) == pytest.approx(OUTPUT_NORMALIZE_PEAK)


def test_target_complement_uses_each_original_channel():
    separator = make_separator(1)
    separator.config.training.target_instrument = "vocals"
    audio = channel_audio(2)
    results = separator.separate(audio, pbar=False)
    np.testing.assert_allclose(results["vocals"] + results["other"], audio.T, atol=1e-6)


@pytest.mark.parametrize("shape", [(0,), (0, 20), (2, 0), (2, 3, 20)])
def test_invalid_audio_rejected_before_model(shape):
    separator = make_separator(1)
    with pytest.raises(ValueError, match="audio|Audio|waveform|samples|channels"):
        separator.separate(np.empty(shape, dtype=np.float32), pbar=False)
    assert not separator.model.calls


@pytest.mark.parametrize("model_type,config", [
    ("bs_roformer", {"model": {"stereo": False}}),
    ("bs_roformer", {"model": {}}),
    ("mel_band_roformer", {"model": {"stereo": False}}),
    ("bs_conformer", {"model": {"stereo": False}}),
    ("demucs", {"model": {"stereo": False}}),
    ("mdx23c", {"audio": {"num_channels": 1}}),
    ("htdemucs", {"training": {"channels": 1}}),
    ("bandit", {"model": {"in_channel": 1}}),
    ("bandit_v2", {"kwargs": {"in_channels": 1}}),
    ("scnet", {"model": {"audio_channels": 1}}),
])
def test_mono_model_configuration_fields(model_type, config):
    assert _model_input_channels(model_type, AttrDict(config)) == 1


def test_loaded_model_channels_override_configuration():
    model = torch.nn.DataParallel(ChannelModel(1))
    assert _model_input_channels("bs_roformer", AttrDict({"model": {"stereo": True}}), model) == 1


def test_apollo_allows_native_mono_input():
    separator = make_separator(1)
    separator.model_type = "apollo"
    audio = channel_audio(1)
    result = separator.separate(audio, pbar=False)["vocals"]
    np.testing.assert_allclose(result, audio.T * 0.25, atol=1e-6)


def test_vr_mono_output_averages_model_channels():
    separator = make_separator(2)
    separator.model_type = "vr"

    class VRModel:
        def separate_array(self, mix, sample_rate):
            assert mix.shape[0] == 2
            return {"vocals": mix.T * np.array([0.25, 0.75], dtype=np.float32)}

    separator.model = VRModel()
    audio = channel_audio(1)
    result = separator.separate(audio, pbar=False)["vocals"]
    assert result.shape == (audio.shape[1], 1)
    np.testing.assert_allclose(result, audio.T * 0.5, atol=1e-6)


def test_vr_callback_assigned_after_loading_is_forwarded_to_the_child():
    separator = make_separator(2)
    separator.model_type = "vr"
    events = []

    class VRModel:
        progress_callback = None
        def separate_array(self, mix, sample_rate):
            self.progress_callback(1, 2, "Processing VR batches")
            return {"vocals": mix.T}

    separator.model = VRModel()
    separator.progress_callback = lambda *args: events.append(args)
    separator.separate(channel_audio(2))
    assert events == [(1, 2, "Processing VR batches")]
    assert separator.model.progress_callback is separator.progress_callback


@pytest.mark.parametrize("vr", [False, True])
def test_close_moves_model_to_cpu_even_if_runtime_cache_cleanup_fails(monkeypatch, vr):
    from types import SimpleNamespace
    from unittest.mock import Mock
    separator = make_separator(2)
    model = separator.model
    model.to = Mock(return_value=model)
    if vr:
        separator.model_type = "vr"
        separator.model = SimpleNamespace(model_run=model)
    separator.store_dirs = {"vocals": "outputs"}
    separator.del_cache = Mock()
    failure = RuntimeError("Runtime cache cleanup failed")
    monkeypatch.setattr("pymss.separator.clear_model_runtime_caches", Mock(side_effect=failure))
    with pytest.raises(RuntimeError) as caught:
        separator.close()
    assert caught.value is failure
    model.to.assert_called_once_with("cpu")
    assert separator.model is None and separator.config is None and separator.store_dirs == {}
    separator.del_cache.assert_called_once()


@pytest.mark.parametrize("model_channels", [1, 2])
def test_real_roformer_auto_loading_preserves_channel_contract(tmp_path, model_channels):
    from pymss_core import get_model_from_config

    config = {
        "audio": {"chunk_size": 128, "sample_rate": 8000, "num_channels": model_channels},
        "model": {
            "dim": 8, "depth": 1, "heads": 2, "dim_head": 4,
            "stereo": model_channels == 2, "num_stems": 1,
            "time_transformer_depth": 1, "freq_transformer_depth": 1,
            "freqs_per_bands": [4, 5], "stft_n_fft": 16,
            "stft_hop_length": 4, "stft_win_length": 16, "mask_estimator_depth": 1,
        },
        "training": {"instruments": ["vocals", "other"], "target_instrument": "vocals", "use_amp": False},
        "inference": {"batch_size": 1, "overlap_size": 16},
    }
    config_path = tmp_path / "model.yaml"
    model_path = tmp_path / "model.ckpt"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    model, _ = get_model_from_config("bs_roformer", config_path)
    torch.save(model.state_dict(), model_path)

    with MSSeparator("auto", model_path, config_path, device="cpu", store_dirs={}) as separator:
        for input_channels in (1, 2, 6):
            audio = channel_audio(input_channels)
            results = separator.separate(audio, pbar=False)
            output_channels = min(input_channels, 2) if model_channels == 2 else (2 if input_channels == 2 else 1)
            assert all(result.shape == (audio.shape[1], output_channels) for result in results.values())
            assert all(np.isfinite(result).all() for result in results.values())
            if input_channels <= 2:
                np.testing.assert_allclose(results["vocals"] + results["other"], audio.T, atol=1e-6)
