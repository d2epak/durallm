"""Unit tests for Tool Execution Idempotency and Deduplication Ledger."""

import unittest

from llm_circuit_breaker.agent.idempotency import (
    ToolExecutionLedger,
    ToolExecutionStatus,
)


class TestToolExecutionIdempotency(unittest.TestCase):
    def setUp(self):
        self.ledger = ToolExecutionLedger()

    def test_tool_call_lifecycle(self):
        rec = self.ledger.register_tool_call(
            tool_call_id="call_1",
            logical_operation_id="op_100",
            tool_name="write_file",
            arguments={"path": "/tmp/test.txt", "content": "hello"},
        )
        self.assertEqual(rec.status, ToolExecutionStatus.PROPOSED)

        self.ledger.mark_validated("call_1")
        self.assertEqual(self.ledger.get_record("call_1").status, ToolExecutionStatus.VALIDATED)

        self.ledger.mark_submitted("call_1")
        self.assertEqual(self.ledger.get_record("call_1").status, ToolExecutionStatus.SUBMITTED)

        receipt = {"bytes_written": 5, "checksum": "abc123"}
        self.ledger.mark_committed("call_1", receipt)
        self.assertEqual(self.ledger.get_record("call_1").status, ToolExecutionStatus.COMMITTED)
        self.assertEqual(self.ledger.get_record("call_1").execution_receipt, receipt)

    def test_idempotency_prevents_duplicate_side_effects_on_retry(self):
        # Scenario: Tool executes successfully on upstream A, but connection drops before response reaches agent.
        # Gateway initiates retry with same logical operation ID and arguments.
        op_id = "agent_turn_42"
        tool_name = "transfer_funds"
        args = {"from_account": "A", "to_account": "B", "amount": 100}

        self.ledger.register_tool_call("call_transfer_1", op_id, tool_name, args)
        self.ledger.mark_submitted("call_transfer_1")
        self.ledger.mark_committed("call_transfer_1", {"tx_id": "tx_999", "status": "settled"})

        # Gateway or Agent retries under same logical operation ID:
        has_receipt, cached_receipt = self.ledger.check_idempotency(op_id, tool_name, args)
        self.assertTrue(has_receipt)
        self.assertIsNotNone(cached_receipt)
        self.assertEqual(cached_receipt["tx_id"], "tx_999")
        self.assertEqual(cached_receipt["status"], "settled")

    def test_different_arguments_or_operation_not_suppressed(self):
        self.ledger.register_tool_call("call_x", "op_1", "run_cmd", {"cmd": "ls"})
        self.ledger.mark_committed("call_x", {"output": "file1.txt"})

        # Different arguments:
        has_receipt, _ = self.ledger.check_idempotency("op_1", "run_cmd", {"cmd": "pwd"})
        self.assertFalse(has_receipt)

        # Different logical operation:
        has_receipt, _ = self.ledger.check_idempotency("op_2", "run_cmd", {"cmd": "ls"})
        self.assertFalse(has_receipt)

    def test_receipts_expire_after_ttl(self):
        now = [1000.0]
        ledger = ToolExecutionLedger(ttl_seconds=60.0, clock=lambda: now[0])
        ledger.register_tool_call("call_1", "op_1", "run_cmd", {"cmd": "ls"})
        ledger.mark_committed("call_1", {"output": "ok"})
        now[0] += 59.0
        self.assertTrue(ledger.check_idempotency("op_1", "run_cmd", {"cmd": "ls"})[0])
        now[0] += 2.0
        self.assertFalse(ledger.check_idempotency("op_1", "run_cmd", {"cmd": "ls"})[0])
        # The next write sweeps expired records out of memory.
        ledger.register_tool_call("call_2", "op_2", "run_cmd", {"cmd": "ls"})
        self.assertIsNone(ledger.get_record("call_1"))

    def test_storage_is_bounded_by_max_records(self):
        ledger = ToolExecutionLedger(max_records=2)
        for i in range(3):
            ledger.register_tool_call(f"call_{i}", f"op_{i}", "run_cmd", {"i": i})
            ledger.mark_committed(f"call_{i}", {"i": i})
        self.assertIsNone(ledger.get_record("call_0"))
        self.assertIsNotNone(ledger.get_record("call_2"))
        self.assertFalse(ledger.check_idempotency("op_0", "run_cmd", {"i": 0})[0])
        self.assertTrue(ledger.check_idempotency("op_2", "run_cmd", {"i": 2})[0])


if __name__ == "__main__":
    unittest.main()
