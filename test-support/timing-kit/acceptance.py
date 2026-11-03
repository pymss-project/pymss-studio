"""Acceptance tools for one immutable macOS Timing test delivery."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

KIT = Path(__file__).resolve().parent
BUILD = json.loads((KIT / "build-info.json").read_text(encoding="utf-8"))
ROOT = Path.home() / f".pymss-studio-timing-test-{BUILD['buildId']}"
GUI = ROOT / "gui"
REPORTS = ROOT / "reports"


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resources(app):
    return next(root for root in (app / "Contents/Resources", app / "Contents/Resources/resources")
                if (root / "python/worker.py").is_file())


def identity(app):
    with (app / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    assert info["CFBundleIdentifier"] == BUILD["identifier"], "Wrong test application identifier"
    assert info["CFBundleShortVersionString"] == BUILD["version"], "Wrong test application version"
    receipt = json.loads((KIT / "build-receipt.json").read_text(encoding="utf-8"))
    assert digest(app / "Contents/MacOS" / info["CFBundleExecutable"]) == receipt["executableSha256"], "Application executable does not match the CI receipt"
    assert json.loads((resources(app) / "python/timing-test-build.json").read_text(encoding="utf-8")) == BUILD, "Application and kit source snapshots differ"
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True, timeout=120)
    return {"buildId": BUILD["buildId"], "version": BUILD["version"], "application": str(app),
            "macOS": platform.mac_ver()[0], "machine": platform.machine(), "signature": "PASS",
            "bundledPackages": {name: importlib.metadata.version(name) for name in ("pymss", "pymss-core", "torch", "mlx")}}


def environment(app):
    result = dict(os.environ)
    for key in ("PYMSS_PLUGIN_BINDING", "PYMSS_PLUGINS_DIR", "PYMSS_STUDIO_PYTHON", "PYMSS_STUDIO_WORKER", "_PYMSS_CLI_CORE", "PYTHONPATH",
                "PYMSS_STUDIO_RUNTIME_ENVS_DIR", "PYMSS_STUDIO_ACTIVE_RUNTIME_FILE", "PYMSS_STUDIO_BUNDLED_RUNTIME_ENVS_DIR",
                "PYMSS_STUDIO_BOOTSTRAP_PYTHON", "PYMSS_STUDIO_ALLOW_DEBUG_RUNTIME_OVERRIDE"):
        result.pop(key, None)
    runtime = resources(app) / "python-runtime"
    result.update(PYTHONHOME=str(runtime), PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1",
                  PYMSS_STUDIO_DATA_ROOT=str(GUI), PYMSS_USER_MODELS=str(GUI / "user-models.json"),
                  PYMSS_MODEL_DIR=str(GUI / "models"), PYMSS_STUDIO_DEFAULT_OUTPUT_DIR=str(GUI / "outputs"),
                  PYMSS_PLUGIN_ENVIRONMENTS_DIR=str(ROOT / "cli-environments"), PYMSS_SCRATCH_DIR=str(ROOT / "scratch"),
                  NUMBA_CACHE_DIR=str(ROOT / "numba"), RADIUS_FFMPEG_IO="1",
                  RADIUS_FFMPEG=str(resources(app) / "bin/ffmpeg"))
    result["PATH"] = str(resources(app) / "bin") + os.pathsep + result.get("PATH", "/usr/bin:/bin")
    return result


def report_name(action):
    REPORTS.mkdir(parents=True, exist_ok=True)
    return REPORTS / f"{action}-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}.json"


def verify(app):
    result = identity(app)
    assert result["machine"] == "arm64", "This delivery requires Apple Silicon"
    assert result["bundledPackages"]["pymss"] == BUILD["runtimeBaseline"]
    child_environment = environment(app)
    os.environ.clear()
    os.environ.update(child_environment)
    # macOS exposes /var through /private/var; fixtures use one canonical spelling.
    tempfile.tempdir = str(Path(tempfile.gettempdir()).resolve())
    os.environ["TMPDIR"] = tempfile.tempdir
    # Load the exact published plugin tests, with all scratch files in their own temporary directories.
    sys.path.insert(0, str(KIT))
    suite = unittest.defaultTestLoader.loadTestsFromName("tooling.tests.test_timing_file_operation")
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        tests = unittest.TextTestRunner(stream=captured, verbosity=2).run(suite)
    print(captured.getvalue())
    result["tests"] = {"run": tests.testsRun, "failures": len(tests.failures), "errors": len(tests.errors),
                       "skipped": tests.skipped, "log": captured.getvalue()}
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True, timeout=120)
    result["signatureAfterTests"] = "PASS"
    result["status"] = "PASS" if tests.wasSuccessful() and not tests.skipped else "FAIL"
    path = report_name("automatic")
    write_json(path, result)
    print(f"Automatic report: {path}")
    if result["status"] != "PASS":
        raise RuntimeError("Automatic checks failed or skipped a required native engine check")


def case_definitions():
    base = ROOT / "outputs"
    return [
        ("default", "原始录音 48k.wav", 100, GUI / "outputs"),
        ("radius-50", "原始录音 48k.wav", 50, base / "radius-50"),
        ("radius-200", "原始录音 48k.wav", 200, base / "radius-200"),
        ("rubberband-50", "原始录音 48k.wav", 50, base / "rubberband-50"),
        ("rubberband-200", "原始录音 48k.wav", 200, base / "rubberband-200"),
        ("radius-16k", "采样率16k.wav", 100, base / "radius-16k"),
        ("radius-96k", "采样率96k.wav", 100, base / "radius-96k"),
        ("repeat", "原始录音 48k.wav", 100, base / "repeat"),
    ]


def prepare(app):
    identity(app)
    import numpy as np
    import soundfile as sf

    manifest_path = ROOT / "fixtures.json"
    if manifest_path.exists():
        saved = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in saved["inputs"]:
            assert digest(item["path"]) == item["sha256"], f"Previously generated input changed: {item['path']}"
        print(f"Existing fixtures retained. Paths: {ROOT / 'GUI-PATHS.txt'}")
        return
    ROOT.mkdir(parents=True, exist_ok=True)
    fixtures = ROOT / "fixtures"
    fixtures.mkdir(exist_ok=True)
    inputs = []
    for name, rate, seconds in [("原始录音 48k.wav", 48000, 12), ("采样率16k.wav", 16000, 6),
                                ("采样率96k.wav", 96000, 6), ("取消测试 180s.wav", 48000, 180)]:
        path = fixtures / name
        assert not path.exists(), f"Existing file will not be overwritten: {path}"
        t = np.arange(rate) / rate
        samples = np.stack([0.15 * np.sin(2 * np.pi * 440 * t), 0.12 * np.sin(2 * np.pi * 660 * t)], axis=1)
        with sf.SoundFile(path, "w", samplerate=rate, channels=2, subtype="PCM_16") as stream:
            for _ in range(seconds):
                stream.write(samples)
        inputs.append({"path": str(path), "name": name, "sampleRate": rate, "channels": 2,
                       "frames": rate * seconds, "sha256": digest(path)})
    cases = [{"id": name, "input": str(fixtures / filename), "durationPercent": percent,
              "outputDirectory": str(directory)} for name, filename, percent, directory in case_definitions()]
    for item in cases:
        Path(item["outputDirectory"]).mkdir(parents=True, exist_ok=True)
    (ROOT / "outputs/cancel-retry").mkdir(parents=True, exist_ok=True)
    sentinel = ROOT / "outputs/repeat/原始录音 48k_processed.wav"
    assert not sentinel.exists(), "The repeat-test sentinel already exists; no files were replaced"
    sf.write(sentinel, np.ones((48000, 2)) * 0.02, 48000, subtype="PCM_16")
    write_json(manifest_path, {"buildId": BUILD["buildId"], "inputs": inputs, "cases": cases,
                               "sentinel": {"path": str(sentinel), "sha256": digest(sentinel)}})
    write_json(ROOT / "GUI-PATHS.json", {"pluginSource": str(KIT / "plugins/timing"), "cases": cases,
                                         "cancelInput": str(fixtures / "取消测试 180s.wav"),
                                         "cancelOutput": str(ROOT / "outputs/cancel-retry")})
    lines = [f"Build: {BUILD['buildId']}", f"Plugin local source: {KIT / 'plugins/timing'}", ""]
    for item in cases:
        lines.extend([item["id"], f"  Input: {item['input']}", f"  Output directory: {item['outputDirectory']}",
                      f"  Duration: {item['durationPercent']}%", ""])
    lines.extend([f"Cancellation input: {fixtures / '取消测试 180s.wav'}", f"Cancellation/retry directory: {ROOT / 'outputs/cancel-retry'}"])
    (ROOT / "GUI-PATHS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    manual = ROOT / "MANUAL-RESULTS.md"
    if not manual.exists():
        manual.write_bytes((KIT / "CHECKLIST.zh-CN.md").read_bytes())
    (ROOT / "screenshots").mkdir(exist_ok=True)
    print(f"Fixtures ready: {ROOT / 'GUI-PATHS.txt'}\nFill in: {manual}")


def launch(app):
    identity(app)
    assert (ROOT / "fixtures.json").is_file(), "Run Prepare Test Fixtures.command first"
    with (app / "Contents/Info.plist").open("rb") as stream:
        executable = plistlib.load(stream)["CFBundleExecutable"]
    env = environment(app)
    env.pop("PYTHONHOME", None)
    env["PIP_NO_INDEX"] = "1"
    print(f"Test data: {GUI}\nPIP_NO_INDEX=1 is intentional for the GUI core-update regression.", flush=True)
    os.execve(str(app / "Contents/MacOS" / executable), [executable], env)


def check(app):
    result = identity(app)
    import numpy as np
    import soundfile as sf

    manifest = json.loads((ROOT / "fixtures.json").read_text(encoding="utf-8"))
    checks = []

    def record(name, success, detail):
        checks.append({"name": name, "status": "PASS" if success else "FAIL", "detail": detail})
        print(checks[-1]["status"], name, detail)

    by_path = {item["path"]: item for item in manifest["inputs"]}
    for item in manifest["inputs"]:
        record("input unchanged", Path(item["path"]).is_file() and digest(item["path"]) == item["sha256"], item["path"])
    sentinel = manifest["sentinel"]
    record("old output preserved", Path(sentinel["path"]).is_file() and digest(sentinel["path"]) == sentinel["sha256"], sentinel["path"])
    for case in manifest["cases"]:
        source = by_path[case["input"]]
        valid = []
        invalid = []
        for path in sorted(Path(case["outputDirectory"]).glob("*.wav")):
            if str(path) == sentinel["path"]:
                continue
            try:
                info = sf.info(path)
                # Read in bounded blocks even when a tester supplies longer replacement audio.
                finite, peak = True, 0.0
                for samples in sf.blocks(path, blocksize=65536, always_2d=True):
                    finite = finite and bool(np.isfinite(samples).all())
                    peak = max(peak, float(np.max(np.abs(samples))))
                expected = source["frames"] * case["durationPercent"] / 100
                okay = (info.samplerate == source["sampleRate"] and info.channels == 2
                        and abs(info.frames - expected) <= 1 and finite and peak > 0.0001
                        and path.stem.startswith(Path(case["input"]).stem + "_processed"))
                (valid if okay else invalid).append({"path": str(path), "frames": info.frames, "sampleRate": info.samplerate,
                                                    "channels": info.channels, "peak": peak})
            except Exception as error:
                invalid.append({"path": str(path), "error": str(error)})
        required = 2 if case["id"] == "repeat" else 1
        record(case["id"], len(valid) >= required and not invalid, {"valid": valid, "invalid": invalid, "required": required})
    partials = [str(path) for directory in (ROOT / "outputs", GUI / "outputs") if directory.exists()
                for path in directory.rglob(".pymss-audio-*")]
    record("no unpublished temporary audio", not partials, partials)
    state_file = GUI / "runtime-envs/active-runtime.json"
    if state_file.is_file():
        state = json.loads(state_file.read_text(encoding="utf-8"))
        result["activeRuntime"] = state
        packages = state.get("packageVersions", {})
        record("GUI core update", state.get("pymssVersion") == BUILD["runtimeUpdateTarget"]
               and state.get("pymssCoreVersion") == "0.1.14"
               and str(state.get("torchVersion", "")).split("+")[0] == "2.7.1"
               and state.get("backend") == "mlx" and bool(state.get("packages", {}).get("mlx")),
               {"versions": packages, "pymss": state.get("pymssVersion"), "core": state.get("pymssCoreVersion"),
                "torch": state.get("torchVersion"), "backend": state.get("backend")})
    else:
        record("GUI core update", False, "No user runtime state; complete the GUI online update first")
    result["checks"] = checks
    result["manualResults"] = str(ROOT / "MANUAL-RESULTS.md")
    result["manualStatus"] = "REQUIRES_TESTER_CONFIRMATION"
    result["status"] = "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL"
    report = report_name("gui-results")
    write_json(report, result)
    archive_path = ROOT / f"Timing-Test-Evidence-{BUILD['buildId']}-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for directory in (REPORTS, ROOT / "screenshots"):
            for path in sorted(directory.rglob("*")):
                if path.is_file():
                    archive.write(path, str(path.relative_to(ROOT)))
        for name in ("MANUAL-RESULTS.md", "GUI-PATHS.txt", "GUI-PATHS.json", "fixtures.json"):
            path = ROOT / name
            if path.is_file():
                archive.write(path, name)
        for path in sorted(GUI.rglob("*.log")):
            if any(part in {"site-packages", "models", "plugins"} for part in path.relative_to(GUI).parts):
                continue
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 2 * 1024 * 1024))
                archive.writestr("logs/" + str(path.relative_to(GUI)) + ".tail.txt", stream.read())
        for name in ("build-info.json", "build-receipt.json"):
            archive.write(KIT / name, name)
    print(f"GUI report: {report}\nEvidence: {archive_path}\nManual GUI checks must be recorded separately.")
    if result["status"] != "PASS":
        raise RuntimeError("One or more required GUI outputs/update checks did not pass; see the report")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("verify", "prepare", "open", "check"))
    parser.add_argument("--app", type=Path, required=True)
    args = parser.parse_args()
    {"verify": verify, "prepare": prepare, "open": launch, "check": check}[args.action](args.app)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"FAILED: {error}", file=sys.stderr)
        raise SystemExit(1)
