"""Authoritative Benchmark Runner CLI and Report Generator (V3 Master Suite)."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path

from benchmarks.harness import BenchmarkHarness
from benchmarks.semantic_failover.runner import run_semantic_failover_benchmark


def main():
    print("\n" + "=" * 85)
    print("  ⚡ LLM CIRCUIT BREAKER V3 — REPRODUCIBLE AGENT RESILIENCE BENCHMARK SUITE")
    print("=" * 85)
    print("Executing Scenarios B1 through B15 against Baselines A, B, C and V3 Gateway...\n")

    harness = BenchmarkHarness()
    summaries = harness.run_all()

    # Print summary table
    print(f"{'System / Baseline':<32} | {'Completion':<12} | {'Recovery':<10} | {'Median Lat':<12} | {'P95 Lat':<10} | {'Attempts':<8}")
    print("-" * 96)
    for name, s in summaries.items():
        print(
            f"{name:<32} | "
            f"{s.completion_rate_pct:>10.1f}% | "
            f"{s.recovery_rate_pct:>8.1f}% | "
            f"{s.median_latency_ms:>10.1f}ms | "
            f"{s.p95_latency_ms:>8.1f}ms | "
            f"{s.avg_attempts_per_request:>8.2f}"
        )

    # Detailed per-scenario breakdown for V3
    v3_summary = summaries["LLM-Circuit-Breaker-V3"]
    print("\n" + "-" * 85)
    print("  V3 SCENARIO BREAKDOWN (B1 - B15):")
    print("-" * 85)
    for r in v3_summary.scenario_results:
        status = "PASSED" if r.success else "FAILED"
        print(f"  [{r.scenario_id:<3}] {status:<8} (Attempts: {r.attempts_count}, Fallback Depth: {r.fallback_depth}, Latency: {r.total_latency_ms:.1f}ms)")
    print("-" * 85)

    # Primary Research Benchmark Execution
    print("\nExecuting Primary Research Benchmark (Compound Multi-Turn Semantic Failover)...")
    research_metrics = run_semantic_failover_benchmark()
    print(f"  ✔ Research Benchmark Task Completed: {research_metrics.task_completed}")
    print(f"  ✔ Critical State Preserved: {research_metrics.critical_state_preserved}")
    print(f"  ✔ Duplicate Tool Side-Effects: {research_metrics.duplicate_tool_execution}")
    print(f"  ✔ Tool Executions Across Both Turns: {research_metrics.tool_executions} (second delivery replayed: {research_metrics.receipt_replayed})")
    print(f"  ✔ Recovery Latency: {research_metrics.recovery_latency_ms:.2f}ms")
    print(f"  ✔ Failover Plans Generated: {research_metrics.failover_plans_generated}")
    print(f"  ✔ Tool Execution Receipt Cached: {research_metrics.receipt_cached}")

    # Generate JSON and Markdown Reports
    results_dir = Path("results")
    results_dir.mkdir(parents=True, exist_ok=True)

    json_path = results_dir / "v3_benchmark_results.json"
    report_path = results_dir / "v3_benchmark_report.md"

    # 1. JSON output
    data = {
        "timestamp": time.time(),
        "date": "2026-09-03",
        "system_summaries": {name: asdict(s) for name, s in summaries.items()},
        "primary_research_benchmark": asdict(research_metrics),
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    # 2. Markdown output
    lines = [
        "# LLM Circuit Breaker V3 — Authoritative Benchmark Report",
        "",
        f"**Date:** {date.today().isoformat()}  ",
        "**Test Suite:** Scenarios B1 through B15 + Primary Research Benchmark  ",
        "**Target Architecture:** V3 Agent-Resilience Gateway with Formal FSM, Real Telemetry, and Semantic Failover  ",
        "",
        "---",
        "",
        "## 1. Multi-Baseline Comparison Table (B1–B15)",
        "",
        "| Baseline / System | Completion Rate | Recovery Rate | Median Latency | P95 Latency | Avg Attempts/Req | Semantic Error Rate |",
        "|---|---|---|---|---|---|---|",
    ]

    for name, s in summaries.items():
        lines.append(
            f"| **{name}** | {s.completion_rate_pct:.1f}% | {s.recovery_rate_pct:.1f}% | {s.median_latency_ms:.2f} ms | {s.p95_latency_ms:.2f} ms | {s.avg_attempts_per_request:.2f} | {s.semantic_error_rate_pct:.1f}% |"
        )

    lines.extend([
        "",
        "---",
        "",
        "## 2. Primary Research Benchmark: Semantic Failover",
        "",
        "Compound multi-turn migration: primary (503 outage) -> 32k secondary (compaction, then invalid tool schema) -> 32k tertiary (validated tool call, receipt committed). Turn 2 re-sends the same logical operation after a lost response; the tool must not run again.",
        "",
        f"- **Task Completed:** `{research_metrics.task_completed}`",
        f"- **Critical State Preserved:** `{research_metrics.critical_state_preserved}`",
        f"- **Tool Correctness:** `{research_metrics.tool_correctness}`",
        f"- **Duplicate Tool Executions:** `{research_metrics.duplicate_tool_execution}`",
        f"- **Tool Executions Across Both Turns:** `{research_metrics.tool_executions}` (second delivery replayed: `{research_metrics.receipt_replayed}`)",
        f"- **Semantic Error Rate:** `{research_metrics.semantic_error_rate_pct:.1f}%`",
        f"- **Total Fallback Hops:** `{research_metrics.fallback_count}`",
        f"- **Recovery Latency:** `{research_metrics.recovery_latency_ms:.2f} ms`",
        f"- **Context Delivered to Tertiary:** `{research_metrics.context_tokens_final}` of `{research_metrics.context_tokens_initial}` tokens ({research_metrics.context_reduction_pct:.1f}% reduction)",
        f"- **Observable FailoverPlans Generated:** `{research_metrics.failover_plans_generated}`",
        f"- **Idempotency Receipt Cached:** `{research_metrics.receipt_cached}`",
        "",
        "---",
        "",
        "## 3. Scenario Details Breakdown (B1–B15)",
        "",
        "| Scenario | Description | V3 Result | Attempts | Fallback Hops | Latency |",
        "|---|---|---|---|---|---|",
    ])

    for r in v3_summary.scenario_results:
        scen_desc = next((s.description for s in harness.scenarios if s.id == r.scenario_id), "")
        status = "PASSED" if r.success else "FAILED"
        lines.append(
            f"| **[{r.scenario_id}]** | {scen_desc} | `{status}` | {r.attempts_count} | {r.fallback_depth} | {r.total_latency_ms:.2f} ms |"
        )

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print("\n" + "=" * 85)
    print(f"✔ Benchmark report written to: {report_path}")
    print(f"✔ Benchmark raw data written to: {json_path}")
    print("=" * 85 + "\n")


if __name__ == "__main__":
    main()
