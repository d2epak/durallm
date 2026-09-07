"""Concrete Provider Adapters with Secure Header Auth."""

from __future__ import annotations

import http.client
import json
import logging
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Dict, Iterator, List, Optional

from durallm._env import ALLOW_LOCAL_UPSTREAM_ENV, env_flag
from durallm.capability.profile import Endpoint
from durallm.errors import CircuitBreakerGatewayError, ConfigurationError
from durallm.protocol.anthropic import (
    ir_to_anthropic_request,
)
from durallm.protocol.gemini import (
    gemini_response_to_ir,
    ir_to_gemini_request,
)
from durallm.protocol.ir import (
    NormalizedRequest,
    NormalizedResponse,
)
from durallm.protocol.openai import (
    ir_to_openai_request,
    openai_response_to_ir,
)
from durallm.providers.base import (
    TRANSPORT_STATUS,
    PreparedRequest,
    ProviderAdapter,
    ProviderExecutionResult,
    ProviderStreamError,
    ProviderStreamResult,
    RequestCancellation,
    TransportTimeouts,
)
from durallm.security.defense import MAX_PAYLOAD_BYTES, enforce_payload_limit, validate_upstream_url

logger = logging.getLogger("durallm.providers")


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


class _HTTPResponseByteStream:
    """Bounded raw-byte reader for a single HTTP response.

    It deliberately never assembles SSE frames or response JSON.  A proxy can
    therefore relay provider-native events with a fixed-size read buffer and
    terminate the upstream connection as soon as its downstream client goes
    away.
    """

    def __init__(
        self,
        response: http.client.HTTPResponse,
        connection: http.client.HTTPConnection,
        timeouts: TransportTimeouts,
        cancellation: RequestCancellation,
        started: float,
        chunk_size: int = 16 * 1024,
    ) -> None:
        self._response = response
        self._connection = connection
        self._timeouts = timeouts
        self._cancellation = cancellation
        self._started = started
        self._chunk_size = chunk_size
        self._bytes_read = 0
        self._closed = False
        self._cancellation.add_callback(self.close)

    def _remaining_seconds(self) -> float:
        remaining = self._timeouts.total_timeout_ms - ((time.monotonic() - self._started) * 1000.0)
        if remaining <= 0:
            raise ProviderStreamError("total", "upstream stream exceeded its total deadline")
        return remaining / 1000.0

    def _set_idle_timeout(self) -> None:
        sock = getattr(self._connection, "sock", None)
        if sock is not None:
            sock.settimeout(min(self._timeouts.idle_timeout_seconds, self._remaining_seconds()))

    def iter_bytes(self) -> Iterator[bytes]:
        try:
            while True:
                if self._cancellation.is_cancelled:
                    raise ProviderStreamError("cancelled", self._cancellation.reason or "stream cancelled")
                self._set_idle_timeout()
                try:
                    # ``read(n)`` may wait for ``n`` bytes and destroys token
                    # latency for chunked SSE. ``read1`` returns a currently
                    # available network buffer while retaining HTTP decoding.
                    read_chunk = getattr(self._response, "read1", self._response.read)
                    chunk = read_chunk(self._chunk_size)
                except (socket.timeout, TimeoutError) as exc:
                    raise ProviderStreamError("idle", "upstream stream was idle beyond its deadline") from exc
                except (OSError, http.client.HTTPException) as exc:
                    phase = "cancelled" if self._cancellation.is_cancelled else "connection"
                    raise ProviderStreamError(phase, f"upstream stream read failed: {exc}") from exc
                if not chunk:
                    return
                self._bytes_read += len(chunk)
                if self._bytes_read > MAX_PAYLOAD_BYTES:
                    raise ProviderStreamError(
                        "buffer_limit",
                        f"upstream stream exceeded {MAX_PAYLOAD_BYTES} byte safety ceiling",
                    )
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._response.close()
        finally:
            self._connection.close()


