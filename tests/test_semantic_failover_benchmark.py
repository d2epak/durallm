"""The research benchmark must earn every number it reports."""

import logging
import unittest

from benchmarks.scenarios import SECONDARY_CONTEXT_WINDOW
from benchmarks.semantic_failover.runner import run_semantic_failover_benchmark


class TestSemanticFailoverBenchmark(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.m = run_semantic_failover_benchmark()

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def test_task_completes_through_two_failover_hops(self):
        self.assertTrue(self.m.task_completed)
        self.assertTrue(self.m.tool_correctness)
        self.assertEqual(self.m.semantic_error_rate_pct, 0.0)
        self.assertEqual((self.m.fallback_count, self.m.failover_plans_generated, self.m.total_attempts), (2, 2, 3))

    def test_root_secret_survives_compaction_into_the_32k_window(self):
        self.assertTrue(self.m.critical_state_preserved)
        self.assertLess(self.m.context_tokens_final, self.m.context_tokens_initial)
        self.assertLessEqual(self.m.context_tokens_final, SECONDARY_CONTEXT_WINDOW)

    def test_lost_response_retry_replays_the_receipt_instead_of_executing(self):
        self.assertEqual((self.m.tool_executions, self.m.duplicate_tool_execution), (1, 0))
        self.assertTrue(self.m.receipt_replayed)
        self.assertTrue(self.m.receipt_cached)


if __name__ == "__main__":
    unittest.main()
