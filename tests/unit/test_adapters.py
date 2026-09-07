"""Provider adapters: transport failures, Anthropic decoding, provider lookup, SSRF boundary."""

import json
import os
import socket
import ssl
import unittest
import urllib.error
from unittest.mock import patch

from durallm.capability.profile import Endpoint
from durallm.classifier import classify_failure
from durallm.errors import CircuitBreakerGatewayError, ConfigurationError
from durallm.models import FailoverReason
from durallm.providers.adapters import AnthropicAdapter, OpenAICompatibleAdapter, ProviderAdapterRegistry
from durallm.providers.base import PreparedRequest, ProviderExecutionResult

PREPARED = PreparedRequest(url="https://api.example.com/v1/chat/completions", headers={}, body_bytes=b"{}")
ENV_NO_LOCAL = {k: v for k, v in os.environ.items() if k != "LLM_BREAKER_ALLOW_LOCAL_UPSTREAM"}


def _execute_with(exc):
    with patch("urllib.request.urlopen", side_effect=exc):
        return OpenAICompatibleAdapter("openai").execute(PREPARED, timeout_seconds=1.0)


class TestTransportErrors(unittest.TestCase):
    """Each failure class must be distinguishable by status and classify to its own reason."""

    def test_timeout(self):
        res = _execute_with(urllib.error.URLError(socket.timeout("timed out")))
        self.assertEqual((res.status_code, res.transport_error), (597, "timeout"))
        self.assertEqual(classify_failure(res.body.decode(), status_code=res.status_code).reason, FailoverReason.timeout)

    def test_connection_refused(self):
        res = _execute_with(urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")))
        self.assertEqual((res.status_code, res.transport_error), (598, "connection"))
        self.assertEqual(classify_failure(res.body.decode(), status_code=res.status_code).reason, FailoverReason.connection_refused)

    def test_tls_failure(self):
        res = _execute_with(urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed: self signed")))
        self.assertEqual((res.status_code, res.transport_error), (596, "tls"))
        self.assertEqual(classify_failure(res.body.decode(), status_code=res.status_code).reason, FailoverReason.ssl_cert_verification)

    def test_unknown_exception_keeps_599(self):
        res = _execute_with(ValueError("boom"))
        self.assertEqual((res.status_code, res.transport_error), (599, "unknown"))
        self.assertTrue(res.body.startswith(b"transport_error:unknown:"))


class TestAnthropicDecoding(unittest.TestCase):

    def test_all_text_blocks_are_kept(self):
        raw = {"id": "msg_1", "content": [{"type": "text", "text": "first "}, {"type": "text", "text": "second"}],
               "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 2}}
        ep = Endpoint(id="a", provider="anthropic", model="claude", base_url="https://api.anthropic.com")
        resp = AnthropicAdapter("anthropic").normalize_response(
            ep, ProviderExecutionResult(status_code=200, headers={}, body=json.dumps(raw).encode()))
        self.assertEqual(resp.content, "first second")


class TestAdapterLookup(unittest.TestCase):

    def test_unknown_provider_raises_instead_of_defaulting_to_openai(self):
        with self.assertRaises(ConfigurationError):
            ProviderAdapterRegistry().get_adapter("not-a-provider")

    def test_unknown_provider_with_declared_protocol_uses_that_protocol(self):
        reg = ProviderAdapterRegistry()
        self.assertIsInstance(reg.get_adapter("my-ollama", protocol="openai"), OpenAICompatibleAdapter)
        self.assertIsInstance(reg.get_adapter("bedrock-proxy", protocol="anthropic"), AnthropicAdapter)
        with self.assertRaises(ConfigurationError):
            reg.get_adapter("x", protocol="soap")


class TestPayloadCeiling(unittest.TestCase):

    def test_oversized_request_body_is_refused_before_any_network_call(self):
        big = PreparedRequest(url=PREPARED.url, headers={}, body_bytes=b"x" * 10_000_001)
        with patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(CircuitBreakerGatewayError):
                OpenAICompatibleAdapter("openai").execute(big, timeout_seconds=1.0)
            urlopen.assert_not_called()

    def test_oversized_response_body_is_refused(self):
        class FakeResponse:
            status = 200
            headers = {}
            def read(self, n=-1):
                return b"x" * (n if n and n > 0 else 10_000_001)
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        with patch("urllib.request.urlopen", return_value=FakeResponse()):
            with self.assertRaises(CircuitBreakerGatewayError):
                OpenAICompatibleAdapter("openai").execute(PREPARED, timeout_seconds=1.0)


class TestUpstreamUrlBoundary(unittest.TestCase):

    def test_loopback_upstream_is_refused_before_any_network_call(self):
        local = PreparedRequest(url="http://127.0.0.1:11434/v1/chat/completions", headers={}, body_bytes=b"{}")
        with patch.dict(os.environ, ENV_NO_LOCAL, clear=True), patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(CircuitBreakerGatewayError):
                OpenAICompatibleAdapter("openai").execute(local, timeout_seconds=1.0)
            urlopen.assert_not_called()

    def test_loopback_upstream_allowed_by_opt_in(self):
        local = PreparedRequest(url="http://localhost:11434/v1/chat/completions", headers={}, body_bytes=b"{}")
        with patch.dict(os.environ, {"LLM_BREAKER_ALLOW_LOCAL_UPSTREAM": "1"}):
            res = _execute_with(urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")))
            with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(socket.timeout("timed out"))):
                res = OpenAICompatibleAdapter("openai").execute(local, timeout_seconds=1.0)
            self.assertEqual(res.transport_error, "timeout")


class TestStreamingDemuxAndStreamFalse(unittest.TestCase):
    """Verify adapters enforce stream=False for buffered execution and demux SSE chunks safely."""

    def test_openai_stream_false_enforced(self):
        from durallm.protocol.ir import NormalizedRequest, NormalizedMessage
        req = NormalizedRequest(
            request_id="r1",
            model="gpt-4o",
            messages=[NormalizedMessage(role="user", content="hello")],
            stream=True,  # Client requested streaming
        )
        ep = Endpoint(id="groq-test", provider="groq", model="openai/gpt-oss-120b", base_url="https://api.groq.com/openai/v1")
        prep = OpenAICompatibleAdapter("groq").prepare_request(ep, req, api_key="test_key")
        body = json.loads(prep.body_bytes.decode("utf-8"))
        self.assertFalse(body.get("stream", False), "stream=False must be enforced for buffered execution")

    def test_anthropic_stream_false_enforced(self):
        from durallm.protocol.ir import NormalizedRequest, NormalizedMessage
        req = NormalizedRequest(
            request_id="r2",
            model="claude-3-7-sonnet-20250219",
            messages=[NormalizedMessage(role="user", content="hello")],
            stream=True,
        )
        ep = Endpoint(id="anthropic-test", provider="anthropic", model="claude-3-7-sonnet-20250219", base_url="https://api.anthropic.com")
        prep = AnthropicAdapter("anthropic").prepare_request(ep, req, api_key="test_key")
        body = json.loads(prep.body_bytes.decode("utf-8"))
        self.assertFalse(body.get("stream", False), "stream=False must be enforced for buffered execution")

    def test_openai_sse_stream_demux(self):
        sse_data = (
            'data: {"id":"chatcmpl-123","model":"qwen","choices":[{"index":0,"delta":{"content":"Hello ","reasoning":"Thinking step."}}]}\n\n'
            'data: {"id":"chatcmpl-123","model":"qwen","choices":[{"index":0,"delta":{"content":"world!"},"finish_reason":"stop"}]}\n\n'
            'data: [DONE]\n'
        )
        ep = Endpoint(id="qwen-ep", provider="groq", model="qwen/qwen3.6-27b", base_url="https://api.groq.com")
        res = ProviderExecutionResult(status_code=200, headers={"content-type": "text/event-stream"}, body=sse_data.encode("utf-8"))
        norm = OpenAICompatibleAdapter("groq").normalize_response(ep, res)
        self.assertEqual(norm.content, "Hello world!")
        self.assertEqual(norm.reasoning_content, "Thinking step.")
        self.assertEqual(norm.finish_reason, "stop")

    def test_anthropic_sse_stream_demux(self):
        sse_data = (
            'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_abc","model":"claude","usage":{"input_tokens":10}}}\n\n'
            'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
            'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"4"}}\n\n'
            'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
            'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\n\n'
            'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        )
        ep = Endpoint(id="claude-ep", provider="anthropic", model="claude-3-7-sonnet", base_url="https://api.anthropic.com")
        res = ProviderExecutionResult(status_code=200, headers={"content-type": "text/event-stream"}, body=sse_data.encode("utf-8"))
        norm = AnthropicAdapter("anthropic").normalize_response(ep, res)
        self.assertEqual(norm.content, "4")
        self.assertEqual(norm.input_tokens, 10)
        self.assertEqual(norm.output_tokens, 2)
        self.assertEqual(norm.finish_reason, "stop")

    def test_empty_body_raises_value_error_and_classifies_as_empty_completion(self):
        ep = Endpoint(id="test-ep", provider="groq", model="qwen", base_url="https://api.groq.com")
        res = ProviderExecutionResult(status_code=200, headers={}, body=b"   ")
        adapter = OpenAICompatibleAdapter("groq")
        with self.assertRaises(ValueError) as ctx:
            adapter.normalize_response(ep, res)
        classified = classify_failure(ctx.exception, status_code=502)
        self.assertEqual(classified.reason, FailoverReason.empty_completion)
        self.assertTrue(classified.retryable)


if __name__ == "__main__":
    unittest.main()

