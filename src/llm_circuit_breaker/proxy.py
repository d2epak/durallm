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
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from llm_circuit_breaker.classifier import classify_api_error
from llm_circuit_breaker.errors import CircuitBreakerGatewayError
from llm_circuit_breaker.observability.logger import DEFAULT_STRUCTURED_LOGGER
from llm_circuit_breaker.pools import POOL_MANAGER
from llm_circuit_breaker.pruner import estimate_tokens
from llm_circuit_breaker.router import UniversalFailoverRouter
from llm_circuit_breaker.security.defense import enforce_payload_limit
from llm_circuit_breaker.translators import (
    anthropic_to_openai_request,
    openai_to_anthropic_response,
)

logger = logging.getLogger("llm_circuit_breaker.proxy")

# Discovery (network) and dotfile key scanning are opt-in via LLM_BREAKER_AUTO_DISCOVER /
# LLM_BREAKER_SCAN_DOTFILES, or `llm-proxy --discover`; importing this module makes no network call.
ROUTER = UniversalFailoverRouter()


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

    def _send_json(self, status: int, data: Any) -> None:
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path

        if path in ("/health", "/healthz"):
            candidates_coding = POOL_MANAGER.get_candidate_routes("coding")
            candidates_agent = POOL_MANAGER.get_candidate_routes("general_agent")
            from llm_circuit_breaker.breaker.registry import DEFAULT_BREAKER_REGISTRY
            breaker_snaps = {name: b.snapshot()["state"] for name, b in DEFAULT_BREAKER_REGISTRY.all().items()}
            self._send_json(200, {
                "status": "healthy",
                "engine": "llm-circuit-breaker",
                "version": "0.2.0",
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

        if path == "/metrics":
            from llm_circuit_breaker.breaker.registry import DEFAULT_BREAKER_REGISTRY
            from llm_circuit_breaker.health.telemetry import DEFAULT_HEALTH_STORE
            all_b = {k: b.snapshot() for k, b in DEFAULT_BREAKER_REGISTRY.all().items()}
            all_h = {k: s.__dict__ for k, s in DEFAULT_HEALTH_STORE.all_snapshots().items()}
            self._send_json(200, {
                "circuit_breakers": all_b,
                "health_telemetry": all_h,
            })
            return

        if path == "/admin/breakers":
            from llm_circuit_breaker.breaker.registry import DEFAULT_BREAKER_REGISTRY
            self._send_json(200, {
                "breakers": {k: b.snapshot() for k, b in DEFAULT_BREAKER_REGISTRY.all().items()}
            })
            return

        if path in ("/v1/models", "/models"):
            # Provide virtual models for auto-configuration
            virtual_models = [
                {"id": "auto-coding-agent", "object": "model", "created": int(time.time()), "owned_by": "circuit-breaker"},
                {"id": "hermes-default", "object": "model", "created": int(time.time()), "owned_by": "circuit-breaker"},
                {"id": "openclaw-default", "object": "model", "created": int(time.time()), "owned_by": "circuit-breaker"},
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
            requested_model = body.get("model", "auto-coding-agent")
            openai_req = anthropic_to_openai_request(body, requested_model)
            status, openai_resp, route = ROUTER.dispatch("coding", openai_req, requested_model)
            self._route = route

            if status != 200:
                self._send_json(status, openai_resp)
                return

            anthropic_resp = openai_to_anthropic_response(openai_resp, requested_model)

            is_streaming = body.get("stream", False)
            if is_streaming:
                self._emit_synthetic_anthropic_stream(anthropic_resp)
            else:
                self._send_json(200, anthropic_resp)
            return

        # ------------------------------------------------------------------
        # 3. OPENAI CHAT COMPLETIONS (Hermes Agent, OpenClaw, Cursor)
        # ------------------------------------------------------------------
        if path in ("/v1/chat/completions", "/chat/completions"):
            requested_model = body.get("model", "hermes-default")
            # Determine pool: if model specifies coding or comes from Claude, use coding, else general_agent
            pool = "coding" if any(k in requested_model.lower() for k in ["code", "claude", "coder"]) else "general_agent"
            status, openai_resp, route = ROUTER.dispatch(pool, body, requested_model)
            self._route = route

            is_streaming = body.get("stream", False)
            if is_streaming and status == 200:
                self._emit_synthetic_openai_stream(openai_resp, requested_model)
            else:
                self._send_json(status, openai_resp)
            return

        self._send_json(404, {"error": {"message": f"POST endpoint {path} not found"}})

    def _emit_synthetic_anthropic_stream(self, anthropic_resp: Dict[str, Any]) -> None:
        """Deliver clean synthetic Anthropic SSE events to Claude Code."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
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

    def _emit_synthetic_openai_stream(self, openai_resp: Dict[str, Any], model: str) -> None:
        """Deliver standard OpenAI SSE chunks."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
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
    except ImportError:
        raise ImportError("FastAPI is optional. Install with: pip install 'llm-circuit-breaker[proxy]'")

    app = FastAPI(title="LLM Circuit Breaker Gateway", version="0.2.0")

    @app.get("/health")
    async def health():
        return {"status": "healthy", "engine": "llm-circuit-breaker"}

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(req: Request):
        body = await req.json()
        return {"input_tokens": estimate_tokens(body)}

    @app.post("/v1/messages")
    async def messages(req: Request):
        body = await req.json()
        requested_model = body.get("model", "auto-coding-agent")
        openai_req = anthropic_to_openai_request(body, requested_model)
        status, openai_resp, route = ROUTER.dispatch("coding", openai_req, requested_model)
        if status != 200:
            return Response(content=json.dumps(openai_resp), status_code=status, media_type="application/json")
        anthropic_resp = openai_to_anthropic_response(openai_resp, requested_model)
        return Response(content=json.dumps(anthropic_resp), status_code=200, media_type="application/json")

    @app.post("/v1/chat/completions")
    async def completions(req: Request):
        body = await req.json()
        requested_model = body.get("model", "hermes-default")
        pool = "coding" if any(k in requested_model.lower() for k in ["code", "claude", "coder"]) else "general_agent"
        status, openai_resp, route = ROUTER.dispatch(pool, body, requested_model)
        return Response(content=json.dumps(openai_resp), status_code=status, media_type="application/json")

    return app


def main():
    parser = argparse.ArgumentParser(description="Run local LLM Circuit Breaker Gateway")
    parser.add_argument("--port", type=int, default=4001, help="Gateway port (default: 4001)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--discover", action="store_true",
                        help="Fetch free OpenRouter models at startup (network call; off by default)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    print("\n" + "=" * 65)
    print("  ⚡ LLM CIRCUIT BREAKER MULTI-AGENT GATEWAY ONLINE")
    print(f"  - Server Address: http://{args.host}:{args.port}")
    print(f"  - Claude Code (Coding Pool): http://{args.host}:{args.port}/v1/messages")
    print(f"  - Hermes / OpenClaw (Agent Pool): http://{args.host}:{args.port}/v1/chat/completions")
    print(f"  - Health Diagnostics: http://{args.host}:{args.port}/health")
    print("=" * 65 + "\n")

    if args.discover:
        from llm_circuit_breaker.discovery import register_discovered_models_to_pools
        register_discovered_models_to_pools()

    server = start_proxy_server(host=args.host, port=args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping LLM Circuit Breaker Gateway...")
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
