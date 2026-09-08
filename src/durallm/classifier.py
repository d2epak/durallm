"""API Error Classifier for LLM Provider Calls.

Maps HTTP status codes, exception payloads, and network errors into
structured failure classifications, preserving the V1 interface while
providing hierarchical taxonomy for the V2 Circuit Breaker and Routing engines.
"""

from __future__ import annotations

import email.utils
import re
import time
from typing import Any, Dict, Optional

from durallm.models import (
    ClassifiedError,
    FailoverReason,
    FailureCategory,
    FailureClassification,
)

_BILLING_PATTERNS = [
    "insufficient credits",
    "credit balance",
    "billing",
    "payment required",
    "out of credits",
    "usage limit reached",
    "monthly spending cap",
    "insufficient_quota",
    "quota_exceeded",
    "account_deactivated",
    "balance is too low",
    "free-models-per-day",
    "free model requests",
    "add 5 credits",
]

_DEPRECATION_PATTERNS = [
    "deprecated",
    "decommissioned",
    "model does not exist",
    "model not found",
    "model_not_found",
    "has been removed",
    "is no longer available",
    "is not found for api version",
    "end of life",
    "eol",
    "has reached its end of life",
    "sunset",
    "no longer supported",
]

_UPSTREAM_429_PATTERNS = [
    "temporarily rate-limited upstream",
    "upstream_provider_shared_pool",
    "provider returned error",
    "daily",
    "quota exceeded",
]

_WAF_PATTERNS = [
    "cloudflare",
    "just a moment...",
    "challenge-running",
    "ddos protection",
    "ray id",
    "attention required! | cloudflare",
    "security verification",
]

_SSL_PATTERNS = [
    "certificate verify failed",
    "self signed certificate",
    "sslcertverificationerror",
    "sslerror",
]

_OUTPUT_CAP_PATTERNS = [
    "must be less than or equal to",
    "less than or equal to",
    "range of max_tokens should be",
    "maximum output tokens",
    "available_tokens",
    "available tokens",
    "max_tokens is less than the context_window",
    "on output tokens",
    "output tokens per minute",
    "otpm",
]

_CONTEXT_OVERFLOW_PATTERNS = [
    "context_length_exceeded",
    "payload too large",
    "maximum context length",
    "token count exceeds limit",
    "request size exceeds",
    "too many tokens",
    "prompt is too long",
    "prompt too long",
]

_TPM_RATE_LIMIT_PATTERNS = [
    "tokens per minute",
    "tpm limit",
    "input tokens per minute",
    "itpm",
]


def parse_retry_after_from_body(error_msg: str) -> Optional[float]:
    """Extract retry-after delay in seconds from provider error body (e.g. Groq 'Please try again in 5.14s.')."""
    import re
    msg = str(error_msg).lower()
    m = re.search(r"try again in\s+([0-9.]+)\s*s", msg)
    if m:
        try:
            return max(0.1, float(m.group(1)))
        except ValueError:
            pass
    return None


def parse_output_cap_from_error(error_msg: str) -> Optional[int]:
    """Extract integer output token limit from provider error strings."""
    import re

    msg = str(error_msg).lower()

    # 1. Groq / generic OTPM limit: "output tokens per minute (OTPM): Limit 1000"
    m = re.search(r"(?:output tokens per minute|otpm)[^0-9]*limit\s*(\d+)", msg)
    if m:
        return int(m.group(1))

    # 2. Groq / generic less than or equal:
    # `max_tokens` must be less than or equal to `16384`
    m = re.search(r"max_tokens[`'\"]?\s+must be less than or equal to\s+[`'\"]?(\d+)", msg)
    if m:
        return int(m.group(1))

    m = re.search(r"less than or equal to\s+[`'\"]?(\d+)", msg)
    if m and ("max_tokens" in msg or "token" in msg):
        return int(m.group(1))

    # 3. DashScope / Alibaba range: "Range of max_tokens should be [1, 65536]"
    m = re.search(r"range of max_tokens should be\s*\[\s*\d+\s*,\s*(\d+)\s*\]", msg)
    if m:
        return int(m.group(1))

    # 4. Model maximum output tokens: "exceeds model's maximum output tokens (65536)"
    m = re.search(r"exceeds model(?:'s)? maximum output tokens\s*\(?\s*(\d+)\s*\)?", msg)
    if m:
        return int(m.group(1))

    # 5. Anthropic / OpenRouter available_tokens
    m = re.search(r"available[_\s]+tokens[:\s]+(\d+)", msg)
    if m:
        return int(m.group(1))

    return None

