import unittest
from unittest.mock import patch

from llm_circuit_breaker.classifier import (
    FailoverReason,
    classify_api_error,
    classify_failure,
    parse_output_cap_from_error,
)
from llm_circuit_breaker.pools import RouteDefinition
from llm_circuit_breaker.router import UniversalFailoverRouter


class TestOutputCapHandling(unittest.TestCase):
    def test_parse_groq_output_cap(self):
        err = "`max_tokens` must be less than or equal to `16384`, the maximum value for `max_tokens` is less than the `context_window` for this model"
        cap = parse_output_cap_from_error(err)
        self.assertEqual(cap, 16384)

    def test_parse_alibaba_range_output_cap(self):
        err = "Range of max_tokens should be [1, 65536]"
        cap = parse_output_cap_from_error(err)
        self.assertEqual(cap, 65536)

    def test_parse_generic_model_max_tokens(self):
        err = "max_tokens (98304) exceeds model's maximum output tokens (65536)"
        cap = parse_output_cap_from_error(err)
        self.assertEqual(cap, 65536)

    def test_parse_anthropic_available_tokens(self):
        err = "prompt is 190000 tokens, available_tokens: 10000"
        cap = parse_output_cap_from_error(err)
        self.assertEqual(cap, 10000)

    def test_classify_groq_output_cap_error(self):
        err = Exception("Error code: 400 - `max_tokens` must be less than or equal to `16384`, the maximum value for `max_tokens` is less than the `context_window` for this model")
        classified = classify_api_error(err, status_code=400)
        self.assertEqual(classified.reason, FailoverReason.output_cap_exceeded)
        self.assertTrue(classified.should_fallback)
        self.assertTrue(classified.retryable)

    def test_classify_failure_groq_output_cap(self):
        err = Exception("`max_tokens` must be less than or equal to `16384`")
        failure = classify_failure(err, status_code=400)
        self.assertEqual(failure.reason, FailoverReason.output_cap_exceeded)
        self.assertFalse(failure.poisons_health)  # Request sizing issue does not poison provider health

    def test_classify_context_overflow(self):
        err = Exception("Error code: 400 - maximum context length is 128000 tokens, prompt is too long")
        classified = classify_api_error(err, status_code=400)
        self.assertEqual(classified.reason, FailoverReason.payload_too_large)
        self.assertTrue(classified.should_fallback)

    def test_dispatch_auto_clamps_and_retries(self):
        router = UniversalFailoverRouter(auto_discover_free=False)
        mock_route = RouteDefinition(
            id="test-groq",
            provider="groq",
            model="qwen/qwen3.8-27b",
            pool="coding",
            base_url="https://api.groq.com/openai/v1",
            api_format="openai",
            env_key="GROQ_API_KEY",
            context_length=32768,
        )

        groq_err_body = b"`max_tokens` must be less than or equal to `16384`"
        success_body = b'{"choices": [{"message": {"content": "Clamped successfully!"}}]}'

        calls = []

        def mock_exec(route, payload, **kwargs):
            calls.append(dict(payload))
            if len(calls) == 1:
                return 400, {}, groq_err_body
            return 200, {}, success_body

        with patch("llm_circuit_breaker.router.execute_upstream_request", side_effect=mock_exec):
            with patch.object(router.pool_manager, "select_route", return_value=mock_route):
                status, resp, route = router.dispatch("coding", {"max_tokens": 32768, "messages": []})

        self.assertEqual(status, 200)
        self.assertEqual(len(calls), 2)
        # First call sent oversized tokens
        self.assertEqual(calls[0]["max_tokens"], 32768)
        # Second call was clamped to cap - 64 = 16320
        self.assertEqual(calls[1]["max_tokens"], 16320)


if __name__ == "__main__":
    unittest.main()
