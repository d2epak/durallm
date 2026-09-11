# DuraLLM V3 — Architecture Specification

This document provides a comprehensive technical reference for the architecture, subsystems, and invariants of the DuraLLM V3 gateway.

---

## 1. High-Level Architecture Overview

```mermaid
graph TD
    Client[AI Agents: Claude Code / Hermes / OpenClaw] -->|HTTP / SSE| Gateway[Gateway Server / Proxy]
    Gateway --> Security[Security Defense Layer\nSSRF, CRLF, Payload Limits, Redaction]
    Security --> IR[Protocol Intermediate Representation IR]
    IR --> Router[Capability-Aware Router]
    
    subgraph Routing & Telemetry Pipeline
        Router --> HardFilter[Hard Constraint Filter\nTools, Vision, Context Size, Privacy]
        HardFilter --> BreakerFilter[Circuit Breaker Admission Check]
        BreakerFilter --> Scorer[Real Telemetry Scorer\nObserved EMA Latency, Tool Success Rate, Cold-Start Policy]
        Scorer --> DecisionRecord[Explainable Decision Audit Record]
    end

    subgraph Execution & Resilience Loop
        DecisionRecord --> Executor[Gateway Executor]
        Executor --> Plan[FailoverPlan Generator]
        Executor --> ContextMgr[Budget-Aware Context Compactor\nStructured Tool Diagnostic Extraction]
        ContextMgr --> Adapter[Provider Adapter]
        Adapter --> Upstream[Upstream LLM Provider]
        Upstream --> RespVal[Response Validator\nEmpty 200 Check & Size Bomb Defense]
        RespVal --> Classify[Hierarchical Error Classifier\n16 Failover Reasons]
        Classify --> BreakerEngine[Resilience4j Circuit Breaker FSM]
        Classify --> Idempotency[Tool Execution Idempotency Ledger]
    end

    subgraph Persistence Store
        BreakerEngine -.-> SQLite[(Optional SQLite WAL Store)]
        Idempotency -.-> SQLite
    end

    Executor -->|Synthetic or True SSE| Client
```

---

## 2. Finite State Machine (FSM)

The circuit breaker operates as an atomic finite state machine with 6 discrete states:

```mermaid
stateDiagram-v2
    [*] --> CLOSED
    CLOSED --> OPEN: Failure rate >= threshold\nOR Slow call rate >= threshold
    OPEN --> HALF_OPEN: Wait duration expires (wait_duration_open_ms)
    HALF_OPEN --> CLOSED: Configured probe permits succeed (half_open_max_calls)
    HALF_OPEN --> OPEN: Any probe call fails OR probe timeout elapsed
    CLOSED --> FORCED_OPEN: Administrative lock
    OPEN --> FORCED_OPEN: Administrative lock
    FORCED_OPEN --> CLOSED: Administrative reset
    CLOSED --> DISABLED: Protection disabled
    CLOSED --> METRICS_ONLY: Passive tracking
```

### Core Invariants:
1. **Permit-Bounded Probes**: When in `HALF_OPEN`, concurrent calls are bounded strictly to `half_open_max_calls` permits. Additional concurrent callers receive `ProbeAdmissionDeniedError` and are routed to alternative candidates.
2. **Zero Upstream Calls When OPEN**: Calls targeting an `OPEN` circuit breaker are immediately rejected with `BreakerOpenError` without placing any network load on the downstream service.
3. **Non-Poisoning Faults**: Errors classified as `SEMANTIC_AGENT_FAILURE`, `CLIENT_ERROR`, or `REQUEST_INCOMPATIBILITY` (such as 400 schema rejections or malformed tool JSON) do not increment failure metrics and cannot trip the breaker.
4. **Tool Idempotency**: Side-effecting tool calls executed before a mid-stream connection drop are recorded in the `ToolExecutionLedger`. Replays on fallback providers attach cached receipts to suppress duplicate execution.

---

## 3. Request Lifecycle Sequence

```mermaid
sequenceDiagram
    autonumber
    actor Agent as Autonomous Agent
    participant Gateway as Gateway Executor
    participant Router as Capability Router
    participant Breaker as Circuit Breaker
    participant Context as Context Compactor
    participant Upstream as Provider Upstream
    participant Safety as Tool Safety Validator
    participant Ledger as Tool Idempotency Ledger

    Agent->>Gateway: POST /v1/messages or /v1/chat/completions
    Gateway->>Gateway: Sanitize Headers & Enforce Payload Limits
    Gateway->>Gateway: Translate Native Request to Protocol IR
    Gateway->>Router: Select Candidate (Requirements, Active Pool)
    Router->>Breaker: Check Breaker State & Probe Permits
    Breaker-->>Router: Admitted (CLOSED or Bounded Probe)
    Router-->>Gateway: Candidate Selected (e.g. Anthropic)
    Gateway->>Context: Verify Token Budget against Model Window
    Context-->>Gateway: Adapted / Compacted Request
    Gateway->>Upstream: Execute HTTP Request
    alt Upstream Fails (503 / 429 / Timeout)
        Upstream-->>Gateway: Failure Response
        Gateway->>Breaker: Record Failure (Update Sliding Window)
        Gateway->>Gateway: Generate Observable FailoverPlan
        Gateway->>Router: Trigger Fallback (Exclude Failed Endpoint)
        Router-->>Gateway: Next Candidate Selected (e.g. Gemini)
        Gateway->>Upstream: Execute Fallback Request
    end
    Upstream-->>Gateway: 200 OK Response
    Gateway->>Safety: Validate Tool Call Schema (Fail Closed)
    Gateway->>Ledger: Check & Commit Tool Execution Receipt
    Gateway->>Breaker: Record Success (Observed Latency)
    Gateway-->>Agent: SSE Stream or JSON Response
```

---

## 4. Subsystem Details

### A. Protocol Intermediate Representation (IR)
The gateway converts all incoming payloads into canonical `NormalizedRequest` / `NormalizedResponse` dataclasses. This eliminates $M \times N$ translator sprawl, converting provider integrations into an $O(N)$ architecture. Thinking/reasoning blocks (Claude 3.7, Gemini thinking) and multi-part content are preserved end-to-end.

### B. Observable `FailoverPlan`
When a candidate migration occurs, the gateway emits an immutable `FailoverPlan` recording source and target endpoints, token counts before and after compaction, remaining deadlines, and reason for failover.

### C. Hierarchical Context Compaction & Diagnostic Extraction
When falling back across models with different context windows (128k $\to$ 32k tokens):
1. System instructions, root user goals, and planted continuation secrets are strictly preserved.
2. The latest execution turns are preserved intact.
3. Historical tool execution logs are parsed for exit codes, error messages, and file paths; noisy multi-megabyte payloads are compressed into structured diagnostic summaries.

### D. Tool Safety & Replay Idempotency
- **Rule 1 (Fail Closed):** If a model generates missing required parameters, the call is rejected without guessing.
- **Rule 2 (Syntactic Repair Only):** Markdown fences and trailing commas are repaired; argument types and parameter names are never altered.
- **Rule 3 (Idempotent Replay):** Tool execution receipts are stored in the `ToolExecutionLedger`. Replays attach cached receipts, preventing duplicate external side-effects.

### E. Security Hardening
- SSRF prevention blocks requests to loopback (`127.0.0.1`), link-local (`169.254.169.254`), and private cloud metadata endpoints.
- CRLF injection prevention sanitizes outbound HTTP headers.
- Credential masking replaces API keys and bearer tokens with `[REDACTED]` in all structured logs.
