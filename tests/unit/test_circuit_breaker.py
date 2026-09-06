"""Formal Circuit Breaker Unit Tests (Resilience4j Specification Compliance)."""

import threading
import unittest
from typing import List

from llm_circuit_breaker.breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerState,
    SlidingWindowType,
    StateTransitionEvent,
)
from llm_circuit_breaker.errors import (
    BreakerOpenError,
    ProbeAdmissionDeniedError,
)
from llm_circuit_breaker.models import (
    FailoverReason,
    FailureCategory,
    FailureClassification,
)


class MockClock:
    """Deterministic monotonic clock for circuit breaker testing."""
    def __init__(self, start: float = 1000.0):
        self.current = start

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


class TestCircuitBreaker(unittest.TestCase):

    def setUp(self):
        self.clock = MockClock()
        self.events: List[StateTransitionEvent] = []

    def make_breaker(
        self,
        failure_rate_threshold: float = 50.0,
        slow_call_rate_threshold: float = 50.0,
        slow_call_duration_ms: float = 2000.0,
        minimum_number_of_calls: int = 4,
        window_size: int = 10,
        wait_duration_open_ms: float = 10000.0,
        half_open_max_calls: int = 2,
    ) -> CircuitBreaker:
        config = CircuitBreakerConfig(
            failure_rate_threshold=failure_rate_threshold,
            slow_call_rate_threshold=slow_call_rate_threshold,
            slow_call_duration_ms=slow_call_duration_ms,
            minimum_number_of_calls=minimum_number_of_calls,
            sliding_window_type=SlidingWindowType.COUNT_BASED,
            sliding_window_size=window_size,
            wait_duration_open_ms=wait_duration_open_ms,
            half_open_max_calls=half_open_max_calls,
            clock=self.clock,
        )
        breaker = CircuitBreaker("test-breaker", config)
        breaker.add_event_listener(self.events.append)
        return breaker

    def test_1_below_threshold_failures_remain_closed(self):
        breaker = self.make_breaker(failure_rate_threshold=50.0, minimum_number_of_calls=4)
        # 1 failure out of 4 calls = 25% failure rate (below 50%)
        breaker.acquire_permission()
        breaker.record_failure(100.0)
        breaker.acquire_permission()
        breaker.record_success(100.0)
        breaker.acquire_permission()
        breaker.record_success(100.0)
        breaker.acquire_permission()
        breaker.record_success(100.0)

        self.assertEqual(breaker.state, CircuitBreakerState.CLOSED)
        self.assertEqual(len(self.events), 0)

    def test_2_threshold_reached_opens_breaker(self):
        breaker = self.make_breaker(failure_rate_threshold=50.0, minimum_number_of_calls=4)
        # 2 failures out of 4 calls = 50% failure rate
        breaker.acquire_permission()
        breaker.record_failure(100.0)
        breaker.acquire_permission()
        breaker.record_failure(100.0)
        breaker.acquire_permission()
        breaker.record_success(100.0)
        breaker.acquire_permission()
        breaker.record_success(100.0)

        self.assertEqual(breaker.state, CircuitBreakerState.OPEN)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0].from_state, CircuitBreakerState.CLOSED)
        self.assertEqual(self.events[0].to_state, CircuitBreakerState.OPEN)

    def test_3_open_rejects_without_upstream_call(self):
        breaker = self.make_breaker(failure_rate_threshold=50.0, minimum_number_of_calls=2)
        breaker.acquire_permission()
        breaker.record_failure(100.0)
        breaker.acquire_permission()
        breaker.record_failure(100.0)
        self.assertEqual(breaker.state, CircuitBreakerState.OPEN)

        with self.assertRaises(BreakerOpenError) as ctx:
            breaker.acquire_permission()
        self.assertEqual(ctx.exception.breaker_id, "test-breaker")

    def test_4_wait_period_controls_half_open(self):
        breaker = self.make_breaker(wait_duration_open_ms=10000.0, minimum_number_of_calls=2)
        breaker.acquire_permission()
        breaker.record_failure(100.0)
        breaker.acquire_permission()
        breaker.record_failure(100.0)
        self.assertEqual(breaker.state, CircuitBreakerState.OPEN)

        # Before wait duration elapsed (5 seconds < 10s)
        self.clock.advance(5.0)
        self.assertEqual(breaker.state, CircuitBreakerState.OPEN)

        # After wait duration elapsed (advance another 5.1s -> total 10.1s)
        self.clock.advance(5.1)
        self.assertEqual(breaker.state, CircuitBreakerState.HALF_OPEN)

    def test_5_only_bounded_probe_calls_enter_half_open(self):
        breaker = self.make_breaker(wait_duration_open_ms=10000.0, half_open_max_calls=2, minimum_number_of_calls=2)
        breaker.acquire_permission()
        breaker.record_failure(100.0)
        breaker.acquire_permission()
        breaker.record_failure(100.0)
        self.clock.advance(10.1)
        self.assertEqual(breaker.state, CircuitBreakerState.HALF_OPEN)

        # Probe 1 admitted
        self.assertTrue(breaker.acquire_permission())
        # Probe 2 admitted
        self.assertTrue(breaker.acquire_permission())
        # Probe 3 rejected because max probes (2) already in flight
        with self.assertRaises(ProbeAdmissionDeniedError):
            breaker.acquire_permission()

    def test_6_successful_probes_close_breaker(self):
        breaker = self.make_breaker(wait_duration_open_ms=10000.0, half_open_max_calls=2, minimum_number_of_calls=2)
        breaker.acquire_permission()
        breaker.record_failure(100.0)
        breaker.acquire_permission()
        breaker.record_failure(100.0)
        self.clock.advance(10.1)

        # Probe 1 succeeds
        breaker.acquire_permission()
        breaker.record_success(50.0)
        self.assertEqual(breaker.state, CircuitBreakerState.HALF_OPEN)

        # Probe 2 succeeds -> closes breaker
        breaker.acquire_permission()
        breaker.record_success(50.0)
        self.assertEqual(breaker.state, CircuitBreakerState.CLOSED)

    def test_7_failed_probe_reopens_breaker(self):
        breaker = self.make_breaker(wait_duration_open_ms=10000.0, half_open_max_calls=2, minimum_number_of_calls=2)
        breaker.acquire_permission()
        breaker.record_failure(100.0)
        breaker.acquire_permission()
        breaker.record_failure(100.0)
        self.clock.advance(10.1)
        self.assertEqual(breaker.state, CircuitBreakerState.HALF_OPEN)

        # Probe 1 fails -> immediately reopens breaker
        breaker.acquire_permission()
        breaker.record_failure(100.0)
        self.assertEqual(breaker.state, CircuitBreakerState.OPEN)

    def test_8_slow_call_threshold_works_independently(self):
        breaker = self.make_breaker(
            failure_rate_threshold=100.0,  # disable failure rate trigger
            slow_call_rate_threshold=50.0,
            slow_call_duration_ms=2000.0,
            minimum_number_of_calls=4,
        )
        # 2 slow calls (3000ms >= 2000ms) out of 4 successful calls = 50% slow rate
        breaker.acquire_permission()
        breaker.record_success(duration_ms=3000.0)  # slow
        breaker.acquire_permission()
        breaker.record_success(duration_ms=3000.0)  # slow
        breaker.acquire_permission()
        breaker.record_success(duration_ms=500.0)
        breaker.acquire_permission()
        breaker.record_success(duration_ms=500.0)

        self.assertEqual(breaker.state, CircuitBreakerState.OPEN)
        self.assertIn("Slow call rate", self.events[-1].reason)

    def test_9_ignored_exceptions_do_not_affect_breaker(self):
        breaker = self.make_breaker(failure_rate_threshold=50.0, minimum_number_of_calls=4)
        non_poisoning = FailureClassification(
            category=FailureCategory.CLIENT_FAULT,
            reason=FailoverReason.auth,
            should_fallback=True,
            retryable=False,
            poisons_health=False,  # DOES NOT POISON
            message="bad client key",
        )

        # 4 calls: 2 non-poisoning failures + 2 successes = 0% poisoning failure rate
        breaker.acquire_permission()
        breaker.record_failure(duration_ms=50.0, failure_classification=non_poisoning)
        breaker.acquire_permission()
        breaker.record_failure(duration_ms=50.0, failure_classification=non_poisoning)
        breaker.acquire_permission()
        breaker.record_success(duration_ms=50.0)
        breaker.acquire_permission()
        breaker.record_success(duration_ms=50.0)

        # Breaker must remain CLOSED because non-poisoning failures are ignored
        self.assertEqual(breaker.state, CircuitBreakerState.CLOSED)

    def test_10_concurrent_callers_cannot_exceed_half_open_permit_count(self):
        breaker = self.make_breaker(wait_duration_open_ms=5000.0, half_open_max_calls=2, minimum_number_of_calls=2)
        breaker.acquire_permission()
        breaker.record_failure(100.0)
        breaker.acquire_permission()
        breaker.record_failure(100.0)
        self.clock.advance(5.1)
        self.assertEqual(breaker.state, CircuitBreakerState.HALF_OPEN)

        admitted = []
        rejected = []

        def worker():
            try:
                breaker.acquire_permission()
                admitted.append(1)
            except ProbeAdmissionDeniedError:
                rejected.append(1)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(admitted), 2, "Exactly 2 probe permits must be granted")
        self.assertEqual(len(rejected), 18, "Remaining 18 callers must be rejected")

    def test_11_clock_behavior_is_deterministic_under_injected_clock(self):
        breaker = self.make_breaker(wait_duration_open_ms=1000.0, minimum_number_of_calls=2)
        breaker.acquire_permission()
        breaker.record_failure(50.0)
        breaker.acquire_permission()
        breaker.record_failure(50.0)
        self.assertEqual(breaker.state, CircuitBreakerState.OPEN)

        # Precise step checks
        self.clock.advance(0.999)
        self.assertEqual(breaker.state, CircuitBreakerState.OPEN)
        self.clock.advance(0.002)
        self.assertEqual(breaker.state, CircuitBreakerState.HALF_OPEN)

    def test_12_state_transitions_emit_events(self):
        breaker = self.make_breaker(wait_duration_open_ms=1000.0, half_open_max_calls=1, minimum_number_of_calls=2)
        breaker.acquire_permission()
        breaker.record_failure(50.0)
        breaker.acquire_permission()
        breaker.record_failure(50.0)  # Trips to OPEN
        self.clock.advance(1.1)
        breaker.acquire_permission()   # Transitions to HALF_OPEN, probe admitted
        breaker.record_success(50.0)  # Transitions to CLOSED

        self.assertEqual(len(self.events), 3)
        self.assertEqual((self.events[0].from_state, self.events[0].to_state), (CircuitBreakerState.CLOSED, CircuitBreakerState.OPEN))
        self.assertEqual((self.events[1].from_state, self.events[1].to_state), (CircuitBreakerState.OPEN, CircuitBreakerState.HALF_OPEN))
        self.assertEqual((self.events[2].from_state, self.events[2].to_state), (CircuitBreakerState.HALF_OPEN, CircuitBreakerState.CLOSED))


if __name__ == "__main__":
    unittest.main()
