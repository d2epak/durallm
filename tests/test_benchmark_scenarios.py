"""Scenario definitions are only worth publishing if the gateway passes them and they discriminate."""

import logging
import unittest

from benchmarks.harness import V3_NAME, BenchmarkHarness
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

    def failures_of(self, system):
        return {sid for sid, scn in self.by_id.items() if not self.harness.run_scenario(system, scn).success}

    def test_static_fallback_baseline_fails_the_scenarios_that_need_state(self):
        # Compaction (B4/B5), validation (B6/B7), receipts (B8), breaker memory (B10),
        # pool isolation (B12), cost/capability/reliability routing (B13-B15).
        expected = {"B4", "B5", "B6", "B7", "B8", "B10", "B12", "B13", "B14", "B15"}
        self.assertEqual(self.failures_of("Baseline-C-Static-Fallback"), expected)

    def test_breaker_alone_does_not_rescue_static_fallback(self):
        # One try per provider per turn: A's breaker opens only after turn 2, too late for B10's turn 2.
        expected = {"B4", "B5", "B6", "B7", "B8", "B10", "B12", "B13", "B14", "B15"}
        self.assertEqual(self.failures_of("Baseline-D-Breaker-Static-Fallback"), expected)

    def test_v1_prototype_prunes_and_isolates_pools_but_neither_validates_nor_routes(self):
        # Its pruner passes B4/B5 and its pools pass B12; it forwards invalid tool calls (B6/B7/B14),
        # re-executes tools (B8), round-robins away from the recovered primary (B10), and ignores
        # cost and capability requirements (B13/B15).
        expected = {"B6", "B7", "B8", "B10", "B13", "B14", "B15"}
        self.assertEqual(self.failures_of("Baseline-E-V1-Prototype"), expected)


if __name__ == "__main__":
    unittest.main()
