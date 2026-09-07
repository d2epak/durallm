"""Unit tests for Frontier 4: Real Trajectory Evaluation (SWE-bench 30-step Agent Track)."""


from benchmarks.trajectory import (
    AgentTrajectoryBenchmarkRunner,
    generate_swe_bench_30_step_trajectory,
    run_swe_bench_comparison,
)


def test_trajectory_step_generation() -> None:
    """Verify the SWE-bench trajectory builds 30 complete realistic steps."""
    steps = generate_swe_bench_30_step_trajectory()
    assert len(steps) == 30
    assert steps[0].name == "read_issue"
    assert steps[4].injected_fault == "503_failover"
    assert steps[11].injected_fault == "429_rate_limit"
    assert steps[19].injected_fault == "context_overflow"
    assert steps[25].injected_fault == "network_drop"
    assert steps[25].is_state_mutating is True
    assert steps[-1].name == "generate_final_summary"


def test_durallm_trajectory_resilience() -> None:
    """Verify DuraLLM completes all 30 steps with 0 duplicate mutations and survives all faults."""
    runner = AgentTrajectoryBenchmarkRunner()
    result = runner.run_durallm_trajectory()

    assert result.total_steps == 30
    assert result.steps_completed == 30
    assert result.completion_rate_pct == 100.0
    assert result.duplicate_mutations == 0
    assert result.failovers_handled >= 1
    assert result.key_rotations_handled >= 1
    assert result.compactions_performed >= 1
    assert result.tokens_saved_by_compaction > 0
    assert result.plan_preserved is True


def test_comparison_against_naive_baseline() -> None:
    """Verify DuraLLM outperforms naive baselines on duplicate mutations and reasoning preservation."""
    results = run_swe_bench_comparison()
    durallm = results["durallm"]
    naive = results["naive_baseline"]

    # DuraLLM achieves 0 duplicate mutations on stateful tool replay
    assert durallm.duplicate_mutations == 0
    assert naive.duplicate_mutations > 0

    # DuraLLM handles key rotations and compactions
    assert durallm.key_rotations_handled > 0
    assert durallm.tokens_saved_by_compaction > 0
    assert durallm.plan_preserved is True
    assert naive.plan_preserved is False
