from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("release_core", ROOT / "scripts/resolve-release-core.py")
CORE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CORE)
MANIFEST = json.loads((ROOT / "python/runtime-manifest.json").read_text(encoding="utf-8"))


class ReleaseCoreResolutionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "manifest.json"
        self.manifest = copy.deepcopy(MANIFEST)
        # Offline wheel fixtures use fixed versions, independent of release pins.
        self.manifest["python"] = "3.12"
        self.manifest["common"].update({
            "pymss": "pymss[proxy]==2.1.8",
            "pymss-core": "pymss-core==0.1.11",
        })
        self.path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def _report(self, command, report):
        name = CORE.canonicalize_name(CORE.Requirement(command[-1]).name)
        selected = [item for item in report["install"]
                    if CORE.canonicalize_name(item["metadata"]["name"]) == name
                    or CORE.canonicalize_name(item["metadata"]["name"]) not in CORE.CORE_PACKAGES]
        Path(command[command.index("--report") + 1]).write_text(
            json.dumps({**report, "install": selected}), encoding="utf-8",
        )

    def test_default_core_requirements_preserve_the_shared_plugin_api_floor(self):
        self.assertEqual(MANIFEST["common"]["pymss"], "pymss[proxy]>=2.3.4")
        self.assertEqual(MANIFEST["common"]["pymss-core"], "pymss-core")
        lines = (ROOT / "python/requirements.txt").read_text(encoding="utf-8").splitlines()
        self.assertIn("pymss[proxy]>=2.3.4", lines)
        self.assertFalse(any(line.startswith("pymss-core") for line in lines))

    def test_ci_uses_an_immutable_official_core_snapshot(self):
        lines = (ROOT / "python/requirements-ci.txt").read_text(encoding="utf-8").splitlines()
        core = [line for line in lines if line.startswith("pymss[proxy]")]
        self.assertEqual(len(core), 1)
        self.assertRegex(core[0], r"^pymss\[proxy\] @ git\+https://github\.com/pymss-project/pymss\.git@[0-9a-f]{40}$")
        self.assertFalse(any(line.startswith("pymss-core") for line in lines))

    def test_minimum_versions_resolve_newer_stable_releases_and_keep_extras(self):
        self.manifest["common"].update({
            "pymss": "pymss[proxy]>=2.2.0", "pymss-core": "pymss-core>=0.1.14",
        })
        self.path.write_text(json.dumps(self.manifest), encoding="utf-8")
        report = {"version": "1", "install": [
            {"metadata": {"name": "pymss", "version": "2.3.0"}},
            {"metadata": {"name": "pymss-core", "version": "0.2.0"}},
        ]}

        def resolve(command, **_kwargs):
            self.assertIn(command[-1], ["pymss[proxy]>=2.2.0", "pymss-core>=0.1.14"])
            self._report(command, report)

        with mock.patch.object(CORE.subprocess, "run", side_effect=resolve):
            self.assertEqual(CORE.resolve_manifest(self.path), {"pymss": "2.3.0", "pymss-core": "0.2.0"})
        resolved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(resolved["common"], self.manifest["common"])
        self.assertEqual(resolved["releaseCore"]["requirements"], {
            "pymss": "pymss[proxy]==2.3.0", "pymss-core": "pymss-core==0.2.0",
        })

    def test_minimum_version_rejects_older_report_without_changing_manifest(self):
        self.manifest["common"]["pymss"] = "pymss[proxy]>=2.2.0"
        self.path.write_text(json.dumps(self.manifest), encoding="utf-8")
        original = self.path.read_bytes()

        def resolve(command, **_kwargs):
            report = {"version": "1", "install": [
                {"metadata": {"name": "pymss", "version": "2.1.8"}},
                {"metadata": {"name": "pymss-core", "version": "0.1.11"}},
            ]}
            self._report(command, report)

        with mock.patch.object(CORE.subprocess, "run", side_effect=resolve) as run:
            with self.assertRaisesRegex(ValueError, "Invalid stable release core"):
                CORE.resolve_manifest(self.path)
        run.assert_called_once()
        self.assertEqual(self.path.read_bytes(), original)

    def test_requirement_validation_keeps_release_sources_and_version_forms_restricted(self):
        for requirement in (
            "other==2.2.0", "pymss @ https://example.com/pymss.whl",
            'pymss>=2.2.0; python_version >= "3.11"', "pymss==2.*",
            "pymss>=2.2.0,<3", "pymss<=2.2.0", "pymss~=2.2.0",
        ):
            with self.subTest(requirement=requirement):
                self.manifest["common"]["pymss"] = requirement
                with self.assertRaises(ValueError):
                    CORE.core_requirements(self.manifest)

    def test_unversioned_resolution_keeps_extras_and_records_resolved_versions(self):
        self.manifest["common"].update({"pymss": "pymss[proxy]", "pymss-core": "pymss-core"})
        self.path.write_text(json.dumps(self.manifest), encoding="utf-8")
        report = {"version": "1", "install": [
            {"metadata": {"name": "pymss", "version": "2.2.0"}},
            {"metadata": {"name": "pymss-core", "version": "0.2.0"}},
        ]}

        def resolve(command, **_kwargs):
            self.assertIn(command[-1], ["pymss[proxy]", "pymss-core"])
            self._report(command, report)

        with mock.patch.object(CORE.subprocess, "run", side_effect=resolve):
            self.assertEqual(CORE.resolve_manifest(self.path), {"pymss": "2.2.0", "pymss-core": "0.2.0"})
        resolved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(resolved["common"], self.manifest["common"])
        self.assertEqual(resolved["releaseCore"]["requirements"], {
            "pymss": "pymss[proxy]==2.2.0", "pymss-core": "pymss-core==0.2.0",
        })

    def test_bundle_verification_requires_a_resolved_version(self):
        for requirement in ("pymss[proxy]", "pymss[proxy]>=2.2.0"):
            with self.subTest(requirement=requirement):
                self.manifest["common"]["pymss"] = requirement
                self.path.write_text(json.dumps(self.manifest), encoding="utf-8")
                with self.assertRaises(ValueError):
                    CORE.verify_installed(self.path)

    def test_real_resolver_selects_newest_stable_python_compatible_wheels(self):
        for name, versions in (
            ("pymss", ("2.1.8", "2.1.9", "2.2.0rc1", "2.2.0")),
            ("pymss-core", ("0.1.11", "0.2.0rc1")),
        ):
            package = name.replace("-", "_")
            for version in versions:
                info = f"{package}-{version}.dist-info"
                with zipfile.ZipFile(self.root / f"{package}-{version}-py3-none-any.whl", "w") as wheel:
                    wheel.writestr(
                        f"{info}/METADATA",
                        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                        "Provides-Extra: proxy\n"
                        + ("Requires-Python: >=3.13\n" if version == "2.2.0" else "Requires-Python: >=3.10\n"),
                    )
                    wheel.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
                    wheel.writestr(f"{info}/RECORD", "")

        run = subprocess.run

        def offline_run(command, **kwargs):
            command = list(command)
            index = command.index("--index-url")
            command[index:index + 2] = ["--no-index", "--find-links", str(self.root)]
            return run(command, capture_output=True, text=True, **kwargs)

        with mock.patch.object(CORE.subprocess, "run", side_effect=offline_run):
            versions = CORE.resolve_manifest(self.path)
        self.assertEqual(versions, {"pymss": "2.1.9", "pymss-core": "0.1.11"})
        expected = copy.deepcopy(self.manifest)
        expected["releaseCore"] = {"requirements": {
            "pymss": "pymss[proxy]==2.1.9", "pymss-core": "pymss-core==0.1.11",
        }}
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), expected)

    def test_resolution_failure_preserves_the_manifest(self):
        original = self.path.read_bytes()
        with mock.patch.object(CORE.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["pip"])):
            with self.assertRaises(subprocess.CalledProcessError):
                CORE.resolve_manifest(self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_real_resolver_follows_pymss_metadata_even_below_a_studio_core_floor(self):
        for name, version, dependencies in (
            ("pymss", "2.3.4", ('pymss-core==0.1.14; extra == "proxy"',
                                 'pymss-core>=0.1.14; sys_platform == "win32"',
                                 'pymss-core>=9; extra == "server"', "torch>=2.7.1")),
            ("pymss-core", "0.1.14", ()), ("pymss-core", "0.1.15", ()),
        ):
            package = name.replace("-", "_")
            info = f"{package}-{version}.dist-info"
            with zipfile.ZipFile(self.root / f"{package}-{version}-py3-none-any.whl", "w") as wheel:
                wheel.writestr(f"{info}/METADATA",
                               f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                               "Provides-Extra: proxy\n"
                               + "".join(f"Requires-Dist: {dep}\n" for dep in dependencies))
                wheel.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
                wheel.writestr(f"{info}/RECORD", "")
        run = subprocess.run

        def offline_run(command, **kwargs):
            self.assertIn("--no-deps", command)
            command = list(command)
            index = command.index("--index-url")
            command[index:index + 2] = ["--no-index", "--find-links", str(self.root)]
            return run(command, capture_output=True, text=True, **kwargs)

        for floor in ("0.1.14", "0.1.15"):
            with self.subTest(floor=floor):
                self.manifest["common"].update({"pymss": "pymss[proxy]>=2.3.4",
                                                "pymss-core": f"pymss-core>={floor}"})
                self.path.write_text(json.dumps(self.manifest), encoding="utf-8")
                original = self.path.read_bytes()
                with mock.patch.object(CORE.subprocess, "run", side_effect=offline_run):
                    versions = CORE.resolve_manifest(self.path)
                    self.assertEqual(versions, {"pymss": "2.3.4", "pymss-core": "0.1.14"})
                self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["common"],
                                 json.loads(original)["common"])

    def test_core_metadata_checks_target_platforms_and_rejects_untrusted_sources(self):
        package = {"requires_dist": ['pymss-core==0.1.14; sys_platform == "darwin"',
                                    'pymss-core>=9; python_version < "3.12"']}
        dependencies = list(CORE.core_dependencies(package, {"proxy"}, "3.12"))
        self.assertEqual([str(item.specifier) for item in dependencies], ["==0.1.14"])
        with self.assertRaisesRegex(ValueError, "versioned index packages"):
            list(CORE.core_dependencies({"requires_dist": [
                "pymss-core @ https://example.invalid/core.whl",
            ]}, set(), "3.12"))

    def test_bundle_verification_uses_build_pins_without_changing_runtime_policy(self):
        self.manifest["releaseCore"] = {"requirements": {
            "pymss": "pymss[proxy]==2.2.0", "pymss-core": "pymss-core==0.2.0",
        }}
        self.path.write_text(json.dumps(self.manifest), encoding="utf-8")
        original = self.path.read_bytes()
        with mock.patch.object(CORE.metadata, "version", side_effect={"pymss": "2.2.0", "pymss-core": "0.2.0"}.get):
            self.assertEqual(CORE.verify_installed(self.path), {"pymss": "2.2.0", "pymss-core": "0.2.0"})
        with mock.patch.object(CORE.metadata, "version", return_value="2.1.8"):
            with self.assertRaises(ValueError):
                CORE.verify_installed(self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_packaging_tools_use_the_same_build_pins_instead_of_runtime_floors(self):
        self.manifest["releaseCore"] = {"requirements": {
            "pymss": "pymss[proxy]==2.2.0", "pymss-core": "pymss-core==0.2.0",
        }}
        self.path.write_text(json.dumps(self.manifest), encoding="utf-8")
        pins = ["pymss[proxy]==2.2.0", "pymss-core==0.2.0"]
        if shutil.which("bash"):
            source = (ROOT / "scripts/prepare-python-runtime.sh").read_text(encoding="utf-8")
            start = source.index("manifest_values() {")
            function = source[start:source.index("\n}\n", start) + 3]
            result = subprocess.run([shutil.which("bash"), "-e", "-c",
                                     function + "\nmanifest_values pymss\nmanifest_values pymss-core\n"],
                                    env={**os.environ, "PYTHON_BIN": Path(sys.executable).as_posix(),
                                         "MANIFEST_PATH": self.path.as_posix(), "MANIFEST_BACKEND": "cpu"},
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), pins)
        if shutil.which("pwsh"):
            source = (ROOT / "scripts/prepare-python-runtime.ps1").read_text(encoding="utf-8")
            start = source.index("function Get-ManifestRequirement {")
            function = source[start:source.index("\nfunction Get-ManifestBackendExtras", start)]
            script = self.root / "requirements.ps1"
            script.write_text("$ErrorActionPreference = 'Stop'\n"
                              "$runtimeManifest = Get-Content -Raw $args[0] | ConvertFrom-Json\n"
                              + function + "\nGet-ManifestRequirement 'pymss'\nGet-ManifestRequirement 'pymss-core'\n",
                              encoding="utf-8")
            result = subprocess.run([shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-File", str(script), str(self.path)],
                                    capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), pins)
        spec = importlib.util.spec_from_file_location("build_core_artifacts", ROOT / "scripts/runtime-artifacts.py")
        artifacts = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(artifacts)
        requirements = artifacts.requirements_for(self.manifest, {"torch": {"requirement": "torch==2.7.1"}, "installMode": "legacy"})
        for pin in pins:
            self.assertIn(pin, requirements)
        self.assertEqual(self.manifest["common"]["pymss"], "pymss[proxy]==2.1.8")

    def test_invalid_report_never_replaces_the_pinned_manifest(self):
        valid = {
            "version": "1",
            "install": [
                {"metadata": {"name": "pymss", "version": "2.1.9"}},
                {"metadata": {"name": "pymss-core", "version": "0.1.11"}},
            ],
        }
        reports = [{**valid, "version": "2"}, {**valid, "install": valid["install"][:1]}]
        for fields in (
            {"metadata": {"name": "pymss", "version": "2.2.0rc1"}},
            {"metadata": {"name": "pymss", "version": "2.1.7"}},
            {"metadata": {"name": "pymss", "version": "2.1.9"}, "is_yanked": True},
            {"metadata": {"name": "unexpected", "version": "1.0"}},
        ):
            reports.append({**valid, "install": [fields, valid["install"][1]]})
        original = self.path.read_bytes()
        for report in reports:
            with self.subTest(report=report):
                def resolve(command, **_kwargs):
                    self._report(command, report)

                with mock.patch.object(CORE.subprocess, "run", side_effect=resolve):
                    with self.assertRaises(ValueError):
                        CORE.resolve_manifest(self.path)
                self.assertEqual(self.path.read_bytes(), original)

    def test_verification_rejects_a_stale_or_missing_bundled_core(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["common"]["pymss"] = "pymss[proxy]==2.1.9"
        self.path.write_text(json.dumps(manifest), encoding="utf-8")
        for version in ("2.1.8", None, "2.1.9"):
            with self.subTest(version=version):
                def installed(name):
                    if name == "pymss":
                        if version is None:
                            raise CORE.metadata.PackageNotFoundError(name)
                        return version
                    return "0.1.11"

                with mock.patch.object(CORE.metadata, "version", side_effect=installed):
                    if version == "2.1.9":
                        self.assertEqual(CORE.verify_installed(self.path)["pymss"], version)
                    else:
                        with self.assertRaises((ValueError, CORE.metadata.PackageNotFoundError)):
                            CORE.verify_installed(self.path)

    def test_macos_release_consumes_shared_cores_before_preparation_and_verifies_the_final_bundle(self):
        workflow = (ROOT / ".github/workflows/release-macos.yml").read_text(encoding="utf-8")
        download = workflow.index("name: Download shared core snapshot")
        prepare = workflow.index("bash scripts/prepare-python-runtime.sh mlx")
        prepared_check = workflow.index("scripts/resolve-release-core.py python/runtime-manifest.json --verify")
        bundle_check = workflow.index('scripts/resolve-release-core.py "$(dirname "$WORKER")/runtime-manifest.json" --verify')
        package = workflow.index("- name: Build managed macOS update archive")
        self.assertLess(download, prepare)
        self.assertNotIn("run: python3 scripts/resolve-release-core.py python/runtime-manifest.json\n", workflow)
        self.assertLess(prepare, prepared_check)
        self.assertLess(prepared_check, bundle_check)
        self.assertLess(bundle_check, package)
