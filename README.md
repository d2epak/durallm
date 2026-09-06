# ⚡ LLM Circuit Breaker

[![CI](https://github.com/d2epak/llm-circuit-breaker/actions/workflows/ci.yml/badge.svg)](https://github.com/d2epak/llm-circuit-breaker/actions/workflows/ci.yml)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Circuit Breaker: 6-state FSM](https://img.shields.io/badge/Circuit%20Breaker-6--state%20FSM-brightgreen.svg)]()
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
- **Agent Continuation Protocol v1 (ACP)**: versioned session IDs, turn checkpoints, and operation lifecycle receipts (`PREPARED`, `SUBMITTED`, `ACKNOWLEDGED`, `INDETERMINATE`).
- **Durable Persistence**: SQLite WAL store for atomic session resumption, write-ahead attempt intent, and idempotent tool-operation receipts.
- **Client Compatibility Matrix**: recorded test suites covering Claude Code, OpenCode, Hermes Agent, and OpenClaw (`docs/CLIENT_COMPATIBILITY.md`).
- **Calibrated Task Selection**: conservative tokenizer preflight, independent credential/deployment resource lane stores, pre-dispatch atomic budget reservations, and shadow quality policy evaluation with confidence calibration.
- Zero third-party core dependencies.
- Importing the package makes no network call and reads no dotfiles. OpenRouter discovery and `~/.zshrc`/`.env` key scanning are opt-in (`LLM_BREAKER_AUTO_DISCOVER=1`, `LLM_BREAKER_SCAN_DOTFILES=1`, or `llm-proxy --discover`).
- The `llm-proxy` HTTP server (`/v1/messages`, `/v1/chat/completions`) serves every request through `GatewayExecutor`: breaker admission, exponential backoff that honours `Retry-After`, classifier-driven retry/fallback, rejection of empty HTTP 200 bodies, fail-closed tool validation, ledger replay of committed tool calls, compact-and-retry on size rejections, and breakers keyed per deployment. Covered by `tests/test_proxy_gateway.py` with mock adapters.
- Security defaults: upstream URLs that resolve to loopback or RFC 1918 addresses are refused unless `LLM_BREAKER_ALLOW_LOCAL_UPSTREAM=1` (cloud metadata hosts are always refused); request and response bodies above 10 MB are rejected; every upstream attempt and proxy response is emitted as a redacted JSON event on the `llm_circuit_breaker.events` logger.

**Experimental**
- The proxy and executor have been exercised primarily against mock adapters and recorded contract fixtures. Live upstream validation against real vendor credentials remains opt-in.
- `CapabilityRouter` cost/latency constraints, the Gemini codec and Anthropic thinking-signature passthrough are tested with recorded shapes.

**Known not yet delivered**
- 402 and 429 open the breaker (documented in `docs/FAILURE_TAXONOMY.md`); other 4xx never poison health as of `0.2.0`+.
- Streaming defaults to atomic-buffered SSE replay. Opt-in `X-LCB-Streaming-Mode: true_streaming` relays a compatible provider's native SSE with phase deadlines and cancellation; after any visible bytes it emits an explicit interruption event rather than splicing a fallback model. Tool and ACP turns remain atomic-buffered.
- Nothing enters the `METRICS_ONLY` breaker state; the state exists in the FSM but no API selects it.

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

**LLM Circuit Breaker addresses these modes with 5 core systems:**
- 🛡️ **Formal 6-State Circuit Breaker**: Finite state machine (`CLOSED`, `OPEN`, `HALF_OPEN`, `FORCED_OPEN`, `DISABLED`, `METRICS_ONLY`) with count-based sliding windows and strictly bounded half-open probe permits (`half_open_active <= half_open_max_calls`).
- 🧠 **Observable `FailoverPlan`**: Every candidate migration records source/target endpoints, token count deltas, compaction flags, and schema adaptations in an explainable audit record.
- 🗜️ **Hierarchical Context Compaction**: Preserves root user goals, system instructions, and extracts structured diagnostics from tool logs (exit codes, error diagnostics) rather than blind character slicing.
- 📜 **Tool Execution Idempotency Ledger**: Tracks tool calls through `PROPOSED` $\to$ `VALIDATED` $\to$ `SUBMITTED` $\to$ `COMMITTED`. Cached receipts suppress duplicate side-effects during retries.
- 🔒 **Ironclad Tool Safety (Rule 3)**: Fails closed on missing required arguments. Never invents parameters. Syntactically repairs markdown fences while strictly forbidding semantic mutations.

---

## 📊 Benchmark Results (B1–B15 in-process suite)

Evaluated across 15 deterministic scenarios (permanent outages, 429 rate limits, timeouts, context overflows, malformed tool syntax, semantic schema violations, tool execution idempotency, mid-stream disconnects, cascades, pool isolation, cost ceilings, tool-reliability routing, and capability mismatches) against 6 in-process baselines. Numbers are copied from the multi-run benchmark report (`results/2026-09-06-1943ba8/report.md`), generated with 3 runs and seed 42 on 2026-09-06:

| Baseline / System | Request Completion | Autonomous Recovery | Median Latency | P95 Latency | Semantic Error Rate |
|---|:---:|:---:|:---:|:---:|:---:|
| **LLM-Circuit-Breaker-V3** | **100.0%** | **80.0%** | **12.12 ms** | **313.64 ms** | **0.0%** |
| **Baseline-A-Direct** | 0.0% | 0.0% | 0.02 ms | 0.40 ms | 20.0% |
| **Baseline-B-Same-Provider-Retry** | 20.0% | 20.0% | 0.05 ms | 0.57 ms | 20.0% |
| **Baseline-C-Static-Fallback** | 33.3% | 33.3% | 0.03 ms | 0.32 ms | 20.0% |
| **Baseline-D-Breaker-Static-Fallback** | 33.3% | 33.3% | 0.04 ms | 0.29 ms | 20.0% |
| **Baseline-E-V1-Prototype** | 53.3% | 53.3% | 0.13 ms | 5.07 ms | 20.0% |
| **Baseline-F-LiteLLM-Router** | 33.3% | 33.3% | 7.98 ms | 24.68 ms | 20.0% |

> All seven rows run in one process against the same mock providers (Baseline D adds V3's breaker to static fallback; Baseline E is the v0.1 router driven through its own dispatch loop; Baseline F drives an in-process `litellm.Router` instance via LiteLLM's `CustomLLM` seam). Every row is scored by the same rule (a turn counts only if every delivered tool call passes the schema validator), so the baselines' semantic errors are the invalid tool calls they forward in B6, B7 and B14. Multi-turn scenarios are judged by verify hooks on observable state (which provider served each turn, how often the tool ran, what the secondary received). The V3 P95 is dominated by the 1 s `Retry-After` wait honoured in B2; the baselines never wait.
> Run the full reproducible benchmark suite: `python -m benchmarks.run`  
> Complete technical analysis: [docs/BENCHMARKS.md](docs/BENCHMARKS.md)

---

## 🥊 Competitive Architectural Comparison

| Capability / Dimension | **LLM Circuit Breaker** | **LiteLLM Proxy** | **Cloudflare AI Gateway** | **Portkey Gateway** | **OpenRouter** |
|---|:---:|:---:|:---:|:---:|:---:|
| **Circuit Breaker Engine** | **6-state FSM**, count/time sliding windows, bounded probe permits | Cooldown timer (`time + 60s`), no permit bounds | Dynamic retries only | Proprietary cloud breaker (enterprise tier) | Static server-side retry |
| **Multi-Turn Semantic Failover** | **Yes (Protocol IR + FailoverPlan)** | No (Raw payload forwarding) | No | No | No |
| **Strict Tool Schema Validation** | **Yes (Fails closed on missing required args)** | No (Passthrough parsing) | No | No | No |
| **Diagnostic Context Compaction** | **Yes (Extracts exit codes, preserves root prompt and tail turns)** | No (Naive truncation) | No | No | No |
| **Tool Execution Idempotency** | **Yes (Replay suppression via cached receipts)** | No (Blind replay on 5xx) | No | No | No |
| **Deployment Footprint** | **Zero mandatory dependencies**; optional SQLite persistence | Requires external Postgres & Redis | Cloudflare Edge Worker (Cloud only) | SaaS cloud or enterprise container | Cloud-only API broker |
| **Security Hardening** | **SSRF defense, CRLF sanitization, credential redaction** | Telemetry enabled by default | Cloud control plane | Cloud control plane | Third-party proxy |

> Competitor columns summarise public documentation as of 2026-09-06 and were not tested by this project.
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
- [Streaming Architecture & Interruption Boundary](docs/STREAMING.md)
- [Recorded Client Compatibility Matrix](docs/CLIENT_COMPATIBILITY.md)
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
import os

from llm_circuit_breaker import Endpoint, GatewayExecutor, ModelProfile, NormalizedMessage, NormalizedRequest

executor = GatewayExecutor()

# Nothing is registered by default: declare at least one endpoint in the pool you will call.
executor.capability_registry.register_endpoint(Endpoint(
    id="groq-llama",
    provider="groq",
    model="llama-3.3-70b-versatile",
    base_url="https://api.groq.com/openai/v1",
    env_key="GROQ_API_KEY",          # name of the key looked up in `api_keys` below
    pool="coding",
    profile=ModelProfile("groq", "llama-3.3-70b-versatile", context_window=131072, supports_tools=True),
))

request = NormalizedRequest(
    model="default",
    messages=[NormalizedMessage(role="user", content="Deploy application")],
)

response, decision, ledger = executor.execute(
    request,
    pool="coding",
    strategy="reliability_aware",
    api_keys={"GROQ_API_KEY": os.environ["GROQ_API_KEY"]},
)
print(f"Selected Endpoint: {decision.selected_endpoint.id}")
print(f"Response: {response.content}")
```

### 3. Launching Local Proxy Server

```bash
python -m llm_circuit_breaker.proxy            # binds 127.0.0.1:4001 by default
# or, after `pip install -e .`:
llm-proxy --port 4001
```

Configure your agents:
- **Claude Code**: `export ANTHROPIC_BASE_URL="http://127.0.0.1:4001"`
- **Hermes / Cursor**: `export OPENAI_BASE_URL="http://127.0.0.1:4001/v1"`

---

## 📄 License

MIT License. Designed and engineered for mission-critical agent reliability.
