# Migration Guide: Upgrading from V1 Prototype to V2 Gateway

This guide details the architectural upgrades and API changes when migrating from `llm-circuit-breaker` v0.2.0 prototype to V2.

---

## 1. Backwards Compatibility Guarantee

V2 is **100% backward-compatible** with existing V1 code and scripts:
- Existing imports from `llm_circuit_breaker` continue to function without modification:
  ```python
  from llm_circuit_breaker import (
      UniversalFailoverRouter,
      POOL_MANAGER,
      IsolatedPoolManager,
      RouteDefinition,
      prune_anthropic_request,
      prune_openai_request,
      anthropic_to_openai_request,
      openai_to_anthropic_response,
      start_proxy_server,
  )
  ```
- The local gateway CLI commands (`llm-proxy` and `llm-discover`) work as before.
- Existing environment variables (`GEMINI_API_KEY`, `GROQ_API_KEY`, `CEREBRAS_API_KEY`, `OPENROUTER_API_KEY`, `MISTRAL_API_KEY`, `NVIDIA_API_KEY`, `GATEWAY_TIMEOUT`, `GATEWAY_PORT`) continue to be honored.

---

## 2. Key Differences Between V1 and V2

| Feature | V1 Prototype (v0.2.0) | V2 Resilience Gateway |
|---|---|---|
| **Circuit Breaker** | Cooldown timestamp (`monotonic + 60s`) | Formal 6-state FSM (`CLOSED`, `OPEN`, `HALF_OPEN`, `FORCED_OPEN`, `DISABLED`, `METRICS_ONLY`) with sliding window & permit-bounded probes |
| **Model Selection** | Priority integer in static list | Capability-aware multi-objective scorer (quality, reliability, latency, cost) with hard constraint verification |
| **Protocol Translation** | Ad-hoc pairwise dictionary conversions | Canonical Protocol Intermediate Representation (`NormalizedRequest`, `NormalizedResponse`) |
| **Context Compaction** | Truncates messages by index | Hierarchical budget-aware compaction (never drops root task or active constraints) |
| **Tool Calling Safety** | Permissive regex stripping | Strict schema validation; safe syntactic normalization; zero hallucinated arguments |
| **Gemini Security** | API key exposed in URL query string | Secure `x-goog-api-key` HTTP header transport |
| **Process State** | Mutated global `socket.setdefaulttimeout()` | Thread-safe, per-request deadline timeouts |
| **Cycle Protection** | None (could loop indefinitely) | `AttemptLedger` detects cycles ($A \to B \to A$) and enforces fallback depth limits |

---

## 3. Upgrading to V2 Capabilities

### Using the New Gateway Executor Directly in Python
```python
from llm_circuit_breaker import (
    GatewayExecutor,
    NormalizedRequest,
    NormalizedMessage,
    NormalizedToolDefinition,
    ExecutionPolicy,
    RetryPolicy,
    FallbackPolicy,
)

# Initialize executor with custom policy
executor = GatewayExecutor(
    policy=ExecutionPolicy(
        retry=RetryPolicy(max_attempts_same_endpoint=2),
        fallback=FallbackPolicy(max_fallback_hops=3),
    )
)

# Dispatch request
req = NormalizedRequest(
    model="default",
    messages=[
        NormalizedMessage(role="user", content="Analyze repository structure"),
    ],
    tools=[
        NormalizedToolDefinition(
            name="list_dir",
            description="List files in a directory",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        )
    ]
)

response, decision, ledger = executor.execute(req, pool="coding", strategy="balanced")

print(f"Selected: {decision.selected_endpoint.provider}/{decision.selected_endpoint.model}")
print(f"Total attempts: {ledger.total_attempts}, Fallbacks: {ledger.fallback_count}")
print(f"Content: {response.content}")
```

### Inspecting Circuit Breaker Health
```python
from llm_circuit_breaker import DEFAULT_BREAKER_REGISTRY

# Inspect all breaker states
for name, breaker in DEFAULT_BREAKER_REGISTRY.all().items():
    snapshot = breaker.snapshot()
    print(f"Breaker {name}: state={snapshot['state']}, metrics={snapshot['metrics']}")
```

### Running the V2 Benchmark Suite
```bash
uv run python -m benchmarks.run
```
Outputs comprehensive performance and recovery stats across scenarios B1–B10 and updates `results/v2_benchmark_report.md`.
