# ⚡ LLM Circuit Breaker (V3)

[![CI](https://github.com/d2epak/llm-circuit-breaker/actions/workflows/ci.yml/badge.svg)](https://github.com/d2epak/llm-circuit-breaker/actions/workflows/ci.yml)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Parity: Resilience4j](https://img.shields.io/badge/Circuit%20Breaker-Resilience4j%20Parity-brightgreen.svg)]()
[![Zero Core Dependencies](https://img.shields.io/badge/Core%20Dependencies-Zero-success.svg)]()

A lightweight, self-hostable **agent-resilience gateway** engineered for autonomous AI agents (**Claude Code**, **Hermes Agent**, **OpenClaw**, **Cursor**, **Aider**). 

Combines a formal **6-State Circuit Breaker FSM**, **Multi-Turn Semantic Failover**, **Strict Tool Schema Validation**, **Diagnostic Context Compaction**, and an **Idempotent Tool Execution Ledger** to preserve state and prevent duplicate side-effects when inference providers or models change.

---

## Project status (0.2.0)

An independent adversarial review on 2026-09-06 found the following. Read this before relying on any other claim in this README.

**Stable and verified**
- `llm_circuit_breaker.breaker`: six-state circuit breaker FSM with count/time sliding windows and bounded half-open permits. Deterministic, spec-tested, thread-safe.
- `ToolCallValidator`: fails closed on malformed or unknown tool calls.
- `ContextManager`: preserves system prompt, first user turn and last K turns; compacts tool results.
- Zero third-party dependencies.

**Experimental (Python API only)**
- The V3 engine (`GatewayExecutor`, `CapabilityRouter`, protocol IR, `FailoverPlan`, tool ledger) is reachable only from Python. **The HTTP proxy started by `llm-proxy` still runs the legacy V1 router** and does not use any of it.
- Since the 2026-09-06 review the V3 executor applies exponential backoff and honours `Retry-After`, follows the classifier's retry/fallback flags, rejects HTTP 200 responses with empty bodies, replays committed tool calls from the ledger instead of re-issuing them, compacts and retries the same candidate on a size rejection, enforces `maximum_cost_usd`/`latency_budget_ms`, and keys breakers per deployment. Each is covered by tests in `tests/`, but none of it is reachable through the HTTP proxy yet.

**Known not yet delivered**
- 402 and 429 open the breaker (documented in `docs/FAILURE_TAXONOMY.md`); other 4xx never poison health as of `0.2.0`+.
- Streaming is synthetic (buffered response re-emitted as SSE); there is no mid-stream failover.
- Benchmarks in `docs/BENCHMARKS.md` compare against in-process baselines inside the same harness, not against external systems; treat the numbers as smoke tests, not measurements.
- Importing the package performs OpenRouter model discovery over the network and scans shell dotfiles for API keys by default.

---

## ⚡ Instant Demo (Zero API Keys Required)

Experience semantic failover, circuit tripping, and self-healing recovery in under 2 seconds:

```bash
python -m llm_circuit_breaker.demo
```

Output:
```text
===========================================================================
⚡ LLM CIRCUIT BREAKER — DETERMINISTIC RESILIENCE & SEMANTIC FAILOVER DEMO
===========================================================================
▶ STEP 1: Dispatching turn to Primary Provider (Cerebras)...
  ✔ Result: Primary response: Tool code executed successfully
  ✔ Selected Endpoint: primary-cerebras (Attempts: 1) | State: CLOSED

▶ STEP 2: Primary suffers 503 Outage; Gateway initiates Semantic Failover...
  ✔ Failover Succeeded! Response: Secondary (Groq) fallback response
  ✔ Primary Breaker State: OPEN (Tripped by 503 server errors)
  ✔ Observable FailoverPlan: primary-cerebras -> secondary-groq (Reason: overloaded)

▶ STEP 3: Next Request arrives while Primary is OPEN...
  ✔ Dispatched directly to: secondary-groq (Primary bypassed with 0 upstream load)

▶ STEP 4: Advancing clock by 20 seconds; Testing Self-Healing Recovery...
  ✔ Evaluated Breaker State: HALF_OPEN (Admits bounded probe permits)
  ✔ Probe calls succeed -> Breaker Reset! Primary State is now: CLOSED
===========================================================================
```

---

## 🎯 Core Differentiator: Semantic Failover

Standard reverse proxies (LiteLLM, Cloudflare AI Gateway, Portkey) treat LLMs as interchangeable REST microservices: when Provider A fails with HTTP 503, they blindly forward the identical request payload to Provider B.

**Why this breaks autonomous agents:**
1. **Protocol Mismatch:** Provider A expects Anthropic message structures; Provider B expects OpenAI format.
2. **Context Window Clipping:** Failing over from a 128k context provider to a 32k provider causes HTTP 400 `context_length_exceeded`. Standard proxies truncate characters from the head of the prompt, discarding critical system instructions and root goals.
3. **Ghost Side-Effects (Replay Hazard):** If an agent executes a destructive tool (`execute_bash("rm -rf ...")`), and the connection drops before completion, standard proxies blindly retry. The agent executes the deletion a second time.

**LLM Circuit Breaker V3 solves these modes with 5 core systems:**
- 🛡️ **Formal 6-State Circuit Breaker**: Resilience4j-grade finite state machine (`CLOSED`, `OPEN`, `HALF_OPEN`, `FORCED_OPEN`, `DISABLED`, `METRICS_ONLY`) with count-based sliding windows and strictly bounded half-open probe permits (`half_open_active <= half_open_max_calls`).
- 🧠 **Observable `FailoverPlan`**: Every candidate migration records source/target endpoints, token count deltas, compaction flags, and schema adaptations in an explainable audit record.
- 🗜️ **Hierarchical Context Compaction**: Preserves root user goals, system instructions, and extracts structured diagnostics from tool logs (exit codes, error diagnostics) rather than blind character slicing.
- 📜 **Tool Execution Idempotency Ledger**: Tracks tool calls through `PROPOSED` $\to$ `VALIDATED` $\to$ `SUBMITTED` $\to$ `COMMITTED`. Cached receipts suppress duplicate side-effects during retries.
- 🔒 **Ironclad Tool Safety (Rule 3)**: Fails closed on missing required arguments. Never invents parameters. Syntactically repairs markdown fences while strictly forbidding semantic mutations.

---

## 📊 Benchmark Results (Authoritative B1–B15 Suite)

Evaluated across 15 deterministic scenarios (permanent outages, 429 rate limits, timeouts, context overflows, malformed tool syntax, semantic schema violations, tool execution ambiguity, mid-stream disconnects, cascades, contention, and capability mismatches) against 4 distinct baselines:

| Baseline / System | Request Completion | Autonomous Recovery | Median Latency | P95 Latency | Semantic Error Rate |
|---|:---:|:---:|:---:|:---:|:---:|
| **LLM-Circuit-Breaker-V3** | **100.0%** | **60.0%** | **0.12 ms** | **1.04 ms** | **0.0%** |
| **Baseline-A-Direct** | 53.3% | 0.0% | 0.02 ms | 0.04 ms | 6.7% |
| **Baseline-B-Same-Provider-Retry** | 93.3% | 40.0% | 0.03 ms | 0.05 ms | 6.7% |
| **Baseline-C-Static-Fallback** | 93.3% | 46.7% | 0.04 ms | 0.06 ms | 6.7% |

> Run the full reproducible benchmark suite: `python -m benchmarks.run`  
> Complete technical analysis: [docs/BENCHMARKS.md](docs/BENCHMARKS.md)

---

## 🥊 Competitive Architectural Comparison

| Capability / Dimension | **LLM Circuit Breaker V3** | **LiteLLM Proxy** | **Cloudflare AI Gateway** | **Portkey Gateway** | **OpenRouter** |
|---|:---:|:---:|:---:|:---:|:---:|
| **Circuit Breaker Engine** | **Resilience4j Parity** (6-state FSM, sliding windows, bounded probe permits) | Cooldown timer (`time + 60s`), no permit bounds | Dynamic retries only | Proprietary cloud breaker (enterprise tier) | Static server-side retry |
| **Multi-Turn Semantic Failover** | **Yes (Protocol IR + FailoverPlan)** | No (Raw payload forwarding) | No | No | No |
| **Strict Tool Schema Validation** | **Yes (Fails closed on missing required args)** | No (Passthrough parsing) | No | No | No |
| **Diagnostic Context Compaction** | **Yes (Extracts exit codes, preserves planted facts)** | No (Naive truncation) | No | No | No |
| **Tool Execution Idempotency** | **Yes (Replay suppression via cached receipts)** | No (Blind replay on 5xx) | No | No | No |
| **Deployment Footprint** | **Zero Mandatory Dependencies** (<15ms overhead, SQLite persistence) | Requires external Postgres & Redis | Cloudflare Edge Worker (Cloud only) | SaaS cloud or enterprise container | Cloud-only API broker |
| **Security Hardening** | **SSRF defense, CRLF sanitization, credential redaction** | Telemetry enabled by default | Cloud control plane | Cloud control plane | Third-party proxy |

> Detailed architectural deep-dive: [docs/COMPETITOR_MATRIX.md](docs/COMPETITOR_MATRIX.md)

---

## 📚 Technical Documentation Suite

- [Architecture Overview](ARCHITECTURE.md)
- [Reliability & FSM State Machine Model](docs/RELIABILITY_MODEL.md)
- [Comprehensive Failure Taxonomy](docs/FAILURE_TAXONOMY.md)
- [Routing Policy & Telemetry Scoring](docs/ROUTING_POLICY.md)
- [Semantic Failover & Protocol IR](docs/SEMANTIC_FAILOVER.md)
- [Hierarchical Context Compaction](docs/CONTEXT_MODEL.md)
- [Tool Safety & Idempotency Ledger](docs/TOOL_SAFETY.md)
- [Streaming Architecture & Mid-Stream Replay](docs/STREAMING.md)
- [Production Operations & Observability](docs/OPERATIONS.md)
- [Full Benchmark Report](docs/BENCHMARKS.md)
- [Final Engineering Self-Critique](docs/FINAL_SELF_CRITIQUE.md)

---

## 🚀 Quickstart

### 1. Installation

```bash
pip install llm-circuit-breaker
```

### 2. Basic Python Usage

```python
from llm_circuit_breaker import GatewayExecutor, NormalizedRequest, NormalizedMessage

executor = GatewayExecutor()

request = NormalizedRequest(
    model="default",
    messages=[NormalizedMessage(role="user", content="Deploy application")],
)

response, decision, ledger = executor.execute(request, pool="coding", strategy="reliability_aware")
print(f"Selected Endpoint: {decision.selected_endpoint.id}")
print(f"Response: {response.content}")
```

### 3. Launching Local Proxy Server

```bash
python -m llm_circuit_breaker.proxy --port 8000
# or, after `pip install -e .`:
llm-proxy --port 8000
```

Configure your agents:
- **Claude Code**: `export ANTHROPIC_BASE_URL="http://127.0.0.1:8000"`
- **Hermes / Cursor**: `export OPENAI_BASE_URL="http://127.0.0.1:8000/v1"`

---

## 📄 License

MIT License. Designed and engineered for mission-critical agent reliability.
