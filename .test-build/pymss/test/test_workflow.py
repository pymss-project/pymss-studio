from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from pymss.workflow import WorkflowError, WorkflowRunner, load_workflow_data, validate_workflow


class FakeSeparator:
    def __init__(self, model_name, calls, sample_rate=44100):
        self.model_name = model_name
        self.calls = calls
        self.closed = False
        self.model_type = "fake"
        self.device = "cpu"
        self.audio_params = {}
        self.config = SimpleNamespace(
            training=SimpleNamespace(instruments=["vocals", "other", "Dry"], target_instrument=None),
            audio={"sample_rate": sample_rate},
            inference={"batch_size": 1},
        )

    def separate(self, mix, pbar=False, stems=None):
        self.calls.append((self.model_name, np.asarray(mix).shape, stems))
        base = np.asarray(mix, dtype=np.float32)
        if base.ndim == 2 and base.shape[0] in (1, 2):
            save_major = base.T
        else:
            save_major = base
        requested = list(stems or self.config.training.instruments)
        return {stem: save_major + (index + 1) for index, stem in enumerate(requested)}

    def close(self):
        self.closed = True


def test_workflow_run_chains_step_outputs_and_saves_selected_stems(tmp_path):
    workflow = load_workflow_data(
        {
            "version": 1,
            "steps": [
                {
                    "id": "split",
                    "model": "split-model",
                    "input": "input",
                    "stems": ["vocals", "other"],
                    "save": {"vocals": "vocal", "other": "other"},
                },
                {
                    "id": "dereverb",
                    "model": "dereverb-model",
                    "input": "split.other",
                    "stems": ["Dry"],
                    "save": {"Dry": "dry"},
                },
            ],
        }
    )
    calls = []
    saved = []
    (tmp_path / "song.wav").write_bytes(b"fake")

    def separator_factory(model_name, **_kwargs):
        return FakeSeparator(model_name, calls)

    def audio_loader(path, sr=None, mono=False):
        assert path == str(tmp_path / "song.wav")
        assert sr is None
        return np.asarray([[1, 2, 3], [4, 5, 6]], dtype=np.float32), 44100

    def audio_saver(path, audio, sr, output_format, _audio_params):
        saved.append((Path(path).relative_to(tmp_path), np.asarray(audio).copy(), sr, output_format))

    runner = WorkflowRunner(
        workflow,
        separator_factory=separator_factory,
        audio_loader=audio_loader,
        audio_saver=audio_saver,
    )

    processed = runner.run(str(tmp_path / "song.wav"), tmp_path)

    assert processed == ["song.wav"]
    assert [item[0].as_posix() for item in saved] == [
        "song/vocal/song_vocals.wav",
        "song/other/song_other.wav",
        "song/dry/song_Dry.wav",
    ]
    assert calls == [
        ("split-model", (2, 3), ["vocals", "other"]),
        ("dereverb-model", (2, 3), ["Dry"]),
    ]


def test_workflow_batches_all_inputs_by_step(tmp_path):
    workflow = load_workflow_data(
        {
            "version": 1,
            "steps": [
                {
                    "id": "split",
                    "model": "split-model",
                    "input": "input",
                    "stems": ["vocals", "other"],
                    "save": {"other": "other"},
                },
                {
                    "id": "dereverb",
                    "model": "dereverb-model",
                    "input": "split.other",
                    "stems": ["Dry"],
                    "save": {"Dry": "dry"},
                },
            ],
        }
    )
    events = []
    (tmp_path / "a.wav").write_bytes(b"fake")
    (tmp_path / "b.wav").write_bytes(b"fake")

    class BatchSeparator(FakeSeparator):
        def separate(self, mix, pbar=False, stems=None):
            events.append(("separate", self.model_name, int(np.asarray(mix).flat[0])))
            return super().separate(mix, pbar=pbar, stems=stems)

        def close(self):
            events.append(("close", self.model_name))
            super().close()

    def separator_factory(model_name, **_kwargs):
        events.append(("open", model_name))
        return BatchSeparator(model_name, [])

    def audio_loader(path, sr=None, mono=False):
        value = 1 if Path(path).name == "a.wav" else 10
        return np.full((2, 4), value, dtype=np.float32), 44100

    runner = WorkflowRunner(
        workflow,
        separator_factory=separator_factory,
        audio_loader=audio_loader,
        audio_saver=lambda *_args, **_kwargs: None,
    )

    assert runner.run(tmp_path, tmp_path) == ["a.wav", "b.wav"]
    assert events == [
        ("open", "split-model"),
        ("separate", "split-model", 1),
        ("separate", "split-model", 10),
        ("close", "split-model"),
        ("open", "dereverb-model"),
        ("separate", "dereverb-model", 3),
        ("separate", "dereverb-model", 12),
        ("close", "dereverb-model"),
    ]


def test_workflow_run_can_flatten_outputs_to_task_folder(tmp_path):
    workflow = load_workflow_data(
        {
            "version": 1,
            "steps": [
                {
                    "id": "split",
                    "model": "split-model",
                    "input": "input",
                    "stems": ["vocals", "other"],
                    "save": {"vocals": "vocal", "other": "other"},
                },
            ],
        }
    )
    saved = []
    (tmp_path / "song.wav").write_bytes(b"fake")

    runner = WorkflowRunner(
        workflow,
        separator_factory=lambda model_name, **_kwargs: FakeSeparator(model_name, []),
        audio_loader=lambda *_args, **_kwargs: (np.zeros((2, 4), dtype=np.float32), 44100),
        audio_saver=lambda path, *_args: saved.append(Path(path).relative_to(tmp_path)),
        output_layout="flat",
    )

    assert runner.run(str(tmp_path / "song.wav"), tmp_path) == ["song.wav"]
    assert [path.as_posix() for path in saved] == [
        "vocal/song_vocals.wav",
        "other/song_other.wav",
    ]


