"""Tests for Frontier 5 Multi-Key Rotation and TPM/RPM Shuffling."""

import unittest
from unittest.mock import MagicMock

from durallm.capability.profile import Endpoint, ModelProfile
from durallm.capability.registry import CapabilityRegistry
from durallm.execution.executor import GatewayExecutor
from durallm.protocol.ir import NormalizedMessage, NormalizedRequest
from durallm.providers.adapters import ProviderAdapterRegistry
from durallm.providers.base import ProviderExecutionResult
from durallm.routing.keys import KeyRotationPool


class TestMultiKeyRotation(unittest.TestCase):

    def test_key_rotation_pool_round_robin_and_cooldown(self):
        pool = KeyRotationPool()
        ep = Endpoint(
            id="groq-llama",
            provider="groq",
            model="llama-3.3-70b-versatile",
            base_url="https://api.groq.com/openai/v1",
            env_key="GROQ_API_KEY",
            profile=ModelProfile("groq", "llama-3.3-70b-versatile"),
        )
        keys = {"GROQ_API_KEY": "key_alpha, key_beta, key_gamma"}

        # 1. Round-robin through available keys
        k1, kid1 = pool.get_active_key(ep, keys)
        k2, kid2 = pool.get_active_key(ep, keys)
        k3, kid3 = pool.get_active_key(ep, keys)
        self.assertEqual(k1, "key_alpha")
        self.assertEqual(k2, "key_beta")
        self.assertEqual(k3, "key_gamma")

        # 2. Rate-limit key_beta for 30s
        self.assertTrue(pool.has_alternative_key(ep, keys, kid2))
        pool.record_rate_limit(kid2, cooldown_seconds=30.0)

        # 3. Next selections only yield alpha and gamma
        k4, kid4 = pool.get_active_key(ep, keys)
        k5, kid5 = pool.get_active_key(ep, keys)
        self.assertIn(k4, ["key_alpha", "key_gamma"])
        self.assertIn(k5, ["key_alpha", "key_gamma"])
        self.assertNotEqual(k4, "key_beta")
        self.assertNotEqual(k5, "key_beta")

    def test_executor_absorbs_429_via_key_rotation_without_cross_provider_failover(self):
        reg = CapabilityRegistry()
        ep_groq = Endpoint(
            id="groq-llama",
            provider="groq",
            model="llama-3.3-70b-versatile",
            base_url="https://api.groq.com/openai/v1",
            env_key="GROQ_KEY",
            priority=1,
            pool="general_agent",
            profile=ModelProfile("groq", "llama-3.3-70b-versatile"),
        )
        ep_fallback = Endpoint(
            id="claude-fallback",
            provider="anthropic",
            model="claude-3-5-sonnet",
            base_url="https://api.anthropic.com/v1",
            env_key="ANTHROPIC_KEY",
            priority=2,
            pool="general_agent",
            profile=ModelProfile("anthropic", "claude-3-5-sonnet"),
        )
        reg.register_endpoint(ep_groq)
        reg.register_endpoint(ep_fallback)

        adapters = ProviderAdapterRegistry()
        mock_groq = MagicMock()
        mock_fallback = MagicMock()

        auth_headers_seen = []

        def mock_prepare(endpoint, req, api_key=""):
            m = MagicMock()
            m.headers = {"Authorization": f"Bearer {api_key}"}
            return m

        def mock_groq_execute(prepared, timeout_seconds=None):
            auth = prepared.headers["Authorization"]
            auth_headers_seen.append(auth)
            if "key_1" in auth:
                # Key 1 hits 429 rate limit
                return ProviderExecutionResult(
                    status_code=429,
                    headers={"Retry-After": "10"},
                    body=b'{"error": {"message": "Rate limit exceeded on this organization key"}}',
                    duration_ms=10.0,
                )
            # Key 2 succeeds!
            return ProviderExecutionResult(
                status_code=200,
                headers={},
                body=b'{"id": "ok_resp", "choices": [{"message": {"role": "assistant", "content": "Hermes resumed with key 2"}}]}',
                duration_ms=25.0,
            )

        mock_groq.prepare_request = mock_prepare
        mock_groq.execute = mock_groq_execute
        mock_groq.normalize_response.return_value = MagicMock(
            content="Hermes resumed with key 2",
            tool_calls=[],
            finish_reason="stop",
            input_tokens=10,
            output_tokens=10,
        )

        adapters.register("groq", mock_groq)
        adapters.register("anthropic", mock_fallback)

        key_pool = KeyRotationPool()
        executor = GatewayExecutor(
            capability_registry=reg,
            adapter_registry=adapters,
            key_pool=key_pool,
            sleeper=lambda _: None,
        )

        req = NormalizedRequest(
            request_id="req_hermes_429",
            model="llama-3.3-70b-versatile",
            messages=[NormalizedMessage(role="user", content="Continue multi-turn task")],
        )

        resp, decision, ledger = executor.execute(
            req,
            pool="general_agent",
            strategy="priority",
            api_keys={"GROQ_KEY": "key_1, key_2", "ANTHROPIC_KEY": "ant_key"},
        )

        # Hermes stayed on groq-llama without switching to claude-fallback!
        self.assertEqual(decision.selected_endpoint.id, "groq-llama")
        self.assertEqual(resp.content, "Hermes resumed with key 2")
        self.assertEqual(len(auth_headers_seen), 2)
        self.assertIn("Bearer key_1", auth_headers_seen[0])
        self.assertIn("Bearer key_2", auth_headers_seen[1])
        # Fallback provider was never called!
        mock_fallback.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
