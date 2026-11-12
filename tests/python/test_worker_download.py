from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from queue import Queue
from types import ModuleType, SimpleNamespace
from unittest import mock
from urllib.error import URLError

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap

from worker_download import (
    _make_pymss_progress_adapter,
    _pymss_reports_progress,
    _watch_download_progress,
    cmd_download_model,
    download_studio_model,
    files_for_studio_model,
    prepare_pymss_download,
)
from worker_proxy import effective_proxy_url, parse_proxy_config


class EffectiveProxyUrlTest(unittest.TestCase):
    """The studio proxy setting resolves to the pymss ``proxy=`` argument."""

    def test_custom_http_proxy_passes_through(self):
        proxy = parse_proxy_config({"mode": "custom", "url": "http://127.0.0.1:8080", "bypass": ""})
        self.assertEqual(effective_proxy_url(proxy), "http://127.0.0.1:8080")

    def test_custom_socks_proxy_passes_through(self):
        # pymss 2.1.1+ handles SOCKS itself (PySocks via pymss[proxy]); the worker no
        # longer decides downloader routing for it.
        proxy = parse_proxy_config({"mode": "custom", "url": "socks5://127.0.0.1:1080", "bypass": ""})
        self.assertEqual(effective_proxy_url(proxy), "socks5://127.0.0.1:1080")

    def test_none_mode_forces_direct_connection(self):
        # "" tells pymss to skip even the environment lookup; None would not.
        proxy = parse_proxy_config({"mode": "none", "url": "", "bypass": ""})
        self.assertEqual(effective_proxy_url(proxy), "")

    def test_system_mode_mirrors_urllib_discovery(self):
        proxy = parse_proxy_config({"mode": "system", "url": "", "bypass": ""})
        with mock.patch("worker_proxy.urllib.request.getproxies", return_value={"https": "http://127.0.0.1:7890"}):
            self.assertEqual(effective_proxy_url(proxy), "http://127.0.0.1:7890")

    def test_system_mode_without_discovered_proxy_is_direct(self):
        proxy = parse_proxy_config({"mode": "system", "url": "", "bypass": ""})
        with mock.patch("worker_proxy.urllib.request.getproxies", return_value={}):
            self.assertEqual(effective_proxy_url(proxy), "")

    def test_system_mode_rejects_schemes_pymss_cannot_use(self):
        proxy = parse_proxy_config({"mode": "system", "url": "", "bypass": ""})
        with mock.patch("worker_proxy.urllib.request.getproxies", return_value={"http": "ftp://127.0.0.1:21"}):
            self.assertEqual(effective_proxy_url(proxy), "")


class PymssCapabilityTest(unittest.TestCase):
    """Which progress source is usable depends on the pymss actually installed."""

    def test_detects_a_pymss_that_accepts_the_callback(self):
        def new_style(model_name, model_dir=None, progress_callback=None):
            pass

        self.assertTrue(_pymss_reports_progress(new_style))

    def test_detects_a_pymss_that_predates_the_callback(self):
        def old_style(model_name, model_dir=None):
            pass

        self.assertFalse(_pymss_reports_progress(old_style))

    def test_treats_an_unintrospectable_callable_as_unsupported(self):
        # Some builtins have no retrievable signature; guessing yes would raise TypeError at the
        # call site and fail the download outright.
        self.assertFalse(_pymss_reports_progress(print))

    def test_old_pymss_with_aria2_falls_back_before_stdout_is_corrupted(self):
        def old_style(model_name, model_dir=None):
            pass

        module = SimpleNamespace(ARIA2C_PATH="C:/bin/aria2c.exe")
        with mock.patch("worker_download.emit"):
            prepare_pymss_download(module, "task", old_style)

        self.assertIsNone(module.ARIA2C_PATH)

    def test_new_pymss_keeps_aria2_when_proxy_allows_it(self):
        def new_style(model_name, model_dir=None, progress_callback=None):
            pass

        module = SimpleNamespace(ARIA2C_PATH="C:/bin/aria2c.exe")
        with mock.patch("worker_download.emit"):
            prepare_pymss_download(module, "task", new_style)

        self.assertEqual(module.ARIA2C_PATH, "C:/bin/aria2c.exe")

    def test_user_can_force_urllib(self):
        def new_style(model_name, model_dir=None, progress_callback=None):
            pass

        module = SimpleNamespace(ARIA2C_PATH="C:/bin/aria2c.exe")
        with mock.patch("worker_download.emit"):
            prepare_pymss_download(module, "task", new_style, "urllib")

        self.assertIsNone(module.ARIA2C_PATH)


