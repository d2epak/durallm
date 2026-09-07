"""Unit tests for calibrated task selection, budget reservation, resource lanes, and shadow quality."""

import time
import unittest

from durallm.capability.profile import (
    CapabilityVerificationStatus,
    Endpoint,
    ModelProfile,
    PrivacyProfile,
)
from durallm.capability.registry import CapabilityRegistry
from durallm.execution.executor import GatewayExecutor
from durallm.protocol.ir import NormalizedMessage, NormalizedRequest
from durallm.providers.adapters import ProviderAdapterRegistry
from durallm.routing.budget import BudgetReservationStore
from durallm.routing.quality import ShadowQualityPolicy
from durallm.routing.requirements import RequirementVector
from durallm.routing.resources import ResourceLaneStore
from durallm.routing.router import CapabilityRouter
from durallm.routing.tokenizer import preflight_context
from tests.faults.mock_provider import MockFaultAction, ProgrammableMockAdapter


class TestBudgetReservationStore(unittest.TestCase):
    def setUp(self):
        self.store = BudgetReservationStore()

    def test_reserve_and_settle(self):
        res = self.store.reserve("res-1", "session-a", amount_usd=1.5, limit_usd=5.0)
        self.assertIsNotNone(res)
        self.assertEqual(res.amount_usd, 1.5)
        self.assertEqual(self.store.remaining("session-a", 5.0), 3.5)

        # Idempotent call
        res_same = self.store.reserve("res-1", "session-a", amount_usd=1.5, limit_usd=5.0)
        self.assertEqual(res, res_same)

        # Settle less than reserved
        self.store.settle("res-1", actual_cost_usd=1.0)
        self.assertEqual(self.store.remaining("session-a", 5.0), 4.0)

    def test_reserve_exceeds_limit(self):
        res1 = self.store.reserve("res-1", "scope-1", amount_usd=4.0, limit_usd=5.0)
        self.assertIsNotNone(res1)
        res2 = self.store.reserve("res-2", "scope-1", amount_usd=2.0, limit_usd=5.0)
        self.assertIsNone(res2)
        self.assertEqual(self.store.remaining("scope-1", 5.0), 1.0)

    def test_release_reservation(self):
        self.store.reserve("res-1", "scope-1", amount_usd=3.0, limit_usd=5.0)
        self.assertEqual(self.store.remaining("scope-1", 5.0), 2.0)
        self.store.release("res-1")
        self.assertEqual(self.store.remaining("scope-1", 5.0), 5.0)

    def test_invalid_amounts(self):
        with self.assertRaises(ValueError):
            self.store.reserve("res-bad", "scope", -1.0, 5.0)
        with self.assertRaises(ValueError):
            self.store.settle("res-bad", -0.5)

    def test_reset(self):
        self.store.reserve("res-1", "scope", 2.0, 5.0)
        self.store.reset()
        self.assertEqual(self.store.remaining("scope", 5.0), 5.0)


class TestResourceLaneStore(unittest.TestCase):
    def test_lane_availability_and_expiry(self):
        current_time = 1000.0
        store = ResourceLaneStore(clock=lambda: current_time)

        self.assertTrue(store.status("lane-a").available)

        store.set_unavailable("lane-a", reason="rate_limit", retry_after_seconds=30.0)
        stat = store.status("lane-a")
        self.assertFalse(stat.available)
        self.assertEqual(stat.reason, "rate_limit")
        self.assertEqual(stat.expires_at, 1030.0)

        # Advance clock past expiry
        current_time = 1035.0
        stat_after = store.status("lane-a")
        self.assertTrue(stat_after.available)

    def test_manual_set_available(self):
        store = ResourceLaneStore()
        store.set_unavailable("lane-b", reason="maintenance", retry_after_seconds=600)
        self.assertFalse(store.status("lane-b").available)
        store.set_available("lane-b")
        self.assertTrue(store.status("lane-b").available)

    def test_reset(self):
        store = ResourceLaneStore()
        store.set_unavailable("lane-c", reason="quota")
        store.reset()
        self.assertTrue(store.status("lane-c").available)


