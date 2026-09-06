"""Durable-state contracts shared by local SQLite and future cluster backends.

The contracts deliberately model ownership and indeterminacy.  A caller must
never turn an incomplete external tool operation into a retry merely because a
process restarted or an acknowledgement was lost.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol


@dataclass(frozen=True)
class StoredSession:
    """Opaque, versioned continuation-session state owned by a SessionStore."""

    session_id: str
    protocol_version: str
    payload: Dict[str, Any]
    revision: int
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class StoredAttempt:
    """Durable attempt intent/result record for request-dispatch recovery."""

    attempt_id: str
    request_id: str
    status: str
    payload: Dict[str, Any]
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class StoredOperation:
    """One logical tool operation, keyed independently from provider call IDs."""

    operation_key: str
    logical_operation_id: str
    tool_name: str
    arguments_hash: str
    arguments: Dict[str, Any]
    status: str
    execution_receipt: Optional[Dict[str, Any]]
    error_message: Optional[str]
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class Lease:
    """Fenced, expiring ownership of a durable session or operation."""

    resource: str
    owner_id: str
    fencing_token: int
    expires_at: float


class SessionStore(Protocol):
    """Compare-and-swap session persistence boundary."""

    def load_session(self, session_id: str) -> Optional[StoredSession]:
        ...

    def create_session(self, session_id: str, protocol_version: str, payload: Dict[str, Any]) -> Optional[StoredSession]:
        ...

    def save_session(
        self, session_id: str, protocol_version: str, payload: Dict[str, Any], expected_revision: int
    ) -> Optional[StoredSession]:
        ...


class AttemptStore(Protocol):
    """Write-ahead persistence for provider dispatch attempts."""

    def prepare_attempt(self, attempt_id: str, request_id: str, payload: Dict[str, Any]) -> StoredAttempt:
        ...

    def load_attempt(self, attempt_id: str) -> Optional[StoredAttempt]:
        ...

    def attempts_for_request(self, request_id: str) -> List[StoredAttempt]:
        ...

    def finish_attempt(self, attempt_id: str, status: str, payload: Dict[str, Any]) -> StoredAttempt:
        ...


class OperationStore(Protocol):
    """Durable tool-operation and receipt persistence boundary."""

    def prepare_operation(
        self,
        tool_call_id: str,
        logical_operation_id: str,
        tool_name: str,
        arguments_hash: str,
        arguments: Dict[str, Any],
    ) -> StoredOperation:
        ...

    def operation_for_call(self, tool_call_id: str) -> Optional[StoredOperation]:
        ...

    def operation_for_key(
        self, logical_operation_id: str, tool_name: str, arguments_hash: str
    ) -> Optional[StoredOperation]:
        ...

    def acquire_lease(self, resource: str, owner_id: str, ttl_seconds: float) -> Optional[Lease]:
        ...

    def release_lease(self, resource: str, owner_id: str, fencing_token: int) -> bool:
        ...
