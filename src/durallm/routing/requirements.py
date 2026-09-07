"""Routing Requirement Vectors and Hard-Constraint Matching."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional, Tuple

from durallm.capability.profile import ModelProfile

if TYPE_CHECKING:
    from durallm.health.telemetry import EndpointHealthSnapshot


@dataclass
class RequirementVector:
    """Explicit requirements that candidate models must satisfy."""
    require_tools: bool = False
    require_parallel_tools: bool = False
    require_structured_output: bool = False
    require_vision: bool = False
    require_reasoning: bool = False
    minimum_context_tokens: int = 0
    maximum_cost_usd: Optional[float] = None   # cap on the per-request cost estimate below
    latency_budget_ms: Optional[float] = None  # cap on the endpoint's observed EMA latency
    task_class: str = "general"  # "coding" or "general"
    allowed_providers: Optional[List[str]] = None
    forbidden_providers: Optional[List[str]] = None
    # Filled by the executor from the request; used only for the cost estimate.
    estimated_input_tokens: int = 0
    expected_output_tokens: int = 0
    safety_margin_tokens: int = 0
    allow_context_compaction: bool = True
    # Data-boundary constraints. ``allow_external_traffic=False`` requires an
    # endpoint explicitly declared for non-external traffic.
    required_region: Optional[str] = None
    required_compliance: Optional[List[str]] = None
    allow_external_traffic: Optional[bool] = None
    require_verified_capabilities: bool = False
    # Quality degradation is a caller consent, never an implicit fallback
    # behavior. A threshold only becomes hard when consent is absent.
    minimum_quality_score: Optional[float] = None
    allow_quality_degradation: bool = False
    # Agent/session budget. Reservation happens immediately before dispatch so
    # concurrent attempts cannot collectively overspend it.
    budget_scope: Optional[str] = None
    budget_limit_usd: Optional[float] = None
    # Frontier 6: Byte-stable SHA-256 hash of system prompt and sorted tool signatures
    prefix_hash: Optional[str] = None

    def estimated_cost_usd(self, profile: ModelProfile) -> float:
        """Per-request cost estimate: input tokens x input price + expected output tokens x output price."""
        out_tokens = self.expected_output_tokens or min(4096, profile.context_window)
        return (
            (self.estimated_input_tokens * profile.input_price_per_1m)
            + (out_tokens * profile.output_price_per_1m)
        ) / 1_000_000.0

    def matches_hard_constraints(
        self,
        profile: ModelProfile,
        health: Optional["EndpointHealthSnapshot"] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Evaluate candidate against hard constraints.
        Returns (passed, exclusion_reason).
        The latency budget is checked only when observed telemetry exists; cold-start endpoints pass.
        """
        if self.safety_margin_tokens < 0:
            return False, "Safety margin must be non-negative"

        if self.minimum_quality_score is not None and not 0.0 <= self.minimum_quality_score <= 1.0:
            return False, "Minimum quality score must be in [0, 1]"

        if self.budget_limit_usd is not None and self.budget_limit_usd < 0:
            return False, "Budget limit must be non-negative"

        # Provider filtering
        if self.allowed_providers is not None:
            if profile.provider.lower() not in [p.lower() for p in self.allowed_providers]:
                return False, f"Provider '{profile.provider}' not in allowed list"

        if self.forbidden_providers is not None:
            if profile.provider.lower() in [p.lower() for p in self.forbidden_providers]:
                return False, f"Provider '{profile.provider}' is in forbidden list"

        # Tool calling (None = undeclared capability for an unknown model; treated as unsupported)
        if self.require_tools and not profile.supports_tools:
            if profile.supports_tools is None:
                return False, f"Model '{profile.model}' has no declared tool-calling capability (unknown model)"
            return False, f"Model '{profile.model}' does not support tool calling"

        # Parallel tools
        if self.require_parallel_tools and not profile.supports_parallel_tools:
            return False, f"Model '{profile.model}' does not support parallel tool calling"

        # Structured output
        if self.require_structured_output and not profile.supports_structured_output:
            return False, f"Model '{profile.model}' does not support structured output"

        # Vision
        if self.require_vision and not profile.supports_vision:
            return False, f"Model '{profile.model}' does not support vision/images"

        # Reasoning
        if self.require_reasoning and not profile.supports_reasoning:
            return False, f"Model '{profile.model}' does not support reasoning/thinking"

        # Capability provenance / expiry. A declared or stale profile may be
        # usable for best-effort traffic but is ineligible when a caller asks
        # for an audited capability claim.
        if self.require_verified_capabilities and not profile.capabilities_are_current(time.time()):
            return False, f"Model '{profile.model}' lacks a current verified capability profile"

        # Privacy and residency are eligibility constraints, not scoring hints.
        privacy = profile.privacy
        if self.required_region is not None and privacy.region != self.required_region:
            return False, f"Model '{profile.model}' is not declared in required region '{self.required_region}'"
        if self.required_compliance is not None:
            declared = {item.lower() for item in privacy.compliance}
            missing = [item for item in self.required_compliance if item.lower() not in declared]
            if missing:
                return False, f"Model '{profile.model}' lacks required compliance: {', '.join(missing)}"
        if self.allow_external_traffic is False and privacy.allows_external_traffic:
            return False, f"Model '{profile.model}' permits external traffic"

        if self.minimum_quality_score is not None:
            score = profile.expected_quality_score
            if score is None and not self.allow_quality_degradation:
                return False, f"Model '{profile.model}' has no calibrated quality estimate"
            if score is not None and score < self.minimum_quality_score and not self.allow_quality_degradation:
                return False, (
                    f"Model quality ({score:.2f}) is below required minimum "
                    f"({self.minimum_quality_score:.2f}) without degradation consent"
                )

        # Minimum context window
        if self.minimum_context_tokens > 0 and profile.context_window < self.minimum_context_tokens:
            return False, f"Context window ({profile.context_window} tokens) below minimum required ({self.minimum_context_tokens} tokens)"

        # Cost ceiling
        if self.maximum_cost_usd is not None:
            est = self.estimated_cost_usd(profile)
            if est > self.maximum_cost_usd:
                return False, f"Estimated cost ${est:.6f} exceeds maximum ${self.maximum_cost_usd:.6f}"

        # Latency budget against observed EMA
        if (
            self.latency_budget_ms is not None
            and health is not None
            and health.ema_latency_ms is not None
            and health.ema_latency_ms > self.latency_budget_ms
        ):
            return False, f"Observed latency EMA ({health.ema_latency_ms:.0f}ms) exceeds budget ({self.latency_budget_ms:.0f}ms)"

        return True, None
