"""Tests for OpenClaw MCP Proxy Edge, Tool Idempotency & Multi-Key Rotation."""

import json
import threading
import time
import unittest
import urllib.request
from typing import Any, Dict

from llm_circuit_breaker.agent.idempotency import ToolExecutionLedger
from llm_circuit_breaker.capability.profile import Endpoint, ModelProfile
from llm_circuit_breaker.capability.registry import CapabilityRegistry
from llm_circuit_breaker.execution.executor import GatewayExecutor
from llm_circuit_breaker.execution.policy import ExecutionPolicy, RetryPolicy
from llm_circuit_breaker.mcp.proxy import MCPProxy, MCPToolDefinition
from llm_circuit_breaker.protocol.ir import NormalizedMessage, NormalizedRequest
from llm_circuit_breaker.providers.adapters import ProviderAdapterRegistry
from llm_circuit_breaker.proxy import start_proxy_server
from tests.faults.mock_provider import MockFaultAction, ProgrammableMockAdapter


class TestOpenClawMCPIdempotency(unittest.TestCase):
    """Verify MCP Proxy JSON-RPC edge, tool idempotency receipts, and duplicate side-effect prevention."""

    def setUp(self) -> None:
        self.ledger = ToolExecutionLedger()
        self.execution_counts: Dict[str, int] = {}

        def deploy_handler(args: Dict[str, Any]) -> str:
            service = args.get("service", "unknown")
            self.execution_counts[service] = self.execution_counts.get(service, 0) + 1
            return f"Service {service} deployed successfully at {time.time()}"

        self.deploy_tool = MCPToolDefinition(
            name="deploy_service",
            description="Deploy an automation workflow or microservice.",
            input_schema={
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "replicas": {"type": "integer"},
                },
                "required": ["service"],
            },
            handler=deploy_handler,
        )
        self.proxy = MCPProxy(tool_ledger=self.ledger, tools=[self.deploy_tool])

    def test_mcp_initialize(self) -> None:
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "OpenClaw", "version": "2.0.0"},
            },
        }
        status, resp, _ = self.proxy.handle_json_rpc(req)
        self.assertEqual(status, 200)
        self.assertEqual(resp["id"], 1)
        self.assertIn("capabilities", resp["result"])
        self.assertIn("tools", resp["result"]["capabilities"])

    def test_mcp_tools_list(self) -> None:
        req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        status, resp, _ = self.proxy.handle_json_rpc(req)
        self.assertEqual(status, 200)
        tools = resp["result"]["tools"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "deploy_service")

    def test_mcp_tool_idempotency_bypasses_duplicate_execution(self) -> None:
        """Calling tools/call twice with the same idempotency key must return cached receipt without re-executing."""
        call_req = {
            "jsonrpc": "2.0",
            "id": 100,
            "method": "tools/call",
            "params": {
                "name": "deploy_service",
                "arguments": {"service": "payment-worker", "replicas": 3},
                "_meta": {
                    "operation_id": "openclaw_op_987",
                },
            },
        }

        # First execution: handler runs
        status1, resp1, headers1 = self.proxy.handle_json_rpc(call_req)
        self.assertEqual(status1, 200)
        self.assertEqual(resp1["result"]["_lcb_status"], "committed")
        self.assertEqual(headers1.get("X-LCB-Tool-Status"), "committed")
        self.assertEqual(self.execution_counts.get("payment-worker"), 1)
        first_output = resp1["result"]["content"][0]["text"]

        # Second execution (retry/replay after network drop): handler MUST NOT run
        call_req["id"] = 101
        status2, resp2, headers2 = self.proxy.handle_json_rpc(call_req)
        self.assertEqual(status2, 200)
        self.assertEqual(resp2["result"]["_lcb_status"], "replayed")
        self.assertEqual(headers2.get("X-LCB-Tool-Status"), "replayed")
        self.assertEqual(headers2.get("X-LCB-Tool-Idempotency"), "replayed")
        # Handler was NOT called a second time
        self.assertEqual(self.execution_counts.get("payment-worker"), 1)
        # Content matches the committed receipt
        self.assertEqual(resp2["result"]["content"][0]["text"], first_output)

    def test_mcp_blocks_duplicate_side_effect_on_indeterminate_status(self) -> None:
        """If a tool submission was interrupted and marked indeterminate, duplicate call fails closed."""
        op_id = "indeterminate_op_42"
        tool_name = "deploy_service"
        args = {"service": "database-migration"}

        # Simulate interrupted submission
        self.ledger.register_tool_call("tc_lost", op_id, tool_name, args)
        self.ledger.mark_submitted("tc_lost")
        self.ledger.mark_indeterminate("tc_lost", "Socket dropped before receiving receipt")

        call_req = {
            "jsonrpc": "2.0",
            "id": 200,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": args,
                "operation_id": op_id,
            },
        }

        status, resp, headers = self.proxy.handle_json_rpc(call_req)
        self.assertEqual(status, 409)
        self.assertEqual(resp["error"]["code"], -32001)
        self.assertIn("indeterminate", resp["error"]["message"])
        self.assertEqual(headers.get("X-LCB-Tool-Status"), "indeterminate")
        self.assertEqual(self.execution_counts.get("database-migration", 0), 0)

    def test_openclaw_multi_key_rotation_under_high_throughput_429s(self) -> None:
        """OpenClaw automation sustains throughput via Multi-Key Rotation on 429."""
        cap_reg = CapabilityRegistry()
        endpoint = Endpoint(
            id="openclaw_automation",
            provider="openai",
            model="openclaw-agent-v1",
            base_url="http://mock-openai",
            env_key="OPENCLAW_KEYS",
            priority=1,
            pool="general_agent",
            profile=ModelProfile("openai", "openclaw-agent-v1"),
        )
        cap_reg.register_endpoint(endpoint)

        adapter = ProgrammableMockAdapter("openai")
        adapter.set_sequence([
            MockFaultAction.rate_limit(retry_after=1),  # Key A fails
            MockFaultAction.rate_limit(retry_after=1),  # Key B fails
            MockFaultAction.success("OpenClaw workflow completed via Key C"),
        ])

        registry = ProviderAdapterRegistry()
        registry.register("openai", adapter)

        executor = GatewayExecutor(
            capability_registry=cap_reg,
            adapter_registry=registry,
            policy=ExecutionPolicy(retry=RetryPolicy(max_attempts_same_endpoint=3)),
            sleeper=lambda _: None,
        )

        req = NormalizedRequest(
            model="openclaw-agent-v1",
            messages=[NormalizedMessage(role="user", content="Execute scheduled automation task.")],
        )

        resp, decision, ledger = executor.execute(
            req,
            pool="general_agent",
            api_keys={"OPENCLAW_KEYS": "KEY_A, KEY_B, KEY_C"},
        )
        self.assertEqual(resp.content, "OpenClaw workflow completed via Key C")
        self.assertEqual(len(adapter.call_history), 3)
        keys_seen = [c.headers["Authorization"] for c in adapter.call_history]
        self.assertEqual(len(set(keys_seen)), 3)
        self.assertEqual(keys_seen[0], "Bearer KEY_A")
        self.assertIn("Bearer KEY_B", keys_seen)
        self.assertIn("Bearer KEY_C", keys_seen)


class TestOpenClawProxyHTTPIntegration(unittest.TestCase):
    """Integration test: exercise /v1/mcp over actual HTTP loopback."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = start_proxy_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_http_mcp_tools_call_and_idempotent_replay(self) -> None:
        url = f"http://127.0.0.1:{self.port}/v1/mcp"
        body = {
            "jsonrpc": "2.0",
            "id": "http-1",
            "method": "tools/call",
            "params": {
                "name": "backup_database",
                "arguments": {"target": "s3://backups/prod"},
                "operation_id": "op_backup_999",
            },
        }

        # First HTTP POST
        req1 = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req1, timeout=5) as r1:
            self.assertEqual(r1.status, 200)
            data1 = json.loads(r1.read())
            self.assertEqual(data1["result"]["_lcb_status"], "committed")

        # Second HTTP POST (Replay)
        body["id"] = "http-2"
        req2 = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req2, timeout=5) as r2:
            self.assertEqual(r2.status, 200)
            data2 = json.loads(r2.read())
            self.assertEqual(data2["result"]["_lcb_status"], "replayed")
            self.assertEqual(r2.headers.get("X-LCB-Tool-Status"), "replayed")


if __name__ == "__main__":
    unittest.main()
