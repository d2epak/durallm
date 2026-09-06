"""Crash-recovery and write-ahead safety tests for the SQLite durable stores."""

import os
import tempfile
import unittest

from llm_circuit_breaker.agent.idempotency import ToolExecutionStatus
from llm_circuit_breaker.continuation import ContinuationRequest, SQLiteContinuationStore
from llm_circuit_breaker.errors import ContinuationProtocolError
from llm_circuit_breaker.execution.policy import RetryPolicy
from llm_circuit_breaker.protocol.ir import NormalizedMessage, NormalizedRequest, NormalizedResponse
from llm_circuit_breaker.proxy import build_proxy_gateway
from llm_circuit_breaker.storage import SQLitePersistenceStore, SQLiteToolExecutionLedger
from tests.faults.mock_provider import MockFaultAction
from tests.faults.test_executor_backoff import build_executor


class TestSQLiteDurableState(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmpdir.name, "gateway.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_session_compare_and_swap_attempts_and_fenced_leases(self):
        store = SQLitePersistenceStore(self.path)
        session = store.create_session("session", "lcb-acp/1", {"epoch": 0})
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(session.revision, 1)
        self.assertIsNone(store.save_session("session", "lcb-acp/1", {"epoch": 1}, expected_revision=0))
        updated = store.save_session("session", "lcb-acp/1", {"epoch": 1}, expected_revision=1)
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.revision, 2)

        first = store.acquire_lease("operation:example", "worker-a", 30)
        self.assertIsNotNone(first)
        self.assertIsNone(store.acquire_lease("operation:example", "worker-b", 30))
        assert first is not None
        self.assertFalse(store.release_lease("operation:example", "worker-a", first.fencing_token + 1))
        self.assertTrue(store.release_lease("operation:example", "worker-a", first.fencing_token))
        self.assertIsNotNone(store.acquire_lease("operation:example", "worker-b", 30))

        prepared = store.prepare_attempt("attempt-1", "request-1", {"endpoint_id": "primary"})
        self.assertEqual(prepared.status, "prepared")
        finished = store.finish_attempt("attempt-1", "succeeded", {"endpoint_id": "primary", "status_code": 200})
        self.assertEqual(finished.status, "succeeded")
        self.assertEqual(store.load_attempt("attempt-1"), finished)
        store.close()

    def test_submitted_operation_becomes_indeterminate_after_restart(self):
        first_store = SQLitePersistenceStore(self.path)
        first_ledger = SQLiteToolExecutionLedger(first_store, owner_id="first", recover_inflight=False)
        first_ledger.register_tool_call("call-1", "operation-1", "deploy", {"service": "api"})
        first_ledger.mark_validated("call-1")
        first_ledger.mark_submitted("call-1")
        self.assertEqual(first_ledger.get_record("call-1").status, ToolExecutionStatus.SUBMITTED)
        first_store.close()

        recovered_store = SQLitePersistenceStore(self.path)
        recovered_ledger = SQLiteToolExecutionLedger(recovered_store, owner_id="second")
        recovered = recovered_ledger.get_record("call-1")
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.status, ToolExecutionStatus.INDETERMINATE)
        recovered_ledger.register_tool_call("call-2", "operation-1", "deploy", {"service": "api"})
        self.assertTrue(recovered_ledger.has_indeterminate_operation("operation-1", "deploy", {"service": "api"}))
        self.assertEqual(recovered_ledger.check_idempotency("operation-1", "deploy", {"service": "api"}), (False, None))
        recovered_store.close()

    def test_committed_receipt_replays_after_restart_without_a_new_submission(self):
        first_store = SQLitePersistenceStore(self.path)
        first_ledger = SQLiteToolExecutionLedger(first_store, owner_id="first", recover_inflight=False)
        first_ledger.register_tool_call("call-1", "operation-1", "deploy", {"service": "api"})
        first_ledger.mark_validated("call-1")
        first_ledger.mark_submitted("call-1")
        receipt = {"deployment_id": "dep-1"}
        first_ledger.mark_committed("call-1", receipt)
        first_store.close()

        recovered_store = SQLitePersistenceStore(self.path)
        recovered_ledger = SQLiteToolExecutionLedger(recovered_store, owner_id="second")
        has_receipt, replayed_receipt = recovered_ledger.check_idempotency("operation-1", "deploy", {"service": "api"})
        self.assertTrue(has_receipt)
        self.assertEqual(replayed_receipt, receipt)
        self.assertFalse(recovered_ledger.has_indeterminate_operation("operation-1", "deploy", {"service": "api"}))
        recovered_store.close()

    def test_continuation_checkpoint_survives_a_store_restart(self):
        first_store = SQLitePersistenceStore(self.path)
        first = SQLiteContinuationStore(first_store, owner_id="first")
        turn = first.begin_turn(ContinuationRequest(state_digest="a" * 64))
        event = first.complete_turn(
            turn,
            NormalizedRequest(request_id="request-1", messages=[NormalizedMessage(role="user", content="hello")]),
            NormalizedResponse(response_id="response-1", content="world"),
            "coding:primary",
        )
        checkpoint = event.checkpoint
        self.assertIsNotNone(checkpoint)
        assert checkpoint is not None
        first_store.close()

        recovered_store = SQLitePersistenceStore(self.path)
        recovered = SQLiteContinuationStore(recovered_store, owner_id="second")
        acknowledged = recovered.acknowledge(turn.session_id, turn.turn_id, turn.epoch, checkpoint.gateway_digest)
        self.assertEqual(acknowledged.event_type, "checkpoint_acknowledged")
        next_turn = recovered.begin_turn(
            ContinuationRequest(
                session_id=turn.session_id,
                parent_checkpoint_digest=checkpoint.gateway_digest,
                state_digest="b" * 64,
            )
        )
        self.assertEqual(next_turn.epoch, 1)
        recovered_store.close()

    def test_live_durable_turn_keeps_its_session_lease_until_completion(self):
        first_store = SQLitePersistenceStore(self.path)
        first = SQLiteContinuationStore(first_store, owner_id="first")
        turn = first.begin_turn(ContinuationRequest(state_digest="a" * 64))
        with self.assertRaises(ContinuationProtocolError):
            first.begin_turn(ContinuationRequest(session_id=turn.session_id, state_digest="b" * 64))
        second_store = SQLitePersistenceStore(self.path)
        second = SQLiteContinuationStore(second_store, owner_id="second")

        with self.assertRaises(ContinuationProtocolError) as active:
            second.begin_turn(ContinuationRequest(session_id=turn.session_id, state_digest="b" * 64))
        self.assertEqual(active.exception.status_code, 409)

        first.interrupt_turn(turn, "client cancelled")
        retry = second.begin_turn(ContinuationRequest(session_id=turn.session_id, state_digest="b" * 64))
        self.assertEqual(retry.epoch, 0)
        second.interrupt_turn(retry, "done")
        first_store.close()
        second_store.close()

    def test_executor_persists_attempt_intent_and_terminal_result(self):
        store = SQLitePersistenceStore(self.path)
        executor, mock_a, _, _ = build_executor(retry=RetryPolicy(max_attempts_same_endpoint=1))
        executor.attempt_store = store
        mock_a.set_sequence([MockFaultAction.success("done")])
        request = NormalizedRequest(request_id="durable-request", messages=[NormalizedMessage(role="user", content="hello")])

        response, _, _ = executor.execute(request, pool="coding", strategy="priority")

        self.assertEqual(response.content, "done")
        attempts = store.attempts_for_request("durable-request")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].status, "succeeded")
        self.assertEqual(attempts[0].payload["status_code"], 200)
        store.close()

    def test_proxy_factory_uses_all_durable_stores_for_a_state_database(self):
        gateway = build_proxy_gateway(self.path)
        self.assertIsInstance(gateway.continuation_store, SQLiteContinuationStore)
        self.assertIsInstance(gateway.executor.tool_ledger, SQLiteToolExecutionLedger)
        self.assertIsInstance(gateway.executor.attempt_store, SQLitePersistenceStore)
        gateway.executor.attempt_store.close()


if __name__ == "__main__":
    unittest.main()
