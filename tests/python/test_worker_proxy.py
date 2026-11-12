import json
import os
import subprocess
import sys
import unittest
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap

from worker_connection import _test_url_for_source
from worker_proxy import ProxyConfigError, configure_process_proxy, effective_proxy_url, parse_proxy_config, redacted_proxy


class ProxyConfigTests(unittest.TestCase):
    def test_http_proxy_with_credentials_is_redacted(self):
        config = parse_proxy_config({"mode": "custom", "url": "https://user:secret@example.com:8443"})
        self.assertEqual(redacted_proxy(config), "https://example.com:8443")
        self.assertEqual(effective_proxy_url(config), "https://user:secret@example.com:8443")

    def test_socks5_proxy_passes_through_for_pymss(self):
        # pymss 2.1.1+ routes SOCKS itself (PySocks via pymss[proxy]) and decides
        # the downloader (aria2c cannot do SOCKS); the worker no longer translates
        # proxy settings into aria2c arguments.
        config = parse_proxy_config({"mode": "custom", "url": "socks5h://127.0.0.1:1080"})
        self.assertEqual(effective_proxy_url(config), "socks5h://127.0.0.1:1080")

    def test_bypass_accepts_common_separators(self):
        config = parse_proxy_config({"mode": "custom", "url": "127.0.0.1:7890", "bypass": "localhost; 127.0.0.1\n*.local"})
        self.assertEqual(config.bypass, ("localhost", "127.0.0.1", "*.local"))

    def test_invalid_proxy_url_has_stable_error_code(self):
        with self.assertRaises(ProxyConfigError) as raised:
            parse_proxy_config({"mode": "custom", "url": "ftp://127.0.0.1:21"})
        self.assertEqual(raised.exception.code, "UNSUPPORTED_PROXY_SCHEME")

    def test_connection_url_follows_download_source(self):
        self.assertIn("modelscope.cn", _test_url_for_source("modelscope"))
        self.assertIn("huggingface.co", _test_url_for_source("huggingface"))
        self.assertIn("hf-mirror.com", _test_url_for_source("hf-mirror"))

    def test_connection_command_runs_without_site_packages(self):
        worker = _worker_test_bootstrap.WORKER_DIR / "worker.py"
        payload = json.dumps({
            "mode": "custom",
            "url": "http://127.0.0.1:1",
            "timeout": 1,
        })
        result = subprocess.run(
            [sys.executable, "-S", str(worker), "test_connection", "--payload", payload],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        event = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(event["type"], "test_connection_result")
        self.assertFalse(event["payload"]["ok"])

    def test_connection_command_has_socks_transport_dependencies(self):
        worker = _worker_test_bootstrap.WORKER_DIR / "worker.py"
        payload = json.dumps({
            "mode": "custom",
            "url": "socks5://127.0.0.1:1",
            "timeout": 1,
        })
        result = subprocess.run(
            [sys.executable, str(worker), "test_connection", "--payload", payload],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        event = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(event["type"], "test_connection_result")
        self.assertFalse(event["payload"]["ok"])
        error = event["payload"].get("error", "")
        self.assertNotIn("No module named", error)
        self.assertNotIn("Missing dependencies for SOCKS support", error)


class SocksRoutingTests(unittest.TestCase):
    """Downloading is delegated to pymss, which calls urllib.request.urlopen directly. urllib
    rejects a socks5:// proxy value outright, so a SOCKS proxy has to reach it some other way."""

    def setUp(self):
        self.enterContext(mock.patch("worker_proxy._configured_proxy", None))
        import socket
        # Both are replaced, so both have to be put back or the rest of the suite inherits a
        # process still pointed at a proxy that does not exist.
        self._socket = socket.socket
        self._create_connection = socket.create_connection
        self._env = {k: os.environ.get(k) for k in
                     ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy")}
        self.addCleanup(self._restore)

    def _restore(self):
        import socket
        socket.socket = self._socket
        socket.create_connection = self._create_connection
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_a_socks_proxy_replaces_the_socket_layer(self):
        import socket
        try:
            import socks
        except ImportError:
            self.skipTest("PySocks is not installed for this interpreter")
        configure_process_proxy(parse_proxy_config(
            {"mode": "custom", "url": "socks5://127.0.0.1:1080", "bypass": ""}))
        # A subclass rather than socksocket itself: it adds the bypass check on top.
        self.assertTrue(issubclass(socket.socket, socks.socksocket))
        self.assertIsNot(socket.create_connection, self._create_connection)

    def test_socks_clears_the_proxy_variables_urllib_would_choke_on(self):
        try:
            import socks  # noqa: F401
        except ImportError:
            self.skipTest("PySocks is not installed for this interpreter")
        os.environ["HTTP_PROXY"] = "http://stale:1"
        configure_process_proxy(parse_proxy_config(
            {"mode": "custom", "url": "socks5://127.0.0.1:1080", "bypass": ""}))
        # Leaving socks5:// in these makes urllib fail with "unknown url type" before the
        # socket layer ever gets a chance.
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertIsNone(os.environ.get(name), name)

    def test_an_http_proxy_still_travels_by_environment(self):
        configure_process_proxy(parse_proxy_config(
            {"mode": "custom", "url": "http://127.0.0.1:7890", "bypass": ""}))
        self.assertEqual(os.environ.get("HTTP_PROXY"), "http://127.0.0.1:7890")
        self.assertEqual(os.environ.get("https_proxy"), "http://127.0.0.1:7890")


class BypassRoutingTests(unittest.TestCase):
    """A bypass list is written in host names, but the socket layer only ever sees addresses."""

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("NO_PROXY", "no_proxy")}
        self.addCleanup(self._restore)

    def _restore(self):
        from worker_proxy import _connect_target
        _connect_target.host = None
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_bypass_matching_follows_no_proxy_semantics(self):
        from worker_proxy import _host_is_bypassed

        os.environ["no_proxy"] = "localhost,.example.com,modelscope.cn"

        self.assertTrue(_host_is_bypassed("localhost"))
        self.assertTrue(_host_is_bypassed("x.example.com"))
        # A bare entry covers its subdomains, which is how NO_PROXY has always read.
        self.assertTrue(_host_is_bypassed("www.modelscope.cn"))
        self.assertFalse(_host_is_bypassed("huggingface.co"))
        self.assertFalse(_host_is_bypassed(""))

    def test_create_connection_carries_the_hostname_to_the_socket(self):
        # create_connection resolves the name to an address before building a socket, so without
        # this the socket only ever sees an IP and no bypass entry could match.
        from worker_proxy import _bypass_aware_create_connection, _connect_target

        seen = []

        def original(address, *_args, **_kwargs):
            seen.append(getattr(_connect_target, "host", None))
            return "socket"

        wrapped = _bypass_aware_create_connection(original)

        self.assertEqual(wrapped(("example.com", 443)), "socket")
        self.assertEqual(seen, ["example.com"])
        # It must not outlive the call, or the next connection inherits the wrong name.
        self.assertIsNone(getattr(_connect_target, "host", None))

    def test_a_bypassed_host_skips_the_proxy(self):
        try:
            import socks
        except ImportError:
            self.skipTest("PySocks is not installed for this interpreter")
        from worker_proxy import _bypass_aware_socket_class, _connect_target

        os.environ["no_proxy"] = "internal.test"
        socket_class = _bypass_aware_socket_class(socks)
        instance = socket_class.__new__(socket_class)  # no real socket needed
        _connect_target.host = "internal.test"

        with mock.patch.object(socks._BaseSocket, "connect") as direct, \
             mock.patch.object(socks.socksocket, "connect") as through_proxy:
            socket_class.connect(instance, ("10.0.0.5", 443))

        direct.assert_called_once()
        through_proxy.assert_not_called()

    def test_other_hosts_still_go_through_the_proxy(self):
        try:
            import socks
        except ImportError:
            self.skipTest("PySocks is not installed for this interpreter")
        from worker_proxy import _bypass_aware_socket_class, _connect_target

        os.environ["no_proxy"] = "internal.test"
        socket_class = _bypass_aware_socket_class(socks)
        instance = socket_class.__new__(socket_class)
        _connect_target.host = "www.modelscope.cn"

        with mock.patch.object(socks._BaseSocket, "connect") as direct, \
             mock.patch.object(socks.socksocket, "connect") as through_proxy:
            socket_class.connect(instance, ("39.1.2.3", 443))

        through_proxy.assert_called_once()
        direct.assert_not_called()


if __name__ == "__main__":
    unittest.main()
