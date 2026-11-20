from __future__ import annotations

import os
import sys
from pathlib import Path


def _rename_no_replace(temporary: Path, path: Path) -> None:
    if os.name == "nt":
        os.rename(temporary, path)
        return
    import ctypes
    import errno

    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = getattr(library, "renamex_np", None)
        arguments = (os.fsencode(temporary), os.fsencode(path), 0x4)  # RENAME_EXCL
        signature = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    elif sys.platform.startswith("linux"):
        rename = getattr(library, "renameat2", None)
        arguments = (-100, os.fsencode(temporary), -100, os.fsencode(path), 1)  # AT_FDCWD, RENAME_NOREPLACE
        signature = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    else:
        rename = None
    if rename is None:
        raise OSError(errno.ENOTSUP, "Atomic file publishing is unavailable", str(path))
    rename.argtypes = signature
    rename.restype = ctypes.c_int
    if rename(*arguments) != 0:
        code = ctypes.get_errno() or errno.EIO
        raise OSError(code, os.strerror(code), str(path))


def publish_file_once(temporary: Path, path: Path) -> None:
    """Publish a complete file. An existing destination always wins."""
    try:
        os.link(temporary, path)
    except FileExistsError:
        raise
    except OSError:
        _rename_no_replace(temporary, path)
        return
    try:
        temporary.unlink(missing_ok=True)
    except OSError:
        # The destination is complete and the caller can clean up its staging file.
        pass
