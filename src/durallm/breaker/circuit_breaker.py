"""Resilience4j-Grade Circuit Breaker Core Implementation."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from durallm.breaker.metrics import (
    SlidingWindowMetrics,
    SlidingWindowType,
)
from durallm.breaker.state import (
    CircuitBreakerState,
    StateTransitionEvent,
)
from durallm.errors import (
    BreakerForcedOpenError,
    BreakerOpenError,
    ProbeAdmissionDeniedError,
)
from durallm.models import FailureClassification

logger = logging.getLogger("durallm.breaker")


@dataclass
class CircuitBreakerConfig:
    """Circuit Breaker configuration thresholds and timings."""
    failure_rate_threshold: float = 50.0  # Percentage (0 - 100)
    slow_call_rate_threshold: float = 50.0  # Percentage (0 - 100)
    slow_call_duration_ms: float = 5000.0  # Milliseconds
    minimum_number_of_calls: int = 10
    sliding_window_type: SlidingWindowType = SlidingWindowType.COUNT_BASED
    sliding_window_size: int = 20
    wait_duration_open_ms: float = 30000.0  # Time in OPEN before entering HALF_OPEN
    half_open_max_calls: int = 2  # Max probe calls admitted in HALF_OPEN
    max_half_open_duration_ms: float = 10000.0  # Timeout for HALF_OPEN probes
    clock: Callable[[], float] = time.monotonic


class CircuitBreaker:
    """Thread-safe circuit breaker with explicit state transitions and bounded probe permits."""

    def __init__(
        self,
        breaker_id: str,
        config: Optional[CircuitBreakerConfig] = None,
        cluster_store: Optional[Any] = None,
    ):
        self.id = breaker_id
        self.config = config or CircuitBreakerConfig()
        self.clock = self.config.clock
        self.cluster_store = cluster_store

        self._lock = threading.RLock()
        self._state = CircuitBreakerState.CLOSED
        self._opened_at_monotonic: float = 0.0
        self._half_open_entered_at_monotonic: float = 0.0

        # Half-open permit tracking
        self._half_open_in_flight: int = 0
        self._half_open_successes: int = 0

        # Metrics engine
        self._metrics = SlidingWindowMetrics(
            window_type=self.config.sliding_window_type,
            window_size=self.config.sliding_window_size,
            slow_call_duration_ms=self.config.slow_call_duration_ms,
            clock=self.clock,
        )

        # Event listeners
        self._event_listeners: List[Callable[[StateTransitionEvent], None]] = []

    @property
    def state(self) -> CircuitBreakerState:
        with self._lock:
            self._check_and_update_state()
            return self._state

    def add_event_listener(self, listener: Callable[[StateTransitionEvent], None]) -> None:
        """Register a callback to receive StateTransitionEvents."""
        with self._lock:
            self._event_listeners.append(listener)

    def _emit_transition(self, from_state: CircuitBreakerState, to_state: CircuitBreakerState, reason: str) -> None:
        now = self.clock()
        event = StateTransitionEvent(
            breaker_id=self.id,
            from_state=from_state,
            to_state=to_state,
            timestamp_monotonic=now,
            reason=reason,
            metrics_snapshot=self._metrics.snapshot(now),
        )
        logger.info(
            "[⚡ BREAKER %s] State changed: %s -> %s (Reason: %s)",
            self.id, from_state.value, to_state.value, reason
        )
        if self.cluster_store is not None:
            try:
                snap = self._metrics.snapshot(now)
                self.cluster_store.sync_breaker_state(
                    self.id,
                    to_state.value,
                    error_count=int(snap.get("failure_rate", 0.0)),
                    trip_timestamp=now,
                )
            except Exception as e:
                logger.warning("Error syncing breaker transition to cluster store: %s", e)
        for listener in self._event_listeners:
            try:
                listener(event)
            except Exception as e:
                logger.warning("Error in breaker event listener: %s", e)

    def _check_and_update_state(self) -> None:
        """Evaluate automatic transitions based on monotonic clock and cluster state."""
        now = self.clock()

        # Check for remote cluster trips from other gateway pods
        if self._state == CircuitBreakerState.CLOSED and self.cluster_store is not None:
            try:
                remote = self.cluster_store.get_cluster_breaker_state(self.id)
                if remote and remote.get("state") == CircuitBreakerState.OPEN.value:
                    prev = self._state
                    self._state = CircuitBreakerState.OPEN
                    self._opened_at_monotonic = now
                    self._emit_transition(prev, CircuitBreakerState.OPEN, "Synchronized trip from remote cluster node")
                    return
            except Exception as e:
                logger.warning("Error reading cluster breaker state: %s", e)

        if self._state == CircuitBreakerState.OPEN:
            wait_seconds = self.config.wait_duration_open_ms / 1000.0
            if now >= (self._opened_at_monotonic + wait_seconds):
                # Transition OPEN -> HALF_OPEN
                prev: CircuitBreakerState = self._state
                self._state = CircuitBreakerState.HALF_OPEN
                self._half_open_entered_at_monotonic = now
                self._half_open_in_flight = 0
                self._half_open_successes = 0
                self._emit_transition(prev, CircuitBreakerState.HALF_OPEN, f"Wait duration of {wait_seconds:.1f}s elapsed")

        elif self._state == CircuitBreakerState.HALF_OPEN:
            max_half_open_seconds = self.config.max_half_open_duration_ms / 1000.0
            if (now - self._half_open_entered_at_monotonic) > max_half_open_seconds and self._half_open_in_flight == 0:
                # Probes timed out without success -> return to OPEN
                prev_half_open: CircuitBreakerState = self._state
                self._state = CircuitBreakerState.OPEN
                self._opened_at_monotonic = now
                self._emit_transition(
                    prev_half_open,
                    CircuitBreakerState.OPEN,
                    "Half-open probe timeout elapsed without recovery",
                )

    def acquire_permission(self) -> bool:
        """
        Request permission to execute an upstream call.
        Raises BreakerOpenError if OPEN/FORCED_OPEN or ProbeAdmissionDeniedError if HALF_OPEN permits exhausted.
        """
        with self._lock:
            self._check_and_update_state()

            if self._state == CircuitBreakerState.FORCED_OPEN:
                raise BreakerForcedOpenError(self.id, f"Breaker '{self.id}' is FORCED_OPEN administratively")

            if self._state == CircuitBreakerState.DISABLED or self._state == CircuitBreakerState.METRICS_ONLY:
                return True

            if self._state == CircuitBreakerState.CLOSED:
                return True

            if self._state == CircuitBreakerState.OPEN:
                now = self.clock()
                remaining = max(0.0, (self._opened_at_monotonic + (self.config.wait_duration_open_ms / 1000.0)) - now)
                raise BreakerOpenError(self.id, remaining_wait_seconds=remaining)

            if self._state == CircuitBreakerState.HALF_OPEN:
                if self._half_open_in_flight >= self.config.half_open_max_calls:
                    raise ProbeAdmissionDeniedError(self.id)
                self._half_open_in_flight += 1
                return True

            return False

    def record_success(self, duration_ms: float) -> None:
        """Record successful upstream execution."""
        with self._lock:
            now = self.clock()
            self._check_and_update_state()

            if self._state == CircuitBreakerState.HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                self._half_open_successes += 1
                if self._half_open_successes >= self.config.half_open_max_calls:
                    # Successful probes close the circuit!
                    prev = self._state
                    self._state = CircuitBreakerState.CLOSED
                    self._metrics.reset()
                    self._half_open_in_flight = 0
                    self._half_open_successes = 0
                    self._emit_transition(prev, CircuitBreakerState.CLOSED, f"Probes succeeded ({self._half_open_successes} successful calls)")
                return

            # Record in sliding window
            self._metrics.record(success=True, duration_ms=duration_ms, timestamp_monotonic=now, poisons_health=True)

            # Check slow call rate threshold in CLOSED state
            if self._state == CircuitBreakerState.CLOSED:
                self._evaluate_closed_thresholds(now)

    def record_failure(
        self,
        duration_ms: float,
        error: Optional[Exception] = None,
        failure_classification: Optional[FailureClassification] = None,
    ) -> None:
        """Record failed upstream execution."""
        with self._lock:
            now = self.clock()
            self._check_and_update_state()

            poisons = failure_classification.poisons_health if failure_classification else True

            if self._state == CircuitBreakerState.HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                if poisons:
                    # Failed probe immediately reopens breaker
                    prev = self._state
                    self._state = CircuitBreakerState.OPEN
                    self._opened_at_monotonic = now
                    self._half_open_in_flight = 0
                    self._half_open_successes = 0
                    reason = f"Half-open probe failed: {error or (failure_classification.message if failure_classification else 'error')}"
                    self._emit_transition(prev, CircuitBreakerState.OPEN, reason)
                return

            self._metrics.record(
                success=False,
                duration_ms=duration_ms,
                timestamp_monotonic=now,
                poisons_health=poisons,
            )

            if self._state == CircuitBreakerState.CLOSED and poisons:
                self._evaluate_closed_thresholds(now)

    def _evaluate_closed_thresholds(self, now: float) -> None:
        """Check if CLOSED metrics breach failure or slow-call thresholds."""
        if self._state != CircuitBreakerState.CLOSED:
            return

        snapshot = self._metrics.snapshot(now)
        total = snapshot["total_calls"]
        if total < self.config.minimum_number_of_calls:
            return

        failure_rate = snapshot["failure_rate"]
        slow_rate = snapshot["slow_call_rate"]

        trip_reason = ""
        if failure_rate >= self.config.failure_rate_threshold:
            trip_reason = f"Failure rate {failure_rate:.1f}% >= threshold {self.config.failure_rate_threshold:.1f}% ({snapshot['failed_calls']}/{total} calls)"
        elif slow_rate >= self.config.slow_call_rate_threshold:
            trip_reason = f"Slow call rate {slow_rate:.1f}% >= threshold {self.config.slow_call_rate_threshold:.1f}% ({snapshot['slow_calls']}/{total} calls)"

        if trip_reason:
            prev = self._state
            self._state = CircuitBreakerState.OPEN
            self._opened_at_monotonic = now
            self._emit_transition(prev, CircuitBreakerState.OPEN, trip_reason)

    def force_open(self) -> None:
        """Administratively lock the breaker into FORCED_OPEN state."""
        with self._lock:
            prev = self._state
            self._state = CircuitBreakerState.FORCED_OPEN
            self._emit_transition(prev, CircuitBreakerState.FORCED_OPEN, "Administratively forced OPEN")

    def force_closed(self) -> None:
        """Administratively lock the breaker into CLOSED state."""
        with self._lock:
            prev = self._state
            self._state = CircuitBreakerState.CLOSED
            self._metrics.reset()
            self._emit_transition(prev, CircuitBreakerState.CLOSED, "Administratively forced CLOSED")

    def disable(self) -> None:
        """Administratively disable the breaker (all calls pass)."""
        with self._lock:
            prev = self._state
            self._state = CircuitBreakerState.DISABLED
            self._emit_transition(prev, CircuitBreakerState.DISABLED, "Administratively DISABLED")

    def reset(self) -> None:
        """Reset breaker to default CLOSED state with clean metrics."""
        with self._lock:
            prev = self._state
            self._state = CircuitBreakerState.CLOSED
            self._opened_at_monotonic = 0.0
            self._half_open_in_flight = 0
            self._half_open_successes = 0
            self._metrics.reset()
            if prev != CircuitBreakerState.CLOSED:
                self._emit_transition(prev, CircuitBreakerState.CLOSED, "Reset to CLOSED")

    def snapshot(self) -> Dict[str, Any]:
        """Return full diagnostic snapshot of circuit breaker."""
        with self._lock:
            self._check_and_update_state()
            now = self.clock()
            m_snap = self._metrics.snapshot(now)
            remaining_wait = 0.0
            if self._state == CircuitBreakerState.OPEN:
                remaining_wait = max(0.0, (self._opened_at_monotonic + (self.config.wait_duration_open_ms / 1000.0)) - now)

            return {
                "id": self.id,
                "state": self._state.value,
                "metrics": m_snap,
                "remaining_wait_seconds": remaining_wait,
                "half_open_in_flight": self._half_open_in_flight,
                "half_open_successes": self._half_open_successes,
            }
