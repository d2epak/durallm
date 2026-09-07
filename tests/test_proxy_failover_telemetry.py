import unittest
from unittest.mock import MagicMock

from llm_circuit_breaker.capability.profile import Endpoint, ModelProfile
from llm_circuit_breaker.capability.registry import CapabilityRegistry
from llm_circuit_breaker.execution.executor import GatewayExecutor
from llm_circuit_breaker.health.telemetry import HealthTelemetryStore
from llm_circuit_breaker.pools import IsolatedPoolManager, RouteDefinition
from llm_circuit_breaker.protocol.ir import NormalizedResponse
from llm_circuit_breaker.providers.base import ProviderExecutionResult
from llm_circuit_breaker.proxy import ProxyGateway, serve_chat_completions
from llm_circuit_breaker.routing.router import CapabilityRouter


class TestProxyFailoverTelemetry(unittest.TestCase):

    def test_failover_telemetry_headers_and_payload_metadata(self):
        reg = CapabilityRegistry()
        ep1 = Endpoint(id="general_agent:primary-broken", provider="test_nvidia", model="nemotron-ultra", base_url="http://p1", protocol="openai", pool="general_agent", priority=1, profile=ModelProfile("test_nvidia", "nemotron-ultra"))
        ep2 = Endpoint(id="general_agent:backup-working", provider="test_groq", model="llama-3.2-11b", base_url="http://p2", protocol="openai", pool="general_agent", priority=2, profile=ModelProfile("test_groq", "llama-3.2-11b"))
        reg.register_endpoint(ep1)
        reg.register_endpoint(ep2)

        health = HealthTelemetryStore()
        router = CapabilityRouter(capability_registry=reg, health_store=health)
        from llm_circuit_breaker.providers.adapters import ProviderAdapterRegistry
        adapters = ProviderAdapterRegistry()
        executor = GatewayExecutor(capability_registry=reg, router=router, health_store=health, adapter_registry=adapters)

        # Primary ep1 fails with 401 Auth error
        mock_adapter1 = MagicMock()
        mock_adapter1.prepare_request.return_value = MagicMock()
        mock_adapter1.execute.return_value = ProviderExecutionResult(
            status_code=401,
            headers={},
            body=b'{"error": {"message": "Invalid API Key"}}',
            duration_ms=10.0,
        )

        # Backup ep2 succeeds with 200 OK
        mock_adapter2 = MagicMock()
        mock_adapter2.prepare_request.return_value = MagicMock()
        mock_adapter2.execute.return_value = ProviderExecutionResult(
            status_code=200,
            headers={},
            body=b'{"id": "resp_backup", "choices": [{"message": {"role": "assistant", "content": "Fallback response"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}',
            duration_ms=20.0,
        )
        mock_adapter2.normalize_response.return_value = NormalizedResponse(
            response_id="resp_backup",
            model="llama-3.2-11b",
            content="Fallback response",
            input_tokens=10,
            output_tokens=5,
        )

        executor.adapter_registry.get_adapter = lambda provider, protocol=None: mock_adapter1 if provider == "test_nvidia" else mock_adapter2

        pm = IsolatedPoolManager()
        pm.agent_routes = [
            RouteDefinition(id="primary-broken", provider="test_nvidia", model="nemotron-ultra", pool="general_agent", base_url="http://p1", api_format="openai", env_key="NVIDIA_API_KEY"),
            RouteDefinition(id="backup-working", provider="test_groq", model="llama-3.2-11b", pool="general_agent", base_url="http://p2", api_format="openai", env_key="GROQ_API_KEY"),
        ]
        pm.keys = {"NVIDIA_API_KEY": "bad_key", "GROQ_API_KEY": "good_key"}

        gateway = ProxyGateway(pool_manager=pm, executor=executor)

        from llm_circuit_breaker import proxy
        old_gateway = proxy.GATEWAY
        try:
            proxy.GATEWAY = gateway
            body = {
                "model": "nvidia/nemotron-ultra",
                "messages": [{"role": "user", "content": "Hello"}],
            }
            status, resp_dict, selected_ep, event, headers = serve_chat_completions(body)

            self.assertEqual(status, 200)
            self.assertEqual(selected_ep.id, "general_agent:backup-working")

            # Check Telemetry Headers
            self.assertEqual(headers.get("X-LCB-Failover"), "true")
            self.assertEqual(headers.get("X-LCB-Requested-Model"), "nvidia/nemotron-ultra")
            self.assertEqual(headers.get("X-LCB-Active-Model"), "llama-3.2-11b")
            self.assertEqual(headers.get("X-LCB-Selected-Endpoint"), "general_agent:backup-working")

            # Check Payload JSON Metadata
            self.assertIn("lcb_failover", resp_dict)
            self.assertTrue(resp_dict["lcb_failover"]["triggered"])
            self.assertEqual(resp_dict["lcb_failover"]["requested_model"], "nvidia/nemotron-ultra")
            self.assertEqual(resp_dict["lcb_failover"]["active_model"], "llama-3.2-11b")

        finally:
            proxy.GATEWAY = old_gateway


if __name__ == "__main__":
    unittest.main()
