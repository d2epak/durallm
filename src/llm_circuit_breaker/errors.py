"""Domain Exceptions for LLM Circuit Breaker Gateway."""

from __future__ import annotations

from typing import Any, Dict, Optional


class CircuitBreakerGatewayError(Exception):
    """Base exception for all LLM Circuit Breaker Gateway errors."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


GatewayError = CircuitBreakerGatewayError
CircuitBreakerError = CircuitBreakerGatewayError


class BreakerOpenError(CircuitBreakerGatewayError):
    """Raised when call is rejected because the target circuit breaker is in OPEN state."""

    def __init__(self, breaker_id: str, message: str = "", remaining_wait_seconds: float = 0.0):
        msg = message or f"Circuit breaker '{breaker_id}' is OPEN (wait time remaining: {remaining_wait_seconds:.1f}s)"
        super().__init__(msg, details={"breaker_id": breaker_id, "remaining_wait_seconds": remaining_wait_seconds})
        self.breaker_id = breaker_id
        self.remaining_wait_seconds = remaining_wait_seconds


class BreakerForcedOpenError(BreakerOpenError):
    """Raised when breaker is administratively locked in FORCED_OPEN state."""


class ProbeAdmissionDeniedError(CircuitBreakerGatewayError):
    """Raised when breaker is in HALF_OPEN state but all probe permits are in use."""

    def __init__(self, breaker_id: str):
        super().__init__(
            f"Circuit breaker '{breaker_id}' is HALF_OPEN and max probe permits are occupied",
            details={"breaker_id": breaker_id}
        )
        self.breaker_id = breaker_id


class DeadlineExceededError(CircuitBreakerGatewayError):
    """Raised when total request deadline or attempt deadline has expired."""

    def __init__(self, message: str, deadline_ms: float = 0.0, elapsed_ms: float = 0.0):
        super().__init__(message, details={"deadline_ms": deadline_ms, "elapsed_ms": elapsed_ms})
        self.deadline_ms = deadline_ms
        self.elapsed_ms = elapsed_ms


class NoHealthyRouteError(CircuitBreakerGatewayError):
    """Raised when candidate selection cannot find any available healthy provider."""

    def __init__(self, message: str = "All candidate providers are unavailable or circuit-broken", pool: str = ""):
        super().__init__(message, details={"pool": pool})
        self.pool = pool


class NoCandidateMatchesError(NoHealthyRouteError):
    """Raised when hard constraints disqualify all registered providers."""


class CycleDetectedError(CircuitBreakerGatewayError):
    """Raised when fallback execution detects a repeating provider/model cycle."""

    def __init__(self, candidate_id: str, history: list[str]):
        super().__init__(
            f"Cycle detected in fallback routing: candidate '{candidate_id}' already attempted in path {history}",
            details={"candidate_id": candidate_id, "history": history}
        )


class NonRecoverableFailureError(CircuitBreakerGatewayError):
    """Upstream failure classified as neither retryable nor eligible for fallback."""

    def __init__(self, message: str, classification: Any = None, endpoint_id: str = ""):
        super().__init__(message, {"endpoint_id": endpoint_id})
        self.classification = classification
        self.endpoint_id = endpoint_id


class RetryBudgetExhaustedError(CircuitBreakerGatewayError):
    """Raised when maximum attempt count or retry budget is exceeded."""


class FallbackBudgetExhaustedError(CircuitBreakerGatewayError):
    """Raised when maximum fallback hops have been exhausted."""


class SchemaValidationError(CircuitBreakerGatewayError):
    """Raised when a request, response, or tool argument schema is invalid."""


class UnsafeToolCallError(CircuitBreakerGatewayError):
    """Raised in strict mode when a tool call has malformed or unresolvable arguments."""

    def __init__(self, tool_name: str, message: str, raw_arguments: str = ""):
        super().__init__(
            f"Unsafe tool call for '{tool_name}': {message}",
            details={"tool_name": tool_name, "raw_arguments": raw_arguments}
        )
        self.tool_name = tool_name
        self.raw_arguments = raw_arguments


class ContextOverflowError(CircuitBreakerGatewayError):
    """Raised when request tokens exceed target context length and cannot be compacted."""

    def __init__(self, required_tokens: int, available_budget: int):
        super().__init__(
            f"Context overflow: required {required_tokens} tokens exceeds available budget {available_budget}",
            details={"required_tokens": required_tokens, "available_budget": available_budget}
        )
        self.required_tokens = required_tokens
        self.available_budget = available_budget


class ProtocolTranslationError(CircuitBreakerGatewayError):
    """Raised when message protocol cannot be converted to or from neutral IR."""


class ConfigurationError(CircuitBreakerGatewayError):
    """Raised on invalid gateway configuration."""
