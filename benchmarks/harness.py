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

import json
import os
import statistics
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Iterator, List, Optional, Protocol
from unittest.mock import patch

from benchmarks.scenarios import BenchmarkScenario, ScenarioRun, ScenarioTurn, get_all_scenarios
from benchmarks.tool_runner import ToolRunner
from llm_circuit_breaker.agent.idempotency import ToolExecutionLedger
from llm_circuit_breaker.agent.tool_validation import ToolCallValidator
from llm_circuit_breaker.breaker.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from llm_circuit_breaker.breaker.registry import CircuitBreakerRegistry
from llm_circuit_breaker.capability.profile import Endpoint, ModelProfile
from llm_circuit_breaker.capability.registry import CapabilityRegistry
from llm_circuit_breaker.errors import CircuitBreakerGatewayError
from llm_circuit_breaker.execution.executor import GatewayExecutor
from llm_circuit_breaker.execution.policy import ExecutionPolicy, FallbackPolicy, RetryPolicy
from llm_circuit_breaker.health.telemetry import HealthTelemetryStore
from llm_circuit_breaker.pools import IsolatedPoolManager, RouteDefinition
from llm_circuit_breaker.protocol.ir import NormalizedRequest, NormalizedResponse, NormalizedToolCall
from llm_circuit_breaker.protocol.openai import (
    ir_to_openai_request,
    ir_to_openai_response,
    openai_request_to_ir,
    openai_response_to_ir,
)
from llm_circuit_breaker.providers.adapters import ProviderAdapterRegistry
from llm_circuit_breaker.router import UniversalFailoverRouter
from tests.faults.mock_provider import ProgrammableMockAdapter

V3_NAME = "LLM-Circuit-Breaker-V3"
PROVIDER_ORDER = ("provider_a", "provider_b", "provider_c")
# Shared by V3 and Baseline D so the breaker itself is not the variable between them.
BREAKER_CONFIG = CircuitBreakerConfig(
    sliding_window_size=5, minimum_number_of_calls=2, wait_duration_open_ms=100.0, half_open_max_calls=2,
)

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
        self.breakers = CircuitBreakerRegistry(default_config=BREAKER_CONFIG)
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


class BreakerStaticFallbackRunner:
    """Baseline D: static a -> b -> c order guarded by one circuit breaker per provider (V3's config);
    no validation, no compaction, no ledger, no telemetry-driven routing."""
    name = "Baseline-D-Breaker-Static-Fallback"

    def __init__(self, fixture: Fixture, strategy: str = "priority"):
        self.fixture = fixture
        self.breakers = {provider: CircuitBreaker(provider, BREAKER_CONFIG) for provider in fixture.adapters}

    def run(self, turn: ScenarioTurn) -> NormalizedResponse:
        last: Optional[Exception] = None
        for provider in PROVIDER_ORDER:
            breaker = self.breakers.get(provider)
            if breaker is None:
                continue
            try:
                breaker.acquire_permission()
            except CircuitBreakerGatewayError as exc:  # OPEN, or half-open probes exhausted
                last = exc
                continue
            started = time.perf_counter()
            try:
                response = call_provider(self.fixture, provider, turn.request)
            except UpstreamError as exc:
                breaker.record_failure((time.perf_counter() - started) * 1000.0, error=exc)
                last = exc
                continue
            breaker.record_success((time.perf_counter() - started) * 1000.0)
            return response
        raise last or UpstreamError("no providers configured")

    def commit_receipt(self, tool_call: NormalizedToolCall, receipt: Dict[str, Any]) -> None:
        pass  # no ledger


