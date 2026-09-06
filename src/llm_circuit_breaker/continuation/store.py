"""In-memory ACP session store.

It is intentionally process-local. The durable repository implementation is
introduced separately so this module cannot be mistaken for crash recovery.
"""

from __future__ import annotations

import re
import threading
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Protocol

from llm_circuit_breaker.continuation.models import (
    ACP_VERSION,
    Checkpoint,
    ContinuationEvent,
    ContinuationRequest,
    ContinuationTurn,
    canonical_digest,
    new_session_id,
    new_turn_id,
)
from llm_circuit_breaker.errors import ContinuationProtocolError
from llm_circuit_breaker.protocol.ir import NormalizedRequest, NormalizedResponse

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class ContinuationStore(Protocol):
    """Storage boundary; a durable implementation must preserve these transitions."""

    def begin_turn(self, request: ContinuationRequest) -> ContinuationTurn:
        ...

    def complete_turn(
        self, turn: ContinuationTurn, request: NormalizedRequest, response: NormalizedResponse, selected_endpoint: str
    ) -> ContinuationEvent:
        ...

    def acknowledge(self, session_id: str, turn_id: str, epoch: int, checkpoint_digest: str) -> ContinuationEvent:
        ...

    def interrupt_turn(self, turn: ContinuationTurn, detail: str) -> ContinuationEvent:
        ...


@dataclass
class _Session:
    session_id: str
    protocol_version: str
    next_epoch: int = 0
    active_turn_id: Optional[str] = None
    active_epoch: Optional[int] = None
    latest_checkpoint: Optional[Checkpoint] = None
    latest_checkpoint_acknowledged: bool = True
    events: List[ContinuationEvent] = field(default_factory=list)


