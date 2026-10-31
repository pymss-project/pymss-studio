import numpy as np
import pytest
import torch
import yaml
from pymss_core import get_model_from_config

from pymss import ModelTypeDetectionError, load_audio
from pymss.graph import run_dag
from pymss.graph.yaml_compiler import compile_workflow_to_dag
from pymss.workflow import load_workflow_data
from test.test_audio_downmix import write_layout_wav


@pytest.fixture
def custom_model(tmp_path):
    config = {
        "audio": {"chunk_size": 128, "sample_rate": 44100, "num_channels": 2},
        "model": {
            "dim": 8, "depth": 1, "heads": 2, "dim_head": 4,
            "stereo": True, "num_stems": 1,
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
    audio = np.sin(np.arange(256, dtype=np.float32) * 0.1)[None, :] * 0.1
    input_path = tmp_path / "input.wav"
    write_layout_wav(input_path, audio, "mono", sample_rate=44100)
    return model_path, config_path, config, input_path, audio


def custom_dag(model_path, config_path, model_type):
    workflow = load_workflow_data({"version": 1, "steps": [{
        "id": "split", "model_path": str(model_path), "config_path": str(config_path),
        "model_type": model_type, "device": "cpu", "stems": ["vocals", "other"],
        "save": {"vocals": "vocals", "other": "other"},
    }]})
    return compile_workflow_to_dag(workflow)


def assert_saved_stems(paths, expected):
    assert len(paths) == 2
    stems = []
    for path in paths:
        audio, rate = load_audio(path)
        assert rate == 44100
        assert audio.shape == expected.shape
        assert np.isfinite(audio).all()
        stems.append(audio)
    np.testing.assert_allclose(sum(stems), expected, atol=1e-6)


@pytest.mark.parametrize("model_type", ["auto", "bs_roformer"])
def test_compiled_workflow_loads_explicit_paths_without_registration(tmp_path, monkeypatch, custom_model, model_type):
    from pymss.graph import nodes

    model_path, config_path, config, input_path, audio = custom_model
    if model_type != "auto":
        config["model_type"] = "unknown"
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    def reject_registry_lookup(name):
        pytest.fail(f"An explicit model path must not require registry lookup: {name}")

    monkeypatch.setattr(nodes, "_resolve_user_model", reject_registry_lookup)
    dag = custom_dag(model_path, config_path, model_type)
    paths = run_dag(dag, input_path=str(input_path), output_dir=tmp_path / "out", device="cpu")
    assert_saved_stems(paths, audio[0])


def test_compiled_auto_keeps_detection_error(tmp_path, custom_model):
    model_path, config_path, config, input_path, _ = custom_model
    config["model_type"] = "unknown"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    dag = custom_dag(model_path, config_path, "auto")
    with pytest.raises(ModelTypeDetectionError, match="Set model_type explicitly"):
        run_dag(dag, input_path=str(input_path), output_dir=tmp_path / "out", device="cpu")
    assert not list((tmp_path / "out").rglob("*.wav"))


def test_registered_custom_graph_still_resolves_model_name(tmp_path, monkeypatch, custom_model):
    from pymss.graph import nodes

    model_path, config_path, _, input_path, audio = custom_model
    dag = custom_dag(model_path, config_path, "auto")
    node = next(node for node in dag.nodes if node.id == "step:split")
    data = nodes.node_data(node)
    data.pop("model_path", None)
    data.pop("config_path", None)
    data["widgets_values"][0] = "registered-model"
    names = []

    def resolve(name):
        names.append(name)
        return {"model_path": str(model_path), "config_path": str(config_path)}

    monkeypatch.setattr(nodes, "_resolve_user_model", resolve)
    paths = run_dag(dag, input_path=str(input_path), output_dir=tmp_path / "out", device="cpu")
    assert names == ["registered-model"]
    assert_saved_stems(paths, audio[0])
