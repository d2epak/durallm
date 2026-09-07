"""The benchmark harness must score the gateway and the baselines with one rule."""

import unittest

from benchmarks.harness import SYSTEMS, V3_NAME, BenchmarkHarness, build_fixture, percentile
from benchmarks.scenarios import BenchmarkScenario, ScenarioTurn
from benchmarks.tool_runner import ToolRunner
from llm_circuit_breaker.capability.profile import Endpoint
from llm_circuit_breaker.protocol.ir import (
    NormalizedMessage,
    NormalizedRequest,
    NormalizedToolCall,
    NormalizedToolDefinition,
)
from llm_circuit_breaker.providers.adapters import ProviderAdapterRegistry
from tests.faults.mock_provider import MockFaultAction, ProgrammableMockAdapter

BASH = NormalizedToolDefinition(
    name="bash", description="run", parameters={
        "type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"],
    },
)


def request(text="go", tools=(BASH,), **kwargs):
    return NormalizedRequest(model="default", messages=[NormalizedMessage(role="user", content=text)], tools=list(tools), **kwargs)


def scenario(sequences, turns=None, **kwargs):
    turns = turns or [ScenarioTurn(request())]
    return BenchmarkScenario(id="T", name="t", description="t", turns=turns,
                             provider_sequences=sequences, expected_outcome="", **kwargs)


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


class TestMultiTurnScoring(unittest.TestCase):

    def test_verify_hook_failure_marks_the_scenario_failed_for_any_system(self):
        scn = scenario({"provider_a": [MockFaultAction.success("ok")]}, verify=lambda run: "not good enough")
        res = BenchmarkHarness(scenarios=[scn]).run_scenario("Baseline-A-Direct", scn)
        self.assertFalse(res.success)
        self.assertEqual(res.final_output, "verify: not good enough")

    def test_replayed_operation_executes_once_through_the_gateway_but_twice_directly(self):
        call = MockFaultAction.valid_tool_call("bash", {"command": "echo once"})
        turns = [ScenarioTurn(request(request_id="op-1")), ScenarioTurn(request(request_id="op-1"))]
        observed = {}
        scn = scenario({"provider_a": [call, call]}, turns=turns,
                       verify=lambda run: observed.__setitem__("executions", run.tool_runner.executions))
        harness = BenchmarkHarness(scenarios=[scn])

        harness.run_scenario(V3_NAME, scn)
        self.assertEqual(observed["executions"], 1)
        harness.run_scenario("Baseline-A-Direct", scn)
        self.assertEqual(observed["executions"], 2)

    def test_turn_sleep_is_not_charged_to_latency(self):
        scn = scenario({"provider_a": [MockFaultAction.success("ok")] * 2},
                       turns=[ScenarioTurn(request()), ScenarioTurn(request(), sleep_ms=60.0)])
        res = BenchmarkHarness(scenarios=[scn]).run_scenario("Baseline-A-Direct", scn)
        self.assertTrue(res.success)
        self.assertLess(res.total_latency_ms, 50.0)

    def test_pool_and_profile_overrides_shape_the_topology(self):
        scn = scenario({"provider_a": [], "provider_c": []},
                       endpoint_pools={"provider_c": "general_agent"},
                       profile_overrides={"provider_a": {"context_window": 131072}})
        fx = build_fixture(scn)
        self.assertEqual(fx.endpoints["provider_c"].pool, "general_agent")
        self.assertEqual(fx.endpoints["provider_a"].profile.context_window, 131072)
        self.assertEqual(fx.adapters["provider_a"].context_window, 131072)


class TestAddedBaselines(unittest.TestCase):

    def test_baseline_d_stops_calling_a_provider_once_its_breaker_opens(self):
        # One try per provider per turn: A fails in turns 1 and 2 (minimum_number_of_calls=2), so turn 3 skips it.
        seqs = {"provider_a": [MockFaultAction.server_error(503)] * 5, "provider_b": [MockFaultAction.success("b")] * 3}
        observed = {}
        scn = scenario(seqs, turns=[ScenarioTurn(request()) for _ in range(3)],
                       verify=lambda run: observed.__setitem__("calls", run.turn_calls))
        res = BenchmarkHarness(scenarios=[scn]).run_scenario("Baseline-D-Breaker-Static-Fallback", scn)
        self.assertTrue(res.success, res.final_output)
        self.assertEqual(observed["calls"], [["provider_a", "provider_b"], ["provider_a", "provider_b"], ["provider_b"]])

    def test_baseline_e_drives_the_v1_dispatch_loop_through_the_mocks(self):
        seqs = {"provider_a": [MockFaultAction.server_error(503)], "provider_b": [MockFaultAction.success("via v1")]}
        scn = scenario(seqs)
        fx = build_fixture(scn)
        runner = SYSTEMS["Baseline-E-V1-Prototype"](fx, "priority")
        resp = runner.run(scn.turns[0])
        self.assertEqual(resp.content, "via v1")
        self.assertEqual(fx.call_log, ["provider_a", "provider_b"])
        # The pool manager is private to the run; the process-wide POOL_MANAGER is untouched.
        from llm_circuit_breaker.pools import POOL_MANAGER
        self.assertIsNot(runner.router.pool_manager, POOL_MANAGER)
        self.assertEqual({r.provider for r in runner.router.pool_manager.coding_routes}, {"provider_a", "provider_b"})

    def test_baseline_f_uses_the_standard_router_fallback(self):
        seqs = {
            "provider_a": [MockFaultAction.server_error(503)],
            "provider_b": [MockFaultAction.success("via standard router")],
        }
        scn = scenario(seqs)
        fx = build_fixture(scn)
        runner = SYSTEMS["Baseline-F-Standard-Router"](fx, "priority")
        resp = runner.run(scn.turns[0])
        self.assertEqual(resp.content, "via standard router")
        self.assertEqual(fx.call_log, ["provider_a", "provider_b"])


class TestToolRunner(unittest.TestCase):

    def test_counts_duplicates_and_honours_replay_marker(self):
        runner = ToolRunner()
        tc = NormalizedToolCall(id="1", name="bash", arguments={"command": "ls"})
        receipt, executed = runner.handle("op", tc)
        self.assertTrue(executed)
        self.assertEqual(runner.handle("op", NormalizedToolCall(id="2", name="bash", arguments={"command": "ls"}))[1], True)
        self.assertEqual((runner.executions, runner.duplicate_executions), (2, 1))
        replay = NormalizedToolCall(id="3", name="bash", arguments={"command": "ls"}, metadata={"replayed": True, "execution_receipt": receipt})
        self.assertEqual(runner.handle("op", replay), (receipt, False))
        self.assertEqual((runner.executions, runner.replays), (2, 1))


class TestTestDoubles(unittest.TestCase):

    def test_mock_rejects_input_over_its_context_window_without_consuming_the_script(self):
        mock = ProgrammableMockAdapter("provider_b", context_window=100)
        mock.set_sequence([MockFaultAction.success("kept")])
        ep = Endpoint(id="e", provider="provider_b", model="m", base_url="mock://b")
        big = NormalizedRequest(model="m", messages=[NormalizedMessage(role="user", content="x" * 1000)])
        small = NormalizedRequest(model="m", messages=[NormalizedMessage(role="user", content="hi")])
        self.assertEqual(mock.execute(mock.prepare_request(ep, big), timeout_seconds=1.0).status_code, 400)
        self.assertEqual(mock.execute(mock.prepare_request(ep, small), timeout_seconds=1.0).status_code, 200)
        self.assertEqual(len(mock.call_history), 2)

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