class StudioModelFileResolutionTest(unittest.TestCase):
    def test_uses_studio_catalog_entry_paths(self):
        entry = SimpleNamespace(
            name="debug_model.ckpt",
            source="catalog",
            relpath="debug/debug_model.ckpt",
            config_relpath="debug/debug_model.yaml",
            auxiliary_relpaths=("debug/debug_model.json",),
        )
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, True))

        with mock.patch("worker_download.get_any_model_entry", return_value=entry):
            resolved_entry, files = files_for_studio_model("debug_model.ckpt", str(root))

        self.assertIs(resolved_entry, entry)
        self.assertEqual(files, [
            ("debug/debug_model.ckpt", root / "debug" / "debug_model.ckpt"),
            ("debug/debug_model.yaml", root / "debug" / "debug_model.yaml"),
            ("debug/debug_model.json", root / "debug" / "debug_model.json"),
        ])


class Aria2FallbackTest(unittest.TestCase):
    def test_retries_with_urllib_when_aria2_fails(self):
        calls: list[str | None] = []
        openssl_env: list[tuple[str | None, str | None]] = []

        def download_file(_url, _dest, _expected_size, _expected_sha256, **_kwargs):
            calls.append(module.ARIA2C_PATH)
            openssl_env.append((os.environ.get("OPENSSL_CONF"), os.environ.get("OPENSSL_MODULES")))
            if module.ARIA2C_PATH:
                raise RuntimeError("aria2c failed with exit code 1: OSSL_PROVIDER_load 'legacy' failed")

        module = SimpleNamespace(
            ARIA2C_PATH="/app/bin/aria2c",
            remote_url=lambda relpath, source="modelscope", endpoint=None: f"https://example.test/{relpath}",
            _download_file=download_file,
            _already_valid=lambda *_args: False,
            _expected_size_and_hash=lambda *_args: (1, "sha256"),
            fetch_modelscope_file_index=lambda timeout=30, proxy=None: {},
        )
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, True))

        with mock.patch("worker_download.emit"), \
             mock.patch.dict(os.environ, {
                 "PYMSS_STUDIO_OPENSSL_CONF": "/app/bin/openssl/openssl.cnf",
                 "PYMSS_STUDIO_OPENSSL_MODULES": "/app/bin/openssl/ossl-modules",
             }, clear=True):
            result = download_studio_model(module, "model", [("model.th", root / "model.th")], task_id="task")

        self.assertEqual(calls, ["/app/bin/aria2c", None])
        self.assertEqual(openssl_env, [("/app/bin/openssl/openssl.cnf", "/app/bin/openssl/ossl-modules"), (None, None)])
        self.assertIsNone(module.ARIA2C_PATH)
        self.assertEqual(result["downloaded"], [str(root / "model.th")])


class DownloadCallbackSafetyTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def module(self, download):
        return SimpleNamespace(
            ARIA2C_PATH="fixture-aria2c",
            remote_url=lambda relpath, **_kwargs: f"https://example.test/{relpath}",
            _download_file=download,
            _already_valid=lambda *_args: False,
            _expected_size_and_hash=lambda *_args: (6, None),
            fetch_modelscope_file_index=lambda **_kwargs: {},
        )

    def test_boundary_and_later_callback_errors_do_not_stop_or_retry_downloads(self):
        attempts, updates = [], []

        def download(_url, dest, _size, _hash, *, timeout, progress_callback):
            attempts.append((dest.name, module.ARIA2C_PATH))
            for done in (0, 3, 6):
                progress_callback(done, 6, "")
            dest.write_bytes(b"abcdef")

        def progress(*args):
            updates.append(args)
            raise OSError("aria2c progress output closed")

        module = self.module(download)
        files = [(name, self.root / name) for name in ("first.bin", "second.bin")]
        result = download_studio_model(module, "model", files, progress_callback=progress)
        self.assertEqual(attempts, [("first.bin", "fixture-aria2c"), ("second.bin", "fixture-aria2c")])
        self.assertEqual(len(updates), 8, "both file boundaries and every transfer callback were attempted")
        self.assertEqual(module.ARIA2C_PATH, "fixture-aria2c")
        self.assertEqual(result["downloaded"], [str(dest) for _, dest in files])
        self.assertTrue(all(dest.read_bytes() == b"abcdef" for _, dest in files))

    def test_keyboard_interrupt_from_boundary_or_later_callback_still_propagates(self):
        for cancel_on in (1, 3):
            with self.subTest(cancel_on=cancel_on):
                attempts, updates = [], []

                def download(_url, _dest, _size, _hash, *, timeout, progress_callback):
                    attempts.append(module.ARIA2C_PATH)
                    for done in (0, 3, 6):
                        progress_callback(done, 6, "")

                def progress(*args):
                    updates.append(args)
                    if len(updates) == cancel_on:
                        raise KeyboardInterrupt("cancel download")

                module = self.module(download)
                with self.assertRaises(KeyboardInterrupt):
                    download_studio_model(module, "model", [("model.bin", self.root / "model.bin")],
                                          progress_callback=progress)
                self.assertEqual(len(attempts), 0 if cancel_on == 1 else 1)
                self.assertEqual(module.ARIA2C_PATH, "fixture-aria2c")

    def test_file_download_errors_are_not_treated_as_callback_errors(self):
        def download(_url, _dest, _size, _hash, *, timeout, progress_callback):
            progress_callback(0, 6, "")
            raise OSError("HTTP file transfer failed")

        module = self.module(download)
        with self.assertRaisesRegex(OSError, "HTTP file transfer failed"):
            download_studio_model(module, "model", [("model.bin", self.root / "model.bin")],
                                  progress_callback=lambda *_args: None)


class ProgressAdapterTest(unittest.TestCase):
    """pymss reports one file at a time; the UI shows one bar for the whole model."""

    def _collect(self, **kwargs):
        seen: list[dict] = []
        adapter = _make_pymss_progress_adapter(emit_progress=lambda **kw: seen.append(kw), **kwargs)
        return adapter, seen

    def test_accumulates_across_files_when_the_byte_count_restarts(self):
        adapter, seen = self._collect(skipped_bytes=0, skipped_files=0, total_bytes=300)
        # 0..100 is the first file; the drop back to 0 is the only signal that a second began.
        for done in (0, 50, 100, 0, 60, 200):
            adapter(done, 0, "")

        self.assertEqual([kw["downloaded_bytes"] for kw in seen], [0, 50, 100, 100, 160, 300])
        self.assertEqual([kw["completed_files"] for kw in seen], [0, 0, 0, 1, 1, 1])

    def test_files_that_were_already_valid_seed_the_total(self):
        # pymss never calls back for a file it skips, so those bytes have to be counted up front.
        adapter, seen = self._collect(skipped_bytes=500, skipped_files=2, total_bytes=600)
        adapter(50, 0, "")

        self.assertEqual(seen[0]["downloaded_bytes"], 550)
        self.assertEqual(seen[0]["completed_files"], 2)


