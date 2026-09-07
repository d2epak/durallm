"""Tool-call ids returned by the provider must reach the client unchanged (ADR 0005)."""

import unittest

from durallm.execution.policy import RetryPolicy
from durallm.protocol.ir import NormalizedMessage, NormalizedRequest, NormalizedToolDefinition
from tests.faults.mock_provider import MockFaultAction
from tests.faults.test_executor_backoff import build_executor


def tool_request():
    return NormalizedRequest(
        model="default",
        messages=[NormalizedMessage(role="user", content="List files")],
        tools=[NormalizedToolDefinition(
            name="bash", description="Run bash",
            parameters={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
        )],
    )


class TestToolCallIdentity(unittest.TestCase):

    def test_provider_tool_call_id_is_preserved(self):
        ex, mock_a, _, _ = build_executor(RetryPolicy(max_attempts_same_endpoint=1))
        mock_a.set_sequence([MockFaultAction.valid_tool_call("bash", {"command": "ls"})])

        resp, _, _ = ex.execute(tool_request(), pool="coding", strategy="priority")

        self.assertEqual([tc.id for tc in resp.tool_calls], ["tc_bash"])

    def test_tool_call_id_is_preserved_after_failover(self):
        ex, mock_a, mock_b, _ = build_executor(RetryPolicy(max_attempts_same_endpoint=1))
        mock_a.set_sequence([MockFaultAction.server_error(503)])
        mock_b.set_sequence([MockFaultAction.valid_tool_call("bash", {"command": "ls"})])

        resp, _, ledger = ex.execute(tool_request(), pool="coding", strategy="priority")

        self.assertEqual(ledger.attempts[-1].endpoint_id, "ep-b")
        self.assertEqual([tc.id for tc in resp.tool_calls], ["tc_bash"])


if __name__ == "__main__":
    unittest.main()
