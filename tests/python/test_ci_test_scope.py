import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scope = _load("ci-test-scope")
runner = _load("run-python-tests")


class CiTestScopeTests(unittest.TestCase):
    def test_runtime_worker_modules_select_the_runtime_packaging_tests(self):
        for path in (
            "python/worker_bootstrap.py",
            "python/worker_optional_packages.py",
            "python/worker_runtime_trash.py",
            "python/worker_rocm_probe.py",
        ):
            self.assertEqual(scope.scope_for_path(path), {"frontend", "backend", "runtime"}, path)

    def test_other_worker_modules_skip_the_runtime_packaging_tests(self):
        for path in ("python/worker.py", "python/worker_audio.py", "python/worker_models.py"):
            self.assertEqual(scope.scope_for_path(path), {"frontend", "backend"}, path)

    def test_runtime_packaging_tests_select_themselves(self):
        for module in runner.RUNTIME_PACKAGING_MODULES:
            self.assertEqual(scope.scope_for_path(f"tests/python/{module}.py"), {"backend", "runtime"}, module)
        self.assertEqual(scope.scope_for_path("tests/python/test_worker_audio.py"), {"backend"})

    def test_unclassified_paths_still_run_every_check(self):
        for path in ("python/runtime-manifest.json", "scripts/prepare-python-runtime.ps1", "src-tauri/src/main.rs"):
            self.assertEqual(scope.scope_for_path(path), scope.ALL, path)
        self.assertEqual(scope.scope_for_path("README.md"), frozenset())


class PythonTestRunnerTests(unittest.TestCase):
    def test_fast_suite_excludes_only_the_runtime_packaging_modules(self):
        def modules(suite):
            return {runner._module_name(runner._first_test_id(item)) for item in suite}

        full = modules(runner.build_suite(skip_runtime_packaging=False))
        fast = modules(runner.build_suite(skip_runtime_packaging=True))
        self.assertTrue(runner.RUNTIME_PACKAGING_MODULES <= full)
        self.assertEqual(full - fast, runner.RUNTIME_PACKAGING_MODULES)

    def test_import_failures_are_attributed_to_their_module(self):
        self.assertEqual(runner._module_name("python.test_worker_audio.Tests.test_one"), "test_worker_audio")
        self.assertEqual(runner._module_name("unittest.loader._FailedTest.python.test_plugin_sources"), "test_plugin_sources")
