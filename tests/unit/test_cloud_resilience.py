"""Unit tests for 5-Tier Cloud Routing, Cooldown Horizons, max_tokens Clamping, and 404 Deprecation."""

import os
import time
import unittest
from unittest.mock import patch

from durallm.capability.profile import Endpoint, ModelProfile
from durallm.capability.registry import CapabilityRegistry
from durallm.classifier import classify_api_error
from durallm.execution.executor import GatewayExecutor
from durallm.execution.policy import ExecutionPolicy, RetryPolicy
from durallm.gateway import ProxyGateway
from durallm.models import FailoverReason
from durallm.pools import (
    DEFAULT_CODING_ROUTES,
    IsolatedPoolManager,
    RouteDefinition,
    load_all_env_keys,
)
from durallm.protocol.ir import NormalizedMessage, NormalizedRequest
from durallm.providers.adapters import ProviderAdapterRegistry
from durallm.storage.sqlite import SQLitePersistenceStore
from tests.faults.mock_provider import MockFaultAction, ProgrammableMockAdapter


class TestCloudResilience(unittest.TestCase):

    def test_5_tier_cloud_topology_and_env_keys(self):
        """Verify 5-tier cloud-only topology and env key discovery."""
        providers = {r.provider.lower() for r in DEFAULT_CODING_ROUTES}
        self.assertIn("groq", providers)
        self.assertIn("sambanova", providers)
        self.assertIn("cerebras", providers)
        self.assertIn("nvidia", providers)
        self.assertIn("openrouter", providers)

        # Test env key discovery includes SambaNova and Cerebras
        with patch.dict(os.environ, {"SAMBANOVA_API_KEY": "sn-test-123", "CEREBRAS_API_KEY": "cb-test-456"}):
            keys = load_all_env_keys(scan_dotfiles=False)
            self.assertEqual(keys.get("SAMBANOVA_API_KEY"), "sn-test-123")
            self.assertEqual(keys.get("CEREBRAS_API_KEY"), "cb-test-456")

    def test_cooldown_horizons_auto_expiry(self):
        """Test Tier 1 (30s), Tier 2 (60s), and Tier 3 (24h) cooldown horizons and auto-expiry."""
        pm = IsolatedPoolManager()
        pm.coding_routes = [
            RouteDefinition(
                id="route-groq",
                provider="groq",
                model="qwen/qwen3.6-27b",
                pool="coding",
                base_url="https://api.groq.com",
                api_format="openai",
                env_key=None,
            ),
            RouteDefinition(
                id="route-sambanova",
                provider="sambanova",
                model="Qwen2.5-Coder-32B-Instruct",
                pool="coding",
                base_url="https://api.sambanova.ai",
                api_format="openai",
                env_key=None,
            ),
        ]

        # Initially both routes are active
        candidates = pm.get_candidate_routes("coding")
        self.assertEqual(len(candidates), 2)

        # Mark groq in cooldown (e.g. 30s transient probe)
        pm.mark_cooldown("coding", "groq", seconds=30.0)
        self.assertTrue(pm.is_provider_in_cooldown("coding", "groq"))

        candidates = pm.get_candidate_routes("coding")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].provider, "sambanova")

        # Expire cooldown artificially
        pm.cooldowns[("coding", "groq")] = time.monotonic() - 1.0
        self.assertFalse(pm.is_provider_in_cooldown("coding", "groq"))
        pm.auto_expire_cooldowns("coding")
        candidates = pm.get_candidate_routes("coding")
        self.assertEqual(len(candidates), 2)

        # Mark sambanova route quota exhausted (24h)
        pm.mark_quota_exhausted("coding", "route-sambanova", seconds=86400.0)
        self.assertTrue(pm.is_route_quota_exhausted("coding", "route-sambanova"))
        candidates = pm.get_candidate_routes("coding")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].id, "route-groq")

    def test_sqlite_persistence_quota_lockout(self, tmp_path=None):
        """Test Tier 3 (24h) quota lockout SQLite persistence and ProxyGateway restoration."""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "durallm_test.db"
            store = SQLitePersistenceStore(db_path)

            # Record a 24h quota lockout
            store.record_quota_lockout(
                provider_id="openrouter",
                pool="coding",
                route_id="openrouter-free-coding",
                expires_at=time.time() + 86400.0,
                reason="Free tier daily credit exhausted (HTTP 402)",
            )

            # Read back active lockouts
            lockouts = store.get_active_quota_lockouts()
            self.assertEqual(len(lockouts), 1)
            self.assertEqual(lockouts[0]["provider_id"], "openrouter")
            self.assertEqual(lockouts[0]["route_id"], "openrouter-free-coding")

            # Initialize a new ProxyGateway with this store to verify auto-restore
            pm = IsolatedPoolManager()
            pm.keys = {"OPENROUTER_API_KEY": "test-key"}
            gateway = ProxyGateway(pool_manager=pm, persistence_store=store)

            self.assertTrue(pm.is_route_quota_exhausted("coding", "openrouter-free-coding"))
            snap = gateway.executor.health_store.get_or_create("coding:openrouter-free-coding")
            self.assertTrue(snap.is_quota_exhausted)

    def test_max_tokens_clamping_prevents_upstream_400(self):
        """Test that excessive request.max_output_tokens is clamped to model profile limits."""
        cap_reg = CapabilityRegistry()
        adapter_reg = ProviderAdapterRegistry()
        mock_adapter = ProgrammableMockAdapter("groq")
        adapter_reg._adapters["groq"] = mock_adapter

        # Groq model profile with 950 max output tokens
        cap_reg.register_profile(
            ModelProfile(
                "groq",
                "qwen/qwen3.6-27b",
                context_window=7000,
                max_output_tokens=950,
                supports_tools=True,
            )
        )
        cap_reg.register_endpoint(
            Endpoint(
                id="groq:qwen36",
                provider="groq",
                model="qwen/qwen3.6-27b",
                base_url="https://api.groq.com",
                pool="coding",
                priority=1,
            )
        )

        mock_adapter.set_sequence([MockFaultAction.success("clamped completion")])
        executor = GatewayExecutor(
            capability_registry=cap_reg,
            adapter_registry=adapter_reg,
            policy=ExecutionPolicy(retry=RetryPolicy(max_attempts_same_endpoint=1)),
        )

        # Send request asking for 16,384 tokens (which would trigger HTTP 400 on Groq without clamping)
        req = NormalizedRequest(
            model="default",
            messages=[NormalizedMessage(role="user", content="hello")],
            max_output_tokens=16384,
        )

        resp, decision, _ = executor.execute(req, pool="coding", strategy="priority")
        self.assertEqual(resp.content, "clamped completion")

        # Verify that the request received by the adapter was clamped to <= 950
        self.assertEqual(len(mock_adapter.request_history), 1)
        adapted_req = mock_adapter.request_history[0]
        self.assertEqual(adapted_req.max_output_tokens, 950)

        # Verify with real OpenAI adapter that serialized JSON body has max_tokens=950
        from durallm.providers.adapters import OpenAICompatibleAdapter
        adapter = OpenAICompatibleAdapter("groq")
        endpoint = cap_reg.get_endpoint("groq:qwen36")
        real_prepared = adapter.prepare_request(endpoint, adapted_req, api_key="test-key")
        import json
        payload = json.loads(real_prepared.body_bytes.decode("utf-8"))
        self.assertEqual(payload.get("max_tokens"), 950)

    def test_404_deprecation_classifier_and_dead_list(self):
        """Test that HTTP 404 model_not_found marks model deprecated and dead without retry loop."""
        from durallm.pools import POOL_MANAGER
        # Test classify_api_error marks deprecated in pool
        classified = classify_api_error(
            {"error": {"message": "The model `old-qwen-model` does not exist", "code": "model_not_found"}},
            status_code=404,
            pool="coding",
            route_id="old-qwen-model",
        )
        self.assertEqual(classified.reason, FailoverReason.model_not_found)
        self.assertFalse(classified.retryable)
        self.assertTrue(classified.should_fallback)

        # Verify pool manager blacklisted it
        self.assertIn(("coding", "old-qwen-model"), POOL_MANAGER.deprecated)

        # Also test on a local IsolatedPoolManager directly
        local_pm = IsolatedPoolManager()
        local_pm.coding_routes = [
            RouteDefinition(
                id="old-deprecated-model",
                provider="groq",
                model="old-qwen-model",
                pool="coding",
                base_url="https://api.groq.com",
                api_format="openai",
                env_key=None,
            )
        ]
        local_pm.mark_deprecated("coding", "old-qwen-model")
        candidates = local_pm.get_candidate_routes("coding")
        self.assertEqual(len(candidates), 0)


if __name__ == "__main__":
    unittest.main()
