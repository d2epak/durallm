"""Tests for Dynamic Context Pruner & History Compactor."""

import unittest

from llm_circuit_breaker.pruner import (
    estimate_tokens,
    prune_anthropic_request,
    prune_openai_request,
)


class TestContextPruner(unittest.TestCase):

    def test_anthropic_pruner_compacts_historical_tool_results(self):
        req = {
            "model": "claude-sonnet-4-6",
            "system": "System instructions here",
            "messages": [
                {"role": "user", "content": "Root goal: test compaction"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "B" * 6000}]},
                {"role": "assistant", "content": "Step 1 done."},
                {"role": "user", "content": "Step 2 please."},
                {"role": "assistant", "content": "Step 2 done."},
                {"role": "user", "content": "Step 3 please."},
                {"role": "assistant", "content": "Step 3 done."},
                {"role": "user", "content": "Step 4 please."},
            ]
        }
        initial_tokens = estimate_tokens(req)
        pruned = prune_anthropic_request(req, max_context_tokens=1200, safety_margin_tokens=200)
        pruned_tokens = estimate_tokens(pruned)

        self.assertLess(pruned_tokens, initial_tokens)
        self.assertEqual(pruned["messages"][0]["content"], "Root goal: test compaction")
        self.assertIn("compacted by Circuit Breaker", pruned["messages"][2]["content"][0]["content"])

    def test_openai_pruner_compacts_historical_tool_messages(self):
        req = {
            "model": "hermes-default",
            "messages": [
                {"role": "system", "content": "System prompt"},
                {"role": "user", "content": "User prompt"},
                {"role": "assistant", "content": "Calling tool..."},
                {"role": "tool", "tool_call_id": "c1", "content": "LOG_DATA" * 500},
                {"role": "assistant", "content": "Turn 1"},
                {"role": "user", "content": "Turn 2"},
                {"role": "assistant", "content": "Turn 3"},
                {"role": "user", "content": "Turn 4"},
                {"role": "assistant", "content": "Turn 5"},
                {"role": "user", "content": "Turn 6"},
                {"role": "assistant", "content": "Turn 7"},
                {"role": "user", "content": "Turn 8"},
            ]
        }
        initial_tokens = estimate_tokens(req)
        pruned = prune_openai_request(req, max_context_tokens=1000, safety_margin_tokens=200)
        pruned_tokens = estimate_tokens(pruned)

        self.assertLess(pruned_tokens, initial_tokens)
        self.assertIn("compacted by Circuit Breaker", pruned["messages"][3]["content"])


if __name__ == "__main__":
    unittest.main()
