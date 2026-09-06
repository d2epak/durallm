"""Concrete Provider Adapters with Secure Header Auth."""

from __future__ import annotations

import json
import logging
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from llm_circuit_breaker._env import ALLOW_LOCAL_UPSTREAM_ENV, env_flag
from llm_circuit_breaker.capability.profile import Endpoint
from llm_circuit_breaker.errors import CircuitBreakerGatewayError, ConfigurationError
from llm_circuit_breaker.security.defense import MAX_PAYLOAD_BYTES, enforce_payload_limit, validate_upstream_url
from llm_circuit_breaker.protocol.anthropic import (
    anthropic_request_to_ir,
    ir_to_anthropic_request,
    ir_to_anthropic_response,
)
from llm_circuit_breaker.protocol.gemini import (
    gemini_response_to_ir,
    ir_to_gemini_request,
)
from llm_circuit_breaker.protocol.ir import (
    NormalizedRequest,
    NormalizedResponse,
)
from llm_circuit_breaker.protocol.openai import (
    ir_to_openai_request,
    ir_to_openai_response,
    openai_response_to_ir,
)
from llm_circuit_breaker.providers.base import (
    TRANSPORT_STATUS,
    PreparedRequest,
    ProviderAdapter,
    ProviderExecutionResult,
)

logger = logging.getLogger("llm_circuit_breaker.providers")


def _transport_kind(exc: BaseException) -> str:
    """Name the transport failure so it gets a distinct status instead of a blanket 599."""
    candidates = [getattr(exc, "reason", None), exc.__cause__, exc.__context__, exc]
    for c in candidates:
        if c is None:
            continue
        if isinstance(c, (socket.timeout, TimeoutError)):
            return "timeout"
        if isinstance(c, ssl.SSLError):
            return "tls"
        if isinstance(c, urllib.error.URLError):
            continue  # a wrapper; its reason was inspected first
        if isinstance(c, (ConnectionError, socket.gaierror, OSError)):
            return "connection"
    if "timed out" in str(exc).lower():
        return "timeout"
    return "unknown"


class BaseHTTPAdapter:
    """Standard-library HTTP executor with per-attempt timeout support."""

    def execute(
        self,
        prepared: PreparedRequest,
        timeout_seconds: float,
    ) -> ProviderExecutionResult:
        # SSRF defense at the network boundary; loopback upstreams (Ollama, LM Studio) are opt-in.
        validate_upstream_url(prepared.url, allow_localhost=env_flag(ALLOW_LOCAL_UPSTREAM_ENV))
        enforce_payload_limit(len(prepared.body_bytes))
        req = urllib.request.Request(
            url=prepared.url,
            data=prepared.body_bytes,
            headers=prepared.headers,
            method=prepared.method,
        )

        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                body = resp.read(MAX_PAYLOAD_BYTES + 1)  # never buffer an unbounded response
                duration = (time.monotonic() - start) * 1000.0
                headers = {k.lower(): v for k, v in resp.headers.items()}
                enforce_payload_limit(len(body))
                return ProviderExecutionResult(
                    status_code=resp.status,
                    headers=headers,
                    body=body,
                    duration_ms=duration,
                )
        except urllib.error.HTTPError as exc:
            duration = (time.monotonic() - start) * 1000.0
            try:
                body = exc.read()
            except Exception:
                body = b""
            headers = {k.lower(): v for k, v in exc.headers.items()} if hasattr(exc, "headers") else {}
            return ProviderExecutionResult(
                status_code=exc.code,
                headers=headers,
                body=body,
                duration_ms=duration,
            )
        except CircuitBreakerGatewayError:
            raise  # a policy refusal (payload ceiling), not a transport failure
        except Exception as exc:
            duration = (time.monotonic() - start) * 1000.0
            kind = _transport_kind(exc)
            return ProviderExecutionResult(
                status_code=TRANSPORT_STATUS[kind],
                headers={},
                body=f"transport_error:{kind}: {exc}".encode("utf-8"),
                duration_ms=duration,
                transport_error=kind,
            )


class OpenAICompatibleAdapter(BaseHTTPAdapter):
    """Adapter for OpenAI, Cerebras, Groq, OpenRouter, Mistral, and NVIDIA NIM."""

    def __init__(self, provider_id: str = "openai"):
        self.provider_id = provider_id

    def prepare_request(
        self,
        endpoint: Endpoint,
        request: NormalizedRequest,
        api_key: Optional[str] = None,
    ) -> PreparedRequest:
        payload = ir_to_openai_request(request, endpoint.model)
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "llm-circuit-breaker/0.2.0",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if endpoint.headers:
            headers.update(endpoint.headers)

        base = endpoint.base_url.rstrip("/")
        url = f"{base}/chat/completions" if not base.endswith("/chat/completions") else base

        return PreparedRequest(url=url, headers=headers, body_bytes=body_bytes)

    def normalize_response(
        self,
        endpoint: Endpoint,
        result: ProviderExecutionResult,
    ) -> NormalizedResponse:
        raw_json = json.loads(result.body.decode("utf-8"))
        return openai_response_to_ir(raw_json)


