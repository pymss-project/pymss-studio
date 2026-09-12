from __future__ import annotations

import errno
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class RuntimeLockBusy(RuntimeError):
    pass


@contextmanager
def runtime_lock(path: Path, *, timeout: float = 5.0, allow_read_only: bool = False) -> Iterator[bool]:
    """Yield whether repairs are allowed; read-only queries never use a separate lock file."""
    writable = True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
    except OSError as exc:
        if not allow_read_only or exc.errno not in {errno.EACCES, errno.EPERM, errno.EROFS}:
            raise
        writable = False
        try:
            # An existing lock must still exclude writers, even if we can only read it.
            handle = path.open("rb")
        except FileNotFoundError:
            # A read-only installation may never have had a lock file. Allow observation
            # only: no recovery, cache persistence, or environment repair is safe here.
            yield False
            return
    # Keep the lock file after release: unlinking it could create two independently locked
    # inodes while another process still holds the original file open.
    with handle:
        if os.name == "nt":
            import msvcrt
            # Windows byte locks may extend beyond EOF. Avoid an initialization write,
            # which could race a reader already holding the lock on an empty file.
        else:
            import fcntl

        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeLockBusy("Runtime environment management is busy") from exc
                time.sleep(min(0.05, remaining))
        try:
            yield writable
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
