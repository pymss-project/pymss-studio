from __future__ import annotations

import contextlib
import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

try:
    from . import _bootstrap as _worker_test_bootstrap
except ImportError:
    import _bootstrap as _worker_test_bootstrap  # noqa: F401

import worker_runtime_activation as activation


class RuntimeActivationTests(unittest.TestCase):
    def check(self, architectures, pci_ids, selected, *, unique_pnp=False):
        calls = []

        class Value:
            def __add__(self, _other):
                return self

            def sum(self):
                return self

            def item(self):
                return 32

        class Bus:
            def __call__(self, buffer, _size, index):
                buffer.value = pci_ids[index].encode()
                return 0

        torch = types.SimpleNamespace(
            version=types.SimpleNamespace(hip="10.0"), float32="fp32",
            cuda=types.SimpleNamespace(is_available=lambda: True, device_count=lambda: len(architectures),
                get_device_properties=lambda i: types.SimpleNamespace(name="AMD", gcnArchName=architectures[i]),
                synchronize=lambda index: calls.append(index)),
            ones=lambda *_args, **_kwargs: Value(),
        )
        request = {"ids": selected, "hipLibrary": "C:/verified/amdhip64.dll", "uniquePnp": unique_pnp}
        output = io.StringIO()
        with mock.patch.dict(sys.modules, {"torch": torch}), \
             mock.patch("ctypes.CDLL", return_value=types.SimpleNamespace(hipDeviceGetPCIBusId=Bus())), \
             mock.patch.object(sys, "argv", ["check", json.dumps(["gfx1031"]), "null", json.dumps(request)]), \
             contextlib.redirect_stdout(output):
            exec(activation._CHECK, {"__name__": "__main__"})
        return json.loads(output.getvalue()), calls

    def test_another_gpu_passing_cannot_activate_the_selected_unsupported_card(self):
        report, calls = self.check(["gfx1032", "gfx1031"], ["0000:02:00.0", "0000:03:00.0"], ["hip-pci:0000:02:00.0"])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["devices"][0]["status"], "targetMissing")
        self.assertEqual(calls, [])

    def test_pci_selection_survives_runtime_index_reordering(self):
        report, calls = self.check(["gfx1031", "gfx1032"], ["0000:03:00.0", "0000:02:00.0"], ["hip-pci:0000:03:00.0"])
        self.assertEqual(report["status"], "passed")
        self.assertEqual(calls, [0])
        self.assertEqual(report["devices"][0]["stableId"], "hip-pci:0000:03:00.0")

    def test_missing_selected_card_is_not_replaced_by_another_card(self):
        report, calls = self.check(["gfx1031"], ["0000:03:00.0"], ["hip-pci:0000:02:00.0"])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["reason"], "selectedDeviceMappingUnavailable")
        self.assertEqual(calls, [])

    def test_all_explicit_selections_must_pass(self):
        report, _ = self.check(["gfx1031", "gfx1032"], ["0000:02:00.0", "0000:03:00.0"],
                               ["hip-pci:0000:02:00.0", "hip-pci:0000:03:00.0"])
        self.assertEqual(report["status"], "failed")

    def test_pnp_mapping_is_allowed_only_for_a_confirmed_unique_adapter(self):
        selected = ["pnp:" + "a" * 24]
        missing, _ = self.check(["gfx1031"], ["0000:03:00.0"], selected)
        self.assertEqual(missing["status"], "failed")
        passed, _ = self.check(["gfx1031"], ["0000:03:00.0"], selected, unique_pnp=True)
        self.assertEqual(passed["status"], "passed")

    def test_invalid_session_selection_cannot_be_used_as_a_stable_identity(self):
        with self.assertRaises(ValueError), mock.patch.object(activation, "_child") as child:
            activation.verify_activation(Path(sys.executable), [], selected_device_ids=["session:0"])
        child.assert_not_called()


if __name__ == "__main__":
    unittest.main()
