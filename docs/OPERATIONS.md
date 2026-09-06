# Production Operations, Deployment & Observability

This guide outlines deployment topologies, configuration management, health telemetry, and persistence for **LLM Circuit Breaker (V3)**.

---

## 1. Deployment Topologies

1. **In-Process Python SDK (Zero Daemon):**
   Integrated directly into Python agent processes (`from llm_circuit_breaker import GatewayExecutor`). Zero external dependencies, minimal latency overhead (<1ms).
2. **Local Sidecar / Gateway Server:**
   Runs as a lightweight HTTP microservice on `127.0.0.1:4001` (the default for `llm-proxy`, `GatewayConfig.port` and `LLM_BREAKER_PORT`) mediating requests for multi-process or multi-language agents.
3. **Optional SQLite Persistence:**
   Set `LLM_BREAKER_STATE_DB=/absolute/path/gateway.db` before starting the
   proxy. The `SQLitePersistenceStore` enables WAL-backed ACP session state,
   write-ahead provider attempts, and durable tool-operation receipts. Read
   `DURABLE_STATE.md` before treating an external tool action as safe to
   replay: a lost acknowledgement is intentionally `INDETERMINATE`.

---

## 2. Telemetry and Prometheus Metrics

The gateway exposes Prometheus metrics:
- `llm_circuit_breaker_calls_total{endpoint, status}`
- `llm_circuit_breaker_latency_ms{endpoint}`
- `llm_circuit_breaker_state{endpoint, state}`
- `llm_circuit_breaker_failover_plans_total{source, target, reason}`
- `llm_circuit_breaker_tool_idempotency_hits_total{tool_name}`

---

## 3. Security Hardening Check

- **SSRF Prevention:** Upstream URLs are validated against private network addresses (`127.0.0.1`, `169.254.169.254`, `10.0.0.0/8`).
- **CRLF Injection:** Request headers are sanitized, stripping `\r` and `\n`.
- **Credential Redaction:** API keys and bearer tokens are automatically masked as `[REDACTED]` in all structured logs.
- **Payload Limits:** Inbound requests exceeding 10MB are rejected before parsing.
