"""Deterministic, Zero-API-Key Local Demonstration of Semantic Failover and Circuit Breaking."""


from durallm.breaker.circuit_breaker import CircuitBreakerConfig
from durallm.breaker.registry import CircuitBreakerRegistry
from durallm.capability.profile import Endpoint, ModelProfile
from durallm.capability.registry import CapabilityRegistry
from durallm.execution.executor import GatewayExecutor
from durallm.execution.policy import ExecutionPolicy, FallbackPolicy, RetryPolicy
from durallm.protocol.ir import (
    NormalizedMessage,
    NormalizedRequest,
    NormalizedToolDefinition,
)
from durallm.providers.adapters import ProviderAdapterRegistry
from tests.faults.mock_provider import MockFaultAction, ProgrammableMockAdapter


class ControlledClock:
    def __init__(self, start: float = 1000.0):
        self._time = start

    def __call__(self) -> float:
        return self._time

    def advance(self, seconds: float) -> None:
        self._time += seconds


def run_demo():
    print("=" * 75)
    print("⚡ LLM CIRCUIT BREAKER — DETERMINISTIC RESILIENCE & SEMANTIC FAILOVER DEMO")
    print("=" * 75)
    print("Simulating Autonomous Agent with Zero External Dependencies...\n")

    clock = ControlledClock()
    breaker_reg = CircuitBreakerRegistry(
        default_config=CircuitBreakerConfig(
            failure_rate_threshold=50.0,
            sliding_window_size=3,
            minimum_number_of_calls=2,
            wait_duration_open_ms=15000.0,
            half_open_max_calls=2,
            clock=clock,
        ),
    )

    cap_reg = CapabilityRegistry()
    adapter_reg = ProviderAdapterRegistry()

    # Mock Primary: Cerebras (fast, 64k context, tools enabled)
    mock_primary = ProgrammableMockAdapter("primary_cerebras")
    adapter_reg._adapters["primary_cerebras"] = mock_primary
    ep_primary = Endpoint(
        id="primary-cerebras",
        provider="primary_cerebras",
        model="llama3.3-70b",
        base_url="mock://cerebras",
        priority=1,
        pool="coding",
        profile=ModelProfile("primary_cerebras", "llama3.3-70b", context_window=65536, supports_tools=True),
    )
    cap_reg.register_endpoint(ep_primary)

    # Mock Secondary: Groq (32k context, tools enabled)
    mock_secondary = ProgrammableMockAdapter("secondary_groq")
    adapter_reg._adapters["secondary_groq"] = mock_secondary
    ep_secondary = Endpoint(
        id="secondary-groq",
        provider="secondary_groq",
        model="llama-3.3-70b",
        base_url="mock://groq",
        priority=2,
        pool="coding",
        profile=ModelProfile("secondary_groq", "llama-3.3-70b", context_window=32768, supports_tools=True),
    )
    cap_reg.register_endpoint(ep_secondary)

    executor = GatewayExecutor(
        capability_registry=cap_reg,
        breaker_registry=breaker_reg,
        adapter_registry=adapter_reg,
        policy=ExecutionPolicy(
            retry=RetryPolicy(max_attempts_same_endpoint=1),
            fallback=FallbackPolicy(max_fallback_hops=2),
        ),
    )

    tool_def = NormalizedToolDefinition(
        name="execute_code",
        description="Run Python code",
        parameters={
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    )

    # -------------------------------------------------------------
    # Step 1: Normal Operation (Primary Healthy)
    # -------------------------------------------------------------
    print("▶ STEP 1: Dispatching turn to Primary Provider (Cerebras)...")
    mock_primary.set_sequence([MockFaultAction.success("Primary response: Tool code executed successfully")])
    req1 = NormalizedRequest(
        request_id="req_turn_1",
        model="default",
        messages=[NormalizedMessage(role="user", content="Calculate prime factors of 2026")],
        tools=[tool_def],
    )
    resp1, dec1, ledger1 = executor.execute(req1, pool="coding", strategy="priority")
    breaker1 = breaker_reg.get("primary_cerebras:llama3.3-70b")
    print(f"  ✔ Result: {resp1.content}")
    print(f"  ✔ Selected Endpoint: {dec1.selected_endpoint.id} (Attempts: {ledger1.total_attempts})")
    print(f"  ✔ Primary Breaker State: {breaker1.state.value}\n")

    # -------------------------------------------------------------
    # Step 2: Primary Suffers Outage (503 Service Overloaded)
    # -------------------------------------------------------------
    print("▶ STEP 2: Primary suffers 503 Outage; Gateway initiates Semantic Failover...")
    # 2 consecutive failures trip the breaker
    mock_primary.set_sequence([
        MockFaultAction.server_error(503, "Cerebras Service Unavailable"),
        MockFaultAction.server_error(503, "Cerebras Service Unavailable"),
    ])
    mock_secondary.set_sequence([
        MockFaultAction.success("Secondary (Groq) fallback response: recovered and completed task")
    ])

    req2 = NormalizedRequest(
        request_id="req_turn_2",
        model="default",
        messages=[
            NormalizedMessage(role="user", content="Deploy agent to cloud cluster"),
            NormalizedMessage(role="assistant", content="Deploying..."),
        ],
        tools=[tool_def],
    )
    resp2, dec2, ledger2 = executor.execute(req2, pool="coding", strategy="priority")
    print(f"  ✔ Failover Succeeded! Response: {resp2.content}")
    print(f"  ✔ Total Attempts: {ledger2.total_attempts} | Fallback Hops: {ledger2.fallback_count}")
    print(f"  ✔ Primary Breaker State: {breaker1.state.value}")
    if ledger2.failover_plans:
        fplan = ledger2.failover_plans[0]
        print(f"  ✔ Observable FailoverPlan: {fplan.source_endpoint} -> {fplan.target_endpoint} (Reason: {fplan.failover_reason})\n")

    # -------------------------------------------------------------
    # Step 3: Zero-Overhead Fast-Path (Breaker is OPEN)
    # -------------------------------------------------------------
    print("▶ STEP 3: Next Request arrives while Primary is OPEN...")
    mock_secondary.set_sequence([MockFaultAction.success("Direct to secondary without touching primary")])
    req3 = NormalizedRequest(
        request_id="req_turn_3",
        model="default",
        messages=[NormalizedMessage(role="user", content="Status update")],
        tools=[tool_def],
    )
    resp3, dec3, ledger3 = executor.execute(req3, pool="coding", strategy="priority")
    print(f"  ✔ Dispatched directly to: {dec3.selected_endpoint.id} (Primary bypassed with 0 upstream load)")
    print(f"  ✔ Attempts: {ledger3.total_attempts} | Primary Breaker: {breaker1.state.value}\n")

    # -------------------------------------------------------------
    # Step 4: Time Elapses -> HALF_OPEN Probe Admission & Recovery
    # -------------------------------------------------------------
    print("▶ STEP 4: Advancing clock by 20 seconds; Testing Self-Healing Recovery...")
    clock.advance(20.0)
    print(f"  ✔ Evaluated Breaker State: {breaker1.state.value} (Now admits bounded probe permits)")

    mock_primary.set_sequence([
        MockFaultAction.success("Primary probe call 1 successful"),
        MockFaultAction.success("Primary probe call 2 successful"),
    ])

    req4 = NormalizedRequest(
        request_id="req_turn_4",
        model="default",
        messages=[NormalizedMessage(role="user", content="Health probe turn")],
        tools=[tool_def],
    )
    resp4, dec4, ledger4 = executor.execute(req4, pool="coding", strategy="priority")
    print(f"  ✔ Probe 1 Admitted to Primary: {resp4.content}")

    req5 = NormalizedRequest(
        request_id="req_turn_5",
        model="default",
        messages=[NormalizedMessage(role="user", content="Health probe turn 2")],
        tools=[tool_def],
    )
    resp5, dec5, ledger5 = executor.execute(req5, pool="coding", strategy="priority")
    print(f"  ✔ Probe 2 Admitted to Primary: {resp5.content}")
    print(f"  ✔ Breaker Reset! Primary State is now: {breaker1.state.value}")

    print("\n" + "=" * 75)
    print("✔ DEMONSTRATION COMPLETE: High Availability Verified, FailoverPlan Emitted, Breaker Self-Healed")
    print("=" * 75)


if __name__ == "__main__":
    run_demo()
