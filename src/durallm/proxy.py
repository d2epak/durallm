"""Local Multi-Protocol Failover Gateway & Proxy.

Provides drop-in local endpoints for:
- Claude Code: Anthropic /v1/messages & /v1/messages/count_tokens
- Hermes Agent & OpenClaw: OpenAI /v1/chat/completions & /v1/models
- Diagnostic Health: /health

Operates with zero mandatory external dependencies via Python's standard library
ThreadingHTTPServer, while optionally providing an ASGI FastAPI app if installed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from durallm.continuation import ContinuationEvent, ContinuationRequest, SQLiteContinuationStore
from durallm.errors import (
    CircuitBreakerGatewayError,
    ConfigurationError,
    ContinuationProtocolError,
    ToolOperationProtocolError,
)
from durallm.execution.executor import GatewayExecutor
from durallm.gateway import ProxyGateway, http_error_for
from durallm.mcp.proxy import MCPProxy
from durallm.observability.logger import DEFAULT_STRUCTURED_LOGGER
from durallm.pools import POOL_MANAGER
from durallm.protocol.anthropic import anthropic_request_to_ir, ir_to_anthropic_response
from durallm.protocol.openai import ir_to_openai_response, openai_request_to_ir
from durallm.pruner import estimate_tokens
from durallm.router import UniversalFailoverRouter
from durallm.security.defense import enforce_payload_limit
from durallm.storage import SQLitePersistenceStore, SQLiteToolExecutionLedger
from durallm.streaming import StreamingMode, interruption_sse

logger = logging.getLogger("durallm.proxy")

# Discovery (network) and dotfile key scanning are opt-in via LLM_BREAKER_AUTO_DISCOVER /
# LLM_BREAKER_SCAN_DOTFILES, or `llm-proxy --discover`; importing this module makes no network call.
# The V1 router is kept as the configuration façade (LLM_ALLOWED_PROVIDERS, configured fallbacks);
# requests are served by the V3 executor through GATEWAY.
ROUTER = UniversalFailoverRouter()


def build_proxy_gateway(storage_path: Optional[str] = None) -> ProxyGateway:
    """Create a proxy, opting into durable ACP/tool/attempt state when configured.

    ``LLM_BREAKER_STATE_DB`` is intentionally opt-in so importing the package
    remains side-effect free. A file-backed SQLite path enables WAL-backed ACP
    sessions, write-ahead provider attempts and tool-operation receipts.
    """
    path = storage_path if storage_path is not None else os.environ.get("LLM_BREAKER_STATE_DB")
    if not path:
        return ProxyGateway(pool_manager=POOL_MANAGER)
    store = SQLitePersistenceStore(path)
    executor = GatewayExecutor(
        tool_ledger=SQLiteToolExecutionLedger(store),
        attempt_store=store,
    )
    return ProxyGateway(
        pool_manager=POOL_MANAGER,
        executor=executor,
        continuation_store=SQLiteContinuationStore(store),
        persistence_store=store,
    )


GATEWAY = build_proxy_gateway()


def pool_for_model(requested_model: str) -> str:
    m = (requested_model or "").lower()
    return "coding" if m.startswith("claude-") or any(k in m for k in ["code", "claude", "coder"]) else "general_agent"


def _attach_tool_operation_metadata(body: Dict[str, Any], response: Any) -> Dict[str, Any]:
    """Expose local ledger IDs without changing the provider-native tool blocks."""
    operations = [
        {
            "tool_call_id": call.id,
            "ledger_call_id": call.metadata["ledger_call_id"],
            "status": "replayed" if call.metadata.get("replayed") else "validated",
        }
        for call in response.tool_calls
        if call.metadata.get("ledger_call_id")
    ]
    if operations:
        body["lcb_tool_operations"] = operations
    return body


def build_failover_telemetry(
    requested_model: str,
    decision: Any,
    ledger: Any,
) -> Tuple[Dict[str, str], Optional[Dict[str, Any]]]:
    """Construct failover telemetry headers and response metadata."""
    selected_ep = decision.selected_endpoint if decision else None
    active_model = selected_ep.model if selected_ep else "unknown"
    selected_id = selected_ep.id if selected_ep else "unknown"

    headers = {
        "X-LCB-Requested-Model": requested_model,
        "X-LCB-Active-Model": active_model,
        "X-LCB-Selected-Endpoint": selected_id,
    }

    is_virtual_alias = (
        requested_model in ("auto-coding-agent", "hermes-default", "openclaw-default", "default")
        or (requested_model or "").lower().startswith("claude-")
    )
    has_failover = (
        (ledger and getattr(ledger, "fallback_count", 0) > 0)
        or any(not getattr(c, "eligible", True) for c in getattr(decision, "evaluated_candidates", []))
        or (not is_virtual_alias and active_model != requested_model)
    )

    if has_failover:
        reason = getattr(decision, "fallback_reason", None) or "upstream_failover"
        headers["X-LCB-Failover"] = "true"
        headers["X-LCB-Failover-Reason"] = reason
        metadata = {
            "triggered": True,
            "requested_model": requested_model,
            "active_model": active_model,
            "selected_endpoint": selected_id,
            "failover_reason": reason,
            "attempts": getattr(ledger, "total_attempts", 1) if ledger else 1,
        }
        return headers, metadata

    return headers, None


def serve_messages(
    body: Dict[str, Any],
    continuation: Optional[ContinuationRequest] = None,
    logical_operation_id: Optional[str] = None,
):
    """Anthropic /v1/messages body -> (status, response dict, selected endpoint, ACP event, failover headers)."""
    requested_model = body.get("model", "auto-coding-agent")
    try:
        request = anthropic_request_to_ir(body)
        if logical_operation_id:
            request.request_id = logical_operation_id
        response, decision, ledger, event = GATEWAY.complete_turn(request, pool="coding", continuation=continuation)
    except CircuitBreakerGatewayError as exc:
        status, kind = http_error_for(exc)
        return status, {"type": "error", "error": {"type": kind, "message": str(exc)}}, None, None, {}

    telemetry_headers, failover_meta = build_failover_telemetry(requested_model, decision, ledger)
    resp_dict = ir_to_anthropic_response(response, requested_model)
    if failover_meta:
        resp_dict["lcb_failover"] = failover_meta
    return 200, _attach_tool_operation_metadata(resp_dict, response), decision.selected_endpoint, event, telemetry_headers


def serve_chat_completions(
    body: Dict[str, Any],
    continuation: Optional[ContinuationRequest] = None,
    logical_operation_id: Optional[str] = None,
):
    """OpenAI /v1/chat/completions body -> (status, response dict, selected endpoint, ACP event, failover headers)."""
    requested_model = body.get("model", "hermes-default")
    try:
        request = openai_request_to_ir(body)
        if logical_operation_id:
            request.request_id = logical_operation_id
        response, decision, ledger, event = GATEWAY.complete_turn(
            request, pool=pool_for_model(requested_model), continuation=continuation
        )
    except CircuitBreakerGatewayError as exc:
        status, kind = http_error_for(exc)
        return status, {"error": {"type": kind, "message": str(exc)}}, None, None, {}

    telemetry_headers, failover_meta = build_failover_telemetry(requested_model, decision, ledger)
    resp_dict = ir_to_openai_response(response, requested_model)
    if failover_meta:
        resp_dict["lcb_failover"] = failover_meta
    return 200, _attach_tool_operation_metadata(resp_dict, response), decision.selected_endpoint, event, telemetry_headers


def continuation_request_from_headers(headers: Any) -> Optional[ContinuationRequest]:
    """Decode an opt-in ACP request; normal OpenAI/Anthropic calls stay best-effort."""
    version = headers.get("X-LCB-ACP-Version")
    acp_values = (
        "X-LCB-Session-Id",
        "X-LCB-Parent-Checkpoint-Digest",
        "X-LCB-State-Digest",
    )
    if version is None:
        if any(headers.get(name) for name in acp_values):
            raise ContinuationProtocolError("ACP headers require X-LCB-ACP-Version", status_code=400)
        return None
    return ContinuationRequest(
        protocol_version=version,
        session_id=headers.get("X-LCB-Session-Id") or None,
        parent_checkpoint_digest=headers.get("X-LCB-Parent-Checkpoint-Digest") or None,
        state_digest=headers.get("X-LCB-State-Digest") or "",
    )


def logical_operation_id_from_headers(headers: Any) -> Optional[str]:
    """Read the caller's stable tool-operation identity, if it supplied one."""
    value = headers.get("X-LCB-Operation-Id")
    if not value:
        return None
    if len(value) > 256 or "\r" in value or "\n" in value:
        raise ToolOperationProtocolError("Invalid X-LCB-Operation-Id", status_code=400)
    return value


