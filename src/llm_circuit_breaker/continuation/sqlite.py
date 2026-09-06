"""Durable SQLite/WAL implementation of the Agent Continuation Protocol store."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict
from typing import Any, Dict, Optional, Tuple

from llm_circuit_breaker.continuation.models import (
    Checkpoint,
    ContinuationEvent,
    ContinuationRequest,
    ContinuationTurn,
    canonical_digest,
    new_session_id,
    new_turn_id,
)
from llm_circuit_breaker.continuation.store import InMemoryContinuationStore
from llm_circuit_breaker.errors import ContinuationProtocolError
from llm_circuit_breaker.protocol.ir import NormalizedRequest, NormalizedResponse
from llm_circuit_breaker.storage.contracts import Lease, StoredSession
from llm_circuit_breaker.storage.sqlite import SQLitePersistenceStore


class SQLiteContinuationStore:
    """Crash-recoverable ACP state using revisioned sessions and short leases.

    A stale active turn after a process crash is discarded without a checkpoint;
    it can therefore be retried at the same epoch. This does not claim an
    upstream model request or external tool effect was undone.
    """

    durable = True

    def __init__(
        self,
        store: SQLitePersistenceStore,
        owner_id: Optional[str] = None,
        lease_ttl_seconds: float = 120.0,
    ) -> None:
        self.store = store
        self.owner_id = owner_id or f"acp_{uuid.uuid4().hex}"
        self.lease_ttl_seconds = lease_ttl_seconds
        # A session lease intentionally spans begin -> complete/interrupt. It
        # prevents a second request from mistaking a live in-flight model turn
        # for a restart merely because ``begin_turn`` returned to the caller.
        self._turn_leases: Dict[str, Lease] = {}

    @staticmethod
    def _empty_payload() -> Dict[str, Any]:
        return {
            "next_epoch": 0,
            "active_turn_id": None,
            "active_epoch": None,
            "latest_checkpoint": None,
            "latest_checkpoint_acknowledged": True,
        }

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

    def _acquire_session(self, session_id: str) -> Lease:
        lease = self.store.acquire_lease(f"session:{session_id}", self.owner_id, self.lease_ttl_seconds)
        if lease is None:
            raise ContinuationProtocolError("An ACP turn is already active for this session", status_code=409)
        return lease

    def _release_session(self, lease: Lease) -> None:
        self.store.release_lease(lease.resource, self.owner_id, lease.fencing_token)

    def _load_or_create(self, request: ContinuationRequest, session_id: str) -> StoredSession:
        session = self.store.load_session(session_id)
        if session is not None:
            return session
        if request.parent_checkpoint_digest:
            raise ContinuationProtocolError("A new ACP session cannot specify a parent checkpoint", status_code=400)
        created = self.store.create_session(session_id, request.protocol_version, self._empty_payload())
        if created is not None:
            return created
        session = self.store.load_session(session_id)
        if session is None:
            raise ContinuationProtocolError("Unable to create ACP session", status_code=503)
        return session

    @staticmethod
    def _checkpoint(payload: Dict[str, Any]) -> Optional[Checkpoint]:
        value = payload.get("latest_checkpoint")
        return Checkpoint.from_dict(value) if value else None

    def _save(self, session: StoredSession, payload: Dict[str, Any]) -> StoredSession:
        saved = self.store.save_session(session.session_id, session.protocol_version, payload, session.revision)
        if saved is None:
            raise ContinuationProtocolError("ACP session changed concurrently; retry the turn", status_code=409)
        return saved

    def begin_turn(self, request: ContinuationRequest) -> ContinuationTurn:
        InMemoryContinuationStore._validate_request(request)
        session_id = request.session_id or new_session_id()
        lease = self._acquire_session(session_id)
        retained = False
        try:
            session = self._load_or_create(request, session_id)
            if session.protocol_version != request.protocol_version:
                raise ContinuationProtocolError("ACP protocol version does not match the existing session", status_code=409)
            payload = dict(session.payload)

            # Holding the new lease proves the prior owner is gone. An active
            # record is consequently a crash/interruption, never permission to
            # advance an epoch without a completed checkpoint.
            if payload.get("active_turn_id"):
                if payload["active_turn_id"] in self._turn_leases:
                    # ``acquire_lease`` renews a lease held by the same store
                    # owner. Do not release that shared lease on this rejected
                    # concurrent begin, or it would orphan the live first turn.
                    retained = True
                    raise ContinuationProtocolError("An ACP turn is already active for this session", status_code=409)
                payload["active_turn_id"] = None
                payload["active_epoch"] = None
                session = self._save(session, payload)
                payload = dict(session.payload)

            checkpoint = self._checkpoint(payload)
            if checkpoint is not None:
                if not payload.get("latest_checkpoint_acknowledged", False):
                    raise ContinuationProtocolError("Previous ACP checkpoint must be acknowledged before the next turn", status_code=409)
                if request.parent_checkpoint_digest != checkpoint.gateway_digest:
                    raise ContinuationProtocolError("ACP parent checkpoint digest is stale or missing", status_code=409)

            turn = ContinuationTurn(
                session_id=session_id,
                turn_id=new_turn_id(),
                epoch=int(payload["next_epoch"]),
                parent_checkpoint_digest=checkpoint.gateway_digest if checkpoint else None,
                client_state_digest=request.state_digest,
                protocol_version=request.protocol_version,
            )
            payload["active_turn_id"] = turn.turn_id
            payload["active_epoch"] = turn.epoch
            self._save(session, payload)
            self._turn_leases[turn.turn_id] = lease
            retained = True
            return turn
        finally:
            if not retained:
                self._release_session(lease)

    def _active_session(self, turn: ContinuationTurn) -> Tuple[StoredSession, Dict[str, Any]]:
        session = self.store.load_session(turn.session_id)
        if session is None or session.protocol_version != turn.protocol_version:
            raise ContinuationProtocolError("ACP turn is no longer active", status_code=409)
        payload = dict(session.payload)
        if (payload.get("active_turn_id"), payload.get("active_epoch")) != (turn.turn_id, turn.epoch):
            raise ContinuationProtocolError("ACP turn is no longer active", status_code=409)
        return session, payload

    def complete_turn(
        self, turn: ContinuationTurn, request: NormalizedRequest, response: NormalizedResponse, selected_endpoint: str
    ) -> ContinuationEvent:
        lease = self._turn_leases.get(turn.turn_id)
        if lease is None:
            raise ContinuationProtocolError("ACP turn is no longer active", status_code=409)
        try:
            session, payload = self._active_session(turn)
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
            digest = canonical_digest(checkpoint_data)
            checkpoint = Checkpoint(
                checkpoint_id=f"acp_ckpt_{digest[:24]}",
                session_id=turn.session_id,
                turn_id=turn.turn_id,
                epoch=turn.epoch,
                parent_checkpoint_digest=turn.parent_checkpoint_digest,
                request_digest=request_digest,
                client_state_digest=turn.client_state_digest,
                response_digest=response_digest,
                gateway_digest=digest,
                created_at=time.time(),
            )
            payload.update(
                {
                    "active_turn_id": None,
                    "active_epoch": None,
                    "next_epoch": turn.epoch + 1,
                    "latest_checkpoint": checkpoint.to_dict(),
                    "latest_checkpoint_acknowledged": False,
                }
            )
            self._save(session, payload)
            return self._event("turn_completed", turn, checkpoint=checkpoint, ack_required=True)
        finally:
            self._turn_leases.pop(turn.turn_id, None)
            self._release_session(lease)

    def acknowledge(self, session_id: str, turn_id: str, epoch: int, checkpoint_digest: str) -> ContinuationEvent:
        InMemoryContinuationStore._validate_identifier("session_id", session_id)
        InMemoryContinuationStore._validate_identifier("turn_id", turn_id)
        InMemoryContinuationStore._validate_digest("checkpoint_digest", checkpoint_digest)
        lease = self._acquire_session(session_id)
        try:
            session = self.store.load_session(session_id)
            if session is None:
                raise ContinuationProtocolError("ACP session has no checkpoint to acknowledge", status_code=404)
            payload = dict(session.payload)
            checkpoint = self._checkpoint(payload)
            if checkpoint is None:
                raise ContinuationProtocolError("ACP session has no checkpoint to acknowledge", status_code=404)
            if (checkpoint.turn_id, checkpoint.epoch, checkpoint.gateway_digest) != (turn_id, epoch, checkpoint_digest):
                raise ContinuationProtocolError("ACP acknowledgement does not match the latest checkpoint", status_code=409)
            if payload.get("latest_checkpoint_acknowledged", False):
                raise ContinuationProtocolError("ACP checkpoint was already acknowledged", status_code=409)
            payload["latest_checkpoint_acknowledged"] = True
            self._save(session, payload)
            turn = ContinuationTurn(
                session_id=session_id,
                turn_id=turn_id,
                epoch=epoch,
                parent_checkpoint_digest=checkpoint.parent_checkpoint_digest,
                client_state_digest=checkpoint.client_state_digest,
                protocol_version=session.protocol_version,
            )
            return self._event("checkpoint_acknowledged", turn, checkpoint=checkpoint)
        finally:
            self._release_session(lease)

    def interrupt_turn(self, turn: ContinuationTurn, detail: str) -> ContinuationEvent:
        lease = self._turn_leases.get(turn.turn_id)
        if lease is None:
            raise ContinuationProtocolError("ACP turn is no longer active", status_code=409)
        try:
            session, payload = self._active_session(turn)
            payload["active_turn_id"] = None
            payload["active_epoch"] = None
            self._save(session, payload)
            return self._event("turn_interrupted", turn, detail=detail)
        finally:
            self._turn_leases.pop(turn.turn_id, None)
            self._release_session(lease)
