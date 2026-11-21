import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))
import studio_cli
import worker_bootstrap


class CliStartupTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.pointer = self.root / "active-runtime.json"
        self.enterContext(patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", self.pointer))
        self.enterContext(patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.root))
        self.inventory = self.enterContext(patch.object(worker_bootstrap, "_installed_envs"))
        self.activate = self.enterContext(patch.object(worker_bootstrap, "cmd_activate_runtime", return_value=0))
        self.cpu = {"backend": "cpu", "pythonPath": str(self.root / "cpu/Scripts/python.exe"), "health": "ready"}

    def test_first_launch_preserves_concurrent_activation_and_stdout(self):
        self.inventory.return_value = [self.cpu]
        def activate(payload):
            print(json.dumps({"type": "runtime_activated", "payload": payload}))
            return 0
        self.activate.side_effect = activate
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(studio_cli.prepare(), 0)
        self.assertEqual(out.getvalue(), "")
        self.activate.assert_called_once_with({"backend": "cpu", "pythonPath": self.cpu["pythonPath"], "onlyIfNoActive": True})

    def test_missing_broken_and_ambiguous_selections(self):
        self.inventory.return_value = []
        with self.assertRaisesRegex(ValueError, "安装运行环境"):
            studio_cli.prepare()
        self.inventory.return_value = [self.cpu, {**self.cpu, "backend": "cuda"}]
        with self.assertRaisesRegex(ValueError, "多个运行环境"):
            studio_cli.prepare()
        self.pointer.write_text('{"backend":"cuda","pythonPath":"missing"}', encoding="utf-8")
        self.inventory.return_value = [self.cpu]
        with self.assertRaisesRegex(ValueError, "修复"):
            studio_cli.prepare()
        self.activate.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Windows extended-length paths")
    def test_relocation_accepts_extended_alias_of_the_same_runtime_root(self):
        python = Path(self.cpu["pythonPath"])
        python.parent.mkdir(parents=True)
        python.touch()
        args = ["studio_cli.py", "relocate", "//?/" + python.as_posix()]
        with patch.object(sys, "argv", args), patch.object(sys, "path", list(sys.path)), \
             patch.object(worker_bootstrap, "_repair_runtime_venv_config") as repair:
            self.assertEqual(studio_cli.main(), 0)
        self.assertTrue(repair.call_args.args[0].samefile(python.parent.parent))

    def test_relocation_repairs_bundled_environment_alongside_user_root(self):
        bundled = self.root / "package/python-runtime/runtime-envs"
        bootstrap = bundled.parent / "python.exe"
        python = bundled / "cpu/Scripts/python.exe"
        python.parent.mkdir(parents=True)
        python.touch()
        bootstrap.touch()
        config = python.parent.parent / "pyvenv.cfg"
        config.write_text("home = __PYMSS_STUDIO_PYTHON_RUNTIME__\n", encoding="utf-8")
        with (
            patch.object(sys, "argv", ["studio_cli.py", "relocate", str(python)]),
            patch.object(sys, "path", list(sys.path)),
            patch.object(worker_bootstrap, "BUNDLED_RUNTIME_ENVS_DIR", bundled),
            patch.object(worker_bootstrap, "_bootstrap_python_path", return_value=bootstrap),
        ):
            self.assertEqual(studio_cli.main(), 0)
        home = config.read_text(encoding="utf-8").splitlines()[0].removeprefix("home = ")
        self.assertTrue(Path(home).samefile(bootstrap.parent))
        self.assertFalse((self.root / ".runtime-management.lock").exists())

    def test_run_preserves_the_upstream_entry_point_arguments_and_exit_code(self):
        package = self.root / "pymss"
        package.mkdir()
        (package / "__init__.py").touch()
        (package / "__main__.py").write_text(
            "import sys\nassert sys.argv[1:] == ['one two', '', '--option']\nraise SystemExit(130)\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, "-P", studio_cli.__file__, "run", "one two", "", "--option"],
            env={**os.environ, "PYTHONPATH": str(self.root)},
            capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 130, result.stderr)

    @unittest.skipIf(sys.platform == "win32", "Unix process wait status")
    def test_run_preserves_sigint_caught_by_the_upstream_entry_point(self):
        package = self.root / "pymss"
        package.mkdir()
        (package / "__init__.py").touch()
        (package / "__main__.py").write_text(
            "import signal\n"
            "try:\n    signal.raise_signal(signal.SIGINT)\n"
            "except KeyboardInterrupt:\n    raise SystemExit(130)\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, "-P", studio_cli.__file__, "run"],
            env={**os.environ, "PYTHONPATH": str(self.root)},
            capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, -signal.SIGINT, result.stderr)


if __name__ == "__main__":
    unittest.main()
