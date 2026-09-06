# Reproducible Benchmarks & Empirical Evaluation

This document details the design, implementation, and results of the **B1 through B15 Benchmark Suite**, the **5 Comparative Baselines**, and the **Primary Research Benchmark** in **LLM Circuit Breaker (V3)**.

---

## 1. The 15 Benchmark Scenarios (B1–B15)

- **B1 Permanent Outage:** Primary fails with persistent HTTP 503; secondary succeeds.
- **B2 Intermittent 429:** Primary alternates 429 and 200; tests jittered backoff and retry-after.
- **B3 Slow Provider / Timeout:** Primary stalls past total deadline; secondary succeeds within 100ms.
- **B4 Context Window Mismatch:** A ~36k-token history fails over from the 128k primary to a 32k secondary whose mock rejects oversize input with HTTP 400; the request the secondary actually receives must fit, keep the root objective, and end with the latest user turn.
- **B5 Root Fact Preservation:** A critical fact in the protected root prompt survives compaction onto the 32k secondary. Facts inside evicted intermediate turns are not preserved (by design), so the scenario does not claim they are.
- **B6 Malformed Tool Call Syntax:** Primary emits unparseable JSON; gateway fails closed and recovers.
- **B7 Semantically Invalid Tool Call:** Primary emits valid JSON violating schema; gateway triggers failover.
- **B8 Tool Execution Idempotency:** The tool ran but its response was lost, so the client re-sends the same logical operation. The harness's tool runner must execute exactly once across both turns; the second delivery must carry the committed receipt.
- **B9 Mid-Stream Disconnect Recovery:** Primary drops the connection mid-response (502) three times; the secondary delivers a complete response.
- **B10 Provider Recovery & Probe:** Primary fails twice then recovers. Turn 2 must not touch the primary (breaker OPEN); after the open-wait elapses, turn 3 must be served by the primary alone (HALF_OPEN probe).
- **B11 Multi-Provider Cascade:** Provider A fails 500, B fails 429, C succeeds without infinite cycle.
- **B12 Pool Isolation:** The coding pool's primary is down; a `general_agent` turn must be served by that pool's own provider without touching the coding pool's providers.
- **B13 Cost Constraint & Budget:** The primary is priced above the request's `maximum_cost_usd`; only the cheap secondary may be called.
- **B14 Tool Reliability Differentiation:** The primary keeps emitting schema-invalid tool calls. Under `reliability_aware` routing the second turn must skip the primary because of its observed tool failure.
- **B15 Capability Mismatch Non-Poisoning:** A `require_vision` request must go straight to the vision-capable secondary; the next text-only turn must still use the primary, proving the mismatch did not trip its breaker.

B4, B5, B8, B10 and B12–B15 are multi-turn or state-checking scenarios: each carries a verify hook that inspects observable state (which provider served each turn, how many times the tool runner executed, what request the secondary actually received) and fails the scenario with a stated reason for any system that gets it wrong.

---

## 2. Multi-Baseline Empirical Results

Seven systems run every scenario through the same harness and are scored by the same rule:

- **Baseline-A-Direct:** one call to the primary; no retry, no fallback.
- **Baseline-B-Same-Provider-Retry:** up to three attempts on the primary; no fallback.
- **Baseline-C-Static-Fallback:** static a → b → c order; no breaker, validation, compaction or ledger.
- **Baseline-D-Breaker-Static-Fallback:** Baseline C guarded by one circuit breaker per provider with V3's configuration; nothing else.
- **Baseline-E-V1-Prototype:** the v0.1 `UniversalFailoverRouter` (round-robin pools, cooldown timers, payload pruning) driven through its real `dispatch` loop, with its upstream HTTP call redirected to the mock providers.
- **Baseline-F-LiteLLM-Router:** an in-process `litellm.Router` instance configuring primary-to-fallback routing through LiteLLM's `CustomLLM` seam onto the mock providers.
- **LLM-Circuit-Breaker-V3:** the current gateway.

| Baseline / System | Completion Rate | Autonomous Recovery | Median Latency | P95 Latency | Semantic Error Rate |
|---|:---:|:---:|:---:|:---:|:---:|
| **LLM-Circuit-Breaker-V3** | **100.0%** | **80.0%** | **12.12 ms** | **313.64 ms** | **0.0%** |
| **Baseline-A-Direct** | 0.0% | 0.0% | 0.02 ms | 0.40 ms | 20.0% |
| **Baseline-B-Same-Provider-Retry** | 20.0% | 20.0% | 0.05 ms | 0.57 ms | 20.0% |
| **Baseline-C-Static-Fallback** | 33.3% | 33.3% | 0.03 ms | 0.32 ms | 20.0% |
| **Baseline-D-Breaker-Static-Fallback** | 33.3% | 33.3% | 0.04 ms | 0.29 ms | 20.0% |
| **Baseline-E-V1-Prototype** | 53.3% | 53.3% | 0.13 ms | 5.07 ms | 20.0% |
| **Baseline-F-LiteLLM-Router** | 33.3% | 33.3% | 7.98 ms | 24.68 ms | 20.0% |

*Takeaway:* Retry and static fallback catch the common HTTP 5xx cases, but **only V3 completes all 15 scenarios**. The V1 prototype's pruner passes the compaction scenarios and its pools pass B12, yet it forwards invalid tool calls, re-executes tools, ignores cost and capability requirements, and its round-robin selection sends B10's recovered turn to the secondary. LiteLLM Router handles basic HTTP 5xx fallbacks (passing B1, B9, B11), but fails compaction on context overflows (B4, B5), forwards invalid tool syntax/schemas (B6, B7, B14), and re-executes duplicate tool calls (B8). Every system is scored by one rule (a response counts only if every delivered tool call passes the real schema validator; attempts are counted from the mock providers' call log). All rows run in one process against the same mock providers, so latencies measure harness overhead; the V3 P95 is dominated by the 1 s `Retry-After` wait honoured in B2.

---

## 3. Primary Research Benchmark (Compound Semantic Failover)

Two turns against three mock providers (`benchmarks/semantic_failover/runner.py`):
1. A ~36k-token history whose root prompt holds a continuation-critical secret is sent to the 128k primary, which answers 503.
2. The gateway fails over to a 32k secondary. The mock rejects oversize input with HTTP 400, so the gateway must compact first; the secondary then emits a schema-invalid tool call.
3. The validator fails closed and a second `FailoverPlan` targets the 32k tertiary, which returns a valid tool call. The harness's tool runner executes it and commits the receipt through the gateway's ledger.
4. The tool's response is lost and the client re-sends the same logical operation. Every hop repeats, but the tertiary's identical tool call arrives marked as replayed with the committed receipt, so the tool runner does not execute it again.

**Measured results (2026-09-06):**
- Task completed, every delivered tool call valid: **True** (semantic error rate 0.0%)
- Critical state preserved: **True** (secret present in the root message the tertiary actually received; latest instruction last)
- Context delivered to the tertiary: **21,661 of 36,071 tokens** (39.9% reduction, under the 32k window)
- Tool executions across both turns: **1** (0 duplicates; the second delivery was replayed from the receipt)
- Fallback hops / `FailoverPlan`s in turn 1: **2 / 2**
- Recovery latency for turn 1: **~1 ms** (in-memory mocks; harness overhead only)

The endpoints declare anthropic/openai/gemini protocols for routing, but the mock adapters do not translate wire formats, so protocol conversion is not exercised by this benchmark.
