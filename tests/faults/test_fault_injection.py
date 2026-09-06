"""Deterministic Fault-Injection Tests for LLM Circuit Breaker Gateway."""

import unittest

from llm_circuit_breaker.breaker.circuit_breaker import CircuitBreakerConfig
from llm_circuit_breaker.breaker.registry import CircuitBreakerRegistry
from llm_circuit_breaker.capability.profile import Endpoint, ModelProfile
from llm_circuit_breaker.capability.registry import CapabilityRegistry
from llm_circuit_breaker.execution.executor import GatewayExecutor
from llm_circuit_breaker.execution.policy import ExecutionPolicy, FallbackPolicy, RetryPolicy
from llm_circuit_breaker.protocol.ir import (
    NormalizedMessage,
    NormalizedRequest,
    NormalizedToolDefinition,
)
from llm_circuit_breaker.providers.adapters import ProviderAdapterRegistry
from tests.faults.mock_provider import MockFaultAction, ProgrammableMockAdapter


class TestDeterministicFaultInjection(unittest.TestCase):

    def setUp(self):
        self.cap_reg = CapabilityRegistry()
        self.breaker_reg = CircuitBreakerRegistry(
            default_config=CircuitBreakerConfig(minimum_number_of_calls=2, failure_rate_threshold=50.0)
        )
        self.adapter_reg = ProviderAdapterRegistry()

        # Mock adapters for primary (provider_a) and fallback (provider_b)
        self.mock_a = ProgrammableMockAdapter("provider_a")
        self.mock_b = ProgrammableMockAdapter("provider_b")

        self.adapter_reg._adapters["provider_a"] = self.mock_a
        self.adapter_reg._adapters["provider_b"] = self.mock_b

        # Endpoints
        self.ep_a = Endpoint(
            id="ep-a",
            provider="provider_a",
            model="model-a",
            base_url="mock://a",
            priority=1,
            pool="coding",
            profile=ModelProfile("provider_a", "model-a", context_window=65536, supports_tools=True),
        )
        self.ep_b = Endpoint(
            id="ep-b",
            provider="provider_b",
            model="model-b",
            base_url="mock://b",
            priority=2,
            pool="coding",
            profile=ModelProfile("provider_b", "model-b", context_window=65536, supports_tools=True),
        )

        self.cap_reg.register_endpoint(self.ep_a)
        self.cap_reg.register_endpoint(self.ep_b)

        self.policy = ExecutionPolicy(
            retry=RetryPolicy(max_attempts_same_endpoint=1),
            fallback=FallbackPolicy(max_fallback_hops=3),
            max_total_attempts=4,
        )

        self.executor = GatewayExecutor(
            capability_registry=self.cap_reg,
            breaker_registry=self.breaker_reg,
            adapter_registry=self.adapter_reg,
            policy=self.policy,
        )

    def test_fault_503_primary_outage_fails_over_to_secondary(self):
        # Primary returns 503 Overloaded; Secondary returns 200 Success
        self.mock_a.set_sequence([MockFaultAction.server_error(503, "Service Overloaded")])
        self.mock_b.set_sequence([MockFaultAction.success("Secondary success output")])

        req = NormalizedRequest(
            model="default",
            messages=[NormalizedMessage(role="user", content="Hello")],
        )

        resp, decision, ledger = self.executor.execute(req, pool="coding", strategy="priority")

        self.assertEqual(resp.content, "Secondary success output")
        self.assertEqual(ledger.total_attempts, 2)
        self.assertEqual(ledger.fallback_count, 1)
        self.assertEqual(ledger.attempts[0].endpoint_id, "ep-a")
        self.assertEqual(ledger.attempts[0].status_code, 503)
        self.assertEqual(ledger.attempts[1].endpoint_id, "ep-b")
        self.assertEqual(ledger.attempts[1].status_code, 200)

        # Verify FailoverPlan was recorded
        self.assertEqual(len(ledger.failover_plans), 1)
        plan = ledger.failover_plans[0]
        self.assertEqual(plan.source_endpoint, "ep-a")
        self.assertEqual(plan.target_endpoint, "ep-b")
        self.assertEqual(plan.failover_reason, "overloaded")

    def test_fault_429_rate_limit_with_retry_after(self):
        # Primary returns 429 with Retry-After: 60; Secondary succeeds
        self.mock_a.set_sequence([MockFaultAction.rate_limit(retry_after=60)])
        self.mock_b.set_sequence([MockFaultAction.success("Recovered via provider B")])

        req = NormalizedRequest(
            model="default",
            messages=[NormalizedMessage(role="user", content="Run analysis")],
        )

        resp, decision, ledger = self.executor.execute(req, pool="coding", strategy="priority")

        self.assertEqual(resp.content, "Recovered via provider B")
        self.assertEqual(ledger.attempts[0].failure.reason.value, "rate_limit")

    def test_fault_malformed_tool_call_fails_over_safely(self):
        # Primary returns 200 with malformed tool call arguments; Secondary returns valid tool call
        self.mock_a.set_sequence([MockFaultAction.malformed_tool_call("bash")])
        self.mock_b.set_sequence([
            MockFaultAction.success(
                content="",
                tool_calls=[{"id": "tc_valid", "type": "function", "function": {"name": "bash", "arguments": '{"command": "ls"}'}}],
            )
        ])

        req = NormalizedRequest(
            model="default",
            messages=[NormalizedMessage(role="user", content="List files")],
            tools=[
                NormalizedToolDefinition(
                    name="bash",
                    description="Run bash",
                    parameters={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
                )
            ],
        )

        resp, decision, ledger = self.executor.execute(req, pool="coding", strategy="priority")

        # Must recover via provider B with valid tool call!
        self.assertEqual(len(resp.tool_calls), 1)
        self.assertEqual(resp.tool_calls[0].name, "bash")
        self.assertEqual(resp.tool_calls[0].arguments, {"command": "ls"})
        self.assertEqual(ledger.attempts[0].failure.reason.value, "malformed_tool_call")


if __name__ == "__main__":
    unittest.main()


class TestClientFaultIsolation(TestDeterministicFaultInjection):
    """A client-side 400 must never open the breaker for other callers (ADR 0003)."""

    def test_repeated_400_does_not_open_breaker_and_falls_back(self):
        self.mock_a.set_sequence([MockFaultAction.server_error(400, "unsupported parameter: tools")] * 10)
        self.mock_b.set_sequence([MockFaultAction.success("served by b")] * 10)
        req = NormalizedRequest(model="default", messages=[NormalizedMessage(role="user", content="x")])

        for _ in range(5):
            resp, _, ledger = self.executor.execute(req, pool="coding", strategy="priority")
            self.assertEqual(resp.content, "served by b")
            self.assertEqual(ledger.attempts[0].status_code, 400)
            self.assertFalse(ledger.attempts[0].failure.poisons_health)

        states = {k: b.snapshot()["state"] for k, b in self.breaker_reg.all().items()}
        self.assertTrue(states, "expected at least one breaker")
        self.assertTrue(all(s == "CLOSED" for s in states.values()), states)
