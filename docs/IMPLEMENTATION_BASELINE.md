# Implementation Baseline: LLM Circuit Breaker (V1 -> V2 Audit)

**Date:** 2026-09-03  
**Auditor:** Antigravity (Principal Engineer)  
**Target Specification:** `DURALLM_V2_LONG_HORIZON_SPEC.md`  
**Current Repository Commit:** `1fc6f93` (v0.2.0 prototype)

---

## 1. Executive Summary

The existing repository (`durallm` v0.2.0) is a functional prototype proxy that translates requests between Anthropic's Messages API and OpenAI's Chat Completions API, targeting free-tier open-weights models and aggregators.

While it addresses several practical integration issues (such as Google AI Studio protobuf sanitization and basic JSON markdown stripping), **it does not yet implement a formal circuit breaker**. Instead, it implements simple cooldown timers mislabeled as a circuit breaker. Furthermore, model routing relies on static lists and round-robin index bumping rather than capability matching; context pruning relies on naive character heuristics; tool repair violates semantic safety by synthesizing missing fields; socket timeouts mutate global process state; and streaming is strictly synthetic (buffered).

V2 requires evolving this prototype into a production-grade, self-hostable **agent-resilience gateway** with true circuit breakers, capability-aware routing, normalized protocol IR, strict tool validation, agent semantic state preservation, budget-aware context compaction, explicit streaming policies, structured observability, deterministic fault injection, and reproducible benchmarks.

---

## 2. Source Modules Audit

| Module | Lines | Responsibilities | Current Limitations & Defects |
|---|---|---|---|
| `__init__.py` | 70 | Public export surface | Exports prototype functions and singleton `POOL_MANAGER`. |
| `classifier.py` | 159 | Maps errors/status codes to `FailoverReason` & `ClassifiedError` | String pattern matching on error messages. Lacks formal taxonomy separating client faults, provider infrastructure, request incompatibility, rate limits, and agent semantic failures. |
| `discovery.py` | 256 | Scrapes OpenRouter catalog for free tool-supporting models | Synchronous HTTP calls via `urllib.request`. Writes to `~/.hermes/model_catalog.json` by default. No capability probes or evidence validation. |
| `pools.py` | 353 | Static route lists (`coding`, `general_agent`), env key scanning, cooldown map | **Not a circuit breaker.** Only stores `(pool, provider) -> monotonic_expiry`. Clears oldest cooldown when all candidates are locked out. Unbounded recovery logic. Global singleton mutable state (`POOL_MANAGER`). |
| `proxy.py` | 325 | `ThreadingHTTPServer` handler for `/v1/messages`, `/v1/chat/completions`, `/health` | Instantiates global `ROUTER = UniversalFailoverRouter(auto_discover_free=True)` which triggers live network calls on import. Only supports synthetic SSE streaming. Missing correlation headers/tracing. |
| `pruner.py` | 138 | Truncates `tool_result` / `role: tool` content | Rough character heuristic `(len + 3) // 4` for tokens. Hardcoded preservation of 6 turns. Does not compute model token budgets or reserve output safety margins. |
| `router.py` | 253 | `UniversalFailoverRouter` and `execute_upstream_request` | Sets `socket.setdefaulttimeout(25)` globally! Places API keys in URLs for Gemini REST (`?key=...`). Hardcoded retry loops (`max_attempts=8`) without jitter, backoff, or deadline tracking. Round-robin candidate selection only. |
| `translators.py` | 388 | Anthropic ↔ OpenAI translation, Gemini protobuf cleaning, JSON repair | Direct pairwise translation (N² problem). `repair_json_string` synthesizes arbitrary keys (`{"command": ...}` or `{"text": ...}`), violating Rule 3 (semantic guessing). |

---

## 3. Public API Surface & Backward Compatibility Constraints

The following public APIs are exported in `durallm/__init__.py` and documented in `README.md`:

1. **Router & Execution**:
   - `UniversalFailoverRouter`: `dispatch(pool, openai_payload, requested_model, max_attempts)` -> `(status, response, route)`
   - `active_provider`, `mark_cooldown(provider, seconds, pool)`, `mark_deprecated(model, pool)`, `get_next_available_route(reason, pool)`
