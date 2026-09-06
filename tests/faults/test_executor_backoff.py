"""Executor backoff tests: same-endpoint retries must wait, honouring Retry-After."""

import unittest

from llm_circuit_breaker.breaker.circuit_breaker import CircuitBreakerConfig
from llm_circuit_breaker.breaker.registry import CircuitBreakerRegistry
from llm_circuit_breaker.capability.profile import Endpoint, ModelProfile
from llm_circuit_breaker.capability.registry import CapabilityRegistry
from llm_circuit_breaker.execution.executor import GatewayExecutor
from llm_circuit_breaker.execution.policy import ExecutionPolicy, FallbackPolicy, RetryPolicy
from llm_circuit_breaker.protocol.ir import NormalizedMessage, NormalizedRequest
from llm_circuit_breaker.providers.adapters import ProviderAdapterRegistry
from tests.faults.mock_provider import MockFaultAction, ProgrammableMockAdapter


class TestExecutorBackoff(unittest.TestCase):

    def _build(self, retry: RetryPolicy):
        cap_reg = CapabilityRegistry()
        # High thresholds so the breaker never opens during these tests.
        breaker_reg = CircuitBreakerRegistry(
            default_config=CircuitBreakerConfig(minimum_number_of_calls=100, failure_rate_threshold=100.0)
        )
        adapter_reg = ProviderAdapterRegistry()
        self.mock_a = ProgrammableMockAdapter("provider_a")
        self.mock_b = ProgrammableMockAdapter("provider_b")
        adapter_reg._adapters["provider_a"] = self.mock_a
        adapter_reg._adapters["provider_b"] = self.mock_b
        for ep_id, prov, model, prio in (("ep-a", "provider_a", "model-a", 1), ("ep-b", "provider_b", "model-b", 2)):
            cap_reg.register_endpoint(Endpoint(
                id=ep_id, provider=prov, model=model, base_url=f"mock://{prov}", priority=prio, pool="coding",
                profile=ModelProfile(prov, model, context_window=65536, supports_tools=True),
            ))
        self.sleeps = []
        policy = ExecutionPolicy(retry=retry, fallback=FallbackPolicy(max_fallback_hops=3), max_total_attempts=6)
        return GatewayExecutor(
            capability_registry=cap_reg, breaker_registry=breaker_reg, adapter_registry=adapter_reg,
            policy=policy, sleeper=self.sleeps.append,
        )

    @staticmethod
    def _req():
        return NormalizedRequest(model="default", messages=[NormalizedMessage(role="user", content="hi")])

    def test_429_retry_after_is_waited_before_same_endpoint_retry(self):
        ex = self._build(RetryPolicy(max_attempts_same_endpoint=2, jitter=False))
        self.mock_a.set_sequence([MockFaultAction.rate_limit(retry_after=30), MockFaultAction.success("after wait")])

        resp, _, ledger = ex.execute(self._req(), pool="coding", strategy="priority")

        self.assertEqual(resp.content, "after wait")
        self.assertEqual([a.endpoint_id for a in ledger.attempts], ["ep-a", "ep-a"])
        self.assertEqual(self.sleeps, [30.0])

    def test_5xx_retries_use_exponential_backoff(self):
        ex = self._build(RetryPolicy(max_attempts_same_endpoint=3, base_backoff_ms=200.0, jitter=False))
        self.mock_a.set_sequence([
            MockFaultAction.server_error(500), MockFaultAction.server_error(500), MockFaultAction.success("third time"),
        ])

        resp, _, ledger = ex.execute(self._req(), pool="coding", strategy="priority")

        self.assertEqual(resp.content, "third time")
        self.assertEqual(ledger.total_attempts, 3)
        self.assertEqual(self.sleeps, [0.2, 0.4])

    def test_retry_after_beyond_deadline_falls_back_without_sleeping(self):
        ex = self._build(RetryPolicy(max_attempts_same_endpoint=2, jitter=False))
        self.mock_a.set_sequence([MockFaultAction.rate_limit(retry_after=60)])
        self.mock_b.set_sequence([MockFaultAction.success("via b")])

        resp, _, ledger = ex.execute(self._req(), pool="coding", strategy="priority", deadline_ms=5000)

        self.assertEqual(resp.content, "via b")
        self.assertEqual(self.sleeps, [])
        self.assertEqual([a.endpoint_id for a in ledger.attempts], ["ep-a", "ep-b"])
        self.assertEqual(ledger.fallback_count, 1)

    def test_fallback_to_other_endpoint_does_not_sleep(self):
        ex = self._build(RetryPolicy(max_attempts_same_endpoint=1, jitter=False))
        self.mock_a.set_sequence([MockFaultAction.server_error(503)])
        self.mock_b.set_sequence([MockFaultAction.success("via b")])

        resp, _, _ = ex.execute(self._req(), pool="coding", strategy="priority")

        self.assertEqual(resp.content, "via b")
        self.assertEqual(self.sleeps, [])


if __name__ == "__main__":
    unittest.main()
