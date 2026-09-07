"""HTTP 200 with an empty body is a semantic failure, not a success (spec §51)."""

import unittest

from durallm.execution.policy import RetryPolicy
from durallm.models import FailoverReason
from tests.faults.mock_provider import MockFaultAction
from tests.faults.test_executor_backoff import build_executor, make_request


class TestResponseSanity(unittest.TestCase):

    def test_empty_200_body_falls_back_without_poisoning_health(self):
        ex, mock_a, mock_b, sleeps = build_executor(RetryPolicy(max_attempts_same_endpoint=2))
        mock_a.set_sequence([MockFaultAction.success(content="")])
        mock_b.set_sequence([MockFaultAction.success(content="real answer")])

        resp, _, ledger = ex.execute(make_request(), pool="coding", strategy="priority")

        self.assertEqual(resp.content, "real answer")
        self.assertEqual([a.endpoint_id for a in ledger.attempts], ["ep-a", "ep-b"])
        first = ledger.attempts[0]
        self.assertFalse(first.success)
        self.assertEqual(first.failure.reason, FailoverReason.empty_completion)
        self.assertFalse(first.failure.poisons_health)
        self.assertEqual(sleeps, [])  # not retryable on the same endpoint
        states = {k: b.snapshot()["state"] for k, b in ex._test_breaker_registry.all().items()}
        self.assertTrue(states, "expected at least one breaker")
        self.assertTrue(all(s == "CLOSED" for s in states.values()), states)


if __name__ == "__main__":
    unittest.main()
