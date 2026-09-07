"""Deadline and Budget-Aware Timeout Management."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from durallm.errors import DeadlineExceededError
from durallm.providers.base import TransportTimeouts


@dataclass
class Deadline:
    """Tracks hierarchical deadlines and remaining request execution budgets."""
    total_timeout_ms: float = 60000.0
    connect_timeout_ms: float = 5000.0
    tls_timeout_ms: float = 5000.0
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

    def transport_timeouts(self, streaming: bool = False) -> TransportTimeouts:
        """Return phase budgets clipped to the remaining request deadline.

        Buffered calls keep the per-attempt ceiling. A visible stream may
        legitimately outlive that retry ceiling, but never the caller's total
        request deadline; once bytes are visible it is not silently retried.
        """
        self.check()
        total = self.remaining_ms() if streaming else min(self.remaining_ms(), self.per_attempt_timeout_ms)
        # A phase cannot exceed its enclosing total budget.  The lower bound
        # avoids handing a zero timeout to socket/http clients during a close
        # race; ``check`` above still rejects an already-expired request.
        bounded_total = max(1.0, total)
        return TransportTimeouts(
            connect_timeout_ms=min(self.connect_timeout_ms, bounded_total),
            tls_timeout_ms=min(self.tls_timeout_ms, bounded_total),
            first_byte_timeout_ms=min(self.ttft_timeout_ms, bounded_total),
            idle_timeout_ms=min(self.idle_stream_timeout_ms, bounded_total),
            total_timeout_ms=bounded_total,
        )
