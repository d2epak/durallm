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
        # OpenRouter clamps to model profile max_output_tokens (e.g. 8192)
        ep_openrouter = Endpoint(
            id="coding:openrouter-gemma4",
            provider="openrouter",
            model="google/gemma-4-31b-it:free",
            base_url="https://openrouter.ai/api/v1",
            profile=ModelProfile("openrouter", "google/gemma-4-31b-it:free", max_output_tokens=8192),
        )
        norm_req = NormalizedRequest(
            request_id="req-test-1",
            model="claude-3-5-sonnet",
            messages=[],
            max_output_tokens=16384,
        )
        prepared_openrouter = adapter.prepare_request(ep_openrouter, norm_req, api_key="test-key")
        import json
        payload_openrouter = json.loads(prepared_openrouter.body_bytes.decode("utf-8"))
        self.assertEqual(payload_openrouter["max_tokens"], 8192)

        # Groq clamps to 950 due to strict 1,000 OTPM limit
        ep_groq = Endpoint(
            id="coding:groq-qwen36",
            provider="groq",
            model="qwen/qwen3.6-27b",
            base_url="https://api.groq.com/openai/v1",
            profile=ModelProfile("groq", "qwen/qwen3.6-27b", max_output_tokens=950),
        )
        prepared_groq = adapter.prepare_request(ep_groq, norm_req, api_key="test-key")
        payload_groq = json.loads(prepared_groq.body_bytes.decode("utf-8"))
        self.assertEqual(payload_groq["max_tokens"], 950)

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
        self.assertIn("qwen/qwen3.6-27b", route_models)
        self.assertIn("openai/gpt-oss-120b", route_models)
        self.assertIn("openai/gpt-oss-20b", route_models)
        self.assertIn("google/gemma-4-31b-it:free", route_models)
        self.assertIn("cohere/north-mini-code:free", route_models)
        self.assertIn("nvidia/nemotron-3-super-120b-a12b:free", route_models)
        self.assertIn("google/gemma-4-31b-it", route_models)
        self.assertIn("openrouter/free", route_models)
        self.assertIn("nvidia/nemotron-3-ultra-550b-a55b", route_models)

        # Check Groq OTPM clamped max_output_tokens
        routes_by_model = {r.model: r for r in DEFAULT_CODING_ROUTES}
        self.assertLessEqual(routes_by_model["qwen/qwen3.6-27b"].max_output_tokens, 1000)
        self.assertLessEqual(routes_by_model["openai/gpt-oss-120b"].max_output_tokens, 1000)
        self.assertLessEqual(routes_by_model["openai/gpt-oss-20b"].max_output_tokens, 1000)

        # Verify capability profiles exist with tool support enabled
        for model in route_models:
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

    # ------------------------------------------------------------------
    # 6. Claude Code /goal Session with 128k max_tokens & 7.3k prompt
    # ------------------------------------------------------------------
    def test_claude_code_goal_session_budget_and_preflight_do_not_collapse(self):
        from durallm.capability.profile import ModelProfile
        from durallm.execution.executor import GatewayExecutor
        from durallm.protocol.anthropic import anthropic_request_to_ir
        from durallm.protocol.openai import openai_request_to_ir
        from durallm.routing.tokenizer import preflight_context

        profile = ModelProfile(
            provider="groq",
            model="qwen/qwen3.6-27b",
            context_window=131072,
            max_output_tokens=8192,
            supports_tools=True,
        )

        # 1. Verify preflight_context does not reject 128k requested output
        res = preflight_context(
            profile,
            input_tokens=7353,
            expected_output_tokens=128000,
            safety_margin_tokens=2048,
            allow_compaction=True,
        )
        self.assertTrue(res.compatible, f"Preflight should be compatible, got reason: {res.reason}")
        self.assertTrue(res.fits_without_compaction)
        # Expected output should be clamped to profile.max_output_tokens (8192)
        self.assertEqual(res.expected_output_tokens, 8192)

        # 2. Verify Anthropic request with 128k max_tokens converts to IR
        anthropic_body = {
            "model": "claude-3-7-sonnet",
            "max_tokens": 128000,
            "system": "You are Claude Code.",
            "messages": [
                {"role": "user", "content": "x" * 29400}  # ~7350 tokens
            ],
        }
        norm_req = anthropic_request_to_ir(anthropic_body)
        self.assertEqual(norm_req.max_output_tokens, 128000)

        # 3. Verify GatewayExecutor budget sizing allocates proper available input budget
        executor = GatewayExecutor()
        ep = Endpoint(
            id="coding:groq-qwen36-coding",
            provider="groq",
            model="qwen/qwen3.6-27b",
            base_url="https://api.groq.com/openai/v1",
            pool="coding",
            profile=profile,
        )
        executor.capability_registry.register_endpoint(ep)

        # Ensure compacting does not raise ContextOverflowError
        max_supported_out = profile.max_output_tokens or 4096
        desired_output = min(norm_req.max_output_tokens or max_supported_out, max_supported_out)
        from durallm.agent.context import ContextBudget
        budget = ContextBudget(
            model_context_window=profile.context_window,
            desired_output_tokens=desired_output,
            safety_margin_tokens=2048,
        )
        self.assertGreater(budget.available_input_budget, 100000)
        compacted, was_compacted = executor.context_manager.compact(norm_req, budget)
        self.assertFalse(was_compacted)

        # 4. Verify OpenAI max_completion_tokens is parsed into IR
        openai_body = {
            "model": "o3-mini",
            "max_completion_tokens": 64000,
            "messages": [{"role": "user", "content": "Hello"}],
        }
        norm_oai = openai_request_to_ir(openai_body)
        self.assertEqual(norm_oai.max_output_tokens, 64000)

    # ------------------------------------------------------------------
    # 7. Context-Adaptive Dynamic Routing for Large Goal Sessions
    # ------------------------------------------------------------------
    def test_context_adaptive_routing_prioritizes_high_context_routes_for_large_prompts(self):
        from durallm.capability.profile import Endpoint, ModelProfile
        from durallm.capability.registry import CapabilityRegistry
        from durallm.routing import CapabilityRouter, RequirementVector

        reg = CapabilityRegistry()
        ep_groq = Endpoint(
            id="coding:groq",
            provider="groq",
            model="qwen/qwen3.6-27b",
            base_url="https://api.groq.com",
            priority=1,
            pool="coding",
            profile=ModelProfile("groq", "qwen/qwen3.6-27b", context_window=131072),
        )
        ep_openrouter = Endpoint(
            id="coding:openrouter",
            provider="openrouter",
            model="qwen/qwen-2.5-coder-32b-instruct:free",
            base_url="https://openrouter.ai",
            priority=2,
            pool="coding",
            profile=ModelProfile("openrouter", "qwen/qwen-2.5-coder-32b-instruct:free", context_window=256000),
        )
        reg.register_endpoint(ep_groq)
        reg.register_endpoint(ep_openrouter)

        router = CapabilityRouter(capability_registry=reg)

        # Small prompt (1000 tokens): priority 1 (Groq) is selected
        req_small = RequirementVector(estimated_input_tokens=1000)
        selected_small, _ = router.select_candidate(req_small, pool="coding", strategy="priority")
        self.assertEqual(selected_small.id, "coding:groq")

        # Large prompt (7500 tokens): 256k OpenRouter model is prioritized over Groq (strict TPM)
        req_large = RequirementVector(estimated_input_tokens=7500)
        selected_large, _ = router.select_candidate(req_large, pool="coding", strategy="priority")
        self.assertEqual(selected_large.id, "coding:openrouter")

    # ------------------------------------------------------------------
    # 8. Fallback Hop Budget Permits Traversing All 6 Pool Routes
    # ------------------------------------------------------------------
    def test_default_fallback_policy_permits_traversing_six_routes(self):
        from durallm.execution.ledger import AttemptLedger
        from durallm.execution.policy import ExecutionPolicy

        policy = ExecutionPolicy()
        self.assertGreaterEqual(policy.fallback.max_fallback_hops, 6)
        self.assertGreaterEqual(policy.max_total_attempts, 6)

        ledger = AttemptLedger(policy)
        # Verify ledger allows 5 fallbacks (6 endpoints) without throwing FallbackBudgetExhaustedError
        for i in range(5):
            ledger.validate_next_candidate(f"endpoint-{i}")
            ledger.mark_fallback()
        # 6th endpoint should be valid
        ledger.validate_next_candidate("endpoint-5")

    # ------------------------------------------------------------------
    # 9. Groq TPM Output Token Defense
    # ------------------------------------------------------------------
    def test_groq_tpm_defense_throttles_max_tokens_for_large_inputs(self):
        import json

        from durallm.capability.profile import ModelProfile
        adapter = OpenAICompatibleAdapter()
        ep_groq = Endpoint(
            id="coding:groq",
            provider="groq",
            model="qwen/qwen3.6-27b",
            base_url="https://api.groq.com",
            profile=ModelProfile("groq", "qwen/qwen3.6-27b", context_window=131072, max_output_tokens=8192),
        )
        # Large prompt of ~8000 tokens
        norm_req = NormalizedRequest(
            request_id="req-groq-tpm",
            model="qwen/qwen3.6-27b",
            messages=[],
            system_instruction="x" * 32000,  # ~8000 tokens
            max_output_tokens=8192,
        )
        prepared = adapter.prepare_request(ep_groq, norm_req, api_key="test-key")
        body = json.loads(prepared.body_bytes.decode("utf-8"))
        # Groq clamps max_tokens to 950 due to 1,000 OTPM limit
        self.assertLessEqual(body["max_tokens"], 950)
        self.assertGreaterEqual(body["max_tokens"], 100)

    # ------------------------------------------------------------------
    # 10. Deadline Extended Defaults for Heavy Coding Models
    # ------------------------------------------------------------------
    def test_deadline_defaults_extended_for_large_coding_models(self):
        from durallm.execution.deadline import Deadline
        d = Deadline()
        self.assertGreaterEqual(d.per_attempt_timeout_ms, 60000.0)
        self.assertGreaterEqual(d.total_timeout_ms, 180000.0)


    # ------------------------------------------------------------------
    # 11. Deep Tail Tool Compaction for 55m /goal Sessions (14k Tokens)
    # ------------------------------------------------------------------
    def test_deep_tail_tool_compaction_for_long_horizon_sessions(self):
        from durallm.pruner import estimate_tokens, prune_for_groq_tpm
        messages = [
            {"role": "system", "content": "You are Claude Code, an expert coding assistant."},
            {"role": "user", "content": "Build the full stack task manager application in FastAPI and React."},
        ]
        # Simulate 10 turns of tool outputs where the last 6 messages contain huge outputs (>7k tokens)
        for i in range(10):
            messages.append({"role": "assistant", "content": f"Running step {i}"})
            messages.append({"role": "tool", "content": f"Large command/file output for step {i}: " + ("abc " * 1000)})
        messages.append({"role": "assistant", "content": "Analyzing latest test failures."})
        messages.append({"role": "user", "content": "Please continue fixing the tests."})

        payload = {"model": "qwen/qwen3.6-27b", "messages": messages}
        initial_tokens = estimate_tokens(payload)
        self.assertGreater(initial_tokens, 7000)

        pruned = prune_for_groq_tpm(payload, max_input_tokens=3800)
        pruned_tokens = estimate_tokens(pruned)
        self.assertLessEqual(pruned_tokens, 3800)
        # Root user objective and system prompt must be strictly preserved
        self.assertEqual(pruned["messages"][0]["content"], "You are Claude Code, an expert coding assistant.")
        self.assertEqual(pruned["messages"][1]["content"], "Build the full stack task manager application in FastAPI and React.")

    # ------------------------------------------------------------------
    # 12. Groq TPM 429 Classification Extracts Retry-After and Sets Retryable
    # ------------------------------------------------------------------
    def test_groq_tpm_classified_as_rate_limit_with_retry_after(self):
        err_msg = (
            '{"error":{"message":"Rate limit reached for model qwen/qwen3.6-27b on tokens per minute (TPM): '
            'Limit 6000, Used 5980, Requested 350. Please try again in 5.2s.","type":"tokens","code":"rate_limit_exceeded"}}'
        )
        classified = classify_failure(err_msg, status_code=429)
        self.assertEqual(classified.category, FailureCategory.RATE_LIMIT)
        self.assertEqual(classified.reason, FailoverReason.rate_limit)
        self.assertTrue(classified.retryable)
        self.assertTrue(classified.should_fallback)
        self.assertTrue(classified.poisons_health)
        self.assertAlmostEqual(classified.retry_after_seconds, 5.2, places=1)

    # ------------------------------------------------------------------
    # 13. Circuit Breaker Does Not Trip on 15s-45s Slow LLM Responses
    # ------------------------------------------------------------------
    def test_circuit_breaker_does_not_trip_on_normal_generation_latencies(self):
        from durallm.breaker.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
        from durallm.breaker.state import CircuitBreakerState

        # 120s slow call duration default allows 15s-45s calls without counting as slow
        cb = CircuitBreaker("test-breaker", CircuitBreakerConfig())
        for _ in range(15):
            cb.record_success(duration_ms=45000.0)  # 45s response latency
        self.assertEqual(cb.state, CircuitBreakerState.CLOSED)

    # ------------------------------------------------------------------
    # 14. AttemptLedger Rate Limit Reset Allows Rollover Without Cycle Error
    # ------------------------------------------------------------------
    def test_attempt_ledger_reset_for_rate_limit_retry(self):
        from durallm.execution.ledger import AttemptLedger
        from durallm.execution.policy import ExecutionPolicy
        from durallm.models import AttemptRecord

        policy = ExecutionPolicy()
        ledger = AttemptLedger(policy)

        # Attempt endpoint A, fallback to B
        rec_a = AttemptRecord(request_id="req1", endpoint_id="ep-a", provider="groq", model="qwen", attempt_index=1, fallback_index=0)
        ledger.record_attempt(rec_a)
        ledger.mark_fallback()

        rec_b = AttemptRecord(request_id="req1", endpoint_id="ep-b", provider="groq", model="gpt-oss", attempt_index=2, fallback_index=1)
        ledger.record_attempt(rec_b)

        # Re-attempting ep-a directly without reset would trip CycleDetectedError
        with self.assertRaises(Exception):
            ledger.validate_next_candidate("ep-a")

        # After rate limit rollover reset, ep-a is re-admitted cleanly
        ledger.reset_for_rate_limit_retry(["ep-a", "ep-b"])
        ledger.validate_next_candidate("ep-a")
        self.assertTrue(ledger.can_attempt_endpoint("ep-a"))


if __name__ == "__main__":
    unittest.main()

