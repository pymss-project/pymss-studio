"""Install and exercise an external reference package in an isolated Studio store."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import wave
from unittest.mock import patch

import numpy as np

from studio_plugin_smoke import ROOT, capture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--studio-root", required=True, type=Path)
    parser.add_argument("--plugin-root", type=Path, default=ROOT / "examples/audio_gain")
    parser.add_argument("--artifacts", type=Path, default=ROOT / "tooling/.tmp/reference-install")
    args = parser.parse_args()
    worker = args.studio_root.resolve() / "python"
    if not (worker / "worker_plugins.py").is_file():
        parser.error("--studio-root must contain the Studio Python worker")
    sys.path.insert(0, str(worker))
    import pymss.operations
    import pymss.plugins
    from pymss.audio_io import load_audio
    import worker_operations
    import worker_plugins

    def reset():
        pymss.operations._DEFAULT_RUNTIME = None
        pymss.plugins.reset()

    passed = []
    with tempfile.TemporaryDirectory(prefix="reference-plugin-") as temporary:
        work = Path(temporary)
        source = work / "source/audio_gain"
        shutil.copytree(args.plugin_root.resolve(), source, ignore=shutil.ignore_patterns("node_modules", "__pycache__", ".git", ".tmp"))
        (source / "node_modules").mkdir()
        cache = source / "node_modules/local-only.js"
        cache.write_text("development dependency")
        (source / ".env").write_text("LOCAL_SETTING=development")
        store = work / "plugins"
        environment = {
            "PYMSS_PLUGINS_DIR": str(work / "manual"),
            "PYMSS_STUDIO_RUNTIME_ENVS_DIR": str(work / "runtimes"),
            "PYMSS_STUDIO_ACTIVE_RUNTIME_FILE": str(work / "active.json"),
            "PYMSS_STUDIO_LOG_TRANSPORT": "protocol",
            "PYTHONPATH": os.pathsep.join([str(worker), os.environ.get("PYTHONPATH", "")]),
        }
        (work / "active.json").write_text('{"backend":"cpu"}')
        audio = work / "source.wav"
        with wave.open(str(audio), "wb") as stream:
            stream.setnchannels(2)
            stream.setsampwidth(2)
            stream.setframerate(48000)
            stream.writeframes(b"".join(struct.pack("<hh", round(3000 * math.sin(2 * math.pi * 440 * i / 48000)), round(1500 * math.sin(2 * math.pi * 440 * i / 48000))) for i in range(4800)))
        original = hashlib.sha256(audio.read_bytes()).hexdigest()

        def dependencies(plugin, *_args):
            worker_plugins._validate_requirements(plugin)

        with patch.dict(os.environ, environment), patch.object(worker_plugins, "_root", return_value=store), patch.object(worker_plugins, "_install_dependencies", side_effect=dependencies):
            def manage(action, **options):
                reset()
                return capture(worker_plugins.cmd_manage_plugin, {"action": action, "taskId": "reference-install", **options})[-1]["payload"]

            installed = manage("install", target=str(source))
            row = next(item for item in installed["installed"] if item["name"] == "audio_gain")
            assert row["loaded"] and row["toolCategory"] == "edit", row
            assert row["locales"]["zh-CN"]["title"] == "音频增益"
            entry = Path(row["contributions"]["views"][0]["entryPath"])
            assert entry.is_file() and store in entry.parents
            assert not (store / "installed/audio_gain/node_modules").exists()
            assert not (store / "installed/audio_gain/.env").exists()
            passed.append("external-package-install-and-tool-discovery-without-development-files")

            def run():
                return capture(worker_operations.cmd_run_operation, {
                    "operation": "example.audio_gain.apply", "requestId": "reference-run",
                    "inputs": {"audio": {"kind": "file", "path": str(audio)}},
                    "params": {"gain_db": 6, "protect_peak": True}, "outputDir": str(work / "output"),
                })[-1]["payload"]

            result = run()
            output = Path(result["outputs"]["audio"]["path"])
            before, sr = load_audio(str(audio), sr=None, mono=False)
            after, output_sr = load_audio(str(output), sr=None, mono=False)
            assert sr == output_sr == 48000
            assert np.asarray(before).shape == np.asarray(after).shape
            np.testing.assert_allclose(after, np.asarray(before) * 10 ** (6 / 20), atol=0.0002)
            assert hashlib.sha256(audio.read_bytes()).hexdigest() == original
            passed.append("real-wav-processing-preserves-input-rate-and-channels")
            second = Path(run()["outputs"]["audio"]["path"])
            assert output != second and output.is_file() and second.is_file()
            passed.append("repeated-output-does-not-overwrite-existing-files")

            cache.write_text("changed dependency cache")
            assert manage("check_updates")["updates"]["audio_gain"]["up_to_date"]
            old_revision = row["contributions"]["views"][0]["revision"]
            project = source / "pyproject.toml"
            project.write_text(project.read_text().replace('version = "0.1.0"', 'version = "0.1.1"', 1))
            updated = manage("update", name="audio_gain")
            row = next(item for item in updated["installed"] if item["name"] == "audio_gain")
            assert row["version"] == "0.1.1" and row["loaded"]
            assert row["contributions"]["views"][0]["revision"] != old_revision
            passed.append("source-update-and-view-revision-without-cache-noise")
            assert not manage("disable", name="audio_gain")["installed"][0]["enabled"]
            assert manage("enable", name="audio_gain")["installed"][0]["loaded"]
            assert not manage("uninstall", name="audio_gain")["installed"]
            assert not entry.exists()
            reset()
            assert "example.audio_gain.apply" not in {item["id"] for item in worker_plugins.operation_runtime().list_operations()}
            passed.append("disable-enable-and-uninstall-without-a-builtin-fallback")
    args.artifacts.mkdir(parents=True, exist_ok=True)
    (args.artifacts / "results.json").write_text(json.dumps({"passed": passed, "dependencies": "validated existing NumPy; no pip or model download"}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Reference install smoke passed: {len(passed)} checks")


if __name__ == "__main__":
    main()
