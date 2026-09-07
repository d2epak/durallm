"""Tests for OpenCode Diagnostic Context Compactor."""

import unittest

from durallm.agent.context import (
    ContextBudget,
    ContextManager,
    estimate_tokens,
    extract_diagnostic_summary,
)
from durallm.errors import ContextOverflowError
from durallm.protocol.ir import (
    NormalizedMessage,
    NormalizedRequest,
    NormalizedToolResult,
)


class TestOpenCodeDiagnosticCompactor(unittest.TestCase):
    """Verify diagnostic context compaction preserves root objectives and critical error diagnostics."""

    def test_extract_diagnostic_summary_from_pytest(self) -> None:
        raw = """============================= test session starts ==============================
platform darwin -- Python 3.11.15, pytest-8.3.4, pluggy-1.5.0
rootdir: /workspace/project
plugins: anyio-4.8.0, cov-6.0.0
collected 45 items

tests/test_auth.py .........................                             [ 55%]
tests/test_payment.py ...F...                                            [ 71%]
tests/test_deploy.py .............                                       [100%]

=================================== FAILURES ===================================
___________________________ test_payment_charge_retry ___________________________
tests/test_payment.py:142: in test_payment_charge_retry
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
E   AssertionError: Expected 200, got 500
E   + where 500 = <Response [500]>.status_code

=========================== short test summary info ============================
FAILED tests/test_payment.py::test_payment_charge_retry - AssertionError: Expected 200, got 500
========================= 1 failed, 44 passed in 3.42s =========================
exit code: 1
"""
        summary = extract_diagnostic_summary(raw, max_chars=800)
        self.assertIn("FAILED tests/test_payment.py::test_payment_charge_retry", summary)
        self.assertIn("AssertionError: Expected 200, got 500", summary)
        self.assertIn("exit code: 1", summary)
        self.assertLessEqual(len(summary), 800)

    def test_extract_diagnostic_summary_from_compiler_and_traceback(self) -> None:
        raw = """$ cargo build --release
   Compiling libc v0.2.169
   Compiling cfg-if v1.0.0
   Compiling engine v0.1.0 (/workspace/engine)
error[E0425]: cannot find value `unresolved_symbol` in this scope
  --> src/executor/pipeline.rs:88:13
   |
88 |     let _ = unresolved_symbol();
   |             ^^^^^^^^^^^^^^^^^ not found in this scope

error[E0308]: mismatched types
  --> src/executor/pipeline.rs:104:18
   |
104|     return Ok(42);
   |            ^^^^^^ expected `()`, found `Result<{integer}, _>`

error: aborting due to 2 previous errors
For more information about this error, try `rustc --explain E0425`.
error: could not compile `engine` (bin "engine") due to 2 previous errors
exit code: 101
"""
        summary = extract_diagnostic_summary(raw, max_chars=600)
        self.assertIn("error[E0425]: cannot find value `unresolved_symbol`", summary)
        self.assertIn("error[E0308]: mismatched types", summary)
        self.assertIn("exit code: 101", summary)

    def test_context_manager_compacts_huge_compiler_log_preserving_root_and_final(self) -> None:
        root_objective = "ROOT_OBJECTIVE: Refactor payment pipeline to support multi-currency."
        final_instruction = "Fix the compiler errors in pipeline.rs and run pytest."

        # Simulate 60,000 characters of compiler & test log output in a tool result
        failing_log = (
            "Building project dependencies...\n"
            + ("Compiling module_part_x...\n" * 500)
            + "src/executor/pipeline.rs:88:13: error: cannot find value `unresolved_symbol`\n"
            + "tests/test_payment.py:142: FAILED - AssertionError: Expected 200, got 500\n"
            + ("Intermediate debug noise line\n" * 800)
            + "Build failed with exit code 1\n"
        )

        req = NormalizedRequest(
            model="opencode-coding-model",
            system_instruction="You are an autonomous senior coding engineer.",
            messages=[
                NormalizedMessage(role="user", content=root_objective),
                NormalizedMessage(role="assistant", content="Running build..."),
                NormalizedMessage(
                    role="tool",
                    content=failing_log,
                    tool_results=[NormalizedToolResult(tool_call_id="call_1", content=failing_log)],
                ),
                NormalizedMessage(role="user", content=final_instruction),
            ],
        )

        # Budget of 4096 tokens total, available input budget = 4096 - 1024 - 512 = 2560 tokens
        budget = ContextBudget(model_context_window=4096, desired_output_tokens=1024, safety_margin_tokens=512)

        manager = ContextManager(preserve_tail_turns=4)
        compacted, was_compacted = manager.compact(req, budget)

        self.assertTrue(was_compacted)
        self.assertLessEqual(estimate_tokens(compacted), budget.available_input_budget)

        # Verify Root Objective is intact
        self.assertEqual(compacted.messages[0].content, root_objective)

        # Verify Final Instruction is intact
        self.assertEqual(compacted.messages[-1].content, final_instruction)

        # Verify critical diagnostic markers were retained in the compacted tool result
        tool_content = compacted.messages[2].tool_results[0].content
        self.assertIn("error", tool_content)
        self.assertIn("unresolved_symbol", tool_content)

    def test_context_manager_fails_closed_if_root_objective_alone_exceeds_budget(self) -> None:
        """Iron rule: if immutable root objective alone exceeds window, must raise ContextOverflowError."""
        huge_root = "CRITICAL_GOAL: " + ("x" * 20000)
        req = NormalizedRequest(
            model="coding",
            messages=[NormalizedMessage(role="user", content=huge_root)],
        )
        budget = ContextBudget(model_context_window=1024, desired_output_tokens=256, safety_margin_tokens=256)
        manager = ContextManager()
        with self.assertRaises(ContextOverflowError):
            manager.compact(req, budget)


if __name__ == "__main__":
    unittest.main()
