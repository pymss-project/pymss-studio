from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap  # noqa: F401

import worker_bootstrap as bootstrap
from worker_runtime_transactions import RocmTransaction, RuntimeRecoveryError, RuntimeSpaceError


class RuntimeTransactionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.active = self.root / "active-runtime.json"
        self.tx = RocmTransaction(self.root, self.active)
        self.current = self.root / "rocm"
        self.seed(self.current, "original")
        self.original_pointer = json.dumps({"backend": "rocm", "pythonPath": str(self.current / "Scripts/python.exe")}).encode()
        self.active.write_bytes(self.original_pointer)
        self.selection = self.root / ".overlays/rocm/active-overlay.json"
        self.selection.parent.mkdir(parents=True)
        self.selection.write_bytes(b'{"baseFingerprint":"original"}')

    def seed(self, directory, tag, transaction_id=None):
        (directory / "Scripts").mkdir(parents=True, exist_ok=True)
        (directory / "Scripts/python.exe").write_bytes(b"interpreter")
        (directory / "generation.txt").write_text(tag)
        state = {"backend": "rocm", "tag": tag, "profileId": "win-rocm721", "torchVersion": "2.9.1+rocm7.2.1"}
        if transaction_id:
            state["environmentTransactionId"] = transaction_id
        (directory / "pymss-runtime-state.json").write_text(json.dumps(state))

    def candidate(self, tx, tag):
        self.seed(self.current, tag, tx.record["id"])
        return {"backend": "rocm", "pythonPath": str(self.current / "Scripts/python.exe"),
                "environmentTransactionId": tx.record["id"]}

    def install(self, tag="new", *, activate=True, replace=False):
        self.tx.begin("install", activate=activate, replace_previous=replace, profile={"id": "win-rocm721", "revision": 1})
        active = self.candidate(self.tx, tag)
        self.tx.restore_selections({"selections": {}})
        self.tx.commit(active if activate else None)

    def tag(self, directory=None):
        return ((directory or self.current) / "generation.txt").read_text()

    def test_success_keeps_self_describing_previous_and_clears_old_overlay_selection(self):
        self.install()
        self.assertEqual(self.tag(), "new")
        self.assertEqual(self.tag(self.tx.previous / "environment"), "original")
        manifest = self.tx.previous_manifest()
        self.assertEqual(manifest["state"]["tag"], "original")
        self.assertEqual(manifest["profile"]["id"], "win-rocm721")
        self.assertEqual(base64.b64decode(manifest["snapshot"]["active"]), self.original_pointer)
        self.assertFalse(self.selection.exists())
        self.assertFalse(self.tx.journal.exists())

    def test_existing_previous_requires_explicit_replacement_then_retains_only_one(self):
        self.install("first")
        with self.assertRaisesRegex(ValueError, "explicitly"):
            self.tx.begin("install", activate=True)
        self.assertEqual(self.tag(), "first")
        self.assertEqual(self.tag(self.tx.previous / "environment"), "original")
        self.install("second", replace=True)
        self.assertEqual(self.tag(self.tx.previous / "environment"), "first")
        self.assertEqual([path.name for path in self.root.glob(".rocm-*")], [".rocm-previous"])

    def test_prepared_install_does_not_switch_another_backend_and_has_commit_evidence(self):
        self.active.write_bytes(b'{"backend":"cpu","pythonPath":"cpu/python"}')
        pointer = self.active.read_bytes()
        self.install(activate=False)
        self.assertEqual(self.active.read_bytes(), pointer)
        self.assertTrue((self.current / ".pymss-runtime-commit.json").is_file())
        self.assertFalse(RocmTransaction(self.root, self.active).recover())

    def test_failed_candidate_restores_files_pointer_and_overlay_selection(self):
        self.tx.begin("install", activate=True)
        self.candidate(self.tx, "failed")
        self.tx.restore_selections({"selections": {}})
        self.assertTrue(RocmTransaction(self.root, self.active).recover())
        self.assertEqual(self.tag(), "original")
        self.assertEqual(self.active.read_bytes(), self.original_pointer)
        self.assertEqual(self.selection.read_bytes(), b'{"baseFingerprint":"original"}')
        self.assertFalse(list(self.root.glob(".rocm-*") ))

    def test_rollback_swaps_versions_and_can_switch_back(self):
        self.install()
        for expected, previous in [("original", "new"), ("new", "original")]:
            record = self.tx.begin("rollback", activate=True)
            self.assertEqual(self.tag(), expected)
            state = json.loads((self.current / "pymss-runtime-state.json").read_text())
            state["environmentTransactionId"] = record["id"]
            (self.current / "pymss-runtime-state.json").write_text(json.dumps(state))
            self.tx.commit({"backend": "rocm", "pythonPath": str(self.current / "Scripts/python.exe"), "environmentTransactionId": record["id"]})
            self.assertEqual(self.tag(self.tx.previous / "environment"), previous)
        self.assertFalse(self.tx.journal.exists())

    def test_failed_rollback_restores_both_slots_and_original_active_pointer(self):
        self.install()
        pointer = self.active.read_bytes()
        self.tx.begin("rollback", activate=True)
        self.assertEqual(self.tag(), "original")
        RocmTransaction(self.root, self.active).recover()
        self.assertEqual(self.tag(), "new")
        self.assertEqual(self.tag(self.tx.previous / "environment"), "original")
        self.assertEqual(self.active.read_bytes(), pointer)

    def test_failed_rollback_without_current_restores_exact_previous_state(self):
        self.install()
        state_path = self.tx.previous / "environment/pymss-runtime-state.json"
        original_state = state_path.read_bytes()
        shutil.rmtree(self.current)
        self.tx.begin("rollback", activate=False)
        (self.current / "pymss-runtime-state.json").write_text(json.dumps({"environmentTransactionId": self.tx.record["id"]}))
        self.tx.abort()
        self.assertFalse(self.current.exists())
        self.assertEqual(state_path.read_bytes(), original_state)

    def test_commit_point_wins_even_when_following_journal_write_fails(self):
        self.tx.begin("install", activate=True)
        active = self.candidate(self.tx, "committed")
        with mock.patch("worker_runtime_transactions._write", side_effect=lambda path, data:
                        self.active.write_text(json.dumps(data)) if path == self.active else (_ for _ in ()).throw(OSError("journal unavailable"))):
            with self.assertRaisesRegex(OSError, "journal unavailable"):
                self.tx.commit(active)
        self.assertTrue(RocmTransaction(self.root, self.active).recover())
        self.assertEqual(self.tag(), "committed")
        self.assertEqual(self.tag(self.tx.previous / "environment"), "original")

    def test_cleanup_failure_after_commit_is_retryable_without_reverting_new_pointer(self):
        self.install("first")
        self.tx.begin("install", activate=True, replace_previous=True)
        active = self.candidate(self.tx, "second")
        with mock.patch("worker_runtime_transactions.shutil.rmtree", side_effect=PermissionError("files occupied")):
            with self.assertRaises(PermissionError):
                self.tx.commit(active)
        self.assertEqual(self.tag(), "second")
        RocmTransaction(self.root, self.active).recover()
        self.assertEqual(self.tag(), "second")
        self.assertEqual(self.tag(self.tx.previous / "environment"), "first")

    def test_restore_rename_failure_preserves_original_backup_and_original_error_context(self):
        self.tx.begin("install", activate=True)
        self.candidate(self.tx, "failed")
        rename = Path.rename

        def blocked(path, destination):
            if path.name == "environment":
                raise PermissionError("original is occupied")
            return rename(path, destination)

        with mock.patch.object(Path, "rename", blocked), self.assertRaises(PermissionError):
            self.tx.abort()
        self.assertTrue(self.tx.journal.is_file())
        self.assertTrue((self.tx._slot("backup") / "environment/generation.txt").is_file())
        RocmTransaction(self.root, self.active).recover()
        self.assertEqual(self.tag(), "original")

    def test_busy_initial_rename_does_not_delete_current_or_previous(self):
        self.install()
        with mock.patch.object(Path, "rename", side_effect=PermissionError("occupied")):
            with self.assertRaises(PermissionError):
                self.tx.begin("install", activate=True, replace_previous=True)
        RocmTransaction(self.root, self.active).recover()
        self.assertEqual(self.tag(), "new")
        self.assertEqual(self.tag(self.tx.previous / "environment"), "original")

    @unittest.skipUnless(os.name == "nt", "Windows directory sharing semantics")
    def test_real_windows_directory_handle_blocks_swap_until_closed(self):
        import ctypes
        from ctypes import wintypes

        # CreateFileW directory handles without FILE_SHARE_DELETE deny renames.
        # https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateFileW(str(self.current), 1, 3, None, 3, 0x02000000, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value, ctypes.get_last_error())
        try:
            with self.assertRaises(PermissionError):
                self.tx.begin("install", activate=True)
            self.assertEqual(self.tag(), "original")
            self.assertEqual(self.active.read_bytes(), self.original_pointer)
        finally:
            self.assertTrue(kernel.CloseHandle(handle))
        self.assertTrue(RocmTransaction(self.root, self.active).recover())
        self.assertEqual(self.tag(), "original")

    def test_space_preflight_keeps_both_versions_intact(self):
        self.install()
        with mock.patch("worker_runtime_transactions.shutil.disk_usage", return_value=shutil._ntuple_diskusage(100, 99, 1)):
            with self.assertRaises(RuntimeSpaceError):
                self.tx.preflight(20, 30)
        self.assertEqual(self.tag(), "new")
        self.assertEqual(self.tag(self.tx.previous / "environment"), "original")
        self.assertFalse(self.tx.journal.exists())

    def test_invalid_journal_and_changed_pointer_preserve_all_copies(self):
        self.tx.journal.write_text(json.dumps({"version": 1, "id": "../outside", "operation": "install",
                                             "activate": True, "hadCurrent": True, "snapshot": {}}))
        with self.assertRaises(RuntimeRecoveryError):
            RocmTransaction(self.root, self.active).recover()
        self.assertEqual(self.tag(), "original")
        self.tx.journal.unlink()
        self.tx.begin("install", activate=True)
        self.candidate(self.tx, "candidate")
        self.active.write_bytes(b'{"changed":"outside transaction"}')
        with self.assertRaisesRegex(RuntimeRecoveryError, "outside"):
            RocmTransaction(self.root, self.active).recover()
        self.assertTrue(self.tx.journal.is_file())
        self.assertEqual(self.tag(self.tx._slot("backup") / "environment"), "original")

    def test_reserved_python_paths_are_rejected_even_in_developer_mode(self):
        hidden = self.root / ".rocm-previous/environment/rocm/Scripts/python.exe"
        hidden.parent.mkdir(parents=True)
        hidden.touch()
        self.active.write_text(json.dumps({"backend": "rocm", "pythonPath": str(hidden), "debugOverride": True}))
        with mock.patch.object(bootstrap, "RUNTIME_ENVS_DIR", self.root), \
             mock.patch.object(bootstrap, "ACTIVE_RUNTIME_FILE", self.active), \
             mock.patch.object(bootstrap, "BUNDLED_RUNTIME_ENVS_DIR", None), \
             mock.patch.object(bootstrap, "ALLOW_DEBUG_RUNTIME_OVERRIDE", True):
            self.assertFalse(bootstrap._is_user_runtime_env(hidden.parent.parent))
            self.assertIsNone(bootstrap._read_runtime_state())

    def test_linked_transaction_paths_are_rejected(self):
        external = Path(self.enterContext(tempfile.TemporaryDirectory()))
        try:
            self.tx.previous.symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("Directory symlinks are unavailable")
        with self.assertRaises(RuntimeRecoveryError):
            self.tx.previous_manifest()
        self.assertTrue(external.is_dir())

    @unittest.skipUnless(os.name == "nt", "Windows short-path aliases")
    def test_short_root_alias_accepts_owned_paths_without_allowing_junctions(self):
        import ctypes

        root = self.root / "runtime environments with long names"
        root.mkdir()
        get_short = ctypes.windll.kernel32.GetShortPathNameW
        get_short.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        get_short.restype = ctypes.c_uint
        buffer = ctypes.create_unicode_buffer(32768)
        if not get_short(str(root), buffer, len(buffer)):
            self.skipTest("Short-path aliases are unavailable on this volume")
        alias = Path(buffer.value)
        if alias == root:
            self.skipTest("8.3 name generation is disabled on this volume")
        self.assertEqual(alias.resolve(), root.resolve())
        transaction = RocmTransaction(alias, alias / "active-runtime.json")
        self.assertEqual(transaction.checked(alias / "rocm"), root.resolve() / "rocm")
        with self.assertRaises(RuntimeRecoveryError):
            transaction.checked(self.root / "unmanaged")

        external = self.root / "external environment"
        external.mkdir()
        marker = external / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        link = root / "rocm"
        result = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(link), str(external)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaisesRegex(RuntimeRecoveryError, "links or junctions"):
            transaction.checked(alias / "rocm" / "keep.txt")
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    @unittest.skipUnless(os.name == "nt", "Windows extended path prefixes")
    def test_extended_root_commits_an_unprefixed_interpreter_path(self):
        root = self.root.resolve()
        extended = Path("\\\\?\\" + str(root))
        self.assertTrue(extended.samefile(root))
        self.tx = RocmTransaction(extended, self.active)
        self.install()
        self.assertEqual(self.tag(), "new")
        self.assertEqual(self.tag(self.tx.previous / "environment"), "original")
        self.assertFalse(self.tx.journal.exists())

    @unittest.skipUnless(os.name == "nt", "Windows extended path prefixes")
    def test_extended_root_cannot_commit_a_foreign_interpreter(self):
        self.tx = RocmTransaction(Path("\\\\?\\" + str(self.root.resolve())), self.active)
        self.tx.begin("install", activate=True)
        active = self.candidate(self.tx, "new")
        foreign = self.root / "foreign/Scripts/python.exe"
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(b"unrelated interpreter")
        active["pythonPath"] = str(foreign)
        with self.assertRaisesRegex(RuntimeRecoveryError, "commit could not be verified"):
            self.tx.commit(active)
        self.assertTrue(self.tx.journal.exists())
        self.assertEqual(self.tag(self.tx._slot("backup") / "environment"), "original")
        self.assertEqual(foreign.read_bytes(), b"unrelated interpreter")

    def test_real_process_crash_before_and_after_pointer_commit_recovers(self):
        code = """
import json, os, sys
from pathlib import Path
from worker_runtime_transactions import RocmTransaction, _write
root=Path(sys.argv[1]); tx=RocmTransaction(root, root/'active-runtime.json')
r=tx.begin('install', activate=True, replace_previous=True)
current=root/'rocm'; (current/'Scripts').mkdir(parents=True)
(current/'Scripts/python.exe').touch(); (current/'generation.txt').write_text('crash-candidate')
_write(current/'pymss-runtime-state.json', {'environmentTransactionId':r['id']})
if sys.argv[2]=='after':
 _write(root/'active-runtime.json', {'backend':'rocm','pythonPath':str(current/'Scripts/python.exe'),'environmentTransactionId':r['id']})
os._exit(23)
"""
        environment = {**os.environ, "PYTHONPATH": str(Path(bootstrap.__file__).parent)}
        for phase in ["before", "after"]:
            result = subprocess.run([sys.executable, "-c", code, str(self.root), phase], env=environment,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 23, result.stderr)
            recovery = RocmTransaction(self.root, self.active)
            self.assertTrue(recovery.recover())
            self.assertFalse(recovery.recover())
            self.assertEqual(self.tag(), "original" if phase == "before" else "crash-candidate")


if __name__ == "__main__":
    unittest.main()