_SCHEMA_INCOMPATIBILITY_PATTERNS = [
    "unrecognized parameter",
    "unknown field",
    "additionalproperties",
    "$schema",
    "invalid_request_error",
    "unknown parameter",
    "schema validation error",
]

_TOOL_MALFORMED_PATTERNS = [
    "malformed tool call",
    "failed to parse function arguments",
    "invalid json in tool arguments",
    "invalid_tool_call",
    "missing required argument",
    "missing required parameter",
    "invalid tool argument",
    "unknown tool",
    "schema validation failed",
]

_CLIENT_FAULT_PATTERNS = [
    "invalid api key",
    "unauthorized",
    "incorrect api key",
    "bad request syntax",
    "malformed json request",
]


def parse_retry_after(header_val: Optional[str]) -> Optional[float]:
    """Parse HTTP Retry-After header value (seconds integer or HTTP date string)."""
    if not header_val:
        return None
    val = header_val.strip()
    try:
        return max(0.0, float(val))
    except ValueError:
        pass
    try:
        parsed_tuple = email.utils.parsedate_tz(val)
        if parsed_tuple:
            timestamp = email.utils.mktime_tz(parsed_tuple)
            diff = timestamp - time.time()
            return max(0.0, diff)
    except Exception:
        pass
    return None


def classify_api_error(
    error: Any,
    status_code: Optional[int] = None,
    headers: Optional[Dict[str, str]] = None,
    pool: Optional[str] = None,
    route_id: Optional[str] = None,
) -> ClassifiedError:
    """Classify an exception or response into a structured ClassifiedError (V1/V2 compatible)."""
    classification = classify_failure(error, status_code=status_code, headers=headers)
    if classification.reason == FailoverReason.billing and pool and route_id:
        try:
            from durallm.pools import POOL_MANAGER
            seconds = float(classification.retry_after_seconds or 86400.0)
            POOL_MANAGER.mark_quota_exhausted(pool, route_id, seconds=seconds)
        except Exception:
            pass
    elif (classification.reason == FailoverReason.model_not_found or classification.is_permanent) and pool and route_id:
        try:
            from durallm.pools import POOL_MANAGER
            POOL_MANAGER.mark_deprecated(pool, route_id)
        except Exception:
            pass
    return ClassifiedError(
        reason=classification.reason,
        should_fallback=classification.should_fallback,
        retryable=classification.retryable,
        status_code=classification.status_code,
        message=classification.message,
        category=classification.category,
        poisons_health=classification.poisons_health,
        is_permanent=classification.is_permanent,
        retry_after_seconds=classification.retry_after_seconds,
        details=classification.details,
    )


