# LLM Circuit Breaker — Public Claim Audit

**Date:** 2026-09-03  
**Review Standard:** Public-claim audit against repository evidence

Every substantive claim across `README.md`, `ARCHITECTURE.md`, docstrings, and benchmark reports is audited below to verify whether it is `PROVEN`, `EXPERIMENTAL`, or `UNSUPPORTED / REQUIRES QUALIFICATION`.

---

## 1. Claim Verification Audit Table

| Claim | Source | Evidence / Test | Status | Corrected / Qualified Wording |
| :--- | :--- | :--- | :---: | :--- |
| **"Zero downtime"** | Historical README / docs | Test suite covers 503 failover, but no distributed system can promise zero downtime under total upstream provider exhaustion. | **UNSUPPORTED AS WRITTEN** | *"High availability through automated multi-provider failover"* |
| **"Self-healing"** | Historical README / docstrings | Breaker transitions from `OPEN` to `HALF_OPEN` and back to `CLOSED` upon successful probe permits. | **QUALIFIED** | *"Self-healing circuit breaker with permit-bounded probe recovery"* |
| **"Seamless streaming"** | Historical README | Mode B buffers turn before streaming; Mode A passes bytes but cannot failover after bytes hit wire without duplicate tokens. | **UNSUPPORTED AS WRITTEN** | *"True SSE passthrough or atomic-buffered replay with explicit mid-stream recovery policies"* |
| **"Lossless context preservation"** | Historical README | Context compaction by definition reduces token count. Planted facts can be preserved, but arbitrary raw text is compacted. | **UNSUPPORTED AS WRITTEN** | *"Hierarchical context compaction preserving critical objectives, constraints, and recent turns"* |
| **"Autonomous routing"** | README / docstrings | Hard constraint matching and multi-objective soft scoring are fully implemented and deterministic. | **PROVEN** | *"Capability-aware, multi-objective deterministic routing"* |
| **"Verified free-model discovery"** | `discovery.py` | Scrapes OpenRouter models API. Catalog presence does not guarantee live uptime or unexhausted free quota. | **QUALIFIED** | *"Dynamic free-model catalog discovery with verification probing"* |
| **"Resilience4j Parity"** | README / ADR 0001 | FSM states, sliding windows (count/time), bounded half-open permits, and slow-call thresholds verified in unit tests. | **PROVEN** | Verified by 12 deterministic unit tests in `tests/unit/test_circuit_breaker.py`. |
| **"Zero third-party dependencies"** | README / pyproject.toml | Standard library Python 3.9+ (`http.server`, `urllib`, `sqlite3`, `threading`). Verified with `uv pip check`. | **PROVEN** | Verified. No external cloud, Redis, or PostgreSQL required for core engine. |
| **"Semantic failover for autonomous agents"** | Mandate & README | Fails closed on malformed tool calls, safely normalizes syntactic JSON errors, preserves root goals during compaction. | **PROVEN** | Validated across B1–B10 and `tests/unit/test_tool_validation.py`. |
| **"100% rejection of corrupt tool calls"** | Benchmark report | Malformed tool arguments rejected as `UNSAFE_TO_REPAIR`, triggering clean failover. | **PROVEN** | Verified in `tests/faults/test_fault_injection.py:test_fault_malformed_tool_call`. |

---

## 2. Policy for Documented Claims Going Forward

1. **Never claim "Zero Downtime"**: Even with infinite fallbacks, downstream network failure or client misconfiguration can prevent request delivery. Use *"Autonomous failover and high availability"*.
2. **Never claim "Seamless Mid-Stream Failover"**: Once bytes are written to a downstream client stream, swapping models mid-sentence causes token incoherence. Clearly document the two distinct modes: Mode A (`TRUE_STREAMING` with abort on mid-stream failure) and Mode B (`ATOMIC_BUFFERED` with turn replay on fallback).
3. **Never claim "Lossless Compaction"**: Compacting 60k tokens into a 32k window permanently discards intermediate raw text. Clearly document that compaction preserves **continuation-critical invariants** (system instructions, root objective, hard constraints, active tools, and recent turns), not raw byte logs.
