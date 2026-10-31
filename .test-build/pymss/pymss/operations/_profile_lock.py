"""Process leases for stable Python profiles; lock files outlive environments."""
from contextlib import contextmanager
import os
from pathlib import Path


@contextmanager
def profile_lock(profile: Path, *, exclusive: bool = False):
    path = profile.parent / ("." + profile.name + ".lock")
    with path.open("a+b") as stream:
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            class Overlapped(ctypes.Structure):
                _fields_ = [("internal", ctypes.c_size_t), ("internal_high", ctypes.c_size_t),
                            ("offset", wintypes.DWORD), ("offset_high", wintypes.DWORD), ("event", wintypes.HANDLE)]

            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
            api.LockFileEx.restype = wintypes.BOOL
            api.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                        wintypes.DWORD, ctypes.POINTER(Overlapped)]
            api.UnlockFileEx.restype = wintypes.BOOL
            handle, overlapped = msvcrt.get_osfhandle(stream.fileno()), Overlapped()
            if not api.LockFileEx(handle, 1 | (2 if exclusive else 0), 0, 1, 0, ctypes.byref(overlapped)):
                error = ctypes.get_last_error()
                if error in {32, 33, 158}:
                    raise BlockingIOError("Python profile is in use")
                raise ctypes.WinError(error)
            try:
                yield
            finally:
                api.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))
        else:
            import fcntl

            fcntl.flock(stream, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)
