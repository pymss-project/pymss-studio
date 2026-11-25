"""Select CI jobs from the actual checkout diff; unknown changes run all checks."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


ALL = frozenset({"frontend", "backend", "native", "runtime"})
NATIVE_SMOKES = {
    "tests/manual/aax_module_smoke.py",
    "tests/manual/test_vst_real_plugin_smoke.py",
    "tests/manual/vst_processing_restrictions_smoke.py",
    "tests/manual/vst_real_plugin_smoke.py",
}
BACKEND_SMOKES = {
    "tests/manual/linux_appimage_update_smoke.py",
    "tests/manual/worker_native_cancellation_smoke.py",
}
DOCUMENTS = {"README.md", "README.zh-CN.md", "CONTRIBUTING.md"}
# Worker modules exercised by the runtime packaging tests, which build real virtual
# environments; keep in sync with RUNTIME_PACKAGING_MODULES in run-python-tests.py.
RUNTIME_WORKER_MODULES = {
    "worker_bootstrap", "worker_optional_packages", "worker_platform", "worker_protocol", "worker_proxy",
}
RUNTIME_TESTS = {
    "tests/python/test_runtime_core_pip.py",
    "tests/python/test_rocm_runtime_packaging.py",
    "tests/python/test_runtime_artifacts.py",
    "tests/python/test_worker_optional_packages.py",
}


def scope_for_path(path: str) -> frozenset[str]:
    if path in DOCUMENTS or (path.startswith(("docs/", ".github/release-notes/")) and path.endswith(".md")):
        return frozenset()
    if path in NATIVE_SMOKES:
        return frozenset({"native"})
    if path in RUNTIME_TESTS:
        return frozenset({"backend", "runtime"})
    if path in BACKEND_SMOKES or path.startswith("tests/python/"):
        return frozenset({"backend"})
    # These frontend tests also round-trip workflows through the real Python core.
    if path.startswith(("src/litegraph/", "src/workflows/", "tests/fixtures/comfy-mss/")) or path in {
        "tests/litegraphRoundtrip.test.mjs", "tests/_litegraphEnv.mjs",
    }:
        return frozenset({"frontend", "backend"})
    if path == "vite.config.ts":
        # Python also checks that generated plugin resources are excluded from Vite's watcher.
        return frozenset({"frontend", "backend"})
    if path.startswith(("src/", "tests/fixtures/")) or path in {"index.html", "tsconfig.json"}:
        return frozenset({"frontend"})
    if re.fullmatch(r"tests/[^/]+\.(?:ts|mjs|js)", path):
        return frozenset({"frontend"})
    worker = re.fullmatch(r"python/(worker(?:_[^/]+)?)\.py", path)
    if worker:
        name = worker.group(1)
        if name in RUNTIME_WORKER_MODULES or name.startswith(("worker_runtime_", "worker_rocm")):
            return frozenset({"frontend", "backend", "runtime"})
        return frozenset({"frontend", "backend"})
    return ALL


def selected_scopes(event: str, base: str) -> frozenset[str]:
    if event not in {"push", "pull_request"} or not re.fullmatch(r"[0-9a-f]{40}", base) or base == "0" * 40:
        return ALL
    try:
        check = subprocess.run(["git", "cat-file", "-e", f"{base}^{{commit}}"], capture_output=True, timeout=10)
        if check.returncode:
            subprocess.run(["git", "fetch", "--no-tags", "--depth=1", "origin", base],
                           capture_output=True, check=True, timeout=30)
        # For PRs HEAD is the merge checkout. Treat renames as separate deletion
        # and addition so a move out of a scope still triggers that scope.
        changed = subprocess.check_output(["git", "diff", "--name-only", "--no-renames", "-z", base, "HEAD", "--"], timeout=15)
    except (OSError, subprocess.SubprocessError):
        print("Unable to compare against the base commit; running all checks.")
        return ALL
    return frozenset().union(*(scope_for_path(os.fsdecode(path)) for path in changed.split(b"\0") if path))


if __name__ == "__main__":
    scopes = selected_scopes(os.environ.get("GITHUB_EVENT_NAME", ""), os.environ.get("BASE_SHA", ""))
    print("Selected CI scopes:", ", ".join(sorted(scopes)) or "none")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for scope in sorted(ALL):
            output.write(f"{scope}={'true' if scope in scopes else 'false'}\n")
