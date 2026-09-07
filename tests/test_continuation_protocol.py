"""Unit tests for the cooperating-client Agent Continuation Protocol."""

import unittest

from durallm.continuation import ACP_VERSION, ContinuationRequest, InMemoryContinuationStore
from durallm.errors import ContinuationProtocolError
from durallm.protocol.ir import (
    NormalizedMessage,
    NormalizedRequest,
    NormalizedResponse,
    NormalizedToolCall,
    NormalizedToolDefinition,
)


class TestInMemoryContinuationStore(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryContinuationStore()

    @staticmethod
    def _request():
        return NormalizedRequest(
            request_id="req_acp",
            model="primary-model",
            messages=[
                NormalizedMessage(
                    role="assistant",
                    content="Calling a tool",
                    tool_calls=[NormalizedToolCall(id="call_1", name="read_file", arguments={"path": "a.py"})],
                )
            ],
            tools=[NormalizedToolDefinition(name="read_file")],
        )

    @staticmethod
    def _response():
        return NormalizedResponse(
            response_id="resp_acp",
            model="fallback-model",
            content="Tool result is ready",
            tool_calls=[NormalizedToolCall(id="call_2", name="write_file", arguments={"path": "b.py"})],
        )

    def test_checkpoint_must_be_acknowledged_before_the_next_epoch(self):
        first = self.store.begin_turn(ContinuationRequest(state_digest="a" * 64))
        self.assertEqual(first.epoch, 0)
        self.assertEqual(first.protocol_version, ACP_VERSION)

        completed = self.store.complete_turn(first, self._request(), self._response(), "coding:provider-b")
        self.assertEqual(completed.event_type, "turn_completed")
        self.assertTrue(completed.ack_required)
        self.assertIsNotNone(completed.checkpoint)
        checkpoint = completed.checkpoint
        assert checkpoint is not None
        self.assertEqual(len(checkpoint.request_digest), 64)
        self.assertEqual(len(checkpoint.response_digest), 64)

        with self.assertRaises(ContinuationProtocolError) as unacknowledged:
            self.store.begin_turn(
                ContinuationRequest(
                    session_id=first.session_id,
                    parent_checkpoint_digest=checkpoint.gateway_digest,
                    state_digest="b" * 64,
                )
            )
        self.assertEqual(unacknowledged.exception.status_code, 409)

        acknowledged = self.store.acknowledge(first.session_id, first.turn_id, first.epoch, checkpoint.gateway_digest)
        self.assertEqual(acknowledged.event_type, "checkpoint_acknowledged")
        self.assertFalse(acknowledged.ack_required)

        second = self.store.begin_turn(
            ContinuationRequest(
                session_id=first.session_id,
                parent_checkpoint_digest=checkpoint.gateway_digest,
                state_digest="b" * 64,
            )
        )
        self.assertEqual(second.epoch, 1)
        self.assertEqual(second.parent_checkpoint_digest, checkpoint.gateway_digest)

    def test_stale_acknowledgements_and_parents_are_rejected(self):
        turn = self.store.begin_turn(ContinuationRequest(state_digest="a" * 64))
        checkpoint = self.store.complete_turn(turn, self._request(), self._response(), "coding:provider-b").checkpoint
        assert checkpoint is not None

        with self.assertRaises(ContinuationProtocolError) as stale_ack:
            self.store.acknowledge(turn.session_id, turn.turn_id, turn.epoch, "b" * 64)
        self.assertEqual(stale_ack.exception.status_code, 409)

        self.store.acknowledge(turn.session_id, turn.turn_id, turn.epoch, checkpoint.gateway_digest)
        with self.assertRaises(ContinuationProtocolError) as stale_parent:
            self.store.begin_turn(
                ContinuationRequest(
                    session_id=turn.session_id,
                    parent_checkpoint_digest="c" * 64,
                    state_digest="d" * 64,
                )
            )
        self.assertEqual(stale_parent.exception.status_code, 409)

        with self.assertRaises(ContinuationProtocolError) as double_ack:
            self.store.acknowledge(turn.session_id, turn.turn_id, turn.epoch, checkpoint.gateway_digest)
        self.assertEqual(double_ack.exception.status_code, 409)

    def test_interruption_releases_the_turn_without_claiming_a_checkpoint(self):
        turn = self.store.begin_turn(ContinuationRequest(state_digest="a" * 64))
        event = self.store.interrupt_turn(turn, "upstream deadline exceeded")
        self.assertEqual(event.event_type, "turn_interrupted")
        self.assertEqual(event.detail, "upstream deadline exceeded")

        retry = self.store.begin_turn(ContinuationRequest(session_id=turn.session_id, state_digest="b" * 64))
        self.assertEqual(retry.epoch, 0)

        with self.assertRaises(ContinuationProtocolError) as no_longer_active:
            self.store.complete_turn(turn, self._request(), self._response(), "coding:provider-b")
        self.assertEqual(no_longer_active.exception.status_code, 409)

    def test_rejects_invalid_versions_digests_and_new_session_parent(self):
        with self.assertRaises(ContinuationProtocolError) as unsupported_version:
            self.store.begin_turn(ContinuationRequest(protocol_version="lcb-acp/999", state_digest="a" * 64))
        self.assertEqual(unsupported_version.exception.status_code, 400)

        with self.assertRaises(ContinuationProtocolError) as invalid_digest:
            self.store.begin_turn(ContinuationRequest(state_digest="not-a-digest"))
        self.assertEqual(invalid_digest.exception.status_code, 400)

        with self.assertRaises(ContinuationProtocolError) as unexpected_parent:
            self.store.begin_turn(
                ContinuationRequest(session_id="client-session", parent_checkpoint_digest="a" * 64, state_digest="b" * 64)
            )
        self.assertEqual(unexpected_parent.exception.status_code, 400)

        with self.assertRaises(ContinuationProtocolError) as unknown_checkpoint:
            self.store.acknowledge("unknown", "turn", 0, "a" * 64)
        self.assertEqual(unknown_checkpoint.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
