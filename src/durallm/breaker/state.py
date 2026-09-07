"""Circuit Breaker States and State Transition Events."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict


class CircuitBreakerState(str, Enum):
    """Formal Circuit Breaker States (Resilience4j semantics)."""
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"
    FORCED_OPEN = "FORCED_OPEN"
    DISABLED = "DISABLED"
    METRICS_ONLY = "METRICS_ONLY"


@dataclass(frozen=True)
class StateTransitionEvent:
    """Event emitted whenever a circuit breaker transitions state."""
    breaker_id: str
    from_state: CircuitBreakerState
    to_state: CircuitBreakerState
    timestamp_monotonic: float
    reason: str
    metrics_snapshot: Dict[str, Any] = field(default_factory=dict)
