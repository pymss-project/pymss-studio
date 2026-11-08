"""Build a header-matched, Torch-free Windows HIP inventory bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from worker_runtime_paths import discover_rocm_native_paths  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pe_imports(path: Path) -> set[str]:
    """Read regular and delay imports without executing an SDK binary."""
    data = path.read_bytes()
    if len(data) < 64 or data[:2] != b"MZ":
        raise ValueError("Invalid Windows detector dependency")
    pe = struct.unpack_from("<I", data, 60)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("Invalid PE header")
    sections, optional_size = struct.unpack_from("<H12xH", data, pe + 6)
    optional = pe + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    if magic != 0x20B:
        raise ValueError("The ROCm detector requires Windows x64 binaries")
    directory = optional + 112
    table = optional + optional_size

    def offset(rva: int) -> int:
        for index in range(sections):
            virtual_size, address, size, raw = struct.unpack_from("<IIII", data, table + index * 40 + 8)
            if address <= rva < address + max(size, virtual_size):
                value = raw + rva - address
                if value < len(data):
                    return value
        raise ValueError("PE import is outside its image")

    imports = set()
    for slot, stride, name_offset in [(1, 20, 12), (13, 32, 4)]:
        rva, length = struct.unpack_from("<II", data, directory + slot * 8)
        if not rva:
            continue
        start = offset(rva)
        for index in range(min(length // stride + 1, 4096)):
            descriptor = data[start + index * stride:start + (index + 1) * stride]
            if len(descriptor) != stride:
                raise ValueError("Truncated PE import table")
            if not any(descriptor):
                break
            if slot == 13 and struct.unpack_from("<I", descriptor)[0] != 1:
                raise ValueError("Unsupported absolute delay import")
            name_rva = struct.unpack_from("<I", descriptor, name_offset)[0]
            position = offset(name_rva)
            end = data.find(b"\0", position, position + 260)
            if end < 0:
                raise ValueError("Invalid PE dependency name")
            name = data[position:end].decode("ascii").lower()
            if not name.endswith(".dll") or Path(name).name != name or "/" in name or "\\" in name:
                raise ValueError("Invalid DLL import")
            imports.add(name)
    return imports


def compiler_environment() -> dict[str, str]:
    environment = dict(os.environ)
    if shutil.which("cl.exe"):
        return environment
    vswhere = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft Visual Studio/Installer/vswhere.exe"
    result = subprocess.run([str(vswhere), "-latest", "-products", "*", "-requires",
                             "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"],
                            capture_output=True, text=True, check=True, timeout=30)
    install = result.stdout.strip()
    if not install:
        raise RuntimeError("MSVC x64 build tools are required to build the detector")
    with tempfile.TemporaryDirectory(prefix="studio-detector-msvc-") as temporary:
        script = Path(temporary) / "environment.cmd"
        script.write_text('@echo off\ncall "' + str(Path(install) / "Common7/Tools/VsDevCmd.bat")
                          + '" -arch=x64 -host_arch=x64 >nul\nif errorlevel 1 exit /b 1\nset\n', encoding="mbcs")
        result = subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(script)],
                                capture_output=True, check=True, timeout=60)
        # Never print or persist the environment dump, which can contain credentials.
        for line in result.stdout.decode("mbcs", errors="replace").splitlines():
            if "=" in line and not line.startswith("="):
                key, value = line.split("=", 1)
                for existing in list(environment):
                    if existing.casefold() == key.casefold():
                        environment.pop(existing)
                environment[key] = value
    return environment


def build(environment: Path, output: Path, profile: str) -> dict:
    environment = environment.resolve(strict=True)
    paths = discover_rocm_native_paths(environment)
    binaries = [environment / raw for raw in paths["nativeLibraryDirs"]]
    hip = [p for directory in binaries for p in directory.glob("amdhip64*.dll")]
    hip = list(dict.fromkeys(p.resolve() for p in hip))
    if len(hip) != 1:
        raise ValueError("The selected SDK must provide one HIP runtime DLL")
    sdk = hip[0].parent.parent
    header = sdk / "include/hip/hip_runtime_api.h"
    if not header.is_file() or not header.resolve().is_relative_to(environment):
        raise ValueError("The matching HIP SDK headers were not found")
    output.mkdir(parents=True, exist_ok=False)
    destination = output / "bin"
    destination.mkdir()
    compiler = compiler_environment()
    with tempfile.TemporaryDirectory(prefix="studio-detector-build-") as temporary:
        exe = Path(temporary) / "rocm-detector.exe"
        compiler_path = next((value for key, value in compiler.items() if key.casefold() == "path"), "")
        executable = shutil.which("cl.exe", path=compiler_path)
        if not executable:
            raise RuntimeError("MSVC x64 compiler was not found after toolchain initialization")
        command = [executable, "/nologo", "/EHsc", "/std:c++17", "/O2", "/MT", "/utf-8",
                   "/I" + str(sdk / "include"), str(ROOT / "tools/rocm-detector/main.cpp"), "/Fe:" + str(exe),
                   "/Fo:" + str(Path(temporary) / "detector.obj"), "/link", "/DYNAMICBASE", "/NXCOMPAT"]
        subprocess.run(command, env=compiler, cwd=temporary, check=True, timeout=180)
        shutil.copy2(exe, destination / exe.name)
    candidates: dict[str, list[Path]] = {}
    for directory in binaries:
        for dll in directory.glob("*.dll"):
            if not dll.resolve().is_relative_to(environment):
                raise ValueError("An SDK DLL resolves outside its environment")
            candidates.setdefault(dll.name.lower(), []).append(dll)
    system = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32"
    pending = [hip[0], destination / "rocm-detector.exe"]
    visited = set()
    system_dependencies = set()
    while pending:
        binary = pending.pop()
        if binary.name.lower() in visited:
            continue
        visited.add(binary.name.lower())
        if binary.parent != destination:
            shutil.copy2(binary, destination / binary.name)
        for name in pe_imports(binary):
            choices = candidates.get(name, [])
            if choices:
                if len({sha256(p) for p in choices}) != 1:
                    raise ValueError("Ambiguous SDK DLL dependency: " + name)
                pending.append(choices[0])
            elif name.startswith(("api-ms-win-", "ext-ms-win-")) or (system / name).is_file():
                system_dependencies.add(name)
            else:
                raise ValueError("Missing detector DLL dependency: " + name)
    licenses = output / "licenses"
    licenses.mkdir()
    header_text = header.read_text(encoding="utf-8")
    notice = header_text.split("*/", 1)[0].removeprefix("/*").strip()
    if "Permission is hereby granted" not in notice or "SOFTWARE" not in notice:
        raise ValueError("The HIP SDK copyright and permission notice could not be identified")
    (licenses / "HIP-runtime-notice.txt").write_text(notice + "\n", encoding="utf-8")
    for source in (sdk / "share/doc").rglob("*"):
        if source.is_file() and any(token in source.name.lower() for token in ("license", "copying", "notice")):
            if not source.resolve().is_relative_to(environment):
                raise ValueError("An SDK license resolves outside its environment")
            shutil.copy2(source, licenses / (source.parent.name + "-" + source.name))
    for metadata in environment.glob("Lib/site-packages/rocm*dist-info"):
        for source in metadata.glob("licenses/**/*"):
            if source.is_file() and source.resolve().is_relative_to(environment):
                name = metadata.name + "-" + source.name
                shutil.copy2(source, licenses / name)
    manifest = {"schemaVersion": 1, "profileId": profile, "executable": "bin/rocm-detector.exe",
                "hipLibrary": "bin/" + hip[0].name, "validationStatus": "preview",
                "sdkHeaderSha256": sha256(header), "systemDependencies": sorted(system_dependencies),
                "files": {p.relative_to(output).as_posix(): sha256(p) for p in sorted(output.rglob("*")) if p.is_file()}}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("The detector is built on Windows x64")
    result = build(args.environment, args.output, args.profile)
    print(json.dumps({"profileId": result["profileId"], "files": len(result["files"]), "validationStatus": "preview"}))
