from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import venv
import zipfile
from pathlib import Path
from unittest.mock import patch

from worker_runtime_artifacts import (
    artifact_lock_id, build_locked_requirements, load_artifact_lock,
    resolve_artifact_plan, validate_artifact_lock, verify_artifact_file,
    verify_installed_coverage,
)

ROOT = Path(__file__).resolve().parents[2]


def wheel(directory, name, requires=(), *, version="1.0"):
    package = name.replace("-", "_")
    info = package + "-" + version + ".dist-info"
    path = directory / (package + "-" + version + "-py3-none-any.whl")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(package + "/__init__.py", "")
        archive.writestr(info + "/METADATA", "Metadata-Version: 2.1\nName: " + name + "\nVersion: " + version + "\n"
                         + "".join("Requires-Dist: " + req + "\n" for req in requires))
        archive.writestr(info + "/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(info + "/RECORD", "")
    return path


class RuntimeArtifactTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.artifacts = []
        for name, deps, targets in (
            ("fixture-common", [], []), ("fixture-host", ["fixture-common"], []),
            ("fixture-device-a", ["fixture-host"], ["gfx1031"]),
            ("fixture-device-b", ["fixture-host"], ["gfx1100"]),
        ):
            path = wheel(self.directory, name, [d + "==1.0" for d in deps])
            identity = {"url": "https://example.invalid/releases/v1/" + path.name,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
            self.artifacts.append({"name": name, "version": "1.0", "filename": path.name, **identity,
                "tags": ["py3-none-any"], "role": "device" if targets else "host", "targets": targets,
                "dependencies": deps, "provenance": {"kind": "upstream-wheel", "source": identity.copy()}})
        self.lock = {"schemaVersion": 1, "backend": "rocm", "profileId": "fixture", "profileRevision": 2,
            "platform": "win32", "machine": "amd64", "python": "3.12", "pythonAbi": "cp312",
            "platformTag": "win_amd64", "channel": "preview", "targets": ["gfx1031", "gfx1100"],
            "hostArtifacts": ["fixture-common", "fixture-host"],
            "targetArtifacts": {"gfx1031": ["fixture-device-a"], "gfx1100": ["fixture-device-b"]},
            "artifacts": sorted(self.artifacts, key=lambda item: item["name"])}
        self.seal()

    def seal(self):
        self.lock["lockId"] = artifact_lock_id(self.lock)

    def test_target_selection_expands_all_to_exact_coverage(self):
        plan = resolve_artifact_plan(self.lock, "fixture", ["all"], profile_revision=2, python_abi="cp312", platform_tag="win_amd64")
        self.assertEqual(plan["resolvedTargets"], ["gfx1031", "gfx1100"])
        self.assertEqual(plan["devicePackageNames"], ["fixture-device-a", "fixture-device-b"])
        self.assertEqual(len(plan["artifacts"]), 4)
        self.assertNotIn("all", plan["resolvedTargets"])

    def test_one_target_installs_complete_selected_binary_closure(self):
        plan = resolve_artifact_plan(self.lock, "fixture", ["gfx1031"])
        self.assertEqual(set(plan["resolvedAcceleratorPackages"]), {"fixture-common", "fixture-host", "fixture-device-a"})
        self.assertEqual(plan["packageRoles"]["fixture-device-a"], "device")

    def test_host_identity_is_stable_when_adding_target_or_changing_mirror(self):
        first = resolve_artifact_plan(self.lock, "fixture", ["gfx1031"])
        both = resolve_artifact_plan(self.lock, "fixture", ["gfx1031", "gfx1100"])
        self.assertEqual(first["hostLockId"], both["hostLockId"])
        for artifact in self.lock["artifacts"]:
            artifact["url"] = artifact["url"].replace("example.invalid", "mirror.invalid")
        self.seal()
        mirrored = resolve_artifact_plan(self.lock, "fixture", ["gfx1031"])
        self.assertEqual(first["hostLockId"], mirrored["hostLockId"])
        self.assertNotEqual(first["lockId"], mirrored["lockId"])

    def test_same_version_changed_host_bytes_changes_host_identity(self):
        first = resolve_artifact_plan(self.lock, "fixture", ["gfx1031"])
        artifact = next(a for a in self.lock["artifacts"] if a["name"] == "fixture-host")
        artifact["sha256"] = "a" * 64
        artifact["provenance"]["source"]["sha256"] = "a" * 64
        self.seal()
        self.assertNotEqual(first["hostLockId"], resolve_artifact_plan(self.lock, "fixture", ["gfx1031"])["hostLockId"])

    def test_lock_content_tampering_is_rejected(self):
        self.lock["channel"] = "stable"
        with self.assertRaisesRegex(ValueError, "lockId"):
            validate_artifact_lock(self.lock)

    def test_invalid_selection_identity_or_abi_fails_before_installation(self):
        for profile, targets, kwargs in (
            ("wrong", [], {}), ("fixture", ["gfx9999"], {}), ("fixture", ["all", "gfx1031"], {}),
            ("fixture", [], {"profile_revision": 1}), ("fixture", [], {"python_abi": "cp313"}),
            ("fixture", [], {"platform_tag": "linux_x86_64"}),
        ):
            with self.subTest(profile=profile, targets=targets, kwargs=kwargs), self.assertRaises(ValueError):
                resolve_artifact_plan(self.lock, profile, targets, **kwargs)

    def test_incomplete_dependency_closure_is_rejected(self):
        self.lock["artifacts"][0]["dependencies"] = ["not-present"]
        self.seal()
        with self.assertRaisesRegex(ValueError, "closure"):
            validate_artifact_lock(self.lock)

    def test_host_and_device_combination_is_rejected(self):
        self.lock["targetArtifacts"]["gfx1031"].append("fixture-host")
        self.lock["targetArtifacts"]["gfx1031"].sort()
        self.seal()
        with self.assertRaisesRegex(ValueError, "both host and device"):
            validate_artifact_lock(self.lock)

    def test_bad_artifact_fields_are_rejected(self):
        for key, value in (("url", "https://user:secret@example.invalid/a.whl"),
                           ("url", "https://example.invalid/a.whl?token=secret"),
                           ("url", "http://example.invalid/a.whl"),
                           ("filename", "../escape.whl"), ("sha256", "a"),
                           ("bytes", True), ("bytes", 0), ("role", "mixed"),
                           ("tags", ["cp313-cp313-win_amd64"]), ("version", "2.0")):
            with self.subTest(key=key, value=value):
                lock = copy.deepcopy(self.lock)
                lock["artifacts"][0][key] = value
                lock["lockId"] = artifact_lock_id(lock)
                with self.assertRaises(ValueError):
                    validate_artifact_lock(lock)

    def test_source_archive_cannot_be_installed_by_client(self):
        self.lock["artifacts"][0]["filename"] = "rocm-1.0.tar.gz"
        self.seal()
        with self.assertRaises(ValueError):
            validate_artifact_lock(self.lock)

    def test_derived_wheel_requires_exact_source_and_binary_build_inputs(self):
        artifact = self.lock["artifacts"][0]
        artifact["provenance"] = {"kind": "derived-wheel", "source": {
            "url": "https://repo.radeon.com/rocm-1.0.tar.gz", "sha256": "a" * 64, "bytes": 32},
            "builder": "pip wheel --no-index --no-deps --no-build-isolation", "buildInputs": []}
        self.seal()
        with self.assertRaisesRegex(ValueError, "build inputs"):
            validate_artifact_lock(self.lock)
        artifact["provenance"]["buildInputs"] = [{"name": "setuptools", "version": "80.9.0",
            "filename": "setuptools-80.9.0-py3-none-any.whl", "url": "https://files.pythonhosted.org/setuptools-80.9.0-py3-none-any.whl",
            "sha256": "b" * 64, "bytes": 512}]
        self.seal()
        self.assertEqual(validate_artifact_lock(self.lock)["lockId"], self.lock["lockId"])

    def test_local_wheelhouse_rehashes_cache_on_every_use(self):
        plan = resolve_artifact_plan(self.lock, "fixture", ["gfx1031"])
        text = build_locked_requirements(plan, wheelhouse=self.directory)
        self.assertIn("file:///", text)
        self.assertEqual(text.count("--hash=sha256:"), 3)
        artifact = plan["artifacts"][0]
        path = self.directory / artifact["filename"]
        content = bytearray(path.read_bytes())
        content[-1] ^= 1
        path.write_bytes(content)
        with self.assertRaisesRegex(RuntimeError, "SHA-256"):
            build_locked_requirements(plan, wheelhouse=self.directory)

    def test_byte_size_mismatch_is_rejected(self):
        artifact = self.lock["artifacts"][0]
        path = self.directory / artifact["filename"]
        path.write_bytes(b"corrupt")
        with self.assertRaisesRegex(RuntimeError, "size"):
            verify_artifact_file(path, artifact)

    def test_selected_requirements_cannot_escape_plan(self):
        plan = resolve_artifact_plan(self.lock, "fixture", ["gfx1031"])
        with self.assertRaisesRegex(ValueError, "not part"):
            build_locked_requirements(plan, artifacts=[next(a for a in self.artifacts if a["name"] == "fixture-device-b")])

    def test_coverage_does_not_claim_gpu_or_model_validation(self):
        plan = resolve_artifact_plan(self.lock, "fixture", ["gfx1031"])
        coverage = verify_installed_coverage(plan, plan["resolvedAcceleratorPackages"])
        self.assertEqual(coverage["installedTargets"], ["gfx1031"])
        self.assertEqual(coverage["hardwareValidation"], "not_run")
        self.assertNotIn("acceleratorAvailable", coverage)
        with self.assertRaises(RuntimeError):
            verify_installed_coverage(plan, {"fixture-common": "1.0"})

    def test_json_reader_rejects_duplicate_fields(self):
        path = self.directory / "lock.json"
        path.write_text('{"schemaVersion":1,"schemaVersion":1}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            load_artifact_lock(path)

    def test_package_build_rejects_wrong_revision_abi_or_recipe_before_touching_existing_runtime(self):
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if powershell is None:
            self.skipTest("PowerShell is required")
        for mismatch in ("revision", "abi", "recipe"):
            with self.subTest(mismatch=mismatch):
                lock = copy.deepcopy(self.lock)
                lock.update({"profileId": "win-rocm721", "profileRevision": 2 if mismatch == "revision" else 1,
                             "channel": "legacy"})
                if mismatch == "abi":
                    lock.update({"python": "3.13", "pythonAbi": "cp313"})
                lock["lockId"] = artifact_lock_id(lock)
                lock_file = self.directory / (mismatch + ".json")
                lock_file.write_text(json.dumps(lock), encoding="utf-8")
                runtime = self.directory / ("preserved-" + mismatch)
                runtime.mkdir()
                marker = runtime / "user-runtime.txt"
                marker.write_text("preserved", encoding="utf-8")
                result = subprocess.run([powershell, "-NoProfile", "-File", str(ROOT / "scripts/prepare-python-runtime.ps1"),
                    "-Variant", "rocm", "-Python", sys.executable, "-RuntimeDir", str(runtime),
                    "-RocmProfile", "win-rocm721", "-RuntimeArtifactLock", str(lock_file)], capture_output=True, text=True, encoding="utf-8", errors="replace")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(marker.read_text(encoding="utf-8"), "preserved")

    def test_package_build_rehashes_local_wheelhouse_before_touching_existing_runtime(self):
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if powershell is None:
            self.skipTest("PowerShell is required")
        manifest = json.loads((ROOT / "python/runtime-manifest.json").read_text(encoding="utf-8"))
        profile = manifest["backends"]["rocm"]["platformOverrides"]["win32"]["profiles"]["win-rocm721"]
        for fault in ("missing", "truncated", "changed-content"):
            with self.subTest(fault=fault):
                wheelhouse = self.directory / fault
                wheelhouse.mkdir()
                artifacts = []
                for name, version in sorted(profile["packages"].items()):
                    path = wheel(wheelhouse, name, version=version)
                    identity = {"url": "https://example.invalid/releases/v1/" + path.name,
                                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
                    artifacts.append({"name": name, "version": version, "filename": path.name, **identity,
                        "tags": ["py3-none-any"], "role": "host", "targets": [], "dependencies": [],
                        "provenance": {"kind": "upstream-wheel", "source": identity.copy()}})
                lock = {**self.lock, "profileId": profile["id"], "profileRevision": profile["revision"],
                        "channel": "legacy", "targets": [], "hostArtifacts": sorted(profile["packages"]),
                        "targetArtifacts": {}, "artifacts": artifacts}
                lock["lockId"] = artifact_lock_id(lock)
                lock_file = wheelhouse / "lock.json"
                lock_file.write_text(json.dumps(lock), encoding="utf-8")
                broken = wheelhouse / artifacts[-1]["filename"]
                if fault == "missing":
                    broken.unlink()
                elif fault == "truncated":
                    broken.write_bytes(b"broken")
                else:
                    content = bytearray(broken.read_bytes())
                    content[-1] ^= 1
                    broken.write_bytes(content)
                runtime = self.directory / ("existing-" + fault)
                runtime.mkdir()
                marker = runtime / "user-runtime.txt"
                marker.write_text("preserved", encoding="utf-8")
                result = subprocess.run([powershell, "-NoProfile", "-File", str(ROOT / "scripts/prepare-python-runtime.ps1"),
                    "-Variant", "rocm", "-Python", sys.executable, "-RuntimeDir", str(runtime),
                    "-RocmProfile", "win-rocm721", "-RuntimeArtifactLock", str(lock_file),
                    "-RuntimeArtifactWheelhouse", str(wheelhouse)], capture_output=True, text=True, encoding="utf-8", errors="replace")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(artifacts[-1]["filename"], result.stderr)
                self.assertIn("SHA-256 mismatch" if fault == "changed-content" else "incorrect size", result.stderr)
                self.assertEqual(marker.read_text(encoding="utf-8"), "preserved")
                self.assertNotIn("Copying", result.stdout)

    def test_real_offline_pip_installs_only_selected_hashed_wheels(self):
        plan = resolve_artifact_plan(self.lock, "fixture", ["gfx1031"])
        requirements = self.directory / "locked.txt"
        requirements.write_text(build_locked_requirements(plan, wheelhouse=self.directory), encoding="utf-8")
        environment = self.directory / "environment with spaces"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        lock_file = self.directory / "lock.json"
        lock_file.write_text(json.dumps(self.lock), encoding="utf-8")
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if powershell:
            prepare = (ROOT / "scripts/prepare-python-runtime.ps1").read_text(encoding="utf-8")
            functions = [re.search(r"^function " + name + r" \{\n.*?^\}", prepare, re.M | re.S).group()
                         for name in ("Invoke-NativeChecked", "Install-LockedRocmRuntime")]
            script = self.directory / "install.ps1"
            script.write_text("$ErrorActionPreference='Stop'\n" + "\n".join(functions)
                + f"\n$root='{ROOT}'\n$Python='{sys.executable}'\n$RuntimeArtifactLock='{lock_file}'\n"
                + f"$RuntimeArtifactWheelhouse='{self.directory}'\n$RocmTargets=@('gfx1031')\n"
                + "$rocmPlan=@{profileId='fixture';profileRevision=2}\n"
                + f"Install-LockedRocmRuntime -PythonPath '{python}' -EnvironmentDir '{environment}'\n", encoding="utf-8-sig")
            command = [powershell, "-NoProfile", "-File", str(script)]
        else:
            command = [sys.executable, str(ROOT / "scripts/runtime-artifacts.py"), "install", "--lock", str(lock_file),
                       "--profile", "fixture", "--targets", "gfx1031", "--python", str(python), "--wheelhouse", str(self.directory)]
        external = self.directory / "untrusted Python overlay"
        external.mkdir()
        (external / "pip.py").write_text("raise RuntimeError('External Python path was used')\n", encoding="utf-8")
        external_info = external / "external_injected-1.0.dist-info"
        external_info.mkdir()
        (external_info / "METADATA").write_text("Metadata-Version: 2.1\nName: external-injected\nVersion: 1.0\nRequires-Dist: missing-external-dependency==1\n", encoding="utf-8")
        child_env = {**os.environ, "PYTHONPATH": str(external) + os.pathsep + os.environ.get("PYTHONPATH", ""),
                     "PYTHONHOME": str(external / "invalid-home"), "PYTHONUSERBASE": str(external / "userbase")}
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", env=child_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"hardwareValidation": "not_run"', result.stdout)

        check = subprocess.run([str(python), "-I", "-X", "utf8", "-m", "pip", "check"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(check.returncode, 0, check.stderr)
        result = subprocess.run([str(python), "-I", "-X", "utf8", "-c", "import json;from importlib import metadata;print(json.dumps({d.metadata['Name']:d.version for d in metadata.distributions()}))"],
            capture_output=True, text=True, check=True, encoding="utf-8", errors="replace")
        versions = json.loads(result.stdout)
        self.assertNotIn("fixture-device-b", versions)
        self.assertNotIn("external-injected", versions)
        verify_installed_coverage(plan, versions)
        self._assert_prepare_snapshot_matches_online_projection(environment, python, versions)

    def test_packaging_plan_defaults_to_all_targets_without_changing_online_target_requirements(self):
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell is unavailable")
        prepare = (ROOT / "scripts/prepare-python-runtime.ps1").read_text(encoding="utf-8")
        functions = [re.search(r"^function " + name + r" \{\n.*?^\}", prepare, re.M | re.S).group()
                     for name in ("Invoke-NativeChecked", "Get-RocmInstallPlan")]
        for profile, targets, expected, state in (("", [], ["all"], "preview"),
                                                  ("win-rocm100-multiarch", ["gfx1031"], ["gfx1031"], "preview"),
                                                  ("win-rocm721", [], [], "legacy")):
            with self.subTest(profile=profile, targets=targets):
                script = self.directory / "plan.ps1"
                target_args = ",".join("'" + target + "'" for target in targets)
                script.write_text("$ErrorActionPreference='Stop'\n" + "\n".join(functions)
                    + f"\n$root='{ROOT}'\n$Python='{sys.executable}'\n$manifestPath='{ROOT / 'python/runtime-manifest.json'}'\n"
                    + f"$RocmProfile='{profile}'\n$RocmTargets=@({target_args})\n"
                    + "Get-RocmInstallPlan | ConvertTo-Json -Depth 30 -Compress\n", encoding="utf-8-sig")
                result = subprocess.run([powershell, "-NoProfile", "-File", str(script)], capture_output=True,
                                        text=True, encoding="utf-8", errors="replace", timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                plan = json.loads(result.stdout.strip())
                self.assertEqual(plan["requestedTargets"], expected)
                self.assertEqual(plan["releaseState"], state)

    def _assert_prepare_snapshot_matches_online_projection(self, environment, python, versions):
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if powershell is None:
            return
        from worker_runtime_profiles import runtime_install_plan, runtime_state_from_artifact_plan
        profile = {"id": "fixture", "revision": 2, "platforms": ["win32"], "machine": ["amd64"],
            "python": "3.12", "installMode": "multiarch", "installationEnabled": True, "releaseState": "preview",
            "packages": {"torch": "1.0", "torchvision": "1.0", "torchaudio": "1.0", "rocm": "1.0"},
            "availableTargets": ["gfx1031", "gfx1100"], "torch": {}}
        manifest = {"backends": {"rocm": {"platforms": ["win32"], "defaultProfile": "fixture", "profiles": {"fixture": profile}}}}
        plan = runtime_install_plan(manifest, "rocm", "fixture", ["gfx1031"], platform_name="win32")
        expected_plan = {**plan, **resolve_artifact_plan(self.lock, "fixture", ["gfx1031"]), "resolutionStatus": "locked"}
        # The fixture tests only artifact identity/coverage, not ROCm versions.
        expected = runtime_state_from_artifact_plan(expected_plan, "3.12.0", versions)
        path = environment / "pymss-runtime-artifact-lock.json"
        path.write_text(json.dumps(self.lock), encoding="utf-8")
        plan_file = self.directory / "plan.json"
        plan_file.write_text(json.dumps(plan), encoding="utf-8")
        prepare = (ROOT / "scripts/prepare-python-runtime.ps1").read_text(encoding="utf-8")
        functions = [re.search(r"^function " + name + r" \{\n.*?^\}", prepare, re.M | re.S).group()
                     for name in ("Invoke-NativeChecked", "Save-AcceleratorSnapshot")]
        script = self.directory / "snapshot.ps1"
        script.write_text("$ErrorActionPreference='Stop'\n" + "\n".join(functions)
            + f"\n$root='{ROOT}'\n$manifestPath='{plan_file}'\n$manifestBackend='rocm'\n"
            + f"$rocmPlan=Get-Content -Raw '{plan_file}'|ConvertFrom-Json\n"
            + f"Save-AcceleratorSnapshot -PythonPath '{python}' -EnvironmentDir '{environment}' | Out-Null\n", encoding="utf-8-sig")
        result = subprocess.run([powershell, "-NoProfile", "-File", str(script)], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads((environment / "pymss-accelerator-state.json").read_text(encoding="utf-8"))
        for key in ("artifactLockId", "hostLockId", "baseFingerprint", "targetFingerprint", "requestedTargets",
                    "resolvedTargets", "installedTargets", "packageRoles", "acceleratorPackageArtifacts"):
            self.assertEqual(actual[key], expected[key], key)

    def test_source_conversion_records_exact_provenance_and_disables_dynamic_build_inputs(self):
        spec = importlib.util.spec_from_file_location("runtime_artifact_source_cli", ROOT / "scripts/runtime-artifacts.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source_dir = self.directory / "source"
        source_dir.mkdir()
        (source_dir / "pyproject.toml").write_text('[build-system]\nrequires=[]\nbuild-backend="fixture_backend"\nbackend-path=["."]\n', encoding="utf-8")
        backend = '''import os, zipfile
def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    filename = 'rocm-1.0-py3-none-any.whl'
    with zipfile.ZipFile(os.path.join(wheel_directory, filename), 'w') as archive:
        archive.writestr('rocm/__init__.py', '')
        archive.writestr('rocm-1.0.dist-info/METADATA', 'Metadata-Version: 2.1\\nName: rocm\\nVersion: 1.0\\n')
        archive.writestr('rocm-1.0.dist-info/WHEEL', 'Wheel-Version: 1.0\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n')
        archive.writestr('rocm-1.0.dist-info/RECORD', '')
    return filename
'''
        (source_dir / "fixture_backend.py").write_text(backend, encoding="utf-8")
        source = self.directory / "rocm-1.0.tar.gz"
        with tarfile.open(source, "w:gz") as archive:
            archive.add(source_dir, arcname="rocm-1.0")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        item = {"metadata": {"name": "rocm", "version": "1.0"}, "download_info": {
            "url": "https://repo.radeon.com/rocm-1.0.tar.gz", "archive_info": {"hashes": {"sha256": digest}}}}
        def fixture_source(identity, directory):
            identity["bytes"] = source.stat().st_size
            return source
        output = self.directory / "wheels"
        output.mkdir()
        build_inputs = [{key: self.artifacts[0][key] for key in ("name", "version", "filename", "url", "sha256", "bytes")}]
        with patch.object(module, "download_source", side_effect=fixture_source), patch.object(module, "run", wraps=module.run) as runner:
            result = module.materialize_item(item, Path(sys.executable), build_inputs, output, self.directory, "https://example.invalid/v1")
        self.assertEqual(result["provenance"]["kind"], "derived-wheel")
        self.assertEqual(result["provenance"]["source"]["sha256"], digest)
        self.assertEqual(result["provenance"]["buildInputs"], build_inputs)
        self.assertIn("--no-build-isolation", runner.call_args.args[0])
        self.assertIn("--no-index", runner.call_args.args[0])
        verify_artifact_file(output / result["filename"], result)
        self.assertIn("-1" + result["sha256"] + "-", result["filename"])
        installed = subprocess.run([sys.executable, "-I", "-X", "utf8", "-m", "pip", "--isolated", "install", "--no-index", "--no-deps",
            "--target", str(self.directory / "source-install"), str(output / result["filename"])], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(installed.returncode, 0, installed.stderr)

    def test_release_jobs_share_the_same_published_lock_and_no_torch_detector(self):
        import yaml

        workflow = (ROOT / ".github/workflows/release-windows.yml").read_text(encoding="utf-8")
        jobs = yaml.safe_load(workflow)["jobs"]
        for variant in ("cpu", "cuda", "online", "rocm"):
            job = jobs.get(f"windows-{variant}-prepare")
            self.assertIsNotNone(job, variant)
            needs = job["needs"]
            self.assertIn("windows-rocm-runtime-assets", [needs] if isinstance(needs, str) else needs, variant)
            resources = [step for step in job["steps"]
                         if step.get("uses", "").startswith("actions/download-artifact@")
                         and step.get("with", {}).get("name") == "rocm-runtime-resources"]
            self.assertEqual(len(resources), 1, variant)
            self.assertEqual(resources[0]["with"]["path"], "python", variant)
        self.assertIn("scripts/build-rocm-detector.py", workflow)
        self.assertIn("windows-x64-rocm-runtime-assets", workflow)
        self.assertIn("--wheelhouse release/rocm-runtime-assets/wheels", workflow)
        self.assertNotIn("$rocmSdkPackage", workflow)

    def test_legacy_release_resolves_the_default_multiarch_lock_with_explicit_targets(self):
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell is unavailable")
        import yaml

        workflow = yaml.safe_load((ROOT / ".github/workflows/release-windows.yml").read_text(encoding="utf-8"))
        steps = workflow["jobs"]["windows-rocm-runtime-assets"]["steps"]
        run = next(step["run"] for step in steps if step.get("name") == "Resolve complete binary closure")
        start = run.index("$defaultProfile =")
        end = run.index("New-Item -ItemType", start)
        branch = run[start:end].replace("${{ github.repository }}", "pymss-project/pymss-studio")
        for mode in ("multiarch", "legacy"):
            with self.subTest(mode=mode):
                manifest = {"backends": {"rocm": {"platformOverrides": {"win32": {
                    "defaultProfile": "default", "profiles": {"default": {
                        "revision": 1, "releaseState": "preview" if mode == "multiarch" else "legacy", "installMode": mode}}}}}}}
                manifest_file = self.directory / "workflow-manifest.json"
                manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
                script = self.directory / "workflow-default.ps1"
                script.write_text("$ErrorActionPreference='Stop'\n"
                    + "function python { $global:LASTEXITCODE=0; ConvertTo-Json -InputObject @($args) -Compress }\n"
                    + f"$manifest=Get-Content -Raw '{manifest_file}' | ConvertFrom-Json\n"
                    + "$env:PYMSS_ROCM_PROFILE='selected'\n$version='0.0.26'\n" + branch, encoding="utf-8-sig")
                result = subprocess.run([powershell, "-NoProfile", "-File", str(script)], capture_output=True,
                                        text=True, encoding="utf-8", errors="replace", timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                arguments = json.loads(result.stdout.strip())
                self.assertEqual(arguments[arguments.index("--profile") + 1], "default")
                if mode == "multiarch":
                    self.assertEqual(arguments[arguments.index("--targets") + 1], "all")
                else:
                    self.assertNotIn("--targets", arguments)

    def test_release_builder_source_path_disables_unlocked_build_dependencies(self):
        spec = importlib.util.spec_from_file_location("runtime_artifact_cli", ROOT / "scripts/runtime-artifacts.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = (ROOT / "scripts/runtime-artifacts.py").read_text(encoding="utf-8")
        self.assertIn('"--no-build-isolation"', source)
        self.assertIn('"--no-index", "--no-deps"', source)
        with self.assertRaises(RuntimeError):
            module.source_identity({"download_info": {"url": "https://untrusted.invalid/pkg.whl", "archive_info": {"hashes": {"sha256": "a" * 64}}}})

    def test_preview_release_snapshot_is_explicit_prerelease_only_and_does_not_edit_production_recipe(self):
        spec = importlib.util.spec_from_file_location("runtime_artifact_snapshot_cli", ROOT / "scripts/runtime-artifacts.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        manifest = json.loads((ROOT / "python/runtime-manifest.json").read_text(encoding="utf-8"))
        profile_id = "win-rocm100-multiarch"
        profiles = manifest["backends"]["rocm"]["platformOverrides"]["win32"]["profiles"]
        profiles[profile_id].update(installationEnabled=False, releaseState="disabled")
        original = copy.deepcopy(manifest)
        revision = profiles[profile_id]["revision"]
        result = module.distribution_manifest(manifest, profile_id, revision, "preview", "0.1.0-rocm-preview.1",
                                               enable_preview_candidate=True)
        preview = result["backends"]["rocm"]["platformOverrides"]["win32"]["profiles"][profile_id]
        self.assertTrue(preview["installationEnabled"])
        self.assertEqual(preview["releaseState"], "preview")
        windows = result["backends"]["rocm"]["platformOverrides"]["win32"]
        self.assertEqual(windows["profiles"]["win-rocm721"]["releaseState"], "legacy")
        self.assertEqual(len(result["distribution"]["sourceManifestSha256"]), 64)
        self.assertEqual(manifest, original)
        self.assertFalse(profiles[profile_id]["installationEnabled"])
        for channel, version, enabled in (("preview", "0.1.0", True), ("stable", "0.1.0-rc.1", True),
                                           ("preview", "0.1.0-rc.1", False)):
            with self.subTest(channel=channel, version=version, enabled=enabled), self.assertRaises(ValueError):
                module.distribution_manifest(manifest, profile_id, revision, channel, version,
                                             enable_preview_candidate=enabled)
        with self.assertRaises(ValueError):
            module.distribution_manifest(manifest, profile_id, revision + 1, "preview", "0.1.0-rc.1", enable_preview_candidate=True)

    def test_enabled_preview_uses_the_normal_release_snapshot_without_a_candidate_override(self):
        spec = importlib.util.spec_from_file_location("runtime_artifact_enabled_snapshot_cli", ROOT / "scripts/runtime-artifacts.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        manifest = json.loads((ROOT / "python/runtime-manifest.json").read_text(encoding="utf-8"))
        windows = manifest["backends"]["rocm"]["platformOverrides"]["win32"]
        profile = windows["profiles"][windows["defaultProfile"]]
        result = module.distribution_manifest(manifest, profile["id"], profile["revision"], "preview", "0.0.26")
        self.assertEqual(result["distribution"]["profileId"], "win-rocm100-multiarch")
        self.assertEqual(result["distribution"]["channel"], "preview")
        self.assertEqual(result["backends"]["rocm"]["platformOverrides"]["win32"]["profiles"][profile["id"]]["validatedTargets"], [])


if __name__ == "__main__":
    unittest.main()