class DownloadCacheIntegrityTest(unittest.TestCase):
    def setUp(self):
        from pymss import model_download

        self.module = model_download
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.dest = self.root / "model.bin"
        self.files = [("model.bin", self.dest)]
        self.payload = b"complete-model-payload"
        self.entry = SimpleNamespace(name="model", relpath="model.bin", size_bytes=0)
        self.download_calls = []
        self.fetch = self.enterContext(mock.patch.object(
            self.module, "fetch_modelscope_file_index", side_effect=URLError("index unavailable"),
        ))

        def download(_url, dest, _size, _digest, *, timeout, progress_callback=None, proxy=None):
            self.download_calls.append(dest)
            dest.write_bytes(self.payload)
            if progress_callback is not None:
                progress_callback(len(self.payload), len(self.payload), "Downloaded model.bin")

        self.enterContext(mock.patch.object(self.module, "_download_file", download))
        self.enterContext(mock.patch.object(self.module, "ARIA2C_PATH", None))
        self.enterContext(mock.patch.object(self.module, "remote_url", return_value="https://example.test/model.bin"))
        self.events = self.enterContext(mock.patch("worker_download.emit"))
        self.failures = self.enterContext(mock.patch("worker_download.emit_error", return_value=1))
        self.enterContext(mock.patch("worker_download.effective_proxy_url", return_value=""))
        self.enterContext(mock.patch("worker_download.model_root", return_value=self.root))
        self.model_info = self.enterContext(mock.patch("worker_download.model_to_dict", return_value={"name": "model"}))
        self.enterContext(mock.patch("worker_download.effective_source_for", return_value="modelscope"))

    def command(self, **payload):
        with mock.patch("worker_download.files_for_studio_model", return_value=(self.entry, self.files)):
            return cmd_download_model({"model": "model", "taskId": "cache-integrity", **payload})

    def test_command_redownloads_unverifiable_cache_after_index_failure(self):
        self.dest.write_bytes(b"truncated")

        result = self.command()

        self.assertEqual(result, 0)
        self.fetch.assert_called_once_with(timeout=30, proxy="")
        self.assertEqual(self.download_calls, [self.dest])
        self.assertEqual(self.dest.read_bytes(), self.payload)
        self.assertTrue(any(call.args[0] == "download_done" for call in self.events.call_args_list))

    def test_direct_download_redownloads_unverifiable_cache_after_index_failure(self):
        self.dest.write_bytes(b"truncated")

        result = download_studio_model(self.module, "model", self.files, proxy="")

        self.fetch.assert_called_once_with(timeout=30, proxy="")
        self.assertEqual(self.download_calls, [self.dest])
        self.assertEqual(self.dest.read_bytes(), self.payload)
        self.assertEqual(result["downloaded"], [str(self.dest)])

    def test_catalog_cache_is_reused_without_a_metadata_request(self):
        self.entry.size_bytes = len(self.payload)
        self.dest.write_bytes(self.payload)
        self.assertEqual(self.command(), 0)
        self.fetch.assert_not_called()
        self.assertEqual(self.download_calls, [])

    def test_direct_catalog_cache_is_reused_without_metadata(self):
        self.entry.size_bytes = len(self.payload)
        self.dest.write_bytes(self.payload)
        result = download_studio_model(self.module, "model", self.files, entry=self.entry)
        self.assertEqual(result, {"downloaded": [], "skipped": [str(self.dest)]})
        self.fetch.assert_not_called()

    def test_wrong_primary_size_is_downloaded_using_catalog_metadata(self):
        self.entry.size_bytes = len(self.payload)
        self.dest.write_bytes(b"truncated")
        self.assertEqual(self.command(), 0)
        self.assertEqual(self.download_calls, [self.dest])
        self.assertEqual(self.dest.read_bytes(), self.payload)

    def test_huggingface_does_not_query_modelscope_even_with_missing_files(self):
        self.entry.size_bytes = len(self.payload)
        self.assertEqual(self.command(source="huggingface"), 0)
        self.fetch.assert_not_called()
        self.assertEqual(self.download_calls, [self.dest])

    def test_empty_secondary_is_not_counted_as_complete(self):
        self.entry.size_bytes = len(self.payload)
        self.dest.write_bytes(self.payload)
        auxiliary = self.root / "config.yaml"
        auxiliary.touch()
        self.files.append(("config.yaml", auxiliary))
        self.assertEqual(self.command(), 0)
        self.assertEqual(self.download_calls, [auxiliary])

    def test_available_index_hash_remains_authoritative(self):
        self.entry.size_bytes = len(self.payload)
        self.dest.write_bytes(b"x" * len(self.payload))
        digest = hashlib.sha256(self.payload).hexdigest()
        result = download_studio_model(self.module, "model", self.files, entry=self.entry,
                                      file_index={"model.bin": {"Size": len(self.payload), "Sha256": digest}})
        self.assertEqual(result["downloaded"], [str(self.dest)])
        self.fetch.assert_not_called()

    def test_explicit_force_download_can_replace_cache_without_index(self):
        self.dest.write_bytes(b"truncated")

        self.assertEqual(self.command(force=True), 0)

        self.assertEqual(self.download_calls, [self.dest])
        self.assertEqual(self.dest.read_bytes(), self.payload)
        self.assertTrue(any(call.args[0] == "download_done" for call in self.events.call_args_list))

    def test_explicit_unverified_modes_keep_existing_cache_semantics(self):
        self.dest.write_bytes(self.payload)
        self.fetch.side_effect = AssertionError("metadata must not be fetched")

        for options in [{"verify": False}, {"endpoint": "https://custom.test"}]:
            with self.subTest(options=options):
                result = download_studio_model(self.module, "model", self.files, **options)
                self.assertEqual(result, {"downloaded": [], "skipped": [str(self.dest)]})

        self.fetch.assert_not_called()
        self.assertEqual(self.download_calls, [])
        self.assertEqual(self.dest.read_bytes(), self.payload)

    def test_available_metadata_still_reuses_a_verified_cache(self):
        self.dest.write_bytes(self.payload)
        self.fetch.side_effect = None
        self.fetch.return_value = {"model.bin": {
            "Size": len(self.payload), "Sha256": hashlib.sha256(self.payload).hexdigest(),
        }}

        result = download_studio_model(self.module, "model", self.files, proxy="")

        self.assertEqual(result, {"downloaded": [], "skipped": [str(self.dest)]})
        self.assertEqual(self.download_calls, [])
        self.fetch.assert_called_once_with(timeout=30, proxy="")


