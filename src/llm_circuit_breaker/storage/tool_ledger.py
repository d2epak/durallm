"""Durable tool ledger backed by :class:`SQLitePersistenceStore`.

The ledger writes an operation intent before exposing a tool call, persists
``SUBMITTED`` and obtains a fenced lease before a client may dispatch the
external tool, and persists the receipt before it reports success. On process
recovery, an unacknowledged submission is ``INDETERMINATE`` and is deliberately
not executable through the ordinary replay path.
"""

from __future__ import annotations

import uuid
from typing import Dict, Optional, Tuple

from llm_circuit_breaker.agent.idempotency import ToolExecutionLedger, ToolExecutionRecord, ToolExecutionStatus
from llm_circuit_breaker.storage.contracts import Lease, StoredOperation
from llm_circuit_breaker.storage.sqlite import SQLitePersistenceStore


class SQLiteToolExecutionLedger(ToolExecutionLedger):
    """A ``ToolExecutionLedger`` whose safety transitions survive restarts."""

    def __init__(
        self,
        store: SQLitePersistenceStore,
        owner_id: Optional[str] = None,
        lease_ttl_seconds: float = 30.0,
        recover_inflight: bool = True,
    ) -> None:
        super().__init__(max_records=100_000, ttl_seconds=float("inf"))
        self.store = store
        self.owner_id = owner_id or f"ledger_{uuid.uuid4().hex}"
        self.lease_ttl_seconds = lease_ttl_seconds
        self._leases: Dict[str, Lease] = {}
        if recover_inflight:
            # A missing acknowledgement is never evidence that the side effect
            # did not happen. Recovery consequently refuses automatic replay.
            self.store.recover_inflight_operations()

    @staticmethod
    def _status(operation: StoredOperation) -> ToolExecutionStatus:
        return ToolExecutionStatus(operation.status)

    @staticmethod
    def _hydrate(record: ToolExecutionRecord, operation: StoredOperation) -> ToolExecutionRecord:
        record.status = SQLiteToolExecutionLedger._status(operation)
        record.execution_receipt = operation.execution_receipt
        record.error_message = operation.error_message
        record.updated_at = operation.updated_at
        return record

    def register_tool_call(
        self,
        tool_call_id: str,
        logical_operation_id: str,
        tool_name: str,
        arguments: Dict[str, object],
    ) -> ToolExecutionRecord:
        record = super().register_tool_call(tool_call_id, logical_operation_id, tool_name, arguments)
        operation = self.store.prepare_operation(
            tool_call_id=tool_call_id,
            logical_operation_id=logical_operation_id,
            tool_name=tool_name,
            arguments_hash=record.arguments_hash,
            arguments=record.arguments,
        )
        return self._hydrate(record, operation)

    def mark_validated(self, tool_call_id: str) -> None:
        record = self.get_record(tool_call_id)
        if record is None or record.status not in (ToolExecutionStatus.PROPOSED, ToolExecutionStatus.VALIDATED):
            return
        self.store.mark_operation_validated(tool_call_id)
        super().mark_validated(tool_call_id)

    def mark_submitted(self, tool_call_id: str) -> None:
        # The durable write and lease acquisition happen *before* the tool
        # runner is allowed to make an external side effect.
        lease = self.store.mark_operation_submitted(tool_call_id, self.owner_id, self.lease_ttl_seconds)
        self._leases[tool_call_id] = lease
        super().mark_submitted(tool_call_id)

    def mark_committed(self, tool_call_id: str, receipt: Dict[str, object]) -> None:
        lease = self._leases.get(tool_call_id)
        if lease is None:
            raise RuntimeError("A durable operation can commit only with its live submission lease")
        self.store.mark_operation_committed(tool_call_id, self.owner_id, lease.fencing_token, receipt)
        self._leases.pop(tool_call_id, None)
        super().mark_committed(tool_call_id, receipt)

    def mark_indeterminate(self, tool_call_id: str, reason: str = "") -> None:
        self.store.mark_operation_indeterminate(tool_call_id, reason)
        self._leases.pop(tool_call_id, None)
        super().mark_indeterminate(tool_call_id, reason)

    def mark_failed(self, tool_call_id: str, error_message: str = "") -> None:
        self.store.mark_operation_failed(tool_call_id, error_message)
        super().mark_failed(tool_call_id, error_message)

    def check_idempotency(
        self,
        logical_operation_id: str,
        tool_name: str,
        arguments: Dict[str, object],
    ) -> Tuple[bool, Optional[Dict[str, object]]]:
        arguments_hash = self.compute_arguments_hash(arguments)
        operation = self.store.operation_for_key(logical_operation_id, tool_name, arguments_hash)
        if operation and operation.status == ToolExecutionStatus.COMMITTED.value and operation.execution_receipt is not None:
            return True, operation.execution_receipt
        return False, None

    def has_indeterminate_operation(
        self,
        logical_operation_id: str,
        tool_name: str,
        arguments: Dict[str, object],
    ) -> bool:
        arguments_hash = self.compute_arguments_hash(arguments)
        operation = self.store.operation_for_key(logical_operation_id, tool_name, arguments_hash)
        return bool(operation and self.store.operation_requires_manual_resolution(operation))

    def get_record(self, tool_call_id: str) -> Optional[ToolExecutionRecord]:
        record = super().get_record(tool_call_id)
        if record is not None:
            return record
        operation = self.store.operation_for_call(tool_call_id)
        if operation is None:
            return None
        record = ToolExecutionRecord(
            tool_call_id=tool_call_id,
            logical_operation_id=operation.logical_operation_id,
            tool_name=operation.tool_name,
            arguments_hash=operation.arguments_hash,
            arguments=operation.arguments,
            status=self._status(operation),
            execution_receipt=operation.execution_receipt,
            created_at=operation.created_at,
            updated_at=operation.updated_at,
            error_message=operation.error_message,
        )
        self._records_by_call_id[tool_call_id] = record
        return record
