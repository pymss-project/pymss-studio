"""Command processes have bounded diagnostics and task-owned descendants."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from pymss.operations import OperationContext, OperationError
from pymss.operations._command_process import CommandProcessError, run_command_process
from pymss.operations import _command_process as process_runner


@pytest.mark.parametrize("platform,observed,late_exit,allowed", [
    ("darwin", True, False, True),
    ("darwin", False, True, True),
    ("darwin", False, False, False),
    ("darwin", False, None, False),
    ("linux", True, True, False),
])
def test_denied_group_signal_is_deferred_only_for_an_exited_darwin_gate(monkeypatch, platform, observed, late_exit, allowed):
    def denied(*_):
        raise PermissionError("The process group cannot be signalled")

    monkeypatch.setattr(process_runner, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(process_runner, "os", SimpleNamespace(killpg=denied))
    observer = SimpleNamespace(exited=lambda: late_exit) if late_exit is not None else None
    if allowed:
        process_runner._terminate_posix_group(SimpleNamespace(pid=37), observer, observed)
    else:
        with pytest.raises(PermissionError):
            process_runner._terminate_posix_group(SimpleNamespace(pid=37), observer, observed)


def test_a_denied_darwin_group_probe_does_not_claim_the_group_is_gone(monkeypatch):
    results = iter([PermissionError("Zombie-only or denied group"), ProcessLookupError("No process group")])

    def signal(*_):
        raise next(results)

    monkeypatch.setattr(process_runner, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(process_runner, "os", SimpleNamespace(killpg=signal))
    assert process_runner._posix_group_alive(37) is True
    assert process_runner._posix_group_alive(37) is False


def command(code):
    return [sys.executable, "-I", "-c", code]


def process_alive(pid):
    if os.name == "nt":
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return api.WaitForSingleObject(handle, 0) == 258
        finally:
            api.CloseHandle(handle)
    status = Path(f"/proc/{pid}/stat")
    if status.is_file() and status.read_text().split(") ", 1)[1].startswith("Z"):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_for(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    assert condition()


def test_stdout_and_stderr_are_drained_without_protocol_interpretation(tmp_path):
    code = 'import sys; sys.stdout.write("{\\\"type\\\":\\\"operation_result\\\"}\\n" + "a"*100000); sys.stderr.write("b"*100000)'
    result = run_command_process(command(code), cwd=tmp_path, context=OperationContext(), timeout=5)
    assert result.returncode == 0
    assert len(result.stderr) == 100000
    assert result.stdout.endswith("a" * 100000)


def test_argv_values_are_passed_without_a_shell(tmp_path):
    values = ['space and 中文', '"quoted"', '$(touch should_not_exist)', '& echo nope', '{input.source}']
    result = run_command_process(command('import sys,json;print(json.dumps(sys.argv[1:]))') + values,
                                 cwd=tmp_path, context=OperationContext(), timeout=5)
    assert json.loads(result.stdout) == values
    assert not (tmp_path / "should_not_exist").exists()


def test_nonzero_exit_has_bounded_diagnostic_context(tmp_path):
    with pytest.raises(CommandProcessError) as caught:
        run_command_process(command('import sys;print("backend refused input",file=sys.stderr);sys.exit(7)'),
                            cwd=tmp_path, context=OperationContext(), timeout=5)
    assert caught.value.code == "COMMAND_FAILED"
    assert "status 7" in str(caught.value)
    assert "backend refused input" in caught.value.extra["stderr"]


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_chatty_commands_keep_only_a_bounded_diagnostic_tail(tmp_path, stream):
    result = run_command_process(command(f'import sys;sys.{stream}.write("x"*100000+"end");sys.{stream}.flush()'),
                                 cwd=tmp_path, context=OperationContext(), output_limit=1024, timeout=5)
    assert len(getattr(result, stream)) == 1024
    assert getattr(result, stream).endswith("end")


def test_timeout_stops_a_command_and_does_not_wait_for_a_full_pipe(tmp_path):
    before = time.monotonic()
    with pytest.raises(CommandProcessError) as caught:
        run_command_process(command('import time;time.sleep(30)'), cwd=tmp_path,
                            context=OperationContext(), timeout=0.15)
    assert caught.value.code == "COMMAND_TIMEOUT"
    assert time.monotonic() - before < 5


def descendant_code(tmp_path, *, exit_parent=False):
    marker = tmp_path / "child.json"
    child = (f'import os,time,json;from pathlib import Path;p=Path({str(marker)!r});'
             't=p.with_suffix(".partial");t.write_text(json.dumps(os.getpid()));os.replace(t,p);time.sleep(30)')
    parent = f'import subprocess,sys,time;subprocess.Popen([sys.executable,"-I","-c",{child!r}]);'
    if exit_parent:
        parent += f'from pathlib import Path\np=Path({str(marker)!r})\nwhile not p.exists(): time.sleep(.01)'
    else:
        parent += 'time.sleep(30)'
    return marker, parent


@pytest.mark.parametrize("exit_parent", [False, True])
def test_cancellation_and_normal_exit_reap_descendants_holding_pipes(tmp_path, exit_parent):
    marker, code = descendant_code(tmp_path, exit_parent=exit_parent)
    context = OperationContext(cancelled=lambda: marker.exists() and not exit_parent)
    if exit_parent:
        run_command_process(command(code), cwd=tmp_path, context=context, timeout=5)
    else:
        with pytest.raises(OperationError, match="cancelled"):
            run_command_process(command(code), cwd=tmp_path, context=context, timeout=5)
    wait_for(marker.exists)
    child = json.loads(marker.read_text())
    wait_for(lambda: not process_alive(child))


def test_cancellation_before_launch_never_starts_the_program(tmp_path):
    with pytest.raises(OperationError, match="cancelled"):
        run_command_process(command('raise AssertionError("should not run")'), cwd=tmp_path,
                            context=OperationContext(cancelled=lambda: True), timeout=5)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("options", [{"output_limit": 0}, {"output_limit": -1}, {"timeout": float("nan")}, {"timeout": 0}])
def test_invalid_resource_limits_fail_before_launch(tmp_path, options):
    with pytest.raises(CommandProcessError) as caught:
        run_command_process(command('raise AssertionError("should not run")'), cwd=tmp_path,
                            context=OperationContext(), **options)
    assert caught.value.code == "COMMAND_ARGUMENTS_INVALID"


def test_forced_worker_exit_closes_the_process_ownership_scope(tmp_path):
    marker, child = descendant_code(tmp_path)
    worker = tmp_path / "worker.py"
    source_root = str(Path(__file__).resolve().parents[1])
    worker.write_text(
        'import sys\nfrom pathlib import Path\n'
        f'sys.path.insert(0,{source_root!r})\n'
        'from pymss.operations import OperationContext\n'
        'from pymss.operations._command_process import run_command_process\n'
        f'run_command_process({command(child)!r},cwd=Path({str(tmp_path)!r}),context=OperationContext(),timeout=30)\n',
        encoding="utf-8",
    )
    process = subprocess.Popen([sys.executable, "-I", str(worker)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_for(marker.exists, seconds=15)
        pid = json.loads(marker.read_text())
        process.kill()
        process.wait(timeout=5)
        wait_for(lambda: not process_alive(pid))
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_cancelled_waiter_does_not_leave_reader_threads(tmp_path):
    before = set(threading.enumerate())
    with pytest.raises(CommandProcessError):
        run_command_process(command('import time;time.sleep(30)'), cwd=tmp_path,
                            context=OperationContext(), timeout=0.1)
    assert set(threading.enumerate()) <= before


def test_windows_style_einval_on_stdin_still_reaps_and_closes_readers(tmp_path, monkeypatch):
    spawn = subprocess.Popen
    created = []
    before = set(threading.enumerate())

    class BrokenInput:
        def __init__(self, stream):
            self.stream = stream

        def write(self, _data):
            raise OSError(22, "Invalid argument")

        def flush(self):
            raise OSError(22, "Invalid argument")

        def close(self):
            self.stream.close()
            raise OSError(22, "Invalid argument")

    def broken_start(*args, **kwargs):
        child = spawn(*args, **kwargs)
        child.stdin = BrokenInput(child.stdin)
        created.append(child)
        return child

    monkeypatch.setattr(process_runner.subprocess, "Popen", broken_start)
    with pytest.raises(CommandProcessError):
        run_command_process(command('print("not released")'), cwd=tmp_path, context=OperationContext(), timeout=2)
    assert created[0].poll() is not None
    assert created[0].stdout.closed and created[0].stderr.closed
    assert set(threading.enumerate()) <= before


def test_exit_observation_uses_kqueue_without_a_waitid_api(monkeypatch):
    class Queue:
        closed = False

        def control(self, change, count, timeout):
            return ["exited"] if change is None else []

        def close(self):
            self.closed = True

    queue = Queue()
    monkeypatch.setattr(process_runner, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(process_runner, "select", SimpleNamespace(
        kqueue=lambda: queue, kevent=lambda *args, **kwargs: "watch", KQ_FILTER_PROC=1,
        KQ_EV_ADD=2, KQ_EV_ONESHOT=4, KQ_NOTE_EXIT=8,
    ))
    observer = process_runner._ExitObserver(SimpleNamespace(pid=123))
    assert observer.exited()
    observer.close()
    assert queue.closed
