"""RPC declarations reuse v2 registration and reject ungranted services."""
from pathlib import Path

import pytest

from pymss.operations import OperationError, Runtime
from pymss.operations.command import read_commands
from pymss.operations.rpc_process import RpcOperation
from pymss.plugins import get_registry
from test.test_command_operations import package  # noqa: F401


@pytest.fixture
def rpc_package(package: Path):  # noqa: F811 - pytest resolves the imported fixture by name
    manifest = package / "pyproject.toml"
    text = manifest.read_text(encoding="utf-8").replace("[tool.pymss.commands", "[tool.pymss.rpc_process")
    text = text.replace('arguments = ["--source",{input="source"},"--target",{output="document"},"--mode",{param="mode"}]',
                        'arguments = []\nprotocol_version = 1\npermissions = ["host.taskInfo", "host.fileInfo"]\nhost_timeout = 2')
    manifest.write_text(text, encoding="utf-8")
    entry = package / "__init__.py"
    entry.write_text(entry.read_text(encoding="utf-8").replace("register_commands", "register_rpc_processes"), encoding="utf-8")
    return package


def test_rpc_descriptions_register_without_starting_a_program(rpc_package):
    runtime = Runtime()
    try:
        reports = runtime.load_plugins([rpc_package.parent])
        assert reports[0]["status"] == "activated", reports
        spec = runtime.describe_operation("author.convert.run")
        assert spec["inputs"]["source"]["type"] == "file"
        operation = read_commands(rpc_package, adapter="rpc-process")[0]
        assert isinstance(operation, RpcOperation)
        assert operation.plugin_id == "author.convert"
        assert operation.permissions == {"host.taskInfo", "host.fileInfo"}
    finally:
        get_registry().nodes.pop("author_convert", None)


@pytest.mark.parametrize("old,new,error", [
    ("protocol_version = 1", "protocol_version = 2", "protocol_version"),
    ('permissions = ["host.taskInfo", "host.fileInfo"]', 'permissions = ["host.taskInfo", "host.taskInfo"]', "permissions"),
    ('permissions = ["host.taskInfo", "host.fileInfo"]', 'permissions = ["host.deleteFiles"]', "permissions"),
    ('permissions = ["host.taskInfo", "host.fileInfo"]', 'permissions = [{}]', "permissions"),
    ("host_timeout = 2", "host_timeout = nan", "timeout"),
    ("arguments = []", 'arguments = [{input="source"}]', "literals"),
])
def test_invalid_rpc_policy_fails_before_activation(rpc_package, old, new, error):
    manifest = rpc_package / "pyproject.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    with pytest.raises(OperationError, match=error):
        read_commands(rpc_package, adapter="rpc-process")
