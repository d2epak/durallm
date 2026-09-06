"""HTTP-level tests for the gateway proxy.

These start the real ThreadingHTTPServer on an ephemeral port and exercise the
GET endpoints end-to-end. Regression guard for the `all_breakers()` AttributeError
that made /health, /healthz, /metrics and /admin/breakers crash at v0.2.0.
"""

import json
import socket
import threading
import unittest
import urllib.request

from llm_circuit_breaker.proxy import start_proxy_server


class TestProxyHttpEndpoints(unittest.TestCase):

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

    def test_every_response_emits_a_structured_event(self):
        with self.assertLogs("llm_circuit_breaker.events", level="INFO") as cm:
            self._get("/health")
        events = [json.loads(r.getMessage()) for r in cm.records]
        hit = [e for e in events if e["event"] == "proxy_response" and e["data"]["path"] == "/health"]
        self.assertEqual(len(hit), 1)
        self.assertEqual((hit[0]["data"]["method"], hit[0]["data"]["status"]), ("GET", 200))
        self.assertIsInstance(hit[0]["data"]["duration_ms"], float)

    def test_unknown_path_returns_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_malformed_content_length_returns_400(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/messages",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        # urllib sets Content-Length itself; override with a non-integer value.
        req.add_unredirected_header("Content-Length", "abc")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)
        body = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertIn("Content-Length", body["error"]["message"])

    def test_oversized_content_length_is_rejected_with_413_before_body_is_read(self):
        # Only headers are sent: a 413 proves the server refused without waiting for 10 MB of body.
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(
                b"POST /v1/messages HTTP/1.0\r\nHost: x\r\nContent-Type: application/json\r\n"
                b"Content-Length: 10000001\r\n\r\n"
            )
            sock.settimeout(5)
            status_line = sock.recv(64).split(b"\r\n", 1)[0]
        self.assertIn(b" 413 ", status_line)


if __name__ == "__main__":
    unittest.main()