def _bounded_http_body(response: http.client.HTTPResponse) -> bytes:
    """Read an error body with the same ceiling as ordinary responses."""
    body = response.read(MAX_PAYLOAD_BYTES + 1)
    enforce_payload_limit(len(body))
    return body


def _open_phase_connection(
    parsed: urllib.parse.SplitResult,
    timeouts: TransportTimeouts,
) -> http.client.HTTPConnection:
    """Connect direct HTTP(S), measuring socket and TLS handshakes separately.

    ``urllib`` exposes only one opaque timeout.  The native streaming path is
    direct by design so it can set an explicit timeout for TCP connect, TLS
    handshake, first response byte and every later idle interval. HTTP proxy
    tunnelling is not silently attempted by this path.
    """
    host = parsed.hostname
    if not host:
        raise ValueError("upstream URL has no hostname")
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"native streaming supports only http(s), got {scheme!r}")
    port = parsed.port or (443 if scheme == "https" else 80)
    raw_socket = socket.create_connection((host, port), timeout=timeouts.connect_timeout_seconds)
    try:
        if scheme == "https":
            raw_socket.settimeout(timeouts.tls_timeout_seconds)
            context = ssl.create_default_context()
            secure_socket = context.wrap_socket(raw_socket, server_hostname=host)
            connection: http.client.HTTPConnection = http.client.HTTPSConnection(host, port=port, context=context)
            connection.sock = secure_socket
        else:
            connection = http.client.HTTPConnection(host, port=port)
            connection.sock = raw_socket
        return connection
    except Exception:
        raw_socket.close()
        raise


