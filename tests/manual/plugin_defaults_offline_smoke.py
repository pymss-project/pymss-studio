"""Prepare bundled tools without downloads and run them against local WAV files."""
from __future__ import annotations

from contextlib import redirect_stdout
import argparse
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest import mock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    work = args.output_dir or Path(tempfile.mkdtemp(prefix="studio-default-tools-offline-"))
    if work.exists() and any(work.iterdir()):
        raise ValueError("The output directory must be empty")
    work.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))
    os.environ.update({
        "PYMSS_STUDIO_RUNTIME_ENVS_DIR": str(work / "runtimes"),
        "PYMSS_STUDIO_ACTIVE_RUNTIME_FILE": str(work / "active.json"),
        "PYMSS_PLUGINS_DIR": str(work / "unmanaged"),
        "PIP_NO_INDEX": "1", "HF_HUB_OFFLINE": "1",
    })
    (work / "active.json").write_text('{"backend":"cpu"}', encoding="utf-8")
    import numpy as np
    import soundfile as sf
    import worker_operations
    import worker_plugins as plugins

    def command(handler, payload):
        output = io.StringIO()
        with redirect_stdout(output):
            code = handler(payload)
        events = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith("{")]
        if code or not events:
            raise AssertionError(events or output.getvalue())
        return events[-1]["payload"]

    root = plugins._root()
    defaults = ["pymss.convert", "pymss.merge", "pymss.slicer", "pymss.inspect"]
    assert plugins._inventory(root, default_policy="auto")["preinstalledPending"] == defaults
    with mock.patch.object(plugins, "_install_dependencies", side_effect=AssertionError("Dependency download is forbidden")), \
            mock.patch.object(plugins, "_acquire_source", side_effect=AssertionError("Source download is forbidden")):
        snapshot = command(plugins.cmd_manage_plugin, {"action": "prepare_preinstalled"})
    assert sorted(row["name"] for row in snapshot["installed"] if row["loaded"]) == sorted(defaults)
    inputs = work / "inputs"
    inputs.mkdir(exist_ok=True)
    signal = (0.1 * np.sin(2 * np.pi * 440 * np.arange(16000) / 16000)).astype(np.float32)
    first, second = inputs / "01.wav", inputs / "02.wav"
    sf.write(first, signal, 16000)
    sf.write(second, signal, 16000)
    cases = [
        ("pymss.inspect.tool", {"request": {"inputPath": str(first)}}, {}),
        ("pymss.convert.batch", {"files": {"paths": [str(first)]}}, {"output_dir": str(work / "converted")}),
        ("pymss.merge.tool", {"request": {"inputDir": str(inputs), "outputDir": str(work / "merged")}}, {}),
        ("pymss.slicer.tool", {"request": {"inputs": [str(first)], "outputDir": str(work / "sliced"),
          "minLength": 300, "minInterval": 100, "hopSize": 10, "maxSilKept": 100}}, {}),
    ]
    results = {}
    for operation, values, params in cases:
        result = command(worker_operations.cmd_run_operation, {
            "operation": operation, "inputs": values, "params": params, "outputDir": str(work / "results"),
        })
        report = result["outputs"]["report"]["value"]
        if operation == "pymss.inspect.tool":
            assert report["audioStreams"][0]["sampleRate"] == 16000, report
        else:
            paths = report.get("outputPaths") or [report.get("outputPath")]
            assert paths and all(path and Path(path).is_file() for path in paths), report
            assert all(sf.info(path).frames > 0 for path in paths)
        results[operation] = "passed"
    (work / "summary.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(f"Four bundled tools prepared without source/dependency downloads; local WAV operations passed: {work}")


if __name__ == "__main__":
    main()
