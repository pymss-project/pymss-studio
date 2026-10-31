from pathlib import Path
import json
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[2]


class PluginPresentationTests(unittest.TestCase):
    def test_registry_and_installed_packages_share_categories_and_locales(self):
        registry = json.loads((ROOT / 'registry.json').read_text(encoding='utf-8'))
        for name, entry in registry.items():
            if name.startswith('_'):
                continue
            with self.subTest(plugin=name):
                project = tomllib.loads((ROOT / entry['subpath'] / 'pyproject.toml').read_text(encoding='utf-8'))
                metadata = project['tool']['pymss']['studio']
                self.assertEqual(entry['version'], project['project']['version'])
                self.assertIn(metadata['category'], {'convert', 'analyze', 'recognize', 'edit'})
                self.assertEqual(entry['category'], metadata['category'])
                self.assertEqual(entry['locales'], metadata['locales'])
                for locale in ['en', 'zh-CN']:
                    self.assertTrue(metadata['locales'][locale]['title'])
                    self.assertTrue(metadata['locales'][locale]['description'])
