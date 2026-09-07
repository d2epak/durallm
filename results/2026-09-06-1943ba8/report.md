# LLM Circuit Breaker V3 — Benchmark Report

**Generated:** 2026-09-06T20:36:28+00:00  
**Commit:** `1943ba8`  
**Environment:** durallm 0.2.0 · Python 3.11.15 · macOS-26.6.2-arm64-arm-64bit  
**Runs:** 3 (seed 42; run *i* re-seeds `random` with seed + *i*, which fixes the jittered backoff draws)  
**Test Suite:** Scenarios B1 through B15 + Primary Research Benchmark  

Values are the mean over 3 run(s); ± is the half-width of the 95% confidence interval (Student's t) where runs differed. Scenario outcomes are deterministic, so a non-zero interval on a completion, recovery or semantic-error rate means a scenario's outcome changed between runs. Latencies include real backoff sleeps and vary with the scheduler.

---

## 1. Multi-Baseline Comparison Table (B1–B15)

| Baseline / System | Completion Rate | Recovery Rate | Median Latency | P95 Latency | Avg Attempts/Req | Semantic Error Rate |
|---|---|---|---|---|---|---|
| **DuraLLM-V3** | 100.0% | 80.0% | 12.12 ± 1.35 ms | 313.64 ± 3.95 ms | 2.60 | 0.0% |
| **Baseline-A-Direct** | 0.0% | 0.0% | 0.02 ± 0.01 ms | 0.40 ± 1.45 ms | 1.20 | 20.0% |
| **Baseline-B-Same-Provider-Retry** | 20.0% | 20.0% | 0.05 ± 0.11 ms | 0.57 ± 1.02 ms | 2.33 | 20.0% |
| **Baseline-C-Static-Fallback** | 33.3% | 33.3% | 0.03 ± 0.02 ms | 0.32 ± 0.41 ms | 2.20 | 20.0% |
| **Baseline-D-Breaker-Static-Fallback** | 33.3% | 33.3% | 0.04 ± 0.03 ms | 0.29 ± 0.21 ms | 2.20 | 20.0% |
| **Baseline-E-V1-Prototype** | 53.3% | 53.3% | 0.13 ± 0.07 ms | 5.07 ± 2.37 ms | 2.00 | 20.0% |
| **Baseline-F-LiteLLM-Router** | 33.3% | 33.3% | 7.98 ± 5.89 ms | 24.68 ± 19.03 ms | 2.20 | 20.0% |

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
- **Recovery Latency:** `1.27 ± 1.84 ms`
- **Context Delivered to Tertiary:** `21661` of `36071` tokens (39.9% reduction)
- **Observable FailoverPlans Generated:** `2`
- **Idempotency Receipt Cached:** `True`
- **Identical across all 3 run(s) (everything but latency):** `True`

---

## 3. Scenario Details Breakdown (B1–B15)

V3 per scenario; result counts passing runs out of 3, the other columns are means.

| Scenario | Description | V3 Result | Attempts | Fallback Hops | Latency |
|---|---|---|---|---|---|
| **[B1]** | Primary provider permanently fails with 503; secondary provider is healthy. | `PASSED` (3/3) | 3.00 | 1.00 | 13.64 ± 6.92 ms |
| **[B2]** | Primary provider answers 429 (Retry-After: 1s) then 200; secondary is healthy. | `PASSED` (3/3) | 2.00 | 0.00 | 1007.98 ± 1.70 ms |
| **[B3]** | Primary provider stalls past the request timeout; secondary answers promptly. | `PASSED` (3/3) | 2.00 | 0.00 | 13.67 ± 3.67 ms |
| **[B4]** | A ~36k-token conversation fails over from the 128k primary to a 32k secondary that rejects oversize input; the root objective and latest turn must survive compaction. | `PASSED` (3/3) | 3.00 | 1.00 | 13.65 ± 3.58 ms |
| **[B5]** | A critical fact in the protected root prompt survives compaction onto a 32k secondary (facts inside evicted intermediate turns are not preserved by design). | `PASSED` (3/3) | 3.00 | 1.00 | 13.43 ± 3.36 ms |
| **[B6]** | Primary emits corrupt JSON; validator fails closed and recovers on Secondary. | `PASSED` (3/3) | 2.00 | 1.00 | 4.86 ± 14.34 ms |
| **[B7]** | Primary emits valid JSON but violates schema; validator triggers safe failover. | `PASSED` (3/3) | 2.00 | 1.00 | 0.52 ± 0.33 ms |
| **[B8]** | The tool ran but its response was lost; the client re-sends the same logical operation. The tool must execute exactly once across both turns. | `PASSED` (3/3) | 2.00 | 0.00 | 0.30 ± 0.09 ms |
| **[B9]** | Primary keeps dropping the connection mid-stream (502); the secondary delivers a complete response. | `PASSED` (3/3) | 3.00 | 1.00 | 13.18 ± 1.76 ms |
| **[B10]** | Primary fails twice then recovers. Turn 2 must not touch the failed primary; after the open-wait elapses, turn 3 must be answered by the primary again via a probe. | `PASSED` (3/3) | 5.00 | 1.00 | 13.62 ± 4.73 ms |
| **[B11]** | Provider A fails with 500, Provider B fails with 429, Provider C succeeds without loop. | `PASSED` (3/3) | 2.00 | 0.00 | 13.38 ± 1.04 ms |
| **[B12]** | The coding pool's primary is down; a general_agent turn must be answered by that pool's own provider without touching the coding pool's providers. | `PASSED` (3/3) | 4.00 | 1.00 | 14.08 ± 9.48 ms |
| **[B13]** | The primary is priced above the request's cost ceiling; the cheap secondary must be used instead. | `PASSED` (3/3) | 1.00 | 0.00 | 0.71 ± 1.69 ms |
| **[B14]** | The primary keeps emitting schema-invalid tool calls. With reliability-aware routing the second turn must skip the primary based on its observed tool failure. | `PASSED` (3/3) | 3.00 | 1.00 | 2.20 ± 6.28 ms |
| **[B15]** | A vision request must go straight to the vision-capable secondary; the next text-only turn must still use the primary, proving the mismatch did not trip its breaker. | `PASSED` (3/3) | 2.00 | 0.00 | 0.43 ± 0.53 ms |
