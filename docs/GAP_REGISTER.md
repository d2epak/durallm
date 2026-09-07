# LLM Circuit Breaker — Gap Register

**Date:** 2026-09-03  
**Review Standard:** Versioned V2 long-horizon specification and repository evidence
**Classification Levels:** `CRITICAL`, `HIGH`, `MEDIUM`, `LOW`, `OBSERVATION`

---

## 1. CRITICAL Gaps

### GAP-C01: Hardcoded Latency in Candidate Soft Scoring
- **Location:** `src/durallm/routing/router.py:100` (`latency_ms=200.0`)
- **Impact:** The routing design forbids hardcoded fake health inputs. Router currently scores candidates assuming fixed 200ms latency rather than observed telemetry from `HealthTelemetryStore`.
- **Resolution:** Bind `HealthTelemetryStore` directly to `CapabilityRouter`. If no observations exist, mark `latency = UNKNOWN` and apply documented cold-start policy.

### GAP-C02: Absence of Tool Execution Idempotency Ledger
- **Location:** `src/durallm/agent/` & `execution/`
- **Impact:** If an upstream tool call executes but the network drops before the response is returned, a naive gateway retry could re-execute a destructive tool (e.g. `rm -rf` or financial transaction).
- **Resolution:** Implement `ToolExecutionLedger` tracking `tool_call_id`, `logical_operation_id`, `request_hash`, `status` (`proposed`, `validated`, `submitted`, `committed`, `ambiguous`, `failed`), and receipts. Replays must verify receipts before allowing execution.

### GAP-C03: Lack of Explicit Semantic Failover Plan (`FailoverPlan`)
- **Location:** `src/durallm/execution/executor.py`
- **Impact:** When failing over from Provider A to Provider B, the gateway currently adapts context and tools inline, but does not construct an auditable, observable `FailoverPlan` combining source, target, reason, state snapshot, context transformation, and tool adaptations.
- **Resolution:** Create `FailoverPlan` dataclass and emit it during every cross-provider failover.

### GAP-C04: Blind Text Truncation in Tool Output Context Compaction
- **Location:** `src/durallm/agent/context.py:105`
- **Impact:** Context compaction currently truncates tool outputs using character slicing (`[:200] + ... + [-200:]`), which can discard critical error messages, return codes, and file paths located in the middle of command logs.
- **Resolution:** Implement structured tool result summarization extracting exit codes, error lines, paths, and status keys.

---

## 2. HIGH Gaps

### GAP-H01: Incomplete Benchmark Suite (Missing Scenarios B11–B15 & Primary Research Benchmark)
- **Location:** `benchmarks/scenarios.py`
- **Impact:** Scenarios B1–B10 exist, but the benchmark plan requires B1–B15 (including B11 multi-provider cascade, B12 concurrent agent contention, B13 cost constraints, B14 tool reliability differentiation, B15 capability mismatch) and a dedicated multi-turn compound primary research benchmark in `benchmarks/semantic_failover/`.
- **Resolution:** Implement B11–B15 and `benchmarks/semantic_failover/` harness.

### GAP-H02: Multi-Baseline Comparison (Baselines A through F)
- **Location:** `benchmarks/harness.py`
- **Impact:** Currently only compares V2 against Direct Baseline. The benchmark plan requires six baselines: Baseline A (Direct), Baseline B (Same-provider retry), Baseline C (Static ordered fallback), Baseline D (Breaker + static fallback), Baseline E (V1 prototype), Baseline F (V3 final system).
- **Resolution:** Implement all 6 baselines in `benchmarks/harness.py`.

### GAP-H03: Security Hardening (SSRF, Request/Response Size Exhaustion)
- **Location:** `src/durallm/providers/adapters.py` & `proxy.py`
- **Impact:** While Gemini header authentication is secured, there is no validation restricting upstream URLs to authorized HTTPS schemes or domains, nor is there explicit defense against response size bombs.
- **Resolution:** Add URL domain allowlisting/validation, max request body limits, and max response stream size limits.

### GAP-H04: High Concurrency Load Testing Under Heavy Contention
- **Location:** `tests/`
- **Impact:** Existing tests test 10 concurrent threads. Need forced concurrency tests with 100+ simultaneous requests asserting `half_open_active <= half_open_max_calls` and verifying zero deadlocks.
- **Resolution:** Implement high-concurrency integration test with `threading.Barrier`.

---

## 3. MEDIUM Gaps

### GAP-M01: Missing Local Zero-API-Key Demo (`python -m durallm.demo`)
- **Location:** Package root
- **Impact:** The release standard requires a complete, deterministic, runnable local demonstration without API keys showing primary failure, breaker trip, context compaction, tool validation, fallback recovery, and probe closure.
- **Resolution:** Implement `src/durallm/demo.py`.

### GAP-M02: Multi-Dimensional Resource Concept Model
- **Location:** `src/durallm/capability/profile.py`
- **Impact:** The resource model requires `Deployment`, `QuotaBucket`, `PricingProfile`, `PrivacyProfile`, and combining `provider × deployment × endpoint × credential` identities.
- **Resolution:** Expand resource model with explicit deployment, quota bucket, and pricing abstractions.

### GAP-M03: Cost Modeling & Budget Enforcement
- **Location:** `src/durallm/execution/`
- **Impact:** Pricing per 1M tokens exists in profiles, but there is no `max_request_cost` or accumulated agent budget checking in `AttemptLedger`.
- **Resolution:** Add cost estimation and budget ceiling checks to `AttemptLedger`.

### GAP-M04: Structured JSON Observability & Credential Redaction
- **Location:** `src/durallm/proxy.py` & logging
- **Impact:** Standard logging format is used. Structured JSON event logging with automatic redaction of API keys, Authorization headers, and raw prompts is required.
- **Resolution:** Implement `StructuredLogger` with JSON formatting and redaction filter.

---

## 4. LOW Gaps

### GAP-L01: Granular Connect, Write, TTFT, and Idle Stream Timeouts
- **Status:** Partially resolved for opt-in native streaming.
- **Location:** `src/durallm/execution/deadline.py` & `providers/adapters.py`
- **Evidence:** `TransportTimeouts` enforces separate direct HTTP(S) TCP-connect, TLS-handshake, first-byte, idle-read, and total budgets. The native reader is cancellable and capped at 10 MB; `tests/test_native_streaming.py` covers socket-level forwarding and disconnect boundaries.
- **Remaining gap:** Atomic buffered requests continue to use the standard-library one-attempt transport timeout. Direct native streaming does not yet support proxy tunnels or a distinct configurable write-phase deadline.

### GAP-L02: Optional Persistence Abstraction (SQLite)
- **Location:** `src/durallm/health/` & `breaker/`
- **Impact:** State is in-memory by default. An optional SQLite storage backend for persistence across restarts is required.
- **Resolution:** Implement `SQLiteBreakerStore` and `SQLiteHealthStore` with clean interface.

---

## 5. OBSERVATIONS

- **OBS-01**: Existing 51 tests pass reliably with zero network access and deterministic execution.
- **OBS-02**: The normalized protocol IR dataclasses provide a solid foundation for cross-protocol conformance.
- **OBS-03**: All work must preserve 100% backward compatibility for existing V1/V2 users.
