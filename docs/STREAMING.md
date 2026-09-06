# Streaming Architecture and Failure Boundary

Streaming has an irreducible boundary: after a client has observed output from
one model, a gateway cannot safely substitute a second model into that same
turn. LLM Circuit Breaker therefore exposes two explicit contracts rather than
claiming seamless mid-stream failover.

## Modes

### `atomic_buffered` (default)

`"stream": true` without an LCB extension runs the ordinary executor to a
completed, validated response and then emits provider-shaped SSE. This is the
compatibility mode for OpenAI-compatible clients, Anthropic clients, cross
protocol routing, tool calls, and ACP turns. No model bytes are visible until
the executor has either completed a response or exhausted its normal retry and
fallback policy.

The buffered body is capped at `MAX_PAYLOAD_BYTES` (10 MB). It has completion
latency rather than token latency; it is not a native provider stream.

### `true_streaming` (opt in)

Send either `X-LCB-Streaming-Mode: true_streaming` or the request extension
`"lcb_stream_mode": "true_streaming"`. The zero-dependency HTTP proxy then:

1. Requires the selected upstream protocol to equal the client protocol
   (OpenAI SSE to OpenAI, Anthropic SSE to Anthropic).
2. Opens a direct HTTP(S) stream with separate TCP-connect, TLS-handshake,
   first-byte, idle-read, and total-deadline budgets.
3. Bounds each read to 16 KiB and the cumulative stream to 10 MB.
4. May retry or select another compatible provider only while no upstream byte
   has been made visible to the client.
5. Once the first byte is prefetched and sent, relays raw provider bytes and
   never replaces that provider in the visible stream.

Tool-bearing turns and ACP continuation turns are intentionally rejected in
`true_streaming` mode. They require whole-turn tool validation, durable
operation state, and checkpoint completion; use `atomic_buffered` until a
transaction-aware streamed tool protocol is introduced.

## Interruption contract

If the native provider connection fails after visibility, the gateway closes
the same stream with an explicit terminal event. It does **not** replay,
continue, or splice output from a fallback model.

- OpenAI-compatible response: `event: lcb.interrupted`, followed by a JSON
  `error` whose `lcb` object contains `event: "interrupted"`,
  `continuation_required: true`, and the selected endpoint ID, then `[DONE]`.
- Anthropic response: a provider-shaped `event: error` containing the same
  `lcb` object.

The client must create a new turn (and, when using ACP, follow the checkpoint
protocol) to continue work. A terminal interruption is not a successful model
completion.

## Cancellation and deadlines

When a downstream client disconnects, the proxy cancels the associated stream
and closes the upstream socket. This is best-effort at the network boundary but
is synchronous within the proxy process; readers check the cancellation token
before every upstream read.

`Deadline.transport_timeouts(streaming=True)` clips connect, TLS, TTFT, and
idle budgets to the remaining caller deadline. Direct streaming currently does
not use an HTTP proxy tunnel and treats bounded request-body writes under the
first-byte socket budget. The regular buffered adapter remains compatible with
the standard library transport; it retains its one-attempt timeout and bounded
body read behavior.

## Evidence

`tests/test_native_streaming.py` runs real local HTTP/1.1 chunked-SSE servers,
not only adapter mocks. It verifies raw stream forwarding, cancellation,
fallback before visibility, and an interruption event without a fallback call
after visible output.
