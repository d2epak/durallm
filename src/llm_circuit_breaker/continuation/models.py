"""Versioned, provider-neutral Agent Continuation Protocol (ACP) records.

ACP deliberately records what a cooperating client and the gateway can prove. It
does not claim to reconstruct an arbitrary agent's local filesystem or execute
tools on the client's behalf.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

ACP_VERSION = "lcb-acp/1"


def canonical_digest(value: Dict[str, Any]) -> str:
    """Return a stable SHA-256 digest for an ACP-visible record."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def new_session_id() -> str:
    return f"acp_sess_{uuid.uuid4().hex}"


def new_turn_id() -> str:
    return f"acp_turn_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class ContinuationRequest:
    """Client assertion accompanying one ACP-governed model turn."""

    state_digest: str
    session_id: Optional[str] = None
    parent_checkpoint_digest: Optional[str] = None
    protocol_version: str = ACP_VERSION


@dataclass(frozen=True)
class Checkpoint:
    """Immutable gateway checkpoint awaiting client acknowledgement."""

    checkpoint_id: str
    session_id: str
    turn_id: str
    epoch: int
    parent_checkpoint_digest: Optional[str]
    request_digest: str
    client_state_digest: str
    response_digest: str
    gateway_digest: str
    created_at: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "Checkpoint":
        """Rehydrate a checkpoint persisted by a durable SessionStore."""
        return cls(
            checkpoint_id=str(value["checkpoint_id"]),
            session_id=str(value["session_id"]),
            turn_id=str(value["turn_id"]),
            epoch=int(value["epoch"]),
            parent_checkpoint_digest=value.get("parent_checkpoint_digest"),
            request_digest=str(value["request_digest"]),
            client_state_digest=str(value["client_state_digest"]),
            response_digest=str(value["response_digest"]),
            gateway_digest=str(value["gateway_digest"]),
            created_at=float(value["created_at"]),
        )


@dataclass(frozen=True)
class ContinuationEvent:
    """Auditable lifecycle event returned to a cooperating ACP client."""

    event_id: str
    event_type: str
    session_id: str
    turn_id: str
    epoch: int
    protocol_version: str = ACP_VERSION
    checkpoint: Optional[Checkpoint] = None
    ack_required: bool = False
    detail: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        if self.checkpoint is not None:
            result["checkpoint"] = self.checkpoint.to_dict()
        return result


@dataclass(frozen=True)
class ContinuationTurn:
    """Capability returned after a session has admitted a new turn."""

    session_id: str
    turn_id: str
    epoch: int
    parent_checkpoint_digest: Optional[str]
    client_state_digest: str
    protocol_version: str = ACP_VERSION
