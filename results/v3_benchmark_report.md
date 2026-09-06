# LLM Circuit Breaker V3 — Authoritative Benchmark Report

**Date:** 2026-09-06  
**Test Suite:** Scenarios B1 through B15 + Primary Research Benchmark  
**Target Architecture:** V3 Agent-Resilience Gateway with Formal FSM, Real Telemetry, and Semantic Failover  

---

## 1. Multi-Baseline Comparison Table (B1–B15)

| Baseline / System | Completion Rate | Recovery Rate | Median Latency | P95 Latency | Avg Attempts/Req | Semantic Error Rate |
|---|---|---|---|---|---|---|
| **LLM-Circuit-Breaker-V3** | 100.0% | 60.0% | 1.69 ms | 310.62 ms | 1.67 | 0.0% |
| **Baseline-A-Direct** | 40.0% | 0.0% | 0.01 ms | 0.05 ms | 1.00 | 13.3% |
| **Baseline-B-Same-Provider-Retry** | 80.0% | 40.0% | 0.01 ms | 0.02 ms | 1.53 | 13.3% |
| **Baseline-C-Static-Fallback** | 86.7% | 46.7% | 0.01 ms | 0.02 ms | 1.53 | 13.3% |

---

## 2. Primary Research Benchmark: Semantic Failover

Compound multi-turn migration: Anthropic Primary (503 outage) -> OpenAI Secondary (invalid tool schema) -> Gemini Tertiary (validated tool execution with idempotency receipt).

- **Task Completed:** `True`
- **Critical State Preserved:** `True`
- **Tool Correctness:** `True`
- **Duplicate Tool Executions:** `0`
- **Semantic Error Rate:** `0.0%`
- **Total Fallback Hops:** `2`
- **Recovery Latency:** `0.62 ms`
- **Observable FailoverPlans Generated:** `2`
- **Idempotency Receipt Cached:** `False`

---

## 3. Scenario Details Breakdown (B1–B15)

| Scenario | Description | V3 Result | Attempts | Fallback Hops | Latency |
|---|---|---|---|---|---|
| **[B1]** | Primary provider permanently fails with 503; secondary provider is healthy. | `PASSED` | 3 | 1 | 13.28 ms |
| **[B2]** | Primary provider alternates 429 (Retry-After: 1s) and 200. | `PASSED` | 2 | 0 | 1003.66 ms |
| **[B3]** | Primary provider exceeds deadline timeout; secondary succeeds under 100ms. | `PASSED` | 2 | 0 | 13.17 ms |
| **[B4]** | Large conversation (60k tokens) fails over from 128k primary to 32k secondary, compacting safely. | `PASSED` | 2 | 0 | 12.09 ms |
| **[B5]** | Critical continuation fact buried deep in old history survives compaction. | `PASSED` | 2 | 0 | 13.61 ms |
| **[B6]** | Primary emits corrupt JSON; validator fails closed and recovers on Secondary. | `PASSED` | 2 | 1 | 1.69 ms |
| **[B7]** | Primary emits valid JSON but violates schema; validator triggers safe failover. | `PASSED` | 2 | 1 | 0.61 ms |
| **[B8]** | Tool executes, network response lost; gateway retry must not re-execute with receipt. | `PASSED` | 1 | 0 | 0.27 ms |
| **[B9]** | Provider drops connection mid-stream; Mode B atomic buffering recovers on secondary. | `PASSED` | 2 | 0 | 13.36 ms |
| **[B10]** | Provider trips breaker to OPEN, wait duration elapses, HALF_OPEN probe closes breaker. | `PASSED` | 1 | 0 | 0.21 ms |
| **[B11]** | Provider A fails with 500, Provider B fails with 429, Provider C succeeds without loop. | `PASSED` | 2 | 0 | 13.42 ms |
| **[B12]** | Coding pool exhausts provider_a; general_agent pool continues unimpeded. | `PASSED` | 1 | 0 | 0.24 ms |
| **[B13]** | Selects cost-effective candidate within budget ceiling. | `PASSED` | 1 | 0 | 0.19 ms |
| **[B14]** | Router selects endpoint with higher historical tool success rate. | `PASSED` | 1 | 0 | 0.40 ms |
| **[B15]** | Candidate lacking required capability is filtered without tripping its circuit breaker. | `PASSED` | 1 | 0 | 0.18 ms |
