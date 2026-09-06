"""Sliding Window Metrics for Circuit Breaker."""

from __future__ import annotations

import collections
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Deque, Dict, Optional


class SlidingWindowType(str, Enum):
    COUNT_BASED = "count"
    TIME_BASED = "time"


@dataclass(frozen=True)
class CallOutcome:
    """Record of a single completed call outcome."""
    success: bool
    duration_ms: float
    timestamp_monotonic: float
    is_slow: bool
    poisons_health: bool


class SlidingWindowMetrics:
    """Thread-safe sliding window metrics for failure and slow-call rates."""

    def __init__(
        self,
        window_type: SlidingWindowType = SlidingWindowType.COUNT_BASED,
        window_size: int = 20,
        slow_call_duration_ms: float = 5000.0,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.window_type = window_type
        self.window_size = window_size
        self.slow_call_duration_ms = slow_call_duration_ms
        self.clock = clock or (lambda: 0.0)

        self._lock = threading.RLock()
        self._samples: Deque[CallOutcome] = collections.deque()

    def reset(self) -> None:
        """Clear all metrics samples."""
        with self._lock:
            self._samples.clear()

    def record(
        self,
        success: bool,
        duration_ms: float,
        timestamp_monotonic: float,
        poisons_health: bool = True,
    ) -> None:
        """Record an outcome into the sliding window."""
        is_slow = duration_ms >= self.slow_call_duration_ms
        outcome = CallOutcome(
            success=success,
            duration_ms=duration_ms,
            timestamp_monotonic=timestamp_monotonic,
            is_slow=is_slow,
            poisons_health=poisons_health,
        )

        with self._lock:
            self._samples.append(outcome)
            self._evict_expired(timestamp_monotonic)

    def _evict_expired(self, current_time_monotonic: float) -> None:
        """Evict samples outside the window boundary."""
        if self.window_type == SlidingWindowType.COUNT_BASED:
            while len(self._samples) > self.window_size:
                self._samples.popleft()
        elif self.window_type == SlidingWindowType.TIME_BASED:
            cutoff = current_time_monotonic - float(self.window_size)
            while self._samples and self._samples[0].timestamp_monotonic < cutoff:
                self._samples.popleft()

    def snapshot(self, current_time_monotonic: Optional[float] = None) -> Dict[str, Any]:
        """Compute thread-safe snapshot of sliding window statistics."""
        with self._lock:
            now = current_time_monotonic if current_time_monotonic is not None else self.clock()
            self._evict_expired(now)

            total = len(self._samples)
            if total == 0:
                return {
                    "total_calls": 0,
                    "success_calls": 0,
                    "failed_calls": 0,
                    "slow_calls": 0,
                    "failure_rate": 0.0,
                    "slow_call_rate": 0.0,
                }

            failed = 0
            slow = 0
            success = 0
            for s in self._samples:
                if not s.success and s.poisons_health:
                    failed += 1
                elif s.success:
                    success += 1
                if s.is_slow:
                    slow += 1

            failure_rate = (failed / total) * 100.0 if total > 0 else 0.0
            slow_call_rate = (slow / total) * 100.0 if total > 0 else 0.0

            return {
                "total_calls": total,
                "success_calls": success,
                "failed_calls": failed,
                "slow_calls": slow,
                "failure_rate": failure_rate,
                "slow_call_rate": slow_call_rate,
            }
