"""Parent lifetime is independent of a stalled protocol destination."""
from collections import deque
from types import SimpleNamespace
import threading

from pymss.operations import _command_bootstrap as gate


def test_parent_eof_is_observed_while_native_stdin_is_full(monkeypatch):
    reads = deque([b"request" * 1000, b""])
    ready = deque([([], [6], []), ([5], [], [])])
    attempts, stopped = [], []
    source = SimpleNamespace(fileno=lambda: 5, read1=lambda _count: reads.popleft())
    sink = SimpleNamespace(fileno=lambda: 6, close=lambda: None)

    def full_pipe(_fd, _data):
        attempts.append(True)
        raise BlockingIOError("Native process is not reading")

    monkeypatch.setattr(gate, "os", SimpleNamespace(
        set_blocking=lambda *_: None, read=lambda *_: reads.popleft(), write=full_pipe, getpgrp=lambda: 23,
        killpg=lambda group, signal: stopped.append((group, signal)),
    ))
    monkeypatch.setattr(gate, "select", SimpleNamespace(select=lambda *_: ready.popleft()))
    monkeypatch.setattr(gate, "signal", SimpleNamespace(SIGKILL=9))
    gate._relay_posix(source, sink, threading.Event())
    assert attempts
    assert stopped == [(23, 9)]


def test_a_peer_closing_stdin_does_not_hide_later_parent_eof(monkeypatch):
    reads = deque([b"request", b"discarded", b""])
    ready = deque([([], [6], []), ([5], [], []), ([5], [], [])])
    stopped = []
    source = SimpleNamespace(fileno=lambda: 5, read1=lambda _count: reads.popleft())
    sink = SimpleNamespace(fileno=lambda: 6, close=lambda: None)

    def closed_pipe(_fd, _data):
        raise BrokenPipeError("Native process closed stdin")

    monkeypatch.setattr(gate, "os", SimpleNamespace(
        set_blocking=lambda *_: None, read=lambda *_: reads.popleft(), write=closed_pipe, getpgrp=lambda: 23,
        killpg=lambda group, signal: stopped.append((group, signal)),
    ))
    monkeypatch.setattr(gate, "select", SimpleNamespace(select=lambda *_: ready.popleft()))
    monkeypatch.setattr(gate, "signal", SimpleNamespace(SIGKILL=9))
    gate._relay_posix(source, sink, threading.Event())
    assert stopped == [(23, 9)]


def test_spurious_read_readiness_does_not_count_as_parent_eof(monkeypatch):
    ready = deque([([5], [], []), ([5], [], [])])
    stopped, attempts = [], []
    source = SimpleNamespace(fileno=lambda: 5, read1=lambda _count: b"")
    sink = SimpleNamespace(fileno=lambda: 6, close=lambda: None)

    def read(_fd, _count):
        attempts.append(True)
        if len(attempts) == 1:
            raise BlockingIOError("Pipe is not ready")
        return b""

    monkeypatch.setattr(gate, "os", SimpleNamespace(
        set_blocking=lambda *_: None, read=read, write=lambda *_: 0, getpgrp=lambda: 23,
        killpg=lambda group, signal: stopped.append((group, signal)),
    ))
    monkeypatch.setattr(gate, "select", SimpleNamespace(select=lambda *_: ready.popleft()))
    monkeypatch.setattr(gate, "signal", SimpleNamespace(SIGKILL=9))
    gate._relay_posix(source, sink, threading.Event())
    assert len(attempts) == 2
    assert stopped == [(23, 9)]
