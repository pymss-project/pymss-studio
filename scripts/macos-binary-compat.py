"""Keep bundled native binaries loadable on the oldest supported macOS release.

pip selects wheels for the build host, so a runner on a newer macOS can install
binaries that terminate on import on the systems the release still supports.

Usage (run with the bundled runtime interpreter):
  macos-binary-compat.py --platform-tag             print the wheel platform tag to install
  macos-binary-compat.py --distributions            print name==version of installed
                                                    distributions with incompatible binaries
  macos-binary-compat.py --check <runtime-root>     fail when any Mach-O file under the
                                                    root requires a newer macOS

The deployment target is MACOSX_DEPLOYMENT_TARGET, or the application's
minimumSystemVersion when the variable is not set.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import struct
import sys
from importlib import metadata
from pathlib import Path

LC_BUILD_VERSION = 0x32
LC_VERSION_MIN_MACOSX = 0x24
PLATFORM_MACOS = 1
MH_MAGIC_64 = 0xFEEDFACF
FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_64 = 0xCAFEBABF
# Java class files share FAT_MAGIC; their version field is far above any real slice count.
MAX_FAT_SLICES = 16
TAURI_CONFIG = Path(__file__).resolve().parent.parent / "src-tauri" / "tauri.conf.json"

Version = tuple[int, int, int]


def parse_version(value: str) -> Version:
    parts = [int(part) for part in value.strip().split(".")]
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"Invalid macOS version: {value!r}")
    return tuple((parts + [0, 0])[:3])  # type: ignore[return-value]


def deployment_target() -> Version:
    value = os.environ.get("MACOSX_DEPLOYMENT_TARGET", "").strip()
    if not value:
        config = json.loads(TAURI_CONFIG.read_text(encoding="utf-8"))
        value = config["bundle"]["macOS"]["minimumSystemVersion"]
    return parse_version(value)


def _decode(value: int) -> Version:
    return value >> 16, (value >> 8) & 0xFF, value & 0xFF


def _thin_minimum(data: bytes) -> Version | None:
    if len(data) < 32 or struct.unpack_from("<I", data)[0] != MH_MAGIC_64:
        return None
    count = struct.unpack_from("<I", data, 16)[0]
    offset = 32
    for _ in range(count):
        if offset + 16 > len(data):
            break
        command, size = struct.unpack_from("<II", data, offset)
        if command == LC_BUILD_VERSION:
            build_platform, minimum = struct.unpack_from("<II", data, offset + 8)
            if build_platform == PLATFORM_MACOS:
                return _decode(minimum)
        elif command == LC_VERSION_MIN_MACOSX:
            return _decode(struct.unpack_from("<I", data, offset + 8)[0])
        if size < 8:
            break
        offset += size
    return None


def _read_thin(file, offset: int) -> bytes:
    file.seek(offset)
    header = file.read(32)
    if len(header) < 32:
        return header
    return header + file.read(struct.unpack_from("<I", header, 20)[0])


def minimum_macos(path: Path) -> Version | None:
    """Return the highest macOS version any slice of a Mach-O file requires."""
    with path.open("rb") as file:
        head = file.read(8)
        if len(head) < 8:
            return None
        if struct.unpack_from("<I", head)[0] == MH_MAGIC_64:
            return _thin_minimum(_read_thin(file, 0))
        magic, slices = struct.unpack_from(">II", head)
        if magic not in (FAT_MAGIC, FAT_MAGIC_64) or not 0 < slices <= MAX_FAT_SLICES:
            return None
        entry = 32 if magic == FAT_MAGIC_64 else 20
        table = file.read(slices * entry)
        found = []
        for index in range(slices):
            base = index * entry + 8
            offset = struct.unpack_from(">Q" if magic == FAT_MAGIC_64 else ">I", table, base)[0]
            version = _thin_minimum(_read_thin(file, offset))
            if version:
                found.append(version)
        return max(found, default=None)


def incompatible_files(paths, target: Version) -> list[tuple[Path, Version]]:
    result = []
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        version = minimum_macos(path)
        if version and version > target:
            result.append((path, version))
    return result


def incompatible_distributions(target: Version) -> list[str]:
    result = set()
    for distribution in metadata.distributions():
        files = [Path(distribution.locate_file(item)) for item in distribution.files or ()
                 if item.suffix in {".so", ".dylib"}]
        if incompatible_files(files, target):
            result.add(f"{distribution.metadata['Name']}=={distribution.version}")
    return sorted(result)


def _format(version: Version) -> str:
    return ".".join(str(part) for part in version)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--platform-tag", action="store_true")
    mode.add_argument("--distributions", action="store_true")
    mode.add_argument("--check", type=Path, metavar="RUNTIME_ROOT")
    args = parser.parse_args(argv)
    target = deployment_target()

    if args.platform_tag:
        print(f"macosx_{target[0]}_{target[1]}_{platform.machine()}")
        return 0
    if args.distributions:
        for requirement in incompatible_distributions(target):
            print(requirement)
        return 0
    offending = incompatible_files(sorted(args.check.rglob("*")), target)
    for path, version in offending:
        print(f"{path}: requires macOS {_format(version)}", file=sys.stderr)
    if offending:
        print(f"{len(offending)} native binaries require a macOS newer than the deployment target "
              f"{_format(target)}", file=sys.stderr)
        return 1
    print(f"All native binaries support macOS {_format(target)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
