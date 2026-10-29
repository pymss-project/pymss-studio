"""End-to-end Studio worker smoke for plugin protocol v2.

    <python with pymss>=2.2> -X utf8 tests/manual/plugin_v2_worker_smoke.py <studio repo> <plugins repo> <work dir>

Runs the real worker.py commands as subprocesses (the way Rust spawns them) against an
isolated runtime directory and the local pymss-plugins checkout as the plugin source.
The work dir is deleted and recreated. See docs/plugin-v2-handoff.md.
"""
import json
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

studio, plugins_repo, work = (Path(value) for value in sys.argv[1:4])
shutil.rmtree(work, ignore_errors=True)
work.mkdir(parents=True)
(work / "active.json").write_text('{"backend":"cpu"}')
environment = {
    **{key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYMSS_PLUGINS_REGISTRY"}},
    "PYMSS_STUDIO_RUNTIME_ENVS_DIR": str(work / "runtimes"),
    "PYMSS_STUDIO_ACTIVE_RUNTIME_FILE": str(work / "active.json"),
    "PYMSS_PLUGINS_DIR": str(work / "pymss-dir"),
    "PYMSS_STUDIO_PLUGINS_SOURCE": str(plugins_repo),
    "PYTHONIOENCODING": "utf-8",
}
results = []


def worker(command, payload):
    process = subprocess.run(
        [sys.executable, str(studio / "python/worker.py"), command, "--payload", json.dumps(payload)],
        capture_output=True, text=True, encoding="utf-8", env=environment, timeout=900,
    )
    events = [json.loads(line) for line in process.stdout.splitlines() if line.startswith("{")]
    return process.returncode, events, process.stderr


def check(name, condition, detail=""):
    results.append((name, bool(condition)))
    print(("PASS " if condition else "FAIL ") + name + ("" if condition else f"  -> {str(detail)[-1500:]}"), flush=True)


def manage(action, **payload):
    code, events, stderr = worker("manage_plugin", {"action": action, "taskId": f"plugin_manage_{action}", **payload})
    final = events[-1] if events else {}
    return code, final, stderr


def row(snapshot, name):
    return next((item for item in snapshot["installed"] if item["name"] == name), None)


# 1. catalog comes from the local registry, keyed by plugin id, without importing plugins
code, events, stderr = worker("plugin_marketplace", {})
snapshot = events[-1]["payload"] if events else {}
names = {item["name"] for item in snapshot.get("catalog", [])}
check("catalog lists plugin ids from local registry", code == 0 and {"pymss.loudnorm", "pymss.convert", "pymss.inspect"} <= names, snapshot.get("catalogError") or stderr)

# 2. install official plugins from the catalog and the example from a local folder
for plugin in ("pymss.convert", "pymss.inspect", "pymss.analysis"):
    code, final, stderr = manage("install", name=plugin, installDependencies=False)
    check(f"install {plugin} from local catalog", code == 0 and final.get("type") == "plugin_manage_result", final.get("payload") or stderr)
code, final, stderr = manage("install", target=str(plugins_repo / "examples/audio_gain"))
check("install example from a local folder", code == 0, final.get("payload") or stderr)

# 3. listing: plugin-id directories, operations from manifests, views from [tool.pymss.studio]
code, events, stderr = worker("plugin_marketplace", {"includeCatalog": False})
snapshot = events[-1]["payload"]
root = Path(snapshot["pluginsDir"])
installed = {item["name"]: item for item in snapshot["installed"] if item["managed"]}
check("install directories are plugin ids", sorted(path.name for path in root.iterdir()) == sorted(installed), sorted(installed))
gain = installed.get("example.audio_gain", {})
check("example row is loaded with declared operations", gain.get("loaded") and gain.get("operations") == ["example.audio_gain.apply"], gain)
view = (gain.get("contributions") or {}).get("views", [{}])[0]
check("example view resolves inside the plugin", view.get("pluginId") == "example.audio_gain" and Path(view.get("entryPath", "")).is_file() and root in Path(view["entryPath"]).parents, view)
check("analysis lists four declared operations", installed.get("pymss.analysis", {}).get("operations", []) and len(installed["pymss.analysis"]["operations"]) == 4, installed.get("pymss.analysis"))

# 4. forced verification imports plugins in a child and keeps them loaded
code, events, stderr = worker("plugin_marketplace", {"includeCatalog": False, "forceVerify": True})
verified = events[-1]["payload"]
check("forced verification succeeds", code == 0 and not verified.get("verificationError") and all(item["loaded"] for item in verified["installed"] if item["managed"]), verified.get("verificationError") or [(i["name"], i.get("error")) for i in verified["installed"]])

# 5. list_operations through the worker runtime
code, events, stderr = worker("list_operations", {})
operations = {item["id"] for item in events[-1]["payload"]["operations"]} if events else set()
check("worker runtime exposes plugin operations", {"example.audio_gain.apply", "pymss.convert.batch", "pymss.inspect.tool"} <= operations, stderr[-1500:])

# 6. run operations end to end
audio = work / "input.wav"
with wave.open(str(audio), "wb") as stream:
    stream.setnchannels(2)
    stream.setsampwidth(2)
    stream.setframerate(44100)
    stream.writeframes(b"\x00\x10\x00\x10" * 44100)
code, events, stderr = worker("run_operation", {
    "operation": "example.audio_gain.apply", "requestId": "gain-1", "outputDir": str(work / "out"),
    "inputs": {"audio": {"kind": "file", "path": str(audio)}}, "params": {"gain_db": -6},
})
result = events[-1] if events else {}
output = ((result.get("payload") or {}).get("outputs") or {}).get("audio", {})
check("run example.audio_gain.apply writes audio", code == 0 and result.get("type") == "operation_result" and Path(output.get("path", "")).is_file(), result or stderr)
progress = [event for event in events if event["type"] == "operation_progress"]
check("plain progress events carry fraction", progress and all("fraction" in event["payload"] for event in progress), events[:3])

code, events, stderr = worker("run_operation", {
    "operation": "pymss.inspect.tool", "requestId": "inspect-1", "outputDir": str(work / "out"),
    "inputs": {"request": {"operation": "inspect", "inputPath": str(audio)}}, "params": {},
})
structured = [event["payload"].get("event") for event in events if event["type"] == "operation_progress" and event["payload"].get("event")]
result = events[-1] if events else {}
check("run pymss.inspect.tool (kit.register_tool) succeeds", code == 0 and result.get("type") == "operation_result", result or stderr)
check("file tool progress arrives as structured event", structured and structured[0].get("type") == "audio_tool_progress", [e["payload"] for e in events if e["type"] == "operation_progress"][:3])

code, events, stderr = worker("run_operation", {
    "operation": "pymss.convert.batch", "requestId": "convert-1", "outputDir": str(work / "converted"),
    "inputs": {"files": {"paths": [str(audio)]}}, "params": {"output_dir": str(work / "converted"), "format": "flac"},
})
result = events[-1] if events else {}
check("run pymss.convert.batch succeeds", code == 0 and result.get("type") == "operation_result" and list((work / "converted").glob("*.flac")), result or stderr)

# 7. disable / enable / update / uninstall
code, final, stderr = manage("disable", name="example.audio_gain")
check("disable keeps the row but stops loading", code == 0 and row(final["payload"], "example.audio_gain")["enabled"] is False, final)
code, events, stderr = worker("run_operation", {"operation": "example.audio_gain.apply", "requestId": "gain-2", "outputDir": str(work / "out"),
                                               "inputs": {"audio": {"kind": "file", "path": str(audio)}}, "params": {}})
check("disabled plugin operation is unavailable", code != 0, events[-1] if events else stderr)
code, final, stderr = manage("enable", name="example.audio_gain")
check("enable restores the plugin", code == 0 and row(final["payload"], "example.audio_gain")["loaded"], final)
code, final, stderr = manage("update", name="pymss.inspect")
check("update from the local catalog succeeds", code == 0, final.get("payload") or stderr)
code, final, stderr = manage("check_updates")
check("check_updates reports every installation", code == 0 and set(final["payload"]["updates"]) == set(installed), final.get("payload", {}).get("updates"))
code, final, stderr = manage("uninstall", name="pymss.analysis")
check("uninstall removes the plugin id directory", code == 0 and not (root / "pymss.analysis").exists(), final)

# 8. a v1 installation is moved aside and reported
legacy = root / "oldtool"
legacy.mkdir()
(legacy / "__init__.py").write_text("def setup(plugin):\n    pass\n")
(legacy / "pyproject.toml").write_text('[tool.pymss.plugin]\nid="pymss.oldtool"\napi_version=1\n')
code, final, stderr = manage("enable", name="example.audio_gain")
check("v1 installation moves to legacy-v1 and is reported", final.get("payload", {}).get("legacyPlugins") == ["oldtool"] and (root.parent / "legacy-v1/oldtool").is_dir(), final.get("payload", {}).get("legacyPlugins"))

# 9. installing a v1 source is rejected
old = work / "old-source"
old.mkdir()
(old / "__init__.py").write_text("def setup(plugin):\n    pass\n")
(old / "pyproject.toml").write_text('[tool.pymss.plugin]\nid="author.old"\napi_version=1\n')
code, final, stderr = manage("install", target=str(old))
check("v1 source is rejected", code != 0 and "api_version" in json.dumps(final.get("payload")), final.get("payload"))

failed = [name for name, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
