"""Benchmark scenarios B1 through B15.

Every scenario is a list of turns sent to the system under test, scripted mock provider
behaviour, and an optional `verify` hook. The hook sees only what an outside observer
could see for any system (which providers were called per turn, the responses, the
requests each provider received, the tool runner's counters), so the same hook grades
the gateway and every baseline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from benchmarks.tool_runner import ToolRunner
from llm_circuit_breaker.agent.context import estimate_tokens
from llm_circuit_breaker.protocol.ir import (
    NormalizedMessage,
    NormalizedRequest,
    NormalizedResponse,
    NormalizedToolDefinition,
)
from llm_circuit_breaker.routing.requirements import RequirementVector
from tests.faults.mock_provider import MockFaultAction, ProgrammableMockAdapter

SECONDARY_CONTEXT_WINDOW = 32768


@dataclass
class ScenarioTurn:
    request: NormalizedRequest
    pool: str = "coding"
    requirements: Optional[RequirementVector] = None
    sleep_ms: float = 0.0  # waited by the harness before the turn, for every system alike


@dataclass
class ScenarioRun:
    """Observable outcome of one system running one scenario."""
    responses: List[NormalizedResponse]
    turn_calls: List[List[str]]  # providers called during each turn, in call order
    adapters: Dict[str, ProgrammableMockAdapter]
    tool_runner: ToolRunner


Verifier = Callable[[ScenarioRun], Optional[str]]


@dataclass
class BenchmarkScenario:
    """Benchmark scenario definition."""
    id: str
    name: str
    description: str
    turns: List[ScenarioTurn]
    provider_sequences: Dict[str, List[MockFaultAction]]
    expected_outcome: str
    strategy: str = "priority"
    profile_overrides: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    endpoint_pools: Dict[str, str] = field(default_factory=dict)
    verify: Optional[Verifier] = None  # returns a failure reason, or None when the run is acceptable

    @property
    def request(self) -> NormalizedRequest:
        return self.turns[0].request


# ---------------------------------------------------------------------------
# Verification helpers (observable behaviour only)
# ---------------------------------------------------------------------------

def served_by(run: ScenarioRun, turn: int, provider: str) -> Optional[str]:
    """The provider that answered a turn is the last one called in it."""
    calls = run.turn_calls[turn] if turn < len(run.turn_calls) else []
    if not calls or calls[-1] != provider:
        return f"turn {turn + 1} answered by {calls[-1] if calls else 'nobody'}, expected {provider}"
    return None


def not_called(run: ScenarioRun, turn: int, provider: str) -> Optional[str]:
    calls = run.turn_calls[turn] if turn < len(run.turn_calls) else []
    if provider in calls:
        return f"{provider} was called in turn {turn + 1}"
    return None


def only_called(run: ScenarioRun, turn: int, provider: str) -> Optional[str]:
    calls = run.turn_calls[turn] if turn < len(run.turn_calls) else []
    if calls != [provider]:
        return f"turn {turn + 1} calls were {calls}, expected [{provider!r}]"
    return None


def first_failure(*checks: Optional[str]) -> Optional[str]:
    return next((c for c in checks if c), None)


def delivered_request(run: ScenarioRun, provider: str) -> Optional[NormalizedRequest]:
    history = run.adapters[provider].request_history
    return history[-1] if history else None


def padded_history(root: str, final: str, pads: int = 5, words_per_pad: int = 2400) -> List[NormalizedMessage]:
    """Root objective, `pads` long intermediate turns (~7.2k tokens each), then the latest question."""
    messages = [NormalizedMessage(role="user", content=root)]
    for i in range(pads):
        messages.append(NormalizedMessage(role="assistant", content=f"Acknowledged step {i}."))
        messages.append(NormalizedMessage(role="user", content=f"LOG_CHUNK_{i} " * words_per_pad))
    messages.append(NormalizedMessage(role="assistant", content="Ready for the next instruction."))
    messages.append(NormalizedMessage(role="user", content=final))
    return messages


def compaction_verifier(root_marker: str, final: str) -> Verifier:
    def verify(run: ScenarioRun) -> Optional[str]:
        req = delivered_request(run, "provider_b")
        if req is None:
            return "provider_b never received a request"
        tokens = estimate_tokens(req)
        if tokens > SECONDARY_CONTEXT_WINDOW:
            return f"provider_b received {tokens} tokens, over its {SECONDARY_CONTEXT_WINDOW} window"
        if root_marker not in (req.messages[0].content or ""):
            return "root objective was dropped during compaction"
        if (req.messages[-1].content or "") != final:
            return "latest user turn was dropped during compaction"
        return None
    return verify


def user_turn(content: str, tools: Optional[List[NormalizedToolDefinition]] = None, **kwargs: Any) -> NormalizedRequest:
    return NormalizedRequest(
        model="default", messages=[NormalizedMessage(role="user", content=content)], tools=list(tools or []), **kwargs,
    )


BASH_TOOL = NormalizedToolDefinition(
    name="bash",
    description="Execute bash command",
    parameters={
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
)


def get_all_scenarios() -> List[BenchmarkScenario]:
    """Return the benchmark scenarios B1 through B15."""
    tool_def = BASH_TOOL
    b4_final = "Status update?"
    b5_final = "Continue execution."

    return [
        # B1 — Permanent Outage
        BenchmarkScenario(
            id="B1",
            name="Permanent Provider Outage",
            description="Primary provider permanently fails with 503; secondary provider is healthy.",
            turns=[ScenarioTurn(user_turn("Execute build"))],
            provider_sequences={
                "provider_a": [MockFaultAction.server_error(503, "Outage")] * 10,
                "provider_b": [MockFaultAction.success("Build successful via Provider B")],
            },
            expected_outcome="fallback_success",
        ),
        # B2 — Intermittent 429
        BenchmarkScenario(
            id="B2",
            name="Intermittent 429 Rate Limit",
            description="Primary provider answers 429 (Retry-After: 1s) then 200; secondary is healthy.",
            turns=[ScenarioTurn(user_turn("Check health"))],
            provider_sequences={
                "provider_a": [MockFaultAction.rate_limit(retry_after=1), MockFaultAction.success("Primary recovered")],
                "provider_b": [MockFaultAction.success("Secondary fallback")],
            },
            expected_outcome="retry_or_fallback_success",
        ),
        # B3 — Slow Provider / Timeout
        BenchmarkScenario(
            id="B3",
            name="Slow Provider Timeout Stall",
            description="Primary provider stalls past the request timeout; secondary answers promptly.",
            turns=[ScenarioTurn(user_turn("Run analysis"))],
            provider_sequences={
                "provider_a": [MockFaultAction.timeout(60_000.0)],
                "provider_b": [MockFaultAction.success("Quick response from secondary")],
            },
            expected_outcome="deadline_fallback_success",
        ),
        # B4 — Context Mismatch Recovery
        BenchmarkScenario(
            id="B4",
            name="Context Window Mismatch Recovery",
            description=(
                "A ~36k-token conversation fails over from the 128k primary to a 32k secondary that rejects "
                "oversize input; the root objective and latest turn must survive compaction."
            ),
            turns=[ScenarioTurn(NormalizedRequest(
                model="default",
                system_instruction="Preserve mission objectives.",
                messages=padded_history("ROOT_GOAL: Deploy cluster securely", b4_final),
            ))],
            provider_sequences={
                "provider_a": [MockFaultAction.server_error(503, "Context Outage")] * 10,
                "provider_b": [MockFaultAction.success("Compacted context processed successfully by Secondary")],
            },
            profile_overrides={"provider_a": {"context_window": 131072}},
            expected_outcome="compaction_fallback_success",
            verify=compaction_verifier("ROOT_GOAL", b4_final),
        ),
        # B5 — Root-Prompt Fact Preservation
        BenchmarkScenario(
            id="B5",
            name="Root-Prompt Fact Preservation",
            description=(
                "A critical fact in the protected root prompt survives compaction onto a 32k secondary "
                "(facts inside evicted intermediate turns are not preserved by design)."
            ),
            turns=[ScenarioTurn(NormalizedRequest(
                model="default",
                system_instruction="Keep critical secrets.",
                messages=padded_history("CRITICAL_SECRET: auth_token_xyz999", b5_final),
            ))],
            provider_sequences={
                "provider_a": [MockFaultAction.server_error(503, "Unavailable")] * 10,
                "provider_b": [MockFaultAction.success("Retrieved and processed with secret intact")],
            },
            profile_overrides={"provider_a": {"context_window": 131072}},
            expected_outcome="critical_fact_preserved",
            verify=compaction_verifier("auth_token_xyz999", b5_final),
        ),
        # B6 — Malformed Tool Call (Invalid JSON Syntax)
        BenchmarkScenario(
            id="B6",
            name="Malformed Tool Call Syntax",
            description="Primary emits corrupt JSON; validator fails closed and recovers on Secondary.",
            turns=[ScenarioTurn(user_turn("List directory contents", [tool_def]))],
            provider_sequences={
                "provider_a": [MockFaultAction.malformed_tool_json("{command: missing_quotes")],
                "provider_b": [MockFaultAction.valid_tool_call("bash", {"command": "ls -la"})],
            },
            expected_outcome="syntactic_repair_or_failover",
        ),
        # B7 — Semantically Invalid Tool Call (Wrong Schema)
        BenchmarkScenario(
            id="B7",
            name="Semantically Invalid Tool Call Schema",
            description="Primary emits valid JSON but violates schema; validator triggers safe failover.",
            turns=[ScenarioTurn(user_turn("Execute maintenance", [tool_def]))],
            provider_sequences={
                "provider_a": [MockFaultAction.valid_tool_call("bash", {"unknown_arg": 123})],
                "provider_b": [MockFaultAction.valid_tool_call("bash", {"command": "echo ok"})],
            },
            expected_outcome="schema_failover_success",
        ),
        # B8 — Tool Execution Ambiguity & Idempotency
        BenchmarkScenario(
            id="B8",
            name="Tool Execution Ambiguity & Idempotency",
            description=(
                "The tool ran but its response was lost; the client re-sends the same logical operation. "
                "The tool must execute exactly once across both turns."
            ),
            turns=[
                ScenarioTurn(user_turn("Run safe tool", [tool_def], request_id="B8-op-1")),
                ScenarioTurn(user_turn("Run safe tool", [tool_def], request_id="B8-op-1")),
            ],
            provider_sequences={
                "provider_a": [MockFaultAction.valid_tool_call("bash", {"command": "echo unique_idempotent_test"})] * 2,
                "provider_b": [MockFaultAction.valid_tool_call("bash", {"command": "echo unique_idempotent_test"})] * 2,
            },
            expected_outcome="idempotent_deduplication",
            verify=lambda run: (
                None if run.tool_runner.executions == 1 and run.tool_runner.duplicate_executions == 0
                else f"tool executed {run.tool_runner.executions} times for one logical operation"
            ),
        ),
        # B9 — Mid-Stream Disconnect Recovery
        BenchmarkScenario(
            id="B9",
            name="Mid-Stream Disconnect Recovery",
            description="Primary keeps dropping the connection mid-stream (502); the secondary delivers a complete response.",
            turns=[ScenarioTurn(user_turn("Generate report"))],
            provider_sequences={
                "provider_a": [MockFaultAction.mid_stream_reset("Partial report header...")] * 3,
                "provider_b": [MockFaultAction.success("Complete atomic report successfully recovered")],
            },
            expected_outcome="mid_stream_recovery_success",
        ),
        # B10 — Provider Recovery & Half-Open Probe
        BenchmarkScenario(
            id="B10",
            name="Provider Recovery and Breaker Probe",
            description=(
                "Primary fails twice then recovers. Turn 2 must not touch the failed primary; after the "
                "open-wait elapses, turn 3 must be answered by the primary again via a probe."
            ),
            turns=[
                ScenarioTurn(user_turn("Probe health 1")),
                ScenarioTurn(user_turn("Probe health 2")),
                ScenarioTurn(user_turn("Probe health 3"), sleep_ms=150.0),
            ],
            provider_sequences={
                "provider_a": [
                    MockFaultAction.server_error(503, "Down"),
                    MockFaultAction.server_error(503, "Down"),
                    MockFaultAction.success("Provider recovered and healthy"),
                ],
                "provider_b": [MockFaultAction.success("Secondary covering")] * 3,
            },
            expected_outcome="probe_recovery_success",
            verify=lambda run: first_failure(
                not_called(run, 1, "provider_a"),
                only_called(run, 2, "provider_a"),
            ),
        ),
        # B11 — Multi-Provider Cascade Failure
        BenchmarkScenario(
            id="B11",
            name="Multi-Provider Cascade Failure",
            description="Provider A fails with 500, Provider B fails with 429, Provider C succeeds without loop.",
            turns=[ScenarioTurn(user_turn("Cascade test"))],
            provider_sequences={
                "provider_a": [MockFaultAction.server_error(500, "Dead")],
                "provider_b": [MockFaultAction.rate_limit(retry_after=30)],
                "provider_c": [MockFaultAction.success("Cascade resolved at Provider C")],
            },
            expected_outcome="cascade_resolution_success",
        ),
        # B12 — Cross-Agent Pool Contention
        BenchmarkScenario(
            id="B12",
            name="Cross-Agent Pool Contention",
            description=(
                "The coding pool's primary is down; a general_agent turn must be answered by that pool's own "
                "provider without touching the coding pool's providers."
            ),
            turns=[
                ScenarioTurn(user_turn("Coding agent turn"), pool="coding"),
                ScenarioTurn(user_turn("General agent turn"), pool="general_agent"),
            ],
            provider_sequences={
                "provider_a": [MockFaultAction.server_error(503, "Down")] * 10,
                "provider_b": [MockFaultAction.success("Coding turn via B")] * 3,
                "provider_c": [MockFaultAction.success("General agent turn via C")] * 3,
            },
            endpoint_pools={"provider_c": "general_agent"},
            expected_outcome="cross_pool_isolation",
            verify=lambda run: only_called(run, 1, "provider_c"),
        ),
        # B13 — Cost Constraint & Budget Enforcement
        BenchmarkScenario(
            id="B13",
            name="Cost Constraint & Route Selection",
            description="The primary is priced above the request's cost ceiling; the cheap secondary must be used instead.",
            turns=[ScenarioTurn(
                user_turn("Cost sensitive request", max_output_tokens=1000),
                requirements=RequirementVector(maximum_cost_usd=0.001),
            )],
            provider_sequences={
                "provider_a": [MockFaultAction.success("Expensive candidate output")],
                "provider_b": [MockFaultAction.success("Cost-efficient candidate output")],
            },
            profile_overrides={
                "provider_a": {"input_price_per_1m": 30.0, "output_price_per_1m": 60.0},
                "provider_b": {"input_price_per_1m": 0.1, "output_price_per_1m": 0.2},
            },
            expected_outcome="cost_effective_selection",
            verify=lambda run: only_called(run, 0, "provider_b"),
        ),
        # B14 — Tool Reliability Differentiation
        BenchmarkScenario(
            id="B14",
            name="Tool Reliability Differentiation",
            description=(
                "The primary keeps emitting schema-invalid tool calls. With reliability-aware routing the "
                "second turn must skip the primary based on its observed tool failure."
            ),
            turns=[
                ScenarioTurn(user_turn("Need reliable tool execution", [tool_def])),
                ScenarioTurn(user_turn("Need reliable tool execution again", [tool_def])),
            ],
            provider_sequences={
                "provider_a": [MockFaultAction.valid_tool_call("bash", {"unknown_arg": "pwd"})] * 2,
                "provider_b": [MockFaultAction.valid_tool_call("bash", {"command": "pwd"})] * 2,
            },
            strategy="reliability_aware",
            expected_outcome="tool_reliability_selection",
            verify=lambda run: not_called(run, 1, "provider_a"),
        ),
        # B15 — Capability Mismatch Non-Poisoning Failover
        BenchmarkScenario(
            id="B15",
            name="Capability Mismatch Non-Poisoning Failover",
            description=(
                "A vision request must go straight to the vision-capable secondary; the next text-only "
                "turn must still use the primary, proving the mismatch did not trip its breaker."
            ),
            turns=[
                ScenarioTurn(user_turn("Vision request with image data"), requirements=RequirementVector(require_vision=True)),
                ScenarioTurn(user_turn("Plain text follow-up")),
            ],
            provider_sequences={
                "provider_a": [MockFaultAction.success("Text only")] * 2,
                "provider_b": [MockFaultAction.success("Vision capable response")] * 2,
            },
            profile_overrides={"provider_b": {"supports_vision": True}},
            expected_outcome="capability_matched_success",
            verify=lambda run: first_failure(
                only_called(run, 0, "provider_b"),
                only_called(run, 1, "provider_a"),
            ),
        ),
    ]
