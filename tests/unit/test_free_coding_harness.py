"""Unit tests for Claude Code Free LLM Harness fixes."""

from __future__ import annotations

import unittest

from durallm.capability.profile import Endpoint
from durallm.capability.registry import DEFAULT_CAPABILITY_REGISTRY
from durallm.classifier import classify_api_error, classify_failure
from durallm.models import FailoverReason, FailureCategory
from durallm.pools import DEFAULT_CODING_ROUTES, POOL_MANAGER
from durallm.protocol.ir import NormalizedRequest
from durallm.providers.adapters import OpenAICompatibleAdapter
from durallm.proxy import build_failover_telemetry, pool_for_model
from durallm.pruner import FREE_TIER_TPM_LIMIT, estimate_tokens, prune_for_free_tpm
from durallm.translators import anthropic_to_openai_request


class TestFreeCodingHarness(unittest.TestCase):
    """Test suite covering the 5 free LLM harness enhancements."""

    # ------------------------------------------------------------------
    # 1. Output Cap Clamping for Groq / OpenRouter
    # ------------------------------------------------------------------
    def test_anthropic_to_openai_request_clamps_max_tokens_for_groq_and_openrouter(self):
        # Oversized max_tokens (e.g. 16384 or 64000 from Claude 3.7 / 3.5)
        anthropic_req = {
            "model": "claude-3-7-sonnet",
            "max_tokens": 16384,
            "messages": [{"role": "user", "content": "Write quicksort"}],
        }

        # Groq dispatch
        groq_req = anthropic_to_openai_request(anthropic_req, "groq/qwen/qwen3.6-27b")
        self.assertEqual(groq_req["max_tokens"], 8192)

        # OpenRouter dispatch
        openrouter_req = anthropic_to_openai_request(anthropic_req, "openrouter/cohere/north-mini-code:free")
        self.assertEqual(openrouter_req["max_tokens"], 8192)

        # Smaller max_tokens preserved
        small_req = {
            "model": "claude-3-5-haiku",
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": "Hello"}],
        }
        clamped_small = anthropic_to_openai_request(small_req, "groq/openai/gpt-oss-120b")
        self.assertEqual(clamped_small["max_tokens"], 2048)

    def test_adapter_prepare_request_clamps_max_tokens_for_groq_and_openrouter(self):
        from durallm.capability.profile import ModelProfile
        adapter = OpenAICompatibleAdapter()
        endpoint = Endpoint(
            id="coding:groq-qwen36",
            provider="groq",
            model="qwen/qwen3.6-27b",
            base_url="https://api.groq.com/openai/v1",
            profile=ModelProfile("groq", "qwen/qwen3.6-27b", max_output_tokens=8192),
        )
        norm_req = NormalizedRequest(
            request_id="req-test-1",
            model="claude-3-5-sonnet",
            messages=[],
            max_output_tokens=16384,
        )
        prepared = adapter.prepare_request(endpoint, norm_req, api_key="test-key")
        import json
        payload = json.loads(prepared.body_bytes.decode("utf-8"))
        self.assertEqual(payload["max_tokens"], 8192)

    # ------------------------------------------------------------------
    # 2. TPM Payload Pruning
    # ------------------------------------------------------------------
    def test_prune_for_free_tpm_under_and_over_ceiling(self):
        self.assertEqual(FREE_TIER_TPM_LIMIT, 12000)

        # Small payload: remains untouched
        small_payload = {
            "model": "qwen/qwen3.6-27b",
            "messages": [
                {"role": "system", "content": "You are a coding assistant."},
                {"role": "user", "content": "hello world"},
            ],
        }
        result_small = prune_for_free_tpm(small_payload, tpm_limit=12000)
        self.assertEqual(len(result_small["messages"]), 2)

        # Oversized payload: 25 historical tool messages with large outputs (>15,000 tokens)
        huge_tool_output = "x" * 4000  # ~1000 tokens each
        messages = [
            {"role": "system", "content": "System prompt for agent"},
            {"role": "user", "content": "Root user task"},
        ]
        for i in range(20):
            messages.append({"role": "tool", "content": f"Turn {i} log: {huge_tool_output}"})
        messages.extend([
            {"role": "assistant", "content": "Almost done"},
            {"role": "user", "content": "Final question"},
        ])
        huge_payload = {"model": "qwen/qwen3.6-27b", "messages": messages}
        initial_tokens = estimate_tokens(huge_payload)
        self.assertGreater(initial_tokens, 12000)

        pruned = prune_for_free_tpm(huge_payload, tpm_limit=12000)
        pruned_tokens = estimate_tokens(pruned)
        self.assertLessEqual(pruned_tokens, 12000)
        # System prompt and root user prompt must be preserved
        self.assertEqual(pruned["messages"][0]["role"], "system")
        self.assertEqual(pruned["messages"][1]["role"], "user")

    # ------------------------------------------------------------------
    # 3. OpenRouter 24h Quota Lockout
    # ------------------------------------------------------------------
    def test_openrouter_free_models_per_day_classified_as_billing_and_locked_out(self):
        err_msg = '{"error": {"message": "rate limit exceeded: free-models-per-day", "code": 429}}'
        classified = classify_failure(err_msg, status_code=429)

        self.assertEqual(classified.category, FailureCategory.RATE_LIMIT)
        self.assertEqual(classified.reason, FailoverReason.billing)
        self.assertFalse(classified.retryable)
        self.assertTrue(classified.should_fallback)
        self.assertFalse(classified.poisons_health)
        self.assertEqual(classified.retry_after_seconds, 86400.0)

        # Verify classify_api_error marks quota exhausted in POOL_MANAGER
        test_route_id = "test-openrouter-route"
        classify_api_error(err_msg, status_code=429, pool="coding", route_id=test_route_id)
        import time
        reset_time = POOL_MANAGER.exhausted_quotas.get(("coding", test_route_id), 0)
        self.assertGreater(reset_time, time.time() + 86000)

    # ------------------------------------------------------------------
    # 4. Verified Free Coding Routes & Capability Registry
    # ------------------------------------------------------------------
    def test_default_coding_routes_contain_verified_active_free_models(self):
        route_models = {r.model for r in DEFAULT_CODING_ROUTES}
        expected_models = {
            "qwen/qwen3.6-27b",
            "openai/gpt-oss-120b",
            "nvidia/nemotron-3-ultra-550b-a55b",
            "google/gemma-4-31b-it",
            "cohere/north-mini-code:free",
            "qwen/qwen-2.5-coder-32b-instruct:free",
        }
        self.assertEqual(route_models, expected_models)

        # Check context windows
        routes_by_model = {r.model: r for r in DEFAULT_CODING_ROUTES}
        self.assertEqual(routes_by_model["qwen/qwen3.6-27b"].context_length, 131072)
        self.assertEqual(routes_by_model["openai/gpt-oss-120b"].context_length, 131072)
        self.assertEqual(routes_by_model["nvidia/nemotron-3-ultra-550b-a55b"].context_length, 131072)
        self.assertEqual(routes_by_model["google/gemma-4-31b-it"].context_length, 131072)
        self.assertEqual(routes_by_model["cohere/north-mini-code:free"].context_length, 256000)
        self.assertEqual(routes_by_model["qwen/qwen-2.5-coder-32b-instruct:free"].context_length, 256000)

        # Verify capability profiles exist with tool support enabled
        for model in expected_models:
            provider = routes_by_model[model].provider
            profile = DEFAULT_CAPABILITY_REGISTRY.get_profile(provider, model)
            self.assertTrue(profile.supports_tools, f"Model {model} must declare supports_tools=True")

    # ------------------------------------------------------------------
    # 5. Subagent Model Aliasing in proxy.py
    # ------------------------------------------------------------------
    def test_subagent_claude_model_aliasing(self):
        # Claude subagent models route directly to coding pool
        self.assertEqual(pool_for_model("claude-opus-5"), "coding")
        self.assertEqual(pool_for_model("claude-3-5-haiku"), "coding")
        self.assertEqual(pool_for_model("claude-3-5-haiku-20241022"), "coding")
        self.assertEqual(pool_for_model("claude-3-5-sonnet"), "coding")
        self.assertEqual(pool_for_model("claude-3-7-sonnet"), "coding")
        self.assertEqual(pool_for_model("general-agent-model"), "general_agent")

        # Telemetry should not flag spurious failovers for virtual claude aliases
        class DummyDecision:
            selected_endpoint = Endpoint(
                id="coding:groq-qwen36",
                provider="groq",
                model="qwen/qwen3.6-27b",
                base_url="https://api.groq.com",
            )
            evaluated_candidates = []

        headers, meta = build_failover_telemetry("claude-opus-5", DummyDecision(), None)
        self.assertNotIn("X-LCB-Failover", headers)
        self.assertIsNone(meta)


if __name__ == "__main__":
    unittest.main()
