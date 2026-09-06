"""The HTTP proxy is served by GatewayExecutor (V3), not the V1 dispatch loop.

Runs the real ThreadingHTTPServer against mock adapters; no upstream is ever contacted.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from llm_circuit_breaker.breaker.circuit_breaker import CircuitBreakerConfig
from llm_circuit_breaker.breaker.registry import CircuitBreakerRegistry
from llm_circuit_breaker.capability.registry import CapabilityRegistry
from llm_circuit_breaker.execution.executor import GatewayExecutor
from llm_circuit_breaker.execution.policy import ExecutionPolicy, FallbackPolicy, RetryPolicy
from llm_circuit_breaker.gateway import ProxyGateway
from llm_circuit_breaker.pools import IsolatedPoolManager, RouteDefinition
from llm_circuit_breaker.providers.adapters import ProviderAdapterRegistry
from llm_circuit_breaker.proxy import start_proxy_server
from tests.faults.mock_provider import MockFaultAction, ProgrammableMockAdapter


def make_route(rid, provider, pool, env_key="MOCK_KEY"):
    return RouteDefinition(id=rid, provider=provider, model=f"{provider}-model", pool=pool,
                           base_url=f"mock://{provider}", api_format="openai", env_key=env_key, context_length=65536)


def build_gateway():
    pm = IsolatedPoolManager()
    pm.keys = {"MOCK_KEY": "test-key"}
    pm.coding_routes = [make_route("a", "provider_a", "coding"), make_route("b", "provider_b", "coding")]
    pm.agent_routes = [make_route("a", "provider_a", "general_agent"), make_route("b", "provider_b", "general_agent"),
                       make_route("no-key", "provider_c", "general_agent", env_key="MISSING_KEY")]
    adapters = ProviderAdapterRegistry()
    mock_a, mock_b = ProgrammableMockAdapter("provider_a"), ProgrammableMockAdapter("provider_b")
    adapters._adapters.update({"provider_a": mock_a, "provider_b": mock_b})
    executor = GatewayExecutor(
        capability_registry=CapabilityRegistry(),
        breaker_registry=CircuitBreakerRegistry(default_config=CircuitBreakerConfig(minimum_number_of_calls=100)),
        adapter_registry=adapters,
        policy=ExecutionPolicy(retry=RetryPolicy(max_attempts_same_endpoint=1, jitter=False),
                               fallback=FallbackPolicy(max_fallback_hops=3), max_total_attempts=6),
        sleeper=lambda s: None,
    )
    return ProxyGateway(pool_manager=pm, executor=executor), mock_a, mock_b


class TestProxyServedByExecutor(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = start_proxy_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.gateway, self.mock_a, self.mock_b = build_gateway()
        self._patch = patch("llm_circuit_breaker.proxy.GATEWAY", self.gateway)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def _post(self, path, body, headers=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def test_openai_request_fails_over_through_the_executor(self):
        self.mock_a.set_sequence([MockFaultAction.server_error(503)])
        self.mock_b.set_sequence([MockFaultAction.success("via b")])

        status, _, raw = self._post("/v1/chat/completions", {
            "model": "hermes-default", "messages": [{"role": "user", "content": "hi"}]})

        body = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(body["choices"][0]["message"]["content"], "via b")
        self.assertEqual(body["model"], "hermes-default")
        self.assertEqual((len(self.mock_a.call_history), len(self.mock_b.call_history)), (1, 1))
        self.assertEqual(self.mock_a.call_history[0].headers.get("Authorization"), "Bearer test-key")

    def test_anthropic_request_is_decoded_to_ir_and_encoded_back(self):
        self.mock_a.set_sequence([MockFaultAction.success("Hello from the executor")])

        status, _, raw = self._post("/v1/messages", {
            "model": "claude-sonnet-4-6", "max_tokens": 64, "system": "be brief",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]})

        body = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(body["type"], "message")
        self.assertEqual(body["content"], [{"type": "text", "text": "Hello from the executor"}])
        self.assertEqual(body["stop_reason"], "end_turn")
        self.assertEqual(body["model"], "claude-sonnet-4-6")
        self.assertEqual(len(self.mock_a.call_history), 1)

    def test_anthropic_streaming_uses_the_executor_response(self):
        self.mock_a.set_sequence([MockFaultAction.success("streamed text")])

        status, headers, raw = self._post("/v1/messages", {
            "model": "claude-sonnet-4-6", "max_tokens": 64, "stream": True,
            "messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "text/event-stream")
        text = raw.decode()
        self.assertIn("streamed text", text)
        self.assertIn("event: message_stop", text)

    def test_exhausted_pool_maps_to_503_in_each_protocol(self):
        self.mock_a.set_sequence([MockFaultAction.server_error(503)] * 2)
        self.mock_b.set_sequence([MockFaultAction.server_error(503)] * 2)

        status, _, raw = self._post("/v1/messages", {"model": "x", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw)["error"]["type"], "no_healthy_route")

        status, _, raw = self._post("/v1/chat/completions", {"model": "hermes-default",
                                                            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw)["error"]["type"], "no_healthy_route")

    def test_routes_are_mirrored_per_pool_and_keyless_routes_are_skipped(self):
        self.mock_a.set_sequence([MockFaultAction.success("ok")])
        self._post("/v1/chat/completions", {"model": "hermes-default", "messages": [{"role": "user", "content": "hi"}]})

        registry = self.gateway.executor.capability_registry
        self.assertEqual(sorted(e.id for e in registry.endpoints_for_pool("coding")), ["coding:a", "coding:b"])
        self.assertEqual(sorted(e.id for e in registry.endpoints_for_pool("general_agent")),
                         ["general_agent:a", "general_agent:b"])
        self.assertEqual(registry.endpoints_for_pool("coding")[0].profile.context_window, 65536)

    def test_routes_added_after_startup_are_picked_up(self):
        # `llm-proxy --discover` and add_discovered_route append routes while the server runs.
        self.mock_a.set_sequence([MockFaultAction.success("ok")])
        self._post("/v1/chat/completions", {"model": "hermes-default", "messages": [{"role": "user", "content": "hi"}]})
        self.gateway.pool_manager.add_discovered_route("general_agent", make_route("late", "provider_b", "general_agent"))
        self.mock_b.set_sequence([MockFaultAction.success("ok")])
        self._post("/v1/chat/completions", {"model": "hermes-default", "messages": [{"role": "user", "content": "hi"}]})

        ids = {e.id for e in self.gateway.executor.capability_registry.endpoints_for_pool("general_agent")}
        self.assertIn("general_agent:late", ids)

    def test_acp_http_sequence_requires_a_checkpoint_acknowledgement(self):
        self.mock_a.set_sequence([MockFaultAction.success("first turn"), MockFaultAction.success("second turn")])
        first_state = "a" * 64
        status, headers, raw = self._post(
            "/v1/chat/completions",
            {"model": "hermes-default", "messages": [{"role": "user", "content": "first"}]},
            headers={"X-LCB-ACP-Version": "lcb-acp/1", "X-LCB-State-Digest": first_state},
        )

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "first turn")
        self.assertEqual(headers["X-LCB-ACP-Version"], "lcb-acp/1")
        self.assertEqual(headers["X-LCB-Continuation-Event"], "turn_completed")
        self.assertEqual(headers["X-LCB-Epoch"], "0")
        self.assertEqual(headers["X-LCB-Ack-Required"], "true")

        session_id = headers["X-LCB-Session-Id"]
        turn_id = headers["X-LCB-Turn-Id"]
        checkpoint = headers["X-LCB-Checkpoint-Digest"]
        status, ack_headers, raw = self._post(
            "/v1/continuations/ack",
            {"session_id": session_id, "turn_id": turn_id, "epoch": 0, "checkpoint_digest": checkpoint},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["continuation"]["event_type"], "checkpoint_acknowledged")
        self.assertEqual(ack_headers["X-LCB-Ack-Required"], "false")

        status, headers, _ = self._post(
            "/v1/chat/completions",
            {"model": "hermes-default", "messages": [{"role": "user", "content": "second"}]},
            headers={
                "X-LCB-ACP-Version": "lcb-acp/1",
                "X-LCB-Session-Id": session_id,
                "X-LCB-Parent-Checkpoint-Digest": checkpoint,
                "X-LCB-State-Digest": "b" * 64,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-LCB-Epoch"], "1")

        status, _, raw = self._post(
            "/v1/chat/completions",
            {"model": "hermes-default", "messages": [{"role": "user", "content": "must not run"}]},
            headers={
                "X-LCB-ACP-Version": "lcb-acp/1",
                "X-LCB-Session-Id": session_id,
                "X-LCB-Parent-Checkpoint-Digest": headers["X-LCB-Checkpoint-Digest"],
                "X-LCB-State-Digest": "c" * 64,
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(raw)["error"]["type"], "continuation_protocol_error")
        self.assertEqual(len(self.mock_a.call_history), 2)

    def test_acp_data_without_its_version_is_rejected_before_dispatch(self):
        status, _, raw = self._post(
            "/v1/chat/completions",
            {"model": "hermes-default", "messages": [{"role": "user", "content": "no ACP version"}]},
            headers={"X-LCB-State-Digest": "a" * 64},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw)["error"]["type"], "continuation_protocol_error")
        self.assertEqual(len(self.mock_a.call_history), 0)


if __name__ == "__main__":
    unittest.main()
