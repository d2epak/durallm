"""Gateway Configuration Loader and Models."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from durallm.breaker.circuit_breaker import CircuitBreakerConfig
from durallm.execution.policy import ExecutionPolicy, FallbackPolicy, RetryPolicy


@dataclass
class GatewayConfig:
    """Production configuration for the LLM Circuit Breaker Gateway."""
    host: str = "127.0.0.1"
    port: int = 4001
    default_pool: str = "general_agent"
    default_strategy: str = "balanced"
    deadline_ms: float = 60000.0
    streaming_mode: str = "true_streaming"
    log_level: str = "INFO"

    # Circuit breaker settings
    breaker_failure_rate_threshold: float = 50.0
    breaker_sliding_window_size: int = 10
    breaker_wait_duration_in_open: float = 30.0
    breaker_half_open_calls: int = 3

    # Policy settings
    retry_max_same_endpoint: int = 2
    fallback_max_hops: int = 3
    max_total_attempts: int = 6

    def to_breaker_config(self) -> CircuitBreakerConfig:
        return CircuitBreakerConfig(
            failure_rate_threshold=self.breaker_failure_rate_threshold,
            sliding_window_size=self.breaker_sliding_window_size,
            wait_duration_open_ms=self.breaker_wait_duration_in_open * 1000.0,
            half_open_max_calls=self.breaker_half_open_calls,
        )

    def to_execution_policy(self) -> ExecutionPolicy:
        return ExecutionPolicy(
            retry=RetryPolicy(max_attempts_same_endpoint=self.retry_max_same_endpoint),
            fallback=FallbackPolicy(max_fallback_hops=self.fallback_max_hops),
            max_total_attempts=self.max_total_attempts,
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> GatewayConfig:
        allowed = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in allowed}
        return cls(**filtered)

    @classmethod
    def from_env(cls) -> GatewayConfig:
        """Load configuration from LLM_BREAKER_* and GATEWAY_* environment variables."""
        return cls(
            host=os.environ.get("LLM_BREAKER_HOST", os.environ.get("HOST", "127.0.0.1")),
            port=int(os.environ.get("LLM_BREAKER_PORT", os.environ.get("PORT", "4001"))),
            default_pool=os.environ.get("LLM_BREAKER_DEFAULT_POOL", "general_agent"),
            default_strategy=os.environ.get("LLM_BREAKER_STRATEGY", "balanced"),
            deadline_ms=float(os.environ.get("LLM_BREAKER_DEADLINE_MS", "60000.0")),
            streaming_mode=os.environ.get("LLM_BREAKER_STREAMING_MODE", "true_streaming"),
            log_level=os.environ.get("LLM_BREAKER_LOG_LEVEL", "INFO"),
            breaker_failure_rate_threshold=float(os.environ.get("LLM_BREAKER_FAILURE_THRESHOLD", "50.0")),
            breaker_sliding_window_size=int(os.environ.get("LLM_BREAKER_WINDOW_SIZE", "10")),
            breaker_wait_duration_in_open=float(os.environ.get("LLM_BREAKER_WAIT_OPEN", "30.0")),
            breaker_half_open_calls=int(os.environ.get("LLM_BREAKER_HALF_OPEN_CALLS", "3")),
            retry_max_same_endpoint=int(os.environ.get("LLM_BREAKER_RETRY_MAX", "2")),
            fallback_max_hops=int(os.environ.get("LLM_BREAKER_FALLBACK_MAX", "3")),
            max_total_attempts=int(os.environ.get("LLM_BREAKER_MAX_TOTAL_ATTEMPTS", "6")),
        )

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> GatewayConfig:
        """Load from file if provided/exists, overlaid with environment variables."""
        path = config_path or os.environ.get("LLM_BREAKER_CONFIG")
        if path and Path(path).exists():
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
                try:
                    data = json.loads(raw)
                except Exception:
                    # Fallback to simple parser if json fails
                    data = {}
            cfg = cls.from_dict(data)
            return cfg
        return cls.from_env()
