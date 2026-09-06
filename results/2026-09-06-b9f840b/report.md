# LLM Circuit Breaker V3 — Benchmark Report

**Generated:** 2026-09-06T05:40:17+00:00  
**Commit:** `b9f840b`  
**Environment:** llm-circuit-breaker 0.2.0 · Python 3.11.15 · macOS-26.6.2-arm64-arm-64bit  
**Runs:** 5 (seed 0; run *i* re-seeds `random` with seed + *i*, which fixes the jittered backoff draws)  
**Test Suite:** Scenarios B1 through B15 + Primary Research Benchmark  

Values are the mean over 5 run(s); ± is the half-width of the 95% confidence interval (Student's t) where runs differed. Scenario outcomes are deterministic, so a non-zero interval on a completion, recovery or semantic-error rate means a scenario's outcome changed between runs. Latencies include real backoff sleeps and vary with the scheduler.

---

## 1. Multi-Baseline Comparison Table (B1–B15)

| Baseline / System | Completion Rate | Recovery Rate | Median Latency | P95 Latency | Avg Attempts/Req | Semantic Error Rate |
|---|---|---|---|---|---|---|
| **LLM-Circuit-Breaker-V3** | 100.0% | 80.0% | 12.86 ± 0.50 ms | 312.08 ± 1.33 ms | 2.60 | 0.0% |
| **Baseline-A-Direct** | 0.0% | 0.0% | 0.03 ± 0.02 ms | 0.08 ± 0.06 ms | 1.20 | 20.0% |
| **Baseline-B-Same-Provider-Retry** | 20.0% | 20.0% | 0.04 ± 0.02 ms | 0.49 ± 0.68 ms | 2.33 | 20.0% |
| **Baseline-C-Static-Fallback** | 33.3% | 33.3% | 0.07 ± 0.01 ms | 0.29 ± 0.20 ms | 2.20 | 20.0% |
| **Baseline-D-Breaker-Static-Fallback** | 33.3% | 33.3% | 0.08 ± 0.02 ms | 0.54 ± 0.37 ms | 2.20 | 20.0% |
| **Baseline-E-V1-Prototype** | 53.3% | 53.3% | 0.23 ± 0.06 ms | 5.13 ± 0.74 ms | 2.00 | 20.0% |

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
- **Recovery Latency:** `1.75 ± 0.61 ms`
- **Context Delivered to Tertiary:** `21661` of `36071` tokens (39.9% reduction)
- **Observable FailoverPlans Generated:** `2`
- **Idempotency Receipt Cached:** `True`
- **Identical across all 5 run(s) (everything but latency):** `True`

---

## 3. Scenario Details Breakdown (B1–B15)

V3 per scenario; result counts passing runs out of 5, the other columns are means.

| Scenario | Description | V3 Result | Attempts | Fallback Hops | Latency |
|---|---|---|---|---|---|
| **[B1]** | Primary provider permanently fails with 503; secondary provider is healthy. | `PASSED` (5/5) | 3.00 | 1.00 | 13.12 ± 0.70 ms |
| **[B2]** | Primary provider answers 429 (Retry-After: 1s) then 200; secondary is healthy. | `PASSED` (5/5) | 2.00 | 0.00 | 1006.03 ± 3.96 ms |
| **[B3]** | Primary provider stalls past the request timeout; secondary answers promptly. | `PASSED` (5/5) | 2.00 | 0.00 | 12.92 ± 1.37 ms |
| **[B4]** | A ~36k-token conversation fails over from the 128k primary to a 32k secondary that rejects oversize input; the root objective and latest turn must survive compaction. | `PASSED` (5/5) | 3.00 | 1.00 | 13.85 ± 0.86 ms |
| **[B5]** | A critical fact in the protected root prompt survives compaction onto a 32k secondary (facts inside evicted intermediate turns are not preserved by design). | `PASSED` (5/5) | 3.00 | 1.00 | 13.21 ± 0.77 ms |
| **[B6]** | Primary emits corrupt JSON; validator fails closed and recovers on Secondary. | `PASSED` (5/5) | 2.00 | 1.00 | 0.82 ± 0.49 ms |
| **[B7]** | Primary emits valid JSON but violates schema; validator triggers safe failover. | `PASSED` (5/5) | 2.00 | 1.00 | 0.57 ± 0.30 ms |
| **[B8]** | The tool ran but its response was lost; the client re-sends the same logical operation. The tool must execute exactly once across both turns. | `PASSED` (5/5) | 2.00 | 0.00 | 0.37 ± 0.16 ms |
| **[B9]** | Primary keeps dropping the connection mid-stream (502); the secondary delivers a complete response. | `PASSED` (5/5) | 3.00 | 1.00 | 12.83 ± 0.92 ms |
| **[B10]** | Primary fails twice then recovers. Turn 2 must not touch the failed primary; after the open-wait elapses, turn 3 must be answered by the primary again via a probe. | `PASSED` (5/5) | 5.00 | 1.00 | 14.59 ± 0.46 ms |
| **[B11]** | Provider A fails with 500, Provider B fails with 429, Provider C succeeds without loop. | `PASSED` (5/5) | 2.00 | 0.00 | 13.11 ± 1.00 ms |
| **[B12]** | The coding pool's primary is down; a general_agent turn must be answered by that pool's own provider without touching the coding pool's providers. | `PASSED` (5/5) | 4.00 | 1.00 | 13.68 ± 0.82 ms |
| **[B13]** | The primary is priced above the request's cost ceiling; the cheap secondary must be used instead. | `PASSED` (5/5) | 1.00 | 0.00 | 0.25 ± 0.02 ms |
| **[B14]** | The primary keeps emitting schema-invalid tool calls. With reliability-aware routing the second turn must skip the primary based on its observed tool failure. | `PASSED` (5/5) | 3.00 | 1.00 | 1.71 ± 1.34 ms |
| **[B15]** | A vision request must go straight to the vision-capable secondary; the next text-only turn must still use the primary, proving the mismatch did not trip its breaker. | `PASSED` (5/5) | 2.00 | 0.00 | 0.47 ± 0.17 ms |
