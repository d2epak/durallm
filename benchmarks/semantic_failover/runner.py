"""Primary research benchmark: compound multi-turn semantic failover.

Turn 1: the agent's request (a ~36k-token history whose root prompt holds a
continuation-critical secret) goes to Provider A, which is down (503). The
gateway fails over to Provider B, whose 32k context forces compaction, and B
answers with a schema-invalid tool call. The validator fails closed and the
gateway issues a second FailoverPlan to Provider C (also 32k), which returns a
valid tool call. The harness executes that tool and commits the receipt.

Turn 2: the tool's response is lost and the client re-sends the same logical
operation. Every hop repeats, but the tool must not run again: the gateway
attaches the committed receipt and the harness's tool runner replays it.

Every metric below is measured on this run. The endpoints declare different
wire protocols (anthropic / openai / gemini) for the routing layer, but the
mock adapters do not exercise wire-format translation. Providers B and C
reject input over their context window with HTTP 400, so a request that
skipped compaction could not complete.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Dict, Tuple

from benchmarks.harness import tool_calls_are_valid
from benchmarks.scenarios import BASH_TOOL, SECONDARY_CONTEXT_WINDOW, padded_history
from benchmarks.tool_runner import ToolRunner
from durallm.agent.context import estimate_tokens
from durallm.agent.idempotency import ToolExecutionLedger
from durallm.breaker.circuit_breaker import CircuitBreakerConfig
from durallm.breaker.registry import CircuitBreakerRegistry
from durallm.capability.profile import Endpoint, ModelProfile
from durallm.capability.registry import CapabilityRegistry
from durallm.execution.executor import GatewayExecutor
from durallm.execution.policy import ExecutionPolicy, FallbackPolicy, RetryPolicy
from durallm.health.telemetry import HealthTelemetryStore
from durallm.protocol.ir import NormalizedRequest
from durallm.providers.adapters import ProviderAdapterRegistry
from tests.faults.mock_provider import MockFaultAction, ProgrammableMockAdapter

PLANTED_SECRET = "CRITICAL_AUTH_SECRET: vault_key_sec_999123847"
FINAL_INSTRUCTION = "Execute recovery bash script now."
OPERATION_ID = "research_bench_op_1"
EXPECTED_ARGUMENTS = {"command": "echo task_complete"}

# (endpoint id, provider, model, declared protocol, context window, priority)
ENDPOINTS = (
    ("ep-a-anthropic", "provider_a_anthropic", "claude-3-5-sonnet", "anthropic", 131072, 1),
    ("ep-b-openai", "provider_b_openai", "gpt-4o-mini", "openai", SECONDARY_CONTEXT_WINDOW, 2),
    ("ep-c-gemini", "provider_c_gemini", "gemini-2.5-flash", "gemini", SECONDARY_CONTEXT_WINDOW, 3),
)


@dataclass
class SemanticFailoverMetrics:
    """Quantitative evaluation metrics for compound semantic failover."""
    task_completed: bool
    critical_state_preserved: bool
    tool_correctness: bool
    tool_executions: int
    duplicate_tool_execution: int
    receipt_replayed: bool
    semantic_error_rate_pct: float
    fallback_count: int
    total_attempts: int
    recovery_latency_ms: float
    context_tokens_initial: int
    context_tokens_final: int
    context_reduction_pct: float
    failover_plans_generated: int
    receipt_cached: bool


def build_gateway() -> Tuple[GatewayExecutor, Dict[str, ProgrammableMockAdapter]]:
    """Three-provider gateway with fresh telemetry and ledger; mocks enforce their context windows."""
    cap_reg = CapabilityRegistry()
    breaker_reg = CircuitBreakerRegistry(
        default_config=CircuitBreakerConfig(sliding_window_size=5, minimum_number_of_calls=2)
    )
    adapter_reg = ProviderAdapterRegistry()
    mocks: Dict[str, ProgrammableMockAdapter] = {}
    for ep_id, provider, model, protocol, window, priority in ENDPOINTS:
        mocks[provider] = ProgrammableMockAdapter(provider, context_window=window)
        adapter_reg.register(provider, mocks[provider])
        cap_reg.register_endpoint(Endpoint(
            id=ep_id, provider=provider, model=model, base_url=f"mock://{provider}", protocol=protocol,
            priority=priority, pool="coding",
            profile=ModelProfile(provider, model, protocol=protocol, context_window=window, supports_tools=True),
        ))
    executor = GatewayExecutor(
        capability_registry=cap_reg,
        breaker_registry=breaker_reg,
        adapter_registry=adapter_reg,
        health_store=HealthTelemetryStore(),
        tool_ledger=ToolExecutionLedger(),
        policy=ExecutionPolicy(
            retry=RetryPolicy(max_attempts_same_endpoint=1),
            fallback=FallbackPolicy(max_fallback_hops=3),
        ),
    )
    return executor, mocks


def run_semantic_failover_benchmark() -> SemanticFailoverMetrics:
    executor, mocks = build_gateway()
    mocks["provider_a_anthropic"].set_sequence([MockFaultAction.server_error(503, "Anthropic primary outage")] * 5)
    mocks["provider_b_openai"].set_sequence([MockFaultAction.valid_tool_call("bash", {"wrong_arg": "echo fail"})] * 2)
    mocks["provider_c_gemini"].set_sequence([MockFaultAction.valid_tool_call("bash", EXPECTED_ARGUMENTS)] * 2)

    request = NormalizedRequest(
        request_id=OPERATION_ID,
        model="default",
        system_instruction="You are an autonomous resilience agent.",
        messages=padded_history(f"MISSION: Restore database cluster\n{PLANTED_SECRET}", FINAL_INSTRUCTION),
        tools=[BASH_TOOL],
    )
    initial_tokens = estimate_tokens(request)
    runner = ToolRunner()

    start = time.perf_counter()
    first, _, ledger = executor.execute(request, pool="coding", strategy="priority")
    latency_ms = (time.perf_counter() - start) * 1000.0
    for tc in first.tool_calls:
        receipt, executed = runner.handle(OPERATION_ID, tc)
        if executed:
            executor.tool_ledger.mark_committed(tc.metadata["ledger_call_id"], receipt)

    # The tool's response was lost: the client re-sends the same logical operation.
    second, _, _ = executor.execute(request, pool="coding", strategy="priority")
    for tc in second.tool_calls:
        runner.handle(OPERATION_ID, tc)

    delivered = mocks["provider_c_gemini"].request_history[0]
    final_tokens = estimate_tokens(delivered)
    task_done = bool(
        first.tool_calls and first.tool_calls[0].name == "bash" and first.tool_calls[0].arguments == EXPECTED_ARGUMENTS
    )
    valid = [tool_calls_are_valid(request, resp) for resp in (first, second)]
    state_preserved = (
        PLANTED_SECRET in (delivered.messages[0].content or "")
        and delivered.messages[-1].content == FINAL_INSTRUCTION
    )
    has_receipt, _ = executor.tool_ledger.check_idempotency(OPERATION_ID, "bash", EXPECTED_ARGUMENTS)
    reduction_pct = ((initial_tokens - final_tokens) / initial_tokens * 100.0) if initial_tokens else 0.0

    return SemanticFailoverMetrics(
        task_completed=task_done,
        critical_state_preserved=state_preserved,
        tool_correctness=all(valid),
        tool_executions=runner.executions,
        duplicate_tool_execution=runner.duplicate_executions,
        receipt_replayed=runner.replays == 1,
        semantic_error_rate_pct=valid.count(False) / len(valid) * 100.0,
        fallback_count=ledger.fallback_count,
        total_attempts=ledger.total_attempts,
        recovery_latency_ms=latency_ms,
        context_tokens_initial=initial_tokens,
        context_tokens_final=final_tokens,
        context_reduction_pct=max(0.0, reduction_pct),
        failover_plans_generated=len(ledger.failover_plans),
        receipt_cached=has_receipt,
    )


if __name__ == "__main__":
    metrics = run_semantic_failover_benchmark()
    print("=" * 70)
    print("PRIMARY RESEARCH BENCHMARK: SEMANTIC FAILOVER METRICS")
    print("=" * 70)
    print(json.dumps(asdict(metrics), indent=2))
