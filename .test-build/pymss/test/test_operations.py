"""Round 2: Operation model + runtime (TDD).

These tests define the new Operation/OperationSpec/Runtime surface:
serializable specs, named params with defaults + validation, isolated
runtime registries, conflict errors, and plugin setup() rollback.
"""

from __future__ import annotations

import json

import pytest

from pymss.operations import (
    OperationError,
    OperationSpec,
    create_runtime,
)
from pymss.plugins import get_registry


@pytest.fixture(autouse=True)
def _clean_global_registry():
    """Keep the global legacy registry untouched by runtime tests."""
    yield


def _loudnorm_spec(**overrides) -> OperationSpec:
    """Minimal realistic spec shared by most tests."""
    params = {
        "type": "object",
        "properties": {
            "target_lufs": {
                "type": "number",
                "default": -14,
                "minimum": -70,
                "maximum": -5,
            }
        },
        "required": [],
        "additionalProperties": False,
    }
    kwargs = dict(
        id="pymss.loudnorm.normalize",
        schema_version=1,
        title="Loudness Normalize",
        inputs={"audio": {"type": "audio"}},
        params=params,
        outputs={"audio": {"type": "audio"}},
    )
    kwargs.update(overrides)
    return OperationSpec(**kwargs)


def _run_loudnorm(ctx, inputs, params):
    audio = inputs["audio"]
    # list input; scaling by a float factor would be a test bug
    data = [s * (1.0 if params["target_lufs"] else 0.0) for s in audio["data"]]
    return {"audio": {"data": data, "sr": audio["sr"]}}


# ---------------------------------------------------------------------------
# OperationSpec
# ---------------------------------------------------------------------------


def test_spec_is_json_serializable():
    spec = _loudnorm_spec()
    dumped = json.dumps(spec.to_dict())
    parsed = json.loads(dumped)
    assert parsed["id"] == "pymss.loudnorm.normalize"
    assert parsed["params"]["properties"]["target_lufs"]["default"] == -14
    assert parsed["inputs"]["audio"]["type"] == "audio"


def test_spec_rejects_bad_id():
    with pytest.raises(OperationError, match="id"):
        _loudnorm_spec(id="not namespaced")


def test_spec_requires_handler_at_register_time_not_build_time():
    spec = _loudnorm_spec()
    assert spec.run is None  # handler attached at runtime.register, not spec build


# ---------------------------------------------------------------------------
# Runtime: register / describe / list
# ---------------------------------------------------------------------------


def test_runtime_register_and_describe():
    rt = create_runtime()
    rt.register(spec=_loudnorm_spec(), run=_run_loudnorm, source="test")
    ops = rt.list_operations()
    assert [o["id"] for o in ops] == ["pymss.loudnorm.normalize"]
    described = rt.describe_operation("pymss.loudnorm.normalize")
    assert described["params"]["properties"]["target_lufs"]["minimum"] == -70


def test_runtime_is_isolated_from_other_instances():
    rt1 = create_runtime()
    rt2 = create_runtime()
    rt1.register(spec=_loudnorm_spec(), run=_run_loudnorm, source="test")
    assert rt2.list_operations() == []
    # And isolated from the legacy global registry
    assert "pymss.loudnorm.normalize" not in [
        c for c in get_registry().capabilities
    ]


def test_duplicate_operation_id_raises():
    rt = create_runtime()
    rt.register(spec=_loudnorm_spec(), run=_run_loudnorm, source="a")
    with pytest.raises(OperationError, match="already registered"):
        rt.register(spec=_loudnorm_spec(), run=_run_loudnorm, source="b")


def test_describe_unknown_operation_raises():
    rt = create_runtime()
    with pytest.raises(OperationError, match="unknown operation"):
        rt.describe_operation("nope.op")


# ---------------------------------------------------------------------------
# Runtime: run with param validation
# ---------------------------------------------------------------------------


