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

import worker_bootstrap as worker
import worker_optional_packages as optional_packages


class OfflineCoreUpdateTests(unittest.TestCase):
    def test_real_resolver_installs_the_confirmed_pair_and_dependency_then_restores_the_base(self):
        self.run_offline_update(bundled=False)

    def test_read_only_bundled_runtime_updates_and_rolls_back_through_user_overlays(self):
        self.run_offline_update(bundled=True)

    def test_bundle_replacement_preserves_core_components_and_rollback(self):
        self.run_offline_update(bundled=True, replace_bundle=True)

    def test_bundle_replacement_preserves_mlx_components_and_rollback(self):
        self.run_offline_update(bundled=True, replace_bundle=True, apple_silicon=True)

    @unittest.skipUnless(os.name == "posix", "POSIX directory symlinks")
    def test_bundled_runtime_update_and_rollback_through_a_directory_alias(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        physical = root / "physical"
        physical.mkdir()
        alias = root / "alias"
        alias.symlink_to(physical, target_is_directory=True)
        self.run_offline_update(bundled=True, temporary_parent=alias)

    @unittest.skipUnless(shutil.which("bash"), "Bash is required for runtime pruning")
    def test_pruned_bundled_runtime_preserves_torch_dependencies_for_offline_updates(self):
        self.run_offline_update(bundled=True, pruned=True)

    def run_offline_update(self, *, bundled, pruned=False, replace_bundle=False, temporary_parent=None, apple_silicon=False):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=temporary_parent)))
        backend = "mlx" if apple_silicon else "cpu"
        envs = root / "runtime-envs"
        envs.mkdir()
        bundled_envs = root / "Installed.app/Contents/Resources/python-runtime/runtime-envs"
        base = (bundled_envs if bundled else envs) / backend
        venv.EnvBuilder(with_pip=True).create(base)
        python = base / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        wheels = root / "wheels"
        wheels.mkdir()

        def wheel(name, version, requirements=()):
            package = name.replace("-", "_")
            info = f"{package}-{version}.dist-info"
            code = (
                "__version__='2.7.1'\nfrom types import SimpleNamespace\n"
                "version=SimpleNamespace(cuda=None,hip=None)\n"
                "cuda=SimpleNamespace(is_available=lambda:False)\n"
            ) if name == "torch" else ""
            entries = {
                f"{package}/__init__.py": code,
                f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nProvides-Extra: proxy\n"
                + "".join(f"Requires-Dist: {requirement}\n" for requirement in requirements),
                f"{info}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            }
            if name == "pymss":
                entries["pymss/graph/__init__.py"] = ""
            if name == "funasr-mlx":
                entries["funasr_mlx/api.py"] = (
                    "import mlx, sentencepiece\n"
                    "def download_model(*args, **kwargs): pass\n"
                    "def transcribe(*args, **kwargs): pass\n"
                )
            record = io.StringIO()
            csv.writer(record).writerows((path, "", "") for path in [*entries, f"{info}/RECORD"])
            entries[f"{info}/RECORD"] = record.getvalue()
            with zipfile.ZipFile(wheels / f"{package}-{version}-py3-none-any.whl", "w") as archive:
                for path, content in entries.items():
                    archive.writestr(path, content)

        # PyTorch 2.7.1 declares this dependency for Python 3.12 and newer.
        wheel("torch", "2.7.1", ['setuptools; python_version >= "3.12"'] if pruned else [])
        if pruned:
            wheel("setuptools", "78.1.0")
        wheel("pymss", "2.1.4", ["pymss-core==0.1.6", "torch>=2"])
        wheel("pymss-core", "0.1.6", ["torch>=2"])
        wheel("pymss", "2.2.0", ["pymss-core>=0.1.7,<0.2", "new-audio-dependency>=1", "torch>=2"])
        wheel("pymss-core", "0.1.7", ["torch>=2"])
        wheel("pymss-core", "0.2.0", ["torch>=2"])
        wheel("new-audio-dependency", "1.0")
        if replace_bundle:
            if apple_silicon:
                wheel("mlx", "0.31.0", ["mlx-metal==0.31.0"])
                wheel("mlx-metal", "0.31.0")
                wheel("funasr-mlx", "0.2.2", ["mlx>=0.31.0", "sentencepiece>=0.2"])
                wheel("sentencepiece", "0.2.1")
            else:
                wheel("funasr", "1.3.26", ["torch>=2", "torchaudio==2.7.1"])
                wheel("torchaudio", "2.7.1", ["torch==2.7.1"])
        child_env = os.environ.copy()
        child_env.pop("PYTHONHOME", None)
        child_env.pop("PYTHONPATH", None)
        seed = subprocess.run([
            str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-index", "--find-links", str(wheels),
            "pymss==2.1.4", "pymss-core==0.1.6", "torch==2.7.1",
            *(["mlx==0.31.0"] if apple_silicon else []),
        ], env=child_env, capture_output=True, text=True, timeout=90)
        self.assertEqual(seed.returncode, 0, seed.stderr)
        if pruned:
            subprocess.run([
                shutil.which("bash"), str(Path(__file__).resolve().parents[2] / "scripts/prune-python-runtime.sh"),
                str(base), "--keep-venv",
            ], env=child_env, capture_output=True, text=True, timeout=60, check=True)
            # The release check only supplies core wheels; base dependencies
            # must remain installed rather than being fetched again.
            for path in wheels.glob("setuptools-*.whl"):
                path.unlink()
        manifest = {
            "manifestVersion": "2026.10.2", "baseGeneration": 1,
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "common": {"pymss": "pymss[proxy]==2.1.4", "pymss-core": "pymss-core==0.1.6"},
            "backends": {backend: {
                "platforms": [sys.platform], "torch": {"requirement": "torch==2.7.1"},
                "extras": ["mlx"] if apple_silicon else [],
            }},
        }
        state = {
            "source": "bundled" if bundled else "managed",
            "backend": backend, "manifestVersion": manifest["manifestVersion"], "stateVersion": worker.ENV_STATE_VERSION,
            "pythonPath": str(python), "torchVersion": "2.7.1", "torchBackend": "cpu",
            "packages": {"pymss": True, "pymss-core": True},
            "packageVersions": {"pymss": "2.1.4", "pymss-core": "0.1.6"},
            "pymssVersion": "2.1.4", "pymssCoreVersion": "0.1.6", "pymssGraphAvailable": True,
        }
        active = envs / "active-runtime.json"
        cached = base / "pymss-runtime-state.json"
        active.write_text(json.dumps(state), encoding="utf-8")
        cached.write_text(json.dumps(state), encoding="utf-8")
        if replace_bundle:
            bundle = root / "Installed.app"
            resources = bundle / "Contents/Resources/python"
            resources.mkdir()
            for source in Path(worker.__file__).parent.glob("*.py"):
                shutil.copy2(source, resources / source.name)
            (resources / "runtime-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            user_files = [root / directory / "retained.bin" for directory in ("models", "settings", "outputs")]
            for path in user_files:
                path.parent.mkdir()
                path.write_bytes(b"retained user content")
            component = optional_packages.RuntimeContext(
                python, backend, f"{backend}-py{sys.version_info.major}{sys.version_info.minor}",
                envs / "optional-packages" / f"{backend}-py{sys.version_info.major}{sys.version_info.minor}",
            )
            # Keep platform selection identical in fresh workers and the parent
            # without changing the interpreter's native filesystem/pip platform.
            component_bootstrap = (
                "import worker_optional_packages as optional; "
                f"optional._is_apple_silicon=lambda: {apple_silicon!r}; "
            )
            component_modules = ("funasr_mlx", "sentencepiece") if apple_silicon else ("funasr", "torchaudio")
            base_modules = ("torch", "mlx", "mlx_metal") if apple_silicon else ("torch",)

            def check_fresh_component(expected_core):
                environment = worker._probe_environment(
                    Path(expected_core) if expected_core != base else None, python_path=python,
                )
                environment.update({
                    "PYMSS_STUDIO_RUNTIME_ENVS_DIR": str(envs),
                    "PYMSS_STUDIO_ACTIVE_RUNTIME_FILE": str(active),
                    "PYTHONPATH": os.pathsep.join(filter(None, [environment.get("PYTHONPATH"), str(resources)])),
                })
                status = subprocess.run([
                    str(python), "-c", component_bootstrap
                    + 'raise SystemExit(optional.cmd_optional_package_status({"package":"funasr"}))',
                ], env=environment, capture_output=True, text=True, timeout=30, check=True)
                result = next(json.loads(line)["payload"] for line in status.stdout.splitlines()
                              if json.loads(line).get("type") == "optional_package_status")
                self.assertTrue(result["installed"])
                self.assertEqual(result["source"], "managed")
                self.assertEqual(result["requirement"], optional_packages._package_spec("funasr")[1]["requirement"])
                loaded = subprocess.run([
                    str(python), "-c", component_bootstrap
                    + "import importlib,json; optional.activate_optional_packages(); "
                    + f"modules=[importlib.import_module(name) for name in {('pymss', *component_modules, *base_modules)!r}]; "
                    + "print(json.dumps({m.__name__: m.__file__ for m in modules}))",
                ], env=environment, capture_output=True, text=True, timeout=30, check=True)
                paths = json.loads(loaded.stdout)
                self.assertTrue(Path(paths["pymss"]).resolve().is_relative_to(Path(expected_core).resolve()))
                for name in base_modules:
                    self.assertTrue(Path(paths[name]).resolve().is_relative_to(base.resolve()))
                for name in component_modules:
                    self.assertTrue(Path(paths[name]).resolve().is_relative_to(component.packages_dir.resolve()))

            def fresh_rollback(payload):
                environment = worker._probe_environment(python_path=python)
                environment.update({
                    "PYMSS_STUDIO_RUNTIME_ENVS_DIR": str(envs),
                    "PYMSS_STUDIO_ACTIVE_RUNTIME_FILE": str(active),
                    "PYMSS_STUDIO_BUNDLED_RUNTIME_ENVS_DIR": str(bundled_envs),
                    "PYTHONPATH": str(resources),
                })
                # Keep this fixture's existing audio-probe boundary while
                # importing the replacement bundle's rollback code afresh.
                completed = subprocess.run([
                    str(python), "-c",
                    "import json,sys; from unittest import mock; import worker_bootstrap as worker; "
                    "worker._verify_runtime_audio=mock.Mock(); "
                    "raise SystemExit(worker.cmd_update_runtime_core(json.loads(sys.argv[1])))",
                    json.dumps(payload),
                ], env=environment, capture_output=True, text=True, timeout=60)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                return [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        baseline = {
            path.relative_to(base).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in base.rglob("*") if path.is_file()
        }
        if bundled:
            paths = [base, *base.rglob("*")]
            modes = [(path, path.stat().st_mode) for path in paths if not path.is_symlink()]

            def restore_permissions():
                for path, mode in modes:
                    path.chmod(mode)

            self.addCleanup(restore_permissions)
            for path, mode in reversed(modes):
                path.chmod(mode & ~0o222)
        real_popen = subprocess.Popen
        installs = []

        def offline_popen(command, *args, **kwargs):
            if "pip" in command and "install" in command:
                command = [*command, "--disable-pip-version-check", "--no-index", "--find-links", str(wheels)]
                installs.append(command)
            return real_popen(command, *args, **kwargs)

        with mock.patch.object(worker, "RUNTIME_ENVS_DIR", envs), \
             mock.patch.object(worker, "ACTIVE_RUNTIME_FILE", active), \
             mock.patch.object(worker, "BUNDLED_RUNTIME_ENVS_DIR", bundled_envs if bundled else None), \
             mock.patch.object(worker, "_manifest", return_value=manifest), \
             mock.patch.object(worker, "_latest_pypi_version", side_effect=lambda name, channel="stable": {"pymss": "2.2.0", "pymss-core": "0.2.0"}[name]) as latest, \
             mock.patch.object(worker, "_verify_runtime_audio"), \
             mock.patch.object(optional_packages, "_is_apple_silicon", return_value=apple_silicon), \
             mock.patch.object(worker.subprocess, "Popen", side_effect=offline_popen), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            payload = {"backend": backend, "pythonPath": str(python), "mirror": "pypi", "mode": "update", "taskId": "offline-pip"}
            self.assertEqual(worker.cmd_update_runtime_core({**payload, "prepareOnly": True}), 0, output.getvalue())
            plan = next(json.loads(line)["payload"] for line in output.getvalue().splitlines() if json.loads(line)["type"] == "runtime_core_update_plan")
            self.assertEqual(plan["targetVersions"], {"pymss": "2.2.0", "pymss-core": "0.1.7"})
            self.assertEqual(json.loads(active.read_text(encoding="utf-8"))["pymssVersion"], "2.1.4")
            latest.side_effect = AssertionError("Confirmed installation must not query upstream")
            self.assertEqual(worker.cmd_update_runtime_core({**payload, "planId": plan["id"]}), 0, output.getvalue())
            installed = json.loads(active.read_text(encoding="utf-8"))
            self.assertEqual(installed["pymssCoreVersion"], "0.1.7")
            self.assertTrue((Path(installed["overlayPath"]) / "new_audio_dependency").is_dir())
            loaded = subprocess.run(
                [str(python), "-c", "import pymss, new_audio_dependency; print(pymss.__file__)"],
                env=worker._probe_environment(Path(installed["overlayPath"]), python_path=python),
                capture_output=True, text=True, timeout=30, check=True,
            )
            self.assertTrue(
                Path(loaded.stdout.strip()).resolve().is_relative_to(Path(installed["overlayPath"]).resolve()),
                f"The worker loaded the core outside the active overlay: {loaded.stdout.strip()}",
            )
            if replace_bundle:
                with mock.patch.dict(os.environ, {
                    "PIP_NO_INDEX": "1", "PIP_FIND_LINKS": wheels.as_uri(), "PIP_CONFIG_FILE": os.devnull,
                }), mock.patch.object(sys, "path", list(sys.path)):
                    optional_packages._install_package(
                        *optional_packages._package_spec("funasr"), "pypi", "en", runtime=component,
                    )
                check_fresh_component(installed["overlayPath"])
                before_replacement = active.read_bytes()
                replacement = root / "Replacement.app"
                previous = root / "Previous.app"
                shutil.copytree(bundle, replacement, symlinks=True)
                (replacement / "Contents/build-version").write_text("next desktop version", encoding="utf-8")
                bundle.rename(previous)
                replacement.rename(bundle)
                check_fresh_component(installed["overlayPath"])
                self.assertEqual(active.read_bytes(), before_replacement)
                previous_base = previous / base.relative_to(bundle)

                def restore_previous_permissions():
                    for path in [previous_base, *previous_base.rglob("*")]:
                        if not path.is_symlink():
                            path.chmod(path.stat().st_mode | 0o200)

                self.addCleanup(restore_previous_permissions)
                self.assertEqual({
                    path.relative_to(previous_base).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in previous_base.rglob("*") if path.is_file()
                }, baseline)
            if replace_bundle:
                events = fresh_rollback({**payload, "mode": "rollback", "prepareOnly": True})
                rollback_plan = next(event["payload"] for event in events if event["type"] == "runtime_core_update_plan")
                events = fresh_rollback({**payload, "planId": rollback_plan["id"]})
                self.assertTrue(any(event["type"] == "runtime_core_update_finished" for event in events))
            else:
                self.assertEqual(worker.cmd_update_runtime_core({**payload, "mode": "rollback", "prepareOnly": True}), 0, output.getvalue())
                plans = [json.loads(line)["payload"] for line in output.getvalue().splitlines() if json.loads(line)["type"] == "runtime_core_update_plan"]
                self.assertEqual(worker.cmd_update_runtime_core({**payload, "planId": plans[-1]["id"]}), 0, output.getvalue())
            restored = json.loads(active.read_text(encoding="utf-8"))
            self.assertEqual(restored["pymssVersion"], "2.1.4")
            self.assertFalse(restored.get("overlayPath"))
            self.assertEqual(json.loads(cached.read_text(encoding="utf-8"))["pymssVersion"], "2.1.4")
            loaded = subprocess.run(
                [str(python), "-c", "import pymss; print(pymss.__file__)"],
                env=worker._probe_environment(python_path=python),
                capture_output=True, text=True, timeout=30, check=True,
            )
            self.assertTrue(
                Path(loaded.stdout.strip()).resolve().is_relative_to(base.resolve()),
                f"Rollback loaded the core outside the base runtime: {loaded.stdout.strip()} (expected {base.resolve()})",
            )
            if replace_bundle:
                check_fresh_component(base)
                for path in user_files:
                    self.assertEqual(path.read_bytes(), b"retained user content")
        if not replace_bundle:
            self.assertEqual(len(installs), 2)
        if bundled:
            after = {
                path.relative_to(base).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in base.rglob("*") if path.is_file()
            }
            self.assertEqual(after, baseline, "The immutable bundled runtime must not change")
