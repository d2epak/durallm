"""Protocol codecs must not drop or invent request/response fields in translation."""

import time
import unittest

from llm_circuit_breaker.protocol.anthropic import anthropic_request_to_ir, ir_to_anthropic_request, ir_to_anthropic_response
from llm_circuit_breaker.protocol.gemini import ir_to_gemini_request
from llm_circuit_breaker.protocol.ir import NormalizedMessage, NormalizedRequest, NormalizedResponse, NormalizedToolDefinition
from llm_circuit_breaker.protocol.openai import ir_to_openai_request, ir_to_openai_response, openai_request_to_ir

TOOL = NormalizedToolDefinition(name="bash", description="run", parameters={"type": "object", "properties": {}})


def request_with(tool_choice):
    return NormalizedRequest(messages=[NormalizedMessage(role="user", content="hi")], tools=[TOOL], tool_choice=tool_choice)


class TestToolChoice(unittest.TestCase):

    def test_anthropic_forced_tool_reaches_openai_and_gemini(self):
        ir = anthropic_request_to_ir({
            "model": "m", "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "bash", "description": "run", "input_schema": {"type": "object", "properties": {}}}],
            "tool_choice": {"type": "tool", "name": "bash"},
        })
        self.assertEqual(ir_to_openai_request(ir, "m")["tool_choice"], {"type": "function", "function": {"name": "bash"}})
        self.assertEqual(ir_to_anthropic_request(ir, "m")["tool_choice"], {"type": "tool", "name": "bash"})
        self.assertEqual(
            ir_to_gemini_request(ir, "m")["toolConfig"],
            {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["bash"]}},
        )

    def test_openai_required_maps_to_anthropic_any(self):
        ir = openai_request_to_ir({
            "model": "m", "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object", "properties": {}}}}],
            "tool_choice": "required",
        })
        self.assertEqual(ir_to_anthropic_request(ir, "m")["tool_choice"], {"type": "any"})
        self.assertEqual(ir_to_openai_request(ir, "m")["tool_choice"], "required")
        self.assertEqual(ir_to_gemini_request(ir, "m")["toolConfig"]["functionCallingConfig"]["mode"], "ANY")

    def test_tool_choice_is_omitted_without_tools(self):
        # Both APIs reject tool_choice when no tools are declared.
        ir = NormalizedRequest(messages=[NormalizedMessage(role="user", content="hi")], tool_choice="required")
        self.assertNotIn("tool_choice", ir_to_openai_request(ir, "m"))
        self.assertNotIn("tool_choice", ir_to_anthropic_request(ir, "m"))
        self.assertNotIn("toolConfig", ir_to_gemini_request(ir, "m"))

    def test_unknown_tool_choice_is_dropped_not_forwarded(self):
        self.assertNotIn("tool_choice", ir_to_openai_request(request_with({"type": "bogus"}), "m"))
        self.assertNotIn("tool_choice", ir_to_anthropic_request(request_with("bogus"), "m"))


class TestResponseTimestamps(unittest.TestCase):

    def test_created_is_a_unix_timestamp(self):
        before = int(time.time())
        created = ir_to_openai_response(NormalizedResponse(content="x"), "m")["created"]
        self.assertGreaterEqual(created, before)
        self.assertLessEqual(created, int(time.time()) + 1)


class TestThinkingBlocks(unittest.TestCase):

    def test_signed_thinking_block_round_trips_to_anthropic(self):
        ir = anthropic_request_to_ir({
            "model": "m", "max_tokens": 10,
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "plan", "signature": "sig123"},
                    {"type": "text", "text": "ok"},
                ]},
            ],
        })
        self.assertEqual(ir.messages[1].reasoning_signature, "sig123")
        block = ir_to_anthropic_request(ir, "m")["messages"][1]["content"][0]
        self.assertEqual(block, {"type": "thinking", "thinking": "plan", "signature": "sig123"})

    def test_unsigned_reasoning_is_not_sent_to_anthropic(self):
        # Reasoning that came from an OpenAI-style upstream has no signature; Anthropic would
        # reject it, so it must not be re-emitted in requests or responses.
        ir = NormalizedRequest(messages=[
            NormalizedMessage(role="user", content="hi"),
            NormalizedMessage(role="assistant", content="ok", reasoning_content="plan"),
        ])
        content = ir_to_anthropic_request(ir, "m")["messages"][1]["content"]
        self.assertEqual([b["type"] for b in content], ["text"])

        resp = ir_to_anthropic_response(NormalizedResponse(content="ok", reasoning_content="plan"), "m")
        self.assertEqual([b["type"] for b in resp["content"]], ["text"])

    def test_signed_reasoning_in_response_keeps_signature(self):
        resp = ir_to_anthropic_response(
            NormalizedResponse(content="ok", reasoning_content="plan", reasoning_signature="s"), "m")
        self.assertEqual(resp["content"][0], {"type": "thinking", "thinking": "plan", "signature": "s"})


if __name__ == "__main__":
    unittest.main()
