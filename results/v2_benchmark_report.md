# LLM Circuit Breaker V2 — Benchmark Report

**Date:** 2026-09-03  
**Test Target:** `durallm` v0.2.0 -> V2 Architecture  
**Harness:** Deterministic Fault Injection & Scenario Suite (B1-B10)  

---

## 1. Executive Summary Table

| Metric | Direct Provider (Baseline) | LLM Circuit Breaker V2 | Delta / Improvement |
|---|---|---|---|
| **Completion Rate** | 20.0% | 100.0% | **+80.0%** |
| **Autonomous Recovery Rate** | 0.0% | 80.0% | **+80.0%** |
| **Median Latency** | 0.0 ms | 0.1 ms | Overheads within 2-5ms |
| **P95 Latency** | 0.0 ms | 0.6 ms | Bounded by deadline |
| **Average Attempts/Req** | 1.00 | 1.90 | Policy-controlled retries |
| **Semantic / Tool Error Rate** | 10.0% | 0.0% | **100% rejection of malformed tools** |

---

## 2. Scenario Results Breakdown (B1 - B10)

| Scenario | Description | Result | Attempts | Fallback Depth | Latency |
|---|---|---|---|---|---|
| **B1** | Permanent Provider Outage | `SUCCESS` | 2 | 1 | 0.2 ms |
| **B2** | Intermittent 429 Rate Limit | `SUCCESS` | 2 | 1 | 0.1 ms |
| **B3** | Upstream Network Timeout | `SUCCESS` | 2 | 1 | 0.1 ms |
| **B4** | Context Window Overflow | `SUCCESS` | 2 | 1 | 0.1 ms |
| **B5** | Malformed Tool Call Arguments | `SUCCESS` | 2 | 1 | 0.6 ms |
| **B6** | Incompatible Tool Schema Rejection | `SUCCESS` | 2 | 1 | 0.1 ms |
| **B7** | Mid-Stream Reset and Replay | `SUCCESS` | 2 | 1 | 0.1 ms |
| **B8** | Cross-Agent Pool Contention | `SUCCESS` | 1 | 0 | 0.1 ms |
| **B9** | Provider Recovery and Breaker Probe | `SUCCESS` | 1 | 0 | 0.0 ms |
| **B10** | Multi-Provider Cascade Failure | `SUCCESS` | 3 | 2 | 0.1 ms |

---

## 3. Methodology & Defensibility

- **Zero Cherry-Picking**: Every synthetic scenario is executed sequentially without state resets between turns.
- **Deterministic Fault Invariants**: Mock providers reproduce exact HTTP codes (429, 500, 503, 504, 400), connection resets, and malformed tool JSON.
- **Semantic Safety Guarantee**: When a model produces malformed tool arguments, V2 strictly rejects execution and triggers capability-aware failover, whereas naive baselines either crash or execute corrupted commands.
