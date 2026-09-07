"""Explainable Routing Decision Records."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from llm_circuit_breaker.capability.profile import Endpoint


@dataclass
class CandidateEvaluation:
    """Evaluation record for a single candidate considered during routing."""
    endpoint_id: str
    provider: str
    model: str
    eligible: bool
    exclusion_reason: Optional[str] = None
    breaker_state: str = "CLOSED"
    hard_constraints_passed: bool = True
    health_score: float = 1.0
    latency_score: float = 1.0
    cost_score: float = 1.0
    final_score: float = 0.0
    rank: int = 0
    is_cold_start: bool = False
    observed_latency_ms: Optional[float] = None
    # Admission and capability evidence are carried with the decision so an
    # operator never has to infer why a particular provider was (not) used.
    resource_lane: Optional[str] = None
    resource_lane_available: bool = True
    tokenizer_id: Optional[str] = None
    tokenizer_revision: Optional[str] = None
    preflight_requires_compaction: bool = False
    expected_quality_score: Optional[float] = None
    quality_confidence: float = 0.0
    quality_provenance: Optional[str] = None
    degradation_required: bool = False
    warm_cache: bool = False


@dataclass
class RoutingDecision:
    """Full explainable record of a routing decision."""
    decision_id: str = field(default_factory=lambda: f"dec_{uuid.uuid4().hex[:8]}")
    request_id: str = ""
    strategy: str = "balanced"
    selected_endpoint: Optional[Endpoint] = None
    evaluated_candidates: List[CandidateEvaluation] = field(default_factory=list)
    total_considered: int = 0
    total_eligible: int = 0
    fallback_reason: Optional[str] = None
    # Shadow quality is observability-only: it is intentionally never allowed
    # to alter a live routing choice until an explicit policy enables that.
    shadow_recommended_endpoint: Optional[str] = None
    shadow_recommended_score: Optional[float] = None
    shadow_abstained: bool = True
    shadow_reason: Optional[str] = None
    timestamp_monotonic: float = field(default_factory=time.monotonic)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "request_id": self.request_id,
            "strategy": self.strategy,
            "selected": f"{self.selected_endpoint.provider}/{self.selected_endpoint.model}" if self.selected_endpoint else None,
            "total_considered": self.total_considered,
            "total_eligible": self.total_eligible,
            "fallback_reason": self.fallback_reason,
            "shadow_quality": {
                "recommended_endpoint": self.shadow_recommended_endpoint,
                "recommended_score": self.shadow_recommended_score,
                "abstained": self.shadow_abstained,
                "reason": self.shadow_reason,
            },
            "candidates": [asdict(c) for c in self.evaluated_candidates],
        }