def test_run_fills_param_defaults_and_executes():
    rt = create_runtime()
    rt.register(spec=_loudnorm_spec(), run=_run_loudnorm, source="test")
    result = rt.run(
        "pymss.loudnorm.normalize",
        inputs={"audio": {"data": [1.0, 2.0], "sr": 44100}},
        params={},  # target_lufs omitted -> default -14
    )
    assert result["audio"]["sr"] == 44100


def test_run_rejects_out_of_range_param():
    rt = create_runtime()
    rt.register(spec=_loudnorm_spec(), run=_run_loudnorm, source="test")
    with pytest.raises(OperationError, match="target_lufs"):
        rt.run(
            "pymss.loudnorm.normalize",
            inputs={"audio": {"data": [1.0], "sr": 44100}},
            params={"target_lufs": 100},  # > maximum -5
        )


def test_run_rejects_unknown_param():
    rt = create_runtime()
    rt.register(spec=_loudnorm_spec(), run=_run_loudnorm, source="test")
    with pytest.raises(OperationError, match="unknown|additional"):
        rt.run(
            "pymss.loudnorm.normalize",
            inputs={"audio": {"data": [1.0], "sr": 44100}},
            params={"nonsense": True},
        )


def test_run_rejects_missing_required_input():
    rt = create_runtime()
    rt.register(spec=_loudnorm_spec(), run=_run_loudnorm, source="test")
    with pytest.raises(OperationError, match="audio"):
        rt.run("pymss.loudnorm.normalize", inputs={}, params={})


# ---------------------------------------------------------------------------
# Plugin setup() integration + rollback
# ---------------------------------------------------------------------------


def test_runtime_loads_plugin_setup_operations(tmp_path, monkeypatch):
    monkeypatch.setenv("PYMSS_PLUGINS_DIR", str(tmp_path))
    plug = tmp_path / "opplug"
    plug.mkdir()
    (plug / "pyproject.toml").write_text(
        '[tool.pymss.plugin]\nid = "x.op"\napi_version = 2\n\n'
        '[[tool.pymss.plugin.operations]]\nid = "x.op.echo"\n'
    )
    (plug / "__init__.py").write_text(
        "def setup(plugin):\n"
        "    plugin.register_operation(\n"
        "        id='x.op.echo',\n"
        "        params={'type': 'object', 'properties': {}, 'additionalProperties': False},\n"
        "        inputs={}, outputs={'value': {'type': 'string'}},\n"
        "        run=lambda ctx, inputs, params: {'value': 'hi'},\n"
        "    )\n"
    )
    rt = create_runtime(load_plugins=True)
    ops = rt.list_operations()
    assert "x.op.echo" in [o["id"] for o in ops]
    result = rt.run("x.op.echo", inputs={}, params={})
    assert result["value"] == "hi"


def test_failing_plugin_setup_rolls_back_its_registrations(tmp_path, monkeypatch):
    monkeypatch.setenv("PYMSS_PLUGINS_DIR", str(tmp_path))
    plug = tmp_path / "halfplug"
    plug.mkdir()
    (plug / "pyproject.toml").write_text(
        '[tool.pymss.plugin]\nid = "x.op"\napi_version = 2\n\n'
        '[[tool.pymss.plugin.operations]]\nid = "x.op.first"\n'
    )
    (plug / "__init__.py").write_text(
        "def setup(plugin):\n"
        "    plugin.register_operation(\n"
        "        id='x.op.first',\n"
        "        params={'type': 'object', 'properties': {}, 'additionalProperties': False},\n"
        "        inputs={}, outputs={},\n"
        "        run=lambda ctx, inputs, params: {},\n"
        "    )\n"
        "    raise RuntimeError('boom after first registration')\n"
    )
    rt = create_runtime(load_plugins=True)
    ids = [o["id"] for o in rt.list_operations()]
    assert "x.op.first" not in ids
    # The failure is surfaced, not swallowed
    report = rt.plugin_report()
    row = next(r for r in report if r["name"] == "halfplug")
    assert not row["loaded"]
    assert "boom after first registration" in row["error"]
