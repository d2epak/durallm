"""Scenario definitions are only worth publishing if the gateway passes them and they discriminate."""

import logging
import unittest

from benchmarks.harness import BenchmarkHarness, V3_NAME
from benchmarks.scenarios import get_all_scenarios


class TestScenarioSuite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.harness = BenchmarkHarness()
        cls.by_id = {s.id: s for s in cls.harness.scenarios}

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def test_fifteen_scenarios_with_unique_ids(self):
        self.assertEqual([s.id for s in get_all_scenarios()], [f"B{i}" for i in range(1, 16)])

    def test_gateway_passes_every_scenario(self):
        for scn in self.harness.scenarios:
            res = self.harness.run_scenario(V3_NAME, scn)
            self.assertTrue(res.success, f"{scn.id}: {res.final_output}")

    def test_static_fallback_baseline_fails_the_scenarios_that_need_state(self):
        # Compaction (B4/B5), validation (B6/B7), receipts (B8), breaker memory (B10),
        # pool isolation (B12), cost/capability/reliability routing (B13-B15).
        expected_failures = {"B4", "B5", "B6", "B7", "B8", "B10", "B12", "B13", "B14", "B15"}
        failed = {
            sid for sid in self.by_id
            if not self.harness.run_scenario("Baseline-C-Static-Fallback", self.by_id[sid]).success
        }
        self.assertEqual(failed, expected_failures)


if __name__ == "__main__":
    unittest.main()
