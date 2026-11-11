from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from . import _bootstrap as _worker_test_bootstrap
except ImportError:
    import _bootstrap as _worker_test_bootstrap  # noqa: F401

import worker_runtime_inventory as inventory


class RuntimeInventoryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.manifest = {"backends": {"rocm": {"platforms": ["win32"], "profiles": {
            "legacy": {"id": "legacy", "installationEnabled": True, "packages": {"torch": "2.9.1"}},
            "next": {"id": "next", "installationEnabled": False, "availableTargets": ["gfx1031"]}}}}}

    def bundle(self):
        root = self.root / "profile"
        root.mkdir()
        for name in ["detector.exe", "hip.dll"]:
            (root / name).write_bytes(name.encode())
        manifest = {"schemaVersion": 1, "executable": "detector.exe", "hipLibrary": "hip.dll",
                    "files": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                              for name in ["detector.exe", "hip.dll"]}}
        (root / "manifest.json").write_text(json.dumps(manifest))
        return root

    def test_unknown_hardware_is_not_mapped_from_a_model_name(self):
        raw = {"devices": [{"Name": "AMD Radeon RX 6700 XT", "PNPDeviceID": "PCI\\VEN_1002&DEV_73DF", "DriverVersion": "1"}]}
        with mock.patch.object(inventory.sys, "platform", "win32"), mock.patch.object(inventory, "_child", return_value=raw):
            devices = inventory.system_inventory()
        self.assertEqual(devices[0]["vendor"], "amd")
        self.assertIsNone(devices[0]["gfx"])
        self.assertEqual(devices[0]["architectureConfidence"], "unknown")

    def test_manifest_digest_is_checked_before_launch(self):
        root = self.bundle()
        (root / "hip.dll").write_bytes(b"tampered")
        with mock.patch.object(inventory, "_child") as child, self.assertRaises(ValueError):
            inventory.native_inventory(root)
        child.assert_not_called()

    def test_manifest_cannot_launch_outside_its_bundle(self):
        root = self.bundle()
        path = root / "manifest.json"
        value = json.loads(path.read_text())
        value["files"]["../secret.exe"] = "a" * 64
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            inventory.verify_detector(root)

    def test_native_identity_uses_pci_instead_of_enumeration_index(self):
        root = self.bundle()
        raw = {"schemaVersion": 1, "available": True, "devices": [{"nativeIndex": 7, "name": "AMD GPU",
               "gfx": "gfx1031:xnack-", "pciBusId": "0000:03:00.0", "memoryBytes": 12 * 1024**3, "isDiscrete": True}]}
        with mock.patch.object(inventory, "_child", return_value=raw):
            device = inventory.native_inventory(root)[0]
        self.assertEqual(device["id"], "hip-pci:0000:03:00.0")
        self.assertEqual(device["gfx"], "gfx1031")
        self.assertEqual(device["architectureConfidence"], "confirmed")
        self.assertNotIn("nativeIndex", device)

    def test_native_detector_failure_preserves_system_choices_and_disabled_policy(self):
        self.bundle()
        system = [{"id": "pnp:a", "vendor": "amd", "name": "AMD", "driverVersion": "1"}]
        with mock.patch.object(inventory.sys, "platform", "win32"), \
             mock.patch.object(inventory, "system_inventory", return_value=system), \
             mock.patch.object(inventory, "native_inventory", side_effect=RuntimeError("unavailable")):
            report = inventory.hardware_inventory(self.manifest, self.root)
        self.assertEqual(report["devices"], system)
        self.assertEqual(report["profiles"][1]["releaseState"], "disabled")
        self.assertFalse(report["profiles"][1]["installationEnabled"])

    def test_inventory_child_is_bounded_and_cleans_up(self):
        result = inventory._child([sys.executable, "-I", "-c", "print('{\"ok\":true}')"], timeout=10)
        self.assertEqual(result, {"ok": True})
        with self.assertRaises(subprocess.TimeoutExpired):
            inventory._child([sys.executable, "-I", "-c", "import time; time.sleep(60)"], timeout=0.2)

    def test_inventory_exposes_the_configured_default_even_without_a_native_detector(self):
        self.manifest["backends"]["rocm"]["defaultProfile"] = "next"
        self.manifest["backends"]["rocm"]["profiles"]["next"].update(installationEnabled=True, releaseState="preview")
        with mock.patch.object(inventory.sys, "platform", "win32"), mock.patch.object(inventory, "system_inventory", return_value=[]):
            report = inventory.hardware_inventory(self.manifest, self.root)
        self.assertEqual(report["defaultProfileId"], "next")
        self.assertEqual(report["profiles"][1]["releaseState"], "preview")


if __name__ == "__main__":
    unittest.main()
