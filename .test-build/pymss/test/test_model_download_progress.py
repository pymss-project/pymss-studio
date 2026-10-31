from __future__ import annotations

import hashlib
import http.server
import os
import subprocess
import threading
import time

import pytest

from pymss import model_download


def test_parse_aria2_progress_line():
    progress = model_download._parse_aria2_progress("[#abcd12 4.0MiB/10MiB(40%) CN:1 DL:512KiB]")

    assert progress == (4 * 1024 * 1024, 10 * 1024 * 1024, "512KiB")


@pytest.mark.parametrize("line", [
    "[#abcd12 1MiB/2MiB(50%) CN:1 DL:512KiB]",
    "[#abcd12 1MiB/2MiB(50%)CN:1 DL:512KiB]",
])
def test_parse_aria2_progress_preserves_legacy_percent_separator(line):
    assert model_download._parse_aria2_progress(line) == (1024 * 1024, 2 * 1024 * 1024, "512KiB")


def test_parse_aria2_progress_ignores_unusable_lines():
    assert model_download._parse_aria2_progress("*** Download Progress Summary ***") is None


@pytest.mark.parametrize("line", [
    "[#abcd12 4.0MiB/0B CN:1 DL:512KiB]",
    "[#abcd12 4.0MiB/0B(0%) CN:1 DL:512KiB]",
])
def test_parse_aria2_progress_preserves_bytes_when_the_total_is_unknown(line):
    assert model_download._parse_aria2_progress(line) == (4 * 1024 * 1024, 0, "512KiB")


@pytest.mark.parametrize("expected_size", [6, None])
def test_aria2_download_uses_expected_size_for_unknown_response_length(monkeypatch, tmp_path, expected_size):
    class FakeProcess:
        stdout = ["[#abcd12 3B/0B CN:1 DL:2B]\n"]

        def __init__(self, cmd, **kwargs):
            assert "--show-console-readout=true" in cmd
            assert "--quiet=false" in cmd
            (tmp_path / cmd[cmd.index("--out") + 1]).write_bytes(b"abcdef")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def wait(self):
            return 0

    monkeypatch.setattr(model_download.subprocess, "Popen", FakeProcess)
    events = []
    model_download._download_file_aria2(
        "https://example.test/model.pth", tmp_path / "model.pth.part", tmp_path / "model.pth",
        expected_size=expected_size, progress_callback=lambda *args: events.append(args),
    )
    assert events == [
        (0, expected_size or 0, "Downloading model.pth"),
        (3, expected_size or 0, "Downloading model.pth (2B/s)"),
        (6, expected_size or 6, "Downloaded model.pth"),
    ]


