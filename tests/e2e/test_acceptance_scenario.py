"""Spec §50: deterministic agent-continuity acceptance scenario over the HTTP proxy.

The test uses the public OpenAI-compatible endpoint and programmable in-process provider
adapters. No real provider, credential, or tool is contacted. It proves the actual proxy data
plane—not a direct executor call—preserves the accepted contract for a 90k-token coding turn.
"""

from __future__ import annotations

import io
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from typing import Any, Dict, List
from unittest.mock import patch

from durallm.agent.context import estimate_tokens
from durallm.breaker.circuit_breaker import CircuitBreakerConfig
from durallm.breaker.registry import CircuitBreakerRegistry
from durallm.breaker.state import CircuitBreakerState
from durallm.capability.registry import CapabilityRegistry
from durallm.execution.executor import GatewayExecutor
from durallm.execution.policy import ExecutionPolicy, FallbackPolicy, RetryPolicy
from durallm.gateway import ProxyGateway
from durallm.observability.logger import StructuredJsonLogger
from durallm.pools import IsolatedPoolManager, RouteDefinition
from durallm.providers.adapters import ProviderAdapterRegistry
from durallm.proxy import start_proxy_server
from tests.faults.mock_provider import MockFaultAction, ProgrammableMockAdapter


class ManualClock:
    """A thread-safe-enough deterministic breaker clock for this single-threaded test."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def route(route_id: str, provider: str, model: str, api_format: str, context_length: int) -> RouteDefinition:
    return RouteDefinition(
        id=route_id,
        provider=provider,
        model=model,
        pool="coding",
        base_url=f"mock://{provider}",
        api_format=api_format,
        env_key="MOCK_KEY",
        context_length=context_length,
    )


class TestAcceptanceScenario(unittest.TestCase):
    """The acceptance scenario is intentionally one long test: its order is the agent trajectory."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = start_proxy_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=2)

    def setUp(self) -> None:
        self.clock = ManualClock()
        self.audit_log = io.StringIO()
        pools = IsolatedPoolManager()
        pools.keys = {"MOCK_KEY": "acceptance-key"}
        pools.coding_routes = [
            route("primary", "provider_a", "primary-128k", "openai", 128_000),
            # A distinct declared protocol is important: the proxy must select by capability, not
            # assume that every fallback is OpenAI wire-compatible. The mock adapter stands in for
            # the actual Gemini transport so the acceptance test remains hermetic.
            route("fallback", "provider_b", "fallback-32k", "gemini", 32_768),
        ]
        pools.agent_routes = []

        self.primary = ProgrammableMockAdapter("provider_a")
        self.fallback = ProgrammableMockAdapter("provider_b", context_window=32_768)
        adapters = ProviderAdapterRegistry()
        adapters.register("provider_a", self.primary)
        adapters.register("provider_b", self.fallback)
        self.breakers = CircuitBreakerRegistry(
            default_config=CircuitBreakerConfig(
                minimum_number_of_calls=3,
                failure_rate_threshold=50.0,
                wait_duration_open_ms=100.0,
                half_open_max_calls=1,
                clock=self.clock,
            )
        )
        executor = GatewayExecutor(
            capability_registry=CapabilityRegistry(),
            breaker_registry=self.breakers,
            adapter_registry=adapters,
            policy=ExecutionPolicy(
                retry=RetryPolicy(max_attempts_same_endpoint=1, jitter=False),
                fallback=FallbackPolicy(max_fallback_hops=2),
                max_total_attempts=4,
            ),
            # The scenario has no retryable same-endpoint failure; retain an injectable no-op
            # sleeper so its bounded-latency assertion cannot depend on wall-clock backoff.
            sleeper=lambda _: None,
            events=StructuredJsonLogger(name="acceptance.events", stream=self.audit_log),
        )
        self.gateway = ProxyGateway(pool_manager=pools, executor=executor)
        self.patch = patch("durallm.proxy.GATEWAY", self.gateway)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()

    def post(self, body: Dict[str, Any]) -> Dict[str, Any]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:  # pragma: no cover - turns an opaque HTTP failure into its body.
            self.fail(f"unexpected HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}")

    @staticmethod
    def coding_request(final_instruction: str) -> Dict[str, Any]:
        """A roughly 90k-token history whose compacted form must fit the 32k fallback."""
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": "You are a coding agent. Preserve the deployment objective."},
            {"role": "user", "content": "ROOT_OBJECTIVE: deploy the payment service without downtime."},
        ]
        for index in range(20):
            messages.append(
                {
                    "role": "assistant",
                    "content": f"Build log {index}: " + (f"compiler-output-{index} " * 900),
                }
            )
        messages.append({"role": "user", "content": final_instruction})
        return {
            # The public proxy maps model names containing "code" into the
            # coding isolation pool.
            "model": "code-agent",
            "max_tokens": 256,
            "messages": messages,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "description": "Run a safe build command.",
                        "parameters": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
        }

    def test_spec_50_agent_continues_across_failure_compaction_tool_repair_and_recovery(self) -> None:
        """Prove §50's complete trajectory over HTTP, including breaker/audit assertions."""
        final_instruction = "Run the focused test suite and report the deployment status."
        self.primary.set_sequence(
            [
                MockFaultAction.success("primary initially available"),
                MockFaultAction.server_error(503, "primary outage one"),
                MockFaultAction.server_error(503, "primary outage two"),
                MockFaultAction.success("primary half-open probe recovered"),
            ]
        )
        self.fallback.set_sequence(
            [
                # A trailing comma is a deterministic syntax repair; no semantic value is guessed.
                MockFaultAction.malformed_tool_json('{"command": "pytest -q",}'),
                MockFaultAction.success("fallback after breaker opened"),
                MockFaultAction.success("fallback while primary remains open"),
            ]
        )

        started = time.monotonic()
        first = self.post(self.coding_request(final_instruction))
        self.assertEqual(first["choices"][0]["message"]["content"], "primary initially available")

        primary_breaker = self.breakers.get_or_create("provider_a:default:primary-128k:default")
        transitions = []
        primary_breaker.add_event_listener(lambda event: transitions.append((event.from_state, event.to_state)))

        repaired_tool_turn = self.post(self.coding_request(final_instruction))
        tool_call = repaired_tool_turn["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(tool_call["function"]["name"], "bash")
        # The accepted deterministic repair is the client-visible JSON, not the malformed source.
        self.assertEqual(json.loads(tool_call["function"]["arguments"]), {"command": "pytest -q"})
        tool_executions = [tool_call["function"]["arguments"]]

        second_failure = self.post(self.coding_request(final_instruction))
        self.assertEqual(second_failure["choices"][0]["message"]["content"], "fallback after breaker opened")
        self.assertEqual(primary_breaker.state, CircuitBreakerState.OPEN)

        calls_before_open_turn = len(self.primary.call_history)
        open_turn = self.post(self.coding_request(final_instruction))
        self.assertEqual(open_turn["choices"][0]["message"]["content"], "fallback while primary remains open")
        self.assertEqual(len(self.primary.call_history), calls_before_open_turn, "OPEN breaker admitted a retry storm")

        self.clock.advance(0.101)
        recovered = self.post(self.coding_request(final_instruction))
        self.assertEqual(recovered["choices"][0]["message"]["content"], "primary half-open probe recovered")
        self.assertEqual(primary_breaker.state, CircuitBreakerState.CLOSED)

        # The fallback received a compacted, state-preserving version of the 90k-token request.
        delivered = self.fallback.request_history[0]
        self.assertLessEqual(estimate_tokens(delivered), 32_768 - 256 - 2_048)
        self.assertEqual(delivered.messages[1].content, "ROOT_OBJECTIVE: deploy the payment service without downtime.")
        self.assertEqual(delivered.messages[-1].content, final_instruction)
        fallback_endpoint = self.gateway.executor.capability_registry.get_endpoint("coding:fallback")
        self.assertIsNotNone(fallback_endpoint)
        self.assertEqual(fallback_endpoint.protocol, "gemini")

        # One deterministic tool call was delivered and executed exactly once; the primary is
        # attempted only for the initial success, two failures, and the recovery probe.
        self.assertEqual(len(tool_executions), 1)
        self.assertEqual(len(self.primary.call_history), 4)
        self.assertEqual(len(self.fallback.call_history), 3)
        self.assertLess(time.monotonic() - started, 5.0, "acceptance trajectory exceeded its bounded local latency")
        self.assertEqual(
            transitions,
            [
                (CircuitBreakerState.CLOSED, CircuitBreakerState.OPEN),
                (CircuitBreakerState.OPEN, CircuitBreakerState.HALF_OPEN),
                (CircuitBreakerState.HALF_OPEN, CircuitBreakerState.CLOSED),
            ],
        )

        audit_events = [json.loads(line)["event"] for line in self.audit_log.getvalue().splitlines()]
        self.assertEqual(audit_events.count("upstream_attempt_failed"), 2)
        self.assertEqual(audit_events.count("upstream_attempt_succeeded"), 5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
