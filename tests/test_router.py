import os
import unittest
from unittest.mock import patch

from durallm.classifier import FailoverReason
from durallm.pools import RouteDefinition
from durallm.router import UniversalFailoverRouter, execute_upstream_request


class TestFailoverRouter(unittest.TestCase):

    def test_routing_and_cooldowns(self):
        fallbacks = [
            {"provider": "nvidia", "model": "nemotron"},
            {"provider": "cerebras", "model": "llama3.3"},
            {"provider": "groq", "model": "llama-versatile"},
        ]
        router = UniversalFailoverRouter(configured_fallbacks=fallbacks, auto_discover_free=False)

        # 1. Initial route is nvidia
        self.assertEqual(router.active_provider["provider"], "nvidia")

        # 2. Nvidia fails with 429 -> cooldown -> failover to cerebras
        router.mark_cooldown("nvidia", seconds=60.0)
        route1 = router.get_next_available_route(reason=FailoverReason.rate_limit)
        self.assertEqual(route1["provider"], "cerebras")

        # 3. Cerebras is deprecated -> failover to groq
        router.mark_deprecated("llama3.3")
        route2 = router.get_next_available_route(reason=FailoverReason.model_not_found)
        self.assertEqual(route2["provider"], "groq")

        # 4. Next route wraps around (nvidia still in cooldown, cerebras deprecated -> skips to groq)
        route3 = router.get_next_available_route(reason=FailoverReason.rate_limit)
        self.assertEqual(route3["provider"], "groq")



class TestUpstreamBoundary(unittest.TestCase):

    def test_private_upstream_is_refused_without_a_network_call(self):
        route = RouteDefinition(id="lan", provider="ollama", model="m", pool="coding",
                                base_url="http://192.168.1.10:11434/v1", api_format="openai", env_key=None)
        env = {k: v for k, v in os.environ.items() if k not in ("DURALLM_ALLOW_LOCAL_UPSTREAM", "LLM_BREAKER_ALLOW_LOCAL_UPSTREAM")}
        with patch.dict(os.environ, env, clear=True), patch("urllib.request.urlopen") as urlopen:
            status, _, body = execute_upstream_request(route, {"messages": []})
        urlopen.assert_not_called()
        self.assertEqual(status, 599)
        self.assertTrue(body.startswith(b"transport_error:blocked:"))


if __name__ == "__main__":
    unittest.main()
