"""Executor retry semantics: backoff, Retry-After, and honouring the failure classification."""

import unittest
from unittest.mock import patch

from llm_circuit_breaker.breaker.circuit_breaker import CircuitBreakerConfig
from llm_circuit_breaker.breaker.registry import CircuitBreakerRegistry
from llm_circuit_breaker.capability.profile import Endpoint, ModelProfile
from llm_circuit_breaker.capability.registry import CapabilityRegistry
from llm_circuit_breaker.errors import NonRecoverableFailureError
from llm_circuit_breaker.execution.executor import GatewayExecutor
from llm_circuit_breaker.execution.policy import ExecutionPolicy, FallbackPolicy, RetryPolicy
from llm_circuit_breaker.models import FailureCategory, FailureClassification, FailoverReason
from llm_circuit_breaker.protocol.ir import NormalizedMessage, NormalizedRequest
from llm_circuit_breaker.providers.adapters import ProviderAdapterRegistry
from tests.faults.mock_provider import MockFaultAction, ProgrammableMockAdapter


def build_executor(retry: RetryPolicy):
    """Two-endpoint fixture (ep-a priority 1, ep-b priority 2) with a recording sleeper."""
    cap_reg = CapabilityRegistry()
    # High thresholds so the breaker never opens during these tests.
    breaker_reg = CircuitBreakerRegistry(
        default_config=CircuitBreakerConfig(minimum_number_of_calls=100, failure_rate_threshold=100.0)
    )
    adapter_reg = ProviderAdapterRegistry()
    mock_a = ProgrammableMockAdapter("provider_a")
    mock_b = ProgrammableMockAdapter("provider_b")
    adapter_reg._adapters["provider_a"] = mock_a
    adapter_reg._adapters["provider_b"] = mock_b
    for ep_id, prov, model, prio in (("ep-a", "provider_a", "model-a", 1), ("ep-b", "provider_b", "model-b", 2)):
        cap_reg.register_endpoint(Endpoint(
            id=ep_id, provider=prov, model=model, base_url=f"mock://{prov}", priority=prio, pool="coding",
            profile=ModelProfile(prov, model, context_window=65536, supports_tools=True),
        ))
    sleeps = []
    policy = ExecutionPolicy(retry=retry, fallback=FallbackPolicy(max_fallback_hops=3), max_total_attempts=6)
    executor = GatewayExecutor(
        capability_registry=cap_reg, breaker_registry=breaker_reg, adapter_registry=adapter_reg,
        policy=policy, sleeper=sleeps.append,
    )
    return executor, mock_a, mock_b, sleeps


def make_request():
    return NormalizedRequest(model="default", messages=[NormalizedMessage(role="user", content="hi")])


class TestExecutorBackoff(unittest.TestCase):

    def test_429_retry_after_is_waited_before_same_endpoint_retry(self):
        ex, mock_a, _, sleeps = build_executor(RetryPolicy(max_attempts_same_endpoint=2, jitter=False))
        mock_a.set_sequence([MockFaultAction.rate_limit(retry_after=30), MockFaultAction.success("after wait")])

        resp, _, ledger = ex.execute(make_request(), pool="coding", strategy="priority")

        self.assertEqual(resp.content, "after wait")
        self.assertEqual([a.endpoint_id for a in ledger.attempts], ["ep-a", "ep-a"])
        self.assertEqual(sleeps, [30.0])

    def test_5xx_retries_use_exponential_backoff(self):
        ex, mock_a, _, sleeps = build_executor(RetryPolicy(max_attempts_same_endpoint=3, base_backoff_ms=200.0, jitter=False))
        mock_a.set_sequence([
            MockFaultAction.server_error(500), MockFaultAction.server_error(500), MockFaultAction.success("third time"),
        ])

        resp, _, ledger = ex.execute(make_request(), pool="coding", strategy="priority")

        self.assertEqual(resp.content, "third time")
        self.assertEqual(ledger.total_attempts, 3)
        self.assertEqual(sleeps, [0.2, 0.4])

    def test_retry_after_beyond_deadline_falls_back_without_sleeping(self):
        ex, mock_a, mock_b, sleeps = build_executor(RetryPolicy(max_attempts_same_endpoint=2, jitter=False))
        mock_a.set_sequence([MockFaultAction.rate_limit(retry_after=60)])
        mock_b.set_sequence([MockFaultAction.success("via b")])

        resp, _, ledger = ex.execute(make_request(), pool="coding", strategy="priority", deadline_ms=5000)

        self.assertEqual(resp.content, "via b")
        self.assertEqual(sleeps, [])
        self.assertEqual([a.endpoint_id for a in ledger.attempts], ["ep-a", "ep-b"])
        self.assertEqual(ledger.fallback_count, 1)

    def test_fallback_to_other_endpoint_does_not_sleep(self):
        ex, mock_a, mock_b, sleeps = build_executor(RetryPolicy(max_attempts_same_endpoint=1, jitter=False))
        mock_a.set_sequence([MockFaultAction.server_error(503)])
        mock_b.set_sequence([MockFaultAction.success("via b")])

        resp, _, _ = ex.execute(make_request(), pool="coding", strategy="priority")

        self.assertEqual(resp.content, "via b")
        self.assertEqual(sleeps, [])


class TestExecutorHonoursClassification(unittest.TestCase):

    def test_non_retryable_failure_is_not_retried_on_same_endpoint(self):
        # 401 is retryable=False: even with budget for 3 attempts, ep-a gets exactly one.
        ex, mock_a, mock_b, sleeps = build_executor(RetryPolicy(max_attempts_same_endpoint=3, jitter=False))
        mock_a.set_sequence([MockFaultAction.server_error(401, "invalid api key")] * 3)
        mock_b.set_sequence([MockFaultAction.success("via b")])

        resp, _, ledger = ex.execute(make_request(), pool="coding", strategy="priority")

        self.assertEqual(resp.content, "via b")
        self.assertEqual([a.endpoint_id for a in ledger.attempts], ["ep-a", "ep-b"])
        self.assertEqual(len(mock_a.call_history), 1)
        self.assertEqual(sleeps, [])

    def test_failure_with_fallback_forbidden_raises_without_trying_other_endpoints(self):
        ex, mock_a, mock_b, _ = build_executor(RetryPolicy(max_attempts_same_endpoint=3, jitter=False))
        mock_a.set_sequence([MockFaultAction.server_error(400, "bad request")])
        mock_b.set_sequence([MockFaultAction.success("must not be reached")])
        forbidden = FailureClassification(
            category=FailureCategory.CLIENT_FAULT, reason=FailoverReason.client_error,
            should_fallback=False, retryable=False, poisons_health=False, status_code=400, message="bad request",
        )

        with patch("llm_circuit_breaker.execution.executor.classify_failure", return_value=forbidden):
            with self.assertRaises(NonRecoverableFailureError) as ctx:
                ex.execute(make_request(), pool="coding", strategy="priority")

        self.assertEqual(ctx.exception.endpoint_id, "ep-a")
        self.assertEqual(len(mock_a.call_history), 1)
        self.assertEqual(len(mock_b.call_history), 0)


if __name__ == "__main__":
    unittest.main()
