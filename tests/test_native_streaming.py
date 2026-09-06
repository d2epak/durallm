"""Real-socket tests for bounded, provider-native streaming.

The upstream is intentionally a tiny local HTTP/1.1 server rather than an
adapter mock.  This exercises TCP connection, chunked SSE decoding, early
failover and the no-splice rule at the proxy boundary.
"""

from __future__ import annotations

import json
import os
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from llm_circuit_breaker.breaker.circuit_breaker import CircuitBreakerConfig
from llm_circuit_breaker.breaker.registry import CircuitBreakerRegistry
from llm_circuit_breaker.capability.registry import CapabilityRegistry
from llm_circuit_breaker.execution.executor import GatewayExecutor
from llm_circuit_breaker.execution.policy import ExecutionPolicy, FallbackPolicy, RetryPolicy
from llm_circuit_breaker.gateway import ProxyGateway
from llm_circuit_breaker.pools import IsolatedPoolManager, RouteDefinition
from llm_circuit_breaker.providers.adapters import OpenAICompatibleAdapter, ProviderAdapterRegistry
from llm_circuit_breaker.providers.base import (
    PreparedRequest,
    ProviderStreamError,
    RequestCancellation,
    TransportTimeouts,
)
from llm_circuit_breaker.proxy import start_proxy_server


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        self.server.requests.append(json.loads(self.rfile.read(content_length).decode("utf-8")))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        for chunk in self.server.chunks:
            self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
            self.wfile.write(chunk)
            self.wfile.write(b"\r\n")
            self.wfile.flush()
        if self.server.drop:
            # Do not write the terminal chunk. A compliant HTTP client must
            # report this as an incomplete body, not a successful completion.
            self.close_connection = True
            return
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()
        self.close_connection = True


class _UpstreamServer(ThreadingHTTPServer):
    def __init__(self, chunks, drop=False):
        super().__init__(("127.0.0.1", 0), _UpstreamHandler)
        self.chunks = chunks
        self.drop = drop
        self.requests = []
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server_address[1]}/v1"

    def close(self):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)


def _gateway(routes):
    pools = IsolatedPoolManager()
    pools.agent_routes = routes
    pools.coding_routes = routes
    return ProxyGateway(
        pool_manager=pools,
        executor=GatewayExecutor(
            capability_registry=CapabilityRegistry(),
            breaker_registry=CircuitBreakerRegistry(default_config=CircuitBreakerConfig(minimum_number_of_calls=100)),
            adapter_registry=ProviderAdapterRegistry(),
            policy=ExecutionPolicy(
                retry=RetryPolicy(max_attempts_same_endpoint=1, jitter=False),
                fallback=FallbackPolicy(max_fallback_hops=3),
                max_total_attempts=4,
            ),
            sleeper=lambda _: None,
        ),
    )


def _route(route_id, base_url):
    return RouteDefinition(
        id=route_id,
        provider="openai",
        model="gpt-test",
        pool="general_agent",
        base_url=base_url,
        api_format="openai",
        env_key=None,
        context_length=65536,
    )


