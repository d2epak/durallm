"""Router admission: pools are isolation boundaries and breakers are keyed per deployment."""

import unittest

from llm_circuit_breaker.breaker.registry import CircuitBreakerRegistry
from llm_circuit_breaker.capability.profile import Endpoint, ModelProfile
from llm_circuit_breaker.capability.registry import CapabilityRegistry
from llm_circuit_breaker.errors import NoHealthyRouteError
from llm_circuit_breaker.execution.policy import RetryPolicy
from llm_circuit_breaker.health.telemetry import HealthTelemetryStore
from llm_circuit_breaker.routing.requirements import RequirementVector
from llm_circuit_breaker.routing.router import CapabilityRouter
from tests.faults.test_executor_backoff import build_executor, make_request


def make_router(*endpoints):
    cap_reg = CapabilityRegistry()
    for ep in endpoints:
        cap_reg.register_endpoint(ep)
    breaker_reg = CircuitBreakerRegistry()
    router = CapabilityRouter(capability_registry=cap_reg, breaker_registry=breaker_reg, health_store=HealthTelemetryStore())
    return router, breaker_reg


def endpoint(ep_id, deployment=None, pool="coding"):
    return Endpoint(
        id=ep_id, provider="azure", model="gpt-4o", base_url=f"mock://{ep_id}", deployment=deployment, pool=pool,
        profile=ModelProfile("azure", "gpt-4o", context_window=128000, supports_tools=True),
    )


class TestPoolIsolation(unittest.TestCase):

    def test_empty_pool_yields_no_candidate(self):
        router, _ = make_router(endpoint("ep-1", pool="coding"))

        ep, decision = router.select_candidate(RequirementVector(), pool="research")

        self.assertIsNone(ep)
        self.assertEqual(decision.total_considered, 0)

    def test_executor_raises_for_empty_pool_even_when_other_pools_are_healthy(self):
        ex, mock_a, _, _ = build_executor(RetryPolicy(max_attempts_same_endpoint=1))
        with self.assertRaises(NoHealthyRouteError):
            ex.execute(make_request(), pool="research", strategy="priority")
        self.assertEqual(mock_a.call_history, [])


class TestBreakerIdentity(unittest.TestCase):

    def test_two_deployments_of_one_model_have_independent_breakers(self):
        eu, us = endpoint("ep-eu", deployment="eu"), endpoint("ep-us", deployment="us")
        router, breaker_reg = make_router(eu, us)

        router.select_candidate(RequirementVector(), pool="coding")
        self.assertEqual(set(breaker_reg.all()), {eu.resource_key, us.resource_key})

        breaker_reg.get_or_create(eu.resource_key).force_open()
        ep, decision = router.select_candidate(RequirementVector(), pool="coding")
        self.assertEqual(ep.id, "ep-us")
        evals = {c.endpoint_id: c for c in decision.evaluated_candidates}
        self.assertFalse(evals["ep-eu"].eligible)
        self.assertTrue(evals["ep-us"].eligible)

    def test_disabled_breaker_does_not_exclude_endpoint(self):
        only = endpoint("ep-1")
        router, breaker_reg = make_router(only)
        breaker_reg.get_or_create(only.resource_key).disable()

        ep, decision = router.select_candidate(RequirementVector(), pool="coding")

        self.assertEqual(ep.id, "ep-1")
        self.assertEqual(decision.total_eligible, 1)


if __name__ == "__main__":
    unittest.main()
