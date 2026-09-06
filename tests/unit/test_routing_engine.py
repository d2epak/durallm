"""Tests for Routing Engine, Hard Constraints, and Strategy Scoring."""

import unittest

from llm_circuit_breaker.breaker.registry import CircuitBreakerRegistry
from llm_circuit_breaker.capability.profile import Endpoint, ModelProfile
from llm_circuit_breaker.capability.registry import CapabilityRegistry
from llm_circuit_breaker.routing import (
    CapabilityRouter,
    RequirementVector,
)


class TestRoutingEngine(unittest.TestCase):

    def setUp(self):
        self.cap_reg = CapabilityRegistry()
        self.breaker_reg = CircuitBreakerRegistry()

        # Register custom test endpoints
        self.ep1 = Endpoint(
            id="ep-groq",
            provider="groq",
            model="llama-3.3-70b",
            base_url="https://api.groq.com",
            priority=1,
            pool="coding",
            profile=ModelProfile("groq", "llama-3.3-70b", context_window=131072, supports_tools=True, is_free=True),
        )
        self.ep2 = Endpoint(
            id="ep-cerebras",
            provider="cerebras",
            model="llama3.1-8b",
            base_url="https://api.cerebras.ai",
            priority=2,
            pool="coding",
            profile=ModelProfile("cerebras", "llama3.1-8b", context_window=32768, supports_tools=False, is_free=True),
        )
        self.ep3 = Endpoint(
            id="ep-gemini",
            provider="gemini",
            model="gemini-2.5-flash",
            base_url="https://generativelanguage.googleapis.com",
            priority=3,
            pool="coding",
            profile=ModelProfile("gemini", "gemini-2.5-flash", context_window=1048576, supports_tools=True, supports_vision=True, is_free=True),
        )

        self.cap_reg.register_endpoint(self.ep1)
        self.cap_reg.register_endpoint(self.ep2)
        self.cap_reg.register_endpoint(self.ep3)

        self.router = CapabilityRouter(
            capability_registry=self.cap_reg,
            breaker_registry=self.breaker_reg,
            default_strategy="balanced",
        )

    def test_hard_constraint_tool_requirement(self):
        # ep2 does NOT support tools, ep1 and ep3 do
        req = RequirementVector(require_tools=True)
        ep, dec = self.router.select_candidate(req, pool="coding", strategy="priority")

        self.assertIsNotNone(ep)
        self.assertEqual(ep.id, "ep-groq")
        # Check that ep-cerebras was marked ineligible
        evals = {c.endpoint_id: c for c in dec.evaluated_candidates}
        self.assertFalse(evals["ep-cerebras"].eligible)
        self.assertIn("does not support tool calling", evals["ep-cerebras"].exclusion_reason)

    def test_hard_constraint_context_minimum(self):
        # Request requiring 100k context tokens: ep2 (32k) disqualified, ep1 (131k) and ep3 (1M) pass
        req = RequirementVector(minimum_context_tokens=100000)
        ep, dec = self.router.select_candidate(req, pool="coding")

        self.assertIsNotNone(ep)
        evals = {c.endpoint_id: c for c in dec.evaluated_candidates}
        self.assertFalse(evals["ep-cerebras"].eligible)
        self.assertIn("below minimum required", evals["ep-cerebras"].exclusion_reason)

    def test_breaker_open_candidate_excluded(self):
        # Trip breaker for groq
        breaker = self.breaker_reg.get_or_create("groq:default:llama-3.3-70b:default")  # Endpoint.resource_key
        breaker.force_open()

        req = RequirementVector(require_tools=True)
        # With groq open and cerebras lacking tools, gemini must be selected
        ep, dec = self.router.select_candidate(req, pool="coding", strategy="priority")

        self.assertIsNotNone(ep)
        self.assertEqual(ep.id, "ep-gemini")
        evals = {c.endpoint_id: c for c in dec.evaluated_candidates}
        self.assertFalse(evals["ep-groq"].eligible)
        self.assertIn("Circuit breaker is FORCED_OPEN", evals["ep-groq"].exclusion_reason)

    def test_explainable_routing_decision_record(self):
        req = RequirementVector(require_tools=True)
        ep, dec = self.router.select_candidate(req, pool="coding", strategy="priority", request_id="req-12345")

        d = dec.to_dict()
        self.assertEqual(d["request_id"], "req-12345")
        self.assertEqual(d["total_considered"], 3)
        self.assertEqual(d["total_eligible"], 2)  # groq and gemini
        self.assertIn("groq/llama-3.3-70b", d["selected"])

    def test_observed_telemetry_affects_routing_and_cold_start_policy(self):
        # Record observed telemetry in health store
        self.router.health_store.record_success(self.ep1.id, latency_ms=45.0)
        self.router.health_store.record_success(self.ep3.id, latency_ms=2500.0)

        req = RequirementVector(require_tools=True)
        ep, dec = self.router.select_candidate(req, pool="coding", strategy="latency_aware")

        self.assertEqual(ep.id, self.ep1.id)
        evals = {c.endpoint_id: c for c in dec.evaluated_candidates}
        self.assertFalse(evals[self.ep1.id].is_cold_start)
        self.assertEqual(evals[self.ep1.id].observed_latency_ms, 45.0)
        self.assertGreater(evals[self.ep1.id].latency_score, evals[self.ep3.id].latency_score)

        # ep2 has no recorded calls -> must be marked is_cold_start
        self.assertTrue(evals[self.ep2.id].is_cold_start)


if __name__ == "__main__":
    unittest.main()
