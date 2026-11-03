import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

import worker_network_diagnostics as network


class NetworkDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.config = mock.Mock(mode="custom")
        self.patches = [
            mock.patch.object(network, "load_proxy_config", return_value=self.config),
            mock.patch.object(network, "configure_process_proxy"),
            mock.patch.object(network, "emit"),
            mock.patch.object(network, "emit_error", return_value=1),
            mock.patch.object(network, "proxy_urlopen"),
        ]
        self.load, self.configure, self.emit, self.error, self.urlopen = [p.start() for p in self.patches]
        for patch in self.patches:
            self.addCleanup(patch.stop)
        self.response = self.urlopen.return_value.__enter__.return_value
        self.response.status = 200

    def test_requests_use_existing_source_rules_and_only_read_bounded_metadata(self):
        self.assertEqual(network.cmd_network_diagnostics({"modelSource": "hf-mirror", "locale": "zh-CN"}), 0)
        self.configure.assert_called_once_with(self.config)
        expected, index = network._resolve_pypi_mirror("auto", "zh-CN")
        self.assertEqual(self.urlopen.call_args_list, [
            mock.call(network._test_url_for_source("hf-mirror"), 15, self.config),
            mock.call(index.rstrip("/") + "/pymss/", 15, self.config),
        ])
        self.assertEqual(self.response.read.call_args_list, [mock.call(1024), mock.call(1024)])
        event, payload = self.emit.call_args.args
        self.assertEqual(event, "network_diagnostics")
        self.assertEqual(payload["checks"][1]["source"], expected)
        self.assertEqual(payload["proxyMode"], "custom")

    def test_one_failed_target_does_not_hide_the_other_target(self):
        # Keep the successful response explicit while the first request times out.
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        self.urlopen.side_effect = [URLError(TimeoutError("timed out")), response]
        self.assertEqual(network.cmd_network_diagnostics({}), 0)
        rows = self.emit.call_args.args[1]["checks"]
        self.assertEqual([row["status"] for row in rows], ["timeout", "ok"])

    def test_http_failure_retains_status_and_closes_the_response(self):
        body = mock.Mock()
        self.urlopen.side_effect = HTTPError("https://example.test", 403, "Forbidden", {}, body)
        result = network._check("models", "modelscope", "https://example.test", self.config)
        self.assertEqual(result["httpStatus"], 403)
        self.assertEqual(result["status"], "failed")
        body.close.assert_called_once()

    def test_proxy_credentials_are_not_emitted_on_failure(self):
        self.urlopen.side_effect = URLError("Cannot connect to http://alice:private-password@proxy.test:8888")
        self.assertEqual(network.cmd_network_diagnostics({}), 0)
        self.assertNotIn("private-password", str(self.emit.call_args))
        self.assertNotIn("alice", str(self.emit.call_args))

    def test_invalid_sources_are_rejected_before_io(self):
        for payload in [{"modelSource": "https://private.test"}, {"mirror": "https://private.test"}, {"locale": 1}]:
            self.assertEqual(network.cmd_network_diagnostics(payload), 1)
        self.load.assert_not_called()
        self.urlopen.assert_not_called()

    def test_proxy_configuration_errors_are_sanitized(self):
        self.load.side_effect = ValueError("Invalid proxy http://alice:private-password@proxy.test")
        self.assertEqual(network.cmd_network_diagnostics({}), 1)
        self.assertNotIn("private-password", str(self.error.call_args))
        self.urlopen.assert_not_called()
