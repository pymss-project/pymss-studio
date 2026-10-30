from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
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

    def test_default_core_requirements_preserve_the_v2_compatibility_floor(self):
        self.assertEqual(MANIFEST["common"]["pymss"], "pymss[proxy]>=2.2.0")
        self.assertEqual(MANIFEST["common"]["pymss-core"], "pymss-core>=0.1.14")
        for filename in ("requirements.txt", "requirements-ci.txt"):
            lines = (ROOT / "python" / filename).read_text(encoding="utf-8").splitlines()
            self.assertIn("pymss[proxy]>=2.2.0", lines)
            self.assertIn("pymss-core>=0.1.14", lines)

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
            self.assertEqual(command[-2:], ["pymss[proxy]>=2.2.0", "pymss-core>=0.1.14"])
            Path(command[command.index("--report") + 1]).write_text(json.dumps(report), encoding="utf-8")

        with mock.patch.object(CORE.subprocess, "run", side_effect=resolve):
            self.assertEqual(CORE.resolve_manifest(self.path), {"pymss": "2.3.0", "pymss-core": "0.2.0"})
        resolved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(resolved["common"]["pymss"], "pymss[proxy]==2.3.0")
        self.assertEqual(resolved["common"]["pymss-core"], "pymss-core==0.2.0")

    def test_minimum_version_rejects_older_report_without_changing_manifest(self):
        self.manifest["common"]["pymss"] = "pymss[proxy]>=2.2.0"
        self.path.write_text(json.dumps(self.manifest), encoding="utf-8")
        original = self.path.read_bytes()

        def resolve(command, **_kwargs):
            report = {"version": "1", "install": [
                {"metadata": {"name": "pymss", "version": "2.1.8"}},
                {"metadata": {"name": "pymss-core", "version": "0.1.11"}},
            ]}
            Path(command[command.index("--report") + 1]).write_text(json.dumps(report), encoding="utf-8")

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
            self.assertEqual(command[-2:], ["pymss[proxy]", "pymss-core"])
            Path(command[command.index("--report") + 1]).write_text(json.dumps(report), encoding="utf-8")

        with mock.patch.object(CORE.subprocess, "run", side_effect=resolve):
            self.assertEqual(CORE.resolve_manifest(self.path), {"pymss": "2.2.0", "pymss-core": "0.2.0"})
        resolved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(resolved["common"]["pymss"], "pymss[proxy]==2.2.0")
        self.assertEqual(resolved["common"]["pymss-core"], "pymss-core==0.2.0")

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
        expected["common"]["pymss"] = "pymss[proxy]==2.1.9"
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), expected)

    def test_resolution_failure_preserves_the_manifest(self):
        original = self.path.read_bytes()
        with mock.patch.object(CORE.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["pip"])):
            with self.assertRaises(subprocess.CalledProcessError):
                CORE.resolve_manifest(self.path)
        self.assertEqual(self.path.read_bytes(), original)

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
                    Path(command[command.index("--report") + 1]).write_text(json.dumps(report), encoding="utf-8")

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

    def test_macos_release_resolves_before_preparation_and_verifies_the_final_bundle(self):
        workflow = (ROOT / ".github/workflows/release-macos.yml").read_text(encoding="utf-8")
        resolve = workflow.index("python3 scripts/resolve-release-core.py python/runtime-manifest.json")
        prepare = workflow.index("bash scripts/prepare-python-runtime.sh mlx")
        prepared_check = workflow.index("scripts/resolve-release-core.py python/runtime-manifest.json --verify")
        bundle_check = workflow.index('scripts/resolve-release-core.py "$(dirname "$WORKER")/runtime-manifest.json" --verify')
        package = workflow.index("- name: Build managed macOS update archive")
        self.assertLess(resolve, prepare)
        self.assertLess(prepare, prepared_check)
        self.assertLess(prepared_check, bundle_check)
        self.assertLess(bundle_check, package)
