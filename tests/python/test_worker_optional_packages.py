from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
import zipfile
from pathlib import Path
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap

import worker_optional_packages as optional_packages


class OptionalRuntimePackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.envs_dir = self.root / "runtime-envs"
        self.active_file = self.envs_dir / "active-runtime.json"
        self.envs_dir.mkdir(parents=True)
        self.active_file.write_text(json.dumps({"backend": "cuda"}), encoding="utf-8")
        self.environment = mock.patch.dict(os.environ, {
            "PYMSS_STUDIO_RUNTIME_ENVS_DIR": str(self.envs_dir),
            "PYMSS_STUDIO_ACTIVE_RUNTIME_FILE": str(self.active_file),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.enterContext(mock.patch.object(optional_packages, "_environment_version", side_effect=lambda name, runtime=None: {
            "torch": "2.7.1", "torchaudio": "2.7.1",
        }.get(name)))

    def test_release_manifest_does_not_bundle_funasr(self) -> None:
        manifest_path = Path(__file__).resolve().parents[2] / "python" / "runtime-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertNotIn("funasr", manifest["common"])

    def test_package_directory_is_scoped_to_active_backend_and_python(self) -> None:
        expected_identity = f"cuda-py{sys.version_info.major}{sys.version_info.minor}"
        self.assertEqual(optional_packages._runtime_identity(), expected_identity)
        self.assertEqual(
            optional_packages._package_dir("funasr").resolve(),
            (self.envs_dir / "optional-packages" / expected_identity / "funasr").resolve(),
        )

    def test_status_prefers_managed_sidecar_version(self) -> None:
        package_dir = optional_packages._package_dir("funasr")
        metadata_dir = package_dir / "funasr-1.3.26.dist-info"
        metadata_dir.mkdir(parents=True)
        (metadata_dir / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: funasr\nVersion: 1.3.26\n",
            encoding="utf-8",
        )
        with mock.patch.object(optional_packages, "_environment_version") as environment_version:
            status = optional_packages.optional_package_status("funasr")
        self.assertTrue(status["installed"])
        self.assertTrue(status["present"])
        self.assertEqual(status["version"], "1.3.26")
        self.assertEqual(status["source"], "managed")
        self.assertTrue(status["canUninstall"])
        self.assertIsNone(status["issue"])
        environment_version.assert_not_called()

    def test_status_probe_does_not_import_an_installed_component(self) -> None:
        package_dir = optional_packages._package_dir("funasr")
        metadata_dir = package_dir / "funasr-1.3.26.dist-info"
        metadata_dir.mkdir(parents=True)
        (metadata_dir / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: funasr\nVersion: 1.3.26\n",
            encoding="utf-8",
        )
        with mock.patch.object(optional_packages, "_environment_version") as environment_version, \
             mock.patch.object(optional_packages.subprocess, "run") as run_process:
            status = optional_packages.optional_package_status("funasr")
        self.assertTrue(status["installed"])
        self.assertTrue(status["present"])
        self.assertIsNone(status["issue"])
        environment_version.assert_not_called()
        run_process.assert_not_called()

    def test_status_marks_an_incompatible_version_for_repair(self) -> None:
        with mock.patch.object(optional_packages, "_sidecar_version", return_value=None), \
             mock.patch.object(optional_packages, "_environment_version", return_value="1.2.0"):
            status = optional_packages.optional_package_status("funasr")
        self.assertFalse(status["installed"])
        self.assertTrue(status["present"])
        self.assertEqual(status["issue"], "version_mismatch")

    def test_activate_only_adds_existing_managed_package_directory(self) -> None:
        package_dir = optional_packages._package_dir("funasr")
        package_dir.mkdir(parents=True)
        isolated_path = list(sys.path)
        with mock.patch.object(sys, "path", isolated_path):
            optional_packages.activate_optional_packages()
            self.assertEqual(sys.path[0], str(package_dir))

    def test_uninstall_removes_managed_package_without_touching_runtime_pip(self) -> None:
        package_dir = optional_packages._package_dir("funasr")
        package_dir.mkdir(parents=True)
        with mock.patch.object(optional_packages, "_environment_version", return_value=None), \
             mock.patch.object(optional_packages, "_run_process") as run_process:
            optional_packages._uninstall_package("funasr")
        self.assertFalse(package_dir.exists())
        run_process.assert_not_called()

    def test_windows_and_linux_keep_environment_component_uninstallation(self) -> None:
        runtime = optional_packages._runtime_context()
        for platform in ("win32", "linux"):
            with self.subTest(platform=platform), \
                 mock.patch.object(sys, "platform", platform), \
                 mock.patch.object(optional_packages, "_environment_version", return_value="1.3.26"), \
                 mock.patch.object(optional_packages, "_run_process") as run_process:
                status = optional_packages.optional_package_status("funasr")
                self.assertEqual(status["source"], "environment")
                self.assertTrue(status["canUninstall"])
                optional_packages._uninstall_package("funasr", runtime=runtime)
                self.assertEqual(run_process.call_args.args[0], [
                    str(runtime.executable), "-m", "pip", "uninstall", "--yes", "funasr",
                ])

    def test_macos_base_component_cannot_be_uninstalled(self) -> None:
        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch.object(optional_packages, "_environment_version", return_value="1.3.26"), \
             mock.patch.object(optional_packages, "_run_process") as run_process:
            status = optional_packages.optional_package_status("funasr")
            self.assertFalse(status["canUninstall"])
            with self.assertRaisesRegex(RuntimeError, "base runtime"):
                optional_packages._uninstall_package("funasr")
            run_process.assert_not_called()

    def test_windows_and_linux_uninstall_both_managed_and_environment_copies(self) -> None:
        package_dir = optional_packages._package_dir("funasr")
        for platform in ("win32", "linux"):
            package_dir.mkdir(parents=True)
            with self.subTest(platform=platform), \
                 mock.patch.object(sys, "platform", platform), \
                 mock.patch.object(optional_packages, "_environment_version", return_value="1.3.26"), \
                 mock.patch.object(optional_packages, "_run_process") as run_process:
                optional_packages._uninstall_package("funasr")
                self.assertFalse(package_dir.exists())
                self.assertIn("uninstall", run_process.call_args.args[0])

    def test_uninstall_removes_only_the_managed_copy_when_the_base_also_supplies_it(self) -> None:
        package_dir = optional_packages._package_dir("funasr")
        package_dir.mkdir(parents=True)
        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch.object(optional_packages, "_environment_version", return_value="1.3.26"), \
             mock.patch.object(optional_packages, "_run_process") as run_process:
            optional_packages._uninstall_package("funasr")
        self.assertFalse(package_dir.exists())
        run_process.assert_not_called()

    def test_uninstall_reports_the_preserved_base_component_as_read_only(self) -> None:
        package_dir = optional_packages._package_dir("funasr")
        information = package_dir / "funasr-1.3.26.dist-info"
        information.mkdir(parents=True)
        (information / "METADATA").write_text("Name: funasr\nVersion: 1.3.26\n", encoding="utf-8")
        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch.object(optional_packages, "_environment_version", return_value="1.3.26"), \
             mock.patch.object(optional_packages, "_run_process") as run_process, \
             mock.patch.object(optional_packages, "emit") as emit:
            self.assertEqual(optional_packages.cmd_manage_optional_package({"package": "funasr", "action": "uninstall"}), 0)
        self.assertFalse(package_dir.exists())
        status = emit.call_args.args[1]
        self.assertTrue(status["installed"])
        self.assertEqual(status["source"], "environment")
        self.assertFalse(status["canUninstall"])
        run_process.assert_not_called()

    def test_windows_and_linux_uninstall_commands_report_component_removed(self) -> None:
        for platform in ("win32", "linux"):
            with self.subTest(platform=platform), \
                 mock.patch.object(sys, "platform", platform), \
                 mock.patch.object(optional_packages, "_environment_version", side_effect=["1.3.26", "1.3.26", None]), \
                 mock.patch.object(optional_packages, "_run_process") as run_process, \
                 mock.patch.object(optional_packages, "emit") as emit:
                self.assertEqual(optional_packages.cmd_manage_optional_package({"package": "funasr", "action": "uninstall"}), 0)
                self.assertFalse(emit.call_args.args[1]["present"])
                run_process.assert_called_once()

    def test_macos_managed_component_uninstall_only_removes_its_directory(self) -> None:
        package_dir = optional_packages._package_dir("funasr")
        package_dir.mkdir(parents=True)
        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch.object(optional_packages, "_environment_version", return_value=None), \
             mock.patch.object(optional_packages, "_run_process") as run_process:
            optional_packages._uninstall_package("funasr")
        self.assertFalse(package_dir.exists())
        run_process.assert_not_called()

    def test_bundled_component_directory_survives_first_core_activation(self) -> None:
        bundled = self.root / "Installed.app/Contents/Resources/python-runtime/runtime-envs"
        bundled.mkdir(parents=True)
        (bundled / "active-runtime.json").write_text(json.dumps({"backend": "mlx"}), encoding="utf-8")
        self.active_file.unlink()
        with mock.patch.dict(os.environ, {"PYMSS_STUDIO_BUNDLED_RUNTIME_ENVS_DIR": str(bundled)}):
            before = optional_packages._runtime_context()
            self.active_file.write_text(json.dumps({"backend": "mlx", "overlayPath": "core-overlay"}), encoding="utf-8")
            after = optional_packages._runtime_context()
        self.assertEqual(before.backend, "mlx")
        self.assertEqual(before.packages_dir, after.packages_dir)

    def test_existing_executable_scoped_components_remain_available(self) -> None:
        identity = optional_packages._runtime_identity(backend="", executable=Path(sys.executable).resolve())
        legacy = self.envs_dir / "optional-packages" / identity
        (legacy / "funasr").mkdir(parents=True)
        runtime = optional_packages._runtime_context()
        self.assertEqual(runtime.backend, "cuda")
        self.assertEqual(runtime.packages_dir.resolve(), legacy.resolve())

    def test_macos_default_component_root_is_outside_the_application(self) -> None:
        with mock.patch.dict(os.environ, {"PYMSS_STUDIO_DATA_ROOT": str(self.root / "data")}), \
             mock.patch.object(sys, "platform", "darwin"):
            os.environ.pop("PYMSS_STUDIO_RUNTIME_ENVS_DIR")
            self.assertEqual(optional_packages._runtime_envs_dir(), self.root / "data/runtime-envs")

    def test_install_stages_and_activates_package_without_writing_to_runtime(self) -> None:
        commands = []

        def fake_process(command, name, action, environment=None, task_id=None):
            self.assertEqual(name, "funasr")
            self.assertEqual(action, "install")
            commands.append(command)
            if "--report" in command:
                report_path = Path(command[command.index("--report") + 1])
                report_path.write_text(json.dumps({
                    "install": [{"metadata": {"name": "funasr", "version": "1.3.26"}}],
                }), encoding="utf-8")
                return
            if "--target" not in command:
                return
            staging = Path(command[command.index("--target") + 1])
            metadata_dir = staging / "funasr-1.3.26.dist-info"
            metadata_dir.mkdir(parents=True)
            (metadata_dir / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: funasr\nVersion: 1.3.26\n",
                encoding="utf-8",
            )

        isolated_path = list(sys.path)
        runtime = optional_packages._runtime_context()
        self.active_file.write_text(json.dumps({"backend": "cpu"}), encoding="utf-8")
        with mock.patch.object(optional_packages, "_run_process", side_effect=fake_process), \
             mock.patch.object(optional_packages, "_emit_stage"), \
             mock.patch.object(optional_packages, "_environment_version", side_effect=lambda name, runtime=None: {
                 "torch": "2.7.1+cu128",
                 "torchaudio": "2.7.1+cu128",
             }.get(name)), \
             mock.patch.object(sys, "path", isolated_path):
            optional_packages._install_package(
                "funasr",
                optional_packages.OPTIONAL_PACKAGES["funasr"],
                "pypi",
                "en-US",
                runtime=runtime,
            )
            package_dir = optional_packages._package_dir("funasr", runtime)
            self.assertTrue(package_dir.is_dir())
            self.assertEqual(sys.path[0], str(package_dir))
            self.assertEqual(runtime.identity, f"cuda-py{sys.version_info.major}{sys.version_info.minor}")
            self.assertFalse(optional_packages._package_dir("funasr").exists())
            install_command = next(command for command in commands if "--target" in command)
            self.assertEqual(install_command[0], str(runtime.executable))
            self.assertIn("--no-deps", install_command)
            self.assertIn("funasr==1.3.26", install_command)
            verification_commands = [
                command for command in commands
                if "-c" in command and "import funasr, torchaudio" in " ".join(command)
            ]
            self.assertEqual(len(verification_commands), 1)

    def test_missing_torchaudio_uses_the_active_torch_build(self) -> None:
        with mock.patch.object(optional_packages, "_environment_version", side_effect=lambda name, runtime=None: {
            "torch": "2.7.1+cu128",
            "torchaudio": None,
        }.get(name)):
            requirement = optional_packages._torchaudio_requirement()
        self.assertEqual(
            requirement,
            ("torchaudio==2.7.1", "https://download.pytorch.org/whl/cu128"),
        )

    def test_mismatched_torchaudio_uses_the_active_torch_build(self) -> None:
        with mock.patch.object(optional_packages, "_environment_version", side_effect=lambda name, runtime=None: {
            "torch": "2.7.1+cu128",
            "torchaudio": "2.6.0+cu124",
        }.get(name)):
            requirement = optional_packages._torchaudio_requirement()
        self.assertEqual(
            requirement,
            ("torchaudio==2.7.1", "https://download.pytorch.org/whl/cu128"),
        )

    def test_linux_rocm_audio_uses_the_linux_index_instead_of_windows_wheels(self) -> None:
        self.active_file.write_text(json.dumps({"backend": "rocm"}), encoding="utf-8")
        with mock.patch("worker_bootstrap.sys.platform", "linux"), mock.patch.object(
            optional_packages, "_environment_version", side_effect=lambda name, runtime=None: {
                "torch": "2.10.0+rocm7.1", "torchaudio": None,
            }.get(name),
        ):
            self.assertEqual(optional_packages._torchaudio_requirement(), (
                "torchaudio==2.10.0", "https://download.pytorch.org/whl/rocm7.1",
            ))

    def test_matching_torchaudio_is_not_reinstalled(self) -> None:
        with mock.patch.object(optional_packages, "_environment_version", side_effect=lambda name, runtime=None: {
            "torch": "2.7.1+cu128",
            "torchaudio": "2.7.1+cu128",
        }.get(name)):
            self.assertIsNone(optional_packages._torchaudio_requirement())

    def test_torchaudio_with_another_accelerator_build_is_replaced_in_the_component(self) -> None:
        with mock.patch.object(optional_packages, "_environment_version", side_effect=lambda name, runtime=None: {
            "torch": "2.7.1+cu128", "torchaudio": "2.7.1+cu124",
        }.get(name)):
            requirement = optional_packages._torchaudio_requirement()
        self.assertEqual(requirement, ("torchaudio==2.7.1", "https://download.pytorch.org/whl/cu128"))

    def test_torchaudio_is_staged_without_reinstalling_torch(self) -> None:
        staging = self.root / "staging"
        with mock.patch.object(
            optional_packages,
            "_torchaudio_requirement",
            return_value=("torchaudio==2.7.1", "https://download.pytorch.org/whl/cu128"),
        ), mock.patch.object(optional_packages, "_emit_stage"), \
             mock.patch.object(optional_packages, "_run_process") as run_process:
            optional_packages._install_torchaudio(staging, "funasr", "optional-task")
        command = run_process.call_args.args[0]
        self.assertIn("--no-deps", command)
        self.assertIn("--target", command)
        self.assertIn("torchaudio==2.7.1", command)
        self.assertFalse(any(value.startswith("torch==") for value in command))

    def test_verification_failure_does_not_reinstall_every_dependency_from_fallback(self) -> None:
        target_installs = 0

        def fake_process(command, name, action, environment=None, task_id=None):
            nonlocal target_installs
            if "--report" in command:
                report_path = Path(command[command.index("--report") + 1])
                report_path.write_text(json.dumps({
                    "install": [{"metadata": {"name": "funasr", "version": "1.3.26"}}],
                }), encoding="utf-8")
                return
            if "--target" in command:
                target_installs += 1
                return
            raise ModuleNotFoundError("missing verification dependency")

        with mock.patch.object(optional_packages, "_run_process", side_effect=fake_process), \
             mock.patch.object(optional_packages, "_emit_stage"), \
             mock.patch.object(optional_packages, "_environment_version", side_effect=lambda name, runtime=None: {
                 "torch": "2.7.1+cu128",
                 "torchaudio": "2.7.1+cu128",
             }.get(name)):
            with self.assertRaisesRegex(ModuleNotFoundError, "missing verification dependency"):
                optional_packages._install_package(
                    "funasr",
                    optional_packages.OPTIONAL_PACKAGES["funasr"],
                    "auto",
                    "zh-CN",
                )
        self.assertEqual(target_installs, 1)

    def test_resolution_includes_the_requested_package_when_metadata_hides_it(self) -> None:
        report = self.root / "report.json"

        def fake_process(command, *_args, **_kwargs):
            Path(command[command.index("--report") + 1]).write_text(
                json.dumps({"install": []}),
                encoding="utf-8",
            )

        with mock.patch.object(optional_packages, "_run_process", side_effect=fake_process):
            resolved = optional_packages._resolve_install_requirements(
                "funasr==1.3.26",
                "https://pypi.org/simple",
                report,
                "funasr",
            )
        self.assertEqual(resolved, ["funasr==1.3.26"])

    def test_optional_resolution_rejects_changes_to_fixed_runtime_packages(self) -> None:
        for name in ("torch", "torchvision", "triton", "mlx", "mlx-metal", "nvidia-cublas-cu12", "rocm-sdk-core", "pymss", "pymss-core", "PyMss__Core"):
            with self.subTest(name=name):
                report = self.root / "report.json"

                def fake_process(command, *_args, **_kwargs):
                    report.write_text(json.dumps({
                        "install": [{"metadata": {"name": name, "version": "9.0"}}],
                    }), encoding="utf-8")

                with mock.patch.object(optional_packages, "_run_process", side_effect=fake_process):
                    with self.assertRaisesRegex(RuntimeError, "runtime package"):
                        optional_packages._resolve_install_requirements(
                            "funasr==1.3.26", "https://pypi.org/simple", report, "funasr",
                        )

    def test_optional_resolution_constrains_torch_and_routes_audio_to_the_backend_installer(self) -> None:
        report = self.root / "report.json"

        def fake_process(command, *_args, **_kwargs):
            constraints = Path(command[command.index("--constraint") + 1])
            self.assertEqual(constraints.read_text(encoding="utf-8"), "torch==2.7.1+cu128\ntorchaudio==2.7.1\n")
            report.write_text(json.dumps({"install": [
                {"metadata": {"name": "funasr", "version": "1.3.26"}},
                {"metadata": {"name": "torchaudio", "version": "2.7.1"}},
                {"metadata": {"name": "audio-helper", "version": "1.0"}},
            ]}), encoding="utf-8")

        with mock.patch.object(optional_packages, "_environment_version", return_value="2.7.1+cu128"), \
             mock.patch.object(optional_packages, "_run_process", side_effect=fake_process):
            resolved = optional_packages._resolve_install_requirements(
                "funasr==1.3.26", "https://pypi.org/simple", report, "funasr",
            )
        self.assertEqual(resolved, ["funasr==1.3.26", "audio-helper==1.0"])
        self.assertFalse(report.with_suffix(".constraints.txt").exists())

    def test_unknown_package_is_rejected_before_running_pip(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported optional runtime package"):
            optional_packages._package_spec("unknown")

    def test_apple_silicon_uses_funasr_mlx_sidecar_without_reading_plugin_inventory(self) -> None:
        runtime = optional_packages._runtime_context()
        target = optional_packages._package_dir("funasr", runtime)
        metadata = target / "funasr_mlx-0.2.1.dist-info"
        metadata.mkdir(parents=True)
        (metadata / "METADATA").write_text("Name: funasr-mlx\nVersion: 0.2.1\n", encoding="utf-8")
        with mock.patch.object(optional_packages, "_is_apple_silicon", return_value=True):
            status = optional_packages.optional_package_status("funasr", runtime)
        self.assertEqual(status["source"], "managed")
        self.assertEqual(status["requirement"], "funasr-mlx>=0.2.2")
        self.assertTrue(status["canUninstall"])

    def test_apple_silicon_requires_the_ctc_tokenizer_for_installed_status(self) -> None:
        runtime = optional_packages._runtime_context()
        for tokenizer_version, expected in [(None, False), ("0.1.99", False), ("0.2.1", True)]:
            with self.subTest(tokenizer=tokenizer_version), \
                 mock.patch.object(optional_packages, "_is_apple_silicon", return_value=True), \
                 mock.patch.object(optional_packages, "_sidecar_version", side_effect=lambda name, path: {
                     "funasr-mlx": "0.2.3", "sentencepiece": tokenizer_version,
                 }.get(name)), \
                 mock.patch.object(optional_packages, "_environment_version", return_value=None):
                status = optional_packages.optional_package_status("funasr", runtime)
            self.assertEqual(status["installed"], expected)
            self.assertEqual(status["issue"], None if expected else "version_mismatch")

    def test_apple_silicon_uninstall_removes_only_mlx_sidecar(self) -> None:
        runtime = optional_packages._runtime_context()
        target = optional_packages._package_dir("funasr", runtime)
        metadata = target / "funasr_mlx-0.2.1.dist-info"
        metadata.mkdir(parents=True)
        (metadata / "METADATA").write_text("Name: funasr-mlx\nVersion: 0.2.1\n", encoding="utf-8")
        with mock.patch.object(optional_packages, "_is_apple_silicon", return_value=True), \
             mock.patch.object(optional_packages, "_environment_version", return_value=None) as environment_version, \
             mock.patch.object(optional_packages, "_run_process") as process:
            optional_packages.cmd_manage_optional_package({"package": "funasr", "action": "uninstall"})
        self.assertFalse(target.exists())
        environment_version.assert_called_with("funasr-mlx", runtime)
        process.assert_not_called()

    def test_mlx_base_component_is_read_only_and_checks_supported_versions(self):
        runtime = optional_packages._runtime_context()
        for version, installed in [("0.2.3", True), ("0.2.1", False), ("0.1.9", False), ("0.2rc1", False)]:
            with self.subTest(version=version), \
                 mock.patch.object(optional_packages, "_is_apple_silicon", return_value=True), \
                 mock.patch.object(optional_packages, "_sidecar_version", return_value=None), \
                 mock.patch.object(optional_packages, "_environment_version", side_effect=lambda name, runtime: "0.2.1" if name == "sentencepiece" else version):
                status = optional_packages.optional_package_status("funasr", runtime)
            self.assertEqual(status["installed"], installed)
            self.assertEqual(status["source"], "environment")
            self.assertFalse(status["canUninstall"])

    def test_mlx_install_plan_rejects_base_runtime_replacement(self) -> None:
        runtime = optional_packages._runtime_context()
        report = self.root / "mlx-plan.json"
        def process(command, *_args, **_kwargs):
            if "--dry-run" in command:
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(json.dumps({"install": [{"metadata": {"name": "mlx", "version": "9.0"}}]}), encoding="utf-8")
        with mock.patch.object(optional_packages, "_is_apple_silicon", return_value=True), \
             mock.patch.object(optional_packages, "_run_process", side_effect=process), \
             self.assertRaisesRegex(RuntimeError, "base runtime"):
            optional_packages._resolve_install_requirements("funasr-mlx>=0.2", "https://pypi.org/simple", report, "funasr", runtime=runtime)
        self.assertFalse(optional_packages._package_dir("funasr", runtime).exists())

    def test_mlx_resolution_keeps_dependencies_and_fixes_the_base_without_torch(self):
        runtime = optional_packages._runtime_context()
        report = self.root / "mlx-plan.json"
        def process(command, *_args, **_kwargs):
            self.assertIn("https://mirror.example/simple", command)
            self.assertIn("sentencepiece>=0.2", command)
            constraints = Path(command[command.index("--constraint") + 1]).read_text()
            self.assertIn("mlx==0.30.0", constraints)
            self.assertNotIn("torch", constraints)
            report.write_text(json.dumps({"install": [
                {"metadata": {"name": "funasr-mlx", "version": "0.2.1"}},
                {"metadata": {"name": "text-helper", "version": "1.0"}},
            ]}), encoding="utf-8")
        with mock.patch.object(optional_packages, "_is_apple_silicon", return_value=True), \
             mock.patch.object(optional_packages, "_environment_version", side_effect=lambda name, runtime: "0.30.0" if name == "mlx" else None), \
             mock.patch.object(optional_packages, "_run_process", side_effect=process):
            result = optional_packages._resolve_install_requirements("funasr-mlx>=0.2", "https://mirror.example/simple", report, "funasr", runtime=runtime)
        self.assertEqual(result, ["funasr-mlx==0.2.1", "text-helper==1.0", "sentencepiece>=0.2"])
        self.assertFalse(report.with_suffix(".constraints.txt").exists())


class OfflineOptionalPackageTests(unittest.TestCase):
    def test_install_load_and_uninstall_preserve_the_read_only_base(self) -> None:
        self.check_offline_component(incompatible_torch=False)

    def test_incompatible_torch_dependency_preserves_the_base_and_existing_component(self) -> None:
        self.check_offline_component(incompatible_torch=True)

    def check_offline_component(self, *, incompatible_torch: bool) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        base = root / "Installed.app/Contents/Resources/python-runtime"
        venv.EnvBuilder(with_pip=True).create(base)
        python = base / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        wheels = root / "wheels"
        wheels.mkdir()

        def wheel(name, version, requirements=()):
            package = name.replace("-", "_")
            info = f"{package}-{version}.dist-info"
            entries = {
                f"{package}/__init__.py": f"__version__ = {version!r}\n",
                f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                + "".join(f"Requires-Dist: {requirement}\n" for requirement in requirements),
                f"{info}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            }
            record = io.StringIO()
            csv.writer(record).writerows((path, "", "") for path in [*entries, f"{info}/RECORD"])
            entries[f"{info}/RECORD"] = record.getvalue()
            output = wheels / f"{package}-{version}-py3-none-any.whl"
            with zipfile.ZipFile(output, "w") as archive:
                for path, content in entries.items():
                    archive.writestr(path, content)

        wheel("torch", "2.7.1")
        wheel("torch", "2.8.0")
        wheel("torchaudio", "2.7.1", ["torch==2.7.1"])
        wheel("mlx", "0.31.0")
        wheel("component-helper", "1.0")
        wheel("funasr", "1.3.26", [
            "torch>=2.8" if incompatible_torch else "torch>=2.7",
            "torchaudio>=2.7", "component-helper>=1", "mlx>=0.31",
        ])
        environment = os.environ.copy()
        for key in list(environment):
            if key.startswith(("PIP_", "PYMSS_")) or key in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}:
                environment.pop(key)
        envs = root / "data/runtime-envs"
        envs.mkdir(parents=True)
        active = envs / "active-runtime.json"
        active.write_text(json.dumps({"backend": "mlx"}), encoding="utf-8")
        environment.update({
            "PIP_NO_INDEX": "1", "PIP_FIND_LINKS": wheels.as_uri(), "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_CACHE_DIR": str(root / "data/pip-cache"),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
            "PYMSS_STUDIO_RUNTIME_ENVS_DIR": str(envs), "PYMSS_STUDIO_ACTIVE_RUNTIME_FILE": str(active),
        })
        subprocess.run([
            str(python), "-m", "pip", "install", "torch==2.7.1", "mlx==0.31.0",
        ], env=environment, capture_output=True, text=True, timeout=60, check=True)

        def snapshot():
            return {
                path.relative_to(base).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in base.rglob("*") if path.is_file()
            }

        before = snapshot()
        modes = [(path, path.stat().st_mode) for path in [base, *base.rglob("*")] if not path.is_symlink()]

        def restore_permissions():
            for path, mode in modes:
                path.chmod(mode)

        self.addCleanup(restore_permissions)
        for path, mode in reversed(modes):
            path.chmod(mode & ~0o222)

        identity = f"mlx-py{sys.version_info.major}{sys.version_info.minor}"
        runtime = optional_packages.RuntimeContext(python, "mlx", identity, envs / "optional-packages" / identity)
        target = optional_packages._package_dir("funasr", runtime)
        isolated_path = list(sys.path)
        with mock.patch.dict(os.environ, environment, clear=True), \
             mock.patch.object(sys, "path", isolated_path), \
             contextlib.redirect_stdout(io.StringIO()):
            if incompatible_torch:
                target.mkdir(parents=True)
                marker = target / "previous-component"
                marker.write_text("preserved", encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    optional_packages._install_package("funasr", optional_packages.OPTIONAL_PACKAGES["funasr"], "pypi", "en", runtime=runtime)
                self.assertEqual(marker.read_text(encoding="utf-8"), "preserved")
            else:
                optional_packages._install_package("funasr", optional_packages.OPTIONAL_PACKAGES["funasr"], "pypi", "en", runtime=runtime)
                # A fresh worker process adds the component directory only for
                # the ASR path, while Torch and MLX keep loading from the base.
                loaded = subprocess.run([
                    str(python), "-c",
                    "import sys, json; sys.path.insert(0, sys.argv[1]); "
                    "from worker_optional_packages import activate_optional_packages; activate_optional_packages(); "
                    "import funasr, component_helper, torchaudio, torch, mlx; "
                    "print(json.dumps({m.__name__: m.__file__ for m in (funasr, component_helper, torchaudio, torch, mlx)}))",
                    str(_worker_test_bootstrap.WORKER_DIR),
                ], env=environment, capture_output=True, text=True, timeout=30, check=True)
                paths = json.loads(loaded.stdout)
                for name in ("funasr", "component_helper", "torchaudio"):
                    self.assertTrue(Path(paths[name]).resolve().is_relative_to(target.resolve()))
                for name in ("torch", "mlx"):
                    self.assertTrue(Path(paths[name]).resolve().is_relative_to(base.resolve()))
                with mock.patch.object(sys, "platform", "darwin"):
                    optional_packages._uninstall_package("funasr", runtime=runtime)
                self.assertFalse(target.exists())
        self.assertEqual(snapshot(), before, "Component management must preserve the fixed runtime")


if __name__ == "__main__":
    unittest.main()
