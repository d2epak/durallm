"""Circuit Breaker Registry."""

from __future__ import annotations

import threading
from typing import Dict, Optional

from durallm.breaker.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
)


class CircuitBreakerRegistry:
    """Thread-safe registry for managing circuit breakers by ID."""

    def __init__(self, default_config: Optional[CircuitBreakerConfig] = None):
        self.default_config = default_config or CircuitBreakerConfig()
        self._lock = threading.RLock()
        self._breakers: Dict[str, CircuitBreaker] = {}

    def get_or_create(
        self,
        breaker_id: str,
        config: Optional[CircuitBreakerConfig] = None,
    ) -> CircuitBreaker:
        """Fetch existing circuit breaker or create a new one."""
        with self._lock:
            if breaker_id not in self._breakers:
                self._breakers[breaker_id] = CircuitBreaker(
                    breaker_id=breaker_id,
                    config=config or self.default_config,
                )
            return self._breakers[breaker_id]

    def get(self, breaker_id: str) -> Optional[CircuitBreaker]:
        """Fetch breaker if exists."""
        with self._lock:
            return self._breakers.get(breaker_id)

    def all(self) -> Dict[str, CircuitBreaker]:
        """Return shallow copy of all registered breakers."""
        with self._lock:
            return dict(self._breakers)

    def reset_all(self) -> None:
        """Reset all registered breakers to clean CLOSED state."""
        with self._lock:
            for b in self._breakers.values():
                b.reset()


DEFAULT_BREAKER_REGISTRY = CircuitBreakerRegistry()
