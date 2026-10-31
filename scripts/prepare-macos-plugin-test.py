"""Prepare the pinned source snapshot used by the macOS plugin test package."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def install():
    snapshot = json.loads((ROOT / ".test-build/snapshot-info.json").read_text(encoding="utf-8"))
    revision = snapshot["snapshotDigest"][:8]
    version = "2.2.0+plugins.20261031." + revision
    source = ROOT / ".test-build/pymss"
    project = source / "pyproject.toml"
    text = project.read_text(encoding="utf-8")
    text, count = re.subn(r'(?m)^version\s*=\s*"[^"]+"', 'version = "' + version + '"', text, count=1)
    if count != 1:
        raise ValueError("The source snapshot does not declare a package version")
    project.write_text(text, encoding="utf-8")
    wheels = ROOT / ".test-build/core-wheel"
    wheels.mkdir(exist_ok=True)
    subprocess.run(["python3", "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--wheel-dir", str(wheels), str(source)], check=True)
    built = list(wheels.glob("pymss-*.whl"))
    if len(built) != 1:
        raise ValueError("Expected one source snapshot wheel")
    python = ROOT / "python-runtime/bin/python3"
    import os

    environment = {**os.environ, "PYTHONHOME": str(ROOT / "python-runtime"), "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run([str(python), "-m", "pip", "install", "--no-deps", "--force-reinstall", str(built[0])], env=environment, check=True)
    manifest = ROOT / "python/runtime-manifest.json"
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["common"]["pymss"] = "pymss==" + version
    manifest.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    state = ROOT / "python-runtime/runtime-envs/active-runtime.json"
    value = json.loads(state.read_text(encoding="utf-8"))
    value["pymssVersion"] = version
    value["packageVersions"]["pymss"] = version
    state.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    info = {"purpose": "macos-plugin-test", "snapshotDigest": snapshot["snapshotDigest"], "pymssVersion": version,
            "bases": {name: row["base"] for name, row in snapshot["repositories"].items()}}
    (ROOT / "python/plugin-test-build.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    subprocess.run([str(python), "-c", "from pymss.operations.python_profiles import prepare_python_environment;from pymss.operations.rpc_process import register_rpc_processes;print('Snapshot process APIs available')"], env=environment, check=True)


def pin_bootstrap():
    snapshot = json.loads((ROOT / ".test-build/snapshot-info.json").read_text(encoding="utf-8"))
    for name, row in snapshot["repositories"].items():
        directory = ROOT if name == "studio" else ROOT / ".test-build" / name
        for relative, expected in row.get("overlay", row.get("files", {})).items():
            if hashlib.sha256((directory / relative).read_bytes()).hexdigest() != expected:
                raise ValueError("Source snapshot checksum mismatch: " + name + "/" + relative)
    manifest = ROOT / "python/runtime-manifest.json"
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["common"]["pymss"] = "pymss==2.2.0"
    value["common"]["pymss-core"] = "pymss-core==0.1.14"
    manifest.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def kit(destination: Path):
    destination.mkdir(parents=True, exist_ok=False)
    for path in (ROOT / '.test-build/mac-test-kit').iterdir():
        if path.is_file() and (path.suffix in {'.py', '.command'} or path.name == 'README.zh-CN.md'):
            shutil.copyfile(path, destination / path.name)
    for path in destination.glob("*.command"):
        path.chmod(0o755)
    for name in ("python_rpc_text", "rust_rpc_text"):
        shutil.copytree(ROOT / ".test-build/pymss-plugins/examples" / name, destination / "plugins" / name,
                        ignore=shutil.ignore_patterns(".cache", "__pycache__", "native", "scripts", ".gitignore"))
    shutil.copytree(ROOT / '.test-build/pymss-plugins/plugins/analysis', destination / 'plugins/analysis',
                    ignore=shutil.ignore_patterns('.cache', '__pycache__', 'node_modules', '.gitignore'))
    shutil.copyfile(ROOT / "python/plugin-test-build.json", destination / "build-info.json")
    checksums = []
    for path in sorted(destination.rglob('*')):
        if path.is_file():
            checksums.append(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.relative_to(destination).as_posix())
    (destination / 'SHA256SUMS.txt').write_text('\n'.join(checksums) + '\n', encoding='utf-8')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["pin-bootstrap", "install", "kit"])
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if args.action == "pin-bootstrap":
        pin_bootstrap()
    elif args.action == "install":
        install()
    else:
        if args.destination is None:
            parser.error("kit requires --destination")
        kit(args.destination)
