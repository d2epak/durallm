# LLM Circuit Breaker V3 — Authoritative Benchmark Report

**Date:** 2026-09-06  
**Test Suite:** Scenarios B1 through B15 + Primary Research Benchmark  
**Target Architecture:** V3 Agent-Resilience Gateway with Formal FSM, Real Telemetry, and Semantic Failover  

---

## 1. Multi-Baseline Comparison Table (B1–B15)

| Baseline / System | Completion Rate | Recovery Rate | Median Latency | P95 Latency | Avg Attempts/Req | Semantic Error Rate |
|---|---|---|---|---|---|---|
| **DuraLLM-V3** | 100.0% | 80.0% | 12.55 ms | 314.08 ms | 2.60 | 0.0% |
| **Baseline-A-Direct** | 0.0% | 0.0% | 0.03 ms | 0.07 ms | 1.20 | 20.0% |
| **Baseline-B-Same-Provider-Retry** | 20.0% | 20.0% | 0.03 ms | 0.12 ms | 2.33 | 20.0% |
| **Baseline-C-Static-Fallback** | 33.3% | 33.3% | 0.07 ms | 0.16 ms | 2.20 | 20.0% |
| **Baseline-D-Breaker-Static-Fallback** | 33.3% | 33.3% | 0.09 ms | 0.27 ms | 2.20 | 20.0% |
| **Baseline-E-V1-Prototype** | 53.3% | 53.3% | 0.24 ms | 4.60 ms | 2.00 | 20.0% |

---

## 2. Primary Research Benchmark: Semantic Failover

Compound multi-turn migration: primary (503 outage) -> 32k secondary (compaction, then invalid tool schema) -> 32k tertiary (validated tool call, receipt committed). Turn 2 re-sends the same logical operation after a lost response; the tool must not run again.

- **Task Completed:** `True`
- **Critical State Preserved:** `True`
- **Tool Correctness:** `True`
- **Duplicate Tool Executions:** `0`
- **Tool Executions Across Both Turns:** `1` (second delivery replayed: `True`)
- **Semantic Error Rate:** `0.0%`
- **Total Fallback Hops:** `2`
- **Recovery Latency:** `5.29 ms`
- **Context Delivered to Tertiary:** `21661` of `36071` tokens (39.9% reduction)
- **Observable FailoverPlans Generated:** `2`
- **Idempotency Receipt Cached:** `True`

---

## 3. Scenario Details Breakdown (B1–B15)

| Scenario | Description | V3 Result | Attempts | Fallback Hops | Latency |
|---|---|---|---|---|---|
| **[B1]** | Primary provider permanently fails with 503; secondary provider is healthy. | `PASSED` | 3 | 1 | 12.41 ms |
| **[B2]** | Primary provider answers 429 (Retry-After: 1s) then 200; secondary is healthy. | `PASSED` | 2 | 0 | 1005.91 ms |
| **[B3]** | Primary provider stalls past the request timeout; secondary answers promptly. | `PASSED` | 2 | 0 | 12.55 ms |
| **[B4]** | A ~36k-token conversation fails over from the 128k primary to a 32k secondary that rejects oversize input; the root objective and latest turn must survive compaction. | `PASSED` | 3 | 1 | 14.70 ms |
| **[B5]** | A critical fact in the protected root prompt survives compaction onto a 32k secondary (facts inside evicted intermediate turns are not preserved by design). | `PASSED` | 3 | 1 | 14.02 ms |
| **[B6]** | Primary emits corrupt JSON; validator fails closed and recovers on Secondary. | `PASSED` | 2 | 1 | 4.08 ms |
| **[B7]** | Primary emits valid JSON but violates schema; validator triggers safe failover. | `PASSED` | 2 | 1 | 0.77 ms |
| **[B8]** | The tool ran but its response was lost; the client re-sends the same logical operation. The tool must execute exactly once across both turns. | `PASSED` | 2 | 0 | 0.82 ms |
| **[B9]** | Primary keeps dropping the connection mid-stream (502); the secondary delivers a complete response. | `PASSED` | 3 | 1 | 13.68 ms |
| **[B10]** | Primary fails twice then recovers. Turn 2 must not touch the failed primary; after the open-wait elapses, turn 3 must be answered by the primary again via a probe. | `PASSED` | 5 | 1 | 17.58 ms |
| **[B11]** | Provider A fails with 500, Provider B fails with 429, Provider C succeeds without loop. | `PASSED` | 2 | 0 | 12.83 ms |
| **[B12]** | The coding pool's primary is down; a general_agent turn must be answered by that pool's own provider without touching the coding pool's providers. | `PASSED` | 4 | 1 | 14.81 ms |
| **[B13]** | The primary is priced above the request's cost ceiling; the cheap secondary must be used instead. | `PASSED` | 1 | 0 | 0.24 ms |
| **[B14]** | The primary keeps emitting schema-invalid tool calls. With reliability-aware routing the second turn must skip the primary based on its observed tool failure. | `PASSED` | 3 | 1 | 1.20 ms |
| **[B15]** | A vision request must go straight to the vision-capable secondary; the next text-only turn must still use the primary, proving the mismatch did not trip its breaker. | `PASSED` | 2 | 0 | 0.48 ms |
