"""Stdlib entry point for a prepared Python command or protocol peer."""
import runpy
import sys
from pathlib import Path

root, script = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
if not script.is_relative_to(root) or script.suffix != ".py":
    raise SystemExit("Invalid Python process entry point")
sys.argv = [str(script), *sys.argv[3:]]
sys.path[:0] = list(dict.fromkeys([str(script.parent), str(root)]))
runpy.run_path(str(script), run_name="__main__")
