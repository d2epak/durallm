"""Tests for Streaming Modes, Synthetic SSE, and Provider Adapters."""

import unittest

from durallm.capability.profile import Endpoint
from durallm.health import HealthTelemetryStore
from durallm.protocol.ir import (
    NormalizedRequest,
    NormalizedResponse,
    NormalizedToolCall,
)
from durallm.providers import (
    GeminiAdapter,
)
from durallm.streaming import (
    synthesize_anthropic_sse,
    synthesize_openai_sse,
)


class TestStreamingAndProviders(unittest.TestCase):

    def test_synthetic_anthropic_sse_stream_generation(self):
        resp = NormalizedResponse(
            response_id="msg_stream_test",
            model="claude-3-7-sonnet",
            content="Hello world",
            reasoning_content="I should greet the user",
            tool_calls=[NormalizedToolCall(id="tc1", name="echo", arguments={"msg": "hi"})],
            finish_reason="tool_calls",
            input_tokens=10,
            output_tokens=25,
        )

        events = list(synthesize_anthropic_sse(resp, "claude-3-7-sonnet"))
        full_stream = "".join(events)

        self.assertIn("event: message_start", full_stream)
        self.assertIn("thinking_delta", full_stream)
        self.assertIn("I should greet the user", full_stream)
        self.assertIn("text_delta", full_stream)
        self.assertIn("Hello world", full_stream)
        self.assertIn("tool_use", full_stream)
        self.assertIn("input_json_delta", full_stream)
        self.assertIn("message_stop", full_stream)

    def test_synthetic_openai_sse_stream_generation(self):
        resp = NormalizedResponse(
            response_id="chatcmpl-stream-test",
            model="gpt-4o",
            content="OpenAI stream text",
            finish_reason="stop",
        )

        events = list(synthesize_openai_sse(resp, "gpt-4o"))
        full_stream = "".join(events)

        self.assertIn("chat.completion.chunk", full_stream)
        self.assertIn("OpenAI stream text", full_stream)
        self.assertIn("data: [DONE]", full_stream)

    def test_gemini_adapter_uses_secure_header_authentication(self):
        adapter = GeminiAdapter()
        ep = Endpoint(
            id="gemini-ep",
            provider="gemini",
            model="gemini-2.5-flash",
            base_url="https://generativelanguage.googleapis.com/v1beta",
        )
        req = NormalizedRequest(model="gemini-2.5-flash")

        prepared = adapter.prepare_request(ep, req, api_key="secret-gemini-key-123")

        # SECURE VERIFICATION: Key must be in headers, NEVER in URL query string!
        self.assertNotIn("secret-gemini-key-123", prepared.url)
        self.assertIn("x-goog-api-key", prepared.headers)
        self.assertEqual(prepared.headers["x-goog-api-key"], "secret-gemini-key-123")

    def test_health_telemetry_ema_latency_tracking(self):
        store = HealthTelemetryStore(ema_alpha=0.5)
        # First measurement seeds real observed latency without hardcoded bias
        store.record_success("ep-1", latency_ms=100.0)
        snap = store.get_or_create("ep-1")
        self.assertEqual(snap.ema_latency_ms, 100.0)
        self.assertFalse(snap.is_cold_start)

        # Second measurement applies alpha EMA: 0.5 * 200 + 0.5 * 100 = 150.0
        store.record_success("ep-1", latency_ms=200.0)
        snap = store.get_or_create("ep-1")
        self.assertEqual(snap.ema_latency_ms, 150.0)
        self.assertEqual(snap.total_calls, 2)
        self.assertEqual(snap.successful_calls, 2)

        # Record failure with cooldown
        store.record_failure("ep-1", latency_ms=500.0, cooldown_seconds=60.0)
        self.assertTrue(snap.is_in_cooldown)
        self.assertEqual(snap.consecutive_failures, 1)


if __name__ == "__main__":
    unittest.main()
