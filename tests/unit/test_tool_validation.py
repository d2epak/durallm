"""Tests for Tool Call Validation and Semantic Safety (Rule 3 Compliance)."""

import unittest

from durallm.agent.tool_validation import (
    ToolCallResult,
    ToolCallValidator,
)


class TestToolCallValidation(unittest.TestCase):

    def setUp(self):
        self.validator = ToolCallValidator(strict=True, allow_syntactic_repairs=True, allow_semantic_repairs=False)
        self.schema = {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer"},
            },
            "required": ["command"],
            "additionalProperties": False,
        }

    def test_valid_tool_call_passes(self):
        report = self.validator.validate_tool_call(
            tool_name="bash",
            arguments={"command": "pytest -v", "timeout": 30},
            schema=self.schema,
            known_tools=["bash"],
        )
        self.assertEqual(report.status, ToolCallResult.VALID)
        self.assertTrue(report.is_executable)
        self.assertEqual(report.validated_arguments["command"], "pytest -v")

    def test_safe_syntactic_repair_of_markdown_fences_and_trailing_commas(self):
        # Malformed JSON with markdown fences and trailing commas
        raw_json = '```json\n{"command": "git status", "timeout": 10, }\n```'
        report = self.validator.validate_tool_call(
            tool_name="bash",
            arguments=raw_json,
            schema=self.schema,
            known_tools=["bash"],
        )
        self.assertEqual(report.status, ToolCallResult.NORMALIZED)
        self.assertTrue(report.is_executable)
        self.assertEqual(report.validated_arguments["command"], "git status")
        self.assertEqual(report.validated_arguments["timeout"], 10)
        self.assertIn("strip_markdown_fences", report.normalizations_applied)
        self.assertIn("strip_trailing_commas", report.normalizations_applied)

    def test_missing_required_property_fails_without_guessing(self):
        # Missing 'command' property: validator must reject rather than invent a command
        report = self.validator.validate_tool_call(
            tool_name="bash",
            arguments={"timeout": 10},
            schema=self.schema,
            known_tools=["bash"],
        )
        self.assertEqual(report.status, ToolCallResult.INVALID)
        self.assertFalse(report.is_executable)
        self.assertIn("Missing required argument", report.error_message)

    def test_unknown_tool_fails_closed(self):
        report = self.validator.validate_tool_call(
            tool_name="hallucinated_tool",
            arguments={"param": "val"},
            schema=self.schema,
            known_tools=["bash", "read_file"],
        )
        self.assertEqual(report.status, ToolCallResult.INVALID)
        self.assertFalse(report.is_executable)
        self.assertIn("not in active tool catalog", report.error_message)

    def test_unparseable_garbage_fails_closed_in_strict_mode(self):
        # Arbitrary unstructured natural language string: must not be coerced into {"command": ...}
        report = self.validator.validate_tool_call(
            tool_name="bash",
            arguments="I will now proceed to write code for the application",
            schema=self.schema,
            known_tools=["bash"],
        )
        self.assertEqual(report.status, ToolCallResult.UNSAFE_TO_REPAIR)
        self.assertFalse(report.is_executable)
        self.assertIn("semantic guessing is disabled", report.error_message)


if __name__ == "__main__":
    unittest.main()