def classify_failure(
    error: Any,
    status_code: Optional[int] = None,
    headers: Optional[Dict[str, str]] = None,
) -> FailureClassification:
    """Comprehensive failure classification mapping errors to categories and health impacts."""
    code = status_code
    if code is None and hasattr(error, "status_code"):
        code = getattr(error, "status_code")
    if code is None and hasattr(error, "code"):
        code = getattr(error, "code")
    if code is None and hasattr(error, "response") and hasattr(error.response, "status_code"):
        code = error.response.status_code

    msg = str(error).lower()
    if hasattr(error, "body") and isinstance(error.body, dict):
        msg += " " + str(error.body).lower()

    # Normalize headers
    clean_headers = {k.lower(): v for k, v in (headers or {}).items()}
    retry_after = parse_retry_after(clean_headers.get("retry-after"))

    # 1. SSL / TLS Verification Failures (Infrastructure)
    if any(p in msg for p in _SSL_PATTERNS) or "sslerror" in type(error).__name__.lower():
        return FailureClassification(
            category=FailureCategory.INFRASTRUCTURE,
            reason=FailoverReason.ssl_cert_verification,
            should_fallback=True,
            retryable=False,
            poisons_health=True,
            status_code=code,
            message=msg,
        )

    # 2. Connection Refused / Socket Refusal (Infrastructure)
    if "connection refused" in msg or "errno 61" in msg or "errno 111" in msg:
        return FailureClassification(
            category=FailureCategory.INFRASTRUCTURE,
            reason=FailoverReason.connection_refused,
            should_fallback=True,
            retryable=False,
            poisons_health=True,
            status_code=599,
            message=msg,
        )

    # 3. Output Token Cap Exceeded (Request Incompatibility / Output Sizing)
    if (code in (400, 429) or code is None) and any(p in msg for p in _OUTPUT_CAP_PATTERNS):
        return FailureClassification(
            category=FailureCategory.REQUEST_INCOMPATIBILITY,
            reason=FailoverReason.output_cap_exceeded,
            should_fallback=True,
            retryable=True,
            poisons_health=False,  # Output cap mismatch does not mean provider infra is down!
            status_code=code or 400,
            message=msg,
        )

    # 3b. Empty Response Body / Empty Completion (Semantic / Upstream Glitch)
    if "empty response body" in msg or "empty completion" in msg:
        return FailureClassification(
            category=FailureCategory.SEMANTIC_AGENT_FAILURE,
            reason=FailoverReason.empty_completion,
            should_fallback=True,
            retryable=True,
            poisons_health=False,
            status_code=code or 502,
            message=msg,
        )

    # 3b. Groq TPM Rate Limit (Groq returns HTTP 413 for tokens per minute rate limits)
    if ("tokens per minute" in msg or "tpm" in msg or "try again in" in msg) and ("rate" in msg or "limit" in msg or code in (413, 429)):
        delay = retry_after
        if not delay:
            m_wait = re.search(r"try again in (\d+(?:\.\d+)?)s?", msg)
            if m_wait:
                try:
                    delay = float(m_wait.group(1))
                except ValueError:
                    delay = 3.0
            else:
                delay = 3.0
        return FailureClassification(
            category=FailureCategory.RATE_LIMIT,
            reason=FailoverReason.rate_limit,
            should_fallback=True,
            retryable=True,
            poisons_health=True,
            is_permanent=False,
            status_code=code or 429,
            retry_after_seconds=delay,
            message=msg,
        )

    # 4. Context Length / Payload Overflow (Request Incompatibility / Sizing)
    if code == 413 or any(p in msg for p in _CONTEXT_OVERFLOW_PATTERNS):
        return FailureClassification(
            category=FailureCategory.REQUEST_INCOMPATIBILITY,
            reason=FailoverReason.payload_too_large,
            should_fallback=True,
            retryable=False,
            poisons_health=False,  # Context overflow does NOT poison provider health!
            status_code=413,
            message=msg,
        )

    # 4a. OpenRouter Daily Free Tier Quota Lockout (free-models-per-day)
    if "free-models-per-day" in msg:
        return FailureClassification(
            category=FailureCategory.RATE_LIMIT,
            reason=FailoverReason.billing,
            should_fallback=True,
            retryable=False,
            poisons_health=False,
            is_permanent=False,
            status_code=code or 429,
            retry_after_seconds=86400.0,
            message=msg,
        )

    # 4b. HTTP 402 Billing / Credits Exhaustion (Rate/Quota Limit)
    if code == 402 or any(p in msg for p in _BILLING_PATTERNS):
        return FailureClassification(
            category=FailureCategory.RATE_LIMIT,
            reason=FailoverReason.billing,
            should_fallback=True,
            retryable=False,
            poisons_health=True,
            is_permanent=True,
            status_code=402,
            message=msg,
        )

    # 5. HTTP 429 Rate Limits / TPM Quotas (Rate Limit)
    if code == 429 or any(p in msg for p in _TPM_RATE_LIMIT_PATTERNS) or "ratelimit" in type(error).__name__.lower():
        is_upstream_shared = any(p in msg for p in _UPSTREAM_429_PATTERNS)
        parsed_delay = parse_retry_after_from_body(msg)
        delay = retry_after or parsed_delay
        if delay is None and any(p in msg for p in _TPM_RATE_LIMIT_PATTERNS):
            delay = 60.0  # TPM limits reset at the 60-second window
        return FailureClassification(
            category=FailureCategory.RATE_LIMIT,
            reason=FailoverReason.upstream_rate_limit if is_upstream_shared else FailoverReason.rate_limit,
            should_fallback=True,
            retryable=True,
            poisons_health=True,
            status_code=429,
            retry_after_seconds=delay,
            message=msg,
        )

    # 6. HTTP 404 / 410 Model Deprecation, Sunset or End of Life
    if (code in (404, 410, 400) and any(p in msg for p in _DEPRECATION_PATTERNS)) or code == 410:
        return FailureClassification(
            category=FailureCategory.REQUEST_INCOMPATIBILITY,
            reason=FailoverReason.model_not_found,
            should_fallback=True,
            retryable=False,
            poisons_health=False,  # Specific model missing does not imply entire provider is dead
            is_permanent=True,
            status_code=code or 410,
            message=msg,
        )
    if code == 404:
        return FailureClassification(
            category=FailureCategory.REQUEST_INCOMPATIBILITY,
            reason=FailoverReason.model_not_found,
            should_fallback=True,
            retryable=False,
            poisons_health=False,
            is_permanent=True,
            status_code=404,
            message=msg,
        )

    # 7. Semantic Tool / Schema Failures
    if any(p in msg for p in _TOOL_MALFORMED_PATTERNS):
        return FailureClassification(
            category=FailureCategory.SEMANTIC_AGENT_FAILURE,
            reason=FailoverReason.malformed_tool_call,
            should_fallback=True,
            retryable=False,
            poisons_health=False,  # Model produced bad tool call, provider infra is healthy
            status_code=code or 200,
            message=msg,
        )

    # 8. Request Schema Incompatibility (e.g. Gemini protobuf rejected $schema)
    if code == 400 and any(p in msg for p in _SCHEMA_INCOMPATIBILITY_PATTERNS):
        return FailureClassification(
            category=FailureCategory.REQUEST_INCOMPATIBILITY,
            reason=FailoverReason.schema_incompatible,
            should_fallback=True,
            retryable=False,
            poisons_health=False,  # Incompatible schema does not mean provider is down
            status_code=400,
            message=msg,
        )

    # 9. Upstream / Client Auth Failures (401, 403)
    if code in (401, 403) or "access denied" in msg or "forbidden" in msg or any(p in msg for p in _CLIENT_FAULT_PATTERNS):
        is_perm = code in (401, 403) or any(k in msg for k in ("access denied", "forbidden", "invalid api key", "incorrect api key", "unauthorized"))
        return FailureClassification(
            category=FailureCategory.CLIENT_FAULT if any(p in msg for p in _CLIENT_FAULT_PATTERNS) else FailureCategory.INFRASTRUCTURE,
            reason=FailoverReason.auth,
            should_fallback=True,  # Fallback to alternate provider with valid key
            retryable=False,
            poisons_health=False,  # Bad credentials do NOT mean provider is down
            is_permanent=is_perm,
            status_code=code or 403,
            message=msg,
        )

    # 10. WAF / Cloudflare Challenge (Infrastructure)
    if any(p in msg for p in _WAF_PATTERNS):
        return FailureClassification(
            category=FailureCategory.INFRASTRUCTURE,
            reason=FailoverReason.waf_blocked,
            should_fallback=True,
            retryable=False,
            poisons_health=True,
            status_code=code or 403,
            message=msg,
        )

    # 11. Upstream Server Errors 5xx / 529 / 408 (Infrastructure)
    if code in (408, 500, 502, 503, 504, 529) or "overloaded" in msg or "bad gateway" in msg or "gateway timeout" in msg:
        is_overload = code in (503, 529) or "overloaded" in msg
        return FailureClassification(
            category=FailureCategory.INFRASTRUCTURE,
            reason=FailoverReason.overloaded if is_overload else FailoverReason.server_error,
            should_fallback=True,
            retryable=True,
            poisons_health=True,
            status_code=code,
            message=msg,
        )

    # 12. Network Timeouts (Infrastructure)
    if "timeout" in msg or "timed out" in msg or "connecttimeout" in type(error).__name__.lower():
        return FailureClassification(
            category=FailureCategory.INFRASTRUCTURE,
            reason=FailoverReason.timeout,
            should_fallback=True,
            retryable=True,
            poisons_health=True,
            status_code=code or 504,
            message=msg,
        )

    # Any other 4xx: the provider rejected this request but is itself up.
    # Never poison health, never retry the same endpoint; let routing try an alternative.
    if code is not None and 400 <= code < 500:
        return FailureClassification(
            category=FailureCategory.REQUEST_INCOMPATIBILITY,
            reason=FailoverReason.client_error,
            should_fallback=True,
            retryable=False,
            poisons_health=False,
            status_code=code,
            message=msg,
        )

    # Fallback to Unknown (5xx-like or no status): try elsewhere, but count against health.
    return FailureClassification(
        category=FailureCategory.UNKNOWN,
        reason=FailoverReason.unknown,
        should_fallback=True,
        retryable=True,
        poisons_health=True,
        status_code=code,
        message=msg,
    )
