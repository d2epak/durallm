"""V2 Capability-Aware Circuit-Breaker Routing Engine."""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from durallm.breaker.registry import (
    DEFAULT_BREAKER_REGISTRY,
    CircuitBreakerRegistry,
)
from durallm.breaker.state import CircuitBreakerState
from durallm.capability.profile import Endpoint
from durallm.capability.registry import (
    DEFAULT_CAPABILITY_REGISTRY,
    CapabilityRegistry,
)
from durallm.routing.cache import (
    PromptCacheTracker,
)
from durallm.routing.decision import (
    CandidateEvaluation,
    RoutingDecision,
)
from durallm.routing.quality import ShadowQualityPolicy
from durallm.routing.requirements import RequirementVector
from durallm.routing.resources import (
    ResourceLaneStore,
)
from durallm.routing.scorer import RoutingScorer
from durallm.routing.tokenizer import preflight_context

logger = logging.getLogger("durallm.routing")


class CapabilityRouter:
    """Capability-aware and circuit-breaker-controlled routing engine."""

    def __init__(
        self,
        capability_registry: Optional[CapabilityRegistry] = None,
        breaker_registry: Optional[CircuitBreakerRegistry] = None,
        health_store: Optional[Any] = None,
        default_strategy: str = "balanced",
        lane_store: Optional[ResourceLaneStore] = None,
        shadow_quality_policy: Optional[ShadowQualityPolicy] = None,
        cache_tracker: Optional[PromptCacheTracker] = None,
    ):
        self.capability_registry = capability_registry or DEFAULT_CAPABILITY_REGISTRY
        self.breaker_registry = breaker_registry or DEFAULT_BREAKER_REGISTRY
        from durallm.health.telemetry import DEFAULT_HEALTH_STORE
        self.health_store = health_store or DEFAULT_HEALTH_STORE
        self.default_strategy = default_strategy
        self.lane_store = lane_store if lane_store is not None else ResourceLaneStore()
        self.shadow_quality_policy = shadow_quality_policy if shadow_quality_policy is not None else ShadowQualityPolicy()
        self.cache_tracker = cache_tracker if cache_tracker is not None else PromptCacheTracker()
        self.scorer = RoutingScorer()

        self._lock = threading.RLock()
        self._round_robin_indices: Dict[str, int] = {}
        self.dead_list: set[str] = set()

    def mark_dead(self, endpoint_id: str, provider: str = "", model: str = "", reason: str = "") -> None:
        """Permanently mark an endpoint or provider:model as dead/blacklisted."""
        with self._lock:
            self.dead_list.add(endpoint_id)
            if provider and model:
                self.dead_list.add(f"{provider}:{model}")
            self.health_store.mark_dead(endpoint_id, provider=provider, model=model, reason=reason)
            logger.warning("[durallm] BLACKLISTED PERMANENT DEAD ENDPOINT: %s (%s)", endpoint_id, reason)

    def clear_dead_list(self) -> None:
        """Reset permanent dead list."""
        with self._lock:
            self.dead_list.clear()
            self.health_store.clear_dead_list()

    def is_dead(self, endpoint_id: str, provider: str = "", model: str = "") -> bool:
        with self._lock:
            if endpoint_id in self.dead_list:
                return True
            if provider and model and f"{provider}:{model}" in self.dead_list:
                return True
            health_snap = self.health_store.get_or_create(endpoint_id, provider=provider, model=model)
            return health_snap.is_dead

    def select_candidate(
        self,
        requirements: RequirementVector,
        pool: str = "general_agent",
        strategy: Optional[str] = None,
        request_id: str = "",
        excluded_endpoints: Optional[List[str]] = None,
        fallback_reason: Optional[str] = None,
    ) -> Tuple[Optional[Endpoint], RoutingDecision]:
        """
        Evaluate candidate pipeline:
        1. Hard constraint compatibility
        2. Breaker admission filter
        3. Soft scoring
        4. Strategy selection
        """
        strat = strategy or self.default_strategy
        endpoints = self.capability_registry.endpoints_for_pool(pool)
        if not endpoints:
            # A pool is an isolation boundary (ADR 0010): never widen to other pools' endpoints.
            logger.warning("Pool '%s' has no registered endpoints", pool)

        exclusions = set(excluded_endpoints or [])
        evaluations: List[CandidateEvaluation] = []
        eligible_endpoints: List[Tuple[Endpoint, CandidateEvaluation]] = []

        for ep in endpoints:
            # Check manual exclusion (e.g. from current attempt ledger)
            if ep.id in exclusions:
                evaluations.append(
                    CandidateEvaluation(
                        endpoint_id=ep.id,
                        provider=ep.provider,
                        model=ep.model,
                        eligible=False,
                        exclusion_reason="Excluded by attempt ledger or recent failure",
                    )
                )
                continue

            # Check permanent DeadList / EOL / Blacklist pre-flight short circuit
            if self.is_dead(ep.id, provider=ep.provider, model=ep.model):
                health_snap = self.health_store.get_or_create(ep.id, provider=ep.provider, model=ep.model)
                reason = health_snap.dead_reason or "Permanently blacklisted dead model / configuration error"
                evaluations.append(
                    CandidateEvaluation(
                        endpoint_id=ep.id,
                        provider=ep.provider,
                        model=ep.model,
                        eligible=False,
                        exclusion_reason=f"DeadList pre-flight short-circuit: {reason}",
                    )
                )
                continue

            profile = ep.profile or self.capability_registry.get_profile(ep.provider, ep.model)
            health_snap = self.health_store.get_or_create(ep.id, provider=ep.provider, model=ep.model)

            # 1. Hard constraint filter (includes cost ceiling and observed-latency budget)
            passed, reason = requirements.matches_hard_constraints(profile, health=health_snap)
            if not passed:
                evaluations.append(
                    CandidateEvaluation(
                        endpoint_id=ep.id,
                        provider=ep.provider,
                        model=ep.model,
                        eligible=False,
                        hard_constraints_passed=False,
                        exclusion_reason=reason,
                        is_cold_start=health_snap.is_cold_start,
                    )
                )
                continue

            # A context decision has to be based on a named tokenizer.  This
            # records conservative-estimator uncertainty rather than silently
            # treating character counts as provider-token truth.
            preflight = preflight_context(
                profile=profile,
                input_tokens=requirements.estimated_input_tokens,
                expected_output_tokens=requirements.expected_output_tokens,
                safety_margin_tokens=requirements.safety_margin_tokens,
                allow_compaction=requirements.allow_context_compaction,
            )
            if not preflight.compatible:
                evaluations.append(
                    CandidateEvaluation(
                        endpoint_id=ep.id,
                        provider=ep.provider,
                        model=ep.model,
                        eligible=False,
                        hard_constraints_passed=False,
                        exclusion_reason=preflight.reason,
                        is_cold_start=health_snap.is_cold_start,
                        resource_lane=ep.lane_key,
                        tokenizer_id=preflight.tokenizer_id,
                        tokenizer_revision=preflight.tokenizer_revision,
                        preflight_requires_compaction=preflight.requires_compaction,
                    )
                )
                continue

            # Rate limits and quota exhaustions are scoped to a credential or
            # deployment lane.  They must not disable unrelated credentials
            # which happen to use the same provider/model.
            if ep.lane_key is not None:
                lane_status = self.lane_store.status(ep.lane_key)
                if not lane_status.available:
                    evaluations.append(
                        CandidateEvaluation(
                            endpoint_id=ep.id,
                            provider=ep.provider,
                            model=ep.model,
                            eligible=False,
                            hard_constraints_passed=True,
                            exclusion_reason=(
                                "Resource lane '%s' unavailable%s" % (
                                    ep.lane_key,
                                    ": " + lane_status.reason if lane_status.reason else "",
                                )
                            ),
                            is_cold_start=health_snap.is_cold_start,
                            resource_lane=ep.lane_key,
                            resource_lane_available=False,
                            tokenizer_id=preflight.tokenizer_id,
                            tokenizer_revision=preflight.tokenizer_revision,
                            preflight_requires_compaction=preflight.requires_compaction,
                        )
                    )
                    continue

            # 2. Circuit Breaker Admission filter (DISABLED / METRICS_ONLY pass through per ADR 0001;
            #    HALF_OPEN is left to the breaker's probe admission at execution time)
            breaker = self.breaker_registry.get_or_create(ep.resource_key)
            breaker_state = breaker.state
            if breaker_state == CircuitBreakerState.OPEN or breaker_state == CircuitBreakerState.FORCED_OPEN:
                evaluations.append(
                    CandidateEvaluation(
                        endpoint_id=ep.id,
                        provider=ep.provider,
                        model=ep.model,
                        eligible=False,
                        breaker_state=breaker_state.value,
                        exclusion_reason=f"Circuit breaker is {breaker_state.value}",
                        is_cold_start=health_snap.is_cold_start,
                    )
                )
                continue

            # 3. Soft scoring with real observed telemetry and prompt cache awareness
            prefix_hash = getattr(requirements, "prefix_hash", None)
            is_warm = bool(prefix_hash and self.cache_tracker.is_warm(ep.id, prefix_hash))
            eval_record = self.scorer.score_candidate(
                endpoint=ep,
                breaker_state=breaker_state,
                health=health_snap,
                warm_cache=is_warm,
            )
            quality = self.shadow_quality_policy.estimate(ep, requirements.task_class)
            eval_record.resource_lane = ep.lane_key
            eval_record.resource_lane_available = True
            eval_record.tokenizer_id = preflight.tokenizer_id
            eval_record.tokenizer_revision = preflight.tokenizer_revision
            eval_record.preflight_requires_compaction = preflight.requires_compaction
            if quality is not None:
                eval_record.expected_quality_score = quality.score
                eval_record.quality_confidence = quality.confidence
                eval_record.quality_provenance = quality.provenance
                eval_record.degradation_required = bool(
                    requirements.minimum_quality_score is not None
                    and quality.score < requirements.minimum_quality_score
                )
            evaluations.append(eval_record)
            eligible_endpoints.append((ep, eval_record))

        total_considered = len(endpoints)
        total_eligible = len(eligible_endpoints)

        shadow = self.shadow_quality_policy.recommend(endpoints, requirements.task_class)

        if not eligible_endpoints:
            decision = RoutingDecision(
                request_id=request_id,
                strategy=strat,
                selected_endpoint=None,
                evaluated_candidates=evaluations,
                total_considered=total_considered,
                total_eligible=0,
                fallback_reason=fallback_reason,
                shadow_recommended_endpoint=shadow.endpoint_id,
                shadow_recommended_score=shadow.score,
                shadow_abstained=shadow.abstained,
                shadow_reason=shadow.reason,
            )
            return None, decision

        # 4. Strategy Selection
        selected_endpoint: Optional[Endpoint] = None

        if strat == "priority":
            is_large_context = requirements.estimated_input_tokens > 5000

            def priority_key(item: Tuple[Endpoint, CandidateEvaluation]):
                ep, ev = item
                cw = (ep.profile.context_window if ep.profile else 65536) or 65536
                is_groq = ep.provider.lower() == "groq"
                if is_large_context:
                    tier = 2 if is_groq else (0 if cw >= 200000 else 1)
                    return (tier, ep.priority, -ev.final_score)
                return (ep.priority, -ev.final_score)

            eligible_endpoints.sort(key=priority_key)
            selected_endpoint = eligible_endpoints[0][0]

        elif strat == "round_robin":
            with self._lock:
                idx = self._round_robin_indices.get(pool, 0) % len(eligible_endpoints)
                selected_endpoint = eligible_endpoints[idx][0]
                self._round_robin_indices[pool] = (idx + 1) % len(eligible_endpoints)

        elif strat == "latency_aware":
            eligible_endpoints.sort(key=lambda x: x[1].latency_score, reverse=True)
            selected_endpoint = eligible_endpoints[0][0]

        elif strat == "cost_aware":
            eligible_endpoints.sort(key=lambda x: x[1].cost_score, reverse=True)
            selected_endpoint = eligible_endpoints[0][0]

        elif strat == "reliability_aware":
            eligible_endpoints.sort(key=lambda x: x[1].health_score, reverse=True)
            selected_endpoint = eligible_endpoints[0][0]

        else:  # "balanced" / default
            eligible_endpoints.sort(key=lambda x: x[1].final_score, reverse=True)
            selected_endpoint = eligible_endpoints[0][0]

        # Assign ranks
        for rank, (ep, ev) in enumerate(eligible_endpoints, 1):
            ev.rank = rank

        decision = RoutingDecision(
            request_id=request_id,
            strategy=strat,
            selected_endpoint=selected_endpoint,
            evaluated_candidates=evaluations,
            total_considered=total_considered,
            total_eligible=total_eligible,
            fallback_reason=fallback_reason,
            shadow_recommended_endpoint=shadow.endpoint_id,
            shadow_recommended_score=shadow.score,
            shadow_abstained=shadow.abstained,
            shadow_reason=shadow.reason,
        )

        return selected_endpoint, decision
