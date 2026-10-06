from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "runtime_update_compatibility",
    ROOT / "scripts" / "check-runtime-update-compatibility.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def manifest(*, generation=1, python="3.12", torch="torch==2.7.1", common=None):
    return {
        "baseGeneration": generation,
        "python": python,
        "common": common or {"pymss": "pymss>=1"},
        "backends": {
            "cpu": {
                "platforms": ["win32"],
                "torch": {"requirement": torch, "indexUrl": "cpu"},
            }
        },
    }


class RuntimeUpdateCompatibilityTests(unittest.TestCase):
    def test_common_package_changes_use_overlay(self):
        previous = manifest(common={"pymss": "pymss>=1"})
        current = manifest(common={"pymss": "pymss>=2", "requests": "requests>=2"})
        self.assertTrue(MODULE.runtime_base_compatibility(previous, current)[0])

    def test_missing_generation_is_generation_one_for_bridge_releases(self):
        previous = manifest()
        previous.pop("baseGeneration")
        self.assertTrue(MODULE.runtime_base_compatibility(
            previous,
            manifest(),
            base_layout_changed=True,
        )[0])

    def test_base_build_script_changes_require_a_new_generation_after_bridge(self):
        compatible, reason = MODULE.runtime_base_compatibility(
            manifest(),
            manifest(),
            base_layout_changed=True,
        )
        self.assertFalse(compatible)
        self.assertIn("base runtime build scripts changed", reason)

    def test_python_or_accelerator_changes_require_base_update(self):
        self.assertFalse(MODULE.runtime_base_compatibility(manifest(), manifest(python="3.13"))[0])
        self.assertFalse(MODULE.runtime_base_compatibility(manifest(), manifest(torch="torch==2.8.0"))[0])
        self.assertFalse(MODULE.runtime_base_compatibility(manifest(), manifest(generation=2))[0])

    def test_new_backend_does_not_invalidate_existing_runtimes(self):
        current = manifest()
        current["backends"]["cuda"] = {
            "platforms": ["win32"],
            "torch": {"requirement": "torch==2.7.1", "indexUrl": "cuda"},
        }
        self.assertTrue(MODULE.runtime_base_compatibility(manifest(), current)[0])

    def test_core_updates_preserve_the_existing_base(self):
        previous = manifest(common={"pymss": "pymss[proxy]==2.1.7", "pymss-core": "pymss-core==0.1.10"})
        current = manifest(common={"pymss": "pymss[proxy]==2.1.8", "pymss-core": "pymss-core==0.1.11"})
        self.assertTrue(MODULE.runtime_base_compatibility(previous, current)[0])
        self.assertFalse(MODULE.runtime_base_compatibility(previous, current, base_layout_changed=True)[0])
        current["python"] = "3.13"
        self.assertFalse(MODULE.runtime_base_compatibility(previous, current)[0])

    def test_dml_addition_and_core_updates_preserve_the_existing_base(self):
        previous = manifest(common={"pymss": "pymss[proxy]==2.1.7", "pymss-core": "pymss-core==0.1.10"})
        current = manifest(common={"pymss": "pymss[proxy]==2.1.8", "pymss-core": "pymss-core==0.1.11"})
        current["backends"]["dml"] = {
            "platforms": ["win32"], "python": "3.12",
            "torch": {"requirement": "torch==2.4.1", "indexUrl": "cpu"},
            "extras": ["torch-directml==0.2.5.dev240914", "numpy>=1.26,<2"],
        }
        self.assertTrue(MODULE.runtime_base_compatibility(previous, current)[0])
        self.assertFalse(MODULE.runtime_base_compatibility(previous, current, base_layout_changed=True)[0])
        current["python"] = "3.13"
        self.assertFalse(MODULE.runtime_base_compatibility(previous, current)[0])


if __name__ == "__main__":
    unittest.main()
