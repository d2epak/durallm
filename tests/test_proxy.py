"""Tests for Circuit Breaker Gateway Proxy Handler and Synthetic Streaming."""

import io
import unittest
from unittest.mock import MagicMock

from durallm.proxy import CircuitBreakerGatewayHandler


class MockSocket:
    def __init__(self, data=b""):
        self.rfile = io.BytesIO(data)
        self.wfile = io.BytesIO()

    def makefile(self, mode, *args, **kwargs):
        if "r" in mode:
            return self.rfile
        return self.wfile

    def sendall(self, data):
        self.wfile.write(data)


class TestProxyHandler(unittest.TestCase):

    def test_synthetic_anthropic_stream_generation(self):
        handler = CircuitBreakerGatewayHandler.__new__(CircuitBreakerGatewayHandler)
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        anthropic_resp = {
            "id": "msg_test_123",
            "model": "claude-sonnet-4-6",
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Let me reason"},
                {"type": "text", "text": "Hello user"},
                {"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "echo 1"}}
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 20}
        }

        handler._emit_synthetic_anthropic_stream(anthropic_resp)
        output = handler.wfile.getvalue().decode("utf-8")

        self.assertIn("event: message_start", output)
        self.assertIn("event: content_block_start", output)
        self.assertIn("event: content_block_delta", output)
        self.assertIn("thinking_delta", output)
        self.assertIn("text_delta", output)
        self.assertIn("input_json_delta", output)
        self.assertIn("event: message_delta", output)
        self.assertIn("event: message_stop", output)


if __name__ == "__main__":
    unittest.main()
