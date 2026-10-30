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
        self.plugin_manifest = self.work / "plugin-manifest.json"
        self.plugin_manifest.write_text(json.dumps({"schemaVersion": 1, "repository": {"url": "https://github.com/example/plugins", "ref": "main"}, "preinstalled": []}), encoding="utf-8")
        self.manifest_patch = mock.patch.object(plugins, "_PLUGIN_MANIFEST", self.plugin_manifest)
        self.manifest_patch.start()
        self.addCleanup(self.manifest_patch.stop)
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

    @staticmethod
    def manifest(name, version="1.0.0", operation=None, dependencies="[]"):
        operation = operation or f"{name}.hello"
        return (
            f'[project]\nname="pymss-plugin-{name}"\nversion="{version}"\nrequires-python=">=3.10"\ndependencies={dependencies}\n\n'
            f'[tool.pymss.plugin]\nid="{name}"\napi_version=2\n\n'
            f'[[tool.pymss.plugin.operations]]\nid="{operation}"\ntitle="Hello"\n'
        )

    def source(self, *, name="hello", version="1.0.0", broken=False, operation=None, print_message=False):
        operation = operation or f"{name}.hello"
        source = self.work / "sources" / name
        source.mkdir(parents=True, exist_ok=True)
        source.joinpath("pyproject.toml").write_text(PluginMarketplaceTests.manifest(name, version, operation))
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
        self.assertEqual(snapshot["installed"][0]["operations"], ["hello.hello"])
        self.assertEqual(snapshot["installed"][0]["operationTitles"], {"hello.hello": "Hello"})
        self.assertEqual(snapshot["installed"][0]["pluginId"], "hello")
        output = io.StringIO()
        with redirect_stdout(output):
            code = worker_operations.cmd_run_operation({
                "operation": "hello.hello", "inputs": {}, "params": {},
                "outputDir": str(self.work / "output"), "requestId": "hello-run",
            })
        self.assertEqual(code, 0, output.getvalue())
        event = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(event["payload"]["outputs"]["value"]["value"], 42)
        self.assertEqual(os.environ["PYMSS_PLUGINS_DIR"], str(self.work / "manual"))

    def test_incompatible_runtime_is_rejected_before_acquiring_or_migrating_plugins(self):
        from pymss.plugins import manifest

        legacy = self.root / "installed/oldtool"
        legacy.mkdir(parents=True)
        legacy_manifest = '[tool.pymss.plugin]\nid="oldtool"\napi_version=1\n'
        (legacy / "pyproject.toml").write_text(legacy_manifest)
        with mock.patch.object(manifest, "API_VERSION", 1), \
             mock.patch.object(plugins, "_acquire_source", side_effect=AssertionError("source acquired")) as acquire:
            code, events = self.manage("install", target=str(self.source()))
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(events[-1]["taskId"], "plugin_manage_test")
        self.assertEqual(events[-1]["payload"]["code"], "PLUGIN_RUNTIME_INCOMPATIBLE")
        self.assertTrue(events[-1]["payload"]["recoverable"])
        acquire.assert_not_called()
        self.assertTrue(legacy.is_dir())
        self.assertEqual((legacy / "pyproject.toml").read_text(), legacy_manifest)
        self.assertFalse((self.root / "legacy-v1").exists())

    def test_verification_rejects_the_legacy_runtime_loader_before_spawning(self):
        from pymss.operations import Runtime

        def legacy_loader(runtime):
            return []

        with mock.patch.object(Runtime, "load_plugins", legacy_loader), \
             mock.patch.object(plugins.subprocess, "run", side_effect=AssertionError("unexpected probe")) as run:
            with self.assertRaises(plugins.PluginRuntimeIncompatible):
                plugins._probe(self.root)
        run.assert_not_called()

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
        for name in ["../hello", "/hello", "hello/../../", "", ".hidden", "a\\b", "hello.", "a..b", "Upper"]:
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

    def test_configured_registry_is_passed_explicitly_and_environment_override_wins(self):
        from pymss.plugins import install as installer
        os.environ.pop("PYMSS_PLUGINS_REGISTRY", None)
        with plugins._plugin_registry():
            self.assertEqual(plugins._REGISTRY_URL.get(), "https://raw.githubusercontent.com/example/plugins/main/registry.json")
            self.assertNotIn("PYMSS_PLUGINS_REGISTRY", os.environ)
        self.assertIsNone(plugins._REGISTRY_URL.get())
        with mock.patch.object(installer, "list_available", return_value=[]) as list_available, redirect_stdout(io.StringIO()):
            plugins.cmd_plugin_marketplace({})
        list_available.assert_called_once_with("https://raw.githubusercontent.com/example/plugins/main/registry.json")
        with mock.patch.dict(os.environ, {"PYMSS_PLUGINS_REGISTRY": "https://override/registry.json"}):
            with plugins._plugin_registry():
                self.assertEqual(plugins._REGISTRY_URL.get(), "https://override/registry.json")

    def test_inventory_only_marketplace_query_does_not_require_registry_config(self):
        with mock.patch.object(plugins, "_PLUGIN_MANIFEST", self.work / "missing.json"):
            output = io.StringIO()
            with redirect_stdout(output):
                code = plugins.cmd_plugin_marketplace({"includeCatalog": False})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["payload"]["catalog"], [])

    def test_recovery_restores_previous_version_after_interrupted_publication(self):
        self.install(self.source())
        backup = self.root / "backups/hello"
        backup.parent.mkdir(exist_ok=True)
        (self.root / "installed/hello").rename(backup)
        snapshot = plugins._inventory(self.root)
        self.assertTrue(snapshot["installed"][0]["loaded"])
        self.assertFalse(backup.exists())

    def test_second_source_with_the_same_plugin_id_is_rejected(self):
        source = self.source()
        self.install(source)
        copy = self.work / "elsewhere/renamed-folder"
        shutil.copytree(source, copy)
        code, events = self.manage("install", target=str(copy))
        self.assertEqual(code, 1, events)
        self.assertIn("already installed", events[-1]["payload"]["message"])
        self.assertFalse((self.root / "installed/renamed-folder").exists())
        self.assertTrue(plugins._inventory(self.root)["installed"][0]["loaded"])

    def test_builtin_operation_collision_rejected(self):
        # Use an actual built-in id rather than assuming the DSP namespace.
        from pymss.operations import default_runtime
        builtin = next(item["id"] for item in default_runtime().list_operations() if item["id"].startswith("pymss.audio."))
        code, events = self.manage("install", target=str(self.source(name="pymss.audio", operation=builtin)))
        self.assertEqual(code, 1, events)
        self.assertFalse((self.root / "installed/pymss.audio").exists())

    def test_install_directory_is_the_manifest_plugin_id(self):
        source = self.source(name="author.hello")
        renamed = self.work / "checkout/any-folder-name"
        shutil.copytree(source, renamed)
        snapshot = self.install(renamed)
        self.assertEqual(snapshot["installed"][0]["name"], "author.hello")
        self.assertTrue((self.root / "installed/author.hello/__init__.py").is_file())

    def test_update_source_providing_another_plugin_id_is_rejected(self):
        source = self.source()
        self.install(source)
        source.joinpath("pyproject.toml").write_text(self.manifest("other", "2.0.0", "other.hello"))
        source.joinpath("__init__.py").write_text(source.joinpath("__init__.py").read_text().replace("hello.hello", "other.hello"))
        code, events = self.manage("update", name="hello")
        self.assertEqual(code, 1, events)
        self.assertIn("provides plugin other, not hello", events[-1]["payload"]["message"])
        self.assertEqual(plugins._manifest(self.root / "installed/hello")["version"], "1.0.0")

    def test_v1_installations_move_to_legacy_and_are_reported(self):
        self.install(self.source())
        legacy = self.root / "installed/oldtool"
        legacy.mkdir()
        legacy.joinpath("__init__.py").write_text("def setup(plugin):\n    pass\n")
        legacy.joinpath("pyproject.toml").write_text('[tool.pymss.plugin]\nid="pymss.oldtool"\napi_version=1\n')
        snapshot = plugins._inventory(self.root)
        self.assertEqual(snapshot["legacyPlugins"], ["oldtool"])
        self.assertEqual([row["name"] for row in snapshot["installed"] if row["managed"]], ["hello"])
        self.assertTrue((self.root / "legacy-v1/oldtool/__init__.py").is_file())
        self.assertFalse(legacy.exists())

    def test_legacy_migration_preserves_existing_and_same_named_installations(self):
        original = self.root / "legacy-v1/oldtool"
        for directory, marker in (
            (original, "archived"),
            (self.root / "installed/oldtool", "enabled"),
            (self.root / "disabled/oldtool", "disabled"),
        ):
            directory.mkdir(parents=True)
            (directory / "pyproject.toml").write_text('[tool.pymss.plugin]\nid="oldtool"\napi_version=1\n')
            (directory / "state.txt").write_text(marker)

        snapshot = plugins._inventory(self.root)
        self.assertEqual((original / "state.txt").read_text(), "archived")
        archived = self.root / "legacy-v1"
        self.assertEqual(sorted((path / "state.txt").read_text() for path in archived.iterdir()),
                         ["archived", "disabled", "enabled"])
        self.assertEqual(len(snapshot["legacyPlugins"]), 3)
        self.assertEqual(plugins._inventory(self.root)["legacyPlugins"], snapshot["legacyPlugins"])

    def test_inventory_reads_manifests_without_probing(self):
        self.install(self.source())
        with mock.patch.object(plugins, "_probe", side_effect=AssertionError("listing must not import plugins")):
            snapshot = plugins._inventory(self.root)
        self.assertTrue(snapshot["installed"][0]["loaded"])
        self.assertEqual(snapshot["installed"][0]["operations"], ["hello.hello"])

    def test_studio_views_must_stay_inside_the_plugin(self):
        source = self.source()
        (source / "ui").mkdir()
        (source / "ui/index.html").write_text("<html></html>")
        (self.work / "sources/secret.html").write_text("secret")
        base = source.joinpath("pyproject.toml").read_text()
        for entry, valid in [("ui/index.html", True), ("../secret.html", False), ("ui/missing.html", False)]:
            with self.subTest(entry=entry):
                source.joinpath("pyproject.toml").write_text(base + f'\n[tool.pymss.studio]\nsdk_version = 1\n[[tool.pymss.studio.views]]\nid="tool"\nentry="{entry}"\noperation="hello.hello"\n')
                if valid:
                    [view] = plugins._studio_views(source)
                    self.assertEqual(Path(view["entryPath"]), (source / "ui/index.html").resolve())
                    self.assertEqual(view["placements"], ["tool"])
                    self.assertEqual(view["permissions"], ["operations"])
                else:
                    with self.assertRaisesRegex(ValueError, "inside the plugin"):
                        plugins._studio_views(source)

    def test_backend_and_interpreter_both_scope_plugin_installations(self):
        (self.work / "active.json").write_text('{"backend":"mlx"}')
        self.assertNotEqual(self.root, plugins._root())
        (self.work / "active.json").write_text('{"backend":"cpu"}')
        with mock.patch.object(sys, "executable", str(self.work / "another-venv/bin/python")):
            self.assertNotEqual(self.root, plugins._root())

    def test_forced_verification_failure_is_visible(self):
        self.install(self.source())
        with mock.patch.object(plugins, "_probe", side_effect=RuntimeError("plugin probe timed out")):
            snapshot = plugins._inventory(self.root, verify=True)
        self.assertTrue(snapshot["installed"][0]["loaded"])
        self.assertIn("timed out", snapshot["verificationError"])

    def test_forced_verification_reports_import_failures(self):
        self.install(self.source())
        self.root.joinpath("installed/hello/__init__.py").write_text("raise RuntimeError('broken after install')\n")
        snapshot = plugins._inventory(self.root, verify=True)
        self.assertFalse(snapshot["installed"][0]["loaded"])
        self.assertIn("broken after install", snapshot["installed"][0]["error"])

    def test_local_source_update_check_compares_its_declared_version(self):
        source = self.source()
        self.install(source)
        self.source(version="2.0.0")
        code, events = self.manage("check_updates")
        self.assertEqual(code, 0, events)
        update = events[-1]["payload"]["updates"]["hello"]
        self.assertFalse(update["up_to_date"])
        self.assertEqual(update["remote_version"], "2.0.0")

    def test_pymss_directory_plugin_is_visible_and_blocks_the_same_plugin_id(self):
        manual = self.work / "manual/hello"
        manual.parent.mkdir(parents=True)
        shutil.copytree(self.source(), manual)
        copy = self.work / "managed/hello"
        shutil.copytree(manual, copy)
        code, events = self.manage("install", target=str(copy))
        self.assertEqual(code, 1, events)
        self.assertIn("already activated from", events[-1]["payload"]["message"])
        row = plugins._inventory(self.root)["installed"][0]
        self.assertEqual(row["name"], "hello")
        self.assertFalse(row["managed"])
        self.assertIsNone(row["loaded"])

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
        self.install(self.source())
        def probe(root):
            with runtime_lock(root / ".lock", timeout=0):
                return []
        with mock.patch.object(plugins, "_probe", side_effect=probe):
            self.assertIsNone(plugins._inventory(self.root, verify=True)["verificationError"])

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
        self.assertIn("hello.hello", [item["id"] for item in runtime.list_operations()])

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

    def test_pymss_directory_plugins_are_listed_from_their_manifest(self):
        manual = self.work / "manual"
        shutil.copytree(self.source(name="author.manual"), manual / "renamed")
        (manual / "old").mkdir()
        (manual / "old/__init__.py").write_text("")
        rows = {row["path"]: row for row in plugins._inventory(self.root)["installed"]}
        listed = rows[str(manual / "renamed")]
        self.assertEqual((listed["name"], listed["managed"], listed["loaded"]), ("author.manual", False, None))
        broken = rows[str(manual / "old")]
        self.assertFalse(broken["loaded"])
        self.assertIn("pyproject.toml", broken["error"])

    def test_ssh_git_reference_is_separate_from_authentication_and_subpath(self):
        from pymss.plugins import install as installer

        source = self.source()

        def clone_source(url, destination, *, subpath, ref):
            shutil.copytree(source, destination)
            return {}

        cases = [
            ("ssh://git@example.com/repo.git@v1#plugins/hello", "ssh://git@example.com/repo.git", "v1", "plugins/hello"),
            ("git@example.com:repo.git@feature/branch", "git@example.com:repo.git", "feature/branch", None),
            ("ssh://git@example.com/repo.git#plugins/hello", "ssh://git@example.com/repo.git", None, "plugins/hello"),
            ("git@example.com:repo.git", "git@example.com:repo.git", None, None),
        ]
        for index, (target, url, ref, subpath) in enumerate(cases):
            with self.subTest(target=target), mock.patch.object(installer, "_clone", side_effect=clone_source) as clone:
                # The published installer owns URL parsing. Keep it real and
                # intercept only acquisition so authentication is never treated
                # as a ref and neither ref nor fragment reaches the Git URL.
                result = plugins._acquire_source(target, self.work / f"download-{index}")
                clone.assert_called_once()
                self.assertEqual(clone.call_args.args[0], url)
                self.assertEqual(clone.call_args.kwargs, {"ref": ref, "subpath": subpath})
                metadata = plugins._manifest(result.path)
                self.assertEqual(metadata["url"], url)
                self.assertEqual(metadata["ref"], ref)
                self.assertEqual(metadata["subpath"], subpath)

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
        self.assertIn("hello.hello", [item["id"] for item in second.list_operations()])

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
        source.joinpath("pyproject.toml").write_text(self.manifest("hello", dependencies="[" + json.dumps(requirement) + "]"))
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
