"""Core Domain Data Models for LLM Circuit Breaker Gateway."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class FailureCategory(str, Enum):
    """Hierarchical category of failure."""
    INFRASTRUCTURE = "infrastructure"
    RATE_LIMIT = "rate_limit"
    REQUEST_INCOMPATIBILITY = "request_incompatibility"
    SEMANTIC_AGENT_FAILURE = "semantic_agent_failure"
    CLIENT_FAULT = "client_fault"
    UNKNOWN = "unknown"


class FailoverReason(str, Enum):
    """Structured failover reason (compatible with V1 enum)."""
    rate_limit = "rate_limit"
    upstream_rate_limit = "upstream_rate_limit"
    billing = "billing"
    auth = "auth"
    model_not_found = "model_not_found"
    overloaded = "overloaded"
    server_error = "server_error"
    timeout = "timeout"
    ssl_cert_verification = "ssl_cert_verification"
    payload_too_large = "payload_too_large"
    output_cap_exceeded = "output_cap_exceeded"
    context_overflow = "context_overflow"
    waf_blocked = "waf_blocked"
    connection_refused = "connection_refused"
    schema_incompatible = "schema_incompatible"
    malformed_tool_call = "malformed_tool_call"
    empty_completion = "empty_completion"
    client_error = "client_error"
    unknown = "unknown"


@dataclass
class FailureClassification:
    """Structured failure classification report."""
    category: FailureCategory
    reason: FailoverReason
    should_fallback: bool
    retryable: bool
    poisons_health: bool = True
    is_permanent: bool = False
    status_code: Optional[int] = None
    retry_after_seconds: Optional[float] = None
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


# Backward compatibility subclass so isinstance(c, ClassifiedError) works
@dataclass
class ClassifiedError(FailureClassification):
    """V1-compatible error dataclass."""
    def __init__(
        self,
        reason: FailoverReason,
        should_fallback: bool,
        retryable: bool,
        status_code: Optional[int] = None,
        message: str = "",
        category: FailureCategory = FailureCategory.UNKNOWN,
        poisons_health: bool = True,
        is_permanent: bool = False,
        retry_after_seconds: Optional[float] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            category=category,
            reason=reason,
            should_fallback=should_fallback,
            retryable=retryable,
            poisons_health=poisons_health,
            is_permanent=is_permanent,
            status_code=status_code,
            retry_after_seconds=retry_after_seconds,
            message=message,
            details=details or {},
        )


@dataclass
class AttemptRecord:
    """Audit record for a single execution attempt."""
    attempt_id: str = field(default_factory=lambda: f"att_{uuid.uuid4().hex[:8]}")
    request_id: str = ""
    operation_id: str = ""
    provider: str = ""
    model: str = ""
    endpoint_id: str = ""
    attempt_index: int = 0
    fallback_index: int = 0
    start_time_monotonic: float = field(default_factory=time.monotonic)
    end_time_monotonic: Optional[float] = None
    latency_ms: float = 0.0
    ttft_ms: Optional[float] = None
    status_code: Optional[int] = None
    success: bool = False
    failure: Optional[FailureClassification] = None
    transformed: bool = False
    compacted: bool = False
    tokens_input: int = 0
    tokens_output: int = 0
    estimated_cost_usd: float = 0.0

    def finish(self, success: bool, status_code: Optional[int] = None, failure: Optional[FailureClassification] = None) -> None:
        self.end_time_monotonic = time.monotonic()
        self.latency_ms = max(0.0, (self.end_time_monotonic - self.start_time_monotonic) * 1000.0)
        self.success = success
        self.status_code = status_code
        self.failure = failure
