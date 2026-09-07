"""Tests for OpenCode Fail-Closed Tool Schema Validation."""

import unittest

from durallm.agent.tool_validation import (
    ToolCallResult,
    ToolCallValidator,
)


class TestOpenCodeToolValidation(unittest.TestCase):
    """Verify Iron Rules 1 & 2 for tool calls from coding agents like OpenCode."""

    def setUp(self) -> None:
        self.validator = ToolCallValidator(strict=True, allow_syntactic_repairs=True)
        self.edit_schema = {
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_str": {"type": "string"},
                    "new_str": {"type": "string"},
                    "start_line": {"type": "integer"},
                },
                "required": ["path", "old_str", "new_str"],
                "additionalProperties": False,
            }
        }

    def test_iron_rule_1_fail_closed_on_missing_required_arguments(self) -> None:
        """Rule 1: Never guess or invent missing required properties; fail closed."""
        report = self.validator.validate_tool_call(
            tool_name="str_replace_editor",
            arguments={"path": "src/main.py", "new_str": "def run(): pass"},  # Missing old_str!
            schema=self.edit_schema,
        )
        self.assertEqual(report.status, ToolCallResult.INVALID)
        self.assertFalse(report.is_executable)
        self.assertIn("Missing required argument(s)", report.error_message or "")
        self.assertIn("old_str", report.error_message or "")

    def test_iron_rule_2_syntactic_repair_markdown_code_fences(self) -> None:
        """Rule 2: Strip markdown code fences around JSON payloads."""
        raw = """```json
{
  "path": "src/main.py",
  "old_str": "def old(): pass",
  "new_str": "def new(): pass"
}
```"""
        report = self.validator.validate_tool_call(
            tool_name="str_replace_editor",
            arguments=raw,
            schema=self.edit_schema,
        )
        self.assertTrue(report.is_executable)
        self.assertEqual(report.status, ToolCallResult.NORMALIZED)
        self.assertIn("strip_markdown_fences", report.normalizations_applied)
        self.assertEqual(report.validated_arguments["path"], "src/main.py")

    def test_iron_rule_2_syntactic_repair_trailing_commas(self) -> None:
        """Rule 2: Strip trailing commas in JSON object / array syntax."""
        raw = """{
  "path": "src/main.py",
  "old_str": "foo",
  "new_str": "bar",
}"""
        report = self.validator.validate_tool_call(
            tool_name="str_replace_editor",
            arguments=raw,
            schema=self.edit_schema,
        )
        self.assertTrue(report.is_executable)
        self.assertIn("strip_trailing_commas", report.normalizations_applied)
        self.assertEqual(report.validated_arguments["old_str"], "foo")

    def test_iron_rule_2_raw_multiline_newlines_in_code_string(self) -> None:
        """Rule 2: Allow unescaped newlines in code strings (strict=False)."""
        raw = '{"path": "src/main.py", "old_str": "foo", "new_str": "line1\nline2\nline3"}'
        report = self.validator.validate_tool_call(
            tool_name="str_replace_editor",
            arguments=raw,
            schema=self.edit_schema,
        )
        self.assertTrue(report.is_executable)
        self.assertEqual(report.validated_arguments["new_str"], "line1\nline2\nline3")

    def test_iron_rule_2_python_ast_dict_repair(self) -> None:
        """Rule 2: Safely convert single-quoted Python dict to JSON dict."""
        raw = "{'path': 'src/main.py', 'old_str': 'foo', 'new_str': 'bar'}"
        report = self.validator.validate_tool_call(
            tool_name="str_replace_editor",
            arguments=raw,
            schema=self.edit_schema,
        )
        self.assertTrue(report.is_executable)
        self.assertIn("python_dict_to_json", report.normalizations_applied)
        self.assertEqual(report.validated_arguments["path"], "src/main.py")

    def test_envelope_unwrapping(self) -> None:
        """Unwrap accidental parameter envelope emitted by model."""
        raw = {
            "parameters": {
                "path": "src/main.py",
                "old_str": "foo",
                "new_str": "bar",
            }
        }
        report = self.validator.validate_tool_call(
            tool_name="str_replace_editor",
            arguments=raw,
            schema=self.edit_schema,
        )
        self.assertTrue(report.is_executable)
        self.assertIn("unwrap_parameters_envelope", report.normalizations_applied)
        self.assertEqual(report.validated_arguments["path"], "src/main.py")

    def test_nested_schema_validation_fails_closed_on_missing_nested_field(self) -> None:
        """Nested schema must fail closed if nested object misses required property."""
        nested_schema = {
            "parameters": {
                "type": "object",
                "properties": {
                    "file_edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "target": {"type": "string"},
                                "replacement": {"type": "string"},
                            },
                            "required": ["target", "replacement"],
                        },
                    }
                },
                "required": ["file_edits"],
            }
        }

        # Invalid: second edit item is missing "replacement"
        bad_args = {
            "file_edits": [
                {"target": "a", "replacement": "b"},
                {"target": "missing_replacement_here"},
            ]
        }
        report = self.validator.validate_tool_call(
            tool_name="batch_edit",
            arguments=bad_args,
            schema=nested_schema,
        )
        self.assertEqual(report.status, ToolCallResult.INVALID)
        self.assertFalse(report.is_executable)
        self.assertIn("Missing required argument(s)", report.error_message or "")


if __name__ == "__main__":
    unittest.main()
