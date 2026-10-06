import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class WindowsInstallerTests(unittest.TestCase):
    def setUp(self):
        manifest = json.loads((ROOT / "python/runtime-manifest.json").read_text(encoding="utf-8"))
        self.backends = {
            name for name, spec in manifest["backends"].items() if "win32" in spec["platforms"]
        }
        self.installer = (ROOT / "installer/pymss-studio.iss").read_text(encoding="utf-8")

    def test_windows_backends_are_recognized_when_preserving_existing_environments(self):
        body = re.search(r"function BundledBackend\(\): string;\s*begin([\s\S]*?)end;", self.installer).group(1)
        mappings = dict(re.findall(
            r"Pos\('([^']+)', LowerCase\('\{#PackageSuffix\}'\)\) > 0 then\s*Result := '([^']+)'",
            body,
        ))
        for backend in self.backends:
            with self.subTest(backend=backend):
                self.assertEqual(mappings.get(backend), backend)

    def test_windows_backends_receive_installed_venv_path_repair(self):
        body = re.search(r"procedure RepairBundledRuntimeEnvs\(\);\s*begin([\s\S]*?)end;", self.installer).group(1)
        repaired = set(re.findall(r"RepairVenvConfig\('([^']+)'\)", body))
        self.assertFalse(self.backends - repaired, f"Unrepaired Windows backends: {self.backends - repaired}")
