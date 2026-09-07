"""Execution Attempt Ledger and Loop Protection."""

from __future__ import annotations

from typing import Any, List

from durallm.errors import (
    CycleDetectedError,
    FallbackBudgetExhaustedError,
    RetryBudgetExhaustedError,
)
from durallm.execution.policy import ExecutionPolicy
from durallm.models import AttemptRecord


class AttemptLedger:
    """Thread-safe attempt ledger tracking retry/fallback state and preventing cycles."""

    def __init__(self, policy: ExecutionPolicy):
        self.policy = policy
        self.attempts: List[AttemptRecord] = []
        self._endpoints_attempted: List[str] = []
        self._endpoint_counts: dict[str, int] = {}
        self.fallback_count: int = 0
        self.accumulated_cost_usd: float = 0.0
        self.failover_plans: List[Any] = []

    def record_failover_plan(self, plan: Any) -> None:
        self.failover_plans.append(plan)

    def add_cost(self, cost_usd: float) -> None:
        self.accumulated_cost_usd += max(0.0, cost_usd)

    @property
    def total_attempts(self) -> int:
        return len(self.attempts)

    def record_attempt(self, record: AttemptRecord) -> None:
        """Register a new execution attempt."""
        self.attempts.append(record)
        ep = record.endpoint_id
        self._endpoints_attempted.append(ep)
        self._endpoint_counts[ep] = self._endpoint_counts.get(ep, 0) + 1

    def attempts_on(self, endpoint_id: str) -> int:
        """Number of attempts already made against this endpoint."""
        return self._endpoint_counts.get(endpoint_id, 0)

    def can_attempt_endpoint(self, endpoint_id: str) -> bool:
        """Return True if endpoint can be attempted under current retry/fallback budget."""
        # 1. Total attempts check
        if self.total_attempts >= self.policy.max_total_attempts:
            return False

        # 2. Per-endpoint retry budget check
        current_count = self._endpoint_counts.get(endpoint_id, 0)
        if current_count >= self.policy.retry.max_attempts_same_endpoint:
            return False

        return True

    def validate_next_candidate(self, endpoint_id: str) -> None:
        """Verify candidate does not violate cycle prevention or budget limits."""
        if self.total_attempts >= self.policy.max_total_attempts:
            raise RetryBudgetExhaustedError(
                f"Maximum request attempts ({self.policy.max_total_attempts}) exhausted"
            )

        if self.fallback_count >= self.policy.fallback.max_fallback_hops:
            raise FallbackBudgetExhaustedError(
                f"Maximum fallback hops ({self.policy.fallback.max_fallback_hops}) exhausted"
            )

        # Cycle check: A -> B -> A is prohibited unless explicitly allowed
        if len(self._endpoints_attempted) >= 2:
            prev = self._endpoints_attempted[-1]
            if endpoint_id != prev and endpoint_id in self._endpoints_attempted[:-1]:
                raise CycleDetectedError(endpoint_id, list(self._endpoints_attempted))

    def mark_fallback(self) -> None:
        """Increment fallback hops count."""
        self.fallback_count += 1

    def reset_for_rate_limit_retry(self, endpoint_ids: List[str]) -> None:
        """Reset retry counts and cycle history for candidates re-admitted after rate-limit wait."""
        for ep in endpoint_ids:
            self._endpoint_counts[ep] = 0
        self._endpoints_attempted = [ep for ep in self._endpoints_attempted if ep not in endpoint_ids]
        self.fallback_count = 0
        if len(self.attempts) >= self.policy.max_total_attempts:
            self.policy.max_total_attempts = len(self.attempts) + len(endpoint_ids) * 2

