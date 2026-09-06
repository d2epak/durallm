"""Explainable Semantic Failover Plan."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class FailoverPlan:
    """
    Explicit, auditable plan created when transferring execution from one provider/model to another.
    Coordinates state preservation, context budgeting, protocol translation, and tool safety.
    """
    plan_id: str = field(default_factory=lambda: f"fplan_{uuid.uuid4().hex[:8]}")
    request_id: str = ""
    source_endpoint: Optional[str] = None
    target_endpoint: str = ""
    failover_reason: str = ""
    state_snapshot_id: Optional[str] = None
    required_transformations: List[str] = field(default_factory=list)
    context_tokens_before: int = 0
    context_tokens_after: int = 0
    context_compaction_applied: bool = False
    tool_transformations: List[str] = field(default_factory=list)
    risk_flags: List[str] = field(default_factory=list)
    remaining_deadline_ms: float = 0.0
    remaining_cost_budget_usd: Optional[float] = None
    created_at_monotonic: float = field(default_factory=time.monotonic)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
