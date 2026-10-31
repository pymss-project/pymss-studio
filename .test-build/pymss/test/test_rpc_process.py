"""Bidirectional RPC stays bounded, task-owned and cancellable."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import subprocess
import threading
import time

import pytest

from pymss.operations import OperationContext, OperationError
from pymss.operations._command_process import CommandProcessError, run_command_process
from pymss.operations._rpc_session import RpcProcessError, RpcSession
from pymss.operations.host_services import ReadonlyHost
from pymss.operations import _rpc_session as sessions
from test.test_command_process import process_alive, wait_for

PEER = Path(__file__).with_name("fixtures") / "rpc_peer.py"
ALL = frozenset({"host.taskInfo", "host.fileInfo"})


def session(tmp_path, *, host=ReadonlyHost("test"), permissions=ALL, timeout=.3, context=None, operation="alice.convert.run"):
    source = tmp_path / "source.txt"
    source.write_bytes(b"selected input")
    context = context or OperationContext(host_request=host)
    protocol = RpcSession(context=context, plugin_id="alice.convert", operation_id=operation,
        inputs={"source": source}, params={}, output_name="document", output=tmp_path / "output.txt",
        permissions=permissions, host_timeout=timeout, handshake_timeout=1)
    return protocol


def run(protocol, scenario, tmp_path, *, timeout=5):
    run_command_process([getattr(sys, "_base_executable", None) or sys.executable, "-I", "-B", str(PEER), scenario], cwd=tmp_path,
                        context=protocol.context, timeout=timeout, protocol=protocol)
    return json.loads((tmp_path / "output.txt").read_text(encoding="utf-8"))


def test_native_host_requests_return_fixed_task_identity(tmp_path):
    protocol = session(tmp_path)
    result = run(protocol, "identity", tmp_path)
    info = result["callbacks"][0]["result"]
    assert info["pluginId"] == "alice.convert"
    assert info["operationId"] == "alice.convert.run"
    assert info["taskId"] == protocol.task_id
    assert info["host"] == "test" and info["interactive"] is False


def test_file_callback_only_describes_the_selected_input_reference(tmp_path):
    result = run(session(tmp_path), "file", tmp_path)
    info = result["callbacks"][0]["result"]
    assert info == {"input": "source", "name": "source.txt", "size": 14, "extension": "txt"}


@pytest.mark.parametrize("scenario,host,permissions,code", [
    ("denied", ReadonlyHost("test"), frozenset({"host.taskInfo"}), "HOST_PERMISSION_DENIED"),
    ("identity", None, ALL, "HOST_UNAVAILABLE"),
    ("unknown", ReadonlyHost("test"), ALL, "HOST_METHOD_NOT_FOUND"),
    ("forged_path", ReadonlyHost("test"), ALL, "HOST_INVALID_PARAMS"),
    ("unselected", ReadonlyHost("test"), ALL, "HOST_PERMISSION_DENIED"),
])
def test_denied_and_unsupported_callbacks_return_errors_without_implicit_approval(tmp_path, scenario, host, permissions, code):
    result = run(session(tmp_path, host=host, permissions=permissions), scenario, tmp_path)
    assert result["callbacks"][0]["error"]["data"]["code"] == code


def test_multiple_callbacks_are_correlated_even_when_services_finish_out_of_order(tmp_path):
    def host(request):
        time.sleep((3 - int(request.id.split(":")[1])) * .025)
        return {"request": request.id}

    result = run(session(tmp_path, host=host), "concurrent", tmp_path)
    replies = result["callbacks"]
    assert {reply["id"] for reply in replies} == {f"plugin:{i}" for i in range(4)}
    assert all(reply["result"]["request"] == reply["id"] for reply in replies)


def test_host_timeout_signals_the_callback_and_the_peer_can_handle_the_error(tmp_path):
    returned = threading.Event()

    def host(request):
        while not request.cancelled():
            time.sleep(.005)
        returned.set()
        request.check_active()

    result = run(session(tmp_path, host=host, timeout=.05), "identity", tmp_path)
    assert result["callbacks"][0]["error"]["data"]["code"] == "HOST_TIMEOUT"
    assert returned.wait(.5)


def test_task_cancellation_during_a_host_callback_wakes_it_and_reaps_the_peer(tmp_path):
    entered, finished = threading.Event(), threading.Event()
    before = set(threading.enumerate())

    def host(request):
        entered.set()
        while not request.cancelled():
            time.sleep(.005)
        finished.set()
        request.check_active()

    context = OperationContext(host_request=host, cancelled=entered.is_set)
    protocol = session(tmp_path, context=context)
    with pytest.raises(OperationError, match="cancelled"):
        run(protocol, "identity", tmp_path)
    assert finished.wait(.5)
    assert set(threading.enumerate()) <= before
    assert not (tmp_path / "output.txt").exists()


@pytest.mark.parametrize("scenario,code", [
    ("wrong_version", "RPC_VERSION_MISMATCH"), ("bad_json", "RPC_INVALID_MESSAGE"),
    ("oversized", "RPC_FRAME_LIMIT"), ("early_exit", "RPC_PEER_EXITED"),
    ("wrong_output", "RPC_INVALID_RESULT"), ("bad_progress", "RPC_INVALID_MESSAGE"),
    ("duplicate", "RPC_DUPLICATE_REQUEST"), ("no_initialize", "RPC_HANDSHAKE_TIMEOUT"),
    ("duplicate_response", "RPC_UNEXPECTED_RESPONSE"),
    ("foreign_response", "RPC_UNEXPECTED_RESPONSE"), ("worker_envelope", "RPC_INVALID_MESSAGE"),
    ("stdout_eof", "RPC_PEER_EXITED"), ("shutdown_hang", "RPC_EXIT_TIMEOUT"),
    ("bool_version", "RPC_VERSION_MISMATCH"), ("overflow_number", "RPC_INVALID_MESSAGE"),
])
def test_protocol_errors_fail_closed(tmp_path, scenario, code):
    with pytest.raises(RpcProcessError) as caught:
        run(session(tmp_path), scenario, tmp_path)
    assert caught.value.code == code


def test_progress_notifications_are_bound_to_the_task_and_do_not_finish_it(tmp_path):
    values = []
    context = OperationContext(progress=lambda fraction, message, event: values.append((fraction, message, event)))
    protocol = session(tmp_path, context=context)
    result = run(protocol, "no_callback", tmp_path)
    assert result["callbacks"] == []
    assert protocol.state == "done"
    assert values
    assert all(event["payload"]["taskId"] == protocol.task_id for _, _, event in values)
    assert all(0 <= fraction <= .94 for fraction, _, _ in values)


def test_separate_tasks_can_reuse_peer_request_ids_without_cross_delivery(tmp_path):
    def one(index):
        directory = tmp_path / str(index)
        directory.mkdir()
        protocol = session(directory, operation=f"alice.convert.run{index}")
        return run(protocol, "identity", directory)["callbacks"][0]["result"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(one, range(2)))
    assert first["operationId"] != second["operationId"]
    assert first["taskId"] != second["taskId"]


def test_custom_host_exception_does_not_leak_exception_payloads_to_the_peer(tmp_path):
    def host(_request):
        raise ValueError("private credential value")

    result = run(session(tmp_path, host=host), "identity", tmp_path)
    error = result["callbacks"][0]["error"]
    assert error["data"]["code"] == "HOST_SERVICE_FAILED"
    assert "credential" not in json.dumps(error)


def test_remote_error_is_preserved_when_the_process_exits_before_the_last_update(tmp_path):
    protocol = session(tmp_path)
    update = protocol.update

    def delay_run_response():
        if protocol.state != "running" or protocol.stop.is_set():
            update()

    protocol.update = delay_run_response
    with pytest.raises(RpcProcessError) as caught:
        run(protocol, "run_error", tmp_path)
    assert caught.value.code == "RPC_REMOTE_ERROR"
    assert str(caught.value) == "Input format is invalid"
    assert caught.value.extra["exitCode"] == 1


def test_a_failed_start_without_protocol_frames_preserves_exit_diagnostics(tmp_path):
    with pytest.raises(CommandProcessError) as caught:
        run(session(tmp_path), "exit_nonzero", tmp_path)
    assert caught.value.code == "COMMAND_FAILED"
    assert "status 7" in str(caught.value)
    assert "Backend initialization failed" in caught.value.extra["stderr"]


def test_a_peer_that_stops_reading_cannot_block_the_task_timeout(tmp_path):
    protocol = session(tmp_path)
    protocol.params = {"value": "x" * 200000}
    before = set(threading.enumerate())
    with pytest.raises(OperationError) as caught:
        run(protocol, "ignore_run", tmp_path, timeout=.3)
    assert caught.value.code == "COMMAND_TIMEOUT"
    assert set(threading.enumerate()) <= before


def test_peer_exit_while_callback_waits_cancels_the_request_and_closes_its_state(tmp_path):
    entered, exited = threading.Event(), threading.Event()

    def host(request):
        entered.set()
        while not request.cancelled():
            time.sleep(.005)
        exited.set()
        request.check_active()

    protocol = session(tmp_path, host=host)
    with pytest.raises(RpcProcessError):
        run(protocol, "callback_exit", tmp_path)
    assert entered.is_set() and exited.wait(.5)
    assert protocol.active == {}
    assert protocol.abandoned_requests


def test_progress_flood_is_coalesced_without_losing_the_reliable_terminal_response(tmp_path):
    values = []
    protocol = session(tmp_path, context=OperationContext(progress=lambda *args: values.append(args)))
    result = run(protocol, "progress_flood", tmp_path)
    assert result["callbacks"][0]["error"]["data"]["code"] == "HOST_UNAVAILABLE"
    assert protocol.state == "done"
    assert len(values) < 5000


def test_cancelled_callback_results_do_not_enter_the_reliable_completion_queue(tmp_path):
    protocol = session(tmp_path, host=lambda request: time.sleep(.1), timeout=.02)
    protocol.state = "running"
    sent = []
    protocol._send = sent.append
    protocol._host_request({"jsonrpc": "2.0", "id": "plugin:late", "method": "host.taskInfo", "params": {}})
    time.sleep(.03)
    protocol.update()
    assert sent[0]["error"]["data"]["code"] == "HOST_TIMEOUT"
    for worker in protocol.workers:
        worker.join(timeout=.5)
    assert protocol.completed.empty()
    protocol.close()


def test_stalled_host_calls_share_a_process_wide_admission_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "_HOST_SLOTS", threading.BoundedSemaphore(2))
    release = threading.Event()
    protocols = []

    def host(_request):
        release.wait(1)
        return {}

    try:
        for index in range(3):
            directory = tmp_path / str(index)
            directory.mkdir()
            protocol = session(directory, host=host)
            protocol.state = "running"
            protocol._send = lambda value, responses=protocol.incoming: responses.put_nowait(value)
            protocols.append(protocol)
            protocol._host_request({"jsonrpc": "2.0", "id": "plugin:1", "method": "host.taskInfo", "params": {}})
        assert len(protocols[0].workers) == len(protocols[1].workers) == 1
        assert not protocols[2].workers
        error = protocols[2].incoming.get_nowait()["error"]
        assert error["data"]["code"] == "HOST_BUSY"
    finally:
        release.set()
        for protocol in protocols:
            protocol.close()


def test_a_legacy_context_without_callback_support_returns_unavailable(tmp_path):
    from types import SimpleNamespace

    context = SimpleNamespace(check_cancelled=lambda: None, report_progress=lambda *args, **kwargs: None)
    result = run(session(tmp_path, context=context), "identity", tmp_path)
    assert result["callbacks"][0]["error"]["data"]["code"] == "HOST_UNAVAILABLE"


@pytest.mark.skipif(os.name == "nt", reason="Requires POSIX process-group and pipe semantics")
def test_hard_killed_host_reaps_a_peer_that_stops_reading_its_protocol_input(tmp_path):
    marker, writing = tmp_path / "peer.json", tmp_path / "writing"
    peer = tmp_path / "blocked-peer.py"
    peer.write_text(
        'import json,os,sys,time\nfrom pathlib import Path\n'
        'request=json.loads(sys.stdin.buffer.readline())\n'
        'print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{"protocolVersion":1}}),flush=True)\n'
        f'p=Path({str(marker)!r});t=p.with_suffix(".partial");t.write_text(json.dumps(os.getpid()));os.replace(t,p)\n'
        'time.sleep(30)\n', encoding="utf-8",
    )
    worker = tmp_path / "blocked-host.py"
    worker.write_text(
        'import sys\nfrom pathlib import Path\n'
        f'sys.path.insert(0,{str(Path(__file__).resolve().parents[1])!r})\n'
        'from pymss.operations import OperationContext\n'
        'from pymss.operations._rpc_session import RpcSession\n'
        'from pymss.operations import _command_process as runner\n'
        'spawn=runner.subprocess.Popen\n'
        'class Input:\n'
        ' def __init__(self,stream): self.stream=stream\n'
        ' def write(self,data):\n'
        f'  if len(data)>100000: Path({str(writing)!r}).touch()\n'
        '  return self.stream.write(data)\n'
        ' def flush(self): return self.stream.flush()\n'
        ' def close(self): return self.stream.close()\n'
        'def start(*args,**kwargs):\n'
        ' child=spawn(*args,**kwargs);child.stdin=Input(child.stdin);return child\n'
        'runner.subprocess.Popen=start\n'
        f'root=Path({str(tmp_path)!r});context=OperationContext()\n'
        'protocol=RpcSession(context=context,plugin_id="alice.convert",operation_id="alice.convert.run",\n'
        ' inputs={},params={"value":"x"*200000},output_name="document",output=root/"output.txt",permissions=frozenset())\n'
        f'runner.run_command_process([sys.executable,"-I","-B",{str(peer)!r}],cwd=root,context=context,timeout=30,protocol=protocol)\n',
        encoding="utf-8",
    )
    process = subprocess.Popen([sys.executable, "-I", "-B", str(worker)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pid = group = None
    try:
        wait_for(lambda: marker.exists() and writing.exists(), seconds=15)
        pid = json.loads(marker.read_text())
        group = os.getpgid(pid)
        process.kill()
        process.wait(timeout=5)
        wait_for(lambda: not process_alive(pid))
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
        if pid is not None and process_alive(pid) and group is not None and os.getpgid(pid) == group:
            os.killpg(group, 9)
