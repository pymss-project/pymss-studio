from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from . import _bootstrap as _worker_test_bootstrap
except ImportError:
    import _bootstrap as _worker_test_bootstrap  # noqa: F401

import worker_runtime_seed as seed


class RuntimeSeedTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.base = self.root / "app/python-runtime"
        self.source = self.base / "runtime-envs/rocm"
        self.managed = self.root / "user/runtime-envs"
        self.source.mkdir(parents=True)
        (self.base / "python.exe").write_bytes(b"python-one")
        (self.base / "python312.dll").write_bytes(b"dll-one")
        (self.base / "Lib/encodings").mkdir(parents=True)
        (self.base / "Lib/encodings/__init__.py").write_text("# stdlib", encoding="utf-8")
        (self.source / "Scripts").mkdir()
        (self.source / "Scripts/python.exe").write_bytes(b"venv-launcher")
        self.state = {"backend": "rocm", "profileId": "seed", "torchVersion": "2.9.1+rocm7.2.1",
                      "baseFingerprint": "base", "installedTargets": ["gfx1100"]}
        (self.source / "pymss-runtime-state.json").write_text(json.dumps(self.state), encoding="utf-8")
        (self.source / "pyvenv.cfg").write_text("home = __TEMPLATE__", encoding="utf-8")

    def test_plan_reports_local_copy_and_no_network_without_modifying_seed(self):
        before = (self.source / "pyvenv.cfg").read_bytes()
        plan = seed.seed_plan(self.managed, self.source.parent, self.base / "python.exe", {"baseGeneration": 2})
        self.assertTrue(plan["preparationRequired"])
        self.assertEqual(plan["downloadBytes"], 0)
        self.assertEqual(plan["targets"], ["gfx1100"])
        self.assertFalse(self.managed.exists())
        self.assertEqual((self.source / "pyvenv.cfg").read_bytes(), before)

    def test_copy_is_independent_and_preserves_old_python_after_app_changes(self):
        destination = self.managed / "rocm"
        state = seed.copy_seed(self.source, destination, self.base / "python.exe", self.managed, 2)
        old = seed.bound_bootstrap_python(destination, self.managed, self.base / "python.exe")
        self.assertEqual(old.read_bytes(), b"python-one")
        self.assertEqual(state["baseFingerprint"], "base")
        self.assertIn(str(old.parent), (destination / "pyvenv.cfg").read_text())
        (self.base / "python.exe").write_bytes(b"python-two")
        new = seed.ensure_managed_bootstrap(self.base, self.managed, 3)
        self.assertNotEqual(new["root"], state["bootstrapBinding"]["root"])
        self.assertEqual(seed.bound_bootstrap_python(destination, self.managed, self.base / "python.exe"), old)
        self.assertEqual(old.read_bytes(), b"python-one")
        self.assertFalse((old.parent / "runtime-envs").exists())
        (destination / "Scripts/python.exe").write_bytes(b"changed")
        self.assertEqual((self.source / "Scripts/python.exe").read_bytes(), b"venv-launcher")

    def test_relative_binding_survives_managed_root_relocation(self):
        destination = self.managed / "rocm"
        seed.copy_seed(self.source, destination, self.base, self.managed, 2)
        moved = self.root / "moved user/runtime-envs"
        moved.parent.mkdir()
        self.managed.rename(moved)
        python = seed.bound_bootstrap_python(moved / "rocm", moved, self.base / "python.exe")
        self.assertTrue(python.is_relative_to(moved))
        binding = json.loads((moved / "rocm/pymss-bootstrap-binding.json").read_text())
        seed.bind_environment(moved / "rocm", moved, binding)
        self.assertIn(str(python), (moved / "rocm/pyvenv.cfg").read_text())

    def test_existing_user_environment_and_foreign_binding_are_not_overwritten(self):
        destination = self.managed / "rocm"
        destination.mkdir(parents=True)
        (destination / "user.txt").write_text("keep")
        with self.assertRaises(ValueError):
            seed.copy_seed(self.source, destination, self.base, self.managed, 2)
        (destination / "pymss-bootstrap-binding.json").write_text(json.dumps({"schemaVersion": 1, "root": "../outside"}))
        with self.assertRaises(ValueError):
            seed.bound_bootstrap_python(destination, self.managed, self.base / "python.exe")
        self.assertEqual((destination / "user.txt").read_text(), "keep")

    def test_corrupt_python_base_fails_instead_of_rebinding_to_new_app(self):
        destination = self.managed / "rocm"
        seed.copy_seed(self.source, destination, self.base, self.managed, 2)
        old = seed.bound_bootstrap_python(destination, self.managed, self.base / "python.exe")
        old.write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            seed.bound_bootstrap_python(destination, self.managed, self.base / "python.exe")

    def test_cancelled_preparation_does_not_publish_partial_base(self):
        self.managed.mkdir(parents=True)
        with self.assertRaises(InterruptedError):
            seed.ensure_managed_bootstrap(self.base, self.managed, 2, cancelled=lambda: True)
        self.assertEqual(list((self.managed / ".bootstrap").iterdir()), [])

    def test_same_root_has_no_copy_plan(self):
        self.assertEqual(seed.seed_plan(self.source.parent, self.source.parent, self.base, {}), {"available": False})

    def test_runtime_info_can_check_seed_without_scanning_the_full_environment(self):
        with mock.patch.object(seed, "_files", side_effect=AssertionError("Full scan on a cheap status query")):
            plan = seed.seed_plan(self.managed, self.source.parent, self.base, {}, include_sizes=False)
        self.assertTrue(plan["preparationRequired"])
        self.assertNotIn("spaceRequiredBytes", plan)


if __name__ == "__main__":
    unittest.main()
