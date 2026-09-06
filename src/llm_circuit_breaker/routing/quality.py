"""Shadow-only quality estimates for task-aware routing evaluation."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

from llm_circuit_breaker.capability.profile import Endpoint


@dataclass(frozen=True)
class QualityEstimate:
    score: float
    confidence: float
    provenance: str
    expires_at: Optional[float] = None

    def is_current(self, now: float) -> bool:
        return self.expires_at is None or self.expires_at > now


@dataclass(frozen=True)
class ShadowQualityRecommendation:
    endpoint_id: Optional[str]
    score: Optional[float]
    abstained: bool
    reason: str


class ShadowQualityPolicy:
    """Collect calibrated quality observations without changing live routing.

    The policy recommends only when it has a current estimate at or above the
    configured confidence threshold. ``CapabilityRouter`` records the result
    for comparison but deliberately never selects from it.
    """

    def __init__(self, min_confidence: float = 0.70, clock=time.time) -> None:
        self.min_confidence = min_confidence
        self._clock = clock
        self._lock = threading.RLock()
        self._estimates: Dict[Tuple[str, str], QualityEstimate] = {}

    @staticmethod
    def _key(endpoint: Endpoint, task_class: str) -> Tuple[str, str]:
        return endpoint.id, task_class

    def observe(
        self,
        endpoint: Endpoint,
        task_class: str,
        score: float,
        confidence: float,
        provenance: str,
        expires_at: Optional[float] = None,
    ) -> None:
        if not 0.0 <= score <= 1.0 or not 0.0 <= confidence <= 1.0:
            raise ValueError("quality score and confidence must be in [0, 1]")
        with self._lock:
            self._estimates[self._key(endpoint, task_class)] = QualityEstimate(
                score=score,
                confidence=confidence,
                provenance=provenance,
                expires_at=expires_at,
            )

    def estimate(self, endpoint: Endpoint, task_class: str) -> Optional[QualityEstimate]:
        with self._lock:
            observed = self._estimates.get(self._key(endpoint, task_class))
        if observed is not None and observed.is_current(self._clock()):
            return observed
        profile = endpoint.profile
        if profile and profile.expected_quality_score is not None:
            return QualityEstimate(
                score=profile.expected_quality_score,
                confidence=profile.quality_confidence,
                provenance=profile.quality_provenance or "profile_declared",
                expires_at=profile.capability_expires_at,
            )
        return None

    def recommend(self, endpoints: Iterable[Endpoint], task_class: str) -> ShadowQualityRecommendation:
        candidates = []
        for endpoint in endpoints:
            estimate = self.estimate(endpoint, task_class)
            if estimate is not None and estimate.confidence >= self.min_confidence:
                candidates.append((endpoint.id, estimate))
        if not candidates:
            return ShadowQualityRecommendation(
                endpoint_id=None,
                score=None,
                abstained=True,
                reason="No current quality estimate meets the confidence threshold",
            )
        endpoint_id, best = max(candidates, key=lambda item: item[1].score)
        return ShadowQualityRecommendation(
            endpoint_id=endpoint_id,
            score=best.score,
            abstained=False,
            reason=f"{best.provenance} at confidence {best.confidence:.2f}",
        )
