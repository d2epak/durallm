"""Adversarial Red Team Tests (Tests 1 through 10 from Mandate Section 56)."""

import time
import unittest

from llm_circuit_breaker.agent.context import ContextBudget, ContextManager, estimate_tokens
from llm_circuit_breaker.agent.idempotency import ToolExecutionLedger
from llm_circuit_breaker.agent.tool_validation import ToolCallValidator
from llm_circuit_breaker.breaker.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from llm_circuit_breaker.breaker.state import CircuitBreakerState
from llm_circuit_breaker.capability.profile import Endpoint, ModelProfile
from llm_circuit_breaker.capability.registry import CapabilityRegistry
from llm_circuit_breaker.classifier import classify_failure
from llm_circuit_breaker.errors import (
    BreakerOpenError,
    CycleDetectedError,
    DeadlineExceededError,
    FallbackBudgetExhaustedError,
    ProbeAdmissionDeniedError,
)
from llm_circuit_breaker.execution.deadline import Deadline
from llm_circuit_breaker.execution.ledger import AttemptLedger
from llm_circuit_breaker.execution.policy import ExecutionPolicy, FallbackPolicy, RetryPolicy
from llm_circuit_breaker.models import FailureCategory, FailoverReason
from llm_circuit_breaker.protocol.ir import (
    NormalizedToolResult,
    NormalizedMessage,
    NormalizedRequest,
    NormalizedToolCall,
    NormalizedToolDefinition,
)
from llm_circuit_breaker.routing.requirements import RequirementVector
from llm_circuit_breaker.routing.router import CapabilityRouter
from llm_circuit_breaker.validation.response import ResponseValidator