def streaming_mode_from_request(body: Dict[str, Any], headers: Any) -> StreamingMode:
    """Read the opt-in streaming contract without changing provider schemas.

    ``stream: true`` alone remains atomic-buffered for compatibility and for
    full tool validation. Native pass-through is an explicit contract because
    it cannot safely switch models after its first visible bytes.
    """
    raw = headers.get("X-LCB-Streaming-Mode") or body.get("lcb_stream_mode") or StreamingMode.ATOMIC_BUFFERED.value
    try:
        return StreamingMode(str(raw).lower())
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in StreamingMode)
        raise ConfigurationError(f"Invalid streaming mode {raw!r}; choose one of: {allowed}") from exc


def open_native_messages_stream(body: Dict[str, Any], logical_operation_id: Optional[str] = None):
    request = anthropic_request_to_ir(body)
    if logical_operation_id:
        request.request_id = logical_operation_id
    return GATEWAY.open_native_stream(request, pool="coding", client_protocol="anthropic")


def open_native_chat_stream(body: Dict[str, Any], logical_operation_id: Optional[str] = None):
    requested_model = body.get("model", "hermes-default")
    request = openai_request_to_ir(body)
    if logical_operation_id:
        request.request_id = logical_operation_id
    return GATEWAY.open_native_stream(
        request,
        pool=pool_for_model(requested_model),
        client_protocol="openai",
    )


