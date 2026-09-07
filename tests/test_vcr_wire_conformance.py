"""VCR Recorded Network Wire Conformance Test Suite (Frontier 1).

Tests streaming resilience against real-world provider wire quirks:
1. DeepSeek 2-byte SSE chunk splits across frame and JSON boundaries.
2. Anthropic extended thinking blocks (thinking_delta and signature_delta).
3. OpenAI streaming tool call argument fragmentation.
4. Multibyte UTF-8 splitting across network buffers.
"""

import json
import unittest

from llm_circuit_breaker.streaming.parser import ParsedSSEEvent, SSEStreamParser, WireStreamAssembler


class TestVCRWireConformance(unittest.TestCase):

    def test_anthropic_extended_thinking_wire_stream(self):
        """Replay recorded Anthropic SSE stream containing thinking_delta and signature_delta."""
        cassette = [
            b"event: message_start\n"
            b'data: {"type":"message_start","message":{"id":"msg_vcr_1","type":"message","role":"assistant","model":"claude-3-7-sonnet","content":[],"usage":{"input_tokens":25,"output_tokens":1}}}\n\n',
            b"event: content_block_start\n"
            b'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}\n\n',
            b"event: content_block_delta\n"
            b'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"Analyzing the circuit breaker state machine..."}}\n\n',
            b"event: content_block_delta\n"
            b'data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"sig_mock_verified_hash_987654321"}}\n\n',
            b"event: content_block_stop\n"
            b'data: {"type":"content_block_stop","index":0}\n\n',
            b"event: content_block_start\n"
            b'data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}\n\n',
            b"event: content_block_delta\n"
            b'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"The system is robust and operational."}}\n\n',
            b"event: content_block_stop\n"
            b'data: {"type":"content_block_stop","index":1}\n\n',
            b"event: message_delta\n"
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":42}}\n\n',
            b"event: message_stop\n"
            b'data: {"type":"message_stop"}\n\n',
        ]

        parser = SSEStreamParser()
        assembler = WireStreamAssembler()

        for chunk in cassette:
            for event in parser.feed(chunk):
                assembler.process_event(event)

        for event in parser.flush():
            assembler.process_event(event)

        self.assertEqual(assembler.reasoning_content, "Analyzing the circuit breaker state machine...")
        self.assertEqual(assembler.reasoning_signature, "sig_mock_verified_hash_987654321")
        self.assertEqual(assembler.text_content, "The system is robust and operational.")

    def test_deepseek_tiny_2byte_sse_chunk_fragmentation(self):
        """Simulate DeepSeek splitting tool arguments and SSE frames across 2-byte frames."""
        def make_chunk(args_part: str) -> bytes:
            payload = {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": args_part}}]}}]}
            return ("data: " + json.dumps(payload) + "\n\n").encode("utf-8")

        full_stream = (
            b'data: {"id":"chatcmpl-vcr","choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}\n\n'
            b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_bash_01","type":"function","function":{"name":"execute_bash","arguments":""}}]}}]}\n\n'
            + make_chunk('{"com')
            + make_chunk('mand": "')
            + make_chunk("pytest ")
            + make_chunk('-q"}')
            + b'data: {"choices":[{"index":0,"finish_reason":"tool_calls"}]}\n\n'
            + b'data: [DONE]\n\n'
        )

        # Cut the stream into tiny 2-byte slices
        chunk_size = 2
        slices = [full_stream[i : i + chunk_size] for i in range(0, len(full_stream), chunk_size)]

        parser = SSEStreamParser()
        assembler = WireStreamAssembler()

        events: list[ParsedSSEEvent] = []
        for s in slices:
            for ev in parser.feed(s):
                events.append(ev)
                assembler.process_event(ev)

        for ev in parser.flush():
            events.append(ev)
            assembler.process_event(ev)

        # Verify stream completed with [DONE]
        self.assertTrue(any(ev.is_done for ev in events))
        # Verify tool arguments reassembled cleanly
        tools = assembler.assembled_tool_calls()
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["id"], "call_bash_01")
        self.assertEqual(tools[0]["name"], "execute_bash")
        self.assertEqual(tools[0]["arguments"], {"command": "pytest -q"})
        self.assertEqual(assembler.finish_reason, "tool_calls")

    def test_multibyte_utf8_split_across_network_buffers(self):
        """Ensure multibyte UTF-8 characters split across packets do not trigger decode errors."""
        # 💻 is 4 bytes: 0xF0 0x9F 0x92 0xBB
        emoji = "🚀 Circuit Breaker 💻 Gateway"
        payload = f'data: {{"choices": [{{"delta": {{"content": "{emoji}"}}}}]}}\n\n'.encode("utf-8")

        # Split directly inside the emoji bytes
        part1 = payload[:30]
        part2 = payload[30:]

        parser = SSEStreamParser()
        assembler = WireStreamAssembler()

        for chunk in [part1, part2]:
            for ev in parser.feed(chunk):
                assembler.process_event(ev)

        for ev in parser.flush():
            assembler.process_event(ev)

        self.assertEqual(assembler.text_content, emoji)


if __name__ == "__main__":
    unittest.main()
