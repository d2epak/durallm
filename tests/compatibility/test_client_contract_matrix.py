"""Recorded Claude Code, OpenCode, Hermes Agent, and OpenClaw proxy contracts.

These are HTTP integration tests, not direct IR unit tests.  The fixture bodies
are captured wire contracts with stable protocol references; real client binary
tests remain opt-in because those clients are not CI dependencies.
"""

from __future__ import annotations

import copy
import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

from durallm.agent.context import estimate_tokens
from durallm.breaker.circuit_breaker import CircuitBreakerConfig
from durallm.breaker.registry import CircuitBreakerRegistry
from durallm.capability.registry import CapabilityRegistry
from durallm.execution.executor import GatewayExecutor
from durallm.execution.policy import ExecutionPolicy, FallbackPolicy, RetryPolicy
from durallm.gateway import ProxyGateway
from durallm.pools import IsolatedPoolManager, RouteDefinition
from durallm.providers.adapters import ProviderAdapterRegistry
from durallm.proxy import start_proxy_server
from tests.faults.mock_provider import MockFaultAction, ProgrammableMockAdapter

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "compatibility" / "client_contracts.v1.json"


def load_contracts() -> List[Dict[str, Any]]:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    if payload["schema_version"] != "lcb-client-contracts/v1":
        raise AssertionError("unsupported client-contract fixture schema")
    return payload["contracts"]


def route(route_id: str, provider: str, api_format: str, context_length: int) -> RouteDefinition:
    return RouteDefinition(
        id=route_id,
        provider=provider,
        model=f"{provider}-model",
        pool="general_agent",
        base_url=f"mock://{provider}",
        api_format=api_format,
        env_key="MOCK_KEY",
        context_length=context_length,
    )


