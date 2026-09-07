"""Agent Semantic State Representation.

Preserves critical agent invariants (objective, constraints, tool definitions,
execution state, decisions, and subgoals) independent of provider protocols.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from durallm.protocol.ir import NormalizedToolDefinition


@dataclass
class AgentState:
    """Provider-neutral semantic state model for autonomous agents."""
    agent_id: str = field(default_factory=lambda: f"agent_{uuid.uuid4().hex[:8]}")
    objective: str = ""
    constraints: List[str] = field(default_factory=list)
    important_decisions: List[str] = field(default_factory=list)
    tool_definitions: List[NormalizedToolDefinition] = field(default_factory=list)
    tool_execution_state: Dict[str, Any] = field(default_factory=dict)
    known_files: Dict[str, str] = field(default_factory=dict)  # path -> status/hash
    latest_tool_outputs: Dict[str, str] = field(default_factory=dict)  # tool_id -> output
    unresolved_errors: List[str] = field(default_factory=list)
    current_subgoal: Optional[str] = None
    version: int = 1
    updated_at: float = field(default_factory=time.time)

    def add_constraint(self, constraint: str) -> None:
        if constraint and constraint not in self.constraints:
            self.constraints.append(constraint)
            self._bump_version()

    def add_decision(self, decision: str) -> None:
        if decision and decision not in self.important_decisions:
            self.important_decisions.append(decision)
            self._bump_version()

    def record_tool_output(self, tool_id: str, output: str) -> None:
        self.latest_tool_outputs[tool_id] = output
        self._bump_version()

    def _bump_version(self) -> None:
        self.version += 1
        self.updated_at = time.time()

    def create_snapshot(self) -> StateSnapshot:
        """Capture an immutable, hashable snapshot of current semantic state."""
        return StateSnapshot.from_agent_state(self)


@dataclass(frozen=True)
class StateSnapshot:
    """Immutable, versioned snapshot of AgentState."""
    snapshot_id: str
    agent_id: str
    version: int
    payload_json: str
    digest: str
    created_at: float

    @classmethod
    def from_agent_state(cls, state: AgentState) -> StateSnapshot:
        data = {
            "agent_id": state.agent_id,
            "objective": state.objective,
            "constraints": state.constraints,
            "important_decisions": state.important_decisions,
            "tool_definitions": [
                {"name": t.name, "description": t.description, "parameters": t.parameters}
                for t in state.tool_definitions
            ],
            "tool_execution_state": state.tool_execution_state,
            "known_files": state.known_files,
            "latest_tool_outputs": state.latest_tool_outputs,
            "unresolved_errors": state.unresolved_errors,
            "current_subgoal": state.current_subgoal,
            "version": state.version,
            "updated_at": state.updated_at,
        }
        raw_json = json.dumps(data, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
        snap_id = f"snap_{state.agent_id}_{state.version}_{digest[:8]}"

        return cls(
            snapshot_id=snap_id,
            agent_id=state.agent_id,
            version=state.version,
            payload_json=raw_json,
            digest=digest,
            created_at=time.time(),
        )

    def restore_agent_state(self) -> AgentState:
        """Reconstitute AgentState from serialized snapshot."""
        data = json.loads(self.payload_json)
        tools = [
            NormalizedToolDefinition(
                name=t["name"],
                description=t.get("description", ""),
                parameters=t.get("parameters", {}),
            )
            for t in data.get("tool_definitions", [])
        ]
        return AgentState(
            agent_id=data["agent_id"],
            objective=data.get("objective", ""),
            constraints=data.get("constraints", []),
            important_decisions=data.get("important_decisions", []),
            tool_definitions=tools,
            tool_execution_state=data.get("tool_execution_state", {}),
            known_files=data.get("known_files", {}),
            latest_tool_outputs=data.get("latest_tool_outputs", {}),
            unresolved_errors=data.get("unresolved_errors", []),
            current_subgoal=data.get("current_subgoal"),
            version=data.get("version", 1),
            updated_at=data.get("updated_at", time.time()),
        )
