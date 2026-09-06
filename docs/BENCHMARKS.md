# Reproducible Benchmarks & Empirical Evaluation

This document details the design, implementation, and results of the **B1 through B15 Benchmark Suite**, the **3 Comparative Baselines**, and the **Primary Research Benchmark** in **LLM Circuit Breaker (V3)**.

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

| Baseline / System | Completion Rate | Autonomous Recovery | Median Latency | P95 Latency | Semantic Error Rate |
|---|:---:|:---:|:---:|:---:|:---:|
| **LLM-Circuit-Breaker-V3** | **100.0%** | **80.0%** | **11.77 ms** | **312.85 ms** | **0.0%** |
| **Baseline-A-Direct** | 0.0% | 0.0% | 0.03 ms | 0.07 ms | 20.0% |
| **Baseline-B-Same-Provider-Retry** | 20.0% | 20.0% | 0.07 ms | 0.33 ms | 20.0% |
| **Baseline-C-Static-Fallback** | 33.3% | 33.3% | 0.07 ms | 0.19 ms | 20.0% |

*Takeaway:* Retry and static fallback catch the common HTTP 5xx cases, but **only V3 completes all 15 scenarios**. Every system is scored by one rule (a response counts only if every delivered tool call passes the real schema validator, attempts are counted from the mock providers' call log), so each baseline forwards the invalid tool calls of B6, B7 and B14 (20.0% semantic error rate). All rows run in one process against the same mock providers, so latencies measure harness overhead; the V3 P95 is dominated by the 1 s `Retry-After` wait honoured in B2.

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
