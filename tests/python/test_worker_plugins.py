from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap

import worker_operations
import worker_plugins as plugins


class PluginMarketplaceTests(unittest.TestCase):
    def setUp(self):
        work = Path(__file__).resolve().parents[2] / ".tmp" / "plugin-tests"
        work.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.environment = mock.patch.dict(os.environ, {
            "PYMSS_STUDIO_RUNTIME_ENVS_DIR": str(self.work / "runtimes"),
            "PYMSS_STUDIO_ACTIVE_RUNTIME_FILE": str(self.work / "active.json"),
            "PYMSS_PLUGINS_DIR": str(self.work / "manual"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        (self.work / "active.json").write_text('{"backend":"cpu"}')
        self.root = plugins._root()
        self.reset_runtime()
        self.addCleanup(self.reset_runtime)
        original_path = list(sys.path)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), original_path))

    @staticmethod
    def reset_runtime():
        import pymss.operations
        import pymss.plugins
        pymss.operations._DEFAULT_RUNTIME = None
        pymss.plugins.reset()

    def source(self, *, name="hello", version="1.0.0", broken=False, operation="example.hello", print_message=False):
        source = self.work / "sources" / name
        source.mkdir(parents=True, exist_ok=True)
        source.joinpath("pyproject.toml").write_text(
            f'[project]\nname="pymss-plugin-{name}"\nversion="{version}"\nrequires-python=">=3.10"\ndependencies=[]\n',
        )
        code = f'''def run(ctx, inputs, params):
    return {{"value": 42}}

def setup(plugin):
    plugin.register_operation(id={operation!r}, title="Hello", inputs={{}},
        params={{"type":"object", "properties":{{}}}},
        outputs={{"value":{{"type":"number"}}}}, run=run)
'''
        if broken:
            code += '    raise RuntimeError("setup failed deliberately")\n'
        if print_message:
            code = 'print("incidental plugin output")\n' + code
        source.joinpath("__init__.py").write_text(code)
        return source

    def manage(self, action, **payload):
        output = io.StringIO()
        with redirect_stdout(output):
            code = plugins.cmd_manage_plugin({"action": action, "taskId": "plugin_manage_test", **payload})
        events = [json.loads(line) for line in output.getvalue().splitlines() if line]
        return code, events

    def install(self, source):
        code, events = self.manage("install", target=str(source))
        self.assertEqual(code, 0, events)
        self.assertEqual(events[-1]["type"], "plugin_manage_result")
        return events[-1]["payload"]

    def test_install_from_local_source_and_run_through_worker(self):
        snapshot = self.install(self.source(print_message=True))
        self.assertTrue(snapshot["installed"][0]["loaded"])
        self.assertEqual(snapshot["installed"][0]["operations"], ["example.hello"])
        output = io.StringIO()
        with redirect_stdout(output):
            code = worker_operations.cmd_run_operation({
                "operation": "example.hello", "inputs": {}, "params": {},
                "outputDir": str(self.work / "output"), "requestId": "hello-run",
            })
        self.assertEqual(code, 0, output.getvalue())
        event = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(event["payload"]["outputs"]["value"]["value"], 42)
        self.assertEqual(os.environ["PYMSS_PLUGINS_DIR"], str(self.work / "manual"))

    def test_successful_update_replaces_source_and_version(self):
        source = self.source()
        self.install(source)
        self.source(version="2.0.0")
        code, events = self.manage("update", name="hello")
        self.assertEqual(code, 0, events)
        self.assertEqual(events[-1]["payload"]["installed"][0]["version"], "2.0.0")

    def test_failed_update_keeps_working_previous_version(self):
        source = self.source()
        self.install(source)
        original = (self.root / "installed/hello/__init__.py").read_text()
        self.source(version="2.0.0", broken=True)
        code, events = self.manage("update", name="hello")
        self.assertEqual(code, 1)
        self.assertIn("setup failed deliberately", events[-1]["payload"]["message"])
        self.assertEqual((self.root / "installed/hello/__init__.py").read_text(), original)
        self.assertEqual(plugins._manifest(self.root / "installed/hello")["version"], "1.0.0")

    def test_failing_dependency_install_does_not_publish_source(self):
        with mock.patch.object(plugins, "_install_dependencies", side_effect=RuntimeError("dependency conflict")):
            code, events = self.manage("install", target=str(self.source()))
        self.assertEqual(code, 1)
        self.assertFalse((self.root / "installed/hello").exists())
        self.assertIn("dependency conflict", events[-1]["payload"]["message"])

    def test_uninstall_removes_source_and_sidecar_without_touching_manual_plugins(self):
        self.install(self.source())
        deps = self.root / "installed/hello/.studio-deps"
        deps.mkdir()
        deps.joinpath("example.py").write_text("value=1")
        manual = self.work / "manual/keep"
        manual.mkdir(parents=True)
        manual.joinpath("__init__.py").write_text("")
        code, events = self.manage("uninstall", name="hello")
        self.assertEqual(code, 0, events)
        self.assertFalse(deps.parent.exists())
        self.assertTrue(manual.exists())

    def test_invalid_names_cannot_escape_installation_directory(self):
        for name in ["../hello", "/hello", "hello/../../", "", ".hidden", "a\\b"]:
            with self.subTest(name=name):
                code, events = self.manage("uninstall", name=name)
                self.assertEqual(code, 1)
                self.assertEqual(events[-1]["payload"]["code"], "PLUGIN_MANAGE_FAILED")

    def test_repair_preserves_original_update_source(self):
        source = self.source()
        self.install(source)
        code, events = self.manage("repair", name="hello")
        self.assertEqual(code, 0, events)
        self.assertEqual(plugins._manifest(self.root / "installed/hello")["url"], str(source))

    def test_catalog_failure_still_returns_installed_plugins(self):
        self.install(self.source())
        from pymss.plugins import install as installer
        output = io.StringIO()
        with mock.patch.object(installer, "list_available", side_effect=RuntimeError("offline")), redirect_stdout(output):
            code = plugins.cmd_plugin_marketplace({})
        snapshot = json.loads(output.getvalue())["payload"]
        self.assertEqual(code, 0)
        self.assertEqual(snapshot["catalogError"], "offline")
        self.assertEqual(snapshot["installed"][0]["name"], "hello")

    def test_catalog_normalizes_search_metadata(self):
        from pymss.plugins import install as installer
        output = io.StringIO()
        with mock.patch.object(installer, "list_available", return_value=[{
            "name": "hello", "url": "https://example.com/plugin", "description": "A test", "tags": ["audio"],
        }]), redirect_stdout(output):
            code = plugins.cmd_plugin_marketplace({})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["payload"]["catalog"][0]["tags"], ["audio"])

    def test_recovery_restores_previous_version_after_interrupted_publication(self):
        self.install(self.source())
        backup = self.root / "backups/hello"
        backup.parent.mkdir(exist_ok=True)
        (self.root / "installed/hello").rename(backup)
        snapshot = plugins._inventory(self.root)
        self.assertTrue(snapshot["installed"][0]["loaded"])
        self.assertFalse(backup.exists())

    def test_duplicate_operation_rejected_without_breaking_existing_plugin(self):
        self.install(self.source())
        code, events = self.manage("install", target=str(self.source(name="duplicate")))
        self.assertEqual(code, 1, events)
        self.assertFalse((self.root / "installed/duplicate").exists())
        self.assertTrue(plugins._inventory(self.root)["installed"][0]["loaded"])

    def test_builtin_operation_collision_rejected(self):
        self.source(operation="pymss.audio.adjust_volume")
        # Use an actual built-in id rather than assuming the DSP namespace.
        from pymss.operations import default_runtime
        builtin = default_runtime().list_operations()[0]["id"]
        code, events = self.manage("install", target=str(self.source(operation=builtin)))
        self.assertEqual(code, 1, events)
        self.assertFalse((self.root / "installed/hello").exists())

    def test_backend_and_interpreter_both_scope_plugin_installations(self):
        (self.work / "active.json").write_text('{"backend":"mlx"}')
        self.assertNotEqual(self.root, plugins._root())
        (self.work / "active.json").write_text('{"backend":"cpu"}')
        with mock.patch.object(sys, "executable", str(self.work / "another-venv/bin/python")):
            self.assertNotEqual(self.root, plugins._root())

    def test_probe_failure_is_visible_on_installed_row(self):
        self.install(self.source())
        with mock.patch.object(plugins, "_probe", side_effect=RuntimeError("plugin probe timed out")):
            snapshot = plugins._inventory(self.root)
        self.assertIsNone(snapshot["installed"][0]["loaded"])
        self.assertIn("timed out", snapshot["verificationError"])

    def test_local_source_update_check_compares_its_declared_version(self):
        source = self.source()
        self.install(source)
        self.source(version="2.0.0")
        code, events = self.manage("check_updates")
        self.assertEqual(code, 0, events)
        update = events[-1]["payload"]["updates"]["hello"]
        self.assertFalse(update["up_to_date"])
        self.assertEqual(update["remote_version"], "2.0.0")

    def test_manual_plugin_is_visible_and_operation_collision_names_the_owner(self):
        manual = self.work / "manual/manual-hello"
        manual.parent.mkdir(parents=True)
        shutil.copytree(self.source(), manual)
        code, events = self.manage("install", target=str(self.source(name="managed-hello")))
        self.assertEqual(code, 1, events)
        self.assertIn("manually installed plugin manual-hello", events[-1]["payload"]["message"])
        row = plugins._inventory(self.root)["installed"][0]
        self.assertEqual(row["name"], "manual-hello")
        self.assertFalse(row["managed"])
        self.assertTrue(row["loaded"])

    def test_recovery_cleans_abandoned_install_and_uninstall_transactions(self):
        for name in ["install-abandoned", "removed-old"]:
            directory = self.root / "transactions" / name
            directory.mkdir(parents=True)
            directory.joinpath("partial-file").write_text("left behind")
        plugins._recover(self.root)
        self.assertEqual(list(self.root.joinpath("transactions").iterdir()), [])

    def test_sidecar_activation_keeps_host_paths_first(self):
        deps = self.root / "installed/hello/.studio-deps"
        deps.mkdir(parents=True)
        before = list(sys.path)
        plugins.activate_plugin_dependencies(self.root)
        self.assertEqual(sys.path[:-1], before)
        self.assertEqual(sys.path[-1], str(deps))

    def test_inventory_does_not_hold_exclusive_lock_while_probing(self):
        from worker_runtime_lock import runtime_lock
        def probe(root):
            with runtime_lock(root / ".lock", timeout=0):
                return []
        with mock.patch.object(plugins, "_probe", side_effect=probe):
            self.assertIsNone(plugins._inventory(self.root)["verificationError"])

    def test_local_source_parent_can_contain_at_sign(self):
        original = self.source()
        source = self.work / "user@example.com/hello"
        source.parent.mkdir()
        shutil.copytree(original, source)
        self.assertTrue(self.install(source)["installed"][0]["loaded"])

    def test_readers_do_not_wait_or_recover_a_foreign_live_transaction(self):
        import subprocess
        import time
        live = self.root / "transactions/install-live"
        live.mkdir(parents=True)
        live.joinpath("marker").write_text("active writer")
        script = "from pathlib import Path; import sys; from worker_runtime_lock import runtime_lock\nwith runtime_lock(Path(sys.argv[1])):\n print('locked', flush=True)\n sys.stdin.read(1)"
        environment = dict(os.environ, PYTHONPATH=str(Path(plugins.__file__).parent))
        child = subprocess.Popen([sys.executable, "-u", "-c", script, str(self.root / ".lock")],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env=environment)
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            started = time.monotonic()
            snapshot = plugins._inventory(self.root, verify=False)
            runtime = plugins.operation_runtime()
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(snapshot["installed"], [])
            self.assertGreaterEqual(len(runtime.list_operations()), 4)
            self.assertTrue(live.joinpath("marker").exists())
        finally:
            child.communicate(input="x", timeout=5)

    def test_read_only_recovery_failure_does_not_hide_installed_plugins(self):
        self.install(self.source())
        with mock.patch.object(plugins, "runtime_lock", side_effect=PermissionError("read-only")):
            snapshot = plugins._inventory(self.root)
            runtime = plugins.operation_runtime()
        self.assertTrue(snapshot["installed"][0]["loaded"])
        self.assertIn("example.hello", [item["id"] for item in runtime.list_operations()])

    def test_missing_local_source_is_not_looked_up_in_the_registry(self):
        from pymss.plugins import install as installer
        with mock.patch.object(installer, "install", side_effect=AssertionError("unexpected registry lookup")):
            with self.assertRaisesRegex(ValueError, "Local plugin source folder is unavailable"):
                plugins._acquire_source(str(self.work / "missing/plugin"), self.work / "destination")

    def test_deleted_update_source_preserves_the_installed_plugin(self):
        source = self.source()
        self.install(source)
        shutil.rmtree(source)
        code, events = self.manage("update", name="hello")
        self.assertEqual(code, 1)
        self.assertIn("Local plugin source folder is unavailable", events[-1]["payload"]["message"])
        self.assertTrue(plugins._inventory(self.root)["installed"][0]["loaded"])

    def test_probe_failure_preserves_manual_plugin_names_with_unknown_status(self):
        manual = self.work / "manual/hello"
        manual.parent.mkdir(parents=True)
        shutil.copytree(self.source(), manual)
        with mock.patch.object(plugins, "_probe", side_effect=RuntimeError("probe timeout")):
            snapshot = plugins._inventory(self.root)
        self.assertEqual(snapshot["installed"][0]["name"], "hello")
        self.assertFalse(snapshot["installed"][0]["managed"])
        self.assertIsNone(snapshot["installed"][0]["loaded"])

    def test_ssh_git_reference_is_separate_from_authentication_and_subpath(self):
        from pymss.plugins import install as installer
        with mock.patch.object(installer, "install") as install:
            plugins._acquire_source("ssh://git@example.com/repo.git@v1#plugins/hello", self.work)
        self.assertEqual(install.call_args.args[0], "ssh://git@example.com/repo.git#plugins/hello")
        self.assertEqual(install.call_args.kwargs["ref"], "v1")
        with mock.patch.object(installer, "install") as install:
            plugins._acquire_source("git@example.com:repo.git@feature/branch", self.work)
        self.assertEqual(install.call_args.kwargs["ref"], "feature/branch")

    def test_network_update_check_does_not_hold_the_writer_lock(self):
        from pymss.plugins import install as installer
        from worker_runtime_lock import runtime_lock
        self.install(self.source())
        manifest_path = self.root / "installed/hello/.pymss-install.json"
        metadata = json.loads(manifest_path.read_text())
        metadata.update(source="url", url="https://example.com/hello.git")
        manifest_path.write_text(json.dumps(metadata))
        def check(*args, **kwargs):
            with runtime_lock(self.root / ".lock", timeout=0):
                return {"remote_version": "2.0.0", "up_to_date": False}
        with mock.patch.object(installer, "check_update", side_effect=check):
            code, events = self.manage("check_updates")
        self.assertEqual(code, 0, events)
        self.assertFalse(events[-1]["payload"]["updates"]["hello"]["up_to_date"])

    def test_source_without_entrypoint_is_not_installed(self):
        source = self.source()
        source.joinpath("__init__.py").unlink()
        code, events = self.manage("install", target=str(source))
        self.assertEqual(code, 1, events)
        self.assertFalse((self.root / "installed/hello").exists())

    def test_operation_runtime_can_be_queried_twice_in_same_process(self):
        self.install(self.source())
        first = plugins.operation_runtime()
        second = plugins.operation_runtime()
        self.assertIs(first, second)
        self.assertIn("example.hello", [item["id"] for item in second.list_operations()])

    def test_real_pip_installs_a_local_wheel_into_the_plugin_sidecar(self):
        import zipfile
        source = self.source()
        wheel = self.work / "studio_market_test_dep-1.0.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("studio_market_test_dep/__init__.py", "VALUE = 123\n")
            archive.writestr("studio_market_test_dep-1.0.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: studio-market-test-dep\nVersion: 1.0.0\n")
            archive.writestr("studio_market_test_dep-1.0.0.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: unittest\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            archive.writestr("studio_market_test_dep-1.0.0.dist-info/RECORD", "")
        requirement = f"studio-market-test-dep @ {wheel.as_uri()}"
        source.joinpath("pyproject.toml").write_text(
            '[project]\nname="hello"\nversion="1.0.0"\ndependencies=[' + json.dumps(requirement) + ']\n',
        )
        entry = source / "__init__.py"
        entry.write_text("import studio_market_test_dep\n" + entry.read_text())
        snapshot = self.install(source)
        self.assertTrue(snapshot["installed"][0]["loaded"])
        self.assertTrue((self.root / "installed/hello/.studio-deps/studio_market_test_dep").is_dir())
        import importlib.metadata
        with self.assertRaises(importlib.metadata.PackageNotFoundError):
            importlib.metadata.version("studio-market-test-dep")

    def test_dependency_resolution_freezes_host_and_installs_only_into_sidecar(self):
        source = self.source()
        source.joinpath("pyproject.toml").write_text('[project]\ndependencies=["tiny-extra>=1"]\n')
        import importlib.metadata
        fake_host = mock.Mock(metadata={"Name": "numpy"}, version="2.0.0")
        calls = []
        def run(command, task_id):
            calls.append(command)
            if "--report" in command:
                Path(command[command.index("--report") + 1]).write_text(json.dumps({"install": [
                    {"metadata": {"name": "tiny-extra", "version": "1.2.0"}},
                ]}))
        with mock.patch.object(importlib.metadata, "distributions", return_value=[fake_host]), \
             mock.patch.object(plugins, "_run_process", side_effect=run), redirect_stdout(io.StringIO()):
            plugins._install_dependencies(source, self.root, "plugin_manage_test", {"mirror": "pypi"})
        self.assertEqual(calls[0][:3], [sys.executable, "-m", "pip"])
        self.assertIn("numpy==2.0.0", source.parent.joinpath("constraints.txt").read_text())
        self.assertIn("--dry-run", calls[0])
        self.assertIn("--no-deps", calls[1])
        self.assertEqual(calls[1][calls[1].index("--target") + 1], str(source / ".studio-deps"))


if __name__ == "__main__":
    unittest.main()