@pytest.mark.parametrize("unknown_length", [False, True])
def test_aria2_reports_real_pipe_progress_with_quiet_config_or_unknown_length(monkeypatch, tmp_path, unknown_length):
    executable = os.environ.get("PYMSS_TEST_ARIA2") or model_download.ARIA2C_PATH
    if not executable:
        pytest.skip("aria2c is unavailable; set PYMSS_TEST_ARIA2 to run the pipe regression")
    data = b"x" * (256 * 1024)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_response(200)
            if not unknown_length:
                self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                for offset in range(0, len(data), 8192):
                    self.wfile.write(data[offset:offset + 8192])
                    self.wfile.flush()
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = tmp_path / "aria2.conf"
    config.write_text(f"quiet={'false' if unknown_length else 'true'}\n", encoding="utf-8")
    popen = subprocess.Popen

    def spawn(command, **kwargs):
        # Use a private config rather than the developer's aria2.conf.
        return popen([command[0], f"--conf-path={config}", *command[1:]], **kwargs)

    monkeypatch.setattr(model_download, "ARIA2C_PATH", executable)
    monkeypatch.setattr(model_download.subprocess, "Popen", spawn)
    events = []
    destination = tmp_path / "model.pth"
    try:
        model_download._download_file(
            f"http://127.0.0.1:{server.server_port}/model.pth", destination,
            expected_size=len(data), expected_sha256=hashlib.sha256(data).hexdigest(),
            timeout=10, retries=0, proxy="", progress_callback=lambda *args: events.append(args),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert destination.read_bytes() == data
    assert any(0 < done < total for done, total, _message in events)
    assert events[-1][:2] == (len(data), len(data))


def test_urllib_download_emits_byte_progress(monkeypatch, tmp_path):
    class FakeResponse:
        headers = {"content-length": "6"}

        def __init__(self):
            self.chunks = iter([b"abc", b"def", b""])

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return next(self.chunks)

    monkeypatch.setattr(model_download.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse())
    events = []
    dest = tmp_path / "model.pth"

    model_download._download_file_urllib(
        "https://example.test/model.pth",
        tmp_path / "model.pth.part",
        dest,
        timeout=1,
        proxy="",
        progress_callback=lambda *args: events.append(args),
    )

    assert dest.read_bytes() == b"abcdef"
    assert events == [
        (0, 6, "Downloading model.pth"),
        (3, 6, "Downloading model.pth"),
        (6, 6, "Downloading model.pth"),
        (6, 6, "Downloaded model.pth"),
    ]


def test_aria2_download_captures_output_and_emits_progress(monkeypatch, tmp_path):
    captured = {}

    class FakeProcess:
        stdout = ["[#abcd12 4.0MiB/10MiB(40%) CN:1 DL:512KiB]\n"]
        returncode = 0

        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            output_path = tmp_path / cmd[cmd.index("--out") + 1]
            output_path.write_bytes(b"abcdef")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def wait(self):
            return self.returncode

    monkeypatch.setattr(model_download.subprocess, "Popen", FakeProcess)
    events = []
    dest = tmp_path / "model.pth"

    model_download._download_file_aria2(
        "https://example.test/model.pth",
        tmp_path / "model.pth.part",
        dest,
        expected_size=6,
        timeout=1,
        progress_callback=lambda *args: events.append(args),
    )

    assert dest.read_bytes() == b"abcdef"
    assert captured["kwargs"]["stdout"] == subprocess.PIPE
    assert captured["kwargs"]["stderr"] == subprocess.STDOUT
    assert events == [
        (0, 6, "Downloading model.pth"),
        (4 * 1024 * 1024, 10 * 1024 * 1024, "Downloading model.pth (512KiB/s)"),
        (6, 6, "Downloaded model.pth"),
    ]


def test_a_failing_callback_does_not_retry_the_download(monkeypatch, tmp_path):
    attempts = []

    class FakeResponse:
        headers = {"content-length": "6"}

        def __init__(self):
            self.chunks = iter([b"abc", b"def", b""])

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return next(self.chunks)

    def fake_urlopen(*_args, **_kwargs):
        attempts.append(1)
        return FakeResponse()

    monkeypatch.setattr(model_download.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(model_download, "ARIA2C_PATH", None)

    calls = []

    def faulty_callback(*_args):
        calls.append(1)
        # OSError is what the retry loop watches for, so this is the dangerous shape.
        raise OSError("callback could not write its log")

    dest = tmp_path / "model.pth"
    model_download._download_file(
        "https://example.test/model.pth", dest, timeout=1, progress_callback=faulty_callback, proxy=""
    )

    assert dest.read_bytes() == b"abcdef"
    assert len(attempts) == 1, "a broken callback must not cause the file to be fetched again"
    assert calls, "the callback should still have been attempted"


def test_an_interrupted_read_kills_aria2(monkeypatch, tmp_path):
    killed = []

    class InterruptedProcess:
        def __init__(self, _cmd, **_kwargs):
            self.stdout = self._lines()

        def _lines(self):
            yield "[#abcd12 1.0MiB/10MiB(10%) CN:1 DL:512KiB]\n"
            raise KeyboardInterrupt("simulated Ctrl+C")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def wait(self):
            return 0

        def kill(self):
            killed.append(True)

    monkeypatch.setattr(model_download.subprocess, "Popen", InterruptedProcess)

    with pytest.raises(KeyboardInterrupt):
        model_download._download_file_aria2(
            "https://example.test/model.pth",
            tmp_path / "model.pth.part",
            tmp_path / "model.pth",
            timeout=1,
        )

    # aria2 ignores a closed stdout, so without an explicit kill it outlives the interrupt and
    # keeps writing to the file.
    assert killed, "aria2c must be killed when the read loop is interrupted"


def test_aria2_failure_reports_captured_output(monkeypatch, tmp_path):
    class FailingProcess:
        stdout = ["errorCode=22 The response status is not successful.\n"]

        def __init__(self, _cmd, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def wait(self):
            return 22

    monkeypatch.setattr(model_download.subprocess, "Popen", FailingProcess)

    with pytest.raises(model_download.DownloadError) as excinfo:
        model_download._download_file_aria2(
            "https://example.test/model.pth",
            tmp_path / "model.pth.part",
            tmp_path / "model.pth",
            timeout=1,
        )

    # Capturing aria2's output is only worth it if the diagnosis survives into the exception.
    assert "exit code 22" in str(excinfo.value)
    assert "errorCode=22" in str(excinfo.value)
