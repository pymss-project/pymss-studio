import contextlib
import io
import json
from pathlib import Path
import re
import types
import unittest
from unittest.mock import patch


class PrepareRuntimeProbeTests(unittest.TestCase):
    def test_probe_template_executes_after_production_replacement(self):
        script = (Path(__file__).resolve().parents[2] / "scripts/prepare-python-runtime.ps1").read_text(
            encoding="utf-8",
        )
        match = re.search(
            r"\$probeScript = @'\r?\n(.*?)\r?\n'@"
            r"\.Replace\('%NAMES%', \$manifestPackageJson\)"
            r"\.Replace\('%MAPPING%', \$manifestMappingJson\)",
            script,
            re.S,
        )
        self.assertIsNotNone(match, "exercise the production PowerShell heredoc and replacement chain")
        template = match.group(1)
        self.assertIn("names = %NAMES%", template)
        self.assertIn("mapping = %MAPPING%", template)
        self.assertNotIn("json.loads(%NAMES%)", template)
        self.assertNotIn("json.loads(%MAPPING%)", template)
        rendered = template.replace("%NAMES%", '["av", "pymss-core"]').replace(
            "%MAPPING%", '{"pymss-core":"pymss_core"}',
        )
        fake_torch = types.SimpleNamespace(
            __version__="test",
            version=types.SimpleNamespace(hip=None, cuda=None),
            cuda=types.SimpleNamespace(is_available=lambda: False),
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.dict("sys.modules", {"torch": fake_torch}):
            exec(compile(rendered, "rendered-runtime-probe.py", "exec"), {})
        result = json.loads(output.getvalue())
        self.assertEqual(result["torchBackend"], "cpu")
        self.assertIn("pymss-core", result["packages"])


if __name__ == "__main__":
    unittest.main()
