"""Multi-Dimensional Provider Health Telemetry and Performance Metrics."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class EndpointHealthSnapshot:
    """Multi-dimensional rolling telemetry and health stats for an endpoint."""
    endpoint_id: str
    provider: str
    model: str
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    consecutive_failures: int = 0
    
    # Latency telemetry (None indicates UNKNOWN / Cold Start)
    ema_latency_ms: Optional[float] = None
    last_latency_ms: Optional[float] = None
    ema_ttft_ms: Optional[float] = None
    last_ttft_ms: Optional[float] = None

    # Error breakdown
    timeout_count: int = 0
    rate_limit_429_count: int = 0
    server_error_5xx_count: int = 0
    semantic_failure_count: int = 0

    # Tool reliability
    tool_calls_attempted: int = 0
    tool_calls_succeeded: int = 0
    tool_calls_failed: int = 0

    # Availability & Cooldown
    cooldown_until_monotonic: float = 0.0
    quota_exhausted_until: float = 0.0
    is_dead: bool = False
    dead_reason: Optional[str] = None
    last_error_message: Optional[str] = None
    last_updated: float = field(default_factory=time.time)

    @property
    def is_in_cooldown(self) -> bool:
        return time.monotonic() < self.cooldown_until_monotonic

    @property
    def is_quota_exhausted(self) -> bool:
        return time.time() < self.quota_exhausted_until

    @property
    def is_cold_start(self) -> bool:
        """Return True if endpoint has zero recorded latency observations."""
        return self.ema_latency_ms is None

    @property
    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 1.0
        return self.successful_calls / self.total_calls

    @property
    def tool_success_rate(self) -> float:
        if self.tool_calls_attempted == 0:
            return 1.0
        return self.tool_calls_succeeded / self.tool_calls_attempted

    @property
    def semantic_success_rate(self) -> float:
        if self.successful_calls == 0:
            return 1.0
        return max(0.0, 1.0 - (self.semantic_failure_count / self.successful_calls))

    @property
    def timeout_rate(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return self.timeout_count / self.total_calls

    @property
    def rate_limit_rate(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return self.rate_limit_429_count / self.total_calls


class HealthTelemetryStore:
    """Thread-safe store for multi-dimensional provider health telemetry."""

    def __init__(self, ema_alpha: float = 0.2):
        self.ema_alpha = ema_alpha
        self._lock = threading.RLock()
        self._snapshots: Dict[str, EndpointHealthSnapshot] = {}

    def get_or_create(self, endpoint_id: str, provider: str = "", model: str = "") -> EndpointHealthSnapshot:
        with self._lock:
            if endpoint_id not in self._snapshots:
                self._snapshots[endpoint_id] = EndpointHealthSnapshot(
                    endpoint_id=endpoint_id,
                    provider=provider,
                    model=model,
                )
            return self._snapshots[endpoint_id]

    def record_success(self, endpoint_id: str, latency_ms: float, ttft_ms: Optional[float] = None) -> None:
        with self._lock:
            snap = self.get_or_create(endpoint_id)
            snap.total_calls += 1
            snap.successful_calls += 1
            snap.consecutive_failures = 0
            snap.last_latency_ms = latency_ms

            # EMA latency calculation (or initialize cold start)
            if snap.ema_latency_ms is None:
                snap.ema_latency_ms = latency_ms
            else:
                snap.ema_latency_ms = (self.ema_alpha * latency_ms) + ((1.0 - self.ema_alpha) * snap.ema_latency_ms)

            if ttft_ms is not None:
                snap.last_ttft_ms = ttft_ms
                if snap.ema_ttft_ms is None:
                    snap.ema_ttft_ms = ttft_ms
                else:
                    snap.ema_ttft_ms = (self.ema_alpha * ttft_ms) + ((1.0 - self.ema_alpha) * snap.ema_ttft_ms)

            snap.last_updated = time.time()

    def mark_dead(self, endpoint_id: str, provider: str = "", model: str = "", reason: str = "") -> None:
        """Permanently mark an endpoint as dead/blacklisted."""
        with self._lock:
            snap = self.get_or_create(endpoint_id, provider=provider, model=model)
            snap.is_dead = True
            snap.dead_reason = reason
            snap.last_error_message = reason
            snap.last_updated = time.time()

    def clear_dead_list(self) -> None:
        """Reset permanent dead markings on all endpoint snapshots."""
        with self._lock:
            for snap in self._snapshots.values():
                snap.is_dead = False
                snap.dead_reason = None

    def record_failure(
        self,
        endpoint_id: str,
        latency_ms: float,
        status_code: Optional[int] = None,
        error_message: str = "",
        cooldown_seconds: Optional[float] = None,
        quota_exhausted_seconds: Optional[float] = None,
        is_permanent: bool = False,
    ) -> None:
        with self._lock:
            snap = self.get_or_create(endpoint_id)
            snap.total_calls += 1
            snap.failed_calls += 1
            snap.consecutive_failures += 1
            snap.last_latency_ms = latency_ms
            snap.last_error_message = error_message
            snap.last_updated = time.time()

            if is_permanent:
                snap.is_dead = True
                snap.dead_reason = error_message

            if status_code in (408, 504) or "timeout" in error_message.lower():
                snap.timeout_count += 1
            elif status_code == 429:
                snap.rate_limit_429_count += 1
            elif status_code and 500 <= status_code <= 599:
                snap.server_error_5xx_count += 1

            if cooldown_seconds and cooldown_seconds > 0:
                snap.cooldown_until_monotonic = time.monotonic() + cooldown_seconds

            if quota_exhausted_seconds and quota_exhausted_seconds > 0:
                snap.quota_exhausted_until = time.time() + quota_exhausted_seconds

    def record_tool_outcome(self, endpoint_id: str, success: bool) -> None:
        with self._lock:
            snap = self.get_or_create(endpoint_id)
            snap.tool_calls_attempted += 1
            if success:
                snap.tool_calls_succeeded += 1
            else:
                snap.tool_calls_failed += 1
            snap.last_updated = time.time()

    def record_semantic_failure(self, endpoint_id: str) -> None:
        with self._lock:
            snap = self.get_or_create(endpoint_id)
            snap.semantic_failure_count += 1
            snap.last_updated = time.time()

    def all_snapshots(self) -> Dict[str, EndpointHealthSnapshot]:
        with self._lock:
            return dict(self._snapshots)


DEFAULT_HEALTH_STORE = HealthTelemetryStore()
