"""maximum_cost_usd and latency_budget_ms are enforced as hard constraints."""

import unittest

from llm_circuit_breaker.breaker.registry import CircuitBreakerRegistry
from llm_circuit_breaker.capability.profile import Endpoint, ModelProfile
from llm_circuit_breaker.capability.registry import CapabilityRegistry
from llm_circuit_breaker.execution.policy import RetryPolicy
from llm_circuit_breaker.health.telemetry import HealthTelemetryStore
from llm_circuit_breaker.routing.requirements import RequirementVector
from llm_circuit_breaker.routing.router import CapabilityRouter
from tests.faults.test_executor_backoff import build_executor, make_request


def profile(price_in, price_out=0.0):
    return ModelProfile("p", "m", context_window=128000, supports_tools=True,
                        input_price_per_1m=price_in, output_price_per_1m=price_out)


class TestCostCeiling(unittest.TestCase):

    def test_estimate_is_tokens_times_price(self):
        req = RequirementVector(estimated_input_tokens=100_000, expected_output_tokens=1_000)
        self.assertAlmostEqual(req.estimated_cost_usd(profile(15.0, 60.0)), 1.5 + 0.06)

    def test_expensive_profile_excluded_and_cheap_one_passes(self):
        req = RequirementVector(maximum_cost_usd=0.01, estimated_input_tokens=100_000)
        passed, reason = req.matches_hard_constraints(profile(15.0))
        self.assertFalse(passed)
        self.assertIn("Estimated cost", reason)
        self.assertTrue(req.matches_hard_constraints(profile(0.1))[0])

    def test_no_ceiling_means_no_cost_check(self):
        req = RequirementVector(estimated_input_tokens=10_000_000)
        self.assertTrue(req.matches_hard_constraints(profile(1000.0))[0])


class TestLatencyBudget(unittest.TestCase):

    def setUp(self):
        self.store = HealthTelemetryStore()
        self.req = RequirementVector(latency_budget_ms=500.0)

    def test_slow_endpoint_excluded_by_observed_ema(self):
        self.store.record_success("ep-slow", latency_ms=800.0)
        passed, reason = self.req.matches_hard_constraints(profile(0.0), health=self.store.get_or_create("ep-slow"))
        self.assertFalse(passed)
        self.assertIn("exceeds budget", reason)

    def test_fast_and_cold_start_endpoints_pass(self):
        self.store.record_success("ep-fast", latency_ms=300.0)
        self.assertTrue(self.req.matches_hard_constraints(profile(0.0), health=self.store.get_or_create("ep-fast"))[0])
        self.assertTrue(self.req.matches_hard_constraints(profile(0.0), health=self.store.get_or_create("ep-new"))[0])


class TestRouterAppliesBudgets(unittest.TestCase):

    def test_priority_routing_skips_candidate_over_cost_ceiling(self):
        cap_reg = CapabilityRegistry()
        for ep_id, prio, price in (("ep-pricey", 1, 15.0), ("ep-cheap", 2, 0.1)):
            cap_reg.register_endpoint(Endpoint(
                id=ep_id, provider="p", model=ep_id, base_url=f"mock://{ep_id}", priority=prio, pool="coding",
                profile=ModelProfile("p", ep_id, context_window=128000, supports_tools=True, input_price_per_1m=price),
            ))
        router = CapabilityRouter(capability_registry=cap_reg, breaker_registry=CircuitBreakerRegistry(),
                                  health_store=HealthTelemetryStore())
        req = RequirementVector(maximum_cost_usd=0.01, estimated_input_tokens=100_000)

        ep, decision = router.select_candidate(req, pool="coding", strategy="priority")

        self.assertEqual(ep.id, "ep-cheap")
        evals = {c.endpoint_id: c for c in decision.evaluated_candidates}
        self.assertFalse(evals["ep-pricey"].hard_constraints_passed)

    def test_executor_passes_caller_requirements_through(self):
        ex, mock_a, mock_b, _ = build_executor(RetryPolicy(max_attempts_same_endpoint=1))
        for ep in ex.capability_registry.endpoints_for_pool("coding"):
            ep.profile.input_price_per_1m = 1_000_000.0 if ep.id == "ep-a" else 0.0

        _, _, ledger = ex.execute(make_request(), pool="coding", strategy="priority",
                                  requirements=RequirementVector(maximum_cost_usd=0.001))

        self.assertEqual([a.endpoint_id for a in ledger.attempts], ["ep-b"])
        self.assertEqual(mock_a.call_history, [])


if __name__ == "__main__":
    unittest.main()
