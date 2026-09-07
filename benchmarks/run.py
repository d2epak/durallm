"""Benchmark runner CLI: N seeded runs of the B1-B15 suite and the research benchmark, reported as mean ± 95% CI.

Usage: python -m benchmarks.run [--runs N] [--seed S] [--out DIR]

Every run re-seeds ``random`` with ``seed + run_index`` (the executor's jittered backoff is the only
random draw) and rebuilds every system from scratch. Outputs go to ``results/<utc-date>-<commit>/``
as ``report.md`` and ``results.json``; the JSON keeps every run's raw per-scenario data.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import random
import statistics
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from benchmarks.harness import BenchmarkHarness, SystemBenchmarkSummary
from benchmarks.semantic_failover.runner import SemanticFailoverMetrics, run_semantic_failover_benchmark
from durallm import __version__

REPO_ROOT = Path(__file__).resolve().parent.parent

# Two-sided 97.5th percentile of Student's t for 1..30 degrees of freedom; 1.96 beyond that.
T_975 = [
    12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
    2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
    2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
]

SUMMARY_FIELDS = (
    "completion_rate_pct", "recovery_rate_pct", "median_latency_ms",
    "p95_latency_ms", "avg_attempts_per_request", "semantic_error_rate_pct",
)
# Research-benchmark fields that must not change between runs; only the latency is timing-dependent.
RESEARCH_STABLE_FIELDS = tuple(
    f for f in SemanticFailoverMetrics.__dataclass_fields__ if f != "recovery_latency_ms"
)


@dataclass
class Stat:
    mean: float
    ci95: float  # half-width of the 95% confidence interval across runs; 0.0 for one run or identical values
    min: float
    max: float

    def fmt(self, unit: str = "", digits: int = 2) -> str:
        text = f"{self.mean:.{digits}f}"
        if self.ci95:
            text += f" ± {self.ci95:.{digits}f}"
        return text + unit


def mean_ci(values: Sequence[float]) -> Stat:
    n = len(values)
    mean = statistics.fmean(values)
    if n < 2:
        return Stat(mean, 0.0, min(values), max(values))
    t = T_975[n - 2] if n - 1 <= len(T_975) else 1.96
    return Stat(mean, t * statistics.stdev(values) / math.sqrt(n), min(values), max(values))


def aggregate(runs: Sequence[Dict[str, SystemBenchmarkSummary]]) -> Dict[str, Dict[str, Stat]]:
    """Per system, mean ± CI of each summary field across runs."""
    return {
        name: {field: mean_ci([getattr(run[name], field) for run in runs]) for field in SUMMARY_FIELDS}
        for name in runs[0]
    }


def scenario_passes(runs: Sequence[Dict[str, SystemBenchmarkSummary]]) -> Dict[str, Dict[str, int]]:
    """Per system and scenario, how many runs passed."""
    passes: Dict[str, Dict[str, int]] = {}
    for run in runs:
        for name, summary in run.items():
            counts = passes.setdefault(name, {})
            for r in summary.scenario_results:
                counts[r.scenario_id] = counts.get(r.scenario_id, 0) + int(r.success)
    return passes


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True, cwd=REPO_ROOT,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def environment(runs: int, seed: int) -> Dict[str, object]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "commit": _git("rev-parse", "--short", "HEAD") or "nogit",
        "dirty_tree": bool(_git("status", "--porcelain", "--untracked-files=no")),
        "package_version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "runs": runs,
        "seed": seed,
    }


def output_dir(env: Dict[str, object]) -> Path:
    return REPO_ROOT / "results" / f"{str(env['generated_at'])[:10]}-{env['commit']}"


def run_suite(runs: int, seed: int):
    harness = BenchmarkHarness()
    suite_runs: List[Dict[str, SystemBenchmarkSummary]] = []
    research_runs: List[SemanticFailoverMetrics] = []
    for i in range(runs):
        random.seed(seed + i)
        suite_runs.append(harness.run_all())
        research_runs.append(run_semantic_failover_benchmark())
    return harness, suite_runs, research_runs


def build_report(env, harness, suite_runs, research_runs) -> str:
    stats = aggregate(suite_runs)
    passes = scenario_passes(suite_runs)
    runs = len(suite_runs)
    research = research_runs[0]
    research_consistent = all(
        getattr(m, f) == getattr(research, f) for m in research_runs for f in RESEARCH_STABLE_FIELDS
    )
    latency = mean_ci([m.recovery_latency_ms for m in research_runs])
    dirty = " (uncommitted changes present)" if env["dirty_tree"] else ""

    lines = [
        "# LLM Circuit Breaker V3 — Benchmark Report",
        "",
        f"**Generated:** {env['generated_at']}  ",
        f"**Commit:** `{env['commit']}`{dirty}  ",
        f"**Environment:** llm-circuit-breaker {env['package_version']} · Python {env['python']} · {env['platform']}  ",
        f"**Runs:** {runs} (seed {env['seed']}; run *i* re-seeds `random` with seed + *i*, which fixes the jittered backoff draws)  ",
        "**Test Suite:** Scenarios B1 through B15 + Primary Research Benchmark  ",
        "",
        f"Values are the mean over {runs} run(s); ± is the half-width of the 95% confidence interval (Student's t) "
        "where runs differed. Scenario outcomes are deterministic, so a non-zero interval on a completion, recovery or "
        "semantic-error rate means a scenario's outcome changed between runs. Latencies include real backoff sleeps "
        "and vary with the scheduler.",
        "",
        "---",
        "",
        "## 1. Multi-Baseline Comparison Table (B1–B15)",
        "",
        "| Baseline / System | Completion Rate | Recovery Rate | Median Latency | P95 Latency | Avg Attempts/Req | Semantic Error Rate |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, s in stats.items():
        lines.append(
            f"| **{name}** | {s['completion_rate_pct'].fmt('%', 1)} | {s['recovery_rate_pct'].fmt('%', 1)} | "
            f"{s['median_latency_ms'].fmt(' ms')} | {s['p95_latency_ms'].fmt(' ms')} | "
            f"{s['avg_attempts_per_request'].fmt()} | {s['semantic_error_rate_pct'].fmt('%', 1)} |"
        )

    lines.extend([
        "",
        "---",
        "",
        "## 2. Primary Research Benchmark: Semantic Failover",
        "",
        "Compound multi-turn migration: primary (503 outage) -> 32k secondary (compaction, then invalid tool schema) -> "
        "32k tertiary (validated tool call, receipt committed). Turn 2 re-sends the same logical operation after a lost "
        "response; the tool must not run again.",
        "",
        f"- **Task Completed:** `{research.task_completed}`",
        f"- **Critical State Preserved:** `{research.critical_state_preserved}`",
        f"- **Tool Correctness:** `{research.tool_correctness}`",
        f"- **Duplicate Tool Executions:** `{research.duplicate_tool_execution}`",
        f"- **Tool Executions Across Both Turns:** `{research.tool_executions}` (second delivery replayed: `{research.receipt_replayed}`)",
        f"- **Semantic Error Rate:** `{research.semantic_error_rate_pct:.1f}%`",
        f"- **Total Fallback Hops:** `{research.fallback_count}`",
        f"- **Recovery Latency:** `{latency.fmt(' ms')}`",
        f"- **Context Delivered to Tertiary:** `{research.context_tokens_final}` of `{research.context_tokens_initial}` tokens ({research.context_reduction_pct:.1f}% reduction)",
        f"- **Observable FailoverPlans Generated:** `{research.failover_plans_generated}`",
        f"- **Idempotency Receipt Cached:** `{research.receipt_cached}`",
        f"- **Identical across all {runs} run(s) (everything but latency):** `{research_consistent}`",
        "",
        "---",
        "",
        "## 3. Scenario Details Breakdown (B1–B15)",
        "",
        f"V3 per scenario; result counts passing runs out of {runs}, the other columns are means.",
        "",
        "| Scenario | Description | V3 Result | Attempts | Fallback Hops | Latency |",
        "|---|---|---|---|---|---|",
    ])
    v3 = "LLM-Circuit-Breaker-V3"
    for idx, first in enumerate(suite_runs[0][v3].scenario_results):
        sid = first.scenario_id
        per_run = [run[v3].scenario_results[idx] for run in suite_runs]
        passed = passes[v3][sid]
        status = "PASSED" if passed == runs else ("FAILED" if passed == 0 else "FLAKY")
        desc = next((s.description for s in harness.scenarios if s.id == sid), "")
        lines.append(
            f"| **[{sid}]** | {desc} | `{status}` ({passed}/{runs}) | "
            f"{statistics.fmean(r.attempts_count for r in per_run):.2f} | "
            f"{statistics.fmean(r.fallback_depth for r in per_run):.2f} | "
            f"{mean_ci([r.total_latency_ms for r in per_run]).fmt(' ms')} |"
        )
    return "\n".join(lines) + "\n"


def build_json(env, suite_runs, research_runs) -> Dict[str, object]:
    return {
        "environment": env,
        "aggregate": {
            name: {field: asdict(stat) for field, stat in fields.items()}
            for name, fields in aggregate(suite_runs).items()
        },
        "scenario_passes": scenario_passes(suite_runs),
        "runs": [
            {
                "system_summaries": {name: asdict(s) for name, s in suite.items()},
                "primary_research_benchmark": asdict(research),
            }
            for suite, research in zip(suite_runs, research_runs)
        ],
    }


def main(argv: Optional[Sequence[str]] = None) -> Path:
    parser = argparse.ArgumentParser(description="Run the B1-B15 benchmark suite and the research benchmark.")
    parser.add_argument("--runs", type=int, default=5, help="number of full runs to aggregate (default 5)")
    parser.add_argument("--seed", type=int, default=0, help="base seed for the jittered backoff draws (default 0)")
    parser.add_argument("--out", type=Path, default=None, help="output directory (default results/<utc-date>-<commit>)")
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be at least 1")

    env = environment(args.runs, args.seed)
    print("\n" + "=" * 85)
    print("  ⚡ LLM CIRCUIT BREAKER V3 — REPRODUCIBLE AGENT RESILIENCE BENCHMARK SUITE")
    print("=" * 85)
    print(f"Commit {env['commit']}{' (dirty)' if env['dirty_tree'] else ''} · Python {env['python']} · "
          f"{args.runs} run(s), seed {args.seed}\n")

    harness, suite_runs, research_runs = run_suite(args.runs, args.seed)
    stats = aggregate(suite_runs)

    print(f"{'System / Baseline':<36} | {'Completion':<14} | {'Recovery':<14} | {'Median Lat':<18} | {'P95 Lat':<18} | {'Attempts':<12}")
    print("-" * 128)
    for name, s in stats.items():
        print(
            f"{name:<36} | {s['completion_rate_pct'].fmt('%', 1):<14} | {s['recovery_rate_pct'].fmt('%', 1):<14} | "
            f"{s['median_latency_ms'].fmt(' ms'):<18} | {s['p95_latency_ms'].fmt(' ms'):<18} | "
            f"{s['avg_attempts_per_request'].fmt():<12}"
        )

    passes = scenario_passes(suite_runs)["LLM-Circuit-Breaker-V3"]
    print("\n" + "-" * 85)
    print("  V3 SCENARIO BREAKDOWN (B1 - B15): passing runs")
    print("-" * 85)
    print("  " + "  ".join(f"{sid}:{n}/{args.runs}" for sid, n in passes.items()))
    print("-" * 85)

    research = research_runs[0]
    print("\nPrimary Research Benchmark (Compound Multi-Turn Semantic Failover), first run:")
    print(f"  ✔ Task Completed: {research.task_completed}")
    print(f"  ✔ Critical State Preserved: {research.critical_state_preserved}")
    print(f"  ✔ Duplicate Tool Side-Effects: {research.duplicate_tool_execution}")
    print(f"  ✔ Tool Executions Across Both Turns: {research.tool_executions} (second delivery replayed: {research.receipt_replayed})")
    print(f"  ✔ Recovery Latency: {mean_ci([m.recovery_latency_ms for m in research_runs]).fmt(' ms')}")
    print(f"  ✔ Failover Plans Generated: {research.failover_plans_generated}")
    print(f"  ✔ Tool Execution Receipt Cached: {research.receipt_cached}")

    out = args.out or output_dir(env)
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / "report.md"
    json_path = out / "results.json"
    report_path.write_text(build_report(env, harness, suite_runs, research_runs), encoding="utf-8")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(build_json(env, suite_runs, research_runs), f, indent=2)

    print("\n" + "=" * 85)
    print(f"✔ Benchmark report written to: {report_path}")
    print(f"✔ Benchmark raw data written to: {json_path}")
    print("=" * 85 + "\n")
    return out


if __name__ == "__main__":
    main()
