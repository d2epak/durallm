"""Retry and Fallback Policies."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional, Set


@dataclass
class RetryPolicy:
    """Policy for retrying on the same provider endpoint."""
    max_attempts_same_endpoint: int = 2
    base_backoff_ms: float = 200.0
    max_backoff_ms: float = 3000.0
    jitter: bool = True
    retry_on_status: Set[int] = field(default_factory=lambda: {408, 429, 500, 502, 503, 504, 529, 599})

    def compute_backoff_seconds(self, attempt_index: int, retry_after: Optional[float] = None) -> float:
        """Compute backoff with exponential increase and jitter, or honor Retry-After."""
        if retry_after is not None and retry_after > 0:
            return retry_after

        # Exponential backoff: base * 2^attempt
        exp = self.base_backoff_ms * (2 ** max(0, attempt_index - 1))
        capped = min(self.max_backoff_ms, exp)

        if self.jitter:
            # Full jitter between 0 and capped
            return (random.uniform(self.base_backoff_ms, capped)) / 1000.0
        return capped / 1000.0


@dataclass
class FallbackPolicy:
    """Policy for failing over to alternative provider endpoints."""
    max_fallback_hops: int = 3
    require_capability_match: bool = True
    avoid_same_provider: bool = False
    avoid_recent_failure: bool = True


@dataclass
class ExecutionPolicy:
    """Combined policy governing request execution."""
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    fallback: FallbackPolicy = field(default_factory=FallbackPolicy)
    max_total_attempts: int = 6