class V1PrototypeRunner:
    """Baseline E: the v0.1 `UniversalFailoverRouter` (round-robin pools, cooldown timers, payload pruning)
    driven through its real `dispatch` loop, with its upstream HTTP call redirected to the mock providers."""
    name = "Baseline-E-V1-Prototype"

    def __init__(self, fixture: Fixture, strategy: str = "priority"):
        self.fixture = fixture
        self.router = UniversalFailoverRouter(auto_discover_free=False)
        pools = IsolatedPoolManager()
        pools.keys = {"MOCK_API_KEY": "mock"}
        pools.coding_routes, pools.agent_routes = [], []
        for provider in PROVIDER_ORDER:
            if provider not in fixture.adapters:
                continue
            endpoint = fixture.endpoints[provider]
            route = RouteDefinition(
                id=f"{provider}-v1", provider=provider, model=endpoint.model, pool=endpoint.pool,
                base_url=f"mock://{provider}", api_format="openai", env_key="MOCK_API_KEY",
                context_length=endpoint.profile.context_window,
            )
            (pools.coding_routes if endpoint.pool == "coding" else pools.agent_routes).append(route)
        self.router.pool_manager = pools

    def _upstream(self, route: RouteDefinition, openai_payload: Dict[str, Any], timeout: int = 0):
        """Stands in for `execute_upstream_request`: same mock providers, same accounting as every other system."""
        adapter = self.fixture.registry.get_adapter(route.provider)
        endpoint = self.fixture.endpoints[route.provider]
        prepared = adapter.prepare_request(endpoint, openai_request_to_ir(openai_payload))
        result = adapter.execute(prepared, timeout_seconds=5.0)
        return result.status_code, result.headers, result.body

    def run(self, turn: ScenarioTurn) -> NormalizedResponse:
        payload = ir_to_openai_request(turn.request, "default")
        with patch("llm_circuit_breaker.router.execute_upstream_request", self._upstream):
            status, parsed, _ = self.router.dispatch(turn.pool, payload)
        if status != 200:
            raise UpstreamError(f"HTTP {status}")
        return openai_response_to_ir(parsed)

    def commit_receipt(self, tool_call: NormalizedToolCall, receipt: Dict[str, Any]) -> None:
        pass  # no ledger


