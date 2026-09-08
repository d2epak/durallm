"""SQLite/WAL implementation of the gateway's durable-state boundaries.

SQLite is deliberately the first durable backend: it is local, auditable and
has no extra service dependency.  Its single-writer model is not a cluster
backend; callers needing multi-host deployment should implement the contracts
in :mod:`durallm.storage.contracts` against a shared transactional
store with the same lease and indeterminate-operation semantics.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

from durallm.agent.idempotency import ToolExecutionRecord, ToolExecutionStatus
from durallm.storage.contracts import Lease, StoredAttempt, StoredOperation, StoredSession


class DurableStoreError(RuntimeError):
    """Raised when a durable-state transition would violate its safety contract."""


class LeaseUnavailableError(DurableStoreError):
    """Raised when another live owner holds an operation or session lease."""


class SQLitePersistenceStore:
    """Thread-safe SQLite persistence with WAL, write-ahead attempts and leases.

    The older breaker and tool receipt methods remain available for backward
    compatibility.  New code should use the explicit session, attempt and
    operation methods below, which make every safety transition durable before
    the associated external action is permitted.
    """

    def __init__(self, db_path: str = ":memory:", timeout_seconds: float = 30.0):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=timeout_seconds, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def close(self) -> None:
        """Close the database connection after all gateway workers have stopped."""
        with self._lock:
            self._conn.close()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = 5000")
            # In-memory databases cannot use WAL, but retain the exact same
            # transition semantics for deterministic unit tests.
            if self.db_path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = FULL")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS circuit_breakers (
                    breaker_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    failure_rate REAL NOT NULL,
                    slow_call_rate REAL NOT NULL,
                    last_transition_time REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tool_executions (
                    tool_call_id TEXT PRIMARY KEY,
                    logical_operation_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_hash TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    execution_receipt_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tool_op_hash
                    ON tool_executions(logical_operation_id, tool_name, arguments_hash);

                CREATE TABLE IF NOT EXISTS durable_sessions (
                    session_id TEXT PRIMARY KEY,
                    protocol_version TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS durable_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_durable_attempt_request
                    ON durable_attempts(request_id, created_at);

                CREATE TABLE IF NOT EXISTS durable_operations (
                    operation_key TEXT PRIMARY KEY,
                    logical_operation_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_hash TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    execution_receipt_json TEXT,
                    error_message TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(logical_operation_id, tool_name, arguments_hash)
                );
                CREATE TABLE IF NOT EXISTS durable_operation_calls (
                    tool_call_id TEXT PRIMARY KEY,
                    operation_key TEXT NOT NULL REFERENCES durable_operations(operation_key),
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS durable_leases (
                    resource TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS provider_quota_lockouts (
                    provider_id TEXT NOT NULL,
                    pool TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    reason TEXT,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (provider_id, pool, route_id)
                );
                CREATE INDEX IF NOT EXISTS idx_quota_expires ON provider_quota_lockouts(expires_at);
                """
            )

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        """Hold a local lock and SQLite's cross-process write lock atomically."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _json(value: Dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _operation_key(logical_operation_id: str, tool_name: str, arguments_hash: str) -> str:
        value = f"{logical_operation_id}\x1f{tool_name}\x1f{arguments_hash}".encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    # ------------------------------------------------------------------
    # Legacy breaker and receipt persistence API
    # ------------------------------------------------------------------
    def save_breaker_state(
        self,
        breaker_id: str,
        state: str,
        failure_rate: float,
        slow_call_rate: float,
        last_transition_time: float,
    ) -> None:
        with self._write_transaction():
            self._conn.execute(
                """
                INSERT INTO circuit_breakers (breaker_id, state, failure_rate, slow_call_rate, last_transition_time, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(breaker_id) DO UPDATE SET
                    state = excluded.state,
                    failure_rate = excluded.failure_rate,
                    slow_call_rate = excluded.slow_call_rate,
                    last_transition_time = excluded.last_transition_time,
                    updated_at = excluded.updated_at
                """,
                (breaker_id, state, failure_rate, slow_call_rate, last_transition_time, time.time()),
            )

    def load_breaker_state(self, breaker_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM circuit_breakers WHERE breaker_id = ?", (breaker_id,)).fetchone()
        return dict(row) if row else None

    def save_tool_execution(self, record: ToolExecutionRecord) -> None:
        with self._write_transaction():
            self._conn.execute(
                """
                INSERT INTO tool_executions (
                    tool_call_id, logical_operation_id, tool_name, arguments_hash,
                    arguments_json, status, execution_receipt_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tool_call_id) DO UPDATE SET
                    status = excluded.status,
                    execution_receipt_json = excluded.execution_receipt_json,
                    updated_at = excluded.updated_at
                """,
                (
                    record.tool_call_id,
                    record.logical_operation_id,
                    record.tool_name,
                    record.arguments_hash,
                    self._json(record.arguments),
                    record.status.value,
                    self._json(record.execution_receipt) if record.execution_receipt else None,
                    record.created_at,
                    record.updated_at,
                ),
            )

    def check_tool_receipt(
        self, logical_operation_id: str, tool_name: str, arguments_hash: str
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT execution_receipt_json FROM tool_executions
                WHERE logical_operation_id = ? AND tool_name = ? AND arguments_hash = ? AND status = ?
                """,
                (logical_operation_id, tool_name, arguments_hash, ToolExecutionStatus.COMMITTED.value),
            ).fetchone()
        return (bool(row and row["execution_receipt_json"]), json.loads(row["execution_receipt_json"]) if row and row["execution_receipt_json"] else None)

    # ------------------------------------------------------------------
    # Provider Quota Lockouts (Tier 3 Daily Quota Persistence)
    # ------------------------------------------------------------------
    def record_quota_lockout(
        self,
        provider_id: str,
        pool: str,
        route_id: str,
        expires_at: float,
        reason: Optional[str] = None,
    ) -> None:
        """Persist a Tier 3 daily quota lockout (24h) for a route or provider."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO provider_quota_lockouts
                    (provider_id, pool, route_id, expires_at, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_id, pool, route_id) DO UPDATE SET
                    expires_at = excluded.expires_at,
                    reason = excluded.reason,
                    created_at = excluded.created_at
                """,
                (provider_id.lower(), pool.lower(), route_id.lower(), expires_at, reason or "", now),
            )

    def get_active_quota_lockouts(self, pool: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieve all currently active (unexpired) quota lockouts."""
        now = time.time()
        with self._lock:
            if pool:
                cur = self._conn.execute(
                    "SELECT provider_id, pool, route_id, expires_at, reason FROM provider_quota_lockouts WHERE pool = ? AND expires_at > ?",
                    (pool.lower(), now),
                )
            else:
                cur = self._conn.execute(
                    "SELECT provider_id, pool, route_id, expires_at, reason FROM provider_quota_lockouts WHERE expires_at > ?",
                    (now,),
                )
            return [
                {
                    "provider_id": row["provider_id"],
                    "pool": row["pool"],
                    "route_id": row["route_id"],
                    "expires_at": float(row["expires_at"]),
                    "reason": row["reason"],
                }
                for row in cur.fetchall()
            ]

    def purge_expired_quota_lockouts(self) -> int:
        """Delete expired quota lockouts from database."""
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM provider_quota_lockouts WHERE expires_at <= ?",
                (now,),
            )
            return cur.rowcount

    # ------------------------------------------------------------------
    # SessionStore: revisioned payloads for durable continuation state
    # ------------------------------------------------------------------
    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> StoredSession:
        return StoredSession(
            session_id=row["session_id"],
            protocol_version=row["protocol_version"],
            payload=json.loads(row["payload_json"]),
            revision=row["revision"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def load_session(self, session_id: str) -> Optional[StoredSession]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM durable_sessions WHERE session_id = ?", (session_id,)).fetchone()
        return self._session_from_row(row) if row else None

    def create_session(self, session_id: str, protocol_version: str, payload: Dict[str, Any]) -> Optional[StoredSession]:
        now = time.time()
        with self._write_transaction():
            try:
                self._conn.execute(
                    """INSERT INTO durable_sessions
                       (session_id, protocol_version, payload_json, revision, created_at, updated_at)
                       VALUES (?, ?, ?, 1, ?, ?)""",
                    (session_id, protocol_version, self._json(payload), now, now),
                )
            except sqlite3.IntegrityError:
                return None
            row = self._conn.execute("SELECT * FROM durable_sessions WHERE session_id = ?", (session_id,)).fetchone()
        return self._session_from_row(row) if row else None

    def save_session(
        self, session_id: str, protocol_version: str, payload: Dict[str, Any], expected_revision: int
    ) -> Optional[StoredSession]:
        now = time.time()
        with self._write_transaction():
            cur = self._conn.execute(
                """UPDATE durable_sessions
                   SET protocol_version = ?, payload_json = ?, revision = revision + 1, updated_at = ?
                   WHERE session_id = ? AND revision = ?""",
                (protocol_version, self._json(payload), now, session_id, expected_revision),
            )
            if cur.rowcount != 1:
                return None
            row = self._conn.execute("SELECT * FROM durable_sessions WHERE session_id = ?", (session_id,)).fetchone()
        return self._session_from_row(row) if row else None

    # ------------------------------------------------------------------
    # AttemptStore: write intent before adapter.execute() is called
    # ------------------------------------------------------------------
    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> StoredAttempt:
        return StoredAttempt(
            attempt_id=row["attempt_id"],
            request_id=row["request_id"],
            status=row["status"],
            payload=json.loads(row["payload_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def prepare_attempt(self, attempt_id: str, request_id: str, payload: Dict[str, Any]) -> StoredAttempt:
        now = time.time()
        with self._write_transaction():
            self._conn.execute(
                """INSERT OR IGNORE INTO durable_attempts
                   (attempt_id, request_id, status, payload_json, created_at, updated_at)
                   VALUES (?, ?, 'prepared', ?, ?, ?)""",
                (attempt_id, request_id, self._json(payload), now, now),
            )
            row = self._conn.execute("SELECT * FROM durable_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise DurableStoreError("Attempt intent was not persisted")
        return self._attempt_from_row(row)

    def load_attempt(self, attempt_id: str) -> Optional[StoredAttempt]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM durable_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        return self._attempt_from_row(row) if row else None

    def attempts_for_request(self, request_id: str) -> List[StoredAttempt]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM durable_attempts WHERE request_id = ? ORDER BY created_at, attempt_id", (request_id,)
            ).fetchall()
        return [self._attempt_from_row(row) for row in rows]

    def finish_attempt(self, attempt_id: str, status: str, payload: Dict[str, Any]) -> StoredAttempt:
        now = time.time()
        with self._write_transaction():
            cur = self._conn.execute(
                "UPDATE durable_attempts SET status = ?, payload_json = ?, updated_at = ? WHERE attempt_id = ?",
                (status, self._json(payload), now, attempt_id),
            )
            if cur.rowcount != 1:
                raise DurableStoreError(f"Unknown durable attempt: {attempt_id}")
            row = self._conn.execute("SELECT * FROM durable_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise DurableStoreError(f"Unknown durable attempt: {attempt_id}")
        return self._attempt_from_row(row)

    # ------------------------------------------------------------------
    # OperationStore: durable receipts plus fenced expiring leases
    # ------------------------------------------------------------------
    @staticmethod
    def _operation_from_row(row: sqlite3.Row) -> StoredOperation:
        receipt = row["execution_receipt_json"]
        return StoredOperation(
            operation_key=row["operation_key"],
            logical_operation_id=row["logical_operation_id"],
            tool_name=row["tool_name"],
            arguments_hash=row["arguments_hash"],
            arguments=json.loads(row["arguments_json"]),
            status=row["status"],
            execution_receipt=json.loads(receipt) if receipt else None,
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _operation_for_call_locked(self, tool_call_id: str) -> Optional[StoredOperation]:
        row = self._conn.execute(
            """SELECT o.* FROM durable_operations o
               JOIN durable_operation_calls c ON c.operation_key = o.operation_key
               WHERE c.tool_call_id = ?""",
            (tool_call_id,),
        ).fetchone()
        return self._operation_from_row(row) if row else None

    def prepare_operation(
        self,
        tool_call_id: str,
        logical_operation_id: str,
        tool_name: str,
        arguments_hash: str,
        arguments: Dict[str, Any],
    ) -> StoredOperation:
        operation_key = self._operation_key(logical_operation_id, tool_name, arguments_hash)
        now = time.time()
        with self._write_transaction():
            self._conn.execute(
                """INSERT OR IGNORE INTO durable_operations (
                    operation_key, logical_operation_id, tool_name, arguments_hash, arguments_json,
                    status, execution_receipt_json, error_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'proposed', NULL, NULL, ?, ?)""",
                (operation_key, logical_operation_id, tool_name, arguments_hash, self._json(arguments), now, now),
            )
            existing_call = self._conn.execute(
                "SELECT operation_key FROM durable_operation_calls WHERE tool_call_id = ?", (tool_call_id,)
            ).fetchone()
            if existing_call is not None and existing_call["operation_key"] != operation_key:
                raise DurableStoreError(f"Tool call id is already bound to another operation: {tool_call_id}")
            self._conn.execute(
                "INSERT OR IGNORE INTO durable_operation_calls (tool_call_id, operation_key, created_at) VALUES (?, ?, ?)",
                (tool_call_id, operation_key, now),
            )
            row = self._conn.execute("SELECT * FROM durable_operations WHERE operation_key = ?", (operation_key,)).fetchone()
        if row is None:
            raise DurableStoreError("Operation intent was not persisted")
        return self._operation_from_row(row)

    def operation_for_call(self, tool_call_id: str) -> Optional[StoredOperation]:
        with self._lock:
            return self._operation_for_call_locked(tool_call_id)

    def operation_for_key(
        self, logical_operation_id: str, tool_name: str, arguments_hash: str
    ) -> Optional[StoredOperation]:
        operation_key = self._operation_key(logical_operation_id, tool_name, arguments_hash)
        with self._lock:
            row = self._conn.execute("SELECT * FROM durable_operations WHERE operation_key = ?", (operation_key,)).fetchone()
        return self._operation_from_row(row) if row else None

    def _acquire_lease_locked(self, resource: str, owner_id: str, ttl_seconds: float, now: float) -> Optional[Lease]:
        ttl = max(0.001, ttl_seconds)
        expires_at = now + ttl
        row = self._conn.execute("SELECT * FROM durable_leases WHERE resource = ?", (resource,)).fetchone()
        if row is None:
            token = 1
            self._conn.execute(
                "INSERT INTO durable_leases (resource, owner_id, fencing_token, expires_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (resource, owner_id, token, expires_at, now),
            )
            return Lease(resource, owner_id, token, expires_at)
        if row["owner_id"] == owner_id:
            self._conn.execute(
                "UPDATE durable_leases SET expires_at = ?, updated_at = ? WHERE resource = ?",
                (expires_at, now, resource),
            )
            return Lease(resource, owner_id, row["fencing_token"], expires_at)
        if row["expires_at"] <= now:
            token = row["fencing_token"] + 1
            self._conn.execute(
                """UPDATE durable_leases SET owner_id = ?, fencing_token = ?, expires_at = ?, updated_at = ?
                   WHERE resource = ?""",
                (owner_id, token, expires_at, now, resource),
            )
            return Lease(resource, owner_id, token, expires_at)
        return None

    def acquire_lease(self, resource: str, owner_id: str, ttl_seconds: float) -> Optional[Lease]:
        with self._write_transaction():
            return self._acquire_lease_locked(resource, owner_id, ttl_seconds, time.time())

    def release_lease(self, resource: str, owner_id: str, fencing_token: int) -> bool:
        with self._write_transaction():
            cur = self._conn.execute(
                "DELETE FROM durable_leases WHERE resource = ? AND owner_id = ? AND fencing_token = ?",
                (resource, owner_id, fencing_token),
            )
            return cur.rowcount == 1

    def _assert_lease_locked(self, resource: str, owner_id: str, fencing_token: int, now: float) -> None:
        row = self._conn.execute("SELECT * FROM durable_leases WHERE resource = ?", (resource,)).fetchone()
        if row is None or (row["owner_id"], row["fencing_token"]) != (owner_id, fencing_token) or row["expires_at"] <= now:
            raise LeaseUnavailableError(f"No live lease for {resource}")

    def mark_operation_validated(self, tool_call_id: str) -> StoredOperation:
        with self._write_transaction():
            op = self._operation_for_call_locked(tool_call_id)
            if op is None:
                raise DurableStoreError(f"Unknown tool call: {tool_call_id}")
            if op.status == ToolExecutionStatus.PROPOSED.value:
                self._conn.execute(
                    "UPDATE durable_operations SET status = ?, updated_at = ? WHERE operation_key = ?",
                    (ToolExecutionStatus.VALIDATED.value, time.time(), op.operation_key),
                )
            row = self._conn.execute("SELECT * FROM durable_operations WHERE operation_key = ?", (op.operation_key,)).fetchone()
        return self._operation_from_row(row)

    def mark_operation_submitted(self, tool_call_id: str, owner_id: str, lease_ttl_seconds: float) -> Lease:
        with self._write_transaction():
            op = self._operation_for_call_locked(tool_call_id)
            if op is None:
                raise DurableStoreError(f"Unknown tool call: {tool_call_id}")
            if op.status not in (ToolExecutionStatus.PROPOSED.value, ToolExecutionStatus.VALIDATED.value):
                raise DurableStoreError(f"Operation cannot be submitted from state {op.status}")
            now = time.time()
            lease = self._acquire_lease_locked(f"operation:{op.operation_key}", owner_id, lease_ttl_seconds, now)
            if lease is None:
                raise LeaseUnavailableError(f"Operation is owned by another live worker: {op.operation_key}")
            self._conn.execute(
                "UPDATE durable_operations SET status = ?, updated_at = ? WHERE operation_key = ?",
                (ToolExecutionStatus.SUBMITTED.value, now, op.operation_key),
            )
            return lease

    def mark_operation_committed(
        self, tool_call_id: str, owner_id: str, fencing_token: int, receipt: Dict[str, Any]
    ) -> StoredOperation:
        with self._write_transaction():
            op = self._operation_for_call_locked(tool_call_id)
            if op is None:
                raise DurableStoreError(f"Unknown tool call: {tool_call_id}")
            resource = f"operation:{op.operation_key}"
            self._assert_lease_locked(resource, owner_id, fencing_token, time.time())
            if op.status != ToolExecutionStatus.SUBMITTED.value:
                raise DurableStoreError(f"Operation cannot commit from state {op.status}")
            now = time.time()
            self._conn.execute(
                """UPDATE durable_operations
                   SET status = ?, execution_receipt_json = ?, error_message = NULL, updated_at = ?
                   WHERE operation_key = ?""",
                (ToolExecutionStatus.COMMITTED.value, self._json(receipt), now, op.operation_key),
            )
            self._conn.execute(
                "DELETE FROM durable_leases WHERE resource = ? AND owner_id = ? AND fencing_token = ?",
                (resource, owner_id, fencing_token),
            )
            row = self._conn.execute("SELECT * FROM durable_operations WHERE operation_key = ?", (op.operation_key,)).fetchone()
        return self._operation_from_row(row)

    def mark_operation_indeterminate(self, tool_call_id: str, reason: str) -> StoredOperation:
        with self._write_transaction():
            op = self._operation_for_call_locked(tool_call_id)
            if op is None:
                raise DurableStoreError(f"Unknown tool call: {tool_call_id}")
            if op.status != ToolExecutionStatus.COMMITTED.value:
                self._conn.execute(
                    """UPDATE durable_operations SET status = ?, error_message = ?, updated_at = ?
                       WHERE operation_key = ?""",
                    (ToolExecutionStatus.INDETERMINATE.value, reason, time.time(), op.operation_key),
                )
                self._conn.execute("DELETE FROM durable_leases WHERE resource = ?", (f"operation:{op.operation_key}",))
            row = self._conn.execute("SELECT * FROM durable_operations WHERE operation_key = ?", (op.operation_key,)).fetchone()
        return self._operation_from_row(row)

    def mark_operation_failed(self, tool_call_id: str, reason: str) -> StoredOperation:
        with self._write_transaction():
            op = self._operation_for_call_locked(tool_call_id)
            if op is None:
                raise DurableStoreError(f"Unknown tool call: {tool_call_id}")
            if op.status not in (ToolExecutionStatus.COMMITTED.value, ToolExecutionStatus.SUBMITTED.value):
                self._conn.execute(
                    "UPDATE durable_operations SET status = ?, error_message = ?, updated_at = ? WHERE operation_key = ?",
                    (ToolExecutionStatus.FAILED.value, reason, time.time(), op.operation_key),
                )
            row = self._conn.execute("SELECT * FROM durable_operations WHERE operation_key = ?", (op.operation_key,)).fetchone()
        return self._operation_from_row(row)

    def recover_inflight_operations(self, reason: str = "Gateway restarted before tool acknowledgement") -> int:
        """Fail closed after restart: submitted work is indeterminate, never replayable."""
        with self._write_transaction():
            cur = self._conn.execute(
                """UPDATE durable_operations SET status = ?, error_message = ?, updated_at = ?
                   WHERE status = ?""",
                (ToolExecutionStatus.INDETERMINATE.value, reason, time.time(), ToolExecutionStatus.SUBMITTED.value),
            )
            self._conn.execute(
                """DELETE FROM durable_leases
                   WHERE resource IN (SELECT 'operation:' || operation_key FROM durable_operations
                                      WHERE status = ?)""",
                (ToolExecutionStatus.INDETERMINATE.value,),
            )
            return cur.rowcount

    @staticmethod
    def operation_requires_manual_resolution(operation: StoredOperation) -> bool:
        return operation.status in (
            ToolExecutionStatus.SUBMITTED.value,
            ToolExecutionStatus.INDETERMINATE.value,
            ToolExecutionStatus.AMBIGUOUS.value,
        )
