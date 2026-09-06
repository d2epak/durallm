"""Reproducible benchmark harness: every system under test is scored by the same rule.

A scenario is run once per system against a fresh set of programmable mock providers.
The system only decides which provider to call and what request to send; the harness
then applies one predicate to what the agent would have received:

* success       - a response was delivered for every turn AND every delivered tool call
                  passes the real ToolCallValidator against the request's tool schemas
                  AND the scenario's observable `verify` hook (if any) raises no objection
* attempts      - total provider calls made by the system (from the mock call log),
                  identical accounting for the gateway and for the baselines
* fallback depth- distinct providers used in the worst turn, minus one
* recovery      - success with more provider calls than turns
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Protocol

from benchmarks.scenarios import BenchmarkScenario, ScenarioRun, ScenarioTurn, get_all_scenarios
from benchmarks.tool_runner import ToolRunner
from llm_circuit_breaker.agent.idempotency import ToolExecutionLedger
from llm_circuit_breaker.agent.tool_validation import ToolCallValidator
from llm_circuit_breaker.breaker.circuit_breaker import CircuitBreakerConfig
from llm_circuit_breaker.breaker.registry import CircuitBreakerRegistry
from llm_circuit_breaker.capability.profile import Endpoint, ModelProfile
from llm_circuit_breaker.capability.registry import CapabilityRegistry
from llm_circuit_breaker.execution.executor import GatewayExecutor
from llm_circuit_breaker.execution.policy import ExecutionPolicy, FallbackPolicy, RetryPolicy
from llm_circuit_breaker.health.telemetry import HealthTelemetryStore
from llm_circuit_breaker.protocol.ir import NormalizedRequest, NormalizedResponse, NormalizedToolCall
from llm_circuit_breaker.providers.adapters import ProviderAdapterRegistry
from tests.faults.mock_provider import ProgrammableMockAdapter

V3_NAME = "LLM-Circuit-Breaker-V3"
PROVIDER_ORDER = ("provider_a", "provider_b", "provider_c")

# (endpoint id, provider, model, context window, priority): one topology shared by every system.
ENDPOINT_TABLE = (
    ("ep-a", "provider_a", "llama3.3-70b", 65536, 1),
    ("ep-b", "provider_b", "llama-3.3-70b", 32768, 2),
    ("ep-c", "provider_c", "mistral-large", 32768, 3),
)


@dataclass
class ScenarioResult:
    """Outcome metrics for a benchmark scenario."""
    scenario_id: str
    system_name: str
    success: bool
    recovery_occurred: bool
    total_latency_ms: float
    attempts_count: int
    fallback_depth: int
    semantic_error: bool
    final_output: Optional[str] = None


@dataclass
class SystemBenchmarkSummary:
    """Aggregated benchmark metrics across all scenarios."""
    system_name: str
    total_scenarios: int = 0
    successful_scenarios: int = 0
    completion_rate_pct: float = 0.0
    recovery_rate_pct: float = 0.0
    median_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    avg_attempts_per_request: float = 0.0
    avg_fallback_depth: float = 0.0
    semantic_error_rate_pct: float = 0.0
    scenario_results: List[ScenarioResult] = field(default_factory=list)


def percentile(values: List[float], q: float) -> float:
    """Linear-interpolated percentile. (The previous index method returned the maximum for n=15.)"""
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


@dataclass
class Fixture:
    """Fresh per-run state: mock adapters, their shared call log and the endpoint topology."""
    adapters: Dict[str, ProgrammableMockAdapter]
    registry: ProviderAdapterRegistry
    call_log: List[str]
    endpoints: Dict[str, Endpoint]


def build_fixture(scenario: BenchmarkScenario) -> Fixture:
    endpoints: Dict[str, Endpoint] = {}
    for ep_id, prov, model, ctx, prio in ENDPOINT_TABLE:
        profile = ModelProfile(prov, model, context_window=ctx, supports_tools=True)
        overrides: Dict[str, Any] = scenario.profile_overrides.get(prov, {})
        endpoints[prov] = Endpoint(
            id=ep_id, provider=prov, model=model, base_url=f"mock://{prov}", priority=prio,
            pool=scenario.endpoint_pools.get(prov, "coding"), profile=replace(profile, **overrides),
        )
    call_log: List[str] = []
    registry = ProviderAdapterRegistry()
    adapters: Dict[str, ProgrammableMockAdapter] = {}
    for prov_id, actions in scenario.provider_sequences.items():
        # The mock rejects oversize input exactly like the provider it stands in for.
        window = endpoints[prov_id].profile.context_window if prov_id in endpoints else None
        adapter = ProgrammableMockAdapter(prov_id, call_log=call_log, context_window=window)
        adapter.set_sequence(list(actions))
        registry.register(prov_id, adapter)
        adapters[prov_id] = adapter
    return Fixture(adapters=adapters, registry=registry, call_log=call_log, endpoints=endpoints)


class UpstreamError(Exception):
    """A provider answered with a non-200 status (baselines have no classifier)."""


def call_provider(fixture: Fixture, provider: str, request: NormalizedRequest) -> NormalizedResponse:
    adapter = fixture.registry.get_adapter(provider)
    endpoint = fixture.endpoints[provider]
    result = adapter.execute(adapter.prepare_request(endpoint, request), timeout_seconds=5.0)
    if result.status_code != 200:
        raise UpstreamError(f"HTTP {result.status_code}")
    return adapter.normalize_response(endpoint, result)


class SystemRunner(Protocol):
    name: str

    def run(self, turn: ScenarioTurn) -> NormalizedResponse:
        """Serve one turn; raise when the system gives up."""

    def commit_receipt(self, tool_call: NormalizedToolCall, receipt: Dict[str, Any]) -> None:
        """Called after the agent executed a delivered tool call (systems with a ledger record it)."""


class V3Runner:
    name = V3_NAME

    def __init__(self, fixture: Fixture, strategy: str = "priority"):
        self.strategy = strategy
        cap_reg = CapabilityRegistry()
        for endpoint in fixture.endpoints.values():
            cap_reg.register_endpoint(endpoint)
        self.breakers = CircuitBreakerRegistry(
            default_config=CircuitBreakerConfig(
                sliding_window_size=5, minimum_number_of_calls=2, wait_duration_open_ms=100.0, half_open_max_calls=2,
            )
        )
        # Fresh telemetry and ledger per run: the module defaults are process-wide singletons.
        self.executor = GatewayExecutor(
            capability_registry=cap_reg,
            breaker_registry=self.breakers,
            adapter_registry=fixture.registry,
            health_store=HealthTelemetryStore(),
            tool_ledger=ToolExecutionLedger(),
            policy=ExecutionPolicy(
                retry=RetryPolicy(max_attempts_same_endpoint=2, base_backoff_ms=10.0),
                fallback=FallbackPolicy(max_fallback_hops=3),
            ),
        )

    def run(self, turn: ScenarioTurn) -> NormalizedResponse:
        response, _, _ = self.executor.execute(
            turn.request, pool=turn.pool, strategy=self.strategy, requirements=turn.requirements,
        )
        return response

    def commit_receipt(self, tool_call: NormalizedToolCall, receipt: Dict[str, Any]) -> None:
        self.executor.tool_ledger.mark_committed(tool_call.metadata["ledger_call_id"], receipt)


class DirectRunner:
    """Baseline A: one call to the primary provider, no retry, no fallback."""
    name = "Baseline-A-Direct"

    def __init__(self, fixture: Fixture, strategy: str = "priority"):
        self.fixture = fixture

    def run(self, turn: ScenarioTurn) -> NormalizedResponse:
        return call_provider(self.fixture, "provider_a", turn.request)

    def commit_receipt(self, tool_call: NormalizedToolCall, receipt: Dict[str, Any]) -> None:
        pass  # no ledger


class SameProviderRetryRunner:
    """Baseline B: up to three attempts on the primary provider, no fallback."""
    name = "Baseline-B-Same-Provider-Retry"

    def __init__(self, fixture: Fixture, strategy: str = "priority"):
        self.fixture = fixture

    def run(self, turn: ScenarioTurn) -> NormalizedResponse:
        last: Optional[Exception] = None
        for _ in range(3):
            try:
                return call_provider(self.fixture, "provider_a", turn.request)
            except UpstreamError as exc:
                last = exc
        raise last  # type: ignore[misc]

    def commit_receipt(self, tool_call: NormalizedToolCall, receipt: Dict[str, Any]) -> None:
        pass  # no ledger


class StaticFallbackRunner:
    """Baseline C: static a -> b -> c order, no breaker, no validation, no compaction."""
    name = "Baseline-C-Static-Fallback"

    def __init__(self, fixture: Fixture, strategy: str = "priority"):
        self.fixture = fixture

    def run(self, turn: ScenarioTurn) -> NormalizedResponse:
        last: Optional[Exception] = None
        for provider in PROVIDER_ORDER:
            if provider not in self.fixture.adapters:
                continue
            try:
                return call_provider(self.fixture, provider, turn.request)
            except UpstreamError as exc:
                last = exc
        raise last or UpstreamError("no providers configured")

    def commit_receipt(self, tool_call: NormalizedToolCall, receipt: Dict[str, Any]) -> None:
        pass  # no ledger


SYSTEMS: Dict[str, Callable[[Fixture, str], SystemRunner]] = {
    V3Runner.name: V3Runner,
    DirectRunner.name: DirectRunner,
    SameProviderRetryRunner.name: SameProviderRetryRunner,
    StaticFallbackRunner.name: StaticFallbackRunner,
}


def tool_calls_are_valid(request: NormalizedRequest, response: NormalizedResponse) -> bool:
    """The same validator the gateway uses, applied to whatever a system delivered to the agent."""
    validator = ToolCallValidator()
    schemas = {t.name: t.parameters for t in request.tools}
    for tc in response.tool_calls:
        arguments = tc.arguments if (tc.arguments or not tc.raw_arguments) else tc.raw_arguments
        report = validator.validate_tool_call(tc.name, arguments, schema=schemas.get(tc.name), known_tools=list(schemas))
        if not report.is_executable:
            return False
    return True


class BenchmarkHarness:
    """Runs every scenario through every registered system and scores them identically."""

    def __init__(self, scenarios: Optional[List[BenchmarkScenario]] = None):
        self.scenarios = scenarios if scenarios is not None else get_all_scenarios()

    def run_scenario(self, system_name: str, scenario: BenchmarkScenario) -> ScenarioResult:
        fixture = build_fixture(scenario)
        runner = SYSTEMS[system_name](fixture, scenario.strategy)
        tool_runner = ToolRunner()
        turns = scenario.turns

        responses: List[NormalizedResponse] = []
        turn_calls: List[List[str]] = []
        error: Optional[str] = None
        semantic_error = False
        elapsed_ms = 0.0
        worst_depth = 0
        for turn in turns:
            if turn.sleep_ms > 0:
                time.sleep(turn.sleep_ms / 1000.0)
            calls_before = len(fixture.call_log)
            start = time.perf_counter()
            try:
                response = runner.run(turn)
            except Exception as exc:  # the system gave up on this turn
                response = None
                error = f"{type(exc).__name__}: {exc}"
            elapsed_ms += (time.perf_counter() - start) * 1000.0
            calls = fixture.call_log[calls_before:]
            turn_calls.append(calls)
            worst_depth = max(worst_depth, max(len(set(calls)) - 1, 0))
            if response is None:
                break
            responses.append(response)
            if not tool_calls_are_valid(turn.request, response):
                semantic_error = True  # the agent would have been handed an unexecutable call
                continue
            for tc in response.tool_calls:
                receipt, executed_now = tool_runner.handle(turn.request.request_id, tc)
                if executed_now:
                    runner.commit_receipt(tc, receipt)

        attempts = len(fixture.call_log)
        verify_failure: Optional[str] = None
        if error is None and not semantic_error and scenario.verify is not None:
            verify_failure = scenario.verify(ScenarioRun(responses, turn_calls, fixture.adapters, tool_runner))
        success = error is None and len(responses) == len(turns) and not semantic_error and verify_failure is None
        if error:
            final_output = error
        elif semantic_error:
            final_output = "invalid tool call delivered"
        elif verify_failure:
            final_output = f"verify: {verify_failure}"
        else:
            final_output = responses[-1].content or "tool_call"
        return ScenarioResult(
            scenario_id=scenario.id,
            system_name=system_name,
            success=success,
            recovery_occurred=success and attempts > len(turns),
            total_latency_ms=elapsed_ms,
            attempts_count=attempts,
            fallback_depth=worst_depth,
            semantic_error=semantic_error,
            final_output=final_output,
        )

    def summarize(self, system_name: str, results: List[ScenarioResult]) -> SystemBenchmarkSummary:
        total = len(results)
        succ = sum(1 for r in results if r.success)
        recs = sum(1 for r in results if r.recovery_occurred)
        sem_errs = sum(1 for r in results if r.semantic_error)
        lats = [r.total_latency_ms for r in results]
        return SystemBenchmarkSummary(
            system_name=system_name,
            total_scenarios=total,
            successful_scenarios=succ,
            completion_rate_pct=(succ / total * 100.0) if total else 0.0,
            recovery_rate_pct=(recs / total * 100.0) if total else 0.0,
            median_latency_ms=statistics.median(lats) if lats else 0.0,
            p95_latency_ms=percentile(lats, 0.95),
            avg_attempts_per_request=statistics.mean([r.attempts_count for r in results]) if results else 0.0,
            avg_fallback_depth=statistics.mean([r.fallback_depth for r in results]) if results else 0.0,
            semantic_error_rate_pct=(sem_errs / total * 100.0) if total else 0.0,
            scenario_results=results,
        )

    def run_system(self, system_name: str) -> SystemBenchmarkSummary:
        return self.summarize(system_name, [self.run_scenario(system_name, s) for s in self.scenarios])

    def run_all(self) -> Dict[str, SystemBenchmarkSummary]:
        return {name: self.run_system(name) for name in SYSTEMS}
