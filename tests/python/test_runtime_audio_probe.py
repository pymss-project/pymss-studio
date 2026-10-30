from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap

import worker_bootstrap
import runtime_audio_probe


class RuntimeAudioProbeTests(unittest.TestCase):
    def test_core_api_checks_reject_a_breaking_graph_interface_before_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "resources").mkdir()
            (root / "resources/model_catalog.json").write_text('{"models": []}', encoding="utf-8")
            modules = {
                "pymss": SimpleNamespace(__file__=str(root / "__init__.py"), MSSeparator=type("Separator", (), {"process_folder": lambda *_args: None}), get_separation_logger=lambda: None),
                "pymss.config": SimpleNamespace(load_config=lambda: None, to_plain=lambda: None),
                "pymss.model_registry": SimpleNamespace(**{name: lambda: None for name in ("get_model_entry", "list_models", "model_root", "model_path_for", "config_path_for", "auxiliary_paths_for")}),
                "pymss.graph": SimpleNamespace(**{name: lambda: None for name in ("compile_workflow_to_dag", "load_comfy_file", "run_dag", "SeparatorCache", "DAGNode", "DAGLink", "get_node_type", "register_node")}),
            }
            with mock.patch.object(runtime_audio_probe.importlib, "import_module", side_effect=modules.__getitem__):
                runtime_audio_probe.verify_core_api()
                del modules["pymss.graph"].run_dag
                with self.assertRaisesRegex(RuntimeError, "pymss.graph.run_dag"):
                    runtime_audio_probe.verify_core_api()

    def test_core_update_verification_requests_the_desktop_api_contract_check(self) -> None:
        with mock.patch.object(worker_bootstrap.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            worker_bootstrap._verify_runtime_audio(Path(sys.executable), Path("staging"), core_api=True)
        self.assertIn("--check-core", run.call_args.args[0])

    def test_audio_decode_and_resampling_keep_both_channels(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", str(_worker_test_bootstrap.WORKER_DIR / "runtime_audio_probe.py")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=True,
        )
        self.assertEqual(json.loads(completed.stdout), {"sampleRate": 8000, "channels": 2, "frames": 160})

    def test_top_level_librosa_import_hides_missing_decoder_but_probe_rejects_it(self) -> None:
        python_dir = _worker_test_bootstrap.WORKER_DIR
        script = """
import importlib.abc
import sys
class MissingAudioread(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'audioread':
            raise ModuleNotFoundError("No module named 'audioread'", name='audioread')
sys.meta_path.insert(0, MissingAudioread())
import librosa
print('top-level import succeeded', flush=True)
from runtime_audio_probe import verify_audio_runtime
verify_audio_runtime()
"""
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            env={**os.environ, "PYTHONPATH": str(python_dir), "PYTHONNOUSERSITE": "1"},
        )
        self.assertIn("top-level import succeeded", result.stdout)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No module named 'audioread'", result.stderr)

    def test_current_manifest_updates_a_runtime_missing_audioread_without_reinstalling_librosa(self) -> None:
        manifest = worker_bootstrap._manifest()
        requirements = worker_bootstrap._overlay_requirements(
            manifest, "cuda", {"librosa": "0.10.2.post1"}, "2.1.8", "0.1.11",
        )
        self.assertIn(manifest["common"]["audioread"], requirements)
        self.assertNotIn(manifest["common"]["librosa"], requirements)
        self.assertGreater(
            tuple(map(int, manifest["manifestVersion"].split("."))),
            (2026, 10, 1),
        )

    def test_probe_uses_selected_interpreter_and_staging_overlay(self) -> None:
        with mock.patch.object(worker_bootstrap.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            worker_bootstrap._verify_runtime_audio(Path(sys.executable), Path("staging"))
        command = run.call_args.args[0]
        self.assertEqual(command[0], str(worker_bootstrap._runtime_command_path(Path(sys.executable))))
        self.assertEqual(Path(command[1]).name, "runtime_audio_probe.py")
        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["PYTHONPATH"], "staging")
        self.assertEqual(environment["PYTHONNOUSERSITE"], "1")
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertNotIn("PYTHONHOME", environment)

    def test_probe_failure_preserves_the_missing_dependency_reason(self) -> None:
        result = mock.Mock(returncode=1, stderr="Traceback...\nModuleNotFoundError: No module named 'audioread'\n")
        with mock.patch.object(worker_bootstrap.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "No module named 'audioread'"):
                worker_bootstrap._verify_runtime_audio(Path(sys.executable))


if __name__ == "__main__":
    unittest.main()
