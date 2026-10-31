"""Isolated Python processes keep conflicting wheel dependencies out of the host."""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import zipfile
import uuid
from pathlib import Path

import pytest

from pymss.operations import OperationContext, OperationError
from pymss.operations.command import read_commands
from pymss.operations.python_profiles import prepare_python_environment, remove_python_environment


def make_package(root: Path, version="1.0.0") -> Path:
    root.mkdir(parents=True)
    (root / "__init__.py").write_text(
        "from pathlib import Path\nfrom pymss.plugins.kit import register_commands\n"
        "def setup(plugin): register_commands(plugin, Path(__file__).parent)\n", encoding="utf-8")
    (root / "main.py").write_text('''import importlib.util, json, sys
from pathlib import Path
import pymss_isolation_probe
Path(sys.argv[2]).write_text(json.dumps({
    "version": pymss_isolation_probe.VERSION,
    "prefix": sys.prefix,
    "hostVisible": importlib.util.find_spec("pymss") is not None,
    "input": Path(sys.argv[1]).read_text(encoding="utf-8"),
}), encoding="utf-8")
''', encoding="utf-8")
    wheels = root / "wheels"
    wheels.mkdir()
    wheel = wheels / f"pymss_isolation_probe-{version}-py3-none-any.whl"
    info = f"pymss_isolation_probe-{version}.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("pymss_isolation_probe/__init__.py", f"VERSION = {version!r}\n")
        archive.writestr(info + "/METADATA", f"Metadata-Version: 2.1\nName: pymss-isolation-probe\nVersion: {version}\n")
        archive.writestr(info + "/WHEEL", "Wheel-Version: 1.0\nGenerator: pymss\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(info + "/RECORD", "")
    (root / "python-lock.json").write_text(json.dumps({"formatVersion": 1, "requiresPython": ">=3.11",
        "targets": {"any": [{"path": wheel.relative_to(root).as_posix(), "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}]}}), encoding="utf-8")
    (root / "pyproject.toml").write_text('''[project]
name = "isolated-python-tool"
version = "1.0.0"
[tool.pymss.plugin]
id = "author.isolated"
api_version = 2
[[tool.pymss.plugin.operations]]
id = "author.isolated.run"
[tool.pymss.python]
format_version = 1
lock = "python-lock.json"
[tool.pymss.commands]
format_version = 1
[[tool.pymss.commands.operations]]
id = "author.isolated.run"
python = "main.py"
arguments = [{input="source"}, {output="document"}]
[tool.pymss.commands.operations.inputs.source]
type = "file"
[tool.pymss.commands.operations.outputs.document]
type = "file"
filename = "result.json"
media_type = "application/json"
''', encoding="utf-8")
    return root


def run(root, tmp_path):
    source = tmp_path / "中文 input.txt"
    source.write_text("text", encoding="utf-8")
    op = read_commands(root)[0]
    result = op.run(OperationContext(workdir=str(tmp_path / "results")), {"source": source}, {})
    return json.loads(Path(result["document"]).read_text(encoding="utf-8"))


def test_conflicting_versions_run_in_distinct_profiles_without_host_dependencies(tmp_path, monkeypatch):
    one = make_package(tmp_path / "one", "1.0.0")
    two = make_package(tmp_path / "two", "2.0.0")
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1]))
    before = set(sys.modules)
    first = prepare_python_environment(one, tmp_path / "profiles")
    second = prepare_python_environment(two, tmp_path / "profiles")
    a, b = run(one, tmp_path), run(two, tmp_path)
    assert a["version"] == "1.0.0" and b["version"] == "2.0.0"
    assert not a["hostVisible"] and not b["hostVisible"]
    assert a["prefix"] != b["prefix"]
    assert "pymss_isolation_probe" not in set(sys.modules) - before
    moved = tmp_path / "disabled"
    one.rename(moved)
    assert run(moved, tmp_path)["version"] == "1.0.0"
    assert Path(first).is_dir() and Path(second).is_dir()


def test_failed_preparation_preserves_old_binding_and_removes_partial_profile(tmp_path):
    root = make_package(tmp_path / "plugin")
    profile = prepare_python_environment(root, tmp_path / "profiles")
    binding = (root / ".pymss-python.json").read_bytes()
    wheel = next((root / "wheels").glob("*.whl"))
    wheel.write_bytes(b"corrupt wheel")
    with pytest.raises(OperationError, match="hash"):
        prepare_python_environment(root, tmp_path / "profiles")
    assert (root / ".pymss-python.json").read_bytes() == binding
    assert Path(profile).is_dir()
    assert len(list((tmp_path / "profiles").glob("python-*"))) == 1


def test_unprepared_python_entry_does_not_fall_back_to_host(tmp_path):
    root = make_package(tmp_path / "plugin")
    with pytest.raises(OperationError, match="prepared"):
        run(root, tmp_path)


def test_profile_cleanup_waits_for_active_execution_lease(tmp_path):
    from pymss.operations.python_profiles import python_environment

    root = make_package(tmp_path / "plugin")
    profile = Path(prepare_python_environment(root, tmp_path / "profiles"))
    with python_environment(root):
        assert remove_python_environment(profile) is False
    assert remove_python_environment(profile) is True
    assert not profile.exists()


def test_python_rpc_uses_the_same_callbacks_and_file_publication(tmp_path):
    from pymss.operations.host_services import ReadonlyHost

    root = make_package(tmp_path / "rpc")
    manifest = root / "pyproject.toml"
    text = manifest.read_text(encoding="utf-8").replace("[tool.pymss.commands", "[tool.pymss.rpc_process")
    text = text.replace('arguments = [{input="source"}, {output="document"}]',
                        'arguments = ["file"]\nprotocol_version = 1\npermissions = ["host.fileInfo"]')
    manifest.write_text(text, encoding="utf-8")
    (root / "main.py").write_bytes((Path(__file__).parent / "fixtures/rpc_peer.py").read_bytes())
    prepare_python_environment(root, tmp_path / "profiles")
    source = tmp_path / "source.txt"
    source.write_text("text", encoding="utf-8")
    operation = read_commands(root, adapter="rpc-process")[0]
    context = OperationContext(workdir=str(tmp_path / "output"), host_request=ReadonlyHost("python-profile"))
    result = operation.run(context, {"source": source}, {})
    output = json.loads(Path(result["document"]).read_text(encoding="utf-8"))
    assert output["callbacks"][0]["result"]["size"] == 4
    assert output["identity"]["pluginId"] == "author.isolated"


def test_incomplete_wheel_closure_is_rejected_without_publishing_a_binding(tmp_path):
    root = make_package(tmp_path / "plugin")
    wheel = next((root / "wheels").glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, value in contents.items():
            if name.endswith("/METADATA"):
                value += b"Requires-Dist: pymss-missing-dependency==1.0\n"
            archive.writestr(name, value)
    lock = root / "python-lock.json"
    data = json.loads(lock.read_text(encoding="utf-8"))
    data["targets"]["any"][0]["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    lock.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(OperationError) as caught:
        prepare_python_environment(root, tmp_path / "profiles")
    assert "pymss-missing-dependency" in caught.value.extra["stderr"]
    assert not (root / ".pymss-python.json").exists()
    assert not list((tmp_path / "profiles").glob("python-*"))


@pytest.mark.parametrize("mutation", ["targets", "script", "lock"])
def test_python_declarations_reject_ambiguous_or_missing_inputs(tmp_path, mutation):
    root = make_package(tmp_path / "plugin")
    manifest = root / "pyproject.toml"
    text = manifest.read_text(encoding="utf-8")
    if mutation == "targets":
        text = text.replace('python = "main.py"', 'python = "main.py"\ntargets = {}')
    elif mutation == "script":
        text = text.replace('python = "main.py"', 'python = "../main.py"')
    else:
        (root / "python-lock.json").unlink()
    manifest.write_text(text, encoding="utf-8")
    with pytest.raises(OperationError):
        read_commands(root)


def test_interrupted_retirement_can_resume_after_the_inner_marker_was_deleted(tmp_path, monkeypatch):
    from pymss.operations import python_profiles as profiles

    root = make_package(tmp_path / "plugin")
    profile = prepare_python_environment(root, tmp_path / "profiles")
    original = profiles.shutil.rmtree

    def interrupted(path, *args, **kwargs):
        (Path(path) / ".pymss-profile.json").unlink(missing_ok=True)
        raise PermissionError("A dependency file is still open")

    with monkeypatch.context() as patch:
        patch.setattr(profiles.shutil, "rmtree", interrupted)
        assert remove_python_environment(profile) is False
    assert not profile.exists()
    assert (profile.parent / ("." + profile.name + ".retired.json")).is_file()
    assert remove_python_environment(profile) is True
    assert not (profile.parent / (".retired-" + profile.name)).exists()
    assert not (profile.parent / ("." + profile.name + ".retired.json")).exists()
    assert profiles.shutil.rmtree == original


def test_cancelled_preparation_removes_the_candidate_without_touching_the_binding(tmp_path):
    root = make_package(tmp_path / "plugin")
    (root / ".pymss-python.json").write_bytes(b"previous binding")
    cancelled = threading.Event()
    context = OperationContext(cancelled=cancelled.is_set)
    with pytest.raises(OperationError, match="cancelled"):
        prepare_python_environment(root, tmp_path / "profiles", context=context,
                                   on_created=lambda _profile: cancelled.set())
    assert (root / ".pymss-python.json").read_bytes() == b"previous binding"
    assert not list((tmp_path / "profiles").glob("python-*"))


def test_cancelling_a_prepared_python_process_reaps_its_child_and_releases_the_lease(tmp_path):
    from test.test_command_process import process_alive

    root = make_package(tmp_path / "plugin")
    (root / "main.py").write_text('''import subprocess,sys,time
from pathlib import Path
child=subprocess.Popen([sys.executable,"-I","-c","import time;time.sleep(60)"])
marker=Path(sys.argv[1]).with_suffix(".pid")
partial=marker.with_suffix(".partial")
partial.write_text(str(child.pid),encoding="ascii")
partial.replace(marker)
time.sleep(60)
''', encoding="utf-8")
    profile = prepare_python_environment(root, tmp_path / "profiles")
    source = tmp_path / "source.txt"
    source.write_text("text", encoding="utf-8")
    marker = source.with_suffix(".pid")
    operation = read_commands(root)[0]
    context = OperationContext(workdir=str(tmp_path / "output"), cancelled=marker.exists)
    with pytest.raises(OperationError, match="cancelled"):
        operation.run(context, {"source": source}, {})
    assert not process_alive(int(marker.read_text(encoding="ascii")))
    assert not list((tmp_path / "output").iterdir())
    assert remove_python_environment(profile) is True


def test_registered_operations_pin_peer_code_as_well_as_dependencies(tmp_path):
    from pymss.operations import Runtime

    root = make_package(tmp_path / "plugins/author.isolated")
    profile = prepare_python_environment(root, tmp_path / "profiles")
    runtime = Runtime()
    reports = runtime.load_plugins([root.parent])
    assert reports[0]["status"] == "activated", reports
    source = tmp_path / "source.txt"
    source.write_text("input", encoding="utf-8")
    (root / "main.py").write_text("raise RuntimeError('New code requires a new binding')\n", encoding="utf-8")
    output = runtime.run("author.isolated.run", inputs={"source": source}, params={},
                         context=OperationContext(workdir=str(tmp_path / "output")))
    assert json.loads(Path(output["document"]).read_text(encoding="utf-8"))["version"] == "1.0.0"
    with pytest.raises(OperationError, match="prepared again"):
        read_commands(root)[0].run(OperationContext(workdir=str(tmp_path / "new")), {"source": source}, {})
    assert Path(profile).is_dir()


def test_failed_preparation_cleanup_uses_the_recoverable_retirement_path(tmp_path, monkeypatch):
    from pymss.operations import python_profiles as profiles

    root = make_package(tmp_path / "plugin")
    (root / ".pymss-python.json").write_bytes(b"old binding")
    created = []

    def fail(*_args, **_kwargs):
        raise OperationError("Environment provisioning failed")

    def interrupted(path, *_args, **_kwargs):
        (Path(path) / ".pymss-profile.json").unlink(missing_ok=True)
        raise PermissionError("A dependency file is still open")

    with monkeypatch.context() as patch:
        patch.setattr(profiles, "run_command_process", fail)
        patch.setattr(profiles.shutil, "rmtree", interrupted)
        with pytest.raises(OperationError, match="provisioning failed"):
            prepare_python_environment(root, tmp_path / "profiles", on_created=created.append)
    assert (root / ".pymss-python.json").read_bytes() == b"old binding"
    assert len(created) == 1
    profile = created[0]
    assert (profile.parent / ("." + profile.name + ".retired.json")).is_file()
    assert remove_python_environment(profile) is True


def test_cli_can_prepare_before_loading_an_unprepared_plugin(tmp_path, monkeypatch, capsys):
    from pymss.cli import build_parser

    root = make_package(tmp_path / "plugin")

    def premature_activation():
        raise AssertionError("Plugin activation must wait until its profile is ready")

    monkeypatch.setattr("pymss.plugins.ensure_plugins_loaded", premature_activation)
    args = build_parser().parse_args(["ops", "prepare-python", str(root), "--profiles-dir", str(tmp_path / "profiles")])
    assert args.func(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert Path(result["profile"]).is_dir()


def test_exception_after_atomic_binding_commit_cannot_delete_its_profile(tmp_path, monkeypatch):
    from pymss.operations import python_profiles as profiles

    root = make_package(tmp_path / "plugin")
    write = profiles._write_json

    def interrupted(path, value):
        write(path, value)
        if path.name == profiles.BINDING:
            raise OSError("Interrupted after the binding was committed")

    with monkeypatch.context() as patch:
        patch.setattr(profiles, "_write_json", interrupted)
        with pytest.raises(OSError, match="binding was committed"):
            prepare_python_environment(root, tmp_path / "profiles")
    binding = json.loads((root / profiles.BINDING).read_bytes())
    assert Path(binding["profile"]).is_dir()
    assert run(root, tmp_path)["version"] == "1.0.0"


@pytest.mark.parametrize("version", [True, 2])
def test_unknown_profile_formats_are_never_deleted(tmp_path, version):
    profile = tmp_path / ("python-" + uuid.uuid4().hex)
    profile.mkdir()
    (profile / ".pymss-profile.json").write_text(json.dumps({"formatVersion": version,
        "planDigest": "0" * 64, "profile": str(profile)}), encoding="utf-8")
    with pytest.raises(OperationError, match="unmanaged"):
        remove_python_environment(profile)
    assert profile.is_dir()
