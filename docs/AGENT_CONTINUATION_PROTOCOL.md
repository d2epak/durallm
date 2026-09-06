# Agent Continuation Protocol (ACP) v1

ACP is an opt-in, provider-neutral protocol for a **cooperating client** and
LLM Circuit Breaker to establish an ordered, auditable sequence of model turns.
It exists to make a provider switch observable and safe to resume; it does not
pretend that an OpenAI- or Anthropic-compatible request alone can reproduce an
agent's private filesystem, shell process, hidden state, or tool effects.

The current identifier is `lcb-acp/1`. Plain `/v1/messages` and
`/v1/chat/completions` traffic remains compatible and is explicitly
best-effort. A client receives ACP guarantees only when it opts in on every
governed turn.

## Wire contract

For the first turn, send these headers with either native request body:

```http
X-LCB-ACP-Version: lcb-acp/1
X-LCB-State-Digest: <lowercase SHA-256 of the client's continuation state>
```

The gateway returns the normal provider-native response body unchanged plus:

```http
X-LCB-ACP-Version: lcb-acp/1
X-LCB-Continuation-Event: turn_completed
X-LCB-Session-Id: acp_sess_...
X-LCB-Turn-Id: acp_turn_...
X-LCB-Epoch: 0
X-LCB-Ack-Required: true
X-LCB-Checkpoint-Id: acp_ckpt_...
X-LCB-Checkpoint-Digest: <lowercase SHA-256>
```

The checkpoint digest commits to the protocol version, session/turn/epoch,
previous checkpoint, client state digest, canonical request/response digests,
and selected gateway endpoint. It is an integrity and ordering record, not a
copy of confidential prompt or response contents.

Before the next model request, acknowledge exactly that checkpoint:

```http
POST /v1/continuations/ack
Content-Type: application/json

{
  "session_id": "acp_sess_...",
  "turn_id": "acp_turn_...",
  "epoch": 0,
  "checkpoint_digest": "<digest from the response>"
}
```

The successful response contains a `checkpoint_acknowledged` continuation
event and `X-LCB-Ack-Required: false`.

Each later turn sends the previous acknowledged checkpoint and a fresh client
state digest:

```http
X-LCB-ACP-Version: lcb-acp/1
X-LCB-Session-Id: acp_sess_...
X-LCB-Parent-Checkpoint-Digest: <previous checkpoint digest>
X-LCB-State-Digest: <current client-state digest>
```

The gateway increments `X-LCB-Epoch` only after a completed checkpoint. It
rejects unsupported versions, malformed digests, a stale/missing parent, a
second active turn, and an unacknowledged prior checkpoint with a
`continuation_protocol_error` (normally `400` or `409`). It performs those
checks before dispatching the provider request.

## Guarantee boundary

ACP v1's default `InMemoryContinuationStore` is thread-safe **within one
gateway process only**. It prevents an ordered client from accidentally
advancing past a result it has not persisted, and it records the actual
endpoint selected after failover. A failed upstream attempt releases its active
turn without issuing a checkpoint; a client can retry that same epoch.

Set `LLM_BREAKER_STATE_DB` to use the SQLite/WAL `SQLiteContinuationStore`.
It persists the session/checkpoint and uses a fenced session lease, so an
uncompleted active turn after restart is interrupted rather than advanced. It
still does not reconstruct a provider request that may have been in flight,
execute tools, or make an arbitrary external action exactly once. Durable tool
operations require a cooperating `SQLiteToolExecutionLedger`; see
`DURABLE_STATE.md` for the explicit submit/commit protocol and its
indeterminate-state boundary.

## Client responsibilities

1. Keep the state whose digest is sent. The gateway cannot derive or restore it.
2. Persist the response and checkpoint before acknowledging it.
3. Send the exact latest acknowledged parent digest on the next turn.
4. Treat native streaming after visible output as best-effort unless a
   protocol-specific interruption event is returned; ACP v1 never splices a
   different provider into a visible response.
5. Use application-level idempotency for side-effecting tool calls until the
   durable operation store is enabled.

The unit and HTTP contract tests in `tests/test_continuation_protocol.py` and
`tests/test_proxy_gateway.py` are the executable specification for this
version.
