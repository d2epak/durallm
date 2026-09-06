"""The benchmark harness must score the gateway and the baselines with one rule."""

import unittest

from benchmarks.harness import SYSTEMS, BenchmarkHarness, V3_NAME, build_fixture, percentile
from benchmarks.scenarios import BenchmarkScenario
from llm_circuit_breaker.capability.profile import Endpoint
from llm_circuit_breaker.protocol.ir import NormalizedMessage, NormalizedRequest, NormalizedToolDefinition
from llm_circuit_breaker.providers.adapters import ProviderAdapterRegistry
from tests.faults.mock_provider import MockFaultAction, ProgrammableMockAdapter

BASH = NormalizedToolDefinition(
    name="bash", description="run", parameters={
        "type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"],
    },
)


def scenario(sequences, tools=(BASH,)):
    request = NormalizedRequest(model="default", messages=[NormalizedMessage(role="user", content="go")], tools=list(tools))
    return BenchmarkScenario(id="T", name="t", description="t", request=request,
                             provider_sequences=sequences, expected_outcome="")


class TestPercentile(unittest.TestCase):

    def test_interpolates_between_order_statistics(self):
        self.assertAlmostEqual(percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.95), 4.8)
        self.assertAlmostEqual(percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5), 3.0)

    def test_degenerate_inputs(self):
        self.assertEqual(percentile([], 0.95), 0.0)
        self.assertEqual(percentile([7.0], 0.95), 7.0)


class TestSymmetricScoring(unittest.TestCase):

    def setUp(self):
        self.scn = scenario({
            "provider_a": [MockFaultAction.valid_tool_call("bash", {"unknown_arg": 123})],
            "provider_b": [MockFaultAction.valid_tool_call("bash", {"command": "ls"})],
            "provider_c": [MockFaultAction.success("unused")],
        })
        self.harness = BenchmarkHarness(scenarios=[self.scn])

    def test_invalid_tool_call_forwarded_by_a_baseline_is_a_failure(self):
        for name in ("Baseline-A-Direct", "Baseline-C-Static-Fallback"):
            res = self.harness.run_scenario(name, self.scn)
            self.assertFalse(res.success, name)
            self.assertTrue(res.semantic_error, name)
            self.assertEqual(res.attempts_count, 1, name)
            self.assertEqual(res.fallback_depth, 0, name)

    def test_gateway_that_falls_back_to_a_valid_call_succeeds_with_symmetric_counts(self):
        res = self.harness.run_scenario(V3_NAME, self.scn)
        self.assertTrue(res.success)
        self.assertFalse(res.semantic_error)
        self.assertEqual(res.attempts_count, 2)      # a (invalid) then b, from the mock call log
        self.assertEqual(res.fallback_depth, 1)
        self.assertTrue(res.recovery_occurred)

    def test_every_registered_system_produces_a_summary(self):
        summaries = self.harness.run_all()
        self.assertEqual(set(summaries), set(SYSTEMS))
        for s in summaries.values():
            self.assertEqual(s.total_scenarios, 1)


class TestAttemptAccountingOnExhaustion(unittest.TestCase):

    def test_gateway_failure_reports_real_provider_calls_not_one(self):
        scn = scenario({p: [MockFaultAction.server_error(503)] * 10 for p in ("provider_a", "provider_b", "provider_c")})
        res = BenchmarkHarness(scenarios=[scn]).run_scenario(V3_NAME, scn)
        self.assertFalse(res.success)
        self.assertGreater(res.attempts_count, 1)
        self.assertEqual(res.fallback_depth, 2)
        self.assertIn("Error", res.final_output)


class TestTestDoubles(unittest.TestCase):

    def test_registry_register_installs_adapter(self):
        reg = ProviderAdapterRegistry()
        mock = ProgrammableMockAdapter("provider_x")
        reg.register("Provider_X", mock)
        self.assertIs(reg.get_adapter("provider_x"), mock)

    def test_mock_records_the_ir_request_and_shared_call_log(self):
        log = []
        mock = ProgrammableMockAdapter("provider_a", call_log=log)
        ep = Endpoint(id="e", provider="provider_a", model="m", base_url="mock://a")
        req = NormalizedRequest(model="m", messages=[NormalizedMessage(role="user", content="hi")])
        mock.execute(mock.prepare_request(ep, req), timeout_seconds=1.0)
        self.assertIs(mock.request_history[-1], req)
        self.assertEqual(log, ["provider_a"])

    def test_fixture_registers_one_adapter_per_scripted_provider(self):
        fx = build_fixture(scenario({"provider_a": [], "provider_b": []}))
        self.assertEqual(set(fx.adapters), {"provider_a", "provider_b"})
        self.assertIs(fx.registry.get_adapter("provider_a"), fx.adapters["provider_a"])


if __name__ == "__main__":
    unittest.main()
