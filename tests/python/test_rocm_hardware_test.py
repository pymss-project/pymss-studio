from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave


REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("rocm_hardware_collector", REPO_ROOT / "scripts/rocm-hardware-test.py")
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


class RocmHardwareCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rocm-collector-test-")
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.package = self.root / "package"
        self.package.mkdir()
        self.output = self.root / "results"
        self.redactor = collector.Redactor(self.package, self.output)

    def make_runtime(self):
        runtime = self.package / "python-runtime/runtime-envs/rocm"
        (runtime / "Scripts").mkdir(parents=True)
        (runtime / "Scripts/python.exe").touch()
        (runtime / "pyvenv.cfg").write_text("home = stale", encoding="utf-8")
        (self.package / "python-runtime/python.exe").touch()
        (runtime / "native/bin").mkdir(parents=True)
        state = {"backend": "rocm", "profileId": "win-rocm721",
                 "nativeLibraryDirs": ["native/bin"], "nativeToolDirs": ["native/bin"]}
        (runtime / "pymss-runtime-state.json").write_text(json.dumps(state), encoding="utf-8")
        return runtime, state

    def test_runtime_is_package_local_and_requires_rocm(self):
        runtime, state = self.make_runtime()
        self.assertEqual(collector.locate_runtime(self.package)[2], runtime)
        state["backend"] = "cuda"
        (runtime / "pymss-runtime-state.json").write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not recorded as ROCm"):
            collector.locate_runtime(self.package)

    def test_native_paths_reject_traversal_absolute_and_foreign_links(self):
        runtime, _state = self.make_runtime()
        for value in ("../outside", "C:/outside", "/outside", "native\\bin", "missing"):
            with self.subTest(value=value), self.assertRaises((ValueError, FileNotFoundError)):
                collector.confined_path(runtime, value)
        self.assertEqual(collector.confined_path(runtime, "native/bin"), runtime / "native/bin")
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (runtime / "foreign").symlink_to(outside, target_is_directory=True)
        except OSError:
            return
        with self.assertRaisesRegex(ValueError, "escapes"):
            collector.confined_path(runtime, "foreign")

    def test_environment_removes_overrides_and_isolates_user_writes(self):
        runtime, state = self.make_runtime()
        bootstrap = self.package / "python-runtime/python.exe"
        self.output.mkdir()
        with patch.dict(os.environ, {"PYMSS_MODEL_DIR": "do-not-touch", "PYTHONPATH": "foreign",
                                     "HIP_VISIBLE_DEVICES": "7", "HTTPS_PROXY": "https://user:secret@example.invalid"}):
            environment, overrides = collector.isolated_environment(self.package, self.output, bootstrap, runtime, state)
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("HIP_VISIBLE_DEVICES", environment)
        self.assertNotIn("HTTPS_PROXY", environment)
        self.assertEqual(environment["PYMSS_MODEL_DIR"], str(self.output / "data/models"))
        self.assertEqual(environment["PYMSS_STUDIO_RUNTIME_ENVS_DIR"], str(self.output / "data/runtime-envs"))
        self.assertEqual(environment["PYMSS_STUDIO_BUNDLED_RUNTIME_ENVS_DIR"], str(runtime.parent))
        self.assertEqual(environment["HF_HUB_OFFLINE"], "1")
        self.assertEqual(overrides["PYMSS_STUDIO_BOOTSTRAP_PYTHON"], str(bootstrap))
        self.assertTrue((self.output / "data/temp").is_dir())

    def test_redaction_keeps_versions_but_removes_credentials_and_user_paths(self):
        value = self.redactor.value({"path": str(self.package / "python-runtime/python.exe"),
            "detail": "token=abcdef", "authorization": "Bearer abc",
            "url": "https://bob:secret@example.invalid/torch.whl?X-Amz-Signature=private",
            "version": "2.9.1+rocm7.2.1"})
        encoded = json.dumps(value)
        for secret in ("abcdef", "Bearer abc", "bob", "secret", "private", str(self.package)):
            self.assertNotIn(secret, encoded)
        self.assertIn("${PACKAGE_ROOT}", encoded)
        self.assertEqual(value["version"], "2.9.1+rocm7.2.1")

    def test_exit_zero_is_not_a_gpu_pass(self):
        report = {"status": "unavailable", "deviceCapabilities": []}
        record = {"status": "pass", "events": [{"type": "runtime_gpu_probe_finished", "payload": report}]}
        self.assertEqual(collector.assess_probe(record, False)["status"], "blocked")
        self.assertEqual(collector.assess_probe({"status": "pass", "events": []}, False)["status"], "fail")

    def runtime_record(self):
        return {"status": "pass", "events": [{"type": "runtime_info", "payload": {
            "packages": {"pymss": True, "pymss-core": True, "numpy": True}, "torchBackend": "rocm",
            "ready": True, "acceleratorAvailable": True, "liveProbeError": None,
            "activationValidation": {"status": "passed", "devices": [{"index": 0, "status": "passed"}]},
            "seedPreparationRequired": True}}]}

    def test_runtime_info_requires_live_minimum_gpu_gate_even_when_dependencies_import(self):
        record = self.runtime_record()
        report = record["events"][0]["payload"]
        self.assertEqual(collector.assess_runtime_info(record)["status"], "pass")
        self.assertTrue(collector.assess_runtime_info(record)["seedPreparationRequired"])
        for validation in ({"status": "failed", "reason": "deviceUnavailable"}, {"status": "notRun"}, None):
            with self.subTest(validation=validation):
                report["activationValidation"] = validation
                self.assertEqual(collector.assess_runtime_info(record)["status"], "blocked")
        self.assertEqual(collector.assess_runtime_info({"status": "pass", "events": []})["status"], "fail")

    def test_runtime_info_distinguishes_import_failure_from_gpu_block_and_rejects_contradictions(self):
        for field, value in (("liveProbeError", {"code": "IMPORT_FAILED"}),
                             ("packages", {"pymss": False}), ("packages", {}), ("packages", {"mlx": False}),
                             ("torchBackend", "cpu"), ("acceleratorAvailable", False), ("ready", False)):
            with self.subTest(field=field, value=value):
                record = self.runtime_record()
                record["events"][0]["payload"][field] = value
                self.assertEqual(collector.assess_runtime_info(record)["status"], "fail")
        record = self.runtime_record()
        record["status"] = "fail"
        self.assertEqual(collector.assess_runtime_info(record)["status"], "fail")

    def inventory_record(self):
        return {"status": "pass", "events": [{"type": "runtime_hardware_inventory", "payload": {
            "devices": [{"vendor": "amd", "id": "hip-pci:0000:03:00.0", "gfx": "gfx1031",
                         "architectureConfidence": "confirmed"}], "detectorStatus": "ready", "errors": [],
            "profiles": [{"id": "win-rocm721", "releaseState": "legacy", "installationEnabled": True},
                         {"id": "win-rocm100-multiarch", "releaseState": "disabled", "installationEnabled": False}]}}]}

    def test_inventory_captures_stable_ids_profile_states_and_native_detector_status(self):
        record = self.inventory_record()
        result = collector.assess_inventory(record)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["stableDeviceIds"], ["hip-pci:0000:03:00.0"])
        self.assertEqual(result["detectorStatus"], "ready")
        self.assertEqual(result["profiles"][1]["releaseState"], "disabled")
        record["events"][0]["payload"]["devices"][0]["id"] = "session:0"
        self.assertEqual(collector.assess_inventory(record)["status"], "blocked")

    def test_inventory_distinguishes_unavailable_hardware_from_detector_or_policy_failure(self):
        record = self.inventory_record()
        report = record["events"][0]["payload"]
        for status in ("device_unavailable", "unavailable"):
            report["detectorStatus"] = status
            self.assertEqual(collector.assess_inventory(record)["status"], "blocked")
        report["detectorStatus"] = "failed"
        self.assertEqual(collector.assess_inventory(record)["status"], "fail")
        report["detectorStatus"] = "ready"
        report["profiles"][1]["installationEnabled"] = True
        self.assertEqual(collector.assess_inventory(record)["status"], "fail")
        self.assertEqual(collector.assess_inventory({"status": "pass", "events": []})["status"], "fail")

    def test_disabled_rocm10_plan_checks_policy_without_installing(self):
        report = {"backend": "rocm", "profileId": "win-rocm100-multiarch",
                  "installationEnabled": False, "releaseState": "disabled"}
        record = {"status": "pass", "events": [{"type": "runtime_install_plan", "payload": report}]}
        self.assertEqual(collector.assess_profile_plan(record)["status"], "pass")
        for field, value in (("installationEnabled", True), ("installationEnabled", 0),
                             ("releaseState", "preview"), ("profileId", "win-rocm721")):
            with self.subTest(field=field, value=value):
                altered = {**report, field: value}
                record["events"][0]["payload"] = altered
                self.assertEqual(collector.assess_profile_plan(record)["status"], "fail")

    def test_enabled_rocm10_preview_requires_explicit_acceptance_in_the_plan(self):
        record = self.inventory_record()
        record["events"][0]["payload"]["profiles"][1].update(releaseState="preview", installationEnabled=True)
        self.assertEqual(collector.assess_inventory(record)["status"], "pass")
        report = {"backend": "rocm", "profileId": "win-rocm100-multiarch", "installationEnabled": True,
                  "releaseState": "preview", "requiresPreviewAcceptance": True}
        record = {"status": "pass", "events": [{"type": "runtime_install_plan", "payload": report}]}
        self.assertEqual(collector.assess_profile_plan(record)["status"], "pass")
        for changed in ({"requiresPreviewAcceptance": False}, {"installationEnabled": False}, {"releaseState": "disabled"}):
            with self.subTest(changed=changed):
                record["events"][0]["payload"] = {**report, **changed}
                self.assertEqual(collector.assess_profile_plan(record)["status"], "fail")

    @unittest.skipUnless(os.name == "nt", "Portable package collection requires Windows")
    def test_collection_routes_inventory_to_bootstrap_without_preparing_or_activating_seed(self):
        runtime, _state = self.make_runtime()
        (self.package / "python").mkdir()
        (self.package / "python/worker.py").touch()
        requested = []
        runtime_record = self.runtime_record()
        runtime_record["events"][0]["payload"]["activationValidation"] = {"status": "failed"}

        class FakeRunner:
            def __init__(_self, *_args):
                _self.records = []

            def run(_self, name, python, request, _timeout):
                requested.append((name, python, request))
                if name == "machine":
                    return {"status": "pass", "events": [{"gpu": [], "os": {}}]}
                if name == "hardware-inventory":
                    return self.inventory_record()
                if name == "runtime-info":
                    return runtime_record
                if name == "dependencies":
                    return {"status": "pass", "events": [{"packages": []}]}
                if name.startswith("probe-"):
                    return self.probe_record(True)
                payload = {"backend": "rocm", "profileId": "win-rocm721"}
                event_type = "runtime_install_plan"
                if name == "rocm100-profile-plan":
                    payload.update({"profileId": "win-rocm100-multiarch", "releaseState": "disabled", "installationEnabled": False})
                elif name == "env-info":
                    event_type = "env_info"
                    payload = {"pymssAvailable": True, "torchAvailable": True, "torchBackend": "rocm",
                               "cudaAvailable": True, "cudaDeviceCount": 1}
                elif name == "list-models":
                    event_type, payload = "models", {"count": 1}
                return {"status": "pass", "events": [{"type": event_type, "payload": payload}]}

        with patch.object(collector, "Runner", FakeRunner), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(collector.collect(self.package, self.output, 5, 10), 2)
        routed = {name: (python, request) for name, python, request in requested}
        for name in ("hardware-inventory", "rocm100-profile-plan"):
            self.assertEqual(routed[name][0], self.package / "python-runtime/python.exe")
        profile_request = routed["rocm100-profile-plan"][1]
        self.assertEqual(json.loads(profile_request["args"][-1])["targets"], ["gfx1031"])
        self.assertEqual(routed["runtime-info"][0], runtime / "Scripts/python.exe")
        commands = {request["args"][0] for _name, _python, request in requested if request["kind"] == "script"}
        self.assertEqual(commands, {"runtime_hardware_inventory", "runtime_info", "runtime_install_plan",
                                    "env_info", "list_models", "runtime_gpu_probe"})
        for name in ("hardware-inventory.json", "runtime-info.json", "rocm100-profile-plan.json"):
            self.assertTrue((self.output / name).is_file())
        summary = json.loads((self.output / "summary.json").read_text(encoding="utf-8"))
        seed = next(check for check in summary["checks"] if check["id"] == "managed-seed-preparation")
        self.assertEqual(seed["status"], "skipped")
        self.assertTrue(seed["seedPreparationRequired"])
        self.assertFalse((self.output / "data/runtime-envs/active-runtime.json").exists())
        self.assertFalse((self.output / "data/runtime-envs/rocm").exists())

    def probe_record(self, fp16=False):
        checks = [{"id": check, "precision": precision, "status": "passed"} for check in collector.CHECK_IDS
                  for precision in (["fp32", "fp16"] if fp16 else ["fp32"])]
        report = {"status": "completed", "deviceCapabilities": [{"index": 0, "modes": [
            {"mode": mode, "status": "completed", "basicAvailable": True, "checks": checks[:]}
            for mode in ("miopen", "nativeHip")]}]}
        return {"status": "pass", "events": [{"type": "runtime_gpu_probe_finished", "payload": report}]}

    def test_probe_requires_every_mode_operator_and_requested_precision(self):
        record = self.probe_record()
        self.assertEqual(collector.assess_probe(record, False)["status"], "pass")
        self.assertEqual(collector.assess_probe(record, True)["status"], "fail")
        record = self.probe_record(True)
        self.assertEqual(collector.assess_probe(record, True)["status"], "pass")
        record["events"][0]["payload"]["deviceCapabilities"][0]["modes"][1]["checks"].pop()
        self.assertEqual(collector.assess_probe(record, True)["status"], "fail")

    def test_fp16_failure_never_becomes_pass_from_basic_flag(self):
        record = self.probe_record(True)
        record["events"][0]["payload"]["deviceCapabilities"][0]["modes"][0]["checks"][0]["status"] = "failed"
        self.assertEqual(collector.assess_probe(record, True)["status"], "fail")

    def make_runner(self):
        self.output.mkdir()
        (self.output / "commands").mkdir()
        return collector.Runner(REPO_ROOT, self.output, dict(os.environ), self.redactor)

    def test_real_subprocess_protocol_and_stderr_are_bounded_and_redacted(self):
        runner = self.make_runner()
        code = "import json,sys; print(json.dumps({'type':'env_info','payload':{'torchVersion':'2.9.1'}})); print('token=private', file=sys.stderr)"
        with contextlib.redirect_stdout(io.StringIO()):
            record = runner.run("protocol", Path(sys.executable), {"kind": "code", "code": code}, 15)
        self.assertEqual(record["status"], "pass", record)
        self.assertTrue(record["treeCleanupConfirmed"])
        self.assertEqual(collector.payload_of(record, "env_info"), {"torchVersion": "2.9.1"})
        self.assertNotIn("private", (self.output / record["stderr"]).read_text(encoding="utf-8"))

    def test_real_timeout_confirms_process_tree_cleanup(self):
        runner = self.make_runner()
        code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)"
        with contextlib.redirect_stdout(io.StringIO()):
            record = runner.run("timeout", Path(sys.executable), {"kind": "code", "code": code}, 1)
        self.assertEqual(record["status"], "fail")
        self.assertTrue(record["timedOut"])
        self.assertTrue(record["treeCleanupConfirmed"], record)
        self.assertFalse(runner.cleanup_failed)

    def test_synthetic_audio_has_expected_channels_duration_and_finite_range(self):
        self.output.mkdir()
        report = collector.synthetic_audio(self.output / "synthetic-stereo.wav")
        self.assertEqual(report["seconds"], 8)
        with wave.open(str(self.output / report["path"]), "rb") as file:
            self.assertEqual(file.getnchannels(), 2)
            self.assertEqual(file.getframerate(), 44100)
            self.assertEqual(file.getnframes(), 44100 * 8)
        self.assertEqual(len(report["sha256"]), 64)

    def test_existing_result_directory_is_refused(self):
        self.output.mkdir()
        sentinel = self.output / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            collector.main(["--package-root", str(self.package), "--output", str(self.output)])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_incomplete_package_still_writes_blocked_summary(self):
        with contextlib.redirect_stdout(io.StringIO()):
            code = collector.collect(self.package, self.output, 5, 10)
        self.assertEqual(code, 2)
        summary = json.loads((self.output / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "blocked")
        self.assertFalse(summary["hardwareValidationComplete"])
        self.assertEqual(summary["releaseReadiness"], "unverified")
        self.assertNotIn(str(self.package), json.dumps(summary))


if __name__ == "__main__":
    unittest.main()