class LiteLLMRouterRunner:
    """Baseline F: the actual LiteLLM ``Router`` over local custom mock providers.

    LiteLLM does not understand this project's IR or mock adapter interface.  Its documented
    custom-provider hook lets the benchmark bridge the Router to the same scripted providers
    every other row uses, without a network call or a reimplementation of Router fallback logic.
    This baseline deliberately provides only LiteLLM's request routing: it adds no LCB context
    compaction, tool validation, receipt ledger, or capability policy.
    """

    name = "Baseline-F-LiteLLM-Router"
    _provider_name = "lcb_benchmark_mock"

    def __init__(self, fixture: Fixture, strategy: str = "priority"):
        self.fixture = fixture
        self._litellm, self._router_cls, self._custom_llm_setup = self._load_litellm()
        owner = self

        class MockProvider(self._litellm.CustomLLM):
            def completion(self, model, messages, **kwargs):
                return owner._complete(model, messages, **kwargs)

        self._handler = MockProvider()
        self._groups = {provider: f"lcb-router-{provider}" for provider in fixture.adapters}
        with self._registered_custom_provider():
            self._router = self._router_cls(
                model_list=[
                    {
                        "model_name": self._groups[provider],
                        "litellm_params": {
                            "model": f"{self._provider_name}/{provider}",
                            "api_base": "http://lcb-benchmark.invalid",
                            "api_key": "benchmark-only",
                        },
                    }
                    for provider in fixture.adapters
                ],
                # A one-way primary-to-secondary cascade is a conventional
                # LiteLLM Router configuration. Reciprocal rules would create
                # A -> B -> A cycles and distort the baseline.
                fallbacks=[
                    {
                        self._groups[PROVIDER_ORDER[0]]: [
                            self._groups[alternative]
                            for alternative in PROVIDER_ORDER[1:]
                            if alternative in self._groups
                        ]
                    }
                ]
                if len(fixture.adapters) > 1
                else [],
                num_retries=0,
                max_fallbacks=max(0, len(fixture.adapters) - 1),
            )

    @staticmethod
    def _load_litellm():
        # LiteLLM otherwise fetches its model-price catalog at import time, which is neither needed
        # nor permitted in this deterministic, offline benchmark.
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        try:
            import litellm
            from litellm import Router
            from litellm.utils import custom_llm_setup
        except ImportError as exc:  # pragma: no cover - CI's dev extra installs LiteLLM.
            raise RuntimeError("Baseline-F-LiteLLM-Router requires the project 'dev' extra") from exc
        return litellm, Router, custom_llm_setup

    @contextmanager
    def _registered_custom_provider(self) -> Iterator[None]:
        """Install the custom provider only around Router construction/calls and restore globals."""
        litellm = self._litellm
        original_map = list(litellm.custom_provider_map)
        original_custom = list(litellm._custom_providers)
        original_providers = list(litellm.provider_list)
        litellm.custom_provider_map = [
            entry for entry in original_map if entry.get("provider") != self._provider_name
        ] + [{"provider": self._provider_name, "custom_handler": self._handler}]
        self._custom_llm_setup()
        try:
            yield
        finally:
            litellm.custom_provider_map = original_map
            litellm._custom_providers[:] = original_custom
            litellm.provider_list[:] = original_providers

    def _complete(self, model: str, messages: List[Dict[str, Any]], optional_params: Dict[str, Any], **kwargs):
        """LiteLLM custom-provider callback backed by the shared mock adapter fixture."""
        provider = model.rsplit("/", 1)[-1]
        if provider not in self.fixture.adapters:
            raise self._litellm.InternalServerError(
                message=f"unknown benchmark provider {provider}", model=model, llm_provider=self._provider_name,
            )
        payload: Dict[str, Any] = {"model": model, "messages": messages}
        for key in ("max_tokens", "temperature", "tools", "tool_choice"):
            if key in optional_params:
                payload[key] = optional_params[key]
        request = openai_request_to_ir(payload)
        adapter = self.fixture.registry.get_adapter(provider)
        endpoint = self.fixture.endpoints[provider]
        result = adapter.execute(adapter.prepare_request(endpoint, request), timeout_seconds=5.0)
        if result.status_code != 200:
            message = result.body.decode("utf-8", errors="replace")
            raise self._litellm.InternalServerError(
                message=f"mock HTTP {result.status_code}: {message}", model=model, llm_provider=self._provider_name,
            )
        normalized = adapter.normalize_response(endpoint, result)
        return self._litellm.ModelResponse(**ir_to_openai_response(normalized, model))

    def run(self, turn: ScenarioTurn) -> NormalizedResponse:
        primary = self._groups.get("provider_a") or next(iter(self._groups.values()))
        payload = ir_to_openai_request(turn.request, primary)
        request_kwargs = {
            key: value
            for key, value in payload.items()
            if key not in ("model", "messages")
        }
        with self._registered_custom_provider():
            response = self._router.completion(model=primary, messages=payload["messages"], **request_kwargs)
        if hasattr(response, "model_dump"):
            raw = response.model_dump()
        else:  # pragma: no cover - compatibility with older supported LiteLLM versions.
            raw = json.loads(response.json())
        # LiteLLM serializes absent tool calls as ``null``; the OpenAI wire form emitted by
        # providers uses an omitted/empty list. Normalize only that representation boundary.
        for choice in raw.get("choices", []):
            message = choice.get("message") or {}
            if message.get("tool_calls") is None:
                message.pop("tool_calls", None)
        return openai_response_to_ir(raw)

    def commit_receipt(self, tool_call: NormalizedToolCall, receipt: Dict[str, Any]) -> None:
        pass  # LiteLLM's Router does not implement this repository's tool receipt ledger.


SYSTEMS: Dict[str, Callable[[Fixture, str], SystemRunner]] = {
    V3Runner.name: V3Runner,
    DirectRunner.name: DirectRunner,
    SameProviderRetryRunner.name: SameProviderRetryRunner,
    StaticFallbackRunner.name: StaticFallbackRunner,
    BreakerStaticFallbackRunner.name: BreakerStaticFallbackRunner,
    V1PrototypeRunner.name: V1PrototypeRunner,
    LiteLLMRouterRunner.name: LiteLLMRouterRunner,
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
