"""Atomic in-memory agent-budget reservations.

The interface is intentionally small so a durable distributed implementation
can replace it without changing executor semantics.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class BudgetReservation:
    reservation_id: str
    scope: str
    amount_usd: float
    limit_usd: float


class BudgetReservationStore:
    """Serialize budget reservations across concurrent in-process attempts."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._reserved: Dict[str, BudgetReservation] = {}
        self._spent: Dict[str, float] = {}

    def reserve(self, reservation_id: str, scope: str, amount_usd: float, limit_usd: float) -> Optional[BudgetReservation]:
        if amount_usd < 0 or limit_usd < 0:
            raise ValueError("budget amounts must be non-negative")
        with self._lock:
            existing = self._reserved.get(reservation_id)
            if existing is not None:
                return existing
            spent = self._spent.get(scope, 0.0)
            reserved = sum(item.amount_usd for item in self._reserved.values() if item.scope == scope)
            if spent + reserved + amount_usd > limit_usd + 1e-12:
                return None
            reservation = BudgetReservation(reservation_id, scope, amount_usd, limit_usd)
            self._reserved[reservation_id] = reservation
            return reservation

    def release(self, reservation_id: str) -> None:
        with self._lock:
            self._reserved.pop(reservation_id, None)

    def settle(self, reservation_id: str, actual_cost_usd: float) -> None:
        if actual_cost_usd < 0:
            raise ValueError("actual cost must be non-negative")
        with self._lock:
            reservation = self._reserved.pop(reservation_id, None)
            if reservation is None:
                return
            self._spent[reservation.scope] = self._spent.get(reservation.scope, 0.0) + actual_cost_usd

    def remaining(self, scope: str, limit_usd: float) -> float:
        with self._lock:
            spent = self._spent.get(scope, 0.0)
            reserved = sum(item.amount_usd for item in self._reserved.values() if item.scope == scope)
            return max(0.0, limit_usd - spent - reserved)


    def reset(self) -> None:
        with self._lock:
            self._reserved.clear()
            self._spent.clear()


DEFAULT_BUDGET_RESERVATION_STORE = BudgetReservationStore()
