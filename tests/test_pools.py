"""Tests for Multi-Agent Isolated Pools and Independent Cooldowns."""

import unittest

from durallm.pools import IsolatedPoolManager, RouteDefinition


class TestIsolatedPools(unittest.TestCase):

    def setUp(self):
        self.mgr = IsolatedPoolManager()
        self.mgr.coding_routes = [
            RouteDefinition("code-cerebras", "cerebras", "llama3.3-70b", "coding", "http://mock", "openai", None, 65536),
            RouteDefinition("code-mistral", "mistral", "codestral-latest", "coding", "http://mock", "openai", None, 256000),
        ]
        self.mgr.agent_routes = [
            RouteDefinition("agent-groq", "groq", "llama-3.3-70b-versatile", "general_agent", "http://mock", "openai", None, 131072),
            RouteDefinition("agent-nvidia", "nvidia", "nemotron", "general_agent", "http://mock", "openai", None, 65536),
        ]

    def test_pool_selection_independence(self):
        c_route = self.mgr.select_route("coding")
        self.assertEqual(c_route.pool, "coding")
        self.assertEqual(c_route.id, "code-cerebras")

        a_route = self.mgr.select_route("general_agent")
        self.assertEqual(a_route.pool, "general_agent")
        self.assertEqual(a_route.id, "agent-groq")

    def test_cooldown_in_coding_does_not_affect_agent_pool(self):
        # Place Cerebras on cooldown in coding pool
        self.mgr.mark_cooldown("coding", "cerebras", seconds=60.0)

        # In coding pool, cerebras is skipped -> mistral selected
        c_route = self.mgr.select_route("coding")
        self.assertEqual(c_route.id, "code-mistral")

        # In agent pool, agent-groq is still healthy and selected
        a_route = self.mgr.select_route("general_agent")
        self.assertEqual(a_route.id, "agent-groq")

    def test_unexported_key_gracefully_skipped(self):
        # Simulate environment where only GROQ_API_KEY is exported
        self.mgr.keys = {"GROQ_API_KEY": "gsk_test123"}

        self.mgr.coding_routes = [
            RouteDefinition("cerebras-r", "cerebras", "llama3.3", "coding", "http://mock", "openai", "CEREBRAS_API_KEY"),
            RouteDefinition("groq-r", "groq", "llama-3.3", "coding", "http://mock", "openai", "GROQ_API_KEY"),
            RouteDefinition("nvidia-r", "nvidia", "nemotron", "coding", "http://mock", "openai", "NVIDIA_API_KEY"),
        ]

        candidates = self.mgr.get_candidate_routes("coding")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].id, "groq-r")

        # Route selection should succeed directly with groq without errors or failing fallback
        selected = self.mgr.select_route("coding")
        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, "groq-r")


if __name__ == "__main__":
    unittest.main()