class TestBaseHTTPAdapterNativeStreaming(unittest.TestCase):
    def setUp(self):
        self.upstream = _UpstreamServer([b"data: one\n\n", b"data: [DONE]\n\n"])
        self.env = patch.dict(os.environ, {"LLM_BREAKER_ALLOW_LOCAL_UPSTREAM": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.upstream.close()

    def test_raw_sse_is_unbuffered_and_stream_flag_is_sent(self):
        prepared = PreparedRequest(
            url=f"{self.upstream.base_url}/chat/completions",
            headers={"Content-Type": "application/json"},
            body_bytes=b'{"stream": true}',
        )
        result = OpenAICompatibleAdapter().open_stream(prepared, TransportTimeouts(total_timeout_ms=2_000))

        self.assertEqual(result.status_code, 200)
        self.assertIsNotNone(result.stream)
        self.assertEqual(b"".join(result.stream.iter_bytes()), b"data: one\n\ndata: [DONE]\n\n")
        self.assertTrue(self.upstream.requests[0]["stream"])

    def test_cancellation_closes_the_upstream_reader(self):
        # Opening is enough for this test: cancellation is synchronous and the
        # reader must fail instead of consuming a later provider event.
        cancellation = RequestCancellation()
        prepared = PreparedRequest(
            url=f"{self.upstream.base_url}/chat/completions",
            headers={"Content-Type": "application/json"},
            body_bytes=b"{}",
        )
        result = OpenAICompatibleAdapter().open_stream(prepared, TransportTimeouts(total_timeout_ms=2_000), cancellation)
        reader = result.stream.iter_bytes()
        self.assertEqual(next(reader), b"data: one\n\n")
        cancellation.cancel("test client closed")
        with self.assertRaises(ProviderStreamError) as caught:
            next(reader)
        self.assertEqual(caught.exception.phase, "cancelled")

    def test_incomplete_chunked_body_is_a_connection_error(self):
        self.upstream.drop = True
        prepared = PreparedRequest(
            url=f"{self.upstream.base_url}/chat/completions",
            headers={"Content-Type": "application/json"},
            body_bytes=b"{}",
        )
        result = OpenAICompatibleAdapter().open_stream(prepared, TransportTimeouts(total_timeout_ms=2_000))
        reader = result.stream.iter_bytes()
        self.assertEqual(next(reader), b"data: one\n\n")
        with self.assertRaises(ProviderStreamError) as caught:
            list(reader)
        self.assertEqual(caught.exception.phase, "connection")


class TestNativeProxyNoSplice(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proxy = start_proxy_server("127.0.0.1", 0)
        cls.port = cls.proxy.server_address[1]
        cls.thread = threading.Thread(target=cls.proxy.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.proxy.server_close()
        cls.thread.join(timeout=2)

    def _post(self, body):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-LCB-Streaming-Mode": "true_streaming"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()

    def test_pre_visible_drop_fails_over_to_a_clean_new_stream(self):
        broken = _UpstreamServer([], drop=True)
        healthy = _UpstreamServer([b"data: fallback\n\n", b"data: [DONE]\n\n"])
        gateway = _gateway([_route("broken", broken.base_url), _route("healthy", healthy.base_url)])
        try:
            with patch.dict(os.environ, {"LLM_BREAKER_ALLOW_LOCAL_UPSTREAM": "1"}), patch(
                "llm_circuit_breaker.proxy.GATEWAY", gateway
            ):
                status, headers, raw = self._post({
                    "model": "hermes-default",
                    "stream": True,
                    "messages": [{"role": "user", "content": "hello"}],
                })
            self.assertEqual(status, 200)
            self.assertEqual(headers["X-LCB-Selected-Endpoint"], "general_agent:healthy")
            self.assertIn(b"fallback", raw)
            self.assertNotIn(b"lcb.interrupted", raw)
            self.assertEqual((len(broken.requests), len(healthy.requests)), (1, 1))
        finally:
            broken.close()
            healthy.close()

    def test_visible_drop_emits_interruption_and_never_calls_fallback(self):
        broken = _UpstreamServer([b"data: partial\n\n"], drop=True)
        healthy = _UpstreamServer([b"data: should-not-appear\n\n", b"data: [DONE]\n\n"])
        gateway = _gateway([_route("broken", broken.base_url), _route("healthy", healthy.base_url)])
        try:
            with patch.dict(os.environ, {"LLM_BREAKER_ALLOW_LOCAL_UPSTREAM": "1"}), patch(
                "llm_circuit_breaker.proxy.GATEWAY", gateway
            ):
                status, _, raw = self._post({
                    "model": "hermes-default",
                    "stream": True,
                    "messages": [{"role": "user", "content": "hello"}],
                })
            self.assertEqual(status, 200)
            self.assertIn(b"data: partial", raw)
            self.assertIn(b"event: lcb.interrupted", raw)
            self.assertIn(b"continuation_required", raw)
            self.assertNotIn(b"should-not-appear", raw)
            self.assertEqual((len(broken.requests), len(healthy.requests)), (1, 0))
        finally:
            broken.close()
            healthy.close()
