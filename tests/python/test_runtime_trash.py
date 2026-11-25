import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from . import _bootstrap  # noqa: F401  (adds python/ to sys.path)

import worker_runtime_trash
from worker_runtime_lock import runtime_lock
from worker_runtime_trash import TRASH_DIR_NAME, has_pending, move_to_trash, purge, start_purge, trash_root


class RuntimeTrashTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.envs_dir = Path(temporary.name) / "runtime-envs"
        self.envs_dir.mkdir()
        self.trash = trash_root(self.envs_dir)

    def _make_env(self, name):
        env_dir = self.envs_dir / name
        (env_dir / "Lib" / "site-packages").mkdir(parents=True)
        (env_dir / "Lib" / "site-packages" / "torch.bin").write_bytes(b"x" * 1024)
        return env_dir

    def _wait_until_empty(self, timeout=20.0):
        deadline = time.monotonic() + timeout
        while has_pending(self.trash) and time.monotonic() < deadline:
            time.sleep(0.05)

    def test_moving_an_environment_frees_its_name_immediately(self):
        env_dir = self._make_env("cpu")
        moved = move_to_trash(env_dir)
        self.assertFalse(env_dir.exists())
        self.assertEqual(moved.parent, self.envs_dir / TRASH_DIR_NAME)
        self.assertTrue((moved / "Lib" / "site-packages" / "torch.bin").is_file())
        self.assertNotEqual(move_to_trash(self._make_env("cpu")), moved)

    def test_purge_removes_trashed_environments_and_keeps_its_lock(self):
        move_to_trash(self._make_env("cpu"))
        move_to_trash(self._make_env("cuda"))
        purge(self.trash)
        self.assertFalse(has_pending(self.trash))
        self.assertTrue((self.trash / ".purge.lock").is_file())

    def test_a_running_purge_is_not_duplicated(self):
        moved = move_to_trash(self._make_env("cpu"))
        with runtime_lock(self.trash / ".purge.lock"):
            purge(self.trash)
        self.assertTrue(moved.is_dir())

    def test_nothing_is_started_without_pending_entries(self):
        with mock.patch.object(worker_runtime_trash.subprocess, "Popen") as popen:
            start_purge(self.trash)
            self.trash.mkdir()
            (self.trash / ".purge.lock").touch()
            start_purge(self.trash)
        popen.assert_not_called()

    def test_detached_purge_outlives_a_caller_whose_output_is_read_to_the_end(self):
        move_to_trash(self._make_env("cpu"))
        code = "import sys; from pathlib import Path; from worker_runtime_trash import start_purge; start_purge(Path(sys.argv[1]))"
        env = {**os.environ, "PYTHONPATH": str(Path(worker_runtime_trash.__file__).parent)}
        # Reading to EOF only returns when no process still holds the pipe, as the host does.
        completed = subprocess.run(
            [sys.executable, "-c", code, str(self.trash)],
            capture_output=True, text=True, timeout=20, env=env,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self._wait_until_empty()
        self.assertFalse(has_pending(self.trash))

