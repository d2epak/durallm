"""Availability state for independent provider credential/deployment lanes."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional


@dataclass(frozen=True)
class ResourceLaneStatus:
    """A resource lane's current admission state."""

    available: bool
    reason: Optional[str] = None
    expires_at: Optional[float] = None

    def is_available(self, now: float) -> bool:
        return self.available or (self.expires_at is not None and self.expires_at <= now)


class ResourceLaneStore:
    """Thread-safe lane availability overlay used before breaker admission.

    A lane can represent one API credential, regional deployment, or upstream
    quota bucket. It is intentionally distinct from a breaker: a known quota
    exhaustion should move only the affected lane, not every endpoint that
    shares the provider name.
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._statuses: Dict[str, ResourceLaneStatus] = {}

    def set_unavailable(self, lane_key: str, reason: str, retry_after_seconds: Optional[float] = None) -> None:
        expires_at = None if retry_after_seconds is None else self._clock() + max(0.0, retry_after_seconds)
        with self._lock:
            self._statuses[lane_key] = ResourceLaneStatus(False, reason=reason, expires_at=expires_at)

    def set_available(self, lane_key: str) -> None:
        with self._lock:
            self._statuses[lane_key] = ResourceLaneStatus(True)

    def status(self, lane_key: str) -> ResourceLaneStatus:
        with self._lock:
            current = self._statuses.get(lane_key)
            if current is None:
                return ResourceLaneStatus(True)
            if current.is_available(self._clock()):
                if not current.available:
                    self._statuses[lane_key] = ResourceLaneStatus(True)
                return ResourceLaneStatus(True)
            return current


    def reset(self) -> None:
        with self._lock:
            self._statuses.clear()


DEFAULT_RESOURCE_LANE_STORE = ResourceLaneStore()
