"""Exercise every bundled tool's admission and local file workflows in isolation."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import math
import os
import struct
import sys
import tempfile
import types
import wave
from pathlib import Path
from unittest.mock import patch

from studio_plugin_smoke import ROOT, capture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--studio-root", required=True, type=Path)
    parser.add_argument(
        "--artifacts", type=Path, default=ROOT / "tooling/.tmp/preinstalled-tools"
    )
    args = parser.parse_args()
    worker = args.studio_root.resolve() / "python"
    sys.path.insert(0, str(worker))
    import pymss.operations
    import pymss.plugins
    import worker_operations
    import worker_plugins
    import worker_tools

    def reset():
        pymss.operations._DEFAULT_RUNTIME = None
        pymss.plugins.reset()

    passed = []
    with tempfile.TemporaryDirectory(prefix="preinstalled-tools-") as temporary:
        work = Path(temporary)
        root = work / "plugins"
        spec = importlib.util.spec_from_file_location("prepare_plugins", args.studio_root / "scripts/prepare-preinstalled-plugins.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        config = {"schemaVersion": 1, "repository": {"url": "https://github.com/TheSmallHanCat/pymss-plugins", "ref": "main"},
                  "preinstalled": ["convert", "inspect", "sdr", "slicer", "merge", "loudnorm", "asr", "game", "tempo_key"]}
        files, packages = builder.collect_packages(ROOT, config)
        files[Path("manifest.json")] = json.dumps({"schemaVersion": 1, "plugins": packages}).encode("utf-8")
        bundles = work / "bundles"
        builder.publish(bundles, files)
        env = {
            "PYMSS_PLUGINS_DIR": str(work / "manual"),
            "PYMSS_STUDIO_RUNTIME_ENVS_DIR": str(work / "runtimes"),
            "PYMSS_STUDIO_ACTIVE_RUNTIME_FILE": str(work / "active.json"),
            "PYMSS_STUDIO_LOG_TRANSPORT": "protocol",
            "PYTHONPATH": os.pathsep.join(
                [str(worker), os.environ.get("PYTHONPATH", "")]
            ),
        }
        (work / "active.json").write_text('{"backend":"cpu"}', encoding="utf-8")
        input_dir = work / "input"
        input_dir.mkdir()
        source = input_dir / "source.wav"
        with wave.open(str(source), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(44100)
            stream.writeframes(
                b"".join(
                    struct.pack(
                        "<h", round(4000 * math.sin(2 * math.pi * 440 * i / 44100))
                    )
                    for i in range(44100)
                )
            )
        with (
            patch.dict(os.environ, env),
            patch.object(worker_plugins, "_root", return_value=root),
            patch.object(worker_plugins, "_BUNDLED_ROOT", bundles),
            # Keep the test-only bundle metadata in this process; imports are still
            # verified by the real isolated report probe.
            patch.object(worker_plugins, "_marketplace_inventory", side_effect=lambda directory, **kwargs: worker_plugins._inventory(directory)),
        ):

            def manage(action, name=None):
                reset()
                return capture(
                    worker_plugins.cmd_manage_plugin,
                    {"action": action, "name": name, "taskId": "tool_lifecycle"},
                )[-1]["payload"]

            def row(snapshot, name):
                return next(
                    item for item in snapshot["installed"] if item["name"] == name
                )

            inventory = capture(
                worker_plugins.cmd_plugin_marketplace, {"includeCatalog": False}
            )[-1]["payload"]
            assert len(inventory["preinstalledPending"]) == 9, inventory
            snapshot = manage("prepare_preinstalled")
            names = {item["name"] for item in snapshot["installed"]}
            assert len(names) == 9, snapshot
            assert all(
                item["preinstalled"]
                and item["enabled"]
                and item["loaded"]
                and item["pluginId"]
                for item in snapshot["installed"]
            ), snapshot
            passed.append("nine-bundled-plugins-discovered-offline")

            def run(name, payload):
                reset()
                operation = row(snapshot, name)["contributions"]["views"][0][
                    "operation"
                ]
                output = work / ("output-" + name)
                payload = {**payload, "outputDir": str(output)}
                events = capture(
                    worker_operations.cmd_run_operation,
                    {
                        "operation": operation,
                        "inputs": {"request": payload},
                        "params": {},
                        "outputDir": str(output),
                        "requestId": name + "-run",
                    },
                )
                result = next(
                    event["payload"]
                    for event in events
                    if event["type"] == "operation_result"
                )["outputs"]["report"]["value"]
                assert any(event["type"] == "operation_progress" for event in events), (
                    events
                )
                assert all(event["requestId"] == name + "-run" for event in events), (
                    events
                )
                return result

            convert_events = capture(
                worker_tools.cmd_audio_tools,
                {
                    "operation": "convert",
                    "inputs": [str(source)],
                    "outputDir": str(work / "convert"),
                    "sampleRate": 48000,
                    "channels": 2,
                    "wavBitDepth": "PCM-16",
                    "requestId": "convert-run",
                },
            )
            converted = convert_events[-1]["payload"]["outputPaths"][0]
            with wave.open(converted, "rb") as stream:
                assert stream.getframerate() == 48000 and stream.getnchannels() == 2
            passed.append("legacy-command-routes-to-real-converter-plugin")
            report = run("inspect", {"operation": "inspect", "inputPath": str(source)})
            assert report["audioStreams"][0]["sampleRate"] == 44100 and report["raw"]
            passed.append("real-inspect-retains-full-ffprobe-metadata")
            report = run(
                "sdr",
                {
                    "operation": "sdr",
                    "referencePath": str(source),
                    "estimatedPath": str(source),
                },
            )
            assert report["averageSdr"] > 50
            passed.append("real-sdr-file-analysis")
            report = run(
                "slicer",
                {
                    "operation": "slicer",
                    "inputs": [str(source)],
                    "minLength": 100,
                    "minInterval": 100,
                    "hopSize": 10,
                    "maxSilKept": 50,
                },
            )
            assert report["segments"] and all(
                Path(path).is_file() for path in report["outputPaths"]
            )
            passed.append("real-slicer-exports-segments")
            report = run(
                "merge",
                {
                    "operation": "merge",
                    "inputDir": str(input_dir),
                    "sortBy": "name",
                    "sortDirection": "asc",
                },
            )
            assert report["merged"] == 1 and Path(report["outputPath"]).is_file()
            passed.append("real-merge-preserves-ordered-output")
            report = run(
                "loudnorm",
                {
                    "operation": "loudness",
                    "inputs": [str(source)],
                    "measurement": "peak",
                    "target": -12,
                    "useLimiter": False,
                },
            )
            assert report["succeeded"] == 1 and report["measurements"]
            passed.append("real-loudness-measurement-and-output")

            class AutoModel:
                def __init__(self, **kwargs):
                    pass

                def generate(self, **kwargs):
                    return [
                        {
                            "text": "Transcribed text",
                            "sentence_info": [
                                {"start": 0, "end": 500, "text": "Transcribed text"}
                            ],
                        }
                    ]

            with patch.dict(
                sys.modules, {"funasr": types.SimpleNamespace(AutoModel=AutoModel)}
            ):
                report = run(
                    "asr",
                    {
                        "operation": "asr",
                        "inputPath": str(source),
                        "outputFormats": ["txt", "json", "srt"],
                    },
                )
            assert (
                report["text"] == "Transcribed text" and len(report["outputPaths"]) == 3
            )
            passed.append("packaged-asr-fake-backend-produces-all-transcripts")
            reset()
            worker_plugins.operation_runtime()
            package = next(
                name
                for name, module in sys.modules.items()
                if getattr(module, "__file__", None)
                and Path(module.__file__).resolve()
                == (root / "installed/game/__init__.py").resolve()
            )
            inference = importlib.import_module(package + ".backend.engine.infer")
            model = work / "game.pt"
            model.touch()
            output = work / "output-game"
            output.mkdir()
            midi = output / "source.mid"
            midi.write_bytes(b"MThd")
            result = inference.MidiInferenceResult(midi, 3, 1.0, 0.0, 0.5, ())
            # Keep this worker's runtime so the fake belongs to its actual installed module.
            snapshot_run = worker_plugins.operation_runtime()
            module = snapshot_run._operations["pymss.game.tool"].run.__module__.split(
                ".backend."
            )[0]
            inference = importlib.import_module(module + ".backend.engine.infer")
            with (
                patch.object(inference, "infer", return_value=result),
                patch.object(worker_operations, "_runtime", return_value=snapshot_run),
            ):
                events = capture(
                    worker_operations.cmd_run_operation,
                    {
                        "operation": "pymss.game.tool",
                        "inputs": {
                            "request": {
                                "operation": "midi",
                                "inputPath": str(source),
                                "modelPath": str(model),
                                "outputDir": str(output),
                                "bpm": 120,
                            }
                        },
                        "params": {},
                        "outputDir": str(output),
                        "requestId": "midi-run",
                    },
                )
            assert events[-1]["payload"]["outputs"]["report"]["value"]["noteCount"] == 3
            passed.append("packaged-game-resolves-its-own-inference-modules")
            # Native analysis intentionally has a view/service permission, without a Python placeholder.
            tempo = row(snapshot, "tempo_key")
            assert (
                "audio.analyze" in tempo["contributions"]["views"][0]["permissions"]
                and not tempo["operations"]
            )
            passed.append("native-analysis-plugin-admission")
            snapshot = manage("disable", "convert")
            assert (
                not row(snapshot, "convert")["enabled"]
                and not row(snapshot, "convert")["loaded"]
            )
            try:
                worker_tools._plugin_operation("convert")

            except ValueError:
                pass
            else:
                raise AssertionError(
                    "Disabled converter still available through old command"
                )
            snapshot = manage("enable", "convert")
            assert row(snapshot, "convert")["loaded"]
            snapshot = manage("uninstall", "convert")
            assert "convert" not in {item["name"] for item in snapshot["installed"]}
            snapshot = manage("prepare_preinstalled")
            assert "convert" not in {item["name"] for item in snapshot["installed"]}
            snapshot = manage("install_preinstalled", "convert")
            assert row(snapshot, "convert")["loaded"]
            passed.append("disable-remove-and-explicit-reinstall-honor-user-choice")
    args.artifacts.mkdir(parents=True, exist_ok=True)
    (args.artifacts / "results.json").write_text(
        json.dumps({"passed": passed}, indent=2), encoding="utf-8"
    )
    print(f"Preinstalled tools smoke passed: {len(passed)} checks")


if __name__ == "__main__":
    main()
