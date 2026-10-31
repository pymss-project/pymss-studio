"""pymss.plugins.kit: tool operations, structured progress and output publishing."""

from __future__ import annotations

import pytest

from pymss.operations import OperationContext, OperationError, Runtime, _RuntimePluginHandle
from pymss.plugins import kit


def test_emit_reports_structured_events_through_the_bound_context():
    calls = []
    context = OperationContext(progress=lambda *args: calls.append(args))
    with kit.operation_context(context):
        kit.emit("tool_progress", {"completed": 1, "total": 4, "current": "a.wav"})
        kit.emit("tool_progress", {"completed": 3, "total": 0})
    assert calls == [
        (0.25, "a.wav", {"type": "tool_progress", "payload": {"completed": 1, "total": 4, "current": "a.wav"}}),
        (0, "", {"type": "tool_progress", "payload": {"completed": 3, "total": 0}}),
    ]


def test_emit_requires_a_context_and_honours_cancellation():
    with pytest.raises(RuntimeError, match="Operation context"):
        kit.emit("tool_progress", {})
    context = OperationContext(progress=lambda *args: None, cancelled=lambda: True)
    with kit.operation_context(context), pytest.raises(OperationError, match="cancelled"):
        kit.emit("tool_progress", {})


def test_register_tool_runs_the_handler_with_its_request():
    runtime = Runtime()
    handle = _RuntimePluginHandle(runtime, source="plugin:author.tool")
    events = []

    def handler(request):
        kit.emit("tool_progress", {"completed": 1, "total": 1})
        return {"echo": request["value"]}

    kit.register_tool(handle, "author.tool.run", handler, title="Run", tool="run")
    assert handle.registered_operations == ["author.tool.run"]
    result = runtime.run(
        "author.tool.run", inputs={"request": {"operation": "run", "value": 7}}, params={},
        context=OperationContext(progress=lambda *args: events.append(args[2])),
    )
    assert result == {"report": {"echo": 7}}
    assert events == [{"type": "tool_progress", "payload": {"completed": 1, "total": 1}}]
    with pytest.raises(ValueError, match="does not match"):
        runtime.run("author.tool.run", inputs={"request": {"operation": "other"}}, params={})


def test_paths_are_validated_and_allocated_without_overwriting(tmp_path):
    existing = tmp_path / "a.wav"
    existing.write_bytes(b"1")
    assert kit.require_file(existing, "Input") == existing.resolve()
    with pytest.raises(ValueError, match="Input does not exist"):
        kit.require_file(tmp_path / "missing.wav", "Input")
    assert kit.require_directory(tmp_path / "new", "Output", create=True).is_dir()
    with pytest.raises(ValueError, match="Output is required"):
        kit.require_directory("", "Output")
    assert kit.available_path(existing) == tmp_path / "a_2.wav"
    assert kit.available_path(tmp_path / "b.wav") == tmp_path / "b.wav"


def test_render_output_publishes_atomically_and_keeps_earlier_files(tmp_path, monkeypatch):
    monkeypatch.delenv("PYMSS_STUDIO_AUDIO_TEMP_TOKEN", raising=False)
    monkeypatch.delenv("PYMSS_STUDIO_AUDIO_TEMP_STATE", raising=False)
    target = tmp_path / "mix.wav"
    target.write_bytes(b"earlier")
    published = kit.render_output(target, lambda partial: partial.write_bytes(b"new"))
    assert published == tmp_path / "mix_2.wav"
    assert target.read_bytes() == b"earlier"
    assert published.read_bytes() == b"new"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["mix.wav", "mix_2.wav"]
    with pytest.raises(RuntimeError, match="No audio data"):
        kit.render_output(tmp_path / "empty.wav", lambda partial: None)
    assert not (tmp_path / "empty.wav").exists()
    assert not list(tmp_path.glob(".pymss-audio-*"))
