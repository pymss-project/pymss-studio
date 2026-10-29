from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest import mock

if __package__:
    from . import test_worker_plugins as marketplace
else:
    import test_worker_plugins as marketplace

import worker_plugins as plugins


class PluginSourceTests(unittest.TestCase):
    source = marketplace.PluginMarketplaceTests.source
    manage = marketplace.PluginMarketplaceTests.manage
    install = marketplace.PluginMarketplaceTests.install
    reset_runtime = staticmethod(marketplace.PluginMarketplaceTests.reset_runtime)

    def test_local_packages_exclude_frontend_dependencies_and_development_state(self):
        source = self.source()
        for directory in ['node_modules', '.venv', 'venv', '.tmp', '.cache', '.pytest_cache']:
            path = source / directory
            path.mkdir()
            (path / 'local-only.txt').write_text('development files')
        (source / '.env').write_text('LOCAL_SETTING=development')
        (source / '.env.local').write_text('LOCAL_SETTING=development')
        acquired = plugins._acquire_source(str(source), self.work / 'copies')
        for name in ['node_modules', '.venv', 'venv', '.tmp', '.cache', '.pytest_cache', '.env', '.env.local']:
            self.assertFalse((acquired.path / name).exists(), name)
        before = plugins._plugin_content_digest(source)
        (source / 'node_modules/local-only.txt').write_text('changed dependency cache')
        (source / '.env').write_text('LOCAL_SETTING=changed')
        self.assertEqual(plugins._plugin_content_digest(source), before)
        self.assertEqual(plugins._plugin_content_digest(acquired.path), before)

    def test_official_catalog_tag_is_passed_to_the_installer(self):
        from pymss.plugins import install as installer
        from types import SimpleNamespace
        source = self.source()
        commit = "v1.0.0"

        def acquire(target, *, plugins_dir, subpath, no_deps, ref, registry_url):
            self.assertEqual(target, "https://github.com/example/plugins")
            self.assertEqual(ref, commit)
            self.assertEqual(subpath, "plugins/hello")
            self.assertTrue(no_deps)
            path = plugins_dir / "hello"
            shutil.copytree(source, path)
            (path / ".pymss-install.json").write_text(json.dumps({"name": "hello", "ref": ref}))
            return SimpleNamespace(name="hello", path=path)

        with mock.patch.object(installer, "list_available", return_value=[{
            "name": "hello", "url": "https://github.com/example/plugins", "subpath": "plugins/hello", "ref": commit,
        }]), mock.patch.object(installer, "install", side_effect=acquire), mock.patch.object(plugins, "_LOCAL_SOURCE") as local:
            local.get.return_value = None
            result = plugins._acquire_source("hello", self.work / "downloads")
        self.assertTrue(plugins._manifest(result.path)["defaultRepository"])
        self.assertEqual(plugins._manifest(result.path)["ref"], commit)

    def test_catalog_commit_fetches_exact_source_even_after_the_branch_advances(self):
        from pymss.plugins import install as installer
        repository = self.work / "repository"
        source = self.source()
        shutil.copytree(source, repository / "plugins/hello")

        def git(*args):
            return subprocess.check_output(["git", "-c", "core.autocrlf=false", *args], cwd=repository, text=True).strip()

        git("init", "--quiet")
        git("add", ".")
        git("-c", "user.name=PluginTests", "-c", "user.email=plugins@example.test", "commit", "--quiet", "-m", "First plugin")
        commit = git("rev-parse", "HEAD")
        project = repository / "plugins/hello/pyproject.toml"
        project.write_text(project.read_text().replace('1.0.0', '2.0.0'))
        git("add", ".")
        git("-c", "user.name=PluginTests", "-c", "user.email=plugins@example.test", "commit", "--quiet", "-m", "New plugin")
        is_url = installer._is_url
        with mock.patch.object(installer, "list_available", return_value=[{
            "name": "hello", "url": str(repository), "subpath": "plugins/hello", "ref": commit, "version": "1.0.0",
        }]), mock.patch.object(installer, "_is_url", side_effect=lambda value: value == str(repository) or is_url(value)):
            acquired = plugins._acquire_source("hello", self.work / "downloads")
        self.assertEqual(plugins._project_version(acquired.path), "1.0.0")
        self.assertEqual(acquired.name, "hello")
        self.assertEqual(plugins._manifest(acquired.path)["ref"], commit)
        self.assertEqual(plugins._manifest(acquired.path)["source"], "url")
        self.assertTrue(plugins._manifest(acquired.path)["defaultRepository"])

    def test_catalog_version_mismatch_is_rejected_before_publication(self):
        from pymss.plugins import install as installer
        from types import SimpleNamespace
        source = self.source()
        with mock.patch.object(installer, "list_available", return_value=[{
            "name": "hello", "url": "https://github.com/example/plugins", "subpath": "plugins/hello", "version": "2.0.0",
        }]), mock.patch.object(installer, "install", return_value=SimpleNamespace(name="hello", path=source)):
            with self.assertRaisesRegex(ValueError, "version does not match"):
                plugins._acquire_source("hello", self.work / "downloads")
        self.assertFalse((self.root / "installed/hello").exists())

    def test_local_catalog_preserves_package_owned_locales_and_category(self):
        root = self.local_source()
        path = root / 'registry.json'
        data = json.loads(path.read_text())
        data['hello'].update(category='recognize', locales={'zh-CN': {'title': '语音识别', 'tags': ['语音'], 'operations': {'example.hello': '语音转写'}}})
        path.write_text(json.dumps(data))
        row = plugins._local_catalog(root)[0]
        self.assertEqual(row['category'], 'recognize')
        self.assertEqual(row['locales']['zh-CN']['title'], '语音识别')
        self.assertEqual(plugins._presentation({'category': [], 'locales': {'zh': 'invalid'}}), {'category': None, 'locales': {}})

    def test_remote_registry_versions_avoid_cloning_each_default_plugin(self):
        from pymss.plugins import install as installer
        self.install(self.source())
        receipt = self.root / 'installed/hello/.pymss-install.json'
        value = json.loads(receipt.read_text())
        value['defaultRepository'] = True
        receipt.write_text(json.dumps(value))
        with mock.patch.object(installer, 'list_available', return_value=[{'name': 'hello', 'url': 'https://example.com/plugins', 'subpath': 'hello', 'version': '2.0.0'}]), \
             mock.patch.object(installer, 'check_update', side_effect=AssertionError('unexpected clone')), redirect_stdout(io.StringIO()):
            result = plugins._check_updates(self.root, None)
        self.assertEqual(result['hello']['remote_version'], '2.0.0')
        self.assertFalse(result['hello']['up_to_date'])

    def test_same_version_source_edits_are_detected_and_dependency_sidecars_are_ignored(self):
        root = self.local_source()
        with mock.patch.dict(os.environ, {'PYMSS_STUDIO_PLUGINS_SOURCE': str(root)}):
            code, events = self.manage('install', name='hello')
            self.assertEqual(code, 0, events)
            target = self.root / 'installed/hello'
            deps = target / '.studio-deps'
            deps.mkdir()
            (deps / 'cache.py').write_text('value = 1')
            with redirect_stdout(io.StringIO()):
                self.assertTrue(plugins._check_updates(self.root, None)['hello']['up_to_date'])
            source = root / 'hello/__init__.py'
            source.write_text(source.read_text() + '\nSOURCE_CHANGED = True\n')
            with redirect_stdout(io.StringIO()):
                changed = plugins._check_updates(self.root, None)['hello']
            self.assertFalse(changed['up_to_date'])
            self.assertTrue(changed['content_changed'])
            self.assertEqual(changed['local_version'], changed['remote_version'])
            code, events = self.manage('update', name='hello')
            self.assertEqual(code, 0, events)
            with redirect_stdout(io.StringIO()):
                self.assertTrue(plugins._check_updates(self.root, None)['hello']['up_to_date'])

    def setUp(self):
        marketplace.PluginMarketplaceTests.setUp(self)
        patch = mock.patch.dict(os.environ, {key: '' for key in ('PYMSS_STUDIO_PLUGINS_SOURCE', 'PYMSS_STUDIO_PLUGIN_SOURCE_OVERRIDE', 'PYMSS_PLUGINS_REGISTRY')})
        patch.start()
        self.addCleanup(patch.stop)

    def local_source(self):
        root = self.work / 'local'
        root.mkdir()
        shutil.copytree(self.source(name='hello'), root / 'hello')
        (root / 'registry.json').write_text(json.dumps({'_comment': 'Plugin registry', 'hello': {'url': 'https://github.com/example/plugins', 'subpath': 'hello'}}), encoding='utf-8')
        return root

    def source_info(self, payload=None):
        output = io.StringIO()
        with redirect_stdout(output):
            code = plugins.cmd_plugin_source_info(payload or {})
        self.assertEqual(code, 0)
        return json.loads(output.getvalue())['payload']

    def test_source_info_defaults_and_saved_overrides(self):
        root = self.local_source()
        with mock.patch.dict(os.environ, {'PYMSS_STUDIO_PLUGINS_SOURCE': str(root)}):
            value = self.source_info()
            self.assertEqual(value['defaults']['mode'], 'local')
            self.assertEqual(value['defaults']['repositoryUrl'], 'https://github.com/example/plugins')
            self.assertEqual(value['effective']['origin'], 'environment')
            self.assertEqual(value['registryPath'], str(root / 'registry.json'))
            self.assertTrue(value['available'])
            self.assertEqual(value['catalogCount'], 1)
            with mock.patch.dict(os.environ, {'PYMSS_STUDIO_PLUGIN_SOURCE_OVERRIDE': json.dumps({'mode': 'repository', 'repositoryUrl': 'https://github.com/other/plugins', 'ref': 'v1', 'registryRef': 'main'})}):
                value = self.source_info()
                self.assertEqual(value['effective']['origin'], 'debug')
                self.assertEqual(value['registryUrl'], 'https://raw.githubusercontent.com/other/plugins/main/registry.json')

    def test_local_checkout_is_used_only_by_an_explicit_machine_override(self):
        root = self.local_source()
        default = self.source_info()
        self.assertEqual(default['defaults']['mode'], 'repository')
        self.assertEqual(default['effective']['mode'], 'repository')
        saved = json.dumps({'mode': 'local', 'localPath': str(root)})
        with mock.patch.dict(os.environ, {'PYMSS_STUDIO_PLUGIN_SOURCE_OVERRIDE': saved}):
            selected = self.source_info()
            self.assertEqual(selected['defaults']['mode'], 'repository')
            self.assertEqual(selected['effective']['mode'], 'local')
            self.assertEqual(selected['effective']['origin'], 'debug')
            self.assertTrue(selected['available'])
        restored = self.source_info()
        self.assertEqual(restored['effective']['mode'], 'repository')

    def test_source_info_returns_structured_validation_errors(self):
        root = self.local_source()
        for draft in [{'mode': 'local', 'localPath': 'relative'}, {'mode': 'local', 'localPath': str(root / 'missing')}, {'mode': 'repository', 'repositoryUrl': 'http://example.com', 'ref': 'main', 'registryRef': 'main'}]:
            with self.subTest(draft=draft):
                value = self.source_info({'source': draft})
                self.assertFalse(value['available'])
                self.assertIsInstance(value['error'], str)
                self.assertTrue(value['defaults'])
        (root / 'registry.json').write_text(json.dumps({'hello': {'subpath': '../outside'}}), encoding='utf-8')
        self.assertFalse(self.source_info({'source': {'mode': 'local', 'localPath': str(root)}})['available'])

    def test_local_catalog_and_short_name_copy_never_use_the_network(self):
        root = self.local_source()
        with mock.patch.dict(os.environ, {'PYMSS_STUDIO_PLUGINS_SOURCE': str(root)}), \
             mock.patch('pymss.plugins.install.install', side_effect=AssertionError('remote install')), \
             mock.patch('pymss.plugins.install.list_available', side_effect=AssertionError('remote catalog')), \
             mock.patch.object(plugins, '_inventory', return_value={}):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(plugins.cmd_plugin_marketplace({}), 0)
            row = json.loads(output.getvalue())['payload']['catalog'][0]
            self.assertEqual(row['url'], str(root))
            with plugins._plugin_registry():
                result = plugins._acquire_source('hello', self.work / 'copied')
            self.assertEqual(result.path.joinpath('__init__.py').read_bytes(), root.joinpath('hello/__init__.py').read_bytes())
            self.assertTrue(plugins._manifest(result.path)['defaultRepository'])
        self.assertIsNone(plugins._LOCAL_SOURCE.get())
        self.assertEqual(os.environ['PYMSS_PLUGINS_REGISTRY'], '')

    def test_explicit_registry_environment_has_highest_priority(self):
        root = self.local_source()
        with mock.patch.dict(os.environ, {'PYMSS_STUDIO_PLUGINS_SOURCE': str(root), 'PYMSS_PLUGINS_REGISTRY': 'https://custom.example/registry.json'}):
            info = self.source_info()
            self.assertEqual(info['effective']['origin'], 'registry-environment')
            self.assertEqual(info['registryUrl'], 'https://custom.example/registry.json')
            self.assertIsNone(info['catalogCount'])
            with plugins._plugin_registry():
                self.assertIsNone(plugins._LOCAL_SOURCE.get())
                self.assertEqual(os.environ['PYMSS_PLUGINS_REGISTRY'], 'https://custom.example/registry.json')

    def test_preinstalled_updates_use_local_source_repeatedly_and_keep_disabled(self):
        root = self.local_source()
        self.install(self.source())
        current = self.root / 'installed/hello'
        metadata = plugins._manifest(current)
        metadata.update(preinstalled=True, source='registry', url='https://github.com/example/plugins', subpath='hello')
        (current / '.pymss-install.json').write_text(json.dumps(metadata), encoding='utf-8')
        self.assertEqual(self.manage('disable', name='hello')[0], 0)
        with mock.patch.dict(os.environ, {'PYMSS_STUDIO_PLUGINS_SOURCE': str(root)}), mock.patch('pymss.plugins.install.install', side_effect=AssertionError('remote install')):
            for index in range(2):
                entry = root / 'hello/__init__.py'
                entry.write_text(entry.read_text(encoding='utf-8') + f'\nLOCAL_UPDATE_{index} = True\n', encoding='utf-8')
                code, events = self.manage('update', name='hello')
                self.assertEqual(code, 0, events)
                self.assertIn(f'LOCAL_UPDATE_{index}', (self.root / 'disabled/hello/__init__.py').read_text(encoding='utf-8'))
                self.assertFalse((self.root / 'installed/hello').exists())
            metadata = plugins._manifest(self.root / 'disabled/hello')
            self.assertTrue(metadata['defaultRepository'])
            self.assertTrue(metadata['preinstalled'])

    def test_update_checks_follow_local_source_and_manual_sources_remain_independent(self):
        root = self.local_source()
        self.install(self.source())
        metadata_path = self.root / 'installed/hello/.pymss-install.json'
        metadata = plugins._manifest(metadata_path.parent)
        metadata.update(preinstalled=True, source='registry', url='https://github.com/example/plugins', subpath='hello')
        metadata_path.write_text(json.dumps(metadata), encoding='utf-8')
        project = root / 'hello/pyproject.toml'
        project.write_text(project.read_text(encoding='utf-8').replace('1.0.0', '2.0.0'), encoding='utf-8')
        with mock.patch.dict(os.environ, {'PYMSS_STUDIO_PLUGINS_SOURCE': str(root)}), mock.patch('pymss.plugins.install.check_update', side_effect=AssertionError('remote check')):
            code, events = self.manage('check_updates')
            self.assertEqual(code, 0, events)
            self.assertEqual(events[-1]['payload']['updates']['hello']['remote_version'], '2.0.0')
        metadata.update(preinstalled=False, source='path', defaultRepository=False, url=str(self.work / 'sources/hello'), subpath=None)
        metadata_path.write_text(json.dumps(metadata), encoding='utf-8')
        with mock.patch.dict(os.environ, {'PYMSS_STUDIO_PLUGINS_SOURCE': str(root / 'missing')}):
            code, events = self.manage('update', name='hello')
        self.assertEqual(code, 0, events)
        self.assertEqual(plugins._manifest(metadata_path.parent)['version'], '1.0.0')

    def test_stale_source_fence_rejects_acquisition_before_installation(self):
        root = self.local_source()
        with mock.patch.dict(os.environ, {'PYMSS_STUDIO_PLUGINS_SOURCE': str(root)}):
            info = self.source_info()
            expected = {key: value for key, value in info['effective'].items() if key != 'origin'}
            expected['registryUrl'] = info['registryUrl']
        code, events = self.manage('install', name='hello', expectedSource=json.dumps(expected))
        self.assertEqual(code, 1, events)
        self.assertIn('source changed', events[-1]['payload']['message'])
        self.assertFalse((self.root / 'installed/hello').exists())

    def test_switching_from_local_to_remote_resolves_the_new_catalog_subpath(self):
        from pymss.plugins.install import InstallResult

        root = self.local_source()
        with mock.patch.dict(os.environ, {'PYMSS_STUDIO_PLUGINS_SOURCE': str(root)}):
            self.assertEqual(self.manage('install', name='hello')[0], 0)
        remote = self.source(version='3.0.0')
        selected = {'mode': 'repository', 'repositoryUrl': 'https://github.com/other/plugins', 'ref': 'main', 'registryRef': 'main'}

        def install(name, plugins_dir, subpath, no_deps, ref, registry_url):
            self.assertEqual(name, selected['repositoryUrl'])
            self.assertEqual(subpath, 'plugins/hello')
            self.assertEqual(ref, 'v3.0.0')
            self.assertTrue(no_deps)
            self.assertEqual(registry_url, 'https://raw.githubusercontent.com/other/plugins/main/registry.json')
            self.assertEqual(os.environ.get('PYMSS_PLUGINS_REGISTRY', ''), '')
            destination = plugins_dir / 'hello'
            shutil.copytree(remote, destination)
            (destination / '.pymss-install.json').write_text(json.dumps({'name': 'hello', 'version': '3.0.0', 'source': 'registry', 'url': selected['repositoryUrl'], 'subpath': 'plugins/hello'}), encoding='utf-8')
            return InstallResult(name='hello', path=destination, source='registry', version='3.0.0')

        with mock.patch.dict(os.environ, {'PYMSS_STUDIO_PLUGINS_SOURCE': str(root), 'PYMSS_STUDIO_PLUGIN_SOURCE_OVERRIDE': json.dumps(selected)}), mock.patch('pymss.plugins.install.install', side_effect=install), mock.patch('pymss.plugins.install.list_available', return_value=[{
            'name': 'hello', 'url': selected['repositoryUrl'], 'subpath': 'plugins/hello', 'version': '3.0.0', 'ref': 'v3.0.0',
        }]):
            code, events = self.manage('update', name='hello')
        self.assertEqual(code, 0, events)
        meta = plugins._manifest(self.root / 'installed/hello')
        self.assertEqual(meta['url'], selected['repositoryUrl'])
        self.assertEqual(meta['subpath'], 'plugins/hello')

    def test_runtime_replacement_rejects_acquisition_before_installation(self):
        code, events = self.manage('install', name='hello', expectedRuntime='other-runtime')
        self.assertEqual(code, 1, events)
        self.assertIn('runtime changed', events[-1]['payload']['message'])
        self.assertFalse((self.root / 'installed/hello').exists())


if __name__ == '__main__':
    unittest.main()
