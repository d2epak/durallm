"""benchmarks/run.py: seeded multi-run aggregation with confidence intervals and runtime provenance."""

import contextlib
import io
import json
import logging
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from benchmarks import run as bench_run


class TestStatistics(unittest.TestCase):

    def test_mean_ci_uses_student_t_for_small_samples(self):
        stat = bench_run.mean_ci([1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertEqual(stat.mean, 3.0)
        # t(0.975, df=4) = 2.776; sample stdev = sqrt(2.5); n = 5.
        self.assertAlmostEqual(stat.ci95, 2.776 * (2.5 ** 0.5) / (5 ** 0.5), places=3)
        self.assertEqual((stat.min, stat.max), (1.0, 5.0))

    def test_single_run_and_identical_runs_have_no_interval(self):
        self.assertEqual(bench_run.mean_ci([7.0]).ci95, 0.0)
        self.assertEqual(bench_run.mean_ci([7.0, 7.0, 7.0]).ci95, 0.0)
        self.assertEqual(bench_run.mean_ci([7.0, 7.0]).fmt(" ms"), "7.00 ms")
        self.assertEqual(bench_run.mean_ci([1.0, 3.0]).fmt("%", 1), "2.0 ± 12.7%")

    def test_aggregate_reports_each_field_across_runs(self):
        def summary(**fields):
            base = {f: 0.0 for f in bench_run.SUMMARY_FIELDS}
            base.update(fields)
            return SimpleNamespace(**base)
        runs = [
            {"sys": summary(completion_rate_pct=100.0, median_latency_ms=10.0)},
            {"sys": summary(completion_rate_pct=100.0, median_latency_ms=14.0)},
        ]
        stats = bench_run.aggregate(runs)
        self.assertEqual(stats["sys"]["completion_rate_pct"].ci95, 0.0)
        self.assertEqual(stats["sys"]["median_latency_ms"].mean, 12.0)
        self.assertGreater(stats["sys"]["median_latency_ms"].ci95, 0.0)


class TestProvenance(unittest.TestCase):

    def test_output_dir_is_named_by_utc_date_and_commit(self):
        env = {"generated_at": "2026-09-06T10:11:12+00:00", "commit": "abc1234"}
        self.assertEqual(bench_run.output_dir(env), bench_run.REPO_ROOT / "results" / "2026-09-06-abc1234")

    def test_environment_comes_from_runtime_not_constants(self):
        env = bench_run.environment(runs=3, seed=9)
        stamped = datetime.fromisoformat(env["generated_at"])
        self.assertIsNotNone(stamped.tzinfo)
        self.assertEqual(stamped.utcoffset().total_seconds(), 0)
        self.assertTrue(env["commit"])
        self.assertIn("dirty_tree", env)
        self.assertEqual((env["runs"], env["seed"]), (3, 9))
        self.assertTrue(env["python"].startswith("3."))


class TestEndToEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.tmp = tempfile.TemporaryDirectory()
        with contextlib.redirect_stdout(io.StringIO()):
            cls.out = bench_run.main(["--runs", "2", "--seed", "1", "--out", cls.tmp.name])
        cls.report = (Path(cls.tmp.name) / "report.md").read_text()
        cls.data = json.loads((Path(cls.tmp.name) / "results.json").read_text())

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)
        cls.tmp.cleanup()

    def test_writes_both_outputs_to_the_requested_directory(self):
        self.assertEqual(self.out, Path(self.tmp.name))
        self.assertEqual(self.data["environment"]["runs"], 2)
        self.assertEqual(self.data["environment"]["seed"], 1)
        self.assertEqual(len(self.data["runs"]), 2)

    def test_every_system_has_a_row_and_v3_passes_every_scenario_in_every_run(self):
        rows = [line for line in self.report.splitlines() if line.startswith("| **")]
        self.assertEqual(len(rows), 7 + 15)  # seven systems (including LiteLLM) + fifteen V3 scenario rows
        self.assertEqual(self.data["aggregate"]["LLM-Circuit-Breaker-V3"]["completion_rate_pct"]["mean"], 100.0)
        self.assertEqual(set(self.data["scenario_passes"]["LLM-Circuit-Breaker-V3"].values()), {2})
        self.assertIn("**Runs:** 2 (seed 1;", self.report)
        self.assertIn(f"**Commit:** `{self.data['environment']['commit']}`", self.report)

    def test_research_benchmark_is_identical_across_runs_apart_from_latency(self):
        self.assertIn("**Identical across all 2 run(s) (everything but latency):** `True`", self.report)


if __name__ == "__main__":
    unittest.main()