class TestClientContractMatrix(unittest.TestCase):
    """Exercise the public HTTP surface against each recorded agent contract."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = start_proxy_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.contracts = load_contracts()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self) -> None:
        pools = IsolatedPoolManager()
        pools.keys = {"MOCK_KEY": "contract-key"}
        # The fallback deliberately declares Gemini. The recorded client can
        # still keep its original OpenAI/Anthropic envelope because the proxy
        # normalizes at the HTTP boundary before emitting the response.
        primary = route("primary", "provider_a", "openai", 1_000_000)
        fallback = route("fallback", "provider_b", "gemini", 32_768)
        pools.coding_routes = [primary, fallback]
        pools.agent_routes = [primary, fallback]
        self.primary = ProgrammableMockAdapter("provider_a")
        self.fallback = ProgrammableMockAdapter("provider_b", context_window=32_768)
        adapters = ProviderAdapterRegistry()
        adapters.register("provider_a", self.primary)
        adapters.register("provider_b", self.fallback)
        executor = GatewayExecutor(
            capability_registry=CapabilityRegistry(),
            breaker_registry=CircuitBreakerRegistry(default_config=CircuitBreakerConfig(minimum_number_of_calls=100)),
            adapter_registry=adapters,
            policy=ExecutionPolicy(
                retry=RetryPolicy(max_attempts_same_endpoint=1, jitter=False),
                fallback=FallbackPolicy(max_fallback_hops=2),
                max_total_attempts=4,
            ),
            sleeper=lambda _: None,
        )
        self.gateway = ProxyGateway(pool_manager=pools, executor=executor)
        self.gateway_patch = patch("durallm.proxy.GATEWAY", self.gateway)
        self.gateway_patch.start()

    def tearDown(self) -> None:
        self.gateway_patch.stop()

    def post(
        self,
        path: str,
        body: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Any, bytes]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    @staticmethod
    def _tool_command(contract: Dict[str, Any], response: Dict[str, Any]) -> str:
        if contract["protocol"] == "anthropic":
            tool = next(item for item in response["content"] if item["type"] == "tool_use")
            return tool["input"]["command"]
        tool = response["choices"][0]["message"]["tool_calls"][0]
        return json.loads(tool["function"]["arguments"])["command"]

    @staticmethod
    def _content(contract: Dict[str, Any], response: Dict[str, Any]) -> str:
        if contract["protocol"] == "anthropic":
            return "".join(item.get("text", "") for item in response["content"] if item["type"] == "text")
        return response["choices"][0]["message"]["content"]

    def test_recorded_tool_contracts_round_trip_over_http(self) -> None:
        """Each client envelope reaches the common tool-validation/IR pipeline."""
        for contract in self.contracts:
            with self.subTest(client=contract["client"]):
                primary_before = len(self.primary.call_history)
                self.primary.set_sequence([MockFaultAction.valid_tool_call("bash", {"command": "pytest -q"})])
                status, _, raw = self.post(contract["path"], copy.deepcopy(contract["body"]))
                self.assertEqual(status, 200, raw.decode("utf-8", errors="replace"))
                response = json.loads(raw)
                self.assertEqual(self._tool_command(contract, response), "pytest -q")
                prepared = self.primary.call_history[primary_before]
                self.assertEqual(prepared.headers["Authorization"], "Bearer contract-key")

    def test_rate_limit_fails_over_without_changing_the_client_envelope(self) -> None:
        """A 429 on the first route is a clean, protocol-preserving continuation."""
        for contract in self.contracts:
            with self.subTest(client=contract["client"]):
                primary_before = len(self.primary.call_history)
                fallback_before = len(self.fallback.call_history)
                self.primary.set_sequence([MockFaultAction.rate_limit(retry_after=1)])
                self.fallback.set_sequence([MockFaultAction.success(f"{contract['client']} resumed")])
                status, _, raw = self.post(contract["path"], copy.deepcopy(contract["body"]))
                self.assertEqual(status, 200, raw.decode("utf-8", errors="replace"))
                response = json.loads(raw)
                self.assertEqual(self._content(contract, response), f"{contract['client']} resumed")
                self.assertEqual(len(self.primary.call_history), primary_before + 1)
                self.assertEqual(len(self.fallback.call_history), fallback_before + 1)

    def test_long_horizon_coding_contracts_preserve_root_and_final_instruction(self) -> None:
        """Claude Code and OpenCode compact cleanly before the Gemini-declared fallback."""
        for contract in self.contracts[:2]:
            with self.subTest(client=contract["client"]):
                body = copy.deepcopy(contract["body"])
                root = "ROOT_OBJECTIVE: deploy the payment service without downtime."
                final = "Run the focused test suite and report deployment status."
                history = "compiler-output " * 900
                if contract["protocol"] == "anthropic":
                    body["messages"] = [{"role": "user", "content": [{"type": "text", "text": root}]}]
                    body["messages"].extend(
                        {"role": "assistant", "content": [{"type": "text", "text": history}]}
                        for _ in range(20)
                    )
                    body["messages"].append({"role": "user", "content": [{"type": "text", "text": final}]})
                else:
                    body["messages"] = [
                        {"role": "system", "content": "You are a coding agent."},
                        {"role": "user", "content": root},
                    ]
                    body["messages"].extend({"role": "assistant", "content": history} for _ in range(20))
                    body["messages"].append({"role": "user", "content": final})

                self.primary.set_sequence([MockFaultAction.server_error(503)])
                self.fallback.set_sequence([MockFaultAction.success("fallback completed")])
                status, _, raw = self.post(contract["path"], body)
                self.assertEqual(status, 200, raw.decode("utf-8", errors="replace"))
                self.assertEqual(self._content(contract, json.loads(raw)), "fallback completed")
                delivered = self.fallback.request_history[-1]
                self.assertLessEqual(estimate_tokens(delivered), 32_768 - 128 - 2_048)
                self.assertIn(root, [message.content for message in delivered.messages])
                self.assertEqual(delivered.messages[-1].content, final)

    def test_default_atomic_streaming_remains_parseable_for_each_client_contract(self) -> None:
        """`stream: true` stays compatible with existing clients unless native mode is requested."""
        for contract in self.contracts:
            with self.subTest(client=contract["client"]):
                body = copy.deepcopy(contract["body"])
                body["stream"] = True
                self.primary.set_sequence([MockFaultAction.success(f"{contract['client']} stream")])
                status, headers, raw = self.post(contract["path"], body)
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("Content-Type"), "text/event-stream")
                if contract["protocol"] == "anthropic":
                    self.assertIn(b"event: message_start", raw)
                    self.assertIn(b"event: message_stop", raw)
                else:
                    self.assertIn(b'"chat.completion.chunk"', raw)
                    self.assertTrue(raw.endswith(b"data: [DONE]\n\n"))

    def test_acp_acknowledgement_guards_the_next_turn_for_every_contract(self) -> None:
        """Restartable client sessions cannot advance until the prior checkpoint is acknowledged."""
        for contract in self.contracts:
            with self.subTest(client=contract["client"]):
                body = copy.deepcopy(contract["body"])
                body.pop("tools")
                self.primary.set_sequence([MockFaultAction.success("first"), MockFaultAction.success("second")])
                status, headers, raw = self.post(
                    contract["path"],
                    body,
                    {"X-LCB-ACP-Version": "lcb-acp/1", "X-LCB-State-Digest": "a" * 64},
                )
                self.assertEqual(status, 200, raw.decode("utf-8", errors="replace"))
                self.assertEqual(headers["X-LCB-Continuation-Event"], "turn_completed")
                ack_status, _, ack_raw = self.post(
                    "/v1/continuations/ack",
                    {
                        "session_id": headers["X-LCB-Session-Id"],
                        "turn_id": headers["X-LCB-Turn-Id"],
                        "epoch": int(headers["X-LCB-Epoch"]),
                        "checkpoint_digest": headers["X-LCB-Checkpoint-Digest"],
                    },
                )
                self.assertEqual(ack_status, 200, ack_raw.decode("utf-8", errors="replace"))
                status, next_headers, raw = self.post(
                    contract["path"],
                    body,
                    {
                        "X-LCB-ACP-Version": "lcb-acp/1",
                        "X-LCB-Session-Id": headers["X-LCB-Session-Id"],
                        "X-LCB-Parent-Checkpoint-Digest": headers["X-LCB-Checkpoint-Digest"],
                        "X-LCB-State-Digest": "b" * 64,
                    },
                )
                self.assertEqual(status, 200, raw.decode("utf-8", errors="replace"))
                self.assertEqual(next_headers["X-LCB-Epoch"], "1")