def _request_target(parsed: urllib.parse.SplitResult) -> str:
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    return target


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
                body = exc.read(MAX_PAYLOAD_BYTES + 1)
                enforce_payload_limit(len(body))
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

    def open_stream(
        self,
        prepared: PreparedRequest,
        timeouts: TransportTimeouts,
        cancellation: Optional[RequestCancellation] = None,
    ) -> ProviderStreamResult:
        """Open a direct provider-native stream with phase-specific deadlines.

        The returned stream is raw bytes, not a normalized response.  This is
        intentional: protocol conversion after the first visible event could
        splice two model trajectories.  Callers may fail over before the first
        byte, but must emit a terminal interruption event after it.
        """
        validate_upstream_url(prepared.url, allow_localhost=env_flag(ALLOW_LOCAL_UPSTREAM_ENV))
        enforce_payload_limit(len(prepared.body_bytes))
        token = cancellation or RequestCancellation()
        started = time.monotonic()
        connection: Optional[http.client.HTTPConnection] = None
        try:
            if token.is_cancelled:
                raise ProviderStreamError("cancelled", token.reason or "stream cancelled before connect")
            parsed = urllib.parse.urlsplit(prepared.url)
            connection = _open_phase_connection(parsed, timeouts)
            token.add_callback(connection.close)
            elapsed_ms = (time.monotonic() - started) * 1000.0
            if elapsed_ms >= timeouts.total_timeout_ms:
                raise ProviderStreamError("total", "upstream stream exceeded its total deadline during setup")

            # Request bytes are bounded before the socket is opened. First-byte
            # timing starts after those bytes have been dispatched.
            sock = getattr(connection, "sock", None)
            if sock is not None:
                remaining_s = max(0.001, (timeouts.total_timeout_ms - elapsed_ms) / 1000.0)
                # A bounded request still needs a socket deadline while it is
                # written. We use the first-byte budget here because this
                # direct transport has no separate write phase configuration.
                sock.settimeout(min(timeouts.first_byte_timeout_seconds, remaining_s))
            connection.request(prepared.method, _request_target(parsed), body=prepared.body_bytes, headers=prepared.headers)
            elapsed_ms = (time.monotonic() - started) * 1000.0
            if elapsed_ms >= timeouts.total_timeout_ms:
                raise ProviderStreamError("total", "upstream stream exceeded its total deadline while sending request")
            sock = getattr(connection, "sock", None)
            if sock is not None:
                remaining_s = max(0.001, (timeouts.total_timeout_ms - elapsed_ms) / 1000.0)
                sock.settimeout(min(timeouts.first_byte_timeout_seconds, remaining_s))
            response = connection.getresponse()
            duration = (time.monotonic() - started) * 1000.0
            headers = {key.lower(): value for key, value in response.headers.items()}
            content_length = headers.get("content-length")
            if content_length is not None:
                try:
                    enforce_payload_limit(int(content_length))
                except ValueError:
                    # Bad Content-Length is handled by http.client while
                    # reading. Do not convert a valid chunked stream into a
                    # configuration error merely because an upstream lies.
                    pass
            if response.status != 200:
                try:
                    body = _bounded_http_body(response)
                finally:
                    connection.close()
                return ProviderStreamResult(
                    status_code=response.status,
                    headers=headers,
                    body=body,
                    duration_ms=duration,
                )
            return ProviderStreamResult(
                status_code=response.status,
                headers=headers,
                duration_ms=duration,
                stream=_HTTPResponseByteStream(response, connection, timeouts, token, started),
            )
        except CircuitBreakerGatewayError:
            if connection is not None:
                connection.close()
            raise
        except ProviderStreamError as exc:
            if connection is not None:
                connection.close()
            kind = "timeout" if exc.phase in {"total", "first_byte", "idle"} else "connection"
            return ProviderStreamResult(
                status_code=TRANSPORT_STATUS[kind],
                headers={},
                body=f"transport_error:{kind}: {exc}".encode("utf-8"),
                duration_ms=(time.monotonic() - started) * 1000.0,
                transport_error=kind,
            )
        except Exception as exc:
            if connection is not None:
                connection.close()
            kind = _transport_kind(exc)
            return ProviderStreamResult(
                status_code=TRANSPORT_STATUS[kind],
                headers={},
                body=f"transport_error:{kind}: {exc}".encode("utf-8"),
                duration_ms=(time.monotonic() - started) * 1000.0,
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
        # Force non-streaming for buffered execution
        payload["stream"] = False

        max_out = (endpoint.profile.max_output_tokens if endpoint.profile else 8192) or 8192
        if "max_tokens" in payload:
            payload["max_tokens"] = min(payload["max_tokens"], max_out)
        else:
            payload["max_tokens"] = min(max_out, 8192)

        if "max_completion_tokens" in payload:
            payload["max_completion_tokens"] = min(payload["max_completion_tokens"], max_out)

        if endpoint.provider.lower() == "groq":
            from durallm.pruner import prune_for_groq_tpm
            # Groq free tier strictly enforces OTPM of 1,000 and ITPM of 7,000 / 8,000 total TPM.
            # Clamp output tokens to at most 950 so OTPM is never violated.
            payload["max_tokens"] = min(payload.get("max_tokens", 950), 950)
            if "max_completion_tokens" in payload:
                payload["max_completion_tokens"] = min(payload["max_completion_tokens"], 950)
            # Compact input payload if needed so input + output <= 7,000.
            payload = prune_for_groq_tpm(payload, max_input_tokens=5200)

        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "durallm/0.2.1",
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
        body_text = result.body.decode("utf-8", errors="replace").strip()
        if not body_text:
            raise ValueError(f"Empty response body received from {endpoint.id} (status {result.status_code})")

        # Resilient handling for upstream SSE streams (data: lines)
        if body_text.startswith("data:") or "\ndata:" in body_text:
            from durallm.protocol.openai import sse_chunks_to_ir
            return sse_chunks_to_ir(body_text, default_model=endpoint.model)

        raw_json = json.loads(body_text)
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
        # Force non-streaming for buffered execution
        payload["stream"] = False
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "User-Agent": "durallm/0.2.1",
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
        body_text = result.body.decode("utf-8", errors="replace").strip()
        if not body_text:
            raise ValueError(f"Empty response body received from {endpoint.id} (status {result.status_code})")

        if body_text.startswith("event:") or "\nevent:" in body_text:
            return self._parse_anthropic_sse(body_text, endpoint.model)

        raw_json = json.loads(body_text)
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
                from durallm.protocol.ir import NormalizedToolCall
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

    def _parse_anthropic_sse(self, sse_text: str, default_model: str) -> NormalizedResponse:
        text_parts: List[str] = []
        reasoning = None
        signature = None
        tool_calls = []
        model = default_model
        msg_id = f"msg_{uuid.uuid4().hex[:12]}"
        input_tokens = 0
        output_tokens = 0
        stop_reason = "end_turn"

        current_block_type: Optional[str] = None
        current_tool_id: str = ""
        current_tool_name: str = ""
        current_tool_input_str: str = ""

        for raw_line in sse_text.splitlines():
            line = raw_line.strip()
            if not line or not line.startswith("data:"):
                continue
            data_str = line[len("data:"):].strip()
            try:
                data = json.loads(data_str)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            evt_type = data.get("type")
            if evt_type == "message_start":
                msg = data.get("message", {})
                if msg.get("id"):
                    msg_id = msg["id"]
                if msg.get("model"):
                    model = msg["model"]
                usage = msg.get("usage", {})
                input_tokens = usage.get("input_tokens", input_tokens)
            elif evt_type == "content_block_start":
                cb = data.get("content_block", {})
                current_block_type = cb.get("type")
                if current_block_type == "text":
                    text_parts.append(cb.get("text", ""))
                elif current_block_type == "thinking":
                    reasoning = cb.get("thinking", "")
                    signature = cb.get("signature")
                elif current_block_type == "tool_use":
                    current_tool_id = cb.get("id", f"toolu_{uuid.uuid4().hex[:8]}")
                    current_tool_name = cb.get("name", "")
                    current_tool_input_str = ""
            elif evt_type == "content_block_delta":
                delta = data.get("delta", {})
                d_type = delta.get("type")
                if d_type == "text_delta":
                    text_parts.append(delta.get("text", ""))
                elif d_type == "thinking_delta":
                    reasoning = (reasoning or "") + delta.get("thinking", "")
                elif d_type == "input_json_delta":
                    current_tool_input_str += delta.get("partial_json", "")
            elif evt_type == "content_block_stop":
                if current_block_type == "tool_use":
                    from durallm.protocol.ir import NormalizedToolCall
                    try:
                        args = json.loads(current_tool_input_str) if current_tool_input_str else {}
                    except Exception:
                        args = {}
                    tool_calls.append(
                        NormalizedToolCall(
                            id=current_tool_id,
                            name=current_tool_name,
                            arguments=args,
                            raw_arguments=current_tool_input_str,
                        )
                    )
                current_block_type = None
            elif evt_type == "message_delta":
                delta = data.get("delta", {})
                if delta.get("stop_reason"):
                    stop_reason = delta["stop_reason"]
                usage = data.get("usage", {})
                output_tokens = usage.get("output_tokens", output_tokens)

        finish_map = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length"}
        return NormalizedResponse(
            response_id=msg_id,
            model=model,
            content="".join(text_parts) if text_parts else None,
            reasoning_content=reasoning,
            reasoning_signature=signature,
            tool_calls=tool_calls,
            finish_reason=finish_map.get(stop_reason, "stop"),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            raw_response={"id": msg_id, "model": model, "content": text_parts},
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
            "User-Agent": "durallm/0.2.1",
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

    def register(self, provider: str, adapter: ProviderAdapter) -> None:
        """Install (or replace) the adapter used for `provider`."""
        self._adapters[provider.lower()] = adapter

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
