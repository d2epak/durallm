"""HTTP-level tests for the gateway proxy.

These start the real ThreadingHTTPServer on an ephemeral port and exercise the
GET endpoints end-to-end. Regression guard for the `all_breakers()` AttributeError
that made /health, /healthz, /metrics and /admin/breakers crash at v0.2.0.
"""

import json
import threading
import unittest
import urllib.request

from llm_circuit_breaker.proxy import start_proxy_server


class TestProxyGetEndpoints(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = start_proxy_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_health_returns_200_with_breaker_states(self):
        for path in ("/health", "/healthz"):
            status, body = self._get(path)
            self.assertEqual(status, 200, path)
            self.assertEqual(body["status"], "healthy")
            self.assertIn("circuit_breakers", body)

    def test_metrics_returns_200(self):
        status, body = self._get("/metrics")
        self.assertEqual(status, 200)
        self.assertIn("circuit_breakers", body)
        self.assertIn("health_telemetry", body)

    def test_admin_breakers_returns_200(self):
        status, body = self._get("/admin/breakers")
        self.assertEqual(status, 200)
        self.assertIn("breakers", body)

    def test_unknown_path_returns_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/nope")
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
