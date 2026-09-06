"""Tool validation outcomes must reach the health telemetry the router scores with."""

import unittest

from llm_circuit_breaker.agent.idempotency import ToolExecutionLedger
from llm_circuit_breaker.breaker.circuit_breaker import CircuitBreakerConfig
from llm_circuit_breaker.breaker.registry import CircuitBreakerRegistry
from llm_circuit_breaker.capability.profile import Endpoint, ModelProfile
from llm_circuit_breaker.capability.registry import CapabilityRegistry
from llm_circuit_breaker.execution.executor import GatewayExecutor
from llm_circuit_breaker.execution.policy import ExecutionPolicy, FallbackPolicy, RetryPolicy
from llm_circuit_breaker.health.telemetry import HealthTelemetryStore
from llm_circuit_breaker.protocol.ir import NormalizedMessage, NormalizedRequest, NormalizedToolDefinition
from llm_circuit_breaker.providers.adapters import ProviderAdapterRegistry
from tests.faults.mock_provider import MockFaultAction, ProgrammableMockAdapter

BASH = NormalizedToolDefinition(
    name="bash", description="run", parameters={
        "type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"],
    },
)


def build():
    cap_reg = CapabilityRegistry()
    adapters = ProviderAdapterRegistry()
    mocks = {}
    for ep_id, prov, prio in (("ep-a", "provider_a", 1), ("ep-b", "provider_b", 2)):
        mocks[prov] = ProgrammableMockAdapter(prov)
        adapters.register(prov, mocks[prov])
        cap_reg.register_endpoint(Endpoint(
            id=ep_id, provider=prov, model="m", base_url=f"mock://{prov}", priority=prio, pool="coding",
            profile=ModelProfile(prov, "m", supports_tools=True),
        ))
    health = HealthTelemetryStore()
    executor = GatewayExecutor(
        capability_registry=cap_reg,
        breaker_registry=CircuitBreakerRegistry(default_config=CircuitBreakerConfig(minimum_number_of_calls=100)),
        adapter_registry=adapters,
        health_store=health,
        tool_ledger=ToolExecutionLedger(),
        policy=ExecutionPolicy(retry=RetryPolicy(max_attempts_same_endpoint=1, jitter=False), fallback=FallbackPolicy(max_fallback_hops=3)),
        sleeper=lambda s: None,
    )
    return executor, mocks, health


def tool_request(text):
    return NormalizedRequest(model="default", messages=[NormalizedMessage(role="user", content=text)], tools=[BASH])


class TestToolOutcomeTelemetry(unittest.TestCase):

    def test_router_scores_with_the_executors_health_store(self):
        executor, _, health = build()
        self.assertIs(executor.router.health_store, health)

    def test_validation_outcome_is_recorded_per_endpoint(self):
        executor, mocks, health = build()
        mocks["provider_a"].set_sequence([MockFaultAction.valid_tool_call("bash", {"unknown_arg": 1})])
        mocks["provider_b"].set_sequence([MockFaultAction.valid_tool_call("bash", {"command": "ls"})])

        executor.execute(tool_request("go"), pool="coding", strategy="priority")

        a, b = health.get_or_create("ep-a"), health.get_or_create("ep-b")
        self.assertEqual((a.tool_calls_attempted, a.tool_calls_failed), (1, 1))
        self.assertEqual((b.tool_calls_attempted, b.tool_calls_succeeded), (1, 1))

    def test_reliability_aware_routing_skips_the_endpoint_with_observed_tool_failures(self):
        executor, mocks, _ = build()
        mocks["provider_a"].set_sequence([MockFaultAction.valid_tool_call("bash", {"unknown_arg": 1})] * 2)
        mocks["provider_b"].set_sequence([MockFaultAction.valid_tool_call("bash", {"command": "ls"})] * 2)

        executor.execute(tool_request("first"), pool="coding", strategy="reliability_aware")
        _, _, ledger = executor.execute(tool_request("second"), pool="coding", strategy="reliability_aware")

        self.assertEqual([a.endpoint_id for a in ledger.attempts], ["ep-b"])
        self.assertEqual(len(mocks["provider_a"].call_history), 1)


if __name__ == "__main__":
    unittest.main()
