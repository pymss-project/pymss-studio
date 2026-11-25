"""Deferred removal of deleted runtime environments.

Removing a multi-gigabyte virtual environment file by file can take minutes on Windows.
A deleted environment is first moved into a trash directory on the same volume, which is
immediate, and its files are then removed by a detached process that holds no runtime lock.
Anything left behind by an interrupted purge is removed by the next one.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from worker_runtime_lock import RuntimeLockBusy, runtime_lock

TRASH_DIR_NAME = ".pymss-trash"
_PURGE_LOCK_NAME = ".purge.lock"


def trash_root(parent: Path) -> Path:
    return parent / TRASH_DIR_NAME


def move_to_trash(directory: Path) -> Path:
    """Move ``directory`` into the trash beside it. Raises ``OSError`` while it is in use."""
    root = trash_root(directory.parent)
    root.mkdir(exist_ok=True)
    target = root / f"{directory.name}-{uuid.uuid4().hex[:12]}"
    directory.rename(target)
    return target


def has_pending(root: Path) -> bool:
    try:
        return any(entry.name != _PURGE_LOCK_NAME for entry in root.iterdir())
    except OSError:
        return False


def purge(root: Path) -> None:
    """Remove everything in the trash. A purge already running on ``root`` takes precedence."""
    if not root.is_dir():
        return
    try:
        with runtime_lock(root / _PURGE_LOCK_NAME, timeout=0):
            for entry in root.iterdir():
                if entry.name == _PURGE_LOCK_NAME:
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    try:
                        entry.unlink()
                    except OSError:
                        continue
    except (RuntimeLockBusy, OSError):
        return


def start_purge(root: Path) -> None:
    """Purge ``root`` in a detached process so the caller can return immediately."""
    if not has_pending(root):
        return
    if os.name == "nt":
        detached = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        # Query workers run in a job that is terminated when the query returns; the purge leaves
        # it. A job that does not permit breakaway rejects the flag, so retry without it.
        attempts = [{"creationflags": detached | subprocess.CREATE_BREAKAWAY_FROM_JOB}, {"creationflags": detached}]
    else:
        attempts = [{"start_new_session": True}]
    for options in attempts:
        try:
            # The child must not inherit the worker's stdout: the host reads it until EOF.
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), str(root)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                **options,
            )
            return
        except OSError:
            continue
    # The trash stays in place and is purged on the next start.


if __name__ == "__main__":
    purge(Path(sys.argv[1]))