class TestShadowQualityPolicy(unittest.TestCase):
    def setUp(self):
        self.profile = ModelProfile(
            provider="groq",
            model="llama-3.3-70b",
            context_window=131072,
            expected_quality_score=0.88,
            quality_confidence=0.85,
            quality_provenance="benchmark_v1",
        )
        self.ep = Endpoint(id="ep-1", provider="groq", model="llama-3.3-70b", base_url="http://mock", profile=self.profile)

    def test_profile_fallback_estimate(self):
        policy = ShadowQualityPolicy(min_confidence=0.70)
        est = policy.estimate(self.ep, "coding")
        self.assertIsNotNone(est)
        self.assertEqual(est.score, 0.88)
        self.assertEqual(est.confidence, 0.85)

    def test_observation_overrides_profile(self):
        policy = ShadowQualityPolicy(min_confidence=0.70)
        policy.observe(self.ep, "coding", score=0.95, confidence=0.92, provenance="canary_eval")
        est = policy.estimate(self.ep, "coding")
        self.assertEqual(est.score, 0.95)
        self.assertEqual(est.provenance, "canary_eval")

    def test_recommendation_and_abstention(self):
        policy = ShadowQualityPolicy(min_confidence=0.80)
        rec = policy.recommend([self.ep], "coding")
        self.assertFalse(rec.abstained)
        self.assertEqual(rec.endpoint_id, "ep-1")
        self.assertEqual(rec.score, 0.88)

        # High confidence threshold causes abstention
        strict_policy = ShadowQualityPolicy(min_confidence=0.99)
        strict_rec = strict_policy.recommend([self.ep], "coding")
        self.assertTrue(strict_rec.abstained)
        self.assertIsNone(strict_rec.endpoint_id)

    def test_invalid_score_confidence(self):
        policy = ShadowQualityPolicy()
        with self.assertRaises(ValueError):
            policy.observe(self.ep, "coding", score=1.5, confidence=0.5, provenance="test")
        with self.assertRaises(ValueError):
            policy.observe(self.ep, "coding", score=0.5, confidence=-0.1, provenance="test")


class TestTokenizerPreflight(unittest.TestCase):
    def setUp(self):
        self.profile = ModelProfile(provider="mock", model="m", context_window=4096)

    def test_fits_without_compaction(self):
        res = preflight_context(self.profile, input_tokens=1000, expected_output_tokens=500, safety_margin_tokens=500, allow_compaction=True)
        self.assertTrue(res.fits_without_compaction)
        self.assertFalse(res.requires_compaction)
        self.assertTrue(res.compatible)

    def test_requires_compaction(self):
        res = preflight_context(self.profile, input_tokens=3500, expected_output_tokens=1000, safety_margin_tokens=500, allow_compaction=True)
        self.assertFalse(res.fits_without_compaction)
        self.assertTrue(res.requires_compaction)
        self.assertTrue(res.compatible)

        # Compaction disallowed
        res_no_compact = preflight_context(self.profile, input_tokens=3500, expected_output_tokens=1000, safety_margin_tokens=500, allow_compaction=False)
        self.assertFalse(res_no_compact.compatible)

    def test_minimum_required_exceeds_window(self):
        res = preflight_context(self.profile, input_tokens=100, expected_output_tokens=3000, safety_margin_tokens=2000, allow_compaction=True)
        self.assertFalse(res.compatible)
        self.assertIn("exceeds context window", res.reason)


class TestRequirementVectorHardConstraints(unittest.TestCase):
    def setUp(self):
        self.privacy = PrivacyProfile(
            data_retention="zero_retention",
            allows_external_traffic=False,
            compliance=["HIPAA", "SOC2"],
            region="us-east-1",
        )
        self.profile = ModelProfile(
            provider="aws",
            model="claude-3-5-sonnet",
            context_window=200000,
            privacy=self.privacy,
            verification_status=CapabilityVerificationStatus.VERIFIED,
            capability_expires_at=time.time() + 3600,
            expected_quality_score=0.92,
        )

    def test_privacy_and_region_filters(self):
        # Matching region
        req_ok = RequirementVector(required_region="us-east-1")
        passed, _ = req_ok.matches_hard_constraints(self.profile)
        self.assertTrue(passed)

        # Non-matching region
        req_bad = RequirementVector(required_region="eu-central-1")
        passed, reason = req_bad.matches_hard_constraints(self.profile)
        self.assertFalse(passed)
        self.assertIn("not declared in required region", reason)

    def test_compliance_filter(self):
        req = RequirementVector(required_compliance=["SOC2", "GDPR"])
        passed, reason = req.matches_hard_constraints(self.profile)
        self.assertFalse(passed)
        self.assertIn("lacks required compliance: GDPR", reason)

    def test_external_traffic_filter(self):
        external_profile = ModelProfile(
            provider="public", model="p", context_window=4096,
            privacy=PrivacyProfile(allows_external_traffic=True),
        )
        req = RequirementVector(allow_external_traffic=False)
        passed, reason = req.matches_hard_constraints(external_profile)
        self.assertFalse(passed)
        self.assertIn("permits external traffic", reason)

    def test_verified_capability_expiry(self):
        expired_profile = ModelProfile(
            provider="aws", model="claude", context_window=200000,
            verification_status=CapabilityVerificationStatus.VERIFIED,
            capability_expires_at=time.time() - 100,  # Expired
        )
        req = RequirementVector(require_verified_capabilities=True)
        passed, reason = req.matches_hard_constraints(expired_profile)
        self.assertFalse(passed)
        self.assertIn("lacks a current verified capability profile", reason)

    def test_quality_threshold_and_degradation_consent(self):
        # Below quality floor without consent
        req_strict = RequirementVector(minimum_quality_score=0.95, allow_quality_degradation=False)
        passed, reason = req_strict.matches_hard_constraints(self.profile)
        self.assertFalse(passed)
        self.assertIn("below required minimum", reason)

        # Below quality floor with consent
        req_consent = RequirementVector(minimum_quality_score=0.95, allow_quality_degradation=True)
        passed, _ = req_consent.matches_hard_constraints(self.profile)
        self.assertTrue(passed)


