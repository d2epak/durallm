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


if __name__ == "__main__":
    unittest.main()
