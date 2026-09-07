"""Multi-Objective Routing Scorer with Observed Telemetry and Cold-Start Policy."""

from __future__ import annotations

from typing import Optional

from llm_circuit_breaker.breaker.state import CircuitBreakerState
from llm_circuit_breaker.capability.profile import Endpoint
from llm_circuit_breaker.health.telemetry import EndpointHealthSnapshot
from llm_circuit_breaker.routing.decision import CandidateEvaluation


class RoutingScorer:
    """
    Computes explainable multi-objective scores for eligible candidate endpoints.
    Uses real observed telemetry; cold-start endpoints are evaluated under an
    explicit exploration policy rather than fake hardcoded inputs.
    """

    def __init__(
        self,
        weight_quality: float = 0.3,
        weight_reliability: float = 0.3,
        weight_latency: float = 0.2,
        weight_cost: float = 0.2,
        cold_start_latency_score: float = 0.8,
    ):
        self.w_quality = weight_quality
        self.w_reliability = weight_reliability
        self.w_latency = weight_latency
        self.w_cost = weight_cost
        self.cold_start_latency_score = cold_start_latency_score

    def score_candidate(
        self,
        endpoint: Endpoint,
        breaker_state: CircuitBreakerState,
        health: Optional[EndpointHealthSnapshot] = None,
        warm_cache: bool = False,
    ) -> CandidateEvaluation:
        """
        Compute multi-objective score based on REAL observed telemetry:
        - Reliability: Breaker state + observed success rate + tool success rate.
        - Latency: Observed EMA latency (or cold-start exploration score if UNKNOWN).
        - Cost: Inversely proportional to declared pricing (free = 1.0).
        - Quality: Verified tool calling, reasoning, and context window capability.
        - Cache: Prompt-cache bonus for endpoints with active prefix in memory.
        """
        # 1. Health / Reliability Score (0.0 to 1.0)
        if breaker_state in (CircuitBreakerState.OPEN, CircuitBreakerState.FORCED_OPEN):
            reliability_score = 0.0
        elif breaker_state == CircuitBreakerState.HALF_OPEN:
            reliability_score = 0.5
        elif health is not None and health.total_calls > 0:
            # Combined availability success and tool reliability
            obs_succ = health.success_rate
            tool_succ = health.tool_success_rate
            sem_succ = health.semantic_success_rate
            reliability_score = (0.5 * obs_succ) + (0.3 * tool_succ) + (0.2 * sem_succ)
        else:
            # Cold start: neutral reliability
            reliability_score = 1.0

        # 2. Latency Score (Real Observed Telemetry)
        is_cold_start = False
        if health is not None and health.ema_latency_ms is not None:
            # Real observed EMA latency, normalized against a 5000ms ceiling
            observed_lat = health.ema_latency_ms
            latency_score = max(0.0, min(1.0, 1.0 - (observed_lat / 5000.0)))
        else:
            # Cold-start policy: optimistic exploration permit
            is_cold_start = True
            observed_lat = None
            latency_score = self.cold_start_latency_score

        # 3. Cost Score
        profile = endpoint.profile
        if profile and profile.is_free:
            cost_score = 1.0
        elif profile and profile.input_price_per_1m > 0:
            cost_score = max(0.1, min(1.0, 1.0 / (1.0 + profile.input_price_per_1m)))
        else:
            cost_score = 0.8

        # 4. Quality Score
        if profile:
            tool_bonus = 0.3 if profile.supports_tools else 0.0
            reason_bonus = 0.2 if profile.supports_reasoning else 0.0
            window_bonus = min(0.3, profile.context_window / 1000000.0)
            quality_score = 0.2 + tool_bonus + reason_bonus + window_bonus
        else:
            quality_score = 0.5

        final_score = (
            self.w_quality * quality_score
            + self.w_reliability * reliability_score
            + self.w_latency * latency_score
            + self.w_cost * cost_score
        )
        # Frontier 6: Boost candidates holding a warm prompt cache for this prefix
        if warm_cache:
            final_score += 0.25

        return CandidateEvaluation(
            endpoint_id=endpoint.id,
            provider=endpoint.provider,
            model=endpoint.model,
            eligible=True,
            breaker_state=breaker_state.value,
            hard_constraints_passed=True,
            health_score=reliability_score,
            latency_score=latency_score,
            cost_score=cost_score,
            final_score=final_score,
            is_cold_start=is_cold_start,
            observed_latency_ms=observed_lat,
            warm_cache=warm_cache,
        )
