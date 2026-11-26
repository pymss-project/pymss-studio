from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("macos_binary_compat", ROOT / "scripts" / "macos-binary-compat.py")
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)


def encode(version: tuple[int, int, int]) -> int:
    return version[0] << 16 | version[1] << 8 | version[2]


def thin(minimum=(26, 2, 0), *, legacy=False) -> bytes:
    if legacy:
        command = struct.pack("<IIII", compat.LC_VERSION_MIN_MACOSX, 16, encode(minimum), encode((26, 5, 0)))
    else:
        command = struct.pack("<IIIIII", compat.LC_BUILD_VERSION, 24, compat.PLATFORM_MACOS,
                              encode(minimum), encode((26, 5, 0)), 0)
    header = struct.pack("<IiiIIIII", compat.MH_MAGIC_64, 0x0100000C, 0, 6, 1, len(command), 0, 0)
    return header + command + b"\0" * 64


def fat(*slices: bytes) -> bytes:
    offsets = [4096 * (index + 1) for index in range(len(slices))]
    data = bytearray()
    table = b"".join(struct.pack(">IIIII", 0x0100000C, 0, offset, len(item), 12)
                     for offset, item in zip(offsets, slices))
    data += struct.pack(">II", compat.FAT_MAGIC, len(slices)) + table
    for offset, item in zip(offsets, slices):
        data += b"\0" * (offset - len(data)) + item
    return bytes(data)


class MacOSBinaryCompatTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def write(self, name: str, content: bytes) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_minimum_macos_reads_thin_legacy_and_fat_binaries(self):
        self.assertEqual(compat.minimum_macos(self.write("thin.dylib", thin((26, 2, 0)))), (26, 2, 0))
        self.assertEqual(compat.minimum_macos(self.write("legacy.so", thin((13, 0, 0), legacy=True))), (13, 0, 0))
        universal = self.write("fat.dylib", fat(thin((14, 0, 0)), thin((26, 2, 0))))
        self.assertEqual(compat.minimum_macos(universal), (26, 2, 0))

    def test_non_mach_o_files_are_ignored(self):
        java_class = b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + b"\0" * 64
        for name, content in (("Main.class", java_class), ("module.py", b"import os\n"), ("empty", b"")):
            with self.subTest(name=name):
                self.assertIsNone(compat.minimum_macos(self.write(name, content)))

    def test_check_fails_only_for_binaries_newer_than_the_deployment_target(self):
        self.write("lib/mlx/libmlx.dylib", thin((26, 2, 0)))
        self.write("lib/torch/libtorch.dylib", thin((14, 0, 0)))
        self.write("lib/numpy/core.so", thin((11, 0, 0), legacy=True))
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"MACOSX_DEPLOYMENT_TARGET": "14.0.0"}), \
             contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(compat.main(["--check", str(self.root)]), 1)
        self.assertIn("libmlx.dylib: requires macOS 26.2.0", stderr.getvalue())
        self.assertNotIn("libtorch", stderr.getvalue())
        (self.root / "lib/mlx/libmlx.dylib").write_bytes(thin((14, 0, 0)))
        with mock.patch.dict(os.environ, {"MACOSX_DEPLOYMENT_TARGET": "14.0.0"}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(compat.main(["--check", str(self.root)]), 0)

    def test_distributions_owning_incompatible_binaries_are_listed_for_reinstall(self):
        site = self.root / "site-packages"
        for name, version, minimum in (("mlx-metal", "0.32.5", (26, 2, 0)), ("torch", "2.7.1", (14, 0, 0))):
            package = name.replace("-", "_")
            self.write(f"site-packages/{package}/lib/lib{package}.dylib", thin(minimum))
            info = site / f"{package}-{version}.dist-info"
            info.mkdir()
            (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8")
            (info / "RECORD").write_text(f"{package}/lib/lib{package}.dylib,,\n", encoding="utf-8")
        with mock.patch.object(compat.sys, "path", [str(site)]):
            self.assertEqual(compat.incompatible_distributions((14, 0, 0)), ["mlx-metal==0.32.5"])

    def test_deployment_target_falls_back_to_the_application_minimum(self):
        environment = {key: value for key, value in os.environ.items() if key != "MACOSX_DEPLOYMENT_TARGET"}
        with mock.patch.dict(os.environ, environment, clear=True):
            self.assertEqual(compat.deployment_target(), (14, 0, 0))
            output = io.StringIO()
            with contextlib.redirect_stdout(output), mock.patch.object(compat.platform, "machine", return_value="arm64"):
                compat.main(["--platform-tag"])
        self.assertEqual(output.getvalue().strip(), "macosx_14_0_arm64")

    def test_runtime_preparation_repairs_before_pruning_and_verifies_the_result(self):
        script = (ROOT / "scripts/prepare-python-runtime.sh").read_text(encoding="utf-8")
        core = script.index('"$MANIFEST_PYMSS_REQUIREMENT" "$MANIFEST_PYMSS_CORE_REQUIREMENT"')
        repair = script.index('"$MACOS_BINARY_COMPAT" --distributions')
        prune = script.index('prune-python-runtime.sh" "$RUNTIME_DIR" --keep-venv', repair)
        check = script.index('"$MACOS_BINARY_COMPAT" --check "$RUNTIME_DIR"')
        self.assertLess(core, repair)
        self.assertLess(repair, prune)
        self.assertLess(prune, check)


if __name__ == "__main__":
    unittest.main()
