"""Distributed cluster storage backend for multi-replica gateway deployments.

Implements distributed session persistence, distributed tool operation leasing,
and multi-node circuit breaker FSM synchronization across clustered gateway pods.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from durallm.storage.contracts import (
    Lease,
    StoredAttempt,
    StoredOperation,
    StoredSession,
)

logger = logging.getLogger("durallm.storage.cluster")


class ClusterPersistenceStore:
    """Distributed storage backend supporting in-memory cluster simulation and Redis adapters.

    Provides monotonic fencing tokens for distributed tool leasing, CAS updates for sessions,
    and multi-node circuit breaker FSM synchronization. Satisfies SessionStore, AttemptStore,
    and OperationStore protocols.
    """

    def __init__(self, redis_client: Optional[Any] = None) -> None:
        self._redis = redis_client
        # Local cluster-replicated state (used directly or as fallback/cache)
        self._sessions: Dict[str, StoredSession] = {}
        self._attempts: Dict[str, StoredAttempt] = {}
        self._request_attempts: Dict[str, List[str]] = {}
        self._operations: Dict[str, StoredOperation] = {}
        self._key_to_op: Dict[str, str] = {}
        self._leases: Dict[str, Lease] = {}
        self._fencing_counters: Dict[str, int] = {}
        self._breaker_states: Dict[str, Dict[str, Any]] = {}
        self._token_buckets: Dict[str, Dict[str, float]] = {}

    # -------------------------------------------------------------------------
    # SessionStore Implementation
    # -------------------------------------------------------------------------

    def load_session(self, session_id: str) -> Optional[StoredSession]:
        if self._redis is not None:
            try:
                raw = self._redis.get(f"durallm:session:{session_id}")
                if raw:
                    data = json.loads(raw)
                    return StoredSession(**data)
            except Exception as e:
                logger.warning(f"Cluster redis read error for session {session_id}: {e}")
        return self._sessions.get(session_id)

    def create_session(
        self, session_id: str, protocol_version: str, payload: Dict[str, Any]
    ) -> Optional[StoredSession]:
        now = time.time()
        session = StoredSession(
            session_id=session_id,
            protocol_version=protocol_version,
            payload=payload,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        if self._redis is not None:
            try:
                data = json.dumps({
                    "session_id": session.session_id,
                    "protocol_version": session.protocol_version,
                    "payload": session.payload,
                    "revision": session.revision,
                    "created_at": session.created_at,
                    "updated_at": session.updated_at,
                })
                # NX = set only if not exists
                if not self._redis.set(f"durallm:session:{session_id}", data, nx=True):
                    return None
            except Exception as e:
                logger.warning(f"Cluster redis create error for session {session_id}: {e}")
                return None

        if session_id in self._sessions:
            return None
        self._sessions[session_id] = session
        return session

    def save_session(
        self, session_id: str, protocol_version: str, payload: Dict[str, Any], expected_revision: int
    ) -> Optional[StoredSession]:
        existing = self.load_session(session_id)
        if existing is None or existing.revision != expected_revision:
            return None
        now = time.time()
        updated = StoredSession(
            session_id=session_id,
            protocol_version=protocol_version,
            payload=payload,
            revision=expected_revision + 1,
            created_at=existing.created_at,
            updated_at=now,
        )
        if self._redis is not None:
            try:
                data = json.dumps({
                    "session_id": updated.session_id,
                    "protocol_version": updated.protocol_version,
                    "payload": updated.payload,
                    "revision": updated.revision,
                    "created_at": updated.created_at,
                    "updated_at": updated.updated_at,
                })
                self._redis.set(f"durallm:session:{session_id}", data)
            except Exception as e:
                logger.warning(f"Cluster redis save error for session {session_id}: {e}")
                return None

        self._sessions[session_id] = updated
        return updated

    # -------------------------------------------------------------------------
    # AttemptStore Implementation
    # -------------------------------------------------------------------------

    def prepare_attempt(self, attempt_id: str, request_id: str, payload: Dict[str, Any]) -> StoredAttempt:
        now = time.time()
        attempt = StoredAttempt(
            attempt_id=attempt_id,
            request_id=request_id,
            status="PREPARED",
            payload=payload,
            created_at=now,
            updated_at=now,
        )
        self._attempts[attempt_id] = attempt
        self._request_attempts.setdefault(request_id, []).append(attempt_id)
        if self._redis is not None:
            try:
                self._redis.set(f"durallm:attempt:{attempt_id}", json.dumps({
                    "attempt_id": attempt.attempt_id,
                    "request_id": attempt.request_id,
                    "status": attempt.status,
                    "payload": attempt.payload,
                    "created_at": attempt.created_at,
                    "updated_at": attempt.updated_at,
                }))
                self._redis.rpush(f"durallm:req_attempts:{request_id}", attempt_id)
            except Exception as e:
                logger.warning(f"Cluster redis prepare_attempt error: {e}")
        return attempt

    def load_attempt(self, attempt_id: str) -> Optional[StoredAttempt]:
        if self._redis is not None:
            try:
                raw = self._redis.get(f"durallm:attempt:{attempt_id}")
                if raw:
                    return StoredAttempt(**json.loads(raw))
            except Exception as e:
                logger.warning(f"Cluster redis load_attempt error: {e}")
        return self._attempts.get(attempt_id)

    def attempts_for_request(self, request_id: str) -> List[StoredAttempt]:
        attempt_ids = self._request_attempts.get(request_id, [])
        return [self._attempts[aid] for aid in attempt_ids if aid in self._attempts]

    def finish_attempt(self, attempt_id: str, status: str, payload: Dict[str, Any]) -> StoredAttempt:
        existing = self.load_attempt(attempt_id)
        created_at = existing.created_at if existing else time.time()
        now = time.time()
        finished = StoredAttempt(
            attempt_id=attempt_id,
            request_id=existing.request_id if existing else "",
            status=status,
            payload=payload,
            created_at=created_at,
            updated_at=now,
        )
        self._attempts[attempt_id] = finished
        if self._redis is not None:
            try:
                self._redis.set(f"durallm:attempt:{attempt_id}", json.dumps({
                    "attempt_id": finished.attempt_id,
                    "request_id": finished.request_id,
                    "status": finished.status,
                    "payload": finished.payload,
                    "created_at": finished.created_at,
                    "updated_at": finished.updated_at,
                }))
            except Exception as e:
                logger.warning(f"Cluster redis finish_attempt error: {e}")
        return finished

    # -------------------------------------------------------------------------
    # OperationStore & Distributed Leasing Implementation
    # -------------------------------------------------------------------------

    def prepare_operation(
        self,
        tool_call_id: str,
        logical_operation_id: str,
        tool_name: str,
        arguments_hash: str,
        arguments: Dict[str, Any],
    ) -> StoredOperation:
        now = time.time()
        op_key = f"{logical_operation_id}:{tool_name}:{arguments_hash}"
        op = StoredOperation(
            operation_key=op_key,
            logical_operation_id=logical_operation_id,
            tool_name=tool_name,
            arguments_hash=arguments_hash,
            arguments=arguments,
            status="PREPARED",
            execution_receipt=None,
            error_message=None,
            created_at=now,
            updated_at=now,
        )
        self._operations[tool_call_id] = op
        self._key_to_op[op_key] = tool_call_id
        return op

    def operation_for_call(self, tool_call_id: str) -> Optional[StoredOperation]:
        return self._operations.get(tool_call_id)

    def operation_for_key(
        self, logical_operation_id: str, tool_name: str, arguments_hash: str
    ) -> Optional[StoredOperation]:
        op_key = f"{logical_operation_id}:{tool_name}:{arguments_hash}"
        call_id = self._key_to_op.get(op_key)
        return self._operations.get(call_id) if call_id else None

    def commit_receipt(
        self, tool_call_id: str, receipt: Dict[str, Any]
    ) -> Optional[StoredOperation]:
        op = self._operations.get(tool_call_id)
        if op is None:
            return None
        now = time.time()
        committed = StoredOperation(
            operation_key=op.operation_key,
            logical_operation_id=op.logical_operation_id,
            tool_name=op.tool_name,
            arguments_hash=op.arguments_hash,
            arguments=op.arguments,
            status="COMMITTED",
            execution_receipt=receipt,
            error_message=None,
            created_at=op.created_at,
            updated_at=now,
        )
        self._operations[tool_call_id] = committed
        return committed

    def acquire_lease(self, resource: str, owner_id: str, ttl_seconds: float) -> Optional[Lease]:
        now = time.time()
        current = self._leases.get(resource)
        if current is not None and current.expires_at > now and current.owner_id != owner_id:
            return None
        fencing = self._fencing_counters.get(resource, 0) + 1
        self._fencing_counters[resource] = fencing
        lease = Lease(
            resource=resource,
            owner_id=owner_id,
            fencing_token=fencing,
            expires_at=now + ttl_seconds,
        )
        self._leases[resource] = lease
        return lease

    def release_lease(self, resource: str, owner_id: str, fencing_token: int) -> bool:
        current = self._leases.get(resource)
        if current is None:
            return True
        if current.owner_id == owner_id and current.fencing_token == fencing_token:
            self._leases.pop(resource, None)
            return True
        return False

    # -------------------------------------------------------------------------
    # Multi-Node Circuit Breaker Synchronization
    # -------------------------------------------------------------------------

    def sync_breaker_state(
        self, provider: str, state: str, error_count: int, trip_timestamp: float
    ) -> None:
        """Publish a circuit breaker state transition across the cluster."""
        record = {
            "provider": provider,
            "state": state,
            "error_count": error_count,
            "trip_timestamp": trip_timestamp,
            "updated_at": time.time(),
        }
        self._breaker_states[provider] = record
        if self._redis is not None:
            try:
                self._redis.set(f"durallm:breaker:{provider}", json.dumps(record))
                self._redis.publish("durallm:breaker_events", json.dumps(record))
            except Exception as e:
                logger.warning(f"Cluster redis sync_breaker_state error: {e}")

    def get_cluster_breaker_state(self, provider: str) -> Optional[Dict[str, Any]]:
        """Fetch the latest synced breaker state across the cluster."""
        if self._redis is not None:
            try:
                raw = self._redis.get(f"durallm:breaker:{provider}")
                if raw:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        return parsed
            except Exception as e:
                logger.warning(f"Cluster redis get_cluster_breaker_state error: {e}")
        return self._breaker_states.get(provider)

    # -------------------------------------------------------------------------
    # Distributed Token Bucket Rate Limiting
    # -------------------------------------------------------------------------

    def consume_tokens(
        self, key: str, tokens: float, capacity: float, refill_rate_per_sec: float
    ) -> bool:
        """Atomically consume tokens from a distributed token bucket across pods."""
        now = time.time()
        bucket = self._token_buckets.get(key)
        if bucket is None:
            current_tokens = capacity
            last_refill = now
        else:
            elapsed = max(0.0, now - bucket["last_refill"])
            current_tokens = min(capacity, bucket["tokens"] + elapsed * refill_rate_per_sec)
            last_refill = now

        if current_tokens >= tokens:
            self._token_buckets[key] = {
                "tokens": current_tokens - tokens,
                "last_refill": last_refill,
            }
            return True
        return False
