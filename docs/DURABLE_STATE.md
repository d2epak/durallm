# Durable Session, Attempt and Tool-Operation State

Set `LLM_BREAKER_STATE_DB=/absolute/path/gateway.db` when starting
`llm-proxy` to opt into the local SQLite/WAL durable backend. Without it, the
gateway is deliberately process-local and should be treated as best-effort.

The SQLite backend exposes three portable boundaries for a future shared-store
implementation: `SessionStore`, `AttemptStore`, and `OperationStore`. It uses
revisioned session records plus expiring, fenced leases. SQLite's write lock
makes these transitions atomic across gateway processes on the same local disk;
it is not a multi-host consensus system.

## Persisted transitions

1. **Session:** ACP session/checkpoint state is saved with compare-and-swap
   revision control. A worker must hold the session lease to advance a turn.
2. **Attempt:** an attempt is saved as `prepared` before the provider adapter
   is invoked, then marked succeeded, failed, or transport-error after it
   returns. This distinguishes never-dispatched work from an uncertain result.
3. **Tool operation:** the `(logical_operation_id, tool name, SHA-256(args))`
   record is written before a tool call is exposed. `mark_submitted` persists
   `SUBMITTED` and obtains the operation lease before the tool runner calls an
   external system. `mark_committed` persists the receipt and releases the
   lease before reporting success.

If a process starts and finds a persisted `SUBMITTED` operation, it changes it
to `INDETERMINATE`. Any identical future tool call is blocked with
`indeterminate_tool_operation`; it is never auto-replayed. A committed receipt
is the sole ordinary replay path. Resolution of an indeterminate payment,
write, deployment, or command is an application-specific reconciliation task
(for example, querying the payment/deployment system by its idempotency key).

## Cooperating tool runner

For retryable tool turns, send the same `X-LCB-Operation-Id` on each request
that represents the same logical action. The normal OpenAI/Anthropic response
body gains a non-provider extension named `lcb_tool_operations`; it maps the
provider tool-call ID to the gateway `ledger_call_id`. Clients that ignore
unknown response fields retain ordinary provider compatibility, but cannot use
the durable tool lifecycle.

An HTTP tool runner calls these local-proxy endpoints before/after the external
side effect:

```text
POST /v1/tool-operations/submit        {"ledger_call_id": "..."}
POST /v1/tool-operations/commit        {"ledger_call_id": "...", "receipt": {...}}
POST /v1/tool-operations/indeterminate {"ledger_call_id": "...", "reason": "..."}
```

`submit` is the durable write-ahead boundary. Only call the real tool after it
returns 200. `commit` durably records the receipt before returning success. If
the runner loses the result, call `indeterminate` when possible; a later proxy
restart also converts any remaining `SUBMITTED` operation to that state.

An in-process agent or custom tool runner can use the same lifecycle directly:

```python
from durallm.storage import SQLitePersistenceStore, SQLiteToolExecutionLedger

store = SQLitePersistenceStore("gateway.db")
ledger = SQLiteToolExecutionLedger(store)

# The gateway supplies ``ledger_call_id`` with a validated model tool call.
ledger.mark_submitted(ledger_call_id)  # durable before the side effect
receipt = run_external_tool()
ledger.mark_committed(ledger_call_id, receipt)  # durable before returning it
```

On an exception after submission, call `ledger.mark_indeterminate` and
reconcile manually; do not issue the tool call again. Leases protect live local
workers but expire, so long-running operations must use an adequate lease TTL
or renew through a backend-specific integration.

The current contract tests cover restart recovery, lease fencing, durable
checkpoint continuation, receipt replay, write-ahead attempts and the blocked
indeterminate path in `tests/test_durable_state.py` and
`tests/faults/test_tool_replay.py`.
