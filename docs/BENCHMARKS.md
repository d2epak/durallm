# Reproducible Benchmarks & Empirical Evaluation

This document details the design, implementation, and results of the **B1 through B15 Benchmark Suite**, the **3 Comparative Baselines**, and the **Primary Research Benchmark** in **LLM Circuit Breaker (V3)**.

---

## 1. The 15 Benchmark Scenarios (B1–B15)

- **B1 Permanent Outage:** Primary fails with persistent HTTP 503; secondary succeeds.
- **B2 Intermittent 429:** Primary alternates 429 and 200; tests jittered backoff and retry-after.
- **B3 Slow Provider / Timeout:** Primary stalls past total deadline; secondary succeeds within 100ms.
- **B4 Context Window Mismatch:** 60k request fails over from 128k primary to 32k secondary, compacting safely.
- **B5 Deep Critical Fact Preservation:** Planted state deep in old history survives compaction.
- **B6 Malformed Tool Call Syntax:** Primary emits unparseable JSON; gateway fails closed and recovers.
- **B7 Semantically Invalid Tool Call:** Primary emits valid JSON violating schema; gateway triggers failover.
- **B8 Tool Execution Ambiguity & Idempotency:** Connection drops after execution; retry uses cached receipt.
- **B9 Mid-Stream Disconnect Recovery:** Provider drops connection mid-stream; Mode B atomic replay recovers.
- **B10 Provider Recovery & Probe:** Breaker trips to OPEN, wait period elapses, HALF_OPEN probe closes breaker.
- **B11 Multi-Provider Cascade:** Provider A fails 500, B fails 429, C succeeds without infinite cycle.
- **B12 Concurrent Agent Contention:** High-load pool does not starve secondary pools.
- **B13 Cost Constraint & Budget:** Router selects cost-efficient candidate within budget ceiling.
- **B14 Tool Reliability Differentiation:** Router prioritizes endpoint with superior historical tool execution.
- **B15 Capability Mismatch Non-Poisoning:** Candidate lacking required capability is filtered without health damage.

---

## 2. Multi-Baseline Empirical Results

| Baseline / System | Completion Rate | Autonomous Recovery | Median Latency | P95 Latency | Semantic Error Rate |
|---|:---:|:---:|:---:|:---:|:---:|
| **LLM-Circuit-Breaker-V3** | **100.0%** | **60.0%** | **1.69 ms** | **310.62 ms** | **0.0%** |
| **Baseline-A-Direct** | 40.0% | 0.0% | 0.01 ms | 0.05 ms | 13.3% |
| **Baseline-B-Same-Provider-Retry** | 80.0% | 40.0% | 0.01 ms | 0.02 ms | 13.3% |
| **Baseline-C-Static-Fallback** | 86.7% | 46.7% | 0.01 ms | 0.02 ms | 13.3% |

*Takeaway:* Retry and static fallback catch the common HTTP 5xx cases, but **only V3 completes all 15 scenarios**. Every system is scored by one rule (a response counts only if every delivered tool call passes the real schema validator, attempts are counted from the mock providers' call log), so each baseline forwards the two invalid tool calls of B6 and B7 (13.3% semantic error rate). All rows run in one process against the same mock providers, so latencies measure harness overhead; the V3 P95 is dominated by the 1 s `Retry-After` wait honoured in B2.

---

## 3. Primary Research Benchmark (Compound Semantic Failover)

Tests multi-turn compound failure:
1. Agent starts on Anthropic Primary.
2. Primary suffers 503 outage.
3. Fallback to OpenAI candidate with smaller 32k context and different protocol.
4. OpenAI candidate emits invalid tool schema.
5. Gateway detects invalid schema, fails closed, and issues `FailoverPlan` to Gemini candidate.
6. Gemini candidate succeeds with validated tool call.
7. Tool receipt is committed to idempotency ledger.

**Results:**
- Task Completion Rate: **100.0%**
- Critical Continuation State Preserved: **True**
- Tool Correctness: **True**
- Duplicate Tool Side-Effects: **0**
- Observable FailoverPlans Generated: **2**
- Total Recovery Latency: **<1.0 ms** (in-memory mock)
