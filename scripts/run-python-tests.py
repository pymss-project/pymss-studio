"""Run the Python test suite, optionally without the runtime packaging modules.

The runtime packaging modules create virtual environments and run real pip installs, which
takes several minutes. CI runs them only when runtime code changes (see ci-test-scope.py).
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"

RUNTIME_PACKAGING_MODULES = frozenset({
    "test_runtime_core_pip",
    "test_rocm_runtime_packaging",
    "test_runtime_artifacts",
    "test_worker_optional_packages",
})


def _first_test_id(suite: unittest.TestSuite | unittest.TestCase) -> str:
    if isinstance(suite, unittest.TestCase):
        return suite.id()
    for item in suite:
        found = _first_test_id(item)
        if found:
            return found
    return ""


def _module_name(test_id: str) -> str:
    # Discovered ids are "python.<module>.<Class>.<test>"; import failures are reported as
    # "unittest.loader._FailedTest.python.<module>".
    parts = test_id.split(".")
    return parts[parts.index("python") + 1] if "python" in parts[:-1] else ""


def build_suite(skip_runtime_packaging: bool) -> unittest.TestSuite:
    discovered = unittest.defaultTestLoader.discover(
        str(TESTS_DIR / "python"), pattern="test_*.py", top_level_dir=str(TESTS_DIR),
    )
    if not skip_runtime_packaging:
        return discovered
    return unittest.TestSuite(
        module for module in discovered
        if _module_name(_first_test_id(module)) not in RUNTIME_PACKAGING_MODULES
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--skip-runtime-packaging", action="store_true",
        help="exclude the modules that build virtual environments and run pip",
    )
    args = parser.parse_args()
    if args.skip_runtime_packaging:
        print("Skipping runtime packaging modules:", ", ".join(sorted(RUNTIME_PACKAGING_MODULES)))
    result = unittest.TextTestRunner(verbosity=1).run(build_suite(args.skip_runtime_packaging))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
