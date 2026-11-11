from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPDX_HEADER = """/*
 * Copyright (c) Advanced Micro Devices, Inc., or its affiliates.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef HIP_INCLUDE_HIP_HIP_RUNTIME_API_H
"""
LEGACY_HEADER = """/*
Copyright (c) 2015 - 2025 Advanced Micro Devices, Inc. All rights reserved.
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software").
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.
*/
#ifndef HIP_INCLUDE_HIP_HIP_RUNTIME_API_H
"""


class RocmDetectorBuildTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.environment = self.root / "runtime with spaces"
        self.sdk = self.environment / "Lib/site-packages/_rocm_sdk_core"
        self.header = self.sdk / "include/hip/hip_runtime_api.h"
        self.header.parent.mkdir(parents=True)
        self.header.write_text(SPDX_HEADER, encoding="utf-8")
        self.binaries = self.sdk / "bin"
        self.binaries.mkdir()
        (self.binaries / "amdhip64_7.dll").write_bytes(b"sdk runtime")
        self.output = self.root / "detector"
        spec = importlib.util.spec_from_file_location("rocm_detector_build", ROOT / "scripts/build-rocm-detector.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.enterContext(mock.patch.object(self.module, "discover_rocm_native_paths", return_value={
            "nativeLibraryDirs": [self.binaries.relative_to(self.environment).as_posix()],
        }))

    def test_legacy_complete_notice_preserves_sdk_attribution(self):
        self.header.write_text(LEGACY_HEADER, encoding="utf-8")
        notice = self.module.hip_runtime_notice(self.header)
        self.assertEqual(notice, LEGACY_HEADER.split("*/", 1)[0].removeprefix("/*").strip())
        self.assertNotIn("#ifndef", notice)

    def test_spdx_notice_includes_full_mit_terms_and_current_attribution(self):
        self.header.write_bytes(b"\xef\xbb\xbf" + SPDX_HEADER.replace("\n", "\r\n").encode("utf-8"))
        notice = self.module.hip_runtime_notice(self.header)
        self.assertIn("Copyright (c) Advanced Micro Devices, Inc., or its affiliates.", notice)
        self.assertIn("SPDX-License-Identifier: MIT", notice)
        self.assertIn("Permission is hereby granted", notice)
        self.assertIn("FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.", notice)
        self.assertNotIn("#ifndef", notice)

    def test_unknown_or_ambiguous_header_license_is_rejected(self):
        for text in (
            SPDX_HEADER.replace("Identifier: MIT", "Identifier: Apache-2.0"),
            SPDX_HEADER.replace("Identifier: MIT", "Identifier: MIT OR GPL-2.0-only"),
            SPDX_HEADER.replace("Identifier: MIT", "Identifier: MIT-0"),
            SPDX_HEADER.replace("Copyright (c)", "Author"),
            SPDX_HEADER.replace("Advanced Micro Devices, Inc., or its affiliates.", "Another vendor"),
            LEGACY_HEADER.replace("Advanced Micro Devices, Inc.", "Another vendor"),
            LEGACY_HEADER.replace("Copyright (c)", "Author"),
            "/* API documentation */\n" + SPDX_HEADER,
            SPDX_HEADER.replace(" */", ""),
        ):
            with self.subTest(header=text):
                self.header.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "copyright and permission notice"):
                    self.module.hip_runtime_notice(self.header)

    def test_spdx_notice_requires_the_bundled_full_license(self):
        with mock.patch.object(self.module, "HIP_LICENSE", self.root / "missing-license"):
            with self.assertRaises(FileNotFoundError):
                self.module.hip_runtime_notice(self.header)

    def test_spdx_header_packages_full_license_and_manifest(self):
        document = self.sdk / "share/doc/hipcc/LICENSE.txt"
        document.parent.mkdir(parents=True)
        document.write_text("HIP compiler driver notice", encoding="utf-8")

        def compile_detector(command, **kwargs):
            executable = next(value[4:] for value in command if value.startswith("/Fe:"))
            Path(executable).write_bytes(b"compiled detector")

        with mock.patch.object(self.module, "compiler_environment", return_value={"PATH": "compiler"}), \
                mock.patch.object(self.module.shutil, "which", return_value="cl.exe"), \
                mock.patch.object(self.module.subprocess, "run", side_effect=compile_detector), \
                mock.patch.object(self.module, "pe_imports", return_value=set()):
            manifest = self.module.build(self.environment, self.output, "win-rocm100-multiarch")
        notice = (self.output / "licenses/HIP-runtime-notice.txt").read_text(encoding="utf-8")
        self.assertIn("SPDX-License-Identifier: MIT", notice)
        self.assertIn("Permission is hereby granted", notice)
        self.assertEqual((self.output / "licenses/hipcc-LICENSE.txt").read_text(encoding="utf-8"),
                         "HIP compiler driver notice")
        for name in ("licenses/HIP-runtime-notice.txt", "licenses/hipcc-LICENSE.txt",
                     "bin/rocm-detector.exe", "bin/amdhip64_7.dll"):
            self.assertEqual(manifest["files"][name], hashlib.sha256((self.output / name).read_bytes()).hexdigest())
        self.assertEqual(json.loads((self.output / "manifest.json").read_text(encoding="utf-8")), manifest)

    def test_invalid_notice_fails_before_output_or_compiler_initialization(self):
        self.header.write_text("/* No license declaration */", encoding="utf-8")
        with mock.patch.object(self.module, "compiler_environment") as compiler:
            with self.assertRaisesRegex(ValueError, "copyright and permission notice"):
                self.module.build(self.environment, self.output, "win-rocm100-multiarch")
        self.assertFalse(self.output.exists())
        compiler.assert_not_called()
