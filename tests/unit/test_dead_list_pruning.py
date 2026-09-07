import unittest
from unittest.mock import MagicMock

from durallm.capability.profile import Endpoint, ModelProfile
from durallm.capability.registry import CapabilityRegistry
from durallm.classifier import FailoverReason, classify_api_error
from durallm.execution.executor import GatewayExecutor
from durallm.health.telemetry import HealthTelemetryStore
from durallm.protocol.ir import NormalizedMessage, NormalizedRequest
from durallm.providers.base import ProviderExecutionResult
from durallm.routing.requirements import RequirementVector
from durallm.routing.router import CapabilityRouter


class TestDeadListPruning(unittest.TestCase):

    def test_classifier_is_permanent_flag(self):
        # 410 EOL
        err_410 = classify_api_error("The model has reached its end of life on 2026-08-26", status_code=410)
        self.assertTrue(err_410.is_permanent)
        self.assertEqual(err_410.reason, FailoverReason.model_not_found)

        # 401 Auth
        err_401 = classify_api_error("401 Unauthorized API key", status_code=401)
        self.assertTrue(err_401.is_permanent)
        self.assertEqual(err_401.reason, FailoverReason.auth)

        # 402 Billing
        err_402 = classify_api_error("Insufficient credits", status_code=402)
        self.assertTrue(err_402.is_permanent)
        self.assertEqual(err_402.reason, FailoverReason.billing)

        # Transient 429
        err_429 = classify_api_error("Rate limit exceeded", status_code=429)
        self.assertFalse(err_429.is_permanent)

        # Transient 503
        err_503 = classify_api_error("Service Unavailable", status_code=503)
        self.assertFalse(err_503.is_permanent)

    def test_router_dead_list_preflight_short_circuit(self):
        reg = CapabilityRegistry()
        ep1 = Endpoint(id="p1:m1", provider="p1", model="m1", base_url="http://p1", protocol="openai", pool="general_agent", profile=ModelProfile("p1", "m1"))
        ep2 = Endpoint(id="p2:m2", provider="p2", model="m2", base_url="http://p2", protocol="openai", pool="general_agent", profile=ModelProfile("p2", "m2"))
        reg.register_endpoint(ep1)
        reg.register_endpoint(ep2)

        health = HealthTelemetryStore()
        router = CapabilityRouter(capability_registry=reg, health_store=health)

        # Initially, ep1 is selected
        candidate, decision = router.select_candidate(RequirementVector(), pool="general_agent", strategy="priority")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.id, "p1:m1")

        # Mark ep1 permanently dead
        router.mark_dead("p1:m1", provider="p1", model="m1", reason="410 Model End of Life")

        # Next select_candidate instantly short-circuits ep1 pre-flight and picks ep2
        candidate2, decision2 = router.select_candidate(RequirementVector(), pool="general_agent", strategy="priority")
        self.assertIsNotNone(candidate2)
        self.assertEqual(candidate2.id, "p2:m2")
        self.assertTrue(any("DeadList pre-flight short-circuit" in (ev.exclusion_reason or "") for ev in decision2.evaluated_candidates if ev.endpoint_id == "p1:m1"))

        # Clear dead list resets eligibility
        router.clear_dead_list()
        candidate3, _ = router.select_candidate(RequirementVector(), pool="general_agent", strategy="priority")
        self.assertEqual(candidate3.id, "p1:m1")

    def test_executor_automatic_blacklisting_on_permanent_error(self):
        reg = CapabilityRegistry()
        ep1 = Endpoint(id="coding:p1:m1", provider="p1", model="m1", base_url="http://p1", protocol="openai", pool="coding", priority=1, profile=ModelProfile("p1", "m1"))
        ep2 = Endpoint(id="coding:p2:m2", provider="p2", model="m2", base_url="http://p2", protocol="openai", pool="coding", priority=2, profile=ModelProfile("p2", "m2"))
        reg.register_endpoint(ep1)
        reg.register_endpoint(ep2)

        health = HealthTelemetryStore()
        router = CapabilityRouter(capability_registry=reg, health_store=health)
        executor = GatewayExecutor(capability_registry=reg, router=router, health_store=health)

        # Mock adapter registry to return 410 EOL on ep1 and 200 OK on ep2
        mock_adapter1 = MagicMock()
        mock_adapter1.prepare_request.return_value = MagicMock()
        mock_adapter1.execute.return_value = ProviderExecutionResult(
            status_code=410,
            headers={},
            body=b'{"error": {"message": "Model p1/m1 is decommissioned and reached end of life"}}',
            duration_ms=10.0,
        )

        mock_adapter2 = MagicMock()
        mock_adapter2.prepare_request.return_value = MagicMock()
        mock_adapter2.execute.return_value = ProviderExecutionResult(
            status_code=200,
            headers={},
            body=b'{"id": "resp_ok", "choices": [{"message": {"role": "assistant", "content": "Hello"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 5}}',
            duration_ms=15.0,
        )
        mock_adapter2.normalize_response.return_value = MagicMock(content="Hello", tool_calls=[], finish_reason="stop", input_tokens=5, output_tokens=5)

        executor.adapter_registry.get_adapter = lambda provider, protocol=None: mock_adapter1 if provider == "p1" else mock_adapter2

        req = NormalizedRequest(request_id="req_test_dead", model="m1", messages=[NormalizedMessage(role="user", content="hi")])

        # Execution attempt 1 fails on ep1 with 410 EOL and fails over to ep2
        resp, decision, ledger = executor.execute(req, pool="coding", strategy="priority")
        self.assertEqual(decision.selected_endpoint.id, "coding:p2:m2")
        self.assertTrue(router.is_dead("coding:p1:m1", provider="p1", model="m1"))

        # Subsequent request immediately skips ep1 pre-flight without calling ep1 adapter!
        mock_adapter1.execute.reset_mock()
        resp2, decision2, ledger2 = executor.execute(req, pool="coding", strategy="priority")
        self.assertEqual(decision2.selected_endpoint.id, "coding:p2:m2")
        mock_adapter1.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
