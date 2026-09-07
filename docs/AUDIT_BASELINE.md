# LLM Circuit Breaker — Audit Baseline

**Date:** 2026-09-03  
**Auditor:** Antigravity Principal Engineering & Reliability Review  
**Repository State:** v0.2.0 post-V2 milestone (`commit c99ee86`)  
**Evaluation Standard:** Versioned V2 long-horizon specification and local evidence available in this repository

---

## 1. Capability Forensic Matrix

States evaluated:
- `DOCUMENTED`: Appears in README, docstrings, or architecture specs.
- `IMPLEMENTED`: Concrete code exists in `src/durallm/`.
- `INTEGRATED`: Actively called by the main dispatch/gateway execution path.
- `TESTED`: Covered by deterministic unit/functional test in `tests/`.
- `FAULT-TESTED`: Verified under injected fault scenarios.
- `BENCHMARKED`: Exercised and recorded in the benchmark suite.
- `EVIDENCE`: Concrete test/file artifacts validating the capability.

| Capability | Documented | Implemented | Integrated | Tested | Fault-tested | Benchmarked | Evidence |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **Resilience4j Circuit Breaker FSM** | YES | YES | YES | YES | YES | YES | `src/durallm/breaker/`, `tests/unit/test_circuit_breaker.py` |
| **Sliding Window Metrics (Count & Time)** | YES | YES | YES | YES | YES | YES | `src/durallm/breaker/metrics.py` |
| **Bounded Half-Open Probe Permits** | YES | YES | YES | YES | YES | YES | `CircuitBreaker.acquire_permission()`, `test_10_concurrent_callers` |
| **Hierarchical Failure Taxonomy** | YES | YES | YES | YES | YES | YES | `src/durallm/classifier.py`, `models.py` |
| **Non-Poisoning Fault Separation** | YES | YES | YES | YES | YES | YES | `test_9_ignored_exceptions`, `classifier.py:poisons_health` |
| **Multi-Dimensional Resource Model** | PARTIAL | PARTIAL | PARTIAL | NO | NO | NO | `ModelProfile` exists, but explicit `Deployment`, `QuotaBucket`, `PricingProfile`, `PrivacyProfile` concepts are absent |
| **Capability Registry & Hard Constraints** | YES | YES | YES | YES | NO | YES | `capability/registry.py`, `routing/requirements.py` |
| **Observed Telemetry in Router** | PARTIAL | PARTIAL | PARTIAL | YES | NO | NO | **CRITICAL GAP**: `router.py:score_candidate` uses hardcoded `latency_ms=200.0` instead of observed telemetry from `HealthTelemetryStore` |
| **Multi-Dimensional Health Tracking** | PARTIAL | PARTIAL | NO | YES | NO | NO | `HealthTelemetryStore` tracks EMA latency and consecutive failures, but lacks TTFT, 429 rate, timeout rate, throughput |
| **Interpretable Adaptive Routing (EMA)** | PARTIAL | PARTIAL | NO | NO | NO | NO | Routing soft scores are static weights; no dynamic EMA adjustment across candidate ranks |
| **Bounded Retries & Fallbacks** | YES | YES | YES | YES | YES | YES | `execution/policy.py`, `ledger.py` |
| **Attempt Ledger & Cycle Protection** | YES | YES | YES | YES | NO | YES | `execution/ledger.py`, `test_cycle_detection_in_fallback_ledger` |
| **Hierarchical Deadlines (Connect/TTFT/Total)**| PARTIAL | PARTIAL | YES | YES | NO | YES | Total and attempt deadlines exist; specific connect/write/TTFT/idle stream timeouts not separately enforced in HTTP adapter |
| **Canonical Protocol IR** | YES | YES | YES | YES | NO | YES | `protocol/ir.py`, `anthropic.py`, `openai.py`, `gemini.py` |
| **Thinking / Reasoning Preservation** | YES | YES | YES | YES | NO | NO | Preserved in IR dataclasses, but cross-protocol differential golden tests needed |
| **Strict Tool Validation (Rule 3)** | YES | YES | YES | YES | YES | YES | `agent/tool_validation.py`, `test_tool_validation.py` |
| **Tool Execution Idempotency Ledger** | NO | NO | NO | NO | NO | NO | **CRITICAL GAP**: No `ToolExecutionLedger` tracking execution receipts vs network loss; cannot guarantee at-least-once vs exactly-once semantics |
| **Provider-Neutral Agent State & Snapshots** | YES | YES | NO | YES | NO | NO | `agent/state.py` defines `AgentState` & `StateSnapshot`, but `GatewayExecutor` does not actively construct a `FailoverPlan` using it |
| **Actual State-Driven Semantic Failover** | PARTIAL | PARTIAL | PARTIAL | PARTIAL | YES | YES | Gateway adapts context and tools on failover, but explicit `FailoverPlan` object is missing |
| **Budget-Aware Context Compaction** | YES | YES | YES | YES | NO | YES | `agent/context.py:ContextManager` |
| **Semantic Extraction of Tool Results** | PARTIAL | PARTIAL | NO | NO | NO | NO | Truncates tool outputs with head/tail string slicing rather than structured extraction of exit codes, paths, and errors |
| **Planted Critical Fact Preservation Test** | YES | YES | NO | YES | NO | NO | `tests/unit/test_agent_semantics.py` has single fact test; deep context tests (50k tokens, fact at 37k) needed |
| **Streaming Architecture (True vs Atomic)** | YES | YES | PARTIAL | YES | NO | YES | `streaming/modes.py` defines modes and synthetic generators, but proxy handler defaults to synthetic only |
| **Mid-Stream Disconnect Recovery** | PARTIAL | PARTIAL | NO | NO | YES | YES | Mode B buffers turn cleanly; Mode A partial-byte abort policy not yet tested with mid-stream disconnect injection |
| **Cross-Agent Pool Isolation** | YES | YES | YES | YES | NO | YES | `coding` pool vs `general_agent` pool isolated in memory; upstream quota sharing not modeled |
| **Cost Modeling & Budget Enforcing** | PARTIAL | PARTIAL | NO | NO | NO | NO | `ModelProfile.input_price_per_1m` exists, but `max_request_cost` enforcement in ledger is absent |
| **Response Validation Pipeline** | PARTIAL | PARTIAL | YES | YES | YES | YES | In `GatewayExecutor`: tool validation is checked, but explicit `ResponseValidator` stage (sanity, length, usage) is inline |
| **Structured Observability & Redaction** | PARTIAL | PARTIAL | PARTIAL | NO | NO | NO | Python standard logging used; structured JSON logs with sensitive credential redaction missing |
| **Security: Secure Gemini Header Auth** | YES | YES | YES | YES | NO | NO | `x-goog-api-key` header verified in `GeminiAdapter` and `router.py` |
| **Security: Zero Process-Wide Mutation** | YES | YES | YES | YES | NO | NO | `socket.setdefaulttimeout` removed from `router.py` |
| **SSRF & Payload Size Exhaustion Defense** | NO | NO | NO | NO | NO | NO | **HIGH GAP**: Base URL validation, maximum payload byte caps, and header sanitization need formal hardening |
| **Concurrency Under Heavy Contention (100+)** | PARTIAL | PARTIAL | NO | PARTIAL | NO | NO | Unit test covers 10 threads; 100+ concurrent requests load test needed |
| **Deterministic Fault Injection Framework** | YES | YES | YES | YES | YES | YES | `tests/faults/mock_provider.py` |
| **Comprehensive Scenarios (B1–B15)** | PARTIAL | PARTIAL | YES | YES | YES | YES | Scenarios B1–B10 implemented; B11–B15 (tool ambiguity, deep critical fact, cost constraint, tool reliability, capability mismatch) needed |
| **Multi-Baseline Comparison (Baselines A–F)**| PARTIAL | PARTIAL | YES | YES | YES | YES | Currently compares V2 vs Direct Baseline; needs Baselines B, C, D, E, F |
| **Primary Research Benchmark** | NO | NO | NO | NO | NO | NO | **CRITICAL GAP**: Dedicated `benchmarks/semantic_failover/` multi-turn compound failure scenario missing |
| **Architecture Decision Records (ADRs 1–10)**| YES | YES | YES | YES | NO | NO | 10 ADRs written in `docs/adr/` |
| **Local Zero-API-Key Demo (`demo.py`)** | NO | NO | NO | NO | NO | NO | `python -m durallm.demo` does not exist |

---

## 2. Summary of Baseline State
- **Core Strengths**: True Resilience4j FSM, Normalized Protocol IR, strict fail-closed tool schema validation (Rule 3), non-poisoning failure taxonomy, zero URL key leaks, zero global socket mutations, 51/51 unit tests passing.
- **Architectural Gaps to Reach Master Mandate (V3)**:
  1. Router must consume real observed telemetry rather than hardcoded 200ms latency.
  2. Implement `ToolExecutionLedger` for idempotency and ambiguous failure detection.
  3. Implement explicit `FailoverPlan` for semantic failover observability.
  4. Implement structured context extraction for tool outputs rather than blind head/tail character slicing.
  5. Expand benchmark suite to full B1–B15 scenarios with 6 fair baselines (A through F).
  6. Build primary research benchmark in `benchmarks/semantic_failover/`.
  7. Implement structured JSON observability with credential redaction.
  8. Implement deterministic local demo `python -m durallm.demo`.
  9. Add security hardening against SSRF, request/response size bombs, and malicious tool schemas.
  10. Conduct forced high-concurrency load testing (100+ simultaneous operations).
