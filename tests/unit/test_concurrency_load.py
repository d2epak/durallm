"""High-Concurrency Load Tests under Forced Contention (100+ Concurrent Requests)."""

import threading
import unittest
from typing import List

from durallm.breaker.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from durallm.breaker.state import CircuitBreakerState
from durallm.errors import BreakerOpenError, ProbeAdmissionDeniedError


class ControlledClock:
    def __init__(self, start: float = 1000.0):
        self._time = start

    def __call__(self) -> float:
        return self._time

    def advance(self, seconds: float) -> None:
        self._time += seconds


class TestConcurrencyAndContention(unittest.TestCase):
    """Verifies thread-safety, permit bounds, and zero race conditions under 100+ concurrent callers."""

    def test_half_open_probe_permits_strictly_bounded_under_100_threads(self):
        max_probe_permits = 3
        clock = ControlledClock()
        config = CircuitBreakerConfig(
            failure_rate_threshold=50.0,
            sliding_window_size=10,
            minimum_number_of_calls=5,
            wait_duration_open_ms=5000.0,
            half_open_max_calls=max_probe_permits,
            clock=clock,
        )
        cb = CircuitBreaker("contention_test_breaker", config=config)

        # 1. Force breaker into OPEN state
        with cb._lock:
            cb._state = CircuitBreakerState.OPEN
            cb._opened_at_monotonic = clock()

        # 2. Advance clock by 10 seconds so wait duration expires -> breaker can transition to HALF_OPEN
        clock.advance(10.0)

        # 3. Spawn 120 threads attempting simultaneous permission acquisition
        num_threads = 120
        barrier = threading.Barrier(num_threads)
        admitted_threads: List[int] = []
        denied_threads: List[int] = []
        lock = threading.Lock()

        def worker(thread_idx: int):
            barrier.wait()  # Align all 120 threads to execute concurrently
            try:
                cb.acquire_permission()
                with lock:
                    admitted_threads.append(thread_idx)
            except (BreakerOpenError, ProbeAdmissionDeniedError):
                with lock:
                    denied_threads.append(thread_idx)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Invariant: Exactly half_open_max_calls threads admitted, rest denied
        self.assertEqual(len(admitted_threads), max_probe_permits)
        self.assertEqual(len(denied_threads), num_threads - max_probe_permits)
        self.assertEqual(cb.state, CircuitBreakerState.HALF_OPEN)

    def test_100_concurrent_callers_metrics_thread_safety(self):
        config = CircuitBreakerConfig(
            sliding_window_size=200,
            minimum_number_of_calls=150,  # Won't trip until 150 calls
            failure_rate_threshold=90.0,
        )
        cb = CircuitBreaker("high_throughput_breaker", config=config)

        num_threads = 100
        barrier = threading.Barrier(num_threads)

        def worker(thread_idx: int):
            barrier.wait()
            cb.acquire_permission()
            if thread_idx % 2 == 0:
                cb.record_success(duration_ms=50.0)
            else:
                cb.record_failure(duration_ms=60.0)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        snap = cb.snapshot()
        metrics = snap["metrics"]
        self.assertEqual(metrics["total_calls"], num_threads)
        self.assertEqual(metrics["failed_calls"], 50)
        self.assertAlmostEqual(metrics["failure_rate"], 50.0, places=1)


if __name__ == "__main__":
    unittest.main()
