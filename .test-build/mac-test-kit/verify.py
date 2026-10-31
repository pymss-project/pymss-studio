"""Verify the bundled plugin runtime in a newly created private test directory."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import zipfile


def dependency_plugin(root: Path, identifier: str, version: str):
    root.mkdir(parents=True, exist_ok=False)
    wheel = root / f"pymss_isolation_probe-{version}-py3-none-any.whl"
    info = f"pymss_isolation_probe-{version}.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("pymss_isolation_probe/__init__.py", f"VERSION = {version!r}\n")
        archive.writestr(info + "/METADATA", f"Metadata-Version: 2.1\nName: pymss-isolation-probe\nVersion: {version}\n")
        archive.writestr(info + "/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(info + "/RECORD", "")
    (root / "python-lock.json").write_text(json.dumps({"formatVersion": 1, "requiresPython": ">=3.11", "targets": {"any": [
        {"path": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}]}}), encoding="utf-8")
    (root / "__init__.py").write_text("from pathlib import Path\nfrom pymss.plugins.kit import register_commands\ndef setup(plugin): register_commands(plugin, Path(__file__).parent)\n", encoding="utf-8")
    (root / "peer.py").write_text("import sys,importlib.util,json\nfrom pathlib import Path\nimport pymss_isolation_probe\nPath(sys.argv[2]).write_text(json.dumps({'version':pymss_isolation_probe.VERSION,'hostVisible':importlib.util.find_spec('pymss') is not None}),encoding='utf-8')\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(f'''[project]
name = "{identifier.replace('.', '-')}"
version = "1.0.0"
[tool.pymss.plugin]
id = "{identifier}"
api_version = 2
[[tool.pymss.plugin.operations]]
id = "{identifier}.run"
[tool.pymss.python]
format_version = 1
lock = "python-lock.json"
[tool.pymss.commands]
format_version = 1
[[tool.pymss.commands.operations]]
id = "{identifier}.run"
python = "peer.py"
arguments = [{{input="source"}},{{output="document"}}]
[tool.pymss.commands.operations.inputs.source]
type = "file"
[tool.pymss.commands.operations.outputs.document]
type = "file"
filename = "version.json"
media_type = "application/json"
''', encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--kit", type=Path, default=Path(__file__).parent)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = args.report.resolve()
    report.mkdir(parents=True, exist_ok=True)
    work = report / "workspace"
    work.mkdir(exist_ok=False)
    resource = args.app.resolve() / "Contents/Resources"
    if not (resource / "python/worker.py").is_file():
        resource /= "resources"
    python = resource / "python-runtime/bin/python3"
    worker = resource / "python/worker.py"
    if not python.is_file() or not worker.is_file():
        raise RuntimeError("The test application does not contain its Python runtime")
    metadata = json.loads((resource / "python/plugin-test-build.json").read_text(encoding="utf-8"))
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("PYTHON", "PYMSS_", "PIP_"))}
    environment.update({"PYTHONHOME": str(resource / "python-runtime"), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
        "PYMSS_STUDIO_DATA_ROOT": str(work / "data"), "PYMSS_STUDIO_RUNTIME_ENVS_DIR": str(work / "runtimes"),
        "PYMSS_STUDIO_ACTIVE_RUNTIME_FILE": str(work / "active.json"), "PYMSS_PLUGINS_DIR": str(work / "manual-plugins"),
        "PYMSS_USER_MODELS": str(work / "user-models.json"), "PYMSS_MODEL_DIR": str(work / "models"),
        "PYMSS_SCRATCH_DIR": str(work / "scratch"), "PYMSS_STUDIO_DEFAULT_OUTPUT_DIR": str(work / "outputs"), "PIP_NO_INDEX": "1"})
    environment["PATH"] = str(resource / "bin") + os.pathsep + environment.get("PATH", "")
    (work / "scratch").mkdir()
    (work / "active.json").write_text('{"backend":"cpu"}', encoding="utf-8")
    source = work / "中文 input.txt"
    source.write_bytes("第一行\r\nsecond\rthird\n".encode())
    expected = "第一行\nsecond\nthird\n".encode()
    results = []
    sequence = 0

    def call(command, payload, *, fail=False):
        nonlocal sequence
        sequence += 1
        request = work / f"request-{sequence}.json"
        request.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        started = time.monotonic()
        child = subprocess.run([str(python), "-X", "utf8", str(worker), command, "--payload", str(request)],
            env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        events = [json.loads(line) for line in child.stdout.splitlines() if line.startswith('{"type"')]
        if not events or ((child.returncode != 0) != fail):
            raise RuntimeError(f"{command} failed: " + (child.stderr or child.stdout)[-4000:])
        errors = [event.get("payload", {}) for event in events if event.get("type") == "error"]
        if errors and not fail:
            raise RuntimeError(f"{command}: {errors[-1].get('message', 'Worker error')}")
        results.append({"command": command, "seconds": round(time.monotonic() - started, 2), "expectedFailure": fail,
                        "events": [event["type"] for event in events]})
        print("PASS", command, "(expected rejection)" if fail else "", flush=True)
        return events

    def run(operation, *, params=None, fail=False):
        return call("run_operation", {"operation": operation, "inputs": {"source": {"kind": "file", "path": str(source)}},
            "params": params or {}, "outputDir": str(work / "outputs"), "requestId": f"mac-check-{sequence}"}, fail=fail)

    def output(events):
        value = next(event["payload"] for event in events if event["type"] == "operation_result")
        return Path(value["outputs"]["document"]["path"])

    def signature():
        child = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(args.app.resolve())],
                               capture_output=True, text=True, timeout=90)
        if child.returncode:
            raise RuntimeError("The application signature is invalid: " + child.stderr[-2000:])
        results.append({"command": "codesign_verify", "status": "passed"})
        print("PASS application signature", flush=True)

    status = "failed"
    failure = None
    try:
        signature()
        probe = subprocess.run([str(python), "-c", "from importlib.metadata import version;from pymss.operations.python_profiles import prepare_python_environment;print(version('pymss'))"],
                               env=environment, capture_output=True, text=True, timeout=30)
        if probe.returncode or probe.stdout.strip() != metadata["pymssVersion"]:
            raise RuntimeError("The application does not contain the expected source snapshot")
        call("env_info", {})
        defaults = call("manage_plugin", {"action": "prepare_preinstalled", "defaultPolicy": "auto"})[-1]["payload"]
        names = {row["name"] for row in defaults["installed"] if row.get("loaded")}
        if not {"pymss.convert", "pymss.merge", "pymss.slicer", "pymss.inspect"} <= names:
            raise RuntimeError("The four bundled tools were not prepared")
        for name in ("python_rpc_text", "rust_rpc_text"):
            package = args.kit.resolve() / "plugins" / name
            call("manage_plugin", {"action": "install", "target": str(package), "defaultPolicy": "review"})
            operation = "example." + name + ".normalize"
            first = output(run(operation, params={"line_ending": "lf"}))
            if first.read_bytes() != expected:
                raise RuntimeError("The text output differs from the expected LF content")
            second = output(run(operation, params={"line_ending": "crlf"}))
            if second == first or first.read_bytes() != expected or second.read_bytes() != expected.replace(b"\n", b"\r\n"):
                raise RuntimeError("Repeated output did not preserve the existing result")
            source.write_bytes(b"\xff")
            run(operation, params={"line_ending": "lf"}, fail=True)
            source.write_bytes("第一行\r\nsecond\rthird\n".encode())
            if output(run(operation, params={"line_ending": "lf"})).read_bytes() != expected:
                raise RuntimeError("The operation did not recover after invalid UTF-8 input")
        for identifier, version in (("validation.python_a", "1.0.0"), ("validation.python_b", "2.0.0")):
            package = work / identifier
            dependency_plugin(package, identifier, version)
            call("manage_plugin", {"action": "install", "target": str(package), "defaultPolicy": "review"})
            value = json.loads(output(run(identifier + ".run")).read_text(encoding="utf-8"))
            if value != {"version": version, "hostVisible": False}:
                raise RuntimeError("The isolated Python dependency or import boundary is incorrect")
        call("manage_plugin", {"action": "uninstall", "name": "validation.python_a", "defaultPolicy": "review"})
        if json.loads(output(run("validation.python_b.run")).read_text(encoding="utf-8"))["version"] != "2.0.0":
            raise RuntimeError("Removing one plugin changed its peer's dependencies")
        signature()
        status = "passed"
    except Exception as error:
        failure = str(error)
        print("FAIL", failure, file=sys.stderr, flush=True)
    finally:
        value = {"status": status, "failure": failure, "machine": platform.machine(), "macOS": platform.mac_ver()[0],
                 "python": sys.version.split()[0], "build": metadata, "checks": results}
        (report / "summary.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("Report:", report, flush=True)
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
