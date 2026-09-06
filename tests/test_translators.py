"""Tests for Bidirectional Protocol Translators, Gemini Adapters & Tool Repair."""

import json
import unittest

from llm_circuit_breaker.translators import (
    anthropic_to_openai_request,
    clean_gemini_schema,
    openai_to_anthropic_response,
    repair_json_string,
)


class TestTranslators(unittest.TestCase):

    def test_clean_gemini_schema_strips_forbidden_keys(self):
        raw_schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "title": "BashCommand",
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run",
                    "default": "ls"
                },
                "timeout": {
                    "type": "integer"
                }
            },
            "required": ["command"]
        }
        cleaned = clean_gemini_schema(raw_schema)
        self.assertNotIn("$schema", cleaned)
        self.assertNotIn("additionalProperties", cleaned)
        self.assertNotIn("title", cleaned)
        self.assertEqual(cleaned["type"], "OBJECT")
        self.assertEqual(cleaned["properties"]["command"]["type"], "STRING")
        self.assertNotIn("default", cleaned["properties"]["command"])
        self.assertEqual(cleaned["properties"]["timeout"]["type"], "INTEGER")
        self.assertEqual(cleaned["required"], ["command"])

    def test_repair_json_string_fences_and_trailing_commas(self):
        fenced = '```json\n{"path": "test.py", "lines": [1, 2, 3, ], }\n```'
        repaired = repair_json_string(fenced)
        data = json.loads(repaired)
        self.assertEqual(data["path"], "test.py")
        self.assertEqual(data["lines"], [1, 2, 3])

    def test_repair_json_string_returns_none_for_unrepairable_input(self):
        # Previously this became {"command": "rm -rf / && echo"} - a fabricated invocation.
        self.assertIsNone(repair_json_string("rm -rf / && echo"))
        self.assertIsNone(repair_json_string('"just a string"'))
        self.assertEqual(repair_json_string(""), "{}")

    def test_unparseable_tool_arguments_do_not_become_a_tool_use_block(self):
        openai_resp = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": "{command: rm -rf /"},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        out = openai_to_anthropic_response(openai_resp, "claude-sonnet-4-6")
        types = [b["type"] for b in out["content"]]
        self.assertNotIn("tool_use", types)
        self.assertIn("unparseable tool_call arguments", out["content"][0]["text"])
        self.assertEqual(out["stop_reason"], "end_turn")

    def test_anthropic_to_openai_request_translation(self):
        anthropic_req = {
            "model": "claude-sonnet-4-6",
            "system": "You are a coding agent.",
            "messages": [
                {"role": "user", "content": "Execute pytest"},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "Starting tests..."},
                    {"type": "tool_use", "id": "tool_123", "name": "bash", "input": {"command": "pytest -v"}}
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tool_123", "content": "3 passed"}
                ]}
            ],
            "tools": [{
                "name": "bash",
                "description": "Run bash",
                "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}
            }]
        }
        openai_req = anthropic_to_openai_request(anthropic_req, "auto-coding-agent")

        self.assertEqual(openai_req["messages"][0]["role"], "system")
        self.assertEqual(openai_req["messages"][1]["role"], "user")
        self.assertEqual(openai_req["messages"][2]["role"], "assistant")
        self.assertEqual(openai_req["messages"][2]["tool_calls"][0]["id"], "tool_123")
        self.assertEqual(openai_req["messages"][3]["role"], "tool")
        self.assertEqual(openai_req["messages"][3]["name"], "bash")
        self.assertEqual(openai_req["messages"][3]["tool_call_id"], "tool_123")
        self.assertEqual(openai_req["messages"][3]["content"], "3 passed")
        self.assertEqual(openai_req["tools"][0]["function"]["name"], "bash")

    def test_openai_to_anthropic_response_translation(self):
        openai_resp = {
            "id": "chatcmpl-test",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "reasoning_content": "Detailed reasoning block",
                    "content": "Running command now",
                    "tool_calls": [{
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command": "ls -l"}'}
                    }]
                },
                "finish_reason": "tool_calls"
            }],
            "usage": {"prompt_tokens": 50, "completion_tokens": 30}
        }
        anthropic_resp = openai_to_anthropic_response(openai_resp, "claude-sonnet-4-6")

        self.assertEqual(anthropic_resp["role"], "assistant")
        self.assertEqual(anthropic_resp["stop_reason"], "tool_use")
        blocks = anthropic_resp["content"]
        self.assertEqual(blocks[0]["type"], "thinking")
        self.assertEqual(blocks[0]["thinking"], "Detailed reasoning block")
        self.assertEqual(blocks[1]["type"], "text")
        self.assertEqual(blocks[1]["text"], "Running command now")
        self.assertEqual(blocks[2]["type"], "tool_use")
        self.assertEqual(blocks[2]["id"], "call_abc")
        self.assertEqual(blocks[2]["input"]["command"], "ls -l")


if __name__ == "__main__":
    unittest.main()
