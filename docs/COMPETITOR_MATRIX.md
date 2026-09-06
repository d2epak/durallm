# Architecture & Competitive Matrix: LLM Gateway Systems

An authoritative, technical comparison between **LLM Circuit Breaker (V3)** and major commercial and open-source LLM gateways: **LiteLLM**, **Cloudflare AI Gateway**, **Portkey**, **Braintrust**, **OpenRouter**, **Kong AI Gateway**, and **Envoy AI Gateway**.

---

## 1. Executive Comparison Matrix

| Gateway System | Real FSM Circuit Breaker | Multi-Turn Semantic Failover | Strict Tool Schema Validation | Diagnostic Context Compaction | Tool Idempotency Ledger | Self-Hostable Weight | Primary Focus |
|---|:---:|:---:|:---:|:---:|:---:|:---:|---|
| **LLM Circuit Breaker (V3)** | **Yes (Count-based FSM + Bounded Probes)** | **Yes (Full Protocol IR + FailoverPlan)** | **Yes (Fails closed on missing schema)** | **Yes (Structured diagnostic extraction)** | **Yes (Replay suppression receipts)** | **Lightweight (Zero core dependencies, in-process or proxy daemon)** | **Agent Resilience & Semantic Integrity** |
| **LiteLLM** | No (Static cooldown timestamps) | Partial (Cross-provider fallback, loose context) | No (Passthrough parsing) | No (Naive character truncation) | No (Blind replay on 5xx) | Medium (Python proxy server) | Unified API Proxy & Model Breadth |
| **Cloudflare AI Gateway** | No (Dynamic retries & caching) | No (Basic static fallback) | No (Byte-level passthrough) | No | No | Closed Cloud / Edge Worker | Caching & Rate Limiting at Edge |
| **Portkey** | Partial (Simple error thresholds) | No (Routing fallbacks only) | No | No | No | Hybrid (Control plane cloud / local agent) | Enterprise Governance & Tracing |
| **Braintrust** | No | No (Proxy fallbacks) | No | No | No | Cloud / SaaS | LLM Observability & Evaluation |
| **OpenRouter** | No (Dynamic provider routing) | No (Internal routing failover) | No | No | No | Hosted SaaS API | Commercial Aggregation & Model Routing |
| **Kong AI Gateway** | Partial (Upstream passive healthcheck) | No (HTTP status failover) | No (Payload agnostic) | No | No | Heavy (Kong Enterprise / Nginx C) | API Gateway & Enterprise Token Governance |
| **Envoy AI Gateway** | Partial (Upstream outlier detection) | No (HTTP retry filter) | No (HTTP filter) | No | No | Heavy (Envoy C++ sidecar) | Mesh Routing & Protocol Transcoding |

---

## 2. Deep-Dive Architectural Differences

### 2.1 Circuit Breaking: Real FSM vs. Cooldown Heuristics

- **The Industry Trend (LiteLLM, OpenRouter, Portkey):**
  Most systems implement "cooldowns" rather than circuit breakers:
  ```python
  # Naive Cooldown Pattern (Used by LiteLLM)
  if error_status in [429, 500, 503]:
      cooldown_until = time.time() + 60
  ```
  *Defects:* Cooldown heuristics do not calculate sliding error rates, do not monitor slow calls, do not isolate transient glitches from real outages, and when the cooldown expires, all queued production traffic thunders back onto the struggling upstream simultaneously (the **Thundering Herd** problem).
- **LLM Circuit Breaker V3:**
  Implements a strict finite-state machine (CLOSED $\to$ OPEN $\to$ HALF-OPEN $\to$ CLOSED) with:
  - Sliding count-based circular window.
  - Failure rate and slow-call rate thresholds evaluated concurrently.
  - State transitions logged with causal audit reasons.
  - **Bounded Probe Admission**: In HALF-OPEN state, strictly $N$ concurrent permits (`half_open_max_calls`) are admitted. If 100 requests arrive, only $N$ test probes reach upstream; the remaining 99 are either fast-failed or safely routed to healthy fallback candidates without touching the recovering provider.

---

### 2.2 Semantic Failover vs. Status-Code Fallback

- **The Industry Trend (All Competitors):**
  Gateways treat LLMs as interchangeable REST microservices: when Provider A returns HTTP 503, the gateway forwards the identical request payload to Provider B.
  *Why Agents Break:*
  1. **Protocol Mismatch:** Provider A expects Anthropic message structures with system blocks; Provider B expects OpenAI format.
  2. **Context Window Clipping:** If Provider A had a 128k context window and Provider B has 32k, the request fails with HTTP 400 `context_length_exceeded`.
  3. **Tool Incompatibility:** Provider B may format tools with subtle differences, or emit tool arguments with invalid schemas.
- **LLM Circuit Breaker V3:**
  Treats model switches as **stateful semantic migrations**:
  - Request is decoded into a canonical, lossless **Protocol Intermediate Representation (IR)**.
  - The Gateway creates an observable `FailoverPlan` recording the candidate migration, token differential, and compaction rules.
  - Dynamic **Context Compaction** reduces conversation history while guaranteeing that facts planted in the root prompt, system directives, and structured tool diagnostic logs (exit codes, error messages) are preserved.

---

### 2.3 Tool Safety and Replay Idempotency

- **The Danger of Blind Retries:**
  Autonomous agents invoke side-effecting tools: `execute_bash("rm -rf ...")`, `charge_credit_card(...)`, `send_email(...)`.
  If an upstream provider executes the tool call, begins streaming, but disconnects or drops the connection before returning the complete HTTP response:
  - Standard gateways blindly retry the turn on Provider B.
  - Provider B re-emits the same tool call.
  - The agent executes the payment or file deletion a second time.
- **LLM Circuit Breaker V3:**
  Features the **Tool Execution Ledger**:
  - Tracks tool calls through explicit state transitions: `PROPOSED` $\to$ `VALIDATED` $\to$ `SUBMITTED` $\to$ `COMMITTED`.
  - Once a tool call executes, its execution receipt is cryptographically fingerprinted and cached.
  - On retry or failover, if the candidate reproduces the identical tool call, the gateway attaches the cached receipt, suppressing duplicate side-effects.

---

## 3. Position and Target Niche

LLM Circuit Breaker is not designed to replace high-throughput enterprise API token routers managing thousands of consumer applications.

It is engineered specifically as a **lightweight, self-hostable resilience gateway for autonomous coding agents, multi-agent swarms, and mission-critical workflows** where task continuity, tool safety, and semantic preservation are paramount.
