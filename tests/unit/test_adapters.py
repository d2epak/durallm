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


if __name__ == "__main__":
    unittest.main()
