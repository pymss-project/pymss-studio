from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

if __package__:
    from . import test_worker_plugins as marketplace
else:
    import test_worker_plugins as marketplace
import worker_plugins as plugins


class PluginConcurrencyTests(unittest.TestCase):
    setUp = marketplace.PluginMarketplaceTests.setUp
    reset_runtime = staticmethod(marketplace.PluginMarketplaceTests.reset_runtime)
    source = marketplace.PluginMarketplaceTests.source
    manifest = staticmethod(marketplace.PluginMarketplaceTests.manifest)

    def start_install(self, label: str, source: Path, dependency_version: str = ""):
        gate = self.work / "gate"
        gate.mkdir(exist_ok=True)
        script = r'''
import sys, time
from pathlib import Path
import worker_plugins as p
p._PLUGIN_MANIFEST = Path(sys.argv[1])
gate, label, source = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
original = p._acquire_source
def acquire(*args, **kwargs):
    (gate / (label + '.started')).touch()
    deadline = time.monotonic() + 25
    while not (gate / 'release').exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('Concurrent acquisition did not start')
        time.sleep(.02)
    return original(*args, **kwargs)
p._acquire_source = acquire
if sys.argv[5]:
    def dependencies(plugin, root, task_id, payload):
        info = plugin / '.studio-deps' / ('studio_concurrency_peer_test-' + sys.argv[5] + '.dist-info')
        info.mkdir(parents=True)
        (info / 'METADATA').write_text('Metadata-Version: 2.1\nName: studio-concurrency-peer-test\nVersion: ' + sys.argv[5] + '\n')
    p._install_dependencies = dependencies
raise SystemExit(p.cmd_manage_plugin({'action':'install', 'target':source, 'taskId':label}))
'''
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(plugins.__file__).parent), os.environ.get("PYTHONPATH", "")]), "PYTHONUTF8": "1"}
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-c", script,
                                    str(self.plugin_manifest), str(gate), label, str(source), dependency_version],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)
        self.addCleanup(self.stop, process)
        return process

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=15)

    def wait_for_preparations(self):
        deadline = time.monotonic() + 20
        while len(list((self.work / "gate").glob("*.started"))) < 2:
            if time.monotonic() >= deadline:
                self.fail("Two different installer processes did not prepare concurrently")
            time.sleep(.02)

    def complete(self, process):
        stdout, stderr = process.communicate(timeout=60)
        events = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
        self.assertTrue(events, stderr)
        return process.returncode, events[-1]

    def test_live_preparations_overlap_and_survive_inventory_recovery(self):
        first = self.start_install("first", self.source(name="one"))
        second = self.start_install("second", self.source(name="two"))
        self.wait_for_preparations()
        transactions = list((self.root / "transactions").glob("install-*"))
        self.assertEqual(len(transactions), 2)
        plugins._recover_for_read(self.root)
        self.assertTrue(all(path.is_dir() for path in transactions))
        (self.work / "gate/release").touch()
        for process in (first, second):
            code, event = self.complete(process)
            self.assertEqual(code, 0, event)
        self.assertTrue((self.root / "installed/one").is_dir())
        self.assertTrue((self.root / "installed/two").is_dir())

    def test_same_id_installers_cannot_replace_the_first_committed_plugin(self):
        source = self.source()
        first, second = self.start_install("first", source), self.start_install("second", source)
        self.wait_for_preparations()
        (self.work / "gate/release").touch()
        results = [self.complete(process) for process in (first, second)]
        self.assertEqual(sorted(code for code, _event in results), [0, 1], results)
        self.assertEqual(plugins._read_manifest(self.root / "installed/hello")["id"], "hello")
        failed = next(event for code, event in results if code)
        self.assertIn("already installed", failed["payload"]["message"])

    def test_recovery_removes_an_abandoned_preparation_and_preserves_the_other(self):
        first = self.start_install("first", self.source(name="one"))
        second = self.start_install("second", self.source(name="two"))
        self.wait_for_preparations()
        self.stop(first)
        plugins._recover_for_read(self.root)
        self.assertEqual(len(list((self.root / "transactions").glob("install-*"))), 1)
        (self.work / "gate/release").touch()
        code, event = self.complete(second)
        self.assertEqual(code, 0, event)
        self.assertFalse((self.root / "installed/one").exists())
        self.assertTrue((self.root / "installed/two").is_dir())

    def test_concurrent_transitive_dependency_conflicts_are_rejected_at_commit(self):
        first = self.start_install("first", self.source(name="one"), "1.0")
        second = self.start_install("second", self.source(name="two"), "2.0")
        self.wait_for_preparations()
        (self.work / "gate/release").touch()
        results = [self.complete(process) for process in (first, second)]
        self.assertEqual(sorted(code for code, _event in results), [0, 1], results)
        self.assertEqual(len(plugins._installed(self.root)), 1)
        failed = next(event for code, event in results if code)
        self.assertIn("dependency conflict", failed["payload"]["message"])
