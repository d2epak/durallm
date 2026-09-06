# Comprehensive Failure Taxonomy & Error Classification

An authoritative reference defining failure categorization, failover reasons, and health poisoning rules in **LLM Circuit Breaker (V3)**.

---

## 1. The 7 Failure Categories

| Category | HTTP Statuses / Triggers | Description | Poisons Breaker Health? |
|---|---|---|:---:|
| **`INFRASTRUCTURE`** | 500, 502, 503, 504, 529, Connection Reset, TCP Timeout | Genuine upstream provider outage, crash, or gateway network failure. | **YES** |
| **`RATE_LIMIT`** | 429 | Upstream concurrency or requests-per-minute (RPM) quota exceeded. | **YES** (opens the breaker; `Retry-After` is honoured before any same-endpoint retry). Non-poisoning cooldown is planned once the router honours health-store cooldowns. |
| **`QUOTA_EXHAUSTED`** | 402, 429 (monthly cap), "insufficient credits" | Billing account out of funds or hard organization spending cap hit. | **YES** (classified under `RATE_LIMIT`/`billing`; opening the breaker is currently the only mechanism that excludes the endpoint). |
| **`CAPABILITY_MISMATCH`** | 400 (context overflow), 404 (model deprecated) | Model lacks required feature (e.g. tools, vision) or context window is too small. | **NO** |
| **`REQUEST_INCOMPATIBILITY`** | 400 (rejected schema, protobuf failure) | Upstream provider cannot parse request format (e.g. Gemini rejecting `$schema` key). | **NO** |
| **`SEMANTIC_AGENT_FAILURE`** | 200 (malformed tool JSON, missing required schema args) | Upstream responded with 200 OK, but output was syntactically corrupt or violated tool contract. | **NO** |
| **`CLIENT_ERROR`** | 401, 403, 400 (invalid user prompt) | Client authentication failed or malformed user request. Non-retryable. | **NO** |

---

## 2. Why HTTP 200 is Not Always Healthy

A critical flaw in standard API proxies is treating any HTTP 200 response as a successful call. In LLM operations, HTTP 200 frequently conceals catastrophic semantic failures:

1. **Empty 200 Responses:**
   Under heavy load, providers occasionally return `200 OK` with `{"choices": [{"message": {"content": ""}}]}`. The agent hangs or crashes.
2. **Syntactic Tool Corruptions:**
   The model returns `200 OK` with unparseable arguments: `{"command": "echo 'unclosed string}`.
3. **Schema Contract Violations:**
   The model invents parameter names (`{"query_string": "abc"}` instead of required `{"query": "abc"}`).
4. **Silent Size Bombs:**
   The model generates runaway output loops exceeding 10MB, exhausting gateway memory buffers.

**The Invariant:** Every 200 OK response passes through the `ResponseValidator` before being delivered to caller or ledger. If semantic validation fails, the turn triggers safe fallback without poisoning the provider's infrastructure health.

---

## 3. The 16 Failover Reasons

```python
class FailoverReason(str, Enum):
    # Infrastructure
    server_error = "server_error"
    overloaded = "overloaded"
    timeout = "timeout"
    connection_failed = "connection_failed"
    circuit_breaker_open = "circuit_breaker_open"

    # Rate Limiting & Quotas
    rate_limit = "rate_limit"
    quota_exceeded = "quota_exceeded"
    billing_issue = "billing_issue"

    # Protocol & Semantic
    schema_incompatible = "schema_incompatible"
    malformed_tool_call = "malformed_tool_call"
    empty_response = "empty_response"
    context_overflow = "context_overflow"
    model_not_found = "model_not_found"

    # Policy
    cost_budget_exceeded = "cost_budget_exceeded"
    deadline_exceeded = "deadline_exceeded"
    cycle_prevented = "cycle_prevented"
```
