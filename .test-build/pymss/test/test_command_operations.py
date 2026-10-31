"""Declarative command validation, v2 registration and file publication."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from pymss.operations import OperationContext, OperationError, Runtime
from pymss.operations.command import current_target, read_commands
from pymss.operations.files import FileArtifact
from pymss.plugins import get_registry, kit


@pytest.fixture
def package(tmp_path):
    root = tmp_path / "plugins/author.convert"
    root.mkdir(parents=True)
    (root / "__init__.py").write_text(
        'from pathlib import Path\nfrom pymss.plugins.kit import register_commands\n'
        'def setup(plugin): register_commands(plugin,Path(__file__).parent)\n', encoding="utf-8")
    executable = "bin/native.exe" if os.name == "nt" else "bin/native"
    program = root / executable
    program.parent.mkdir()
    program.write_bytes(b"native command placeholder")
    program.chmod(0o755)
    (root / "pyproject.toml").write_text(f'''
[project]
name = "author-convert"
version = "1.0.0"
[tool.pymss.plugin]
id = "author.convert"
api_version = 2
[[tool.pymss.plugin.operations]]
id = "author.convert.run"
[[tool.pymss.plugin.contributions.nodes]]
type = "author_convert"
inputs = [{{name="source",type="STRING"}},{{name="mode",type="STRING"}}]
outputs = [{{name="document",type="STRING"}}]
[tool.pymss.commands]
format_version = 1
[[tool.pymss.commands.operations]]
id = "author.convert.run"
title = "Convert"
node_type = "author_convert"
arguments = ["--source",{{input="source"}},"--target",{{output="document"}},"--mode",{{param="mode"}}]
[tool.pymss.commands.operations.targets]
"{current_target()}" = "{executable}"
[tool.pymss.commands.operations.inputs.source]
type = "file"
extensions = ["txt"]
[tool.pymss.commands.operations.params]
type = "object"
additionalProperties = false
[tool.pymss.commands.operations.params.properties.mode]
type = "string"
enum = ["lf","crlf"]
default = "lf"
[tool.pymss.commands.operations.outputs.document]
type = "file"
filename = "converted.txt"
media_type = "text/plain"
''', encoding="utf-8")
    return root


@pytest.fixture
def command(package):
    return read_commands(package)[0]


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "含空格 ' $(echo no).txt"
    path.write_bytes(b"text\r\nnot audio\xff")
    return path


@pytest.fixture
def converter(monkeypatch):
    calls = []

    def run(argv, **options):
        calls.append((argv, options))
        Path(argv[4]).write_bytes(Path(argv[2]).read_bytes().replace(b"\r\n", b"\n"))

    monkeypatch.setattr("pymss.operations.command.run_command_process", run)
    return calls


def test_native_commands_register_through_v2_and_the_graph_uses_the_same_contract(package, source, tmp_path, converter):
    # Import the public graph entry before plugin loading, as an actual host does.
    from pymss.graph.core import DAGNode, StringArtifact
    from types import SimpleNamespace

    runtime = Runtime()
    report = runtime.load_plugins([package.parent])
    try:
        assert report[0]["status"] == "activated", report
        description = runtime.describe_operation("author.convert.run")
        assert description["inputs"]["source"]["type"] == "file"
        json.dumps(description, allow_nan=False)
        result = runtime.run("author.convert.run", inputs={"source": {"kind": "file", "path": str(source)}},
                             params={}, context=OperationContext(workdir=str(tmp_path / "out")))
        assert isinstance(result["document"], FileArtifact)
        assert Path(result["document"]).read_bytes() == b"text\nnot audio\xff"
        node = DAGNode("convert", "author_convert", data={"params": {"mode": "crlf"}})
        context = SimpleNamespace(output_dir=tmp_path / "graph", logger=None, current_node_id="convert", nodes_by_id={"convert": node}, report_progress=lambda *args: None)
        registration = get_registry().nodes["author_convert"]
        graph = registration.func(context, {"source": StringArtifact(str(source))})
        assert isinstance(graph.outputs[0], FileArtifact)
        assert converter[-1][0][-1] == "crlf"
        node.data["params"]["mode"] = "invalid"
        with pytest.raises(OperationError, match="mode"):
            registration.func(context, {"source": StringArtifact(str(source)), "mode": "invalid"})
    finally:
        get_registry().nodes.pop("author_convert", None)


def test_public_file_outputs_are_json_describable_and_keep_prior_exports(command, source, tmp_path, converter):
    directory = tmp_path / "out"
    directory.mkdir()
    (directory / "converted.txt").write_bytes(b"user export")
    first = command.run(OperationContext(workdir=str(directory)), {"source": source}, {"mode": "lf"})
    second = command.run(OperationContext(workdir=str(directory)), {"source": first["document"]}, {"mode": "lf"})
    assert (directory / "converted.txt").read_bytes() == b"user export"
    assert Path(first["document"]).name == "converted_2.txt"
    assert Path(second["document"]).name == "converted_3.txt"
    assert first["document"].to_dict()["kind"] == "file"
    assert first["document"].media_type == "text/plain"
    assert first["document"].value == first["document"].path


def test_empty_generic_files_are_valid_outputs(command, source, tmp_path, monkeypatch):
    monkeypatch.setattr("pymss.operations.command.run_command_process", lambda argv, **_: Path(argv[4]).write_bytes(b""))
    result = command.run(OperationContext(workdir=str(tmp_path / "out")), {"source": source}, {"mode": "lf"})
    assert Path(result["document"]).stat().st_size == 0


@pytest.mark.parametrize("kind", ["missing", "directory", "oversized", "hardlink"])
def test_invalid_outputs_do_not_publish(command, source, tmp_path, monkeypatch, kind):
    command = replace(command, max_output_bytes=1)

    def write(argv, **_):
        output = Path(argv[4])
        if kind == "directory":
            output.mkdir()
        elif kind == "oversized":
            output.write_bytes(b"xx")
        elif kind == "hardlink":
            os.link(source, output)

    monkeypatch.setattr("pymss.operations.command.run_command_process", write)
    directory = tmp_path / "out"
    with pytest.raises(OperationError):
        command.run(OperationContext(workdir=str(directory)), {"source": source}, {"mode": "lf"})
    assert not list(directory.iterdir())


def test_failed_process_never_publishes_its_partial_file(command, source, tmp_path, monkeypatch):
    def fail(argv, **_):
        Path(argv[4]).write_bytes(b"partial")
        raise OperationError("Native program failed")

    monkeypatch.setattr("pymss.operations.command.run_command_process", fail)
    directory = tmp_path / "out"
    with pytest.raises(OperationError, match="Native program failed"):
        command.run(OperationContext(workdir=str(directory)), {"source": source}, {"mode": "lf"})
    assert not list(directory.iterdir())


def test_cancellation_before_publication_discards_the_complete_candidate(command, source, tmp_path, converter):
    cancelled = False

    def progress(_fraction, _message, event):
        nonlocal cancelled
        cancelled = event["payload"]["phase"] == "publishing"

    directory = tmp_path / "out"
    with pytest.raises(OperationError, match="cancelled"):
        command.run(OperationContext(workdir=str(directory), cancelled=lambda: cancelled, progress=progress),
                    {"source": source}, {"mode": "lf"})
    assert not list(directory.iterdir())


def test_missing_targets_extensions_and_output_directory_fail_without_execution(command, source, tmp_path, converter):
    with pytest.raises(OperationError, match="working directory"):
        command.run(OperationContext(), {"source": source}, {"mode": "lf"})
    with pytest.raises(OperationError, match="platform"):
        replace(command, targets={}).run(OperationContext(), {"source": source}, {"mode": "lf"})
    bad = tmp_path / "input.exe"
    bad.write_bytes(b"x")
    with pytest.raises(OperationError, match="extension"):
        command.run(OperationContext(workdir=str(tmp_path)), {"source": bad}, {"mode": "lf"})
    assert not converter


@pytest.mark.parametrize("old,new,error", [
    ("format_version = 1", "format_version = 2", "format"),
    ('{input="source"}', '{input="unknown"}', "undeclared"),
    ('filename = "converted.txt"', 'filename = "../converted.txt"', "filename"),
    ('filename = "converted.txt"', 'filename = "NUL.txt"', "filename"),
    ('{param="mode"}', '{param="mode",input="source"}', "exactly one"),
    ('arguments = [', 'timeout = nan\narguments = [', "timeout"),
    ('type = "file"\nextensions', 'type = "audio"\nextensions', "file ports"),
    ('{name="mode",type="STRING"}', '{name="mode",type="AUDIO"}', "node ports"),
])
def test_invalid_declarations_fail_during_registration(package, old, new, error):
    manifest = package / "pyproject.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    with pytest.raises(OperationError, match=error):
        read_commands(package)


def test_malformed_command_rolls_back_operations_and_nodes(package):
    manifest = package / "pyproject.toml"
    manifest.write_text(manifest.read_text().replace('{input="source"}', '{input="bad"}'))
    runtime = Runtime()
    report = runtime.load_plugins([package.parent])
    assert report[0]["status"] == "failed"
    assert runtime.list_operations() == []
    assert "author_convert" not in get_registry().nodes


def test_an_unbuilt_source_package_is_rejected_before_activation(package):
    for binary in (package / "bin").iterdir():
        binary.unlink()
    runtime = Runtime()
    report = runtime.load_plugins([package.parent])
    assert report[0]["status"] == "failed"
    assert "not installed" in report[0]["error"]
    assert runtime.list_operations() == []


def test_process_declarations_cannot_register_under_a_foreign_plugin_identity(package):
    from pymss.operations import _RuntimePluginHandle

    runtime = Runtime()
    plugin = _RuntimePluginHandle(runtime, "plugin:another.author")
    with pytest.raises(OperationError, match="registering plugin"):
        kit.register_commands(plugin, package)
    assert runtime.list_operations() == []


def test_executable_paths_cannot_escape_the_package(command, source, tmp_path):
    command = replace(command, targets={current_target(): "../outside.exe"})
    with pytest.raises(OperationError, match="inside"):
        command.run(OperationContext(workdir=str(tmp_path)), {"source": source}, {"mode": "lf"})


def test_string_values_cannot_inject_native_command_options(command, source, tmp_path, converter):
    with pytest.raises(OperationError, match="command options"):
        command.run(OperationContext(workdir=str(tmp_path)), {"source": source}, {"mode": "--output=elsewhere"})
    assert not converter


def test_relative_or_aliased_scratch_paths_are_resolved_before_launch(command, source, tmp_path, converter, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYMSS_SCRATCH_DIR", "scratch")
    result = command.run(OperationContext(workdir=str(tmp_path / "out")), {"source": source}, {"mode": "lf"})
    assert Path(result["document"]).is_file()
    assert converter[0][1]["cwd"].is_absolute()


def test_generic_output_helper_keeps_its_audio_default_and_accepts_explicit_empty_files(tmp_path):
    with pytest.raises(RuntimeError, match="No audio data"):
        kit.render_output(tmp_path / "audio.wav", lambda partial: partial.write_bytes(b""))
    path = kit.render_output(tmp_path / "empty.txt", lambda partial: partial.write_bytes(b""), allow_empty=True)
    assert path.is_file() and path.stat().st_size == 0
