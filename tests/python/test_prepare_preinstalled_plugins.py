from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("prepare_plugins", ROOT / "scripts/prepare-preinstalled-plugins.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
from worker_plugin_config import load_plugin_config, plugin_registry_url  # noqa: E402


class PreparePluginTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.studio, self.source = self.work / "studio", self.work / "plugins"
        (self.studio / "python").mkdir(parents=True)
        self.source.mkdir()
        self.config = {"schemaVersion": 1, "repository": {"url": "https://github.com/example/plugins", "ref": "main"}, "preinstalled": ["alpha"]}
        self.config_path = self.studio / "python/plugin-manifest.json"
        self.write_config()
        self.registry = {}
        self.package("alpha")
        self.package("beta")
        self.repo_git("init", "--quiet", "--initial-branch=main")
        self.commit()

    def write_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def package(self, name):
        package = self.source / "plugins" / name
        (package / "ui").mkdir(parents=True)
        (package / "__init__.py").write_bytes(b"def setup(plugin):\r\n    pass\r\n")
        (package / "pyproject.toml").write_text(f'''
[project]
name = "pymss-plugin-{name}"
version = "1.0.0"
[tool.pymss.plugin]
id = "{name}"
api_version = 2
[tool.pymss.studio]
sdk_version = 1
category = "edit"
icon = "OptionsOutline"
[[tool.pymss.studio.views]]
id = "tool"
entry = "ui/index.html"
''', encoding="utf-8")
        (package / "ui/index.html").write_text("<!doctype html><p>Tool</p>", encoding="utf-8")
        (package / "ui/icon.png").write_bytes(b"\x89PNG\r\n\x00")
        (package / "LICENSE").write_text("License\r\n", encoding="utf-8", newline="")
        (package / ".env").write_text("LOCAL_SETTING=private", encoding="utf-8")
        (package / "__pycache__").mkdir()
        (package / "__pycache__/stale.pyc").write_bytes(b"cache")
        self.registry[name] = {"url": self.config["repository"]["url"], "subpath": f"plugins/{name}"}
        (self.source / "registry.json").write_text(json.dumps(self.registry), encoding="utf-8")
        return package

    def repo_git(self, *args):
        result = subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=self.source, check=True, capture_output=True, text=True, encoding="utf-8")
        return result.stdout.strip()

    def commit(self):
        self.repo_git("add", ".")
        self.repo_git("-c", "user.name=PluginTests", "-c", "user.email=plugins@example.test", "commit", "--quiet", "-m", "Plugin packages")
        return self.repo_git("rev-parse", "HEAD")

    def prepare(self, **kwargs):
        return builder.prepare(self.studio, source=self.source, **kwargs)

    def test_configuration_selects_packages_and_preserves_binary_assets(self):
        manifest = self.prepare()
        output = self.studio / "python/preinstalled_plugins"
        self.assertEqual([row["name"] for row in manifest["plugins"]], ["alpha"])
        self.assertFalse((output / "beta").exists())
        self.assertEqual((output / "alpha/ui/icon.png").read_bytes(), b"\x89PNG\r\n\x00")
        self.assertEqual((output / "alpha/__init__.py").read_bytes(), b"def setup(plugin):\n    pass\n")
        self.assertFalse((output / "alpha/.env").exists())
        self.assertFalse((output / "alpha/__pycache__").exists())
        self.assertEqual((manifest["plugins"][0]["category"], manifest["plugins"][0]["icon"]), ("edit", "OptionsOutline"))
        self.assertNotIn("toolId", manifest["plugins"][0])
        self.assertEqual(manifest["source"]["commit"], self.repo_git("rev-parse", "HEAD"))
        self.assertTrue(builder.verify_generated(output, self.config))
        self.assertFalse(builder.verify_generated(output, self.config, release=True))

    def test_empty_selection_prepares_and_checks_offline_without_fetching_a_repository(self):
        self.prepare()
        self.config["preinstalled"] = []
        self.write_config()
        with mock.patch.object(builder, "git", side_effect=AssertionError("empty selection must not fetch")):
            manifest = builder.prepare(self.studio, release=True)
            builder.prepare(self.studio, release=True, check=True)
        self.assertEqual(manifest["plugins"], [])
        output = self.studio / "python/preinstalled_plugins"
        self.assertEqual([path.name for path in output.iterdir()], ["manifest.json"])
        self.assertTrue(builder.verify_generated(output, self.config, release=True))
        self.config["repository"]["ref"] = "a" * 40
        self.write_config()
        with mock.patch.object(builder, "git", side_effect=AssertionError("empty pinned selection must not fetch")):
            builder.prepare(self.studio, release=True)
        self.assertTrue(builder.verify_generated(output, self.config, release=True))

    def test_offline_reuse_and_integrity_check_require_the_same_configuration(self):
        self.prepare()
        with mock.patch.object(builder, "git", side_effect=AssertionError("offline")):
            builder.prepare(self.studio)
            builder.prepare(self.studio, check=True)
        output = self.studio / "python/preinstalled_plugins"
        (output / "alpha/ui/index.html").write_text("modified", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "stale or damaged"):
            builder.prepare(self.studio, check=True)
        self.prepare()
        self.config["preinstalled"] = ["beta"]
        self.write_config()
        self.assertFalse(builder.verify_generated(output, self.config))
        self.prepare()
        self.assertFalse((output / "alpha").exists())
        self.assertTrue((output / "beta/ui/index.html").is_file())

    def test_failed_source_validation_keeps_the_previous_snapshot(self):
        self.prepare()
        output = self.studio / "python/preinstalled_plugins"
        before = (output / "manifest.json").read_bytes()
        (self.source / "plugins/alpha/ui/index.html").unlink()
        with self.assertRaisesRegex(ValueError, "UI is not built"):
            self.prepare()
        self.assertEqual((output / "manifest.json").read_bytes(), before)
        self.assertTrue(builder.verify_generated(output, self.config))

    def test_publish_rollback_restores_the_old_directory(self):
        self.prepare()
        output = self.studio / "python/preinstalled_plugins"
        before = (output / "manifest.json").read_bytes()
        original = Path.rename

        def rename(path, destination):
            if path.name == "next":
                raise PermissionError("locked")
            return original(path, destination)

        with mock.patch.object(Path, "rename", rename), self.assertRaises(PermissionError):
            self.prepare()
        self.assertEqual((output / "manifest.json").read_bytes(), before)
        self.assertTrue(builder.verify_generated(output, self.config))

    def test_failed_rollback_preserves_the_previous_snapshot_for_recovery(self):
        self.prepare()
        output = self.studio / "python/preinstalled_plugins"
        before = (output / "manifest.json").read_bytes()
        original = Path.rename

        def rename(path, destination):
            if path.name in {"next", "previous"}:
                raise PermissionError("locked")
            return original(path, destination)

        with mock.patch.object(Path, "rename", rename), self.assertRaisesRegex(RuntimeError, "previous resources remain"):
            self.prepare()
        backups = list((self.studio / ".tmp/plugin-resources").glob("*/previous/manifest.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)

    def test_transient_windows_directory_lock_is_retried(self):
        self.prepare()
        original = Path.rename
        blocked = False

        def rename(path, destination):
            nonlocal blocked
            if path.name == "next" and not blocked:
                blocked = True
                error = PermissionError("sharing violation")
                error.winerror = 32
                raise error
            return original(path, destination)

        with mock.patch.object(Path, "rename", rename), mock.patch.object(builder.time, "sleep") as sleep:
            self.prepare()
        sleep.assert_called_once()
        self.assertTrue(builder.verify_generated(self.studio / "python/preinstalled_plugins", self.config))

    def test_release_fetches_records_and_reuses_a_remote_commit(self):
        real_git = builder.git

        def git(*args, cwd):
            if args[0] == "fetch":
                args = (*args[:3], str(self.source), *args[4:])
            return real_git(*args, cwd=cwd)

        with mock.patch.object(builder, "git", side_effect=git):
            manifest = builder.prepare(self.studio, release=True)
            self.assertFalse(manifest["source"]["local"])
            self.assertTrue(builder.verify_generated(self.studio / "python/preinstalled_plugins", self.config, release=True))
            (self.source / "plugins/alpha/ui/index.html").write_text("new", encoding="utf-8")
            next_commit = self.commit()
            cached = builder.prepare(self.studio, release=True)
            self.assertEqual(cached["source"]["commit"], manifest["source"]["commit"])
            refreshed = builder.prepare(self.studio, release=True, refresh=True)
            self.assertEqual(refreshed["source"]["commit"], next_commit)
        with self.assertRaisesRegex(ValueError, "without a local source"):
            self.prepare(release=True)

    def test_missing_wrong_and_escaping_registry_entries_are_rejected(self):
        for entry in [None, {"url": self.config["repository"]["url"], "subpath": "plugins/beta"}, {"url": self.config["repository"]["url"], "subpath": "../outside"}, {"url": "https://github.com/another/repo", "subpath": "plugins/alpha"}]:
            with self.subTest(entry=entry):
                self.registry["alpha"] = entry
                (self.source / "registry.json").write_text(json.dumps(self.registry), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.prepare()

    def test_malformed_and_duplicate_protocol_metadata_is_rejected(self):
        project = self.source / "plugins/alpha/pyproject.toml"
        original = project.read_text(encoding="utf-8")
        for text in [original.replace('[tool.pymss.plugin]\nid = "alpha"\napi_version = 2', '[tool.pymss]\nplugin = []'), original.replace('api_version = 2', 'api_version = 1'), original.replace('category = "edit"', 'category = []'), original.replace('[[tool.pymss.studio.views]]\nid = "tool"\nentry = "ui/index.html"', 'views = [1]')]:
            with self.subTest(text=text):
                project.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.prepare()
        project.write_text(original, encoding="utf-8")
        beta = self.source / "plugins/beta/pyproject.toml"
        beta.write_text(beta.read_text(encoding="utf-8").replace('id = "beta"', 'id = "alpha"'), encoding="utf-8")
        self.config["preinstalled"] = ["alpha", "beta"]
        self.write_config()
        with self.assertRaisesRegex(ValueError, "must select the package with that plugin id, not alpha"):
            self.prepare()

    def test_generated_extra_files_and_symlinks_are_not_trusted(self):
        self.prepare()
        output = self.studio / "python/preinstalled_plugins"
        extra = output / "obsolete.py"
        extra.write_text("old", encoding="utf-8")
        self.assertFalse(builder.verify_generated(output, self.config))
        extra.unlink()
        try:
            os.symlink(self.source / "plugins", output / "linked", target_is_directory=True)
        except OSError:
            return  # Other assertions still run on Windows without symlink rights.
        self.assertFalse(builder.verify_generated(output, self.config))

    def test_configuration_rejects_invalid_sources_and_duplicate_names(self):
        valid = json.loads(json.dumps(self.config))
        for change in [{"schemaVersion": True}, {"repository": {"url": "http://github.com/example/plugins", "ref": "main"}}, {"repository": {"url": valid["repository"]["url"], "ref": "--upload-pack=bad"}}, {"repository": {"url": valid["repository"]["url"], "ref": "../main"}}, {"preinstalled": ["alpha", "alpha"]}, {"preinstalled": ["../alpha"]}]:
            with self.subTest(change=change):
                self.config_path.write_text(json.dumps({**valid, **change}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_plugin_config(self.config_path)
        self.assertEqual(plugin_registry_url(valid), "https://raw.githubusercontent.com/example/plugins/main/registry.json")
        valid["repository"].update(ref="a" * 40, registryRef="main")
        self.assertEqual(plugin_registry_url(valid), "https://raw.githubusercontent.com/example/plugins/main/registry.json")

    def test_build_entrypoints_prepare_before_copying_release_resources(self):
        setup = (ROOT / ".github/actions/setup-release-env/action.yml").read_text(encoding="utf-8")
        self.assertIn("pnpm plugins:prepare --release", setup)
        for platform in ("windows", "macos", "linux"):
            workflow = (ROOT / f".github/workflows/release-{platform}.yml").read_text(encoding="utf-8")
            self.assertLess(workflow.index("uses: ./.github/actions/setup-release-env"), workflow.index("Copy-Item \"python\"" if platform == "windows" else "rsync -a"))
        launcher = (ROOT / "scripts/tauri.mjs").read_text(encoding="utf-8")
        self.assertLess(launcher.index("scripts/prepare-preinstalled-plugins.py"), launcher.index("await import('@tauri-apps/cli/tauri.js')"))
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("python/preinstalled_plugins/", ignored)
        vite = (ROOT / "vite.config.ts").read_text(encoding="utf-8")
        for directory in ("'.tmp'", "'.cache'", "'python/preinstalled_plugins'"):
            self.assertIn(directory, vite)


if __name__ == "__main__":
    unittest.main()
