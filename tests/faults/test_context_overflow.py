"""Context budgets: unsatisfiable budgets raise, and a 413 triggers compact-then-retry on the same candidate (spec §15)."""

import unittest

from durallm.agent.context import ContextBudget, ContextManager
from durallm.errors import ContextOverflowError, NoHealthyRouteError
from durallm.execution.policy import RetryPolicy
from durallm.protocol.ir import NormalizedMessage, NormalizedRequest
from tests.faults.mock_provider import MockFaultAction
from tests.faults.test_executor_backoff import build_executor, make_request


def long_conversation(turns=12, chars_per_message=10_000):
    messages = [
        NormalizedMessage(role="user" if i % 2 == 0 else "assistant", content=f"turn {i} " + ("x" * chars_per_message))
        for i in range(turns)
    ]
    return NormalizedRequest(model="default", messages=messages, system_instruction="Be precise.")


class TestUnsatisfiableBudget(unittest.TestCase):

    def test_protected_content_over_budget_raises(self):
        # Two messages (root + tail) that cannot be dropped, far above a 512-token floor.
        req = long_conversation(turns=2, chars_per_message=20_000)
        budget = ContextBudget(model_context_window=1000, desired_output_tokens=200, safety_margin_tokens=100)

        with self.assertRaises(ContextOverflowError) as ctx:
            ContextManager(preserve_tail_turns=2).compact(req, budget)
        self.assertGreater(ctx.exception.required_tokens, ctx.exception.available_budget)
        self.assertEqual(ctx.exception.available_budget, budget.available_input_budget)

    def test_satisfiable_budget_still_compacts(self):
        req = long_conversation(turns=12, chars_per_message=10_000)
        budget = ContextBudget(model_context_window=32768, desired_output_tokens=4096, safety_margin_tokens=2048)
        compacted, was_compacted = ContextManager().compact(req, budget)
        self.assertTrue(was_compacted)


class TestCompactThenRetry(unittest.TestCase):

    def test_413_compacts_and_retries_same_candidate_before_fallback(self):
        ex, mock_a, mock_b, sleeps = build_executor(RetryPolicy(max_attempts_same_endpoint=2))
        mock_a.set_sequence([MockFaultAction.server_error(413, "request too large"), MockFaultAction.success("ok")])

        resp, _, ledger = ex.execute(long_conversation(), pool="coding", strategy="priority")

        self.assertEqual(resp.content, "ok")
        self.assertEqual([a.endpoint_id for a in ledger.attempts], ["ep-a", "ep-a"])
        self.assertEqual([a.compacted for a in ledger.attempts], [False, True])
        self.assertEqual(ledger.fallback_count, 0)
        self.assertEqual(mock_b.call_history, [])
        self.assertEqual(sleeps, [])

    def test_second_413_falls_back_when_protected_core_cannot_shrink(self):
        ex, mock_a, mock_b, _ = build_executor(RetryPolicy(max_attempts_same_endpoint=3))
        mock_a.set_sequence([MockFaultAction.server_error(413)] * 3)
        mock_b.set_sequence([MockFaultAction.success("from b")])

        resp, _, ledger = ex.execute(long_conversation(), pool="coding", strategy="priority")

        self.assertEqual(resp.content, "from b")
        self.assertEqual([a.endpoint_id for a in ledger.attempts], ["ep-a", "ep-a", "ep-b"])
        self.assertEqual(ledger.fallback_count, 1)

    def test_413_on_tiny_request_falls_back_without_resending(self):
        ex, mock_a, mock_b, _ = build_executor(RetryPolicy(max_attempts_same_endpoint=2))
        mock_a.set_sequence([MockFaultAction.server_error(413)] * 2)
        mock_b.set_sequence([MockFaultAction.success("from b")])

        resp, _, ledger = ex.execute(make_request(), pool="coding", strategy="priority")

        self.assertEqual(resp.content, "from b")
        self.assertEqual([a.endpoint_id for a in ledger.attempts], ["ep-a", "ep-b"])
        self.assertEqual(len(mock_a.call_history), 1)

    def test_no_candidate_fits_raises_no_healthy_route(self):
        ex, mock_a, mock_b, _ = build_executor(RetryPolicy(max_attempts_same_endpoint=1))
        for ep in ex.capability_registry.endpoints_for_pool("coding"):
            ep.profile.context_window = 4096
        with self.assertRaises(NoHealthyRouteError):
            ex.execute(long_conversation(turns=2, chars_per_message=40_000), pool="coding", strategy="priority")
        self.assertEqual(mock_a.call_history, [])
        self.assertEqual(mock_b.call_history, [])


if __name__ == "__main__":
    unittest.main()
