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

        # Mark groq route in cooldown (e.g. 30s transient probe)
        pm.mark_cooldown("coding", "route-groq", seconds=30.0)
        self.assertTrue(pm.is_route_in_cooldown("coding", "route-groq"))
        # Backward-compat: provider-level check still works
        self.assertTrue(pm.is_provider_in_cooldown("coding", "groq"))

        candidates = pm.get_candidate_routes("coding")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].provider, "sambanova")

        # Expire cooldown artificially
        pm.cooldowns[("coding", "route-groq")] = time.monotonic() - 1.0
        self.assertFalse(pm.is_route_in_cooldown("coding", "route-groq"))
        pm.auto_expire_cooldowns("coding")
        candidates = pm.get_candidate_routes("coding")
        self.assertEqual(len(candidates), 2)

        # Mark sambanova route quota exhausted (24h)
        pm.mark_quota_exhausted("coding", "route-sambanova", seconds=86400.0)
        self.assertTrue(pm.is_route_quota_exhausted("coding", "route-sambanova"))
        candidates = pm.get_candidate_routes("coding")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].id, "route-groq")

    def test_per_route_cooldown_independence(self):
        """A 429 on openrouter/gemma must NOT block openrouter/qwen or openrouter/north-mini."""
        pm = IsolatedPoolManager()
        pm.coding_routes = [
            RouteDefinition(
                id="or-gemma", provider="openrouter", model="google/gemma-4-31b-it:free",
                pool="coding", base_url="https://openrouter.ai/api/v1", api_format="openai", env_key=None,
            ),
            RouteDefinition(
                id="or-qwen", provider="openrouter", model="qwen/qwen-2.5-coder-32b-instruct:free",
                pool="coding", base_url="https://openrouter.ai/api/v1", api_format="openai", env_key=None,
            ),
            RouteDefinition(
                id="or-north", provider="openrouter", model="cohere/north-mini-code:free",
                pool="coding", base_url="https://openrouter.ai/api/v1", api_format="openai", env_key=None,
            ),
        ]

        # All 3 routes are initially active
        self.assertEqual(len(pm.get_candidate_routes("coding")), 3)

        # 429 on gemma: only gemma gets cooled down, qwen and north stay active
        pm.mark_cooldown("coding", "or-gemma", seconds=60.0)

        candidates = pm.get_candidate_routes("coding")
        self.assertEqual(len(candidates), 2)
        candidate_ids = {c.id for c in candidates}
        self.assertIn("or-qwen", candidate_ids)
        self.assertIn("or-north", candidate_ids)
        self.assertNotIn("or-gemma", candidate_ids)

        # Verify route-level check
        self.assertTrue(pm.is_route_in_cooldown("coding", "or-gemma"))
        self.assertFalse(pm.is_route_in_cooldown("coding", "or-qwen"))
        self.assertFalse(pm.is_route_in_cooldown("coding", "or-north"))

        # Provider-level check returns True because at least one route is in cooldown
        self.assertTrue(pm.is_provider_in_cooldown("coding", "openrouter"))

    def test_sticky_primary_routing_with_decay_affinity(self):
        """After a success, select_route should prefer the same route until 120s decay."""
        pm = IsolatedPoolManager()
        pm.coding_routes = [
            RouteDefinition(
                id="groq-a", provider="groq", model="model-a",
                pool="coding", base_url="https://api.groq.com/openai/v1", api_format="openai", env_key=None,
            ),
            RouteDefinition(
                id="nvidia-b", provider="nvidia", model="model-b",
                pool="coding", base_url="https://integrate.api.nvidia.com/v1", api_format="openai", env_key=None,
            ),
        ]

        # Without any success recorded, first select returns groq-a (round-robin index 0)
        r1 = pm.select_route("coding")
        self.assertEqual(r1.id, "groq-a")

        # Next round-robin would return nvidia-b
        r2 = pm.select_route("coding")
        self.assertEqual(r2.id, "nvidia-b")

        # Record success on nvidia-b — sticky affinity established
        pm.record_route_success("coding", "nvidia-b")

        # Now select_route should prefer nvidia-b (sticky) instead of groq-a (round-robin)
        r3 = pm.select_route("coding")
        self.assertEqual(r3.id, "nvidia-b")
        r4 = pm.select_route("coding")
        self.assertEqual(r4.id, "nvidia-b")

        # Simulate 120s decay by backdating the success timestamp
        pm.last_success_at["coding"] = time.monotonic() - 121.0

        # Now sticky has expired, should fall back to round-robin
        r5 = pm.select_route("coding")
        # Round-robin resumes from its last index
        self.assertIn(r5.id, {"groq-a", "nvidia-b"})

        # If sticky route goes into cooldown, should fall back to the other
        pm.record_route_success("coding", "nvidia-b")
        pm.mark_cooldown("coding", "nvidia-b", seconds=60.0)

        r6 = pm.select_route("coding")
        self.assertEqual(r6.id, "groq-a")

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

    def test_groq_tpm_input_limit_inflight_compaction_retry(self):
        """Test Groq TPM input token limit parsing and in-flight payload compaction retry without a 60s cooldown loop."""
        from durallm.classifier import classify_failure, parse_input_tpm_limit
        from durallm.models import FailureCategory

        err_msg = (
            '{"error":{"message":"request too large for model `qwen/qwen3.6-27b` in organization '
            '`org_01krs77pgdfxg83872v2vtq6ba` service tier `on_demand` on input tokens per minute (TPM): '
            'Limit 6000, Requested 15972."}}'
        )
        limit = parse_input_tpm_limit(err_msg)
        self.assertEqual(limit, 6000)

        classified = classify_failure(err_msg, status_code=413)
        self.assertEqual(classified.category, FailureCategory.REQUEST_INCOMPATIBILITY)
        self.assertEqual(classified.reason, FailoverReason.payload_too_large)
        self.assertFalse(classified.poisons_health)
        self.assertEqual(classified.details.get("token_limit"), 6000)

        # Test GatewayExecutor in-flight compaction on TPM limit
        cap_reg = CapabilityRegistry()
        adapter_reg = ProviderAdapterRegistry()
        mock_adapter = ProgrammableMockAdapter("groq")
        adapter_reg._adapters["groq"] = mock_adapter

        cap_reg.register_profile(
            ModelProfile(
                "groq",
                "qwen/qwen3.6-27b",
                context_window=32768,
                max_output_tokens=4096,
                supports_tools=True,
            )
        )
        cap_reg.register_endpoint(
            Endpoint(
                id="groq:qwen36-tpm",
                provider="groq",
                model="qwen/qwen3.6-27b",
                base_url="https://api.groq.com",
                pool="coding",
                priority=1,
            )
        )

        # Sequence: First attempt fails with 413 input TPM limit, second attempt succeeds with compacted payload
        mock_adapter.set_sequence([
            MockFaultAction(status_code=413, body=err_msg.encode("utf-8")),
            MockFaultAction.success("compacted groq completion"),
        ])

        executor = GatewayExecutor(
            capability_registry=cap_reg,
            adapter_registry=adapter_reg,
            policy=ExecutionPolicy(retry=RetryPolicy(max_attempts_same_endpoint=2)),
        )

        # Create request with large multi-turn history (>4000 tokens) that can be compacted
        req = NormalizedRequest(
            model="default",
            messages=[
                NormalizedMessage(role="user", content="Initial objective: build a compiler"),
                NormalizedMessage(role="assistant", content="Let's build intermediate representation " + ("x" * 4000)),
                NormalizedMessage(role="user", content="Here is a huge log trace " + ("error[E0425]: cannot find value in this scope\n" * 1500)),
                NormalizedMessage(role="user", content="Now fix the compile issue"),
            ],
            max_output_tokens=1024,
        )

        resp, decision, ledger = executor.execute(req, pool="coding", strategy="priority")
        self.assertEqual(resp.content, "compacted groq completion")
        # Ensure it attempted the same endpoint twice rather than locking it out for 60s
        self.assertEqual(len(mock_adapter.request_history), 2)
        self.assertEqual(ledger.attempts[0].endpoint_id, "groq:qwen36-tpm")
        self.assertEqual(ledger.attempts[1].endpoint_id, "groq:qwen36-tpm")
        self.assertTrue(ledger.attempts[1].compacted)

    def test_openrouter_account_wide_daily_quota_lockout_and_utc_reset(self):
        """Test OpenRouter account-wide daily quota lockout and UTC midnight reset calculation."""
        from durallm.classifier import calculate_seconds_until_utc_midnight, classify_failure
        from durallm.models import FailureCategory

        reset_sec = calculate_seconds_until_utc_midnight()
        self.assertGreater(reset_sec, 60.0)
        self.assertLessEqual(reset_sec, 86400.0)

        err_msg = (
            '{"error":{"message":"rate limit exceeded: free-models-per-day. add 5 credits to unlock 1000 free model requests per day",'
            '"code":429,"metadata":{"headers":{}}}}'
        )
        classified = classify_failure(err_msg, status_code=429)
        self.assertEqual(classified.category, FailureCategory.RATE_LIMIT)
        self.assertEqual(classified.reason, FailoverReason.billing)
        self.assertTrue(classified.details.get("account_wide"))
        self.assertAlmostEqual(classified.retry_after_seconds, reset_sec, delta=5.0)

        # Test executor excludes all OpenRouter models in one hop
        cap_reg = CapabilityRegistry()
        adapter_reg = ProviderAdapterRegistry()
        mock_or = ProgrammableMockAdapter("openrouter")
        mock_sn = ProgrammableMockAdapter("sambanova")
        adapter_reg._adapters["openrouter"] = mock_or
        adapter_reg._adapters["sambanova"] = mock_sn

        cap_reg.register_profile(ModelProfile("openrouter", "model-a", 32768, 4096, True))
        cap_reg.register_profile(ModelProfile("openrouter", "model-b", 32768, 4096, True))
        cap_reg.register_profile(ModelProfile("sambanova", "model-c", 32768, 4096, True))

        cap_reg.register_endpoint(Endpoint(id="openrouter:model-a", provider="openrouter", model="model-a", base_url="https://openrouter.ai", pool="coding", priority=1))
        cap_reg.register_endpoint(Endpoint(id="openrouter:model-b", provider="openrouter", model="model-b", base_url="https://openrouter.ai", pool="coding", priority=2))
        cap_reg.register_endpoint(Endpoint(id="sambanova:model-c", provider="sambanova", model="model-c", base_url="https://sambanova.ai", pool="coding", priority=3))

        mock_or.set_sequence([MockFaultAction(status_code=429, body=err_msg.encode("utf-8"))])
        mock_sn.set_sequence([MockFaultAction.success("sambanova fallback success")])

        pm = IsolatedPoolManager()
        executor = GatewayExecutor(
            capability_registry=cap_reg,
            adapter_registry=adapter_reg,
            pool_manager=pm,
            policy=ExecutionPolicy(retry=RetryPolicy(max_attempts_same_endpoint=1)),
        )

        req = NormalizedRequest(model="default", messages=[NormalizedMessage(role="user", content="write code")])
        resp, decision, ledger = executor.execute(req, pool="coding", strategy="priority")

        # Response succeeded via sambanova without wasting an attempt on openrouter:model-b!
        self.assertEqual(resp.content, "sambanova fallback success")
        self.assertEqual(len(mock_or.request_history), 1)  # Only model-a attempted, model-b skipped because account is locked out!
        self.assertEqual(len(mock_sn.request_history), 1)
        self.assertTrue(pm.is_provider_quota_exhausted("openrouter"))

    def test_nvidia_adaptive_timeout_and_transient_socket_timeout(self):
        """Test NVIDIA NIM adaptive timeouts and transient socket read timeout handling."""
        from durallm.execution.deadline import Deadline
        from durallm.router import calculate_adaptive_timeout

        # Test Deadline per_attempt_timeout_for_provider scaling for enterprise tier and large input tokens
        dl = Deadline(total_timeout_ms=300000.0, per_attempt_timeout_ms=50000.0)
        t_std = dl.per_attempt_timeout_for_provider("openai", estimated_input_tokens=1000)
        t_nv_small = dl.per_attempt_timeout_for_provider("nvidia", estimated_input_tokens=1000)
        t_nv_large = dl.per_attempt_timeout_for_provider("nvidia", estimated_input_tokens=20000)

        self.assertGreaterEqual(t_std, 35.0)
        self.assertGreaterEqual(t_nv_small, 45.0)
        self.assertGreater(t_nv_large, t_nv_small)
        self.assertLessEqual(t_nv_large, 90.0)

        # Test calculate_adaptive_timeout in router
        payload_small = {"messages": [{"role": "user", "content": "hello"}]}
        payload_large = {"messages": [{"role": "user", "content": "x" * 65000}]}
        ad_small = calculate_adaptive_timeout("nvidia", payload_small)
        ad_large = calculate_adaptive_timeout("nvidia", payload_large)
        self.assertGreaterEqual(ad_small, 45.0)
        self.assertGreater(ad_large, ad_small)


if __name__ == "__main__":
    unittest.main()

