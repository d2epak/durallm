"""Tests for Execution Policies, Deadlines, and Cycle Protection."""

import unittest

from llm_circuit_breaker.errors import (
    CycleDetectedError,
    DeadlineExceededError,
    FallbackBudgetExhaustedError,
)
from llm_circuit_breaker.execution import (
    AttemptLedger,
    Deadline,
    ExecutionPolicy,
    FallbackPolicy,
    RetryPolicy,
)
from llm_circuit_breaker.models import AttemptRecord


class MockClock:
    def __init__(self, start: float = 100.0):
        self.time = start

    def __call__(self) -> float:
        return self.time

    def advance(self, seconds: float) -> None:
        self.time += seconds


class TestExecutionPolicy(unittest.TestCase):

    def test_deadline_budget_and_expiration(self):
        clock = MockClock()
        deadline = Deadline(total_timeout_ms=5000.0, per_attempt_timeout_ms=2000.0, clock=clock)

        self.assertFalse(deadline.is_expired())
        self.assertEqual(deadline.remaining_ms(), 5000.0)
        self.assertEqual(deadline.per_attempt_timeout_seconds(), 2.0)

        # Advance 4 seconds -> remaining is 1 second (< per_attempt of 2s)
        clock.advance(4.0)
        self.assertFalse(deadline.is_expired())
        self.assertEqual(deadline.remaining_ms(), 1000.0)
        self.assertEqual(deadline.per_attempt_timeout_seconds(), 1.0)

        # Advance another 1.1s -> expired
        clock.advance(1.1)
        self.assertTrue(deadline.is_expired())
        with self.assertRaises(DeadlineExceededError):
            deadline.check()

    def test_retry_backoff_calculation_and_retry_after(self):
        policy = RetryPolicy(base_backoff_ms=200.0, max_backoff_ms=2000.0, jitter=False)

        # Exponential backoff without jitter
        self.assertEqual(policy.compute_backoff_seconds(1), 0.2)
        self.assertEqual(policy.compute_backoff_seconds(2), 0.4)
        self.assertEqual(policy.compute_backoff_seconds(3), 0.8)

        # Explicit Retry-After takes precedence
        self.assertEqual(policy.compute_backoff_seconds(1, retry_after=45.0), 45.0)

    def test_cycle_detection_in_fallback_ledger(self):
        policy = ExecutionPolicy(
            retry=RetryPolicy(max_attempts_same_endpoint=1),
            fallback=FallbackPolicy(max_fallback_hops=4),
            max_total_attempts=5,
        )
        ledger = AttemptLedger(policy)

        # Attempt A
        rec_a1 = AttemptRecord(endpoint_id="provider-A")
        ledger.record_attempt(rec_a1)

        # Fallback to B
        ledger.mark_fallback()
        rec_b1 = AttemptRecord(endpoint_id="provider-B")
        ledger.record_attempt(rec_b1)

        # Attempting B again on retry is allowed
        # But attempting A again after B is a cycle (A -> B -> A)
        with self.assertRaises(CycleDetectedError) as ctx:
            ledger.validate_next_candidate("provider-A")
        self.assertEqual(ctx.exception.details["candidate_id"], "provider-A")

    def test_fallback_budget_exhaustion(self):
        policy = ExecutionPolicy(
            fallback=FallbackPolicy(max_fallback_hops=2),
            max_total_attempts=10,
        )
        ledger = AttemptLedger(policy)

        ledger.mark_fallback()  # hop 1
        ledger.mark_fallback()  # hop 2

        # 3rd fallback exceeds max hops
        with self.assertRaises(FallbackBudgetExhaustedError):
            ledger.validate_next_candidate("provider-C")


if __name__ == "__main__":
    unittest.main()
