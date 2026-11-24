"""Exercise the isolated native host with the vendored VST3 fixture.

Build the fixture and host first, then run, for example::

    python tests/manual/vst_host_smoke.py --host HOST_EXE \
        --fixture-dll FIXTURE_DLL --ffmpeg FFMPEG_EXE --output-dir ARTIFACTS

Only Python's standard library is required. All generated bundles, audio, and
reports live in a new directory below --output-dir. No installed plugin,
application setting, or user project is changed. Audio is rendered offline;
--playback also opens the default output device with silent audio.

The gain fixture reports three latency samples without actually delaying its
audio. Gain comparisons therefore use interior samples. Its linear gain stages
cannot prove noncommutative chain ordering; actual PDC and nonlinear-order
coverage require an additional fixture or a real plugin. Its editor attaches a
native window but does not draw controls.
"""

from __future__ import annotations

import argparse
import base64
import copy
import ctypes
import json
import math
import os
from pathlib import Path
import queue
import shutil
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any
import wave


class Host:
    def __init__(self, executable: Path, *, env: dict[str, str] | None = None) -> None:
        self.process = subprocess.Popen(
            [str(executable), "--audio-host"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        self.responses: queue.Queue[dict[str, Any] | Exception] = queue.Queue()
        self.errors: list[str] = []
        self.sequence = 0
        self.project_id: str | None = None
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                self.responses.put(json.loads(line))
            except (ValueError, TypeError) as error:
                self.responses.put(RuntimeError(f"Host emitted invalid JSON: {line[:200]!r}: {error}"))
        self.responses.put(RuntimeError("Host closed its response stream"))

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.errors.append(line.rstrip())
            del self.errors[:-32]

    def request(self, operation: str, *, expect_error: bool = False, **arguments: Any) -> dict[str, Any]:
        self.sequence += 1
        message = {"id": self.sequence, "op": operation, **arguments}
        if self.project_id and operation not in {"load", "update", "shutdown"}:
            message.setdefault("projectId", self.project_id)
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.process.stdin.flush()
        try:
            response = self.responses.get(timeout=60)
        except queue.Empty as error:
            raise TimeoutError(f"Host did not answer {operation!r} within 60 seconds") from error
        if isinstance(response, Exception):
            raise RuntimeError(f"{response}; stderr: {'; '.join(self.errors)}") from response
        if response.get("id") != self.sequence:
            raise AssertionError(f"Unexpected response ID for {operation}: {response}")
        if expect_error:
            assert response.get("ok") is False, f"Expected {operation} to fail: {response}"
            return response
        assert response.get("ok") is True, f"{operation} failed: {response}"
        result = response["result"]
        if operation in {"load", "update"}:
            self.project_id = arguments["project"]["id"]
        if operation == "unload":
            self.project_id = None
        return result

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self.request("shutdown")
                self.process.wait(timeout=10)
            except Exception:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        if self.process.stdin is not None:
            self.process.stdin.close()


def read_float_wav(path: Path) -> tuple[int, list[tuple[float, float]]]:
    raw = path.read_bytes()
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE", "Render is not a RIFF WAV"
    position = 12
    fmt = None
    samples = None
    while position + 8 <= len(raw):
        kind, count = struct.unpack_from("<4sI", raw, position)
        start = position + 8
        if kind == b"fmt ":
            fmt = struct.unpack_from("<HHIIHH", raw, start)
            if fmt[0] == 0xFFFE:
                assert count >= 40, "WAVE_FORMAT_EXTENSIBLE lacks its subformat"
                subformat = struct.unpack_from("<I", raw, start + 24)[0]
                fmt = (subformat, *fmt[1:])
        elif kind == b"data":
            samples = raw[start:start + count]
        position = start + count + (count & 1)
    assert fmt is not None and samples is not None, "WAV lacks format or audio data"
    encoding, channels, rate, _, _, bits = fmt
    assert (encoding, channels, bits) == (3, 2, 32), f"Unexpected WAV format: {fmt}"
    frames = list(struct.iter_unpack("<ff", samples))
    assert all(math.isfinite(value) for frame in frames for value in frame), "Render contains invalid samples"
    return rate, frames


def visible_windows(process_id: int) -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    found = []

    @callback_type
    def collect(hwnd: int, _: int) -> bool:
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != process_id or not user32.IsWindowVisible(hwnd):
            return True
        name = ctypes.create_unicode_buffer(user32.GetWindowTextLengthW(hwnd) + 1)
        user32.GetWindowTextW(hwnd, name, len(name))
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        found.append({"handle": hwnd, "title": name.value, "width": rect.right - rect.left, "height": rect.bottom - rect.top})
        return True

    user32.EnumWindows(collect, 0)
    return found


def interior_values(frames: list[tuple[float, float]], rate: int) -> tuple[float, float]:
    assert len(frames) >= rate // 10, "Rendered file is unexpectedly short"
    sample = frames[rate // 10:rate // 3]
    return tuple(sum(frame[channel] for frame in sample) / len(sample) for channel in (0, 1))


def assert_interior(frames: list[tuple[float, float]], rate: int, expected: tuple[float, float]) -> None:
    observed = interior_values(frames, rate)
    for actual, target in zip(observed, expected):
        assert abs(actual - target) < 2e-6, f"Interior sample mismatch: observed={observed}, expected={expected}"
    for frame in frames[rate // 10:rate // 3]:
        assert max(abs(frame[channel] - expected[channel]) for channel in (0, 1)) < 2e-6


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", type=Path, default=root / "native/target/debug/pymss-audio-host.exe")
    fixture = parser.add_mutually_exclusive_group(required=True)
    fixture.add_argument("--fixture-dll", type=Path, help="Built vendored vst3_fixture.dll (Windows)")
    fixture.add_argument("--bundle", type=Path, help="Existing vendored Fixture.vst3 bundle")
    parser.add_argument("--ffmpeg", type=Path, default=Path(shutil.which("ffmpeg") or "ffmpeg"))
    parser.add_argument("--output-dir", type=Path, default=root / ".tmp/vst-host-smoke")
    parser.add_argument("--skip-editor", action="store_true", help="Skip native window checks in headless sessions")
    parser.add_argument("--playback", action="store_true", help="Also test CPAL transport using silent device output")
    args = parser.parse_args()
    host_path = args.host.resolve(strict=True)
    ffmpeg = args.ffmpeg.resolve(strict=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = args.output_dir.resolve() / f"{timestamp}-{os.getpid()}"
    run_dir.mkdir(parents=True, exist_ok=False)
    if args.fixture_dll:
        assert os.name == "nt", "--fixture-dll builds a Windows bundle; use --bundle on other platforms"
        bundle = run_dir / "Fixture.vst3"
        binary = bundle / "Contents/x86_64-win/Fixture.vst3"
        binary.parent.mkdir(parents=True)
        shutil.copy2(args.fixture_dll.resolve(strict=True), binary)
    else:
        bundle = args.bundle.resolve(strict=True)
    audio = run_dir / "source.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(struct.pack("<hh", 4096, -8192) * 24000)
    report: dict[str, Any] = {
        "host": str(host_path), "ffmpeg": str(ffmpeg), "bundle": str(bundle),
        "cases": [], "limitations": [
            "Fixture Gain reports 3 samples latency but does not delay audio; this does not validate actual PDC.",
            "Linear gain stages validate routing and bypass but cannot establish noncommutative processing order.",
            "The fixture editor attaches a native view without drawing controls; visual compatibility requires real plugins.",
        ],
    }
    host: Host | None = None
    render_sequence = 0

    def case(name: str, function: Any) -> None:
        started = time.monotonic()
        try:
            details = function()
            report["cases"].append({"name": name, "status": "passed", "details": details,
                                    "seconds": round(time.monotonic() - started, 3)})
            print(f"PASS {name}", flush=True)
        except Exception as error:
            report["cases"].append({"name": name, "status": "failed", "error": str(error),
                                    "seconds": round(time.monotonic() - started, 3)})
            print(f"FAIL {name}: {error}", flush=True)

    try:
        scanned = subprocess.run([str(host_path), "--plugin-scan", str(bundle)], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=30)
        assert scanned.returncode == 0, f"Plugin scan failed: {scanned.stderr}"
        plugins = json.loads(scanned.stdout)["plugins"]
        gain = next(plugin for plugin in plugins if plugin["name"] == "Fixture Gain")
        assert not gain["isInstrument"]
        assert any(plugin["isInstrument"] for plugin in plugins), "Fixture Synth was not classified as an instrument"
        report["scan"] = plugins
        print(f"PASS scan ({len(plugins)} plugin classes)", flush=True)
        host = Host(host_path)

        def slot(identifier: str) -> dict[str, Any]:
            return {"id": identifier, "format": "vst3", "pluginId": gain["id"], "name": gain["name"],
                    "path": str(bundle), "bypassed": False}

        def project(identifier: str = "smoke", *, plugins: bool = True) -> dict[str, Any]:
            return {
                "id": identifier, "name": "Audio Host Verification", "masterVolume": 1, "masterPan": 0,
                "sources": [{"id": "source", "path": str(audio), "name": "Audio", "duration": 0.5,
                             "sampleRate": 48000, "channels": 2}],
                "tracks": [{"id": "track", "sourceId": "source", "volume": 1, "pan": 0,
                            "muted": False, "solo": False, "pluginChain": [slot("track-gain")] if plugins else []}],
                "pluginChain": [],
            }

        def load(spec: dict[str, Any]) -> dict[str, Any]:
            assert host is not None
            return host.request("load", project=spec, ffmpeg=str(ffmpeg))

        def render(label: str) -> tuple[int, list[tuple[float, float]], dict[str, Any]]:
            nonlocal render_sequence
            assert host is not None
            render_sequence += 1
            path = run_dir / f"{render_sequence:02}-{label}.wav"
            result = host.request("render", path=str(path))
            rate, frames = read_float_wav(path)
            assert result["channels"] == 2 and result["sampleRate"] == rate
            assert abs(result["duration"] - len(frames) / rate) < 1 / rate
            return rate, frames, result

        def initial_gain() -> dict[str, Any]:
            load(project())
            rate, frames, result = render("initial-gain")
            observed = interior_values(frames, rate)
            report["initialGainObserved"] = [observed[0] / 0.125, observed[1] / -0.25]
            assert_interior(frames, rate, (0.0625, -0.125))
            return result

        case("default_component_gain", initial_gain)

        def parameter_and_restore() -> dict[str, Any]:
            spec = project("state-roundtrip")
            load(spec)
            host.request("param", slotId="track-gain", paramId="7", value=1.5)
            rate, frames, _ = render("gain-1.5")
            assert_interior(frames, rate, (0.1875, -0.375))
            snapshot = host.request("snapshot")
            saved = snapshot["states"]["track-gain"]
            assert base64.b64decode(saved, validate=True)
            spec["tracks"][0]["pluginChain"][0]["state"] = saved
            host.request("unload")
            load(spec)
            rate, frames, _ = render("restored-1.5")
            assert_interior(frames, rate, (0.1875, -0.375))
            return {"stateBytes": len(base64.b64decode(saved)), "gain": 1.5}

        case("plain_parameter_and_state_roundtrip", parameter_and_restore)

        def paused_parameter_snapshot() -> dict[str, Any]:
            spec = project("paused-state")
            load(spec)
            host.request("param", slotId="track-gain", paramId="7", value=1.25)
            snapshot = host.request("snapshot")
            saved = snapshot["states"]["track-gain"]
            component = base64.b64decode(saved, validate=True)
            assert component[:4] == b"SCV3" and struct.unpack_from("<I", component, 4)[0] == 8
            normalized_gain = struct.unpack_from("<d", component, 8)[0]
            assert abs(normalized_gain - 0.625) < 1e-12, (
                f"Paused component state omitted its pending parameter: {normalized_gain} != 0.625")
            spec["tracks"][0]["pluginChain"][0]["state"] = saved
            host.request("unload")
            load(spec)
            rate, frames, _ = render("paused-state-1.25")
            assert_interior(frames, rate, (0.15625, -0.3125))
            return {"gain": 1.25}

        case("parameter_saved_before_any_processing", paused_parameter_snapshot)

        def routing_and_bypass() -> dict[str, Any]:
            spec = project("routing")
            spec["tracks"][0].update(volume=0.8, pan=0.5)
            spec.update(masterVolume=0.6, masterPan=-0.25, pluginChain=[slot("master-gain")])
            load(spec)
            host.request("param", slotId="track-gain", paramId="7", value=0.5)
            host.request("param", slotId="master-gain", paramId="7", value=1.5)
            rate, frames, _ = render("track-master")
            assert_interior(frames, rate, (0.0225, -0.0675))
            spec["tracks"][0]["pluginChain"][0]["bypassed"] = True
            host.request("update", project=spec, ffmpeg=str(ffmpeg))
            rate, frames, _ = render("track-bypassed")
            assert_interior(frames, rate, (0.045, -0.135))
            spec["tracks"][0]["pluginChain"][0]["bypassed"] = False
            spec["pluginChain"][0]["bypassed"] = True
            host.request("update", project=spec, ffmpeg=str(ffmpeg))
            rate, frames, _ = render("master-bypassed")
            assert_interior(frames, rate, (0.015, -0.045))
            return {"activeProduct": 0.75, "trackBypassedProduct": 1.5, "masterBypassedProduct": 0.5}

        case("track_master_routing_and_bypass", routing_and_bypass)

        def edits_and_mixer() -> dict[str, Any]:
            spec = project("edits", plugins=False)
            clips = [
                {"id": "first", "assetId": "source", "start": 0.05, "offset": 0.1, "duration": 0.3,
                 "volume": 1.25, "fadeIn": 0.2, "fadeOut": 0.2, "muted": False, "locked": False},
                {"id": "second", "assetId": "source", "start": 0.2, "offset": 0.2, "duration": 0.1,
                 "volume": 0.5, "fadeIn": 0.1, "fadeOut": 0.1, "muted": False, "locked": False},
                {"id": "muted-clip", "assetId": "source", "start": 0, "offset": 0, "duration": 0.5,
                 "volume": 2, "fadeIn": 0, "fadeOut": 0, "muted": True, "locked": False},
            ]
            spec["tracks"][0].update(volume=0.8, pan=0.5, solo=True, clips=clips)
            excluded = copy.deepcopy(spec["tracks"][0])
            excluded.update(id="excluded-track", solo=False, clips=None)
            muted = copy.deepcopy(excluded)
            muted.update(id="muted-track", muted=True, solo=True)
            spec["tracks"].extend([excluded, muted])
            spec.update(masterVolume=0.7, masterPan=-0.25)
            load(spec)
            rate, frames, result = render("edits-fades-pan-solo-mute")
            assert len(frames) == round(0.5 * rate), "Muted/solo-excluded clips must retain project duration"
            for index, frame in enumerate(frames):
                clip_gain = 0.0
                for clip in clips:
                    begin = round(clip["start"] * rate)
                    duration = round(clip["duration"] * rate)
                    if clip["muted"] or not begin <= index < begin + duration:
                        continue
                    relative = index - begin
                    fade_in = min(1.0, relative / (clip["fadeIn"] * rate)) if clip["fadeIn"] else 1.0
                    fade_out = min(1.0, (duration - relative) / (clip["fadeOut"] * rate)) if clip["fadeOut"] else 1.0
                    clip_gain += clip["volume"] * fade_in * fade_out
                expected = (0.125 * clip_gain * 0.8 * 0.5 * 0.7,
                            -0.25 * clip_gain * 0.8 * 0.7 * 0.75)
                assert max(abs(frame[channel] - expected[channel]) for channel in (0, 1)) < 2e-6, (
                    f"Mixer differs at frame {index}: {frame} != {expected}")
            return result

        case("clip_overlap_fades_pan_solo_mute_and_duration", edits_and_mixer)

        def stale_project_id() -> dict[str, Any]:
            load(project("current-project", plugins=False))
            rejected = host.request("seek", projectId="old-project", position=0.25, expect_error=True)
            assert "changed" in rejected["error"].lower()
            status = host.request("status")
            assert status["projectId"] == "current-project" and status["position"] == 0
            return {"error": rejected["error"]}

        case("project_id_isolation", stale_project_id)

        def playback_transport() -> dict[str, Any]:
            spec = project("transport")
            spec["masterVolume"] = 0
            load(spec)

            def until(predicate: Any, description: str) -> dict[str, Any]:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    status = host.request("status")
                    assert status.get("error") is None, status
                    if predicate(status):
                        return status
                    time.sleep(0.01)
                raise AssertionError(f"Transport did not {description}: {status}")

            host.request("play", position=0, loop=True)
            until(lambda status: status["playing"] and status["position"] > 0.1, "advance during playback")
            paused = host.request("pause")
            time.sleep(0.1)
            status = host.request("status")
            assert not status["playing"] and status["position"] == paused["position"], (
                f"Paused cursor advanced: {paused} -> {status}")
            sought = host.request("seek", position=0.3)
            time.sleep(0.05)
            status = host.request("status")
            assert not status["playing"] and abs(status["position"] - 0.3) <= 1 / status["sampleRate"], (
                f"Paused seek was overwritten: {sought} -> {status}")
            host.request("play", position=0.45, loop=True)
            until(lambda status: status["playing"] and status["position"] < 0.2, "wrap to the loop start")
            spec["tracks"][0]["pan"] = 0.25
            host.request("update", project=spec, ffmpeg=str(ffmpeg))
            time.sleep(0.65)
            status = host.request("status")
            assert status["playing"] and status.get("error") is None, f"Mixer update lost looping: {status}"
            host.request("play", position=0.45, loop=False)
            ended = until(lambda current: not current["playing"], "finish non-looping playback")
            assert abs(ended["position"] - 0.5) <= 1 / ended["sampleRate"], ended
            return {"pausedPosition": paused["position"], "completedPosition": ended["position"],
                    "sampleRate": ended["sampleRate"], "underruns": ended["underruns"]}

        if args.playback:
            case("silent_cpal_play_pause_seek_loop_and_mix_update", playback_transport)
        else:
            report["cases"].append({"name": "silent_cpal_play_pause_seek_loop_and_mix_update", "status": "skipped",
                                    "reason": "Pass --playback to open the default audio device"})

        def invalid_inputs_are_recoverable() -> dict[str, Any]:
            failures = []
            invalid = project("bad-state")
            invalid["tracks"][0]["pluginChain"][0]["state"] = "not-base64"
            failures.append(host.request("load", project=invalid, ffmpeg=str(ffmpeg), expect_error=True)["error"])
            invalid = project("bad-format")
            invalid["tracks"][0]["pluginChain"][0]["format"] = "au"
            failures.append(host.request("load", project=invalid, ffmpeg=str(ffmpeg), expect_error=True)["error"])
            invalid = project("missing-audio")
            invalid["sources"][0]["missing"] = True
            failures.append(host.request("load", project=invalid, ffmpeg=str(ffmpeg), expect_error=True)["error"])
            load(project("recovered", plugins=False))
            assert host.request("status")["projectId"] == "recovered"
            return {"errors": failures}

        case("invalid_state_format_audio_and_recovery", invalid_inputs_are_recoverable)

        def editor_cycle() -> dict[str, Any]:
            load(project("editor"))
            captures = []
            for _ in range(2):
                opened = host.request("editor", slotId="track-gain", open=True)
                assert opened["open"] is True
                if os.name == "nt":
                    windows = visible_windows(host.process.pid)
                    assert any(window["title"] == "Fixture Gain" and window["width"] >= 320
                               and window["height"] >= 240 for window in windows), windows
                    captures.append(windows)
                closed = host.request("editor", slotId="track-gain", open=False)
                assert closed["open"] is False
                if os.name == "nt":
                    assert not any(window["title"] == "Fixture Gain" for window in visible_windows(host.process.pid))
            return {"visibleWindows": captures}

        if args.skip_editor:
            report["cases"].append({"name": "editor_open_close_reopen", "status": "skipped", "reason": "--skip-editor"})
        else:
            case("editor_open_close_reopen", editor_cycle)

            def edit_open_chain() -> dict[str, Any]:
                spec = project("edit-open-chain")
                chain = spec["tracks"][0]["pluginChain"]
                chain.append(slot("second-gain"))
                session_id = load(spec)["sessionId"]
                handles = {}
                for effect in chain:
                    identifier = effect["id"]
                    old = set(handles.values())
                    assert host.request("editor", slotId=identifier, open=True)["open"]
                    if os.name == "nt":
                        new = {
                            window["handle"] for window in visible_windows(host.process.pid)
                            if window["title"] == "Fixture Gain"
                        } - old
                        assert len(new) == 1, new
                        handles[identifier] = new.pop()

                def update(open_ids: set[str]) -> None:
                    status = host.request("update", project=spec, ffmpeg=str(ffmpeg))
                    assert status["sessionId"] == session_id, status
                    assert set(status["openEditors"]) == open_ids, status
                    if os.name == "nt":
                        actual = {
                            window["handle"] for window in visible_windows(host.process.pid)
                            if window["title"] == "Fixture Gain"
                        }
                        assert actual == {handles[identifier] for identifier in open_ids}, actual

                chain.append(slot("third-gain"))
                update({"track-gain", "second-gain"})
                chain.reverse()
                update({"track-gain", "second-gain"})
                chain.pop(1)
                update({"track-gain"})
                return {"survivingNativeWindow": os.name == "nt"}

            case("chain_edits_keep_surviving_native_editors", edit_open_chain)
    except Exception as error:
        report["fatalError"] = str(error)
        print(f"FAIL setup: {error}", flush=True)
    finally:
        if host is not None:
            host.close()
            report["hostExitCode"] = host.process.returncode
            if host.errors:
                report["hostStderr"] = host.errors
        failed = [case for case in report["cases"] if case["status"] == "failed"]
        report["passed"] = sum(case["status"] == "passed" for case in report["cases"])
        report["failed"] = len(failed) + int("fatalError" in report)
        if report.get("hostExitCode", 0) != 0:
            report["failed"] += 1
        report_path = run_dir / "report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{report['passed']} passed; {report['failed']} failed; report: {report_path}", flush=True)
    return int(report["failed"] != 0)


if __name__ == "__main__":
    sys.exit(main())
