import contextlib
import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
    from .test_worker_bootstrap import MANIFEST, probe_result
else:
    import _bootstrap as _worker_test_bootstrap
    from test_worker_bootstrap import MANIFEST, probe_result

import worker_bootstrap


class RuntimeProbeProjectionTests(unittest.TestCase):
    def setUp(self):
        self.python = Path(sys.executable)
        self.env_dir = self.python.parent
        self.state_path = self.env_dir / "pymss-runtime-state.json"
        self.enterContext(mock.patch.object(worker_bootstrap, "_manifest", return_value=MANIFEST))
        self.enterContext(mock.patch.object(worker_bootstrap, "runtime_lock", side_effect=lambda *a, **k: contextlib.nullcontext(True)))
        self.enterContext(mock.patch.object(worker_bootstrap, "_is_bundled_runtime_env", return_value=False))
        self.enterContext(mock.patch.object(worker_bootstrap, "_is_bundled_bootstrap_python", return_value=False))
        self.enterContext(mock.patch.object(worker_bootstrap, "_env_state_path", return_value=self.state_path))
        self.enterContext(mock.patch.object(worker_bootstrap, "_env_python_path", return_value=self.python))
        self.enterContext(mock.patch.object(worker_bootstrap, "_emit"))
        self.target = self.enterContext(mock.patch.object(worker_bootstrap, "_target_runtime_from_payload"))
        self.probe = self.enterContext(mock.patch.object(worker_bootstrap, "_probe_python_runtime"))
        self.write = self.enterContext(mock.patch.object(worker_bootstrap, "_atomic_write_json"))
        self.activate = self.enterContext(mock.patch.object(worker_bootstrap, "_write_runtime_state"))

    def test_discovery_and_uncached_activation_keep_the_same_field_contract(self):
        # Readiness is tested separately; these cases isolate the state projection.
        with mock.patch.object(worker_bootstrap, "_runtime_probe_is_ready", return_value=True):
            for graph in (True, False, None, "absent"):
                for satisfies_manifest in (True, False):
                    with self.subTest(graph=graph, satisfies_manifest=satisfies_manifest), \
                            mock.patch.object(worker_bootstrap, "_manifest_versions_are_satisfied", return_value=(satisfies_manifest, [])):
                        probed = {
                            **probe_result("cpu"), "pythonVersion": None, "torchVersion": None,
                            "acceleratorAvailable": False, "pymssVersion": None, "pymssCoreVersion": "0.1.6",
                            "pymssGraphAvailable": graph, "unrelatedProbeField": "not persisted",
                        }
                        if graph == "absent":
                            del probed["pymssGraphAvailable"]
                        original = copy.deepcopy(probed)
                        expected = {
                            "backend": "cpu", "manifestVersion": "test-1" if satisfies_manifest else None,
                            "stateVersion": worker_bootstrap.ENV_STATE_VERSION,
                            "pythonVersion": None, "torchVersion": None, "torchBackend": "cpu",
                            "acceleratorAvailable": False,
                            "packages": {name: True for name in MANIFEST["common"]},
                            "packageVersions": {name: "1.0.0" for name in MANIFEST["common"]},
                            "pymssVersion": None, "pymssCoreVersion": "0.1.6", "pymssGraphAvailable": graph is True,
                        }
                        self.probe.return_value = probed
                        self.write.reset_mock()
                        self.assertEqual(worker_bootstrap._discover_runtime_state("cpu", self.python, MANIFEST, persist=False), expected)
                        self.write.assert_not_called()
                        self.assertEqual(worker_bootstrap._discover_runtime_state("cpu", self.python, MANIFEST, persist=True), expected)
                        self.write.assert_called_once_with(self.state_path, expected)

                        self.target.return_value = (None, self.env_dir, self.state_path, self.python)
                        self.write.reset_mock()
                        self.assertEqual(worker_bootstrap.cmd_activate_runtime({"backend": "cpu"}), 0)
                        self.write.assert_called_once_with(self.state_path, expected)
                        active = self.activate.call_args.args[0]
                        self.assertEqual({key: value for key, value in active.items() if key not in {"pythonPath", "logPath", "activatedAt"}}, expected)
                        self.assertEqual(active["pythonPath"], str(self.python))
                        self.assertEqual(active["logPath"], str(self.env_dir / "pymss-runtime-install.log"))
                        self.assertTrue(active["activatedAt"])
                        self.assertEqual(probed, original)

    def existing_state(self):
        return {
            **probe_result("cuda"), "backend": "cpu", "manifestVersion": "older",
            "stateVersion": worker_bootstrap.ENV_STATE_VERSION + 1,
            "pymssVersion": "2.1.3", "pymssCoreVersion": "0.1.6",
            "installedAt": "2026-09-01", "customField": {"preserved": True},
        }

    def partial_probe(self):
        return {
            "pythonVersion": None, "torchVersion": None, "torchBackend": None,
            "acceleratorAvailable": False, "packages": {}, "packageVersions": {},
            "pymssVersion": None, "pymssCoreVersion": None,
        }

    def test_cached_activation_preserves_old_fields_but_missing_graph_is_false(self):
        state = self.existing_state()
        original = copy.deepcopy(state)
        self.target.return_value = (state, self.env_dir, self.state_path, self.python)
        self.probe.return_value = self.partial_probe()
        with mock.patch.object(worker_bootstrap, "_runtime_probe_is_ready", return_value=True):
            self.assertEqual(worker_bootstrap.cmd_activate_runtime({"backend": "cpu"}), 0)
        expected = {**original, "torchVersion": None, "torchBackend": None, "acceleratorAvailable": False, "pymssGraphAvailable": False}
        self.write.assert_called_once_with(self.state_path, expected)
        self.assertEqual(state, original)

    def test_repair_preserves_missing_graph_but_honours_explicit_false(self):
        state = self.existing_state()
        original = copy.deepcopy(state)
        for graph in ("absent", False):
            with self.subTest(graph=graph):
                self.probe.return_value = self.partial_probe()
                if graph is False:
                    self.probe.return_value["pymssGraphAvailable"] = False
                expected = {
                    **original, "stateVersion": worker_bootstrap.ENV_STATE_VERSION,
                    "torchVersion": None, "torchBackend": None, "acceleratorAvailable": False,
                    "pymssGraphAvailable": graph != False,
                }
                self.assertEqual(worker_bootstrap._repaired_env_state("cpu", state, persist=False), expected)
                self.write.assert_not_called()
                self.assertEqual(state, original)


if __name__ == "__main__":
    unittest.main()