2. **Error Classification**:
   - `classify_api_error(error, status_code)` -> `ClassifiedError`
   - `FailoverReason` enum (`rate_limit`, `billing`, `auth`, `model_not_found`, `overloaded`, `server_error`, `timeout`, `ssl_cert_verification`, `payload_too_large`, `waf_blocked`, `connection_refused`, `unknown`)
   - `ClassifiedError` dataclass (`reason`, `should_fallback`, `retryable`, `status_code`, `message`)
3. **Pools & Routes**:
   - `POOL_MANAGER` (singleton instance of `IsolatedPoolManager`)
   - `IsolatedPoolManager`: `get_candidate_routes(pool)`, `select_route(pool, requested_model)`, `mark_cooldown(...)`, `mark_deprecated(...)`, `mark_quota_exhausted(...)`
   - `RouteDefinition`: dataclass with `id`, `provider`, `model`, `pool`, `base_url`, `api_format`, `env_key`, `context_length`, `max_output_tokens`, `headers`, `is_discovered`
4. **Context Compaction**:
   - `prune_anthropic_request(request, max_context_tokens, safety_margin_tokens)`
   - `prune_openai_request(request, max_context_tokens, safety_margin_tokens)`
   - `estimate_tokens(payload)`
5. **Protocol Translation**:
   - `anthropic_to_openai_request(anthropic_req, model_name)`
   - `openai_to_anthropic_response(openai_resp, requested_model)`
   - `clean_gemini_schema(schema)`
   - `convert_openai_to_gemini_payload(openai_req)`
   - `convert_gemini_to_openai_response(gemini_resp, model_name)`
   - `repair_json_string(raw)`
6. **Discovery**:
   - `discover_free_models(min_context, timeout)`
   - `is_model_free(pricing)`
   - `supports_tool_calling(item)`
7. **Server & Handlers**:
   - `CircuitBreakerGatewayHandler` (`BaseHTTPRequestHandler`)
   - `start_proxy_server(host, port)`
   - `create_proxy_app()` (FastAPI ASGI factory)
   - Scripts: `llm-proxy`, `llm-discover`

**Compatibility Rule**: All existing public functions and class interfaces must be preserved as compatibility wrappers delegating to the new modular V2 subsystems.

---

## 4. Current Test Suite Analysis

All 14 existing unit tests pass:
- `tests/test_classifier.py`: 1 test verifying error status codes and exception messages against `FailoverReason`.
- `tests/test_discovery.py`: 2 tests mocking `fetch_openrouter_catalog` and verifying `is_model_free` and `supports_tool_calling`.
- `tests/test_pools.py`: 3 tests verifying pool selection independence, cooldown isolation, and unexported key bypass.
- `tests/test_proxy.py`: 1 test verifying synthetic Anthropic SSE event generation.
- `tests/test_pruner.py`: 2 tests verifying character truncation in historical messages.
- `tests/test_router.py`: 1 test checking failover and cooldown progression.
- `tests/test_translators.py`: 4 tests checking schema cleaning, JSON backtick repair, Anthropic -> OpenAI request translation, and OpenAI -> Anthropic response translation.

### Deficiencies in Test Suite
- No tests for true circuit breaker state transitions (`CLOSED` -> `OPEN` -> `HALF_OPEN` -> `CLOSED`).
- No tests for concurrent access or thread contention.
- No tests for network timeouts or socket behavior under load.
- No tests for tool validation failures (e.g. hallucinated parameters or invalid types).
- No tests for deadlocks, cascade loops, or budget exhaustion.
- No tests for true streaming or mid-stream failures.
- No fault-injection tests or benchmark suite.

---

## 5. Identified Defects & Security Vulnerabilities

