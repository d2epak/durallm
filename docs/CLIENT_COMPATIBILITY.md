# Client Compatibility Matrix

This matrix states what is tested by the repository, not a marketing claim
that every client release is interchangeable. The executable source of truth is
[`tests/fixtures/compatibility/client_contracts.v1.json`](../tests/fixtures/compatibility/client_contracts.v1.json), exercised over an actual local HTTP proxy by
[`tests/compatibility/test_client_contract_matrix.py`](../tests/compatibility/test_client_contract_matrix.py).

## Contract versions

| Client integration | Versioned wire contract in CI | Public proxy endpoint | Tested scenarios |
| --- | --- | --- | --- |
| Claude Code | Anthropic Messages API `2023-06-01` | `/v1/messages` | tool-call translation/validation, 429 failover to a Gemini-declared fallback, long-context compaction, atomic SSE, ACP checkpoint acknowledgement, partial native-stream interruption |
| OpenCode | OpenAI Chat Completions v1 compatibility profile | `/v1/chat/completions` | tool-call translation/validation, 429 failover, long-context compaction, atomic SSE, ACP checkpoint acknowledgement, native SSE interruption through the shared OpenAI contract |
| Hermes Agent | OpenAI Chat Completions v1 compatibility profile | `/v1/chat/completions` | tool-call translation/validation, 429 failover, atomic SSE, ACP checkpoint acknowledgement, native SSE interruption through the shared OpenAI contract |
| OpenClaw | OpenAI Chat Completions v1 compatibility profile | `/v1/chat/completions` | tool-call translation/validation, 429 failover, atomic SSE, ACP checkpoint acknowledgement, native SSE interruption through the shared OpenAI contract |

`client_reference` in the fixture is deliberately a protocol/version reference,
not an assertion that an arbitrary current release of a named client was
executed in GitHub Actions. CI has no client binary, credentials, or real
provider account. A release may claim only the recorded wire contracts above
unless it publishes an additional real-client integration artifact.

## What the matrix proves

- Every fixture is posted to the public HTTP server; no test calls
  `GatewayExecutor` directly.
- The primary provider can rate-limit and a Gemini-declared fallback can finish
  the same client turn without changing its Anthropic or OpenAI response
  envelope.
- Claude Code and OpenCode long histories preserve the root objective and final
  instruction while compacting to a 32k fallback context. This test caught and
  now guards the OpenAI system-message/root-objective eviction boundary.
- Tool calls retain a client-visible tool identifier and validated JSON across
  both envelopes.
- Existing `stream: true` requests remain atomic-buffered. Native streaming is
  explicitly opt-in and its real-socket tests cover pre-visible failover and
  post-visible terminal interruption. The Claude Code path receives an
  Anthropic `event: error`; the OpenAI profile receives `event:
  lcb.interrupted` followed by `[DONE]`.
- Every recorded contract can run an ACP turn only after acknowledging the
  preceding checkpoint. Durable restart/operation recovery is additionally
  covered by `tests/test_durable_state.py` and `tests/faults/test_tool_replay.py`.

## Release gate for a real client version

For an actual Claude Code, OpenCode, Hermes Agent, or OpenClaw release, pin the
client build identifier, configuration file, and provider-model versions in a
release artifact; replay the matching fixture through a process-level test;
and attach the unredacted protocol trace (without credentials or prompts) to
the release evidence. Do not replace this with a broad “compatible with latest”
claim: client and provider protocol changes must create a new fixture version
and a new recorded result.

## Intentional boundaries

- The OpenAI compatibility profile does not prove proprietary extensions such
  as client-specific response schemas, hidden prompt fields, or local tool
  sandboxes.
- Native `true_streaming` refuses tool-bearing and ACP turns; use the default
  atomic mode for those workloads until a transactional streamed-tool protocol
  exists.
- The tests use deterministic mock provider adapters for semantic failover and
  a real local HTTP/1.1 server for native transport. They do not measure live
  provider behavior, production rate limits, client UI behavior, or a
  long-running agent process restart.
