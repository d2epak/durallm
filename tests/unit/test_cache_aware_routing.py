"""Tests for Frontier 6 Cache-Aware & Prefix-Aware Failover and Anthropic Breakpoints."""

import unittest

from durallm.capability.profile import Endpoint, ModelProfile
from durallm.capability.registry import CapabilityRegistry
from durallm.protocol.anthropic import anthropic_request_to_ir, ir_to_anthropic_request
from durallm.protocol.ir import NormalizedMessage, NormalizedRequest, NormalizedToolDefinition
from durallm.routing.cache import PromptCacheTracker, compute_prefix_hash
from durallm.routing.requirements import RequirementVector
from durallm.routing.router import CapabilityRouter


class TestCacheAwareRouting(unittest.TestCase):

    def test_anthropic_ephemeral_cache_control_preserved_round_trip(self):
        """Ensure cache_control: {type: ephemeral} survives translation through Protocol IR."""
        raw_anthropic = {
            "model": "claude-3-5-sonnet",
            "system": [
                {
                    "type": "text",
                    "text": "You are a repository repair assistant.",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "tools": [
                {
                    "name": "bash",
                    "description": "Run shell commands.",
                    "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}},
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Inspect the failing test suite.",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ],
        }

        ir = anthropic_request_to_ir(raw_anthropic)
        self.assertEqual(ir.system_cache_control, {"type": "ephemeral"})
        self.assertEqual(ir.tools[0].cache_control, {"type": "ephemeral"})
        self.assertEqual(ir.messages[0].cache_control, {"type": "ephemeral"})

        re_emitted = ir_to_anthropic_request(ir, "claude-3-5-sonnet")
        self.assertIsInstance(re_emitted["system"], list)
        self.assertEqual(re_emitted["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(re_emitted["tools"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(re_emitted["messages"][0]["content"][0]["cache_control"], {"type": "ephemeral"})

    def test_byte_stable_prefix_hash_invariance(self):
        """Prefix hash is invariant to tool declaration ordering, but sensitive to prompt changes."""
        t1 = NormalizedToolDefinition(name="git", description="Git operations", parameters={})
        t2 = NormalizedToolDefinition(name="bash", description="Bash command", parameters={})

        req1 = NormalizedRequest(
            system_instruction="System instruction v1",
            tools=[t1, t2],
            messages=[NormalizedMessage(role="user", content="Root objective")],
        )
        # Same request with reversed tool order
        req2 = NormalizedRequest(
            system_instruction="System instruction v1",
            tools=[t2, t1],
            messages=[NormalizedMessage(role="user", content="Root objective")],
        )
        # Request with modified system instruction
        req3 = NormalizedRequest(
            system_instruction="Modified instruction v2",
            tools=[t1, t2],
            messages=[NormalizedMessage(role="user", content="Root objective")],
        )

        h1 = compute_prefix_hash(req1)
        h2 = compute_prefix_hash(req2)
        h3 = compute_prefix_hash(req3)

        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, h3)

    def test_router_prioritizes_warm_cache_endpoint(self):
        """Router awards score bonus to endpoint holding a warm prompt cache."""
        reg = CapabilityRegistry()
        ep_cold = Endpoint(
            id="ep-cold",
            provider="prov_a",
            model="model-1",
            base_url="http://prov_a",
            pool="coding",
            priority=1,
            profile=ModelProfile("prov_a", "model-1", supports_tools=True),
        )
        ep_warm = Endpoint(
            id="ep-warm",
            provider="prov_b",
            model="model-1",
            base_url="http://prov_b",
            pool="coding",
            priority=1,
            profile=ModelProfile("prov_b", "model-1", supports_tools=True),
        )
        reg.register_endpoint(ep_cold)
        reg.register_endpoint(ep_warm)

        tracker = PromptCacheTracker(ttl_seconds=300.0)
        router = CapabilityRouter(capability_registry=reg, cache_tracker=tracker)

        req = NormalizedRequest(
            system_instruction="Shared large prompt >1024 tokens",
            messages=[NormalizedMessage(role="user", content="Turn 1")],
        )
        p_hash = compute_prefix_hash(req)

        # Record warm cache on ep-warm
        tracker.record_warm_cache("ep-warm", p_hash)
        self.assertTrue(tracker.is_warm("ep-warm", p_hash))
        self.assertFalse(tracker.is_warm("ep-cold", p_hash))

        # Select candidate: ep-warm receives warm-cache bonus and wins selection
        selected, decision = router.select_candidate(
            requirements=RequirementVector(prefix_hash=p_hash),
            pool="coding",
            strategy="balanced",
        )
        self.assertEqual(selected.id, "ep-warm")
        eval_warm = next(e for e in decision.evaluated_candidates if e.endpoint_id == "ep-warm")
        eval_cold = next(e for e in decision.evaluated_candidates if e.endpoint_id == "ep-cold")
        self.assertTrue(eval_warm.warm_cache)
        self.assertFalse(eval_cold.warm_cache)
        self.assertGreater(eval_warm.final_score, eval_cold.final_score)


if __name__ == "__main__":
    unittest.main()