def native_stream_chunks(native_stream: Any, protocol: str):
    """Yield provider bytes for ASGI while preserving the no-splice boundary."""
    completed = False
    try:
        for chunk in native_stream.iter_bytes():
            yield chunk
        completed = True
    except Exception as exc:
        yield interruption_sse(protocol, str(exc), native_stream.endpoint.id)
    finally:
        if not completed:
            native_stream.cancel("ASGI downstream stream ended")


def update_tool_operation(body: Dict[str, Any], action: str) -> Dict[str, Any]:
    """Apply a cooperating tool-runner state transition through the gateway."""
    tool_call_id = body.get("ledger_call_id")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise ToolOperationProtocolError("ledger_call_id is required", status_code=400)
    ledger = GATEWAY.executor.tool_ledger
    if ledger.get_record(tool_call_id) is None:
        raise ToolOperationProtocolError("Unknown ledger_call_id", status_code=404)
    if action == "submit":
        ledger.mark_submitted(tool_call_id)
    elif action == "commit":
        receipt = body.get("receipt")
        if not isinstance(receipt, dict):
            raise ToolOperationProtocolError("commit requires an object receipt", status_code=400)
        ledger.mark_committed(tool_call_id, receipt)
    elif action == "indeterminate":
        ledger.mark_indeterminate(tool_call_id, str(body.get("reason", "Lost tool acknowledgement")))
    else:
        raise ToolOperationProtocolError(f"Unsupported tool operation action: {action}", status_code=404)
    record = ledger.get_record(tool_call_id)
    if record is None:
        raise ToolOperationProtocolError("Tool operation disappeared", status_code=409)
    return {"tool_operation": record.to_dict()}


def continuation_headers(event: Optional[ContinuationEvent]) -> Dict[str, str]:
    """Expose ACP metadata in headers without changing provider-native bodies."""
    if event is None:
        return {}
    result = {
        "X-LCB-ACP-Version": event.protocol_version,
        "X-LCB-Continuation-Event": event.event_type,
        "X-LCB-Session-Id": event.session_id,
        "X-LCB-Turn-Id": event.turn_id,
        "X-LCB-Epoch": str(event.epoch),
        "X-LCB-Ack-Required": "true" if event.ack_required else "false",
    }
    if event.checkpoint is not None:
        result["X-LCB-Checkpoint-Id"] = event.checkpoint.checkpoint_id
        result["X-LCB-Checkpoint-Digest"] = event.checkpoint.gateway_digest
    return result


