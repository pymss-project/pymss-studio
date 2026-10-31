from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('view_components', ROOT / 'tooling/scripts/view_components.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ComponentManifestTests(unittest.TestCase):
    def project(self, root, component='ui/Tool.vue', entry='ui/dist/index.html'):
        directory = root / 'plugins/custom'
        (directory / 'ui').mkdir(parents=True, exist_ok=True)
        (directory / 'ui/Tool.vue').write_text('<template><div /></template>', encoding='utf-8')
        lines = ['[tool.pymss.plugin]', 'id="example.custom"', 'api_version=2', '[tool.pymss.studio]', 'sdk_version=1', '[[tool.pymss.studio.views]]', 'id="panel"', 'title="Custom"', f'entry="{entry}"']
        if component is not None:
            lines.append(f'component="{component}"')
        (directory / 'pyproject.toml').write_text('\n'.join(lines), encoding='utf-8')
        return directory

    def test_new_component_is_discovered_without_a_central_tool_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = self.project(root)
            [entry] = builder.component_entries(root)
            self.assertEqual(entry['plugin'], 'custom')
            self.assertEqual(entry['view'], 'panel')
            self.assertEqual(Path(entry['component']), directory / 'ui/Tool.vue')
            self.assertIsNone(entry['fileTool'])

    def test_component_and_output_paths_cannot_escape_or_overwrite_python_sources(self):
        for source, output in [('../outside.vue', 'ui/dist/index.html'), ('ui/Tool.vue', '../outside.html'), ('ui/Tool.vue', '__init__.py')]:
            with self.subTest(source=source, output=output), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.project(root, source, output)
                with self.assertRaises(ValueError):
                    builder.component_entries(root)

    def test_static_plugin_views_remain_valid_without_a_component_declaration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.project(root, None)
            self.assertEqual(builder.component_entries(root), [])

    def test_a_package_can_keep_its_own_build_and_locale_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = self.project(root)
            (directory / 'scripts').mkdir()
            (directory / 'scripts/build.mjs').write_text('process.exit(0)')
            project = directory / 'pyproject.toml'
            project.write_text(project.read_text().replace('[tool.pymss.studio]', '[tool.pymss.studio]\nbuild_script="scripts/build.mjs"'))
            [entry] = builder.component_entries(root)
            self.assertEqual(Path(entry['buildScript']), directory / 'scripts/build.mjs')
            self.assertEqual(Path(entry['packageRoot']), directory)
            self.project(root, component=None)
            project.write_text(project.read_text().replace('[tool.pymss.studio]', '[tool.pymss.studio]\nbuild_script="scripts/build.mjs"'))
            [standalone] = builder.component_entries(root)
            self.assertIsNone(standalone['component'])
            self.assertEqual(standalone['buildScript'], entry['buildScript'])
            manifest = project.read_text()
            for script in ['../outside.mjs', 'scripts/missing.mjs', '__init__.py']:
                project.write_text(manifest.replace('build_script="scripts/build.mjs"', f'build_script="{script}"'))
                with self.subTest(script=script), self.assertRaises(ValueError):
                    builder.component_entries(root)
