"""Compatibility entry point for the component-based Studio view builder."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    raise SystemExit(
        subprocess.call(
            [
                "node",
                str(Path(__file__).with_name("build_tool_views.mjs")),
                *sys.argv[1:],
            ]
        )
    )
