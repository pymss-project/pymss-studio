"""Platform facts that do not depend on Windows Management Instrumentation."""
from __future__ import annotations

import os
import platform
import sys


def machine() -> str:
    if sys.platform == "win32":
        # Match platform.machine's native-architecture fallback, including WOW64.
        return os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE", "")
    return platform.machine()


def description() -> str:
    if sys.platform == "win32":
        version = sys.getwindowsversion()
        return f"Windows-{version.major}.{version.minor}.{version.build}-{machine()}"
    return platform.platform()
