"""Unit tests for Security Hardening, Response Validation, and Credential Redaction."""

import unittest

from llm_circuit_breaker.errors import CircuitBreakerGatewayError
from llm_circuit_breaker.observability.logger import redact_sensitive_data
from llm_circuit_breaker.protocol.ir import (
    NormalizedRequest,
    NormalizedResponse,
    NormalizedToolCall,
    NormalizedToolDefinition,
)
from llm_circuit_breaker.security.defense import (
    enforce_payload_limit,
    sanitize_headers,
    validate_upstream_url,
)
from llm_circuit_breaker.validation.response import ResponseValidator


class TestSecurityAndValidation(unittest.TestCase):
    def test_ssrf_prevention(self):
        # Disallowed schemes
        with self.assertRaises(CircuitBreakerGatewayError):
            validate_upstream_url("file:///etc/passwd")

        with self.assertRaises(CircuitBreakerGatewayError):
            validate_upstream_url("ftp://malicious.com/exploit")

        # Cloud metadata service access blocked
        with self.assertRaises(CircuitBreakerGatewayError):
            validate_upstream_url("http://169.254.169.254/latest/meta-data")

        # Valid HTTPS endpoints allowed
        self.assertTrue(validate_upstream_url("https://api.groq.com/openai/v1"))
        self.assertTrue(validate_upstream_url("https://api.cerebras.ai/v1"))

    def test_private_and_loopback_hosts_blocked_by_default(self):
        # A misconfigured or attacker-supplied base_url must not reach the LAN or the host itself.
        for host in ("127.0.0.1", "localhost", "[::1]", "0.0.0.0", "10.0.0.5", "172.16.0.1", "172.31.255.9", "192.168.1.10"):
            with self.assertRaises(CircuitBreakerGatewayError, msg=host):
                validate_upstream_url(f"http://{host}:11434/v1")
        # 172.32.x.x is public, not RFC1918.
        self.assertTrue(validate_upstream_url("http://172.32.0.1/v1"))

    def test_allow_localhost_opt_in_never_unblocks_metadata_endpoints(self):
        self.assertTrue(validate_upstream_url("http://127.0.0.1:11434/v1", allow_localhost=True))
        self.assertTrue(validate_upstream_url("http://192.168.1.10:8080/v1", allow_localhost=True))
        for url in ("http://169.254.169.254/latest/meta-data", "http://metadata.google.internal/computeMetadata/v1"):
            with self.assertRaises(CircuitBreakerGatewayError, msg=url):
                validate_upstream_url(url, allow_localhost=True)

    def test_header_sanitization_prevents_crlf_injection(self):
        malicious_headers = {
            "Content-Type": "application/json\r\nSet-Cookie: session=hijacked",
            "X-Valid": "normal_value",
        }
        clean = sanitize_headers(malicious_headers)
        self.assertNotIn("\r", clean["Content-Type"])
        self.assertNotIn("\n", clean["Content-Type"])
        self.assertEqual(clean["Content-Type"], "application/jsonSet-Cookie: session=hijacked")
        self.assertEqual(clean["X-Valid"], "normal_value")

    def test_payload_limit_enforcement(self):
        enforce_payload_limit(1024, max_allowed_bytes=2048)
        with self.assertRaises(CircuitBreakerGatewayError):
            enforce_payload_limit(5000, max_allowed_bytes=2048)

    def test_default_payload_ceiling_is_10mb(self):
        enforce_payload_limit(10_000_000)
        with self.assertRaises(CircuitBreakerGatewayError):
            enforce_payload_limit(10_000_001)

    def test_response_validator_accepts_valid_response_and_reports_usage(self):
        validator = ResponseValidator()
        req = NormalizedRequest(model="test", messages=[])
        resp = NormalizedResponse(model="test", content="hello", tool_calls=[], input_tokens=12, output_tokens=3)

        result = validator.validate(resp, req)
        self.assertTrue(result.is_valid)
        self.assertIsNone(result.rejection_reason)
        self.assertIs(result.sanitized_response, resp)
        self.assertEqual((result.input_tokens, result.output_tokens), (12, 3))

    def test_response_validator_rejects_empty_200_response(self):
        validator = ResponseValidator()
        req = NormalizedRequest(model="test", messages=[])
        empty_resp = NormalizedResponse(model="test", content="", tool_calls=[])

        result = validator.validate(empty_resp, req)
        self.assertFalse(result.is_valid)
        self.assertEqual(result.rejection_reason, "empty_response")

    def test_response_validator_rejects_malformed_tool_call(self):
        validator = ResponseValidator()
        req = NormalizedRequest(
            model="test",
            messages=[],
            tools=[
                NormalizedToolDefinition(
                    name="bash",
                    description="Run bash command",
                    parameters={
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                    },
                )
            ],
        )

        # Missing required parameter "command"
        bad_tool_resp = NormalizedResponse(
            model="test",
            content=None,
            tool_calls=[NormalizedToolCall(id="call_1", name="bash", arguments={"wrong_arg": 123})],
        )

        result = validator.validate(bad_tool_resp, req)
        self.assertFalse(result.is_valid)
        self.assertEqual(result.rejection_reason, "malformed_tool_call")

    def test_credential_redaction(self):
        data = {
            "api_key": "sk-ant-api03-abcdef12345678901234567890",
            "authorization": "Bearer secret_token_xyz",
            "prompt": "Hello world from autonomous agent turn",
            "nested": {
                "user_password": "super_secret_pw",
                "normal_field": 12345,
            },
        }
        redacted = redact_sensitive_data(data)
        self.assertEqual(redacted["api_key"], "[REDACTED]")
        self.assertEqual(redacted["authorization"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["user_password"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["normal_field"], 12345)

    def test_redaction_leaves_token_counts_alone_but_catches_key_variants(self):
        # The old unanchored pattern turned every usage counter into "[REDACTED]".
        data = {"input_tokens": 12, "max_tokens": 4096, "output_tokens": 3, "tokens_input": 7,
                "access_token": "abc", "secret_key": "def", "X-Api-Key": "ghi"}
        redacted = redact_sensitive_data(data)
        self.assertEqual({k: redacted[k] for k in ("input_tokens", "max_tokens", "output_tokens", "tokens_input")},
                         {"input_tokens": 12, "max_tokens": 4096, "output_tokens": 3, "tokens_input": 7})
        for k in ("access_token", "secret_key", "X-Api-Key"):
            self.assertEqual(redacted[k], "[REDACTED]", k)

    def test_secret_values_inside_free_text_are_masked_in_place(self):
        msg = "401 from groq: invalid key gsk_abcdefghijklmnopqrstuvwxyz0123 (header Bearer tok_123)"
        out = redact_sensitive_data(msg)
        self.assertNotIn("gsk_abcdefghijklmnopqrstuvwxyz0123", out)
        self.assertNotIn("tok_123", out)
        self.assertIn("401 from groq: invalid key [REDACTED_API_KEY]", out)


if __name__ == "__main__":
    unittest.main()
