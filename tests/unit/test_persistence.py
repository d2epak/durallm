"""Unit tests for SQLitePersistenceStore."""

import unittest

from durallm.agent.idempotency import ToolExecutionRecord, ToolExecutionStatus
from durallm.storage.sqlite import SQLitePersistenceStore


class TestSQLitePersistence(unittest.TestCase):
    def setUp(self):
        self.store = SQLitePersistenceStore(":memory:")

    def test_breaker_state_persistence(self):
        self.store.save_breaker_state(
            breaker_id="groq:llama-3.3-70b",
            state="OPEN",
            failure_rate=75.0,
            slow_call_rate=10.0,
            last_transition_time=12345.67,
        )

        loaded = self.store.load_breaker_state("groq:llama-3.3-70b")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["state"], "OPEN")
        self.assertEqual(loaded["failure_rate"], 75.0)

    def test_tool_receipt_persistence(self):
        rec = ToolExecutionRecord(
            tool_call_id="call_99",
            logical_operation_id="op_alpha",
            tool_name="deploy_service",
            arguments_hash="hash_123",
            arguments={"service": "api", "version": "1.2.0"},
            status=ToolExecutionStatus.COMMITTED,
            execution_receipt={"deployment_id": "dep_555", "status": "deployed"},
        )
        self.store.save_tool_execution(rec)

        has_receipt, receipt = self.store.check_tool_receipt("op_alpha", "deploy_service", "hash_123")
        self.assertTrue(has_receipt)
        self.assertEqual(receipt["deployment_id"], "dep_555")


if __name__ == "__main__":
    unittest.main()