class TestCalibratedRoutingIntegration(unittest.TestCase):
    def test_resource_lane_exclusion_in_router(self):
        cap_reg = CapabilityRegistry()
        lane_store = ResourceLaneStore()
        router = CapabilityRouter(capability_registry=cap_reg, lane_store=lane_store)

        ep1 = Endpoint(
            id="ep-primary", provider="groq", model="llama-3.3-70b", base_url="http://mock",
            priority=1, pool="coding", resource_lane="groq-account-1",
            profile=ModelProfile("groq", "llama-3.3-70b", context_window=131072, supports_tools=True),
        )
        ep2 = Endpoint(
            id="ep-backup", provider="groq", model="llama-3.3-70b", base_url="http://mock",
            priority=2, pool="coding", resource_lane="groq-account-2",
            profile=ModelProfile("groq", "llama-3.3-70b", context_window=131072, supports_tools=True),
        )
        cap_reg.register_endpoint(ep1)
        cap_reg.register_endpoint(ep2)

        # When account 1 is available, priority selects ep1
        selected, dec = router.select_candidate(RequirementVector(), pool="coding", strategy="priority")
        self.assertEqual(selected.id, "ep-primary")

        # Mark account 1 unavailable due to quota
        lane_store.set_unavailable("groq-account-1", reason="quota_exhausted", retry_after_seconds=60)

        # Router excludes ep1 and selects ep2 without affecting the provider as a whole
        selected2, dec2 = router.select_candidate(RequirementVector(), pool="coding", strategy="priority")
        self.assertEqual(selected2.id, "ep-backup")
        excluded = next(c for c in dec2.evaluated_candidates if c.endpoint_id == "ep-primary")
        self.assertFalse(excluded.eligible)
        self.assertIn("Resource lane 'groq-account-1' unavailable", excluded.exclusion_reason)

    def test_executor_budget_reservation_enforcement(self):
        cap_reg = CapabilityRegistry()
        adapter_reg = ProviderAdapterRegistry()
        mock_adapter = ProgrammableMockAdapter("provider_a")
        mock_adapter.set_sequence([MockFaultAction.success("response content")])
        adapter_reg._adapters["provider_a"] = mock_adapter

        profile = ModelProfile(
            "provider_a", "model-a", context_window=65536, supports_tools=True,
            input_price_per_1m=10.0, output_price_per_1m=20.0,
        )
        cap_reg.register_endpoint(Endpoint(
            id="ep-a", provider="provider_a", model="model-a", base_url="mock://a", pool="coding",
            profile=profile,
        ))

        budget_store = BudgetReservationStore()
        executor = GatewayExecutor(
            capability_registry=cap_reg,
            adapter_registry=adapter_reg,
            budget_store=budget_store,
        )

        req = NormalizedRequest(request_id="req-budget-test", messages=[NormalizedMessage(role="user", content="hello")])
        requirements = RequirementVector(budget_limit_usd=1.0, budget_scope="team-budget")

        resp, dec, ledger = executor.execute(req, pool="coding", requirements=requirements)
        self.assertEqual(resp.content, "response content")
        # Cost settled
        self.assertLess(budget_store.remaining("team-budget", 1.0), 1.0)


if __name__ == "__main__":
    unittest.main()