class AnthropicAdapter(BaseHTTPAdapter):
    """Adapter for native Anthropic Messages API."""

    def __init__(self, provider_id: str = "anthropic"):
        self.provider_id = provider_id

    def prepare_request(
        self,
        endpoint: Endpoint,
        request: NormalizedRequest,
        api_key: Optional[str] = None,
    ) -> PreparedRequest:
        payload = ir_to_anthropic_request(request, endpoint.model)
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "User-Agent": "llm-circuit-breaker/0.2.0",
        }
        if api_key:
            headers["x-api-key"] = api_key
        if endpoint.headers:
            headers.update(endpoint.headers)

        base = endpoint.base_url.rstrip("/")
        url = f"{base}/messages" if not base.endswith("/messages") else base

        return PreparedRequest(url=url, headers=headers, body_bytes=body_bytes)

    def normalize_response(
        self,
        endpoint: Endpoint,
        result: ProviderExecutionResult,
    ) -> NormalizedResponse:
        raw_json = json.loads(result.body.decode("utf-8"))
        # Anthropic message response to IR
        text_parts: List[str] = []
        reasoning = None
        signature = None
        tool_calls = []
        for b in raw_json.get("content", []):
            if b.get("type") == "text":
                text_parts.append(b.get("text", ""))  # keep every block, not just the last
            elif b.get("type") == "thinking":
                reasoning = b.get("thinking", "")
                signature = b.get("signature")
            elif b.get("type") == "tool_use":
                from llm_circuit_breaker.protocol.ir import NormalizedToolCall
                tool_calls.append(
                    NormalizedToolCall(
                        id=b.get("id", ""),
                        name=b.get("name", ""),
                        arguments=b.get("input", {}),
                    )
                )

        usage = raw_json.get("usage", {})
        stop_reason = raw_json.get("stop_reason", "end_turn")
        finish_map = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length"}

        return NormalizedResponse(
            response_id=raw_json.get("id", ""),
            model=endpoint.model,
            content="".join(text_parts),
            reasoning_content=reasoning,
            reasoning_signature=signature,
            tool_calls=tool_calls,
            finish_reason=finish_map.get(stop_reason, "stop"),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            raw_response=raw_json,
        )


class GeminiAdapter(BaseHTTPAdapter):
    """
    Adapter for Google AI Studio / Gemini REST generateContent API.
    SECURE: Uses 'x-goog-api-key' header instead of putting keys into URL query strings.
    """

    def __init__(self, provider_id: str = "gemini"):
        self.provider_id = provider_id

    def prepare_request(
        self,
        endpoint: Endpoint,
        request: NormalizedRequest,
        api_key: Optional[str] = None,
    ) -> PreparedRequest:
        payload = ir_to_gemini_request(request, endpoint.model)
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "llm-circuit-breaker/0.2.0",
        }
        # SECURE AUTH IN HEADER, NOT URL QUERY PARAMETER!
        if api_key:
            headers["x-goog-api-key"] = api_key

        if endpoint.headers:
            headers.update(endpoint.headers)

        base = endpoint.base_url.rstrip("/")
        url = f"{base}/models/{endpoint.model}:generateContent"

        return PreparedRequest(url=url, headers=headers, body_bytes=body_bytes)

    def normalize_response(
        self,
        endpoint: Endpoint,
        result: ProviderExecutionResult,
    ) -> NormalizedResponse:
        raw_json = json.loads(result.body.decode("utf-8"))
        return gemini_response_to_ir(raw_json, endpoint.model)


class ProviderAdapterRegistry:
    """Registry providing the right adapter for each provider."""

    def __init__(self):
        self._adapters: Dict[str, ProviderAdapter] = {
            "openai": OpenAICompatibleAdapter("openai"),
            "groq": OpenAICompatibleAdapter("groq"),
            "cerebras": OpenAICompatibleAdapter("cerebras"),
            "openrouter": OpenAICompatibleAdapter("openrouter"),
            "mistral": OpenAICompatibleAdapter("mistral"),
            "nvidia": OpenAICompatibleAdapter("nvidia"),
            "anthropic": AnthropicAdapter("anthropic"),
            "gemini": GeminiAdapter("gemini"),
        }

    def get_adapter(self, provider: str, protocol: Optional[str] = None) -> ProviderAdapter:
        """Adapter for a known provider, else for the endpoint's declared wire protocol.

        Unknown providers used to fall through to the OpenAI adapter silently.
        """
        p_clean = provider.lower()
        if p_clean in self._adapters:
            return self._adapters[p_clean]
        if protocol and protocol.lower() in ("openai", "anthropic", "gemini"):
            return self._adapters[protocol.lower()]
        raise ConfigurationError(
            f"No adapter for provider '{provider}'. Known providers: {sorted(self._adapters)}; "
            f"otherwise set Endpoint.protocol to 'openai', 'anthropic' or 'gemini'."
        )


DEFAULT_ADAPTER_REGISTRY = ProviderAdapterRegistry()
