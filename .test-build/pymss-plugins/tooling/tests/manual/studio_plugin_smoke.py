"""Install, run, and uninstall a plugin through an isolated Studio worker."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np


ROOT = Path(__file__).resolve().parents[3]


def capture(command, payload):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = command(payload)
    events = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
    assert code == 0, events
    return events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--studio-root", required=True, type=Path)
    parser.add_argument("--artifacts", type=Path, default=ROOT / "tooling/.tmp/studio-smoke")
    args = parser.parse_args()
    worker_directory = args.studio_root.resolve() / "python"
    if not (worker_directory / "worker_plugins.py").is_file():
        parser.error("--studio-root must contain python/worker_plugins.py")
    sys.path.insert(0, str(worker_directory))
    import worker_operations
    import worker_plugins

    args.artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="plugin-tools-") as temporary:
        work = Path(temporary)
        plugin_root = work / "plugins"
        environment = {
            "PYMSS_PLUGINS_DIR": str(work / "manual"),
            "PYMSS_STUDIO_RUNTIME_ENVS_DIR": str(work / "runtimes"),
            "PYMSS_STUDIO_ACTIVE_RUNTIME_FILE": str(work / "active.json"),
            "PYMSS_STUDIO_LOG_TRANSPORT": "protocol",
            "PYTHONPATH": os.pathsep.join([str(worker_directory), os.environ.get("PYTHONPATH", "")]),
        }
        (work / "active.json").write_text('{"backend":"cpu"}', encoding="utf-8")

        def reuse_existing_dependencies(plugin, root, task_id, payload):
            # Check installed requirements instead of invoking pip or downloading packages.
            worker_plugins._validate_requirements(plugin)

        with patch.dict(os.environ, environment), patch.object(worker_plugins, "_root", return_value=plugin_root), patch.object(worker_plugins, "_install_dependencies", side_effect=reuse_existing_dependencies):
            install = capture(worker_plugins.cmd_manage_plugin, {
                "action": "install", "target": str(ROOT / "plugins/loudnorm"), "taskId": "tool_install",
            })
            inventory = next(event["payload"] for event in install if event["type"] == "plugin_manage_result")
            row = next(plugin for plugin in inventory["installed"] if plugin["name"] == "loudnorm")
            assert row["loaded"] and row["managed"], row
            entry = Path(row["contributions"]["views"][0]["entryPath"])
            assert entry.is_file() and plugin_root in entry.parents, entry

            source = work / "source.wav"
            sr = 44100
            t = np.arange(sr, dtype=np.float32) / sr
            audio = (0.1 * np.sin(2 * np.pi * 440 * t) * 32767).astype("<i2")
            with wave.open(str(source), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(sr)
                stream.writeframes(audio.tobytes())
            run = capture(worker_operations.cmd_run_operation, {
                "operation": "pymss.loudnorm.normalize", "inputs": {"audio": {"kind": "file", "path": str(source)}},
                "params": {"target_lufs": -18.0}, "outputDir": str(work / "output"), "requestId": "tool_run",
            })
            result = next(event["payload"] for event in run if event["type"] == "operation_result")
            output = Path(result["outputs"]["audio"]["path"])
            assert output.is_file() and output.stat().st_size > 0, result
            removed = capture(worker_plugins.cmd_manage_plugin, {"action": "uninstall", "name": "loudnorm", "taskId": "tool_remove"})
            after = next(event["payload"] for event in removed if event["type"] == "plugin_manage_result")
            assert not after["installed"], after
            assert not entry.exists()
            report = {"install": "passed", "contributedView": "passed", "audioExecution": "passed", "uninstall": "passed", "dependencyInstaller": "reused installed requirements; no pip invocation"}
    (args.artifacts / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("Studio plugin smoke passed: install, contributed view, audio execution, uninstall")


if __name__ == "__main__":
    main()
