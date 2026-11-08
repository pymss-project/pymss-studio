"""Exercise accelerator constraints with real pip and small local wheels only."""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import tempfile
import unittest
import venv
import zipfile
from pathlib import Path

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap  # noqa: F401

from worker_runtime_profiles import accelerator_constraints


class OfflineAcceleratorConstraintTests(unittest.TestCase):
    def test_rocm_sdk_and_device_kernels_cannot_be_upgraded_by_core_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "runtime"
            venv.EnvBuilder(with_pip=True).create(base)
            python = base / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            wheels = root / "wheels"
            wheels.mkdir()

            def wheel(name, version, requirements=()):
                module = name.replace("-", "_")
                info = f"{module}-{version}.dist-info"
                entries = {
                    f"{module}/__init__.py": "",
                    f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                    + "".join(f"Requires-Dist: {requirement}\n" for requirement in requirements),
                    f"{info}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
                }
                record = io.StringIO()
                csv.writer(record).writerows((path, "", "") for path in [*entries, f"{info}/RECORD"])
                entries[f"{info}/RECORD"] = record.getvalue()
                with zipfile.ZipFile(wheels / f"{module}-{version}-py3-none-any.whl", "w") as archive:
                    for path, content in entries.items():
                        archive.writestr(path, content)

            fixed = {
                "torch": "2.13.0+rocm10.0.0", "torchaudio": "2.11.0.2+rocm10.0.0",
                "rocm": "10.0.0", "rocm-sdk-core": "10.0.0", "amd-torch-device-gfx1031": "2.13.0+rocm10.0.0",
            }
            for name, version in fixed.items():
                wheel(name, version)
            for name in ["rocm", "rocm-sdk-core", "amd-torch-device-gfx1031"]:
                wheel(name, "99.0")
            wheel("pymss", "2.3.4", ["torch>=2.13", "torchaudio==2.11.0.2+rocm10.0.0"])
            for index, name in enumerate(["rocm", "rocm-sdk-core", "amd-torch-device-gfx1031"], 1):
                wheel("pymss", f"2.3.{4 + index}", [f"{name}==99.0"])
            environment = {key: value for key, value in os.environ.items()
                           if not key.upper().startswith("PIP_") and key.upper() not in {"PYTHONHOME", "PYTHONPATH"}}
            environment["PIP_CONFIG_FILE"] = os.devnull
            environment["PYTHONNOUSERSITE"] = "1"
            command = [str(python), "-m", "pip", "install", "--disable-pip-version-check",
                       "--no-index", "--find-links", str(wheels)]
            seeded = subprocess.run(command + [f"{name}=={version}" for name, version in fixed.items()] + ["pymss==2.3.4"],
                                    env=environment, capture_output=True, text=True, timeout=60)
            self.assertEqual(seeded.returncode, 0, seeded.stderr)
            constraints = root / "constraints.txt"
            constraints.write_text(accelerator_constraints(fixed), encoding="utf-8")
            for index, name in enumerate(["rocm", "rocm-sdk-core", "amd-torch-device-gfx1031"], 1):
                with self.subTest(name=name):
                    resolved = subprocess.run(command + ["--dry-run", "--constraint", str(constraints), f"pymss==2.3.{4 + index}"],
                                              env=environment, capture_output=True, text=True, timeout=60)
                    self.assertNotEqual(resolved.returncode, 0)
                    self.assertIn("ResolutionImpossible", resolved.stderr)
            versions = subprocess.run([str(python), "-c", "from importlib import metadata; import json; "
                                       "print(json.dumps({d.metadata['Name']:d.version for d in metadata.distributions()}))"],
                                      env=environment, capture_output=True, text=True, timeout=30, check=True)
            installed = json.loads(versions.stdout)
            self.assertEqual({name: installed[name] for name in fixed}, fixed)
            self.assertEqual(installed["pymss"], "2.3.4")


if __name__ == "__main__":
    unittest.main()
