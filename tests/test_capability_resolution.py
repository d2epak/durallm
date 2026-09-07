"""Profile resolution is exact-match or explicit alias; unknown models are pessimistic."""

import unittest

from durallm.breaker.registry import CircuitBreakerRegistry
from durallm.capability.profile import Endpoint
from durallm.capability.registry import CapabilityRegistry
from durallm.health.telemetry import HealthTelemetryStore
from durallm.routing.requirements import RequirementVector
from durallm.routing.router import CapabilityRouter


class TestProfileResolution(unittest.TestCase):

    def setUp(self):
        self.reg = CapabilityRegistry()

    def test_exact_match_never_resolves_to_a_longer_name(self):
        self.assertEqual(self.reg.get_profile("openai", "gpt-4o").model, "gpt-4o")
        self.assertEqual(self.reg.get_profile("openai", "gpt-4o-mini").model, "gpt-4o-mini")
        # "gpt-4" is a substring of both registered names; it must not borrow either profile.
        self.assertIsNone(self.reg.get_profile("openai", "gpt-4").supports_tools)

    def test_builtin_and_custom_aliases(self):
        self.assertEqual(self.reg.get_profile("groq", "llama-3.3-70b").model, "llama-3.3-70b-versatile")
        self.reg.register_alias("openai", "gpt-4o-latest", "gpt-4o")
        self.assertEqual(self.reg.get_profile("openai", "GPT-4O-LATEST").model, "gpt-4o")

    def test_alias_does_not_cross_providers(self):
        self.assertIsNone(self.reg.get_profile("openai", "llama-3.3-70b").supports_tools)

    def test_unknown_model_is_excluded_when_tools_required(self):
        unknown = self.reg.get_profile("acme", "mystery-model")
        self.assertIsNone(unknown.supports_tools)
        passed, reason = RequirementVector(require_tools=True).matches_hard_constraints(unknown)
        self.assertFalse(passed)
        self.assertIn("unknown model", reason)
        self.assertTrue(RequirementVector().matches_hard_constraints(unknown)[0])


class TestRouterWithUnknownModel(unittest.TestCase):

    def test_unknown_model_endpoint_not_chosen_for_tool_requests(self):
        reg = CapabilityRegistry()
        reg.register_endpoint(Endpoint(id="ep-unknown", provider="acme", model="mystery", base_url="mock://x", priority=1, pool="coding"))
        reg.register_endpoint(Endpoint(id="ep-known", provider="openai", model="gpt-4o", base_url="mock://y", priority=2, pool="coding"))
        router = CapabilityRouter(capability_registry=reg, breaker_registry=CircuitBreakerRegistry(), health_store=HealthTelemetryStore())

        ep, decision = router.select_candidate(RequirementVector(require_tools=True), pool="coding", strategy="priority")

        self.assertEqual(ep.id, "ep-known")
        evals = {c.endpoint_id: c for c in decision.evaluated_candidates}
        self.assertFalse(evals["ep-unknown"].hard_constraints_passed)
        # Without a tool requirement the unknown, higher-priority endpoint is still usable.
        ep, _ = router.select_candidate(RequirementVector(), pool="coding", strategy="priority")
        self.assertEqual(ep.id, "ep-unknown")


if __name__ == "__main__":
    unittest.main()
