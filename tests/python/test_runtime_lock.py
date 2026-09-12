import contextlib
import errno
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from functools import partial
from pathlib import Path
from unittest import mock

from . import _bootstrap

import worker_bootstrap
from worker_runtime_lock import RuntimeLockBusy, runtime_lock


class RuntimeLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.lock_file = self.root / ".runtime-management.lock"

    def _child_command(self, code):
        return [sys.executable, "-B", "-c", code, str(self.lock_file)]

    def _child_options(self):
        return {
            "cwd": str(_bootstrap.WORKER_DIR),
            "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        }

    @contextlib.contextmanager
    def _deny_lock_writes(self, error=errno.EACCES):
        original_open = Path.open

        def open_file(path, mode="r", *args, **kwargs):
            if path == self.lock_file and mode == "a+b":
                raise OSError(error, os.strerror(error))
            return original_open(path, mode, *args, **kwargs)

        with mock.patch.object(Path, "open", open_file):
            yield

    def test_read_only_handle_keeps_the_same_lock(self):
        # Locking beyond EOF also works on Windows; no initialization write is needed.
        self.lock_file.touch()
        with self.lock_file.open("rb") as owner:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with self._deny_lock_writes(), self.assertRaises(RuntimeLockBusy):
                    with runtime_lock(self.lock_file, timeout=0, allow_read_only=True):
                        pass
            finally:
                if os.name == "nt":
                    msvcrt.locking(owner.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(owner.fileno(), fcntl.LOCK_UN)
        with self._deny_lock_writes(), runtime_lock(self.lock_file, allow_read_only=True) as writable:
            self.assertFalse(writable)
            code = """
import sys
from pathlib import Path
from worker_runtime_lock import RuntimeLockBusy, runtime_lock
try:
    with runtime_lock(Path(sys.argv[1]), timeout=0):
        print('acquired')
except RuntimeLockBusy:
    print('busy')
"""
            blocked = subprocess.run(self._child_command(code), capture_output=True, text=True, timeout=10, check=True, **self._child_options())
            self.assertEqual(blocked.stdout.strip(), "busy")
        self.assertEqual(self.lock_file.read_bytes(), b"")

    def test_missing_read_only_lock_does_not_create_another_lock(self):
        for error in (errno.EACCES, errno.EPERM, errno.EROFS):
            with self.subTest(error=error), self._deny_lock_writes(error):
                with runtime_lock(self.lock_file, allow_read_only=True) as writable:
                    self.assertFalse(writable)
                with self.assertRaises(OSError):
                    with runtime_lock(self.lock_file):
                        pass
            self.assertEqual(list(self.root.iterdir()), [])

    def test_unexpected_lock_errors_do_not_enable_read_only_mode(self):
        with self._deny_lock_writes(errno.EIO), self.assertRaises(OSError):
            with runtime_lock(self.lock_file, allow_read_only=True):
                pass

    def test_read_only_filesystem_with_no_runtime_directory(self):
        runtime_root = self.root / "runtime-envs"
        with mock.patch.object(Path, "mkdir", side_effect=OSError(errno.EROFS, "Read-only file system")):
            with runtime_lock(runtime_root / self.lock_file.name, allow_read_only=True) as writable:
                self.assertFalse(writable)
        self.assertFalse(runtime_root.exists())

    def test_read_only_queries_do_not_repair_or_recover_environments(self):
        from .test_worker_bootstrap import MANIFEST, probe_result

        env_dir = self.root / "cpu"
        python = env_dir / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"interpreter")
        active_file = self.root / "active-runtime.json"
        active_file.write_text(json.dumps({"backend": "cpu", "pythonPath": str(python)}))
        for name in (".cpu.reinstalling", ".cpu.core-updating", ".cpu.core-backup"):
            directory = self.root / name
            directory.mkdir()
            (directory / "marker").write_bytes(b"preserved")
        state_file = env_dir / "pymss-runtime-state.json"

        def snapshot():
            return {str(path.relative_to(self.root)): path.read_bytes() if path.is_file() else None for path in self.root.rglob("*")}

        for stale_state in (False, True):
            if stale_state:
                state_file.write_text(json.dumps({"backend": "cpu", "stateVersion": 1, "torchBackend": "cuda"}))
            before = snapshot()
            with self.subTest(stale_state=stale_state), self._deny_lock_writes(), \
                 mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.root), \
                 mock.patch.object(worker_bootstrap, "ACTIVE_RUNTIME_FILE", active_file), \
                 mock.patch.object(worker_bootstrap, "BUNDLED_RUNTIME_ENVS_DIR", None), \
                 mock.patch.object(worker_bootstrap, "_manifest", return_value=MANIFEST), \
                 mock.patch.object(worker_bootstrap, "_module_available", return_value=False), \
                 mock.patch.object(worker_bootstrap, "_detect_gpu_vendors", return_value=[]), \
                 mock.patch.object(worker_bootstrap, "_probe_python_runtime", return_value=probe_result()), \
                 mock.patch.object(worker_bootstrap, "_probe_python_package_versions", return_value={}), \
                 mock.patch.object(worker_bootstrap, "_manifest_versions_are_satisfied", return_value=(True, [])), \
                 mock.patch.object(worker_bootstrap, "_recover_runtime_transactions") as recover, \
                 mock.patch.object(worker_bootstrap, "_repair_runtime_venv_config") as repair_config, \
                 mock.patch.object(worker_bootstrap, "_make_posix_venv_relocatable") as repair_links, \
                 mock.patch.object(worker_bootstrap, "_atomic_write_json") as write_json, \
                 mock.patch.object(sys, "platform", "win32"):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(worker_bootstrap.cmd_runtime_info({}), 0)
                info = json.loads(output.getvalue())["payload"]
                self.assertTrue(info["ready"])
                self.assertEqual(info["installedEnvironments"][0]["torchBackend"], "cpu")
                self.assertFalse(info["installedEnvironments"][0]["coreUpdateSupported"])
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(worker_bootstrap.cmd_runtime_env_sizes({}), 0)
                sizes = json.loads(output.getvalue())["payload"]
                self.assertGreater(sizes["sizes"]["cpu"], 0)
                self.assertEqual(sizes["incompleteBackends"], [])
                for action in (recover, repair_config, repair_links, write_json):
                    action.assert_not_called()
            self.assertEqual(snapshot(), before)

    def test_read_only_root_rejects_writers_before_mutation(self):
        commands = (
            worker_bootstrap.cmd_install_runtime,
            worker_bootstrap.cmd_update_runtime_core,
            worker_bootstrap.cmd_activate_runtime,
            worker_bootstrap.cmd_delete_runtime,
        )
        with self._deny_lock_writes(), \
             mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.root), \
             mock.patch.object(worker_bootstrap, "_recover_runtime_transactions") as recover, \
             mock.patch.object(worker_bootstrap, "_target_runtime_from_payload") as target:
            for command in commands:
                with self.subTest(command=command.__name__), contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertNotEqual(command({"backend": "cpu", "taskId": "runtime_permission_check"}), 0)
                    event = json.loads(output.getvalue())
                    self.assertEqual(event["payload"]["code"], "RUNTIME_PERMISSION_DENIED")
                    self.assertEqual(event["taskId"], "runtime_permission_check")
            recover.assert_not_called()
            target.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_lock_excludes_other_processes_and_is_reusable(self):
        code = """
import sys
from pathlib import Path
from worker_runtime_lock import RuntimeLockBusy, runtime_lock
try:
    with runtime_lock(Path(sys.argv[1]), timeout=0):
        print('acquired')
except RuntimeLockBusy:
    print('busy')
"""
        with runtime_lock(self.lock_file):
            blocked = subprocess.run(self._child_command(code), capture_output=True, text=True, timeout=10, check=True, **self._child_options())
        released = subprocess.run(self._child_command(code), capture_output=True, text=True, timeout=10, check=True, **self._child_options())
        self.assertEqual(blocked.stdout.strip(), "busy")
        self.assertEqual(released.stdout.strip(), "acquired")
        self.assertTrue(self.lock_file.exists())

    def test_exception_releases_lock(self):
        with self.assertRaisesRegex(ValueError, "interrupted"):
            with runtime_lock(self.lock_file):
                raise ValueError("interrupted")
        with runtime_lock(self.lock_file, timeout=0):
            pass

    def test_terminated_worker_releases_lock(self):
        code = """
import sys
from pathlib import Path
from worker_runtime_lock import runtime_lock
path = Path(sys.argv[1])
with runtime_lock(path):
    path.with_suffix('.ready').write_text('ready')
    sys.stdin.read()
"""
        process = subprocess.Popen(self._child_command(code), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **self._child_options())
        try:
            deadline = time.monotonic() + 5
            ready = self.lock_file.with_suffix(".ready")
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(ready.exists(), "Lock owner did not start")
            with self.assertRaises(RuntimeLockBusy):
                with runtime_lock(self.lock_file, timeout=0):
                    pass
            process.kill()
            process.communicate(timeout=5)
            with runtime_lock(self.lock_file, timeout=0):
                pass
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_busy_commands_leave_active_transactions_untouched(self):
        staging = self.root / ".cpu.core-updating"
        backup = self.root / ".cpu.reinstalling"
        for directory in (staging, backup):
            directory.mkdir()
            (directory / "marker").write_text("preserved")
        commands = (
            worker_bootstrap.cmd_runtime_info,
            worker_bootstrap.cmd_runtime_env_sizes,
            worker_bootstrap.cmd_install_runtime,
            worker_bootstrap.cmd_update_runtime_core,
            worker_bootstrap.cmd_activate_runtime,
            worker_bootstrap.cmd_delete_runtime,
        )
        with runtime_lock(self.lock_file), \
             mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.root), \
             mock.patch.object(worker_bootstrap, "runtime_lock", partial(runtime_lock, timeout=0)), \
             mock.patch.object(worker_bootstrap, "_recover_runtime_transactions") as recover, \
             mock.patch.object(worker_bootstrap, "_target_runtime_from_payload") as target, \
             mock.patch.object(worker_bootstrap, "_runtime_info_payload") as info, \
             mock.patch.object(worker_bootstrap, "_env_size_targets") as sizes:
            for command in commands:
                with self.subTest(command=command.__name__), contextlib.redirect_stdout(io.StringIO()) as output:
                    result = command({"backend": "cpu", "taskId": "runtime_lock_check"})
                    self.assertNotEqual(result, 0)
                    error = json.loads(output.getvalue())
                    self.assertEqual(error["payload"]["code"], "RUNTIME_BUSY")
                    self.assertTrue(error["payload"]["recoverable"])
            for action in (recover, target, info, sizes):
                action.assert_not_called()
        for directory in (staging, backup):
            self.assertEqual((directory / "marker").read_text(), "preserved")

    def test_query_recovers_orphan_staging_after_owner_exits(self):
        staging = self.root / ".cpu.core-updating"
        staging.mkdir()
        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.root), \
             mock.patch.object(worker_bootstrap, "_runtime_info_payload", return_value={}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(worker_bootstrap.cmd_runtime_info({}), 0)
        self.assertFalse(staging.exists())

    def test_command_holds_lock_through_query_and_releases_after_exception(self):
        def query(_payload, **_kwargs):
            with self.assertRaises(RuntimeLockBusy):
                with runtime_lock(self.lock_file, timeout=0):
                    pass
            raise ValueError("probe interrupted")

        with mock.patch.object(worker_bootstrap, "RUNTIME_ENVS_DIR", self.root), \
             mock.patch.object(worker_bootstrap, "_runtime_info_payload", side_effect=query):
            with self.assertRaisesRegex(ValueError, "probe interrupted"):
                worker_bootstrap.cmd_runtime_info({})
        with runtime_lock(self.lock_file, timeout=0):
            pass
