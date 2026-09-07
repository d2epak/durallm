"""Circuit Breaker Subsystem."""

from durallm.breaker.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
)
from durallm.breaker.metrics import (
    CallOutcome,
    SlidingWindowMetrics,
    SlidingWindowType,
)
from durallm.breaker.registry import (
    DEFAULT_BREAKER_REGISTRY,
    CircuitBreakerRegistry,
)
from durallm.breaker.state import (
    CircuitBreakerState,
    StateTransitionEvent,
)

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitBreakerRegistry",
    "DEFAULT_BREAKER_REGISTRY",
    "CircuitBreakerState",
    "StateTransitionEvent",
    "SlidingWindowMetrics",
    "SlidingWindowType",
    "CallOutcome",
]
