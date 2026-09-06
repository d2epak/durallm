"""A committed tool call re-proposed under the same logical operation is replayed, not re-executed."""

import unittest

from llm_circuit_breaker.agent.idempotency import ToolExecutionStatus
from llm_circuit_breaker.execution.policy import RetryPolicy
from llm_circuit_breaker.protocol.ir import NormalizedMessage, NormalizedRequest, NormalizedToolDefinition
from tests.faults.mock_provider import MockFaultAction
from tests.faults.test_executor_backoff import build_executor


class StatefulToolRunner:
    """Client-side runner: executes unless the gateway marked the call replayed, and commits receipts."""

    def __init__(self, ledger):
        self.ledger = ledger
        self.executions = 0

    def run(self, tool_call):
        if tool_call.metadata.get("replayed"):
            return tool_call.metadata["execution_receipt"]
        self.executions += 1
        receipt = {"exit_code": 0, "run": self.executions}
        self.ledger.mark_committed(tool_call.metadata["ledger_call_id"], receipt)
        return receipt


def turn_request(operation_id):
    return NormalizedRequest(
        request_id=operation_id,
        model="default",
        messages=[NormalizedMessage(role="user", content="Deploy")],
        tools=[NormalizedToolDefinition(
            name="bash", description="Run bash",
            parameters={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
        )],
    )


class TestToolReplay(unittest.TestCase):

    def test_disconnect_after_execution_does_not_execute_twice(self):
        ex, mock_a, _, _ = build_executor(RetryPolicy(max_attempts_same_endpoint=1))
        runner = StatefulToolRunner(ex.tool_ledger)
        # The model proposes the same side-effecting call on both turns.
        mock_a.set_sequence([MockFaultAction.valid_tool_call("bash", {"command": "deploy"})] * 2)

        # Turn 1: tool executes and commits, then the connection drops before the client can continue.
        resp1, _, _ = ex.execute(turn_request("op-1"), pool="coding", strategy="priority")
        receipt1 = runner.run(resp1.tool_calls[0])

        # Turn 2: client retries the same logical operation.
        resp2, _, _ = ex.execute(turn_request("op-1"), pool="coding", strategy="priority")
        tc2 = resp2.tool_calls[0]
        self.assertTrue(tc2.metadata["replayed"])
        self.assertEqual(tc2.metadata["execution_receipt"], receipt1)
        self.assertEqual(ex.tool_ledger.get_record(tc2.metadata["ledger_call_id"]).status, ToolExecutionStatus.REPLAYED)

        receipt2 = runner.run(tc2)
        self.assertEqual(receipt2, receipt1)
        self.assertEqual(runner.executions, 1)

    def test_new_operation_is_not_suppressed(self):
        ex, mock_a, _, _ = build_executor(RetryPolicy(max_attempts_same_endpoint=1))
        runner = StatefulToolRunner(ex.tool_ledger)
        mock_a.set_sequence([MockFaultAction.valid_tool_call("bash", {"command": "deploy"})] * 2)

        for op in ("op-1", "op-2"):
            resp, _, _ = ex.execute(turn_request(op), pool="coding", strategy="priority")
            self.assertFalse(resp.tool_calls[0].metadata.get("replayed", False))
            runner.run(resp.tool_calls[0])
        self.assertEqual(runner.executions, 2)


if __name__ == "__main__":
    unittest.main()