def test_workflow_flat_output_disambiguates_duplicate_input_stems(tmp_path):
    workflow = load_workflow_data(
        {
            "version": 1,
            "steps": [
                {
                    "id": "split",
                    "model": "split-model",
                    "input": "input",
                    "stems": ["vocals"],
                    "save": {"vocals": "vocal"},
                },
            ],
        }
    )
    saved = []
    (tmp_path / "song.flac").write_bytes(b"fake")
    (tmp_path / "song.wav").write_bytes(b"fake")
    (tmp_path / "song_2.wav").write_bytes(b"fake")

    runner = WorkflowRunner(
        workflow,
        separator_factory=lambda model_name, **_kwargs: FakeSeparator(model_name, []),
        audio_loader=lambda *_args, **_kwargs: (np.zeros((2, 4), dtype=np.float32), 44100),
        audio_saver=lambda path, *_args: saved.append(Path(path).relative_to(tmp_path)),
        output_layout="flat",
    )

    assert runner.run(tmp_path, tmp_path) == ["song.flac", "song.wav", "song_2.wav"]
    assert [path.as_posix() for path in saved] == [
        "vocal/song_vocals.wav",
        "vocal/song_3_vocals.wav",
        "vocal/song_2_vocals.wav",
    ]


def test_validate_workflow_rejects_unknown_step_reference():
    workflow = load_workflow_data(
        {
            "version": 1,
            "steps": [
                {"id": "dereverb", "model": "model-b", "input": "split.other", "stems": ["Dry"]},
            ],
        }
    )

    with pytest.raises(WorkflowError, match="unknown step"):
        validate_workflow(workflow, model_resolver=lambda *_args, **_kwargs: None)


def test_validate_workflow_requires_downstream_stems_to_be_requested():
    workflow = load_workflow_data(
        {
            "version": 1,
            "steps": [
                {"id": "split", "model": "model-a", "input": "input", "stems": ["vocals"]},
                {"id": "dereverb", "model": "model-b", "input": "split.other", "stems": ["Dry"]},
            ],
        }
    )

    with pytest.raises(WorkflowError, match="split.other"):
        validate_workflow(workflow, model_resolver=lambda *_args, **_kwargs: None)


def test_cli_workflow_init_and_validate_template(tmp_path, capsys):
    from pymss.cli import main

    config_path = tmp_path / "workflow.yaml"

    assert main(["workflow", "init", "-o", str(config_path)]) == 0
    assert config_path.is_file()

    assert main(["workflow", "validate", "-c", str(config_path)]) == 0
    output = capsys.readouterr().out
    assert "Workflow is valid" in output


def test_workflow_run_accepts_explicit_model_files(tmp_path):
    (tmp_path / "song.wav").write_bytes(b"fake")
    workflow = load_workflow_data(
        {
            "version": 1,
            "steps": [
                {
                    "id": "restore",
                    "model_type": "mel_band_roformer",
                    "model_path": "/models/restore.ckpt",
                    "config_path": "/models/restore.yaml",
                    "input": "input",
                    "stems": ["restored"],
                    "save": {"restored": "sr"},
                },
            ],
        }
    )
    received = []

    def separator_factory(model_name, **kwargs):
        received.append((model_name, kwargs))
        return FakeSeparator(model_name, [])

    runner = WorkflowRunner(
        workflow,
        separator_factory=separator_factory,
        audio_loader=lambda *_args, **_kwargs: (np.zeros((2, 4), dtype=np.float32), 44100),
        audio_saver=lambda *_args, **_kwargs: None,
    )

    assert runner.run(str(tmp_path / "song.wav"), tmp_path) == ["song.wav"]
    assert received[0][0] == "restore"
    assert received[0][1]["model_type"] == "mel_band_roformer"
    assert received[0][1]["model_path"] == "/models/restore.ckpt"
    assert received[0][1]["config_path"] == "/models/restore.yaml"


def test_workflow_run_passes_step_specific_inference_params(tmp_path):
    (tmp_path / "song.wav").write_bytes(b"fake")
    workflow = load_workflow_data(
        {
            "version": 1,
            "defaults": {
                "inference_params": {
                    "batch_size": 1,
                    "overlap_size": 100,
                },
            },
            "steps": [
                {
                    "id": "split",
                    "model": "split-model",
                    "input": "input",
                    "stems": ["vocals"],
                    "inference_params": {"overlap_size": 200},
                },
                {
                    "id": "deverb",
                    "model": "deverb-model",
                    "input": "input",
                    "stems": ["Dry"],
                    "inference_params": {"overlap_size": 300},
                },
            ],
        }
    )
    received = []

    def separator_factory(model_name, **kwargs):
        received.append((model_name, kwargs["inference_params"]))
        return FakeSeparator(model_name, [])

    runner = WorkflowRunner(
        workflow,
        separator_factory=separator_factory,
        audio_loader=lambda *_args, **_kwargs: (np.zeros((2, 4), dtype=np.float32), 44100),
        audio_saver=lambda *_args, **_kwargs: None,
    )

    assert runner.run(str(tmp_path / "song.wav"), tmp_path) == ["song.wav"]
    assert received == [
        ("split-model", {"batch_size": 1, "overlap_size": 200}),
        ("deverb-model", {"batch_size": 1, "overlap_size": 300}),
    ]