class TestRedTeamAdversarial(unittest.TestCase):
    """Hostile edge-case tests validating system invariants against gaming or corruption."""

    # Test 1: HTTP 200 + malformed tool schema -> semantic failure, not provider outage
    def test_01_http_200_malformed_tool_schema_does_not_poison_health(self):
        validator = ToolCallValidator(strict=True)
        schema = {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        }
        res = validator.validate_tool_call("bash", {"wrong_key": 123}, schema=schema, known_tools=["bash"])
        self.assertFalse(res.is_executable)

        classification = classify_failure(res.error_message, status_code=200)
        # Invariant: Must be classified as semantic failure, and MUST NOT poison provider health
        self.assertEqual(classification.category, FailureCategory.SEMANTIC_AGENT_FAILURE)
        self.assertFalse(classification.poisons_health)

    # Test 2: 429 + Retry-After=60 -> no premature retry
    def test_02_rate_limit_429_retry_after_honored(self):
        headers = {"retry-after": "60"}
        classification = classify_failure("Rate limit exceeded", status_code=429, headers=headers)
        self.assertEqual(classification.reason, FailoverReason.rate_limit)
        self.assertEqual(classification.retry_after_seconds, 60.0)

        policy = RetryPolicy()
        backoff = policy.compute_backoff_seconds(1, retry_after=classification.retry_after_seconds)
        self.assertEqual(backoff, 60.0)

    # Test 3: Multiple simultaneous HALF_OPEN candidates -> never exceed configured probe count
    def test_03_half_open_never_exceeds_configured_probe_count(self):
        config = CircuitBreakerConfig(
            wait_duration_open_ms=10.0,
            half_open_max_calls=2,
        )
        cb = CircuitBreaker("probe_test", config=config)
        with cb._lock:
            cb._state = CircuitBreakerState.HALF_OPEN
            cb._half_open_entered_at_monotonic = time.monotonic()
            cb._half_open_in_flight = 0

        # Admit permit 1 & 2
        cb.acquire_permission()
        cb.acquire_permission()

        # Permit 3 MUST be denied immediately
        with self.assertRaises(ProbeAdmissionDeniedError):
            cb.acquire_permission()

    # Test 4: Tool execution succeeds, response lost -> no duplicate execution with ledger
    def test_04_lost_response_idempotency_prevents_duplicate_execution(self):
        ledger = ToolExecutionLedger()
        op_id = "agent_turn_critical_5"
        tool_name = "transfer_payment"
        args = {"amount": 500, "dest": "acct_888"}

        rec = ledger.register_tool_call("call_pay_1", op_id, tool_name, args)
        ledger.mark_committed("call_pay_1", {"tx_id": "tx_settled_123", "status": "committed"})

        # Gateway retries request under same logical op:
        has_receipt, receipt = ledger.check_idempotency(op_id, tool_name, args)
        self.assertTrue(has_receipt)
        self.assertEqual(receipt["tx_id"], "tx_settled_123")

    # Test 5: Critical fact hidden deep in context -> fact preserved
    def test_05_critical_fact_hidden_deep_in_context_survives_compaction(self):
        planted_fact = "PLANTED_SECRET_KEY: alpha_vault_9921"
        noisy = "\n".join([f"LOG_DATA_LINE_{i}" for i in range(1000)])
        # The fact lives in the protected root prompt; the noise is a compactable tool result.
        # (Noise inside the root prompt itself cannot be shrunk and now raises ContextOverflowError.)
        req = NormalizedRequest(
            model="large",
            messages=[
                NormalizedMessage(role="user", content=f"GOAL: Restore system\n{planted_fact}"),
                NormalizedMessage(
                    role="assistant", content="Working...",
                    tool_results=[NormalizedToolResult(tool_call_id="call_log", tool_name="read_log", content=noisy)],
                ),
                NormalizedMessage(role="user", content="Next step"),
            ],
        )
        budget = ContextBudget(model_context_window=2000, desired_output_tokens=500, safety_margin_tokens=200)
        manager = ContextManager(preserve_tail_turns=2)
        compacted, was_compacted = manager.compact(req, budget)

        self.assertTrue(was_compacted)
        self.assertIn(planted_fact, compacted.messages[0].content)
        self.assertLessEqual(estimate_tokens(compacted), budget.available_input_budget)

    # Test 6: Low-latency unreliable provider vs high-latency reliable provider -> policy-controlled
    def test_06_unreliable_vs_reliable_provider_selection(self):
        cap_reg = CapabilityRegistry()
        ep_unreliable = Endpoint(
            id="ep-fast-bad",
            provider="fast_bad",
            model="model-1",
            base_url="mock://fast",
            pool="coding",
            profile=ModelProfile("fast_bad", "model-1", supports_tools=True),
        )
        ep_reliable = Endpoint(
            id="ep-slow-good",
            provider="slow_good",
            model="model-2",
            base_url="mock://slow",
            pool="coding",
            profile=ModelProfile("slow_good", "model-2", supports_tools=True),
        )
        cap_reg.register_endpoint(ep_unreliable)
        cap_reg.register_endpoint(ep_reliable)

        router = CapabilityRouter(capability_registry=cap_reg)
        # Fast endpoint has 90% failure rate
        for _ in range(9):
            router.health_store.record_failure("ep-fast-bad", latency_ms=10.0, status_code=500)
        router.health_store.record_success("ep-fast-bad", latency_ms=10.0)

        # Slow endpoint has 100% success rate
        for _ in range(10):
            router.health_store.record_success("ep-slow-good", latency_ms=300.0)

        req = RequirementVector(require_tools=True)
        ep, dec = router.select_candidate(req, pool="coding", strategy="reliability_aware")
        self.assertEqual(ep.id, "ep-slow-good")

    # Test 7: Provider becomes healthy during load -> bounded reintegration
    def test_07_provider_recovery_bounded_reintegration(self):
        config = CircuitBreakerConfig(wait_duration_open_ms=10.0, half_open_max_calls=2)
        cb = CircuitBreaker("recovery_test", config=config)
        with cb._lock:
            cb._state = CircuitBreakerState.OPEN
            cb._opened_at_monotonic = time.monotonic() - 1.0  # Open wait duration expired

        # First 2 calls admitted as probes
        cb.acquire_permission()
        cb.acquire_permission()
        # 3rd is bounded
        with self.assertRaises(ProbeAdmissionDeniedError):
            cb.acquire_permission()

        # Both probes succeed -> breaker closes
        cb.record_success(20.0)
        cb.record_success(20.0)
        self.assertEqual(cb.state, CircuitBreakerState.CLOSED)

    # Test 8: Fallback cycle appears dynamically -> cycle blocked
    def test_08_dynamic_fallback_cycle_detected_and_blocked(self):
        policy = ExecutionPolicy()
        ledger = AttemptLedger(policy)

        # Attempt A -> B -> A is prohibited
        from llm_circuit_breaker.models import AttemptRecord
        ledger.record_attempt(AttemptRecord(endpoint_id="ep-a"))
        ledger.record_attempt(AttemptRecord(endpoint_id="ep-b"))

        with self.assertRaises(CycleDetectedError):
            ledger.validate_next_candidate("ep-a")

    # Test 9: Deadline is exhausted during retry -> no further attempt
    def test_09_deadline_exhaustion_blocks_further_attempts(self):
        deadline = Deadline(total_timeout_ms=10.0)
        time.sleep(0.02)  # Expire 10ms deadline
        self.assertTrue(deadline.is_expired())
        with self.assertRaises(DeadlineExceededError):
            deadline.check()

    # Test 10: Fallback budget exhausted -> no further attempts
    def test_10_fallback_budget_exhaustion_blocks_further_hops(self):
        policy = ExecutionPolicy(fallback=FallbackPolicy(max_fallback_hops=2))
        ledger = AttemptLedger(policy)
        ledger.mark_fallback()
        ledger.mark_fallback()

        with self.assertRaises(FallbackBudgetExhaustedError):
            ledger.validate_next_candidate("ep-new")


if __name__ == "__main__":
    unittest.main()
