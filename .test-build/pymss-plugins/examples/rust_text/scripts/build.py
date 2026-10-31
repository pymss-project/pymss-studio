"""Build the native command for the developer's current platform."""
from __future__ import annotations

import argparse
from pathlib import Path
import platform
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    compiler = shutil.which("rustc")
    if compiler is None:
        raise SystemExit("Rust is required to build this development checkout. Install a prebuilt plugin package for normal use.")
    root = Path(__file__).resolve().parents[1]
    machine = platform.machine().lower()
    architecture = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    target = f"{sys.platform}-{architecture}"
    if target not in {f"{system}-{arch}" for system in ("win32", "darwin", "linux") for arch in ("x86_64", "arm64")}:
        raise SystemExit("This platform and architecture are not supported")
    destination = (args.output_root or root) / "bin" / target
    destination.mkdir(parents=True, exist_ok=True)
    program = destination / ("text-normalize.exe" if sys.platform == "win32" else "text-normalize")
    subprocess.run([compiler, "--edition=2021", "-O", str(root / "native/main.rs"), "-o", str(program)], check=True, timeout=120)
    print(program)


if __name__ == "__main__":
    main()
