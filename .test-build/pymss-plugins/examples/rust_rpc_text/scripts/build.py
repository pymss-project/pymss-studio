"""Explicit developer build; plugin installation never runs Cargo."""
from __future__ import annotations

import argparse
from pathlib import Path
import platform
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    cargo = shutil.which("cargo")
    if cargo is None:
        raise SystemExit("Cargo is required to build the native plugin during development")
    root = Path(__file__).resolve().parents[1]
    build_root = root / ".cache/native-target"
    machine = platform.machine().lower()
    target = f"{sys.platform}-" + {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    if target not in {f"{system}-{arch}" for system in ("win32", "darwin", "linux") for arch in ("x86_64", "arm64")}:
        raise SystemExit("This platform and architecture are not supported")
    subprocess.run([cargo, "build", "--locked", "--release", "--manifest-path", str(root / "native/Cargo.toml"),
                    "--target-dir", str(build_root),
                    *(["--offline"] if args.offline else [])], check=True, timeout=180)
    source = build_root / "release" / ("pymss-rpc-text.exe" if sys.platform == "win32" else "pymss-rpc-text")
    output = root / "bin" / target / source.name
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, output)
    print(output)


if __name__ == "__main__":
    main()
