"""Deadline and Budget-Aware Timeout Management."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from llm_circuit_breaker.errors import DeadlineExceededError


@dataclass
class Deadline:
    """Tracks hierarchical deadlines and remaining request execution budgets."""
    total_timeout_ms: float = 60000.0
    connect_timeout_ms: float = 5000.0
    ttft_timeout_ms: float = 15000.0
    idle_stream_timeout_ms: float = 10000.0
    per_attempt_timeout_ms: float = 25000.0
    clock: Callable[[], float] = time.monotonic
    start_time_monotonic: Optional[float] = None

    def __post_init__(self):
        if self.start_time_monotonic is None:
            self.start_time_monotonic = self.clock()

    def elapsed_ms(self) -> float:
        """Elapsed time in milliseconds since deadline started."""
        return max(0.0, (self.clock() - self.start_time_monotonic) * 1000.0)

    def remaining_ms(self) -> float:
        """Remaining time in milliseconds before total deadline expires."""
        return max(0.0, self.total_timeout_ms - self.elapsed_ms())

    def is_expired(self) -> bool:
        """Return True if total operation deadline has passed."""
        return self.remaining_ms() <= 0.0

    def check(self) -> None:
        """Raise DeadlineExceededError if expired."""
        if self.is_expired():
            raise DeadlineExceededError(
                f"Operation deadline of {self.total_timeout_ms:.0f}ms expired (elapsed: {self.elapsed_ms():.0f}ms)",
                deadline_ms=self.total_timeout_ms,
                elapsed_ms=self.elapsed_ms(),
            )

    def per_attempt_timeout_seconds(self) -> float:
        """Remaining attempt timeout in seconds, bounded by remaining total deadline."""
        self.check()
        rem_seconds = self.remaining_ms() / 1000.0
        attempt_seconds = self.per_attempt_timeout_ms / 1000.0
        return max(0.1, min(attempt_seconds, rem_seconds))
