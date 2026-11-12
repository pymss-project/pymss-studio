from __future__ import annotations

import copy
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request
import urllib.request

from . import _bootstrap  # noqa: F401
import worker_runtime_artifacts as artifacts
import worker_proxy


class RuntimeArtifactDownloadTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.body = b"locked runtime wheel"
        self.filename = "torch-2.13.0+rocm10.0.0-cp312-cp312-win_amd64.whl"
        self.artifact = {"name": "torch", "filename": self.filename,
                         "url": artifacts._GITHUB_RELEASE_ROOT + "/v0.1.0/" + quote(self.filename, safe=""),
                         "bytes": len(self.body), "sha256": hashlib.sha256(self.body).hexdigest()}
        self.plan = {"artifacts": [self.artifact]}
        self.opener = mock.Mock()
        self.enterContext(mock.patch.object(artifacts, "build_proxy_opener", return_value=self.opener))

    def urls(self):
        return [call.args[0].full_url for call in self.opener.open.call_args_list]

    def test_old_github_lock_maps_to_both_mirrors_without_mutation(self):
        original = copy.deepcopy(self.artifact)
        urls = artifacts.artifact_download_urls(self.artifact)
        self.assertEqual(urls, [root + "/0.1.0/" + quote(self.filename, safe="")
                               for _, root in artifacts._RUNTIME_MIRRORS])
        self.assertEqual(self.artifact, original)

    def test_mirror_lock_and_prerelease_folder_keep_the_same_fallback_order(self):
        for _, root in artifacts._RUNTIME_MIRRORS:
            item = {**self.artifact, "url": root + "/1.2.3-rc.1%2Bbuild/" + quote(self.filename, safe="")}
            self.assertEqual(artifacts.artifact_download_urls(item),
                             [base + "/1.2.3-rc.1%2Bbuild/" + quote(self.filename, safe="")
                              for _, base in artifacts._RUNTIME_MIRRORS])

    def test_custom_non_github_location_is_not_rewritten(self):
        url = "https://example.invalid/" + self.filename
        self.assertEqual(artifacts.artifact_download_urls({**self.artifact, "url": url}), [url])

    def test_shared_wheel_location_maps_to_both_mirrors(self):
        for _, root in artifacts._RUNTIME_MIRRORS:
            item = {**self.artifact, "url": root + "/wheels/" + quote(self.filename, safe="")}
            self.assertEqual(artifacts.artifact_download_urls(item),
                             [base + "/wheels/" + quote(self.filename, safe="")
                              for _, base in artifacts._RUNTIME_MIRRORS])

    def test_explicit_source_overrides_locale_and_missing_source_uses_locale(self):
        for source, locale, index in (("huggingface", "zh-CN", 1), ("modelscope", "en-US", 0),
                                      ("", "en-US", 1), ("", "zh-TW", 0), ("", "", 0)):
            with self.subTest(source=source, locale=locale):
                (self.root / self.filename).unlink(missing_ok=True)
                self.opener.reset_mock()
                self.opener.open.side_effect = lambda *_args, **_kwargs: io.BytesIO(self.body)
                artifacts.download_locked_artifacts(self.plan, self.root, source=source, locale=locale)
                self.assertEqual(self.urls(), [artifacts.artifact_download_urls(self.artifact)[index]])

    def test_working_mirror_is_reused_for_later_files_and_can_fall_back_again(self):
        other = {**self.artifact, "name": "other", "filename": "other-1.0-py3-none-any.whl"}
        other["url"] = artifacts._GITHUB_RELEASE_ROOT + "/v0.1.0/" + other["filename"]
        third = {**other, "name": "third", "filename": "third-1.0-py3-none-any.whl"}
        third["url"] = artifacts._GITHUB_RELEASE_ROOT + "/v0.1.0/" + third["filename"]
        self.opener.open.side_effect = [URLError("unavailable"), io.BytesIO(self.body),
                                        io.BytesIO(self.body), URLError("missing file"), io.BytesIO(self.body)]
        artifacts.download_locked_artifacts({"artifacts": [self.artifact, other, third]}, self.root)
        first_urls = artifacts.artifact_download_urls(self.artifact)
        self.assertEqual(self.urls(), [*first_urls, artifacts.artifact_download_urls(other)[1],
                                       *reversed(artifacts.artifact_download_urls(third))])

    def test_another_installation_does_not_inherit_previous_mirror_failures(self):
        self.opener.open.side_effect = [URLError("unavailable"), io.BytesIO(self.body)]
        artifacts.download_locked_artifacts(self.plan, self.root)
        (self.root / self.filename).unlink()
        self.opener.reset_mock()
        self.opener.open.side_effect = [io.BytesIO(self.body)]
        artifacts.download_locked_artifacts(self.plan, self.root)
        self.assertEqual(self.urls(), artifacts.artifact_download_urls(self.artifact)[:1])

    def test_unrecognized_github_or_invalid_official_locations_are_rejected(self):
        for url in (
            "https://github.com/other/project/releases/download/v0.1.0/" + self.filename,
            "https://github.com./other/project/" + self.filename,
            artifacts._GITHUB_RELEASE_ROOT + "/updater/" + self.filename,
            artifacts._GITHUB_RELEASE_ROOT + "/v0.1.0/nested/" + self.filename,
            artifacts._GITHUB_RELEASE_ROOT + "/v0.1.0/other.whl",
            self.artifact["url"] + "?token=secret",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                artifacts.artifact_download_urls({**self.artifact, "url": url})

    def test_first_mirror_success_publishes_verified_file_and_closes_response(self):
        response = io.BytesIO(self.body)
        self.opener.open.return_value = response
        original = copy.deepcopy(self.plan)
        artifacts.download_locked_artifacts(self.plan, self.root)
        self.assertEqual((self.root / self.filename).read_bytes(), self.body)
        self.assertEqual(self.urls(), artifacts.artifact_download_urls(self.artifact)[:1])
        self.assertTrue(response.closed)
        self.assertEqual(self.plan, original)
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_network_failure_retries_hf_without_accessing_github(self):
        self.opener.open.side_effect = [URLError("unavailable"), io.BytesIO(self.body)]
        artifacts.download_locked_artifacts(self.plan, self.root)
        self.assertEqual(self.urls(), artifacts.artifact_download_urls(self.artifact))
        self.assertEqual((self.root / self.filename).read_bytes(), self.body)

    def test_invalid_content_retries_hf_and_never_installs_bad_bytes(self):
        for invalid in (b"short", self.body + b"extra", b"x" * len(self.body)):
            with self.subTest(body=invalid):
                (self.root / self.filename).unlink(missing_ok=True)
                self.opener.reset_mock()
                self.opener.open.side_effect = [io.BytesIO(invalid), io.BytesIO(self.body)]
                artifacts.download_locked_artifacts(self.plan, self.root)
                self.assertEqual(self.urls(), artifacts.artifact_download_urls(self.artifact))
                self.assertEqual((self.root / self.filename).read_bytes(), self.body)

    def test_all_mirrors_failing_preserves_existing_file_and_cleans_partials(self):
        (self.root / self.filename).write_bytes(b"old incomplete content")
        self.opener.open.side_effect = URLError("https://cdn.invalid/?token=secret")
        with self.assertRaises(RuntimeError) as error:
            artifacts.download_locked_artifacts(self.plan, self.root)
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(self.urls(), artifacts.artifact_download_urls(self.artifact))
        self.assertEqual((self.root / self.filename).read_bytes(), b"old incomplete content")
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_verified_local_file_is_rehashed_before_reuse(self):
        (self.root / self.filename).write_bytes(self.body)
        artifacts.download_locked_artifacts(self.plan, self.root)
        self.opener.open.assert_not_called()
        (self.root / self.filename).write_bytes(b"x" * len(self.body))
        self.opener.open.return_value = io.BytesIO(self.body)
        artifacts.download_locked_artifacts(self.plan, self.root)
        self.opener.open.assert_called_once()
        self.assertEqual((self.root / self.filename).read_bytes(), self.body)

    def test_unselected_artifacts_and_unsafe_paths_fail_before_download(self):
        changed = {**self.artifact, "bytes": 1}
        with self.assertRaises(ValueError):
            artifacts.download_locked_artifacts(self.plan, self.root, artifacts=[changed])
        unsafe = {**self.artifact, "filename": "../outside.whl"}
        with self.assertRaises(ValueError):
            artifacts.download_locked_artifacts({"artifacts": [unsafe]}, self.root)
        self.opener.open.assert_not_called()

    def test_redirects_to_github_or_plain_http_are_rejected_before_request(self):
        handler = artifacts._RuntimeRedirectHandler()
        request = Request(artifacts.artifact_download_urls(self.artifact)[0])
        for url in ("http://cdn.invalid/wheel", "https://github.com/owner/wheel",
                    "https://release-assets.githubusercontent.com/wheel", "https://github.com./wheel"):
            with self.subTest(url=url), self.assertRaises(URLError):
                handler.redirect_request(request, None, 302, "Found", {}, url)
        redirected = handler.redirect_request(request, None, 302, "Found", {},
                                              "https://cdn-lfs.hf.co/file?signature=token")
        self.assertEqual(redirected.full_url, "https://cdn-lfs.hf.co/file?signature=token")


class RuntimeArtifactProxyTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(worker_proxy, "_configured_proxy", None))

    def test_download_transport_preserves_the_configured_proxy_policy(self):
        body = b"locked runtime wheel"
        filename = "fixture-1.0-py3-none-any.whl"
        artifact = {"name": "fixture", "filename": filename,
                    "url": artifacts._RUNTIME_MIRRORS[0][1] + "/wheels/" + filename,
                    "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
        cases = (
            ({"mode": "custom", "url": "socks5://custom-socks.invalid:1080"}, "www.modelscope.cn"),
            ({"mode": "custom", "url": "socks5h://custom-socks.invalid:1080"}, "www.modelscope.cn"),
            ({"mode": "none"}, "www.modelscope.cn"),
            ({"mode": "custom", "url": "http://custom-http.invalid:8081"}, "custom-http.invalid:8081"),
            ({"mode": "system"}, "system-proxy.invalid:8080"),
        )
        for settings, expected in cases:
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as directory:
                targets = []
                def https_open(_handler, request):
                    targets.append(request.host)
                    response = io.BytesIO(body)
                    response.code, response.msg = 200, "OK"
                    response.info = lambda: {}
                    return response
                def discovered_proxies():
                    return urllib.request.getproxies_environment() or {"https": "http://system-proxy.invalid:8080"}
                with mock.patch.dict(os.environ, {}, clear=True), \
                     mock.patch.object(urllib.request, "_opener", None), \
                     mock.patch.object(urllib.request, "getproxies", side_effect=discovered_proxies), \
                     mock.patch.object(urllib.request.HTTPSHandler, "https_open", https_open), \
                     mock.patch.object(worker_proxy, "_route_all_sockets_through_socks"):
                    # Exercise the environment-only configuration too: configuring
                    # SOCKS clears its HTTP_PROXY input before the download starts.
                    os.environ["PYMSS_STUDIO_PROXY_MODE"] = settings["mode"]
                    if settings.get("url"):
                        os.environ["HTTP_PROXY"] = settings["url"]
                    config = worker_proxy.load_proxy_config()
                    worker_proxy.configure_process_proxy(config)
                    artifacts.download_locked_artifacts({"artifacts": [artifact]}, directory)
                    self.assertEqual(targets, [expected])
                    self.assertEqual((Path(directory) / filename).read_bytes(), body)