1. **Security - Credential Transport in URL**: `router.py` (line 42) constructs URLs with credentials in query parameters: `url = f"...?key={api_key or ''}"`. Query strings leak into web server access logs, proxy logs, and HTTP client traces. Credentials must be passed in headers (`x-goog-api-key` or `Authorization: Bearer`).
2. **Global State Mutation**: `router.py` (line 24) calls `socket.setdefaulttimeout(DEFAULT_TIMEOUT)`. This alters socket timeouts for any other library running within the same Python process. Timeouts must be per-request.
3. **Semantic Uncertainty Mutation**: `translators.py` (lines 34-37) invents JSON structure (`{"command": ...}` or `{"text": ...}`) when unparseable strings are encountered. This can cause autonomous agents to execute hallucinated commands.
4. **CI Configuration Error**: `.github/workflows/ci.yml` runs `pip install .[dev,proxy]`. `proxy` is not defined in `pyproject.toml` (only `asgi` is defined).
5. **Impure Imports / Side Effects**: Importing `durallm.proxy` initializes `ROUTER = UniversalFailoverRouter(auto_discover_free=True)` which triggers background discovery network calls to OpenRouter.
6. **Cooldown Thrashing / No True Breaker**: When all endpoints in a pool are on cooldown, the pool manager unconditionally deletes the oldest cooldown (`del self.cooldowns[oldest_key]`), immediately re-hammering a known failing provider.

---

## 6. Target Architectural Gaps (V1 vs V2 Spec)

| Domain | V1 Prototype State | V2 Requirement |
|---|---|---|
| **Circuit Breaker** | Monotonic cooldown timestamp per provider | Formal state machine: `CLOSED`, `OPEN`, `HALF_OPEN`, `FORCED_OPEN`, `DISABLED`. Sliding time/count windows, failure-rate & slow-call thresholds, bounded half-open probe permits. |
| **Failure Taxonomy** | 13 coarse enum values | Hierarchical taxonomy distinguishing provider infrastructure, rate limits, request incompatibilities, client errors, and semantic agent failures. |
| **Capability Registry** | None (only context length and hardcoded pool string) | Structured `ModelProfile` with declared context window, max tokens, tool calling, parallel tools, structured outputs, vision, reasoning, streaming, and pricing. |
| **Candidate Routing** | Round-robin over static candidate lists | Requirement vector matching (hard constraint filtering) followed by soft multi-objective scoring (latency, cost, reliability, tool success). |
| **Retry / Fallback** | Unbounded attempt loop (`max_attempts=8`) | Strict policy engine with exponential backoff + jitter, `Retry-After` parsing, per-attempt timeouts, global operation deadlines, retry/fallback budgets, loop/cycle detection. |
| **Protocol IR** | Pairwise direct translation (Anthropic ↔ OpenAI) | Central `NormalizedRequest` and `NormalizedResponse` Intermediate Representation with provider adapters (O(N) instead of O(N²)). |
| **Tool Validation** | Regex cleanup with unsafe fallback synthesis | Strict schema validation before execution: `valid`, `normalized`, `invalid`, `unsafe_to_repair`. Zero hallucination / semantic guessing. |
| **Semantic State** | None (raw messages passed through) | Neutral `AgentState` and `StateSnapshot` capturing objective, constraints, decisions, active tool schemas, tool execution state, and progress. |
| **Context Adaptation** | Naive character truncation of historical tool results | Budget-aware compaction calculating exact input budget, output reservations, safety margins, with hierarchical retention (objective > constraints > tools > recent turns). |
| **Streaming** | Only synthetic buffered streaming | Dual explicit modes: Mode A (True streaming passthrough) and Mode B (Synthetic / buffered streaming for atomic validation). Mid-stream failure policies. |
| **Observability** | Minimal debug print/logger statements | Structured JSON telemetry with `request_id`, `operation_id`, `attempt_id`, latencies, TTFT, token usage, cost estimates, breaker state, routing explanation. Optional OTel. |
| **Fault Injection** | None | Programmable mock provider framework with deterministic failure scenarios (429, 500, 503, timeout, mid-stream abort, schema rejection, malformed tool call). |
| **Benchmarks** | Anecdotal 45-minute Claude Code run | Reproducible benchmark harness with 10 synthetic fault scenarios (B1-B10), statistical reporting, and baseline comparisons. |

---

## 7. Baseline Conclusion & Entry into Phase Planning

The prototype code base provides a clean starting point and demonstrates the utility of protocol translation and context pruning. However, all core reliability mechanisms require complete modular implementation to fulfill the requirements of `DURALLM_V2_LONG_HORIZON_SPEC.md`.

All 14 current tests must remain green as regression anchors, augmented by comprehensive test suites for each new subsystem.
