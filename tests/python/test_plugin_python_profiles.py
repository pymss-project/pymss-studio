"""Real marketplace transactions for isolated Python process dependencies."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import zipfile
from contextlib import redirect_stdout

if __package__:
    from . import test_plugin_runtime_bindings as bindings
else:
    import test_plugin_runtime_bindings as bindings

import worker_operations
import worker_plugin_python as profiles
import worker_plugins as plugins


class PythonProfileTests(bindings.unittest.TestCase):
    source = bindings.RuntimeBindingTests.source
    manage = bindings.RuntimeBindingTests.manage
    install = bindings.RuntimeBindingTests.install
    reset_runtime = staticmethod(bindings.RuntimeBindingTests.reset_runtime)
    switch = bindings.RuntimeBindingTests.switch

    def setUp(self):
        bindings.RuntimeBindingTests.setUp(self)
        try:
            from pymss.operations.python_profiles import prepare_python_environment  # noqa: F401
        except ImportError:
            self.skipTest("The active pymss version does not provide isolated Python profiles")

    def package(self, name="author.alpha", dependency="1.0.0", version="1.0.0"):
        root = self.work / ("source-" + name)
        root.mkdir(exist_ok=True)
        wheel = root / f"pymss_isolation_probe-{dependency}-py3-none-any.whl"
        info = f"pymss_isolation_probe-{dependency}.dist-info"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("pymss_isolation_probe/__init__.py", f"VERSION = {dependency!r}\n")
            archive.writestr(info + "/METADATA", f"Metadata-Version: 2.1\nName: pymss-isolation-probe\nVersion: {dependency}\n")
            archive.writestr(info + "/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            archive.writestr(info + "/RECORD", "")
        (root / "python-lock.json").write_text(json.dumps({"formatVersion": 1, "requiresPython": ">=3.11",
            "targets": {"any": [{"path": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}]}}), encoding="utf-8")
        (root / "__init__.py").write_text("from pathlib import Path\nfrom pymss.plugins.kit import register_commands\ndef setup(plugin): register_commands(plugin,Path(__file__).parent)\n", encoding="utf-8")
        (root / "main.py").write_text("import sys\nfrom pathlib import Path\nimport pymss_isolation_probe\nPath(sys.argv[2]).write_text(pymss_isolation_probe.VERSION,encoding='utf-8')\n", encoding="utf-8")
        (root / "pyproject.toml").write_text(f'''[project]
name = "{name.replace('.', '-')}"
version = "{version}"
[tool.pymss.plugin]
id = "{name}"
api_version = 2
[[tool.pymss.plugin.operations]]
id = "{name}.run"
[tool.pymss.python]
format_version = 1
lock = "python-lock.json"
[tool.pymss.commands]
format_version = 1
[[tool.pymss.commands.operations]]
id = "{name}.run"
python = "main.py"
arguments = [{{input="source"}},{{output="document"}}]
[tool.pymss.commands.operations.inputs.source]
type = "file"
[tool.pymss.commands.operations.outputs.document]
type = "file"
filename = "version.txt"
media_type = "text/plain"
''', encoding="utf-8")
        return root

    def profile(self, name="author.alpha"):
        return Path(json.loads((self.root / "installed" / name / profiles.BINDING).read_bytes())["profile"])

    def run_operation(self, name="author.alpha"):
        self.reset_runtime()
        source = self.work / "中文 input.txt"
        source.write_text("text", encoding="utf-8")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = worker_operations.cmd_run_operation({"operation": name + ".run", "params": {},
                "inputs": {"source": {"kind": "file", "path": str(source)}},
                "outputDir": str(self.work / "results"), "requestId": name})
        self.assertEqual(code, 0, stdout.getvalue())
        events = [json.loads(line) for line in stdout.getvalue().splitlines()]
        result = next(row["payload"] for row in events if row["type"] == "operation_result")
        return Path(result["outputs"]["document"]["path"]).read_text(encoding="utf-8")

    def test_conflicting_profiles_install_update_and_uninstall_through_existing_transactions(self):
        self.install(self.package())
        self.install(self.package("author.beta", "2.0.0"))
        first, second = self.profile(), self.profile("author.beta")
        self.assertEqual(self.run_operation(), "1.0.0")
        self.assertEqual(self.run_operation("author.beta"), "2.0.0")
        self.assertIsNone(importlib.util.find_spec("pymss_isolation_probe"))
        self.package(dependency="2.0.0", version="2.0.0")
        code, events = self.manage("update", name="author.alpha")
        self.assertEqual(code, 0, events)
        self.assertNotEqual(self.profile(), first)
        self.assertFalse(first.exists())
        self.assertEqual(self.run_operation(), "2.0.0")
        active = self.profile()
        self.assertEqual(self.manage("uninstall", name="author.alpha")[0], 0)
        self.assertFalse(active.exists())
        self.assertTrue(second.exists())
        self.assertEqual(self.run_operation("author.beta"), "2.0.0")

    def test_failed_registration_keeps_old_profile_and_cleans_candidate(self):
        source = self.package()
        self.install(source)
        previous = self.profile()
        self.package(dependency="2.0.0", version="2.0.0")
        (source / "__init__.py").write_text("def setup(plugin): raise RuntimeError('Registration failed')\n", encoding="utf-8")
        code, events = self.manage("update", name="author.alpha")
        self.assertEqual(code, 1, events)
        self.assertEqual(self.profile(), previous)
        self.assertEqual(self.run_operation(), "1.0.0")
        self.assertEqual(list((self.root / "python-profiles").glob("python-*")), [previous])

    def test_switching_runtime_prepares_a_fresh_profile_and_preserves_the_old_one(self):
        self.install(self.package())
        cpu_profile = self.profile()
        self.switch("cuda")
        code, events = self.manage("prepare", name="author.alpha")
        self.assertEqual(code, 0, events)
        self.assertNotEqual(self.profile(), cpu_profile)
        self.assertTrue(cpu_profile.is_dir())
        self.assertEqual(self.run_operation(), "1.0.0")

    def test_collection_preserves_running_leases(self):
        from pymss.operations.python_profiles import python_environment

        self.install(self.package())
        profile = self.profile()
        with python_environment(self.root / "installed/author.alpha"):
            code, events = self.manage("uninstall", name="author.alpha")
            self.assertEqual(code, 0, events)
            self.assertTrue(profile.exists())
        profiles.collect_python_profiles(self.root)
        self.assertFalse(profile.exists())

    def test_active_transaction_journal_keeps_candidate_alive_until_rollback(self):
        source = self.package()
        self.root.mkdir(parents=True, exist_ok=True)
        with plugins._installation_transaction(self.root, concurrent=True) as transaction:
            candidate = transaction / "sources/author.alpha"
            candidate.parent.mkdir()
            shutil.copytree(source, candidate)
            profiles.prepare_python(candidate, self.root, lambda _message: None)
            profile = Path(json.loads((candidate / profiles.BINDING).read_bytes())["profile"])
            profiles.collect_python_profiles(self.root)
            self.assertTrue(profile.is_dir())
        self.assertFalse(profile.exists())

    def test_abandoned_transaction_recovery_reclaims_its_partial_profile(self):
        import uuid

        self.root.mkdir(parents=True, exist_ok=True)
        profile = self.root / "python-profiles" / ("python-" + uuid.uuid4().hex)
        profile.mkdir(parents=True)
        (profile / ".pymss-profile.json").write_text(json.dumps({"formatVersion": 1,
            "profile": str(profile.resolve()), "planDigest": "0" * 64}), encoding="utf-8")
        (profile / ".preparing").touch()
        transaction = self.root / "transactions/install-abandoned"
        transaction.mkdir(parents=True)
        (transaction / ("python-profile-" + profile.name + ".json")).write_text(
            json.dumps({"profile": str(profile.resolve())}), encoding="utf-8")
        profiles.collect_python_profiles(self.root)
        self.assertTrue(profile.exists())
        plugins._recover(self.root)
        profiles.collect_python_profiles(self.root)
        self.assertFalse(profile.exists())


if __name__ == "__main__":
    bindings.unittest.main()
