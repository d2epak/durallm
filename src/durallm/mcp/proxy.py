"""Model Context Protocol (MCP) Reverse Proxy Edge with Idempotency Receipts."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from durallm.agent.idempotency import (
    DEFAULT_TOOL_LEDGER,
    ToolExecutionLedger,
)
from durallm.agent.tool_validation import ToolCallValidator

logger = logging.getLogger("durallm.mcp.proxy")


@dataclass
class MCPToolDefinition:
    """Specification and optional local handler for an MCP tool."""

    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Optional[Callable[[Dict[str, Any]], Any]] = None


class MCPProxy:
    """
    Reverse proxy edge for Model Context Protocol (MCP) JSON-RPC requests.

    Features:
    - MCP JSON-RPC 2.0 dispatch (initialize, tools/list, tools/call).
    - Iron Rule 3 Idempotency: Prevents duplicate tool executions (e.g. bash/API calls)
      via ToolExecutionLedger receipts. Replayed calls return cached receipts.
    - Indeterminate state protection: Fails closed on indeterminate / lost-ack operations.
    - Schema validation against registered input schemas.
    """

    def __init__(
        self,
        tool_ledger: Optional[ToolExecutionLedger] = None,
        tools: Optional[List[MCPToolDefinition]] = None,
        validator: Optional[ToolCallValidator] = None,
    ):
        self.tool_ledger = tool_ledger or DEFAULT_TOOL_LEDGER
        self.validator = validator or ToolCallValidator(strict=True)
        self.tools: Dict[str, MCPToolDefinition] = {}
        if tools:
            for t in tools:
                self.tools[t.name] = t

    def register_tool(self, tool: MCPToolDefinition) -> None:
        self.tools[tool.name] = tool

    def handle_json_rpc(
        self,
        body: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
        """
        Process an MCP JSON-RPC request.
        Returns (http_status, json_rpc_response, response_headers).
        """
        headers = headers or {}
        req_id = body.get("id")

        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
            return 400, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32600, "message": "Invalid Request: must be valid JSON-RPC 2.0"},
            }, {}

        method = body.get("method")
        params = body.get("params", {})
        if not isinstance(params, dict):
            params = {}

        if method == "initialize":
            return self._handle_initialize(req_id, params)
        elif method in ("tools/list", "tools/list_tools"):
            return self._handle_tools_list(req_id)
        elif method == "tools/call":
            return self._handle_tools_call(req_id, params, headers)
        else:
            return 200, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }, {}

    def _handle_initialize(self, req_id: Any, params: Dict[str, Any]) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
        protocol_version = params.get("protocolVersion", "2024-11-05")
        return 200, {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": protocol_version,
                "capabilities": {
                    "tools": {"listChanged": False},
                },
                "serverInfo": {
                    "name": "llm-circuit-breaker-mcp-proxy",
                    "version": "3.0.0",
                },
            },
        }, {"Content-Type": "application/json"}

    def _handle_tools_list(self, req_id: Any) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
        tool_list = [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
            }
            for t in self.tools.values()
        ]
        return 200, {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": tool_list},
        }, {"Content-Type": "application/json"}

    def _handle_tools_call(
        self,
        req_id: Any,
        params: Dict[str, Any],
        headers: Dict[str, str],
    ) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        if not isinstance(tool_name, str) or not tool_name:
            return 200, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": "Invalid params: 'name' is required"},
            }, {}

        # 1. Validate Schema if tool is registered
        if tool_name in self.tools:
            tool_def = self.tools[tool_name]
            report = self.validator.validate_tool_call(
                tool_name=tool_name,
                arguments=arguments,
                schema=tool_def.input_schema,
            )
            if not report.is_executable:
                return 200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Schema Validation Error: {report.error_message}"}],
                        "isError": True,
                        "_lcb_status": "schema_invalid",
                    },
                }, {"X-LCB-Tool-Status": "schema_invalid"}
            arguments = report.validated_arguments

        # 2. Extract Operation & Idempotency IDs
        meta = params.get("_meta", {})
        if not isinstance(meta, dict):
            meta = {}

        operation_id = (
            meta.get("operation_id")
            or meta.get("idempotency_key")
            or params.get("operation_id")
            or params.get("idempotency_key")
            or headers.get("Idempotency-Key")
            or headers.get("idempotency-key")
            or headers.get("X-LCB-Operation-Id")
            or headers.get("x-lcb-operation-id")
        )
        if not operation_id:
            # Deterministic operation ID derived from tool name and argument hash
            arg_hash = ToolExecutionLedger.compute_arguments_hash(arguments)
            operation_id = f"op_{tool_name}_{arg_hash[:16]}"

        tool_call_id = (
            meta.get("tool_call_id")
            or params.get("tool_call_id")
            or headers.get("X-LCB-Tool-Call-Id")
            or headers.get("x-lcb-tool-call-id")
            or f"mcp_{operation_id}_{req_id or int(time.time() * 1000)}"
        )

        # 3. Check Indeterminate Status (Lost Acknowledgment Protection)
        if self.tool_ledger.has_indeterminate_operation(operation_id, tool_name, arguments):
            logger.warning("MCP tool call %s blocked due to indeterminate prior execution", tool_name)
            return 409, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32001,
                    "message": (
                        f"Duplicate side-effect hazard: tool '{tool_name}' has indeterminate status from a prior execution. "
                        "Automatic re-execution prohibited by LLM Circuit Breaker."
                    ),
                    "data": {"operation_id": operation_id, "indeterminate": True},
                },
            }, {"X-LCB-Tool-Status": "indeterminate", "X-LCB-Tool-Idempotency": "blocked"}

        # 4. Check Committed Idempotency Receipt (Deduplication)
        has_receipt, cached_receipt = self.tool_ledger.check_idempotency(operation_id, tool_name, arguments)
        if has_receipt and cached_receipt is not None:
            logger.info("MCP tool call %s replayed from cached receipt (idempotency key: %s)", tool_name, operation_id)
            self.tool_ledger.mark_replayed(tool_call_id, cached_receipt)
            output_text = cached_receipt.get("output", "")
            if not isinstance(output_text, str):
                output_text = json.dumps(output_text, ensure_ascii=False)
            return 200, {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": output_text}],
                    "isError": cached_receipt.get("is_error", False),
                    "_lcb_status": "replayed",
                    "_lcb_receipt": cached_receipt,
                },
            }, {
                "X-LCB-Tool-Status": "replayed",
                "X-LCB-Tool-Idempotency": "replayed",
                "Content-Type": "application/json",
            }

        # 5. First-time Execution: Register Lifecycle
        self.tool_ledger.register_tool_call(tool_call_id, operation_id, tool_name, arguments)
        self.tool_ledger.mark_validated(tool_call_id)
        self.tool_ledger.mark_submitted(tool_call_id)

        # 6. Execute Tool
        tool_def = self.tools.get(tool_name)
        if tool_def and tool_def.handler:
            try:
                raw_result = tool_def.handler(arguments)
                output_text = raw_result if isinstance(raw_result, str) else json.dumps(raw_result, ensure_ascii=False)
                receipt = {
                    "status": "committed",
                    "output": output_text,
                    "is_error": False,
                    "timestamp": time.time(),
                }
                self.tool_ledger.mark_committed(tool_call_id, receipt)
                return 200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": output_text}],
                        "isError": False,
                        "_lcb_status": "committed",
                        "_lcb_receipt": receipt,
                    },
                }, {
                    "X-LCB-Tool-Status": "committed",
                    "X-LCB-Tool-Idempotency": "committed",
                    "Content-Type": "application/json",
                }
            except Exception as exc:
                err_msg = str(exc)
                logger.exception("Error executing MCP tool %s", tool_name)
                self.tool_ledger.mark_failed(tool_call_id, err_msg)
                return 200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Error: {err_msg}"}],
                        "isError": True,
                        "_lcb_status": "failed",
                    },
                }, {
                    "X-LCB-Tool-Status": "failed",
                    "Content-Type": "application/json",
                }
        else:
            # Default dispatch receipt
            receipt = {
                "status": "committed",
                "output": json.dumps({"acknowledged": True, "tool": tool_name, "arguments": arguments}),
                "is_error": False,
                "timestamp": time.time(),
            }
            self.tool_ledger.mark_committed(tool_call_id, receipt)
            return 200, {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": receipt["output"]}],
                    "isError": False,
                    "_lcb_status": "committed",
                    "_lcb_receipt": receipt,
                },
            }, {
                "X-LCB-Tool-Status": "committed",
                "X-LCB-Tool-Idempotency": "committed",
                "Content-Type": "application/json",
            }


DEFAULT_MCP_PROXY = MCPProxy()