class DownloadMetadataTest(unittest.TestCase):
    def test_verify_false_ignores_a_supplied_index_and_does_not_fetch_metadata(self):
        fetch = mock.Mock(side_effect=AssertionError("metadata must not be fetched"))
        expected = mock.Mock(return_value=(None, None))
        download = mock.Mock()
        module = SimpleNamespace(
            ARIA2C_PATH=None,
            remote_url=lambda relpath, **_kwargs: f"https://example.test/{relpath}",
            _download_file=download,
            _already_valid=lambda *_args: False,
            _expected_size_and_hash=expected,
            fetch_modelscope_file_index=fetch,
        )
        dest = Path("fixture-model.bin")
        result = download_studio_model(module, "model", [("model.bin", dest)],
                                       verify=False, file_index={"model.bin": {"Size": 64}})
        fetch.assert_not_called()
        expected.assert_called_once_with("model.bin", None)
        self.assertEqual(download.call_args.args[2:4], (None, None))
        self.assertEqual(result["downloaded"], [str(dest)])


class PartialFileWatcherTest(unittest.TestCase):
    """The fallback for a pymss that cannot report progress itself."""

    def _watch_once(self, *, count_partial_bytes: bool) -> dict:
        tmp = Path(tempfile.mkdtemp())
        dest = tmp / "model.pth"
        # aria2 preallocates, so a barely-started download already sits at its full size.
        (tmp / "model.pth.part").write_bytes(b"x" * 120)

        seen: list[dict] = []
        stop = threading.Event()

        def emit_progress(**kwargs):
            seen.append(kwargs)
            stop.set()  # one pass is all the assertions need

        _watch_download_progress(
            files=[("vr/model.pth", dest)],
            expected_sizes={str(dest): 120},
            already_done=set(),
            stop=stop,
            emit_progress=emit_progress,
            count_partial_bytes=count_partial_bytes,
        )
        return seen[0]

    def test_counts_partial_bytes_when_the_downloader_grows_the_file(self):
        result = self._watch_once(count_partial_bytes=True)

        self.assertEqual(result["downloaded_bytes"], 120)

    def test_ignores_partial_bytes_that_would_report_a_finished_download(self):
        result = self._watch_once(count_partial_bytes=False)

        self.assertEqual(result["downloaded_bytes"], 0)
        self.assertEqual(result["completed_files"], 0)