class InMemoryContinuationStore:
    """Thread-safe ACP reference store for one gateway process only."""

    durable = False

    def __init__(self) -> None:
        self._sessions: Dict[str, _Session] = {}
        self._lock = threading.RLock()

    def begin_turn(self, request: ContinuationRequest) -> ContinuationTurn:
        self._validate_request(request)
        with self._lock:
            session_id = request.session_id or new_session_id()
            session = self._sessions.get(session_id)
            if session is None:
                if request.parent_checkpoint_digest:
                    raise ContinuationProtocolError("A new ACP session cannot specify a parent checkpoint", status_code=400)
                session = _Session(session_id=session_id, protocol_version=request.protocol_version)
                self._sessions[session_id] = session
            elif session.protocol_version != request.protocol_version:
                raise ContinuationProtocolError("ACP protocol version does not match the existing session", status_code=409)

            if session.active_turn_id:
                raise ContinuationProtocolError("An ACP turn is already active for this session", status_code=409)
            latest = session.latest_checkpoint
            if latest is not None:
                if not session.latest_checkpoint_acknowledged:
                    raise ContinuationProtocolError("Previous ACP checkpoint must be acknowledged before the next turn", status_code=409)
                if request.parent_checkpoint_digest != latest.gateway_digest:
                    raise ContinuationProtocolError("ACP parent checkpoint digest is stale or missing", status_code=409)

            turn = ContinuationTurn(
                session_id=session_id,
                turn_id=new_turn_id(),
                epoch=session.next_epoch,
                parent_checkpoint_digest=latest.gateway_digest if latest else None,
                client_state_digest=request.state_digest,
                protocol_version=request.protocol_version,
            )
            session.active_turn_id = turn.turn_id
            session.active_epoch = turn.epoch
            session.events.append(self._event("turn_started", turn))
            return turn

    def complete_turn(
        self, turn: ContinuationTurn, request: NormalizedRequest, response: NormalizedResponse, selected_endpoint: str
    ) -> ContinuationEvent:
        with self._lock:
            session = self._active_session(turn)
            # ``asdict`` recursively projects nested tool calls/results.  A
            # shallow ``__dict__`` projection would fail as soon as a request
            # contains an assistant tool call, exactly the workflow ACP must
            # preserve across a continuation boundary.
            request_digest = canonical_digest(asdict(request))
            response_record = asdict(response)
            response_record["selected_endpoint"] = selected_endpoint
            response_digest = canonical_digest(response_record)
            checkpoint_data = {
                "protocol_version": turn.protocol_version,
                "session_id": turn.session_id,
                "turn_id": turn.turn_id,
                "epoch": turn.epoch,
                "parent_checkpoint_digest": turn.parent_checkpoint_digest,
                "request_digest": request_digest,
                "client_state_digest": turn.client_state_digest,
                "response_digest": response_digest,
                "selected_endpoint": selected_endpoint,
            }
            gateway_digest = canonical_digest(checkpoint_data)
            checkpoint = Checkpoint(
                checkpoint_id=f"acp_ckpt_{gateway_digest[:24]}",
                session_id=turn.session_id,
                turn_id=turn.turn_id,
                epoch=turn.epoch,
                parent_checkpoint_digest=turn.parent_checkpoint_digest,
                request_digest=request_digest,
                client_state_digest=turn.client_state_digest,
                response_digest=response_digest,
                gateway_digest=gateway_digest,
                created_at=self._now(),
            )
            session.active_turn_id = None
            session.active_epoch = None
            session.next_epoch = turn.epoch + 1
            session.latest_checkpoint = checkpoint
            session.latest_checkpoint_acknowledged = False
            event = self._event("turn_completed", turn, checkpoint=checkpoint, ack_required=True)
            session.events.append(event)
            return event

    def acknowledge(self, session_id: str, turn_id: str, epoch: int, checkpoint_digest: str) -> ContinuationEvent:
        self._validate_identifier("session_id", session_id)
        self._validate_identifier("turn_id", turn_id)
        self._validate_digest("checkpoint_digest", checkpoint_digest)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.latest_checkpoint is None:
                raise ContinuationProtocolError("ACP session has no checkpoint to acknowledge", status_code=404)
            checkpoint = session.latest_checkpoint
            if (checkpoint.turn_id, checkpoint.epoch, checkpoint.gateway_digest) != (turn_id, epoch, checkpoint_digest):
                raise ContinuationProtocolError("ACP acknowledgement does not match the latest checkpoint", status_code=409)
            if session.latest_checkpoint_acknowledged:
                raise ContinuationProtocolError("ACP checkpoint was already acknowledged", status_code=409)
            session.latest_checkpoint_acknowledged = True
            turn = ContinuationTurn(
                session_id=session_id,
                turn_id=turn_id,
                epoch=epoch,
                parent_checkpoint_digest=checkpoint.parent_checkpoint_digest,
                client_state_digest=checkpoint.client_state_digest,
                protocol_version=session.protocol_version,
            )
            event = self._event("checkpoint_acknowledged", turn, checkpoint=checkpoint)
            session.events.append(event)
            return event

    def interrupt_turn(self, turn: ContinuationTurn, detail: str) -> ContinuationEvent:
        with self._lock:
            session = self._active_session(turn)
            session.active_turn_id = None
            session.active_epoch = None
            event = self._event("turn_interrupted", turn, detail=detail)
            session.events.append(event)
            return event

    def _active_session(self, turn: ContinuationTurn) -> _Session:
        session = self._sessions.get(turn.session_id)
        if session is None or (session.active_turn_id, session.active_epoch) != (turn.turn_id, turn.epoch):
            raise ContinuationProtocolError("ACP turn is no longer active", status_code=409)
        return session

    @staticmethod
    def _event(
        event_type: str,
        turn: ContinuationTurn,
        checkpoint: Optional[Checkpoint] = None,
        ack_required: bool = False,
        detail: Optional[str] = None,
    ) -> ContinuationEvent:
        return ContinuationEvent(
            event_id=f"acp_evt_{new_turn_id()[9:]}",
            event_type=event_type,
            session_id=turn.session_id,
            turn_id=turn.turn_id,
            epoch=turn.epoch,
            protocol_version=turn.protocol_version,
            checkpoint=checkpoint,
            ack_required=ack_required,
            detail=detail,
        )

    @staticmethod
    def _now() -> float:
        import time

        return time.time()

    @staticmethod
    def _validate_request(request: ContinuationRequest) -> None:
        if request.protocol_version != ACP_VERSION:
            raise ContinuationProtocolError(f"Unsupported ACP protocol version: {request.protocol_version}", status_code=400)
        if request.session_id:
            InMemoryContinuationStore._validate_identifier("session_id", request.session_id)
        InMemoryContinuationStore._validate_digest("state_digest", request.state_digest)
        if request.parent_checkpoint_digest:
            InMemoryContinuationStore._validate_digest("parent_checkpoint_digest", request.parent_checkpoint_digest)

    @staticmethod
    def _validate_identifier(name: str, value: str) -> None:
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ContinuationProtocolError(f"Invalid ACP {name}", status_code=400)

    @staticmethod
    def _validate_digest(name: str, value: str) -> None:
        if not _DIGEST_RE.fullmatch(value):
            raise ContinuationProtocolError(f"ACP {name} must be a lowercase SHA-256 digest", status_code=400)