class CircuitBreakerGatewayHandler(BaseHTTPRequestHandler):
    """Zero-dependency HTTP request handler for Anthropic & OpenAI protocols."""

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Suppress default noisy access logs

    def handle_one_request(self) -> None:
        self._started = time.monotonic()
        self._route = None
        super().handle_one_request()

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        # Called by send_response for every reply (JSON, stream, or error): one redacted event each.
        route = getattr(self, "_route", None)
        started = getattr(self, "_started", None)
        DEFAULT_STRUCTURED_LOGGER.info(
            "proxy_response",
            request_id=self.headers.get("X-Request-Id") if getattr(self, "headers", None) else None,
            method=getattr(self, "command", None),
            path=urllib.parse.urlsplit(getattr(self, "path", "") or "").path,
            status=int(code) if str(code).isdigit() else code,
            duration_ms=round((time.monotonic() - started) * 1000.0, 1) if started else None,
            provider=route.provider if route else None,
            model=route.model if route else None,
        )

    def _send_json(self, status: int, data: Any, extra_headers: Optional[Dict[str, str]] = None) -> None:
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_HEAD(self) -> None:
        path = urllib.parse.urlsplit(self.path).path

        if path in ("/health", "/healthz", "/api/hello", "/free-gateway/health", "/metrics", "/admin/breakers", "/admin/canary/status", "/v1/models", "/models"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "close")
            self.end_headers()
            return

        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path

        if path in ("/health", "/healthz", "/free-gateway/health"):
            candidates_coding = POOL_MANAGER.get_candidate_routes("coding")
            candidates_agent = POOL_MANAGER.get_candidate_routes("general_agent")
            from durallm.breaker.registry import DEFAULT_BREAKER_REGISTRY
            breaker_snaps = {name: b.snapshot()["state"] for name, b in DEFAULT_BREAKER_REGISTRY.all().items()}
            self._send_json(200, {
                "status": "healthy",
                "ok": True,
                "engine": "durallm",
                "service": "claude-free-resilient",
                "gateway": "anthropic-messages-bridge",
                "version": "0.2.1",
                "pools": {
                    "coding": {
                        "active_candidates": len(candidates_coding),
                        "models": [f"{r.provider}/{r.model}" for r in candidates_coding]
                    },
                    "general_agent": {
                        "active_candidates": len(candidates_agent),
                        "models": [f"{r.provider}/{r.model}" for r in candidates_agent]
                    }
                },
                "circuit_breakers": breaker_snaps,
                "active_keys": list(POOL_MANAGER.keys.keys()),
                "cooldowns": {f"{k[0]}:{k[1]}": max(0, int(v - time.monotonic())) for k, v in POOL_MANAGER.cooldowns.items()}
            })
            return

        if path == "/api/hello":
            self._send_json(200, {
                "ok": True,
                "status": "healthy",
                "service": "claude-free-resilient",
                "gateway": "anthropic-messages-bridge",
                "engine": "durallm",
            })
            return

        if path == "/metrics":
            from durallm.breaker.registry import DEFAULT_BREAKER_REGISTRY
            from durallm.health.telemetry import DEFAULT_HEALTH_STORE
            all_b = {k: b.snapshot() for k, b in DEFAULT_BREAKER_REGISTRY.all().items()}
            all_h = {k: s.__dict__ for k, s in DEFAULT_HEALTH_STORE.all_snapshots().items()}
            self._send_json(200, {
                "circuit_breakers": all_b,
                "health_telemetry": all_h,
            })
            return

        if path == "/admin/breakers":
            from durallm.breaker.registry import DEFAULT_BREAKER_REGISTRY
            self._send_json(200, {
                "breakers": {k: b.snapshot() for k, b in DEFAULT_BREAKER_REGISTRY.all().items()}
            })
            return

        if path == "/admin/canary/status":
            from durallm.canary import DEFAULT_CANARY_SCHEDULER
            self._send_json(200, {
                "scheduler": DEFAULT_CANARY_SCHEDULER.status(),
                "quirks_ledger": DEFAULT_CANARY_SCHEDULER.prober.load_ledger(),
            })
            return


        if path in ("/v1/models", "/models"):
            # Provide virtual models for auto-configuration
            virtual_models = [
                {"id": "auto-coding-agent", "object": "model", "created": int(time.time()), "owned_by": "circuit-breaker"},
                {"id": "hermes-default", "object": "model", "created": int(time.time()), "owned_by": "circuit-breaker"},
                {"id": "openclaw-default", "object": "model", "created": int(time.time()), "owned_by": "circuit-breaker"},
                {"id": "claude-opus-5", "object": "model", "created": int(time.time()), "owned_by": "durallm-harness"},
                {"id": "claude-3-5-haiku", "object": "model", "created": int(time.time()), "owned_by": "durallm-harness"},
                {"id": "claude-3-5-haiku-20241022", "object": "model", "created": int(time.time()), "owned_by": "durallm-harness"},
                {"id": "claude-3-5-sonnet", "object": "model", "created": int(time.time()), "owned_by": "durallm-harness"},
                {"id": "claude-3-7-sonnet", "object": "model", "created": int(time.time()), "owned_by": "durallm-harness"},
            ]
            for r in POOL_MANAGER.coding_routes:
                virtual_models.append({"id": f"coding/{r.provider}/{r.model}", "object": "model", "created": int(time.time()), "owned_by": r.provider})
            for r in POOL_MANAGER.agent_routes:
                virtual_models.append({"id": f"agent/{r.provider}/{r.model}", "object": "model", "created": int(time.time()), "owned_by": r.provider})
            self._send_json(200, {"object": "list", "data": virtual_models})
            return

        self._send_json(404, {"error": {"message": f"Endpoint {path} not found"}})

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self._send_json(400, {"error": {"message": "Invalid Content-Length header"}})
            return
        try:
            enforce_payload_limit(content_length)  # checked before the body is read into memory
        except CircuitBreakerGatewayError as e:
            self._send_json(413, {"error": {"message": str(e)}})
            return
        raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"

        try:
            body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception as e:
            self._send_json(400, {"error": {"message": f"Malformed JSON body: {e}"}})
            return

        if path == "/admin/canary/run":
            from durallm.canary import DEFAULT_CANARY_SCHEDULER
            result = DEFAULT_CANARY_SCHEDULER.trigger_now()
            self._send_json(200, result)
            return

        if path == "/v1/continuations/ack":
            try:
                event = GATEWAY.acknowledge_continuation(
                    session_id=str(body.get("session_id", "")),
                    turn_id=str(body.get("turn_id", "")),
                    epoch=int(body.get("epoch")),
                    checkpoint_digest=str(body.get("checkpoint_digest", "")),
                )
            except (TypeError, ValueError, CircuitBreakerGatewayError) as exc:
                if isinstance(exc, CircuitBreakerGatewayError):
                    status, kind = http_error_for(exc)
                else:
                    status, kind = 400, "continuation_protocol_error"
                self._send_json(status, {"error": {"type": kind, "message": str(exc)}})
                return
            self._send_json(200, {"continuation": event.to_dict()}, continuation_headers(event))
            return

        tool_operation_actions = {
            "/v1/tool-operations/submit": "submit",
            "/v1/tool-operations/commit": "commit",
            "/v1/tool-operations/indeterminate": "indeterminate",
        }
        if path in tool_operation_actions:
            try:
                result = update_tool_operation(body, tool_operation_actions[path])
            except CircuitBreakerGatewayError as exc:
                status, kind = http_error_for(exc)
                self._send_json(status, {"error": {"type": kind, "message": str(exc)}})
                return
            except Exception as exc:
                self._send_json(409, {"error": {"type": "tool_operation_protocol_error", "message": str(exc)}})
                return
            self._send_json(200, result)
            return

        if path in ("/v1/mcp", "/mcp", "/v1/mcp/tools/call"):
            mcp_proxy = MCPProxy(tool_ledger=GATEWAY.executor.tool_ledger)
            req_headers = {k: v for k, v in self.headers.items()}
            status, resp, resp_headers = mcp_proxy.handle_json_rpc(body, headers=req_headers)
            self._send_json(status, resp, resp_headers)
            return

        try:
            continuation = continuation_request_from_headers(self.headers)
            logical_operation_id = logical_operation_id_from_headers(self.headers)
        except CircuitBreakerGatewayError as exc:
            status, kind = http_error_for(exc)
            self._send_json(status, {"error": {"type": kind, "message": str(exc)}})
            return

        # ------------------------------------------------------------------
        # 1. ANTHROPIC TOKEN COUNTING
        # ------------------------------------------------------------------
        if path in ("/v1/messages/count_tokens", "/messages/count_tokens"):
            est_tokens = estimate_tokens(body)
            self._send_json(200, {"input_tokens": est_tokens})
            return

        # ------------------------------------------------------------------
        # 2. ANTHROPIC MESSAGES (Claude Code)
        # ------------------------------------------------------------------
        if path in ("/v1/messages", "/messages"):
            try:
                streaming_mode = streaming_mode_from_request(body, self.headers)
            except CircuitBreakerGatewayError as exc:
                status, kind = http_error_for(exc)
                self._send_json(status, {"type": "error", "error": {"type": kind, "message": str(exc)}})
                return
            if body.get("stream", False) and streaming_mode == StreamingMode.TRUE_STREAMING:
                if continuation is not None:
                    self._send_json(409, {
                        "type": "error",
                        "error": {
                            "type": "continuation_protocol_error",
                            "message": "ACP turns require atomic_buffered streaming until checkpoint completion is stream-aware",
                        },
                    })
                    return
                try:
                    native_stream = open_native_messages_stream(body, logical_operation_id)
                except CircuitBreakerGatewayError as exc:
                    status, kind = http_error_for(exc)
                    self._send_json(status, {"type": "error", "error": {"type": kind, "message": str(exc)}})
                    return
                self._route = native_stream.endpoint
                self._emit_native_stream(native_stream, "anthropic")
                return
            status, anthropic_resp, self._route, event, telemetry_headers = serve_messages(body, continuation, logical_operation_id)
            headers = {**continuation_headers(event), **telemetry_headers}
            if status != 200:
                self._send_json(status, anthropic_resp, headers)
            elif body.get("stream", False):
                self._emit_synthetic_anthropic_stream(anthropic_resp, headers)
            else:
                self._send_json(200, anthropic_resp, headers)
            return

        # ------------------------------------------------------------------
        # 3. OPENAI CHAT COMPLETIONS (Hermes Agent, OpenClaw, Cursor)
        # ------------------------------------------------------------------
        if path in ("/v1/chat/completions", "/chat/completions"):
            try:
                streaming_mode = streaming_mode_from_request(body, self.headers)
            except CircuitBreakerGatewayError as exc:
                status, kind = http_error_for(exc)
                self._send_json(status, {"error": {"type": kind, "message": str(exc)}})
                return
            if body.get("stream", False) and streaming_mode == StreamingMode.TRUE_STREAMING:
                if continuation is not None:
                    self._send_json(409, {
                        "error": {
                            "type": "continuation_protocol_error",
                            "message": "ACP turns require atomic_buffered streaming until checkpoint completion is stream-aware",
                        },
                    })
                    return
                try:
                    native_stream = open_native_chat_stream(body, logical_operation_id)
                except CircuitBreakerGatewayError as exc:
                    status, kind = http_error_for(exc)
                    self._send_json(status, {"error": {"type": kind, "message": str(exc)}})
                    return
                self._route = native_stream.endpoint
                self._emit_native_stream(native_stream, "openai")
                return
            status, openai_resp, self._route, event, telemetry_headers = serve_chat_completions(body, continuation, logical_operation_id)
            headers = {**continuation_headers(event), **telemetry_headers}
            if status == 200 and body.get("stream", False):
                self._emit_synthetic_openai_stream(openai_resp, body.get("model", "hermes-default"), headers)
            else:
                self._send_json(status, openai_resp, headers)
            return

        self._send_json(404, {"error": {"message": f"POST endpoint {path} not found"}})

    def _emit_native_stream(self, native_stream: Any, protocol: str) -> None:
        """Relay raw provider bytes; never switch model after visibility begins."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-LCB-Streaming-Mode", StreamingMode.TRUE_STREAMING.value)
        self.send_header("X-LCB-Selected-Endpoint", native_stream.endpoint.id)
        self.end_headers()

        visible = False
        try:
            for chunk in native_stream.iter_bytes():
                self.wfile.write(chunk)
                self.wfile.flush()
                visible = True
        except (BrokenPipeError, ConnectionResetError):
            native_stream.cancel("downstream client disconnected")
        except Exception as exc:
            # The first chunk was prefetched before sending headers; every
            # error reaching here is necessarily after the selected provider
            # became visible. Do not route it to another provider.
            if visible:
                try:
                    self.wfile.write(interruption_sse(protocol, str(exc), native_stream.endpoint.id))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    native_stream.cancel("downstream client disconnected")
            else:
                native_stream.cancel("native stream failed before downstream visibility")

    def _emit_synthetic_anthropic_stream(
        self, anthropic_resp: Dict[str, Any], extra_headers: Optional[Dict[str, str]] = None
    ) -> None:
        """Deliver clean synthetic Anthropic SSE events to Claude Code."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()

        def send_event(event_type: str, data: Dict[str, Any]) -> None:
            payload = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            try:
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
            except BrokenPipeError:
                pass

        msg_id = anthropic_resp.get("id", "msg_synthetic")
        model = anthropic_resp.get("model", "auto-coding-agent")
        usage = anthropic_resp.get("usage", {})

        # 1. message_start
        send_event("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": usage.get("input_tokens", 0), "output_tokens": 1}
            }
        })

        # 2. Content blocks
        blocks = anthropic_resp.get("content", [])
        for idx, b in enumerate(blocks):
            btype = b.get("type")
            if btype == "thinking":
                send_event("content_block_start", {"type": "content_block_start", "index": idx, "content_block": {"type": "thinking", "thinking": ""}})
                send_event("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": {"type": "thinking_delta", "thinking": b.get("thinking", "")}})
                if b.get("signature"):
                    send_event("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": {"type": "signature_delta", "signature": b["signature"]}})
                send_event("content_block_stop", {"type": "content_block_stop", "index": idx})
            elif btype == "text":
                send_event("content_block_start", {"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}})
                send_event("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": b.get("text", "")}})
                send_event("content_block_stop", {"type": "content_block_stop", "index": idx})
            elif btype == "tool_use":
                send_event("content_block_start", {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {"type": "tool_use", "id": b.get("id"), "name": b.get("name"), "input": {}}
                })
                json_str = json.dumps(b.get("input", {}), ensure_ascii=False)
                send_event("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": {"type": "input_json_delta", "partial_json": json_str}})
                send_event("content_block_stop", {"type": "content_block_stop", "index": idx})

        # 3. message_delta & message_stop
        send_event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": anthropic_resp.get("stop_reason", "end_turn"), "stop_sequence": None},
            "usage": {"output_tokens": usage.get("output_tokens", 10)}
        })
        send_event("message_stop", {"type": "message_stop"})

    def _emit_synthetic_openai_stream(
        self, openai_resp: Dict[str, Any], model: str, extra_headers: Optional[Dict[str, str]] = None
    ) -> None:
        """Deliver standard OpenAI SSE chunks."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()

        choices = openai_resp.get("choices", [])
        msg = choices[0].get("message", {}) if choices else {}

        chunk = {
            "id": openai_resp.get("id", "chatcmpl-stream"),
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": msg.get("content"),
                    "tool_calls": msg.get("tool_calls"),
                },
                "finish_reason": choices[0].get("finish_reason", "stop") if choices else "stop"
            }]
        }
        try:
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode("utf-8"))
            self.wfile.flush()
        except BrokenPipeError:
            pass


def start_proxy_server(host: str = "127.0.0.1", port: int = 4001) -> ThreadingHTTPServer:
    """Create and bind the standard-library gateway server.

    The returned server is bound but NOT serving. Call ``serve_forever()``
    on it (or run it in a thread) to accept requests; ``main()`` does this.
    Pass ``port=0`` to bind an ephemeral port and read it back from
    ``server.server_address[1]``.
    """
    server = ThreadingHTTPServer((host, port), CircuitBreakerGatewayHandler)
    logger.info("⚡ LLM Circuit Breaker Gateway bound to http://%s:%d (call serve_forever() to start)", host, server.server_address[1])
    return server


# Optional FastAPI / ASGI App for ASGI deployments
def create_proxy_app():
    """Optional ASGI application for FastAPI/Uvicorn users."""
    try:
        from fastapi import FastAPI, Request, Response
        from fastapi.responses import StreamingResponse
    except ImportError:
        raise ImportError("FastAPI is optional. Install with: pip install 'durallm[proxy]'")

    app = FastAPI(title="LLM Circuit Breaker Gateway", version="0.2.1")

    @app.get("/health")
    async def health():
        return {"status": "healthy", "engine": "durallm"}

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(req: Request):
        body = await req.json()
        return {"input_tokens": estimate_tokens(body)}

    @app.post("/v1/messages")
    async def messages(req: Request):
        body = await req.json()
        try:
            continuation = continuation_request_from_headers(req.headers)
            logical_operation_id = logical_operation_id_from_headers(req.headers)
        except CircuitBreakerGatewayError as exc:
            status, kind = http_error_for(exc)
            return Response(
                content=json.dumps({"type": "error", "error": {"type": kind, "message": str(exc)}}),
                status_code=status,
                media_type="application/json",
            )
        try:
            streaming_mode = streaming_mode_from_request(body, req.headers)
        except CircuitBreakerGatewayError as exc:
            status, kind = http_error_for(exc)
            return Response(
                content=json.dumps({"type": "error", "error": {"type": kind, "message": str(exc)}}),
                status_code=status,
                media_type="application/json",
            )
        if body.get("stream", False) and streaming_mode == StreamingMode.TRUE_STREAMING:
            if continuation is not None:
                return Response(
                    content=json.dumps({"type": "error", "error": {
                        "type": "continuation_protocol_error",
                        "message": "ACP turns require atomic_buffered streaming until checkpoint completion is stream-aware",
                    }}),
                    status_code=409,
                    media_type="application/json",
                )
            try:
                native_stream = open_native_messages_stream(body, logical_operation_id)
            except CircuitBreakerGatewayError as exc:
                status, kind = http_error_for(exc)
                return Response(
                    content=json.dumps({"type": "error", "error": {"type": kind, "message": str(exc)}}),
                    status_code=status,
                    media_type="application/json",
                )
            return StreamingResponse(
                native_stream_chunks(native_stream, "anthropic"),
                media_type="text/event-stream",
                headers={
                    "X-LCB-Streaming-Mode": StreamingMode.TRUE_STREAMING.value,
                    "X-LCB-Selected-Endpoint": native_stream.endpoint.id,
                },
            )
        status, anthropic_resp, _, event, telemetry_headers = serve_messages(body, continuation, logical_operation_id)
        return Response(
            content=json.dumps(anthropic_resp),
            status_code=status,
            media_type="application/json",
            headers={**continuation_headers(event), **telemetry_headers},
        )

    @app.post("/v1/chat/completions")
    async def completions(req: Request):
        body = await req.json()
        try:
            continuation = continuation_request_from_headers(req.headers)
            logical_operation_id = logical_operation_id_from_headers(req.headers)
        except CircuitBreakerGatewayError as exc:
            status, kind = http_error_for(exc)
            return Response(
                content=json.dumps({"error": {"type": kind, "message": str(exc)}}),
                status_code=status,
                media_type="application/json",
            )
        try:
            streaming_mode = streaming_mode_from_request(body, req.headers)
        except CircuitBreakerGatewayError as exc:
            status, kind = http_error_for(exc)
            return Response(
                content=json.dumps({"error": {"type": kind, "message": str(exc)}}),
                status_code=status,
                media_type="application/json",
            )
        if body.get("stream", False) and streaming_mode == StreamingMode.TRUE_STREAMING:
            if continuation is not None:
                return Response(
                    content=json.dumps({"error": {
                        "type": "continuation_protocol_error",
                        "message": "ACP turns require atomic_buffered streaming until checkpoint completion is stream-aware",
                    }}),
                    status_code=409,
                    media_type="application/json",
                )
            try:
                native_stream = open_native_chat_stream(body, logical_operation_id)
            except CircuitBreakerGatewayError as exc:
                status, kind = http_error_for(exc)
                return Response(
                    content=json.dumps({"error": {"type": kind, "message": str(exc)}}),
                    status_code=status,
                    media_type="application/json",
                )
            return StreamingResponse(
                native_stream_chunks(native_stream, "openai"),
                media_type="text/event-stream",
                headers={
                    "X-LCB-Streaming-Mode": StreamingMode.TRUE_STREAMING.value,
                    "X-LCB-Selected-Endpoint": native_stream.endpoint.id,
                },
            )
        status, openai_resp, _, event, telemetry_headers = serve_chat_completions(body, continuation, logical_operation_id)
        return Response(
            content=json.dumps(openai_resp),
            status_code=status,
            media_type="application/json",
            headers={**continuation_headers(event), **telemetry_headers},
        )

    @app.post("/v1/continuations/ack")
    async def acknowledge_continuation(req: Request):
        body = await req.json()
        try:
            event = GATEWAY.acknowledge_continuation(
                session_id=str(body.get("session_id", "")),
                turn_id=str(body.get("turn_id", "")),
                epoch=int(body.get("epoch")),
                checkpoint_digest=str(body.get("checkpoint_digest", "")),
            )
        except (TypeError, ValueError, CircuitBreakerGatewayError) as exc:
            if isinstance(exc, CircuitBreakerGatewayError):
                status, kind = http_error_for(exc)
            else:
                status, kind = 400, "continuation_protocol_error"
            return Response(
                content=json.dumps({"error": {"type": kind, "message": str(exc)}}),
                status_code=status,
                media_type="application/json",
            )
        return Response(
            content=json.dumps({"continuation": event.to_dict()}),
            status_code=200,
            media_type="application/json",
            headers=continuation_headers(event),
        )

    @app.post("/v1/tool-operations/{action}")
    async def tool_operation(req: Request, action: str):
        body = await req.json()
        try:
            result = update_tool_operation(body, action)
        except CircuitBreakerGatewayError as exc:
            status, kind = http_error_for(exc)
            return Response(
                content=json.dumps({"error": {"type": kind, "message": str(exc)}}),
                status_code=status,
                media_type="application/json",
            )
        except Exception as exc:
            return Response(
                content=json.dumps({"error": {"type": "tool_operation_protocol_error", "message": str(exc)}}),
                status_code=409,
                media_type="application/json",
            )
        return Response(content=json.dumps(result), status_code=200, media_type="application/json")

    @app.post("/v1/mcp")
    @app.post("/mcp")
    @app.post("/v1/mcp/tools/call")
    async def mcp_endpoint(req: Request):
        body = await req.json()
        mcp_proxy = MCPProxy(tool_ledger=GATEWAY.executor.tool_ledger)
        status, resp, resp_headers = mcp_proxy.handle_json_rpc(body, headers=dict(req.headers))
        return Response(
            content=json.dumps(resp),
            status_code=status,
            media_type="application/json",
            headers=resp_headers,
        )

    return app


def main():
    parser = argparse.ArgumentParser(description="Run local LLM Circuit Breaker Gateway")
    parser.add_argument("--port", type=int, default=4001, help="Gateway port (default: 4001)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--discover", action="store_true",
                        help="Fetch free OpenRouter models at startup (network call; off by default)")
    parser.add_argument("--canary", action="store_true",
                        help="Start autonomous 1:00 AM UK time canary probe scheduler")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    # Synchronize pool manager with persistent quirks ledger
    POOL_MANAGER.load_from_quirks_ledger()

    print("\n" + "=" * 65)
    print("  ⚡ LLM CIRCUIT BREAKER MULTI-AGENT GATEWAY ONLINE")
    print(f"  - Server Address: http://{args.host}:{args.port}")
    print(f"  - Claude Code (Coding Pool): http://{args.host}:{args.port}/v1/messages")
    print(f"  - Hermes / OpenClaw (Agent Pool): http://{args.host}:{args.port}/v1/chat/completions")
    print(f"  - Canary Status: http://{args.host}:{args.port}/admin/canary/status")
    print(f"  - Health Diagnostics: http://{args.host}:{args.port}/health")
    print("=" * 65 + "\n")

    if args.discover:
        from durallm.discovery import register_discovered_models_to_pools
        register_discovered_models_to_pools()

    canary_enabled = args.canary or os.environ.get("DURALLM_ENABLE_CANARY", "").lower() in ("true", "1", "yes")
    if canary_enabled:
        from durallm.canary import DEFAULT_CANARY_SCHEDULER
        DEFAULT_CANARY_SCHEDULER.start(run_immediately=False)

    server = start_proxy_server(host=args.host, port=args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if canary_enabled:
            from durallm.canary import DEFAULT_CANARY_SCHEDULER
            DEFAULT_CANARY_SCHEDULER.stop()
        print("\nStopping LLM Circuit Breaker Gateway...")
        server.shutdown()
        server.server_close()



if __name__ == "__main__":
    main()