class DownloadCommandProgressTest(unittest.TestCase):
    """Exercise the worker event stream through the actual download command."""

    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.now = 0.0
        self.events = []
        self.progress_updates = Queue()
        self.module = ModuleType("pymss.model_download")
        self.module.ARIA2C_PATH = "fixture-aria2c"
        self.module.remote_url = lambda relpath, **_kwargs: f"https://example.test/{relpath}"
        self.module.fetch_modelscope_file_index = lambda **_kwargs: {}
        self.valid = set()
        self.sizes = {}
        self.module._already_valid = lambda dest, *_args: dest in self.valid
        self.module._expected_size_and_hash = lambda relpath, _index: (self.sizes.get(relpath), None)
        package = ModuleType("pymss")
        package.model_download = self.module
        self.enterContext(mock.patch.dict(sys.modules, {
            "pymss": package, "pymss.model_download": self.module,
        }))
        self.enterContext(mock.patch("worker_download.time.monotonic", side_effect=lambda: self.now))
        self.enterContext(mock.patch("worker_download.effective_proxy_url", return_value=""))
        self.enterContext(mock.patch("worker_download.model_root", return_value=self.root))
        self.enterContext(mock.patch("worker_download.model_to_dict", return_value={"name": "model"}))
        self.enterContext(mock.patch("worker_download.effective_source_for", return_value="modelscope"))
        self.failures = self.enterContext(mock.patch("worker_download.emit_error", return_value=1))

        def emit(event, payload, **kwargs):
            self.events.append((event, dict(payload), kwargs.get("task_id"), self.now))
            if event == "download_progress":
                self.progress_updates.put(dict(payload))

        self.enterContext(mock.patch("worker_download.emit", side_effect=emit))

    def run_command(self, files, downloader, *, legacy=False):
        def with_callback(_name, progress_callback=None):
            pass

        def without_callback(_name):
            pass

        self.module.download_model = without_callback if legacy else with_callback
        self.module._download_file = downloader
        entry = SimpleNamespace(name="model", relpath=files[0][0], size_bytes=self.sizes.get(files[0][0], 0))
        with mock.patch("worker_download.files_for_studio_model", return_value=(entry, files)):
            result = cmd_download_model({"model": "model", "taskId": "transfer"})
        self.assertEqual(result, 0, self.failures.call_args)
        self.failures.assert_not_called()
        self.assertTrue(all(task_id == "transfer" for _, _, task_id, _ in self.events))

    def assert_completion_stages(self):
        progress = [payload["progress"] for _, payload, _, _ in self.events if "progress" in payload]
        self.assertEqual(progress, sorted(progress), "preparation and transfer must not move backwards")
        self.assertEqual(progress[-3:], [95, 97, 100])
        self.assertTrue(all(payload["progress"] <= 95 for event, payload, _, _ in self.events if event == "download_progress"))
        self.assertEqual(self.events[-1][0], "download_done")

    def test_known_size_command_starts_at_zero_and_reserves_completion_stages(self):
        self.sizes = {"model.bin": 1000}

        def download(_url, dest, _size, _hash, *, timeout, progress_callback):
            for done in (0, 10, 500, 1000):
                progress_callback(done, 1000, "")
            dest.write_bytes(b"x" * 1000)

        self.run_command([("model.bin", self.root / "model.bin")], download)
        stages = [payload["progress"] for event, payload, _, _ in self.events if event == "download_stage"]
        self.assertEqual(stages, [0, 0, 97])
        self.assert_completion_stages()

    def test_unknown_size_metrics_are_sampled_and_identical_events_stay_suppressed(self):
        def download(_url, dest, _size, _hash, *, timeout, progress_callback):
            progress_callback(0, 0, "")
            for done in range(1, 201):
                self.now = done / 500
                progress_callback(done, 0, "")
            for at, done in [(1.05, 300), (1.1, 400), (2.1, 500), (3.2, 500), (5.0, 500)]:
                self.now = at
                progress_callback(done, 0, "")
            dest.write_bytes(b"x" * 500)

        self.run_command([("model.bin", self.root / "model.bin")], download)
        sampled = [(payload, at) for event, payload, _, at in self.events
                   if event == "download_progress" and payload["completedFiles"] == 0]
        self.assertEqual([payload["aggregateDownloadedBytes"] for payload, _ in sampled], [0, 300, 500, 500])
        self.assertTrue(all(payload["progress"] == 0 and payload["aggregateTotalBytes"] == 0 for payload, _ in sampled))
        self.assertTrue(all(right[1] - left[1] >= 1 for left, right in zip(sampled, sampled[1:])))
        self.assertGreater(sampled[1][0]["speedBytesPerSecond"], 0)
        self.assertEqual(sampled[-1][0]["speedBytesPerSecond"], 0)
        settled = [payload for event, payload, _, _ in self.events if event == "download_progress"][-1]
        self.assertEqual((settled["aggregateDownloadedBytes"], settled["aggregateTotalBytes"]), (500, 500))
        self.assert_completion_stages()

    def test_completion_counts_mixed_known_unknown_and_already_valid_files(self):
        self.sizes = {"known.bin": 100}
        skipped = self.root / "skipped.bin"
        skipped.write_bytes(b"s" * 60)
        self.valid.add(skipped)

        def download(_url, dest, size, _hash, *, timeout, progress_callback):
            amount = size or 40
            progress_callback(0, size or 0, "")
            progress_callback(amount, size or 0, "")
            dest.write_bytes(b"x" * amount)

        self.run_command([(name, self.root / name) for name in ("known.bin", "unknown.bin", "skipped.bin")], download)
        updates = [payload for event, payload, _, _ in self.events if event == "download_progress"]
        self.assertEqual(updates[0]["aggregateDownloadedBytes"], 60)
        self.assertTrue(all(payload["aggregateTotalBytes"] == 0 for payload in updates[:-1]))
        self.assertEqual([payload["progress"] for payload in updates], [31, 63, 95])
        self.assertEqual(updates[-1]["aggregateDownloadedBytes"], 200)
        self.assertEqual(updates[-1]["aggregateTotalBytes"], 200)
        self.assertEqual(updates[-1]["completedFiles"], 3)
        self.assert_completion_stages()

    def test_legacy_command_uses_the_real_watcher_with_partial_size_metadata(self):
        dest = self.root / "model.bin"
        cached = self.root / "cached.bin"
        cached.write_bytes(b"c" * 100)
        self.valid.add(cached)
        self.sizes = {"cached.bin": 100}

        def download(_url, destination, _size, _hash, *, timeout):
            self.assertIsNone(self.module.ARIA2C_PATH, "old aria2 output must not enter the protocol")
            initial = self.progress_updates.get(timeout=2)
            self.assertEqual((initial["aggregateDownloadedBytes"], initial["aggregateTotalBytes"], initial["progress"]), (100, 0, 47))
            partial = destination.with_name(destination.name + ".part")
            self.now = 1.1
            partial.write_bytes(b"x" * 20)
            sampled = self.progress_updates.get(timeout=2)
            self.assertEqual((sampled["aggregateDownloadedBytes"], sampled["progress"]), (120, 47))
            partial.write_bytes(b"x" * 64)
            partial.replace(destination)
            self.now = 1.2
            completed = self.progress_updates.get(timeout=2)
            self.assertEqual((completed["completedFiles"], completed["aggregateTotalBytes"]), (2, 0))

        self.run_command([("cached.bin", cached), ("model.bin", dest)], download, legacy=True)
        settled = [payload for event, payload, _, _ in self.events if event == "download_progress"][-1]
        self.assertEqual((settled["aggregateDownloadedBytes"], settled["aggregateTotalBytes"]), (164, 164))
        self.assert_completion_stages()

    def test_failed_index_still_downloads_and_reports_actual_completion(self):
        self.module.fetch_modelscope_file_index = mock.Mock(side_effect=URLError("index unavailable"))

        def download(_url, dest, size, digest, *, timeout, progress_callback):
            self.assertEqual((size, digest), (None, None))
            for done in (0, 32, 64):
                progress_callback(done, 0, "")
            dest.write_bytes(b"x" * 64)

        self.run_command([("model.bin", self.root / "model.bin")], download)
        self.module.fetch_modelscope_file_index.assert_called_once_with(timeout=30, proxy="")
        self.assertTrue(any(event == "download_log" and payload["level"] == "warning"
                            and "index" in payload["message"].lower() for event, payload, _, _ in self.events))
        settled = [payload for event, payload, _, _ in self.events if event == "download_progress"][-1]
        self.assertEqual((settled["aggregateDownloadedBytes"], settled["aggregateTotalBytes"]), (64, 64))
        self.assert_completion_stages()

    def test_available_index_retains_published_size_and_hash_for_download(self):
        index = {"model.bin": {"Size": 64, "Sha256": "published-hash"}}
        self.module.fetch_modelscope_file_index = mock.Mock(side_effect=[index, URLError("index became unavailable")])

        def expected(relpath, metadata):
            self.assertIs(metadata, index)
            return metadata[relpath]["Size"], metadata[relpath]["Sha256"]

        self.module._expected_size_and_hash = expected

        def download(_url, dest, size, digest, *, timeout, progress_callback):
            self.assertEqual((size, digest), (64, "published-hash"))
            progress_callback(64, 64, "")
            dest.write_bytes(b"x" * 64)

        self.run_command([("model.bin", self.root / "model.bin")], download)
        self.module.fetch_modelscope_file_index.assert_called_once_with(timeout=30, proxy="")
        self.assert_completion_stages()

    def test_index_fallback_does_not_hide_a_file_download_failure(self):
        self.module.fetch_modelscope_file_index = mock.Mock(side_effect=URLError("index unavailable"))

        def download(_url, _dest, _size, _hash, *, timeout, progress_callback):
            raise URLError("file transfer failed")

        def with_callback(_name, progress_callback=None):
            pass

        self.module.download_model = with_callback
        self.module._download_file = download
        files = [("model.bin", self.root / "model.bin")]
        with mock.patch("worker_download.files_for_studio_model", return_value=(SimpleNamespace(name="model"), files)):
            result = cmd_download_model({"model": "model", "taskId": "transfer"})
        self.assertEqual(result, 1)
        self.failures.assert_called_once()
        self.assertEqual(self.failures.call_args.args[0], "MODEL_DOWNLOAD_FAILED")
        self.assertIn("file transfer failed", self.failures.call_args.args[1])
        self.assertFalse(any(event == "download_done" for event, _, _, _ in self.events))

    def test_same_file_retry_does_not_count_as_a_new_file_or_duplicate_bytes(self):
        for known in (True, False):
            with self.subTest(known_sizes=known):
                self.events.clear()
                self.module.ARIA2C_PATH = "fixture-aria2c"
                self.sizes = {"weights.bin": 100, "config.bin": 20} if known else {}
                attempts = {}
                weight_updates = []

                def download(_url, dest, _size, _hash, *, timeout, progress_callback):
                    attempts[dest.name] = attempts.get(dest.name, 0) + 1
                    if dest.name == "weights.bin":
                        if attempts[dest.name] == 1:
                            progress_callback(0, 100, "")
                            progress_callback(40, 100, "")
                            raise RuntimeError("aria2c transfer failed")
                        for done in (0, 20, 100):
                            progress_callback(done, 100, "")
                        weight_updates.extend(payload for event, payload, _, _ in self.events if event == "download_progress")
                        dest.write_bytes(b"w" * 100)
                    else:
                        # A downloader need not emit its own initial zero; Studio knows
                        # this is a different file and must preserve that boundary.
                        for done in (10, 20):
                            progress_callback(done, 20, "")
                        dest.write_bytes(b"c" * 20)

                self.run_command([(name, self.root / name) for name in ("weights.bin", "config.bin")], download)
                self.assertEqual(attempts, {"weights.bin": 2, "config.bin": 1})
                self.assertTrue(all(payload["completedFiles"] == 0 for payload in weight_updates))
                updates = [payload for event, payload, _, _ in self.events if event == "download_progress"]
                byte_counts = [payload["aggregateDownloadedBytes"] for payload in updates]
                self.assertEqual(byte_counts, sorted(byte_counts))
                self.assertLessEqual(max(byte_counts), 120)
                self.assertEqual(updates[-1]["aggregateDownloadedBytes"], 120)
                self.assertEqual(updates[-1]["completedFiles"], 2)
                self.assert_completion_stages()


if __name__ == "__main__":
    unittest.main()
