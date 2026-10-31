from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pymss.graph import LegacyWorkflowRunner
from pymss.workflow import load_workflow_data


@pytest.mark.parametrize("fail_first", [False, True])
def test_cli_runner_default_cache_releases_models_across_tracks(tmp_path, monkeypatch, fail_first):
    import pymss.graph.runner as module
    first, second = tmp_path / "a.wav", tmp_path / "b.wav"
    first.touch()
    second.touch()
    loaded = []

    def factory(name, **kwargs):
        assert not any(not item.closed for item in loaded)
        item = SimpleNamespace(name=name, closed=False)
        item.close = Mock(side_effect=lambda: setattr(item, "closed", True))
        loaded.append(item)
        return item

    def run(dag, **kwargs):
        cache = kwargs["separator_cache"]
        cache.get(model_name="a")
        cache.get(model_name="b")
        if fail_first and kwargs["input_path"] == str(first):
            raise RuntimeError("Separation failed")
        return []

    monkeypatch.setattr(module, "run_dag", run)
    workflow = load_workflow_data({"version": 1, "steps": [{"id": "a", "model": "a", "stems": ["Vocals"]}]})
    runner = LegacyWorkflowRunner(workflow, separator_factory=factory, continue_on_error=True)
    assert runner.run(tmp_path, tmp_path / "outputs") == (["b.wav"] if fail_first else ["a.wav", "b.wav"])
    assert len(loaded) == 4 and all(item.closed for item in loaded)
    assert all(item.close.call_count == 1 for item in loaded)
