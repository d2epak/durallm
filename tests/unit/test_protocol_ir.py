"""Tests for Normalized Protocol Intermediate Representation (IR) and Adapters."""

import unittest

from llm_circuit_breaker.protocol import (
    NormalizedMessage,
    NormalizedRequest,
    NormalizedToolCall,
    NormalizedToolDefinition,
    anthropic_request_to_ir,
    ir_to_anthropic_response,
    ir_to_gemini_request,
    ir_to_openai_request,
    openai_response_to_ir,
)


class TestProtocolIR(unittest.TestCase):

    def test_anthropic_to_ir_to_openai_roundtrip(self):
        anthropic_req = {
            "model": "claude-sonnet-4-6",
            "system": "You are a senior python engineer.",
            "messages": [
                {"role": "user", "content": "Run the tests"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "I should call bash pytest."},
                        {"type": "text", "text": "Executing tests..."},
                        {"type": "tool_use", "id": "tool_123", "name": "bash", "input": {"cmd": "pytest"}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tool_123", "content": "10 passed"},
                    ],
                },
            ],
            "tools": [
                {
                    "name": "bash",
                    "description": "Execute bash shell command",
                    "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}},
                }
            ],
            "max_tokens": 4096,
            "temperature": 0.2,
        }

        ir = anthropic_request_to_ir(anthropic_req)
        self.assertEqual(ir.system_instruction, "You are a senior python engineer.")
        self.assertEqual(len(ir.messages), 3)
        self.assertEqual(ir.messages[1].role, "assistant")
        self.assertEqual(ir.messages[1].reasoning_content, "I should call bash pytest.")
        self.assertEqual(len(ir.messages[1].tool_calls), 1)
        self.assertEqual(ir.messages[1].tool_calls[0].id, "tool_123")
        self.assertEqual(ir.messages[1].tool_calls[0].arguments, {"cmd": "pytest"})
        self.assertEqual(len(ir.tools), 1)

        # Convert IR to OpenAI
        openai_req = ir_to_openai_request(ir, "gpt-4o")
        self.assertEqual(openai_req["model"], "gpt-4o")
        self.assertEqual(openai_req["messages"][0]["role"], "system")
        self.assertEqual(openai_req["messages"][0]["content"], "You are a senior python engineer.")
        self.assertEqual(openai_req["messages"][2]["role"], "assistant")
        self.assertEqual(openai_req["messages"][2]["tool_calls"][0]["id"], "tool_123")
        self.assertEqual(openai_req["messages"][3]["role"], "tool")
        self.assertEqual(openai_req["messages"][3]["name"], "bash")
        self.assertEqual(openai_req["messages"][3]["content"], "10 passed")

    def test_openai_to_ir_to_anthropic_response_roundtrip(self):
        openai_resp = {
            "id": "chatcmpl-999",
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Running task...",
                        "reasoning_content": "Deep reasoning here",
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path": "main.py"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 150, "completion_tokens": 75},
        }

        ir_resp = openai_response_to_ir(openai_resp)
        self.assertEqual(ir_resp.content, "Running task...")
        self.assertEqual(ir_resp.reasoning_content, "Deep reasoning here")
        self.assertEqual(len(ir_resp.tool_calls), 1)
        self.assertEqual(ir_resp.tool_calls[0].id, "call_abc")
        self.assertEqual(ir_resp.tool_calls[0].arguments, {"path": "main.py"})

        anthropic_resp = ir_to_anthropic_response(ir_resp, "claude-3-7-sonnet")
        self.assertEqual(anthropic_resp["role"], "assistant")
        self.assertEqual(anthropic_resp["stop_reason"], "tool_use")
        # OpenAI-style reasoning carries no Anthropic signature, so it must not become a
        # thinking block: Anthropic rejects unsigned thinking blocks when they are echoed back.
        self.assertEqual([b["type"] for b in anthropic_resp["content"]], ["text", "tool_use"])
        self.assertEqual(anthropic_resp["content"][0]["text"], "Running task...")
        self.assertEqual(anthropic_resp["content"][1]["id"], "call_abc")
        self.assertEqual(anthropic_resp["content"][1]["input"], {"path": "main.py"})

    def test_gemini_request_and_response_translation(self):
        ir = NormalizedRequest(
            model="gemini-2.5-flash",
            system_instruction="Be concise.",
            messages=[
                NormalizedMessage(role="user", content="List directory"),
                NormalizedMessage(
                    role="assistant",
                    content="",
                    tool_calls=[NormalizedToolCall(id="c1", name="ls", arguments={"dir": "."})],
                ),
            ],
            tools=[
                NormalizedToolDefinition(
                    name="ls",
                    description="List files",
                    parameters={
                        "$schema": "http://json-schema.org/draft-07/schema#",
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"dir": {"type": "string"}},
                    },
                )
            ],
        )

        gemini_payload = ir_to_gemini_request(ir, "gemini-2.5-flash")
        self.assertIn("contents", gemini_payload)
        self.assertIn("tools", gemini_payload)
        decl = gemini_payload["tools"][0]["functionDeclarations"][0]
        self.assertNotIn("$schema", decl["parameters"])
        self.assertNotIn("additionalProperties", decl["parameters"])
        self.assertEqual(decl["parameters"]["type"], "OBJECT")


if __name__ == "__main__":
    unittest.main()
